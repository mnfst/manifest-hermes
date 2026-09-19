"""manifest: repair rejected tool calls with Manifest. Hermes Agent plugin, no dependencies.

A failed MCP tool call is sent to the Manifest heal API. When corrected
arguments come back, the plugin re-invokes the tool itself with the patched
arguments and returns the healed result to the model. The agent never sees
Manifest, a retry instruction, or the repair — only the tool's response,
healed or not. One heal and one internal retry per failure, fail-open.

Only MCP tools are repaired, from any MCP server, plus any name prefix
listed in MNFST_TOOLS. Built-in tools are never touched.

Captures carry statusCode 424 (Failed Dependency): the transport succeeded but
the tool execution it depended on failed. The `x-manifest-tool-call: mcp` and
`x-manifest-mcp-server` headers mark the row for backend segmentation.

Environment: MNFST_KEY (required), MNFST_URL (optional),
MNFST_HEAL_TIMEOUT seconds (default 20), MNFST_TOOLS (comma-separated name
prefixes to treat as external), MNFST_HEAL_LOG (default 1: append heal
attempts/outcomes to HERMES_TRACE_DIR for measurement). If the optional
`mnfst` package is installed, HTTP calls made inside the Hermes process are
healed at the transport level as well; MNFST_HEAL_HTTP=0 skips that.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, Optional

try:  # loaded as a package by Hermes
    from .manifest_heal import DEFAULT_URL, HealClient, Healer, healed_args
except ImportError:  # loaded flat, plugin directory on sys.path
    from manifest_heal import DEFAULT_URL, HealClient, Healer, healed_args  # type: ignore

logger = logging.getLogger(__name__)


def _tool_entry(tool_name: str):
    """The Hermes registry entry for a tool, or None when it cannot be read."""
    try:
        from tools.registry import registry  # Hermes' own tool registry
        return registry.get_entry(tool_name)
    except Exception:
        return None


_HOSTS: Dict[str, Optional[str]] = {}


def _host_map() -> Dict[str, str]:
    """Optional `MNFST_HOST_MAP=server=host,...` overrides for capture URLs.

    Useful when a stdio MCP server fronts a known API host (tests, gateways):
    the map wins over the config-derived URL host.
    """
    raw = os.environ.get("MNFST_HOST_MAP", "")
    out: Dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            key, _, value = pair.partition("=")
            if key.strip() and value.strip():
                out[key.strip()] = value.strip()
    return out


def _mcp_server_host(server: str) -> Optional[str]:
    """The host of a configured MCP server, from Hermes' own configuration.

    Cached: the lookup parses the config file, and the answer cannot change
    without a restart. `MNFST_HOST_MAP` overrides per server.
    """
    override = _host_map().get(server)
    if override:
        return override
    if server in _HOSTS:
        return _HOSTS[server]
    host = None
    try:
        from urllib.parse import urlsplit
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly() or {}).get("mcp_servers") or {}).get(server)
        url = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(url, str) and url:
            host = urlsplit(url).netloc.split("@")[-1].lower() or None
    except Exception:
        host = None
    _HOSTS[server] = host
    return host


def external_tool_filter(extra: tuple = (), entry_of: Callable[[str], Any] = _tool_entry):
    """The service a tool calls, or None when the tool is local.

    Manifest learns a contract from a service's own rejections. A local tool
    has none, and rewriting the arguments of a shell or file tool is not
    something a remote service should do.

    An `mcp-<server>` toolset marks the repairable ones: that is how Hermes
    registers every MCP server's tools, whatever the server. The server names
    the service. Everything else counts as local, including a built-in tool
    that reaches an API, so an unreadable registry heals nothing rather than
    everything. MNFST_TOOLS is the explicit opt-in for a tool outside that
    rule; the prefix names its service.
    """
    def service_of(tool_name: str) -> Optional[str]:
        entry = entry_of(tool_name)
        toolset = getattr(entry, "toolset", None) if entry is not None else None
        if toolset and toolset.startswith("mcp-"):
            return toolset[len("mcp-"):].strip("-_") or "mcp"
        for prefix in extra:
            if tool_name.startswith(prefix):
                return prefix.strip("-_") or "tool"
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


def _registry_dispatch():
    def dispatch(name: str, args: dict):
        from tools.registry import registry  # Hermes' own tool registry
        return registry.dispatch(name, args)
    return dispatch


def build_callbacks(healer: Healer,
                    service_of: Optional[Callable[[str], Optional[str]]] = None,
                    host_of: Callable[[str], Optional[str]] = _mcp_server_host,
                    dispatch: Optional[Callable[[str, dict], Any]] = None,
                    ) -> Dict[str, Callable[..., Any]]:
    """The transform_tool_result callback: capture, heal, re-invoke, report.

    `dispatch` is the re-invocation seam (Hermes' registry by default); tests
    inject a fake here.
    """
    service_of = service_of or external_tool_filter()
    dispatch = dispatch or _registry_dispatch()

    def on_result(tool_name: str = "", args: Any = None, result: Any = None,
                  status: Optional[str] = None, error_message: Optional[str] = None,
                  **_: Any) -> Optional[str]:
        try:
            if status != "error" or not isinstance(result, str) or not isinstance(args, dict):
                return None
            server = service_of(tool_name)
            if server is None:
                return None
            patch = healer.on_error(tool_name, args, error_message or "", raw_result=result,
                                    server=server, host=host_of(server))
            if patch is None:
                return None
            pending = healer.take(tool_name, args)
            if pending is None:
                return None
            merged = healed_args(args, patch)
            if merged == args or not merged:
                # Nothing changed, or nothing is left to send: either way the patch
                # cannot fix anything, so report it and pass the error through.
                healer.outcome(tool_name, pending.args, "error", error_message or "tool error")
                return None
            try:
                healed = dispatch(tool_name, merged)
            except Exception as exc:
                healer.outcome(tool_name, pending.args, "error", f"{type(exc).__name__}: {exc}")
                logger.debug("manifest internal retry raised: %s", exc)
                return None
            failed = _looks_failed(healed)
            healer.outcome(tool_name, pending.args,
                           "error" if failed else "ok",
                           _error_text(healed) if failed else None)
            if failed:
                return None  # the original error result stands
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
    timeout = float(os.environ.get("MNFST_HEAL_TIMEOUT", "20"))
    healer = Healer(HealClient(config_key, url, timeout=timeout), timeout=timeout)
    callbacks = build_callbacks(healer)
    ctx.register_hook("transform_tool_result", callbacks["transform_tool_result"])
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
