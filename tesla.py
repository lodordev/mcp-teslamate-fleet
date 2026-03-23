"""Tesla MCP Server — TeslaMate analytics + Fleet API commands.

Single-file FastMCP server. Stdio transport. Works with TeslaMate, Fleet API,
or both — tools are available based on which backends you configure.

Two data paths:
  1. TeslaMate Postgres (read-only) — historical telemetry and analytics
  2. Tesla Fleet API via HTTP proxy (commands + live data)

Read tools (TeslaMate):
  tesla_status            — Current vehicle state (battery, range, location, climate)
  tesla_charging_history  — Charging sessions over N days
  tesla_drives            — Recent drives with distance, duration, efficiency
  tesla_battery_health    — Battery degradation trend
  tesla_efficiency        — Wh/mi consumption trends
  tesla_location_history  — Where the car has been, time at each location
  tesla_state_history     — Vehicle state transitions (online/asleep/offline)
  tesla_software_updates  — Firmware version history

Analytics tools (TeslaMate):
  tesla_savings           — Gas savings scorecard
  tesla_trip_cost         — Estimate trip cost to a destination
  tesla_efficiency_by_temp — Efficiency curve by temperature
  tesla_charging_by_location — Charging patterns by location
  tesla_top_destinations  — Most visited locations
  tesla_longest_trips     — Top drives ranked by distance
  tesla_monthly_summary   — Monthly driving summary
  tesla_vampire_drain     — Battery loss while parked

Live data tool (Fleet API):
  tesla_live              — Real-time vehicle data from Fleet API

Command tools (Fleet API + HTTP proxy):
  tesla_climate_on        — Start climate preconditioning
  tesla_climate_off       — Stop climate
  tesla_set_temp          — Set cabin temperature
  tesla_charge_start      — Start charging
  tesla_charge_stop       — Stop charging
  tesla_set_charge_limit  — Set charge limit percentage
  tesla_lock              — Lock doors
  tesla_unlock            — Unlock doors (requires confirm=True)
  tesla_honk              — Honk horn
  tesla_flash             — Flash headlights
  tesla_trunk             — Open/close trunk or frunk (requires confirm=True)
  tesla_sentry            — Toggle sentry mode

Environment variables:
  # TeslaMate Postgres
  TESLAMATE_DB_HOST     — Postgres host (e.g. localhost, 192.168.1.50)
  TESLAMATE_DB_PORT     — Postgres port (default: 5432)
  TESLAMATE_DB_USER     — Postgres user (default: teslamate)
  TESLAMATE_DB_PASS     — Postgres password
  TESLAMATE_DB_NAME     — Postgres database (default: teslamate)

  # Fleet API
  TESLA_PROXY_URL       — HTTP proxy URL for commands (e.g. https://localhost:4443)
  TESLA_FLEET_URL       — Fleet API URL (default: NA region)
  TESLA_VIN             — Vehicle VIN
  TESLA_TOKEN_FILE      — Path to tokens.json (Fleet API OAuth tokens)
  TESLA_CLIENT_ID       — Fleet API client ID (for token refresh)
  TESLA_CLIENT_SECRET   — Fleet API client secret (for token refresh)

  # Vehicle config
  TESLA_CAR_ID          — TeslaMate car ID (default: 1)
  TESLA_BATTERY_KWH     — Usable battery capacity in kWh (default: 75)
  TESLA_BATTERY_RANGE_KM — EPA range at 100% in km (default: 525)

  # Cost defaults (overridable per-tool-call)
  TESLA_ELECTRICITY_RATE — $/kWh (default: 0.12)
  TESLA_GAS_PRICE        — $/gallon for comparison (default: 3.50)
  TESLA_GAS_MPG          — Comparable gas vehicle MPG (default: 28)

  # TLS
  TESLA_VERIFY_SSL      — TLS verification for proxy (default: true)

Safety:
  - Unlock, trunk, and window commands require confirm=True parameter
  - Rate limit: 40 commands/day (hard cap)
  - All commands counted for audit trail
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras
from fastmcp import FastMCP

# -- Configuration ------------------------------------------------------------

# TeslaMate Postgres (read-only telemetry)
DB_HOST = os.environ.get("TESLAMATE_DB_HOST", "")
DB_PORT = int(os.environ.get("TESLAMATE_DB_PORT", "5432"))
DB_USER = os.environ.get("TESLAMATE_DB_USER", "teslamate")
DB_PASS = os.environ.get("TESLAMATE_DB_PASS", "")
DB_NAME = os.environ.get("TESLAMATE_DB_NAME", "teslamate")

# Fleet API (commands + live data)
PROXY_URL = os.environ.get("TESLA_PROXY_URL", "").rstrip("/")
FLEET_URL = os.environ.get(
    "TESLA_FLEET_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com"
).rstrip("/")
VIN = os.environ.get("TESLA_VIN", "")
TOKEN_FILE = os.environ.get("TESLA_TOKEN_FILE", "")
CLIENT_ID = os.environ.get("TESLA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET", "")

# Vehicle-specific
CAR_ID = int(os.environ.get("TESLA_CAR_ID", "1"))
BATTERY_KWH = float(os.environ.get("TESLA_BATTERY_KWH", "75"))
BATTERY_RANGE_KM = float(os.environ.get("TESLA_BATTERY_RANGE_KM", "525"))
KWH_PER_KM = BATTERY_KWH / BATTERY_RANGE_KM

# Cost defaults (overridable per-call on savings/trip tools)
ELECTRICITY_RATE = float(os.environ.get("TESLA_ELECTRICITY_RATE", "0.12"))
GAS_PRICE = float(os.environ.get("TESLA_GAS_PRICE", "3.50"))
GAS_MPG = int(os.environ.get("TESLA_GAS_MPG", "28"))

# TLS verification (set false for self-signed proxy certs)
VERIFY_SSL = os.environ.get("TESLA_VERIFY_SSL", "true").lower() not in (
    "false",
    "0",
    "no",
)

# Backend availability
HAS_TESLAMATE = bool(DB_HOST and DB_PASS)
HAS_FLEET_API = bool(VIN and TOKEN_FILE)
HAS_PROXY = bool(PROXY_URL and VIN and TOKEN_FILE)

mcp = FastMCP("tesla")

# -- Rate limiting -------------------------------------------------------------

_command_count: int = 0
_command_day: str = ""
DAILY_COMMAND_LIMIT = 40


def _check_rate_limit() -> str | None:
    """Check daily command rate limit. Returns error message or None if OK."""
    global _command_count, _command_day
    today = datetime.now().strftime("%Y-%m-%d")
    if today != _command_day:
        _command_day = today
        _command_count = 0
    if _command_count >= DAILY_COMMAND_LIMIT:
        return (
            f"Daily command limit reached ({DAILY_COMMAND_LIMIT}). Resets at midnight."
        )
    return None


def _log_command(cmd: str) -> None:
    """Increment command counter."""
    global _command_count
    _command_count += 1


# -- Token management ----------------------------------------------------------

_cached_token: str = ""
_token_expiry: float = 0


def _get_access_token() -> str:
    """Get a valid Fleet API access token, refreshing if needed."""
    global _cached_token, _token_expiry

    if _cached_token and time.time() < _token_expiry - 300:
        return _cached_token

    if not TOKEN_FILE:
        raise RuntimeError(
            "TESLA_TOKEN_FILE not set. "
            "Set this to the path of your Fleet API tokens.json file."
        )

    token_path = Path(TOKEN_FILE)
    if not token_path.exists():
        raise RuntimeError(f"Token file not found: {TOKEN_FILE}")

    tokens = json.loads(token_path.read_text())
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 0)

    # Check if token is still valid (rough estimate)
    file_mtime = token_path.stat().st_mtime
    token_age = time.time() - file_mtime
    if token_age < expires_in - 600:
        _cached_token = access_token
        _token_expiry = file_mtime + expires_in
        return access_token

    # Refresh the token
    if not refresh_token or not CLIENT_ID or not CLIENT_SECRET:
        _cached_token = access_token
        _token_expiry = time.time() + 300
        return access_token

    resp = httpx.post(
        "https://auth.tesla.com/oauth2/v3/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=15.0,
    )
    if resp.status_code == 200:
        new_tokens = resp.json()
        token_path.write_text(json.dumps(new_tokens))
        _cached_token = new_tokens["access_token"]
        _token_expiry = time.time() + new_tokens.get("expires_in", 28800)
        return _cached_token

    # Refresh failed — use existing token
    _cached_token = access_token
    _token_expiry = time.time() + 60
    return access_token


# -- Fleet API helpers ---------------------------------------------------------


async def _fleet_get(path: str) -> dict:
    """GET from Fleet API (direct, for data reads)."""
    if not HAS_FLEET_API:
        raise RuntimeError(
            "Fleet API not configured. "
            "Set TESLA_VIN and TESLA_TOKEN_FILE environment variables."
        )
    token = _get_access_token()
    async with httpx.AsyncClient(timeout=15.0, verify=VERIFY_SSL) as client:
        resp = await client.get(
            f"{FLEET_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def _fleet_command(command: str, body: dict | None = None) -> dict:
    """Send a vehicle command through the HTTP proxy (signed)."""
    if not HAS_PROXY:
        return {
            "error": (
                "Fleet API proxy not configured. "
                "Set TESLA_PROXY_URL, TESLA_VIN, and TESLA_TOKEN_FILE."
            )
        }
    if not VIN:
        return {"error": "TESLA_VIN not set."}

    limit_err = _check_rate_limit()
    if limit_err:
        return {"result": False, "reason": limit_err}

    token = _get_access_token()
    url = f"{PROXY_URL}/api/1/vehicles/{VIN}/command/{command}"
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=VERIFY_SSL) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body or {},
            )
            try:
                result = resp.json()
            except Exception:
                return {
                    "error": f"Non-JSON response ({resp.status_code}): "
                    f"{resp.text[:200]}"
                }
            _log_command(command)
            return result
    except Exception as e:
        return {"error": f"Command failed: {e}"}


# -- DB helper -----------------------------------------------------------------


def _get_conn():
    """Get a Postgres connection to TeslaMate's database."""
    if not HAS_TESLAMATE:
        raise RuntimeError(
            "TeslaMate database not configured. "
            "Set TESLAMATE_DB_HOST and TESLAMATE_DB_PASS environment variables. "
            "See README for setup instructions."
        )
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        dbname=DB_NAME,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a read-only query and return results as list of dicts."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = []
            for row in cur.fetchall():
                rows.append(
                    {
                        k: float(v) if isinstance(v, Decimal) else v
                        for k, v in dict(row).items()
                    }
                )
            return rows
    finally:
        conn.close()


def _query_one(sql: str, params: tuple = ()) -> dict | None:
    """Execute a read-only query and return first result."""
    rows = _query(sql, params)
    return rows[0] if rows else None


def _km_to_mi(km: float | None) -> float | None:
    """Convert km to miles, handling None."""
    return round(km * 0.621371, 1) if km else None


def _c_to_f(c: float | None) -> int | None:
    """Convert Celsius to Fahrenheit, handling None."""
    return round(c * 9 / 5 + 32) if c is not None else None


# -- TeslaMate Read Tools ------------------------------------------------------


@mcp.tool()
async def tesla_status() -> str:
    """Current vehicle state — battery, range, location, climate, odometer.

    Returns the latest position snapshot and vehicle info from TeslaMate.
    """
    car = _query_one(
        f"SELECT id, name, model, efficiency FROM cars WHERE id = {CAR_ID} LIMIT 1"
    )

    pos = _query_one(f"""
        SELECT battery_level, ideal_battery_range_km,
               is_climate_on, inside_temp, outside_temp, driver_temp_setting,
               odometer, speed, power,
               latitude, longitude, date
        FROM positions
        WHERE car_id = {CAR_ID}
        ORDER BY date DESC
        LIMIT 1
    """)

    state = _query_one(f"""
        SELECT state, start_date, end_date
        FROM states
        WHERE car_id = {CAR_ID}
        ORDER BY start_date DESC
        LIMIT 1
    """)

    charge = _query_one(f"""
        SELECT charge_energy_added, duration_min,
               start_battery_level, end_battery_level,
               start_date, end_date
        FROM charging_processes
        WHERE car_id = {CAR_ID}
        ORDER BY start_date DESC
        LIMIT 1
    """)

    # Check geofence for current position
    geofence = None
    if pos and pos.get("latitude") and pos.get("longitude"):
        geofence = _query_one(
            """
            SELECT name, radius, dist.distance_m FROM geofences,
            LATERAL (SELECT (6371000 * acos(
                       cos(radians(%s)) * cos(radians(latitude))
                       * cos(radians(longitude) - radians(%s))
                       + sin(radians(%s)) * sin(radians(latitude))
                   )) AS distance_m) dist
            WHERE dist.distance_m <= radius
            ORDER BY dist.distance_m
            LIMIT 1
        """,
            (pos["latitude"], pos["longitude"], pos["latitude"]),
        )
        if geofence is None:
            geofence = _query_one(
                """
                SELECT name FROM geofences
                WHERE ABS(latitude - %s) < 0.01 AND ABS(longitude - %s) < 0.01
                ORDER BY ABS(latitude - %s) + ABS(longitude - %s)
                LIMIT 1
            """,
                (pos["latitude"], pos["longitude"], pos["latitude"], pos["longitude"]),
            )

    update = _query_one(f"""
        SELECT version FROM updates
        WHERE car_id = {CAR_ID}
        ORDER BY start_date DESC
        LIMIT 1
    """)

    lines = []
    if car:
        name = car.get("name") or car.get("model") or "Tesla"
        model = car.get("model") or ""
        lines.append(f"**{name}** ({model})")

    if update and update.get("version"):
        lines.append(f"Software: {update['version']}")

    if pos:
        bat = pos.get("battery_level")
        range_mi = _km_to_mi(pos.get("ideal_battery_range_km"))
        lines.append(f"Battery: {bat}%" + (f" ({range_mi} mi)" if range_mi else ""))

        is_charging = (
            charge
            and charge.get("end_date") is None
            and charge.get("start_date") is not None
        )
        if is_charging:
            kwh = charge.get("charge_energy_added") or 0
            lines.append(f"Charging: Yes ({kwh:.1f} kWh added so far)")
        else:
            lines.append("Charging: Not charging")

        if pos.get("is_climate_on"):
            inside_f = _c_to_f(pos.get("inside_temp"))
            target_f = _c_to_f(pos.get("driver_temp_setting"))
            line = "Climate: ON"
            if inside_f is not None:
                line += f", cabin {inside_f}°F"
            if target_f is not None:
                line += f", target {target_f}°F"
            lines.append(line)
        else:
            inside_f = _c_to_f(pos.get("inside_temp"))
            outside_f = _c_to_f(pos.get("outside_temp"))
            parts = ["Climate: Off"]
            if inside_f is not None:
                parts.append(f"cabin {inside_f}°F")
            if outside_f is not None:
                parts.append(f"outside {outside_f}°F")
            lines.append(", ".join(parts))

        odo_mi = _km_to_mi(pos.get("odometer"))
        if odo_mi:
            lines.append(f"Odometer: {round(odo_mi):,} mi")

        if state:
            vehicle_state = state.get("state", "unknown")
            lines.append(f"State: {vehicle_state}")

        if geofence and geofence.get("name"):
            lines.append(f"Location: {geofence['name']}")
        else:
            lat, lon = pos.get("latitude"), pos.get("longitude")
            if lat is not None and lon is not None:
                lines.append(f"Location: {lat:.4f}, {lon:.4f}")
            else:
                lines.append("Location: unknown")

        lines.append(f"Last update: {str(pos.get('date', 'unknown'))[:19]}")

    if charge and charge.get("end_date"):
        kwh = charge.get("charge_energy_added") or 0
        dur = charge.get("duration_min") or 0
        start_bat = charge.get("start_battery_level") or "?"
        end_bat = charge.get("end_battery_level") or "?"
        lines.append(
            f"Last charge: {kwh:.1f} kWh in {dur} min ({start_bat}% → {end_bat}%)"
        )

    return "\n".join(lines) if lines else "No vehicle data found. Is TeslaMate running?"


@mcp.tool()
async def tesla_charging_history(days: int = 30) -> str:
    """Charging sessions over the last N days.

    Shows energy added, duration, battery range, and location for each session.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _query(
        f"""
        SELECT cp.start_date, cp.end_date,
               cp.charge_energy_added, cp.duration_min,
               cp.start_battery_level, cp.end_battery_level,
               a.display_name AS location
        FROM charging_processes cp
        LEFT JOIN positions p ON cp.position_id = p.id
        LEFT JOIN addresses a ON a.id = (
            SELECT a2.id FROM addresses a2
            WHERE ABS(a2.latitude - p.latitude) < 0.001
              AND ABS(a2.longitude - p.longitude) < 0.001
            ORDER BY ABS(a2.latitude - p.latitude) + ABS(a2.longitude - p.longitude)
            LIMIT 1
        )
        WHERE cp.car_id = {CAR_ID} AND cp.start_date >= %s
        ORDER BY cp.start_date DESC
        LIMIT 50
    """,
        (cutoff,),
    )

    if not rows:
        return f"No charging sessions in the last {days} days."

    lines = [f"**Charging History** (last {days} days, {len(rows)} sessions)\n"]
    total_kwh = 0.0
    for r in rows:
        kwh = r.get("charge_energy_added") or 0
        total_kwh += kwh
        dur = r.get("duration_min") or 0
        start_pct = r.get("start_battery_level", "?")
        end_pct = r.get("end_battery_level", "?")
        loc = r.get("location") or "Unknown"
        date_str = str(r.get("start_date", ""))[:16]
        lines.append(
            f"- {date_str}: {kwh:.1f} kWh, {dur} min, {start_pct}% → {end_pct}%, {loc}"
        )

    lines.append(f"\n**Total:** {total_kwh:.1f} kWh across {len(rows)} sessions")
    return "\n".join(lines)


@mcp.tool()
async def tesla_drives(days: int = 30) -> str:
    """Recent drives — distance, duration, efficiency, start/end locations.

    Shows the last N days of driving activity with energy consumption.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _query(
        f"""
        SELECT d.start_date, d.end_date,
               d.distance, d.duration_min,
               d.start_ideal_range_km, d.end_ideal_range_km,
               d.outside_temp_avg,
               sa.display_name AS start_location,
               ea.display_name AS end_location
        FROM drives d
        LEFT JOIN addresses sa ON d.start_address_id = sa.id
        LEFT JOIN addresses ea ON d.end_address_id = ea.id
        WHERE d.car_id = {CAR_ID} AND d.start_date >= %s
        ORDER BY d.start_date DESC
        LIMIT 50
    """,
        (cutoff,),
    )

    if not rows:
        return f"No drives recorded in the last {days} days."

    lines = [f"**Drives** (last {days} days, {len(rows)} trips)\n"]
    total_km = 0.0
    total_kwh = 0.0
    total_min = 0
    for r in rows:
        dist_km = r.get("distance") or 0
        total_km += dist_km
        dist_mi = _km_to_mi(dist_km) or 0
        dur = r.get("duration_min") or 0
        total_min += dur
        start = r.get("start_location") or "?"
        end = r.get("end_location") or "?"
        date_str = str(r.get("start_date", ""))[:16]
        range_start = r.get("start_ideal_range_km") or 0
        range_end = r.get("end_ideal_range_km") or 0
        kwh = max(0, (range_start - range_end) * KWH_PER_KM)
        total_kwh += kwh

        eff_str = ""
        if dist_km > 0 and kwh > 0:
            wh_per_mi = round(kwh * 1000 / (dist_km * 0.621371))
            eff_str = f", {wh_per_mi} Wh/mi"

        lines.append(f"- {date_str}: {dist_mi} mi, {dur} min, {start} → {end}{eff_str}")

    total_mi = _km_to_mi(total_km) or 0
    avg_eff = ""
    if total_km > 0 and total_kwh > 0:
        avg_wh_per_mi = round(total_kwh * 1000 / (total_km * 0.621371))
        avg_eff = f", avg {avg_wh_per_mi} Wh/mi"
    lines.append(
        f"\n**Total:** {total_mi} mi, {total_kwh:.1f} kWh, "
        f"{total_min} min across {len(rows)} trips{avg_eff}"
    )
    return "\n".join(lines)


@mcp.tool()
async def tesla_battery_health() -> str:
    """Battery degradation trend — range at 100% charge over time.

    Shows monthly snapshots of ideal range when battery is at 100%.
    """
    rows = _query(f"""
        SELECT date_trunc('month', date) AS month,
               AVG(ideal_battery_range_km) AS avg_ideal_km,
               COUNT(*) AS samples
        FROM positions
        WHERE car_id = {CAR_ID}
          AND battery_level = 100
          AND ideal_battery_range_km IS NOT NULL
        GROUP BY date_trunc('month', date)
        ORDER BY month DESC
        LIMIT 24
    """)

    if not rows:
        rows = _query(f"""
            SELECT date, ideal_battery_range_km
            FROM positions
            WHERE car_id = {CAR_ID}
              AND battery_level >= 99
              AND ideal_battery_range_km IS NOT NULL
            ORDER BY date DESC
            LIMIT 20
        """)
        if not rows:
            return "Not enough data for battery health. Need positions at 100% charge."

        lines = ["**Battery Health** (snapshots at ~100% charge)\n"]
        for r in rows:
            range_mi = _km_to_mi(r.get("ideal_battery_range_km"))
            date_str = str(r.get("date", ""))[:10]
            lines.append(f"- {date_str}: {range_mi} mi ideal range at 100%")
        return "\n".join(lines)

    lines = ["**Battery Health** (monthly averages at 100% charge)\n"]
    for r in rows:
        range_mi = _km_to_mi(r.get("avg_ideal_km"))
        month = str(r.get("month", ""))[:7]
        samples = r.get("samples", 0)
        lines.append(f"- {month}: {range_mi} mi ideal range ({samples} samples)")

    if len(rows) >= 2:
        newest = _km_to_mi(rows[0].get("avg_ideal_km")) or 0
        oldest = _km_to_mi(rows[-1].get("avg_ideal_km")) or 0
        if oldest > 0:
            deg_pct = round((1 - newest / oldest) * 100, 1)
            lines.append(f"\n**Degradation:** {deg_pct}% over {len(rows)} months")

    return "\n".join(lines)


@mcp.tool()
async def tesla_efficiency(days: int = 90) -> str:
    """Energy consumption trends — Wh/mi over time.

    Shows weekly average efficiency from driving data.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _query(
        f"""
        SELECT date_trunc('week', start_date) AS week,
               SUM(distance) AS total_km,
               SUM(GREATEST(start_ideal_range_km - end_ideal_range_km, 0)
                   * {KWH_PER_KM}) AS total_kwh,
               SUM(duration_min) AS total_min,
               COUNT(*) AS trips,
               AVG(outside_temp_avg) AS avg_temp
        FROM drives
        WHERE car_id = {CAR_ID} AND start_date >= %s AND distance > 0
        GROUP BY date_trunc('week', start_date)
        ORDER BY week DESC
    """,
        (cutoff,),
    )

    if not rows:
        return f"No driving data in the last {days} days."

    lines = [f"**Efficiency** (last {days} days, weekly)\n"]
    for r in rows:
        km = r.get("total_km") or 0
        mi = _km_to_mi(km) or 0
        kwh = r.get("total_kwh") or 0
        trips = r.get("trips", 0)
        week = str(r.get("week", ""))[:10]
        temp = r.get("avg_temp")
        temp_str = f", avg {_c_to_f(temp)}°F" if temp is not None else ""

        eff_str = ""
        if km > 0 and kwh > 0:
            wh_per_mi = round(kwh * 1000 / (km * 0.621371))
            eff_str = f", {wh_per_mi} Wh/mi"

        lines.append(
            f"- {week}: {mi} mi, {kwh:.1f} kWh, {trips} trips{eff_str}{temp_str}"
        )

    return "\n".join(lines)


@mcp.tool()
async def tesla_location_history(days: int = 7) -> str:
    """Where the car has been — top locations by time spent.

    Groups positions by proximity and shows time at each cluster.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    rows = _query(
        f"""
        SELECT ROUND(latitude::numeric, 3) AS lat,
               ROUND(longitude::numeric, 3) AS lon,
               COUNT(*) AS position_count,
               MIN(date) AS first_seen,
               MAX(date) AS last_seen
        FROM positions
        WHERE car_id = {CAR_ID} AND date >= %s
        GROUP BY ROUND(latitude::numeric, 3), ROUND(longitude::numeric, 3)
        ORDER BY position_count DESC
        LIMIT 20
    """,
        (cutoff,),
    )

    if not rows:
        return f"No location data in the last {days} days."

    geofences = _query("SELECT name, latitude, longitude, radius FROM geofences")

    lines = [f"**Location History** (last {days} days)\n"]
    for r in rows:
        lat = float(r.get("lat", 0))
        lon = float(r.get("lon", 0))
        count = r.get("position_count", 0)
        last = str(r.get("last_seen", ""))[:16]

        loc = f"{lat}, {lon}"
        for gf in geofences:
            dlat = abs(gf["latitude"] - lat)
            dlon = abs(gf["longitude"] - lon)
            if dlat < 0.005 and dlon < 0.005:
                loc = gf["name"]
                break

        lines.append(f"- {loc}: {count} data points, last seen {last}")

    return "\n".join(lines)


@mcp.tool()
async def tesla_state_history(days: int = 7) -> str:
    """Vehicle state transitions — online, asleep, offline.

    Shows when the car was awake vs sleeping, useful for vampire drain analysis.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _query(
        f"""
        SELECT state, start_date, end_date
        FROM states
        WHERE car_id = {CAR_ID} AND start_date >= %s
        ORDER BY start_date DESC
        LIMIT 100
    """,
        (cutoff,),
    )

    if not rows:
        return f"No state data in the last {days} days."

    lines = [f"**State History** (last {days} days, {len(rows)} transitions)\n"]

    totals: dict[str, float] = {}
    for r in rows:
        st = r.get("state", "unknown")
        start = r.get("start_date")
        end = r.get("end_date") or datetime.utcnow()
        if start:
            dur_h = (end - start).total_seconds() / 3600
            totals[st] = totals.get(st, 0) + dur_h

    for st, hours in sorted(totals.items(), key=lambda x: -x[1]):
        lines.append(f"- {st}: {hours:.1f} hours")

    lines.append("\nRecent transitions:")
    for r in rows[:20]:
        st = r.get("state", "?")
        start = str(r.get("start_date", ""))[:16]
        end = r.get("end_date")
        dur = ""
        if end and r.get("start_date"):
            dur_min = round((end - r["start_date"]).total_seconds() / 60)
            dur = f" ({dur_min} min)"
        lines.append(f"- {start}: {st}{dur}")

    return "\n".join(lines)


@mcp.tool()
async def tesla_software_updates() -> str:
    """Firmware version history — all recorded software versions and install dates."""
    rows = _query(f"""
        SELECT version, start_date, end_date
        FROM updates
        WHERE car_id = {CAR_ID}
        ORDER BY start_date DESC
        LIMIT 20
    """)

    if not rows:
        return "No software update history found."

    lines = ["**Software Updates**\n"]
    for r in rows:
        ver = r.get("version", "unknown")
        start = str(r.get("start_date", ""))[:16]
        end = r.get("end_date")
        dur = ""
        if end and r.get("start_date"):
            dur_min = round((end - r["start_date"]).total_seconds() / 60)
            dur = f" ({dur_min} min)"
        lines.append(f"- {start}: {ver}{dur}")

    return "\n".join(lines)


# -- Fleet API Live Data -------------------------------------------------------


@mcp.tool()
async def tesla_live() -> str:
    """Live vehicle data from Fleet API — real-time battery, charging, climate, locks, sentry.

    More current than TeslaMate (which polls on intervals). Use this when you need
    the latest state right now.
    """
    if not VIN:
        return "TESLA_VIN not set."

    data = await _fleet_get(
        f"/api/1/vehicles/{VIN}/vehicle_data"
        f"?endpoints=charge_state%3Bclimate_state%3Bvehicle_state%3Bdrive_state"
    )
    r = data.get("response", {})
    cs = r.get("charge_state", {})
    cl = r.get("climate_state", {})
    vs = r.get("vehicle_state", {})
    ds = r.get("drive_state", {})

    vehicle_name = vs.get("vehicle_name") or "Tesla"
    lines = [f"**{vehicle_name}** (live)\n"]
    lines.append(
        f"Battery: {cs.get('battery_level')}% ({cs.get('battery_range', 0):.0f} mi)"
    )
    lines.append(
        f"Charging: {cs.get('charging_state')} (limit {cs.get('charge_limit_soc')}%)"
    )
    if cs.get("charging_state") == "Charging":
        rate = cs.get("charge_rate", 0)
        added = cs.get("charge_energy_added", 0)
        mins = cs.get("minutes_to_full_charge", 0)
        lines.append(f"  Rate: {rate} mph, {added:.1f} kWh added, {mins} min to full")

    inside_f = _c_to_f(cl.get("inside_temp"))
    outside_f = _c_to_f(cl.get("outside_temp"))
    lines.append(
        f"Climate: {'ON' if cl.get('is_climate_on') else 'Off'}"
        f", inside {inside_f}°F, outside {outside_f}°F"
    )
    if cl.get("is_climate_on"):
        target_f = _c_to_f(cl.get("driver_temp_setting"))
        lines.append(f"  Target: {target_f}°F")

    lines.append(f"Locked: {'Yes' if vs.get('locked') else 'No'}")
    lines.append(f"Sentry: {'On' if vs.get('sentry_mode') else 'Off'}")
    lines.append(f"Software: {vs.get('car_version', '?')}")
    lines.append(f"Odometer: {vs.get('odometer', 0):.0f} mi")

    if ds.get("speed"):
        lines.append(f"Driving: {ds['speed']} mph")
    else:
        lines.append("Driving: Parked")

    tires = []
    for pos, label in [("fl", "FL"), ("fr", "FR"), ("rl", "RL"), ("rr", "RR")]:
        bar = vs.get(f"tpms_pressure_{pos}")
        if bar:
            psi = round(bar * 14.5038, 1)
            warn = " ⚠" if vs.get(f"tpms_soft_warning_{pos}") else ""
            tires.append(f"{label}:{psi}{warn}")
    if tires:
        lines.append(f"Tires: {', '.join(tires)} PSI")

    media = vs.get("media_info", {})
    title = media.get("now_playing_title", "")
    artist = media.get("now_playing_artist", "")
    if title:
        playing = title
        if artist:
            playing = f"{artist} — {title}"
        lines.append(f"Playing: {playing} ({media.get('now_playing_source', '?')})")

    update = vs.get("software_update", {})
    if update.get("status") and update.get("version", "").strip():
        lines.append(
            f"Update available: {update['version'].strip()} "
            f"(est. {update.get('expected_duration_sec', 0) // 60} min)"
        )

    lines.append(f"\nCommands today: {_command_count}/{DAILY_COMMAND_LIMIT}")
    return "\n".join(lines)


# -- Fleet API Command Tools ---------------------------------------------------


def _cmd_result(result: dict | None, success_msg: str) -> str:
    """Parse a Fleet API command response into a user-friendly message."""
    if not result:
        return "Failed: no response from proxy"
    if result.get("error"):
        return f"Failed: {result['error']}"
    resp = result.get("response", {})
    if resp and resp.get("result"):
        return success_msg
    return f"Failed: {resp.get('reason', 'unknown')}"


@mcp.tool()
async def tesla_climate_on() -> str:
    """Start climate preconditioning — heats or cools cabin to target temp."""
    result = await _fleet_command("auto_conditioning_start")
    return _cmd_result(result, "Climate started. Cabin will reach target temperature.")


@mcp.tool()
async def tesla_climate_off() -> str:
    """Stop climate preconditioning."""
    result = await _fleet_command("auto_conditioning_stop")
    return _cmd_result(result, "Climate stopped.")


@mcp.tool()
async def tesla_set_temp(temp_f: int = 70) -> str:
    """Set cabin temperature target (Fahrenheit). Both driver and passenger.

    Args:
        temp_f: Target temperature in Fahrenheit (60-85 reasonable range)
    """
    temp_c = round((temp_f - 32) * 5 / 9, 1)
    result = await _fleet_command(
        "set_temps",
        {
            "driver_temp": temp_c,
            "passenger_temp": temp_c,
        },
    )
    return _cmd_result(result, f"Temperature set to {temp_f}°F ({temp_c}°C).")


@mcp.tool()
async def tesla_charge_start() -> str:
    """Start charging (vehicle must be plugged in)."""
    result = await _fleet_command("charge_start")
    return _cmd_result(result, "Charging started.")


@mcp.tool()
async def tesla_charge_stop() -> str:
    """Stop charging."""
    result = await _fleet_command("charge_stop")
    return _cmd_result(result, "Charging stopped.")


@mcp.tool()
async def tesla_set_charge_limit(percent: int = 80) -> str:
    """Set charge limit percentage (50-100).

    Args:
        percent: Charge limit as percentage. 80% is recommended for daily use.
    """
    if percent < 50 or percent > 100:
        return "Charge limit must be between 50 and 100."
    result = await _fleet_command("set_charge_limit", {"percent": percent})
    return _cmd_result(result, f"Charge limit set to {percent}%.")


@mcp.tool()
async def tesla_lock() -> str:
    """Lock all doors."""
    result = await _fleet_command("door_lock")
    return _cmd_result(result, "Doors locked.")


@mcp.tool()
async def tesla_unlock(confirm: bool = False) -> str:
    """Unlock all doors. Requires confirm=True for safety.

    Args:
        confirm: Must be True to execute. Prevents accidental unlocks.
    """
    if not confirm:
        return "Unlock requires confirm=True. This will physically unlock the car."
    result = await _fleet_command("door_unlock")
    return _cmd_result(result, "Doors unlocked.")


@mcp.tool()
async def tesla_honk() -> str:
    """Honk the horn."""
    result = await _fleet_command("honk_horn")
    return _cmd_result(result, "Horn honked.")


@mcp.tool()
async def tesla_flash() -> str:
    """Flash the headlights."""
    result = await _fleet_command("flash_lights")
    return _cmd_result(result, "Lights flashed.")


@mcp.tool()
async def tesla_trunk(which: str = "rear", confirm: bool = False) -> str:
    """Open or close the trunk or frunk. Requires confirm=True.

    Args:
        which: "rear" for trunk, "front" for frunk
        confirm: Must be True to execute.
    """
    if not confirm:
        return f"Trunk ({which}) requires confirm=True."
    if which not in ("rear", "front"):
        return "which must be 'rear' or 'front'"
    result = await _fleet_command("actuate_trunk", {"which_trunk": which})
    return _cmd_result(result, f"{'Trunk' if which == 'rear' else 'Frunk'} actuated.")


@mcp.tool()
async def tesla_sentry(on: bool = True) -> str:
    """Toggle sentry mode on or off.

    Args:
        on: True to enable, False to disable sentry mode.
    """
    result = await _fleet_command("set_sentry_mode", {"on": on})
    return _cmd_result(result, f"Sentry mode {'enabled' if on else 'disabled'}.")


# -- Analytics Tools -----------------------------------------------------------


@mcp.tool()
async def tesla_savings(
    gas_price: float = None,
    mpg_equivalent: int = None,
) -> str:
    """Gas savings scorecard — how much you've saved vs a gas car.

    Args:
        gas_price: Gas price per gallon (default from TESLA_GAS_PRICE env, or $3.50)
        mpg_equivalent: Comparable gas vehicle MPG (default from TESLA_GAS_MPG env, or 28)
    """
    _gas = gas_price or GAS_PRICE
    _mpg = mpg_equivalent or GAS_MPG

    lifetime = _query_one(f"""
        SELECT COALESCE(SUM(distance), 0) AS total_km,
               COALESCE(SUM(GREATEST(start_ideal_range_km - end_ideal_range_km, 0)
                   * {KWH_PER_KM}), 0) AS total_kwh
        FROM drives WHERE car_id = {CAR_ID} AND distance > 0
    """)
    monthly = _query_one(f"""
        SELECT COALESCE(SUM(distance), 0) AS total_km,
               COALESCE(SUM(GREATEST(start_ideal_range_km - end_ideal_range_km, 0)
                   * {KWH_PER_KM}), 0) AS total_kwh
        FROM drives WHERE car_id = {CAR_ID} AND distance > 0
          AND date_trunc('month', start_date) = date_trunc('month', NOW())
    """)

    if not lifetime:
        return "No driving data yet."

    lines = ["**Gas Savings Scorecard**\n"]

    for label, data in [("This Month", monthly), ("Lifetime", lifetime)]:
        if not data or not data.get("total_km"):
            continue
        mi = round(data["total_km"] * 0.621371, 1)
        kwh = data["total_kwh"] or 0
        elec_cost = round(kwh * ELECTRICITY_RATE, 2)
        gas_cost = round(mi / _mpg * _gas, 2)
        saved = round(gas_cost - elec_cost, 2)
        cost_per_mi = round(elec_cost / mi * 100, 1) if mi > 0 else 0

        lines.append(f"**{label}:** {mi:,.1f} mi")
        lines.append(
            f"  Electricity: {kwh:,.1f} kWh × ${ELECTRICITY_RATE} = ${elec_cost:,.2f}"
        )
        lines.append(
            f"  Gas equivalent: {mi:,.1f} mi ÷ {_mpg} MPG × "
            f"${_gas}/gal = ${gas_cost:,.2f}"
        )
        lines.append(
            f"  **Saved: ${saved:,.2f}** ({cost_per_mi}¢/mi electric vs "
            f"{round(_gas / _mpg * 100, 1)}¢/mi gas)"
        )
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def tesla_trip_cost(
    destination: str,
    gas_price: float = None,
    mpg_equivalent: int = None,
) -> str:
    """Estimate trip cost to a destination — kWh, cost, range check.

    Uses your personal 30-day average efficiency and current battery level.

    Args:
        destination: City, address, or place name (e.g. "Atlanta, GA")
        gas_price: Gas price per gallon (default from TESLA_GAS_PRICE env)
        mpg_equivalent: Comparable gas vehicle MPG (default from TESLA_GAS_MPG env)
    """
    _gas = gas_price or GAS_PRICE
    _mpg = mpg_equivalent or GAS_MPG

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": destination, "format": "json", "limit": "1"},
            headers={"User-Agent": "TeslaMCP/1.0"},
        )
        results = resp.json()
    if not results:
        return f"Could not geocode '{destination}'."

    dest_lat = float(results[0]["lat"])
    dest_lon = float(results[0]["lon"])
    dest_name = results[0].get("display_name", destination).split(",")[0]

    pos = _query_one(f"""
        SELECT latitude, longitude, battery_level, ideal_battery_range_km
        FROM positions WHERE car_id = {CAR_ID}
        ORDER BY date DESC LIMIT 1
    """)
    if not pos:
        return "No current position data."

    lat1, lon1 = math.radians(pos["latitude"]), math.radians(pos["longitude"])
    lat2, lon2 = math.radians(dest_lat), math.radians(dest_lon)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    straight_mi = 3959 * 2 * math.asin(math.sqrt(a))
    road_mi = round(straight_mi * 1.3, 1)
    round_trip = round(road_mi * 2, 1)

    eff = _query_one(f"""
        SELECT COALESCE(SUM(GREATEST(start_ideal_range_km - end_ideal_range_km, 0)
                   * {KWH_PER_KM}), 0) AS kwh,
               COALESCE(SUM(distance), 0) AS km
        FROM drives WHERE car_id = {CAR_ID}
          AND start_date >= NOW() - INTERVAL '30 days' AND distance > 0
    """)
    wh_per_mi = 300  # default
    if eff and eff["km"] > 0:
        wh_per_mi = round(eff["kwh"] * 1000 / (eff["km"] * 0.621371))

    kwh_round = round(round_trip * wh_per_mi / 1000, 1)
    cost_round = round(kwh_round * ELECTRICITY_RATE, 2)
    gas_equiv = round(round_trip / _mpg * _gas, 2)

    bat = pos.get("battery_level", 0)
    range_mi = round((pos.get("ideal_battery_range_km") or 0) * 0.621371)

    lines = [
        f"**Trip to {dest_name}** ({road_mi} mi each way, {round_trip} mi round trip)\n"
    ]
    lines.append(f"Estimated: {kwh_round} kWh @ {wh_per_mi} Wh/mi (your 30-day avg)")
    lines.append(f"Cost: ${cost_round} (gas equivalent: ${gas_equiv})")
    lines.append(f"Current battery: {bat}% ({range_mi} mi)")

    if range_mi >= round_trip:
        lines.append("Range: Sufficient for round trip")
    elif range_mi >= road_mi:
        lines.append("Range: Sufficient one-way, charge at destination for return")
    else:
        pct_needed = min(95, round(round_trip / range_mi * bat)) if range_mi > 0 else 95
        lines.append(
            f"Range: NOT sufficient — charge to {pct_needed}%+ before departure"
        )

    return "\n".join(lines)


@mcp.tool()
async def tesla_efficiency_by_temp() -> str:
    """Efficiency curve by temperature — Wh/mi at different temps.

    Shows how outside temperature affects energy consumption.
    """
    rows = _query(f"""
        SELECT
            CASE
                WHEN outside_temp_avg < 0 THEN 'Below 32°F'
                WHEN outside_temp_avg < 4.4 THEN '32-40°F'
                WHEN outside_temp_avg < 10 THEN '40-50°F'
                WHEN outside_temp_avg < 15.6 THEN '50-60°F'
                WHEN outside_temp_avg < 21.1 THEN '60-70°F'
                WHEN outside_temp_avg < 26.7 THEN '70-80°F'
                WHEN outside_temp_avg < 32.2 THEN '80-90°F'
                ELSE 'Above 90°F'
            END AS temp_range,
            COUNT(*) AS trips,
            SUM(distance) AS total_km,
            SUM(GREATEST(start_ideal_range_km - end_ideal_range_km, 0)
                * {KWH_PER_KM}) AS total_kwh
        FROM drives
        WHERE car_id = {CAR_ID} AND distance > 1
          AND (start_ideal_range_km - end_ideal_range_km) > 0
          AND outside_temp_avg IS NOT NULL
        GROUP BY temp_range
        ORDER BY MIN(outside_temp_avg)
    """)

    if not rows:
        return "Not enough driving data with temperature readings."

    lines = ["**Efficiency by Temperature**\n"]
    lines.append(f"{'Temp Range':<15} {'Trips':>6} {'Wh/mi':>8} {'Miles':>10}")
    lines.append("-" * 45)
    for r in rows:
        km = r.get("total_km") or 0
        kwh = r.get("total_kwh") or 0
        mi = km * 0.621371
        wh_mi = round(kwh * 1000 / mi) if mi > 0 else 0
        lines.append(
            f"{r['temp_range']:<15} {r['trips']:>6} {wh_mi:>7} {round(mi):>9,}"
        )

    return "\n".join(lines)


@mcp.tool()
async def tesla_charging_by_location() -> str:
    """Charging patterns by location — where you charge and how much."""
    rows = _query(f"""
        SELECT a.display_name AS location,
               COUNT(*) AS sessions,
               COALESCE(SUM(cp.charge_energy_added), 0) AS total_kwh,
               COALESCE(AVG(cp.charge_energy_added), 0) AS avg_kwh,
               COALESCE(SUM(cp.duration_min), 0) AS total_min
        FROM charging_processes cp
        JOIN positions p ON cp.position_id = p.id
        LEFT JOIN addresses a ON a.id = (
            SELECT a2.id FROM addresses a2
            WHERE ABS(a2.latitude - p.latitude) < 0.005
              AND ABS(a2.longitude - p.longitude) < 0.005
            LIMIT 1
        )
        WHERE cp.car_id = {CAR_ID} AND cp.end_date IS NOT NULL
        GROUP BY a.display_name
        ORDER BY total_kwh DESC
        LIMIT 15
    """)

    if not rows:
        return "No charging data yet."

    lines = ["**Charging by Location**\n"]
    for r in rows:
        loc = r.get("location") or "Unknown"
        sessions = r.get("sessions", 0)
        kwh = r.get("total_kwh", 0)
        cost = round(kwh * ELECTRICITY_RATE, 2)
        lines.append(f"- **{loc}**: {sessions} sessions, {kwh:.1f} kWh (~${cost})")

    return "\n".join(lines)


@mcp.tool()
async def tesla_top_destinations(limit: int = 15) -> str:
    """Most visited locations ranked by number of visits.

    Args:
        limit: Number of destinations to show (default: 15)
    """
    rows = _query(
        f"""
        SELECT ea.display_name AS destination,
               COUNT(*) AS visits,
               COALESCE(SUM(d.distance), 0) AS total_km
        FROM drives d
        JOIN addresses ea ON d.end_address_id = ea.id
        WHERE d.car_id = {CAR_ID} AND d.distance > 1
        GROUP BY ea.display_name
        ORDER BY visits DESC
        LIMIT %s
    """,
        (limit,),
    )

    if not rows:
        return "No driving data yet."

    lines = ["**Top Destinations**\n"]
    for i, r in enumerate(rows, 1):
        dest = r.get("destination") or "Unknown"
        visits = r.get("visits", 0)
        mi = round((r.get("total_km") or 0) * 0.621371)
        lines.append(f"{i}. {dest} — {visits} visits ({mi:,} mi total)")

    return "\n".join(lines)


@mcp.tool()
async def tesla_longest_trips(limit: int = 10) -> str:
    """Top drives ranked by distance — your epic road trips.

    Args:
        limit: Number of trips to show (default: 10)
    """
    rows = _query(
        f"""
        SELECT d.start_date, d.distance, d.duration_min,
               GREATEST(d.start_ideal_range_km - d.end_ideal_range_km, 0)
                   * {KWH_PER_KM} AS consumption_kwh,
               sa.display_name AS start_loc,
               ea.display_name AS end_loc
        FROM drives d
        LEFT JOIN addresses sa ON d.start_address_id = sa.id
        LEFT JOIN addresses ea ON d.end_address_id = ea.id
        WHERE d.car_id = {CAR_ID} AND d.distance > 0
        ORDER BY d.distance DESC
        LIMIT %s
    """,
        (limit,),
    )

    if not rows:
        return "No driving data yet."

    lines = ["**Longest Trips**\n"]
    for i, r in enumerate(rows, 1):
        mi = round((r.get("distance") or 0) * 0.621371, 1)
        dur = r.get("duration_min") or 0
        start = r.get("start_loc") or "?"
        end = r.get("end_loc") or "?"
        date = str(r.get("start_date", ""))[:10]
        kwh = r.get("consumption_kwh") or 0
        lines.append(f"{i}. {mi} mi — {start} → {end} ({date}, {dur}min, {kwh:.1f}kWh)")

    return "\n".join(lines)


@mcp.tool()
async def tesla_monthly_summary(months: int = 6) -> str:
    """Monthly driving summary — miles, kWh, cost, efficiency.

    Args:
        months: Number of months to show (default: 6)
    """
    rows = _query(
        f"""
        SELECT date_trunc('month', start_date) AS month,
               COUNT(*) AS trips,
               COALESCE(SUM(distance), 0) AS total_km,
               COALESCE(SUM(GREATEST(start_ideal_range_km - end_ideal_range_km, 0)
                   * {KWH_PER_KM}), 0) AS total_kwh,
               COALESCE(SUM(duration_min), 0) AS total_min
        FROM drives
        WHERE car_id = {CAR_ID} AND distance > 0
        GROUP BY date_trunc('month', start_date)
        ORDER BY month DESC
        LIMIT %s
    """,
        (months,),
    )

    if not rows:
        return "No driving data yet."

    lines = ["**Monthly Summary**\n"]
    lines.append(
        f"{'Month':<12} {'Trips':>6} {'Miles':>10} {'kWh':>8} {'Wh/mi':>7} {'Cost':>8}"
    )
    lines.append("-" * 57)

    for r in rows:
        month = str(r.get("month", ""))[:7]
        trips = r.get("trips", 0)
        km = r.get("total_km") or 0
        mi = round(km * 0.621371)
        kwh = r.get("total_kwh") or 0
        wh_mi = round(kwh * 1000 / (km * 0.621371)) if km > 0 else 0
        cost = round(kwh * ELECTRICITY_RATE, 2)
        lines.append(
            f"{month:<12} {trips:>6} {mi:>9,} {kwh:>7.1f} {wh_mi:>7} ${cost:>6.2f}"
        )

    return "\n".join(lines)


@mcp.tool()
async def tesla_vampire_drain(days: int = 14) -> str:
    """Vampire drain analysis — battery loss while parked overnight.

    Checks for periods where the car was parked (no drives) for 8+ hours
    and measures battery drop.

    Args:
        days: Number of days to analyze (default: 14)
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _query(
        f"""
        WITH ordered AS (
            SELECT date, battery_level,
                   LAG(battery_level) OVER (ORDER BY date) AS prev_level,
                   LAG(date) OVER (ORDER BY date) AS prev_date
            FROM positions
            WHERE car_id = {CAR_ID} AND date >= %s AND battery_level IS NOT NULL
            ORDER BY date
        )
        SELECT date, prev_date, battery_level, prev_level,
               prev_level - battery_level AS drain,
               EXTRACT(EPOCH FROM (date - prev_date)) / 3600 AS hours_parked
        FROM ordered
        WHERE prev_level IS NOT NULL
          AND prev_level - battery_level > 0
          AND EXTRACT(EPOCH FROM (date - prev_date)) / 3600 >= 8
          AND EXTRACT(EPOCH FROM (date - prev_date)) / 3600 <= 48
        ORDER BY drain DESC
        LIMIT 20
    """,
        (cutoff,),
    )

    if not rows:
        return f"No significant vampire drain detected in the last {days} days."

    lines = [f"**Vampire Drain** (last {days} days)\n"]
    total_drain = 0
    for r in rows:
        drain = r.get("drain", 0)
        total_drain += drain
        hours = r.get("hours_parked", 0)
        rate = round(drain / hours, 2) if hours > 0 else 0
        date = str(r.get("prev_date", ""))[:10]
        lines.append(f"- {date}: -{drain}% over {hours:.0f}h ({rate}%/hr)")

    avg_rate = round(
        total_drain
        / len(rows)
        / (sum(r.get("hours_parked", 8) for r in rows) / len(rows)),
        2,
    )
    lines.append(f"\nAverage drain rate: {avg_rate}%/hr")
    if avg_rate > 1.0:
        lines.append(
            "⚠ Above normal — check sentry mode camera activity "
            "or third-party app polling"
        )
    elif avg_rate > 0.5:
        lines.append("Slightly elevated — sentry mode active?")
    else:
        lines.append("Normal range for a parked Tesla")

    return "\n".join(lines)


# -- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
