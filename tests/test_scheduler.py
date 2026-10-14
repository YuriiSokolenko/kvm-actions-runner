"""Scheduler tests exercise actual admission flock and fake GitHub/QEMU."""
import contextlib
import fcntl
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manager
from admission import AdmissionPolicy, HostMetrics
from github_api import GitHubError


class FakeGitHub:
    def __init__(self):
        self.queue = 2
        self.runner_states = []
        self.token = "ghs_" + "X" * 40
        self.fail_token = False
        self.deleted = []

    def queued_jobs(self):
        return self.queue

    def runners(self):
        return self.runner_states

    def registration_token(self):
        if self.fail_token:
            raise GitHubError("github_http_403")
        return self.token

    def delete_runner(self, runner_id):
        self.deleted.append(runner_id)


class FakeBackend:
    def __init__(self):
        self.names = []
        self.started = []
        self.fail_start = False
        self.stopped = []

    def active(self):
        return self.names[:]

    def start(self, name, token):
        if self.fail_start:
            raise manager.BackendError("backend_start_failed")
        self.started.append((name, token))
        self.names.append(name)

    def stop(self, name):
        self.stopped.append(name)
        self.names.remove(name)


class FakeProbe:
    def __init__(self):
        self.host = HostMetrics(4, 2.0, 12000, 50000)

    def sample(self):
        return self.host


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scheduler_path = Path(self.tmp.name)
        self.sched_patch = patch.object(manager, "SCHEDULER", self.scheduler_path)
        self.sched_patch.start()
        self.addCleanup(self.sched_patch.stop)
        self.github = FakeGitHub()
        self.backend = FakeBackend()
        self.probe = FakeProbe()
        self.scheduler = manager.Scheduler(self.github, self.backend, self.probe, AdmissionPolicy(), 1)
        self.logs = []
        self.log_patch = patch.object(manager, "log", self.logs.append)
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def test_multiple_queued_jobs_never_spawn_second_vm(self):
        self.scheduler.once()
        self.assertEqual(1, len(self.backend.started))
        self.scheduler.once()
        self.assertEqual(1, len(self.backend.started))
        self.assertTrue(all("ghs_" not in line for line in self.logs))

    def test_high_load_starvation_is_visible_then_recovers(self):
        self.probe.host = HostMetrics(4, 3.5, 12000, 50000)
        self.scheduler.once()
        self.assertEqual([], self.backend.started)
        self.assertIn("admission_denied reason=cpu_load_high", self.logs)
        self.probe.host = HostMetrics(4, 2.0, 12000, 50000)
        self.scheduler.retry_after = 0
        self.scheduler.once()
        self.assertEqual(1, len(self.backend.started))

    def test_flock_contention_is_reason_coded(self):
        with (self.scheduler_path / "admission.lock").open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                self.assertEqual("shared_lock_busy", self.scheduler.spawn().reason)
                self.assertEqual([], self.backend.started)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertTrue(self.scheduler.spawn().allowed)

    def test_missing_token_or_qemu_failure_cannot_leak_token(self):
        self.github.fail_token = True
        self.scheduler.once()
        self.assertEqual([], self.backend.started)
        self.assertTrue(any("github_http_403" in line for line in self.logs))
        self.scheduler.retry_after = 0
        self.github.fail_token = False
        self.backend.fail_start = True
        self.scheduler.once()
        self.assertTrue(any("backend_start_failed" in line for line in self.logs))
        self.assertTrue(all(self.github.token not in line for line in self.logs))

    def test_stale_registration_cleanup_stays_in_own_pool(self):
        self.github.queue = 0
        self.github.runner_states = [
            {"name": "n150-rc-eph-lost", "id": 8, "status": "offline", "busy": False},
            {"name": "n150-gen-eph-other", "id": 9, "status": "offline", "busy": False},
            {"name": "n150-rc-eph-working", "id": 10, "status": "offline", "busy": True},
        ]
        self.scheduler.once()
        self.assertEqual([8], self.github.deleted)

    def test_idle_cleanup_requires_4_polls_and_second_check(self):
        self.github.queue = 0
        name = "n150-rc-eph-1234"
        self.backend.names = [name]
        self.github.runner_states = [{"name": name, "id": 1, "status": "online", "busy": False}]
        for _ in range(3):
            self.scheduler.once()
        self.assertEqual([], self.backend.stopped)
        self.scheduler.once()
        self.assertEqual([name], self.backend.stopped)

    def test_invalid_metrics_are_fail_closed_with_reason(self):
        self.probe.host = HostMetrics(4, float("nan"), 12000, 50000)
        self.scheduler.once()
        self.assertTrue(any("invalid_host_metrics" in line for line in self.logs))
        self.assertFalse(self.backend.started)
