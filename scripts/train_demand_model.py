"""Train the direct multi-horizon LightGBM demand forecaster.

Splits data/training_table.parquet chronologically by origin timestamp (never
shuffled): last 6 weeks of origins = test, the 6 weeks before that = validation,
everything earlier = train. Trains one LightGBM model with `horizon` as a
feature so it serves every forecast distance from 1h to 168h ahead.
"""
import json

import lightgbm as lgb
import numpy as np
import pandas as pd

WEEK = pd.Timedelta(days=7)

table = pd.read_parquet("data/training_table.parquet")
table["target_event_type"] = table["target_event_type"].astype("category")

origin_max = table["origin_timestamp"].max()
test_start = origin_max - 6 * WEEK
val_start = origin_max - 12 * WEEK

train = table[table["origin_timestamp"] < val_start]
val = table[(table["origin_timestamp"] >= val_start) & (table["origin_timestamp"] < test_start)]
test = table[table["origin_timestamp"] >= test_start]

print(f"train rows: {len(train)} ({train['origin_timestamp'].min()} -> {train['origin_timestamp'].max()})")
print(f"val rows:   {len(val)} ({val['origin_timestamp'].min()} -> {val['origin_timestamp'].max()})")
print(f"test rows:  {len(test)} ({test['origin_timestamp'].min()} -> {test['origin_timestamp'].max()})")

feature_cols = [c for c in table.columns if c not in ("origin_timestamp", "target")]
cat_cols = ["target_event_type"]

X_train, y_train = train[feature_cols], train["target"]
X_val, y_val = val[feature_cols], val["target"]
X_test, y_test = test[feature_cols], test["target"]

dtrain = lgb.Dataset(X_train, label=y_train, categorical_feature=cat_cols)
dval = lgb.Dataset(X_val, label=y_val, categorical_feature=cat_cols, reference=dtrain)

params = {
    "objective": "regression",
    "metric": "l2",
    "num_leaves": 63,
    "learning_rate": 0.05,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "seed": 42,
    "verbose": -1,
}
booster = lgb.train(
    params, dtrain,
    num_boost_round=2000,
    valid_sets=[dval],
    callbacks=[lgb.early_stopping(stopping_rounds=100), lgb.log_evaluation(period=100)],
)

booster.save_model("models/lgbm_demand_model.txt")
print(f"saved models/lgbm_demand_model.txt (best_iteration={booster.best_iteration})")

pred_test = booster.predict(X_test, num_iteration=booster.best_iteration)


def mae(y, p):
    return float(np.mean(np.abs(y - p)))


def rmse(y, p):
    return float(np.sqrt(np.mean((y - p) ** 2)))


def mape(y, p):
    return float(np.mean(np.abs((y - p) / y)) * 100)


h = test["horizon"].to_numpy()
y = y_test.to_numpy()
p = pred_test

metrics = {
    "overall": {"mae": mae(y, p), "rmse": rmse(y, p), "mape": mape(y, p), "n": int(len(y))},
    "next_day_h1_24": {
        "mae": mae(y[h <= 24], p[h <= 24]), "rmse": rmse(y[h <= 24], p[h <= 24]),
        "mape": mape(y[h <= 24], p[h <= 24]), "n": int((h <= 24).sum()),
    },
    "next_week_h1_168": {
        "mae": mae(y, p), "rmse": rmse(y, p), "mape": mape(y, p), "n": int(len(y)),
    },
}

by_horizon = []
for hh in range(1, 169):
    m = h == hh
    by_horizon.append({"horizon": hh, "mae": mae(y[m], p[m]), "mape": mape(y[m], p[m])})
metrics["by_horizon"] = by_horizon

with open("reports/metrics.json", "w") as f:
    json.dump(metrics, f, indent=2)

print()
print("=== test metrics ===")
print(f"overall:          MAE={metrics['overall']['mae']:.2f} MW  RMSE={metrics['overall']['rmse']:.2f} MW  MAPE={metrics['overall']['mape']:.2f}%")
print(f"next-day (h<=24):  MAE={metrics['next_day_h1_24']['mae']:.2f} MW  RMSE={metrics['next_day_h1_24']['rmse']:.2f} MW  MAPE={metrics['next_day_h1_24']['mape']:.2f}%")

print()
print("=== top 15 feature importances (gain) ===")
imp = pd.Series(booster.feature_importance(importance_type="gain"), index=feature_cols)
print(imp.sort_values(ascending=False).head(15).to_string())

print()
print("wrote reports/metrics.json")

test_predictions = pd.DataFrame({
    "origin_timestamp": test["origin_timestamp"].to_numpy(),
    "horizon": h,
    "actual": y,
    "predicted": p,
})
test_predictions.to_parquet("data/test_predictions.parquet", index=False)
print("wrote data/test_predictions.parquet")
