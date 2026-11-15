"""A tiny real HTTP server, so capture is tested against sockets, not mocks.

Patching an HTTP client and asserting with a mocked transport would test the
mock. These tests make real requests to a loopback server, which is the only way
to know the patch points actually sit where the traffic is.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import pytest


class MockAPI:
    """A loopback HTTP server with a settable route table."""

    def __init__(self):
        self.routes: Dict[Tuple[str, str], Tuple[int, Dict[str, str], Any]] = {}
        self.log = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- configuration ----------------------------------------------------

    def route(
        self,
        method: str,
        path: str,
        status: int = 200,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> "MockAPI":
        self.routes[(method.upper(), path)] = (status, dict(headers or {}), body)
        return self

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "MockAPI":
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _handle(self):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                api.log.append(
                    {
                        "method": self.command,
                        "path": parts.path,
                        "query": parts.query,
                        "headers": dict(self.headers),
                        "body": raw,
                    }
                )

                route = api.routes.get((self.command, parts.path))
                if route is None:
                    self._send(404, {}, {"error": "not found", "path": parts.path})
                    return
                status, headers, body = route
                self._send(status, headers, body)

            def _send(self, status, headers, body):
                payload = b""
                content_type = headers.get("Content-Type")
                if isinstance(body, bytes):
                    payload = body
                elif isinstance(body, str):
                    payload = body.encode("utf-8")
                    content_type = content_type or "text/plain; charset=utf-8"
                elif body is not None:
                    payload = json.dumps(body).encode("utf-8")
                    content_type = content_type or "application/json"

                self.send_response(status)
                for name, value in headers.items():
                    if name.lower() == "content-type":
                        continue
                    self.send_header(name, value)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_PATCH = _handle
            do_DELETE = _handle
            do_HEAD = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def url(self, path: str) -> str:
        return self.base + path


@pytest.fixture
def api():
    server = MockAPI().start()
    try:
        yield server
    finally:
        server.stop()
