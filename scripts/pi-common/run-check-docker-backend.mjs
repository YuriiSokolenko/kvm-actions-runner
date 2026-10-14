const DEFAULT_EXECUTOR_URL = 'http://127.0.0.1:17343';

function infra(code, message, command = 'trusted-run-check-executor') {
  return { component: 'sandbox', code, command, message };
}

function sameEnvironmentContract(expected, actual) {
  if (!expected || !actual || expected.version !== actual.version) return false;
  if (!Array.isArray(expected.keys) || !Array.isArray(actual.keys)) return false;
  return expected.keys.length === actual.keys.length
    && [...expected.keys].sort().every((key, index) => key === [...actual.keys].sort()[index]);
}

async function requestExecutor(env, payload, timeoutMs) {
  const base = env.PI_RUN_CHECK_EXECUTOR_URL || DEFAULT_EXECUTOR_URL;
  const token = env.RUN_CHECK_EXECUTOR_TOKEN;
  const runnerName = env.RUNNER_NAME || env.ACTIONS_RUNNER_NAME;
  if (!token || !runnerName) throw Object.assign(new Error('trusted run_check executor identity is unavailable'), { code: 'EXECUTOR_CONFIG' });

  let response;
  try {
    response = await fetch(`${base}${payload.operation === 'preflight' ? '/v1/preflight' : '/v1/run-check'}`, {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify({ runner_name: runnerName, ...payload.body }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    throw Object.assign(new Error(`trusted run_check executor request failed: ${error.message}`), { code: error.name === 'TimeoutError' ? 'EXECUTOR_TIMEOUT' : 'EXECUTOR_UNAVAILABLE' });
  }

  let result;
  try { result = await response.json(); } catch {
    throw Object.assign(new Error(`trusted run_check executor returned HTTP ${response.status} without JSON`), { code: 'EXECUTOR_PROTOCOL' });
  }
  if (!response.ok) {
    throw Object.assign(new Error(result?.error?.message || `trusted run_check executor returned HTTP ${response.status}`), {
      code: result?.error?.code || 'EXECUTOR_HTTP',
      status: result?.error?.status,
    });
  }
  return result;
}

export function createDockerSandboxBackend(env = process.env) {
  return {
    async run({ root, request, env: cleanEnv, timeoutMs }) {
      try {
        const result = await requestExecutor(env, {
          operation: 'run-check',
          body: { root, params: request, env: cleanEnv, timeout_ms: timeoutMs },
        }, timeoutMs + 30000);
        if (result.status === 'invalid') return { requestInvalid: result };
        if (result.infrastructure) return { infrastructure: result.infrastructure, ...result };
        return { ...result, sandboxRoot: '/workspace' };
      } catch (error) {
        return { infrastructure: infra(error.code || 'EXECUTOR_UNAVAILABLE', error.message) };
      }
    },

    async preflight({ root, env: cleanEnv, envContract, timeoutMs }) {
      try {
        const result = await requestExecutor(env, {
          operation: 'preflight',
          body: { root, env: cleanEnv, env_contract: envContract },
        }, timeoutMs + 30000);
        if (!sameEnvironmentContract(envContract, result.environment_contract)) {
          return {
            ok: false,
            status: 'infra_error',
            summary: 'INFRASTRUCTURE ERROR: trusted run_check executor environment contract does not match the runtime',
            infrastructure: {
              component: 'sandbox',
              code: 'CHECK_ENV_CONTRACT',
              command: 'trusted-run-check-executor',
              message: 'trusted run_check executor environment contract does not match the runtime',
            },
            diagnostics: [], stdout_tail: '', stderr_tail: '', truncated: false,
          };
        }
        return result;
      } catch (error) {
        return {
          ok: false,
          status: 'infra_error',
          summary: `INFRASTRUCTURE ERROR: ${error.message}`,
          infrastructure: { component: 'sandbox', code: error.code || 'EXECUTOR_UNAVAILABLE', command: 'trusted-run-check-executor' },
          diagnostics: [], stdout_tail: '', stderr_tail: '', truncated: false,
        };
      }
    },
  };
}
