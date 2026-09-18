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
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

VERSION = "0.2.0"
DEFAULT_URL = "https://api.manifest.build"
# Captures of MCP tool calls carry a dedicated sentinel status so the backend can
# segment them from plain HTTP traffic. 418 is permanently reserved (RFC 2324 /
# RFC 9110), so no real API failure can ever collide with the synthetic envelope;
# the code travels inside the capture body, never as a wire status.
TOOL_CALL_STATUS = 418
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
            # OpenAI-shaped errors travel untouched too: code/param/type are
            # identity inputs on the backend (dropping them changes the fingerprint).
            err = parsed.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str) and err["message"]:
                return parsed
    return {"error": {"message": error_message}}


def tool_url(server: str, tool_name: str, host: Optional[str] = None) -> str:
    """The failing call's URL.

    Manifest reads the service from the host and the endpoint from the path, so
    the MCP server's own host names the service and each tool is its own
    endpoint. The server's real host is used when it can be read from the Hermes
    configuration; the `mcp` scheme is the fallback when it cannot.

    The router's real path is not used: it carries a per-agent session id, which
    would give every agent a different endpoint and collapse all of its tools
    into one.
    """
    if host:
        return f"https://{host}/{tool_name}"
    return f"mcp://{server}/{tool_name}"


def tool_headers(server: str) -> dict:
    """A tool call is not a plain HTTP request, and the headers say so.

    The path is synthesized from the tool name, so a reader who would otherwise
    try the URL gets told what this row is and which MCP server produced it.
    """
    return {"content-type": "application/json", "x-manifest-tool-call": "mcp",
            "x-manifest-mcp-server": server}


def heal_payload(*, trace_id: str, tool_name: str, server: str, args: Any, error_message: str,
                 host: Optional[str] = None, raw_result: Any = None,
                 response_time_ms: int = 0) -> dict:
    return {
        "traceId": trace_id,
        "request": {"method": "POST", "url": tool_url(server, tool_name, host),
                    "headers": tool_headers(server), "body": traveling_body(args)},
        "response": {"statusCode": TOOL_CALL_STATUS, "body": error_body(error_message, raw_result),
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
        # Same client convention as the SDKs: mnfst-node/x, mnfst-python/x.
        return {"authorization": f"Bearer {self.key}", "content-type": "application/json",
                "user-agent": f"mnfst-hermes/{VERSION}"}

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
        if os.environ.get("MNFST_HEAL_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
            _trace_emit("heal_debug", {"http_status": status, "response": body, "payload": payload})
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


# --- local measurement sink (plugin-side only; no prompt/runtime changes) -----
# Every heal attempt, verdict, and retry outcome appends one JSON line to
# HERMES_TRACE_DIR (default ~/.hermes/logs/hermes-trace). That stream is what
# measures potential: attempts -> patches -> retries succeeded.

def _trace_sink() -> Optional[str]:
    if os.environ.get("MNFST_HEAL_LOG", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return os.environ.get("HERMES_TRACE_DIR") or str(
        Path.home() / ".hermes" / "logs" / "hermes-trace")


def _trace_emit(event: str, data: dict) -> None:
    try:
        path = _trace_sink()
        if not path:
            return
        record = {"ts": time.time(), "event": event, "source": "manifest-plugin"}
        record.update(data)
        with open(Path(path) / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
                 raw_result: Any = None, server: str = "mcp",
                 host: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            if key_of(tool_name, args) in self._burned:
                return None
        try:
            payload = heal_payload(trace_id=uuid.uuid4().hex, tool_name=tool_name, server=server,
                                   args=args, error_message=error_message, host=host,
                                   raw_result=raw_result)
            result = self._pool.submit(self.api.heal, payload).result(timeout=self.timeout)
        except FutureTimeout:
            _trace_emit("heal_attempt", {"tool": tool_name, "server": server, "args": args,
                                         "error": error_message, "verdict": "timeout"})
            return None
        except Exception:
            _trace_emit("heal_attempt", {"tool": tool_name, "server": server, "args": args,
                                         "error": error_message, "verdict": "transport_error"})
            return None
        if not isinstance(result, dict) or result.get("status") not in ("patched", "unverified"):
            verdict = result.get("status") if isinstance(result, dict) else "unusable_response"
            _trace_emit("heal_attempt", {"tool": tool_name, "server": server, "args": args,
                                         "error": error_message, "verdict": verdict})
            return None
        healed = result.get("healedRequest")
        body = healed.get("body") if isinstance(healed, dict) else None
        if not isinstance(body, dict):
            _trace_emit("heal_attempt", {"tool": tool_name, "server": server, "args": args,
                                         "error": error_message, "verdict": "patch_missing_body"})
            return None
        _trace_emit("heal_attempt", {"tool": tool_name, "server": server, "args": args,
                                     "error": error_message, "verdict": result.get("status"),
                                     "patch": body, "attempt_id": result.get("healAttemptId")})
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
            _trace_emit("heal_outcome", {"tool": tool_name, "attempt_id": pending.attempt_id,
                                         "patched_args": pending.args,
                                         "retry_result": "success", "status_code": 200})
            self._report(pending.attempt_id, 200)
        else:
            _trace_emit("heal_outcome", {"tool": tool_name, "attempt_id": pending.attempt_id,
                                         "patched_args": pending.args,
                                         "retry_result": "failed",
                                         "error": error_message or "tool error", "status_code": 422})
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
