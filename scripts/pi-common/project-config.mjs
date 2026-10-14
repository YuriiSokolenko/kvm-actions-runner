import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Project configuration for the agent harness.
 *
 * WHY: everything that is a decision about ONE repository (default branch, agent
 * branch naming, pipeline label names, git identity, control-plane paths, final
 * checks, environment setup) must be data owned by that repository, not literals
 * inside harness code. The harness reads this file; it never names a project.
 *
 * TRUST: the file is control plane. It is loaded only from the trusted control
 * checkout (never from an agent worktree / process cwd) and `.agent-harness.json`
 * itself is always a protected path (see control-plane-policy.mjs).
 *
 * FORMAT: JSON, because the control plane deliberately has no third-party
 * dependencies and Node has no built-in YAML parser. The schema is
 * format-neutral; a future standalone harness can add a YAML front-end at
 * `parseConfigText()` without touching any consumer.
 */

export const CONFIG_FILE_NAME = '.agent-harness.json';
export const CONFIG_ENV = 'AGENT_HARNESS_CONFIG';

const REQUIRED_LABEL_ROLES = Object.freeze([
  'queued', 'triageReady', 'ready', 'running', 'pr', 'needsHuman', 'architectReady', 'epic', 'reviewPassed', 'reviewChangesRequested',
]);
const OPTIONAL_LABEL_ROLES = Object.freeze(['blocked']);
const LABEL_ROLES = Object.freeze([...REQUIRED_LABEL_ROLES, ...OPTIONAL_LABEL_ROLES]);
// Workflow files the harness dispatches or watches, by ROLE. Filenames are project
// wiring (the caller workflows live in the project repository), not harness names.
const WORKFLOW_ROLES = Object.freeze(['dispatcher', 'architect', 'implementer', 'reviewer', 'repair', 'ci', 'mergeGate', 'triage']);
const TOP_LEVEL = new Set([
  'version', 'git', 'labels', 'workflows', 'automation', 'agents', 'controlPlane', 'checks', 'environment', 'workspace', 'model',
]);

class ConfigError extends Error {
  constructor(message) {
    super(`Invalid ${CONFIG_FILE_NAME}: ${message}`);
    this.name = 'ConfigError';
  }
}

function object(value, where) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ConfigError(`${where} must be an object`);
  return value;
}

function string(value, where) {
  if (typeof value !== 'string' || !value.trim()) throw new ConfigError(`${where} must be a non-empty string`);
  return value;
}

function stringList(value, where, { optional = false } = {}) {
  if (value === undefined && optional) return [];
  if (!Array.isArray(value)) throw new ConfigError(`${where} must be an array of strings`);
  return value.map((item, index) => string(item, `${where}[${index}]`));
}

function relativeDirectoryList(value, where, { optional = false, root = null } = {}) {
  return stringList(value, where, { optional }).map((item, index) => {
    const normalized = item.replace(/\\/g, '/');
    const parts = normalized.split('/');
    if (/^(?:\/|[A-Za-z]:\/)/.test(normalized) || parts.some(part => part === '.' || part === '..')) {
      throw new ConfigError(`${where}[${index}] must be a relative directory path without . or .. segments`);
    }
    if (root) {
      const target = path.resolve(root, item);
      if (!fs.existsSync(target) || !fs.statSync(target).isDirectory()) {
        throw new ConfigError(`${where}[${index}] does not identify an existing directory: ${item}`);
      }
    }
    return item;
  });
}

function rejectUnknown(value, allowed, where) {
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) throw new ConfigError(`${where}.${key} is not a known setting`);
  }
}

/** A fixed-argv command. Never a shell string: config cannot smuggle shell syntax. */
function command(value, where) {
  object(value, where);
  rejectUnknown(value, ['name', 'builtin', 'command', 'args'], where);
  const name = string(value.name, `${where}.name`);
  if (value.builtin !== undefined) return Object.freeze({ name, builtin: string(value.builtin, `${where}.builtin`) });
  const args = (value.args ?? []).map((item, index) => {
    if (typeof item === 'string') return item;
    object(item, `${where}.args[${index}]`);
    rejectUnknown(item, ['files'], `${where}.args[${index}]`);
    const files = object(item.files, `${where}.args[${index}].files`);
    rejectUnknown(files, ['dir', 'pattern'], `${where}.args[${index}].files`);
    try { new RegExp(string(files.pattern, `${where}.args[${index}].files.pattern`)); } catch (error) {
      throw new ConfigError(`${where}.args[${index}].files.pattern is not a valid regular expression: ${error.message}`);
    }
    return Object.freeze({ files: Object.freeze({ dir: string(files.dir, `${where}.args[${index}].files.dir`), pattern: files.pattern }) });
  });
  return Object.freeze({ name, command: string(value.command, `${where}.command`), args: Object.freeze(args) });
}

/**
 * Model endpoint and catalog. This is infrastructure wiring (which host serves which
 * model), so it is data owned by the consuming repository, never a harness literal.
 * Optional at parse time so unrelated fixtures stay small; `modelConfig()` fails closed.
 */
function modelSettings(value) {
  if (value === undefined) return null;
  object(value, 'model');
  rejectUnknown(value, ['provider', 'baseUrl', 'choices'], 'model');
  const baseUrl = string(value.baseUrl, 'model.baseUrl');
  let parsed;
  try { parsed = new URL(baseUrl); } catch { parsed = null; }
  if (!parsed || !['http:', 'https:'].includes(parsed.protocol)) throw new ConfigError('model.baseUrl must be an http(s) URL');
  const choices = object(value.choices, 'model.choices');
  if (!Object.keys(choices).length) throw new ConfigError('model.choices must name at least one model');
  return Object.freeze({
    provider: string(value.provider, 'model.provider'),
    baseUrl,
    choices: Object.freeze(Object.fromEntries(Object.entries(choices).map(([name, entry]) => {
      object(entry, `model.choices.${name}`);
      rejectUnknown(entry, ['id', 'label'], `model.choices.${name}`);
      return [name, Object.freeze({ id: string(entry.id, `model.choices.${name}.id`), label: string(entry.label, `model.choices.${name}.label`) })];
    }))),
  });
}

function commandList(value, where) {
  if (!Array.isArray(value)) throw new ConfigError(`${where} must be an array`);
  return Object.freeze(value.map((item, index) => command(item, `${where}[${index}]`)));
}

export function parseConfigText(text, source = CONFIG_FILE_NAME, options = {}) {
  let raw;
  try { raw = JSON.parse(text); } catch (error) { throw new ConfigError(`${source} is not valid JSON: ${error.message}`); }
  return validateConfig(raw, options);
}

export function validateConfig(raw, options = {}) {
  object(raw, 'root');
  for (const key of Object.keys(raw)) {
    if (!TOP_LEVEL.has(key)) throw new ConfigError(`${key} is not a known top-level setting`);
  }
  if (raw.version !== 1) throw new ConfigError('version must be 1');

  const git = object(raw.git, 'git');
  rejectUnknown(git, ['defaultBranch', 'issueBranchPrefix', 'checkpointBranchSuffix', 'identity'], 'git');
  const identity = object(git.identity, 'git.identity');
  rejectUnknown(identity, ['name', 'email'], 'git.identity');

  const labels = object(raw.labels, 'labels');
  rejectUnknown(labels, LABEL_ROLES, 'labels');
  for (const role of REQUIRED_LABEL_ROLES) string(labels[role], `labels.${role}`);
  for (const role of OPTIONAL_LABEL_ROLES) {
    if (labels[role] !== undefined) string(labels[role], `labels.${role}`);
  }

  const workflows = object(raw.workflows, 'workflows');
  rejectUnknown(workflows, WORKFLOW_ROLES, 'workflows');
  for (const role of WORKFLOW_ROLES) {
    if (!/^[A-Za-z0-9._-]+\.ya?ml$/.test(string(workflows[role], `workflows.${role}`))) {
      throw new ConfigError(`workflows.${role} must be a workflow file name such as ${role}.yml`);
    }
  }

  const automation = object(raw.automation, 'automation');
  rejectUnknown(automation, ['modeVariable'], 'automation');

  const agents = object(raw.agents, 'agents');
  rejectUnknown(agents, ['promptsDir'], 'agents');

  const workspace = object(raw.workspace ?? {}, 'workspace');
  rejectUnknown(workspace, ['cleanDirectories', 'cleanFiles'], 'workspace');

  const controlPlane = object(raw.controlPlane ?? {}, 'controlPlane');
  rejectUnknown(controlPlane, ['prefixes', 'exact', 'patterns'], 'controlPlane');
  const patterns = stringList(controlPlane.patterns, 'controlPlane.patterns', { optional: true }).map((source, index) => {
    try { return new RegExp(source); } catch (error) {
      throw new ConfigError(`controlPlane.patterns[${index}] is not a valid regular expression: ${error.message}`);
    }
  });

  const checks = object(raw.checks ?? {}, 'checks');
  rejectUnknown(checks, ['final', 'profiles', 'packageRoots', 'ciRepairableSteps'], 'checks');
  const packageRoots = object(checks.packageRoots ?? {}, 'checks.packageRoots');
  rejectUnknown(packageRoots, ['canonicalRoots', 'allowDuplicatePackages'], 'checks.packageRoots');
  const profiles = {};
  for (const [name, spec] of Object.entries(object(checks.profiles ?? {}, 'checks.profiles'))) {
    profiles[name] = command({ name, ...object(spec, `checks.profiles.${name}`) }, `checks.profiles.${name}`);
  }

  const environment = object(raw.environment ?? {}, 'environment');
  rejectUnknown(environment, ['default', 'stages', 'pathPrepend'], 'environment');
  const stages = {};
  for (const [stage, steps] of Object.entries(object(environment.stages ?? {}, 'environment.stages'))) {
    stages[stage] = commandList(steps, `environment.stages.${stage}`);
  }

  return Object.freeze({
    version: 1,
    git: Object.freeze({
      defaultBranch: string(git.defaultBranch, 'git.defaultBranch'),
      issueBranchPrefix: string(git.issueBranchPrefix, 'git.issueBranchPrefix'),
      checkpointBranchSuffix: string(git.checkpointBranchSuffix, 'git.checkpointBranchSuffix'),
      identity: Object.freeze({ name: string(identity.name, 'git.identity.name'), email: string(identity.email, 'git.identity.email') }),
    }),
    labels: Object.freeze(Object.fromEntries(
      LABEL_ROLES.filter(role => labels[role] !== undefined).map(role => [role, labels[role]]),
    )),
    workflows: Object.freeze(Object.fromEntries(WORKFLOW_ROLES.map(role => [role, workflows[role]]))),
    workspace: Object.freeze({
      cleanDirectories: Object.freeze(stringList(workspace.cleanDirectories, 'workspace.cleanDirectories', { optional: true })),
      cleanFiles: Object.freeze(stringList(workspace.cleanFiles, 'workspace.cleanFiles', { optional: true })),
    }),
    automation: Object.freeze({ modeVariable: string(automation.modeVariable, 'automation.modeVariable') }),
    agents: Object.freeze({ promptsDir: string(agents.promptsDir, 'agents.promptsDir') }),
    controlPlane: Object.freeze({
      prefixes: Object.freeze(stringList(controlPlane.prefixes, 'controlPlane.prefixes', { optional: true })),
      exact: Object.freeze(stringList(controlPlane.exact, 'controlPlane.exact', { optional: true })),
      patterns: Object.freeze(patterns),
    }),
    checks: Object.freeze({
      final: checks.final === undefined ? Object.freeze([]) : commandList(checks.final, 'checks.final'),
      profiles: Object.freeze(profiles),
      // CI step names whose failure is a code failure PR Fix may repair; anything else is infrastructure.
      ciRepairableSteps: Object.freeze(stringList(checks.ciRepairableSteps, 'checks.ciRepairableSteps', { optional: true })),
      packageRoots: Object.freeze({
        canonicalRoots: Object.freeze(relativeDirectoryList(packageRoots.canonicalRoots, 'checks.packageRoots.canonicalRoots', {
          optional: true,
          root: options.root ?? null,
        })),
        allowDuplicatePackages: Object.freeze(stringList(packageRoots.allowDuplicatePackages, 'checks.packageRoots.allowDuplicatePackages', { optional: true })),
      }),
    }),
    model: modelSettings(raw.model),
    environment: Object.freeze({
      default: environment.default === undefined ? Object.freeze([]) : commandList(environment.default, 'environment.default'),
      stages: Object.freeze(stages),
      pathPrepend: Object.freeze(stringList(environment.pathPrepend, 'environment.pathPrepend', { optional: true })),
    }),
  });
}

/**
 * Locate the config in a TRUSTED location only, in this order:
 *   1. AGENT_HARNESS_CONFIG (explicit path),
 *   2. $GITHUB_WORKSPACE/.agent-harness.json (the trusted control checkout),
 *   3. walking up from this module (harness and project share one checkout today).
 * The process cwd is intentionally NOT searched: agent sessions run with cwd set
 * to a writable worktree, and config read from there would be agent-controlled.
 */
export function locateConfigFile(env = process.env, moduleDir = path.dirname(fileURLToPath(import.meta.url))) {
  if (env[CONFIG_ENV]) return path.resolve(env[CONFIG_ENV]);
  if (env.GITHUB_WORKSPACE) {
    const candidate = path.join(env.GITHUB_WORKSPACE, CONFIG_FILE_NAME);
    if (fs.existsSync(candidate)) return candidate;
  }
  for (let dir = moduleDir; ; dir = path.dirname(dir)) {
    const candidate = path.join(dir, CONFIG_FILE_NAME);
    if (fs.existsSync(candidate)) return candidate;
    if (path.dirname(dir) === dir) break;
  }
  return null;
}

export function loadProjectConfig(options = {}) {
  const file = locateConfigFile(options.env ?? process.env, options.moduleDir);
  if (!file) {
    throw new ConfigError(`not found. Set ${CONFIG_ENV}, or add ${CONFIG_FILE_NAME} to the trusted control checkout`);
  }
  return parseConfigText(fs.readFileSync(file, 'utf8'), file, { root: path.dirname(file) });
}

let cached = null;

/** Process-wide config. Fails closed: there are no built-in project defaults. */
export function projectConfig() {
  cached ??= loadProjectConfig();
  return cached;
}

export function resetProjectConfigForTests() {
  cached = null;
}

// --- derived project vocabulary: the only place these strings are composed ---

export const baseBranch = () => projectConfig().git.defaultBranch;
export const baseRef = () => `origin/${baseBranch()}`;
export const issueBranch = number => `${projectConfig().git.issueBranchPrefix}${number}`;
export const checkpointBranch = number => `${issueBranch(number)}${projectConfig().git.checkpointBranchSuffix}`;
export const gitIdentity = () => projectConfig().git.identity;
/** Top-level directories the environment setup creates (e.g. `.venv` from `.venv/bin`). */
export const environmentDirectories = () => [...new Set(
  projectConfig().environment.pathPrepend.map(entry => entry.split('/')[0]).filter(entry => entry && entry !== '.'),
)];
export const workflowFile = role => {
  const file = projectConfig().workflows[role];
  if (!file) throw new Error(`unknown workflow role: ${role}`);
  return file;
};
export const modelConfig = () => {
  const model = projectConfig().model;
  if (!model) throw new ConfigError('model is required to run a model stage (provider, baseUrl, choices)');
  return model;
};
export const labelName = role => {
  const name = projectConfig().labels[role];
  if (!name) throw new Error(`unknown label role: ${role}`);
  return name;
};

const escapeRegExp = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * `<prefix><n>` -> n, otherwise null. Checkpoint branches are not issue branches.
 * Strict (default) accepts only positive numbers without leading zeros; guards
 * that historically accepted any digits pass `{ strict: false }`.
 */
export function parseIssueBranch(ref, { strict = true } = {}) {
  const digits = strict ? '([1-9]\\d*)' : '(\\d+)';
  const match = new RegExp(`^${escapeRegExp(projectConfig().git.issueBranchPrefix)}${digits}$`).exec(String(ref ?? ''));
  return match ? Number(match[1]) : null;
}

/** `refs/heads/<prefix><n><checkpoint suffix>` -> n, otherwise null. */
export function parseCheckpointRef(ref) {
  const { issueBranchPrefix, checkpointBranchSuffix } = projectConfig().git;
  const match = new RegExp(`^refs/heads/${escapeRegExp(issueBranchPrefix)}(\\d+)${escapeRegExp(checkpointBranchSuffix)}$`).exec(String(ref ?? ''));
  return match ? Number(match[1]) : null;
}

export const isIssueBranch = ref => parseIssueBranch(ref) !== null;
export const issueBranchPrefix = () => projectConfig().git.issueBranchPrefix;

/**
 * Expand a configured command into concrete `{ command, args }` for a checkout.
 * `{files:{dir,pattern}}` argument tokens become the sorted matching files of
 * `dir` (relative to `root`), so a profile can say "run every test file"
 * without a shell glob.
 */
export function expandCommand(spec, root) {
  const args = [];
  for (const arg of spec.args ?? []) {
    if (typeof arg === 'string') { args.push(arg); continue; }
    const dir = path.join(root, arg.files.dir);
    const pattern = new RegExp(arg.files.pattern);
    if (!fs.existsSync(dir)) continue;
    for (const name of fs.readdirSync(dir).filter(n => pattern.test(n)).sort()) {
      args.push(path.relative(root, path.join(dir, name)));
    }
  }
  return { command: spec.command, args };
}
