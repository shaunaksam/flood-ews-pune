import pandas as pd
from pathlib import Path

DATA_DIR = Path("data/processed")
INPUT = DATA_DIR / "imd_pune_regional_daily_summary_2021_2025.csv"
OUTPUT = DATA_DIR / "imd_threshold_exceedance_days_2021_2025.csv"

THRESHOLD_MM = 64.5

df = pd.read_csv(INPUT, parse_dates=["date"])
df = df.sort_values("date")

df["center_exceeds"] = df["center_cell_mm"] >= THRESHOLD_MM
df["regional_mean_exceeds"] = df["regional_mean_mm"] >= THRESHOLD_MM
df["regional_max_exceeds"] = df["regional_max_mm"] >= THRESHOLD_MM
df["any_cell_exceeds"] = df["cells_at_or_above_64_5_mm"] > 0

print("Full historical record")
print("Days:", len(df))
print("Period:", df["date"].min().date(), "to", df["date"].max().date())

print("\nThreshold exceedances by year")
yearly = df.groupby(df["date"].dt.year).agg(
    days=("date", "count"),
    center_cell_days=("center_exceeds", "sum"),
    regional_mean_days=("regional_mean_exceeds", "sum"),
    regional_max_days=("regional_max_exceeds", "sum"),
    days_with_any_cell=("any_cell_exceeds", "sum"),
)

yearly["regional_mean_pct"] = (
    100 * yearly["regional_mean_days"] / yearly["days"]
).round(1)

print(yearly.to_string())

exceedances = df[df["regional_mean_exceeds"]].copy()
exceedances.to_csv(OUTPUT, index=False)

print("\nTotal days with regional mean >= 64.5 mm:",
      len(exceedances))
print("Percentage of the full record:",
      round(100 * len(exceedances) / len(df), 2), "%")
print("Saved:", OUTPUT)