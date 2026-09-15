"""Real MCP connections; the hook order and result envelope follow Hermes model_tools.

Set MNFST_TEST_APP_URL and MNFST_TEST_KEY to run against a disposable Manifest
backend seeded with this fixture's approved sort repair instead of StubHeal.
"""
import asyncio
import json
import os
import pathlib
import socket
import sys
import threading
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from manifest_heal import HealClient, Healer, RETRY_LINE
from tests.mcp_fixture import server, ERROR
from tests.stub_heal import StubHeal
from tests.test_end_to_end import load_plugin, wait_for, PATCHED


class RecordingClient(HealClient):
    def __init__(self, key, url):
        super().__init__(key, url)
        self.captures = []

    def heal(self, payload):
        self.captures.append(payload)
        return super().heal(payload)


@contextmanager
def manifest_client():
    if os.environ.get("MNFST_TEST_APP_URL"):
        yield RecordingClient(os.environ["MNFST_TEST_KEY"], os.environ["MNFST_TEST_APP_URL"])
    else:
        stub = StubHeal().start()
        stub.result = PATCHED
        try:
            yield RecordingClient("test", stub.url)
        finally:
            stub.stop()


@asynccontextmanager
async def connection(transport, http_statuses):
    if transport == "stdio":
        params = StdioServerParameters(command=sys.executable,
            args=[str(pathlib.Path(__file__).with_name("mcp_fixture.py"))])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                yield client, None
        return

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    http_server = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="error"))
    thread = threading.Thread(target=http_server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    async def record(response):
        http_statuses.append(response.status_code)

    try:
        assert await asyncio.to_thread(wait_for, lambda: http_server.started)
        async with httpx.AsyncClient(event_hooks={"response": [record]}) as http:
            async with streamable_http_client(origin + "/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    yield client, origin
    finally:
        http_server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()


async def exercise(transport):
    statuses = []
    with manifest_client() as api:
        healer = Healer(api)
        async with connection(transport, statuses) as (client, origin):
            cb = load_plugin().build_callbacks(healer, lambda _: "repair-fixture", lambda _: origin)
            args = {"sort": "wrong", "token": "local-secret"}
            ids = {"session_id": f"session-{transport}", "task_id": "task"}
            first = await client.call_tool("list_issues", args)
            assert first.isError is True
            if transport == "http":
                assert statuses[-1] == 200
            message = first.content[0].text
            raw = json.dumps({"error": message})  # Hermes' error-result envelope
            cb["post_tool_call"](tool_name="list_issues", args=args, result=raw,
                                  status="error", error_message=message, **ids)
            result = cb["transform_tool_result"](tool_name="list_issues", args=args,
                result=raw, status="error", error_message=message, **ids)
            assert result.endswith(RETRY_LINE.format(tool="list_issues"))
            retry = cb["tool_request"](tool_name="list_issues", args=args, **ids)
            second = await client.call_tool("list_issues", retry["args"])
            assert second.isError is False
            assert second.content[0].text == "repaired with original credential"
            cb["post_tool_call"](tool_name="list_issues", args=retry["args"], status="ok", **ids)
            assert len(api.captures) == 1
            capture = api.captures[0]
            assert capture["target"]["protocol"] == "mcp"
            assert capture["request"] == {"body": {"sort": "wrong"}}
            assert capture["response"]["body"] == ERROR
            assert await asyncio.to_thread(wait_for, lambda: not any(t.is_alive() for t in api._pending))
        healer._pool.shutdown(wait=True)


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_repaired_mcp_call(transport):
    asyncio.run(exercise(transport))
