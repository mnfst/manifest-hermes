"""Tracked tool calls. Standard library only.

Every MCP tool call the plugin does not send to POST /v1/heal is recorded as
metadata and sent in batches to POST /v1/requests: the method, the tool's URL
(the same one its heal capture would use; HTTP servers only), the status (200 when the call
worked, the 418 tool-call sentinel when it failed and was not healed), the
duration Hermes measured, and when it happened. Never the arguments or the
result.

`record()` is an in-memory append under a short lock. One daemon thread sends:
when 500 calls are waiting or every 5 s, at most once per second, 500 per
batch. Past MAX_BUFFER the newest call is dropped. A batch whose send raises
is retried once, then dropped.
"""
from __future__ import annotations

import collections
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

try:  # loaded as a package by Hermes
    from .manifest_heal import tool_url
except ImportError:  # loaded flat, plugin directory on sys.path
    from manifest_heal import tool_url  # type: ignore

MAX_BUFFER = 5000
MAX_BATCH = 500
MAX_URL = 4096


def tracked_call(server: str, tool_name: str, host: Optional[str], status_code: int,
                 duration_ms: Optional[int]) -> Optional[dict]:
    """The wire record for one tool call, or None when it is not tracked.

    Only tool calls on an MCP server reached over HTTP are tracked: a stdio
    server is a local process with no address, and Colibri tracks HTTP services.
    """
    if not host:
        return None
    url = tool_url(server, tool_name, host)
    if len(url) > MAX_URL or not 100 <= status_code <= 599:
        return None
    return {
        "traceId": uuid.uuid4().hex,
        "method": "POST",
        "url": url,
        "statusCode": status_code,
        "responseTimeMs": max(0, int(duration_ms)) if isinstance(duration_ms, (int, float)) else None,
        "occurredAt": datetime.now(timezone.utc).isoformat(),
    }


class CallBuffer:
    def __init__(self, send: Callable[[list], None], interval: float = 5.0,
                 flush_at: int = 500, min_gap: float = 1.0) -> None:
        self._send = send
        self._interval = interval
        self._flush_at = flush_at
        self._min_gap = min_gap
        self._last_sent = float("-inf")
        self._queue: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._drained = threading.Event()
        self._sending = False
        self._started = False

    def record(self, call: Optional[dict]) -> None:
        if call is None:
            return
        with self._lock:
            if len(self._queue) >= MAX_BUFFER:
                return
            self._queue.append(call)
            full = len(self._queue) >= self._flush_at
            if not self._started:
                self._started = True
                try:
                    threading.Thread(target=self._run, name="mnfst-tracking", daemon=True).start()
                except Exception:
                    self._started = False
        if full:
            self._wake.set()

    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def flush(self, timeout: float) -> None:
        """Send everything buffered now; wait at most `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while (self.size() > 0 or self._sending) and self._started:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._drained.clear()
            self._wake.set()
            self._drained.wait(remaining)

    def _run(self) -> None:
        while True:
            self._wake.wait(self._interval)
            self._wake.clear()
            self._drain()
            self._drained.set()

    def _drain(self) -> None:
        while True:
            # Wait out the gap before taking the batch, so calls arriving
            # meanwhile ride in it.
            wait = self._last_sent + self._min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            with self._lock:
                batch = [self._queue.popleft() for _ in range(min(MAX_BATCH, len(self._queue)))]
                self._sending = bool(batch)
            if not batch:
                return
            try:
                for attempt in range(2):  # one retry, then the batch is dropped
                    if attempt:
                        wait = self._last_sent + self._min_gap - time.monotonic()
                        if wait > 0:
                            time.sleep(wait)
                    self._last_sent = time.monotonic()
                    try:
                        self._send(batch)
                        break
                    except Exception:
                        pass
            finally:
                self._sending = False
