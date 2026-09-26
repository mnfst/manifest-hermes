"""manifest: the Manifest Python SDK, started inside Hermes. Hermes Agent plugin.

The plugin only installs and starts `mnfst` (manifest-python). Hermes installs
the dependency with the plugin and again after every `hermes update`, and calls
`register()` in every Hermes process (CLI, gateway, workers) after it has loaded
its own configuration, so `MNFST_KEY` set with `hermes config set` is in the
environment by then.

From there the SDK does everything: it instruments the HTTP clients of the
process, including the one Hermes' MCP client uses, heals the failures Manifest
has a patch for and tracks every other call as metadata. It reads MNFST_KEY,
MNFST_URL, MNFST_ALLOWLIST and MNFST_DENYLIST from the environment itself.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    if not os.environ.get("MNFST_KEY", "").strip():
        logger.warning("manifest plugin: MNFST_KEY is not set; nothing started")
        return
    try:
        from mnfst import manifest
    except ImportError:
        logger.warning("manifest plugin: the mnfst package is not installed; "
                       "run `hermes plugins enable manifest` to install it")
        return
    try:
        manifest()
    except Exception as exc:  # the SDK must never keep Hermes from starting
        logger.warning("manifest plugin: mnfst failed to start: %s", exc)
        return
    logger.info("manifest plugin: mnfst started")
