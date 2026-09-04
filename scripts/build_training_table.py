"""Build the direct multi-horizon training table for the LightGBM demand forecaster.

For every valid origin hour t and horizon h in 1..168, one row is produced with
features known at t (lags, rolling stats, origin calendar) plus features of the
target hour t+h (calendar, events, and weather -- weather here is the actual
historical reading standing in for a weather *forecast*; a production system
would substitute real forecast values for the target_* weather columns).
"""
import numpy as np
import pandas as pd

MAX_HORIZON = 168
LAG_HISTORY = 168

df = pd.read_csv("data/hourly_features.csv", parse_dates=["timestamp"])
df["event_type"] = df["event_type"].fillna("none").astype("category")

n = len(df)
load = df["load_MW"].to_numpy()
ts = df["timestamp"].to_numpy()

# --- origin-time features (available at every row t, independent of horizon) ---
hour_of_day = df["timestamp"].dt.hour.to_numpy()
day_of_week = df["day_of_week"].to_numpy()
month = df["timestamp"].dt.month.to_numpy()

def shifted(arr, k):
    """arr shifted forward by k rows (i.e. value from k rows ago), NaN-padded."""
    out = np.full(n, np.nan)
    if k < n:
        out[k:] = arr[:n - k]
    return out

lag_1 = shifted(load, 1)
lag_24 = shifted(load, 24)
lag_48 = shifted(load, 48)
lag_168 = shifted(load, LAG_HISTORY)

roll_mean_24 = pd.Series(load).rolling(24).mean().to_numpy()
roll_mean_168 = pd.Series(load).rolling(168).mean().to_numpy()
roll_std_24 = pd.Series(load).rolling(24).std().to_numpy()
momentum_24 = load - lag_24

origin = pd.DataFrame({
    "origin_idx": np.arange(n),
    "origin_hour_of_day": hour_of_day,
    "origin_day_of_week": day_of_week,
    "origin_month": month,
    "lag_1": lag_1,
    "lag_24": lag_24,
    "lag_48": lag_48,
    "lag_168": lag_168,
    "rolling_mean_24h": roll_mean_24,
    "rolling_mean_168h": roll_mean_168,
    "rolling_std_24h": roll_std_24,
    "load_momentum_24h": momentum_24,
})

# valid origins: full lag history behind them, full horizon range ahead of them
valid_origin = (origin["origin_idx"] >= LAG_HISTORY) & (origin["origin_idx"] <= n - 1 - MAX_HORIZON)
origin = origin[valid_origin].reset_index(drop=True)
origin["origin_timestamp"] = ts[origin["origin_idx"].to_numpy()]
print(f"valid origins: {len(origin)}")

target_weather_cols = {
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
target_event_cols = {
    "event_type": "target_event_type",
    "event_intensity": "target_event_intensity",
    "event_active": "target_event_active",
    "hours_to_event": "target_hours_to_event",
    "hours_since_event": "target_hours_since_event",
    "expected_attendance": "target_expected_attendance",
}
target_calendar_cols = {
    "day_of_week": "target_day_of_week",
    "is_weekend": "target_is_weekend",
    "is_holiday": "target_is_holiday",
    "is_school_break": "target_is_school_break",
}

parts = []
for h in range(1, MAX_HORIZON + 1):
    tgt_idx = origin["origin_idx"].to_numpy() + h
    block = origin.copy()
    block["horizon"] = h
    block["target"] = load[tgt_idx]
    block["target_hour_of_day"] = df["timestamp"].dt.hour.to_numpy()[tgt_idx]
    block["target_month"] = df["timestamp"].dt.month.to_numpy()[tgt_idx]
    for src, dst in target_calendar_cols.items():
        block[dst] = df[src].to_numpy()[tgt_idx]
    for src, dst in target_weather_cols.items():
        block[dst] = df[src].to_numpy()[tgt_idx]
    block["target_event_type"] = df["event_type"].to_numpy()[tgt_idx]
    for src, dst in target_event_cols.items():
        if src == "event_type":
            continue
        block[dst] = df[src].to_numpy()[tgt_idx]
    parts.append(block)
    if h % 24 == 0:
        print(f"built horizon block h={h}")

table = pd.concat(parts, ignore_index=True)
table["target_event_type"] = table["target_event_type"].astype("category")
table = table.drop(columns=["origin_idx"])

# hours_to_event / hours_since_event / expected_attendance are null whenever no
# event is active/upcoming/recent (the common case) or, for expected_attendance,
# whenever the winning event is a match/concert with no verifiable attendance
# figure -- these are meaningful values, not missing data, so they get sentinel
# fills rather than causing the row to be dropped.
table["target_hours_to_event"] = table["target_hours_to_event"].fillna(999.0)
table["target_hours_since_event"] = table["target_hours_since_event"].fillna(999.0)
table["target_expected_attendance"] = table["target_expected_attendance"].fillna(0.0)

print(f"training table rows: {len(table)}, columns: {len(table.columns)}")
print(f"null feature rows (dropped): {table.isna().any(axis=1).sum()}")
table = table.dropna()
print(f"final rows after dropping any-null rows: {len(table)}")

table.to_parquet("data/training_table.parquet", index=False)
print("wrote data/training_table.parquet")
