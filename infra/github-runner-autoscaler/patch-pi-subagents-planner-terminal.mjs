// Narrow, version-pinned integration repair for pi-subagents 0.76.1.
// Its foreground text-output gate treats a successful terminal submit_plan as
// failure because the terminating assistant toolUse turn has no final prose.
// Main revalidates this same per-lifecycle sidecar independently.
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { restoredWork } from '../../scripts/pi-common/restored-work.mjs';

const PINNED_VERSION = '0.76.1';

export function acceptedTerminalPlannerReceipt(messages, state, lifecycleId) {
  if (!Array.isArray(messages) || !state || state.phase !== 'submitted' ||
      state.failureKind || typeof state.planText !== 'string' || !state.planText.trim() ||
      !lifecycleId || !state.submissionReceipt) return false;
  const receipt = state.submissionReceipt;
  const budget = state.submissionBudget;
  const history = state.budgetHistory;
  const lastBudget = Array.isArray(history) ? history.at(-1) : null;
  if (receipt.lifecycleId !== lifecycleId ||
      typeof receipt.toolCallId !== 'string' || !receipt.toolCallId ||
      receipt.admitted !== true || receipt.executed !== true ||
      receipt.providerComplete !== true || receipt.stopReason !== 'tooluse' ||
      receipt.providerBudgetVerified !== true ||
      receipt.planTextBytes !== Buffer.byteLength(state.planText, 'utf8') ||
      (budget !== 4096 && budget !== 8192) ||
      receipt.submissionBudget !== budget || lastBudget?.phase !== 'submission_pending' ||
      lastBudget?.verified !== true || lastBudget.effective !== budget) return false;
  const assistant = messages.findLast(message => message?.role === 'assistant');
  if (!assistant || assistant.stopReason !== 'toolUse' || assistant.errorMessage) return false;
  const calls = Array.isArray(assistant.content)
    ? assistant.content.filter(part => part?.type === 'toolCall') : [];
  if (calls.length !== 1 || calls[0].name !== 'submit_plan' ||
      calls[0].id !== receipt.toolCallId) return false;
  const results = messages.filter(message => message?.role === 'toolResult' &&
    message.toolCallId === receipt.toolCallId);
  return results.length === 1 && results[0].isError === false;
}

// Only the pinned coding-session agent may complete on terminal toolUse without
// assistant prose. Both the executed tool envelope and this run's durable receipt
// must agree. The parent independently rechecks the full Git candidate revision.
export function acceptedTerminalImplementerReceipt(messages, receipt, metadataBytes, env, expectedSessionId, errInfo = {}) {
  if (!Array.isArray(messages) || !receipt || !env || !expectedSessionId ||
      !metadataBytes || receipt.kind !== 'pi_terminal_receipt' ||
      receipt.schema_version !== 2 || receipt.status !== 'success' ||
      !['changed', 'already_satisfied', 'blocked'].includes(receipt.outcome)) return false;
  const run = String(env.PI_VALIDATION_RUN_ID || '').trim() ||
    (env.GITHUB_RUN_ID ? String(env.GITHUB_RUN_ID) + '-' + String(env.GITHUB_RUN_ATTEMPT || '1') : '');
  const issue = String(env.PI_ISSUE || env.ISSUE || '').trim();
  const attempt = env.PI_VALIDATION_REPAIR === 'true'
    ? 'validation-repair:' + (String(env.PI_VALIDATION_REPAIR_ATTEMPT || '1').trim() || '1')
    : 'primary';
  if (!run || !issue || receipt.run_id !== run || receipt.issue !== issue ||
      receipt.attempt_id !== attempt || receipt.session_id !== expectedSessionId ||
      !/^[0-9a-f]{64}$/.test(receipt.candidate_revision?.digest || '') ||
      !/^[0-9a-f]{40}$/.test(receipt.candidate_revision?.base_commit || '') ||
      receipt.result_metadata_sha256 !== createHash('sha256').update(metadataBytes).digest('hex')) return false;
  let metadata;
  try { metadata = JSON.parse(metadataBytes.toString('utf8')); }
  catch { return false; }
  if (metadata?.outcome !== receipt.outcome || !Array.isArray(metadata.files)) return false;
  if (receipt.outcome === 'changed') {
    const accepted = metadata.accepted_scope?.accepted;
    if (!Array.isArray(accepted) || metadata.files.length === 0 ||
        !metadata.files.every(file => typeof file === 'string' && file &&
          accepted.some(item => item?.path === file))) return false;
  } else if (metadata.files.length !== 0) return false;

  const lastAssistant = messages.findLastIndex(message => message?.role === 'assistant');
  if (lastAssistant < 0) return false;
  const assistant = messages[lastAssistant];
  if (assistant.stopReason !== 'toolUse' || assistant.errorMessage) return false;
  if (messages.some(message => message?.role === 'assistant' && message.errorMessage)) return false;
  const calls = Array.isArray(assistant.content)
    ? assistant.content.filter(part => part?.type === 'toolCall') : [];
  if (calls.length !== 1 || calls[0].name !== 'submit_result' ||
      typeof calls[0].id !== 'string' || !calls[0].id) return false;
  const results = messages.slice(lastAssistant + 1).filter(message =>
    message?.role === 'toolResult' && message.toolCallId === calls[0].id);
  if (results.length !== 1 || results[0].isError !== false ||
      results[0].toolName !== 'submit_result') return false;
  if (messages.slice(0, lastAssistant).some(message =>
    message?.role === 'toolResult' && message.toolCallId === calls[0].id)) return false;
  let args = calls[0].arguments;
  try { if (typeof args === 'string') args = JSON.parse(args); }
  catch { return false; }
  if (!args || typeof args !== 'object' || Array.isArray(args)) return false;
  const keys = Object.keys(args);
  const runtimeOwned = env.PI_VALIDATION_REPAIR === 'true' || restoredWork(env);
  if (receipt.outcome === 'changed') {
    if (runtimeOwned) {
      if (keys.length !== 0 || metadata.result_text != null) return false;
    } else if (keys.length !== 1 || keys[0] !== 'resultText' ||
        typeof metadata.result_text !== 'string' || !metadata.result_text.trim() ||
        args.resultText !== metadata.result_text) return false;
  } else if (receipt.outcome === 'already_satisfied') {
    if (!(runtimeOwned && keys.length === 0) &&
        !(keys.length === 1 && args.already_satisfied === true)) return false;
  } else if (keys.length !== 1 || typeof args.blocked_reason !== 'string' ||
      args.blocked_reason.trim() !== metadata.blocked_reason) return false;
  // Recoverable earlier tool failures are not a second terminal failure.
  // A provider/abort/timeout failure must never be hidden by a stale marker.
  if (errInfo?.hasError) {
    if (/provider|abort|cancel|timeout|transport|network|signal/i.test(String(errInfo.errorType || ''))) return false;
    if (!messages.slice(0, lastAssistant).some(message =>
      message?.role === 'toolResult' && message.isError === true)) return false;
  }
  return true;
}

export function patchPiSubagentsSource(source) {
  const importAnchor = 'import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";';
  const helperAnchor = 'const artifactOutputByResult = new WeakMap();';
  const decisionAnchor = 'const missingOutput = !finalText?.trim() && !validatedStructuredOutput;';
  const conditionAnchor = 'if ((missingOutput || terminalEmptyAfterUsefulWork) && (!errInfo.hasError || hasEmptyTerminalAssistantResponse(messages))) {';
  const errorConditionAnchor = 'else if (errInfo.hasError) {';
  const ensureOnce = (text, fragment) => {
    if (text.split(fragment).length !== 2) throw new Error('pi-subagents 0.76.1 source drift: ' + fragment);
  };
  ensureOnce(source, importAnchor);
  ensureOnce(source, helperAnchor);
  ensureOnce(source, decisionAnchor);
  ensureOnce(source, conditionAnchor);
  ensureOnce(source, errorConditionAnchor);
  const policy = acceptedTerminalPlannerReceipt.toString();
  // Inline the shared predicate: installed pi-subagents cannot import our source tree.
  const restorePolicy = restoredWork.toString();
  const implementerPolicy = acceptedTerminalImplementerReceipt.toString();
  const guard = [
    '// Only verified Planner or Implementer terminal toolUse may omit final text.',
    '// An actual executed tool result AND a current-lifecycle durable receipt are required.',
    policy,
    restorePolicy,
    implementerPolicy,
    'function trustedPlannerTerminalToolUse(messages, agentName) {',
    '  if (agentName !== "implementation-planner") return false;',
    '  const sidecar = process.env.PI_PLANNER_EVIDENCE_STATE_FILE;',
    '  const lifecycleId = process.env.PI_PLANNER_LIFECYCLE_ID;',
    '  if (!sidecar || !lifecycleId) return false;',
    '  try {',
    '    return acceptedTerminalPlannerReceipt(messages, JSON.parse(readFileSync(sidecar, "utf8")), lifecycleId);',
    '  } catch (error) {',
    '    // Log only the exception category; sidecar paths and plan content remain private.',
    '    const kind = error?.name === "ReferenceError" ? "injected_dependency_missing" : "receipt_unavailable";',
    '    console.warn("PI_PLANNER_TERMINAL_RECEIPT_CHECK_FAILED " + JSON.stringify({ kind }));',
    '    return false;',
    '  }',
    '}',
    'function trustedImplementerTerminalToolUse(messages, agentName, errInfo) {',
    '  if (agentName !== "implementer-coding-session") return false;',
    '  // This injected code runs in the parent Node process, not in the coding fork.',
    '  // pi-agent-runtime keeps its process-global session ID single-flight through',
    '  // delegation and receipt handling. Separate runner processes have separate env.',
    '  const env = process.env;',
    '  if (!env.PI_IMPLEMENTER_SUBAGENT_TERMINAL_SESSION_ID || !env.PI_TERMINAL_RESULT_FILE || !env.PI_IMPLEMENTER_RESULT_FILE) return false;',
    '  try {',
    '    return acceptedTerminalImplementerReceipt(messages,',
    '      JSON.parse(readFileSync(env.PI_TERMINAL_RESULT_FILE, "utf8")),',
    '      readFileSync(env.PI_IMPLEMENTER_RESULT_FILE),',
    '      env, env.PI_IMPLEMENTER_SUBAGENT_TERMINAL_SESSION_ID, errInfo);',
    '  } catch (error) {',
    '    const kind = error?.name === "ReferenceError" ? "injected_dependency_missing" : "receipt_unavailable";',
    '    console.warn("PI_IMPLEMENTER_TERMINAL_RECEIPT_CHECK_FAILED " + JSON.stringify({ kind }));',
    '    return false;',
    '  }',
    '}',
    '',
  ].join('\n');
  return source
    .replace(importAnchor, () => 'import { existsSync, statSync, mkdirSync, rmSync, writeFileSync, readFileSync } from "node:fs";\nimport { createHash } from "node:crypto";')
    .replace(helperAnchor, () => guard + helperAnchor)
    .replace(decisionAnchor, () => 'const acceptedTerminalPlan = trustedPlannerTerminalToolUse(messages, agent.name);\n\t\tconst acceptedTerminalImplementer = trustedImplementerTerminalToolUse(messages, agent.name, errInfo);\n\t\tconst acceptedTerminalToolUse = acceptedTerminalPlan || acceptedTerminalImplementer;\n\t\t' + decisionAnchor)
    .replace(conditionAnchor, () => 'if (!acceptedTerminalToolUse && (missingOutput || terminalEmptyAfterUsefulWork) && (!errInfo.hasError || hasEmptyTerminalAssistantResponse(messages))) {')
    // An earlier recoverable evidence-tool failure must not override a verified
    // successful terminal submission. Without the exact receipt, preserve the
    // original upstream hidden-error classification.
    .replace(errorConditionAnchor, () => 'else if (!acceptedTerminalToolUse && errInfo.hasError) {');
}

function main() {
  const root = process.argv[2];
  if (!root) throw new Error('Pass the installed pi-subagents package directory');
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  if (pkg.name !== 'pi-subagents' || pkg.version !== PINNED_VERSION) {
    throw new Error('Expected pi-subagents@' + PINNED_VERSION + ', got ' + pkg.name + '@' + pkg.version);
  }
  // npm publishes compiled JavaScript in dist-pkg; the GitHub tag contains TypeScript only.
  const target = path.join(root, 'src/runs/foreground/execution.js');
  const original = fs.readFileSync(target, 'utf8');
  const patched = patchPiSubagentsSource(original);
  fs.writeFileSync(target, patched);
  console.log('PI_SUBAGENTS_PLANNER_TERMINAL_PATCH applied version=' + PINNED_VERSION);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
