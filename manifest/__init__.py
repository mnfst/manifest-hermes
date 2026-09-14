"""manifest: repair rejected tool calls with Manifest.

A failed tool result is sent to the Manifest heal API. When a corrected set
of arguments comes back, the model is told to call the tool again, and the
retry runs with the corrected arguments. One heal, one retry, fail-open.

Environment: MNFST_KEY (required), MNFST_URL (optional),
MNFST_HEAL_TIMEOUT seconds (default 20), MNFST_HEAL_HTTP=0 to skip the
transport-level manifest() install for in-process HTTP tools.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

from mnfst.config import resolve_config
from mnfst.heal_api import HealApi

from .manifest_heal import RETRY_LINE, Healer

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
            patched = healer.on_error(tool_name, args, error_message or "")
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
    config = resolve_config()
    if config.api_key is None:
        logger.warning("manifest plugin: MNFST_KEY is not set; nothing registered")
        return
    timeout = float(os.environ.get("MNFST_HEAL_TIMEOUT", "20"))
    healer = Healer(HealApi(config), timeout=timeout)
    callbacks = build_callbacks(healer)
    ctx.register_hook("transform_tool_result", callbacks["transform_tool_result"])
    ctx.register_middleware("tool_request", callbacks["tool_request"])
    ctx.register_hook("post_tool_call", callbacks["post_tool_call"])
    logger.info("manifest plugin: tool-call repair registered")
    if os.environ.get("MNFST_HEAL_HTTP", "1") != "0":
        try:
            from mnfst import manifest
            manifest()
        except Exception as exc:
            logger.warning("manifest plugin: transport install skipped: %s", exc)
