# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-03-23

### Added
- Initial release with 29 tools across TeslaMate + Fleet API
- 8 read tools: status, drives, charging history, battery health, efficiency, location history, state history, software updates
- 8 analytics tools: savings, trip cost, efficiency by temp, charging by location, top destinations, longest trips, monthly summary, vampire drain
- 1 live data tool via Fleet API
- 12 command tools: climate, charging, locks, horn, lights, trunk, sentry mode
- Graceful degradation — works with TeslaMate only, Fleet API only, or both
- Configurable battery capacity, electricity rate, gas price via env vars
- Safety: confirm=True required for unlock/trunk, 40 commands/day rate limit
- Glama inspection support (glama.json)
- CI linting with ruff
- PyPI package (`mcp-teslamate-fleet`) via GitHub Actions trusted publishing

[0.1.0]: https://github.com/lodordev/mcp-teslamate-fleet/releases/tag/v0.1.0
