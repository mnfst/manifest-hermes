"""Repair a rejected tool call through the Manifest heal API. Standard library only.

A failure comes in as (tool_name, args, error_message); the served patch comes
back as a `Patch` the caller retries with at once and then reports on. One heal
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
from typing import Any, Callable, Optional

VERSION = "0.4.0"  # x-release-please-version
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


def healed_args(args: dict, body: dict) -> dict:
    """The arguments to retry the tool with.

    The served body is the whole healed body, not an overlay: an operation that
    *removes* or *moves* an argument expresses itself as that key's absence, so
    layering the body over the original arguments would resurrect exactly the
    argument the patch took out. The body therefore wins, and only the
    credential-named fields withheld from the capture are put back - the server
    never saw them, so it could not have echoed them.
    """
    withheld = {k: v for k, v in args.items() if is_secret_field(k) and k not in body}
    return {**body, **withheld}


def _unwrap_error(value: Any, depth: int = 4) -> Optional[dict]:
    """The tool's own error object, however deeply Hermes wrapped it.

    Hermes hands the plugin `{"error": <object or string>}`, and what it carries
    may be an envelope again: another `{"error": ...}`, or a `message` that is
    itself the stringified error. Each layer is peeled until the object holding
    the real message is in hand, because the `type`/`param`/`code` beside that
    message are identity inputs on the backend - collapsing them to a bare
    message changes the fingerprint and the capture lands on the wrong issue.
    """
    if depth <= 0:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    # An inner envelope always wins over the wrapper it arrived in.
    nested = _unwrap_error(value.get("error"), depth - 1)
    if nested is not None:
        return nested
    message = value.get("message")
    if isinstance(message, str) and message:
        encoded = _unwrap_error(message, depth - 1)
        return encoded if encoded is not None else value
    # No message, but code/type/param are identity inputs on their own: an
    # error object that names them is still the object to send.
    if any(value.get(key) for key in ("code", "type", "param")):
        return value
    return None


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
            err = _unwrap_error(parsed.get("error"))
            if err is not None:
                return {"error": err}
    return {"error": {"message": error_message}}


def tool_url(server: str, tool_name: str, host: str) -> str:
    """The failing call's URL.

    Manifest reads the service from the host and the endpoint from the path, so
    the MCP server's own host names the service and each tool is its own
    endpoint. Only servers reached over HTTP have one: a stdio server is a local
    process with no address, and the plugin leaves its tools alone.

    The router's real path is not used: it carries a per-agent session id, which
    would give every agent a different endpoint and collapse all of its tools
    into one.
    """
    return f"https://{host}/{tool_name}"


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

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every call carries MNFST_KEY: a redirect would hand it to whatever host it names,
    over plain http as readily as https. A 3xx is answered as the error it is instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


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

    def _call(self, method: str, path: str, body: dict,
              timeout: Optional[float] = None) -> tuple[int, Any]:
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                         headers=self._headers(), method=method)
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as response:
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

    def send_requests(self, calls: list) -> None:
        """Send one batch of tracked tool calls to POST /v1/requests.

        Raises only when resending could help (network error, timeout, 429,
        5xx), so the buffer retries once; any other answer, including 404 from
        a server that predates the route, is final. A disabled project pauses
        sending like healing.
        """
        if not calls or not self.enabled():
            return
        try:
            status, body = self._call("POST", "/v1/requests", {"requests": calls}, timeout=5.0)
        except Exception as exc:
            raise RuntimeError("tracked calls not delivered") from exc
        if status == 403 and isinstance(body, dict) and body.get("error") == "project_disabled":
            self._disabled_until = self.clock() + DISABLE_SECONDS
        elif status == 429 or status >= 500:
            raise RuntimeError(f"tracked calls refused ({status})")

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

# --- local measurement sink (plugin-side only; no prompt/runtime changes) -----
# Every heal attempt, verdict, and retry outcome appends one JSON line to
# HERMES_TRACE_DIR (default ~/.hermes/logs/hermes-trace). That stream is what
# measures potential: attempts -> patches -> retries succeeded. Arguments are
# logged as they travel: credential-named fields withheld, like the capture.

def _trace_sink() -> str:
    return os.environ.get("HERMES_TRACE_DIR") or str(
        Path.home() / ".hermes" / "logs" / "hermes-trace")


def _trace_emit(event: str, data: dict) -> None:
    try:
        path = _trace_sink()
        record = {"ts": time.time(), "event": event, "source": "manifest-plugin"}
        record.update(data)
        Path(path).mkdir(parents=True, exist_ok=True)
        with open(Path(path) / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass


@dataclass
class Patch:
    """A served patch: the healed body and the attempt it will be reported on."""
    tool_name: str
    body: dict
    attempt_id: Optional[str]


HEAL_WORKERS = 4


class Healer:
    def __init__(self, api: HealClient, timeout: float = 20.0) -> None:
        self.api = api
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=HEAL_WORKERS, thread_name_prefix="mnfst-heal")
        # One slot per worker: a failure that finds every worker busy is passed through
        # rather than queued, so a slow API can never pile up heals that run long after
        # the call they were for has moved on.
        self._slots = threading.BoundedSemaphore(HEAL_WORKERS)

    def on_error(self, tool_name: str, args: dict, error_message: str,
                 raw_result: Any = None, server: str = "mcp",
                 host: Optional[str] = None, response_time_ms: Any = None) -> Optional[Patch]:
        """Send the failure; the served patch, or None when there is nothing to retry.

        A server with no HTTP address (stdio) is never sent: Colibri heals HTTP
        services, and the call would need a made-up URL.
        """
        if not host:
            return None
        attempt = {"tool": tool_name, "server": server, "args": traveling_body(args),
                   "error": error_message}
        try:
            payload = heal_payload(trace_id=uuid.uuid4().hex, tool_name=tool_name, server=server,
                                   args=args, error_message=error_message, host=host,
                                   raw_result=raw_result,
                                   response_time_ms=response_time_ms if isinstance(response_time_ms, (int, float)) else 0)
            if not self._slots.acquire(blocking=False):
                _trace_emit("heal_attempt", {**attempt, "verdict": "busy"})
                return None
            try:
                future = self._pool.submit(self._heal, payload)
            except Exception:
                self._slots.release()
                raise
            result = future.result(timeout=self.timeout)
        except FutureTimeout:
            # A heal that never started never reaches _heal's release: give its slot back here.
            if future.cancel():
                self._slots.release()
            _trace_emit("heal_attempt", {**attempt, "verdict": "timeout"})
            return None
        except Exception:
            _trace_emit("heal_attempt", {**attempt, "verdict": "transport_error"})
            return None
        if not isinstance(result, dict) or result.get("status") not in ("patched", "unverified"):
            verdict = result.get("status") if isinstance(result, dict) else "unusable_response"
            _trace_emit("heal_attempt", {**attempt, "verdict": verdict})
            return None
        healed = result.get("healedRequest")
        body = healed.get("body") if isinstance(healed, dict) else None
        if not isinstance(body, dict):
            _trace_emit("heal_attempt", {**attempt, "verdict": "patch_missing_body"})
            return None
        _trace_emit("heal_attempt", {**attempt, "verdict": result.get("status"), "patch": body,
                                     "attempt_id": result.get("healAttemptId")})
        return Patch(tool_name, body, result.get("healAttemptId"))

    def _heal(self, payload: dict) -> Optional[dict]:
        try:
            return self.api.heal(payload)
        finally:
            self._slots.release()

    def outcome(self, patch: Patch, ok: bool, error: Any = None) -> None:
        """The retry ran: report what the tool answered with the patched arguments."""
        if ok:
            _trace_emit("heal_outcome", {"tool": patch.tool_name, "attempt_id": patch.attempt_id,
                                         "patched_args": traveling_body(patch.body),
                                         "retry_result": "success", "status_code": 200})
            self._report(patch.attempt_id, 200)
            return
        _trace_emit("heal_outcome", {"tool": patch.tool_name, "attempt_id": patch.attempt_id,
                                     "patched_args": traveling_body(patch.body),
                                     "retry_result": "failed", "error": error, "status_code": 422})
        self._report(patch.attempt_id, 422, error)

    def not_attempted(self, patch: Patch, reason: str = NOT_ATTEMPTED) -> None:
        """The retry never reached the tool: no evidence about the patch either way.

        `NOT_ATTEMPTED` says the patch changed nothing; any other reason is the
        transport error that kept the retry from running.
        """
        _trace_emit("heal_outcome", {"tool": patch.tool_name, "attempt_id": patch.attempt_id,
                                     "patched_args": traveling_body(patch.body),
                                     "retry_result": "not_attempted", "reason": reason})
        self._report(patch.attempt_id, 0, reason)

    def _report(self, attempt_id: Optional[str], status_code: int, error: Any = None) -> None:
        if not attempt_id:
            return
        try:
            self.api.report(attempt_id, status_code, error)
        except Exception:
            pass
