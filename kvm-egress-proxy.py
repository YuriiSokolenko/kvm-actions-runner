#!/usr/bin/env python3
"""Small allowlist-only HTTP CONNECT proxy for QEMU's restricted user net.

Bind this only to loopback and expose it to a guest through QEMU guestfwd while
`restrict=on` is set. QEMU drops all guest packets except explicitly forwarded
services; this proxy then restricts destinations and rejects non-public DNS
answers before connecting. It is intentionally not a general-purpose proxy.
"""

from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import sys
import threading
import time
from urllib.parse import urlsplit

MAX_HEADER = 16 * 1024
MAX_TUNNEL_SECONDS = 1800
TUNNEL_IDLE_SECONDS = 300
TUNNEL_BUFFER_BYTES = 256 * 1024
TUNNEL_CHUNK_BYTES = 64 * 1024
MAX_CONNECTIONS = 64
EXACT_HOSTS = frozenset(
    {
        "api.github.com",
        "api.adoptium.net",
        "archive.ubuntu.com",
        "codeload.github.com",
        "downloads.gradle.org",
        "files.pythonhosted.org",
        "github.com",
        "github-releases.githubusercontent.com",
        "github-registry-files.githubusercontent.com",
        "github-cloud.githubusercontent.com",
        "github-cloud.s3.amazonaws.com",
        "githubusercontent.com",
        "objects-origin.githubusercontent.com",
        "objects.githubusercontent.com",
        "actions.githubusercontent.com",
        "pipelines.actions.githubusercontent.com",
        "plugins.gradle.org",
        "plugins-artifacts.gradle.org",
        "pypi.org",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "repo.maven.apache.org",
        "results-receiver.actions.githubusercontent.com",
        "security.ubuntu.com",
        "services.gradle.org",
        "nodejs.org",
    }
)
ALLOWED_SUFFIXES = (
    ".blob.core.windows.net",
    ".actions.githubusercontent.com",
    ".githubusercontent.com",
)


def allowed_host(host: str) -> bool:
    host = host.rstrip(".").lower().encode("idna").decode("ascii")
    return host in EXACT_HOSTS or any(host.endswith(suffix) for suffix in ALLOWED_SUFFIXES)


def public_addresses(host: str, port: int) -> list[tuple[int, tuple]]:
    if not allowed_host(host):
        raise ValueError("destination host is not allowlisted")
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    usable: list[tuple[int, tuple]] = []
    for family, socktype, proto, _canon, sockaddr in answers:
        address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        if not address.is_global:
            continue
        usable.append((family, sockaddr))
    if not usable:
        raise ValueError("destination resolved only to non-public addresses")
    return usable


def connect_public(host: str, port: int) -> socket.socket:
    errors: list[OSError] = []
    for family, sockaddr in public_addresses(host, port):
        try:
            upstream = socket.socket(family, socket.SOCK_STREAM)
            upstream.settimeout(10)
            upstream.connect(sockaddr)
            upstream.settimeout(None)
            return upstream
        except OSError as error:
            errors.append(error)
    raise OSError("could not connect to allowlisted public destination") from (errors[-1] if errors else None)


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(15)
        try:
            header = bytearray()
            while b"\r\n\r\n" not in header:
                chunk = client.recv(2048)
                if not chunk:
                    return
                header.extend(chunk)
                if len(header) > MAX_HEADER:
                    raise ValueError("request header too large")
            end = header.index(b"\r\n\r\n") + 4
            request_bytes = bytes(header[:end])
            remainder = bytes(header[end:])
            lines = request_bytes.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":
                host, separator, raw_port = target.rpartition(":")
                if not separator or not raw_port.isdigit() or int(raw_port) != 443:
                    raise ValueError("only HTTPS CONNECT on port 443 is allowed")
                upstream = connect_public(host.strip("[]"), 443)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if remainder:
                    upstream.sendall(remainder)
                self.tunnel(client, upstream)
                return

            uri = urlsplit(target)
            if uri.scheme != "http" or not uri.hostname or (uri.port or 80) != 80:
                raise ValueError("only absolute HTTP requests on port 80 are allowed")
            upstream = connect_public(uri.hostname, 80)
            path = uri.path or "/"
            if uri.query:
                path += "?" + uri.query
            rewritten = f"{method} {path} {version}\r\n".encode("ascii")
            filtered = [line for line in lines[1:] if line and not line.lower().startswith(
                ("proxy-authorization:", "proxy-connection:")
            )]
            upstream.sendall(rewritten + "\r\n".join(filtered).encode("iso-8859-1") + b"\r\n\r\n" + remainder)
            self.tunnel(client, upstream)
        except (OSError, ValueError, UnicodeError) as error:
            try:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            print(f"deny peer={self.client_address[0]} reason={type(error).__name__}", file=sys.stderr, flush=True)

    @staticmethod
    def tunnel(client: socket.socket, upstream: socket.socket) -> None:
        client.setblocking(False)
        upstream.setblocking(False)
        peers = {client: upstream, upstream: client}
        pending = {client: bytearray(), upstream: bytearray()}
        read_open = {client: True, upstream: True}
        write_shutdown = {client: False, upstream: False}
        started = last_activity = time.monotonic()
        try:
            while any(read_open.values()) or any(pending.values()):
                now = time.monotonic()
                remaining = min(
                    MAX_TUNNEL_SECONDS - (now - started),
                    TUNNEL_IDLE_SECONDS - (now - last_activity),
                )
                if remaining <= 0:
                    return
                readable = [
                    sock for sock in peers
                    if read_open[sock] and len(pending[peers[sock]]) < TUNNEL_BUFFER_BYTES
                ]
                writable = [sock for sock in peers if pending[sock]]
                try:
                    ready_read, ready_write, exceptional = select.select(
                        readable, writable, list(peers), remaining
                    )
                except (OSError, ValueError):
                    return
                if exceptional:
                    return
                if not ready_read and not ready_write:
                    return
                for source in ready_read:
                    room = TUNNEL_BUFFER_BYTES - len(pending[peers[source]])
                    try:
                        data = source.recv(min(TUNNEL_CHUNK_BYTES, room))
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return
                    last_activity = time.monotonic()
                    if data:
                        pending[peers[source]].extend(data)
                    else:
                        read_open[source] = False
                for target in ready_write:
                    try:
                        written = target.send(pending[target])
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return
                    if written <= 0:
                        return
                    del pending[target][:written]
                    last_activity = time.monotonic()
                for source, target in peers.items():
                    if not read_open[source] and not pending[target] and not write_shutdown[target]:
                        try:
                            target.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        write_shutdown[target] = True
        finally:
            client.close()
            upstream.close()


class LocalOnlyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    _slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main() -> None:
    address = ("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 3128)
    with LocalOnlyServer(address, ProxyHandler) as server:
        print(f"listening=127.0.0.1:{address[1]} allowlisted_hosts={len(EXACT_HOSTS)}", flush=True)
        server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
