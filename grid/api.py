"""Deterministic HTTP API over the optimization engine.

Both scenarios return the identical InterventionResult shape, so the frontend
animates them with one rendering path. State lives in the process and is never
reset by a tick or a disturbance -- only an explicit /api/reset rebuilds it.

Run: uvicorn grid.api:app --reload   (UI at http://127.0.0.1:8000/)
"""
import os
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from grid import demand as demand_mod
from grid import scenarios
from grid.schemas import DisturbanceRequest, NetworkState, TickRequest
from grid.state import LOADING_BANDS, OVERLOAD_THRESHOLD, SOFT_ALARM_THRESHOLD, GridState

app = FastAPI(title="AURA grid optimizer", version="1.0")

_state: GridState | None = None
_history: list[dict] = []
_tick_cursor = {"ts": scenarios.DEMO_START}


def get_state() -> GridState:
    global _state
    if _state is None:
        _state = scenarios.bootstrap_state()
    return _state


def _network_state(state: GridState) -> dict:
    shed = state.shed_by_substation()
    substations = []
    for name, sub in sorted(state.network.substations.items()):
        substations.append({
            "name": name,
            "lat": sub.lat,
            "lon": sub.lon,
            "role": "source" if sub.is_source else "demand",
            "demand_mw": round(state.demand_mw.get(name, 0.0), 1),
            "served_mw": round(state.served_mw.get(name, 0.0), 1),
            "shed_mw": round(shed.get(name, 0.0), 1),
        })

    corridors = state.corridor_utilisation()
    live_keys = {
        f"{a}|{b}" for (a, b), gids in state.network.corridors.items()
        if any(
            state.network.lines.loc[state.network.lines["gid"] == g].iloc[0]["status"] == "Commissioned"
            for g in gids
        )
    }
    future = [
        {"from": a, "to": b, "circuits": len(gids)}
        for (a, b), gids in state.network.corridors.items()
        if f"{a}|{b}" not in live_keys
    ]

    risk = state.risk()
    if risk["overloaded_corridors"] > 0:
        phase = "overload"
    elif risk["corridors_at_risk"] > 0:
        phase = "at_risk"
    elif risk["total_shed_mw"] > 0.1:
        phase = "shedding"
    else:
        phase = "stable"

    return {
        "tick": state.tick,
        "substations": substations,
        "corridors": {k: v for k, v in corridors.items() if k in live_keys},
        "future_corridors": future,
        "risk": risk,
        "phase": phase,
        "demand_scale": round(state.demand_scale, 4),
        "temperature_c": state.temperature_c,
    }


@app.get("/api/state", response_model=NetworkState, response_model_by_alias=True)
def read_state():
    return _network_state(get_state())


@app.get("/api/history")
def read_history():
    """Every intervention so far, oldest first -- the log never clears."""
    return _history


@app.post("/api/tick")
def post_tick(req: TickRequest):
    """Case 1: next forecast arrives, rebalance ahead of the demand."""
    state = get_state()
    ts = pd.Timestamp(req.as_of) if req.as_of else _tick_cursor["ts"]
    try:
        result = scenarios.tick(state, ts, horizon_h=req.horizon_h)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _tick_cursor["ts"] = ts + pd.Timedelta(hours=1)
    _history.append(result)
    return result


@app.post("/api/disturbance")
def post_disturbance(req: DisturbanceRequest):
    """Case 2, phase 1: a judge raises temperature/demand or fails a corridor.

    Deliberately does NOT run the optimizer -- this returns the raw, unmanaged
    physical consequence (real PTDF recalculation, genuine overloads if the
    stress is severe enough). AURA only responds when the judge separately
    calls POST /api/optimize.
    """
    state = get_state()
    try:
        result = scenarios.apply_stress(
            state, req.kind,
            temperature_delta_c=req.temperature_delta_c,
            demand_multiplier=req.demand_multiplier,
            corridor=req.corridor,
            line_gids=req.line_gids,
            as_of=_tick_cursor["ts"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _history.append(result)
    return result


@app.post("/api/optimize")
def post_optimize():
    """Case 2, phase 2: the judge's explicit [ AURA REBALANCE ]. Runs the real
    optimizer against whatever state currently exists and commits the result."""
    state = get_state()
    result = scenarios.optimize_now(state)
    _history.append(result)
    return result


@app.get("/api/optimize/preview")
def read_optimize_preview():
    """What AURA would do right now, without committing it -- powers the
    optimizer panel's "AURA CAN: redistribute N MW" line before the judge
    clicks anything. Real LP solve, real validation, zero side effects."""
    return scenarios.preview_intervention(get_state())


@app.get("/api/config")
def read_config():
    """Tells the frontend which map library to load, and the loading
    thresholds that already govern the backend's own risk calculations, so
    the map/legend/alerts never hardcode a cutoff that could drift from it.

    Mapbox GL JS renders nothing without a token, so when MAPBOX_TOKEN is unset
    the client falls back to MapLibre GL JS (API-identical) with a free dark
    basemap. Setting the env var switches it to real Mapbox with no code change.
    """
    token = os.environ.get("MAPBOX_TOKEN", "").strip()
    return {
        "mapbox_token": token,
        "engine": "mapbox" if token else "maplibre",
        "at_risk_threshold": SOFT_ALARM_THRESHOLD,
        "overload_threshold": OVERLOAD_THRESHOLD,
        "loading_bands": LOADING_BANDS,
    }


@app.get("/api/forecast")
def read_forecast(hours: int = 12):
    """Recent actual load plus the model's forward curve, for the left panel."""
    _, hourly = demand_mod._load()
    now = _tick_cursor["ts"]
    idx = hourly.index[hourly["timestamp"] == now]
    if len(idx) == 0:
        raise HTTPException(status_code=400, detail=f"{now} not in hourly features")
    i = int(idx[0])

    past = [
        {"ts": str(hourly["timestamp"].iloc[j]), "actual_mw": round(float(hourly["load_MW"].iloc[j]), 1)}
        for j in range(max(0, i - hours), i + 1)
    ]
    future = []
    for h in range(1, hours + 1):
        try:
            mw = demand_mod.citywide_forecast(now, horizon_h=h)
        except ValueError:
            break
        future.append({
            "ts": str(now + pd.Timedelta(hours=h)),
            "predicted_mw": round(mw, 1),
        })
    return {"now": str(now), "past": past, "future": future}


@app.post("/api/reset")
def post_reset():
    """Rebuild the grid from the calibrated baseline (between demo runs only)."""
    global _state, _history
    _state = scenarios.bootstrap_state()
    _history = []
    _tick_cursor["ts"] = scenarios.DEMO_START
    return _network_state(_state)


FRONTEND = Path(__file__).resolve().parent / "frontend"
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
