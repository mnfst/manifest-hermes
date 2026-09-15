# manifest-hermes

Hermes Agent plugin. A tool call is rejected, Manifest repairs the arguments, the retry succeeds.

```sh
hermes plugins install mnfst/manifest-hermes
hermes plugins enable manifest
```

Set `MNFST_KEY` (your Manifest project key, with Autofix on) when prompted or in the Hermes env file, then restart Hermes. No other dependency.

## What it repairs

MCP tools only, from any MCP server. Hermes registers them under an `mcp-<server>` toolset, and the server names the service in Manifest.

Built-in tools are never repaired, including API-backed ones such as `web_search`. Local tools (terminal, file, memory) are never sent anywhere. A tool the registry cannot place counts as local, so an unreadable registry heals nothing rather than everything. `MNFST_TOOLS` is the explicit opt-in for a tool outside that rule.

## How it works

`transform_tool_result` sends a failed tool result to the Manifest heal API. With corrected arguments, the model is told to call the tool again; the `tool_request` middleware applies them on that call; `post_tool_call` reports the outcome. One heal, one retry per failure. Anything unexpected passes the original result through.

If the `mnfst` package happens to be installed in the Hermes environment, HTTP calls made by tools inside the Hermes process are healed at the transport level too. Set `MNFST_HEAL_HTTP=0` to skip that.

Environment: `MNFST_KEY`, `MNFST_URL` (optional), `MNFST_HEAL_TIMEOUT` (seconds, default 20), `MNFST_TOOLS` (extra external name prefixes), `MNFST_HEAL_HTTP`.

## Privacy

Tool names, arguments, and error text of rejected external calls are sent to Manifest. Credential-named fields are withheld; nested business data is not.
