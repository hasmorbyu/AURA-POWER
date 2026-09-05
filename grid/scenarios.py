"""The two scenario drivers, both running the same optimize -> validate loop.

Case 1 (tick) reacts to a new forecast. Case 2 (disturbance) reacts to a judge
raising temperature/demand or failing a corridor. Neither resets state: each
call mutates the running GridState and re-optimizes from wherever the grid
currently is.
"""
import pandas as pd

from grid import demand as demand_mod
from grid.core import Solution, naive_dispatch, optimize_with_switching
from grid.electrical import validate
from grid.state import SOFT_ALARM_THRESHOLD, GridState

MAX_ATTEMPTS = 4
DERATE_SAFETY = 0.95


def run_intervention(state: GridState, trigger: dict, commit: bool = True) -> dict:
    """Optimize, validate through power flow, retry on violations, then apply.

    Each candidate rejected by power flow is recorded with the assets that
    failed it, and those lines are derated so the next solve routes around
    them. Only a validated dispatch is ever written back to the state.

    `commit=False` runs the real LP solve and real pandapower validation --
    nothing about the computation is skipped -- but restores every field it
    touched before returning, so a speculative "what would AURA do right now"
    preview can never leave a side effect on the shared GridState.
    """
    util_before = state.corridor_utilisation()
    risk_before = state.risk()
    served_before = dict(state.served_mw)
    flows_before = dict(state.flows_mw)
    injections_before = dict(state.injections_mw)
    switched_before = set(state.switched_out)
    tick_before = state.tick

    derate: dict[int, float] = {}
    rejected: list[dict] = []
    accepted: Solution | None = None
    accepted_switches: list[int] = []
    validation = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        solution, opened = optimize_with_switching(state, derate)
        if not solution.feasible:
            rejected.append({
                "attempt": attempt,
                "reason": f"LP infeasible ({solution.status})",
                "violating_assets": [],
            })
            break

        # The switching search proposes opening bottleneck circuits; those
        # have to be in place for power flow to validate the same network.
        state.switched_out.update(opened)
        validation = validate(state, solution)
        if validation.converged and validation.ok:
            accepted, accepted_switches = solution, opened
            break
        state.switched_out.difference_update(opened)

        rejected.append({
            "attempt": attempt,
            "reason": validation.reason or "power flow rejected the dispatch",
            "violating_assets": validation.violations[:6],
        })
        if not validation.violations:
            break
        for v in validation.violations:
            gid = v["gid"]
            shrink = min(1.0, 100.0 / max(v["loading_pct"], 1.0)) * DERATE_SAFETY
            derate[gid] = derate.get(gid, 1.0) * shrink

    if accepted is None and validation is not None and validation.converged:
        # Physics still shows overloads after retries. Take the last dispatch
        # anyway (it is the least-bad found) but report it as unvalidated so
        # the operator/judge sees the grid is running beyond limits.
        accepted, accepted_switches = solution, opened
        state.switched_out.update(opened)

    if accepted is None:
        return _result(
            state, trigger, accepted=False, actions=[], rejected=rejected,
            util_before=util_before, risk_before=risk_before,
            served_before=served_before, flows_before=flows_before,
            solution=None, validation=validation,
            message="No feasible dispatch found; grid state left unchanged.",
        )

    state.flows_mw = dict(accepted.flows_mw)
    state.injections_mw = dict(accepted.injections_mw)
    state.served_mw = dict(accepted.served_mw)
    state.tick += 1
    # switched_out already reflects `accepted_switches` from the loop above,
    # whether or not we end up committing -- reverted below if commit=False
    switch_actions = [
        {
            "kind": "switch_out",
            "corridor": _corridor_of(state, gid),
            "substation": None,
            "delta_mw": 0.0,
            "detail": f"opened bottleneck circuit {_line_name(state, gid)} to relieve congestion",
        }
        for gid in accepted_switches
    ]

    actions = switch_actions + _build_actions(state, accepted, flows_before, served_before)
    validated = bool(validation and validation.converged and validation.ok)
    message = (
        "Dispatch validated by DC power flow and applied."
        if validated else
        "Applied best available dispatch; power flow still reports overloads."
    )
    result = _result(
        state, trigger, accepted=True, actions=actions, rejected=rejected,
        util_before=util_before, risk_before=risk_before,
        served_before=served_before, flows_before=flows_before,
        solution=accepted, validation=validation, message=message,
    )
    if not commit:
        # real solve, real validation, but a preview must leave no trace
        state.flows_mw = flows_before
        state.injections_mw = injections_before
        state.served_mw = served_before
        state.switched_out = switched_before
        state.tick = tick_before
    return result


def _build_actions(state, solution, flows_before, served_before) -> list[dict]:
    actions: list[dict] = []

    corridor_delta: dict[str, float] = {}
    for (a, b), gids in state.network.corridors.items():
        delta = sum(
            abs(solution.flows_mw.get(g, 0.0)) - abs(flows_before.get(g, 0.0))
            for g in gids if g in solution.flows_mw or g in flows_before
        )
        if abs(delta) > 1.0:
            corridor_delta[f"{a}|{b}"] = round(delta, 1)

    for corridor, delta in sorted(corridor_delta.items(), key=lambda kv: -abs(kv[1]))[:12]:
        actions.append({
            "kind": "reroute",
            "corridor": corridor,
            "substation": None,
            "delta_mw": delta,
            "detail": f"{'increased' if delta > 0 else 'reduced'} flow by {abs(delta):.1f} MW",
        })

    for gid, flow in solution.flows_mw.items():
        was, now = abs(flows_before.get(gid, 0.0)) > 1.0, abs(flow) > 1.0
        if was == now:
            continue
        line = state.network.lines.loc[state.network.lines["gid"] == gid].iloc[0]
        actions.append({
            "kind": "switch_in" if now else "switch_out",
            "corridor": f"{line['from_sub']}|{line['to_sub']}",
            "substation": None,
            "delta_mw": round(abs(flow) - abs(flows_before.get(gid, 0.0)), 1),
            "detail": f"{'brought into' if now else 'took out of'} service: {line['line_name']}",
        })

    for name, shed in solution.shed_mw.items():
        actions.append({
            "kind": "shed",
            "corridor": None,
            "substation": name,
            "delta_mw": -round(shed, 1),
            "detail": f"shed {shed:.1f} MW at {name} (no feasible route to serve it)",
        })

    return actions


def _result(state, trigger, *, accepted, actions, rejected, util_before, risk_before,
            served_before, flows_before, solution, validation, message) -> dict:
    util_after = state.corridor_utilisation()
    risk_after = state.risk()

    affected_corridors = sorted({
        k for k in util_after
        if abs(util_after[k]["loading"] - util_before.get(k, {}).get("loading", 0.0)) > 0.005
    })
    shed_now = state.shed_by_substation()
    affected_subs = sorted(set(shed_now) | {
        n for n in state.served_mw
        if abs(state.served_mw[n] - served_before.get(n, 0.0)) > 0.5
    })

    mw_restored = round(
        sum(state.served_mw.values()) - sum(served_before.values()), 1
    )
    mw_rerouted = round(sum(
        abs(state.flows_mw.get(g, 0.0) - flows_before.get(g, 0.0))
        for g in set(state.flows_mw) | set(flows_before)
    ), 1)

    return {
        "tick": state.tick,
        "trigger": trigger,
        "accepted": accepted,
        "actions": actions,
        "rejected_alternatives": rejected,
        "affected_corridors": affected_corridors,
        "affected_substations": affected_subs,
        "utilisation_before": {k: v["loading"] for k, v in util_before.items()},
        "utilisation_after": {k: v["loading"] for k, v in util_after.items()},
        "mw_restored": mw_restored,
        "mw_rerouted": mw_rerouted,
        # DC power flow is lossless by construction, so the reported loss is the
        # LP's resistance-weighted estimate.
        "estimated_loss_mw": solution.estimated_loss_mw if solution else 0.0,
        "risk_before": risk_before,
        "risk_after": risk_after,
        "power_flow_validated": bool(validation and validation.converged and validation.ok),
        "message": message,
    }


def _line_name(state: GridState, gid: int) -> str:
    return str(state.network.lines.loc[state.network.lines["gid"] == gid].iloc[0]["line_name"])


def _corridor_of(state: GridState, gid: int) -> str:
    row = state.network.lines.loc[state.network.lines["gid"] == gid].iloc[0]
    return f"{row['from_sub']}|{row['to_sub']}"


def max_servable_scale(state: GridState, lo: float = 0.4, hi: float = 1.0, iters: int = 4) -> float:
    """Largest fraction of the snapshot's nodal demand the network can actually serve.

    The snapshot's line flows satisfy conservation but not impedance-driven
    physics (it is generated data), so under a real DC model the network cannot
    deliver the full nominal demand to every substation. Bisecting for the
    servable level gives the demo an honest healthy starting point instead of
    one that is already shedding load before anything happens.
    """
    probe = GridState.from_baseline(state.network)
    for _ in range(iters):
        mid = (lo + hi) / 2
        demand_mod.apply_scale(probe, mid)
        probe.switched_out = set()
        solution, _ = optimize_with_switching(probe, max_switches=3)
        shed = sum(solution.shed_mw.values()) if solution.feasible else float("inf")
        if shed <= 1.0:
            lo = mid
        else:
            hi = mid
    return lo


DEMO_START = pd.Timestamp("2025-06-15 06:00:00")


def bootstrap_state(headroom: float = 0.80, network=None, as_of: pd.Timestamp | None = None) -> GridState:
    """Build the starting state, calibrated to a demand level the grid can serve.

    The citywide forecast at `as_of` becomes the reference point: later ticks
    move nodal demand by the *ratio* between their forecast and this one, so a
    5-minute step moves the grid a few percent rather than jumping it to a
    completely different operating point.
    """
    state = GridState.from_baseline(network)
    scale = max_servable_scale(state)
    demand_mod.apply_scale(state, scale * headroom)
    solution, opened = optimize_with_switching(state)
    state.switched_out.update(opened)
    state.flows_mw = dict(solution.flows_mw)
    state.injections_mw = dict(solution.injections_mw)
    state.served_mw = dict(solution.served_mw)
    state.baseline_scale = state.demand_scale
    state.citywide_ref_mw = demand_mod.citywide_forecast(as_of or DEMO_START, horizon_h=2)
    return state


# --- Case 1: predictive balancing -------------------------------------------

def tick(state: GridState, as_of: pd.Timestamp, horizon_h: int = 2) -> dict:
    """Advance one forecast step and rebalance before the demand arrives."""
    citywide = demand_mod.citywide_forecast(as_of, horizon_h=horizon_h)
    demand_mod.apply_citywide_mw(state, citywide)
    trigger = {
        "type": "forecast_tick",
        "as_of": str(as_of),
        "horizon_h": horizon_h,
        "citywide_forecast_mw": round(citywide, 1),
        "demand_scale": round(state.demand_scale, 4),
    }
    return run_intervention(state, trigger)


# --- Case 2: judge-driven stress, and AURA's separate, manual response -----

def _apply_disturbance_mutation(
    state: GridState,
    kind: str,
    *,
    temperature_delta_c: float | None = None,
    demand_multiplier: float | None = None,
    corridor: str | None = None,
    line_gids: list[int] | None = None,
    as_of: pd.Timestamp | None = None,
) -> dict:
    """Mutate demand/failures only -- no redispatch, no optimizer. Returns the trigger."""
    trigger: dict = {"type": kind}

    if kind == "temperature":
        delta = temperature_delta_c or 0.0
        base_ts = as_of or pd.Timestamp("2025-06-15 14:00:00")
        citywide = demand_mod.citywide_forecast(base_ts, 2, temperature_delta_c=delta)
        demand_mod.apply_citywide_mw(state, citywide)
        state.temperature_c = (state.temperature_c or 0.0) + delta
        trigger.update({
            "temperature_delta_c": delta,
            "citywide_forecast_mw": round(citywide, 1),
            "demand_scale": round(state.demand_scale, 4),
        })

    elif kind == "demand":
        mult = demand_multiplier or 1.0
        demand_mod.apply_scale(state, state.demand_scale * mult)
        trigger.update({
            "demand_multiplier": mult,
            "demand_scale": round(state.demand_scale, 4),
        })

    elif kind == "line_failure":
        gids: list[int] = list(line_gids or [])
        if corridor:
            a, b = corridor.split("|")
            key = tuple(sorted((a, b)))
            for gid in state.network.corridors.get(key, []):
                row = state.network.lines.loc[state.network.lines["gid"] == gid].iloc[0]
                if row["status"] == "Commissioned":
                    gids.append(int(gid))
        state.failed_lines.update(gids)
        for gid in gids:
            state.flows_mw.pop(gid, None)
        trigger.update({"corridor": corridor, "failed_gids": sorted(gids), "count": len(gids)})

    else:
        raise ValueError(f"unknown disturbance kind: {kind}")

    return trigger


def apply_stress(
    state: GridState,
    kind: str,
    *,
    temperature_delta_c: float | None = None,
    demand_multiplier: float | None = None,
    corridor: str | None = None,
    line_gids: list[int] | None = None,
    as_of: pd.Timestamp | None = None,
) -> dict:
    """Apply a judge's stress event and show its raw, unmanaged consequence.

    Deliberately does NOT call the optimizer. This is the "before AURA" half
    of the demo: real demand/failure mutation, then a real PTDF flow
    recalculation (`naive_dispatch`) showing what the network does with no
    smart redispatch -- including genuine overloads, if the stress is severe
    enough to cause them. AURA only responds when `optimize_now` is called
    separately.
    """
    util_before = state.corridor_utilisation()
    risk_before = state.risk()
    served_before = dict(state.served_mw)
    flows_before = dict(state.flows_mw)

    trigger = _apply_disturbance_mutation(
        state, kind,
        temperature_delta_c=temperature_delta_c, demand_multiplier=demand_multiplier,
        corridor=corridor, line_gids=line_gids, as_of=as_of,
    )

    solution = naive_dispatch(state)
    state.flows_mw = dict(solution.flows_mw)
    state.injections_mw = dict(solution.injections_mw)
    state.served_mw = dict(solution.served_mw)
    state.tick += 1

    return _result(
        state, trigger, accepted=True, actions=[], rejected=[],
        util_before=util_before, risk_before=risk_before,
        served_before=served_before, flows_before=flows_before,
        solution=solution, validation=None,
        message="Stress applied; this is the unmanaged grid response. AURA has not intervened.",
    )


def disturbance(state: GridState, kind: str, **kwargs) -> dict:
    """Backwards-compatible alias for `apply_stress` -- kept as the public
    entry point `/api/disturbance` calls; the naive-dispatch behavior above
    *is* the intended semantics, not a fallback."""
    return apply_stress(state, kind, **kwargs)


def optimize_now(state: GridState) -> dict:
    """The judge's explicit [ AURA REBALANCE ] action: run AURA's real
    optimizer against whatever state currently exists, commit the result."""
    return run_intervention(state, trigger={"type": "manual_optimize"})


def preview_intervention(state: GridState) -> dict:
    """What AURA *would* do right now, without touching the shared state.

    Runs the real LP and real pandapower validation (see `run_intervention`'s
    `commit` parameter) so the optimizer panel can honestly show "AURA can
    redistribute N MW" before the judge clicks anything.
    """
    return run_intervention(state, trigger={"type": "preview"}, commit=False)
