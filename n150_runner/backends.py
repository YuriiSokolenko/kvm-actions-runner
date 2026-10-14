"""Minimal typed Unix-socket client: scheduling never touches Docker or /dev/kvm."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from .config import Configuration, Pool


class BackendError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Worker:
    name: str
    pool: str
    running: bool


class Backends:
    def __init__(self, config: Configuration, *, transport=None):
        self.config = config
        self.transport = transport

    def _call(self, backend: str, command: str, pool: Pool | None = None,
              *, name: str = "", token: str = "") -> dict:
        request = {"command": command, "pool": pool.id if pool else "",
                   "name": name, "token": token}
        if self.transport is not None:
            return self.transport(backend, request)
        path = str(self.config.socket_dir / (backend + ".sock"))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(100)
                sock.connect(path)
                # One bounded JSON record. The backend does not echo tokens.
                sock.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                result = bytearray()
                while not result.endswith(b"\n"):
                    fragment = sock.recv(4096)
                    if not fragment:
                        raise BackendError("BACKEND_CONNECTION_CLOSED")
                    result.extend(fragment)
                    if len(result) > 32768:
                        raise BackendError("BACKEND_RESPONSE_TOO_LARGE")
                value = json.loads(result)
        except (OSError, ValueError, UnicodeError):
            raise BackendError("BACKEND_UNAVAILABLE") from None
        if not isinstance(value, dict):
            raise BackendError("BACKEND_INVALID_RESPONSE")
        if value.get("ok") is not True:
            code = value.get("code")
            if not isinstance(code, str) or not code.isupper():
                code = "BACKEND_REJECTED"
            raise BackendError(code)
        return value

    def workers(self, backend: str) -> list[Worker]:
        response = self._call(backend, "list")
        value = response.get("workers")
        if not isinstance(value, list):
            raise BackendError("BACKEND_INVALID_LIST")
        workers = []
        for worker in value:
            if (not isinstance(worker, dict)
                    or not isinstance(worker.get("name"), str)
                    or not isinstance(worker.get("pool"), str)
                    or type(worker.get("running")) is not bool):
                raise BackendError("BACKEND_INVALID_LIST")
            workers.append(Worker(worker["name"], worker["pool"], worker["running"]))
        return workers

    def probe(self, backend: str) -> dict:
        return self._call(backend, "probe")

    def start(self, pool: Pool, name: str, token: str) -> None:
        self._call(pool.backend, "start", pool, name=name, token=token)

    def stop(self, pool: Pool, name: str) -> None:
        self._call(pool.backend, "stop", pool, name=name)

    def recover(self, backend: str) -> None:
        self._call(backend, "recover")
