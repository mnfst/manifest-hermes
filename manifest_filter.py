"""Tool calls that never reach Manifest: neither healed nor tracked.

The URL matched is the one Manifest would see, https://<MCP server host>/<tool name>. An
entry is a domain (`linear.app`) or a domain with a path (`mcp.linear.app/list_issues`); the
domain covers its subdomains either way, and the path matches whole segments. Kept in step
with manifest-python's url_filter.py: tests/fixtures/url-filter.json is shared.
"""
from __future__ import annotations

import re
from typing import Iterable, List, NamedTuple, Optional, Tuple, Union
from urllib.parse import urlsplit

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_HOST = re.compile(r"^(?:[a-z0-9_-]+(?:\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])$")


class Rule(NamedTuple):
    host: str
    path: Optional[str]


Rules = Tuple[Rule, ...]
RuleList = Optional[Union[str, Iterable[str]]]


def parse_rule(entry: str) -> Optional[Rule]:
    """One entry, normalized; None when it cannot be read. The scheme, port, query and
    fragment are ignored."""
    rest = re.split(r"[?#]", _SCHEME.sub("", entry.strip(), count=1))[0]
    authority, slash, path = rest.partition("/")
    path = (slash + path).rstrip("/")
    host = re.sub(r":\d+$", "", authority.lower().removeprefix("*.")).strip(".")
    # `*` inside a path is reserved for a future segment wildcard, so it is refused today.
    if not _HOST.match(host) or "*" in path:
        return None
    return Rule(host.strip("[]"), path or None)


def _entries(value: RuleList) -> List[str]:
    items = value.split(",") if isinstance(value, str) else (value or ())
    return [str(item).strip() for item in items if str(item).strip()]


def pick_rules(option: RuleList, env_value: Optional[str]) -> Tuple[Optional[Rules], List[str]]:
    """An option beats its env var, like key and url; a blank one is unset and falls back to
    it. Returns the rules (None when nothing was given) and the entries that were dropped."""
    given = _entries(option) or _entries(env_value)
    parsed = [(entry, parse_rule(entry)) for entry in given]
    rules = tuple(rule for _, rule in parsed if rule is not None)
    return (rules if given else None), [entry for entry, rule in parsed if rule is None]


def _covers(rule: Rule, host: str, path: str) -> bool:
    return ((host == rule.host or host.endswith("." + rule.host))
            and (rule.path is None or path == rule.path or path.startswith(rule.path + "/")))


def is_excluded(allow: Optional[Rules], deny: Rules, url: str) -> bool:
    """A denied call is excluded; with an allowlist, so is every call not on it. An
    unreadable URL is not. `allow` is None when no allowlist was given; an allowlist of only
    bad entries still excludes."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    path = parts.path or "/"
    if any(_covers(rule, host, path) for rule in deny):
        return True
    return allow is not None and not any(_covers(rule, host, path) for rule in allow)
