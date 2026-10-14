#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
script="$root/infra/github-runner-autoscaler/prepare-recursive-coder-kvm.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
bash -n "$script" || fail 'preparation script has a shell syntax error'
grep -Fq 'qemu-img check -- "$image"' "$script" || fail 'qcow2 consistency validation is missing'
grep -Fq 'mv -nT -- "$temporary" "$golden"' "$script" || fail 'golden image is not atomically installed without replacement'
grep -Fq 'refusing to replace it' "$script" || fail 'existing incorrect images are not protected'
grep -Fq 'KVM manager exists; roll back the deployment first' "$script" || fail 'rollback does not protect a deployed manager'
if grep -E 'apt(-get)? install|docker (restart|stop|rm|prune)|sysctl|apparmor|seccomp' "$script"; then
  fail 'preparation script contains an unrelated package, service, or host policy mutation'
fi

printf 'untrusted fixture image\n' >"$tmp/wrong.img"
preflight_root="$tmp/preflight-root"
preflight_script="$tmp/prepare-preflight.sh"
# Exercise the original production script with host-only checks removed
# solely in the TEST COPY; CI has no /dev/kvm or QEMU installation.
grep -Fxq 'check_host' "$script" || fail 'production host safety check missing'
grep -Fxq 'require_commands' "$script" || fail 'production tool safety check missing'
sed -e "s|^root=/var/lib/recursive-coder-kvm$|root=$preflight_root|" \
  -e '/^check_host$/d' -e '/^require_commands$/d' "$script" >"$preflight_script"
chmod 0755 "$preflight_script"
if KVM_STAGING_IMAGE="$tmp/wrong.img" bash "$preflight_script" --dry-run >"$tmp/output" 2>&1; then
  fail 'dry-run accepted an image with the wrong pinned checksum'
fi
grep -Fq 'Ubuntu image SHA-256 mismatch' "$tmp/output" || fail 'checksum failure was not clear'
[ ! -e "$preflight_root" ] || fail 'dry-run created KVM preparation directories'

if bash "$preflight_script" --verify >"$tmp/output" 2>&1; then
  fail 'verify unexpectedly passed while the prepared golden image is absent'
fi
grep -Fq 'Dedicated KVM directories are not prepared' "$tmp/output" || fail 'missing preparation state was not reported'

# Exercise successful install, repeat, verify and rollback against a private
# temporary root. The fake root identity and command wrappers never elevate or
# write outside that temporary directory.
test_root="$tmp/isolation"
mkdir -p "$test_root/bin"
fixture="$tmp/noble.img"
printf 'locally supplied test image\n' >"$fixture"
fixture_sha="$(sha256sum "$fixture" | awk '{print $1}')"
sed -e "s|^root=/var/lib/recursive-coder-kvm$|root=$test_root/host-root|" \
  -e "s|^expected_sha=.*$|expected_sha=$fixture_sha|" \
  -e '/^check_host$/d' -e '/^require_commands$/d' \
  "$script" >"$test_root/prepare.sh"
chmod 0755 "$test_root/prepare.sh"
cat >"$test_root/bin/id" <<'SH'
#!/bin/sh
echo 0
SH
cat >"$test_root/bin/docker" <<'SH'
#!/bin/sh
case "$1" in info) exit 0 ;; inspect) exit 1 ;; *) exit 90 ;; esac
SH
cat >"$test_root/bin/qemu-img" <<'SH'
#!/bin/sh
case "$1" in
  info) printf '{"format":"raw"}\n' ;;
  check) exit 0 ;;
  *) exit 90 ;;
esac
SH
cat >"$test_root/bin/install" <<'SH'
#!/bin/bash
if [[ "$1" = -d ]]; then
  mode=755
  while [[ $# -gt 0 ]]; do
    case "$1" in -m) mode="$2"; shift 2 ;; *) path="$1"; shift ;; esac
  done
  mkdir -p "$path"; chmod "$mode" "$path"
else
  mode=644; args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      -m) mode="$2"; shift 2 ;;
      --) shift ;;
      *) args+=("$1"); shift ;;
    esac
  done
  cp -- "${args[0]}" "${args[1]}"; chmod "$mode" "${args[1]}"
fi
SH
cat >"$test_root/bin/chown" <<'SH'
#!/bin/sh
exit 0
SH
cat >"$test_root/bin/stat" <<'SH'
#!/bin/bash
if [[ "$1" = -c && "$2" = %u:%g:%a ]]; then
  printf '0:0:%s\n' "$(/usr/bin/stat -c '%a' "$3")"
else
  exec /usr/bin/stat "$@"
fi
SH
cat >"$test_root/bin/df" <<'SH'
#!/bin/sh
printf 'Filesystem 1M-blocks Used Available Use%% Mounted\nfixture 60000 1000 59000 2%% /tmp\n'
SH
chmod 0755 "$test_root/bin/"*
export KVM_STAGING_IMAGE="$fixture"
export PATH="$test_root/bin:$PATH"

bash "$test_root/prepare.sh" --dry-run >"$test_root/output"
grep -Fq 'Read-only preparation preflight passed' "$test_root/output"
[ ! -e "$test_root/host-root" ] || fail 'successful dry-run created preparation state'
bash "$test_root/prepare.sh" --prepare >"$test_root/output"
golden="$test_root/host-root/golden/noble-server-cloudimg-amd64.img"
manifest="$test_root/host-root/state/preparation.json"
[ "$(sha256sum "$golden" | awk '{print $1}')" = "$fixture_sha" ] || fail 'prepared image differs from verified staging source'
[ "$(stat -c '%a' "$golden")" = 444 ] || fail 'prepared golden image is writable'
grep -Fq '"golden_created_by_prepare":true' "$manifest" || fail 'rollback ownership was not recorded'
bash "$test_root/prepare.sh" --prepare >"$test_root/output"
grep -Fq 'already installed; no replacement' "$test_root/output" || fail 'prepare is not idempotent'
bash "$test_root/prepare.sh" --verify >"$test_root/output"
grep -Fq 'Host KVM access, image integrity, permissions and bounded disk headroom verified' "$test_root/output"
bash "$test_root/prepare.sh" --rollback >"$test_root/output"
[ ! -e "$golden" ] || fail 'rollback did not remove the image created by preparation'
[ -f "$fixture" ] || fail 'rollback removed the original staged source'
bash "$test_root/prepare.sh" --rollback >"$test_root/output"
grep -Fq 'No preparation manifest; no assets are owned' "$test_root/output" || fail 'rollback is not idempotent'

printf 'incorrect existing image\n' >"$golden"
if bash "$test_root/prepare.sh" --prepare >"$test_root/output" 2>&1; then
  fail 'prepare replaced an existing image with a different checksum'
fi
grep -Fq 'Existing golden image has an unexpected checksum; refusing to replace it' "$test_root/output" \
  || fail 'existing golden image rejection was not reported'
[ "$(cat "$golden")" = 'incorrect existing image' ] || fail 'incorrect golden image was modified'

printf 'recursive-coder KVM preparation checks passed\n'
