from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from searchx.http import HttpClient, HttpError


class _RedirectHandler(BaseHTTPRequestHandler):
    requests_seen: list[tuple[str, str | None, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        type(self).requests_seen.append(
            (
                self.path,
                self.headers.get("Authorization"),
                self.headers.get("X-API-KEY"),
            )
        )
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", "/capture")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, _format: str, *args: object) -> None:
        pass


class HttpClientSecurityTests(unittest.TestCase):
    def test_redirect_is_not_followed_with_request_headers(self) -> None:
        _RedirectHandler.requests_seen = []
        # The execution environment can block while resolving the local
        # server's FQDN; the server only needs a stable local name for this
        # in-process transport test.
        with patch("socket.getfqdn", return_value="localhost"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            with self.assertRaises(HttpError) as raised:
                HttpClient(retries=0).request_json(
                    "GET",
                    f"http://127.0.0.1:{server.server_port}/start",
                    headers={"Authorization": "Bearer test-only-secret", "X-API-KEY": "test-key"},
                )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(raised.exception.status, 302)
        self.assertEqual(
            _RedirectHandler.requests_seen,
            [("/start", "Bearer test-only-secret", "test-key")],
        )


if __name__ == "__main__":
    unittest.main()
