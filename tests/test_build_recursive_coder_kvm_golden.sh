#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
builder="$root/scripts/build-recursive-coder-kvm-golden.sh"
fail(){ echo "FAIL: $*" >&2; exit 1; }
bash -n "$builder" || fail 'builder shell syntax is invalid'
# SIGINT and SIGTERM must exit the script so EXIT cleanup runs once.
grep -Fq "trap 'exit 130' INT" "$builder" || fail 'Ctrl-C does not exit builder'
grep -Fq "trap 'exit 143' TERM" "$builder" || fail 'SIGTERM does not exit builder'
grep -Fq 'trap cleanup EXIT' "$builder" || fail 'builder EXIT cleanup is missing'
grep -Fq 'terminate_child "$qemu_pid"' "$builder" || fail 'QEMU cleanup is not bounded'
grep -Fq 'kill -KILL "$pid"' "$builder" || fail 'QEMU cleanup has no force-stop fallback'
grep -Fq 'restrict=on,ipv6=off' "$builder" || fail 'QEMU guest networking is not restricted'
grep -Fq 'kvm-egress-proxy.py' "$builder" || fail 'builder omits repository allowlist proxy'
grep -Fq 'actions-runner-linux-x64-2.338.0.tar.gz' "$builder" || fail 'pinned Actions runner archive is missing'
grep -Fq 'sha256sum -c -' "$builder" || fail 'runner archive checksum is not verified'
grep -Fq 'prepared.manifest' "$builder" || fail 'prepared guest manifest is missing'
grep -Fq 'BUILDER_BWRAP_PROBE=PASS' "$builder" || fail 'unprivileged Bubblewrap probe is missing'
grep -Fq 'emit_keys_to_console' "$builder" || fail 'cloud-init must not print ephemeral host keys to the serial log'
grep -Fq 'REDACTED PRIVATE KEY BLOCK' "$builder" || fail 'failure diagnostics must redact private key blocks'
grep -Fq 'cloud-init clean --logs --machine-id --seed' "$builder" || fail 'cloud-init identity is not cleaned before image capture'
grep -Fq 'PROMOTED_FILENAME=' "$builder" || fail 'digest-named promotion is missing'
grep -Fq 'chmod 0444' "$builder" || fail 'candidate is not made read-only'
grep -Fq 'mv -nT -- "$promote_image_tmp" "$target"' "$builder" || fail 'promotion is not atomic and no-replace'
if grep -E 'hostfwd|virtiofs|9p|docker.sock|ACTIONS_RUNNER_INPUT_TOKEN|RECURSIVE_CODER_GH_ADMIN_TOKEN' "$builder"; then
  fail 'builder exposes host forwarding, shared filesystems or runner credentials'
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/out"
printf 'wrong pinned source\n' >"$tmp/wrong.img"
chmod 0444 "$tmp/wrong.img"
if KVM_BUILDER_BASE_IMAGE="$tmp/wrong.img" KVM_BUILDER_OUTPUT_DIR="$tmp/out" bash "$builder" --dry-run >"$tmp/output" 2>&1; then
  fail 'builder preflight accepted an unpinned base image'
fi
grep -Fq 'stock image digest mismatch' "$tmp/output" || fail 'read-only source was rejected before checksum verification'
[ -z "$(find "$tmp/out" -mindepth 1 -print -quit)" ] || fail 'failed dry-run modified output directory'
printf 'writable source\n' >"$tmp/writable.img"
chmod 0644 "$tmp/writable.img"
if KVM_BUILDER_BASE_IMAGE="$tmp/writable.img" KVM_BUILDER_OUTPUT_DIR="$tmp/out" bash "$builder" --dry-run >"$tmp/output" 2>&1; then
  fail 'builder accepted a source image with write permission bits'
fi
grep -Fq 'stock image has write permission bits' "$tmp/output" || fail 'writable source was not rejected by mode-bit validation'
if KVM_BUILDER_BASE_IMAGE="$tmp/missing.img" KVM_BUILDER_OUTPUT_DIR="$tmp/out" bash "$builder" --dry-run >"$tmp/output" 2>&1; then
  fail 'builder accepted a missing source image'
fi
grep -Fq 'readable regular non-symlink' "$tmp/output" || fail 'missing source was not rejected clearly'
ln -s "$tmp/writable.img" "$tmp/symlink.img"
if KVM_BUILDER_BASE_IMAGE="$tmp/symlink.img" KVM_BUILDER_OUTPUT_DIR="$tmp/out" bash "$builder" --dry-run >"$tmp/output" 2>&1; then
  fail 'builder accepted a symlink source image'
fi
grep -Fq 'readable regular non-symlink' "$tmp/output" || fail 'symlink source was not rejected clearly'
[ -z "$(find "$tmp/out" -mindepth 1 -print -quit)" ] || fail 'failed preflight modified output directory'
# Optional pinned image exercises the root/0444 failure mode without embedding it in fixtures.
if [[ -n "${KVM_BUILDER_TEST_PINNED_IMAGE:-}" ]]; then
  pinned_output="${KVM_BUILDER_TEST_OUTPUT_DIR:-$tmp/out}"
  mkdir -p "$pinned_output"
  KVM_BUILDER_BASE_IMAGE="$KVM_BUILDER_TEST_PINNED_IMAGE" KVM_BUILDER_OUTPUT_DIR="$pinned_output" bash "$builder" --dry-run >"$tmp/output" 2>&1 || { cat "$tmp/output" >&2; fail 'builder rejected the staged pinned read-only image'; }
  grep -Fq 'Read-only preflight passed' "$tmp/output" || fail 'pinned dry-run did not complete'
fi
# v2 must build from the v1 image, pin all downloaded toolchains,
# warm a trusted public revision and scrub guest identities before capture.
v2="$root/scripts/recursive-coder-golden-v2-guest.sh"
bash -n "$v2" || fail 'v2 guest bootstrap shell syntax is invalid'
grep -Fq -- '--build-v2' "$builder" || fail 'v2 build mode missing'
grep -Fq -- '--dry-run-v2' "$builder" || fail 'v2 preflight mode missing'
grep -Fq 'toolchain_manifest_sha256' "$builder" || fail 'v2 provenance is not verified'
grep -Fq 'recursive-coder-golden-v2-guest.sh' "$builder" || fail 'v2 guest script is not embedded in cloud-init'
grep -Fq 'dce76f55f8e251a3a1f130eb120f30b3d271de2b76c9b0729d316b5a1b6dc01f' "$v2" || fail 'Gradle 9.8.1 checksum is missing'
grep -Fq '680d3f30b24a7ff24b98db5e96f294c0070f8f9078df658da1bce1b9c9873c88' "$v2" || fail 'Node 22 checksum is missing'
grep -Fq 'sha256sum -c -' "$v2" || fail 'v2 downloads are not checksum verified'
grep -Fq '3028a05e602a9572ba41bc1f3daa28d0fdb762c0' "$v2" || fail 'prewarm source revision is not pinned'
grep -Fq 'GRADLE_USER_HOME="$base/gradle-home"' "$v2" || fail 'v2 dependency cache is not captured'
grep -Fq 'testClasses :app:cli:installDist' "$v2" || fail 'v2 must prewarm actual JVM test classes and the CLI distribution'
if grep -Fq 'testClasses integrationTestClasses e2eTestClasses' "$v2"; then
  fail 'v2 builder invokes nonexistent integrationTestClasses/e2eTestClasses tasks'
fi
grep -Fq 'runuser -u runner -- /usr/bin/bwrap' "$v2" || fail 'v2 Bubblewrap probe is missing'
grep -Fq 'cloud-init clean --logs --machine-id --seed' "$v2" || fail 'v2 does not scrub cloud-init identity'
grep -Fq 'rm -rf /tmp/recursive-coder-warm /home/runner' "$v2" || fail 'builder is retaining source and runner home'
grep -Fq 'pkill -TERM -u runner' "$v2" || fail 'v2 does not stop lingering compiler processes'
grep -Fq 'pkill -KILL -u runner' "$v2" || fail 'v2 cleanup lacks a bounded force-stop fallback'
grep -Fq 'chmod -R a+rX "$base/python" "$base/gradle-9.8.1" "$base/node-v22.21.1-linux-x64"' "$v2" || fail 'v2 toolchains are not accessible to unprivileged runner'
python3 - "$v2" <<'PY' || fail 'v2 must stop runner processes before deleting its home or user'
from pathlib import Path
import sys
s=Path(sys.argv[1]).read_text()
assert s.index('pkill -TERM -u runner') < s.index('rm -rf /tmp/recursive-coder-warm /home/runner') < s.index('userdel runner')
PY
if grep -E 'ACTIONS_RUNNER_INPUT_TOKEN|RECURSIVE_CODER_GH_ADMIN_TOKEN|hostfwd|docker.sock' "$v2"; then
  fail 'v2 build embeds credentials or a host-escape interface'
fi
if KVM_BUILDER_V1_IMAGE="$tmp/wrong.img" KVM_BUILDER_OUTPUT_DIR="$tmp/out" bash "$builder" --dry-run-v2 >"$tmp/output" 2>&1; then
  fail 'v2 accepted an unpinned parent image'
fi
grep -Fq 'stock image digest mismatch' "$tmp/output" || fail 'v2 pinned parent was not validated'
echo 'recursive-coder KVM golden builder checks passed'
