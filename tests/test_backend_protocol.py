import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from n150_runner.config import load_config
from n150_runner.executor import DockerExecutor, ExecutorError, _Server

CONF = load_config("config/pools.json")
POOLS = {p.id: p for p in CONF.pools}


class DockerExecutorTest(unittest.TestCase):
    def setUp(self):
        self.executor = DockerExecutor(POOLS)
        self.calls = []

        def fake_docker(*args, token="", env=None, timeout=35):
            self.calls.append((args, dict(env or {})))
            return ""
        self.executor._docker = fake_docker

    def test_general_docker_socket_is_only_general(self):
        self.executor.start(POOLS["general"], "n150-gen-eph-123", "test_token_12345678901234567890")
        calls = [args for args, _ in self.calls if args and args[0] == "run"]
        self.assertEqual(2, len(calls))  # one evidence preparation, one worker
        worker_args = calls[-1]
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", worker_args)
        self.assertIn("--cap-drop", worker_args)
        self.assertNotIn("test_token_12345678901234567890", " ".join(worker_args))
        self.assertTrue(any("RUNNER_TOKEN" in env for args, env in self.calls))

    def test_stale_control_image_is_not_silently_started(self):
        commands = []

        def fake_docker(*args, token="", env=None, timeout=35):
            commands.append(args)
            if args[0] == "ps":
                return "n150-control\\texited\\tcontrol\\n".replace("\\t", "\t").replace("\\n", "\n")
            if args[:2] == ("image", "inspect"):
                return "sha256:updated-image"
            if args[0] == "inspect":
                return '[{"Image":"sha256:old-image","Config":{},"HostConfig":{},"Mounts":[]}]'
            raise AssertionError(args)

        self.executor._docker = fake_docker
        with self.assertRaises(ExecutorError) as error:
            self.executor.start(POOLS["control"], "n150-control",
                                "github_pat_admin_1234567890123456789")
        self.assertEqual("CONTROL_CONFIG_DRIFT", error.exception.code)
        self.assertFalse(any(args[0] in ("run", "start", "rm") for args in commands))

    def test_recovery_only_removes_exited_ephemeral_containers(self):
        commands = []

        def mocked_docker(*args, token="", env=None, timeout=35):
            commands.append(args)
            if args[0] == "ps":
                return ("n150-gen-eph-leftover\\texited\\tgeneral\\n"
                        "n150-control\\texited\\tcontrol\\n").replace("\\t", "\t").replace("\\n", "\n")
            return ""

        self.executor._docker = mocked_docker
        self.executor.recover()
        removed = [args for args in commands if args[0] == "rm"]
        self.assertEqual([("rm", "--", "n150-gen-eph-leftover")], removed)
        self.assertFalse(any("n150-control" in args for args in removed))

    def test_pi_never_receives_docker_socket(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PI_CONFIG_HOST_DIR": temp}):
            self.executor.start(POOLS["pi"], "n150-pi-eph-123", "test_token_12345678901234567890")
        args = self.calls[-1][0]
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", args)
        self.assertIn("RUN_CHECK_EXECUTOR_TOKEN", args)
        self.assertIn("--cap-drop", args)

    def test_control_does_not_use_ephemeral_flag_and_cannot_stop(self):
        self.executor.start(POOLS["control"], "n150-control", "github_pat_admin_1234567890123456789")
        args = self.calls[-1][0]
        self.assertNotIn("--rm", args)
        self.assertIn("unless-stopped", args)
        with self.assertRaises(ExecutorError):
            self.executor.stop(POOLS["control"], "n150-control")


class IPCDispatchTest(unittest.TestCase):
    def setUp(self):
        self.server = _Server.__new__(_Server)
        self.server.pools = {"pi": POOLS["pi"]}
        class Stub:
            def start(self, *args):
                return None
        self.server.executor = Stub()

    def test_arbitrary_command_or_identity_is_rejected(self):
        valid = {"command": "start", "pool": "pi", "name": "n150-pi-eph-123",
                 "token": "test_token_12345678901234567890"}
        self.assertEqual({}, self.server.dispatch(valid))
        for change in ({"pool": "general"}, {"name": "n150-control"},
                       {"command": "run"}, {"arbitrary": "shell"}):
            body = dict(valid, **change)
            with self.subTest(change=change), self.assertRaises(ExecutorError):
                self.server.dispatch(body)

    def test_global_commands_cannot_carry_token_or_pool(self):
        with self.assertRaises(ExecutorError):
            self.server.dispatch({"command": "probe", "pool": "pi", "name": "",
                                  "token": "token"})


if __name__ == "__main__":
    unittest.main()
