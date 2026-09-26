"""manifest: repair rejected tool calls with Manifest. Hermes Agent plugin, no dependencies.

A failed MCP tool call is sent to the Manifest heal API. When corrected
arguments come back, the plugin re-invokes the tool itself with the patched
arguments, through Hermes' own call path so every hook and guard runs again,
and returns the healed result to the model. The agent never sees Manifest, a
retry instruction, or the repair — only the tool's response, healed or not.
One heal and one internal retry per failure, fail-open.

Only MCP tools on servers reached over HTTP are repaired and tracked. Built-in
and local tools, and stdio MCP servers (a local process, no address), are never
touched.

Captures carry statusCode 418, the sentinel for a tool call: 418 is permanently
reserved (RFC 2324 / RFC 9110), so no real API failure can collide with the
synthetic envelope. The `x-manifest-tool-call: mcp` and `x-manifest-mcp-server`
headers mark the row for backend segmentation.

Environment: MNFST_KEY (required) and MNFST_URL (optional).
MNFST_ALLOWLIST / MNFST_DENYLIST (optional, comma-separated) keep tool calls out
of Manifest entirely: neither healed nor tracked. An entry is a domain
(linear.app, subdomains included) or a domain with a tool name
(mcp.linear.app/list_issues); the denylist wins (see manifest_filter). Every heal
attempt and outcome is appended to HERMES_TRACE_DIR for measurement,
credentials withheld.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

try:  # loaded as a package by Hermes
    from .manifest_heal import (DEFAULT_URL, NOT_ATTEMPTED, TOOL_CALL_STATUS, HealClient, Healer,
                                error_body, healed_args, tool_url)
    from .manifest_tracking import CallBuffer, tracked_call
    from .manifest_filter import Rules, is_excluded, pick_rules
except ImportError:  # loaded flat, plugin directory on sys.path
    from manifest_heal import (DEFAULT_URL, NOT_ATTEMPTED, TOOL_CALL_STATUS,  # type: ignore
                               HealClient, Healer, error_body, healed_args, tool_url)
    from manifest_tracking import CallBuffer, tracked_call  # type: ignore
    from manifest_filter import Rules, is_excluded, pick_rules  # type: ignore

logger = logging.getLogger(__name__)

# The identity fields Hermes threads through every tool hook; the internal
# retry carries the same ones so it is observed as part of the same call.
_CALL_IDS = ("task_id", "session_id", "tool_call_id", "turn_id", "api_request_id")


def _tool_entry(tool_name: str):
    """The Hermes registry entry for a tool, or None when it cannot be read."""
    try:
        from tools.registry import registry  # Hermes' own tool registry
        return registry.get_entry(tool_name)
    except Exception:
        return None


_HOSTS: Dict[str, Optional[str]] = {}


def _netloc(value: str) -> Optional[str]:
    """The bare host of a URL or a host string: no scheme, path, userinfo or case."""
    url = value if "://" in value else "//" + value
    return urlsplit(url).netloc.split("@")[-1].lower() or None




def _mcp_server_host(server: str) -> Optional[str]:
    """The host of a configured MCP server, from Hermes' own configuration.

    Cached: the lookup parses the config file, and the answer cannot change
    without a restart.
    """
    if server in _HOSTS:
        return _HOSTS[server]
    host = None
    try:
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly() or {}).get("mcp_servers") or {}).get(server)
        url = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(url, str) and url:
            host = _netloc(url)
    except Exception:
        host = None
    _HOSTS[server] = host
    return host


def external_tool_filter(entry_of: Callable[[str], Any] = _tool_entry):
    """The service a tool calls, or None when the tool is local.

    Manifest learns a contract from a service's own rejections. A local tool
    has none, and rewriting the arguments of a shell or file tool is not
    something a remote service should do.

    An `mcp-<server>` toolset marks the repairable ones: that is how Hermes
    registers every MCP server's tools, whatever the server. The server names
    the service. Everything else counts as local, including a built-in tool
    that reaches an API, so an unreadable registry heals nothing rather than
    everything.
    """
    def service_of(tool_name: str) -> Optional[str]:
        entry = entry_of(tool_name)
        toolset = getattr(entry, "toolset", None) if entry is not None else None
        if toolset and toolset.startswith("mcp-"):
            return toolset[len("mcp-"):].strip("-_") or "mcp"
        return None

    return service_of


def _looks_failed(result: Any) -> bool:
    """Whether a tool result reads as an error (same derivation Hermes uses)."""
    if not isinstance(result, str):
        return True
    try:
        parsed = json.loads(result)
    except Exception:
        return '"error"' in result
    if isinstance(parsed, dict):
        if parsed.get("error"):
            return True
        if parsed.get("isError") is True:
            return True
    return False


def _error_text(result: Any) -> str:
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, str):
                return err
            if err is not None:
                return json.dumps(err)
    except Exception:
        pass
    return "tool error"


def _hermes_dispatch() -> Callable[[str, dict, dict], Any]:
    """Re-invoke a tool the way Hermes invokes it for the model.

    `handle_function_call` runs the `pre_tool_call` policy hooks, edit approval
    and the tool-execution middleware, so server-dictated arguments get every
    check the original call got and a policy plugin sees the retry. The call's
    identity fields ride along so it is observed as part of the same call; the
    agent loop owns `post_tool_call` and fires it once, with the final result.
    The request middleware is skipped: the arguments in hand are already its
    output. An older Hermes without that entry point falls back to the bare
    registry.
    """
    def dispatch(name: str, args: dict, ids: dict):
        try:
            from model_tools import handle_function_call
        except ImportError:
            from tools.registry import registry  # Hermes' own tool registry
            return registry.dispatch(name, args)
        return handle_function_call(name, args, skip_tool_request_middleware=True,
                                    **{k: v for k, v in ids.items() if k in _CALL_IDS and v})
    return dispatch


# The retry re-enters transform_tool_result (it is a real Hermes call). While
# one is running on this call, the nested hook must not heal the retry's own
# failure: one heal and one retry per failure.
_RETRYING: ContextVar[bool] = ContextVar("mnfst_retrying", default=False)


def build_callbacks(healer: Healer,
                    service_of: Optional[Callable[[str], Optional[str]]] = None,
                    host_of: Callable[[str], Optional[str]] = _mcp_server_host,
                    dispatch: Optional[Callable[[str, dict, dict], Any]] = None,
                    tracker: Any = None,
                    allow: Optional[Rules] = None,
                    deny: Rules = (),
                    ) -> Dict[str, Callable[..., Any]]:
    """The transform_tool_result callback: capture, heal, re-invoke, report.

    `dispatch(tool_name, args, call_ids)` is the re-invocation seam (Hermes'
    own call path by default); tests inject a fake here. `tracker` receives one
    metadata record per MCP tool call that is not sent to heal (see
    manifest_tracking); None records nothing. `allow` / `deny` are the parsed
    allowlist and denylist (see manifest_filter); `allow` is None when none was given.
    """
    service_of = service_of or external_tool_filter()
    dispatch = dispatch or _hermes_dispatch()

    def retry(tool_name: str, args: dict, ids: dict) -> Any:
        token = _RETRYING.set(True)
        try:
            return dispatch(tool_name, args, ids)
        finally:
            _RETRYING.reset(token)

    def track(tool_name: str, server: str, host: str, status_code: int, duration_ms: Any) -> None:
        if tracker is not None:
            tracker.record(tracked_call(server, tool_name, host, status_code, duration_ms))

    def on_result(tool_name: str = "", args: Any = None, result: Any = None,
                  status: Optional[str] = None, error_message: Optional[str] = None,
                  duration_ms: Any = None, **ids: Any) -> Optional[str]:
        try:
            if _RETRYING.get():
                return None   # the plugin's own retry: neither healed nor tracked
            server = service_of(tool_name)
            if server is None:
                return None   # a local tool: no service, never reported
            host = host_of(server)
            if not host:
                return None   # a stdio server: a local process, neither healed nor tracked
            if is_excluded(allow, deny, tool_url(server, tool_name, host)):
                return None   # kept out by the allowlist or denylist: neither healed nor tracked
            if status != "error":
                track(tool_name, server, host, 200, duration_ms)
                return None
            if not healer.api.enabled():
                # Healing is paused: the failure is not sent to heal, so it is tracked.
                track(tool_name, server, host, TOOL_CALL_STATUS, duration_ms)
                return None
            if not isinstance(result, str) or not isinstance(args, dict):
                return None
            patch = healer.on_error(tool_name, args, error_message or "", raw_result=result,
                                    server=server, host=host, response_time_ms=duration_ms)
            if patch is None:
                return None
            merged = healed_args(args, patch.body)
            if merged == args or not merged:
                # Nothing changed, or nothing is left to send: the patch was never
                # exercised, and a report must not claim otherwise.
                healer.not_attempted(patch, NOT_ATTEMPTED)
                return None
            try:
                healed = retry(tool_name, merged, ids)
            except Exception as exc:
                healer.not_attempted(patch, f"{type(exc).__name__}: {exc}")
                logger.debug("manifest internal retry raised: %s", exc)
                return None
            if _looks_failed(healed):
                # The rejection travels in the same shape as the capture, so the
                # backend can tell a recurrence from a different error.
                healer.outcome(patch, False, error_body(_error_text(healed), healed))
                return None  # the original error result stands
            healer.outcome(patch, True)
            return healed if isinstance(healed, str) else json.dumps(healed)
        except Exception as exc:
            logger.debug("manifest transform_tool_result failed open: %s", exc)
            return None

    return {"transform_tool_result": on_result}


def register(ctx) -> None:
    config_key = os.environ.get("MNFST_KEY", "").strip()
    if not config_key:
        logger.warning("manifest plugin: MNFST_KEY is not set; nothing registered")
        return
    url = os.environ.get("MNFST_URL", "").strip() or DEFAULT_URL
    client = HealClient(config_key, url)
    healer = Healer(client)
    tracker = CallBuffer(client.send_requests)
    atexit.register(tracker.flush, 2.0)   # at most two seconds at exit
    allow, bad_allow = pick_rules(None, os.environ.get("MNFST_ALLOWLIST"))
    deny, bad_deny = pick_rules(None, os.environ.get("MNFST_DENYLIST"))
    if bad_allow or bad_deny:
        logger.warning("manifest plugin: ignoring unreadable allowlist/denylist entries: %s",
                       ", ".join(bad_allow + bad_deny))
    callbacks = build_callbacks(healer, tracker=tracker, allow=allow, deny=deny or ())
    ctx.register_hook("transform_tool_result", callbacks["transform_tool_result"])
    logger.info("manifest plugin: tool-call repair registered")
