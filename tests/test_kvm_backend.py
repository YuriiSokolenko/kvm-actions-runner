import contextlib
import fcntl
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "infra/github-runner-autoscaler/kvm-backend.py"
SPEC = importlib.util.spec_from_file_location("kvm_backend", MODULE_PATH)
assert SPEC and SPEC.loader
kvm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kvm)


class KvmBackendTests(unittest.TestCase):
    def test_cloud_init_delivers_only_short_lived_token_and_removes_bootstrap(self):
        token = "ghs_" + "x" * 36
        userdata, metadata = kvm.cloud_init("n150-rc-eph-test", token)
        self.assertNotIn(token, userdata)
        self.assertIn(kvm.base64.b64encode(token.encode()).decode(), userdata)
        self.assertIn("permissions: '0600'", userdata)
        self.assertIn("shred -u /var/lib/runner-bootstrap/token.b64", userdata)
        self.assertIn('trap \'code=$?; shred -u /var/lib/runner-bootstrap/token.b64', userdata)
        self.assertIn('systemctl poweroff --force', userdata)
        self.assertIn("rm -f /run/cloud-init/instance-data-sensitive.json", userdata)
        self.assertIn("usermod --lock --shell /usr/sbin/nologin ubuntu", userdata)
        self.assertIn("kernel.apparmor_restrict_unprivileged_userns=0", userdata)
        self.assertIn("chmod 0755 /usr/bin/bwrap", userdata)
        self.assertNotIn("chmod 4755 /usr/bin/bwrap", userdata)
        self.assertIn("nftables", userdata)
        self.assertIn("policy drop", userdata)
        self.assertIn("ip daddr 10.0.2.100 tcp dport 3128 accept", userdata)
        self.assertIn("udp sport 68 udp dport 67 accept", userdata)
        self.assertIn("--ephemeral --disableupdate --unattended", userdata)
        self.assertIn('ACTIONS_RUNNER_INPUT_TOKEN="$token" runuser -u runner --preserve-environment', userdata)
        self.assertNotIn('--token "$token"', userdata)
        self.assertIn("systemd-run --quiet --unit=actions-runner-delayed-start --on-active=10s", userdata)
        self.assertIn("After=network-online.target cloud-final.service", userdata)
        self.assertIn("ExecStopPost=+/usr/local/sbin/runner-shutdown-diagnostics", userdata)
        self.assertIn("ExecStopPost=+/usr/bin/systemctl --no-block poweroff", userdata)
        self.assertIn("runner-shutdown-diagnostics-v1", userdata)
        self.assertIn("systemd-oomd.service", userdata)
        self.assertIn("runner-shutdown-diagnostics-end", userdata)
        self.assertIn("kvm_bootstrap_stage=", userdata)
        self.assertIn("stage apt_start", userdata)
        self.assertIn("stage runner_ready", userdata)
        self.assertIn("prepared_golden_verified", userdata)
        self.assertIn("prepared.manifest", userdata)
        self.assertIn("toolchain-v2.manifest", userdata)
        self.assertIn("baked_ci_toolchain_verified", userdata)
        # v2 tools originally inherited root's umask 077; runtime must allow
        # the ephemeral unprivileged worker to traverse their directories.
        self.assertIn("chmod -R a+rX /opt/recursive-coder-kvm/python", userdata)
        self.assertIn("      /opt/recursive-coder-kvm/gradle-9.8.1", userdata)
        self.assertIn("      /opt/recursive-coder-kvm/node-v22.21.1-linux-x64", userdata)
        self.assertIn("10-baked-ci-tools.conf", userdata)
        self.assertIn("GRADLE_USER_HOME=/opt/recursive-coder-kvm/gradle-home", userdata)
        self.assertIn("GRADLE_OPTS=-Dorg.gradle.workers.max=1 -Dorg.gradle.jvmargs=-Xmx1g", userdata)
        self.assertIn("node-v22.21.1-linux-x64/bin", userdata)
        self.assertIn("gradle-9.8.1/bin", userdata)
        self.assertIn("python/bin", userdata)
        self.assertIn(f"schema=1 runner={kvm.RUNNER_VERSION} sha256={kvm.RUNNER_SHA256}", userdata)
        self.assertIn("sha256sum -c -", userdata)
        self.assertIn('if [ -n "$prepared_runner" ]; then', userdata)
        self.assertIn('tar -xzf "$prepared_runner"', userdata)
        self.assertIn("apt-get update", userdata)
        self.assertIn("--ephemeral --disableupdate", userdata)
        self.assertNotIn("prepared_runner=/home/runner", userdata)
        self.assertIn('oifname "lo" accept', userdata)
        self.assertIn(kvm.REPOSITORY, userdata)
        self.assertEqual("n150-rc-eph-test", json.loads(metadata)["instance-id"])

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is available on Linux only")
    def test_generated_runner_service_unit_passes_systemd_verification(self):
        userdata, _ = kvm.cloud_init("n150-rc-eph-unitcheck", "ghs_" + "z" * 36)
        lines = userdata.splitlines()
        start = next(i for i, line in enumerate(lines) if "cat >/etc/systemd/system/actions-runner.service" in line) + 1
        end = next(i for i in range(start, len(lines)) if lines[i].strip() == "UNIT")
        unit = "\n".join(line[6:] for line in lines[start:end]) + "\n"
        # The runner executable exists only in the guest image; /bin/true lets
        # systemd-analyze validate every unit directive without touching host units.
        unit = unit.replace("/home/runner/actions-runner/run.sh", "/bin/true")
        unit = unit.replace("/usr/local/sbin/runner-shutdown-diagnostics", "/bin/true")
        with tempfile.TemporaryDirectory(prefix="kvm-systemd-unit-test-") as temporary:
            unit_path = Path(temporary) / "actions-runner.service"
            unit_path.write_text(unit)
            result = subprocess.run(
                ["systemd-analyze", "verify", str(unit_path)],
                check=False, text=True, capture_output=True, timeout=15,
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_qemu_command_has_bounded_kvm_guest_and_no_host_shares_or_forwarding(self):
        cfg = {
            "qemu": "/usr/bin/qemu-system-x86_64", "cpus": 2, "memory_mb": 4096,
            "bridge": "/usr/local/bin/kvm-proxy-bridge",
        }
        command = kvm.qemu_command("n150-rc-eph-test", cfg, Path("/kvm-state/n150-rc-eph-test"))
        joined = " ".join(command)
        self.assertIn("q35,accel=kvm", joined)
        self.assertIn("-smp 2 -m 4096", joined)
        self.assertIn("restrict=on", joined)
        self.assertIn("guestfwd=tcp:10.0.2.100:3128", joined)
        self.assertNotIn("hostfwd", joined)
        self.assertNotIn("virtiofs", joined)
        self.assertNotIn("9p", joined)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("tap,", joined)

    def test_guest_resource_policy_rejects_values_above_host_limits(self):
        with tempfile.TemporaryDirectory(prefix="kvm-policy-test-") as temporary:
            root = Path(temporary)
            golden = root / "golden.img"
            golden.write_bytes(b"fixture")
            golden.chmod(0o444)
            state = root / "state"
            state.mkdir(mode=0o700)
            cfg = {
                "golden": golden, "golden_sha256": "0" * 64,
                "state": state, "qemu": "qemu", "qemu_img": "qemu-img",
                "xorriso": "xorriso", "cpus": 3, "memory_mb": 4096,
                "overlay_gib": 24, "lifetime": 1800,
            }
            with patch.object(kvm.shutil, "which", return_value="/usr/bin/fake"):
                with self.assertRaisesRegex(RuntimeError, "two-vCPU/four-GiB"):
                    kvm.validate_runtime(cfg, require_device=False)

    def test_supervisor_removes_overlay_after_guest_exit_and_timeout(self):
        with tempfile.TemporaryDirectory(prefix="kvm-supervisor-test-") as temporary:
            root = Path(temporary)
            state = root / "n150-rc-eph-test"
            state.mkdir(mode=0o700)
            fake_qemu = root / "fake-qemu"
            fake_qemu.write_text("#!/bin/sh\nexec /bin/true\n")
            fake_qemu.chmod(0o755)
            (state / "state.json").write_text(json.dumps({"supervisor_pid": os.getpid(), "qemu_pid": 0}))
            cfg = {
                "state": root, "qemu": str(fake_qemu), "bridge": "/tmp/kvm-bridge",
                "cpus": 2, "memory_mb": 4096,
                "lifetime": 2,
            }
            self.assertEqual(0, kvm.supervise(state.name, cfg))
            self.assertFalse(state.exists())
            self.assertEqual(1, len(list((root / "diagnostics").glob("*.json"))))

            state.mkdir(mode=0o700)
            fake_qemu.write_text("#!/bin/sh\nexec /bin/sleep 5\n")
            (state / "state.json").write_text(json.dumps({"supervisor_pid": os.getpid(), "qemu_pid": 0}))
            cfg["lifetime"] = 0.05
            self.assertEqual(124, kvm.supervise(state.name, cfg))
            self.assertFalse(state.exists())
            records = list((root / "diagnostics").glob("*.json"))
            self.assertEqual(2, len(records))
            self.assertIn("lifetime_limit", [json.loads(path.read_text())["reason"] for path in records])

    def test_diagnostics_are_sanitized_bounded_private_and_retained(self):
        with tempfile.TemporaryDirectory(prefix="kvm-diag-test-") as temporary:
            root = Path(temporary)
            state = root / "n150-rc-eph-diag"
            state.mkdir(mode=0o700)
            (state / "state.json").write_text(json.dumps({
                "name": state.name, "created_at": 123, "registration_token": "ghs_" + "x" * 30,
            }))
            (state / "serial.log").write_text(
                "z" * (kvm.DIAGNOSTIC_TAIL_BYTES * 3) + "\n"
                "kvm_bootstrap_stage=apt_start uptime=2.0\n"
                "=== runner-shutdown-diagnostics-v1 ===\n"
                "service_result=signal exit_code=killed exit_status=9\n"
                "/home/runner/_work/project/private-file\n"
                "kernel: Out of memory: Killed process 42 (java)\n"
                "runner failed github_pat_" + "y" * 36 + "\n"
                "authorization: Bearer abcdefghijklmnop\n"
                "=== runner-shutdown-diagnostics-end ===\n"
                + "systemd: Reached shutdown.target\n" * 1000
            )
            cgroup = root / "cgroup"
            cgroup.mkdir()
            (cgroup / "memory.events").write_text("oom 1\noom_kill 1\n")
            (cgroup / "memory.current").write_text("4096\n")
            (cgroup / "memory.peak").write_text("8192\n")
            (cgroup / "memory.swap.current").write_text("0\n")
            output = kvm.preserve_diagnostics(
                state, "guest_oom", 137, cgroup_root=cgroup,
            )
            self.assertIsNotNone(output)
            self.assertFalse((state / "root.qcow2").exists())
            payload = output.read_text()
            self.assertIn("Out of memory", payload)
            incident = json.loads(payload)
            self.assertTrue(incident["guest_evidence"]["shutdown_marker_found"])
            self.assertIn("exit_status=9", incident["guest_evidence"]["shutdown_details"])
            self.assertTrue(any(
                stage.startswith("kvm_bootstrap_stage=apt_start ")
                for stage in incident["guest_evidence"]["bootstrap_stages"]
            ))
            self.assertNotIn("service_result=", incident["logs"]["serial.log"],
                             "A long systemd shutdown should displace the old serial tail")
            self.assertIn("oom_kill 1", payload)
            self.assertIn("[REDACTED_GITHUB_TOKEN]", payload)
            self.assertNotIn("github_pat_", payload)
            self.assertNotIn("ghs_", payload)
            self.assertNotIn("abcdefghijklmnop", payload)
            self.assertNotIn("/home/runner", payload)
            self.assertIn("[RUNNER_PATH]", payload)
            self.assertLessEqual(output.stat().st_size, kvm.DIAGNOSTIC_MAX_BYTES)
            self.assertEqual(0o600, output.stat().st_mode & 0o777)
            self.assertEqual(0o700, output.parent.stat().st_mode & 0o777)
            for index in range(kvm.DIAGNOSTIC_RETENTION + 2):
                (output.parent / f"n150-rc-eph-old-{index}.json").write_text("{}")
            kvm.preserve_diagnostics(state, "repeat", 0, cgroup_root=cgroup)
            self.assertLessEqual(len(list(output.parent.glob("n150-rc-eph-*.json"))),
                                 kvm.DIAGNOSTIC_RETENTION)

    def test_supervisor_file_actions_close_inherited_admission_lock_fd(self):
        with tempfile.TemporaryFile() as lock:
            fd = lock.fileno()
            actions = kvm._supervisor_file_actions(fd + 1)
            self.assertIn((os.POSIX_SPAWN_CLOSE, fd), actions)

    def test_supervisor_preserves_diagnostics_on_qemu_failure(self):
        with tempfile.TemporaryDirectory(prefix="kvm-supervisor-fail-") as temporary:
            root = Path(temporary)
            state = root / "n150-rc-eph-fail"
            state.mkdir(mode=0o700)
            fake_qemu = root / "fake-qemu"
            fake_qemu.write_text("#!/bin/sh\necho 'QEMU fixture failure' >&2\nexit 7\n")
            fake_qemu.chmod(0o755)
            (state / "state.json").write_text(json.dumps({
                "name": state.name, "created_at": 123, "supervisor_pid": os.getpid(), "qemu_pid": 0,
            }))
            cfg = {"state": root, "qemu": str(fake_qemu), "bridge": "/tmp/kvm-bridge",
                   "cpus": 2, "memory_mb": 512, "lifetime": 2}
            self.assertEqual(7, kvm.supervise(state.name, cfg))
            self.assertFalse(state.exists())
            diagnostics = list((root / "diagnostics").glob("*.json"))
            self.assertEqual(1, len(diagnostics))
            record = json.loads(diagnostics[0].read_text())
            self.assertEqual("qemu_exit", record["reason"])
            self.assertEqual(7, record["qemu_exit_status"])
            self.assertIn("QEMU fixture failure", record["logs"]["qemu.log"])

    def test_recover_removes_abandoned_vm_state(self):
        with tempfile.TemporaryDirectory(prefix="kvm-recover-test-") as temporary:
            root = Path(temporary)
            state = root / "n150-rc-eph-abandoned"
            state.mkdir(mode=0o700)
            (state / "root.qcow2").write_bytes(b"discard")
            (state / "state.json").write_text(json.dumps({"supervisor_pid": 2147483647, "qemu_pid": 0}))
            kvm.recover({"state": root})
            self.assertFalse(state.exists())
            records = list((root / "diagnostics").glob("*.json"))
            self.assertEqual(1, len(records))
            self.assertEqual("recovery_orphaned_supervisor",
                             json.loads(records[0].read_text())["reason"])

    def test_start_supervise_and_stop_clean_the_overlay_without_logging_token(self):
        with tempfile.TemporaryDirectory(prefix="kvm-lifecycle-test-") as temporary:
            root = Path(temporary)
            state_root = root / "state"
            state_root.mkdir(mode=0o700)
            golden = root / "golden.img"
            golden.write_bytes(b"golden")
            golden.chmod(0o444)
            fake_qemu = root / "fake-qemu"
            env_capture = root / "qemu-env"
            fake_qemu.write_text(f"#!/bin/sh\nenv > {env_capture}\nexec /bin/sleep 30\n")
            fake_qemu.chmod(0o755)
            env = {
                "KVM_GOLDEN_IMAGE": str(golden),
                "KVM_GOLDEN_SHA256": "0" * 64,
                "KVM_STATE_DIR": str(state_root),
                "KVM_QEMU_BINARY": str(fake_qemu),
                "KVM_GUEST_CPUS": "2",
                "KVM_GUEST_MEMORY_MB": "4096",
                "KVM_OVERLAY_MAX_GIB": "24",
                "KVM_MAX_LIFETIME_SECONDS": "60",
                "KVM_XORRISO_BINARY": "/usr/bin/xorriso",
                "KVM_QEMU_IMG_BINARY": "/usr/bin/qemu-img",
                "GH_ADMIN_TOKEN": "manager-only-test-token",
            }
            info = subprocess.CompletedProcess([], 0, json.dumps({"format": "raw", "virtual-size": 4 * 1024**3}), "")

            def fake_run(command, **_kwargs):
                if command[1:3] == ["info", "--output=json"]:
                    return info
                if command[1:2] == ["create"]:
                    self.assertIn("preallocation=metadata,extended_l2=on", command)
                    Path(command[-2]).write_bytes(b"overlay")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[1:3] == ["-as", "mkisofs"]:
                    Path(command[5]).write_bytes(b"seed")
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(command)

            name = "n150-rc-eph-test"
            state = state_root / name
            output = io.StringIO()
            supervisor_pid = None
            admission_path = root / "admission.lock"
            admission = admission_path.open("w")
            fcntl.flock(admission.fileno(), fcntl.LOCK_EX)
            with patch.dict(os.environ, env), patch.object(kvm, "validate_runtime"), \
                patch.object(kvm, "recover"), patch.object(kvm, "active_names", return_value=[]), \
                patch.object(kvm, "run", side_effect=fake_run), contextlib.redirect_stdout(output):
                kvm.start_vm(name, "ghs_" + "z" * 36, kvm.config())
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if state.exists() and (state / "state.json").exists():
                        metadata = json.loads((state / "state.json").read_text())
                        supervisor_pid = metadata.get("supervisor_pid")
                        if metadata.get("qemu_pid"):
                            if env_capture.exists():
                                break
                    time.sleep(0.05)
                else:
                    self.fail("supervisor did not start the disposable QEMU process")
                fcntl.flock(admission.fileno(), fcntl.LOCK_UN)
                admission.close()
                lock_probe = subprocess.run(
                    ["flock", "-n", str(admission_path), "-c", "true"],
                    check=False, text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(0, lock_probe.returncode,
                                 "detached supervisor inherited the autoscaler admission lock")
                kvm.stop_vm(name, kvm.config())
                deadline = time.monotonic() + 5
                while state.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if supervisor_pid:
                    try:
                        os.waitpid(supervisor_pid, 0)
                    except ChildProcessError:
                        pass
            self.assertFalse(state.exists(), "overlay and bootstrap media must be removed")
            stop_records = list((state_root / "diagnostics").glob("*.json"))
            self.assertEqual(1, len(stop_records))
            self.assertIn(json.loads(stop_records[0].read_text())["reason"],
                          ("supervisor_signal_15", "forced_stop_cleanup"))
            self.assertNotIn("ghs_", output.getvalue())
            self.assertNotIn("GH_ADMIN_TOKEN", env_capture.read_text())


if __name__ == "__main__":
    unittest.main()
