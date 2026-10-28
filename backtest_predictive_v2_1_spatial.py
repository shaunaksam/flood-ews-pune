"""Historical response back-test for Flood EWS predictive index V2.1.

Uses observed IMD daily rainfall from the nearest cell in the extracted 3x3 grid
for each ward centroid. It is a retrospective rainfall-response test, NOT a
forecast-skill test and NOT validation against confirmed flood labels.
"""
from pathlib import Path
import math
import sys

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent
PROCESSED = ROOT / "data" / "processed"
GRID_FILE = PROCESSED / "imd_pune_3x3_grid_daily_2021_2025.csv"
DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/flood_ews"

# Match current V2.1 prototype assumptions. Do not infer 6-hour rainfall from daily data.
SCENARIO_CUSECS = 15000.0
DISCHARGE_THRESHOLD_CUSECS = 25000.0
RAIN_REFERENCE_24H_MM = 115.5
PRIORITY_THRESHOLD = 0.35
HIGH_THRESHOLD = 0.60
CRITICAL_THRESHOLD = 0.80


def minmax_inverse(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").astype(float)
    low, high = values.min(), values.max()
    if pd.isna(low) or pd.isna(high):
        raise ValueError(f"Cannot normalize column {series.name}: missing values")
    if math.isclose(high, low):
        return pd.Series(0.0, index=series.index)
    return ((high - values) / (high - low)).clip(0, 1)


def minmax_forward(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").astype(float)
    low, high = values.min(), values.max()
    if pd.isna(low) or pd.isna(high):
        raise ValueError(f"Cannot normalize column {series.name}: missing values")
    if math.isclose(high, low):
        return pd.Series(0.0, index=series.index)
    return ((values - low) / (high - low)).clip(0, 1)


def haversine_km(lat1, lon1, lat2, lon2):
    """Vector distance from one ward coordinate to a vector of grid coordinates."""
    lat1 = np.radians(float(lat1))
    lon1 = np.radians(float(lon1))
    lat2 = np.radians(np.asarray(lat2, dtype=float))
    lon2 = np.radians(np.asarray(lon2, dtype=float))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def main():
    if not GRID_FILE.is_file():
        raise FileNotFoundError(
            f"Missing rainfall grid file: {GRID_FILE}\n"
            "Expected data/processed/imd_pune_3x3_grid_daily_2021_2025.csv"
        )

    print("Connecting to PostgreSQL and loading ward centroids...")
    engine = create_engine(DATABASE_URL)
    query = text("""
        SELECT wardnum, name, elevation_mean, river_distance_m,
               dam_discharge_incident_count,
               ST_Y(ST_Centroid(geometry)) AS latitude,
               ST_X(ST_Centroid(geometry)) AS longitude
        FROM wards
        ORDER BY wardnum
    """)
    with engine.connect() as conn:
        wards = pd.read_sql_query(query, conn)

    if len(wards) != 58 or wards["wardnum"].nunique() != len(wards):
        raise ValueError(
            f"Expected 58 unique wards; got {len(wards)} rows and "
            f"{wards['wardnum'].nunique()} unique ward numbers."
        )
    for col in ["elevation_mean", "river_distance_m", "dam_discharge_incident_count", "latitude", "longitude"]:
        wards[col] = pd.to_numeric(wards[col], errors="coerce")
        if wards[col].isna().any():
            bad = wards.loc[wards[col].isna(), "wardnum"].tolist()
            raise ValueError(f"Missing {col} for ward(s): {bad}")

    # Reproduce V2.1 static susceptibility normalization from compute_risk().
    wards["elevation_risk"] = minmax_inverse(wards["elevation_mean"])
    wards["river_risk"] = minmax_inverse(wards["river_distance_m"])
    wards["incident_risk"] = minmax_forward(wards["dam_discharge_incident_count"])
    wards["static_vulnerability"] = (
        0.3 * wards["elevation_risk"]
        + 0.5 * wards["incident_risk"]
        + 0.2 * wards["river_risk"]
    ).clip(0, 1)
    wards["urban_susceptibility_proxy"] = (
        0.7 * wards["elevation_risk"] + 0.3 * wards["river_risk"]
    ).clip(0, 1)
    wards["river_corridor_susceptibility_proxy"] = (
        0.6 * wards["incident_risk"] + 0.4 * wards["river_risk"]
    ).clip(0, 1)

    print(f"Loaded {len(wards)} wards from PostgreSQL.")

    grid = pd.read_csv(GRID_FILE, parse_dates=["date"])
    expected = {"date", "latitude", "longitude", "rainfall_mm"}
    missing = expected - set(grid.columns)
    if missing:
        raise ValueError(f"Grid CSV missing columns: {sorted(missing)}")
    grid["date"] = pd.to_datetime(grid["date"], errors="coerce").dt.normalize()
    for col in ["latitude", "longitude", "rainfall_mm"]:
        grid[col] = pd.to_numeric(grid[col], errors="coerce")
    if grid[list(expected)].isna().any().any():
        raise ValueError("Grid data contains missing dates, coordinates or rainfall values.")
    if (grid["rainfall_mm"] < 0).any():
        raise ValueError("Grid data contains negative rainfall values after extraction.")
    if grid.duplicated(["date", "latitude", "longitude"]).any():
        raise ValueError("Duplicate date/grid-cell rows detected.")
    cell_counts = grid.groupby("date").size()
    if not (cell_counts == 9).all():
        raise ValueError("Every date must contain exactly nine cells in the 3x3 grid.")

    cells = grid[["latitude", "longitude"]].drop_duplicates().reset_index(drop=True)
    if len(cells) != 9:
        raise ValueError(f"Expected 9 grid cells but found {len(cells)}.")

    # Assign each ward to the nearest of the nine available grid cells.
    assignments = []
    grid_lats = cells["latitude"].to_numpy()
    grid_lons = cells["longitude"].to_numpy()
    for _, ward in wards.iterrows():
        distances = haversine_km(
            ward["latitude"], ward["longitude"], grid_lats, grid_lons
        )
        index = int(np.argmin(distances))
        assignments.append({
            "wardnum": int(ward["wardnum"]),
            "name": ward["name"],
            "latitude": float(ward["latitude"]),
            "longitude": float(ward["longitude"]),
            "grid_latitude": float(grid_lats[index]),
            "grid_longitude": float(grid_lons[index]),
            "nearest_grid_distance_km": float(distances[index]),
        })
    assignment = pd.DataFrame(assignments)
    max_distance = assignment["nearest_grid_distance_km"].max()
    over_20 = assignment.loc[assignment["nearest_grid_distance_km"] > 20]
    print(f"Rainfall grid: {len(cells)} cells, {grid['date'].nunique()} dates.")
    print(f"Nearest-grid assignment distance: median={assignment['nearest_grid_distance_km'].median():.1f} km, max={max_distance:.1f} km.")
    if not over_20.empty:
        print("WARNING: wards more than 20 km from nearest extracted cell:")
        print(over_20[["wardnum", "name", "nearest_grid_distance_km"]].to_string(index=False))

    # Give each ward a historical observed daily rainfall value from its nearest grid cell.
    rain_for_join = grid[["date", "latitude", "longitude", "rainfall_mm"]].rename(columns={
        "latitude": "grid_latitude",
        "longitude": "grid_longitude",
        "rainfall_mm": "observed_rainfall_24h_mm",
    })
    ward_daily = assignment.merge(
        rain_for_join,
        on=["grid_latitude", "grid_longitude"],
        how="inner",
        validate="many_to_many",
    )
    if ward_daily.duplicated(["date", "wardnum"]).any():
        raise ValueError("Duplicate ward/date rows after joining grid rainfall.")

    # Join ward vulnerability components.
    component_cols = [
        "wardnum", "elevation_risk", "river_risk", "incident_risk",
        "static_vulnerability", "urban_susceptibility_proxy",
        "river_corridor_susceptibility_proxy",
    ]
    ward_daily = ward_daily.merge(
        wards[component_cols], on="wardnum", how="left", validate="many_to_one"
    )

    # Observed daily totals support only the 24-hour branch. Never invent a six-hour amount.
    ward_daily["rainfall_hazard_score"] = (
        ward_daily["observed_rainfall_24h_mm"] / RAIN_REFERENCE_24H_MM
    ).clip(0, 1)
    ward_daily["urban_waterlogging_index"] = (
        ward_daily["rainfall_hazard_score"] * ward_daily["urban_susceptibility_proxy"]
    ).clip(0, 1)

    if SCENARIO_CUSECS > DISCHARGE_THRESHOLD_CUSECS:
        discharge_hazard = 1.0
    else:
        discharge_hazard = np.clip(
            (SCENARIO_CUSECS - 15000.0) / (DISCHARGE_THRESHOLD_CUSECS - 15000.0),
            0,
            1,
        )
    river_pressure = np.maximum(ward_daily["rainfall_hazard_score"], discharge_hazard)
    ward_daily["river_corridor_index"] = (
        river_pressure * ward_daily["river_corridor_susceptibility_proxy"]
    ).clip(0, 1)

    # At 15,000 cusecs the historic high-discharge corridor override is not active.
    ward_daily["corridor_flagged"] = False
    ward_daily["predictive_risk_score"] = ward_daily[
        ["urban_waterlogging_index", "river_corridor_index"]
    ].max(axis=1).clip(0, 1)
    ward_daily["risk_level"] = np.select(
        [
            ward_daily["predictive_risk_score"] >= CRITICAL_THRESHOLD,
            ward_daily["predictive_risk_score"] >= HIGH_THRESHOLD,
            ward_daily["predictive_risk_score"] >= 0.4,
        ],
        ["CRITICAL", "HIGH", "MODERATE"],
        default="LOW",
    )
    ward_daily["priority"] = ward_daily["predictive_risk_score"] >= PRIORITY_THRESHOLD
    ward_daily["high_or_critical"] = ward_daily["predictive_risk_score"] >= HIGH_THRESHOLD
    ward_daily["critical"] = ward_daily["predictive_risk_score"] >= CRITICAL_THRESHOLD

    # Save ward-by-day rows and city-by-day summary.
    ward_output = PROCESSED / "predictive_v2_1_spatial_ward_daily_replay.csv"
    ward_daily.sort_values(["date", "predictive_risk_score"], ascending=[True, False]).to_csv(ward_output, index=False)

    daily = ward_daily.groupby("date").agg(
        ward_count=("wardnum", "nunique"),
        mean_observed_rainfall_mm=("observed_rainfall_24h_mm", "mean"),
        max_observed_rainfall_mm=("observed_rainfall_24h_mm", "max"),
        mean_city_average_predictive_index=("predictive_risk_score", "mean"),
        city_max_predictive_index=("predictive_risk_score", "max"),
        wards_priority_ge_0_35=("priority", "sum"),
        wards_high_or_critical_ge_0_60=("high_or_critical", "sum"),
        wards_critical_ge_0_80=("critical", "sum"),
    ).reset_index()
    daily["year"] = daily["date"].dt.year
    daily_output = PROCESSED / "predictive_v2_1_spatial_historical_daily_replay.csv"
    daily.to_csv(daily_output, index=False)

    print("\nHISTORICAL SPATIAL RAINFALL-RESPONSE SUMMARY (not forecast-skill metrics)")
    print(f"Discharge scenario: {SCENARIO_CUSECS:,.0f} cusecs (scenario only).")
    print("Rainfall: nearest of the 9 extracted IMD grid cells for each ward; daily observed totals only.")
    print("No six-hour rainfall inferred; no confirmed flood label is used in this replay.")
    print("\nAll-years summary:")
    print(pd.DataFrame([{
        "days": len(daily),
        "ward_day_rows": len(ward_daily),
        "days_any_priority": int((daily["wards_priority_ge_0_35"] > 0).sum()),
        "pct_days_any_priority": round(100 * (daily["wards_priority_ge_0_35"] > 0).mean(), 2),
        "days_any_high_or_critical": int((daily["wards_high_or_critical_ge_0_60"] > 0).sum()),
        "days_any_critical": int((daily["wards_critical_ge_0_80"] > 0).sum()),
        "mean_city_average_index": round(daily["mean_city_average_predictive_index"].mean(), 4),
        "max_city_index": round(daily["city_max_predictive_index"].max(), 4),
        "max_priority_wards_on_a_day": int(daily["wards_priority_ge_0_35"].max()),
        "max_high_critical_wards_on_a_day": int(daily["wards_high_or_critical_ge_0_60"].max()),
    }]).to_string(index=False))

    print("\nYearly summary:")
    yearly = daily.groupby("year").agg(
        days=("date", "count"),
        days_any_priority=("wards_priority_ge_0_35", lambda s: int((s > 0).sum())),
        days_any_high_or_critical=("wards_high_or_critical_ge_0_60", lambda s: int((s > 0).sum())),
        days_any_critical=("wards_critical_ge_0_80", lambda s: int((s > 0).sum())),
        mean_city_average_index=("mean_city_average_predictive_index", "mean"),
        max_city_index=("city_max_predictive_index", "max"),
        max_priority_wards_on_a_day=("wards_priority_ge_0_35", "max"),
        max_high_critical_wards_on_a_day=("wards_high_or_critical_ge_0_60", "max"),
    ).round(4)
    print(yearly.to_string())

    print("\nTop 15 dates by city maximum predictive index:")
    print(daily.sort_values("city_max_predictive_index", ascending=False).head(15)[[
        "date", "mean_observed_rainfall_mm", "max_observed_rainfall_mm",
        "mean_city_average_predictive_index", "city_max_predictive_index",
        "wards_priority_ge_0_35", "wards_high_or_critical_ge_0_60", "wards_critical_ge_0_80",
    ]].to_string(index=False))

    # Score-band sensitivity summary lets us inspect threshold effects without retuning the scores.
    sensitivity_rows = []
    for threshold in [0.35, 0.40, 0.50, 0.60, 0.70, 0.80]:
        counts = ward_daily.groupby("date")["predictive_risk_score"].apply(lambda s: int((s >= threshold).sum()))
        sensitivity_rows.append({
            "score_threshold": threshold,
            "days_with_at_least_one_ward": int((counts > 0).sum()),
            "pct_days_with_at_least_one_ward": round(100 * float((counts > 0).mean()), 2),
            "max_wards_on_any_day": int(counts.max()),
            "mean_wards_per_day": round(float(counts.mean()), 2),
        })
    sensitivity = pd.DataFrame(sensitivity_rows)
    sensitivity_output = PROCESSED / "predictive_v2_1_spatial_threshold_sensitivity.csv"
    sensitivity.to_csv(sensitivity_output, index=False)
    print("\nThreshold sensitivity (threshold counts are diagnostic, not calibrated classes):")
    print(sensitivity.to_string(index=False))

    print("\nSaved output files:")
    print(" ", ward_output)
    print(" ", daily_output)
    print(" ", sensitivity_output)
    print("\nInterpretation note: 9 grid cells serve 58 wards, so neighbouring wards share rainfall inputs.")
    print("The 2025 IMD file's provenance still needs confirmation. This tests response to observed daily rainfall, not advance forecast skill.")
    engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
