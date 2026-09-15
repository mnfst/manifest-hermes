import json
import sys
import threading
from types import ModuleType

from manifest_heal import Healer, HealClient, error_body
from tests.test_end_to_end import callbacks, load_plugin, PATCHED


class API:
    def __init__(self):
        self.heals, self.outcomes = [], []

    def heal(self, payload):
        self.heals.append(payload)
        return PATCHED

    def report(self, *args):
        self.outcomes.append(args)


def test_pending_repair_and_outcome_stay_in_their_session():
    api = API()
    cb = callbacks(Healer(api))
    args = {"sort": "wrong", "token": "local-secret"}
    cb["transform_tool_result"](session_id="a", tool_name="list_issues", args=args,
        result='{"error":"invalid sort"}', status="error", error_message="invalid sort")
    assert cb["tool_request"](session_id="b", tool_name="list_issues", args=args) is None
    retry = cb["tool_request"](session_id="a", tool_name="list_issues", args=args)
    assert retry["args"] == {"sort": "created_at", "token": "local-secret"}
    assert api.heals[0]["request"]["body"] == {"sort": "wrong"}
    cb["post_tool_call"](session_id="b", tool_name="list_issues", args=retry["args"], status="ok")
    assert api.outcomes == []
    cb["post_tool_call"](session_id="a", tool_name="list_issues", args=retry["args"], status="ok")
    assert api.outcomes == [("a1", 200, None)]
    # Repeating the original failure cannot start another repair loop.
    assert cb["transform_tool_result"](session_id="a", tool_name="list_issues", args=args,
        result='{"error":"invalid sort"}', status="error", error_message="invalid sort") is None
    assert len(api.heals) == 1


def test_missing_session_identity_does_not_capture():
    api = API()
    cb = load_plugin().build_callbacks(Healer(api), lambda _: "server", lambda _: None)
    assert cb["transform_tool_result"](tool_name="t", args={"sort": "x"},
        result='{"error":"invalid sort"}', status="error") is None
    assert api.heals == []


def test_patched_credentials_cannot_replace_or_add_local_secrets():
    api = API()
    api.heal = lambda _: {**PATCHED, "healedRequest": {"body": {
        "sort": "created_at", "token": "remote", "password": "injected"}}}
    healer = Healer(api)
    original = {"sort": "x", "token": "local", "removed": "gone"}
    healer.on_error("t", original, "invalid sort")
    assert healer.take("t", original).args == {"sort": "created_at", "token": "local"}


def test_hermes_wrapped_validator_text_stays_structured_on_both_legs():
    issues = {"issues": [{"code": "too_big", "path": ["limit"], "message": "max 10"}]}
    message = json.dumps(issues)
    raw = json.dumps({"error": message})
    assert error_body(message, raw) == issues
    api = API()
    healer = Healer(api)
    healer.on_error("t", {"sort": "x"}, message, raw_result=raw)
    retry = healer.take("t", {"sort": "x"})
    healer.outcome("t", retry.args, "error", message, raw_result=raw)
    assert api.heals[0]["response"]["body"] == issues
    assert api.outcomes[0] == ("a1", 422, issues)


def test_origin_drops_router_credentials_and_tracks_config_changes(monkeypatch):
    config = {"server": {"url": "http://user:secret@localhost:9911/session/secret?key=secret"}}
    module = ModuleType("tools.mcp_tool_config")
    module._load_mcp_config = lambda: config
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_config", module)
    plugin = load_plugin()
    assert plugin._mcp_service_url("server") == "http://localhost:9911"
    config["server"] = {"command": "python", "args": ["server.py"]}
    assert plugin._mcp_service_url("server") is None


def test_http_instrumentation_is_explicit_opt_in(monkeypatch):
    invoked = []
    module = ModuleType("mnfst")
    module.manifest = lambda: invoked.append(True)
    monkeypatch.setitem(sys.modules, "mnfst", module)
    monkeypatch.setenv("MNFST_KEY", "test")
    monkeypatch.delenv("MNFST_HEAL_HTTP", raising=False)

    class Ctx:
        def register_hook(self, *args): pass
        def register_middleware(self, *args): pass

    load_plugin().register(Ctx())
    assert invoked == []
    monkeypatch.setenv("MNFST_HEAL_HTTP", "1")
    load_plugin().register(Ctx())
    assert invoked == [True]


def test_sdk_user_agent_is_recognizable_by_manifest():
    assert HealClient("key")._headers()["user-agent"].startswith("mnfst-hermes/")


def test_timed_out_heals_do_not_build_an_unbounded_queue():
    api = API()
    release = threading.Event()

    def blocked_heal(payload):
        api.heals.append(payload)
        release.wait(timeout=5)
        return None

    api.heal = blocked_heal
    healer = Healer(api, timeout=0.01)
    try:
        for number in range(8):
            assert healer.on_error("t", {"sort": str(number)}, "invalid sort") is None
        assert len(api.heals) == 4
    finally:
        release.set()
        healer._pool.shutdown(wait=True)
