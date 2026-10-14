import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from n150_runner.admission import HostMetrics
from n150_runner.backends import Backends, BackendError
from n150_runner.config import load_config
from n150_runner.github_api import Runner
from n150_runner.scheduler import Scheduler, Journal


ORIGINAL = load_config("config/pools.json")


class FakeExecutors:
    def __init__(self):
        self.workers = []
        self.started = []
        self.failed_pool = None
        self.hide_started = False

    def __call__(self, backend, request):
        cmd = request["command"]
        if cmd == "probe":
            return {"ok": True, "disk_free_mb": 30000, "healthy": True}
        if cmd == "list":
            return {"ok": True, "workers": [
                {key: worker[key] for key in ("pool", "name", "running")}
                for worker in self.workers if worker["backend"] == backend
            ]}
        if cmd == "start":
            self.started.append(request["pool"])
            if self.failed_pool == request["pool"]:
                raise BackendError("EXECUTOR_COMMAND_FAILED")
            if not self.hide_started:
                self.workers.append({"name": request["name"], "pool": request["pool"],
                                     "backend": backend, "running": True})
            return {"ok": True}
        if cmd == "stop":
            self.workers = [w for w in self.workers if w["name"] != request["name"]]
            return {"ok": True}
        raise AssertionError(cmd)


class FakeAPI:
    def __init__(self, repository, backend, pools):
        self.repository = repository
        self.backend = backend
        self.pools = pools
        self.token = "github_pat_admin_test_token_1234567890123456"
        self.demands = {}
        self.removed = []
        self.extra_runners = []

    def queued_jobs_by_pool(self, pools):
        return {p.id: self.demands.get(p.id, 0) for p in pools if p.mode == "ephemeral"}

    def runners(self):
        results = []
        for index, worker in enumerate(self.backend.workers, 1):
            pool = self.pools[worker["pool"]]
            if pool.repository == self.repository:
                results.append(Runner(index, worker["name"], False, "online"))
        return results + self.extra_runners

    def registration_token(self):
        return "test_ephemeral_token_1234567890123456789"

    def delete_runner(self, ident):
        self.removed.append(ident)


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = replace(ORIGINAL, state_dir=root / "state", socket_dir=root / "sockets")
        self.executor = FakeExecutors()
        self.clients = {repo: FakeAPI(repo, self.executor, {p.id: p for p in self.config.pools})
                        for repo in {p.repository for p in self.config.pools}}
        self.now = 1000
        self.load = 0.5
        self.scheduler = Scheduler(
            self.config, backends=Backends(self.config, transport=self.executor),
            apis=self.clients,
            probe=lambda: HostMetrics(4, self.load, 16000),
            clock=lambda: self.now,
            model=lambda host, active: max(0, 8 - active))

    def demand(self, pool, number):
        p = next(p for p in self.config.pools if p.id == pool)
        self.clients[p.repository].demands[pool] = number

    def tick(self, seconds=10):
        self.scheduler.poll()
        self.now += seconds

    def test_control_prioritized_and_only_one_kvm(self):
        self.demand("pi", 2)
        self.demand("general", 2)
        self.demand("recursive-coder-kvm", 2)
        self.tick()
        self.assertEqual("control", self.executor.started[0])
        self.assertLessEqual(self.executor.started.count("recursive-coder-kvm"), 1)
        self.assertLessEqual(len([w for w in self.executor.workers if w["pool"] != "control"]), 6)
        self.assertGreater(len(self.executor.started), 1)

    def test_kvm_p0_pressure_recovers_without_restart(self):
        self.demand("recursive-coder-kvm", 2)
        self.load = 2.0
        self.tick()
        self.assertNotIn("recursive-coder-kvm", self.executor.started)
        self.load = 0.5
        self.tick()
        self.assertEqual(1, self.executor.started.count("recursive-coder-kvm"))

    def test_failed_launch_backoff_and_durable_reservation(self):
        self.demand("general", 1)
        self.executor.failed_pool = "general"
        self.tick()
        self.assertEqual(1, self.executor.started.count("general"))
        self.tick()
        self.assertEqual(1, self.executor.started.count("general"))
        self.assertTrue(any(item.pool == "general" for item in self.scheduler.journal.entries.values()))
        # Reservation never disappears just because the executor briefly
        # returned an error. On expiry, the next poll may make progress.
        self.executor.failed_pool = None
        self.now += 181
        self.tick()
        self.assertEqual(2, self.executor.started.count("general"))

    def test_backend_started_then_invisible_no_double_spawn(self):
        self.demand("pi", 1)
        self.executor.hide_started = True
        self.tick()
        self.assertEqual(["control", "pi"], self.executor.started)
        # Both launches are durably reserved even before backend visibility.
        # A repeated poll cannot start either a second Control or Pi worker.
        self.tick()
        self.assertEqual(1, self.executor.started.count("control"))
        self.assertEqual(1, self.executor.started.count("pi"))
        self.assertEqual(2, len(self.scheduler.journal.entries))

    def test_unknown_busy_registration_reserves_a_global_slot(self):
        self.demand("general", 3)
        api = self.clients["YuriiSokolenko/social-mcp"]
        api.extra_runners = [Runner(999, "n150-gen-eph-remote-busy", True, "online")]
        self.tick()
        # Three desired minus one busy-but-unseen registration: only two
        # launches can be admitted even if Docker reports no such worker.
        self.assertEqual(2, self.executor.started.count("general"))
        self.tick()
        self.assertEqual(2, self.executor.started.count("general"))

    def test_corrupt_journal_fails_closed(self):
        path = self.config.state_dir / "pending.json"
        path.write_text("not-json")
        with self.assertRaises(RuntimeError):
            Journal(self.config.state_dir)


if __name__ == "__main__":
    unittest.main()
