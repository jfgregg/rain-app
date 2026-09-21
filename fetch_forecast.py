"""
fetch_forecast.py

Starter pipeline for the Rain app. Pulls NOAA GEFS ensemble forecasts and
NWS current-conditions observations for a given location, and computes the
hourly / window-max probability pair described in PROJECT_BRIEF.md.

This is a SCAFFOLD, not a finished pipeline — the GEFS-fetching parts are
stubbed with TODOs since they need real network access to NOAA's servers
to test and iterate on (not available in the environment this was drafted
in). Hand this to Claude Code along with PROJECT_BRIEF.md and iterate from
here — run it, see what breaks, fix it live.

Setup (run locally):
    pip install herbie-data xarray numpy requests

Usage (once built out):
    python fetch_forecast.py --lat 51.5074 --lon -0.1278
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

# ── Config ──────────────────────────────────────────────────────────────

WINDOW_HOURS = 3          # v1: fixed window size for window_max_pop
FORECAST_HOURS = 24       # how far ahead to compute
SHOWER_THRESHOLD_PP = 25  # percentage points: window_max - hourly, to call it a "shower"

INTENSITY_BUCKETS_MM_HR = {
    "light": (0, 2.5),
    "moderate": (2.5, 7.6),
    "heavy": (7.6, 15),
    "intense": (15, float("inf")),
}


# ── Data shapes ─────────────────────────────────────────────────────────

@dataclass
class HourForecast:
    time: str                # ISO8601
    hourly_pop: float        # 0-100
    window_max_pop: float    # 0-100, same scale, from surrounding window
    rain_type: str           # "shower" | "storm" | "none"
    intensity: Optional[str] # "light" | "moderate" | "heavy" | "intense" | None
    intensity_mm_hr: float   # ensemble-mean precip rate if raining


@dataclass
class CurrentConditions:
    temp_c: float
    wind_kph: float
    short_forecast: str
    station_id: str
    station_distance_km: float
    observed_at: str         # ISO8601
    minutes_since_update: float


# ── GEFS ensemble fetch + probability calc ─────────────────────────────

def _latest_gefs_run() -> datetime:
    """
    Return the most recent GEFS init time that is likely published on S3.
    GEFS runs at 00/06/12/18Z with ~4-5hr latency. We use a 5hr buffer
    and fall back to the previous run if the expected one isn't available yet.
    """
    from herbie import Herbie

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    for lag_hours in (5, 11, 17, 23):  # try up to 4 consecutive runs back
        candidate = now_utc - timedelta(hours=lag_hours)
        run_hour = (candidate.hour // 6) * 6
        run_time = candidate.replace(hour=run_hour, minute=0, second=0, microsecond=0)
        # Quick check: does F03 exist for control member?
        try:
            h = Herbie(run_time, model="gefs", product="atmos.5", fxx=3, member="c00", verbose=False)
            if h.grib:
                return run_time
        except Exception:
            continue
    raise RuntimeError("Could not find a recent available GEFS run.")


def _fetch_apcp_at_point(
    run_time: datetime, member, fxx: int, lat: float, lon360: float
) -> tuple:
    """Download one (member, fxx) APCP slice and interpolate to the point."""
    from herbie import Herbie

    h = Herbie(run_time, model="gefs", product="atmos.5", fxx=fxx, member=member, verbose=False)
    ds = h.xarray(":APCP:", remove_grib=False)
    val = float(ds["tp"].interp(latitude=lat, longitude=lon360))
    return (member, fxx, max(0.0, val))  # guard against tiny negatives from interpolation


def fetch_gefs_members(lat: float, lon: float, forecast_hours: int = FORECAST_HOURS) -> dict:
    """
    Pull GEFS ensemble members for the given location and compute 3-hourly
    precipitation buckets (mm) for each member out to `forecast_hours`.

    GEFS is 3-hourly, not hourly — APCP is cumulative from model init, so
    each bucket is the diff between consecutive fxx steps. Returns:

        {
          "2026-09-05T03:00:00Z": [0.0, 0.2, 1.4, ...],  # mm per member
          "2026-09-05T06:00:00Z": [...],
          ...
        }

    Downloads 31 members × N fxx steps in parallel (ThreadPoolExecutor).
    Herbie caches GRIB subsets locally, so re-runs for the same model run
    hit disk instead of S3.
    """
    import warnings
    warnings.filterwarnings("ignore")

    run_time = _latest_gefs_run()
    fxx_steps = list(range(3, forecast_hours + 1, 3))   # [3, 6, 9, ..., 24]
    members = ["c00"] + list(range(1, 31))               # 31 total
    lon360 = lon % 360                                   # GEFS grid is 0–360

    print(f"  GEFS run: {run_time:%Y-%m-%d %HZ}  |  {len(members)} members × {len(fxx_steps)} steps")

    # Download all (member, fxx) combos in parallel — bottleneck is S3 latency
    cumulative: dict[tuple, float] = {}
    tasks = [(m, f) for m in members for f in fxx_steps]

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(_fetch_apcp_at_point, run_time, m, f, lat, lon360): (m, f)
            for m, f in tasks
        }
        done = 0
        for future in as_completed(futures):
            member, fxx, val = future.result()
            cumulative[(member, fxx)] = val
            done += 1
            if done % 31 == 0:
                print(f"  {done}/{len(tasks)} slices downloaded…")

    # Convert cumulative APCP → 3-hour bucket diffs
    result: dict[str, list[float]] = {}
    for i, fxx in enumerate(fxx_steps):
        valid_time = run_time + timedelta(hours=fxx)
        time_str = valid_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        bucket_vals = []
        for member in members:
            cum = cumulative[(member, fxx)]
            if i == 0:
                bucket_mm = cum  # F03 accumulation starts from 0
            else:
                prev_fxx = fxx_steps[i - 1]
                bucket_mm = max(0.0, cum - cumulative[(member, prev_fxx)])
            bucket_vals.append(round(bucket_mm, 3))

        result[time_str] = bucket_vals

    return result


def compute_hourly_pop(member_precip_mm: list[float], rain_threshold_mm: float = 0.2) -> float:
    """% of ensemble members with precip above threshold in this single hour."""
    if not member_precip_mm:
        return 0.0
    hits = sum(1 for v in member_precip_mm if v >= rain_threshold_mm)
    return round(100 * hits / len(member_precip_mm), 1)


def compute_window_max_pop(
    hourly_members: dict[str, list[float]],
    center_time: str,
    window_hours: int = WINDOW_HOURS,
    rain_threshold_mm: float = 0.2,
) -> float:
    """
    % of ensemble members with precip above threshold at ANY point within the
    window surrounding center_time. A member only needs to hit the threshold
    in ONE hour of the window to count — this is what makes window_max_pop
    rise above the individual hourly values when timing (not occurrence) is
    the uncertain part.

    Window spans `window_hours` steps centered-forward from center_time:
    [center, center + window_hours). A member counts as a hit if it exceeds
    the threshold in ANY step within the window.
    """
    times = sorted(hourly_members.keys())
    try:
        center_idx = times.index(center_time)
    except ValueError:
        return 0.0

    window_times = times[center_idx : center_idx + window_hours]
    if not window_times:
        return 0.0

    # Determine number of members from the first available window slot
    n_members = len(hourly_members[window_times[0]])

    hits = 0
    for m in range(n_members):
        # Member m hits if it rains in any window step
        if any(hourly_members[t][m] >= rain_threshold_mm for t in window_times):
            hits += 1

    return round(100 * hits / n_members, 1)


def classify_rain_type(hourly_pop: float, window_max_pop: float) -> str:
    """Shower vs storm vs none, per PROJECT_BRIEF.md's simple starting rule."""
    if window_max_pop < 10:
        return "none"
    if (window_max_pop - hourly_pop) > SHOWER_THRESHOLD_PP:
        return "shower"
    return "storm"


def bucket_intensity(mean_precip_mm_hr: float) -> Optional[str]:
    for label, (low, high) in INTENSITY_BUCKETS_MM_HR.items():
        if low <= mean_precip_mm_hr < high:
            return label
    return None


def build_hourly_forecast(lat: float, lon: float) -> list[HourForecast]:
    """
    Top-level pipeline function: fetch members, compute both probabilities
    and type/intensity for each hour. This is what the API layer will call.
    """
    members_by_hour = fetch_gefs_members(lat, lon)  # {"time": [mm, mm, ...], ...}

    results = []
    for time_str, member_vals in members_by_hour.items():
        hourly_pop = compute_hourly_pop(member_vals)
        window_max_pop = compute_window_max_pop(members_by_hour, time_str)
        rain_type = classify_rain_type(hourly_pop, window_max_pop)
        mean_precip = sum(v for v in member_vals if v > 0) / max(
            1, sum(1 for v in member_vals if v > 0)
        )
        results.append(
            HourForecast(
                time=time_str,
                hourly_pop=hourly_pop,
                window_max_pop=window_max_pop,
                rain_type=rain_type,
                intensity=bucket_intensity(mean_precip) if hourly_pop > 0 else None,
                intensity_mm_hr=round(mean_precip, 2),
            )
        )
    return results


# ── NWS current conditions (this part works today, no ensemble needed) ──

def get_current_conditions(lat: float, lon: float) -> CurrentConditions:
    """
    NWS API current conditions, with station distance + staleness surfaced
    (per PROJECT_BRIEF.md — don't hide how far/old the reading is).

    This function is real and should work as-is against api.weather.gov —
    good one to test first when you sit down with Claude Code.
    """
    headers = {"User-Agent": "rain-app (personal project)"}

    points_resp = requests.get(
        f"https://api.weather.gov/points/{lat},{lon}", headers=headers, timeout=10
    )
    points_resp.raise_for_status()
    points = points_resp.json()

    stations_url = points["properties"]["observationStations"]
    stations_resp = requests.get(stations_url, headers=headers, timeout=10)
    stations_resp.raise_for_status()
    stations = stations_resp.json()["features"]
    if not stations:
        raise RuntimeError("No observation stations found near this location.")

    nearest = stations[0]
    station_id = nearest["properties"]["stationIdentifier"]
    station_lon, station_lat = nearest["geometry"]["coordinates"]

    obs_resp = requests.get(
        f"https://api.weather.gov/stations/{station_id}/observations/latest",
        headers=headers,
        timeout=10,
    )
    obs_resp.raise_for_status()
    obs = obs_resp.json()["properties"]

    observed_at = obs["timestamp"]
    observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    minutes_since = (datetime.now(timezone.utc) - observed_dt).total_seconds() / 60

    return CurrentConditions(
        temp_c=obs["temperature"]["value"] or 0.0,
        wind_kph=obs["windSpeed"]["value"] or 0.0,
        short_forecast=obs.get("textDescription") or "",
        station_id=station_id,
        station_distance_km=_haversine_km(lat, lon, station_lat, station_lon),
        observed_at=observed_at,
        minutes_since_update=round(minutes_since, 1),
    )


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    from math import radians, sin, cos, sqrt, atan2

    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return round(r * 2 * atan2(sqrt(a), sqrt(1 - a)), 2)


# ── CLI entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch rain forecast for a location.")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    args = parser.parse_args()

    print("Fetching current conditions...")
    current = get_current_conditions(args.lat, args.lon)
    print(json.dumps(asdict(current), indent=2))

    print("\nFetching hourly forecast (ensemble)...")
    hourly = build_hourly_forecast(args.lat, args.lon)
    print(json.dumps([asdict(h) for h in hourly], indent=2))
