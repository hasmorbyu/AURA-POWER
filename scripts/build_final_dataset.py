"""Build the final LightGBM-ready dataset: demand + weather + engineered event features.

Collapses the sparse one-hot event flags in delhi_events.csv into a compact,
decay-aware representation (event_type / event_name / event_intensity /
hours_to_event / hours_since_event / event_active / expected_attendance) and
merges it with the already-aligned demand+weather data.
"""
import numpy as np
import pandas as pd

LOOKAHEAD_HOURS = 48
LOOKBACK_HOURS = 24

PRIORITY = {
    "major_event": 0,
    "concert": 1,
    "match": 2,
    "election": 3,
    "festival": 4,
    "holiday": 5,
    "school_break": 6,
}

# --- confidently-known name lookups (date -> name, None = deliberately unresolved) ---

HOLIDAY_NAMES = {
    "2023-04-04": "Mahavir Jayanti", "2023-04-07": "Good Friday",
    "2023-04-22": "Id-ul-Fitr", "2023-05-05": "Buddha Purnima",
    "2023-06-29": "Id-ul-Zuha (Bakrid)", "2023-07-29": "Muharram",
    "2023-08-15": "Independence Day", "2023-09-07": "Janmashtami",
    "2023-09-28": "Milad-un-Nabi / Id-e-Milad", "2023-10-02": "Gandhi Jayanti",
    "2023-10-24": "Dussehra", "2023-11-12": "Diwali",
    "2023-11-27": "Guru Nanak Jayanti", "2023-12-25": "Christmas Day",
    "2024-01-26": "Republic Day", "2024-03-25": "Holi",
    "2024-03-29": "Good Friday", "2024-04-11": "Id-ul-Fitr",
    "2024-04-17": "Ram Navami", "2024-04-21": "Mahavir Jayanti",
    "2024-05-23": "Buddha Purnima", "2024-06-17": "Id-ul-Zuha (Bakrid)",
    "2024-07-17": "Muharram", "2024-08-15": "Independence Day",
    "2024-08-26": "Janmashtami", "2024-09-16": "Milad-un-Nabi / Id-e-Milad",
    "2024-10-02": "Gandhi Jayanti", "2024-10-12": "Dussehra",
    "2024-10-17": None,  # unresolved
    "2024-10-31": "Diwali", "2024-11-15": "Guru Nanak Jayanti",
    "2024-12-25": "Christmas Day",
    "2025-01-26": "Republic Day", "2025-02-26": "Maha Shivratri",
    "2025-03-14": "Holi", "2025-03-31": "Id-ul-Fitr",
    "2025-04-10": "Mahavir Jayanti", "2025-04-18": "Good Friday",
    "2025-05-12": "Buddha Purnima", "2025-06-07": "Id-ul-Zuha (Bakrid)",
    "2025-07-06": "Muharram", "2025-08-15": "Independence Day",
    "2025-08-16": "Janmashtami", "2025-09-05": "Milad-un-Nabi / Id-e-Milad",
    "2025-10-02": "Gandhi Jayanti", "2025-10-07": None,  # unresolved
    "2025-10-20": "Diwali", "2025-11-05": "Guru Nanak Jayanti",
    "2025-12-25": "Christmas Day",
}

FESTIVAL_NAMES = {
    "2023-04-14": None,  # unresolved: Ambedkar Jayanti vs Baisakhi
    "2023-04-22": "Id-ul-Fitr", "2023-10-24": "Dussehra",
    "2023-11-12": "Diwali", "2023-11-13": "Govardhan Puja",
    "2023-11-15": "Bhai Dooj", "2023-11-27": "Guru Nanak Jayanti",
    "2023-12-25": "Christmas",
    "2024-04-09": "Chaitra Navratri / Ugadi", "2024-04-17": "Ram Navami",
    "2024-10-31": "Diwali", "2024-11-01": "Govardhan Puja",
    "2024-11-02": "Bhai Dooj", "2024-11-15": "Guru Nanak Jayanti",
    "2025-03-13": "Holika Dahan", "2025-03-14": "Holi",
    "2025-10-20": "Diwali", "2025-10-21": "Govardhan Puja",
    "2025-10-22": "Bhai Dooj", "2025-11-05": "Guru Nanak Jayanti",
}

ELECTION_NAMES = {
    "2024-05-25": "Lok Sabha Election 2024 - Delhi Polling (Phase 6)",
    "2024-06-04": "Lok Sabha Election 2024 - Counting Day",
    "2025-02-05": "Delhi Assembly Election 2025 - Polling",
    "2025-02-08": "Delhi Assembly Election 2025 - Result Day",
}

MAJOR_EVENT_NAMES = {
    "2023-09-09": "G20 Summit New Delhi 2023",
    "2023-09-10": "G20 Summit New Delhi 2023",
    "2024-01-26": "Republic Day Parade",
    "2025-01-26": "Republic Day Parade",
}

TIER1 = {"Diwali", "Holi", "Independence Day", "Republic Day"}
TIER2 = {"Dussehra", "Id-ul-Fitr", "Id-ul-Zuha (Bakrid)", "Gandhi Jayanti",
         "Guru Nanak Jayanti", "Christmas Day", "Christmas"}


def name_tier_intensity(name):
    if name is None:
        return 0.4
    if name in TIER1:
        return 1.0
    if name in TIER2:
        return 0.7
    return 0.5


def instances_from_dated_flag(ev, flag_col, names, event_type):
    """One instance per contiguous run of same-named flagged dates (00:00-23:55)."""
    dates = sorted(pd.to_datetime(ev.loc[ev[flag_col] == 1, "date"]).dt.date.unique())
    out = []
    i = 0
    while i < len(dates):
        d0 = dates[i]
        name = names.get(str(d0))
        j = i
        while (j + 1 < len(dates)
               and (dates[j + 1] - dates[j]).days == 1
               and names.get(str(dates[j + 1])) == name):
            j += 1
        d1 = dates[j]
        out.append({
            "start": pd.Timestamp(d0), "end": pd.Timestamp(d1) + pd.Timedelta(hours=23, minutes=55),
            "event_type": event_type, "event_name": name,
            "intensity": name_tier_intensity(name),
            "attendance": 0.0,
        })
        i = j + 1
    return out


def instances_from_burst_flag(ev, flag_col, event_type, intensity):
    """One instance per date, using the observed min/max timestamp that day (event bursts, not full days)."""
    sub = ev.loc[ev[flag_col] == 1, ["timestamp", "date"]]
    out = []
    for date, grp in sub.groupby("date"):
        out.append({
            "start": grp["timestamp"].min(), "end": grp["timestamp"].max(),
            "event_type": event_type, "event_name": None,
            "intensity": intensity, "attendance": np.nan,
        })
    return out


def instances_from_breaks(ev):
    days = sorted(pd.to_datetime(ev.loc[ev["is_school_break"] == 1, "date"]).dt.date.unique())
    ranges = []
    start = prev = days[0]
    for d in days[1:]:
        if (d - prev).days > 1:
            ranges.append((start, prev))
            start = d
        prev = d
    ranges.append((start, prev))

    out = []
    for d0, d1 in ranges:
        if d0.month == 1:
            label = "Winter Vacation"
        elif d0.month == 5:
            label = "Summer Vacation"
        else:
            label = "Autumn Break (Dussehra)"
        out.append({
            "start": pd.Timestamp(d0), "end": pd.Timestamp(d1) + pd.Timedelta(hours=23, minutes=55),
            "event_type": "school_break", "event_name": label,
            "intensity": 0.3, "attendance": 0.0,
        })
    return out


def main():
    ev = pd.read_csv("data/delhi_events.csv", parse_dates=["timestamp"], low_memory=False)
    dw = pd.read_csv("data/demand_weather.csv", parse_dates=["timestamp"])
    assert len(ev) == len(dw) == 293184
    assert ev["timestamp"].equals(dw["timestamp"])

    instances = []
    instances += instances_from_dated_flag(ev, "is_holiday", HOLIDAY_NAMES, "holiday")
    instances += instances_from_dated_flag(ev, "is_festival", FESTIVAL_NAMES, "festival")
    instances += instances_from_dated_flag(ev, "is_election", ELECTION_NAMES, "election")
    instances += instances_from_dated_flag(ev, "is_major_event", MAJOR_EVENT_NAMES, "major_event")
    instances += instances_from_burst_flag(ev, "is_match", "match", intensity=0.5)
    instances += instances_from_burst_flag(ev, "is_concert", "concert", intensity=0.7)
    instances += instances_from_breaks(ev)

    n = len(ev)
    ts = ev["timestamp"].to_numpy()

    best_state = np.full(n, 3, dtype=np.int8)      # 0=active,1=upcoming,2=recent,3=none
    best_priority = np.full(n, 99, dtype=np.int8)
    best_distance = np.full(n, np.inf)
    event_type = np.array([None] * n, dtype=object)
    event_name = np.array([None] * n, dtype=object)
    event_intensity = np.zeros(n)
    expected_attendance = np.zeros(n)

    lookahead = np.timedelta64(LOOKAHEAD_HOURS, "h")
    lookback = np.timedelta64(LOOKBACK_HOURS, "h")

    for inst in instances:
        start = np.datetime64(inst["start"])
        end = np.datetime64(inst["end"])
        prio = PRIORITY[inst["event_type"]]

        active = (ts >= start) & (ts <= end)
        upcoming = (ts >= start - lookahead) & (ts < start)
        recent = (ts > end) & (ts <= end + lookback)

        for mask, state, dist in (
            (active, 0, np.zeros(n)),
            (upcoming, 1, (start - ts) / np.timedelta64(1, "h")),
            (recent, 2, (ts - end) / np.timedelta64(1, "h")),
        ):
            if not mask.any():
                continue
            better = mask & (
                (state < best_state)
                | ((state == best_state) & (prio < best_priority))
                | ((state == best_state) & (prio == best_priority) & (dist < best_distance))
            )
            if better.any():
                best_state[better] = state
                best_priority[better] = prio
                best_distance[better] = dist[better] if hasattr(dist, "__len__") else dist
                event_type[better] = inst["event_type"]
                event_name[better] = inst["event_name"]
                event_intensity[better] = inst["intensity"]
                expected_attendance[better] = inst["attendance"]

    event_active = (best_state == 0).astype(int)
    hours_to_event = np.where(best_state == 1, best_distance, np.where(best_state == 0, 0.0, np.nan))
    hours_since_event = np.where(best_state == 2, best_distance, np.where(best_state == 0, 0.0, np.nan))
    no_event = best_state == 3
    event_type[no_event] = None
    event_name[no_event] = None
    event_intensity[no_event] = 0.0
    expected_attendance[no_event] = 0.0

    features = pd.DataFrame({
        "timestamp": ev["timestamp"],
        "date": ev["date"],
        "day_of_week": ev["day_of_week"],
        "is_weekend": ev["is_weekend"],
        "is_holiday": ev["is_holiday"],
        "is_school_break": ev["is_school_break"],
        "event_type": event_type,
        "event_name": event_name,
        "event_intensity": event_intensity,
        "hours_to_event": hours_to_event,
        "hours_since_event": hours_since_event,
        "event_active": event_active,
        "expected_attendance": expected_attendance,
    })

    final = dw.merge(features, on="timestamp", how="inner")
    assert len(final) == 293184

    final.to_csv("data/final_dataset.csv", index=False)

    print(f"rows: {len(final)}, columns: {len(final.columns)}")
    print()
    print("event_type distribution:")
    print(final["event_type"].fillna("none").value_counts())
    print()
    unresolved = final[(final["event_active"] == 1)
                        & final["event_type"].isin(["holiday", "festival"])
                        & final["event_name"].isna()]
    print(f"rows on unresolved (unnamed) holiday/festival dates: {unresolved['date'].nunique()} distinct dates")
    print(sorted(unresolved["date"].unique()))
    print()
    print(f"rows with match/concert as winning event_type: "
          f"{(final['event_type'].isin(['match', 'concert'])).sum()} "
          f"(event_name intentionally null for these, expected_attendance intentionally null for these)")
    print("wrote data/final_dataset.csv")


if __name__ == "__main__":
    main()
