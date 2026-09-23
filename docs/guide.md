# Guide

Configuration, limits and development for the Manifest Hermes plugin. The
[README](../README.md) covers install and setup.

## Configuration

Two variables, both read once at registration. The environment cannot change
without a restart.

| Variable | What it does |
| --- | --- |
| `MNFST_KEY` | Your Manifest project key. Required; without it the plugin registers nothing. |
| `MNFST_URL` | Point at another Manifest endpoint. Optional. |

The heal request waits 20 seconds for a patch, then the original result stands.

### What gets repaired

A tool is repaired when Hermes registered it under an `mcp-<server>` toolset.
Everything else counts as local, including a built-in tool that reaches an API,
so an unreadable registry heals nothing rather than everything. There is no
opt-in for other tools: a local tool's arguments are never sent anywhere.

## Where a capture lands

A capture carries the MCP server's real host and the tool as the path, for
example `https://backend.composio.dev/GMAIL_FETCH_EMAILS`, so the service in
Manifest is the server that rejected the call and each tool is its own
endpoint. The router's real path is not used: it carries a per-agent session
id. When the host cannot be read from the Hermes configuration, the URL falls
back to `mcp://<server>/<tool>`.

Captures carry `statusCode: 418`, the sentinel for a tool call rather than a
wire status. 418 is permanently reserved (RFC 2324 / RFC 9110), so no real API
failure can collide with the synthetic envelope. The `x-manifest-tool-call: mcp`
and `x-manifest-mcp-server` headers let the backend segment tool calls from
plain HTTP traffic.

## Every tool call is tracked

Every MCP tool call the plugin does not send to heal is also reported, as
metadata only, to `POST /v1/requests`: the same URL a capture would use, the
status (`200` when the tool call worked, `418` when it failed while healing was
paused), the duration Hermes measured, and when it happened. Never the
arguments or the result. Local tools are never reported, and neither is the
plugin's own retry.

Calls are kept in memory and sent from a background thread: when 500 are
waiting or every five seconds, at most once per second, 500 per request.
Recording a call never delays the tool loop. At most 5,000 calls wait; newer
ones are dropped past that. A send that fails with a network error, 429 or 5xx
is retried once. Calls still waiting are sent when Hermes exits, for at most
two seconds. A disabled project pauses sending like healing.

This tracks tool calls, not the HTTP requests behind them: an MCP server
usually answers HTTP 200 even when a tool fails (the error rides in the
JSON-RPC body), so the tool's result is the signal.

## How the retry runs

The retry is a real Hermes tool call (`handle_function_call`), not a bare
registry dispatch. It runs through the `pre_tool_call` policy hooks, edit
approval and the tool-execution middleware with the original call's identity,
so server-dictated arguments get every check the first call got and a policy
plugin sees the retry.

The agent loop owns `post_tool_call` and fires it once per model-facing call,
with the final (healed) result.

A patch that changes nothing, or a retry that never reaches the tool, is
reported as *not attempted* rather than as a failure.

No Hermes middleware is used, so the plugin runs on any Hermes version.

## Measuring

Every heal attempt and retry outcome appends one JSON line to `$HERMES_TRACE_DIR/events.jsonl` (default
`~/.hermes/logs/hermes-trace/`):

```
heal_attempt  {tool, args, error, verdict: patched|no_patch|timeout|transport_error, patch?, attempt_id}
heal_outcome  {tool, attempt_id, patched_args, retry_result: success|failed|not_attempted, status_code?}
```

The funnel, attempts to patches to retries succeeded, is the effectiveness
measure. The directory is created on first write. Logged arguments are the ones
that travelled: credential-named fields are withheld here as well as on the
wire.

## Development

The plugin is installed by copy, not by pip, so it must import with no
dependencies of its own. Install the dev extras to run the suite:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
```

CI runs the same commands on Python 3.10, 3.13 and 3.14, plus an import check
and a `plugin.yaml` validation.
