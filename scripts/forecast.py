"""Produce a next-day (24h) and next-week (168h) demand forecast from a given as-of timestamp.

Demo/backtest tool: the as-of timestamp must exist in data/hourly_features.csv
with 168h of history behind it and 168h of future ahead of it (so the target-time
weather/calendar/event columns can be looked up). It uses the *actual* historical
weather/events at each target hour as a stand-in for a forecast -- a production
deployment would substitute real weather-forecast values for the target_* weather
columns instead of historical readings.

Usage: python scripts/forecast.py 2025-12-01T00:00:00
"""
import sys

import lightgbm as lgb
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

MAX_HORIZON = 168
LAG_HISTORY = 168

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


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    as_of = pd.Timestamp(sys.argv[1])

    df = pd.read_csv("data/hourly_features.csv", parse_dates=["timestamp"])
    df["event_type"] = df["event_type"].fillna("none").astype("category")

    if as_of not in set(df["timestamp"]):
        print(f"error: {as_of} not found in data/hourly_features.csv")
        sys.exit(1)
    idx = df.index[df["timestamp"] == as_of][0]
    if idx < LAG_HISTORY or idx > len(df) - 1 - MAX_HORIZON:
        print(f"error: {as_of} needs {LAG_HISTORY}h of history and {MAX_HORIZON}h of future in the dataset")
        sys.exit(1)

    load = df["load_MW"].to_numpy()
    rows = []
    for h in range(1, MAX_HORIZON + 1):
        tgt = idx + h
        row = {
            "origin_hour_of_day": df["timestamp"].dt.hour.iloc[idx],
            "origin_day_of_week": df["day_of_week"].iloc[idx],
            "origin_month": df["timestamp"].dt.month.iloc[idx],
            "lag_1": load[idx - 1],
            "lag_24": load[idx - 24],
            "lag_48": load[idx - 48],
            "lag_168": load[idx - 168],
            "rolling_mean_24h": load[idx - 23:idx + 1].mean(),
            "rolling_mean_168h": load[idx - 167:idx + 1].mean(),
            "rolling_std_24h": load[idx - 23:idx + 1].std(),
            "load_momentum_24h": load[idx] - load[idx - 24],
            "horizon": h,
            "target_hour_of_day": df["timestamp"].dt.hour.iloc[tgt],
            "target_month": df["timestamp"].dt.month.iloc[tgt],
        }
        for src, dst in TARGET_CALENDAR_COLS.items():
            row[dst] = df[src].iloc[tgt]
        for src, dst in TARGET_WEATHER_COLS.items():
            row[dst] = df[src].iloc[tgt]
        row["target_event_type"] = df["event_type"].iloc[tgt]
        for src, dst in TARGET_EVENT_COLS.items():
            row[dst] = df[src].iloc[tgt]
        rows.append(row)

    features = pd.DataFrame(rows)
    features["target_hours_to_event"] = features["target_hours_to_event"].fillna(999.0)
    features["target_hours_since_event"] = features["target_hours_since_event"].fillna(999.0)
    features["target_expected_attendance"] = features["target_expected_attendance"].fillna(0.0)
    features["target_event_type"] = features["target_event_type"].astype("category")

    booster = lgb.Booster(model_file="models/lgbm_demand_model.txt")
    feature_cols = booster.feature_name()
    preds = booster.predict(features[feature_cols])

    target_times = as_of + pd.to_timedelta(np.arange(1, MAX_HORIZON + 1), unit="h")
    out = pd.DataFrame({"timestamp": target_times, "horizon": np.arange(1, MAX_HORIZON + 1), "forecast_MW": preds})
    out.to_csv("reports/latest_forecast.csv", index=False)
    print(f"wrote reports/latest_forecast.csv ({len(out)} rows)")
    print()
    print("next-day forecast (first 24h):")
    print(out.head(24).to_string(index=False))

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(out["timestamp"], out["forecast_MW"], color="#2563eb")
    ax.axvspan(target_times[0], target_times[23], color="#2563eb", alpha=0.08, label="Next-day window")
    ax.set_title(f"Forecast from as-of {as_of}")
    ax.set_ylabel("Forecast load (MW)")
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("reports/latest_forecast.png", dpi=130)
    print("wrote reports/latest_forecast.png")


if __name__ == "__main__":
    main()
