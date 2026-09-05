"""Mutable grid state, carried across ticks and disturbances.

State is never reset between interventions: a judge failing a line and then
raising demand sees both effects stacked, exactly as a real operator would.
"""
from dataclasses import dataclass, field

from grid.data import GridNetwork, load_network

SOFT_ALARM_THRESHOLD = 0.90  # corridor loading fraction that counts as "at risk"
OVERLOAD_THRESHOLD = 1.00    # corridor loading fraction that counts as genuinely overloaded

# Finer-grained bands for the map's colour scale and legend -- consistent with
# (but more granular than) SOFT_ALARM_THRESHOLD/OVERLOAD_THRESHOLD above, which
# drive the coarser corridors_at_risk/overloaded_corridors counts. One table,
# served to the frontend via /api/config, so the map/legend/alerts panel never
# hardcode a cutoff that could drift from what the backend actually means by it.
LOADING_BANDS = [
    {"key": "safe", "label": "Safe", "max": 0.70, "color": "#2dd4a8"},
    {"key": "elevated", "label": "Elevated", "max": 0.85, "color": "#eab308"},
    {"key": "high", "label": "High", "max": 1.00, "color": "#f97316"},
    {"key": "overloaded", "label": "Overloaded", "max": 1.10, "color": "#ef4444"},
    {"key": "critical", "label": "Critical", "max": None, "color": "#ff1a4d"},
]


@dataclass
class GridState:
    network: GridNetwork
    demand_mw: dict[str, float]          # substation -> current projected demand
    supply_cap_mw: dict[str, float]      # substation -> max injectable power
    failed_lines: set[int] = field(default_factory=set)   # gids failed by a disturbance
    switched_out: set[int] = field(default_factory=set)   # gids the optimizer opened for congestion relief
    flows_mw: dict[int, float] = field(default_factory=dict)  # gid -> signed flow (from->to +)
    injections_mw: dict[str, float] = field(default_factory=dict)  # substation -> generation
    served_mw: dict[str, float] = field(default_factory=dict)  # substation -> demand actually served
    demand_scale: float = 1.0            # cumulative multiplier applied to baseline demand
    temperature_c: float | None = None   # last temperature used for a model-driven demand update
    baseline_scale: float = 1.0          # demand scale the demo starts healthy at
    citywide_ref_mw: float = 0.0         # citywide forecast MW matching baseline_scale
    tick: int = 0

    @classmethod
    def from_baseline(cls, network: GridNetwork | None = None) -> "GridState":
        net = network or load_network()
        demand = {n: s.baseline_demand_mw for n, s in net.substations.items()}
        # Sources can inject above their snapshot output; headroom lets the
        # optimizer redispatch rather than shed the moment demand rises.
        supply = {n: s.baseline_supply_mw * 1.35 for n, s in net.substations.items()}
        state = cls(network=net, demand_mw=demand, supply_cap_mw=supply)
        state.served_mw = dict(demand)
        state.injections_mw = {n: s.baseline_supply_mw for n, s in net.substations.items()}
        state.flows_mw = {
            int(r.gid): float(r.current_load_mw)
            for r in net.commissioned.itertuples()
        }
        return state

    def available_lines(self):
        """Commissioned lines that are neither failed nor switched out."""
        for row in self.network.commissioned.itertuples():
            gid = int(row.gid)
            if gid not in self.failed_lines and gid not in self.switched_out:
                yield row

    def line_capacity(self, gid: int) -> float:
        if gid in self.failed_lines or gid in self.switched_out:
            return 0.0
        row = self.network.lines.loc[self.network.lines["gid"] == gid]
        return float(row["capacity_mw"].iloc[0])

    def corridor_utilisation(self) -> dict[str, dict]:
        """Aggregate parallel circuits into one entry per substation-pair corridor."""
        out: dict[str, dict] = {}
        for (a, b), gids in self.network.corridors.items():
            cap = 0.0
            flow = 0.0
            net_signed = 0.0  # positive = net flow from a to b
            live = 0
            worst = 0.0
            kv = 0.0
            for gid in gids:
                row = self.network.lines.loc[self.network.lines["gid"] == gid].iloc[0]
                if row["status"] != "Commissioned":
                    continue
                if gid in self.failed_lines or gid in self.switched_out:
                    continue
                line_cap = float(row["capacity_mw"])
                raw_flow = self.flows_mw.get(gid, 0.0)  # + = row.from_sub -> row.to_sub
                line_flow = abs(raw_flow)
                cap += line_cap
                flow += line_flow
                live += 1
                if line_cap > 0:
                    worst = max(worst, line_flow / line_cap)
                kv = max(kv, float(row["voltage_kv"]))
                # corridor's (a, b) is alphabetical, not necessarily this row's
                # own from/to order, so flip the sign when the row runs b -> a
                net_signed += raw_flow if row["from_sub"] == a else -raw_flow
            key = f"{a}|{b}"
            out[key] = {
                "from": a,
                "to": b,
                "capacity_mw": round(cap, 1),
                "flow_mw": round(flow, 1),
                "loading": round(flow / cap, 4) if cap > 0 else 0.0,
                # The binding constraint is always an individual circuit, never
                # the corridor average -- a saturated 250 MW line inside a 5 GW
                # corridor is what actually strands load.
                "max_circuit_loading": round(worst, 4),
                "flow_direction": 1 if net_signed >= 0 else -1,
                "voltage_kv": kv,
                "circuits_live": live,
                "circuits_total": len(gids),
            }
        return out

    def risk(self) -> dict:
        util = self.corridor_utilisation()
        loadings = [c["loading"] for c in util.values() if c["capacity_mw"] > 0]
        circuit_loadings = [c["max_circuit_loading"] for c in util.values() if c["capacity_mw"] > 0]
        at_risk = [k for k, c in util.items() if c["max_circuit_loading"] >= SOFT_ALARM_THRESHOLD]
        overloaded = [k for k, c in util.items() if c["max_circuit_loading"] >= OVERLOAD_THRESHOLD]
        at_limit = sum(
            1 for row in self.available_lines()
            if float(row.capacity_mw) > 0
            and abs(self.flows_mw.get(int(row.gid), 0.0)) >= 0.99 * float(row.capacity_mw)
        )
        total_demand = sum(self.demand_mw.values())
        total_served = sum(self.served_mw.values())

        # a substation is "stressed" if it sits on either end of an at-risk
        # or overloaded corridor -- derived from existing topology, no new data
        stressed_keys = set(at_risk)
        stressed_subs = {
            sub for key in stressed_keys for sub in key.split("|")
        }

        return {
            "max_corridor_loading": round(max(loadings), 4) if loadings else 0.0,
            "max_circuit_loading": round(max(circuit_loadings), 4) if circuit_loadings else 0.0,
            "circuits_at_limit": at_limit,
            "corridors_at_risk": len(at_risk),
            "at_risk_corridors": sorted(at_risk),
            "overloaded_corridors": len(overloaded),
            "overloaded_corridor_keys": sorted(overloaded),
            "stressed_substations": len(stressed_subs),
            "stressed_substation_names": sorted(stressed_subs),
            "total_demand_mw": round(total_demand, 1),
            "total_served_mw": round(total_served, 1),
            "total_shed_mw": round(total_demand - total_served, 1),
            "failed_lines": len(self.failed_lines),
            "switched_out_lines": len(self.switched_out),
        }

    def shed_by_substation(self) -> dict[str, float]:
        return {
            name: round(self.demand_mw[name] - self.served_mw.get(name, 0.0), 1)
            for name in self.demand_mw
            if self.demand_mw[name] - self.served_mw.get(name, 0.0) > 0.1
        }
