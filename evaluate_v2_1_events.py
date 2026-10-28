"""Compare V2.1's historical rainfall-response replay with documented event windows.

This is retrospective sensitivity/context analysis only. It does not measure
forecast lead time or establish flood-prediction accuracy.
"""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "processed"
REGISTRY = DATA / "pune_documented_event_registry.csv"
DAILY = DATA / "predictive_v2_1_spatial_historical_daily_replay.csv"
WARD_DAILY = DATA / "predictive_v2_1_spatial_ward_daily_replay.csv"
OUTPUT = DATA / "predictive_v2_1_spatial_event_evaluation.csv"

REQUIRED_DAILY = {
    "date", "mean_observed_rainfall_mm", "max_observed_rainfall_mm",
    "mean_city_average_predictive_index", "city_max_predictive_index",
    "wards_priority_ge_0_35", "wards_high_or_critical_ge_0_60",
    "wards_critical_ge_0_80",
}


def read_dates():
    for path in [REGISTRY, DAILY, WARD_DAILY]:
        if not path.exists():
            raise FileNotFoundError(
                f"Missing required file: {path}\n"
                "Place this script in the repository root and ensure the V2.1 "
                "replay and corrected event registry are in data/processed."
            )
    events = pd.read_csv(REGISTRY)
    daily = pd.read_csv(DAILY, parse_dates=["date"])
    ward_daily = pd.read_csv(WARD_DAILY, parse_dates=["date"])
    missing = REQUIRED_DAILY - set(daily.columns)
    if missing:
        raise ValueError(f"Daily replay lacks columns: {sorted(missing)}")
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce").dt.normalize()
    ward_daily["date"] = pd.to_datetime(ward_daily["date"], errors="coerce").dt.normalize()
    if daily["date"].isna().any() or ward_daily["date"].isna().any():
        raise ValueError("Replay contains unparseable dates.")
    if daily["date"].duplicated().any():
        raise ValueError("Daily replay has duplicate dates.")
    if not {"event_id", "episode_window", "reported_event_date"}.issubset(events.columns):
        raise ValueError("Event registry is missing expected event columns.")
    return events, daily.sort_values("date"), ward_daily


def main():
    events, daily, ward_daily = read_dates()
    daily_by_date = daily.set_index("date", drop=False)
    rows = []

    for event in events.to_dict("records"):
        window = str(event["episode_window"])
        parts = [part.strip() for part in window.split("to")]
        if len(parts) != 2:
            raise ValueError(f"Cannot parse episode_window for {event['event_id']}: {window}")
        start = pd.Timestamp(parts[0]).normalize()
        end = pd.Timestamp(parts[1]).normalize()
        reported = pd.Timestamp(event["reported_event_date"]).normalize()
        episode = daily[daily["date"].between(start, end)]
        before3 = daily[daily["date"].between(start - pd.Timedelta(days=3), start - pd.Timedelta(days=1))]
        prev = daily[daily["date"] == start - pd.Timedelta(days=1)]
        reported_row = daily[daily["date"] == reported]

        if episode.empty:
            raise ValueError(f"No replay dates found for {event['event_id']} window {window}")

        peak_idx = episode["city_max_predictive_index"].idxmax()
        peak = episode.loc[peak_idx]
        mean_peak_idx = episode["mean_city_average_predictive_index"].idxmax()
        mean_peak = episode.loc[mean_peak_idx]

        any_priority_pre3 = int((before3["wards_priority_ge_0_35"] > 0).any()) if not before3.empty else None
        any_high_pre3 = int((before3["wards_high_or_critical_ge_0_60"] > 0).any()) if not before3.empty else None
        any_critical_pre3 = int((before3["wards_critical_ge_0_80"] > 0).any()) if not before3.empty else None

        # Ward count and max/mean risk on the report date, when available.
        rrow = reported_row.iloc[0] if not reported_row.empty else None
        rows.append({
            "event_id": event["event_id"],
            "episode_id": event.get("episode_id"),
            "evidence_class": event.get("evidence_class"),
            "confidence": event.get("confidence"),
            "episode_start": start.date().isoformat(),
            "episode_end": end.date().isoformat(),
            "reported_event_date": reported.date().isoformat(),
            "pre3day_max_city_index": round(float(before3["city_max_predictive_index"].max()), 3) if not before3.empty else None,
            "pre3day_max_city_average_index": round(float(before3["mean_city_average_predictive_index"].max()), 3) if not before3.empty else None,
            "priority_any_day_pre3": any_priority_pre3,
            "high_critical_any_day_pre3": any_high_pre3,
            "critical_any_day_pre3": any_critical_pre3,
            "previous_day_max_city_index": round(float(prev.iloc[0]["city_max_predictive_index"]), 3) if not prev.empty else None,
            "reported_date_city_max_index": round(float(rrow["city_max_predictive_index"]), 3) if rrow is not None else None,
            "reported_date_city_average_index": round(float(rrow["mean_city_average_predictive_index"]), 3) if rrow is not None else None,
            "reported_date_priority_wards": int(rrow["wards_priority_ge_0_35"]) if rrow is not None else None,
            "episode_peak_city_index": round(float(peak["city_max_predictive_index"]), 3),
            "episode_peak_index_date": peak["date"].date().isoformat(),
            "episode_peak_city_average_index": round(float(mean_peak["mean_city_average_predictive_index"]), 3),
            "episode_peak_average_date": mean_peak["date"].date().isoformat(),
            "episode_max_priority_wards": int(episode["wards_priority_ge_0_35"].max()),
            "episode_max_high_critical_wards": int(episode["wards_high_or_critical_ge_0_60"].max()),
            "episode_max_critical_wards": int(episode["wards_critical_ge_0_80"].max()),
            "episode_days_with_any_priority": int((episode["wards_priority_ge_0_35"] > 0).sum()),
            "episode_days_with_any_high_critical": int((episode["wards_high_or_critical_ge_0_60"] > 0).sum()),
            "episode_peak_max_grid_rainfall_mm": round(float(episode.loc[episode["max_observed_rainfall_mm"].idxmax(), "max_observed_rainfall_mm"]), 1),
            "evaluation_note": "Observed daily rainfall replay; pre-event risk is not archived-forecast lead time.",
            "source_url": event.get("source_url"),
        })

    out = pd.DataFrame(rows)
    out.to_csv(OUTPUT, index=False)
    display_cols = [
        "event_id", "evidence_class", "episode_start", "episode_end",
        "previous_day_max_city_index", "pre3day_max_city_index",
        "reported_date_city_max_index", "episode_peak_city_index",
        "episode_peak_index_date", "episode_max_high_critical_wards",
        "episode_max_critical_wards", "episode_days_with_any_priority",
    ]
    print("V2.1 DOCUMENTED-EVENT WINDOW EVALUATION")
    print("Reminder: this measures historical response to observed daily rainfall, not forecast skill.")
    print(out[display_cols].to_string(index=False))
    print(f"\nSaved: {OUTPUT}")
    print("Ward-level rows available for detailed inspection:", len(ward_daily))


if __name__ == "__main__":
    main()
