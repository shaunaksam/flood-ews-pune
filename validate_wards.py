import os
import json
import urllib.request
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parent
P = ROOT / "data" / "processed"
EVENTS = P / "groundsource_pune_events.csv"
PAIRS = P / "groundsource_pune_ward_event_pairs.csv"
WARDS = ROOT / "data" / "raw" / "pune_wards.geojson"
OUT = P / "ward_validation.csv"
URL = os.environ.get("SCORES_URL", "http://127.0.0.1:8001/ml/map/scores")
WARD_KEYS = ["wardnum", "ward_num", "WARDNUM", "ward_no", "Ward_No", "prabhag", "ward", "id"]


def main():
    try:
        with urllib.request.urlopen(URL, timeout=60) as r:
            scores = json.load(r)["scores"]
    except Exception as exc:
        print(f"Could not read ward scores from {URL} ({type(exc).__name__}). Start the server first, then run again.")
        return
    ev = pd.read_csv(EVENTS, parse_dates=["start_date"])
    ev = ev[(ev["area_km2"] <= 25) & (ev["start_date"].dt.year >= 2022)]
    pairs = pd.read_csv(PAIRS)
    pairs = pairs[pairs["uuid"].isin(set(ev["uuid"]))]
    counts = pairs.groupby("wardnum").size().rename("events")
    w = gpd.read_file(WARDS)
    w = w.set_crs(epsg=4326) if w.crs is None else w.to_crs(epsg=4326)
    key = next((k for k in WARD_KEYS if k in w.columns), None)
    w["wardnum"] = pd.to_numeric(w[key], errors="coerce") if key else np.arange(1, len(w) + 1)
    if w["wardnum"].isna().any():
        w["wardnum"] = np.arange(1, len(w) + 1)
    w["km2"] = w.to_crs(epsg=32643).area / 1e6
    df = w[["wardnum", "km2"]].merge(counts, left_on="wardnum", right_index=True, how="left")
    df["events"] = df["events"].fillna(0).astype(int)
    s = pd.DataFrame([{"wardnum": int(k), "name": v["n"], "vuln": v["v"], "terrain": v["u"], "river": v["r"], "past_link": bool(v["c"])} for k, v in scores.items()])
    df = df.merge(s, on="wardnum", how="inner")
    if len(df) < 40:
        print(f"Warning: only {len(df)} wards matched between the geojson and the app. Ward numbering may differ; results are unreliable.")
    df["per_km2"] = df["events"] / df["km2"]
    df["combined"] = df[["vuln", "terrain", "river"]].rank(pct=True).mean(axis=1)
    print(f"Wards compared: {len(df)}   local flood reports since 2022 touching them: {int(df['events'].sum())}\n")
    rows = []
    for col, label in [("vuln", "Built-in vulnerability"), ("terrain", "Waterlogging-prone terrain"), ("river", "River-corridor closeness"), ("combined", "Combined rank of the three")]:
        d = df.dropna(subset=[col])
        rho_c = spearmanr(d[col], d["events"]).correlation
        rho_d = spearmanr(d[col], d["per_km2"]).correlation
        top = d.nlargest(max(1, len(d) // 4), col)
        share_ev = 100 * top["events"].sum() / max(d["events"].sum(), 1)
        share_area = 100 * top["km2"].sum() / d["km2"].sum()
        rows.append({"measure": label, "rank_corr_with_report_count": round(rho_c, 2), "rank_corr_with_reports_per_km2": round(rho_d, 2), "top_quarter_wards_hold_pct_of_reports": round(share_ev, 1), "...while_covering_pct_of_area": round(share_area, 1)})
    r = pd.DataFrame(rows)
    print(r.to_string(index=False))
    f = df.groupby("past_link").agg(wards=("wardnum", "size"), reports_per_km2=("per_km2", "mean")).round(2).reset_index()
    print("\nWards linked to a past discharge event vs not:")
    print(f.to_string(index=False))
    show = df.sort_values("per_km2", ascending=False).head(10)[["name", "events", "km2", "per_km2", "vuln", "terrain", "river"]].round(2)
    print("\nTen wards with the most reports per km2:")
    print(show.to_string(index=False))
    df.round(4).to_csv(OUT, index=False)
    print(f"\nSaved: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
