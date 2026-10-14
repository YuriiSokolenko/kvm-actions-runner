#!/usr/local/bin/node
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const PORT = Number(process.env.RUN_CHECK_EXECUTOR_PORT || 17343);
const IMAGE = process.env.RUN_CHECK_SANDBOX_IMAGE || '';
const STAGE_ROOT = process.env.RUN_CHECK_STAGE_ROOT || '/run-check-stage';
const STAGE_VOLUME = process.env.RUN_CHECK_STAGE_VOLUME || 'social-mcp-run-check-stage';
const RUNNER_PREFIX = process.env.RUN_CHECK_RUNNER_PREFIX || 'n150-pi-eph';
const RUNNER_WORK_ROOT = '/home/runner/actions-runner/_work/';
const PREFLIGHT_ROOT_PREFIX = '/tmp/pi-sandbox-preflight-';
const SANDBOX_UID = 1001;
const SANDBOX_GID = 1001;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_WORKTREE_BYTES = 512 * 1024 * 1024;
const MAX_WORKTREE_ENTRIES = 50000;
const FIXED_PATH = '/usr/local/bin:/usr/bin:/bin';
const FIXED_ENV = {
  PATH: FIXED_PATH,
  HOME: '/tmp',
  TMPDIR: '/tmp',
  PYTHONDONTWRITEBYTECODE: '1',
  PYTHONIOENCODING: 'utf-8',
  PYTHONPATH: '/workspace/src',
  RUFF_CACHE_DIR: '/tmp/ruff',
};
const HARNESS_ROOT = process.env.RUN_CHECK_HARNESS_ROOT || '/opt/social-mcp';
process.env.AGENT_HARNESS_CONFIG ||= path.join(HARNESS_ROOT, '.agent-harness.json');
const {
  RUN_CHECK_ENV_CONTRACT,
  buildRunCheckSpec,
  normalizeRunCheckPaths,
} = await import(pathToFileURL(path.join(HARNESS_ROOT, 'scripts/pi-common/run-check.mjs')));

const runCommand = (args, { timeoutMs = 30000, maxOutputBytes = 4 * 1024 * 1024 } = {}) => new Promise((resolve, reject) => {
  const child = spawn('docker', args, { stdio: ['ignore', 'pipe', 'pipe'], env: { PATH: process.env.PATH || '/usr/local/bin:/usr/bin:/bin' } });
  const chunks = { stdout: [], stderr: [] };
  const lengths = { stdout: 0, stderr: 0 };
  let overLimit = false;
  for (const stream of ['stdout', 'stderr']) child[stream].on('data', chunk => {
    chunks[stream].push(chunk);
    lengths[stream] += chunk.length;
    while (lengths[stream] > maxOutputBytes) {
      overLimit = true;
      const excess = lengths[stream] - maxOutputBytes;
      const first = chunks[stream][0];
      if (first.length <= excess) chunks[stream].shift();
      else chunks[stream][0] = first.subarray(excess);
      lengths[stream] -= Math.min(first.length, excess);
    }
  });
  const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
  child.once('error', error => { clearTimeout(timer); reject(error); });
  child.once('close', (code, signal) => {
    clearTimeout(timer);
    const output = {
      code,
      signal,
      stdout: Buffer.concat(chunks.stdout).toString('utf8'),
      stderr: Buffer.concat(chunks.stderr).toString('utf8'),
      overLimit,
    };
    if (code !== 0) {
      const error = new Error(output.stderr.trim() || `docker exited ${code ?? signal}`);
      error.code = 'DOCKER_COMMAND_FAILED';
      error.output = output;
      reject(error);
    } else resolve(output);
  });
});

function failure(code, message, command = 'docker-sandbox') {
  return { component: 'sandbox', code, command, message: String(message).slice(0, 400) };
}

async function dockerInspect(target) {
  const { stdout } = await runCommand(['inspect', target], { timeoutMs: 15000 });
  return JSON.parse(stdout)[0];
}

async function imageMetadata() {
  if (!IMAGE || !/^[A-Za-z0-9][A-Za-z0-9./:@_-]{0,254}$/.test(IMAGE)) throw Object.assign(new Error('RUN_CHECK_SANDBOX_IMAGE is missing or invalid'), { code: 'SANDBOX_IMAGE_CONFIG' });
  const { stdout } = await runCommand(['image', 'inspect', IMAGE], { timeoutMs: 15000 });
  const [image] = JSON.parse(stdout);
  return { image: IMAGE, image_id: image.Id, image_created: image.Created };
}

function constantTimeEqual(left, right) {
  const a = Buffer.from(String(left));
  const b = Buffer.from(String(right));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

async function authorizeRunner(request, authorization) {
  const prefix = RUNNER_PREFIX.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`^${prefix}-[A-Za-z0-9-]{1,80}$`);
  if (typeof request.runner_name !== 'string' || !pattern.test(request.runner_name)) throw Object.assign(new Error('invalid Pi runner identity'), { code: 'RUNNER_IDENTITY' });
  const supplied = /^Bearer ([A-Fa-f0-9]{64})$/.exec(authorization || '')?.[1];
  if (!supplied) throw Object.assign(new Error('executor authorization is missing or malformed'), { code: 'UNAUTHORIZED', status: 401 });

  const runner = await dockerInspect(request.runner_name);
  if (!runner.State?.Running || runner.Config?.Labels?.['social-mcp.pi-runner'] !== 'ephemeral') {
    throw Object.assign(new Error('request did not originate from a running ephemeral Pi runner'), { code: 'RUNNER_IDENTITY' });
  }
  const env = runner.Config.Env || [];
  const token = env.find(entry => entry.startsWith('RUN_CHECK_EXECUTOR_TOKEN='))?.slice('RUN_CHECK_EXECUTOR_TOKEN='.length);
  if (!token || !constantTimeEqual(supplied, token)) throw Object.assign(new Error('executor token does not match the runner'), { code: 'UNAUTHORIZED', status: 401 });
  if (runner.Config.User === '' || runner.Config.User === '0' || runner.Config.User === 'root') {
    throw Object.assign(new Error('Pi agent runner is configured as root'), { code: 'RUNNER_SECURITY' });
  }
  if (runner.HostConfig?.Privileged || (runner.HostConfig?.CapAdd || []).length) {
    throw Object.assign(new Error('Pi agent runner has privileged mode or added capabilities'), { code: 'RUNNER_SECURITY' });
  }
  if ((runner.HostConfig?.SecurityOpt || []).some(option => /(?:apparmor|seccomp)=unconfined/i.test(option))) {
    throw Object.assign(new Error('Pi agent runner has an unconfined AppArmor/seccomp setting'), { code: 'RUNNER_SECURITY' });
  }
  const mounts = runner.Mounts || [];
  if (mounts.some(mount => /(?:^|\/)var\/run\/docker\.sock$/.test(mount.Destination || '') || /docker\.sock/.test(mount.Source || ''))) {
    throw Object.assign(new Error('Pi agent runner must not have Docker socket access'), { code: 'RUNNER_SECURITY' });
  }
  return runner;
}

async function canonicalWorkspace(runnerName, requested, operation) {
  if (typeof requested !== 'string' || !path.isAbsolute(requested) || requested.includes('\0')) throw Object.assign(new Error('worktree path must be absolute'), { code: 'WORKTREE_PATH' });
  const { stdout } = await runCommand(['exec', runnerName, 'realpath', '-e', '--', requested], { timeoutMs: 10000 });
  const real = stdout.trim();
  const allowed = operation === 'preflight'
    ? real.startsWith(PREFLIGHT_ROOT_PREFIX)
    : real.startsWith(RUNNER_WORK_ROOT) && real.length > RUNNER_WORK_ROOT.length;
  if (!allowed || real !== requested) throw Object.assign(new Error('worktree path is outside the runner workspace boundary'), { code: 'WORKTREE_PATH' });
  return real;
}

function removeExcluded(sourcePath, name) {
  const excluded = new Set([
    '.git', '.venv', 'venv', 'node_modules', '.pi', '.pytest_cache', '.ruff_cache',
    'secrets', '.ssh', '.aws', '.docker', '.gnupg', '.netrc', '.npmrc', '.pypirc',
    '.git-credentials', '.gitconfig', 'credentials', 'credentials.json',
  ]);
  const current = path.join(sourcePath, name);
  if (excluded.has(name) || name === '.env' || (name.startsWith('.env.') && name !== '.env.example') || /^id_(rsa|ed25519)(\.pub)?$/.test(name)) {
    fs.rmSync(current, { recursive: true, force: true });
    return;
  }
  const stat = fs.lstatSync(current);
  if (stat.isDirectory()) for (const child of fs.readdirSync(current)) removeExcluded(current, child);
}

function hardenStage(stage) {
  let bytes = 0;
  let entries = 0;
  const visit = current => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name);
      const stat = fs.lstatSync(target);
      entries += 1;
      if (entries > MAX_WORKTREE_ENTRIES) throw Object.assign(new Error('worktree has too many entries for a focused check'), { code: 'WORKTREE_TOO_LARGE' });
      if (stat.isSymbolicLink()) continue;
      if (stat.isDirectory()) {
        visit(target);
        fs.chownSync(target, SANDBOX_UID, SANDBOX_GID);
        fs.chmodSync(target, 0o555);
      } else if (stat.isFile()) {
        bytes += stat.size;
        if (bytes > MAX_WORKTREE_BYTES) throw Object.assign(new Error('worktree exceeds the 512 MiB focused-check snapshot limit'), { code: 'WORKTREE_TOO_LARGE' });
        fs.chownSync(target, SANDBOX_UID, SANDBOX_GID);
        fs.chmodSync(target, (stat.mode & 0o111) ? 0o555 : 0o444);
      } else {
        throw Object.assign(new Error('worktree contains a special file not accepted by the sandbox'), { code: 'WORKTREE_SPECIAL_FILE' });
      }
    }
  };
  visit(stage);
  fs.chownSync(stage, SANDBOX_UID, SANDBOX_GID);
  fs.chmodSync(stage, 0o555);
  return { bytes, entries };
}

// Requests originate in the runner's filesystem namespace, while the shared
// builder runs against the staged copy. Translate only paths rooted at the
// canonical runner worktree; the builder then performs its normal existence,
// symlink, and containment checks against the staged tree.
export function remapRunnerPaths(root, params) {
  return normalizeRunCheckPaths(root, params);
}

/** The executor boundary: translate runner paths, then validate/build against the staged tree. */
export function buildStagedRunCheckSpec(canonicalRoot, stageRoot, params, options) {
  return buildRunCheckSpec(stageRoot, remapRunnerPaths(canonicalRoot, params), options);
}

async function stageWorktree(runnerName, root, operation) {
  const canonical = await canonicalWorkspace(runnerName, root, operation);
  const stage = fs.mkdtempSync(path.join(STAGE_ROOT, 'check-'));
  try {
    await runCommand(['cp', '-a', `${runnerName}:${canonical}/.`, stage], { timeoutMs: 60000, maxOutputBytes: 256 * 1024 });
    for (const entry of fs.readdirSync(stage)) removeExcluded(stage, entry);
    const stats = hardenStage(stage);
    return { stage, canonicalRoot: canonical, subpath: path.relative(STAGE_ROOT, stage), stats };
  } catch (error) {
    fs.rmSync(stage, { recursive: true, force: true });
    throw error;
  }
}

export function safeCheckEnvironment(requested) {
  if (!requested || typeof requested !== 'object' || Array.isArray(requested)) throw Object.assign(new Error('sandbox environment must be an object'), { code: 'CHECK_ENV' });
  const allowed = new Set(RUN_CHECK_ENV_CONTRACT.keys);
  for (const key of Object.keys(requested)) if (!allowed.has(key)) throw Object.assign(new Error(`unsupported check environment key: ${key}`), { code: 'CHECK_ENV' });
  const output = { ...FIXED_ENV };
  for (const key of ['LANG', 'LC_ALL']) {
    if (requested[key] != null) {
      if (typeof requested[key] !== 'string' || !/^[A-Za-z0-9_.@-]{1,64}$/.test(requested[key])) throw Object.assign(new Error(`invalid ${key} value`), { code: 'CHECK_ENV' });
      output[key] = requested[key];
    }
  }
  const moduleList = /^(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:,[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)*)?$/;
  for (const key of ['PI_TRUSTED_ACCEPTANCE_TARGETS', 'PI_TRUSTED_ACCEPTANCE_BASELINE_TARGETS']) {
    if (requested[key] != null) {
      if (typeof requested[key] !== 'string' || requested[key].length > 2000 || !moduleList.test(requested[key])) {
        throw Object.assign(new Error(`invalid ${key} value`), { code: 'CHECK_ENV' });
      }
      output[key] = requested[key];
    }
  }
  return output;
}

export function assertCheckEnvironmentContract(contract) {
  const expected = RUN_CHECK_ENV_CONTRACT;
  const actualKeys = Array.isArray(contract?.keys) ? [...contract.keys].sort() : null;
  const expectedKeys = [...expected.keys].sort();
  const matches = contract?.version === expected.version
    && actualKeys?.length === expectedKeys.length
    && expectedKeys.every((key, index) => key === actualKeys[index]);
  if (!matches) {
    throw Object.assign(new Error('run_check environment contract does not match the trusted executor'), { code: 'CHECK_ENV_CONTRACT' });
  }
  return true;
}

function containerArgs({ name, subpath, env, command, args, timeoutMs }) {
  const payload = Buffer.from(JSON.stringify({ command, args, timeout_ms: timeoutMs, env })).toString('base64url');
  return [
    'create', '--pull=never', '--name', name,
    '--label', 'social-mcp.run-check=ephemeral',
    '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
    '--read-only', '--user', `${SANDBOX_UID}:${SANDBOX_GID}`,
    '--pids-limit', '256', '--memory', '2g', '--cpus', '2',
    '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m',
    '--mount', `type=volume,source=${STAGE_VOLUME},target=/workspace,volume-subpath=${subpath},readonly`,
    '--workdir', '/workspace',
    ...Object.entries(env).flatMap(([key, value]) => ['--env', `${key}=${value}`]),
    '--entrypoint', 'python3', IMAGE, '/usr/local/lib/run-check-sandbox-exec.py', payload,
  ];
}

export function verifySandboxContainerConfig(container, subpath) {
  const host = container.HostConfig || {};
  const config = container.Config || {};
  const mounts = container.Mounts || [];
  const workspace = mounts.find(mount => mount.Destination === '/workspace');
  const security = host.SecurityOpt || [];
  const noNewPrivileges = security.some(option => option === 'no-new-privileges' || option === 'no-new-privileges:true');
  const secretEnv = (config.Env || []).filter(entry => /(?:TOKEN|SECRET|API_KEY|ACCESS_KEY|SSH_AUTH_SOCK|GITHUB|MODEL|OPENAI|ANTHROPIC)/i.test(entry.split('=', 1)[0]));
  const noDockerMount = !mounts.some(mount => /docker\.sock/.test(`${mount.Source || ''} ${mount.Destination || ''}`));
  const expectedMount = workspace?.Type === 'volume' && workspace.Name === STAGE_VOLUME && workspace.RW === false && workspace.Destination === '/workspace';
  const ok = host.NetworkMode === 'none'
    && (host.CapDrop || []).includes('ALL')
    && (host.CapAdd || []).length === 0
    && host.Privileged === false
    && host.ReadonlyRootfs === true
    && config.User === `${SANDBOX_UID}:${SANDBOX_GID}`
    && noNewPrivileges
    && host.PidMode !== 'host'
    && host.IpcMode !== 'host'
    && expectedMount
    && noDockerMount
    && secretEnv.length === 0;
  return { ok, network: host.NetworkMode, capabilities_dropped: (host.CapDrop || []).includes('ALL'), privileged: host.Privileged === true, readonly_rootfs: host.ReadonlyRootfs === true, user: config.User, no_new_privileges: noNewPrivileges, workspace_readonly: Boolean(expectedMount), docker_socket_absent: noDockerMount, credentials_absent: secretEnv.length === 0, workspace_subpath: subpath };
}

async function runSandbox({ runnerName, root, operation, params, requestedEnv, timeoutMs }) {
  let stageInfo;
  let containerName;
  let returned;
  const remember = value => { returned = value; return value; };
  const startedAt = Date.now();
  try {
    const image = await imageMetadata();
    stageInfo = await stageWorktree(runnerName, root, operation);
    const env = safeCheckEnvironment(requestedEnv);
    const sandboxRoot = '/workspace';
    const stagedParams = operation === 'preflight'
      ? { kind: 'python_compile', paths: [`${stageInfo.canonicalRoot}/.pi-run-check-preflight.py`] }
      : params;
    // Preflight must exercise the same absolute runner-root translation and
    // staged path validation used for real focused checks.
    const validation = buildStagedRunCheckSpec(stageInfo.canonicalRoot, stageInfo.stage, stagedParams, { bins: { python: '/usr/local/bin/python3', ruff: '/usr/local/bin/ruff', pytest: '/usr/local/bin/pytest', node: '/usr/local/bin/node' }, env: { PATH: FIXED_PATH } });
    const built = operation === 'preflight'
      ? { spec: { command: '/usr/local/bin/python3', args: ['/usr/local/lib/run-check-sandbox-probe.py', 'worktree'] } }
      : validation;
    if (operation !== 'preflight' && built.spec) {
      built.spec.args = built.spec.args.map(arg => typeof arg === 'string' ? arg.replaceAll(stageInfo.stage, sandboxRoot) : arg);
    }
    if (!built.spec) throw Object.assign(new Error('check command was not constructed'), { code: 'CHECK_PLAN' });

    const safeName = `run-check-${crypto.randomUUID()}`;
    containerName = safeName;
    const args = containerArgs({ name: safeName, subpath: stageInfo.subpath, env, command: built.spec.command, args: built.spec.args, timeoutMs });
    await runCommand(args, { timeoutMs: 30000, maxOutputBytes: 256 * 1024 });
    const container = await dockerInspect(safeName);
    const security = verifySandboxContainerConfig(container, stageInfo.subpath);
    if (!security.ok) throw Object.assign(new Error(`sandbox container did not match its required security profile: ${JSON.stringify(security)}`), { code: 'SANDBOX_SECURITY_PROFILE' });
    await runCommand(['start', safeName], { timeoutMs: 10000 });
    let exitCode;
    try {
      const wait = await runCommand(['wait', safeName], { timeoutMs: timeoutMs + 10000, maxOutputBytes: 1024 });
      exitCode = Number(wait.stdout.trim());
    } catch (error) {
      if (error.code === 'DOCKER_COMMAND_FAILED') throw error;
      await runCommand(['kill', safeName], { timeoutMs: 10000 }).catch(() => {});
      return remember({ timedOut: true, exitCode: null, durationMs: Date.now() - startedAt, stdout: '', stderr: 'Sandbox container was killed after its hard timeout.', truncated: false });
    }
    const logs = await runCommand(['logs', safeName], { timeoutMs: 10000, maxOutputBytes: 10 * 1024 * 1024 });
    if (logs.overLimit) throw Object.assign(new Error('sandbox result payload exceeded the output limit'), { code: 'SANDBOX_OUTPUT_LIMIT' });
    const line = logs.stdout.trim().split('\n').at(-1);
    let result;
    try { result = JSON.parse(line); } catch { throw Object.assign(new Error(`sandbox exited ${exitCode} without a structured result`), { code: 'SANDBOX_RESULT_MISSING' }); }
    if (result.protocol_error) throw Object.assign(new Error(result.protocol_error), { code: 'SANDBOX_PROTOCOL' });
    if (result.spawn_error) throw Object.assign(new Error(result.spawn_error.message), { code: result.spawn_error.code || 'CHECK_SPAWN_FAILED', component: 'check_command' });
    if (operation === 'preflight') {
      if (exitCode !== 0 || result.exitCode !== 0) throw Object.assign(new Error('sandbox preflight probe failed'), { code: 'SANDBOX_PREFLIGHT_FAILED' });
      const probe = JSON.parse(result.stdout);
      return remember({ ok: true, duration_ms: Date.now() - startedAt, ...image, sandbox: { ...probe, ...security, network_disabled: security.network === 'none' } });
    }
    if (exitCode !== 0) throw Object.assign(new Error(`sandbox wrapper exited with code ${exitCode}`), { code: 'SANDBOX_WRAPPER_FAILED' });
    return remember({ ...result, image: image.image, image_id: image.image_id, sandbox_security: security });
  } catch (error) {
    if (error.name === 'InvalidCheck') return remember({ status: 'invalid', message: error.message });
    const info = failure(error.code || 'SANDBOX_EXECUTOR_ERROR', error.message, error.component === 'check_command' ? null : 'docker-sandbox');
    if (error.component === 'check_command') info.component = 'check_command';
    if (operation === 'preflight') return remember({ ok: false, status: 'infra_error', summary: `INFRASTRUCTURE ERROR: ${error.message}`, infrastructure: info, diagnostics: [], stdout_tail: '', stderr_tail: '' });
    return remember({ infrastructure: info, exitCode: null, durationMs: Date.now() - startedAt, stdout: '', stderr: error.output?.stderr || error.message, truncated: false });
  } finally {
    if (containerName) {
      try {
        await runCommand(['rm', '-f', containerName], { timeoutMs: 10000 });
        if (returned) returned.container_removed = true;
      } catch (error) {
        if (returned) {
          returned.container_removed = false;
          returned.infrastructure = failure('SANDBOX_CLEANUP_FAILED', error.message);
          returned.exitCode = null;
          if (operation === 'preflight') {
            returned.ok = false;
            returned.status = 'infra_error';
            returned.summary = `INFRASTRUCTURE ERROR: sandbox container cleanup failed: ${error.message}`;
          }
        }
      }
    }
    if (stageInfo?.stage) {
      try {
        fs.chmodSync(stageInfo.stage, 0o755);
        fs.rmSync(stageInfo.stage, { recursive: true, force: true });
      } catch { /* stale stage directories are removed at executor startup */ }
    }
  }
}

async function handle(request, authorization, pathname) {
  const runner = await authorizeRunner(request, authorization);
  const runnerEnv = runner.Config?.Env || [];
  const runnerLabels = runnerEnv.find(entry => entry.startsWith('RUNNER_LABELS='))?.slice('RUNNER_LABELS='.length) || null;
  const runnerEvidence = {
    name: runner.Name.replace(/^\//, ''),
    labels: runnerLabels,
    image: runner.Config?.Image || null,
    image_id: runner.Image || null,
    user: runner.Config?.User || null,
    privileged: runner.HostConfig?.Privileged === true,
    cap_add: runner.HostConfig?.CapAdd || [],
    security_options: runner.HostConfig?.SecurityOpt || [],
    docker_socket_absent: !(runner.Mounts || []).some(mount => /(?:^|\/)var\/run\/docker\.sock$/.test(mount.Destination || '') || /docker\.sock/.test(mount.Source || '')),
  };
  if (pathname === '/v1/preflight') {
    assertCheckEnvironmentContract(request.env_contract);
    const root = await canonicalWorkspace(runner.Name.replace(/^\//, ''), request.root, 'preflight');
    const result = await runSandbox({
      runnerName: runner.Name.replace(/^\//, ''),
      root,
      operation: 'preflight',
      requestedEnv: request.env,
      timeoutMs: 15000,
    });
    return { ...result, environment_contract: RUN_CHECK_ENV_CONTRACT, pi_runner: runnerEvidence };
  }
  const root = await canonicalWorkspace(runner.Name.replace(/^\//, ''), request.root, 'run-check');
  if (!Number.isInteger(request.timeout_ms) || request.timeout_ms < 1 || request.timeout_ms > 600000) throw Object.assign(new Error('invalid check timeout'), { code: 'CHECK_TIMEOUT' });
  const result = await runSandbox({ runnerName: runner.Name.replace(/^\//, ''), root, operation: 'run-check', params: request.params, requestedEnv: request.env, timeoutMs: request.timeout_ms });
  if (result.infrastructure) return result;
  if (result.protocol_error || result.status === 'invalid') return { status: 'invalid', message: result.message || 'invalid run_check request' };
  return result;
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let length = 0;
    request.on('data', chunk => {
      length += chunk.length;
      if (length > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('request body too large'), { code: 'BODY_TOO_LARGE', status: 413 }));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.once('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { reject(Object.assign(new Error('request body must be JSON'), { code: 'INVALID_JSON', status: 400 })); }
    });
    request.once('error', reject);
  });
}

export async function managerSandboxGate() {
  const image = await imageMetadata();
  await runCommand([
    'run', '--rm', '--pull=never', '--network', 'none', '--cap-drop', 'ALL',
    '--security-opt', 'no-new-privileges', '--read-only', '--user', `${SANDBOX_UID}:${SANDBOX_GID}`,
    '--tmpfs', '/tmp:rw,nosuid,nodev,size=16m', '--entrypoint', 'python3', IMAGE,
    '/usr/local/lib/run-check-sandbox-probe.py', 'image',
  ], { timeoutMs: 30000 });
  return image;
}

export function createServer() {
  return http.createServer(async (req, res) => {
    const pathname = new URL(req.url, 'http://127.0.0.1').pathname;
    if (req.method === 'GET' && pathname === '/healthz') {
      try {
        const image = await imageMetadata();
        await runCommand(['info', '--format', '{{.ServerVersion}}'], { timeoutMs: 10000, maxOutputBytes: 1024 });
        res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ ok: true, executor: 'trusted-local-docker', ...image }));
      } catch (error) {
        res.writeHead(503, { 'content-type': 'application/json' }).end(JSON.stringify({ ok: false, error: { code: error.code || 'EXECUTOR_UNAVAILABLE', message: error.message } }));
      }
      return;
    }
    if (req.method !== 'POST' || !['/v1/preflight', '/v1/run-check'].includes(pathname)) {
      res.writeHead(404).end();
      return;
    }
    try {
      const body = await readJson(req);
      const result = await handle(body, req.headers.authorization, pathname);
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(result));
    } catch (error) {
      const status = error.status || (error.code === 'UNAUTHORIZED' ? 401 : 400);
      res.writeHead(status, { 'content-type': 'application/json' }).end(JSON.stringify({ error: { code: error.code || 'REQUEST_INVALID', message: error.message } }));
    }
  });
}

export function buildSandboxContainerArgs(options) {
  return containerArgs(options);
}

export function startServer() {
  fs.mkdirSync(STAGE_ROOT, { recursive: true });
  for (const entry of fs.readdirSync(STAGE_ROOT)) {
    const target = path.join(STAGE_ROOT, entry);
    const stat = fs.lstatSync(target);
    if (stat.isDirectory() && !stat.isSymbolicLink()) {
      fs.chmodSync(target, 0o755);
      fs.rmSync(target, { recursive: true, force: true });
    }
  }
  const server = createServer();
  server.listen(PORT, '0.0.0.0', () => process.stdout.write(`RUN_CHECK_EXECUTOR listening port=${PORT} image=${IMAGE}\n`));
  return server;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) startServer();
