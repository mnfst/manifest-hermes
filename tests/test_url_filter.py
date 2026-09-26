"""MNFST_ALLOWLIST / MNFST_DENYLIST: a tool call excluded by either list never
reaches Manifest — not healed, not tracked, result untouched."""
import json
from pathlib import Path

from manifest_filter import Rule, is_excluded, parse_rule, pick_rules
from tests.stub_heal import StubHeal
from tests.test_end_to_end import PATCHED, fake_dispatch, hermes_call, load_plugin, make

plugin = load_plugin()
# Shared with the Node, Python and PHP SDKs: the same file, the same answers.
CASES = json.loads((Path(__file__).parent / "fixtures" / "url-filter.json").read_text())


class Recorder:
    def __init__(self):
        self.calls = []

    def record(self, call):
        self.calls.append(call)


def test_parses_every_shared_entry_the_same_way():
    for case in CASES["parse"]:
        blank = case["entry"].strip() == ""
        rule = None if blank else parse_rule(case["entry"])
        assert (rule._asdict() if rule else None) == case["rule"], case["entry"]
        assert bool(pick_rules([case["entry"]], None)[1]) == bool(case.get("invalid")), case["entry"]


def test_matches_every_shared_url_the_same_way():
    for case in CASES["match"]:
        allow, _ = pick_rules(case.get("allow"), None)
        deny, _ = pick_rules(case.get("deny"), None)
        assert is_excluded(allow, deny or (), case["url"]) == case["excluded"], case


def rules(value):
    return pick_rules(value, None)[0]


def hooked(stub, recorder, allow=None, deny=()):
    return plugin.build_callbacks(make(stub), lambda name: "linear", lambda server: "mcp.linear.app:443",
                                  dispatch=fake_dispatch, tracker=recorder, allow=allow, deny=deny)


def test_a_denied_server_is_neither_healed_nor_tracked():
    stub = StubHeal().start()
    try:
        stub.result = PATCHED
        recorder = Recorder()
        cb = hooked(stub, recorder, deny=rules("linear.app"))
        assert hermes_call(cb, "list_issues", {"sort": "bad"}) == '{"error": "invalid sort"}'
        assert hermes_call(cb, "list_issues", {"sort": "created_at"}) == '{"ok": true}'
        assert stub.heals == []
        assert recorder.calls == []
    finally:
        stub.stop()


def test_a_server_off_the_allowlist_is_neither_healed_nor_tracked():
    stub = StubHeal().start()
    try:
        stub.result = PATCHED
        recorder = Recorder()
        cb = hooked(stub, recorder, allow=rules("composio.dev"))
        assert hermes_call(cb, "list_issues", {"sort": "bad"}) == '{"error": "invalid sort"}'
        assert hermes_call(cb, "list_issues", {"sort": "created_at"}) == '{"ok": true}'
        assert stub.heals == []
        assert recorder.calls == []
    finally:
        stub.stop()


def test_register_reads_both_lists_from_the_environment(monkeypatch):
    monkeypatch.setenv("MNFST_KEY", "mnfx_k")
    monkeypatch.setenv("MNFST_ALLOWLIST", "a.com")
    monkeypatch.setenv("MNFST_DENYLIST", "b.com/list_issues")
    seen = {}
    real = plugin.build_callbacks
    monkeypatch.setattr(plugin, "build_callbacks", lambda healer, **kw: seen.update(kw) or real(healer, **kw))

    class Ctx:
        def register_hook(self, name, fn):
            pass

    plugin.register(Ctx())
    assert (seen["allow"], seen["deny"]) == ((Rule("a.com", None),), (Rule("b.com", "/list_issues"),))


def test_a_denied_tool_is_skipped_and_the_servers_other_tools_are_tracked():
    stub = StubHeal().start()
    try:
        stub.result = PATCHED
        recorder = Recorder()
        cb = hooked(stub, recorder, deny=rules("linear.app/list_issues"))
        assert hermes_call(cb, "list_issues", {"sort": "bad"}) == '{"error": "invalid sort"}'
        assert hermes_call(cb, "list_projects", {"sort": "created_at"}) == '{"ok": true}'
        assert stub.heals == []
        assert [call["url"] for call in recorder.calls] == ["https://mcp.linear.app:443/list_projects"]
    finally:
        stub.stop()
