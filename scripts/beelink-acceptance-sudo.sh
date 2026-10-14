#!/usr/bin/env bash
# Beelink N150 acceptance and coordinated runner cutover.
# This script never prints token values. --preflight is read-only.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly SCRIPT_DIR REPO_ROOT
readonly COMPOSE_FILE="${REPO_ROOT}/compose.yaml"
readonly CONFIG_FILE="${REPO_ROOT}/config/pools.json"
readonly ENV_FILE="${N150_ENV_FILE:-${REPO_ROOT}/.env}"
readonly BACKUP_ROOT="${N150_BACKUP_ROOT:-/var/backups/n150-unified-acceptance}"
OLD_DOCKER_FILE="${OLD_DOCKER_COMPOSE_FILE:-/home/yurasik/github-runners/.beelink-update-20261010T063413Z-40490/infra/github-runner-autoscaler/compose.yaml}"
OLD_DOCKER_ENV="${OLD_DOCKER_ENV_FILE:-/home/yurasik/github-runners/social-mcp-deploy-210120f4/infra/github-runner-autoscaler/.env}"
OLD_KVM_FILE="${OLD_KVM_COMPOSE_FILE:-/home/yurasik/github-runners/social-mcp-pr-794/infra/github-runner-autoscaler/compose.yaml}"
OLD_KVM_ENV="${OLD_KVM_ENV_FILE:-/home/yurasik/github-runners/social-mcp-pr-794/infra/github-runner-autoscaler/.env}"
readonly GOLDEN_SHA256="9fc0ce202ba8def25b126323af21d5a5bb0fb8903cb7367f2fc246ae568e1706"
readonly ALL_OLD_MANAGERS=(pi-runner-manager general-runner-manager recursive-coder-kvm-runner-manager)
readonly DOCKER_VOLUMES=(social-mcp-runner-scheduler social-mcp-control-runner-state social-mcp-general-runner-evidence social-mcp-run-check-stage)
readonly BUILT_IMAGES=(n150/unified-runner-scheduler:0.1.0 n150/unified-docker-executor:0.1.0 n150/unified-kvm-executor:0.1.0 n150/unified-run-check-executor:0.1.0 n150/github-pi-runner-ephemeral:1.1.0-mini-swe-r5 n150/github-general-runner-ephemeral:0.87.11 n150/github-control-runner:0.1.8 n150/run-check-sandbox:0.1.4)

MODE="${1:-}"
LOG_FILE=""
BACKUP_DIR=""

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() {
  log "BLOCKED: $*"
  if [[ -n "$LOG_FILE" ]]; then
    log "ACTION: no automatic cleanup was attempted. Review ${LOG_FILE}; after jobs and VMs are drained, use --rollback if the cutover had started."
  else
    log "ACTION: preflight made no service or file changes. Fix the stated issue and rerun --preflight."
  fi
  exit 1
}
on_error() {
  local rc=$?
  if [[ -n "$LOG_FILE" ]]; then
    log "FAILURE: command at line ${BASH_LINENO[0]:-unknown} exited ${rc}. No automatic cleanup was attempted."
    log "ACTION: inspect ${LOG_FILE} and ${BACKUP_DIR:-$BACKUP_ROOT}; run --rollback only after jobs and KVM guests are drained."
  else
    log "FAILURE: command at line ${BASH_LINENO[0]:-unknown} exited ${rc}. No services were changed in --preflight."
  fi
  exit "$rc"
}
trap on_error ERR

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/beelink-acceptance-sudo.sh --preflight|--apply|--rollback

--preflight  Privileged read-only inventory and cutover safety checks.
--apply      Build and run the coordinated cutover after all preconditions pass.
--rollback   Stop the unified scheduler after jobs drain and restore old Compose stacks.

Optional environment overrides:
  N150_ENV_FILE, N150_BACKUP_ROOT, KVM_GOLDEN_HOST_DIR,
  OLD_DOCKER_COMPOSE_FILE, OLD_DOCKER_ENV_FILE,
  OLD_KVM_COMPOSE_FILE, OLD_KVM_ENV_FILE, N150_BACKUP_DIR
EOF
}

require_root() { [[ ${EUID} -eq 0 ]] || die "run with sudo: sudo bash $0 ${MODE}"; }
require_tools() {
  local tool
  for tool in docker python3 sha256sum stat ss pgrep; do
    command -v "$tool" >/dev/null 2>&1 || die "required command is missing: ${tool}"
  done
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable to root."
  docker compose version >/dev/null 2>&1 || die "Docker Compose plugin is unavailable."
}

compose() { docker compose --project-directory "$REPO_ROOT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }
old_docker_compose() {
  local project_dir
  project_dir="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' pi-runner-manager)"
  docker compose --project-directory "$project_dir" --env-file "$OLD_DOCKER_ENV" -f "$OLD_DOCKER_FILE" "$@"
}
old_kvm_compose() {
  local project_dir
  project_dir="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' recursive-coder-kvm-runner-manager)"
  docker compose --project-directory "$project_dir" --env-file "$OLD_KVM_ENV" -f "$OLD_KVM_FILE" "$@"
}

need_env_key() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 1
  python3 - "$ENV_FILE" "$key" <<'PY'
import pathlib, sys
path, wanted = sys.argv[1:]
value = ""
for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    key, sep, raw = line.partition("=")
    if sep and key.strip() == wanted:
        value = raw.strip().strip("\"'")
        break
print("yes" if value else "no")
PY
}

env_value() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import pathlib, sys
path, wanted, default = sys.argv[1:]
for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line.startswith("export "):
        line = line[7:].lstrip()
    key, sep, raw = line.partition("=")
    if sep and key.strip() == wanted:
        print(raw.strip().strip("\"'"))
        raise SystemExit(0)
print(default)
PY
}

container_env_api() {
  local container="$1" repo="$2"
  docker inspect --format '{{json .Config.Env}}' "$container" | python3 -c '
import json, sys, urllib.error, urllib.request
repo = sys.argv[1]
env = dict(item.split("=", 1) for item in json.load(sys.stdin) if "=" in item)
token = (env.get("RECURSIVE_CODER_GH_ADMIN_TOKEN") if repo.endswith("/recursive-coder") else None) or env.get("GH_ADMIN_TOKEN", "")
if not token:
    print(json.dumps({"ok": False, "error": "credential unavailable"})); raise SystemExit(0)
url = "https://api.github.com/repos/" + repo + "/actions/runners?per_page=100"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
try:
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.load(response)
    runners = data.get("runners", [])
    print(json.dumps({"ok": True, "count": data.get("total_count", len(runners)), "busy": [r.get("name", "unknown") for r in runners if r.get("busy")], "offline": [r.get("name", "unknown") for r in runners if r.get("status") != "online"]}))
except urllib.error.HTTPError as exc:
    print(json.dumps({"ok": False, "error": "GitHub API HTTP " + str(exc.code)}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__}))
' "$repo"
}

runner_api_checks() {
  local result
  for spec in 'pi-runner-manager YuriiSokolenko/social-mcp' 'recursive-coder-kvm-runner-manager YuriiSokolenko/recursive-coder'; do
    read -r manager repo <<<"$spec"
    docker inspect "$manager" >/dev/null 2>&1 || die "expected legacy manager is missing: ${manager}"
    result="$(container_env_api "$manager" "$repo")"
    log "GitHub runner API (${repo}): ${result}"
    python3 -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if d.get("ok") and not d.get("busy") else 1)' "$result" \
      || die "GitHub runner API is unavailable or at least one runner is busy for ${repo}. Drain jobs, then retry."
  done
}

check_no_worker_processes() {
  local c top
  while IFS= read -r c; do
    [[ -n "$c" ]] || continue
    top="$(docker top "$c" -eo args 2>/dev/null || true)"
    if grep -Eiq 'Runner\.Worker|qemu-system|qemu-kvm' <<<"$top"; then
      log "BLOCKED: active runner worker or guest process found in ${c}."
      return 1
    fi
  done < <(docker ps --format '{{.Names}}' | grep -E '^(n150-(pi-eph|gen-eph|rc-eph)-|recursive-coder-kvm-runner-manager$)' || true)
  if pgrep -af '[q]emu-system|[q]emu-kvm' >/dev/null; then
    log "BLOCKED: host QEMU process exists."
    return 1
  fi
}

check_config_and_credentials() {
  [[ -f "$COMPOSE_FILE" && -f "$CONFIG_FILE" ]] || die "run this script from the committed repository checkout."
  [[ -f "$ENV_FILE" ]] || die "new deployment env file is missing: ${ENV_FILE}; copy .env.example and set the required values."
  [[ "$(need_env_key SOCIAL_MCP_GH_TOKEN)" == yes ]] || die "SOCIAL_MCP_GH_TOKEN is missing or empty in ${ENV_FILE}."
  [[ "$(need_env_key RECURSIVE_CODER_GH_TOKEN)" == yes ]] || die "RECURSIVE_CODER_GH_TOKEN is missing or empty in ${ENV_FILE}."
  [[ "$(need_env_key MODEL_STATUS_URL)" == yes ]] || die "MODEL_STATUS_URL is missing or empty in ${ENV_FILE}; Pi admission must use the host model endpoint."
  local model_probe
  model_probe="$(python3 - "$ENV_FILE" <<'PY'
import pathlib, sys, urllib.error, urllib.request
url = ""
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, sep, value = line.partition("=")
    if sep and key.strip() == "MODEL_STATUS_URL":
        url = value.strip().strip("\"'")
        break
try:
    with urllib.request.urlopen(url, timeout=8) as response:
        response.read(1)
        print("HTTP " + str(response.status))
except urllib.error.HTTPError as error:
    print("HTTP " + str(error.code))
except Exception as error:
    print("unreachable (" + type(error).__name__ + ")")
PY
  )"
  [[ "$model_probe" == HTTP\ 2* ]] || die "Pi model metrics endpoint is unavailable (${model_probe}); restore it before cutover so model-gated scheduling can be validated."
  python3 - "$ENV_FILE" <<'PY' || die "repository tokens must be distinct values in ${ENV_FILE}."
import pathlib, sys
values = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key, sep, value = line.partition("=")
    if sep:
        values[key.strip()] = value.strip().strip("\"'")
sys.exit(0 if values.get("SOCIAL_MCP_GH_TOKEN") and values.get("RECURSIVE_CODER_GH_TOKEN") and values["SOCIAL_MCP_GH_TOKEN"] != values["RECURSIVE_CODER_GH_TOKEN"] else 1)
PY
  local pi_config kvm_state
  pi_config="$(python3 - "$ENV_FILE" <<'PY'
import pathlib, sys
values = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, sep, value = line.partition("=")
    if sep: values[key.strip()] = value.strip().strip("\"'")
print(values.get("PI_CONFIG_HOST_DIR", "/var/lib/n150/pi-agent"))
PY
  )"
  kvm_state="$(python3 - "$ENV_FILE" <<'PY'
import pathlib, sys
values = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, sep, value = line.partition("=")
    if sep: values[key.strip()] = value.strip().strip("\"'")
print(values.get("KVM_STATE_HOST_DIR", "/var/lib/recursive-coder-kvm/state"))
PY
  )"
  [[ -d "$pi_config" ]] || die "Pi configuration directory is missing: ${pi_config}."
  [[ -d "$kvm_state" ]] || die "KVM state directory is missing: ${kvm_state}."
  compose config --quiet || die "Compose configuration is invalid."
  log "New Compose configuration, required credential presence, distinct repository tokens, model endpoint (${model_probe}), and bind paths: PASS (secret values redacted)."
}

check_volumes() {
  local v
  for v in "${DOCKER_VOLUMES[@]}"; do
    docker volume inspect "$v" >/dev/null 2>&1 || die "required existing named volume is missing: ${v}; do not create an empty replacement."
    log "Protected named volume exists: ${v}"
  done
}

check_golden() {
  local golden_image
  golden_image="$(env_value KVM_GOLDEN_HOST_DIR /var/lib/recursive-coder-kvm/golden)/noble-server-cloudimg-amd64-prepared-9fc0ce202ba8.img"
  [[ -r "$golden_image" ]] || die "Golden Image v2 is not readable at ${golden_image}."
  local digest
  digest="$(sha256sum "$golden_image" | awk '{print $1}')"
  [[ "$digest" == "$GOLDEN_SHA256" ]] || die "Golden Image v2 checksum mismatch; expected ${GOLDEN_SHA256}, got ${digest}. No image was changed."
  log "Pinned Golden Image v2 SHA-256: PASS (${digest})."
}

check_legacy_compose() {
  [[ -r "$OLD_DOCKER_FILE" && -r "$OLD_DOCKER_ENV" ]] || die "previous Docker runner Compose file or env file is missing; configure OLD_DOCKER_COMPOSE_FILE and OLD_DOCKER_ENV_FILE."
  [[ -r "$OLD_KVM_FILE" && -r "$OLD_KVM_ENV" ]] || die "previous KVM Compose file or env file is missing; configure OLD_KVM_COMPOSE_FILE and OLD_KVM_ENV_FILE."
}

kvm_admission_snapshot() {
  local kvm_state
  kvm_state="$(env_value KVM_STATE_HOST_DIR /var/lib/recursive-coder-kvm/state)"
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" python3 - "$CONFIG_FILE" "$kvm_state" <<'PY'
import json, os, sys
from n150_runner.admission import admit, host_metrics
from n150_runner.config import load_config
config = load_config(sys.argv[1])
metrics = host_metrics()
pool = next(p for p in config.pools if p.id == "recursive-coder-kvm")
state = sys.argv[2]
available_mb = (os.statvfs(state).f_bavail * os.statvfs(state).f_frsize) // (1024 * 1024)
counts = {p.id: 0 for p in config.pools}
counts["control"] = 1
reason = admit(pool, config.host, metrics, counts=counts, pools=config.pools,
               model_available=config.host.model_max_concurrency,
               kvm_disk_free_mb=available_mb)
print(json.dumps({"cpu_count": metrics.cpu_count, "load_1m": round(metrics.load_1m, 2),
                  "mem_available_mb": metrics.mem_available_mb, "kvm_disk_free_mb": available_mb,
                  "hypothetical_kvm_admission": reason}))
PY
}

check_preconditions() {
  require_root
  require_tools
  [[ "$(uname -s)" == Linux ]] || die "target must be Ubuntu Linux."
  grep -qi ubuntu /etc/os-release || die "target is not Ubuntu Linux."
  local cpu_count
  cpu_count="$(getconf _NPROCESSORS_ONLN)"
  (( cpu_count == 4 )) || die "expected four online CPUs on Beelink N150; observed ${cpu_count}."
  lscpu | grep -Eiq 'Model name:.*Intel\(R\) N150' || die "CPU model is not Intel N150."
  [[ -c /dev/kvm ]] || die "/dev/kvm is unavailable."
  [[ -S /var/run/docker.sock ]] || die "Docker Unix socket is missing."
  [[ "$(stat -Lc '%a:%U:%G' /var/run/docker.sock)" == 660:root:docker ]] || die "Docker socket must be mode 660 and root:docker; review ownership before cutover."
  check_config_and_credentials
  check_volumes
  check_golden
  check_legacy_compose
  for c in "${ALL_OLD_MANAGERS[@]}" n150-control; do
    docker inspect "$c" >/dev/null 2>&1 || die "required previous container is missing: ${c}. Reinspect host inventory before cutover."
  done
  runner_api_checks
  check_no_worker_processes || die "active job/VM detected; do not stop services."
}

preflight() {
  check_preconditions
  # shellcheck disable=SC1091
  log "Host: $(hostname), $(. /etc/os-release; printf '%s %s' "$PRETTY_NAME" "${VERSION_ID:-}"); kernel $(uname -r); CPUs ${cpu_count:-$(getconf _NPROCESSORS_ONLN)}."
  log "Load: $(awk '{print $1" "$2" "$3}' /proc/loadavg); memory available: $(awk '/MemAvailable:/ {printf "%.0f MiB", $2/1024}' /proc/meminfo); disk: $(df -hP / | awk 'NR==2 {print $4 " free of " $2}')"
  log "Docker: $(docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'); Compose: $(docker compose version --short); KVM: $(if command -v kvm-ok >/dev/null 2>&1; then kvm-ok 2>&1 | tail -1; else printf 'kvm-ok unavailable'; fi)"
  log "Old manager states: $(docker inspect --format '{{.Name}}={{.State.Status}}' "${ALL_OLD_MANAGERS[@]}" | tr '\n' ' '); Control: $(docker inspect --format '{{.State.Status}}' n150-control)."
  log "Docker socket: $(stat -Lc '%a %U:%G %n' /var/run/docker.sock); run-check port 17343 may be reused only after old managers stop."
  log "Images:"
  local image
  for image in "${BUILT_IMAGES[@]}"; do
    if docker image inspect "$image" >/dev/null 2>&1; then log "  present ${image}"; else log "  missing ${image}"; fi
  done
  log "Current runner containers:"
  docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E '(^|\t)(n150-|.*runner-manager|.*runner)' || true
  log "Measured KVM candidate admission with current host counters and only Control reserved: $(kvm_admission_snapshot)"
  log "Recent #795-related KVM events (if any):"
  for event in admission_denied backend_start_failed outcome_unknown; do
    docker logs --since 24h recursive-coder-kvm-runner-manager 2>&1 | grep -E "${event}" | tail -5 || true
  done
  log "Legacy generic KVM launch-failure warnings (unclassified):"
  docker logs --since 24h recursive-coder-kvm-runner-manager 2>&1 | grep -F 'warning: failed to start runner; stopping spawn batch' | tail -5 || true
  log "Preflight PASS: safety checks succeeded. This only establishes readiness to review; it does not apply the cutover."
}

start_logging() {
  mkdir -p "$BACKUP_ROOT"
  chmod 700 "$BACKUP_ROOT"
  BACKUP_DIR="${N150_BACKUP_DIR:-${BACKUP_ROOT}/$(date -u +%Y%m%dT%H%M%SZ)}"
  [[ ! -e "$BACKUP_DIR" ]] || die "backup directory already exists: ${BACKUP_DIR}; select a new N150_BACKUP_DIR rather than overwriting a previous snapshot."
  mkdir -m 700 -- "$BACKUP_DIR"
  LOG_FILE="${BACKUP_DIR}/$(basename -- "$MODE").log"
  exec > >(tee -a "$LOG_FILE") 2>&1
  log "Mode ${MODE}; repo ${REPO_ROOT}; log ${LOG_FILE}. Secret values are not logged."
}

save_manifest() {
  local manifest="$BACKUP_DIR/manifest.env"
  {
    printf 'REPO_ROOT=%q\n' "$REPO_ROOT"
    printf 'ENV_FILE=%q\n' "$ENV_FILE"
    printf 'OLD_DOCKER_FILE=%q\n' "$OLD_DOCKER_FILE"
    printf 'OLD_DOCKER_ENV=%q\n' "$OLD_DOCKER_ENV"
    printf 'OLD_KVM_FILE=%q\n' "$OLD_KVM_FILE"
    printf 'OLD_KVM_ENV=%q\n' "$OLD_KVM_ENV"
    docker ps --format '{{.Names}}' | grep -E '^n150-(pi-eph|gen-eph|rc-eph)-' | while IFS= read -r name; do printf 'PREVIOUS_WORKER=%q\n' "$name"; done
  } >"$manifest"
  chmod 600 "$manifest"
  for c in "${ALL_OLD_MANAGERS[@]}" n150-control; do
    docker inspect --format '{{.Name}} {{.Config.Image}} {{.Image}} {{.State.Status}} {{json .Mounts}} {{json .Config.Labels}}' "$c" >>"${BACKUP_DIR}/previous-containers.txt"
  done
  docker volume ls --format '{{.Name}}' >"${BACKUP_DIR}/volume-names.txt"
  docker image ls --digests --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Digest}}' >"${BACKUP_DIR}/image-ids.txt"
  install -m 600 "$OLD_DOCKER_FILE" "${BACKUP_DIR}/old-docker-compose.yaml"
  install -m 600 "$OLD_KVM_FILE" "${BACKUP_DIR}/old-kvm-compose.yaml"
  log "Previous Compose files, container image/mount metadata, image IDs, and volume names saved under ${BACKUP_DIR}; source env files remain untouched at their original protected paths."
}

backup_images() {
  local image backup_tag timestamp
  timestamp="$(basename -- "$BACKUP_DIR")"
  : >"${BACKUP_DIR}/previous-image-tags.tsv"
  while IFS= read -r image; do
    [[ -n "$image" ]] || continue
    if docker image inspect "$image" >/dev/null 2>&1; then
      backup_tag="${image%%:*}:acceptance-backup-${timestamp}"
      docker image tag "$image" "$backup_tag"
      printf '%s\t%s\n' "$image" "$backup_tag" >>"${BACKUP_DIR}/previous-image-tags.tsv"
      log "Preserved existing image tag ${image} as ${backup_tag}."
    fi
  done < <(printf '%s\n' "${BUILT_IMAGES[@]}")
  chmod 600 "${BACKUP_DIR}/previous-image-tags.tsv"
}

apply_cutover() {
  check_preconditions
  start_logging
  save_manifest
  backup_images
  log "Building worker images (no running services will be stopped during the build)."
  compose --profile images build worker-pi worker-general worker-control worker-sandbox
  compose build scheduler docker-executor kvm-executor run-check-executor
  compose config --quiet
  check_golden
  log "Final drain check before stopping old scheduling authorities."
  runner_api_checks
  check_no_worker_processes || die "a runner job or KVM VM became active during image build; old services remain running."

  log "Stopping old managers by exact container name; no broad Compose down/cleanup is used."
  docker stop "${ALL_OLD_MANAGERS[@]}"
  for c in "${ALL_OLD_MANAGERS[@]}"; do
    [[ "$(docker inspect -f '{{.State.Running}}' "$c")" == false ]] || die "old manager still running: ${c}; scheduler will not be enabled."
  done

  log "Stopping previously registered, idle ephemeral runner containers by exact pool prefixes."
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    docker stop --time 20 "$name"
  done < <(docker ps --format '{{.Names}}' | grep -E '^n150-(pi-eph|gen-eph|rc-eph)-' || true)

  log "Removing only the idle Control container to free its fixed name; its named state volume is preserved."
  docker stop --time 20 n150-control
  docker rm n150-control

  log "Starting isolated Docker, KVM, and run-check executors before the scheduler."
  compose up -d docker-executor kvm-executor run-check-executor
  for spec in 'docker-executor docker.sock' 'kvm-executor kvm.sock'; do
    read -r service socket_name <<<"$spec"
    local socket_path="/run/n150/${socket_name}"
    local socket_stat
    socket_stat="$(docker compose exec -T "$service" stat -Lc '%F %a %u:%g' "$socket_path")"
    log "Executor socket ${service}:${socket_name}: ${socket_stat}"
    [[ "$socket_stat" == "socket 660 0:0" ]] || die "executor socket ownership or permissions are not root:root 0660: ${service}:${socket_name}."
  done
  local mounted_docker_stat
  mounted_docker_stat="$(docker compose exec -T docker-executor stat -Lc '%F %a %u:%g' /var/run/docker.sock)"
  log "Docker socket as seen by Docker executor: ${mounted_docker_stat}"
  [[ "$mounted_docker_stat" == socket\ 660\ 0:* ]] || die "Docker socket permissions/ownership changed inside the executor."

  log "Running scheduler read-only verification before enabling the single scheduler."
  compose run --rm --no-deps scheduler python3 -m n150_runner verify
  log "Starting the unified scheduler; legacy managers are confirmed stopped."
  compose up -d scheduler
  compose exec -T scheduler python3 -m n150_runner status
  compose exec -T scheduler python3 -m n150_runner verify
  log "Apply PASS: executors and one unified scheduler are running. No acceptance jobs have been launched by this script."
  log "Next: trigger the reviewed acceptance workflows and collect evidence; use --rollback only after draining jobs and VMs."
}

find_latest_backup() {
  if [[ -n "${N150_BACKUP_DIR:-}" ]]; then BACKUP_DIR="$N150_BACKUP_DIR"; return; fi
  [[ -d "$BACKUP_ROOT" ]] || die "backup root is missing: ${BACKUP_ROOT}"
  BACKUP_DIR="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -print | sort | tail -1)"
  [[ -n "$BACKUP_DIR" ]] || die "no prior apply backup exists under ${BACKUP_ROOT}."
}

rollback() {
  require_root
  require_tools
  find_latest_backup
  [[ -f "${BACKUP_DIR}/manifest.env" ]] || die "manifest missing from ${BACKUP_DIR}."
  # This manifest is created by this script, mode 0600, and contains only paths.
  manifest_path() {
    python3 - "${BACKUP_DIR}/manifest.env" "$1" <<'PY'
import pathlib, shlex, sys
path, wanted = sys.argv[1:]
for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
    key, sep, value = line.partition("=")
    if sep and key == wanted:
        parts = shlex.split(value)
        if len(parts) == 1:
            print(parts[0])
        raise SystemExit(0)
raise SystemExit(1)
PY
  }
  OLD_DOCKER_FILE="$(manifest_path OLD_DOCKER_FILE)"
  OLD_DOCKER_ENV="$(manifest_path OLD_DOCKER_ENV)"
  OLD_KVM_FILE="$(manifest_path OLD_KVM_FILE)"
  OLD_KVM_ENV="$(manifest_path OLD_KVM_ENV)"
  LOG_FILE="${BACKUP_DIR}/rollback.log"
  exec > >(tee -a "$LOG_FILE") 2>&1
  log "Rollback using protected snapshot ${BACKUP_DIR}."
  [[ -f "$COMPOSE_FILE" && -f "$ENV_FILE" ]] || die "unified checkout or env file is missing."
  compose stop scheduler || true
  log "Scheduler admissions stopped. Confirming no jobs or VMs remain before touching workers."
  runner_api_checks
  check_no_worker_processes || die "a runner job or VM remains; leave executors running and retry rollback after it drains."
  compose stop docker-executor kvm-executor run-check-executor || true
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    docker stop --time 20 "$name" >/dev/null 2>&1 || true
  done < <(docker ps --format '{{.Names}}' | grep -E '^n150-(pi-eph|gen-eph|rc-eph)-' || true)
  # Remove only the new fixed-name Control container; the persistent volume is retained.
  if docker inspect n150-control >/dev/null 2>&1; then docker stop --time 20 n150-control; docker rm n150-control; fi
  [[ -r "$OLD_DOCKER_FILE" && -r "$OLD_DOCKER_ENV" ]] || die "previous Docker Compose or env file is missing; set OLD_DOCKER_COMPOSE_FILE / OLD_DOCKER_ENV_FILE and retry."
  [[ -r "$OLD_KVM_FILE" && -r "$OLD_KVM_ENV" ]] || die "previous KVM Compose or env file is missing; set OLD_KVM_COMPOSE_FILE / OLD_KVM_ENV_FILE and retry."
  log "Restoring old persistent workers and managers from their unchanged Compose files."
  if [[ -f "${BACKUP_DIR}/previous-image-tags.tsv" ]]; then
    while IFS=$'\t' read -r original backup_tag; do
      [[ -n "$original" && -n "$backup_tag" ]] || continue
      docker image tag "$backup_tag" "$original"
      log "Restored previous image tag ${original}."
    done <"${BACKUP_DIR}/previous-image-tags.tsv"
  fi
  old_docker_compose up -d
  old_kvm_compose up -d
  if [[ -f "${BACKUP_DIR}/manifest.env" ]]; then
    while IFS= read -r name; do
      [[ -n "$name" ]] || continue
      docker start "$name" >/dev/null 2>&1 || log "Previous idle worker ${name} was not restarted; the old manager will reconcile it."
    done < <(sed -n 's/^PREVIOUS_WORKER=//p' "${BACKUP_DIR}/manifest.env" | xargs -r -n1 printf '%s\n')
  fi
  log "Rollback PASS: legacy managers restored. All named volumes, state and Golden Images were retained."
  log "Verify old runner readiness and review ${LOG_FILE}."
}

case "$MODE" in
  --preflight) preflight ;;
  --apply) apply_cutover ;;
  --rollback) rollback ;;
  *) usage; exit 2 ;;
esac
