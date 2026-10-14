#!/usr/bin/env python3
"""One disposable Ubuntu/KVM guest per ephemeral recursive-coder runner."""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = "YuriiSokolenko/recursive-coder"
RUNNER_URL = f"https://github.com/{REPOSITORY}"
RUNNER_VERSION = "2.338.0"
RUNNER_SHA256 = "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32"
RUNNER_LABELS = "n150,recursive-coder-ci"
NAME_RE = re.compile(r"^n150-rc-eph-[A-Za-z0-9-]{1,80}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{20,512}$")
GUEST_PROXY = "10.0.2.100:3128"
DIAGNOSTIC_RETENTION = 10
DIAGNOSTIC_MAX_BYTES = 128 * 1024
DIAGNOSTIC_TAIL_BYTES = 12 * 1024
DIAGNOSTIC_SERIAL_SCAN_BYTES = 4 * 1024 * 1024
DIAGNOSTIC_GUEST_BLOCK_BYTES = 32 * 1024
DIAGNOSTIC_SECRET_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:(?:bearer|token)\s+)?|(?:token|password|secret|credential|authorization)[\w.-]*\s*[:=]\s*)[^\s]+")
DIAGNOSTIC_GH_TOKEN_RE = re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
QEMU_ENV_KEYS = (
    "KVM_GOLDEN_IMAGE", "KVM_GOLDEN_SHA256", "KVM_STATE_DIR", "KVM_QEMU_BINARY",
    "KVM_QEMU_IMG_BINARY", "KVM_XORRISO_BINARY", "KVM_PROXY_PORT", "KVM_PROXY_BRIDGE",
    "KVM_GUEST_MEMORY_MB", "KVM_GUEST_CPUS", "KVM_OVERLAY_MAX_GIB", "KVM_MAX_LIFETIME_SECONDS",
)


def config() -> dict[str, object]:
    return {
        "golden": Path(os.environ.get("KVM_GOLDEN_IMAGE", "/golden/noble-server-cloudimg-amd64.img")),
        "golden_sha256": os.environ.get("KVM_GOLDEN_SHA256", ""),
        "state": Path(os.environ.get("KVM_STATE_DIR", "/kvm-state")),
        "qemu": os.environ.get("KVM_QEMU_BINARY", "qemu-system-x86_64"),
        "qemu_img": os.environ.get("KVM_QEMU_IMG_BINARY", "qemu-img"),
        "xorriso": os.environ.get("KVM_XORRISO_BINARY", "xorriso"),
        "bridge": os.environ.get("KVM_PROXY_BRIDGE", "/usr/local/bin/kvm-proxy-bridge"),
        "proxy_port": int(os.environ.get("KVM_PROXY_PORT", "43128")),
        "memory_mb": int(os.environ.get("KVM_GUEST_MEMORY_MB", "4096")),
        "cpus": int(os.environ.get("KVM_GUEST_CPUS", "2")),
        "overlay_gib": int(os.environ.get("KVM_OVERLAY_MAX_GIB", "24")),
        "lifetime": int(os.environ.get("KVM_MAX_LIFETIME_SECONDS", "1800")),
    }


def run(args: list[str], *, timeout: int = 60, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, timeout=timeout, text=True, capture_output=capture)


def validate_runtime(cfg: dict[str, object], *, require_device: bool = True) -> None:
    golden = Path(cfg["golden"])
    state = Path(cfg["state"])
    if not golden.is_file() or golden.stat().st_mode & 0o222:
        raise RuntimeError("golden image must exist and be read-only")
    expected = str(cfg["golden_sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("KVM_GOLDEN_SHA256 must be a lowercase SHA-256 digest")
    if shutil.which(str(cfg["qemu"])) is None or shutil.which(str(cfg["qemu_img"])) is None:
        raise RuntimeError("QEMU and qemu-img must be installed")
    if shutil.which(str(cfg["xorriso"])) is None:
        raise RuntimeError("xorriso is required to create a cloud-init seed")
    if require_device and not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("/dev/kvm is not accessible to the KVM manager")
    if not state.exists():
        raise RuntimeError("KVM state directory is not provisioned")
    if state.stat().st_mode & 0o077:
        raise RuntimeError("KVM state directory must be mode 0700")
    if not 1 <= int(cfg["cpus"]) <= 2 or not 512 <= int(cfg["memory_mb"]) <= 4096:
        raise RuntimeError("guest resources exceed the two-vCPU/four-GiB policy")
    if not 1 <= int(cfg["overlay_gib"]) <= 24 or int(cfg["lifetime"]) <= 0:
        raise RuntimeError("guest disk or lifetime exceeds the configured bound")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_image(cfg: dict[str, object]) -> None:
    validate_runtime(cfg)
    golden = Path(cfg["golden"])
    if sha256_file(golden) != cfg["golden_sha256"]:
        raise RuntimeError("golden image SHA-256 mismatch")
    info = json.loads(run([str(cfg["qemu_img"]), "info", "--output=json", str(golden)], capture=True).stdout)
    if info.get("format") not in {"qcow2", "raw"}:
        raise RuntimeError(f"unsupported golden image format: {info.get('format')}")


def cloud_init(name: str, token: str) -> tuple[str, str]:
    token_b64 = base64.b64encode(token.encode("ascii")).decode("ascii")
    bootstrap = f"""#!/bin/bash
set -euo pipefail
umask 077
trap 'code=$?; shred -u /var/lib/runner-bootstrap/token.b64 2>/dev/null || true; systemctl poweroff --force 2>/dev/null || poweroff -f 2>/dev/null || true; exit "$code"' ERR
export http_proxy=http://{GUEST_PROXY} https_proxy=http://{GUEST_PROXY}
export HTTP_PROXY="$http_proxy" HTTPS_PROXY="$https_proxy" ALL_PROXY="$https_proxy"
cat >/etc/apt/apt.conf.d/90runner-proxy <<'APT'
Acquire::http::Proxy \"http://{GUEST_PROXY}/\";
Acquire::https::Proxy \"http://{GUEST_PROXY}/\";
APT
stage() {{ printf 'kvm_bootstrap_stage=%s guest_uptime_s=%s\\n' "$1" "$(cut -d' ' -f1 /proc/uptime)" >>/dev/ttyS0 2>/dev/null || true; }}
stage provisioning_start
# A prepared golden is immutable and digest-pinned by verify_image(). Never bake
# ephemeral runner registration, identity keys, job workspaces, or credentials.
prepared_runner=""
if [ -e /opt/recursive-coder-kvm/prepared.manifest ]; then
  expected_manifest='schema=1 runner={RUNNER_VERSION} sha256={RUNNER_SHA256}'
  [ "$(cat /opt/recursive-coder-kvm/prepared.manifest)" = "$expected_manifest" ]
  prepared_runner=/opt/recursive-coder-kvm/actions-runner.tar.gz
  [ -f "$prepared_runner" ]
  echo '{RUNNER_SHA256}  /opt/recursive-coder-kvm/actions-runner.tar.gz' | sha256sum -c -
  for required in git jq nft bwrap java python3 curl eject runuser; do
    command -v "$required" >/dev/null
  done
  stage prepared_golden_verified
  if [ -e /opt/recursive-coder-kvm/toolchain-v2.manifest ]; then
    expected_v2='schema=2 gradle=9.8.1 gradle_sha256=dce76f55f8e251a3a1f130eb120f30b3d271de2b76c9b0729d316b5a1b6dc01f node=22.21.1 node_sha256=680d3f30b24a7ff24b98db5e96f294c0070f8f9078df658da1bce1b9c9873c88 python=3.12 jsonschema=4.25.1 source=3028a05e602a9572ba41bc1f3daa28d0fdb762c0'
    [ "$(cat /opt/recursive-coder-kvm/toolchain-v2.manifest)" = "$expected_v2" ]
    /opt/recursive-coder-kvm/gradle-9.8.1/bin/gradle --version | grep -Fq 'Gradle 9.8.1'
    [ "$(/opt/recursive-coder-kvm/node-v22.21.1-linux-x64/bin/node --version)" = v22.21.1 ]
    /opt/recursive-coder-kvm/python/bin/python -c 'from importlib.metadata import version; assert version("jsonschema") == "4.25.1"'
    [ -d /opt/recursive-coder-kvm/gradle-home ]
    # The v2 toolchains were installed by root with umask 077. Their original
    # directory modes can block the unprivileged Actions runner (EACCES when
    # checkout searches python/bin/git). Fix access only inside this disposable
    # guest's writable overlay; the SHA-pinned host golden remains immutable.
    chmod -R a+rX /opt/recursive-coder-kvm/python \
      /opt/recursive-coder-kvm/gradle-9.8.1 \
      /opt/recursive-coder-kvm/node-v22.21.1-linux-x64
    stage baked_ci_toolchain_verified
  fi
else
  stage apt_start
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
    bash bubblewrap ca-certificates curl eject git jq nftables openjdk-21-jdk procps python3 python3-pip \\
    python3-venv tar unzip xz-utils libatomic1 libcurl4 libgcc-s1 libicu74 \\
    libkrb5-3 liblttng-ust1 libssl3t64 libstdc++6 libunwind8 zlib1g libbz2-1.0 \\
    libffi8 liblzma5 libreadline8t64 libsqlite3-0 libncursesw6 libgdbm6t64 libuuid1
  stage apt_complete
fi
useradd --create-home --uid 1001 --shell /bin/bash runner
usermod --lock --shell /usr/sbin/nologin ubuntu
gpasswd --delete ubuntu sudo >/dev/null 2>&1 || true
install -d -o runner -g runner -m 0755 /home/runner/actions-runner
stage runner_package_start
if [ -n "$prepared_runner" ]; then
  tar -xzf "$prepared_runner" -C /home/runner/actions-runner
else
  curl -fsSL --retry 3 \\
    https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz \\
    -o /tmp/actions-runner.tar.gz
  echo '{RUNNER_SHA256}  /tmp/actions-runner.tar.gz' | sha256sum -c -
  tar -xzf /tmp/actions-runner.tar.gz -C /home/runner/actions-runner
  rm -f /tmp/actions-runner.tar.gz
fi
stage runner_package_complete
chown -R runner:runner /home/runner/actions-runner
chmod 0755 /usr/bin/bwrap
chown root:root /usr/bin/bwrap
cat >/etc/sysctl.d/90-recursive-coder-bwrap-userns.conf <<'SYSCTL'
kernel.apparmor_restrict_unprivileged_userns=0
SYSCTL
sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
runuser -u runner -- /usr/bin/bwrap --die-with-parent --new-session --unshare-all \\
  --tmpfs / --proc /proc --dev /dev \\
  --ro-bind /usr /usr --ro-bind /bin /bin \\
  --ro-bind /lib /lib --ro-bind /lib64 /lib64 \\
  -- /usr/bin/true
stage runner_registration_start
token="$(base64 -d /var/lib/runner-bootstrap/token.b64)"
ACTIONS_RUNNER_INPUT_TOKEN="$token" runuser -u runner --preserve-environment -- \\
  /home/runner/actions-runner/config.sh \\
  --url {RUNNER_URL} --name {name} --labels {RUNNER_LABELS} \\
  --ephemeral --disableupdate --unattended --work _work
unset token
stage runner_registration_complete
shred -u /var/lib/runner-bootstrap/token.b64
rm -rf /var/lib/runner-bootstrap
eject -r /dev/sr0 >/dev/null 2>&1 || true
rm -f /var/lib/cloud/instance/user-data.txt
instance_id="$(cat /var/lib/cloud/data/instance-id 2>/dev/null || true)"
if [ -n "$instance_id" ]; then rm -f "/var/lib/cloud/instances/$instance_id/user-data.txt"; fi
rm -f /run/cloud-init/instance-data-sensitive.json
rm -f /var/log/cloud-init.log /var/log/cloud-init-output.log
cat >/usr/local/sbin/runner-shutdown-diagnostics <<'DIAGNOSTICS'
#!/bin/bash
set +e
{{
  echo '=== runner-shutdown-diagnostics-v1 ==='
  printf 'service_result=%s exit_code=%s exit_status=%s time=%s\n' \
    "${{SERVICE_RESULT:-unknown}}" "${{EXIT_CODE:-unknown}}" "${{EXIT_STATUS:-unknown}}" "$(date -u +%FT%TZ)"
  systemctl show actions-runner.service -p ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus
  systemctl show systemd-oomd.service -p ActiveState -p SubState -p Result
  echo '=== memory ==='
  grep -E 'MemAvailable|SwapFree' /proc/meminfo
  cat /proc/pressure/memory 2>/dev/null
  echo '=== filesystem ==='
  df -h / /home/runner 2>/dev/null
  echo '=== kernel oom/oomd records ==='
  journalctl --no-pager -k -b --since '-30 minutes' 2>/dev/null | grep -Ei 'out of memory|oom-kill|killed process|systemd-oomd' | tail -n 80
  echo '=== runner service failure records ==='
  journalctl --no-pager -u actions-runner.service -b --since '-30 minutes' 2>/dev/null | grep -Ei 'failed|error|exit|shutdown signal|result|status' | tail -n 80
}} | sed -E 's/(gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)/[REDACTED_GITHUB_TOKEN]/g; s/((token|password|secret|authorization)[^ =:]*[=: ]+)[^ ]+/\\1[REDACTED]/Ig' | tail -c 32768 >>/dev/ttyS0 2>/dev/null || true
echo '=== runner-shutdown-diagnostics-end ===' >>/dev/ttyS0 2>/dev/null || true
DIAGNOSTICS
chmod 0755 /usr/local/sbin/runner-shutdown-diagnostics
cat >/etc/systemd/system/actions-runner.service <<'UNIT'
[Unit]
Description=Ephemeral GitHub Actions runner
After=network-online.target cloud-final.service
Wants=network-online.target

[Service]
Type=simple
User=runner
WorkingDirectory=/home/runner/actions-runner
Environment=HTTP_PROXY=http://{GUEST_PROXY}
Environment=HTTPS_PROXY=http://{GUEST_PROXY}
Environment=http_proxy=http://{GUEST_PROXY}
Environment=https_proxy=http://{GUEST_PROXY}
Environment="JAVA_TOOL_OPTIONS=-Dhttp.proxyHost=10.0.2.100 -Dhttp.proxyPort=3128 -Dhttps.proxyHost=10.0.2.100 -Dhttps.proxyPort=3128 -Dhttp.nonProxyHosts=localhost|127.*"
Environment=NO_PROXY=localhost,127.0.0.1
ExecStart=/home/runner/actions-runner/run.sh
# '+' makes systemd run this post-stop command with full privileges even though
# the long-lived Actions listener runs as the unprivileged runner account.
ExecStopPost=+/usr/local/sbin/runner-shutdown-diagnostics
ExecStopPost=+/usr/bin/systemctl --no-block poweroff
Restart=no
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNIT
if [ -e /opt/recursive-coder-kvm/toolchain-v2.manifest ]; then
  install -d -m 0755 /etc/systemd/system/actions-runner.service.d
  cat >/etc/systemd/system/actions-runner.service.d/10-baked-ci-tools.conf <<'CI_TOOLS'
[Service]
Environment="PATH=/opt/recursive-coder-kvm/gradle-9.8.1/bin:/opt/recursive-coder-kvm/node-v22.21.1-linux-x64/bin:/opt/recursive-coder-kvm/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="GRADLE_USER_HOME=/opt/recursive-coder-kvm/gradle-home"
Environment="GRADLE_OPTS=-Dorg.gradle.workers.max=1 -Dorg.gradle.jvmargs=-Xmx1g"
Environment="JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64"
CI_TOOLS
fi
systemctl daemon-reload
cat >/etc/nftables.conf <<'NFT'
flush ruleset
table inet runner_egress {{
  chain output {{
    type filter hook output priority 0; policy drop;
    oifname "lo" accept
    ct state established,related accept
    ip daddr 10.0.2.100 tcp dport 3128 accept
    udp sport 68 udp dport 67 accept
  }}
}}
NFT
systemctl enable nftables.service
systemctl restart nftables.service
systemctl enable actions-runner.service
# cloud-final is still running this bootstrap, so do not synchronously start a
# unit ordered After=cloud-final.service. A transient timer starts it once
# cloud-init has returned and systemd has completed cloud-final.
stage runner_ready
systemd-run --quiet --unit=actions-runner-delayed-start --on-active=10s \\
  /usr/bin/systemctl start actions-runner.service
"""
    userdata = f"""#cloud-config
write_files:
  - path: /var/lib/runner-bootstrap/token.b64
    owner: root:root
    permissions: '0600'
    content: |
      {token_b64}
  - path: /var/lib/runner-bootstrap/bootstrap.sh
    owner: root:root
    permissions: '0700'
    content: |
""" + "".join(f"      {line}\n" for line in bootstrap.splitlines()) + "runcmd:\n  - [bash, /var/lib/runner-bootstrap/bootstrap.sh]\n"
    metadata = json.dumps({"instance-id": name, "local-hostname": name}) + "\n"
    return userdata, metadata


def qemu_command(name: str, cfg: dict[str, object], state_dir: Path) -> list[str]:
    overlay = state_dir / "root.qcow2"
    seed = state_dir / "seed.iso"
    qemu = str(cfg["qemu"])
    return [
        qemu,
        "-nodefaults", "-no-user-config",
        "-machine", "q35,accel=kvm", "-cpu", "host",
        "-smp", str(cfg["cpus"]), "-m", str(cfg["memory_mb"]),
        "-display", "none", "-no-reboot",
        "-serial", f"file:{state_dir / 'serial.log'}", "-monitor", "none",
        "-pidfile", str(state_dir / "qemu.pid"),
        "-drive", f"if=virtio,format=qcow2,file={overlay}",
        "-drive", f"if=ide,media=cdrom,readonly=on,format=raw,file={seed}",
        "-netdev", (
            f"user,id=net0,restrict=on,ipv6=off,"
            f"guestfwd=tcp:10.0.2.100:3128-cmd:{cfg['bridge']}"
        ),
        "-device", "virtio-net-pci,netdev=net0",
    ]


def _proc_matches(pid: int, text: str) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return text in command
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False


def _pid_alive(pid: int, expected: str) -> bool:
    return pid > 1 and _proc_matches(pid, expected)


def _write_state(path: Path, metadata: dict[str, object]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(metadata) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def _stop_qemu_if_present(state_dir: Path, pid: int) -> None:
    pidfile = str(state_dir / "qemu.pid")
    if not _pid_alive(pid, pidfile):
        try:
            pid = int((state_dir / "qemu.pid").read_text().strip())
        except (OSError, ValueError):
            return
        if not _pid_alive(pid, pidfile):
            return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_alive(pid, pidfile):
        time.sleep(0.1)
    if _pid_alive(pid, pidfile):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _redact_diagnostic_text(value: str) -> str:
    value = DIAGNOSTIC_SECRET_RE.sub("[REDACTED]", value)
    value = DIAGNOSTIC_GH_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", value)
    return re.sub(r"/home/runner(?:/[^\s,;]+)?", "[RUNNER_PATH]", value)


def _bounded_tail(path: Path, limit: int = DIAGNOSTIC_TAIL_BYTES) -> str:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - limit))
            raw = source.read(limit)
        return _redact_diagnostic_text(raw.decode("utf-8", errors="replace"))[-limit:]
    except OSError:
        return ""


def _extract_serial_evidence(path: Path) -> dict[str, object]:
    """Read a bounded serial window before cleanup; keep guest diagnosis separate from shutdown spam."""
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(0, source.tell() - DIAGNOSTIC_SERIAL_SCAN_BYTES))
            window = source.read(DIAGNOSTIC_SERIAL_SCAN_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {"shutdown_marker_found": False, "shutdown_details": "", "bootstrap_stages": []}

    start_marker = "=== runner-shutdown-diagnostics-v1 ==="
    end_marker = "=== runner-shutdown-diagnostics-end ==="
    start = window.rfind(start_marker)
    detail = ""
    if start >= 0:
        end = window.find(end_marker, start + len(start_marker))
        if end >= 0:
            end += len(end_marker)
        else:
            end = min(len(window), start + DIAGNOSTIC_GUEST_BLOCK_BYTES)
        detail = _redact_diagnostic_text(window[start:end])[:DIAGNOSTIC_GUEST_BLOCK_BYTES]

    stages = [
        _redact_diagnostic_text(line.strip())[:256]
        for line in window.splitlines()
        if line.startswith("kvm_bootstrap_stage=")
    ]
    return {
        "shutdown_marker_found": start >= 0,
        "shutdown_details": detail,
        "bootstrap_stages": stages[-24:],
    }


def _read_cgroup_diagnostics(root: Path = Path("/sys/fs/cgroup")) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("memory.current", "memory.peak", "memory.swap.current", "memory.events"):
        try:
            result[name] = _redact_diagnostic_text((root / name).read_text()[:2048])
        except OSError:
            result[name] = "unavailable"
    return result


def preserve_diagnostics(
    state_dir: Path,
    reason: str,
    qemu_exit_status: int | None,
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    now: datetime.datetime | None = None,
) -> Path | None:
    """Keep a bounded, sanitized incident record outside disposable VM state."""
    try:
        metadata = json.loads((state_dir / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        metadata = {"name": state_dir.name}
    timestamp = now or datetime.datetime.now(datetime.timezone.utc)
    diagnostic_root = state_dir.parent / "diagnostics"
    try:
        diagnostic_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(diagnostic_root, 0o700)
        record = {
            "name": _redact_diagnostic_text(str(metadata.get("name", state_dir.name)))[:128],
            "created_at": metadata.get("created_at"),
            "shutdown_at": timestamp.isoformat(),
            "reason": _redact_diagnostic_text(reason)[:256],
            "qemu_exit_status": qemu_exit_status,
            "host_cgroup": _read_cgroup_diagnostics(cgroup_root),
            "guest_evidence": _extract_serial_evidence(state_dir / "serial.log"),
            "logs": {name: _bounded_tail(state_dir / name)
                     for name in ("serial.log", "qemu.log", "supervisor.log")},
        }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = payload[:DIAGNOSTIC_MAX_BYTES]
        path = diagnostic_root / f"{state_dir.name}-{time.time_ns()}.json"
        temp = path.with_suffix(".tmp")
        temp.write_bytes(payload)
        os.chmod(temp, 0o600)
        temp.replace(path)
        retained = sorted(diagnostic_root.glob("n150-rc-eph-*.json"),
                          key=lambda item: item.stat().st_mtime, reverse=True)
        for expired in retained[DIAGNOSTIC_RETENTION:]:
            expired.unlink(missing_ok=True)
        return path
    except OSError:
        return None


def _supervisor_file_actions(output_fd: int) -> list[tuple[int, ...]]:
    actions = [
        (os.POSIX_SPAWN_DUP2, output_fd, 1),
        (os.POSIX_SPAWN_DUP2, output_fd, 2),
    ]
    try:
        open_fds = []
        for entry in os.listdir("/proc/self/fd"):
            if entry.isdigit():
                fd = int(entry)
                try:
                    os.fstat(fd)
                except OSError:
                    continue
                open_fds.append(fd)
    except OSError:
        open_fds = list(range(3, min(os.sysconf("SC_OPEN_MAX"), 65536)))
    actions.extend((os.POSIX_SPAWN_CLOSE, fd)
                   for fd in open_fds if fd > 2 and fd != output_fd)
    if output_fd > 2:
        actions.append((os.POSIX_SPAWN_CLOSE, output_fd))
    return actions


def cleanup_state(state_dir: Path) -> None:
    if state_dir.exists():
        shutil.rmtree(state_dir)


def recover(cfg: dict[str, object]) -> None:
    root = Path(cfg["state"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for state_dir in root.iterdir():
        if not state_dir.is_dir() or not NAME_RE.fullmatch(state_dir.name):
            continue
        try:
            metadata = json.loads((state_dir / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            _stop_qemu_if_present(state_dir, 0)
            preserve_diagnostics(state_dir, "recovery_invalid_metadata", None)
            cleanup_state(state_dir)
            continue
        supervisor_pid = int(metadata.get("supervisor_pid", 0))
        qemu_pid = int(metadata.get("qemu_pid", 0))
        supervisor_ok = _pid_alive(supervisor_pid, f"{Path(__file__).resolve()} supervise --name {state_dir.name}")
        qemu_ok = _pid_alive(qemu_pid, str(state_dir / "qemu.pid"))
        if supervisor_ok:
            continue
        if qemu_ok:
            _stop_qemu_if_present(state_dir, qemu_pid)
        preserve_diagnostics(state_dir, "recovery_orphaned_supervisor", None)
        cleanup_state(state_dir)


def active_names(cfg: dict[str, object]) -> list[str]:
    recover(cfg)
    root = Path(cfg["state"])
    active: list[str] = []
    for state_dir in root.iterdir():
        try:
            metadata = json.loads((state_dir / "state.json").read_text())
            pid = int(metadata["supervisor_pid"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        if _pid_alive(pid, f"{Path(__file__).resolve()} supervise --name {state_dir.name}"):
            active.append(state_dir.name)
    return sorted(active)


def create_seed(state_dir: Path, name: str, token: str, cfg: dict[str, object]) -> None:
    userdata, metadata = cloud_init(name, token)
    with tempfile.TemporaryDirectory(prefix="seed-", dir=state_dir) as temp:
        source = Path(temp)
        (source / "user-data").write_text(userdata)
        (source / "meta-data").write_text(metadata)
        os.chmod(source / "user-data", 0o600)
        os.chmod(source / "meta-data", 0o600)
        run([
            str(cfg["xorriso"]), "-as", "mkisofs", "-quiet", "-output", str(state_dir / "seed.iso"),
            "-volid", "cidata", "-joliet", "-rock", str(source / "user-data"), str(source / "meta-data"),
        ], timeout=30)
    os.chmod(state_dir / "seed.iso", 0o600)


def start_vm(name: str, token: str, cfg: dict[str, object]) -> None:
    if not NAME_RE.fullmatch(name):
        raise RuntimeError("invalid runner name")
    if not TOKEN_RE.fullmatch(token):
        raise RuntimeError("invalid registration token format")
    validate_runtime(cfg)
    root = Path(cfg["state"])
    recover(cfg)
    if active_names(cfg):
        raise RuntimeError("KVM concurrency limit of one is already occupied")
    state_dir = root / name
    state_dir.mkdir(mode=0o700)
    supervisor_pid = 0
    try:
        golden_info = json.loads(run([str(cfg["qemu_img"]), "info", "--output=json", str(cfg["golden"])], capture=True).stdout)
        golden_format = golden_info.get("format")
        if golden_format not in {"qcow2", "raw"}:
            raise RuntimeError("unsupported golden image format")
        run([
            str(cfg["qemu_img"]), "create", "-f", "qcow2", "-F", golden_format,
            "-b", str(cfg["golden"]), "-o", "preallocation=metadata,extended_l2=on",
            str(state_dir / "root.qcow2"), f"{cfg['overlay_gib']}G",
        ], timeout=60)
        overlay_info = json.loads(run([str(cfg["qemu_img"]), "info", "--output=json", str(state_dir / "root.qcow2")], capture=True).stdout)
        if int(overlay_info.get("virtual-size", 0)) > int(cfg["overlay_gib"]) * (1024**3):
            raise RuntimeError("qcow2 virtual disk exceeds configured limit")
        create_seed(state_dir, name, token, cfg)
        del token
        metadata = {"name": name, "created_at": int(time.time()), "supervisor_pid": 0, "qemu_pid": 0}
        command = [sys.executable, str(Path(__file__).resolve()), "supervise", "--name", name]
        child_env = {"PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")}
        child_env.update({key: os.environ[key] for key in QEMU_ENV_KEYS if key in os.environ})
        with (state_dir / "supervisor.log").open("ab", buffering=0) as output:
            supervisor_pid = os.posix_spawn(
                sys.executable,
                command,
                child_env,
                file_actions=_supervisor_file_actions(output.fileno()),
                setsid=True,
            )
        metadata["supervisor_pid"] = supervisor_pid
        _write_state(state_dir / "state.json", metadata)
        print(f"started KVM runner {name} supervisor_pid={supervisor_pid}", flush=True)
    except BaseException:
        if supervisor_pid > 1:
            try:
                os.killpg(supervisor_pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and _pid_alive(supervisor_pid, str(Path(__file__).resolve())):
                    time.sleep(0.1)
            except OSError:
                pass
            if _pid_alive(supervisor_pid, str(Path(__file__).resolve())):
                try:
                    os.killpg(supervisor_pid, signal.SIGKILL)
                except OSError:
                    pass
        preserve_diagnostics(state_dir, "start_failure", None)
        cleanup_state(state_dir)
        raise


def supervise(name: str, cfg: dict[str, object]) -> int:
    state_dir = Path(cfg["state"]) / name
    qemu: subprocess.Popen | None = None
    reason = "supervisor_exit"
    return_code: int | None = None

    class SupervisorStop(Exception):
        def __init__(self, signum: int):
            self.signum = signum

    def handle_stop(signum: int, _frame: object) -> None:
        raise SupervisorStop(signum)

    previous_sigterm = signal.signal(signal.SIGTERM, handle_stop)
    try:
        if not state_dir.is_dir():
            reason = "state_missing"
            return 1
        metadata_path = state_dir / "state.json"
        deadline = time.monotonic() + 10
        metadata: dict[str, object] = {}
        while time.monotonic() < deadline:
            try:
                metadata = json.loads(metadata_path.read_text())
                if int(metadata.get("supervisor_pid", 0)) == os.getpid():
                    break
            except (OSError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        else:
            reason = "metadata_timeout"
            return 1
        qemu_log = (state_dir / "qemu.log").open("ab", buffering=0)
        try:
            qemu = subprocess.Popen(
                qemu_command(name, cfg, state_dir), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=qemu_log, close_fds=True,
            )
            metadata["qemu_pid"] = qemu.pid
            _write_state(metadata_path, metadata)
            try:
                return_code = qemu.wait(timeout=int(cfg["lifetime"]))
                reason = "qemu_exit"
                print(f"QEMU exited name={name} exit={return_code}", flush=True)
                return return_code
            except subprocess.TimeoutExpired:
                reason = "lifetime_limit"
                print(f"QEMU lifetime limit reached name={name}", flush=True)
                qemu.terminate()
                try:
                    qemu.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    qemu.kill()
                    qemu.wait(timeout=10)
                return_code = 124
                return return_code
        finally:
            qemu_log.close()
    except SupervisorStop as stop:
        reason = f"supervisor_signal_{stop.signum}"
        return_code = 128 + stop.signum
        return return_code
    except BaseException as error:
        reason = f"supervisor_exception_{type(error).__name__}"
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if qemu is not None and qemu.poll() is None:
            qemu.terminate()
            try:
                qemu.wait(timeout=10)
            except subprocess.TimeoutExpired:
                qemu.kill()
                qemu.wait(timeout=10)
        preserve_diagnostics(state_dir, reason, return_code)
        cleanup_state(state_dir)


def stop_vm(name: str, cfg: dict[str, object]) -> None:
    if not NAME_RE.fullmatch(name):
        raise RuntimeError("invalid runner name")
    state_dir = Path(cfg["state"]) / name
    if not state_dir.exists():
        return
    try:
        metadata = json.loads((state_dir / "state.json").read_text())
        pid = int(metadata.get("supervisor_pid", 0))
    except (OSError, json.JSONDecodeError, ValueError):
        cleanup_state(state_dir)
        return
    if not _pid_alive(pid, f"{Path(__file__).resolve()} supervise --name {name}"):
        recover(cfg)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and state_dir.exists():
        if not _pid_alive(pid, f"{Path(__file__).resolve()} supervise --name {name}"):
            break
        time.sleep(0.1)
    if state_dir.exists():
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        cleanup_state(state_dir)


def proxy(cfg: dict[str, object]) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from importlib.machinery import SourceFileLoader

    module = SourceFileLoader("kvm_egress_proxy", str(Path(__file__).with_name("kvm-egress-proxy.py"))).load_module()
    with module.LocalOnlyServer(("127.0.0.1", int(cfg["proxy_port"])), module.ProxyHandler) as server:
        server.serve_forever(poll_interval=0.25)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("verify", "recover", "list", "start", "stop", "supervise", "proxy"))
    parser.add_argument("--name")
    args = parser.parse_args()
    cfg = config()
    try:
        if args.command == "verify":
            verify_image(cfg)
            print("KVM runtime and golden image verified")
        elif args.command == "recover":
            recover(cfg)
        elif args.command == "list":
            print("\n".join(active_names(cfg)))
        elif args.command == "start":
            if not args.name:
                raise RuntimeError("--name is required")
            token = sys.stdin.read(2048).strip()
            start_vm(args.name, token, cfg)
        elif args.command == "stop":
            if not args.name:
                raise RuntimeError("--name is required")
            stop_vm(args.name, cfg)
        elif args.command == "supervise":
            if not args.name:
                raise RuntimeError("--name is required")
            return supervise(args.name, cfg)
        elif args.command == "proxy":
            proxy(cfg)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(f"KVM backend error: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
