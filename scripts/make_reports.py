"""Build the two evaluation plots from the trained model's test-set predictions."""
import json

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

with open("reports/metrics.json") as f:
    metrics = json.load(f)

preds = pd.read_parquet("data/test_predictions.parquet")

# --- error_by_horizon.png ---
by_h = pd.DataFrame(metrics["by_horizon"])
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
ax1.plot(by_h["horizon"], by_h["mae"], color="#2563eb")
ax1.set_ylabel("MAE (MW)")
ax1.set_title("Forecast error vs. horizon (test set)")
ax1.axvline(24, color="gray", linestyle="--", linewidth=1)
ax1.text(24, ax1.get_ylim()[1] * 0.95, " next-day cutoff", fontsize=8, color="gray")
ax2.plot(by_h["horizon"], by_h["mape"], color="#dc2626")
ax2.set_ylabel("MAPE (%)")
ax2.set_xlabel("Horizon (hours ahead)")
ax2.axvline(24, color="gray", linestyle="--", linewidth=1)
fig.tight_layout()
fig.savefig("reports/error_by_horizon.png", dpi=130)
print("wrote reports/error_by_horizon.png")

# --- sample_forecast.png ---
# Pick an origin roughly in the middle of the test window with a full 168h horizon set.
origins = sorted(preds["origin_timestamp"].unique())
sample_origin = origins[len(origins) // 2]
sample = preds[preds["origin_timestamp"] == sample_origin].sort_values("horizon")
target_times = pd.to_datetime(sample_origin) + pd.to_timedelta(sample["horizon"], unit="h")

fig, ax = plt.subplots(figsize=(12, 5))
ax.plot(target_times, sample["actual"], label="Actual", color="#111827", linewidth=1.5)
ax.plot(target_times, sample["predicted"], label="Forecast", color="#2563eb", linewidth=1.5, linestyle="--")
ax.axvspan(target_times.iloc[0], target_times.iloc[23], color="#2563eb", alpha=0.06, label="Next-day window")
ax.set_title(f"Sample 168h forecast vs. actual (as-of {pd.to_datetime(sample_origin)})")
ax.set_ylabel("Load (MW)")
ax.legend()
fig.autofmt_xdate()
fig.tight_layout()
fig.savefig("reports/sample_forecast.png", dpi=130)
print("wrote reports/sample_forecast.png")
