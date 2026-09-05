# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A Delhi electricity demand forecasting project. Raw 5-minute demand readings, hourly weather, and a calendar/event dataset are aligned and merged into a single feature-rich dataset, which is then used to train a LightGBM model that forecasts demand anywhere from 1 hour to 1 week ahead.

## Setup

There is no dependency manifest yet (no `requirements.txt`/`pyproject.toml`) — recreate the environment with:

```
python3 -m venv .venv
source .venv/bin/activate
pip install lightgbm pandas numpy scipy matplotlib pyarrow \
    networkx pandapower pulp fastapi uvicorn httpx
```

`.venv/` is gitignored; the commands above are the source of truth for what's needed.

## Running the pipeline

There's no build system, linter, or test suite — the pipeline is a sequence of scripts run from the repo root (venv activated), each consuming the previous one's output:

1. `python scripts/align_datasets.py` — aligns hourly weather onto the 5-min demand timestamps via a backward as-of merge → `data/demand_weather.csv`
2. `python scripts/build_final_dataset.py` — collapses `delhi_events.csv`'s sparse one-hot event flags into engineered features (`event_type`/`event_name`/`event_intensity`/`hours_to_event`/`hours_since_event`/`event_active`/`expected_attendance`) and merges with `demand_weather.csv` → `data/final_dataset.csv`
3. `python scripts/prepare_hourly.py` — resamples `final_dataset.csv` to hourly resolution and imputes gaps in `load_MW` → `data/hourly_features.csv`
4. `python scripts/build_training_table.py` — expands `hourly_features.csv` into a direct multi-horizon training table (one row per origin-hour × horizon pair) → `data/training_table.parquet`
5. `python scripts/train_demand_model.py` — trains the LightGBM model on a chronological train/val/test split → `models/lgbm_demand_model.txt`, `reports/metrics.json`, `data/test_predictions.parquet`
6. `python scripts/make_reports.py` — builds `reports/error_by_horizon.png` and `reports/sample_forecast.png` from the trained model's test predictions
7. `python scripts/forecast.py <timestamp>` — produces a 168h forecast from any as-of timestamp present in `hourly_features.csv` → `reports/latest_forecast.csv` / `.png`

## Architecture

### Data lineage
```
demand_5min.csv + weather_hourly.csv
  --(align_datasets.py)--> demand_weather.csv
  + delhi_events.csv --(build_final_dataset.py)--> final_dataset.csv        [5-min, canonical merged dataset]
  --(prepare_hourly.py)--> hourly_features.csv                              [hourly, ML-ready]
  --(build_training_table.py)--> training_table.parquet                    [one row per (origin hour, horizon)]
  --(train_demand_model.py)--> lgbm_demand_model.txt + metrics.json + test_predictions.parquet
```

### Design decisions that aren't obvious from any single file

- **Direct multi-horizon forecasting**: one LightGBM model with `horizon` (1–168h) as a feature, rather than a recursive one-step model or 168 separate per-horizon models. Every training row pairs "origin time `t`" features (lags, rolling stats) with "target time `t+h`" features (calendar, weather, events) and a target of `load_MW` at `t+h`. See `scripts/build_training_table.py` and `scripts/train_demand_model.py`.
- **Weather-as-forecast assumption**: target-time weather columns use actual historical readings as a stand-in for a real weather forecast. This is a backtesting simplification, not production-ready — a live deployment must substitute real forecast values (see the docstring in `scripts/forecast.py`).
- **Event-type priority resolution**: when multiple calendar events overlap the same hour, `event_type` is resolved by priority: `major_event > concert > match > election > festival > holiday > school_break` (the `PRIORITY` dict in `scripts/build_final_dataset.py`). `is_holiday` and `is_school_break` remain standalone flags even when a higher-priority event wins that hour's `event_type`.
- **Sentinel values instead of dropped rows**: `hours_to_event`/`hours_since_event`/`expected_attendance` are null whenever no event is nearby (the common case). These get sentinel fills (999 / 0) in `scripts/build_training_table.py` rather than causing the row to be dropped — dropping on any-null was tried first and discarded 75% of the training set.
- **Known, deliberate data gaps**: 3 calendar dates and all match/concert event names are left null in `final_dataset.csv` because they couldn't be verified from public record. The exact dates and the reasoning are in the name-lookup tables at the top of `scripts/build_final_dataset.py`.

## Environment notes

Git repo with a single local commit and no remote configured yet.

## Grid optimization engine (`grid/`)

A second subsystem that consumes the forecaster's output: a DC optimal power flow
engine over Delhi's transmission network, driving two demo scenarios through one
shared optimization core.

### Running it

```
source .venv/bin/activate
python -m grid.demo                                   # both scenarios, no server
uvicorn grid.api:app --host 127.0.0.1 --port 8000     # UI at http://127.0.0.1:8000/
```

### Module map

| file | role |
|---|---|
| `data.py` | CSV → substations (nodes) + NetworkX MultiGraph; derives nodal demand |
| `electrical.py` | shared topology, PTDF computation, pandapower DC power-flow validation |
| `demand.py` | citywide LightGBM forecast → per-substation demand; temperature lever |
| `state.py` | `GridState`: mutable loads/outages/flows, carried across interventions |
| `core.py` | the DC-OPF (PuLP) + greedy switching relief — one core, both scenarios |
| `scenarios.py` | `tick()` (Case 1, auto-optimizing), `apply_stress()`/`disturbance()` (Case 2, naive — no auto-optimize), `optimize_now()`/`preview_intervention()` (AURA's separate, explicit response) |
| `api.py` | FastAPI: `/api/state`, `/api/tick`, `/api/disturbance`, `/api/optimize`, `/api/optimize/preview`, `/api/forecast`, `/api/config`, `/api/history`, `/api/reset` |
| `frontend/` | single-screen 3D map twin (vanilla JS, no build step) served by FastAPI at `/` |
| `frontend/map.js` | dual-engine Mapbox/MapLibre loader + all GL layer definitions |
| `frontend/chart.js` | inline-SVG forecast chart for the left panel |

### Modeling decisions that aren't obvious from any single file

- **Nodal demand is derived, not given.** The CSV has no per-substation demand
  column. Summing `current_load_mw` as `+` at each line's "from" substation and
  `-` at its "to" substation yields a net injection that sums to ~0 across all 20
  substations (Kirchhoff's current law holds on the snapshot). Negative nodes are
  demand centres, positive are sources.
- **Only `status == "Commissioned"` lines carry power** (560 of 1000). The rest
  are future infrastructure — displayed on the twin, never routed through.
- **The optimizer decides injections, not flows.** Power splits by impedance, so
  a transport-style LP would propose dispatches physics ignores. `core.py` is a
  DC-OPF: PTDF maps nodal injections to line flows exactly as power flow would.
  `electrical.build_topology` is the single source of truth both the PTDF and the
  pandapower model are built from — that's why LP-predicted and power-flow
  loadings agree to ~0.01 percentage points.
- **Line impedances are approximated** from `condu_type`/`bundl_type`/`length_km`
  using standard per-conductor Ω/km values; the CSV has no r/x columns.
- **DC, not AC, power flow** — no reactive power or voltage set-points exist in
  the data. DC is lossless by construction, so reported losses come from the LP's
  resistance-weighted estimate, not from pandapower.
- **Load shedding is not a fallback path.** Unserved load is the most expensive
  objective term, so the solver sheds only when no feasible dispatch exists, and
  then sheds the minimum. Both scenarios get graceful degradation for free.
- **The binding constraint is always an individual circuit**, never the corridor
  average — a saturated 250 MW line inside a 5 GW corridor is what strands load.
  Risk reporting and the UI colour by `max_circuit_loading` for this reason.
- **Switching is a real intervention.** When a saturated low-capacity circuit
  blocks delivery, `optimize_with_switching` opens it so parallel circuits pick
  up the flow — never the last live circuit on a corridor.
- **Stress and optimization are two separate steps, on purpose.** A judge
  action (`apply_stress`/`disturbance`) mutates demand or fails a line and then
  calls `core.naive_dispatch()` — a real PTDF flow recalculation with *no*
  redispatch, showing the unmanaged physical consequence, genuine overloads
  included. It never calls the optimizer. `optimize_now()` is the separate,
  explicit action that runs the real DC-OPF + validation loop and commits it.
  `preview_intervention()` runs that same real solve with `commit=False` (the
  LP and pandapower validation execute for real; only the final state
  write-back is skipped) so the UI can honestly show what AURA *would* do
  before the judge clicks anything, with zero side effects — verified by
  hashing `/api/state` before and after a preview call.

### Frontend

A Bento-grid dashboard (`index.html`/`style.css`) built around one causal
narrative: forecast → stress → grid impact → AURA optimizer → validation. The
digital twin map is the largest cell; every other card is sized to its
importance (status/demand small, forecast/alerts/optimizer medium, validation
wide, event log narrow-tall and internally scrolling). A stress event **never**
auto-triggers the optimizer — `[ Simulate Stress ]` and `[ AURA Rebalance ]`
are separate, explicit actions, matching the backend split above. A technical
details drawer (model/horizon/power-flow/optimizer/validation, plus the raw
rejected-alternatives list) is off-canvas and secondary by design.

- **Map engine is chosen at runtime.** Mapbox GL JS renders nothing without a
  token, so `/api/config` reports whether `MAPBOX_TOKEN` is set: if it is, the
  client loads Mapbox GL JS + `dark-v11`; otherwise MapLibre GL JS (API-identical
  fork) + CARTO dark-matter. Same layer code either way — export
  `MAPBOX_TOKEN=pk...` before uvicorn to switch.
- **Substation coordinates are display-only.** `REAL_COORDS` in `data.py`
  replaces the CSV's 0.01°-rounded positions with the actual Delhi localities so
  the network lands on real geography. Impedances still come from the CSV's
  `length_km`, so the optimizer is bit-identical — verified by hashing the
  dispatch before and after the change. The known cost: a corridor's drawn length
  need not match its `length_km`.
- **Corridors carry two independent encodings.** Thickness/sag is driven by
  `abs(flow_mw)` normalised against the network's own current max flow —
  *never* by capacity, so a big corridor carrying little power looks thin.
  Colour is driven by `loading` against the five-band table `/api/config`
  serves (`safe/elevated/high/overloaded/critical`, from `state.LOADING_BANDS`)
  — the same thresholds the backend's `risk()` uses, so the map, the Grid
  Alerts panel, and the legend can never disagree about what "overloaded"
  means. Only elevated-and-above bands get glow/animation at all; a stable
  grid deliberately looks subdued, not like a uniform glowing spiderweb.
- **Substations are DOM antenna markers**, not a GL layer, specifically so a
  size/colour change on re-render is an ordinary CSS transition — antenna
  height tracks live load, colour flips to red the instant a substation sheds.
- **An AURA-reroute overlay** (gold, temporary) marks whatever the *last*
  optimize response's `affected_corridors` actually changed, laid on top of —
  never instead of — the real thickness/colour, which update immediately and
  permanently regardless of whether the overlay is still fading.

- **The demo starts at a calibrated operating point.** The snapshot's flows
  satisfy conservation but not impedance physics, so the full nominal 45.8 GW is
  not physically deliverable. `bootstrap_state()` bisects for the largest
  servable demand and starts at 80% of it, so the grid begins healthy and
  disturbances have somewhere to go.
