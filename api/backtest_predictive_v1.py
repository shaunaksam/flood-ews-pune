"""Retrospective rainfall-response back-test for Flood EWS predictive index V1.

Run from the repository root:
    python backtest_predictive_v1.py

This replays observed IMD daily rainfall through the current V1 susceptibility
formula. It is NOT a forecast-skill or early-warning lead-time evaluation because
archived forecasts and hourly rainfall are not available in this input series.
"""
from pathlib import Path
import re
import sys
import os

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"
RAIN_FILE = PROCESSED / "imd_pune_regional_daily_summary_2021_2025.csv"
EVENT_FILE = PROCESSED / "pune_documented_event_registry.csv"
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/flood_ews")

# These match api/main.py predictive V1 assumptions.
RAIN_REFERENCE_24H_MM = 115.5
DEFAULT_CUSECS = 15000.0
THRESHOLD_CUSECS = 25000.0
WARD_ALERT_PRIORITY_THRESHOLD = 0.35
HIGH_THRESHOLD = 0.60
CRITICAL_THRESHOLD = 0.80


def clamp01(value):
    return float(np.clip(value, 0.0, 1.0))


def normalise_low_is_risk(series: pd.Series) -> pd.Series:
    """Equivalent to (value - max) / (min - max): lower means riskier."""
    minimum = float(series.min())
    maximum = float(series.max())
    if maximum == minimum:
        return pd.Series(0.0, index=series.index)
    return (maximum - series) / (maximum - minimum)


def normalise_high_is_risk(series: pd.Series) -> pd.Series:
    minimum = float(series.min())
    maximum = float(series.max())
    if maximum == minimum:
        return pd.Series(0.0, index=series.index)
    return (series - minimum) / (maximum - minimum)


def load_wards() -> pd.DataFrame:
    engine = create_engine(DB_URL, pool_pre_ping=True)
    try:
        wards = pd.read_sql(
            "SELECT wardnum, name, elevation_mean, river_distance_m, "
            "dam_discharge_incident_count FROM wards",
            engine,
        )
    finally:
        engine.dispose()

    if wards.empty:
        raise RuntimeError("Database query returned no wards.")
    required_numeric = [
        "wardnum", "elevation_mean", "river_distance_m",
        "dam_discharge_incident_count",
    ]
    for column in required_numeric:
        wards[column] = pd.to_numeric(wards[column], errors="coerce")
        if wards[column].isna().any():
            raise RuntimeError(f"Ward database has missing/non-numeric {column} values.")
    if wards["wardnum"].duplicated().any():
        raise RuntimeError("Duplicate wardnum values found in wards table.")
    return wards


def prepare_susceptibility(wards: pd.DataFrame) -> pd.DataFrame:
    """Precompute V1's fixed ward susceptibility fields once for all dates."""
    result = wards.copy()
    result["elevation_risk"] = normalise_low_is_risk(result["elevation_mean"])
    result["river_risk"] = normalise_low_is_risk(result["river_distance_m"])
    result["incident_risk"] = normalise_high_is_risk(
        result["dam_discharge_incident_count"]
    )
    result["static_vulnerability"] = (
        0.3 * result["elevation_risk"]
        + 0.5 * result["incident_risk"]
        + 0.2 * result["river_risk"]
    )
    result["urban_susceptibility"] = (
        0.7 * result["elevation_risk"] + 0.3 * result["river_risk"]
    ).clip(0.0, 1.0)
    result["river_susceptibility"] = (
        0.6 * result["incident_risk"] + 0.4 * result["river_risk"]
    ).clip(0.0, 1.0)
    return result


def score_day(wards: pd.DataFrame, rainfall_mm: float) -> dict:
    """Use only 24-hour observed rainfall; the IMD grid has no 6-hour values."""
    if not np.isfinite(rainfall_mm) or rainfall_mm < 0:
        raise ValueError(f"Invalid rainfall value: {rainfall_mm}")

    rainfall_severity = clamp01(rainfall_mm / RAIN_REFERENCE_24H_MM)
    # Default discharge is at the V1 reference level, so discharge pressure is 0.
    discharge_hazard = clamp01(
        (DEFAULT_CUSECS - DEFAULT_CUSECS)
        / (THRESHOLD_CUSECS - DEFAULT_CUSECS)
    )
    river_pressure = max(rainfall_severity, discharge_hazard)

    urban_index = rainfall_severity * wards["urban_susceptibility"]
    river_index = river_pressure * wards["river_susceptibility"]
    final_index = pd.concat([urban_index, river_index], axis=1).max(axis=1).clip(0, 1)

    high_mask = final_index >= HIGH_THRESHOLD
    critical_mask = final_index >= CRITICAL_THRESHOLD
    priority_mask = final_index >= WARD_ALERT_PRIORITY_THRESHOLD
    top_idx = final_index.sort_values(ascending=False).head(5).index

    return {
        "rainfall_mm": round(float(rainfall_mm), 3),
        "rainfall_severity_24h_only": round(rainfall_severity, 4),
        "city_average_predictive_index": round(float(final_index.mean()), 4),
        "city_max_predictive_index": round(float(final_index.max()), 4),
        "wards_priority_ge_0_35": int(priority_mask.sum()),
        "wards_high_or_critical_ge_0_60": int(high_mask.sum()),
        "wards_critical_ge_0_80": int(critical_mask.sum()),
        "top_5_wards": "; ".join(
            f"{int(wards.loc[i, 'wardnum'])} {wards.loc[i, 'name']} ({final_index.loc[i]:.3f})"
            for i in top_idx
        ),
    }


def main():
    if not RAIN_FILE.exists():
        raise SystemExit(
            f"Missing {RAIN_FILE}. Run the IMD extraction scripts first."
        )

    rainfall = pd.read_csv(RAIN_FILE, parse_dates=["date"])
    required = {"date", "center_cell_mm", "regional_mean_mm"}
    missing = required - set(rainfall.columns)
    if missing:
        raise SystemExit(f"Rainfall input is missing columns: {sorted(missing)}")
    rainfall["date"] = rainfall["date"].dt.normalize()
    if rainfall["date"].duplicated().any():
        raise SystemExit("Rainfall input has duplicate dates.")
    if rainfall[["center_cell_mm", "regional_mean_mm"]].isna().any().any():
        raise SystemExit("Rainfall input contains missing central/regional values.")
    if len(rainfall) != 1826:
        print(f"WARNING: Expected 1,826 daily rows; found {len(rainfall)}.")

    wards = prepare_susceptibility(load_wards())
    print(f"Loaded {len(wards)} wards from PostgreSQL.")
    print(f"Rainfall dates: {rainfall['date'].min().date()} to {rainfall['date'].max().date()} ({len(rainfall)} rows)")
    print("Discharge scenario: 15,000 cusecs (scenario only; no live feed).")
    print("Method: observed daily rainfall, 24-hour branch only; six-hour severity is NOT inferred.")

    outputs = []
    for _, row in rainfall.iterrows():
        for mode, column in [
            ("central_grid_cell", "center_cell_mm"),
            ("nine_cell_regional_mean", "regional_mean_mm"),
        ]:
            metrics = score_day(wards, float(row[column]))
            outputs.append({
                "date": row["date"].date().isoformat(),
                "rainfall_input_mode": mode,
                **metrics,
            })

    daily = pd.DataFrame(outputs)
    daily_path = PROCESSED / "predictive_v1_historical_rainfall_replay.csv"
    daily.to_csv(daily_path, index=False)

    summary_rows = []
    for mode, group in daily.groupby("rainfall_input_mode"):
        summary_rows.append({
            "rainfall_input_mode": mode,
            "days": len(group),
            "days_with_any_ward_priority": int((group["wards_priority_ge_0_35"] > 0).sum()),
            "pct_days_with_any_ward_priority": round(100 * (group["wards_priority_ge_0_35"] > 0).mean(), 2),
            "days_with_any_high_or_critical_ward": int((group["wards_high_or_critical_ge_0_60"] > 0).sum()),
            "days_with_any_critical_ward": int((group["wards_critical_ge_0_80"] > 0).sum()),
            "mean_city_average_index": round(group["city_average_predictive_index"].mean(), 4),
            "max_city_index": round(group["city_max_predictive_index"].max(), 4),
            "max_priority_wards_on_a_day": int(group["wards_priority_ge_0_35"].max()),
            "max_high_critical_wards_on_a_day": int(group["wards_high_or_critical_ge_0_60"].max()),
        })
    summary = pd.DataFrame(summary_rows)
    summary_path = PROCESSED / "predictive_v1_historical_replay_summary.csv"
    summary.to_csv(summary_path, index=False)

    print("\nHISTORICAL RAINFALL-RESPONSE SUMMARY (not forecast-skill metrics)")
    print(summary.to_string(index=False))

    print("\nTop 15 central-cell rainfall days by city-wide maximum index:")
    central = daily[daily["rainfall_input_mode"] == "central_grid_cell"]
    print(
        central.sort_values("city_max_predictive_index", ascending=False)
        [["date", "rainfall_mm", "rainfall_severity_24h_only",
          "city_average_predictive_index", "city_max_predictive_index",
          "wards_priority_ge_0_35", "wards_high_or_critical_ge_0_60",
          "wards_critical_ge_0_80"]]
        .head(15).to_string(index=False)
    )

    if EVENT_FILE.exists():
        events = pd.read_csv(EVENT_FILE)
        if {"event_id", "episode_window", "reported_event_date"}.issubset(events.columns):
            event_rows = []
            for _, event in events.iterrows():
                dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(event["episode_window"]))
                if len(dates) < 2:
                    continue
                start, end = pd.Timestamp(dates[0]), pd.Timestamp(dates[1])
                for mode in ["central_grid_cell", "nine_cell_regional_mean"]:
                    series = daily[daily["rainfall_input_mode"] == mode].copy()
                    series["parsed_date"] = pd.to_datetime(series["date"])
                    window = series[series["parsed_date"].between(start, end)]
                    prior = series[series["parsed_date"] == start - pd.Timedelta(days=1)]
                    if window.empty:
                        continue
                    peak = window.loc[window["city_max_predictive_index"].idxmax()]
                    event_rows.append({
                        "event_id": event["event_id"],
                        "episode_id": event.get("episode_id"),
                        "evidence_class": event.get("evidence_class"),
                        "episode_start": start.date().isoformat(),
                        "episode_end": end.date().isoformat(),
                        "reported_event_date": event["reported_event_date"],
                        "rainfall_input_mode": mode,
                        "previous_day_city_max_index": (
                            float(prior.iloc[0]["city_max_predictive_index"]) if not prior.empty else np.nan
                        ),
                        "peak_city_max_index_in_episode": float(peak["city_max_predictive_index"]),
                        "peak_city_average_index_in_episode": float(window["city_average_predictive_index"].max()),
                        "episode_days_with_any_high_or_critical_ward": int(
                            (window["wards_high_or_critical_ge_0_60"] > 0).sum()
                        ),
                        "peak_rainfall_input_mm": float(window["rainfall_mm"].max()),
                        "interpretation": "Observed-rainfall replay only; not a forecast or proof of early warning lead time.",
                    })
            if event_rows:
                event_path = PROCESSED / "predictive_v1_event_window_replay.csv"
                pd.DataFrame(event_rows).to_csv(event_path, index=False)
                print("\nEvent-window replay saved:", event_path)
        else:
            print("\nEvent registry found, but expected columns are missing; skipped event window replay.")
    else:
        print(f"\nOptional event registry not found (skipped): {EVENT_FILE}")

    print("\nSaved output files:")
    print(" ", daily_path)
    print(" ", summary_path)
    print("\nImportant: these outputs assess response to OBSERVED daily rainfall. They do not measure forecast accuracy or warning lead time.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
