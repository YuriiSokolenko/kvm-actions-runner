#!/usr/bin/env bash
set -euo pipefail

: "${GH_ADMIN_TOKEN:?GH_ADMIN_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${RUNNER_NAME:=n150-control}"
: "${RUNNER_LABELS:=n150,control}"
: "${ACTIONS_RUNNER_BASELINE_VERSION:?ACTIONS_RUNNER_BASELINE_VERSION is required}"
: "${CONTROL_REPAIR_COOLDOWN_SECONDS:=300}"
: "${CONTROL_RETRY_SECONDS:=30}"
: "${CONTROL_SHORT_RETRY_SECONDS:=5}"
: "${CONTROL_UPDATE_WAIT_SECONDS:=30}"
: "${CONTROL_UPDATE_SHUTDOWN_WAIT_SECONDS:=90}"
: "${CONTROL_CHILD_STOP_WAIT_SECONDS:=30}"

for name in CONTROL_REPAIR_COOLDOWN_SECONDS CONTROL_RETRY_SECONDS CONTROL_SHORT_RETRY_SECONDS CONTROL_UPDATE_WAIT_SECONDS CONTROL_UPDATE_SHUTDOWN_WAIT_SECONDS CONTROL_CHILD_STOP_WAIT_SECONDS; do
  value="${!name}"
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${name} must be a non-negative integer" >&2
    exit 2
  }
done

ADMIN_TOKEN="${GH_ADMIN_TOKEN}"
unset GH_ADMIN_TOKEN

RUNNER_HOME="${RUNNER_HOME:-/home/runner/actions-runner}"
RUNNER_BASELINE_HOME="${RUNNER_BASELINE_HOME:-/opt/actions-runner-baseline}"
API="https://api.github.com/repos/${GITHUB_REPOSITORY}"
REPAIR_MARKER=".control-last-repair"
AUTH=(
  -H "Accept: application/vnd.github+json"
  -H "X-GitHub-Api-Version: 2026-03-10"
)

auth_header() {
  printf 'header = "Authorization: Bearer %s"\n' "${ADMIN_TOKEN}"
}

registration_token() {
  curl -fsS --connect-timeout 5 --max-time 15 -K <(auth_header) -X POST "${AUTH[@]}" \
    "${API}/actions/runners/registration-token" | jq -er '.token'
}

runner_api_healthy() {
  local response
  response="$(curl -fsS --connect-timeout 5 --max-time 15 -K <(auth_header) "${AUTH[@]}" \
    "${API}/actions/runners?per_page=1")" || return 1
  jq -e '(.runners | type) == "array"' <<<"${response}" >/dev/null
}

clear_local_registration() {
  gosu runner rm -f .runner .credentials .credentials_rsaparams
}

registration_complete() {
  [ -s .runner ] && [ -s .credentials ]
}

registration_files_present() {
  [ -e .runner ] || [ -e .credentials ] || [ -e .credentials_rsaparams ]
}

configure_runner() {
  local token
  token="$(registration_token)"
  gosu runner ./config.sh \
    --url "https://github.com/${GITHUB_REPOSITORY}" \
    --token "${token}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}" \
    --work "_work" \
    --unattended \
    --replace
}

runtime_version() {
  gosu runner ./bin/Runner.Listener --version 2>/dev/null | tail -n 1 | tr -d '\r'
}

version_is_older() {
  local current="$1" baseline="$2" first
  [ "${current}" != "${baseline}" ] || return 1
  first="$(printf '%s\n%s\n' "${current}" "${baseline}" | sort -V | head -n 1)"
  [ "${first}" = "${current}" ]
}

restore_runtime_baseline() {
  echo "restoring control runner runtime baseline ${ACTIONS_RUNNER_BASELINE_VERSION}" >&2
  gosu runner rm -rf bin externals
  gosu runner cp -a "${RUNNER_BASELINE_HOME}/." "${RUNNER_HOME}/"
}

ensure_runtime_baseline() {
  local current=""
  if [ -x ./bin/Runner.Listener ]; then
    current="$(runtime_version || true)"
  fi

  if [ -z "${current}" ]; then
    restore_runtime_baseline
    return
  fi

  if version_is_older "${current}" "${ACTIONS_RUNNER_BASELINE_VERSION}"; then
    echo "upgrading persisted runner runtime ${current} -> baseline ${ACTIONS_RUNNER_BASELINE_VERSION}" >&2
    restore_runtime_baseline
  fi
}

repair_allowed() {
  local now last
  [ ! -f "${REPAIR_MARKER}" ] && return 0
  now="$(date +%s)"
  last="$(cat "${REPAIR_MARKER}" 2>/dev/null || true)"
  [[ "${last}" =~ ^[0-9]+$ ]] || return 0
  (( now - last >= CONTROL_REPAIR_COOLDOWN_SECONDS ))
}

mark_repair() {
  local now
  now="$(date +%s)"
  gosu runner sh -c 'printf "%s\n" "$1" > "$2"' _ "${now}" "${REPAIR_MARKER}"
}

sleep_pid=""
listener_pid=""
registration_pid=""
update_waiting=0
launch_in_progress=0
shutdown_requested=0

complete_launch() {
  launch_in_progress=0
  if [ "${shutdown_requested}" -eq 1 ]; then
    shutdown
  fi
}

interruptible_sleep() {
  launch_in_progress=1
  sleep "$1" &
  sleep_pid=$!
  complete_launch
  wait "${sleep_pid}" 2>/dev/null || true
  sleep_pid=""
}

wait_for_update() {
  local i
  update_waiting=1
  for ((i = 0; i <= CONTROL_UPDATE_WAIT_SECONDS; i += 1)); do
    if [ -f update.finished ]; then
      gosu runner rm -f update.finished || true
      update_waiting=0
      return 0
    fi
    interruptible_sleep 1
  done
  update_waiting=0
  return 1
}

wait_for_update_during_shutdown() {
  local i
  for ((i = 0; i < CONTROL_UPDATE_SHUTDOWN_WAIT_SECONDS; i += 1)); do
    if [ -f update.finished ]; then
      gosu runner rm -f update.finished || true
      return 0
    fi
    sleep 1
  done
  return 1
}

stop_process_group() {
  local pid="$1" signal="$2" i

  kill "-${signal}" -- "-${pid}" 2>/dev/null || true
  for ((i = 0; i < CONTROL_CHILD_STOP_WAIT_SECONDS; i += 1)); do
    kill -0 -- "-${pid}" 2>/dev/null || break
    sleep 1
  done

  if kill -0 -- "-${pid}" 2>/dev/null; then
    echo "warning: process group ${pid} did not stop after ${CONTROL_CHILD_STOP_WAIT_SECONDS}s; killing it" >&2
    kill -KILL -- "-${pid}" 2>/dev/null || true
  fi
  wait "${pid}" 2>/dev/null || true
}

shutdown() {
  if [ "${launch_in_progress}" -eq 1 ]; then
    shutdown_requested=1
    return
  fi

  trap - TERM INT

  if [ -n "${sleep_pid}" ]; then
    kill -TERM -- "-${sleep_pid}" 2>/dev/null || true
    wait "${sleep_pid}" 2>/dev/null || true
    sleep_pid=""
  fi

  if [ "${update_waiting}" -eq 1 ]; then
    echo "control runner update is in progress; waiting for update script before shutdown" >&2
    wait_for_update_during_shutdown || echo "warning: update did not finish before shutdown wait expired" >&2
  fi

  if [ -n "${registration_pid}" ]; then
    stop_process_group "${registration_pid}" INT
    registration_pid=""
    clear_local_registration || echo "warning: failed to clear interrupted registration state" >&2
  fi

  if [ -n "${listener_pid}" ]; then
    stop_process_group "${listener_pid}" INT
    listener_pid=""
  fi

  exit 0
}
trap shutdown TERM INT

cd "${RUNNER_HOME}"
ensure_runtime_baseline

# Each background registration/listener/sleep receives its own process group.
# PID 1 can therefore stop the whole active tree rather than orphaning children.
set -m

credential_failures=0

while true; do
  if ! registration_complete; then
    if registration_files_present; then
      echo "warning: incomplete control runner registration state; clearing it before registration" >&2
      if ! clear_local_registration; then
        echo "warning: failed to clear incomplete registration state; retrying later" >&2
        interruptible_sleep "${CONTROL_RETRY_SECONDS}"
        continue
      fi
    fi

    launch_in_progress=1
    configure_runner &
    registration_pid=$!
    complete_launch
    registration_status=0
    wait "${registration_pid}" || registration_status=$?
    registration_pid=""

    if [ "${registration_status}" -ne 0 ]; then
      echo "warning: control runner registration failed status=${registration_status}; clearing partial local state" >&2
      clear_local_registration || echo "warning: failed to clear partial registration state" >&2
      interruptible_sleep "${CONTROL_RETRY_SECONDS}"
      continue
    fi

    if ! registration_complete; then
      echo "warning: config.sh exited successfully without complete registration files" >&2
      clear_local_registration || echo "warning: failed to clear incomplete registration state" >&2
      interruptible_sleep "${CONTROL_RETRY_SECONDS}"
      continue
    fi

    credential_failures=0
  fi

  launch_in_progress=1
  gosu runner ./bin/Runner.Listener run &
  listener_pid=$!
  complete_launch
  listener_status=0
  wait "${listener_pid}" || listener_status=$?
  listener_pid=""

  case "${listener_status}" in
    0)
      echo "control runner listener exited cleanly; stopping container" >&2
      exit 0
      ;;
    2)
      credential_failures=0
      echo "control runner listener requested retry" >&2
      interruptible_sleep "${CONTROL_SHORT_RETRY_SECONDS}"
      ;;
    3|4)
      credential_failures=0
      echo "control runner listener is updating; waiting for update completion" >&2
      if wait_for_update; then
        if ! runtime_version >/dev/null 2>&1; then
          echo "warning: updated runner runtime failed version probe; restoring image baseline" >&2
          restore_runtime_baseline || echo "warning: failed to restore runner baseline after update" >&2
        fi
      else
        echo "warning: runner update did not signal completion within upstream 31s window" >&2
      fi
      ;;
    6)
      credential_failures=0
      echo "control runner configuration refreshed; retrying listener after short delay" >&2
      interruptible_sleep "${CONTROL_SHORT_RETRY_SECONDS}"
      ;;
    1|5)
      credential_failures=$((credential_failures + 1))

      if [ "${credential_failures}" -lt 2 ]; then
        echo "warning: listener failed status=${listener_status}; retrying once with existing credentials before repair" >&2
        interruptible_sleep "${CONTROL_RETRY_SECONDS}"
        continue
      fi

      if ! runner_api_healthy; then
        echo "warning: listener failed status=${listener_status} but GitHub runners API is unavailable; preserving credentials" >&2
        interruptible_sleep "${CONTROL_RETRY_SECONDS}"
        continue
      fi

      if ! repair_allowed; then
        echo "warning: listener failed status=${listener_status}; automatic re-registration is inside cooldown, preserving credentials" >&2
        interruptible_sleep "${CONTROL_RETRY_SECONDS}"
        continue
      fi

      if ! mark_repair; then
        echo "warning: failed to persist repair cooldown; preserving credentials" >&2
        interruptible_sleep "${CONTROL_RETRY_SECONDS}"
        continue
      fi

      if ! clear_local_registration; then
        echo "warning: failed to clear registration for repair; preserving current files" >&2
        interruptible_sleep "${CONTROL_RETRY_SECONDS}"
        continue
      fi

      echo "warning: repeated listener failure status=${listener_status}; scheduling bounded clean re-registration" >&2
      credential_failures=0
      ;;
    7)
      credential_failures=0
      echo "error: GitHub runner version is deprecated; rebuild/deploy the control image with a supported runner version" >&2
      interruptible_sleep "${CONTROL_REPAIR_COOLDOWN_SECONDS}"
      ;;
    *)
      credential_failures=0
      echo "warning: control runner listener exited unexpected status=${listener_status}; preserving registration" >&2
      interruptible_sleep "${CONTROL_RETRY_SECONDS}"
      ;;
  esac
done
