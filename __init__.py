"""manifest: repair rejected tool calls with Manifest. Hermes Agent plugin, no dependencies.

A failed tool result is sent to the Manifest heal API. When a corrected set
of arguments comes back, the model is told to call the tool again, and the
retry runs with the corrected arguments. One heal, one retry, fail-open.

Environment: MNFST_KEY (required), MNFST_URL (optional),
MNFST_HEAL_TIMEOUT seconds (default 20). If the optional `mnfst` package is
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


def rewrite_result(result: str, tool_name: str) -> str:
    return result + "\n\n" + RETRY_LINE.format(tool=tool_name)


def build_callbacks(healer: Healer) -> Dict[str, Callable[..., Any]]:
    def on_result(tool_name: str = "", args: Any = None, result: Any = None,
                  status: Optional[str] = None, error_message: Optional[str] = None,
                  **_: Any) -> Optional[str]:
        try:
            if status != "error" or not isinstance(result, str) or not isinstance(args, dict):
                return None
            patched = healer.on_error(tool_name, args, error_message or "", raw_result=result)
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
    healer = Healer(HealClient(key, url, timeout=timeout), timeout=timeout)
    callbacks = build_callbacks(healer)
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
