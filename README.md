<div align="center">

![Manifest SDK Architecture](./docs/github-sdk.png)

# Manifest for Hermes

**Turn 🔴 rejected tool calls into 🟢 successful ones in real time.**

[![CI](https://github.com/mnfst/manifest-hermes/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/mnfst/manifest-hermes/actions/workflows/ci.yml)
[![Hermes plugin](https://img.shields.io/badge/Hermes-plugin-942FFA)](https://github.com/mnfst/manifest-hermes)
[![License](https://img.shields.io/badge/license-MIT-blue)](plugin.yaml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

</div>

---

## What is Manifest

Manifest is a self-healing layer that fixes and retries failed API requests on the fly.

* 🎯 **Fix failures automatically** before they impact your users.
* 🔔 **Get notified of root causes** so you can fix them permanently.
* 🔌 **Works across your stack** with internal APIs, external services, and agent tools.

This plugin brings that layer to [Hermes](https://github.com/NousResearch/hermes-agent): an MCP tool call is rejected, Manifest repairs the arguments, the retry succeeds. The model never sees the failure.

## How it works

![How Manifest heals a failed request: a 400 reaches Manifest, drops to a patch from the knowledge base or the healing agents, and is retried once, returning a 200 OK](./docs/sdk-flow-diagram.png)

## Prerequisites

- <a href="https://github.com/NousResearch/hermes-agent" target="_blank">Hermes</a> with plugins enabled
- <a href="https://www.python.org/downloads/" target="_blank">Python 3.10</a> or higher

## Get started

```sh
hermes plugins install mnfst/manifest-hermes
hermes plugins enable manifest
```

No other dependency.

## Setup

1. Create a project in your [Manifest dashboard](https://app-staging.manifest.build) and copy its project key.
2. Set the key as an environment variable, when prompted or in the Hermes env file:

```sh
export MNFST_KEY='your-project-key'
```

3. Restart Hermes so the plugin loads.

Self-healing is enabled by default in your project settings.

## Try it

Let your agent make a tool call that would normally be rejected. Manifest catches it, repairs the arguments, and retries:

```
You: fetch my 5 most recent emails

  GMAIL_FETCH_EMAILS { max_results: "5" }   ← rejected, max_results must be an integer
  GMAIL_FETCH_EMAILS { max_results: 5 }     ← Manifest's patch, retried automatically

Agent: Here are your 5 most recent emails…
```

The agent only ever sees the healed result. Check your [Manifest dashboard](https://app-staging.manifest.build) to see all repairs and insights.

## What is covered

| The agent calls… | Covered |
| --- | --- |
| An MCP tool, from any MCP server | ✅ healed |
| A tool named in `MNFST_TOOLS` | ✅ healed |
| A built-in tool, including API-backed ones such as `web_search` | ❌ never repaired |
| A local tool (terminal, file, memory) | ❌ never sent anywhere |
| An HTTP call made by a tool inside the Hermes process | ✅ healed when the `mnfst` package is installed |

Hermes registers MCP tools under an `mcp-<server>` toolset. A tool the registry cannot place counts as local, so an unreadable registry heals nothing rather than everything.

A capture carries the MCP server's real host and the tool as the path, for example `https://backend.composio.dev/GMAIL_FETCH_EMAILS`, so the service in Manifest is the server that rejected the call and each tool is its own endpoint. The router's real path is not used: it carries a per-agent session id. When the host cannot be read from the Hermes configuration, the URL falls back to `mcp://<server>/<tool>`.

## How the repair works

`transform_tool_result` sends a failed tool result to the Manifest heal API. When corrected arguments come back, the plugin **re-invokes the tool itself** with the patched arguments and returns the healed result. The model never sees Manifest, a retry instruction, or the repair — it only ever sees the tool's response, healed or not. One heal and one internal retry per failure; anything unexpected passes the original result through.

The retry is a real Hermes tool call (`handle_function_call`), not a bare registry dispatch: it runs through the `pre_tool_call` policy hooks, edit approval and the tool-execution middleware with the original call's identity, so server-dictated arguments get every check the first call got and a policy plugin sees the retry. The agent loop owns `post_tool_call` and fires it once per model-facing call, with the final (healed) result. A patch that changes nothing, or a retry that never reaches the tool, is reported as *not attempted* rather than as a failure.

Captures carry `statusCode: 418` — the sentinel for a tool call rather than a wire status. 418 is permanently reserved (RFC 2324 / RFC 9110), so no real API failure can collide with the synthetic envelope. The `x-manifest-tool-call: mcp` and `x-manifest-mcp-server` headers let the backend segment tool calls from plain HTTP traffic. No Hermes middleware is used, so the plugin runs on any Hermes version.

## Measuring

With `MNFST_HEAL_LOG=1` (the default), every heal attempt and retry outcome appends one JSON line to `$HERMES_TRACE_DIR/events.jsonl` (default `~/.hermes/logs/hermes-trace/`):

```
heal_attempt  {tool, args, error, verdict: patched|no_patch|timeout|transport_error, patch?, attempt_id}
heal_outcome  {tool, attempt_id, patched_args, retry_result: success|failed|not_attempted, status_code?}
```

The funnel — attempts → patches → retries succeeded — is the effectiveness measure. The directory is created on first write. Logged arguments are the ones that travelled: credential-named fields are withheld here as well as on the wire.

## Configuration

| Variable | What it does |
| --- | --- |
| `MNFST_KEY` | Your Manifest project key. Required. |
| `MNFST_URL` | Override the Manifest endpoint. Optional. |
| `MNFST_HEAL_TIMEOUT` | Seconds to wait for a patch. Default `20`. |
| `MNFST_TOOLS` | Extra external name prefixes to treat as healable. |
| `MNFST_HOST_MAP` | `server=host,...` overrides for the capture host when a stdio server fronts a known API. A URL is reduced to its host. |
| `MNFST_HEAL_HTTP` | Set to `0` to skip transport-level healing of HTTP calls made inside the Hermes process. |
| `MNFST_HEAL_LOG` | Measurement sink. Default on. |
| `MNFST_HEAL_DEBUG` | Set to `1` to log the raw heal request and response to the trace file. |

## Updating

```sh
hermes plugins update manifest
```

That pulls the latest commit into `~/.hermes/plugins/manifest`. Restart Hermes afterwards so the new code loads. If the install is pinned to a commit or is not a git checkout (a copied directory), Hermes refuses to pull; reinstall instead:

```sh
hermes plugins install mnfst/manifest-hermes --force
```

## Privacy

Tool names, arguments, and error text of rejected external calls are sent to Manifest. Credential-named fields are withheld; nested business data is not.

## More

[Node.js SDK](https://github.com/mnfst/manifest-node) · [Python SDK](https://github.com/mnfst/manifest-python) · [PHP SDK](https://github.com/mnfst/manifest-php) · [Website](https://manifest.build)
