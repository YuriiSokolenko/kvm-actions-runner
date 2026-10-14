"""One scheduler for Pi, General, persistent Control and KVM workers.

The only scheduling authority takes the historical shared admission lock
for its entire process lifetime. Executor services never decide demand,
priorities, model reservations or global capacity.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import signal
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .admission import AdmissionError, HostMetrics, admit, host_metrics, model_capacity
from .backends import BackendError, Backends, Worker
from .config import Configuration, Pool, load_config, verify_credentials
from .github_api import GitHub, GitHubError, Runner


def report(event: str, **fields) -> None:
    """No user-supplied strings, secrets, command output, URLs or host paths."""
    payload = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "event": event}
    for key, value in fields.items():
        if isinstance(value, (int, float, bool)) or value is None:
            payload[key] = value
        elif isinstance(value, str) and value.replace("_", "").replace("-", "").isalnum() and len(value) <= 64:
            payload[key] = value
    print(json.dumps(payload, separators=(",", ":")), flush=True)


@dataclass(frozen=True, slots=True)
class Pending:
    pool: str
    name: str
    started_at: float


class Journal:
    """Atomic, size-bounded launch reservations survive scheduler restarts."""

    def __init__(self, directory: Path):
        self.path = directory / "pending.json"
        self.entries: dict[str, Pending] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or len(data) > 64:
                raise ValueError("bad length")
            for item in data:
                if (not isinstance(item, dict) or set(item) != {"pool", "name", "started_at"}
                        or not isinstance(item["pool"], str)
                        or not isinstance(item["name"], str)
                        or type(item["started_at"]) not in (int, float)):
                    raise ValueError("bad reservation")
                self.entries[item["name"]] = Pending(**item)
        except (ValueError, OSError, TypeError):
            raise RuntimeError("SCHEDULER_JOURNAL_CORRUPT") from None

    def persist(self) -> None:
        # Replace+fsync ensures a crash never leaves half-written JSON.
        data = [{"pool": p.pool, "name": p.name, "started_at": p.started_at}
                for p in self.entries.values()]
        fd, temp = tempfile.mkstemp(prefix=".pending-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def reserve(self, pool: Pool, name: str, now: float) -> None:
        self.entries[name] = Pending(pool.id, name, now)
        self.persist()

    def reconcile(self, active_names: set[str], now: float) -> None:
        before = len(self.entries)
        # Keep unknown launches reserved for a bounded grace period. A crash
        # after starting but before observing a Docker/KVM worker must not
        # immediately start a duplicate. Operators investigate repeated faults.
        self.entries = {name: p for name, p in self.entries.items()
                        if name not in active_names and 0 <= now - p.started_at < 180}
        if len(self.entries) != before:
            self.persist()


class Scheduler:
    def __init__(self, config: Configuration, *, backends: Backends | None = None,
                 apis: dict[str, GitHub] | None = None,
                 probe=host_metrics, clock=time.time, model=model_capacity):
        self.config = config
        self.backends = backends or Backends(config)
        self.apis = apis if apis is not None else {
            p.repository: GitHub(p.repository, os.environ[p.token_env])
            for p in config.pools
        }
        self.probe = probe
        self.clock = clock
        self.model = model
        self.config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.journal = Journal(self.config.state_dir)
        self.failures = defaultdict(int)
        self.retry_after = defaultdict(float)
        self.idle_streak = defaultdict(int)
        self.age = defaultdict(int)
        self.last_denials = {}
        self.last_summary = 0.0

    def _workers(self) -> list[Worker]:
        return [*self.backends.workers("docker"), *self.backends.workers("kvm")]

    def _runner_statuses(self) -> dict[str, dict[str, Runner]]:
        snapshot = {}
        for repository, api in self.apis.items():
            snapshot[repository] = {runner.name: runner for runner in api.runners()}
        return snapshot

    def _demand(self) -> dict[str, int]:
        demand = {}
        for repository, api in self.apis.items():
            pools = [pool for pool in self.config.pools if pool.repository == repository]
            demand.update(api.queued_jobs_by_pool(pools))
        return demand

    def _deny(self, pool: Pool, code: str, now: float) -> None:
        last = self.last_denials.get(pool.id)
        if last != code or now - self.last_summary >= 300:
            report("admission_denied", pool=pool.id, reason=code)
        self.last_denials[pool.id] = code

    def _start(self, pool: Pool, api: GitHub, now: float, *, persistent=False) -> bool:
        # Issue reservation before acquiring a potentially slow token or backend.
        # No accepted launch can disappear without leaving a durable identifier.
        name = pool.runner_name if persistent else pool.runner_prefix + "-" + secrets.token_hex(6)
        self.journal.reserve(pool, name, now)
        try:
            # Persistent Control uses repo-scoped administrative credentials for
            # its own reconciling entrypoint. Ephemeral jobs get short-lived
            # registration tokens, not the admin token.
            token = api.token if persistent else api.registration_token()
            self.backends.start(pool, name, token)
            self.failures[pool.id] = 0
            self.retry_after[pool.id] = 0
            report("backend_started", pool=pool.id, backend=pool.backend)
            return True
        except (BackendError, GitHubError) as err:
            self.failures[pool.id] = min(self.failures[pool.id] + 1, 6)
            delay = min(300, 5 * 2 ** (self.failures[pool.id] - 1))
            self.retry_after[pool.id] = now + delay
            report("backend_start_failed", pool=pool.id, reason=err.code, retry_seconds=delay)
            return False

    def _cleanup(self, workers: list[Worker], runners: dict[str, dict[str, Runner]],
                 demand: dict[str, int], now: float) -> None:
        active_names = {w.name for w in workers if w.running}
        any_names = {w.name for w in workers}
        pending_names = set(self.journal.entries)
        for pool in self.config.pools:
            if pool.mode == "persistent":
                continue
            prefix = pool.runner_prefix + "-"
            registered = runners[pool.repository]
            # Only delete a clearly offline, nonbusy, prefix-owned record
            # whose backend worker and pending launch are both absent.
            for name, reg in registered.items():
                if (name.startswith(prefix) and reg.status == "offline" and not reg.busy
                        and name not in any_names and name not in pending_names):
                    try:
                        self.apis[pool.repository].delete_runner(reg.id)
                        report("stale_registration_removed", pool=pool.id)
                    except GitHubError as err:
                        report("stale_cleanup_failed", pool=pool.id, reason=err.code)
            if demand.get(pool.id, 0):
                # An idle worker is useful to the queued job, don't retire it.
                for name in list(self.idle_streak):
                    if name.startswith(prefix):
                        self.idle_streak.pop(name, None)
                continue
            for worker in workers:
                if worker.pool != pool.id or not worker.running:
                    continue
                reg = registered.get(worker.name)
                if not reg or reg.status != "online" or reg.busy:
                    self.idle_streak.pop(worker.name, None)
                    continue
                self.idle_streak[worker.name] += 1
                if self.idle_streak[worker.name] < pool.idle_polls:
                    continue
                try:
                    # Revalidate immediately, since GitHub may assign a job
                    # between the original list and the stop request.
                    fresh = {r.name: r for r in self.apis[pool.repository].runners()}.get(worker.name)
                    if fresh is None or fresh.status != "online" or fresh.busy:
                        continue
                    self.backends.stop(pool, worker.name)
                    report("idle_retired", pool=pool.id, backend=pool.backend)
                    self.idle_streak.pop(worker.name, None)
                except (GitHubError, BackendError) as err:
                    report("retire_failed", pool=pool.id, reason=err.code)

    def poll(self) -> None:
        now = self.clock()
        # All reads are taken before mutations; a malformed or inaccessible
        # GitHub response or backend snapshot prevents *every* new launch.
        workers = self._workers()
        docker_probe = self.backends.probe("docker")
        kvm_probe = self.backends.probe("kvm")
        metrics = self.probe()
        runners = self._runner_statuses()
        demand = self._demand()
        active_names = {w.name for w in workers if w.running}
        self.journal.reconcile(active_names, now)
        self._cleanup(workers, runners, demand, now)
        counts = defaultdict(int)
        for worker in workers:
            if worker.running:
                counts[worker.pool] += 1
        for pending in self.journal.entries.values():
            if pending.pool not in {p.id for p in self.config.pools}:
                raise RuntimeError("SCHEDULER_JOURNAL_POOL_INVALID")
            counts[pending.pool] += 1
        # Treat still-busy GitHub registrations without a visible backend
        # worker as occupied slots. A crashed/offline worker must not allow
        # another manager-controlled launch to overbook the same pool.
        known = {w.name for w in workers if w.running} | set(self.journal.entries)
        for pool in self.config.pools:
            if pool.mode != "ephemeral":
                continue
            counts[pool.id] += sum(
                1 for reg in runners[pool.repository].values()
                if reg.busy and reg.name.startswith(pool.runner_prefix + "-")
                and reg.name not in known
            )
        pools = self.config.pools
        control = next(p for p in pools if p.mode == "persistent")
        if counts[control.id] < 1 and now >= self.retry_after[control.id]:
            reason = admit(control, self.config.host, metrics, counts=counts, pools=pools,
                           model_available=0)
            if reason == "ALLOW":
                if self._start(control, self.apis[control.repository], now, persistent=True):
                    counts[control.id] += 1
                    metrics = replace(metrics, mem_available_mb=metrics.mem_available_mb - control.memory_mb)
            else:
                self._deny(control, reason, now)
        if counts[control.id] < 1:
            # Reserve Control capacity first; no opportunistic ephemeral starts.
            report("control_unavailable", reason="CONTROL_NOT_READY")
            return
        pi_active = sum(counts[p.id] for p in pools if p.model_gated)
        try:
            model_slots = self.model(self.config.host, pi_active)
        except AdmissionError as err:
            model_slots = 0
            for p in pools:
                if p.model_gated:
                    self._deny(p, err.code, now)
        jobs = {}
        for p in pools:
            if p.mode != "ephemeral":
                continue
            registered_busy = sum(1 for r in runners[p.repository].values()
                                  if r.name.startswith(p.runner_prefix + "-") and r.busy)
            jobs[p.id] = max(0, min(p.max_runners, demand.get(p.id, 0) + registered_busy) - counts[p.id])
            if jobs[p.id] > 0:
                self.age[p.id] = min(1000, self.age[p.id] + 1)
        if now - self.last_summary >= 60:
            for p in pools:
                report("pool_snapshot", pool=p.id, active=counts[p.id],
                       queued=demand.get(p.id, 0), wanted=jobs.get(p.id, 0))
            report("host_snapshot", cpu_count=metrics.cpu_count, cpu_load_1m=round(metrics.load_1m, 2),
                   mem_available_mb=metrics.mem_available_mb, model_capacity=model_slots)
            self.last_summary = now
        # Age-based weighted priority prevents one busy pool from excluding
        # another forever if admission eventually becomes possible.
        while any(count > 0 for count in jobs.values()):
            candidates = [p for p in pools if jobs.get(p.id, 0) > 0 and now >= self.retry_after[p.id]]
            if not candidates:
                break
            candidates.sort(key=lambda p: (-p.priority - min(1000, self.age[p.id]) * p.weight, p.id))
            candidate = None
            for p in candidates:
                reason = admit(p, self.config.host, metrics, counts=counts, pools=pools,
                               model_available=model_slots,
                               kvm_disk_free_mb=kvm_probe.get("disk_free_mb"))
                if reason == "ALLOW":
                    candidate = p
                    break
                self._deny(p, reason, now)
                jobs[p.id] = 0
            if candidate is None:
                break
            pool = candidate
            if self._start(pool, self.apis[pool.repository], now):
                counts[pool.id] += 1
                metrics = replace(metrics, mem_available_mb=metrics.mem_available_mb - pool.memory_mb)
                self.age[pool.id] = 0
                if pool.model_gated:
                    model_slots = max(0, model_slots - 1)
                jobs[pool.id] -= 1
            else:
                jobs[pool.id] = 0

    def run(self, *, once: bool = False, sleep=time.sleep) -> None:
        lock = self.config.state_dir / "admission.lock"
        with lock.open("a+") as fp:
            try:
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("SCHEDULER_ALREADY_RUNNING") from None
            report("scheduler_started", pools=len(self.config.pools))
            while True:
                try:
                    self.poll()
                except (BackendError, GitHubError, AdmissionError, OSError, RuntimeError) as err:
                    reason = getattr(err, "code", "SCHEDULER_POLL_ERROR")
                    report("poll_deferred", reason=reason)
                if once:
                    return
                sleep(self.config.poll_seconds)


def cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="One N150 Runner Manager")
    parser.add_argument("command", choices=("plan", "verify", "run", "once", "status"))
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "plan":
        for p in config.pools:
            report("pool_plan", pool=p.id, backend=p.backend, limit=p.max_runners, priority=p.priority)
        return
    if args.command == "status":
        journal = Journal(config.state_dir)
        report("local_status", pending=len(journal.entries))
        for backend in ("docker", "kvm"):
            workers = Backends(config).workers(backend)
            report("backend_status", backend=backend, workers=len(workers),
                   running=sum(w.running for w in workers))
        return
    verify_credentials(config)
    if args.command == "verify":
        for backend in ("docker", "kvm"):
            Backends(config).probe(backend)
        report("verify_ok")
        return
    scheduler = Scheduler(config)
    scheduler.run(once=args.command == "once")


if __name__ == "__main__":
    cli()
