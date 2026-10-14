import importlib.util
import unittest
from pathlib import Path


POLICY_PATH = Path(__file__).parents[1] / "kvm_deploy_policy.py"
SPEC = importlib.util.spec_from_file_location("kvm_deploy_policy", POLICY_PATH)
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class NanoCpuPolicyTests(unittest.TestCase):
    def test_accepts_exact_cap_and_docker_rounding(self):
        self.assertTrue(POLICY.nano_cpus_within_limit(2_250_000_000))
        self.assertTrue(POLICY.nano_cpus_within_limit(2_249_999_872))
        self.assertTrue(POLICY.nano_cpus_within_limit(2_249_000_000))

    def test_rejects_missing_or_materially_insufficient_quota(self):
        for value in (None, 0, 2_000_000_000, 2_248_999_999):
            with self.subTest(value=value):
                self.assertFalse(POLICY.nano_cpus_within_limit(value))

    def test_rejects_quota_above_cap_and_non_integer_values(self):
        for value in (2_250_000_001, 2_500_000_000, 2.25, True):
            with self.subTest(value=value):
                self.assertFalse(POLICY.nano_cpus_within_limit(value))


if __name__ == "__main__":
    unittest.main()
