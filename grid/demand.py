"""Turn a citywide MW forecast into per-substation demand.

The LightGBM model forecasts Delhi as a single number; the grid needs demand at
each of the 20 substations. Each demand substation keeps its share of the
baseline nodal profile (derived from the network snapshot), and the whole
profile is scaled so it sums to the forecast total. Supply headroom scales with
it so sources can actually follow the load.

The same path serves both scenarios: Case 1 feeds it a forecast for the next
5-minute tick, Case 2 feeds it a forecast re-run with the judge's temperature
override, or a flat multiplier when the judge just wants "+20% demand".
"""
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

# Feature names must match the trained LightGBM model.  They live here rather
# than in the offline reporting script so the web runtime has no plotting
# dependency.
TARGET_WEATHER_COLS = {
    "temperature_2m (°C)": "target_temperature",
    "relative_humidity_2m (%)": "target_humidity",
    "apparent_temperature (°C)": "target_apparent_temp",
    "wind_speed_10m (km/h)": "target_wind",
    "cloud_cover (%)": "target_cloud_cover",
    "rain (mm)": "target_rain",
    "precipitation (mm)": "target_precipitation",
    "is_day ()": "target_is_day",
    "shortwave_radiation (W/m²)": "target_radiation",
}
TARGET_EVENT_COLS = {
    "event_intensity": "target_event_intensity",
    "event_active": "target_event_active",
    "hours_to_event": "target_hours_to_event",
    "hours_since_event": "target_hours_since_event",
    "expected_attendance": "target_expected_attendance",
}
TARGET_CALENDAR_COLS = {
    "day_of_week": "target_day_of_week",
    "is_weekend": "target_is_weekend",
    "is_holiday": "target_is_holiday",
    "is_school_break": "target_is_school_break",
}

REPO = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO / "models" / "lgbm_demand_model.txt"
HOURLY_PATH = REPO / "data" / "hourly_features.csv"

_booster: lgb.Booster | None = None
_hourly: pd.DataFrame | None = None


def _load():
    global _booster, _hourly
    if _booster is None:
        _booster = lgb.Booster(model_file=str(MODEL_PATH))
    if _hourly is None:
        _hourly = pd.read_csv(HOURLY_PATH, parse_dates=["timestamp"])
        _hourly["event_type"] = _hourly["event_type"].fillna("none").astype("category")
    return _booster, _hourly


def citywide_forecast(
    as_of: pd.Timestamp,
    horizon_h: int = 2,
    temperature_delta_c: float = 0.0,
) -> float:
    """Forecast citywide demand `horizon_h` hours after `as_of`.

    temperature_delta_c shifts the target hour's temperature before predicting,
    which is how the judge console's temperature lever works -- it goes through
    the real model, not a hand-written heuristic.
    """
    booster, hourly = _load()
    matches = hourly.index[hourly["timestamp"] == as_of]
    if len(matches) == 0:
        raise ValueError(f"{as_of} not present in hourly_features.csv")
    idx = int(matches[0])
    if idx < 168 or idx + horizon_h >= len(hourly):
        raise ValueError(f"{as_of} needs 168h history and {horizon_h}h future in the dataset")

    load = hourly["load_MW"].to_numpy()
    tgt = idx + horizon_h
    row = {
        "origin_hour_of_day": hourly["timestamp"].dt.hour.iloc[idx],
        "origin_day_of_week": hourly["day_of_week"].iloc[idx],
        "origin_month": hourly["timestamp"].dt.month.iloc[idx],
        "lag_1": load[idx - 1],
        "lag_24": load[idx - 24],
        "lag_48": load[idx - 48],
        "lag_168": load[idx - 168],
        "rolling_mean_24h": load[idx - 23: idx + 1].mean(),
        "rolling_mean_168h": load[idx - 167: idx + 1].mean(),
        "rolling_std_24h": load[idx - 23: idx + 1].std(),
        "load_momentum_24h": load[idx] - load[idx - 24],
        "horizon": horizon_h,
        "target_hour_of_day": hourly["timestamp"].dt.hour.iloc[tgt],
        "target_month": hourly["timestamp"].dt.month.iloc[tgt],
    }
    for src, dst in TARGET_CALENDAR_COLS.items():
        row[dst] = hourly[src].iloc[tgt]
    for src, dst in TARGET_WEATHER_COLS.items():
        row[dst] = hourly[src].iloc[tgt]
    row["target_event_type"] = hourly["event_type"].iloc[tgt]
    for src, dst in TARGET_EVENT_COLS.items():
        row[dst] = hourly[src].iloc[tgt]

    row["target_temperature"] = float(row["target_temperature"]) + temperature_delta_c
    row["target_apparent_temp"] = float(row["target_apparent_temp"]) + temperature_delta_c

    frame = pd.DataFrame([row])
    frame["target_hours_to_event"] = frame["target_hours_to_event"].fillna(999.0)
    frame["target_hours_since_event"] = frame["target_hours_since_event"].fillna(999.0)
    frame["target_expected_attendance"] = frame["target_expected_attendance"].fillna(0.0)
    frame["target_event_type"] = frame["target_event_type"].astype("category")

    return float(booster.predict(frame[booster.feature_name()])[0])


def apply_citywide_mw(state, citywide_mw: float) -> dict[str, float]:
    """Move substation demand in proportion to the citywide forecast.

    The forecaster is trained on the metered city load series, a different
    measurement from the transmission snapshot's implied nodal demand, so the
    forecast is applied as a *ratio* against the reference forecast that the
    grid's calibrated starting point corresponds to -- never as an absolute MW
    figure. A forecast 40% above the reference means 40% more nodal demand.
    """
    ref = state.citywide_ref_mw or _baseline_city_mw()
    scale = state.baseline_scale * (citywide_mw / ref) if ref else state.baseline_scale
    return apply_scale(state, scale)


def apply_scale(state, scale: float) -> dict[str, float]:
    """Set demand (and matching supply headroom) to `scale` x the snapshot."""
    state.demand_scale = scale
    for name, sub in state.network.substations.items():
        state.demand_mw[name] = sub.baseline_demand_mw * scale
        state.supply_cap_mw[name] = sub.baseline_supply_mw * max(1.35, scale * 1.35)
    return dict(state.demand_mw)


def _baseline_city_mw() -> float:
    """Mean citywide load in the training data -- the reference for ratios."""
    _, hourly = _load()
    return float(hourly["load_MW"].mean())
