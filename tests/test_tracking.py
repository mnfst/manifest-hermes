"""Tracked tool calls: every MCP tool call the plugin does not send to heal is
recorded as metadata (method, URL, status, duration) and sent in batches to
POST /v1/requests. Local tools and the plugin's own retries never are."""
import threading
import time

from manifest_heal import Healer, HealClient
from manifest_tracking import CallBuffer, tracked_call
from tests.stub_heal import StubHeal
from tests.test_end_to_end import PATCHED, fake_dispatch, load_plugin


def call(i):
    return {"traceId": f"t{i}", "method": "POST", "url": "https://a.dev/x", "statusCode": 200,
            "responseTimeMs": 1, "occurredAt": "1970-01-01T00:00:00+00:00"}


# --- the buffer -----------------------------------------------------------------

def test_sends_at_500_in_one_batch():
    sent = []
    buf = CallBuffer(sent.append, interval=60, min_gap=0)
    for i in range(500):
        buf.record(call(i))
    buf.flush(timeout=2)
    assert [len(b) for b in sent] == [500]


def test_drops_past_5000_without_blocking():
    gate = threading.Event()
    buf = CallBuffer(lambda b: gate.wait(), interval=60)  # the send hangs
    started = time.monotonic()
    for i in range(50_000):
        buf.record(call(i))
    assert time.monotonic() - started < 1.0
    assert buf.size() <= 5000
    gate.set()


def test_a_failed_batch_is_retried_once_then_dropped():
    calls = []

    def boom(batch):
        calls.append(len(batch))
        raise RuntimeError("down")

    buf = CallBuffer(boom, interval=60, min_gap=0)
    for i in range(10):
        buf.record(call(i))
    buf.flush(timeout=2)
    assert calls == [10, 10]
    assert buf.size() == 0


def test_never_sends_twice_within_min_gap():
    at = []
    buf = CallBuffer(lambda b: at.append(time.monotonic()), interval=60, min_gap=0.2)
    for i in range(1500):
        buf.record(call(i))
    buf.flush(timeout=5)
    assert len(at) == 3
    assert all(b - a >= 0.19 for a, b in zip(at, at[1:]))


# --- the record -------------------------------------------------------------------

def test_a_tool_call_is_recorded_like_its_heal_capture():
    record = tracked_call("linear", "create_issue", "mcp.linear.app", 200, 142)
    assert set(record) == {"traceId", "method", "url", "statusCode", "responseTimeMs", "occurredAt"}
    assert (record["method"], record["url"], record["statusCode"], record["responseTimeMs"]) == \
        ("POST", "https://mcp.linear.app/create_issue", 200, 142)
    assert record["occurredAt"].endswith("+00:00")


def test_a_stdio_server_is_recorded_under_the_mcp_scheme():
    assert tracked_call("files", "read", None, 200, None)["url"] == "mcp://files/read"


def test_an_unusable_record_is_never_built():
    assert tracked_call("s", "x" * 5000, "h.dev", 200, 1) is None
    assert tracked_call("s", "t", "h.dev", 0, 1) is None


# --- the hook ---------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    def record(self, call):
        self.calls.append(call)


def hooked(stub, recorder, service="linear", host="mcp.linear.app"):
    healer = Healer(HealClient("mnfx_k", stub.url, timeout=5.0), timeout=5.0)
    return healer, load_plugin().build_callbacks(
        healer, lambda name: service, lambda server: host, dispatch=fake_dispatch,
        tracker=recorder)["transform_tool_result"]


def test_a_successful_mcp_tool_call_is_tracked_with_its_duration():
    stub = StubHeal().start()
    try:
        recorder = Recorder()
        _, hook = hooked(stub, recorder)
        assert hook(tool_name="create_issue", args={"title": "x"}, result='{"ok": true}',
                    status="ok", duration_ms=142) is None
        assert [(c["url"], c["statusCode"], c["responseTimeMs"]) for c in recorder.calls] == \
            [("https://mcp.linear.app/create_issue", 200, 142)]
        assert stub.heals == []
    finally:
        stub.stop()


def test_a_failed_mcp_tool_call_goes_to_heal_not_to_tracking():
    stub = StubHeal().start()
    try:
        stub.result = PATCHED
        recorder = Recorder()
        _, hook = hooked(stub, recorder)
        hook(tool_name="list_issues", args={"sort": "newest"}, result='{"error": "invalid sort"}',
             status="error", error_message="invalid sort", duration_ms=30)
        assert len(stub.heals) == 1
        assert stub.heals[0]["responseTimeMs"] == 30   # the capture carries Hermes' duration too
        assert recorder.calls == []   # neither the failure nor the healed retry
    finally:
        stub.stop()


def test_a_failure_while_healing_is_paused_is_tracked_as_a_tool_error():
    stub = StubHeal().start()
    try:
        recorder = Recorder()
        healer, hook = hooked(stub, recorder)
        healer.api._disabled_until = healer.api.clock() + 60   # project_disabled pause
        hook(tool_name="list_issues", args={}, result='{"error": "invalid sort"}',
             status="error", error_message="invalid sort", duration_ms=30)
        assert stub.heals == []
        assert [c["statusCode"] for c in recorder.calls] == [418]
    finally:
        stub.stop()


def test_a_local_tool_is_never_tracked():
    stub = StubHeal().start()
    try:
        recorder = Recorder()
        _, hook = hooked(stub, recorder, service=None)
        hook(tool_name="terminal", args={"command": "ls"}, result="ok", status="ok", duration_ms=5)
        assert recorder.calls == []
    finally:
        stub.stop()


def test_send_requests_raises_only_when_a_retry_could_help():
    stub = StubHeal().start()
    try:
        client = HealClient("mnfx_k", stub.url, timeout=5.0)
        for status in (202, 400, 401, 404):
            stub.requests_status = status
            client.send_requests([call(1)])
        for status in (429, 500):
            stub.requests_status = status
            try:
                client.send_requests([call(1)])
                raise AssertionError(f"{status} should raise")
            except RuntimeError:
                pass
        stub.requests_status = 202
        client.send_requests([call(2)])
        assert [c["traceId"] for c in stub.tracked][-1] == "t2"
    finally:
        stub.stop()
