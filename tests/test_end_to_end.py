"""The full sequence as Hermes fires it (model_tools.handle_function_call):
tool_request middleware -> dispatch -> post_tool_call -> transform_tool_result."""
import importlib.util
import json
import pathlib
import sys
import time

from manifest_heal import Healer, HealClient, RETRY_LINE, error_body, tool_url
from tests.stub_heal import StubHeal


def load_plugin():
    """Load the repo root as a package, the way Hermes loads a plugin directory."""
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("manifest_plugin", root / "__init__.py",
                                                  submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["manifest_plugin"] = module
    spec.loader.exec_module(module)
    return module


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def fake_tool(args):  # rejects anything but sort=created_at
    return '{"ok": true}' if args.get("sort") == "created_at" else '{"error": "invalid sort"}'


def hermes_call(cb, tool_name, args):
    swap = cb["tool_request"](tool_name=tool_name, args=dict(args), original_args=dict(args))
    effective = swap["args"] if swap else args
    result = fake_tool(effective)
    status = "error" if '"error"' in result else "ok"
    error = "invalid sort" if status == "error" else None
    cb["post_tool_call"](tool_name=tool_name, args=effective, result=result, status=status, error_message=error)
    replaced = cb["transform_tool_result"](tool_name=tool_name, args=effective, result=result,
                                           status=status, error_message=error)
    return replaced if isinstance(replaced, str) else result


PATCHED = {"status": "patched", "issueId": "i1", "healAttemptId": "a1",
           "healedRequest": {"body": {"sort": "created_at"}}}


def make(stub):
    return Healer(HealClient("mnfx_k", stub.url, timeout=5.0), timeout=5.0)


def callbacks(healer, plugin=None):
    """Build with every tool treated as external; scope itself is tested below."""
    return (plugin or load_plugin()).build_callbacks(
        healer, lambda name: "composio", lambda server: "backend.composio.dev")


def test_reject_then_repaired_retry():
    stub = StubHeal().start()
    try:
        stub.result = PATCHED
        healer = make(stub)
        cb = callbacks(healer)

        first = hermes_call(cb, "list_issues", {"sort": "occurrence_count", "token": "s3cret"})
        assert first.endswith(RETRY_LINE.format(tool="list_issues"))
        capture = stub.heals[0]
        assert capture["request"]["url"] == "https://backend.composio.dev/list_issues"
        assert capture["request"]["headers"]["x-manifest-tool-call"] == "mcp"
        assert capture["request"]["headers"]["x-manifest-mcp-server"] == "composio"
        assert capture["request"]["body"] == {"sort": "occurrence_count"}  # token withheld
        assert capture["response"] == {"statusCode": 422,
                                       "body": {"error": {"message": "invalid sort"}},
                                       "truncated": False}

        second = hermes_call(cb, "list_issues", {"sort": "occurrence_count", "token": "s3cret"})
        assert second == '{"ok": true}'

        assert wait_for(lambda: len(stub.outcomes) == 1)
        assert len(stub.heals) == 1
        assert stub.outcomes == [("a1", {"response": {"statusCode": 200}})]

        # the patched args failing again are never re-healed
        assert healer.on_error("list_issues", {"sort": "created_at"}, "still bad") is None
        assert len(stub.heals) == 1
    finally:
        stub.stop()


def test_no_patch_passes_the_error_through():
    stub = StubHeal().start()
    try:
        cb = callbacks(make(stub))
        out = hermes_call(cb, "list_issues", {"sort": "x"})
        assert out == '{"error": "invalid sort"}'
        assert cb["tool_request"](tool_name="list_issues", args={"sort": "x"}) is None
    finally:
        stub.stop()


def test_failed_retry_reports_422_with_the_error():
    stub = StubHeal().start()
    try:
        stub.result = {"status": "patched", "healAttemptId": "a2", "healedRequest": {"body": {"sort": "nope"}}}
        cb = callbacks(make(stub))
        hermes_call(cb, "list_issues", {"sort": "x"})
        out = hermes_call(cb, "list_issues", {"sort": "x"})  # retry with sort=nope fails too
        assert out == '{"error": "invalid sort"}'  # no second retry line
        assert wait_for(lambda: len(stub.outcomes) == 1)
        assert stub.outcomes[0][0] == "a2"
        assert stub.outcomes[0][1]["response"]["statusCode"] == 422
        assert stub.outcomes[0][1]["response"]["body"] == {"error": "invalid sort"}
        assert len(stub.heals) == 1
    finally:
        stub.stop()


def test_unreachable_or_disabled_api_fails_open():
    down = Healer(HealClient("mnfx_k", "http://127.0.0.1:9", timeout=2.0), timeout=2.0)
    assert down.on_error("t", {"a": 1}, "e") is None

    stub = StubHeal().start()
    try:
        stub.disabled = True
        client = HealClient("mnfx_k", stub.url, timeout=5.0)
        healer = Healer(client, timeout=5.0)
        assert healer.on_error("t", {"a": 1}, "e") is None
        assert not client.enabled()  # paused for five minutes after project_disabled
        stub.disabled = False
        stub.result = PATCHED
        assert healer.on_error("t", {"a": 1}, "e") is None  # still paused, no request sent
        assert len(stub.heals) == 1
    finally:
        stub.stop()


def test_expired_patch_is_reported_not_attempted():
    stub = StubHeal().start()
    try:
        now = [0.0]
        healer = Healer(HealClient("mnfx_k", stub.url, timeout=5.0), timeout=5.0, ttl=10.0, clock=lambda: now[0])
        stub.result = PATCHED
        healer.on_error("t", {"a": 1}, "e")
        now[0] = 11.0
        healer.expire()
        assert healer.take("t", {"a": 1}) is None
        assert wait_for(lambda: len(stub.outcomes) == 1)
        assert stub.outcomes[0][1] == {"failure": {"kind": "not_attempted", "message": "replay_not_attempted"}}
    finally:
        stub.stop()


def test_register_wires_three_seams_and_respects_missing_key(monkeypatch):
    class Ctx:
        def __init__(self):
            self.hooks, self.middleware = {}, {}

        def register_hook(self, name, fn):
            self.hooks[name] = fn

        def register_middleware(self, kind, fn):
            self.middleware[kind] = fn

    monkeypatch.delenv("MNFST_KEY", raising=False)
    ctx = Ctx()
    load_plugin().register(ctx)
    assert ctx.hooks == {} and ctx.middleware == {}

    monkeypatch.setenv("MNFST_KEY", "mnfx_k")
    monkeypatch.setenv("MNFST_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MNFST_HEAL_HTTP", "0")
    ctx = Ctx()
    load_plugin().register(ctx)
    assert set(ctx.hooks) == {"transform_tool_result", "post_tool_call"}
    assert set(ctx.middleware) == {"tool_request"}


def test_plugin_has_no_third_party_imports():
    root = pathlib.Path(__file__).resolve().parents[1]
    for name in ("__init__.py", "manifest_heal.py"):
        text = (root / name).read_text()
        assert "from mnfst" not in text.replace("from mnfst import manifest  # optional", "")
        assert "import httpx" not in text and "import requests" not in text


def test_the_real_host_names_the_service_and_the_tool_is_the_endpoint():
    """Manifest reads the service from the host and the endpoint from the path."""
    assert (tool_url("composio", "GMAIL_FETCH_EMAILS", "backend.composio.dev")
            == "https://backend.composio.dev/GMAIL_FETCH_EMAILS")
    # No configured host to read: the scheme says the row is a tool call.
    assert tool_url("composio", "GMAIL_FETCH_EMAILS") == "mcp://composio/GMAIL_FETCH_EMAILS"


def test_the_headers_say_the_row_is_a_tool_call():
    from manifest_heal import tool_headers
    headers = tool_headers("composio")
    assert headers["x-manifest-tool-call"] == "mcp"
    assert headers["x-manifest-mcp-server"] == "composio"


def test_validator_payloads_travel_untouched():
    """The server recognizes Zod and Pydantic dialects and repairs by field path;
    a free-text error travels as a message envelope instead."""
    zod = '{"issues": [{"code": "invalid_enum_value", "path": ["sort"], "message": "bad sort"}]}'
    assert error_body("bad sort", zod) == json.loads(zod)
    pydantic = '{"detail": [{"type": "greater_than", "loc": ["body", "limit"], "msg": "too big"}]}'
    assert error_body("too big", pydantic) == json.loads(pydantic)
    nest = '{"errors": [{"code": "too_big", "path": ["limit"], "message": "max 100"}]}'
    assert error_body("max 100", nest) == json.loads(nest)
    assert error_body("plain text", '{"error": "plain text"}') == {"error": {"message": "plain text"}}
    assert error_body("plain text", "not json at all") == {"error": {"message": "plain text"}}
    assert error_body("plain text", '{"issues": []}') == {"error": {"message": "plain text"}}


def test_a_validator_rejection_reaches_the_api_in_its_own_shape():
    stub = StubHeal().start()
    try:
        stub.result = PATCHED
        cb = callbacks(make(stub))
        raw = '{"issues": [{"code": "invalid_enum_value", "path": ["sort"], "message": "bad sort"}]}'
        cb["transform_tool_result"](tool_name="list_issues", args={"sort": "x"}, result=raw,
                                    status="error", error_message="bad sort")
        assert stub.heals[0]["response"]["body"] == json.loads(raw)
    finally:
        stub.stop()


class Entry:
    def __init__(self, toolset, requires_env=()):
        self.toolset, self.requires_env = toolset, list(requires_env)


REGISTRY = {
    # every MCP server, not one vendor
    "list_issues": Entry("mcp-composio"),
    "search_tickets": Entry("mcp-linear"),
    # built-in tools, API-backed or not, are out of scope
    "web_search": Entry("web", ["FIRECRAWL_API_KEY"]),
    "web_extract": Entry("web", ["FIRECRAWL_API_KEY"]),
    "terminal": Entry("terminal"),
    "read_file": Entry("file"),
    "memory": Entry("memory"),
}


def test_only_mcp_tools_are_repaired():
    plugin = load_plugin()
    service_of = plugin.external_tool_filter(entry_of=REGISTRY.get)
    assert service_of("list_issues") == "composio"
    assert service_of("search_tickets") == "linear"      # any MCP server, not just Composio
    assert service_of("web_search") is None              # built-in, even though it calls an API
    assert service_of("web_extract") is None
    assert service_of("terminal") is None
    assert service_of("read_file") is None
    assert service_of("memory") is None
    assert service_of("never_registered") is None        # unplaceable counts as local

    extended = plugin.external_tool_filter(extra=("github_",), entry_of=REGISTRY.get)
    assert extended("github_create_issue") == "github"
    assert extended("terminal") is None


def test_a_local_tool_failure_never_reaches_the_api():
    stub = StubHeal().start()
    try:
        stub.result = PATCHED
        plugin = load_plugin()
        cb = plugin.build_callbacks(make(stub), plugin.external_tool_filter(entry_of=REGISTRY.get),
                                    lambda server: None)
        out = cb["transform_tool_result"](tool_name="terminal", args={"command": "rm -rf /tmp/x"},
                                          result='{"error": "exit 1"}', status="error",
                                          error_message="exit 1")
        assert out is None
        assert stub.heals == []

        healed = cb["transform_tool_result"](tool_name="list_issues", args={"sort": "x"},
                                             result='{"error": "invalid sort"}', status="error",
                                             error_message="invalid sort")
        assert healed is not None
        assert len(stub.heals) == 1
    finally:
        stub.stop()


def test_registry_lookup_without_hermes_places_nothing():
    """Outside Hermes the registry import fails; the filter then heals nothing."""
    plugin = load_plugin()
    assert plugin._tool_entry("anything") is None
    assert plugin.external_tool_filter()("anything") is None


def test_an_unreadable_configuration_falls_back_to_the_mcp_scheme():
    plugin = load_plugin()
    assert plugin._mcp_server_host("never-configured") is None
