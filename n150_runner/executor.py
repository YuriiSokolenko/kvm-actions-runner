"""Isolated Docker/KVM executors, with a narrow authenticated-by-filesystem local IPC.

Scheduler has *no* Docker socket or /dev/kvm. Docker and KVM executors
expose only preconfigured pool-scoped list/probe/start/stop/recover verbs.
No arbitrary image, mount, command, path, network or environment is accepted.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
from pathlib import Path

from .config import Pool, load_config

TOKEN = re.compile(r"^[A-Za-z0-9_.-]{20,512}$")
MAX_LINE = 8192
TIMEOUT = 90


class ExecutorError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _run(argv: list[str], *, token: str = "", timeout: int = 35, env=None) -> str:
    try:
        result = subprocess.run(argv, input=token if token else None, text=True, check=True,
                                capture_output=True, timeout=timeout, env=env)
        if len(result.stdout) > 32768:
            raise ExecutorError("EXECUTOR_RESPONSE_TOO_LARGE")
        return result.stdout
    except subprocess.TimeoutExpired:
        raise ExecutorError("EXECUTOR_TIMEOUT") from None
    except (OSError, subprocess.CalledProcessError):
        # Avoid potentially secret-bearing stderr from Docker, QEMU or GitHub.
        raise ExecutorError("EXECUTOR_COMMAND_FAILED") from None


class DockerExecutor:
    def __init__(self, pools: dict[str, Pool]):
        self.pools = pools
        self.evidence_initialized = False

    @staticmethod
    def _docker(*args: str, token: str = "", env=None, timeout: int = 35) -> str:
        return _run(["docker", *args], token=token, env=env, timeout=timeout)

    def probe(self) -> dict:
        self._docker("info", "--format", "{{.ServerVersion}}", timeout=15)
        return {"healthy": True}

    def list(self) -> list[dict]:
        output = self._docker("ps", "-a", "--filter", "label=n150-manager.managed=true",
                              "--format", "{{.Names}}\t{{.State}}\t{{.Label \"n150-manager.pool\"}}")
        workers = []
        for row in output.splitlines():
            parts = row.split("\t")
            if len(parts) != 3 or parts[2] not in self.pools:
                raise ExecutorError("DOCKER_INVALID_LIST")
            workers.append({"name": parts[0], "pool": parts[2], "running": parts[1] == "running"})
        return workers

    def _ensure_evidence(self, pool: Pool) -> None:
        if self.evidence_initialized or not pool.mount_docker_socket:
            return
        volume = os.environ.get("GENERAL_EVIDENCE_VOLUME", "social-mcp-general-runner-evidence")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", volume):
            raise ExecutorError("INVALID_EVIDENCE_VOLUME")
        self._docker("run", "--rm", "--user", "0", "--entrypoint", "chmod",
                     "-v", volume + ":/evidence", pool.runner_image, "1777", "/evidence", timeout=45)
        self.evidence_initialized = True

    def _verify_control_container(self, pool: Pool, name: str, state: str) -> None:
        """Reject stale persistent Control configuration; never recreate automatically."""
        desired_image_id = self._docker(
            "image", "inspect", "--format", "{{.Id}}", pool.runner_image
        ).strip()
        try:
            inspected = json.loads(self._docker("inspect", name))
        except (ValueError, TypeError):
            raise ExecutorError("CONTROL_INSPECT_INVALID") from None
        if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
            raise ExecutorError("CONTROL_INSPECT_INVALID")
        container = inspected[0]
        config = container.get("Config") or {}
        host = container.get("HostConfig") or {}
        labels = config.get("Labels") or {}
        env = config.get("Env") or []
        mount = container.get("Mounts") or []
        required_env = {
            "RUNNER_NAME=" + name,
            "RUNNER_LABELS=" + ",".join(pool.labels),
            "GITHUB_REPOSITORY=" + pool.repository,
        }
        expected_labels = {
            "n150-manager.managed": "true",
            "n150-manager.pool": pool.id,
            "social-mcp.runner-repository": pool.repository,
            "social-mcp.runner-pool": pool.runner_name,
        }
        caps = host.get("CapAdd") or []
        security = host.get("SecurityOpt") or []
        state_mounted = any(
            isinstance(m, dict)
            and m.get("Type") == "volume"
            and m.get("Name") == state
            and m.get("Destination") == "/home/runner/actions-runner"
            for m in mount
        )
        if (
            not desired_image_id.startswith("sha256:")
            or container.get("Image") != desired_image_id
            or not isinstance(labels, dict)
            or any(labels.get(key) != value for key, value in expected_labels.items())
            or not isinstance(env, list)
            or not required_env.issubset(set(env))
            or host.get("NetworkMode") != pool.network
            or host.get("Memory") != pool.memory_mb * 1024 * 1024
            or host.get("NanoCpus") != int(pool.cpus * 1_000_000_000)
            or (host.get("RestartPolicy") or {}).get("Name") != "unless-stopped"
            or not {"SETUID", "SETGID", "KILL"}.issubset(set(caps))
            or not any("no-new-privileges" in option for option in security)
            or not state_mounted
        ):
            raise ExecutorError("CONTROL_CONFIG_DRIFT")

    def start(self, pool: Pool, name: str, token: str) -> None:
        # An existing persistent worker must not silently retain an outdated
        # image, labels, state mount or resource limits after a configuration bump.
        # No live Control container is auto-removed and no volume is modified.
        existing_workers = [w for w in self.list() if w["name"] == name]
        if pool.mode == "persistent":
            if not TOKEN.fullmatch(token):
                raise ExecutorError("INVALID_CONTROL_TOKEN")
            state = os.environ.get("CONTROL_STATE_VOLUME", "social-mcp-control-runner-state")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", state):
                raise ExecutorError("INVALID_CONTROL_STATE_VOLUME")
            if existing_workers:
                self._verify_control_container(pool, name, state)
                if not existing_workers[0]["running"]:
                    self._docker("start", name)
                return
        elif any(w["running"] for w in existing_workers):
            return
        args = ["run", "-d", "--name", name,
                "--label", "n150-manager.managed=true",
                "--label", "n150-manager.pool=" + pool.id,
                "--label", "social-mcp.runner-repository=" + pool.repository,
                "--label", "social-mcp.runner-pool=" + (pool.runner_prefix or pool.runner_name),
                "--network", pool.network,
                "--cpus", str(pool.cpus),
                "--memory", str(pool.memory_mb) + "m",
                "--memory-swap", str(pool.memory_mb) + "m",
                "--pids-limit", "512", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                "-e", "GITHUB_REPOSITORY=" + pool.repository,
                "-e", "RUNNER_NAME=" + name,
                "-e", "RUNNER_LABELS=" + ",".join(pool.labels)]
        env = dict(os.environ)
        if pool.mode == "persistent":
            if not TOKEN.fullmatch(token):
                raise ExecutorError("INVALID_CONTROL_TOKEN")
            # Never rotate or recreate Control automatically. The operator
            # must explicitly recreate the container while preserving its
            # named state volume when image/config/token changes.
            env["GH_ADMIN_TOKEN"] = token
            args += ["--restart", "unless-stopped", "--cap-add", "SETUID",
                     "--cap-add", "SETGID", "--cap-add", "KILL",
                     "-v", state + ":/home/runner/actions-runner", "-e", "GH_ADMIN_TOKEN"]
        else:
            if not TOKEN.fullmatch(token):
                raise ExecutorError("INVALID_RUNNER_TOKEN")
            env["RUNNER_TOKEN"] = token
            args += ["--rm", "--label", "social-mcp.pi-runner=ephemeral",
                     "-e", "RUNNER_TOKEN"]
            if pool.mount_pi_config:
                path = Path(os.environ.get("PI_CONFIG_HOST_DIR", "/host/pi-config"))
                if not path.is_dir():
                    raise ExecutorError("PI_CONFIG_UNAVAILABLE")
                env["RUN_CHECK_EXECUTOR_TOKEN"] = secrets.token_hex(32)
                args += ["-v", str(path) + ":/pi-config-ro:ro",
                         "-e", "RUN_CHECK_EXECUTOR_TOKEN", "-e",
                         "PI_RUN_CHECK_EXECUTOR_URL=http://127.0.0.1:17343"]
                if os.environ.get("PI_ZOEKT_URL"):
                    args += ["-e", "PI_ZOEKT_URL=" + os.environ["PI_ZOEKT_URL"],
                             "-e", "PI_ZOEKT_REPOSITORY=" + pool.repository]
            if pool.mount_docker_socket:
                self._ensure_evidence(pool)
                volume = os.environ.get("GENERAL_EVIDENCE_VOLUME", "social-mcp-general-runner-evidence")
                args += ["-v", "/var/run/docker.sock:/var/run/docker.sock",
                         "-v", volume + ":/evidence", "-e", "INFRA_EVIDENCE_DIR=/evidence"]
        args.append(pool.runner_image)
        self._docker(*args, env=env, timeout=60)

    def stop(self, pool: Pool, name: str) -> None:
        if pool.mode != "ephemeral":
            raise ExecutorError("CONTROL_STOP_FORBIDDEN")
        self._docker("stop", "--time", "20", name, timeout=45)

    def recover(self) -> None:
        self.probe()
        # An ephemeral container may be left in "exited" after a daemon
        # crash despite --rm. Remove only our labelled, *stopped* workers.
        # Never delete the persistent Control container or its state volume.
        for worker in self.list():
            pool = self.pools[worker["pool"]]
            if pool.mode == "ephemeral" and not worker["running"]:
                self._docker("rm", "--", worker["name"], timeout=30)


class KVMExecutor:
    """Preserves the proven kvm-backend.py, overlay cap and guest egress proxy."""

    def __init__(self, pools: dict[str, Pool]):
        self.pools = pools
        self.script = os.environ.get("KVM_BACKEND_SCRIPT", "/usr/local/bin/kvm-backend.py")

    def _command(self, *args: str, token: str = "", timeout: int = TIMEOUT) -> str:
        return _run([sys.executable, self.script, *args], token=token, timeout=timeout)

    def list(self) -> list[dict]:
        pool = next(iter(self.pools.values()))
        return [{"name": name, "pool": pool.id, "running": True}
                for name in self._command("list").splitlines() if name]

    def probe(self) -> dict:
        root = Path(os.environ.get("KVM_STATE_DIR", "/kvm-state"))
        if not root.is_dir():
            raise ExecutorError("KVM_STATE_MISSING")
        stat = os.statvfs(root)
        return {"healthy": True, "disk_free_mb": (stat.f_bavail * stat.f_frsize) // (1024 * 1024)}

    def start(self, pool: Pool, name: str, token: str) -> None:
        current = {w["name"] for w in self.list()}
        if name in current:
            return
        if current:
            raise ExecutorError("KVM_ONE_GUEST_LIMIT")
        if not TOKEN.fullmatch(token):
            raise ExecutorError("INVALID_RUNNER_TOKEN")
        self._command("start", "--name", name, token=token, timeout=90)

    def stop(self, pool: Pool, name: str) -> None:
        self._command("stop", "--name", name, timeout=90)

    def recover(self) -> None:
        self._command("recover", timeout=90)


class _Server(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, socket_path: str, executor, pools):
        self.executor = executor
        self.pools = pools
        super().__init__(socket_path, _Handler)

    def dispatch(self, item: dict) -> dict:
        if not isinstance(item, dict) or set(item) != {"command", "pool", "name", "token"}:
            raise ExecutorError("INVALID_REQUEST")
        command, pool_id, name, token = (item[k] for k in ("command", "pool", "name", "token"))
        if not all(isinstance(v, str) for v in (command, pool_id, name, token)):
            raise ExecutorError("INVALID_REQUEST")
        if command in ("list", "probe", "recover"):
            if pool_id or name or token:
                raise ExecutorError("INVALID_REQUEST")
            if command == "list":
                return {"workers": self.executor.list()}
            if command == "probe":
                return self.executor.probe()
            self.executor.recover()
            return {}
        pool = self.pools.get(pool_id)
        if pool is None:
            raise ExecutorError("UNKNOWN_POOL")
        identity = re.escape(pool.runner_prefix) + r"-[A-Za-z0-9-]{1,80}"
        if not re.fullmatch(identity if pool.mode == "ephemeral" else re.escape(pool.runner_name), name):
            raise ExecutorError("INVALID_RUNNER_NAME")
        if command == "start":
            self.executor.start(pool, name, token)
            return {}
        if command == "stop" and not token:
            self.executor.stop(pool, name)
            return {}
        raise ExecutorError("UNKNOWN_COMMAND")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        data = self.rfile.readline(MAX_LINE + 1)
        try:
            if not data.endswith(b"\n") or len(data) > MAX_LINE:
                raise ExecutorError("INVALID_REQUEST_SIZE")
            reply = {"ok": True, **self.server.dispatch(json.loads(data))}
        except ExecutorError as err:
            reply = {"ok": False, "code": err.code}
        except (ValueError, TypeError, UnicodeError):
            reply = {"ok": False, "code": "INVALID_REQUEST"}
        except Exception:
            # Do not leak QEMU/Docker stdout, stderr, credentials or paths.
            reply = {"ok": False, "code": "EXECUTOR_INTERNAL_ERROR"}
        self.wfile.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")


def serve(backend: str) -> None:
    config = load_config()
    pools = {p.id: p for p in config.pools_for_backend(backend)}
    if not pools:
        raise SystemExit("no pools configured for backend")
    executor = DockerExecutor(pools) if backend == "docker" else KVMExecutor(pools)
    config.socket_dir.mkdir(mode=0o770, parents=True, exist_ok=True)
    socket_path = str(config.socket_dir / (backend + ".sock"))
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    with _Server(socket_path, executor, pools) as server:
        os.chmod(socket_path, 0o660)
        print(json.dumps({"component": backend + "-executor", "event": "ready"}), flush=True)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("docker", "kvm"):
        raise SystemExit("usage: python -m n150_runner.executor docker|kvm")
    serve(sys.argv[1])
