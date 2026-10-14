import json
import unittest
from copy import deepcopy
from pathlib import Path

from n150_runner.config import parse_config, verify_credentials


RAW = json.loads((Path(__file__).parents[1] / "config" / "pools.json").read_text())


class ConfigurationTest(unittest.TestCase):
    def test_exact_four_pools_with_one_control_and_one_kvm(self):
        conf = parse_config(RAW)
        self.assertEqual({"control", "pi", "general", "recursive-coder-kvm"},
                         {p.id for p in conf.pools})
        self.assertEqual(1, sum(p.mode == "persistent" for p in conf.pools))
        self.assertEqual(1, sum(p.backend == "kvm" for p in conf.pools))
        self.assertEqual(2, len({p.token_env for p in conf.pools}))
        self.assertFalse(next(p for p in conf.pools if p.backend == "kvm").mount_docker_socket)

    def test_strict_fields(self):
        for changed in [{"oops": True}, {"poll_seconds": 0}, {"poll_seconds": True}]:
            raw = deepcopy(RAW)
            raw.update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parse_config(raw)

    def test_illegal_capabilities_fail(self):
        cases = [
            ("pi", "mount_docker_socket", True),
            ("recursive-coder-kvm", "mount_pi_config", True),
            ("recursive-coder-kvm", "max_runners", 2),
            ("recursive-coder-kvm", "cpus", 4),
            ("recursive-coder-kvm", "memory_mb", 8192),
            ("general", "model_gated", True),
            ("control", "mode", "ephemeral"),
        ]
        for pool, field, value in cases:
            raw = deepcopy(RAW)
            target = next(p for p in raw["pools"] if p["id"] == pool)
            target[field] = value
            with self.subTest(pool=pool, field=field), self.assertRaises(ValueError):
                parse_config(raw)

    def test_overlapping_identity_and_labels_fail(self):
        raw = deepcopy(RAW)
        raw["pools"][2]["labels"] = list(raw["pools"][1]["labels"])
        with self.assertRaises(ValueError):
            parse_config(raw)

    def test_credentials_are_separate(self):
        cfg = parse_config(RAW)
        with self.assertRaises(ValueError):
            verify_credentials(cfg, {"SOCIAL_MCP_GH_TOKEN": "one"})
        verify_credentials(cfg, {"SOCIAL_MCP_GH_TOKEN": "one",
                                 "RECURSIVE_CODER_GH_TOKEN": "two"})


if __name__ == "__main__":
    unittest.main()
