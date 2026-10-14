#!/usr/bin/env python3
"""Dedicated, one-VM N150 GitHub Actions KVM scheduler.

No Docker socket, Pi/LLM hooks, model slot allocation or general runner code.
All stateful admission happens under the existing cross-pool advisory lock.
"""
from __future__ import annotations

import fcntl
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from threading import Event

from admission import Admission, AdmissionPolicy
from github_api import GitHubClient, GitHubError
from host_probe import HostProbe


RUNNER_PREFIX = "n150-rc-eph-"
NAME_RE = re.compile(r"^n150-rc-eph-[A-Za-z0-9-]{1,80}$")
BACKEND = Path(os.environ.get("KVM_BACKEND_SCRIPT", "/usr/local/bin/kvm-backend.py"))
STATE = Path(os.environ.get("KVM_STATE_DIR", "/kvm-state"))
SCHEDULER = Path(os.environ.get("SCHEDULER_STATE_DIR", "/scheduler"))
STOP = Event()


def log(message: str) -> None:
    print(time.strftime("[manager] %Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, flush=True)


class BackendError(Exception):
    def __init__(self, code: str):
        self.code = code


class Backend:
    def __init__(self, script: Path = BACKEND):
        self.script = script

    def command(self, action: str, name: str | None = None, token: str | None = None, timeout: int = 60) -> str:
        argv = [sys.executable, str(self.script), action]
        if name:
            if not NAME_RE.fullmatch(name):
                raise BackendError("backend_invalid_name")
            argv += ["--name", name]
        try:
            completed = subprocess.run(
                argv, input=token, capture_output=True, text=True, check=False,
                timeout=timeout, close_fds=True,
            )
        except subprocess.TimeoutExpired:
            raise BackendError(f"backend_{action}_timeout") from None
        except OSError:
            raise BackendError(f"backend_{action}_unavailable") from None
        if completed.returncode != 0:
            # Backend already redacts its own diagnostics. Do not log stdout,
            # stderr or exception strings (which can expose guest bootstrap).
            raise BackendError(f"backend_{action}_failed")
        return completed.stdout

    def active(self) -> list[str]:
        text = self.command("list")
        names = [v for v in text.splitlines() if v]
        if any(not NAME_RE.fullmatch(v) for v in names):
            raise BackendError("backend_list_invalid")
        return names

    def start(self, name: str, token: str) -> None:
        self.command("start", name=name, token=token)

    def stop(self, name: str) -> None:
        self.command("stop", name=name)

    def recover(self) -> None:
        self.command("recover")

    def start_proxy(self) -> subprocess.Popen:
        # Wait for the backend's post-bind READY signal, instead of making an
        # invalid nc -z connection that the deny-only proxy logs as a request.
        proc = subprocess.Popen(
            [sys.executable, str(self.script), "proxy"], stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL, stderr=None, text=True, bufsize=1,
            close_fds=True,
        )
        try:
            assert proc.stdout is not None
            ready, _, _ = select.select([proc.stdout], [], [], 10)
            if not ready or proc.stdout.readline().strip() != "KVM_PROXY_READY" or proc.poll() is not None:
                raise BackendError("proxy_start_failed")
            return proc
        except BaseException:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise


def safe_int(env: str, default: int, minimum: int = 1, maximum: int = 100000) -> int:
    raw = os.getenv(env, str(default))
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"{env} must be a bounded integer")
    number = int(raw)
    if not minimum <= number <= maximum:
        raise ValueError(f"{env} must be a bounded integer")
    return number


class Scheduler:
    def __init__(self, github: GitHubClient, backend: Backend, probe: HostProbe,
                 policy: AdmissionPolicy, poll_seconds: int = 30):
        self.github = github
        self.backend = backend
        self.probe = probe
        self.policy = policy
        self.poll_seconds = poll_seconds
        self.idle: dict[str, int] = {}
        self.failure_count = 0
        self.retry_after = 0.0
        self.last_denial: str | None = None
        self.last_denial_at = 0.0

    def report_denial(self, code: str) -> None:
        now = time.monotonic()
        if self.last_denial != code or now - self.last_denial_at >= 300:
            log(f"admission_denied reason={code}")
            self.last_denial_at = now
        self.last_denial = code

    def active(self) -> list[str]:
        names = self.backend.active()
        if len(names) > 1:
            raise BackendError("backend_concurrency_violation")
        return names

    def spawn(self) -> Admission:
        # Shared flock is deliberately from the legacy externally named Docker
        # volume. This does not take, recreate or rename other pools' volumes.
        with (SCHEDULER / "admission.lock").open("a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return Admission(False, "shared_lock_busy")
            try:
                active = self.active()
                try:
                    metrics = self.probe.sample()
                except (OSError, ValueError, OverflowError):
                    return Admission(False, "metrics_unavailable")
                decision = self.policy.check(len(active), metrics)
                if not decision.allowed:
                    return decision
                try:
                    token = self.github.registration_token()
                except GitHubError as error:
                    return Admission(False, error.args[0])
                name = RUNNER_PREFIX + uuid.uuid4().hex[:12]
                try:
                    self.backend.start(name, token)
                except BackendError as error:
                    return Admission(False, error.code)
                finally:
                    del token
                return Admission(True, "started")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def cleanup_stale(self, runners: list[dict], active: list[str]) -> None:
        names = set(active)
        for runner in runners:
            name = runner.get("name")
            if not isinstance(name, str) or not name.startswith(RUNNER_PREFIX):
                continue
            if name not in names and runner.get("status") == "offline" and runner.get("busy") is False:
                self.github.delete_runner(runner.get("id"))

    def retire_idle(self, queued: int, runners: list[dict], active: list[str]) -> None:
        self.idle = {name: count for name, count in self.idle.items() if name in active}
        if queued:
            self.idle.clear()
            return
        idle = {runner.get("name") for runner in runners
                if runner.get("status") == "online" and runner.get("busy") is False}
        for name in active:
            if name not in idle:
                self.idle.pop(name, None)
                continue
            self.idle[name] = self.idle.get(name, 0) + 1
            if self.idle[name] < 4:
                continue
            # Double-check a second GitHub snapshot before terminating a VM.
            refreshed = self.github.runners()
            if any(r.get("name") == name and r.get("status") == "online" and r.get("busy") is False
                   for r in refreshed) and self.github.queued_jobs() == 0:
                self.backend.stop(name)
                log("idle_runner_retired")
                self.idle.pop(name, None)

    def once(self) -> None:
        try:
            active = self.active()
            runners = self.github.runners()
            queued = self.github.queued_jobs()
            busy = sum(1 for r in runners if isinstance(r.get("name"), str)
                       and r["name"].startswith(RUNNER_PREFIX) and r.get("busy") is True)
            if not active:
                self.cleanup_stale(runners, active)
            self.retire_idle(queued, runners, active)
            # A pending VM boot counts as active; never start a second one.
            desired = min(1, queued + busy)
            log(f"queued={queued} busy={busy} active={len(active)} desired={desired} spawning={int(desired > len(active))}")
            if desired <= len(active) or time.monotonic() < self.retry_after:
                return
            decision = self.spawn()
            if decision.allowed:
                self.failure_count = 0
                self.retry_after = 0
                self.last_denial = None
                log("runner_started")
            else:
                self.report_denial(decision.reason)
                self.failure_count += 1
                self.retry_after = time.monotonic() + min(300, self.poll_seconds * (2 ** min(4, self.failure_count - 1)))
        except (GitHubError, BackendError) as error:
            code = error.args[0] if isinstance(error, GitHubError) else error.code
            self.report_denial(code)
            self.failure_count += 1
            self.retry_after = time.monotonic() + min(300, self.poll_seconds * (2 ** min(4, self.failure_count - 1)))
        except (OSError, ValueError, TypeError):
            self.report_denial("state_unavailable")
            self.retry_after = time.monotonic() + self.poll_seconds

    def run(self) -> None:
        while not STOP.is_set():
            self.once()
            STOP.wait(self.poll_seconds)


def main() -> int:
    try:
        if os.getenv("GITHUB_REPOSITORY", "YuriiSokolenko/recursive-coder") != "YuriiSokolenko/recursive-coder":
            raise ValueError("repository must be recursive-coder")
        if safe_int("MAX_RUNNERS", 1) != 1:
            raise ValueError("KVM pool supports exactly one VM")
        if safe_int("KVM_GUEST_CPUS", 2) != 2 or safe_int("KVM_GUEST_MEMORY_MB", 4096) != 4096:
            raise ValueError("KVM guest resources must match the validated image")
        if safe_int("KVM_MAX_LIFETIME_SECONDS", 1800) > 1800:
            raise ValueError("KVM_MAX_LIFETIME_SECONDS exceeds policy")
        policy = AdmissionPolicy(
            host_max_ephemeral=safe_int("HOST_MAX_EPHEMERAL", 12),
            legacy_reserved=safe_int("LEGACY_RESERVED_RUNNERS", 11, 0),
            cpu_headroom=safe_int("KVM_MIN_HOST_CPU_HEADROOM", 1),
            host_memory_reserve_mb=safe_int("HOST_MEMORY_RESERVE_MB", 3072),
            worker_memory_mb=safe_int("WORKER_MEMORY_MB", 5120),
            overlay_max_gib=safe_int("KVM_OVERLAY_MAX_GIB", 24, 1, 24),
        )
        if policy.host_max_ephemeral <= policy.legacy_reserved:
            raise ValueError("No host slots reserved for KVM")
        if not SCHEDULER.is_dir() or not STATE.is_dir():
            raise ValueError("existing scheduler and VM state volumes are required")
        token = os.environ.get("GH_ADMIN_TOKEN", "")
        github = GitHubClient(token, safe_int("GITHUB_HTTP_TIMEOUT_SECONDS", 15))
        backend = Backend()
        with (SCHEDULER / "kvm-manager.lock").open("a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("admission_denied reason=another_kvm_manager_active")
                return 1
            backend.recover()
            # The existing golden is always verified before the manager can
            # request a registration token or boot a guest.
            backend.command("verify", timeout=180)
            proxy = backend.start_proxy()
            def request_stop(_signum: int, _frame: object) -> None:
                STOP.set()
            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            try:
                log("started repo=YuriiSokolenko/recursive-coder max=1")
                Scheduler(github, backend, HostProbe(STATE), policy, safe_int("POLL_SECONDS", 30)).run()
            finally:
                # Stop ONLY this pool's guests and proxy; never touch Docker or
                # other runner managers, including their state/volumes.
                try:
                    for name in backend.active():
                        backend.stop(name)
                except BackendError:
                    log("shutdown_vm_cleanup_failed")
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait()
        return 0
    except (BackendError, ValueError, OSError) as error:
        code = error.code if isinstance(error, BackendError) else type(error).__name__
        log(f"startup_failed reason={code}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
