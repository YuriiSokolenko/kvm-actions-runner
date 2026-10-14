#!/usr/local/bin/python3
"""Run a prevalidated fixed-argv check and return bounded structured output."""
import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque

TAIL_LIMIT = 4 * 1024 * 1024
ALLOWED_ENVIRONMENT_KEYS = frozenset({
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING",
    "RUFF_CACHE_DIR",
    "PI_TRUSTED_ACCEPTANCE_TARGETS",
    "PI_TRUSTED_ACCEPTANCE_BASELINE_TARGETS",
})


def validate_environment(environment):
    """Reject sandbox process environments outside the executor allowlist."""
    if not isinstance(environment, dict) or any(key not in ALLOWED_ENVIRONMENT_KEYS for key in environment):
        raise ValueError("invalid sandbox environment")


class Tail:
    def __init__(self):
        self.chunks = deque()
        self.size = 0
        self.truncated = False
        self.lock = threading.Lock()

    def read(self, stream):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            with self.lock:
                self.chunks.append(chunk)
                self.size += len(chunk)
                while self.size > TAIL_LIMIT:
                    first = self.chunks[0]
                    excess = self.size - TAIL_LIMIT
                    if len(first) <= excess:
                        self.chunks.popleft()
                        self.size -= len(first)
                    else:
                        self.chunks[0] = first[excess:]
                        self.size -= excess
                    self.truncated = True

    def text(self):
        with self.lock:
            return b"".join(self.chunks).decode("utf-8", errors="replace")


def main():
    encoded = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        command = payload["command"]
        args = payload["args"]
        timeout_ms = int(payload["timeout_ms"])
        environment = payload["env"]
        if not isinstance(command, str) or not command or not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("invalid fixed command")
        validate_environment(environment)
    except Exception as error:  # malformed trusted-executor payload is infrastructure failure
        print(json.dumps({"protocol_error": str(error)}))
        return 70

    started = time.monotonic()
    stdout = Tail()
    stderr = Tail()
    try:
        child = subprocess.Popen(
            [command, *args], cwd="/workspace", env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, close_fds=True,
        )
    except OSError as error:
        print(json.dumps({"spawn_error": {"code": error.errno, "message": str(error)}, "duration_ms": int((time.monotonic() - started) * 1000)}))
        return 0

    threads = [threading.Thread(target=stdout.read, args=(child.stdout,), daemon=True), threading.Thread(target=stderr.read, args=(child.stderr,), daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        exit_code = child.wait(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        exit_code = child.wait()
    for thread in threads:
        thread.join(timeout=2)

    print(json.dumps({
        "exitCode": exit_code,
        "timedOut": timed_out,
        "durationMs": int((time.monotonic() - started) * 1000),
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "truncated": stdout.truncated or stderr.truncated,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
