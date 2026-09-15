"""Stdlib stub of the Manifest heal contract: POST /v1/heal, PATCH /v1/heal-attempts/<id>."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class StubHeal:
    def __init__(self) -> None:
        self.result: Optional[dict] = None
        self.heals: list[dict] = []
        self.outcomes: list[tuple[str, dict]] = []
        self.disabled = False  # answer 403 project_disabled to heals
        self._server: Optional[ThreadingHTTPServer] = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "StubHeal":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _read(self) -> dict:
                length = int(self.headers.get("content-length", 0))
                return json.loads(self.rfile.read(length) or b"{}")

            def _reply(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path != "/v1/heal":
                    return self._reply(404, {"error": "not_found"})
                stub.heals.append(self._read())
                if stub.disabled:
                    return self._reply(403, {"error": "project_disabled"})
                self._reply(200, stub.result or {"status": "no_patch", "issueId": "stub"})

            def do_PATCH(self):
                prefix = "/v1/heal-attempts/"
                if not self.path.startswith(prefix):
                    return self._reply(404, {"error": "not_found"})
                stub.outcomes.append((self.path[len(prefix):], self._read()))
                self._reply(200, {})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
