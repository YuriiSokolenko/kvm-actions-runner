#!/usr/bin/env bash
# Owner-operated preparation of the dedicated recursive-coder KVM assets.
# This script never installs packages or starts/stops/restarts services.
set -euo pipefail
umask 077

root=/var/lib/recursive-coder-kvm
golden_dir="$root/golden"
state_dir="$root/state"
golden="$golden_dir/noble-server-cloudimg-amd64.img"
manifest="$state_dir/preparation.json"
expected_sha=6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2
staging_image="${KVM_STAGING_IMAGE:-/tmp/noble-server-cloudimg-amd64.img}"
mode="${1:---dry-run}"
temporary=""

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[recursive-coder-kvm-prepare] %s\n' "$*"; }
usage() {
  cat <<'USAGE'
Usage: prepare-recursive-coder-kvm.sh [--dry-run|--prepare|--verify|--rollback]

Set KVM_STAGING_IMAGE to the locally staged, SHA-256-pinned Ubuntu 24.04 amd64
cloud image. The default is /tmp/noble-server-cloudimg-amd64.img.
USAGE
}
case "$mode" in --dry-run|--prepare|--verify|--rollback) ;; *) usage >&2; exit 2 ;; esac
[[ $# -le 1 ]] || { usage >&2; exit 2; }

cleanup() { [[ -z "$temporary" || ! -e "$temporary" ]] || rm -f -- "$temporary"; }
trap cleanup EXIT

require_root() { [[ "$(id -u)" = 0 ]] || fail "Run --prepare/--rollback with sudo; this script does not invoke sudo"; }
require_commands() {
  local command
  for command in qemu-img qemu-system-x86_64 sha256sum python3 flock install stat df awk find grep mv chown chmod rm dirname; do
    command -v "$command" >/dev/null 2>&1 || fail "Required command is missing: $command (install prerequisites separately; this script does not install packages)"
  done
}
check_host() {
  [[ "$(uname -m)" = x86_64 ]] || fail 'Ubuntu amd64 KVM guest requires an x86_64 host'
  [[ -r /dev/kvm && -w /dev/kvm ]] || fail '/dev/kvm must be readable and writable by the owner running this command'
  local memory_kib
  memory_kib="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
  [[ "$memory_kib" =~ ^[0-9]+$ ]] && (( memory_kib >= 8 * 1024 * 1024 )) \
    || fail 'Need at least 8 GiB MemAvailable for a bounded validation guest and existing services'
}
check_paths() {
  local path
  for path in "$root" "$golden_dir" "$state_dir"; do
    [[ ! -L "$path" ]] || fail "Dedicated KVM path must not be a symlink: $path"
  done
}
image_path() {
  if [[ -f "$golden" ]]; then printf '%s\n' "$golden"; else printf '%s\n' "$staging_image"; fi
}
validate_image() {
  local image="$1" actual_sha info format
  [[ -f "$image" && ! -L "$image" ]] || fail "Ubuntu image must be a regular file: $image"
  actual_sha="$(sha256sum -- "$image" | awk '{print $1}')"
  [[ "$actual_sha" = "$expected_sha" ]] || fail "Ubuntu image SHA-256 mismatch; expected pinned digest $expected_sha"
  info="$(qemu-img info --output=json -- "$image")" || fail 'qemu-img could not inspect the staged image'
  format="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("format", ""))' <<<"$info")"
  [[ "$format" = raw || "$format" = qcow2 ]] || fail "Unsupported Ubuntu image format: ${format:-unknown}"
  if [[ "$format" = qcow2 ]]; then qemu-img check -- "$image" >/dev/null || fail 'qcow2 consistency check failed'; fi
  # The pinned digest is the official Ubuntu noble-server-cloudimg-amd64
  # artifact; matching it cryptographically establishes the amd64 build.
  log "Pinned Ubuntu 24.04 amd64 image verified (format=$format, sha256=$actual_sha)"
}
check_existing_golden() {
  if [[ -e "$golden" || -L "$golden" ]]; then
    [[ -f "$golden" && ! -L "$golden" ]] || fail 'Existing golden path is not a regular file; refusing replacement'
    local actual_sha
    actual_sha="$(sha256sum -- "$golden" | awk '{print $1}')"
    [[ "$actual_sha" = "$expected_sha" ]] || fail 'Existing golden image has an unexpected checksum; refusing to replace it'
    [[ "$(stat -c '%u:%g:%a' "$golden")" = '0:0:444' ]] \
      || fail 'Existing pinned image must already be root:root mode 0444; refusing to alter a working image'
    log 'Existing pinned golden image is intact; it will not be replaced'
  fi
}
check_disk() {
  local available probe="$root"
  while [[ ! -d "$probe" ]]; do probe="$(dirname "$probe")"; done
  available="$(df -Pm "$probe" 2>/dev/null | awk 'NR==2 {print $4}')"
  [[ "$available" =~ ^[0-9]+$ ]] && (( available >= 25600 )) \
    || fail 'Need at least 25 GiB free on the filesystem holding the 24 GiB disposable overlay'
}
check_no_deployment() {
  if command -v docker >/dev/null 2>&1; then
    docker info >/dev/null 2>&1 || fail 'Cannot verify Docker state; refusing host preparation'
    if docker inspect recursive-coder-kvm-runner-manager >/dev/null 2>&1; then
      fail 'KVM manager exists; keep preparation separate and remove it through its deployment rollback first'
    fi
  fi
  if [[ -d "$state_dir" ]] && find "$state_dir" -mindepth 1 -maxdepth 1 \
    ! -name preparation.json ! -name prepare.lock -print -quit | grep -q .; then
    fail 'KVM state contains VM data; preparation requires an idle, undeployed pool'
  fi
}
write_manifest() {
  local created="$1" temp_manifest="$manifest.tmp.$$"
  printf '{"schema":1,"golden_created_by_prepare":%s,"sha256":"%s"}\n' \
    "$created" "$expected_sha" >"$temp_manifest"
  chown root:root "$temp_manifest"
  chmod 0600 "$temp_manifest"
  mv -f -- "$temp_manifest" "$manifest"
}
verify_prepared() {
  [[ -d "$golden_dir" && -d "$state_dir" ]] || fail 'Dedicated KVM directories are not prepared'
  [[ "$(stat -c '%u:%g:%a' "$golden_dir")" = '0:0:755' ]] || fail 'Golden directory must be root:root mode 0755'
  [[ "$(stat -c '%u:%g:%a' "$state_dir")" = '0:0:700' ]] || fail 'KVM state directory must be root:root mode 0700'
  validate_image "$golden"
  [[ "$(stat -c '%u:%g:%a' "$golden")" = '0:0:444' ]] || fail 'Golden image must be root:root mode 0444'
  check_disk
  log 'Host KVM access, image integrity, permissions and bounded disk headroom verified'
}

require_commands
check_host
check_paths

case "$mode" in
  --dry-run)
    image="$(image_path)"
    validate_image "$image"
    check_existing_golden
    check_disk
    check_no_deployment
    log 'Read-only preparation preflight passed; no files or services changed'
    ;;
  --verify)
    verify_prepared
    ;;
  --prepare)
    require_root
    check_existing_golden
    check_no_deployment
    if [[ ! -e "$golden" ]]; then validate_image "$staging_image"; fi
    install -d -o root -g root -m 0755 "$golden_dir"
    install -d -o root -g root -m 0700 "$state_dir"
    exec 9>"$state_dir/prepare.lock"
    flock -n 9 || fail 'Another KVM preparation or rollback is in progress'
    check_existing_golden
    check_no_deployment
    if [[ ! -e "$golden" ]]; then
      validate_image "$staging_image"
      write_manifest true
      temporary="$golden_dir/.noble-server-cloudimg-amd64.img.part.$$"
      install -o root -g root -m 0600 -- "$staging_image" "$temporary"
      validate_image "$temporary"
      chmod 0444 "$temporary"
      chown root:root "$temporary"
      mv -nT -- "$temporary" "$golden"
      [[ ! -e "$temporary" ]] || fail 'Golden image path appeared during installation; refusing to overwrite it'
      temporary=""
      log 'Pinned golden image installed atomically; staged source was left untouched'
    else
      [[ -f "$manifest" ]] || write_manifest false
      log 'Pinned golden image already installed; no replacement performed'
    fi
    chmod 0755 "$golden_dir"
    chown root:root "$golden_dir"
    chmod 0700 "$state_dir"
    chown root:root "$state_dir"
    verify_prepared
    log 'Preparation complete. No packages, host security policy, Docker, or runner services were changed.'
    ;;
  --rollback)
    require_root
    [[ -d "$state_dir" ]] || { log 'Preparation state is absent; nothing to roll back'; exit 0; }
    exec 9>"$state_dir/prepare.lock"
    flock -n 9 || fail 'Another KVM preparation or rollback is in progress'
    [[ -f "$manifest" ]] || { log 'No preparation manifest; no assets are owned by this transaction'; exit 0; }
    docker_path="$(command -v docker || true)"
    if [[ -n "$docker_path" ]]; then
      "$docker_path" info >/dev/null 2>&1 || fail 'Cannot verify Docker state; refusing preparation rollback'
      if "$docker_path" inspect recursive-coder-kvm-runner-manager >/dev/null 2>&1; then
        fail 'KVM manager exists; roll back the deployment first and retry preparation rollback'
      fi
    fi
    if find "$state_dir" -mindepth 1 -maxdepth 1 ! -name preparation.json ! -name prepare.lock -print -quit | grep -q .; then
      fail 'KVM state contains VM data; stop and clean the KVM manager before rollback'
    fi
    created="$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1])).get("golden_created_by_prepare", False)).lower())' "$manifest")"
    if [[ "$created" = true && -e "$golden" ]]; then
      [[ "$(sha256sum -- "$golden" | awk '{print $1}')" = "$expected_sha" ]] \
        || fail 'Installed image changed since preparation; preserving it for manual review'
      rm -f -- "$golden"
      log 'Removed only the pinned golden image created by this preparation transaction'
    fi
    rm -f -- "$manifest"
    log 'Preparation rollback complete; directories and unrelated contents were preserved'
    ;;
esac
