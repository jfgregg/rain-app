"""
api.py — local FastAPI server for the Rain app.

Endpoints:
    GET /current?lat=&lon=     — NWS current conditions (temp, wind, staleness)
    GET /forecast?lat=&lon=    — GEFS ensemble hourly forecast (pop, type, intensity)
    GET /all?lat=&lon=         — both in one shot (what the frontend will use)

Run with:
    uvicorn api:app --reload --port 8000
"""

import time
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from fetch_forecast import (
    build_hourly_forecast,
    get_current_conditions,
)

# Simple in-process cache: keyed by (lat, lon), holds (result, timestamp).
# Forecast data is refreshed at most once per hour — GEFS runs every 6 hrs
# and the ensemble fetch takes minutes, so re-fetching on every request would
# make the app unusable.
_forecast_cache: dict[tuple, tuple] = {}
FORECAST_TTL = 3600  # seconds


def _cached_forecast(lat: float, lon: float) -> list:
    key = (round(lat, 4), round(lon, 4))
    cached = _forecast_cache.get(key)
    if cached:
        result, ts = cached
        if time.time() - ts < FORECAST_TTL:
            return result
    hours = build_hourly_forecast(lat, lon)
    _forecast_cache[key] = (hours, time.time())
    return hours

app = FastAPI(title="Rain API", version="0.1.0")

# Allow the local frontend (file:// or localhost dev server) to call this
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/current")
def current_conditions(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """NWS current observation nearest to (lat, lon)."""
    try:
        result = get_current_conditions(lat, lon)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return asdict(result)


@app.get("/forecast")
def hourly_forecast(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """GEFS ensemble hourly forecast for (lat, lon)."""
    try:
        hours = _cached_forecast(lat, lon)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return [asdict(h) for h in hours]


@app.get("/all")
def all_data(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """Current conditions + hourly forecast in one response."""
    try:
        current = get_current_conditions(lat, lon)
        hours = _cached_forecast(lat, lon)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "current": asdict(current),
        "forecast": [asdict(h) for h in hours],
    }
