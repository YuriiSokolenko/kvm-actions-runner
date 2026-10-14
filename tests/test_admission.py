"""Regression tests for #795 admission starvation and fail-closed gates."""
import dataclasses
import math
import unittest

from admission import AdmissionPolicy, HostMetrics


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.policy = AdmissionPolicy()
        self.safe = HostMetrics(online_cpus=4, load1=0.5, mem_available_mb=12000, disk_available_mb=40000)

    def test_four_core_moderate_load_admitted_even_if_legacy_heuristic_would_deny(self):
        for load in (0, 0.9, 1.0, 1.01, 1.5, 2.5, 3.0):
            sample = dataclasses.replace(self.safe, load1=load)
            with self.subTest(load=load):
                self.assertEqual("admitted", self.policy.check(0, sample).reason)

    def test_rejects_sustained_high_load_but_recovers(self):
        for load in (3.01, 4.0, 50.0):
            with self.subTest(load=load):
                self.assertEqual("cpu_load_high", self.policy.check(0, dataclasses.replace(self.safe, load1=load)).reason)
        self.assertTrue(self.policy.check(0, dataclasses.replace(self.safe, load1=2.0)).allowed)

    def test_all_resource_and_pool_limits(self):
        cases = (
            (0, dataclasses.replace(self.safe, online_cpus=2), "cpu_count_insufficient"),
            (0, dataclasses.replace(self.safe, mem_available_mb=8191), "memory_low"),
            (0, dataclasses.replace(self.safe, disk_available_mb=25599), "disk_low"),
            (1, self.safe, "pool_full"),
            (-1, self.safe, "invalid_active_count"),
        )
        for active, sample, code in cases:
            with self.subTest(code=code):
                self.assertEqual(code, self.policy.check(active, sample).reason)
        self.assertEqual("global_pool_reserved",
                         dataclasses.replace(self.policy, legacy_reserved=12).check(0, self.safe).reason)

    def test_malformed_metrics_never_admit(self):
        for value in (float("nan"), float("inf"), -1, "2", None, True):
            with self.subTest(value=value):
                self.assertEqual("invalid_host_metrics", self.policy.check(0, dataclasses.replace(self.safe, load1=value)).reason)
        for field, value in (("online_cpus", None), ("online_cpus", True),
                             ("mem_available_mb", -1), ("disk_available_mb", "80000")):
            with self.subTest(field=field):
                self.assertEqual("invalid_host_metrics",
                                 self.policy.check(0, dataclasses.replace(self.safe, **{field: value})).reason)
