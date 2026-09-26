# Changelog

## [0.5.0](https://github.com/mnfst/manifest-hermes/compare/v0.4.0...v0.5.0) (2026-09-26)


### ⚠ BREAKING CHANGES

* the transform_tool_result hook, the synthetic 418 tool call captures and the plugin's own heal and tracking code are removed. Rejected MCP tool calls are no longer repaired.

### Features

* start the Manifest Python SDK inside Hermes instead of healing tool calls ([#20](https://github.com/mnfst/manifest-hermes/issues/20)) ([12fee74](https://github.com/mnfst/manifest-hermes/commit/12fee746cafc2f74173998c231cfdc57cf0ff0dc))

## [0.4.0](https://github.com/mnfst/manifest-hermes/compare/v0.3.0...v0.4.0) (2026-09-23)


### Features

* track every MCP tool call as metadata ([bf5ea59](https://github.com/mnfst/manifest-hermes/commit/bf5ea591f0429bd434c17ad4cf4e5db46a545feb))
* track every MCP tool call as metadata ([ca19da0](https://github.com/mnfst/manifest-hermes/commit/ca19da04041c51aea6371f4cbf4d19ca2a3c3388))


### Bug Fixes

* leave stdio MCP servers alone, healing included ([2b7d320](https://github.com/mnfst/manifest-hermes/commit/2b7d32084fea9225924c1a2c212b1b26e453d298))
* track only tool calls on HTTP MCP servers ([9e58633](https://github.com/mnfst/manifest-hermes/commit/9e586330e375e300f4c05b83a4243e4e34971a01))
