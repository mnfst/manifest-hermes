# manifest-hermes

Hermes Agent plugin. When an MCP tool rejects its arguments, Manifest can supply an approved repair for one retry.

```sh
hermes plugins install mnfst/manifest-hermes
hermes plugins enable manifest
```

Set `MNFST_KEY` (your Manifest project key, with Autofix on) when prompted or in the Hermes env file, then restart Hermes. No other dependency.

## What it repairs

MCP tools only, from any MCP server. Hermes registers them under an `mcp-<server>` toolset.

HTTP and stdio MCP use the same hook flow. A tool error is captured even when its HTTP transport returned `200`. The capture carries explicit metadata and the tool's arguments:

```json
{
  "traceId": "unique-call-id",
  "target": {"protocol": "mcp", "server": "composio", "tool": "GMAIL_FETCH_EMAILS", "serviceUrl": "https://backend.composio.dev"},
  "request": {"body": {"limit": 500}},
  "response": {"isError": true, "body": {"error": {"message": "limit must be at most 100"}}}
}
```

`tool` is the name registered in Hermes. `serviceUrl` is optional: only the configured HTTP(S) origin travels, never the router path, query, or embedded credentials. A stdio server needs no URL. Manifest stores a logical `mcp://<server>/<tool>` identifier and applies repairs only to the arguments.

Built-in tools are never repaired, including `web_search`, terminal, file, and memory tools. Tools exposed by an MCP server are in scope, including local stdio servers. An unreadable registry heals nothing. `MNFST_TOOLS` explicitly opts additional name prefixes into this argument-repair flow.

## How it works

`transform_tool_result` sends a failed tool result to the Manifest heal API. With corrected arguments, the model is told to call the tool again; the `tool_request` middleware applies them on that call; `post_tool_call` reports `isError` for the retry. One heal, one retry per failure. Repairs stay within the originating session and task; calls without either identity are skipped. Withheld credential fields are restored locally before retry. Anything unexpected passes the original result through.

The default plugin has no runtime dependencies and does not install HTTP interception. To also heal ordinary API calls inside the Hermes process, install `mnfst` separately and set `MNFST_HEAL_HTTP=1`.

Version 0.2 requires Manifest's native MCP capture contract. Deploy the backend support before updating the plugin. An older backend rejects the new capture and the original tool error passes through. New errors still need an approved patch in Manifest before they can be repaired.

Environment: `MNFST_KEY`, `MNFST_URL` (optional), `MNFST_HEAL_TIMEOUT` (seconds, default 20), `MNFST_TOOLS` (extra external name prefixes), `MNFST_HEAL_HTTP`.

## Privacy

Tool names, arguments, and error text of rejected MCP calls (including local stdio servers) are sent to Manifest. Credential-named top-level argument fields are withheld; nested data and error text are not redacted.

## Development

```sh
uv venv .venv
uv pip install --python .venv/bin/python pytest 'mcp>=1.26,<2'
.venv/bin/python -m pytest -q
```

The transport tests run real MCP HTTP and stdio servers. To exercise the plugin against the real Manifest backend, run that repository's `hermes-plugin.e2e-spec.ts` with `MANIFEST_HERMES_ROOT` pointing to this checkout (including its `.venv`). Use a disposable test database.
