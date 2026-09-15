"""manifest: repair rejected tool calls with Manifest. Hermes Agent plugin, no dependencies.

A failed tool result is sent to the Manifest heal API. When a corrected set
of arguments comes back, the model is told to call the tool again, and the
retry runs with the corrected arguments. One heal, one retry, fail-open.

Only calls to external services are repaired: any MCP server's tools, and
built-in tools that declare the credential they need (web_search and the
like), plus any name prefix listed in MNFST_TOOLS. A local tool (terminal,
file, memory) is never touched.

Environment: MNFST_KEY (required), MNFST_URL (optional),
MNFST_HEAL_TIMEOUT seconds (default 20), MNFST_TOOLS (comma-separated name
prefixes to treat as external). If the optional `mnfst` package is
installed, HTTP calls made inside the Hermes process are healed at the
transport level as well; MNFST_HEAL_HTTP=0 skips that.
"""
from __future__ import annotations

import logging
import os
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


def external_tool_filter(extra: tuple = (), entry_of: Callable[[str], Any] = _tool_entry):
    """The service a tool calls, or None when the tool is local.

    Manifest learns a contract from a service's own rejections. A local tool
    has none, and rewriting the arguments of a shell or file tool is not
    something a remote service should do. Two signals mark a call as external,
    and neither is tied to one vendor:

      * an `mcp-<server>` toolset, which is how Hermes registers every MCP
        server's tools, whatever the server;
      * a non-empty `requires_env`, which is how a built-in tool declares the
        credential it needs to reach its API (`web_search`, `web_extract`,
        and the other API-backed tools). Local tools declare none.

    A tool that matches neither counts as local and is left alone, so an
    unreadable registry heals nothing rather than everything. MNFST_TOOLS adds
    name prefixes for a tool these signals miss; the prefix names its service.
    """
    def service_of(tool_name: str) -> Optional[str]:
        entry = entry_of(tool_name)
        toolset = getattr(entry, "toolset", None) if entry is not None else None
        if toolset and toolset.startswith("mcp-"):
            return toolset[len("mcp-"):].strip("-_") or "mcp"
        if entry is not None and getattr(entry, "requires_env", None) and toolset:
            return toolset.strip("-_")
        for prefix in extra:
            if tool_name.startswith(prefix):
                return prefix.strip("-_") or "tool"
        return None

    return service_of


def rewrite_result(result: str, tool_name: str) -> str:
    return result + "\n\n" + RETRY_LINE.format(tool=tool_name)


def build_callbacks(healer: Healer,
                    service_of: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Callable[..., Any]]:
    service_of = service_of or external_tool_filter()

    def on_result(tool_name: str = "", args: Any = None, result: Any = None,
                  status: Optional[str] = None, error_message: Optional[str] = None,
                  **_: Any) -> Optional[str]:
        try:
            if status != "error" or not isinstance(result, str) or not isinstance(args, dict):
                return None
            server = service_of(tool_name)
            if server is None:
                return None
            patched = healer.on_error(tool_name, args, error_message or "", raw_result=result,
                                      server=server)
            return rewrite_result(result, tool_name) if patched is not None else None
        except Exception as exc:
            logger.debug("manifest transform_tool_result failed open: %s", exc)
            return None

    def on_request(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
        try:
            if not isinstance(args, dict):
                return None
            pending = healer.take(tool_name, args)
            if pending is None:
                return None
            return {"args": pending.args, "source": "manifest", "reason": "healed"}
        except Exception as exc:
            logger.debug("manifest tool_request failed open: %s", exc)
            return None

    def on_post(tool_name: str = "", args: Any = None, status: Optional[str] = None,
                error_message: Optional[str] = None, **_: Any) -> None:
        try:
            if isinstance(args, dict):
                healer.outcome(tool_name, args, status, error_message)
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
    if os.environ.get("MNFST_HEAL_HTTP", "1") != "0":
        try:
            from mnfst import manifest  # optional: transport-level healing for in-process HTTP
        except ImportError:
            return
        try:
            manifest()
        except Exception as exc:
            logger.warning("manifest plugin: transport install skipped: %s", exc)
