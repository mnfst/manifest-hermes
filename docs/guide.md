# Guide

Configuration, limits and development for the Manifest Hermes plugin. The
[README](../README.md) covers install and setup.

## Configuration

Only `MNFST_KEY` is required. Everything below has a working default.

| Variable | What it does |
| --- | --- |
| `MNFST_KEY` | Your Manifest project key. Required; without it the plugin registers nothing. |
| `MNFST_URL` | Point at another Manifest endpoint. |
| `MNFST_HEAL_TIMEOUT` | Seconds to wait for a patch. Default `20`. |
| `MNFST_TOOLS` | Comma-separated name prefixes to treat as external. See below. |
| `MNFST_HOST_MAP` | `server=host,...` overrides for the capture host when a stdio server fronts a known API. A URL is reduced to its host. |
| `MNFST_HEAL_HTTP` | Set to `0` to skip transport-level healing of HTTP calls made inside the Hermes process. |
| `MNFST_HEAL_LOG` | Measurement sink. Default on. |
| `MNFST_HEAL_DEBUG` | Set to `1` to log the raw heal request and response to the trace file. |

All of them are read once, at registration. The environment cannot change
without a restart.

### Healing a tool that is not MCP

A tool is repaired when Hermes registered it under an `mcp-<server>` toolset.
Everything else counts as local, including a built-in tool that reaches an API,
so an unreadable registry heals nothing rather than everything.

`MNFST_TOOLS` is the opt-in for a tool outside that rule, and the prefix you
list names the service in Manifest:

```sh
export MNFST_TOOLS='stripe_,shopify_'
```

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

With `MNFST_HEAL_LOG=1` (the default), every heal attempt and retry outcome
appends one JSON line to `$HERMES_TRACE_DIR/events.jsonl` (default
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
