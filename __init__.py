"""manifest: repair rejected tool calls with Manifest. Hermes Agent plugin, no dependencies.

A failed tool result is sent to the Manifest heal API. When a corrected set
of arguments comes back, the model is told to call the tool again, and the
retry runs with the corrected arguments. One heal, one retry, fail-open.

Only MCP tools are repaired, from any MCP server, plus any name prefix
listed in MNFST_TOOLS. Built-in tools are never touched.

Environment: MNFST_KEY (required), MNFST_URL (optional),
MNFST_HEAL_TIMEOUT seconds (default 20), MNFST_TOOLS (comma-separated name
prefixes to treat as external). Optional, separate in-process HTTP healing
requires the `mnfst` package and MNFST_HEAL_HTTP=1.
"""
from __future__ import annotations

import logging
import os
import json
from typing import Any, Callable, Dict, Optional

try:  # loaded as a package by Hermes
    from .manifest_heal import DEFAULT_URL, RETRY_LINE, HealClient, Healer
except ImportError:  # loaded flat, plugin directory on sys.path
    from manifest_heal import DEFAULT_URL, RETRY_LINE, HealClient, Healer  # type: ignore

logger = logging.getLogger(__name__)


def _tool_entry(tool_name: str):
    """The Hermes registry entry for a tool, or None when it cannot be read."""
    try:
        from tools.registry import registry  # Hermes' own tool registry
        return registry.get_entry(tool_name)
    except Exception:
        return None


def _mcp_service_url(server: str) -> Optional[str]:
    """Read the current profile's MCP config and send only its HTTP(S) origin."""
    try:
        from urllib.parse import urlsplit
        from tools.mcp_tool_config import _load_mcp_config
        entry = _load_mcp_config().get(server)
        url = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(url, str) and url:
            parts = urlsplit(url)
            if parts.scheme in ("http", "https") and parts.hostname:
                return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1].lower()}"
    except Exception:
        pass
    return None


def external_tool_filter(extra: tuple = (), entry_of: Callable[[str], Any] = _tool_entry):
    """The MCP server a tool calls, or None for a built-in tool.

    Manifest learns a contract from a server's own rejections, over HTTP or
    stdio. Built-in shell and file tools remain outside this repair flow.

    An `mcp-<server>` toolset marks the repairable ones: that is how Hermes
    registers every MCP server's tools, whatever the server. The server names
    the service. Everything else is excluded, including a built-in tool
    that reaches an API, so an unreadable registry heals nothing rather than
    everything. MNFST_TOOLS is the explicit opt-in for a tool outside that
    rule; the prefix names its service.
    """
    def service_of(tool_name: str) -> Optional[str]:
        entry = entry_of(tool_name)
        toolset = getattr(entry, "toolset", None) if entry is not None else None
        if toolset and toolset.startswith("mcp-"):
            return toolset[len("mcp-"):] or None
        for prefix in extra:
            if tool_name.startswith(prefix):
                return prefix.strip("-_") or "tool"
        return None

    return service_of


def rewrite_result(result: str, tool_name: str) -> str:
    return result + "\n\n" + RETRY_LINE.format(tool=tool_name)


def build_callbacks(healer: Healer,
                    service_of: Optional[Callable[[str], Optional[str]]] = None,
                    service_url_of: Callable[[str], Optional[str]] = _mcp_service_url,
                    ) -> Dict[str, Callable[..., Any]]:
    service_of = service_of or external_tool_filter()

    def on_result(tool_name: str = "", args: Any = None, result: Any = None,
                  status: Optional[str] = None, error_message: Optional[str] = None,
                  session_id: Optional[str] = None, task_id: Optional[str] = None,
                  duration_ms: int = 0,
                  **_: Any) -> Optional[str]:
        try:
            if not session_id and not task_id:
                return None  # No identity to scope a later retry safely.
            if status != "error" or not isinstance(result, str) or not isinstance(args, dict):
                return None
            server = service_of(tool_name)
            if server is None:
                return None
            patched = healer.on_error(tool_name, args, error_message or "", raw_result=result,
                                      server=server, service_url=service_url_of(server),
                                      scope=json.dumps([session_id, task_id]),
                                      response_time_ms=duration_ms)
            return rewrite_result(result, tool_name) if patched is not None else None
        except Exception as exc:
            logger.debug("manifest transform_tool_result failed open: %s", exc)
            return None

    def on_request(tool_name: str = "", args: Any = None, session_id: Optional[str] = None,
                   task_id: Optional[str] = None, **_: Any) -> Optional[dict]:
        try:
            if not isinstance(args, dict):
                return None
            pending = healer.take(tool_name, args, scope=json.dumps([session_id, task_id]))
            if pending is None:
                return None
            return {"args": pending.args, "source": "manifest", "reason": "healed"}
        except Exception as exc:
            logger.debug("manifest tool_request failed open: %s", exc)
            return None

    def on_post(tool_name: str = "", args: Any = None, status: Optional[str] = None,
                error_message: Optional[str] = None, result: Any = None,
                session_id: Optional[str] = None, task_id: Optional[str] = None, **_: Any) -> None:
        try:
            if isinstance(args, dict):
                healer.outcome(tool_name, args, status, error_message,
                               scope=json.dumps([session_id, task_id]), raw_result=result)
        except Exception as exc:
            logger.debug("manifest post_tool_call failed open: %s", exc)
        return None

    return {"transform_tool_result": on_result, "tool_request": on_request, "post_tool_call": on_post}


def register(ctx) -> None:
    key = os.environ.get("MNFST_KEY", "").strip()
    if not key:
        logger.warning("manifest plugin: MNFST_KEY is not set; nothing registered")
        return
    url = os.environ.get("MNFST_URL", "").strip() or DEFAULT_URL
    timeout = float(os.environ.get("MNFST_HEAL_TIMEOUT", "20"))
    extra = tuple(t.strip() for t in os.environ.get("MNFST_TOOLS", "").split(",") if t.strip())
    healer = Healer(HealClient(key, url, timeout=timeout), timeout=timeout)
    callbacks = build_callbacks(healer, external_tool_filter(extra))
    ctx.register_hook("transform_tool_result", callbacks["transform_tool_result"])
    ctx.register_middleware("tool_request", callbacks["tool_request"])
    ctx.register_hook("post_tool_call", callbacks["post_tool_call"])
    logger.info("manifest plugin: tool-call repair registered")
    if os.environ.get("MNFST_HEAL_HTTP", "0") == "1":
        try:
            from mnfst import manifest  # optional: transport-level healing for in-process HTTP
        except ImportError:
            return
        try:
            manifest()
        except Exception as exc:
            logger.warning("manifest plugin: transport install skipped: %s", exc)
