import pandas as pd
from pathlib import Path

DATA_DIR = Path("data/processed")
INPUT = DATA_DIR / "imd_pune_regional_daily_summary_2021_2025.csv"
OUTPUT = DATA_DIR / "imd_pune_heavy_rain_episodes_2021_2025.csv"

THRESHOLD_MM = 64.5

# Merge exceedance dates if separated by at most one non-exceedance day.
MAX_GAP_DAYS = 1

df = pd.read_csv(INPUT, parse_dates=["date"])
df["date"] = df["date"].dt.normalize()
df = df.sort_values("date").reset_index(drop=True)

if df["date"].duplicated().any():
    raise SystemExit("Duplicate dates found in input.")

required = {
    "date",
    "center_cell_mm",
    "regional_mean_mm",
    "regional_max_mm",
    "cells_at_or_above_64_5_mm",
}
missing = required - set(df.columns)

if missing:
    raise SystemExit(f"Missing columns: {sorted(missing)}")

# Find dates when at least one grid cell reaches the threshold.
candidates = df[
    df["regional_max_mm"] >= THRESHOLD_MM
].copy().sort_values("date")

if candidates.empty:
    raise SystemExit("No threshold-exceedance dates found.")

# Consecutive exceedance dates belong to the same episode.
# A gap of one non-exceedance day is also allowed.
max_date_difference = MAX_GAP_DAYS + 1

date_gaps = candidates["date"].diff().dt.days
candidates["episode_id"] = (
    date_gaps.gt(max_date_difference).cumsum() + 1
)

episodes = []

for episode_id, group in candidates.groupby("episode_id"):
    start = group["date"].min()
    end = group["date"].max()

    # Include intervening days in the episode's rainfall summary.
    window = df[df["date"].between(start, end)]

    peak_cell_row = window.loc[
        window["regional_max_mm"].idxmax()
    ]
    peak_mean_row = window.loc[
        window["regional_mean_mm"].idxmax()
    ]

    episodes.append({
        "episode_id": int(episode_id),
        "start_date": start.date(),
        "end_date": end.date(),
        "span_days": len(window),
        "threshold_exceedance_days": len(group),
        "intervening_non_exceedance_days":
            len(window) - len(group),
        "exceedance_dates": ", ".join(
            d.strftime("%Y-%m-%d") for d in group["date"]
        ),
        "peak_grid_rainfall_mm": round(
            peak_cell_row["regional_max_mm"], 2
        ),
        "peak_grid_rainfall_date":
            peak_cell_row["date"].date(),
        "peak_regional_mean_mm": round(
            window["regional_mean_mm"].max(), 2
        ),
        "peak_regional_mean_date":
            peak_mean_row["date"].date(),
        "max_cells_at_or_above_threshold": int(
            window["cells_at_or_above_64_5_mm"].max()
        ),
        "sum_daily_regional_mean_mm": round(
            window["regional_mean_mm"].sum(), 2
        ),
    })

result = pd.DataFrame(episodes)
result.to_csv(OUTPUT, index=False)

print("Threshold:", THRESHOLD_MM, "mm")
print("Candidate exceedance dates:", len(candidates))
print("Grouped rainfall episodes:", len(result))
print("\nEpisode summary:")
print(result.to_string(index=False))
print("\nSaved:", OUTPUT)