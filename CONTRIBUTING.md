# Contributing to Manifest for Hermes

Thanks for your interest in contributing to the Manifest plugin for Hermes!

## Prerequisites

- Python 3.10+
- pip

## Getting Started

1. Fork and clone the repository:

```bash
git clone https://github.com/<your-username>/manifest-hermes.git
cd manifest-hermes
python -m pip install -e ".[dev]"
```

The dev extras exist only to run the suite. The plugin itself is installed by
copy, with `hermes plugins install`, never with pip, so it must import with no
dependencies of its own.

## Development

```bash
python -m pytest -q
python -c "import manifest_heal, __init__"   # The import check CI also runs
```

CI runs the same commands on Python 3.10, 3.13 and 3.14, plus a `plugin.yaml`
validation.

Bump `version` in `plugin.yaml` when the plugin's behavior changes; that is what
`hermes plugins update manifest` reports.

## Making Changes

1. Create a branch from `master` for your change
2. Make your changes
3. Run tests to make sure everything passes
4. Write clear commit messages using conventional commits (e.g., `feat:`, `fix:`, `docs:`)
5. Open a pull request against `master`

## Commit Messages

Use conventional commit titles:
- `feat:` for new features (prepares a minor version)
- `fix:` for bug fixes (prepares a patch version)
- `docs:` for documentation changes
- `!` or `BREAKING CHANGE:` for breaking changes (prepares a major version)

## Supported Platforms

The plugin works with:
- Hermes, through the `transform_tool_result` hook
- MCP tools, from any MCP server

Built-in tools and local tools (terminal, file, memory) are never repaired. See
[the coverage table](README.md#what-is-covered).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
