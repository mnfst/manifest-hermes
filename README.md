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
* 🎯 **Repair failed API requests on the fly**, so your agent keeps working.
* 🛠️ **Know what to fix**, with a prompt for your coding agent.

This plugin brings that layer to [Hermes](https://github.com/NousResearch/hermes-agent). It installs the [Manifest Python SDK](https://github.com/mnfst/manifest-python) and starts it inside every Hermes process: the interactive session, the gateway and its workers. From there, every HTTP call Hermes makes is tracked, and a failure Manifest has a patch for is repaired and retried before Hermes sees it.

## How it works

![How the plugin works: a failed request is sent to Manifest with its error, patched, and retried once, returning a 200 OK](./docs/sdk-flow-diagram.png)

## Prerequisites

- <a href="https://github.com/NousResearch/hermes-agent" target="_blank">Hermes</a> with plugins enabled
- <a href="https://www.python.org/downloads/" target="_blank">Python 3.10</a> or higher

## Get started

1. Create a project in your [Manifest dashboard](https://dashboard.manifest.build) and copy its project key.

2. Update Hermes, then install and enable the plugin. Hermes installs the SDK (`mnfst`) with it:

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

That is the whole setup. `MNFST_URL` points the SDK at another Manifest endpoint if you need one.

## Try it

Ask your agent for something that reaches an API: search the web, or use a hosted MCP server. Each call appears in your [Manifest dashboard](https://dashboard.manifest.build) under the service it reached, with its status and how long it took.

When a call is rejected with a 4xx error, the request and the error are sent to Manifest. A rejection Manifest has no patch for yet reaches Hermes unchanged, and appears in the dashboard, grouped in an issue. Once there is a patch, the next request that fails the same way is repaired and retried, and Hermes only ever sees the answer to the retry.

## What is covered

| Hermes… | Covered |
| --- | --- |
| Calls an API over httpx or requests: web search and extract, vision, X search, the LLM itself | ✅ tracked and healed |
| Uses an MCP server reached over HTTP | ✅ tracked; a rejected tool call is not healed |
| Calls an API over aiohttp or urllib: Home Assistant, the Discord, Feishu and Slack adapters | ❌ not seen yet |
| Uses an MCP server that runs as a program on your machine (stdio) | ❌ not seen, no HTTP involved |
| Runs a CLI or a script (curl, gh, a Python script) | ❌ not seen, a separate process |

An MCP server answers HTTP 200 even when it rejects a tool call: the error travels in the JSON-RPC body. The SDK tracks those calls as 200 and only sends 4xx responses to heal, so a rejected tool call is not healed.

Which calls are healed, and how, is set per service in the dashboard.

## Updating

```sh
hermes plugins update manifest
```

That pulls the latest commit into `~/.hermes/plugins/manifest`. Restart Hermes afterwards so the new code loads. If the install is pinned to a commit or is not a git checkout (a copied directory), Hermes refuses to pull; reinstall instead:

```sh
hermes plugins install mnfst/manifest-hermes --force
```

## Choosing which calls reach Manifest

Keep calls out of Manifest entirely: they are neither repaired nor tracked, and nothing about them leaves your agent. Each entry is a domain or a domain with a path:

```sh
hermes config set MNFST_ALLOWLIST api.tavily.com,mcp.linear.app   # only these services
hermes config set MNFST_DENYLIST api.openai.com                    # never this one
```

- A domain covers its subdomains, with or without a path: `linear.app` and `linear.app/mcp` both match `mcp.linear.app`.
- A path matches whole segments, and is case-sensitive.
- A scheme, port, query or fragment in an entry is ignored. `*` in a path is not supported yet: the entry is skipped with a warning.
- The denylist wins over the allowlist. With no allowlist, every call is eligible.

The lists are read by the SDK, from mnfst 1.3.0.

## Privacy

Every call is reported as metadata only: method, URL without its query string, status and timing. A call rejected with a 4xx error, other than 401, 402, 403 and 429, is sent in full so it can be repaired, with credential-named fields withheld. [What the SDK sends](https://github.com/mnfst/manifest-python/blob/main/docs/guide.md#data-sent-to-manifest).

## More

[Documentation](https://docs.manifest.build) · [Configuration, limits & development](docs/guide.md) · [Node.js SDK](https://github.com/mnfst/manifest-node) · [Python SDK](https://github.com/mnfst/manifest-python) · [PHP SDK](https://github.com/mnfst/manifest-php) · [Website](https://manifest.build)
