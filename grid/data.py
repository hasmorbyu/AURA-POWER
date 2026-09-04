"""Load the Delhi transmission network from CSV into a NetworkX graph.

Substations are nodes (parsed from line_name, positioned by the lat/lon in
sy_ps_loca). Transmission lines are edges. Only status == "Commissioned" lines
carry load and participate in routing; the rest are future infrastructure that
the digital twin displays but never routes power through.

Per-substation demand is not a column in the source data. It is derived as the
nodal net injection: summing current_load_mw as + at each line's "from"
substation and - at its "to" substation. Across all 20 substations this sums to
~0 (Kirchhoff's current law holds on the snapshot), so negative nodes are net
demand centres and positive nodes are net sources.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
import pandas as pd

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "delhi_power_transmission_lines.csv"

# Real Delhi/NCR coordinates for the 20 substations. The source CSV rounds
# every position to 0.01 degrees (~1.1 km), which snaps sites to a visible grid
# and puts several on identical latitudes -- fine for a schematic, wrong for a
# geospatial twin. These are the actual localities each substation is named for.
#
# DISPLAY ONLY. Line impedances come from `length_km` in the CSV, never from
# these coordinates, so the optimizer, its calibrated baseline, and every
# result are unaffected. The tradeoff is that a corridor's drawn length no
# longer always matches its `length_km`; recomputing lengths from geography
# would silently change the PTDF and re-baseline the whole demo.
REAL_COORDS = {
    "Badarpur":     (28.4926, 77.3026),
    "Bamnoli":      (28.5560, 76.9840),
    "Bawana":       (28.7980, 77.0330),
    "Dwarka":       (28.5921, 77.0460),
    "Lodhi Road":   (28.5885, 77.2270),
    "Mahipalpur":   (28.5450, 77.1265),
    "Mayur Vihar":  (28.6090, 77.2950),
    "Mundka":       (28.6820, 76.9750),
    "Najafgarh":    (28.6090, 76.9800),
    "Narela":       (28.8530, 77.0920),
    "Okhla":        (28.5355, 77.2745),
    "Patparganj":   (28.6280, 77.2900),
    "Punjabi Bagh": (28.6680, 77.1310),
    "Pusa":         (28.6395, 77.1520),
    "Ridge":        (28.6800, 77.2050),
    "Rohini":       (28.7400, 77.0670),
    "Sarita Vihar": (28.5300, 77.2890),
    "Shahdara":     (28.6730, 77.2890),
    "Vasant Kunj":  (28.5200, 77.1590),
    "Wazirabad":    (28.7180, 77.2290),
}

# Approximate AC resistance / reactance per km by conductor family. The source
# CSV has no r/x columns, only conductor and bundle type, so these are standard
# textbook values for these conductor classes -- engineering approximations, not
# measured parameters of the real Delhi network.
CONDUCTOR_OHM_PER_KM = {
    "ACSR Zebra": (0.0674, 0.40),
    "ACSR Moose": (0.0357, 0.38),
    "AAAC": (0.0680, 0.40),
    "AL 59 Zebra": (0.0630, 0.39),
    "ACSR / AAAC / AL59": (0.0550, 0.38),
}
DEFAULT_OHM_PER_KM = (0.0650, 0.40)

# Bundling puts n sub-conductors in parallel per phase: resistance divides by n,
# reactance drops modestly with bundle size.
BUNDLE_SUBCONDUCTORS = {
    "Single": 1,
    "Twin": 2,
    "Twin Moose": 2,
    "Quad": 4,
    "Hexa": 6,
}


@dataclass
class Substation:
    name: str
    lat: float
    lon: float
    net_injection_mw: float = 0.0  # + = net source, - = net demand centre

    @property
    def is_source(self) -> bool:
        return self.net_injection_mw > 0

    @property
    def baseline_demand_mw(self) -> float:
        """Local demand implied by the snapshot (0 for net-source substations)."""
        return max(0.0, -self.net_injection_mw)

    @property
    def baseline_supply_mw(self) -> float:
        return max(0.0, self.net_injection_mw)


@dataclass
class GridNetwork:
    graph: nx.MultiGraph
    substations: dict[str, Substation]
    lines: pd.DataFrame  # one row per physical line, with parsed columns added
    corridors: dict[tuple[str, str], list[int]] = field(default_factory=dict)

    @property
    def commissioned(self) -> pd.DataFrame:
        return self.lines[self.lines["status"] == "Commissioned"]

    def corridor_key(self, a: str, b: str) -> tuple[str, str]:
        return tuple(sorted((a, b)))


def _parse_pair(line_name: str) -> tuple[str, str]:
    m = re.match(r"Mock (.+?) - (.+?) \d+ kV", line_name)
    if not m:
        raise ValueError(f"unparseable line_name: {line_name!r}")
    return m.group(1), m.group(2)


def _parse_coords(sy_ps_loca: str) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in re.findall(r"\(([\d.]+)N, ([\d.]+)E\)", sy_ps_loca)]


def _line_impedance(row) -> tuple[float, float]:
    r_km, x_km = CONDUCTOR_OHM_PER_KM.get(row["condu_type"], DEFAULT_OHM_PER_KM)
    n = BUNDLE_SUBCONDUCTORS.get(row["bundl_type"], 1)
    return r_km / n, x_km / (1 + 0.15 * (n - 1))


def load_network(csv_path: Path = CSV_PATH) -> GridNetwork:
    df = pd.read_csv(csv_path)

    pairs = df["line_name"].apply(_parse_pair)
    df["from_sub"] = [p[0] for p in pairs]
    df["to_sub"] = [p[1] for p in pairs]
    df["voltage_kv"] = df["voltage"].str.replace(" kV", "", regex=False).astype(float)
    df["in_service"] = df["status"] == "Commissioned"
    df[["r_ohm_per_km", "x_ohm_per_km"]] = df.apply(
        lambda r: pd.Series(_line_impedance(r)), axis=1
    )

    substations: dict[str, Substation] = {}
    for _, row in df.iterrows():
        coords = _parse_coords(row["sy_ps_loca"])
        for name, (lat, lon) in zip((row["from_sub"], row["to_sub"]), coords):
            if name not in substations:
                lat, lon = REAL_COORDS.get(name, (lat, lon))
                substations[name] = Substation(name=name, lat=lat, lon=lon)

    for _, row in df[df["in_service"]].iterrows():
        substations[row["from_sub"]].net_injection_mw += row["current_load_mw"]
        substations[row["to_sub"]].net_injection_mw -= row["current_load_mw"]

    graph = nx.MultiGraph()
    for name, sub in substations.items():
        graph.add_node(name, lat=sub.lat, lon=sub.lon, net_injection_mw=sub.net_injection_mw)

    corridors: dict[tuple[str, str], list[int]] = {}
    for idx, row in df.iterrows():
        graph.add_edge(
            row["from_sub"], row["to_sub"], key=int(row["gid"]),
            gid=int(row["gid"]),
            capacity_mw=float(row["capacity_mw"]),
            current_load_mw=float(row["current_load_mw"]),
            voltage_kv=float(row["voltage_kv"]),
            length_km=float(row["length_km"]),
            r_ohm_per_km=float(row["r_ohm_per_km"]),
            x_ohm_per_km=float(row["x_ohm_per_km"]),
            status=row["status"],
            in_service=bool(row["in_service"]),
            name=row["line_name"],
        )
        corridors.setdefault(tuple(sorted((row["from_sub"], row["to_sub"]))), []).append(int(row["gid"]))

    return GridNetwork(graph=graph, substations=substations, lines=df, corridors=corridors)


if __name__ == "__main__":
    net = load_network()
    print(f"substations: {len(net.substations)}")
    print(f"lines: {len(net.lines)} ({len(net.commissioned)} commissioned)")
    print(f"corridors: {len(net.corridors)}")
    print()
    print("net injection by substation (MW):")
    for s in sorted(net.substations.values(), key=lambda s: s.net_injection_mw):
        role = "source" if s.is_source else "demand"
        print(f"  {s.name:<14} {s.net_injection_mw:>10.1f}  {role}")
    total_demand = sum(s.baseline_demand_mw for s in net.substations.values())
    total_supply = sum(s.baseline_supply_mw for s in net.substations.values())
    print(f"\ntotal baseline demand: {total_demand:.1f} MW, supply: {total_supply:.1f} MW")
