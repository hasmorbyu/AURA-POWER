"""Electrical model: one topology definition, two consumers.

`build_topology` is the single source of truth for the network's electrical
structure -- buses per (substation, voltage level), transmission lines between
same-voltage buses, and transformers tying voltage levels together inside a
substation. Both the optimizer (via PTDF) and the validator (via pandapower)
are built from it, so the LP predicts the same flows the power flow computes.
That matters: real power splits across parallel paths by impedance, so the
optimizer must decide *injections* and let physics place the flows.

DC power flow (not AC) is deliberate: the source data has no reactive power or
voltage set-points, DC is linear and always converges on a connected network,
and it is what operators use for congestion screening in this timeframe.
"""
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandapower as pp

from grid.state import GridState

if TYPE_CHECKING:  # avoids a circular import with grid.core
    from grid.core import Solution

OVERLOAD_LIMIT_PCT = 100.0
MVA_BASE = 100.0
TRAFO_SN_MVA = 8000.0
TRAFO_VK_PERCENT = 12.0


@dataclass
class Topology:
    buses: list[tuple[str, float]]                  # (substation, kv)
    bus_idx: dict[tuple[str, float], int]
    branches: list[dict]                            # line and transformer branches
    load_bus: dict[str, int]                        # substation -> bus index for its load
    gen_bus: dict[str, int]                         # substation -> bus index for its generation
    line_branch_rows: list[int] = field(default_factory=list)  # indices of line (not trafo) branches


def build_topology(state: GridState) -> Topology:
    sub_levels: dict[str, set[float]] = {}
    for row in state.available_lines():
        sub_levels.setdefault(row.from_sub, set()).add(float(row.voltage_kv))
        sub_levels.setdefault(row.to_sub, set()).add(float(row.voltage_kv))

    buses: list[tuple[str, float]] = []
    for sub in sorted(sub_levels):
        for kv in sorted(sub_levels[sub]):
            buses.append((sub, kv))
    bus_idx = {b: i for i, b in enumerate(buses)}

    branches: list[dict] = []
    line_rows: list[int] = []
    for row in state.available_lines():
        kv = float(row.voltage_kv)
        x_ohm = max(float(row.x_ohm_per_km) * float(row.length_km), 1e-4)
        z_base = kv ** 2 / MVA_BASE
        line_rows.append(len(branches))
        branches.append({
            "kind": "line",
            "gid": int(row.gid),
            "from": bus_idx[(row.from_sub, kv)],
            "to": bus_idx[(row.to_sub, kv)],
            "x_pu": x_ohm / z_base,
            "capacity_mw": float(row.capacity_mw),
            "kv": kv,
            "length_km": float(row.length_km),
            "r_ohm_per_km": float(row.r_ohm_per_km),
            "x_ohm_per_km": float(row.x_ohm_per_km),
            "name": str(row.line_name),
            "from_sub": row.from_sub,
            "to_sub": row.to_sub,
        })

    for sub, levels in sub_levels.items():
        ordered = sorted(levels)
        for hv, lv in zip(ordered[1:], ordered[:-1]):
            branches.append({
                "kind": "trafo",
                "gid": None,
                "from": bus_idx[(sub, hv)],
                "to": bus_idx[(sub, lv)],
                "x_pu": TRAFO_VK_PERCENT / 100.0 * (MVA_BASE / TRAFO_SN_MVA),
                "capacity_mw": TRAFO_SN_MVA,
                "hv": hv, "lv": lv, "sub": sub,
            })

    load_bus, gen_bus = {}, {}
    for sub, levels in sub_levels.items():
        load_bus[sub] = bus_idx[(sub, min(levels))]
        gen_bus[sub] = bus_idx[(sub, max(levels))]

    return Topology(buses, bus_idx, branches, load_bus, gen_bus, line_rows)


def compute_ptdf(topo: Topology, slack_bus: int):
    """PTDF[b, n]: flow on branch b from 1 MW injected at bus n, drawn at slack.

    Computed on exactly the topology the validator uses, so the optimizer's
    predicted flows match the power flow's.
    """
    nb, nn = len(topo.branches), len(topo.buses)
    if nb == 0:
        return np.zeros((0, nn))

    incidence = np.zeros((nb, nn))
    susceptance = np.zeros(nb)
    for k, br in enumerate(topo.branches):
        incidence[k, br["from"]] = 1.0
        incidence[k, br["to"]] = -1.0
        susceptance[k] = 1.0 / max(br["x_pu"], 1e-6)

    b_branch = np.diag(susceptance)
    b_bus = incidence.T @ b_branch @ incidence

    keep = [i for i in range(nn) if i != slack_bus]
    b_inv = np.zeros((nn, nn))
    b_inv[np.ix_(keep, keep)] = np.linalg.pinv(b_bus[np.ix_(keep, keep)])

    return b_branch @ incidence @ b_inv


def pick_slack(state: GridState, topo: Topology) -> tuple[str, int]:
    """Largest source substation carries the slack, at its highest voltage bus."""
    sub = max(topo.gen_bus, key=lambda s: state.network.substations[s].net_injection_mw)
    return sub, topo.gen_bus[sub]


@dataclass
class ValidationResult:
    converged: bool
    ok: bool
    line_loading_pct: dict[int, float] = field(default_factory=dict)  # gid -> %
    violations: list[dict] = field(default_factory=list)
    total_loss_mw: float = 0.0
    reason: str = ""


def build_pandapower_net(state: GridState, solution: "Solution", topo: Topology | None = None):
    """Build the pandapower model of the proposed dispatch, from the same topology."""
    topo = topo or build_topology(state)
    net = pp.create_empty_network()

    pp_bus = {}
    for i, (sub, kv) in enumerate(topo.buses):
        pp_bus[i] = pp.create_bus(net, vn_kv=kv, name=f"{sub}@{kv}")

    gid_by_line_index: dict[int, int] = {}
    for br in topo.branches:
        if br["kind"] == "line":
            idx = pp.create_line_from_parameters(
                net,
                from_bus=pp_bus[br["from"]], to_bus=pp_bus[br["to"]],
                length_km=max(br["length_km"], 0.1),
                r_ohm_per_km=br["r_ohm_per_km"], x_ohm_per_km=br["x_ohm_per_km"],
                c_nf_per_km=0.0,
                max_i_ka=br["capacity_mw"] / (math.sqrt(3) * br["kv"]),
                name=br["name"],
            )
            gid_by_line_index[idx] = br["gid"]
        else:
            pp.create_transformer_from_parameters(
                net,
                hv_bus=pp_bus[br["from"]], lv_bus=pp_bus[br["to"]],
                sn_mva=TRAFO_SN_MVA, vn_hv_kv=br["hv"], vn_lv_kv=br["lv"],
                vkr_percent=0.1, vk_percent=TRAFO_VK_PERCENT,
                pfe_kw=0.0, i0_percent=0.0,
                name=f"{br['sub']} {br['hv']}/{br['lv']}",
            )

    for sub, bus in topo.load_bus.items():
        served = solution.served_mw.get(sub, 0.0)
        if served > 0.01:
            pp.create_load(net, bus=pp_bus[bus], p_mw=served, q_mvar=0.0)

    slack_sub, _ = pick_slack(state, topo)
    for sub, bus in topo.gen_bus.items():
        mw = solution.injections_mw.get(sub, 0.0)
        if sub == slack_sub:
            pp.create_ext_grid(net, bus=pp_bus[bus], vm_pu=1.0, name=f"slack:{sub}")
        elif mw > 0.01:
            pp.create_gen(net, bus=pp_bus[bus], p_mw=mw, vm_pu=1.0, name=f"gen:{sub}")

    return net, gid_by_line_index


def validate(state: GridState, solution: "Solution", topo: Topology | None = None) -> ValidationResult:
    """Run DC power flow on the proposed dispatch and report any overloads."""
    try:
        net, gid_by_index = build_pandapower_net(state, solution, topo)
        pp.rundcpp(net, numba=False)
    except Exception as exc:  # pandapower raises on unsolvable/degenerate nets
        return ValidationResult(converged=False, ok=False, reason=f"power flow failed: {exc}")

    if net.res_line.empty:
        return ValidationResult(converged=False, ok=False, reason="no line results")

    loading, violations = {}, []
    for idx, row in net.res_line.iterrows():
        gid = gid_by_index.get(idx)
        if gid is None:
            continue
        cap = state.line_capacity(gid)
        pct = abs(float(row["p_from_mw"])) / cap * 100.0 if cap else 0.0
        loading[gid] = round(pct, 2)
        if pct > OVERLOAD_LIMIT_PCT:
            line_row = state.network.lines.loc[state.network.lines["gid"] == gid].iloc[0]
            violations.append({
                "gid": gid,
                "line": str(line_row["line_name"]),
                "corridor": f"{line_row['from_sub']}|{line_row['to_sub']}",
                "loading_pct": round(pct, 2),
            })

    return ValidationResult(
        converged=True,
        ok=not violations,
        line_loading_pct=loading,
        violations=sorted(violations, key=lambda v: -v["loading_pct"]),
        total_loss_mw=0.0,  # DC power flow is lossless by construction
        reason="" if not violations else f"{len(violations)} line(s) over {OVERLOAD_LIMIT_PCT:.0f}%",
    )
