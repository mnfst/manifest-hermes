# manifest-hermes

Hermes Agent plugin. A tool call is rejected, Manifest repairs the arguments, the retry succeeds.

```sh
hermes plugins install mnfst/manifest-hermes
hermes plugins enable manifest
```

Set `MNFST_KEY` (your Manifest project key, with Autofix on) when prompted or in the Hermes env file, then restart Hermes. No other dependency.

## What it repairs

MCP tools only, from any MCP server. Hermes registers them under an `mcp-<server>` toolset.

A capture carries the MCP server's real host and the tool as the path, for example `https://backend.composio.dev/GMAIL_FETCH_EMAILS`, so the service in Manifest is the server that rejected the call and each tool is its own endpoint. The router's real path is not used: it carries a per-agent session id. The headers `x-manifest-tool-call: mcp` and `x-manifest-mcp-server` mark the row as a tool call rather than a plain HTTP request. When the host cannot be read from the Hermes configuration, the URL falls back to `mcp://<server>/<tool>`.

Built-in tools are never repaired, including API-backed ones such as `web_search`. Local tools (terminal, file, memory) are never sent anywhere. A tool the registry cannot place counts as local, so an unreadable registry heals nothing rather than everything. `MNFST_TOOLS` is the explicit opt-in for a tool outside that rule.

## How it works

`transform_tool_result` sends a failed tool result to the Manifest heal API. When
corrected arguments come back, the plugin **re-invokes the tool itself** with the
patched arguments and returns the healed result. The model never sees Manifest,
a retry instruction, or the repair — it only ever sees the tool's response,
healed or not. One heal and one internal retry per failure; anything unexpected
passes the original result through.

Captures carry `statusCode: 424` (Failed Dependency — the transport succeeded but
the tool execution it depended on failed) plus the `x-manifest-tool-call: mcp` and
`x-manifest-mcp-server` headers, so the backend can segment tool calls from plain
HTTP traffic. No Hermes middleware is used, so the plugin runs on any Hermes version.

## Measuring

With `MNFST_HEAL_LOG=1` (the default), every heal attempt and retry outcome appends
one JSON line to `$HERMES_TRACE_DIR/events.jsonl` (default `~/.hermes/logs/hermes-trace/`):

    heal_attempt  {tool, args, error, verdict: patched|no_patch|timeout|transport_error, patch?, attempt_id}
    heal_outcome  {tool, attempt_id, patched_args, retry_result: success|failed, status_code}

The funnel — attempts → patches → retries succeeded — is the effectiveness measure.

If the `mnfst` package happens to be installed in the Hermes environment, HTTP calls made by tools inside the Hermes process are healed at the transport level too. Set `MNFST_HEAL_HTTP=0` to skip that.

Environment: `MNFST_KEY`, `MNFST_URL` (optional), `MNFST_HEAL_TIMEOUT` (seconds, default 20), `MNFST_TOOLS` (extra external name prefixes), `MNFST_HEAL_HTTP`, `MNFST_HEAL_LOG` (measurement sink, default on).

## Privacy

Tool names, arguments, and error text of rejected external calls are sent to Manifest. Credential-named fields are withheld; nested business data is not.
