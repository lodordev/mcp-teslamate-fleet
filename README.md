# Tesla MCP Server

MCP server for Tesla vehicles combining **TeslaMate** historical analytics with **Fleet API** live data and commands. Works with Claude Code, Claude Desktop, Cursor, and any MCP-compatible client.

The first MCP server to bring both data sources together — use TeslaMate for deep analytics and Fleet API for real-time control, or configure just one.

## Features

**29 tools** across four categories:

| Category | Tools | Backend |
|----------|-------|---------|
| **Status & History** | `tesla_status`, `tesla_drives`, `tesla_charging_history`, `tesla_battery_health`, `tesla_efficiency`, `tesla_location_history`, `tesla_state_history`, `tesla_software_updates` | TeslaMate |
| **Analytics** | `tesla_savings`, `tesla_trip_cost`, `tesla_efficiency_by_temp`, `tesla_charging_by_location`, `tesla_top_destinations`, `tesla_longest_trips`, `tesla_monthly_summary`, `tesla_vampire_drain` | TeslaMate |
| **Live Data** | `tesla_live` | Fleet API |
| **Commands** | `tesla_climate_on/off`, `tesla_set_temp`, `tesla_charge_start/stop`, `tesla_set_charge_limit`, `tesla_lock`, `tesla_unlock`, `tesla_honk`, `tesla_flash`, `tesla_trunk`, `tesla_sentry` | Fleet API |

**Safety:** `unlock` and `trunk` commands require `confirm=True`. All commands are rate-limited to 40/day.

## Quick Start

### Claude Code (`.mcp.json`)

```json
{
  "mcpServers": {
    "tesla": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/lodordev/mcp-tesla", "tesla-mcp"],
      "env": {
        "TESLAMATE_DB_HOST": "localhost",
        "TESLAMATE_DB_PASS": "your_password",
        "TESLA_VIN": "your_vin",
        "TESLA_TOKEN_FILE": "/path/to/tokens.json",
        "TESLA_CLIENT_ID": "your_client_id",
        "TESLA_CLIENT_SECRET": "your_client_secret",
        "TESLA_PROXY_URL": "https://localhost:4443",
        "TESLA_VERIFY_SSL": "false"
      }
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

Same structure — add under `mcpServers`.

### Local install

```bash
git clone https://github.com/lodordev/mcp-tesla
cd tesla-mcp
pip install -e .
```

## Prerequisites

You need **at least one** of these backends configured. Both is ideal.

### TeslaMate (analytics + history)

[TeslaMate](https://github.com/teslamate-org/teslamate) is an open-source Tesla data logger. It records driving, charging, and vehicle state to a Postgres database.

If you already run TeslaMate, you just need the database connection details. If not, see the [TeslaMate installation guide](https://docs.teslamate.org/docs/installation/docker).

### Fleet API (live data + commands)

Tesla's Fleet API provides real-time vehicle data and remote commands. Setup requires:

1. **Register an app** at [developer.tesla.com](https://developer.tesla.com)
2. **Generate OAuth tokens** — use Tesla's [token generation flow](https://developer.tesla.com/docs/fleet-api/getting-started/what-is-fleet-api)
3. **Set up the HTTP proxy** — commands must be signed using the [Tesla Vehicle Command Protocol](https://github.com/teslamotors/vehicle-command). Deploy the `tesla-http-proxy` from that repo

The proxy is only needed for commands (`tesla_climate_on`, `tesla_lock`, etc.). `tesla_live` works with just a token.

Tesla provides a **$10/month free credit** for Fleet API, which is more than enough for personal MCP use.

## Configuration

All configuration is via environment variables.

### TeslaMate Database

| Variable | Default | Description |
|----------|---------|-------------|
| `TESLAMATE_DB_HOST` | *(required)* | Postgres host |
| `TESLAMATE_DB_PORT` | `5432` | Postgres port |
| `TESLAMATE_DB_USER` | `teslamate` | Postgres user |
| `TESLAMATE_DB_PASS` | *(required)* | Postgres password |
| `TESLAMATE_DB_NAME` | `teslamate` | Database name |

### Fleet API

| Variable | Default | Description |
|----------|---------|-------------|
| `TESLA_VIN` | *(required)* | Vehicle VIN |
| `TESLA_TOKEN_FILE` | *(required)* | Path to `tokens.json` with OAuth tokens |
| `TESLA_CLIENT_ID` | | Fleet API client ID (for token refresh) |
| `TESLA_CLIENT_SECRET` | | Fleet API client secret (for token refresh) |
| `TESLA_PROXY_URL` | | HTTP proxy URL for commands |
| `TESLA_FLEET_URL` | NA region | Fleet API endpoint ([regional options](https://developer.tesla.com/docs/fleet-api/getting-started/base-urls)) |
| `TESLA_VERIFY_SSL` | `true` | Set `false` for self-signed proxy certs |

### Vehicle Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TESLA_CAR_ID` | `1` | TeslaMate car ID (for multi-car instances) |
| `TESLA_BATTERY_KWH` | `75` | Usable battery capacity in kWh |
| `TESLA_BATTERY_RANGE_KM` | `525` | EPA range at 100% in km |

Energy consumption is estimated from ideal range deltas using these values. Adjust for your vehicle:

| Vehicle | Battery (kWh) | Range (km) |
|---------|--------------|------------|
| Model 3 Standard Range | 54 | 350 |
| Model 3 Long Range | 75 | 500 |
| Model Y Long Range | 75 | 525 |
| Model S Long Range | 100 | 650 |
| Model X Long Range | 100 | 560 |

### Cost Defaults

These are defaults — `tesla_savings` and `tesla_trip_cost` accept per-call overrides.

| Variable | Default | Description |
|----------|---------|-------------|
| `TESLA_ELECTRICITY_RATE` | `0.12` | Electricity cost in $/kWh |
| `TESLA_GAS_PRICE` | `3.50` | Gas price in $/gallon (for comparison) |
| `TESLA_GAS_MPG` | `28` | Comparable gas vehicle MPG |

## Architecture

Single-file Python server (~1400 lines) using [FastMCP](https://github.com/jlowin/fastmcp). Two data paths:

```
┌─────────────┐     ┌──────────────┐
│  TeslaMate   │────▶│   Postgres   │──┐
│  (logger)    │     │  (telemetry) │  │
└─────────────┘     └──────────────┘  │   ┌───────────┐     ┌────────────┐
                                       ├──▶│ tesla.py  │────▶│ MCP Client │
┌─────────────┐     ┌──────────────┐  │   │ (server)  │     │ (Claude,   │
│  Tesla       │────▶│ HTTP Proxy   │──┘   └───────────┘     │  Cursor)   │
│  Fleet API   │     │ (commands)   │                         └────────────┘
└─────────────┘     └──────────────┘
```

## Limitations

- **Single vehicle** — queries use a configurable `car_id` but tools don't accept it as a parameter. Multi-car users should run separate server instances.
- **Imperial units** — output is in miles, °F, and PSI. Metric support is planned.
- **Estimated kWh** — TeslaMate's `drives` table doesn't include energy consumed directly. We estimate from ideal range deltas using your configured battery capacity. Accuracy is ~90-95%.

## Credits

Inspired by [cobanov/teslamate-mcp](https://github.com/cobanov/teslamate-mcp). Built with [FastMCP](https://github.com/jlowin/fastmcp) and [TeslaMate](https://github.com/teslamate-org/teslamate).

## License

MIT
