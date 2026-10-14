import importlib.util
import socket
import socketserver
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "infra/github-runner-autoscaler/kvm-egress-proxy.py"
SPEC = importlib.util.spec_from_file_location("kvm_egress_proxy", MODULE_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class EgressPolicyTests(unittest.TestCase):
    def test_only_exact_hosts_and_blob_storage_subdomains_are_allowed(self):
        self.assertTrue(proxy.allowed_host("api.github.com"))
        self.assertTrue(proxy.allowed_host("release-assets.githubusercontent.com."))
        self.assertTrue(proxy.allowed_host("run-123.blob.core.windows.net"))
        self.assertFalse(proxy.allowed_host("attacker.github.com"))
        self.assertFalse(proxy.allowed_host("blob.core.windows.net"))
        self.assertFalse(proxy.allowed_host("192.168.8.184"))

    @patch.object(proxy.socket, "getaddrinfo")
    def test_rejects_private_dns_answers_and_pins_public_answers(self, getaddrinfo):
        def record(address, family):
            sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)

        getaddrinfo.return_value = [
            record("127.0.0.1", socket.AF_INET),
            record("192.168.8.184", socket.AF_INET),
            record("::1", socket.AF_INET6),
            record("93.184.216.34", socket.AF_INET),
        ]
        result = proxy.public_addresses("api.github.com", 443)
        self.assertEqual([socket.AF_INET], [family for family, _ in result])
        self.assertEqual(("93.184.216.34", 443), result[0][1])

    @patch.object(proxy.socket, "getaddrinfo", return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443)),
    ])
    def test_fails_closed_when_allowed_name_resolves_only_to_private_ip(self, _getaddrinfo):
        with self.assertRaisesRegex(ValueError, "non-public"):
            proxy.public_addresses("api.github.com", 443)

    def test_github_artifact_blob_endpoint_is_allowlisted(self):
        self.assertTrue(proxy.allowed_host("actions-artifacts-123.blob.core.windows.net"))


class TunnelTests(unittest.TestCase):
    def _pump_pair(self):
        guest_proxy, guest = socket.socketpair()
        upstream_proxy, upstream = socket.socketpair()
        for sock in (guest_proxy, guest, upstream_proxy, upstream):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        pump = threading.Thread(
            target=proxy.ProxyHandler.tunnel,
            args=(guest_proxy, upstream_proxy),
            daemon=True,
        )
        pump.start()
        return guest, upstream, pump

    @staticmethod
    def _read_slow(sock, chunks, pause=0.0001):
        result = bytearray()
        while True:
            data = sock.recv(chunks)
            if not data:
                return bytes(result)
            result.extend(data)
            if pause:
                time.sleep(pause)

    def test_large_bidirectional_transfer_survives_backpressure_and_partial_writes(self):
        guest, upstream, pump = self._pump_pair()
        guest.settimeout(30)
        upstream.settimeout(30)
        upload = bytes(range(251)) * 16384
        download = bytes(reversed(range(251))) * 16384
        received_upload = []
        received_download = []

        upload_reader = threading.Thread(target=lambda: received_upload.append(self._read_slow(upstream, 4096)))
        download_reader = threading.Thread(target=lambda: received_download.append(self._read_slow(guest, 4096)))
        upload_reader.start()

        def send_download_after_upload():
            _ = received_upload
            while upload_reader.is_alive():
                time.sleep(0.002)
            upstream.sendall(download)
            upstream.shutdown(socket.SHUT_WR)

        sender = threading.Thread(target=send_download_after_upload)
        sender.start()
        guest.sendall(upload)
        guest.shutdown(socket.SHUT_WR)
        download_reader.start()
        upload_reader.join(35)
        sender.join(35)
        download_reader.join(35)
        pump.join(5)
        self.assertFalse(upload_reader.is_alive(), "upload reader stalled")
        self.assertFalse(download_reader.is_alive(), "download reader stalled")
        self.assertFalse(pump.is_alive(), "proxy tunnel did not close after both half-closes")
        self.assertEqual(upload, received_upload[0])
        self.assertEqual(download, received_download[0])
        guest.close()
        upstream.close()

    def test_idle_tunnel_times_out_and_closes_upstream(self):
        guest_proxy, guest = socket.socketpair()
        upstream_proxy, upstream = socket.socketpair()
        with patch.object(proxy, "TUNNEL_IDLE_SECONDS", 0.05), patch.object(proxy, "MAX_TUNNEL_SECONDS", 1):
            started = time.monotonic()
            proxy.ProxyHandler.tunnel(guest_proxy, upstream_proxy)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(b"", upstream.recv(1))
        guest.close()
        upstream.close()

    def test_peer_interruption_terminates_tunnel(self):
        guest_proxy, guest = socket.socketpair()
        upstream_proxy, upstream = socket.socketpair()
        guest.close()
        with patch.object(proxy, "TUNNEL_IDLE_SECONDS", 0.2), patch.object(proxy, "MAX_TUNNEL_SECONDS", 1):
            started = time.monotonic()
            proxy.ProxyHandler.tunnel(guest_proxy, upstream_proxy)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(b"", upstream.recv(1))
        upstream.close()

    def test_github_artifact_connect_is_forwarded(self):
        server = proxy.LocalOnlyServer(("127.0.0.1", 0), proxy.ProxyHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        proxy_upstream, artifact_endpoint = socket.socketpair()
        client = socket.create_connection(server.server_address, timeout=5)
        with patch.object(proxy, "connect_public", return_value=proxy_upstream) as connect:
            try:
                client.sendall(
                    b"CONNECT actions-artifacts-123.blob.core.windows.net:443 HTTP/1.1\r\n"
                    b"Host: actions-artifacts-123.blob.core.windows.net:443\r\n\r\n"
                )
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    response.extend(client.recv(1024))
                self.assertTrue(response.startswith(b"HTTP/1.1 200"), response)
                connect.assert_called_once_with("actions-artifacts-123.blob.core.windows.net", 443)
                client.sendall(b"artifact-upload-chunk")
                self.assertEqual(b"artifact-upload-chunk", artifact_endpoint.recv(64))
                artifact_endpoint.sendall(b"upload-ack")
                self.assertEqual(b"upload-ack", client.recv(64))
            finally:
                client.close()
                artifact_endpoint.close()
                server.shutdown()
                server.server_close()
                server_thread.join(5)

if __name__ == "__main__":
    unittest.main()
