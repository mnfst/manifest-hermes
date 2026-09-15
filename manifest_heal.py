"""Repair a rejected tool call through the Manifest heal API. Standard library only.

A failure comes in as (tool_name, args, error_message); a patch goes into a
pending cache; the runtime's pre-call seam asks for it on the retry. One heal
and one retry per failure; everything fails open.

The heal client mirrors the mnfst SDK's contract: bearer project key,
POST /v1/heal for a capture, PATCH /v1/heal-attempts/<id> for the outcome,
credential-named fields withheld before anything leaves the process, and a
five-minute pause when the project is disabled server-side.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

VERSION = "0.1.0"
DEFAULT_URL = "https://api.manifest.build"
RETRY_LINE = ("Manifest prepared corrected arguments for {tool}. "
              "Call {tool} again with the same arguments to apply them.")
NOT_ATTEMPTED = "replay_not_attempted"
DISABLE_SECONDS = 300.0
MAX_INFLIGHT_REPORTS = 64

# --- masking (same names as the mnfst SDK) -----------------------------------

_SECRET_NAMES = {
    "api_key", "apikey", "api_token", "key", "token", "access_token", "refresh_token",
    "auth", "authorization", "signature", "sig", "secret", "client_secret", "password",
    "session", "session_id", "bearer", "jwt", "id_token", "auth_token", "pwd", "passwd",
    "private_key",
}
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")


def _normalize(name: str) -> str:
    return _CAMEL.sub(r"\1_\2", str(name)).replace("-", "_").lower().removeprefix("x_")


def is_secret_field(name: Any) -> bool:
    return isinstance(name, str) and _normalize(name) in _SECRET_NAMES


def traveling_body(body: Any) -> Any:
    """The body as it travels: credential-named top-level keys withheld."""
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if not is_secret_field(k)}
    return body


def error_body(error_message: str, raw_result: Any = None) -> Any:
    """The rejection as the heal API reads it.

    A validator's own payload travels untouched: the server recognizes Zod
    (`issues`/`errors`) and Pydantic (`detail`) and addresses the bad argument
    by its field path. Anything else travels as a message envelope, the shape
    the server's generic extractor reads; a bare string under `error` would
    fall through to its stringify fallback and carry no structure at all.
    """
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("issues", "errors", "detail"):
                value = parsed.get(key)
                if isinstance(value, list) and value:
                    return parsed
    return {"error": {"message": error_message}}


def heal_payload(*, trace_id: str, tool_name: str, args: Any, error_message: str,
                 raw_result: Any = None, response_time_ms: int = 0) -> dict:
    return {
        "traceId": trace_id,
        "request": {"method": "POST", "url": f"mcp://{tool_name}",
                    "headers": {"content-type": "application/json"}, "body": traveling_body(args)},
        "response": {"statusCode": 422, "body": error_body(error_message, raw_result),
                     "truncated": False},
        "responseTimeMs": int(response_time_ms),
    }


# --- heal client --------------------------------------------------------------

class HealClient:
    def __init__(self, key: str, url: str = DEFAULT_URL, timeout: float = 20.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.key = key
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.clock = clock
        self._disabled_until = 0.0
        self._lock = threading.Lock()
        self._pending: list[threading.Thread] = []

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self.key}", "content-type": "application/json",
                "user-agent": f"manifest-hermes/{VERSION}"}

    def enabled(self) -> bool:
        return self.clock() >= self._disabled_until

    def _call(self, method: str, path: str, body: dict) -> tuple[int, Any]:
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                         headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, (json.loads(raw) if raw else None)
            except ValueError:
                return error.code, None

    def heal(self, payload: dict) -> Optional[dict]:
        """The server's answer for a capture, or None (no patch, disabled, unreachable)."""
        if not self.enabled():
            return None
        status, body = self._call("POST", "/v1/heal", payload)
        if status == 403 and isinstance(body, dict) and body.get("error") == "project_disabled":
            self._disabled_until = self.clock() + DISABLE_SECONDS
            return None
        return body if status == 200 and isinstance(body, dict) else None

    def report(self, attempt_id: str, status_code: int, error: Any = None) -> None:
        """Fire and forget, bounded: a lost report costs one learning signal, never the tool loop."""
        if status_code == 0:
            body = {"failure": {"kind": "not_attempted" if error == NOT_ATTEMPTED else "transport_error",
                                "message": error or NOT_ATTEMPTED}}
        else:
            body = {"response": {"statusCode": status_code}}
            if error is not None:
                body["response"].update(body=error, truncated=False)

        def send() -> None:
            try:
                self._call("PATCH", f"/v1/heal-attempts/{attempt_id}", body)
            except Exception:
                pass

        thread = threading.Thread(target=send, daemon=True, name="mnfst-report")
        with self._lock:
            self._pending = [t for t in self._pending if t.is_alive()]
            if len(self._pending) >= MAX_INFLIGHT_REPORTS:
                return
            self._pending.append(thread)
            thread.start()


# --- the loop -----------------------------------------------------------------

def key_of(tool_name: str, args: Any) -> str:
    return tool_name + "\x00" + json.dumps(args, sort_keys=True, default=str)


@dataclass
class Pending:
    tool_name: str
    args: dict            # the patched arguments
    attempt_id: Optional[str]
    deadline: float


class Healer:
    def __init__(self, api: HealClient, timeout: float = 20.0, ttl: float = 600.0,
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

    def on_error(self, tool_name: str, args: dict, error_message: str,
                 raw_result: Any = None) -> Optional[dict]:
        with self._lock:
            if key_of(tool_name, args) in self._burned:
                return None
        try:
            payload = heal_payload(trace_id=uuid.uuid4().hex, tool_name=tool_name, args=args,
                                   error_message=error_message, raw_result=raw_result)
            result = self._pool.submit(self.api.heal, payload).result(timeout=self.timeout)
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
            self.api.report(attempt_id, status_code, error)
        except Exception:
            pass
