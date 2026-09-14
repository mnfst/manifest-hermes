"""The full sequence as Hermes fires it (model_tools.handle_function_call):
tool_request middleware -> dispatch -> post_tool_call -> transform_tool_result."""
from mnfst.config import resolve_config
from mnfst.heal_api import HealApi

import manifest as plugin
from manifest.manifest_heal import Healer, RETRY_LINE
from tests.stub_heal import StubHeal
import time


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


def test_reject_then_repaired_retry():
    stub = StubHeal().start()
    try:
        stub.result = {"status": "patched", "issueId": "i1", "healAttemptId": "a1",
                       "healedRequest": {"body": {"sort": "created_at"}}}
        healer = Healer(HealApi(resolve_config(api_key="mnfx_k", url=stub.url)), timeout=5.0)
        cb = plugin.build_callbacks(healer)

        first = hermes_call(cb, "list_issues", {"sort": "occurrence_count"})
        assert first.endswith(RETRY_LINE.format(tool="list_issues"))
        assert stub.heals[0]["request"]["url"] == "mcp://list_issues"

        second = hermes_call(cb, "list_issues", {"sort": "occurrence_count"})  # model retries as told
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
        healer = Healer(HealApi(resolve_config(api_key="mnfx_k", url=stub.url)), timeout=5.0)
        cb = plugin.build_callbacks(healer)
        out = hermes_call(cb, "list_issues", {"sort": "x"})
        assert out == '{"error": "invalid sort"}'
        assert cb["tool_request"](tool_name="list_issues", args={"sort": "x"}) is None
    finally:
        stub.stop()
