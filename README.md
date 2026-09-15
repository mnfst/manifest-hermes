# manifest-hermes

Hermes Agent plugin. A tool call is rejected, Manifest repairs the arguments, the retry succeeds.

```sh
hermes plugins install mnfst/manifest-hermes
hermes plugins enable manifest
```

Set `MNFST_KEY` (your Manifest project key, with Autofix on) when prompted or in the Hermes env file, then restart Hermes. No other dependency.

## What it repairs

Calls to external services only. Two signals mark one, neither tied to a vendor:

- an `mcp-<server>` toolset, which is how Hermes registers every MCP server's tools;
- a non-empty `requires_env`, which is how a built-in tool declares the credential it needs to reach its API (`web_search` and the like).

Local tools (terminal, file, memory) declare neither. They are never sent anywhere and never rewritten. A tool that matches neither signal counts as local, so an unreadable registry heals nothing rather than everything. If a tool is missed, list its name prefix in `MNFST_TOOLS`.

## How it works

`transform_tool_result` sends a failed tool result to the Manifest heal API. With corrected arguments, the model is told to call the tool again; the `tool_request` middleware applies them on that call; `post_tool_call` reports the outcome. One heal, one retry per failure. Anything unexpected passes the original result through.

If the `mnfst` package happens to be installed in the Hermes environment, HTTP calls made by tools inside the Hermes process are healed at the transport level too. Set `MNFST_HEAL_HTTP=0` to skip that.

Environment: `MNFST_KEY`, `MNFST_URL` (optional), `MNFST_HEAL_TIMEOUT` (seconds, default 20), `MNFST_TOOLS` (extra external name prefixes), `MNFST_HEAL_HTTP`.

## Privacy

Tool names, arguments, and error text of rejected external calls are sent to Manifest. Credential-named fields are withheld; nested business data is not.
