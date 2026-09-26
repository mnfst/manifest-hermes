"""Regressions from the SDK review: redirects never carry the key, heals never pile up."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import manifest_heal
from manifest_heal import Healer, HealClient

HEAL_WORKERS = getattr(manifest_heal, "HEAL_WORKERS", 4)


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_a_redirect_never_carries_the_key_to_another_host():
    seen = []

    class Thief(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # urllib turns a redirected POST into a GET
            self.do_POST()

        def do_POST(self):
            seen.append(self.headers.get("authorization"))
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    thief, thief_url = serve(Thief)

    class Redirecting(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.send_response(302)
            self.send_header("location", thief_url + "/v1/heal")
            self.send_header("content-length", "0")
            self.end_headers()

    manifest, manifest_url = serve(Redirecting)
    try:
        assert HealClient("mnfx_secret", manifest_url, timeout=2.0).heal({"traceId": "t"}) is None
        assert seen == []
    finally:
        manifest.shutdown()
        thief.shutdown()


class SlowApi:
    """A Manifest that never answers until released."""

    def __init__(self):
        self.calls = 0
        self.release = threading.Event()
        self.lock = threading.Lock()

    def heal(self, payload):
        with self.lock:
            self.calls += 1
        self.release.wait(5)
        return None


def test_heals_that_find_every_worker_busy_are_passed_through_not_queued():
    api = SlowApi()
    healer = Healer(api, timeout=0.05)
    try:
        for _ in range(HEAL_WORKERS + 6):
            assert healer.on_error("list_issues", {"sort": "x"}, "invalid sort", host="mcp.example.com") is None
        api.release.set()
        healer._pool.shutdown(wait=True)
        # Only the calls that found a free worker were ever sent; none ran late.
        assert api.calls == HEAL_WORKERS
    finally:
        api.release.set()
