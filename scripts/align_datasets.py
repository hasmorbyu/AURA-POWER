"""Align hourly weather data onto the 5-minute electricity demand timestamps.

Each demand timestamp is matched to the most recent hourly weather reading
(as-of backward merge), since weather is reported once per hour and holds
roughly constant until the next reading.
"""
import pandas as pd

START = "2023-04-01 00:00:00"
END = "2026-01-12 23:55:00"

demand = pd.read_csv("data/demand_5min.csv", parse_dates=["timestamp"])
demand = demand[(demand["timestamp"] >= START) & (demand["timestamp"] <= END)].sort_values("timestamp")

weather = pd.read_csv("data/weather_hourly.csv", skiprows=2, parse_dates=["time"])
weather = weather.rename(columns={"time": "timestamp"}).sort_values("timestamp")

aligned = pd.merge_asof(demand, weather, on="timestamp", direction="backward")

missing = aligned["temperature_2m (°C)"].isna().sum()
print(f"demand rows: {len(demand)}")
print(f"weather rows: {len(weather)}")
print(f"aligned rows: {len(aligned)}")
print(f"rows with no matched weather: {missing}")

aligned.to_csv("data/aligned_demand_weather.csv", index=False)
print("wrote data/aligned_demand_weather.csv")
