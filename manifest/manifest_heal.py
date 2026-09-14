"""Repair a rejected tool call through the Manifest heal API.

Runtime-agnostic: a failure comes in as (tool_name, args, error_message), a
patch goes into a pending cache, and the runtime's pre-call seam asks for it
on the retry. One heal and one retry per failure; everything fails open.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, Optional

from mnfst.heal_api import NOT_ATTEMPTED, HealApi
from mnfst.wire import heal_payload

RETRY_LINE = ("Manifest prepared corrected arguments for {tool}. "
              "Call {tool} again with the same arguments to apply them.")


def key_of(tool_name: str, args: Any) -> str:
    return tool_name + "\x00" + json.dumps(args, sort_keys=True, default=str)


@dataclass
class Pending:
    tool_name: str
    args: dict            # the patched arguments
    attempt_id: Optional[str]
    deadline: float


class Healer:
    def __init__(self, api: HealApi, timeout: float = 20.0, ttl: float = 600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.api = api
        self.timeout = timeout
        self.ttl = ttl
        self.clock = clock
        self._pending: dict[str, Pending] = {}
        self._burned: dict[str, float] = {}     # key of patched args -> deadline
        self._applied: dict[str, Pending] = {}  # key of patched args -> pending, for outcome
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mnfst-heal")

    def on_error(self, tool_name: str, args: dict, error_message: str) -> Optional[dict]:
        with self._lock:
            if key_of(tool_name, args) in self._burned:
                return None
        try:
            payload = heal_payload(
                trace_id=uuid.uuid4().hex, method="POST", url=f"mcp://{tool_name}",
                headers={"content-type": "application/json"}, body=args, status_code=422,
                response_body={"error": error_message}, truncated=False, response_time_ms=0)
            future = self._pool.submit(self.api.heal, payload)
            result = future.result(timeout=self.timeout)
        except FutureTimeout:
            return None
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("status") not in ("patched", "unverified"):
            return None
        healed = result.get("healedRequest")
        body = healed.get("body") if isinstance(healed, dict) else None
        if not isinstance(body, dict):
            return None
        with self._lock:
            self._pending[key_of(tool_name, args)] = Pending(
                tool_name, body, result.get("healAttemptId"), self.clock() + self.ttl)
        return body

    def take(self, tool_name: str, args: dict) -> Optional[Pending]:
        self.expire()
        with self._lock:
            pending = self._pending.pop(key_of(tool_name, args), None)
            if pending is None:
                return None
            patched_key = key_of(tool_name, pending.args)
            self._burned[patched_key] = self.clock() + self.ttl
            self._applied[patched_key] = pending
        return pending

    def outcome(self, tool_name: str, args: dict, status: Optional[str],
                error_message: Optional[str]) -> None:
        with self._lock:
            pending = self._applied.pop(key_of(tool_name, args), None)
        if pending is None:
            return
        if status == "ok":
            self._report(pending.attempt_id, 200)
        else:
            self._report(pending.attempt_id, 422, {"error": error_message or "tool error"})

    def expire(self) -> None:
        now = self.clock()
        with self._lock:
            dropped = [k for k, p in self._pending.items() if p.deadline <= now]
            reports = [self._pending.pop(k) for k in dropped]
            for k in [k for k, d in self._burned.items() if d <= now]:
                self._burned.pop(k, None)
                self._applied.pop(k, None)
        for pending in reports:
            self._report(pending.attempt_id, 0, NOT_ATTEMPTED)

    def _report(self, attempt_id: Optional[str], status_code: int, error: Any = None) -> None:
        if not attempt_id:
            return
        try:
            self.api.report_outcome(attempt_id, status_code, error)
        except Exception:
            pass
