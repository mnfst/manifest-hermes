# Guide

Configuration and development for the Manifest Hermes plugin. The
[README](../README.md) covers install and setup.

## What the plugin does

Nothing but start the [Manifest Python SDK](https://github.com/mnfst/manifest-python)
(`mnfst`) in each Hermes process. Healing, tracking, credential masking and the
allowlist live in the SDK; its
[guide](https://github.com/mnfst/manifest-python/blob/main/docs/guide.md)
covers how each one works.

- **Install.** Hermes reads the plugin's dependencies from `[project].dependencies`
  in `pyproject.toml` when you install or enable it, and installs them again after
  `hermes update` rebuilds its virtual environment. Dependabot raises the
  `mnfst` version there with each SDK release.
- **Start.** Hermes calls `register()` in every Hermes process: the interactive
  session, the gateway and its workers. It has loaded `~/.hermes/.env` by then,
  so a key set with `hermes config set MNFST_KEY` is in the environment.
  `register()` calls `mnfst.manifest()` once, which patches the HTTP clients of
  that process.
- **Fail open.** Without `MNFST_KEY`, with `mnfst` missing, or when the SDK fails
  to start, the plugin logs a warning and Hermes runs as if it were not installed.

The plugin registers no Hermes hook.

## Why not `mnfst run hermes`

`mnfst run` starts the SDK through a `sitecustomize` on `PYTHONPATH`. In Hermes:

- the gateway runs as a service, so its launch command would have to be edited;
- `sitecustomize` checks `MNFST_KEY` before Hermes loads `~/.hermes/.env`;
- `hermes update` can rebuild the virtual environment and drop a hand-installed
  `mnfst`;
- `PYTHONPATH` and `MNFST_KEY` pass down to every Python script the agent runs,
  which would then report to Manifest too.

## Configuration

All read by the SDK, once, when the plugin starts it. The environment cannot
change without a restart.

| Variable | What it does |
| --- | --- |
| `MNFST_KEY` | Your Manifest project key. Required; without it the plugin starts nothing. |
| `MNFST_URL` | Point at another Manifest endpoint. Optional. |
| `MNFST_ALLOWLIST` | Only these calls reach Manifest. Optional ([entries](../README.md#choosing-which-calls-reach-manifest)). |
| `MNFST_DENYLIST` | These calls never reach Manifest; wins over the allowlist. Optional. |

## MCP servers

Hermes' MCP client sends its requests through httpx2, which the SDK patches, so
every call to an MCP server reached over HTTP is tracked, under the server's
host. A server that runs as a local process (stdio) makes no HTTP call and is not
seen.

An MCP server answers HTTP 200 even when it rejects a tool call: the error
travels in the JSON-RPC body. The SDK tracks the call as a 200 and only sends
4xx responses to heal, so a rejected tool call is not healed.

Before connecting to a server, Hermes checks it with requests that are expected
to fail, such as a `HEAD` answered with 405 and a `GET` answered with 400. Those
are 4xx responses, so the SDK sends them to heal like any other; Manifest
answers that it has no patch.

Hermes checks a tool call's arguments against the tool's schema before sending
it. A call that fails that check, such as a string where the schema asks for an
integer, never reaches the server, so no request is made and Manifest sees
nothing.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
```

CI runs the same commands on Python 3.10, 3.13 and 3.14, plus an import check
and a `plugin.yaml` validation.
