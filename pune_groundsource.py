from pathlib import Path
import urllib.request
import numpy as np
import pandas as pd
import geopandas as gpd
import pyarrow.parquet as pq
import shapely
from shapely.geometry import box

ROOT = Path(__file__).resolve().parent
URL = "https://zenodo.org/records/18647054/files/groundsource_2026.parquet?download=1"
RAW = ROOT / "data" / "raw" / "groundsource_2026.parquet"
WARDS = ROOT / "data" / "raw" / "pune_wards.geojson"
OUT_CSV = ROOT / "data" / "processed" / "groundsource_pune_events.csv"
OUT_GEO = ROOT / "data" / "processed" / "groundsource_pune_events.geojson"
OUT_PAIRS = ROOT / "data" / "processed" / "groundsource_pune_ward_event_pairs.csv"
WARD_KEYS = ["wardnum", "ward_num", "WARDNUM", "ward_no", "Ward_No", "prabhag", "ward", "id"]


def download():
    RAW.parent.mkdir(parents=True, exist_ok=True)
    if RAW.exists() and RAW.stat().st_size > 100_000_000:
        print(f"Using existing {RAW.name} ({RAW.stat().st_size / 1e6:.0f} MB)")
        return
    print("Downloading Groundsource (about 667 MB, one time)...")
    last = [0]

    def hook(b, bs, total):
        done = b * bs
        if total > 0 and done - last[0] > 40_000_000:
            last[0] = done
            print(f"  {done / 1e6:.0f} / {total / 1e6:.0f} MB")

    urllib.request.urlretrieve(URL, str(RAW), reporthook=hook)


def load_wards():
    w = gpd.read_file(WARDS)
    w = w.set_crs(epsg=4326) if w.crs is None else w.to_crs(epsg=4326)
    key = next((k for k in WARD_KEYS if k in w.columns), None)
    w["wardnum"] = pd.to_numeric(w[key], errors="coerce") if key else np.arange(1, len(w) + 1)
    if w["wardnum"].isna().any():
        w["wardnum"] = np.arange(1, len(w) + 1)
    return w[["wardnum", "geometry"]].reset_index(drop=True)


def main():
    download()
    wards = load_wards()
    union = wards.geometry.union_all() if hasattr(wards.geometry, "union_all") else wards.geometry.unary_union
    minx, miny, maxx, maxy = wards.total_bounds
    pre = box(minx - 0.05, miny - 0.05, maxx + 0.05, maxy + 0.05)
    pf = pq.ParquetFile(RAW)
    print(f"Scanning {pf.metadata.num_rows:,} events for Pune...")
    keep = []
    seen = 0
    for batch in pf.iter_batches(batch_size=150_000, columns=["uuid", "area_km2", "geometry", "start_date", "end_date"]):
        df = batch.to_pandas()
        seen += len(df)
        geoms = shapely.from_wkb(df["geometry"].to_numpy())
        b = shapely.bounds(geoms)
        m = (b[:, 0] <= pre.bounds[2]) & (b[:, 2] >= pre.bounds[0]) & (b[:, 1] <= pre.bounds[3]) & (b[:, 3] >= pre.bounds[1])
        if not m.any():
            continue
        sub = df[m].copy()
        g = geoms[m]
        hit = shapely.intersects(g, union)
        sub = sub[hit]
        sub["geometry"] = g[hit]
        if len(sub):
            keep.append(sub)
        if seen % 600_000 < 150_000:
            print(f"  scanned {seen:,}")
    if not keep:
        print("No events intersect Pune wards.")
        return
    ev = pd.concat(keep, ignore_index=True)
    ev["start_date"] = pd.to_datetime(ev["start_date"], errors="coerce")
    ev["end_date"] = pd.to_datetime(ev["end_date"], errors="coerce")
    gdf = gpd.GeoDataFrame(ev, geometry="geometry", crs="EPSG:4326")
    cen = gdf.geometry.representative_point()
    gdf["lon"] = cen.x.round(5)
    gdf["lat"] = cen.y.round(5)
    pairs = gpd.sjoin(gdf[["uuid", "geometry"]], wards, how="inner", predicate="intersects")[["uuid", "wardnum"]]
    counts = pairs.groupby("uuid").size().rename("n_wards_touched")
    gdf = gdf.merge(counts, left_on="uuid", right_index=True, how="left")
    gdf["n_wards_touched"] = gdf["n_wards_touched"].fillna(0).astype(int)
    gdf["year"] = gdf["start_date"].dt.year
    gdf["month"] = gdf["start_date"].dt.month
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    gdf.drop(columns="geometry").to_csv(OUT_CSV, index=False)
    gdf[["uuid", "start_date", "end_date", "area_km2", "n_wards_touched", "geometry"]].assign(
        start_date=lambda d: d["start_date"].dt.strftime("%Y-%m-%d"), end_date=lambda d: d["end_date"].dt.strftime("%Y-%m-%d")
    ).to_file(OUT_GEO, driver="GeoJSON")
    pairs.merge(gdf[["uuid", "start_date"]], on="uuid").to_csv(OUT_PAIRS, index=False)
    print(f"\nPune events found: {len(gdf):,}")
    print("\nBy year:")
    print(gdf.groupby("year").size().to_string())
    print("\nBy month (all years):")
    print(gdf.groupby("month").size().to_string())
    print("\nEvent size, km2 (median / 75% / 95% / max):", [round(float(x), 1) for x in gdf["area_km2"].quantile([0.5, 0.75, 0.95, 1.0])])
    small = gdf[gdf["area_km2"] <= 25]
    print(f"Local-scale events (25 km2 or less): {len(small):,}")
    print("\nWard-level touches per event (median / max):", int(gdf["n_wards_touched"].median()), int(gdf["n_wards_touched"].max()))
    print("\nBiggest-impact days (events starting that day):")
    print(gdf.groupby(gdf["start_date"].dt.strftime("%Y-%m-%d")).size().sort_values(ascending=False).head(15).to_string())
    print(f"\nSaved: {OUT_CSV.relative_to(ROOT)}, {OUT_GEO.relative_to(ROOT)}, {OUT_PAIRS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
