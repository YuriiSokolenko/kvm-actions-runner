import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { ruffArgs } from './ruff-spec.mjs';
import { duplicatePackageRootDiagnostics } from './package-root-check.mjs';
import { expandCommand, projectConfig } from './project-config.mjs';
import { createDockerSandboxBackend } from './run-check-docker-backend.mjs';
import { appendDiagnostic } from './diagnostics-artifact.mjs';

/**
 * Backend-neutral focused verification for agents that have no unrestricted
 * shell. Requests are a closed, structured contract (never a command string);
 * results are bounded, machine-readable, and carry parsed diagnostics plus a
 * stdout/stderr tail so a failure is something the agent can debug from.
 *
 * This never replaces the authoritative validation in `product-checks.mjs`;
 * both use the repository-owned Ruff spec.
 */

export const CHECK_KINDS = Object.freeze(['python_compile', 'ruff', 'pytest', 'node_test', 'profile']);

// `infra_error` means the runner could not run the check at all (sandbox or tool missing/broken).
// It is never a verdict on the agent's change, unlike `fail`, and must not be answered with a retry or a shell.
export const CHECK_STATUSES = Object.freeze(['pass', 'fail', 'timeout', 'invalid', 'infra_error']);

const MAX_PATHS = 20;
const MAX_DIAGNOSTICS = 20;
const MAX_MESSAGE_CHARS = 400;
const TAIL_CHARS = 3000;
const CAPTURE_LIMIT_BYTES = 4 * 1024 * 1024;
const OUTPUT_SPOOL_LIMIT_BYTES = 32 * 1024 * 1024;
const DEFAULT_TIMEOUT_SECONDS = 120;
const MAX_TIMEOUT_SECONDS = 600;
const PREFLIGHT_TIMEOUT_MS = 15000;

// Versioned producer/executor handshake. Preflight must prove that the deployed trusted executor
// accepts exactly this request-side environment vocabulary before any model work begins.
export const RUN_CHECK_ENV_CONTRACT = Object.freeze({
  version: 2,
  keys: Object.freeze([
    'HOME',
    'LANG',
    'LC_ALL',
    'PATH',
    'PI_TRUSTED_ACCEPTANCE_BASELINE_TARGETS',
    'PI_TRUSTED_ACCEPTANCE_TARGETS',
    'PYTHONDONTWRITEBYTECODE',
    'PYTHONIOENCODING',
    'TMPDIR',
  ]),
});

// Only optional caller values from this contract are copied; fixed keys below are runtime-owned.
const ENV_ALLOWLIST = [
  'PATH',
  'LANG',
  'LC_ALL',
  'PI_TRUSTED_ACCEPTANCE_TARGETS',
  'PI_TRUSTED_ACCEPTANCE_BASELINE_TARGETS',
];

// Compiles in memory so a focused check never writes __pycache__ into the worktree.
const PYTHON_COMPILE_SCRIPT = [
  'import json, sys',
  'bad = 0',
  'for name in sys.argv[1:]:',
  '    try:',
  '        compile(open(name, "rb").read(), name, "exec")',
  '    except SyntaxError as e:',
  '        bad += 1',
  '        print(json.dumps({"file": name, "line": e.lineno, "column": e.offset, "message": e.msg}))',
  'sys.exit(1 if bad else 0)',
].join('\n');

/** Named repository profiles. Fixed argv only; no model-supplied text reaches a shell. */
// Profiles are project configuration (`checks.profiles` in .agent-harness.json):
// fixed argv only, so a caller can pick a name but never supply a command.
export const PROFILES = Object.freeze(Object.fromEntries(
  Object.entries(projectConfig().checks.profiles).map(([name, spec]) => [name, root => expandCommand(spec, root)]),
));

class InvalidCheck extends Error {
  constructor(message) {
    super(message);
    this.name = 'InvalidCheck';
  }
}

// Namespace-independent lexical containment. The executor can apply this to
// the runner root before validating existence and symlinks in its staged copy.
function relativeCheckPath(root, requested, canonicalRoot = root) {
  if (typeof requested !== 'string' || !requested.trim() || requested.includes('\0')) {
    throw new InvalidCheck('path entries must be non-empty strings');
  }
  let relative = path.relative(path.resolve(root), path.resolve(root, requested)) || '.';
  if (path.isAbsolute(requested) && (relative === '..' || relative.startsWith(`..${path.sep}`))) {
    relative = path.relative(path.resolve(canonicalRoot), requested) || '.';
  }
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new InvalidCheck(`path escapes the current worktree: ${requested}`);
  }
  if (requested.startsWith('-') || relative.startsWith('-')) {
    throw new InvalidCheck(`path must not start with "-": ${requested}`);
  }
  return relative;
}

/** Normalize model paths without accessing a different filesystem namespace. */
export function normalizeRunCheckPaths(root, params, canonicalRoot = root) {
  if (params?.kind === 'python_compile' || params?.kind === 'ruff') {
    return { ...params, paths: Array.isArray(params.paths)
      ? params.paths.map(item => relativeCheckPath(root, item, canonicalRoot)) : params.paths };
  }
  if (params?.kind === 'pytest' || params?.kind === 'node_test') {
    return { ...params, targets: Array.isArray(params.targets) ? params.targets.map(target => {
      if (typeof target !== 'string') throw new InvalidCheck('targets must be strings');
      const [file, ...selectors] = target.split('::');
      return [relativeCheckPath(root, file, canonicalRoot), ...selectors].join('::');
    }) : params.targets };
  }
  return params;
}

function invalid(kind, message) {
  return {
    status: 'invalid',
    kind: typeof kind === 'string' ? kind : null,
    exit_code: null,
    duration_ms: 0,
    summary: message,
    diagnostics: [],
    stdout_tail: '',
    stderr_tail: '',
    truncated: false,
  };
}

const INFRA_GUIDANCE = 'This is a runner infrastructure failure, not a verification result for your change. '
  + 'Do not retry it, do not look for a shell or bash workaround, and do not treat the code as failing; '
  + 'report it as an infrastructure blocker.';

/**
 * Structured runner-infrastructure failure. `info` = { component, code, command, message }, where
 * component is 'sandbox' or 'check_command'. `extra` may carry run details (tails, duration, kind fields).
 */
function infraError(kind, info, extra = {}) {
  return {
    status: 'infra_error',
    kind: typeof kind === 'string' ? kind : null,
    exit_code: null,
    duration_ms: 0,
    summary: `INFRASTRUCTURE ERROR: ${info.message}. ${INFRA_GUIDANCE}`,
    infrastructure: { component: info.component, code: info.code, command: info.command ?? null },
    diagnostics: [],
    stdout_tail: '',
    stderr_tail: '',
    truncated: false,
    ...extra,
  };
}

function rejectUnknownFields(params, allowed) {
  for (const key of Object.keys(params)) {
    if (!allowed.includes(key)) throw new InvalidCheck(`unsupported field "${key}"; allowed: ${allowed.join(', ')}`);
  }
}

function containedRelativePath(root, requested, { mustBeFile = false } = {}) {
  // Normalize against the caller's root first (which may itself be a
  // symlink, e.g. /tmp on macOS), then check the actual filesystem boundary.
  const relative = relativeCheckPath(root, requested);
  const worktree = fs.realpathSync(root);
  const absolute = path.resolve(worktree, relative);
  if (!fs.existsSync(absolute)) throw new InvalidCheck(`path does not exist: ${requested}`);
  const real = fs.realpathSync(absolute);
  if (real !== worktree && !real.startsWith(`${worktree}${path.sep}`)) {
    throw new InvalidCheck(`path resolves outside the current worktree: ${requested}`);
  }
  if (mustBeFile && !fs.statSync(real).isFile()) throw new InvalidCheck(`path is not a file: ${requested}`);
  return path.relative(worktree, absolute) || '.';
}

function pathList(root, value, field, options) {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_PATHS) {
    throw new InvalidCheck(`${field} must be an array of 1-${MAX_PATHS} strings`);
  }
  return value.map(item => containedRelativePath(root, item, options));
}

// Target paths are never commands: they must be real workspace entries and must match
// the chosen test runner before either the runner or recovery ledger sees a failure.
function focusedTestTargets(root, targets, kind) {
  if (!Array.isArray(targets) || targets.length < 1 || targets.length > MAX_PATHS) {
    throw new InvalidCheck(`targets must be an array of 1-${MAX_PATHS} strings`);
  }
  return targets.map(target => {
    if (typeof target !== 'string') throw new InvalidCheck('targets must be strings');
    const [file, ...selectors] = target.split('::');
    if (kind === 'node_test' && selectors.length) {
      throw new InvalidCheck('node_test takes .test.mjs/.test.js file paths without pytest :: selectors');
    }
    const relative = containedRelativePath(root, file, { mustBeFile: kind === 'node_test' });
    if (kind === 'node_test') {
      if (!/\.test\.(?:mjs|js)$/.test(relative)) {
        throw new InvalidCheck('node_test accepts .test.mjs or .test.js files only; use kind=pytest for Python .py tests');
      }
      return relative;
    }
    if (fs.statSync(path.resolve(root, relative)).isFile() && !relative.endsWith('.py')) {
      throw new InvalidCheck('pytest accepts Python .py test files only; use kind=node_test for .test.mjs/.test.js files');
    }
    if (selectors.length && !fs.statSync(path.resolve(root, relative)).isFile()) {
      throw new InvalidCheck('pytest :: selectors require a Python .py test file');
    }
    return [relative, ...selectors].join('::');
  });
}

function commandFor(root, params, bins) {
  switch (params.kind) {
    case 'python_compile': {
      rejectUnknownFields(params, ['kind', 'paths']);
      const files = pathList(root, params.paths, 'paths', { mustBeFile: true });
      if (files.some(file => !file.endsWith('.py'))) throw new InvalidCheck('python_compile accepts .py files only');
      return { command: bins.python, args: ['-c', PYTHON_COMPILE_SCRIPT, ...files] };
    }
    case 'ruff': {
      rejectUnknownFields(params, ['kind', 'paths']);
      const files = pathList(root, params.paths, 'paths');
      return { command: bins.ruff, args: ruffArgs(root, files, { json: true }) };
    }
    case 'pytest': {
      rejectUnknownFields(params, ['kind', 'targets']);
      const targets = focusedTestTargets(root, params.targets, 'pytest');
      return {
        command: bins.pytest,
        args: ['-q', '--tb=short', '-rfE', '--no-header', '-p', 'no:cacheprovider', ...targets],
      };
    }
    case 'node_test': {
      rejectUnknownFields(params, ['kind', 'targets']);
      const targets = focusedTestTargets(root, params.targets, 'node_test');
      return { command: bins.node, args: ['--test', '--test-reporter=tap', ...targets] };
    }
    case 'profile': {
      rejectUnknownFields(params, ['kind', 'profile']);
      const build = Object.hasOwn(PROFILES, params.profile) ? PROFILES[params.profile] : null;
      if (!build) throw new InvalidCheck(`unknown profile; allowed: ${Object.keys(PROFILES).join(', ')}`);
      const spec = build(root);
      return { ...spec, command: spec.command === 'pytest' ? bins.pytest : spec.command };
    }
    default:
      throw new InvalidCheck(`unknown check kind; allowed: ${CHECK_KINDS.join(', ')}`);
  }
}

/** Validate the closed request contract and construct its trusted fixed-argv command. */
export function buildRunCheckSpec(root, params, options = {}) {
  if (!params || typeof params !== 'object' || Array.isArray(params)) throw new InvalidCheck('request must be an object');
  const env = options.env ?? process.env;
  const bins = {
    python: options.bins?.python ?? env.PI_PYTHON_BIN ?? 'python3',
    ruff: options.bins?.ruff ?? 'ruff',
    pytest: options.bins?.pytest ?? 'pytest',
    node: options.bins?.node ?? 'node',
  };
  const request = normalizeRunCheckPaths(root, params, fs.realpathSync(root));
  return { request, spec: commandFor(root, request, bins) };
}

function checkEnv(env) {
  const clean = { PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8', HOME: '/tmp', TMPDIR: '/tmp' };
  for (const name of ENV_ALLOWLIST) if (env[name] != null) clean[name] = env[name];
  return clean;
}

/** No focused check runs without OS-enforced network and filesystem isolation. */
function isolatedCommand(root, spec) {
  const worktree = fs.realpathSync(root);
  // Linux uses the trusted Docker executor below. Never invoke nested bwrap in
  // the runner container, where namespace creation is blocked by production policy.
  if (process.platform === 'darwin') {
    // Keep the interpreter/toolchain readable while denying home credentials
    // and every network socket. The worktree is the sole exception in HOME.
    const home = os.homedir().replaceAll('\\', '\\\\').replaceAll('"', '\\"');
    const allowed = worktree.replaceAll('\\', '\\\\').replaceAll('"', '\\"');
    const profile = `(version 1) (allow default) (deny network*) (deny file-read* (subpath "${home}")) (allow file-read* (subpath "${allowed}"))`;
    return { command: '/usr/bin/sandbox-exec', args: ['-p', profile, spec.command, ...spec.args] };
  }
  return null;
}

function cap(text, limit) {
  const value = String(text ?? '');
  return { text: value.length > limit ? value.slice(-limit) : value, truncated: value.length > limit };
}

function shorten(text) {
  const value = String(text ?? '').trim();
  return value.length > MAX_MESSAGE_CHARS ? `${value.slice(0, MAX_MESSAGE_CHARS)}…` : value;
}

/** Bounded, deterministic subprocess run. Kills the whole process tree on timeout. */
function execute({ command, args, cwd, env, timeoutMs }) {
  return new Promise((resolve) => {
    const started = Date.now();
    const chunks = { stdout: [], stderr: [] };
    let fullOutputDir = null;
    let fullOutputPaths = null;
    try {
      fullOutputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-run-check-output-'));
      fullOutputPaths = { stdout: path.join(fullOutputDir, 'stdout'), stderr: path.join(fullOutputDir, 'stderr') };
    } catch { /* The bounded in-memory tails remain available if spooling cannot start. */ }
    let fullOutputCaptureFailed = false;
    const captureFailedByStream = { stdout: false, stderr: false };
    const spooledBytes = { stdout: 0, stderr: 0 };
    const totalBytes = { stdout: 0, stderr: 0 };
    const spoolTruncated = { stdout: false, stderr: false };
    const sizes = { stdout: 0, stderr: 0 };
    let dropped = false;
    let timedOut = false;
    let settled = false;

    const child = spawn(command, args, { cwd, env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const collect = (name) => (data) => {
      totalBytes[name] += data.length;
      const remaining = Math.max(0, OUTPUT_SPOOL_LIMIT_BYTES - spooledBytes[name]);
      const spoolChunk = data.subarray(0, remaining);
      if (data.length > remaining) spoolTruncated[name] = true;
      if (spoolChunk.length && fullOutputPaths) {
        try {
          fs.appendFileSync(fullOutputPaths[name], spoolChunk);
          spooledBytes[name] += spoolChunk.length;
        } catch {
          fullOutputCaptureFailed = true;
          captureFailedByStream[name] = true;
        }
      } else if (spoolChunk.length) {
        fullOutputCaptureFailed = true;
        captureFailedByStream[name] = true;
      }
      chunks[name].push(data);
      sizes[name] += data.length;
      while (sizes[name] > CAPTURE_LIMIT_BYTES) {
        dropped = true;
        const excess = sizes[name] - CAPTURE_LIMIT_BYTES;
        const first = chunks[name][0];
        if (first.length <= excess) chunks[name].shift();
        else chunks[name][0] = first.subarray(excess);
        sizes[name] -= Math.min(first.length, excess);
      }
    };
    child.stdout.on('data', collect('stdout'));
    child.stderr.on('data', collect('stderr'));

    const killTree = () => {
      try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
    };
    const timer = setTimeout(() => { timedOut = true; killTree(); }, timeoutMs);

    const finish = (exitCode, spawnError) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      killTree(); // reap any grandchildren that outlived the direct child
      const readFullOutput = name => {
        try {
          const prefix = fullOutputPaths && fs.existsSync(fullOutputPaths[name])
            ? fs.readFileSync(fullOutputPaths[name], 'utf8')
            : '';
          if (captureFailedByStream[name] && totalBytes[name] > 0) {
            const tail = Buffer.concat(chunks[name]).toString('utf8');
            return `${prefix}\n[complete output spool unavailable; retained bounded tail follows]\n${tail}`;
          }
          if (spoolTruncated[name]) {
            const tail = Buffer.concat(chunks[name]).toString('utf8');
            return `${prefix}\n[output spool truncated after ${OUTPUT_SPOOL_LIMIT_BYTES} bytes; omitted ${totalBytes[name] - OUTPUT_SPOOL_LIMIT_BYTES} bytes]\n${tail}`;
          }
          return prefix;
        }
        catch {
          fullOutputCaptureFailed = true;
          captureFailedByStream[name] = true;
          return Buffer.concat(chunks[name]).toString('utf8');
        }
      };
      const fullStdout = readFullOutput('stdout');
      const fullStderr = readFullOutput('stderr');
      resolve({
        exitCode,
        timedOut,
        dropped: dropped || fullOutputCaptureFailed || Object.values(spoolTruncated).some(Boolean),
        outputCaptureFailed: fullOutputCaptureFailed,
        outputSpoolTruncated: Object.values(spoolTruncated).some(Boolean),
        outputSpoolOmittedBytes: Object.fromEntries(Object.keys(spoolTruncated).map(name => [name, Math.max(0, totalBytes[name] - OUTPUT_SPOOL_LIMIT_BYTES)])),
        spawnError,
        durationMs: Date.now() - started,
        stdout: fullStdout,
        stderr: fullStderr,
      });
      if (fullOutputDir) {
        try { fs.rmSync(fullOutputDir, { recursive: true, force: true }); }
        catch { /* Temporary diagnostic cleanup is best-effort. */ }
      }
    };
    child.once('error', error => finish(null, error));
    child.once('close', code => finish(code, null));
  });
}

function parseRuff(stdout) {
  try {
    const items = JSON.parse(stdout);
    if (!Array.isArray(items)) return null;
    return items.map(item => ({
      file: item.filename ?? null,
      line: item.location?.row ?? null,
      column: item.location?.column ?? null,
      code: item.code ?? null,
      message: shorten(item.message),
    }));
  } catch {
    return null;
  }
}

function parseCompile(stdout) {
  const diagnostics = [];
  for (const line of stdout.split('\n')) {
    if (!line.trim()) continue;
    try {
      const item = JSON.parse(line);
      diagnostics.push({
        file: item.file ?? null,
        line: item.line ?? null,
        column: item.column ?? null,
        code: 'SyntaxError',
        message: shorten(item.message),
      });
    } catch { /* not a diagnostic line */ }
  }
  return diagnostics;
}

function parsePytest(text) {
  const diagnostics = [];
  for (const line of text.split('\n')) {
    const match = /^(FAILED|ERROR) (\S+?)(?:::(\S+))?(?: - (.*))?$/.exec(line.trim());
    if (!match) continue;
    const [, , file, testName, message = ''] = match;
    const lineHit = new RegExp(`^${file.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}:(\\d+): `, 'm').exec(text);
    diagnostics.push({
      file,
      line: lineHit ? Number(lineHit[1]) : null,
      column: null,
      code: /^[A-Za-z_.]+(Error|Exception)\b/.exec(message)?.[0] ?? (message.startsWith('assert') ? 'AssertionError' : 'TestFailure'),
      message: shorten(testName ? `${testName}: ${message}` : message),
    });
  }
  return diagnostics;
}

function pytestSummary(text) {
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (/\b\d+ (passed|failed|error|errors|skipped|deselected)\b/.test(lines[i]) || /^no tests ran/.test(lines[i])) {
      return lines[i].replace(/^=+\s*|\s*=+$/g, '');
    }
  }
  return null;
}

function parseNodeTest(text) {
  const lines = text.split('\n');
  const failures = [];
  for (let index = 0; index < lines.length; index += 1) {
    const failure = /^(\s*)not ok \d+ - (.+)$/.exec(lines[index]);
    if (!failure) continue;
    // Node's TAP reporter puts YAML-like metadata between --- and ... for
    // each failing test. Do not search a fixed window: nested failures can
    // otherwise borrow their parent or sibling's metadata.
    const metadata = [];
    let inDetails = false;
    for (let cursor = index + 1; cursor < lines.length; cursor += 1) {
      const line = lines[cursor];
      if (!inDetails) {
        if (/^\s*---\s*$/.test(line)) inDetails = true;
        else if (line.trim()) break;
        continue;
      }
      if (/^\s*\.\.\.\s*$/.test(line) || /^\s*not ok \d+ - /.test(line)) break;
      metadata.push(line);
    }
    const readField = name => {
      const position = metadata.findIndex(line => line.trimStart().startsWith(name + ':'));
      if (position < 0) return null;
      const value = metadata[position].trimStart().slice(name.length + 1).trim();
      if (!/^\|[+-]?$/.test(value)) {
        if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
          return value.slice(1, -1);
        }
        return value;
      }
      // Assertion failures usually use "error: |-" followed by indented
      // human-readable details. Capture useful lines, bounded by shorten().
      const indent = /^\s*/.exec(metadata[position])[0].length;
      const content = [];
      for (let cursor = position + 1; cursor < metadata.length; cursor += 1) {
        const line = metadata[cursor];
        if (line.trim() && /^\s*/.exec(line)[0].length <= indent) break;
        if (line.trim()) content.push(line.trim());
        if (content.length === 3) break;
      }
      return content.join(' ');
    };
    const location = /^(.*):(\d+):(\d+)$/.exec(readField('location') ?? '');
    const error = readField('error');
    failures.push({
      offset: failure[1].length,
      failureType: readField('failureType'),
      diagnostic: {
        file: location?.[1] ?? null,
        line: location ? Number(location[2]) : null,
        column: location ? Number(location[3]) : null,
        code: 'NodeTestFailure',
        message: shorten(error ? failure[2] + ': ' + error : failure[2]),
      },
    });
  }
  // An outer "subtestsFailed" record only repeats the nested leaf failure.
  // Retain it if there is no nested record (e.g. truncated TAP output).
  const diagnostics = failures.filter((failure, index) => {
    if (failure.failureType !== 'subtestsFailed') return true;
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      if (failures[cursor].offset <= failure.offset) break;
      return false;
    }
    return true;
  }).map(failure => failure.diagnostic);
  const passed = /^\s*# pass (\d+)/m.exec(text);
  const failed = /^\s*# fail (\d+)/m.exec(text);
  const summary = passed && failed ? passed[1] + ' passed, ' + failed[1] + ' failed' : null;
  return { diagnostics, summary };
}

function analyze(request, run, root, sandboxRoot = root) {
  const combined = `${run.stdout}\n${run.stderr}`;
  let diagnostics = [];
  let summary = null;

  if (request.kind === 'ruff') {
    const parsed = parseRuff(run.stdout);
    if (parsed) {
      const real = fs.realpathSync(sandboxRoot);
      diagnostics = parsed.map(item => ({
        ...item,
        file: item.file && path.relative(real, item.file.startsWith(real) ? item.file : path.resolve(sandboxRoot, item.file)),
      }));
      summary = diagnostics.length ? `${diagnostics.length} Ruff violation(s)` : 'All checks passed';
    }
  } else if (request.kind === 'python_compile') {
    diagnostics = parseCompile(run.stdout);
    summary = diagnostics.length ? `${diagnostics.length} syntax error(s)` : 'Compiled cleanly';
  } else if (request.kind === 'pytest' || request.profile === 'pytest_all') {
    diagnostics = parsePytest(combined);
    summary = pytestSummary(combined);
  } else if (request.kind === 'node_test') {
    ({ diagnostics, summary } = parseNodeTest(combined));
    // TAP locations originate inside /workspace in Docker, not on the runner.
    // Render only paths within that sandbox root; never leak host paths.
    diagnostics = diagnostics.map(item => {
      if (!item.file) return item;
      let source = item.file;
      try {
        if (source.startsWith('file://')) source = fileURLToPath(source);
      } catch { return { ...item, file: null }; }
      const base = path.resolve(sandboxRoot);
      const relative = path.relative(base, path.isAbsolute(source) ? source : path.resolve(base, source));
      return {
        ...item,
        file: relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) ? null : relative,
      };
    });
  }
  return { diagnostics, summary };
}

/**
 * Run one allow-listed focused check inside `root`.
 * Never throws for bad requests or failing checks: both are returned as evidence.
 */
export async function runCheck(root, params, options = {}) {
  const env = options.env ?? process.env;

  let request;
  let spec;
  try {
    ({ request, spec } = buildRunCheckSpec(root, params, { bins: options.bins, env }));
  } catch (error) {
    if (error instanceof InvalidCheck) return invalid(params?.kind, error.message);
    throw error;
  }

  const checksPythonLayout = request.kind === 'python_compile'
    || request.kind === 'ruff'
    || request.kind === 'pytest'
    || (request.kind === 'profile' && request.profile === 'pytest_all');
  const packageRootDiagnostics = checksPythonLayout
    ? duplicatePackageRootDiagnostics(root, projectConfig().checks.packageRoots)
    : [];
  if (packageRootDiagnostics.length) {
    return {
      status: 'fail',
      kind: request.kind,
      ...(request.kind === 'profile' ? { profile: request.profile } : {}),
      exit_code: null,
      duration_ms: 0,
      summary: 'Duplicate package root detected before focused validation',
      diagnostics: packageRootDiagnostics.slice(0, MAX_DIAGNOSTICS),
      stdout_tail: '',
      stderr_tail: '',
      truncated: packageRootDiagnostics.length > MAX_DIAGNOSTICS,
    };
  }

  const seconds = Math.min(
    Number(options.timeoutSeconds ?? env.PI_RUN_CHECK_TIMEOUT_SECONDS ?? DEFAULT_TIMEOUT_SECONDS),
    MAX_TIMEOUT_SECONDS,
  );
  const timeoutMs = options.timeoutMs ?? seconds * 1000;
  const backend = options.backend
    ?? (options.sandboxFactory ? createLocalSandboxBackend(options.sandboxFactory) : selectSandboxBackend(env));
  if (!backend) return infraError(request.kind, {
    component: 'sandbox', code: 'UNSUPPORTED_PLATFORM', command: null, message: `No check sandbox is available on ${process.platform}`,
  });
  const run = await backend.run({ root: path.resolve(root), request, spec, env: checkEnv(env), timeoutMs });
  if (run.requestInvalid) return invalid(request.kind, run.requestInvalid.message || 'request rejected by trusted executor');
  if (run.infrastructure) return infraError(request.kind, run.infrastructure, {
    exit_code: run.exitCode ?? null,
    duration_ms: run.durationMs ?? 0,
    stdout_tail: cap(run.stdout, TAIL_CHARS).text,
    stderr_tail: cap(run.stderr, TAIL_CHARS).text,
    truncated: Boolean(run.truncated),
  });

  const kind = request.kind;
  const stdoutTail = cap(run.stdout, TAIL_CHARS);
  const stderrTail = cap(run.stderr, TAIL_CHARS);
  const truncated = run.dropped || stdoutTail.truncated || stderrTail.truncated;
  const base = {
    kind,
    ...(kind === 'profile' ? { profile: request.profile } : {}),
    exit_code: run.exitCode,
    duration_ms: run.durationMs,
    stdout_tail: stdoutTail.text,
    stderr_tail: stderrTail.text,
    truncated,
    ...(run.outputCaptureFailed ? { output_capture_failed: true } : {}),
    ...(run.outputSpoolTruncated ? {
      output_spool_truncated: true,
      output_spool_omitted_bytes: run.outputSpoolOmittedBytes,
    } : {}),
    ...(run.image ? { sandbox_image: run.image } : {}),
    ...(run.image_id ? { sandbox_image_id: run.image_id } : {}),
    ...(run.sandbox_security ? { sandbox_security: run.sandbox_security } : {}),
    ...(run.container_removed !== undefined ? { sandbox_container_removed: run.container_removed } : {}),
  };

  const checkDiagnosticId = `check-${process.pid}-${Date.now()}`;
  const diagnosticStored = (truncated || run.exitCode !== 0 || run.timedOut) && appendDiagnostic(env.PI_DIAGNOSTICS_FILE, {
    id: checkDiagnosticId,
    at: new Date().toISOString(),
    type: 'run_check',
    kind,
    profile: request.profile ?? null,
    status: run.timedOut ? 'timeout' : run.exitCode === 0 ? 'pass' : 'fail',
    exit_code: run.exitCode,
    duration_ms: run.durationMs,
    stdout: run.stdout,
    stderr: run.stderr,
    capture_incomplete: Boolean(run.outputCaptureFailed),
    spool_truncated: Boolean(run.outputSpoolTruncated),
    spool_omitted_bytes: run.outputSpoolOmittedBytes,
  });
  if (diagnosticStored) base.diagnostic_ref = `${path.basename(env.PI_DIAGNOSTICS_FILE)}#${checkDiagnosticId}`;
  else if (truncated || run.exitCode !== 0 || run.timedOut) base.diagnostic_artifact_failed = true;

  if (run.timedOut) {
    return { status: 'timeout', ...base, summary: `Timed out after ${Math.round(timeoutMs / 1000)}s; process tree killed`, diagnostics: [] };
  }
  if (run.spawnError) return infraError(kind, {
    component: 'check_command', code: run.spawnError.code || run.spawnError.message,
    command: spec.command, message: `Could not start ${spec.command}: ${run.spawnError.code || run.spawnError.message}`,
  }, base);

  const { diagnostics, summary } = analyze(request, run, path.resolve(root), run.sandboxRoot ?? path.resolve(root));
  const passed = run.exitCode === 0;
  return {
    status: passed ? 'pass' : 'fail',
    ...base,
    summary: summary ?? (passed ? 'Check passed' : `Check failed with exit code ${run.exitCode}`),
    diagnostics: diagnostics.slice(0, MAX_DIAGNOSTICS),
    ...(diagnostics.length > MAX_DIAGNOSTICS ? { truncated: true } : {}),
  };
}

/**
 * Prove the check sandbox works before any agent work depends on it. The selected
 * backend owns the probe and must never fall back to an unsandboxed run.
 * Resolves `{ ok: true, duration_ms }`, or `{ ok: false, ...infra_error result }` with the same
 * structured `infrastructure` block a failing `runCheck` would return.
 */
export async function sandboxPreflight(options = {}) {
  const env = options.env ?? process.env;
  const timeoutMs = options.timeoutMs ?? PREFLIGHT_TIMEOUT_MS;
  const fail = (info, extra = {}) => ({ ok: false, ...infraError('sandbox_preflight', info, extra) });

  const probeRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-sandbox-preflight-'));
  try {
    fs.writeFileSync(path.join(probeRoot, '.pi-run-check-preflight'), 'readable\n', { mode: 0o444 });
    fs.writeFileSync(path.join(probeRoot, '.pi-run-check-preflight.py'), 'probe = True\n', { mode: 0o444 });
    const backend = options.backend ?? (options.sandboxFactory
      ? createLocalSandboxBackend(options.sandboxFactory)
      : selectSandboxBackend(env));
    if (!backend) return fail({ component: 'sandbox', code: 'UNSUPPORTED_PLATFORM', command: null, message: `No check sandbox is available on ${process.platform}` });
    if (backend.preflight) {
      const result = await backend.preflight({
        root: probeRoot,
        env: checkEnv(env),
        envContract: RUN_CHECK_ENV_CONTRACT,
        timeoutMs,
      });
      if (result?.ok) return result;
      const summary = String(result?.summary || 'The trusted sandbox preflight failed');
      return {
        ...result,
        ok: false,
        status: 'infra_error',
        summary: summary.includes('do not look for a shell') ? summary : `${summary}. ${INFRA_GUIDANCE}`,
      };
    }
    const isolated = (options.sandboxFactory ?? isolatedCommand)(probeRoot, { command: 'true', args: [] });
    if (!isolated) return fail({ component: 'sandbox', code: 'UNSUPPORTED_PLATFORM', command: null, message: `No check sandbox is available on ${process.platform}` });
    const run = await execute({ command: isolated.command, args: isolated.args, cwd: probeRoot, env: checkEnv(env), timeoutMs });
    const details = { exit_code: run.exitCode, duration_ms: run.durationMs, stdout_tail: cap(run.stdout, TAIL_CHARS).text, stderr_tail: cap(run.stderr, TAIL_CHARS).text };
    if (run.timedOut) return fail({ component: 'sandbox', code: 'TIMEOUT', command: isolated.command, message: `Check sandbox probe timed out after ${timeoutMs}ms` }, details);
    if (run.spawnError) return fail({ component: 'sandbox', code: run.spawnError.code || 'SPAWN_FAILED', command: isolated.command, message: `Could not start sandbox ${isolated.command}: ${run.spawnError.code || run.spawnError.message}` }, details);
    if (run.exitCode !== 0) return fail({ component: 'sandbox', code: 'PROBE_FAILED', command: isolated.command, message: `Check sandbox probe exited with code ${run.exitCode}` }, details);
    return { ok: true, duration_ms: run.durationMs };
  } finally {
    fs.rmSync(probeRoot, { recursive: true, force: true });
  }
}

function createLocalSandboxBackend(factory) {
  return {
    async run({ root, spec, env, timeoutMs }) {
      const isolated = factory(root, spec);
      if (!isolated) return { infrastructure: { component: 'sandbox', code: 'UNAVAILABLE', command: null, message: 'Sandbox backend is unavailable' } };
      return execute({ command: isolated.command, args: isolated.args, cwd: root, env, timeoutMs });
    },
  };
}

function selectSandboxBackend(env) {
  if (process.platform === 'linux') return createDockerSandboxBackend(env);
  if (process.platform === 'darwin') return {
    run({ root, spec, env: cleanEnv, timeoutMs }) {
      const sandbox = isolatedCommand(root, spec);
      return execute({ command: sandbox.command, args: sandbox.args, cwd: root, env: cleanEnv, timeoutMs });
    },
  };
  return null;
}

/** Structured, bounded metric record (no raw output, no secrets). */
export function checkMetricRecord(result, { backend, stage }) {
  return {
    backend,
    stage,
    kind: result.kind,
    profile: result.profile ?? null,
    status: result.status,
    duration_ms: result.duration_ms,
    truncated: result.truncated,
    diagnostics: result.diagnostics.length,
    ...(result.infrastructure ? { infrastructure: result.infrastructure.component, infrastructure_code: result.infrastructure.code } : {}),
  };
}
