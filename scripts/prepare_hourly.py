"""Resample final_dataset.csv (5-min) to hourly resolution for the demand forecaster.

load_MW is averaged across each hour; weather is averaged (it is already constant
within the hour, so this is equivalent to a lookup); calendar/event columns take
the first reading of the hour (they only change at hour boundaries in practice).
"""
import pandas as pd

df = pd.read_csv("data/final_dataset.csv", parse_dates=["timestamp"])
df = df.set_index("timestamp")

weather_cols = [
    "temperature_2m (°C)", "relative_humidity_2m (%)", "apparent_temperature (°C)",
    "wind_speed_10m (km/h)", "cloud_cover (%)", "rain (mm)", "precipitation (mm)",
    "is_day ()", "shortwave_radiation (W/m²)",
]
first_cols = [
    "date", "day_of_week", "is_weekend", "is_holiday", "is_school_break",
    "event_type", "event_intensity", "hours_to_event", "hours_since_event",
    "event_active", "expected_attendance",
]

agg = {"load_MW": "mean"}
agg.update({c: "mean" for c in weather_cols})
agg.update({c: "first" for c in first_cols})

hourly = df.resample("1h").agg(agg)
hourly = hourly.reset_index()

n_missing = hourly["load_MW"].isna().sum()

# Short gaps (<=3h): linear time interpolation. Longer gaps, including full
# missing days (e.g. 2024-01-01/02, 2026-01-08 are entirely NaN in the source
# data): seasonal-naive fill from the same hour one week prior/after, since a
# straight-line interpolation across a full day would erase the daily demand
# cycle instead of preserving it.
hourly["load_MW"] = hourly["load_MW"].interpolate(method="linear", limit=3)
still_missing = hourly["load_MW"].isna()
if still_missing.any():
    by_week_before = hourly["load_MW"].shift(168)
    by_week_after = hourly["load_MW"].shift(-168)
    hourly.loc[still_missing, "load_MW"] = by_week_before[still_missing]
    still_missing = hourly["load_MW"].isna()
    hourly.loc[still_missing, "load_MW"] = by_week_after[still_missing]

hourly.to_csv("data/hourly_features.csv", index=False)
print(f"rows: {len(hourly)}")
print(f"range: {hourly['timestamp'].min()} -> {hourly['timestamp'].max()}")
print(f"load_MW nulls before fill: {n_missing}, after fill: {hourly['load_MW'].isna().sum()}")
print("wrote data/hourly_features.csv")
