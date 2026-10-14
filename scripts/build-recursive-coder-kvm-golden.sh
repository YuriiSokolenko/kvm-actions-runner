#!/usr/bin/env bash
set -euo pipefail
umask 077
dir="$(cd "$(dirname "$0")" && pwd)"
base="${KVM_BUILDER_BASE_IMAGE:-/var/lib/recursive-coder-kvm/golden/noble-server-cloudimg-amd64.img}"
base_sha=6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2
out="${KVM_BUILDER_OUTPUT_DIR:-/var/tmp/recursive-coder-kvm-prepared}"
golden=/var/lib/recursive-coder-kvm/golden
mode="${1:---dry-run}"; candidate="${2:-}"
if [[ "$mode" = --build-v2 || "$mode" = --dry-run-v2 ]]; then
  base="${KVM_BUILDER_V1_IMAGE:-/var/lib/recursive-coder-kvm/golden/noble-server-cloudimg-amd64-prepared-18db16acc608.img}"
  base_sha=18db16acc6085326c9d4a5dc10dbefbe4bf4d37b01031da588a9ecf5c27e10a5
fi
tmp= proxy_pid= qemu_pid= promote_image_tmp= promote_prov_tmp=
fail(){
  echo "ERROR: $*" >&2
  if [[ -n "${serial:-}" && -f "${serial:-}" && -d "$out" ]]; then
    diagnostic="$out/failed-build-$(date -u +%Y%m%dT%H%M%SZ).serial.log"
    python3 - "$serial" "$diagnostic" <<'PYDIAG' || true
import pathlib,re,sys
text=pathlib.Path(sys.argv[1]).read_text(errors="replace")
text=re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "[REDACTED PRIVATE KEY BLOCK]", text, flags=re.S)
text=re.sub(r"-----BEGIN SSH HOST KEY KEYS-----.*?-----END SSH HOST KEY KEYS-----",
            "[REDACTED SSH HOST KEYS]", text, flags=re.S)
pathlib.Path(sys.argv[2]).write_text(text)
PYDIAG
    chmod 0444 "$diagnostic" 2>/dev/null || true
    echo "BUILD_DIAGNOSTICS=$diagnostic" >&2
  fi
  exit 1
}
sha(){ sha256sum -- "$1" | awk '{print $1}'; }
# Terminate only processes created by this builder. Do not wait indefinitely
# for QEMU on interrupted builds or on failure during cloud-init.
terminate_child(){
  local pid="${1:-}" attempt
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; return 0; }
  kill -TERM "$pid" 2>/dev/null || true
  for ((attempt=0; attempt<40; attempt++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}
cleanup(){
  trap - INT TERM
  terminate_child "$qemu_pid"
  terminate_child "$proxy_pid"
  [[ -z "$promote_image_tmp" || ! -e "$promote_image_tmp" ]] || rm -f -- "$promote_image_tmp"
  [[ -z "$promote_prov_tmp" || ! -e "$promote_prov_tmp" ]] || rm -f -- "$promote_prov_tmp"
  [[ -z "$tmp" || ! -e "$tmp" ]] || rm -rf -- "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
usage(){ echo "Usage: $0 [--dry-run|--dry-run-v2|--build|--build-v2|--verify CANDIDATE|--promote-dry-run CANDIDATE|--promote CANDIDATE]"; }
check_host(){
  [[ "$(uname -m)" = x86_64 ]] || fail 'requires x86_64'
  [[ -r /dev/kvm && -w /dev/kvm ]] || fail 'current user needs /dev/kvm access'
  local m; m="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
  [[ "$m" =~ ^[0-9]+$ ]] && ((m >= 8*1024*1024)) || fail 'requires 8 GiB available RAM'
  for c in qemu-img qemu-system-x86_64 nc python3 sha256sum install stat df awk flock timeout; do command -v "$c" >/dev/null || fail "missing tool $c"; done
  [[ -r "$dir/../kvm-egress-proxy.py" ]] || fail 'allowlist proxy missing'
}
check_base(){
  [[ -f "$base" && ! -L "$base" && -r "$base" ]] || fail 'stock image must be a readable regular non-symlink file'
  # test -w is privilege-sensitive: root may report writable for a 0444
  # file. Inspect the actual mode bits so validation is consistent for every caller.
  local permissions
  permissions="$(stat -c %a -- "$base")" || fail 'cannot inspect stock image permissions'
  [[ "$permissions" =~ ^[0-7]{3,4}$ ]] || fail 'cannot parse stock image permissions'
  (( (8#$permissions & 0222) == 0 )) || fail 'stock image has write permission bits'
  [[ "$(sha "$base")" = "$base_sha" ]] || fail 'stock image digest mismatch'
  qemu-img check -- "$base" >/dev/null || fail 'stock qcow2 check failed'
}
check_space(){
  local path="$1" avail
  while [[ ! -d "$path" ]]; do path="$(dirname "$path")"; done
  avail="$(df -Pm "$path" | awk 'NR==2 {print $4}')"
  [[ "$avail" =~ ^[0-9]+$ ]] && ((avail >= 25600)) || fail 'requires 25 GiB free on build-output filesystem'
}
validate(){
  local f="$1" digest
  [[ -f "$f" && ! -L "$f" && -f "$f.provenance.json" ]] || fail 'candidate or provenance sidecar missing'
  digest="$(sha "$f")"
  qemu-img info --output=json -- "$f" | python3 -c 'import json,sys; assert json.load(sys.stdin)["format"]=="qcow2"'
  qemu-img check -- "$f" >/dev/null || fail 'candidate qcow2 invalid'
  python3 - "$f.provenance.json" "$digest" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]))
assert p["schema"] in (1, 2) and p["candidate_sha256"]==sys.argv[2]
sources={1:"6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2",
         2:"18db16acc6085326c9d4a5dc10dbefbe4bf4d37b01031da588a9ecf5c27e10a5"}
assert p["source_sha256"]==sources[p["schema"]]
if p["schema"] == 2:
    assert len(p["toolchain_manifest_sha256"])==64
    assert p["dependency_source_sha256"]=="3028a05e602a9572ba41bc1f3daa28d0fdb762c0"
assert p["runner_version"]=="2.338.0" and p["runner_sha256"]=="af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32"
assert len(p["package_manifest_sha256"])==64
PY
  echo "CANDIDATE_SHA256=$digest"
}
case "$mode" in
--dry-run|--dry-run-v2)
 check_base
 check_host
 check_space "$out"
 echo 'Read-only preflight passed; no VM/image created.' ;;
--verify) [[ $# = 2 ]] || { usage >&2; exit 2; }; check_host; validate "$candidate" ;;
--promote-dry-run|--promote)
 [[ $# = 2 ]] || { usage >&2; exit 2; }
 [[ "$mode" = --promote-dry-run || "$(id -u)" = 0 ]] || fail 'promotion requires owner-run sudo; this script never invokes sudo'
 check_host; validate "$candidate"
 [[ -d "$golden" && "$(stat -c %u:%g:%a "$golden")" = 0:0:755 ]] || fail 'golden directory not prepared'
 if docker inspect recursive-coder-kvm-runner-manager >/dev/null 2>&1; then
   if [[ "$(docker inspect recursive-coder-kvm-runner-manager --format "{{.State.Running}}")" = true ]]; then
     active="$(docker exec recursive-coder-kvm-runner-manager python3 /usr/local/bin/kvm-backend.py list)" || fail 'cannot inspect active VMs'
     [[ -z "$active" ]] || fail 'wait until the active KVM VM has been cleaned up'
     docker inspect recursive-coder-kvm-runner-manager --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -Eq '^KVM_GOLDEN_IMAGE=/golden/noble-server-cloudimg-amd64(-prepared-[0-9a-f]{12})?\.img$' || fail 'manager golden image selection is invalid; refusing to stage'
   fi
 fi
 digest="$(sha "$candidate")"; short="$(printf %s "$digest" | cut -c1-12)"
 target="$golden/noble-server-cloudimg-amd64-prepared-$short.img"
 if [[ -e "$target" || -L "$target" ]]; then
   [[ -f "$target" && ! -L "$target" && "$(sha "$target")" = "$digest" && "$(stat -c %u:%g:%a "$target")" = 0:0:444 ]] || fail 'existing digest target differs; refusing overwrite'
   [[ -f "$target.provenance.json" && "$(sha "$target.provenance.json")" = "$(sha "$candidate.provenance.json")" ]] || fail 'existing provenance differs; refusing overwrite'
   echo "Prepared image already promoted: $(basename "$target")"
 else
   [[ "$mode" = --promote ]] || { echo "PROMOTION_TARGET=$target"; echo "PROMOTION_SHA256=$digest"; exit 0; }
   promote_image_tmp="$golden/.$(basename "$target").part.$$"
   promote_prov_tmp="$golden/.$(basename "$target").provenance.part.$$"
   [[ ! -e "$promote_image_tmp" && ! -e "$promote_prov_tmp" ]] || fail 'promotion temporary path already exists'
   install -o root -g root -m 0600 -- "$candidate" "$promote_image_tmp"
   install -o root -g root -m 0600 -- "$candidate.provenance.json" "$promote_prov_tmp"
   [[ "$(sha "$promote_image_tmp")" = "$digest" ]] || fail 'staged image digest mismatch'
   [[ "$(sha "$promote_prov_tmp")" = "$(sha "$candidate.provenance.json")" ]] || fail 'staged provenance digest mismatch'
   chmod 0444 "$promote_image_tmp" "$promote_prov_tmp"; chown root:root "$promote_image_tmp" "$promote_prov_tmp"
   mv -nT -- "$promote_prov_tmp" "$target.provenance.json"; promote_prov_tmp=""
   mv -nT -- "$promote_image_tmp" "$target"; promote_image_tmp=""
   [[ "$(sha "$target")" = "$digest" ]] || fail 'promoted digest mismatch'
 fi
 echo "PROMOTED_FILENAME=$(basename "$target")"; echo "PROMOTED_SHA256=$digest"; echo 'Stock golden preserved.' ;;
--build|--build-v2)
 [[ $# = 1 ]] || { usage >&2; exit 2; }
 if [[ "$mode" = --build-v2 ]] && docker inspect recursive-coder-kvm-runner-manager >/dev/null 2>&1; then
   active="$(docker exec recursive-coder-kvm-runner-manager python3 /usr/local/bin/kvm-backend.py list)" || fail 'cannot inspect active KVM guests'
   [[ -z "$active" ]] || fail 'wait until current KVM job has been cleaned up before building v2'
 fi
 check_host; check_base; command -v xorriso >/dev/null || fail 'build requires xorriso; install it through the owner-approved host preparation procedure'; mkdir -p "$out"
 check_space "$out"
 exec 9>"$out/build.lock"; flock -n 9 || fail 'another image build is active'
 tmp="$(mktemp -d "$out/.build.XXXXXX")"
 port="$(python3 - <<'PY'
import socket
s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()
PY
)"
 printf '#!/bin/sh\nexec /usr/bin/nc -N 127.0.0.1 %s\n' "$port" >"$tmp/proxy-bridge"; chmod 0700 "$tmp/proxy-bridge"
 python3 "$dir/../kvm-egress-proxy.py" "$port" >"$tmp/proxy.log" 2>&1 & proxy_pid=$!
 for _ in $(seq 1 50); do grep -q '^listening=' "$tmp/proxy.log" && break; sleep .1; done
 grep -q '^listening=' "$tmp/proxy.log" || fail 'allowlist proxy failed'
 format="$(qemu-img info --output=json -- "$base" | python3 -c 'import json,sys;print(json.load(sys.stdin)["format"])')"
 overlay="$tmp/overlay.qcow2"; seed="$tmp/seed.iso"; serial="$tmp/serial.log"
 qemu-img create -f qcow2 -F "$format" -b "$base" "$overlay" 12G >/dev/null
 cat >"$tmp/bootstrap.sh" <<'GUEST'
#!/bin/bash
set -euo pipefail
echo BUILDER_BOOTSTRAP_START=$(cut -d" " -f1 /proc/uptime) >>/dev/ttyS0
export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128
cat >/etc/apt/apt.conf.d/90builder-proxy <<'APT'
Acquire::http::Proxy "http://10.0.2.100:3128/";
Acquire::https::Proxy "http://10.0.2.100:3128/";
APT
echo BUILDER_APT_START=$(cut -d" " -f1 /proc/uptime) >>/dev/ttyS0
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash bubblewrap ca-certificates curl eject git jq nftables openjdk-21-jdk procps python3 python3-pip python3-venv tar unzip xz-utils libatomic1 libcurl4 libgcc-s1 libicu74 libkrb5-3 liblttng-ust1 libssl3t64 libstdc++6 libunwind8 zlib1g libbz2-1.0 libffi8 liblzma5 libreadline8t64 libsqlite3-0 libncursesw6 libgdbm6t64 libuuid1
echo BUILDER_APT_END=$(cut -d" " -f1 /proc/uptime) >>/dev/ttyS0
dpkg-query -W -f='${binary:Package}=${Version}\\n' | sort >/var/lib/dpkg-package-manifest.txt
chmod 0444 /var/lib/dpkg-package-manifest.txt
install -d -o root -g root -m 0755 /opt/recursive-coder-kvm
echo BUILDER_RUNNER_INSTALL_START=$(cut -d" " -f1 /proc/uptime) >>/dev/ttyS0
curl -fsSL --retry 3 https://github.com/actions/runner/releases/download/v2.338.0/actions-runner-linux-x64-2.338.0.tar.gz -o /tmp/runner.tgz
echo 'af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32  /tmp/runner.tgz' | sha256sum -c -
install -o root -g root -m 0444 /tmp/runner.tgz /opt/recursive-coder-kvm/actions-runner.tar.gz
echo BUILDER_RUNNER_INSTALL_END=$(cut -d" " -f1 /proc/uptime) >>/dev/ttyS0
echo 'schema=1 runner=2.338.0 sha256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32' >/opt/recursive-coder-kvm/prepared.manifest
chmod 0444 /opt/recursive-coder-kvm/prepared.manifest
rm -f /tmp/runner.tgz
useradd --create-home --uid 1001 --shell /bin/bash runner
sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
runuser -u runner -- /usr/bin/bwrap --die-with-parent --new-session --unshare-all --tmpfs / --proc /proc --dev /dev --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 -- /usr/bin/true
echo BUILDER_BWRAP_PROBE=PASS >>/dev/ttyS0
userdel --remove runner
rm -f /etc/apt/apt.conf.d/90builder-proxy /etc/ssh/ssh_host_* /var/lib/dbus/machine-id
pkg_sha=$(sha256sum /var/lib/dpkg-package-manifest.txt | cut -d' ' -f1)
echo BUILDER_PACKAGES_SHA256=$pkg_sha >>/dev/ttyS0
rm -f /var/lib/kvm-image-builder.sh
cloud-init clean --logs --machine-id --seed
rm -rf /var/lib/cloud /var/log/cloud-init.log /var/log/cloud-init-output.log
truncate -s 0 /etc/machine-id
sync
echo BUILDER_IMAGE_READY=PASS >>/dev/ttyS0
systemctl poweroff --force
GUEST
 if [[ "$mode" = --build-v2 ]]; then
   [[ -r "$dir/recursive-coder-golden-v2-guest.sh" ]] || fail 'v2 guest bootstrap not found'
   cp "$dir/recursive-coder-golden-v2-guest.sh" "$tmp/bootstrap.sh"
 fi
 python3 - "$tmp" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
u={"ssh":{"emit_keys_to_console":False},"write_files":[{"path":"/var/lib/kvm-image-builder.sh","owner":"root:root","permissions":"0700","content":(p/"bootstrap.sh").read_text()}],"runcmd":[["bash","/var/lib/kvm-image-builder.sh"]]}
(p/"user-data").write_text("#cloud-config\n"+json.dumps(u))
(p/"meta-data").write_text("instance-id: kvm-golden-builder\nlocal-hostname: kvm-golden-builder\n")
PY
 xorriso -as mkisofs -quiet -output "$seed" -volid cidata -joliet -rock "$tmp/user-data" "$tmp/meta-data"
 qemu-system-x86_64 -nodefaults -no-user-config -machine q35,accel=kvm -cpu host -smp 2 -m 4096 -display none -no-reboot -serial "file:$serial" -monitor none \
  -drive "if=virtio,format=qcow2,file=$overlay" -drive "if=ide,media=cdrom,readonly=on,format=raw,file=$seed" \
  -netdev "user,id=net0,restrict=on,ipv6=off,guestfwd=tcp:10.0.2.100:3128-cmd:$tmp/proxy-bridge" -device virtio-net-pci,netdev=net0 >"$tmp/qemu.log" 2>&1 & qemu_pid=$!
 SECONDS=0
 while kill -0 "$qemu_pid" 2>/dev/null && ! grep -q 'BUILDER_BOOTSTRAP_START=' "$serial" 2>/dev/null; do sleep .2; done
 boot_elapsed="$SECONDS"
 if ! timeout 1200 bash -c 'while kill -0 "$1" 2>/dev/null; do sleep 2; done' _ "$qemu_pid"; then fail 'builder exceeded 20-minute limit'; fi
 wait "$qemu_pid" || fail 'builder VM failed'; qemu_pid=
 grep -q BUILDER_BWRAP_PROBE=PASS "$serial" || fail 'Bubblewrap probe failed'
 grep -q BUILDER_IMAGE_READY=PASS "$serial" || fail 'guest did not shut down cleanly'
 marker(){ sed -n "s/.*$1=//p" "$serial" | tail -n 1 | tr -d '\r'; }
 apt_start="$(marker BUILDER_APT_START)"; apt_end="$(marker BUILDER_APT_END)"
 runner_start="$(marker BUILDER_RUNNER_INSTALL_START)"; runner_end="$(marker BUILDER_RUNNER_INSTALL_END)"
 [[ "$apt_start" =~ ^[0-9]+([.][0-9]+)?$ && "$apt_end" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail 'apt timing markers missing'
 [[ "$runner_start" =~ ^[0-9]+([.][0-9]+)?$ && "$runner_end" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail 'runner archive timing markers missing'
 apt_elapsed="$(python3 -c 'import sys; print(round(float(sys.argv[2])-float(sys.argv[1]),1))' "$apt_start" "$apt_end")"
 runner_elapsed="$(python3 -c 'import sys; print(round(float(sys.argv[2])-float(sys.argv[1]),1))' "$runner_start" "$runner_end")"
 vm_elapsed="$SECONDS"
 pkg_sha="$(sed -n 's/.*BUILDER_PACKAGES_SHA256=//p' "$serial" | tail -n 1 | tr -d '\r')"
 [[ "$pkg_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'package provenance digest missing'
 v2_manifest_sha=""
 if [[ "$mode" = --build-v2 ]]; then
   grep -q 'BUILDER_V2_TOOLCHAIN=PASS' "$serial" || fail 'v2 toolchain probe failed'
   v2_manifest_sha="$(marker BUILDER_V2_MANIFEST_SHA256)"
   [[ "$v2_manifest_sha" =~ ^[0-9a-f]{64}$ ]] || fail 'v2 manifest digest missing'
 fi
 candidate="$out/noble-server-cloudimg-amd64-prepared-$(date -u +%Y%m%dT%H%M%SZ).img"
 qemu-img convert -f qcow2 -O qcow2 -o compat=1.1,lazy_refcounts=off "$overlay" "$candidate"
 qemu-img check -- "$candidate" >/dev/null; chmod 0444 "$candidate"; digest="$(sha "$candidate")"
 python3 - "$candidate.provenance.json" "$digest" "$pkg_sha" "$mode" "$v2_manifest_sha" <<'PY'
import json,platform,sys,time
schema=2 if sys.argv[4]=="--build-v2" else 1
source=("18db16acc6085326c9d4a5dc10dbefbe4bf4d37b01031da588a9ecf5c27e10a5" if schema==2
        else "6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2")
p={"schema":schema,"created_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"builder_host_arch":platform.machine(),"source_sha256":source,"runner_version":"2.338.0","runner_sha256":"af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32","candidate_sha256":sys.argv[2],"package_manifest_sha256":sys.argv[3],"credential_scan":"not-applicable-no-credential-inputs","network":"qemu-user restrict=on; HTTPS allowlist proxy only"}
if schema==2:
    p["toolchain_manifest_sha256"]=sys.argv[5]
    p["dependency_source_sha256"]="3028a05e602a9572ba41bc1f3daa28d0fdb762c0"
open(sys.argv[1],"w").write(json.dumps(p,sort_keys=True,indent=2)+"\n")
PY
 chmod 0444 "$candidate.provenance.json"; grep -E 'BUILDER_(BWRAP_PROBE|IMAGE_READY)=PASS' "$serial"
 echo "CANDIDATE=$candidate"; echo "SHA256=$digest"
 echo "TIMING_VM_BOOT_TO_BOOTSTRAP_SECONDS=$boot_elapsed"
 echo "TIMING_VM_RUN_TO_SHUTDOWN_SECONDS=$vm_elapsed"
 echo "TIMING_APT_PROVISION_SECONDS=$apt_elapsed"
 if [[ "$mode" = --build-v2 ]]; then
   cache_start="$(marker BUILDER_V2_CACHE_START)"
   cache_end="$(marker BUILDER_V2_CACHE_END)"
   [[ "$cache_start" =~ ^[0-9]+([.][0-9]+)?$ && "$cache_end" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail 'v2 cache timing markers missing'
   cache_elapsed="$(python3 -c 'import sys; print(round(float(sys.argv[2])-float(sys.argv[1]),1))' "$cache_start" "$cache_end")"
   echo "TIMING_V2_TOOLCHAINS_INSTALL_SECONDS=$runner_elapsed"
   echo "TIMING_V2_DEPENDENCY_PREWARM_SECONDS=$cache_elapsed"
 else
   echo "TIMING_RUNNER_ARCHIVE_INSTALL_SECONDS=$runner_elapsed"
 fi
 echo "TIMING_REGISTRATION=not-measured-no-credentials"
 echo "TIMING_GITHUB_JOB_READINESS=not-measured-no-live-runner-registration"
 echo 'Stock golden unchanged.' ;;
*) usage >&2; exit 2 ;;
esac
