# manifest-hermes

Hermes Agent plugin. A tool call is rejected, Manifest repairs the arguments, the retry succeeds.

```sh
hermes plugins install mnfst/manifest-hermes
hermes plugins enable manifest
```

Set `MNFST_KEY` (your Manifest project key, with Autofix on) when prompted or in the Hermes env file, then restart Hermes. No other dependency.

## Updating

```sh
hermes plugins update manifest
```

That pulls the latest commit into `~/.hermes/plugins/manifest`. Restart Hermes afterwards so the new code loads. If the install is pinned to a commit or is not a git checkout (a copied directory), Hermes refuses to pull; reinstall instead:

```sh
hermes plugins install mnfst/manifest-hermes --force
```

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

The retry is a real Hermes tool call (`handle_function_call`), not a bare registry
dispatch: it runs through the `pre_tool_call` policy hooks, edit approval and the
tool-execution middleware with the original call's identity, so server-dictated
arguments get every check the first call got and a policy plugin sees the retry.
The agent loop owns `post_tool_call` and fires it once per model-facing call,
with the final (healed) result. A patch that changes nothing, or a retry that
never reaches the tool, is reported as *not attempted* rather than as a failure.

Captures carry `statusCode: 418` — the sentinel for a tool call rather than a
wire status. 418 is permanently reserved (RFC 2324 / RFC 9110), so no real API
failure can collide with the synthetic envelope. The `x-manifest-tool-call: mcp`
and `x-manifest-mcp-server` headers let the backend segment tool calls from plain
HTTP traffic. No Hermes middleware is used, so the plugin runs on any Hermes version.

## Measuring

With `MNFST_HEAL_LOG=1` (the default), every heal attempt and retry outcome appends
one JSON line to `$HERMES_TRACE_DIR/events.jsonl` (default `~/.hermes/logs/hermes-trace/`):

    heal_attempt  {tool, args, error, verdict: patched|no_patch|timeout|transport_error, patch?, attempt_id}
    heal_outcome  {tool, attempt_id, patched_args, retry_result: success|failed|not_attempted, status_code?}

The funnel — attempts → patches → retries succeeded — is the effectiveness measure.
The directory is created on first write. Logged arguments are the ones that
travelled: credential-named fields are withheld here as well as on the wire.

If the `mnfst` package happens to be installed in the Hermes environment, HTTP calls made by tools inside the Hermes process are healed at the transport level too. Set `MNFST_HEAL_HTTP=0` to skip that.

Environment: `MNFST_KEY`, `MNFST_URL` (optional), `MNFST_HEAL_TIMEOUT` (seconds, default 20), `MNFST_TOOLS` (extra external name prefixes), `MNFST_HOST_MAP` (`server=host,...` overrides for the capture host when a stdio server fronts a known API; a URL is reduced to its host), `MNFST_HEAL_HTTP`, `MNFST_HEAL_LOG` (measurement sink, default on).

## Privacy

Tool names, arguments, and error text of rejected external calls are sent to Manifest. Credential-named fields are withheld; nested business data is not.
