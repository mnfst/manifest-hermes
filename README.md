<div align="center">

![Manifest SDK Architecture](./docs/github-sdk.png)

# Manifest for Hermes

**The API resilience layer for your Hermes agent.**

[![CI](https://github.com/mnfst/manifest-hermes/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/mnfst/manifest-hermes/actions/workflows/ci.yml)
[![Hermes plugin](https://img.shields.io/badge/Hermes-plugin-942FFA)](https://github.com/mnfst/manifest-hermes)
[![License](https://img.shields.io/badge/license-MIT-blue)](plugin.yaml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

</div>

---

## What is Manifest

Manifest is the API resilience layer for your apps and agents. It works with every API they call: external services, your internal APIs and MCP tools.

* 🗺️ **See every API your agent depends on**, and how reliable each one is.
* 🎯 **Repair failed tool calls on the fly**, so your agent keeps working.
* 🛠️ **Know what to fix**, with a prompt for your coding agent.

This plugin brings that layer to [Hermes](https://github.com/NousResearch/hermes-agent). When an MCP tool call is rejected and Manifest has a patch for the error, Manifest repairs the arguments and the plugin retries the call. The model never sees the failure.

## How it works

![How the plugin works: a failed tool call is sent to Manifest with its error, patched, and retried once, returning a 200 OK](./docs/sdk-flow-diagram.png)

## Prerequisites

- <a href="https://github.com/NousResearch/hermes-agent" target="_blank">Hermes</a> with plugins enabled
- <a href="https://www.python.org/downloads/" target="_blank">Python 3.10</a> or higher

## Get started

1. Create a project in your [Manifest dashboard](https://dashboard.manifest.build) and copy its project key.

2. Update Hermes, then install and enable the plugin. Older versions of Hermes refuse it:

   ```sh
   hermes update
   hermes plugins install mnfst/manifest-hermes
   hermes plugins enable manifest
   ```

3. Store your key in the settings of Hermes:

   ```sh
   hermes config set MNFST_KEY your-project-key
   ```

4. Start a new Hermes session so the plugin loads. If you run the Hermes gateway, restart it with `hermes gateway restart`.

That is the whole setup. `MNFST_URL` points the plugin at another Manifest endpoint if you need one. No other dependency and no other configuration.

## Try it

When your agent makes a tool call that the MCP server rejects, and Manifest has a patch for that error, the plugin repairs the arguments and retries:

```
You: fetch my 5 most recent emails

  GMAIL_FETCH_EMAILS { max_results: "5" }   ← rejected, max_results must be an integer
  GMAIL_FETCH_EMAILS { max_results: 5 }     ← Manifest's patch, retried automatically

Agent: Here are your 5 most recent emails…
```

The agent only ever sees the healed result. A rejection Manifest has no patch for yet reaches the agent unchanged, and appears in your [Manifest dashboard](https://dashboard.manifest.build), grouped in an issue.

## What is covered

| The agent calls… | Covered |
| --- | --- |
| An MCP tool, on a server reached over HTTP | ✅ healed |
| An MCP tool, on a server that runs as a program on your machine (stdio) | ❌ not seen |
| A built-in tool, including API-backed ones such as `web_search` | ❌ never repaired |
| A local tool (terminal, file, memory) | ❌ never sent anywhere |

Hermes registers MCP tools under an `mcp-<server>` toolset. A tool the registry cannot place counts as local, so an unreadable registry heals nothing rather than everything.

Hermes places a capture under the MCP server that rejected the call, with each tool as its own endpoint.

## How the repair works

`transform_tool_result` sends a failed tool result to the Manifest heal API. When corrected arguments come back, the plugin **re-invokes the tool itself** and returns the healed result. The model never sees Manifest, a retry instruction, or the repair. One heal and one internal retry per failure; anything unexpected passes the original result through.

The retry goes through Hermes' own call path, so every hook and guard that ran on the first call runs again. [How the retry, the 418 sentinel and the measurement log work](docs/guide.md).

## Updating

```sh
hermes plugins update manifest
```

That pulls the latest commit into `~/.hermes/plugins/manifest`. Restart Hermes afterwards so the new code loads. If the install is pinned to a commit or is not a git checkout (a copied directory), Hermes refuses to pull; reinstall instead:

```sh
hermes plugins install mnfst/manifest-hermes --force
```

## Choosing which tool calls reach Manifest

Keep tool calls out of Manifest entirely: they are neither repaired nor tracked, and nothing about them leaves your agent. A tool call is matched as `https://<MCP server host>/<tool name>`, so each entry is a domain or a domain with a tool name:

```sh
MNFST_ALLOWLIST=linear.app                      # only Linear's MCP server
MNFST_DENYLIST=linear.app/delete_issue,internal.example.com   # never these
```

- A domain covers its subdomains, with or without a tool name: `linear.app` and `linear.app/list_issues` both match `mcp.linear.app`.
- A scheme or port in an entry is ignored. `*` is not supported yet: the entry is skipped with a warning in the Hermes log, and an allowlist made only of skipped entries lets nothing through.
- The denylist wins over the allowlist. With no allowlist, every MCP tool call over HTTP is eligible.
- Paths are compared decoded, with `.` and `..` resolved, so `%6Cist_issues` matches `list_issues`.

## Privacy

Tool names, arguments, and error text of rejected external calls are sent to Manifest. Credential-named fields are withheld; nested business data is not.

Every other MCP tool call to a server reached over HTTP is reported as metadata only: the server, the tool, whether it worked, and how long it took. Never its arguments or result. Local tools are never reported. [Details](docs/guide.md#every-tool-call-is-tracked).

## More

[Documentation](https://docs.manifest.build) · [Configuration, limits & development](docs/guide.md) · [Node.js SDK](https://github.com/mnfst/manifest-node) · [Python SDK](https://github.com/mnfst/manifest-python) · [PHP SDK](https://github.com/mnfst/manifest-php) · [Website](https://manifest.build)
