#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${RUNNER_TOKEN:?RUNNER_TOKEN is required}"
: "${RUNNER_NAME:?RUNNER_NAME is required}"
: "${RUNNER_LABELS:=n150,pi-agent}"
RUNNER_HOME="${RUNNER_HOME:-/home/runner}"

# Pi needs writable state for lock files and refreshed auth/model metadata.
# Seed a private copy from the host-mounted read-only configuration.
if [ -d /pi-config-ro ]; then
  rm -rf /home/runner/.pi/agent
  mkdir -p /home/runner/.pi/agent
  cp -a /pi-config-ro/. /home/runner/.pi/agent/
fi

# The host-mounted Pi config may have older adapter/extension installations.
# Overlay packages baked into this image, pin their versions, and permit
# project MCP servers for headless `pi` invocations used by GitHub Actions.
if [ -d /opt/pi-package-seed/npm ]; then
  mkdir -p /home/runner/.pi/agent/npm
  cp -a /opt/pi-package-seed/npm/. /home/runner/.pi/agent/npm/
  node --input-type=module <<'NODE'
import fs from 'node:fs';

const file = '/home/runner/.pi/agent/settings.json';
let settings = {};
try {
  settings = JSON.parse(fs.readFileSync(file, 'utf8'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
const packages = Array.isArray(settings.packages) ? settings.packages : [];
const extensions = Array.isArray(settings.extensions) ? settings.extensions : [];
const adapterVersion = process.env.PI_MCP_ADAPTER_VERSION;
const subagentsVersion = process.env.PI_SUBAGENTS_VERSION;
if (!adapterVersion || !subagentsVersion) throw new Error('Pi extension versions are required');
const pinnedPackages = [`npm:pi-mcp-adapter@${adapterVersion}`, `npm:pi-subagents@${subagentsVersion}`];
const withoutPinnedPackages = packages.filter((item) => {
  const source = typeof item === 'string' ? item : item?.source;
  return typeof source !== 'string' || !/^npm:pi-(?:mcp-adapter|subagents)(?:@|$)/.test(source);
});
settings.packages = [...pinnedPackages, ...withoutPinnedPackages];
// pi-mcp-adapter owns the /mcp command in this image. Disable Pi's built-in
// MCP extension explicitly so clean and migrated host configs use one adapter.
settings.extensions = [...extensions.filter((item) => item !== 'builtin:mcp' && item !== '-builtin:mcp'), '-builtin:mcp'];
// Jobs run in a disposable, per-job container. Trust that job's checkout so
// Pi's headless CI mode loads its project .mcp.json and other project config.
settings.defaultProjectTrust = 'always';
fs.writeFileSync(file, `${JSON.stringify(settings, null, 2)}\n`, { mode: 0o600 });

const adapterConfigFile = '/home/runner/.pi/agent/mcp-adapter.json';
let adapterConfig = {};
try {
  adapterConfig = JSON.parse(fs.readFileSync(adapterConfigFile, 'utf8'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
const legacyConfigFile = '/home/runner/.pi/agent/mcp.json';
try {
  const legacyConfig = JSON.parse(fs.readFileSync(legacyConfigFile, 'utf8'));
  // pi-mcp-adapter 3.x moved the global configuration to mcp-adapter.json.
  // Carry over servers and adapter options from the existing host config so
  // the image upgrade does not silently drop Context7/SearXNG or Orbit MCP.
  adapterConfig.mcpServers = { ...(legacyConfig.mcpServers ?? {}), ...(adapterConfig.mcpServers ?? {}) };
  adapterConfig.settings = { ...(legacyConfig.settings ?? {}), ...(adapterConfig.settings ?? {}) };
  for (const [key, value] of Object.entries(legacyConfig)) {
    if (key !== 'mcpServers' && key !== 'settings' && !(key in adapterConfig)) adapterConfig[key] = value;
  }
  fs.renameSync(legacyConfigFile, `${legacyConfigFile}.migrated`);
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
adapterConfig.settings = { ...(adapterConfig.settings ?? {}), projectServers: 'allow' };
fs.writeFileSync(adapterConfigFile, `${JSON.stringify(adapterConfig, null, 2)}\n`, { mode: 0o600 });
NODE
  node /usr/local/bin/check-pi-searxng-mcp
fi

# Best-effort durable evidence (#437): this container is --rm, so keep a bounded
# trail on the shared volume when the manager provides one.
EVIDENCE_DIR="${INFRA_EVIDENCE_DIR:-}"
if [ -z "$EVIDENCE_DIR" ] || [ ! -d "$EVIDENCE_DIR" ] || [ ! -w "$EVIDENCE_DIR" ]; then EVIDENCE_DIR=""; fi
worker_evidence() {
  [ -n "$EVIDENCE_DIR" ] || return 0
  printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUNNER_NAME" "$(printf '%s' "${1:0:2000}" | tr '\n' ' ')" >> "$EVIDENCE_DIR/worker-events.log" 2>/dev/null || true
}

if [[ ",${RUNNER_LABELS}," == *,general,* ]]; then
  for check in info metadata; do
    attempts=0
    while true; do
      if [ "$check" == info ]; then
        if output="$(timeout 30 docker info 2>&1)"; then break; fi
      else
        if output="$(timeout 30 docker system df 2>&1)"; then break; fi
      fi
      if [ "$attempts" -eq 0 ]; then
        attempts=1
        echo "warning: Docker $check health check failed; retrying once in 5s: $output" >&2
        sleep 5
        continue
      fi
      if [ "$check" == info ]; then
        echo "infra_error DOCKER_DAEMON_UNHEALTHY: $output" >&2
        worker_evidence "infra_error DOCKER_DAEMON_UNHEALTHY: $output"
      else
        echo "infra_error DOCKER_METADATA_CORRUPTION: $output; general runner will not register" >&2
        worker_evidence "infra_error DOCKER_METADATA_CORRUPTION: $output"
      fi
      exit 1
    done
  done
fi

cd "${RUNNER_HOME}/actions-runner"

./config.sh \
  --url "https://github.com/${GITHUB_REPOSITORY}" \
  --token "${RUNNER_TOKEN}" \
  --name "${RUNNER_NAME}" \
  --labels "${RUNNER_LABELS}" \
  --work "_work" \
  --ephemeral \
  --unattended \
  --disableupdate

if [ -z "$EVIDENCE_DIR" ]; then
  exec ./run.sh
fi

# Keep the tail of the runner's own diagnostics so a `Set up job` failure stays
# explainable after the container and the GitHub job log are gone. Forward stop
# signals because this shell is PID 1 and no longer exec'd into the runner.
./run.sh &
runner_pid=$!
trap 'kill -TERM "$runner_pid" 2>/dev/null || true' TERM INT
status=0
while kill -0 "$runner_pid" 2>/dev/null; do
  wait "$runner_pid" || status=$?
done
{
  latest="$(ls -t _diag/Worker_*.log 2>/dev/null | head -n 1)"
  [ -z "$latest" ] || tail -n 200 "$latest" > "$EVIDENCE_DIR/${RUNNER_NAME}-worker-diag.log"
  worker_evidence "runner exited status=$status"
  ls -t "$EVIDENCE_DIR"/*-worker-diag.log 2>/dev/null | tail -n +51 | xargs -r rm -f
  if [ "$(wc -l < "$EVIDENCE_DIR/worker-events.log")" -gt 500 ]; then
    tail -n 500 "$EVIDENCE_DIR/worker-events.log" > "$EVIDENCE_DIR/worker-events.log.tmp" \
      && mv "$EVIDENCE_DIR/worker-events.log.tmp" "$EVIDENCE_DIR/worker-events.log"
  fi
} 2>/dev/null || true
exit "$status"
