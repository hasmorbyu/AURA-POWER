"""End-to-end walkthrough of both scenarios, no server needed.

Run: python -m grid.demo
"""
import json

import pandas as pd

from grid.scenarios import bootstrap_state, disturbance, tick

RULE = "=" * 78


def show(result: dict, label: str) -> None:
    ra, rb = result["risk_after"], result["risk_before"]
    print(f"\n--- {label} ---")
    print(f"trigger        : {json.dumps(result['trigger'])}")
    print(f"accepted       : {result['accepted']}  |  power-flow validated: {result['power_flow_validated']}")
    print(f"message        : {result['message']}")
    print(
        f"circuit loading: {rb['max_circuit_loading']:.0%} -> {ra['max_circuit_loading']:.0%}"
        f"   (corridors at risk {rb['corridors_at_risk']} -> {ra['corridors_at_risk']},"
        f" circuits at limit {rb['circuits_at_limit']} -> {ra['circuits_at_limit']})"
    )
    print(
        f"load           : demand {ra['total_demand_mw']:.0f} MW,"
        f" served {ra['total_served_mw']:.0f} MW, shed {ra['total_shed_mw']:.0f} MW"
    )
    print(f"MW restored    : {result['mw_restored']:+.1f}   |  MW rerouted: {result['mw_rerouted']:.1f}")
    print(f"est. losses    : {result['estimated_loss_mw']:.1f} MW")

    if result["rejected_alternatives"]:
        print("rejected alternatives (power flow refused these):")
        for r in result["rejected_alternatives"]:
            assets = ", ".join(
                f"{a['corridor']} @ {a['loading_pct']:.0f}%" for a in r["violating_assets"][:3]
            )
            print(f"   attempt {r['attempt']}: {r['reason']}" + (f"  [{assets}]" if assets else ""))
    else:
        print("rejected alternatives: none (first dispatch validated)")

    if result["actions"]:
        print("selected intervention:")
        for a in result["actions"][:6]:
            where = a["corridor"] or a["substation"] or "-"
            print(f"   [{a['kind']:<10}] {where:<28} {a['delta_mw']:>+9.1f} MW  {a['detail']}")
        if len(result["actions"]) > 6:
            print(f"   ... {len(result['actions']) - 6} more actions")

    shed_subs = [s for s in result["affected_substations"]]
    if ra["total_shed_mw"] > 0.1:
        print(f"LOAD SHED at   : {', '.join(shed_subs[:8]) or 'n/a'}")


def main() -> None:
    print(RULE)
    print("AURA grid optimizer -- calibrating starting state")
    print(RULE)
    state = bootstrap_state()
    r = state.risk()
    print(f"demand scale {state.demand_scale:.3f} (largest the network can physically serve)")
    print(f"demand {r['total_demand_mw']:.0f} MW, served {r['total_served_mw']:.0f} MW, shed {r['total_shed_mw']:.0f} MW")
    print(f"max circuit loading {r['max_circuit_loading']:.0%}, corridors at risk {r['corridors_at_risk']}")

    print()
    print(RULE)
    print("CASE 1 -- AI PREDICTIVE BALANCING (forecast-driven, every tick)")
    print(RULE)
    for hh in ["08:00", "10:00", "12:00"]:
        result = tick(state, pd.Timestamp(f"2025-06-15 {hh}:00"))
        show(result, f"tick @ {hh} (+2h forecast)")

    print()
    print(RULE)
    print("CASE 2 -- JUDGE-DRIVEN GRID FAILURE (state carries over, never resets)")
    print(RULE)

    show(disturbance(state, "temperature", temperature_delta_c=6.0,
                     as_of=pd.Timestamp("2025-06-15 12:00:00")),
         "judge raises temperature +6 C (through the real forecast model)")

    show(disturbance(state, "demand", demand_multiplier=1.10),
         "judge raises demand +10%")

    show(disturbance(state, "line_failure", corridor="Pusa|Lodhi Road"),
         "judge fails the Pusa <-> Lodhi Road corridor")

    show(disturbance(state, "line_failure", corridor="Mundka|Pusa"),
         "judge fails Mundka <-> Pusa as well")

    show(disturbance(state, "line_failure", corridor="Najafgarh|Pusa"),
         "judge fails Najafgarh <-> Pusa -- cascading stress")

    print()
    print(RULE)
    print("FINAL STATE")
    print(RULE)
    r = state.risk()
    print(json.dumps(r, indent=2))
    shed = state.shed_by_substation()
    if shed:
        print("\nsubstations with unserved load:")
        for name, mw in sorted(shed.items(), key=lambda kv: -kv[1]):
            print(f"   {name:<14} {mw:>9.1f} MW")


if __name__ == "__main__":
    main()
