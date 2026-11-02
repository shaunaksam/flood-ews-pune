from pathlib import Path
import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, average_precision_score, brier_score_loss

ROOT = Path(__file__).resolve().parent
P = ROOT / "data" / "processed"
EVENTS = P / "groundsource_pune_events.csv"
PAIRS = P / "groundsource_pune_ward_event_pairs.csv"
IMD = P / "imd_pune_3x3_grid_daily_2021_2025.csv"
WARD_STATIC = P / "ward_validation.csv"
OUT_METRICS = P / "ward_model_metrics_v1.csv"
OUT_COEF = P / "ward_model_coefficients_v1.csv"
OUT_INDEX = P / "ward_learned_index_v1.csv"
OUT_MODEL = ROOT / "models" / "ward_report_v1.joblib"
START = "2022-01-01"
RNG = np.random.default_rng(11)


def build():
    ev = pd.read_csv(EVENTS, parse_dates=["start_date"])
    ev = ev[(ev["area_km2"] <= 25) & (ev["start_date"] >= START)]
    pairs = pd.read_csv(PAIRS)
    pairs = pairs[pairs["uuid"].isin(set(ev["uuid"]))].merge(ev[["uuid", "start_date"]].drop_duplicates("uuid"), on="uuid", how="inner", suffixes=("_p", ""))
    pairs["date"] = pairs["start_date"].dt.normalize()
    pos = pairs.drop_duplicates(["date", "wardnum"])[["date", "wardnum"]].assign(y=1)
    g = pd.read_csv(IMD, parse_dates=["date"])
    rain = g.groupby("date")["rainfall_mm"].agg(obs_mean="mean", obs_max="max").reset_index()
    rain = rain[rain["date"] >= START].reset_index(drop=True)
    st = pd.read_csv(WARD_STATIC)[["wardnum", "name", "km2", "vuln", "terrain", "river"]].dropna().reset_index(drop=True)
    grid = rain.assign(k=1).merge(st.assign(k=1), on="k").drop(columns="k")
    grid = grid.merge(pos, on=["date", "wardnum"], how="left")
    grid["y"] = grid["y"].fillna(0).astype(int)
    grid["rm"] = np.log1p(grid["obs_mean"].clip(lower=0))
    grid["rx"] = np.log1p(grid["obs_max"].clip(lower=0))
    grid["larea"] = np.log(grid["km2"].clip(lower=0.05))
    yr = grid["date"].dt.year
    for y in (2023, 2024, 2025):
        grid[f"y{y}"] = (yr == y).astype(int)
    for c in ("vuln", "terrain", "river", "larea"):
        grid[f"rm_{c}"] = grid["rm"] * grid[c]
    return grid, st


FEATS = {
    "M0 rain only (no ward info)": ["rm", "rx", "y2023", "y2024", "y2025"],
    "M1 rain + hand-set vulnerability": ["rm", "rx", "vuln", "rm_vuln", "y2023", "y2024", "y2025"],
    "M2 rain + learned weights (logistic)": ["rm", "rx", "vuln", "terrain", "river", "larea", "rm_vuln", "rm_terrain", "rm_river", "rm_larea", "y2023", "y2024", "y2025"],
    "M3 rain + learned weights (boosted trees)": ["rm", "rx", "vuln", "terrain", "river", "larea", "y2023", "y2024", "y2025"],
}


def make(name):
    if name.startswith("M3"):
        return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.06, max_iter=200, random_state=0)
    return make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000, C=1.0))


def hit_at(df, col, k, wet_only=False):
    out = []
    for d, g in df.groupby("date"):
        if g["y"].sum() == 0:
            continue
        if wet_only and g["obs_mean"].iloc[0] < 10:
            continue
        top = g.nlargest(k, col)
        out.append(top["y"].sum() / g["y"].sum())
    return (float(np.mean(out)) if out else float("nan")), len(out)


def main():
    grid, st = build()
    wards = grid["wardnum"].nunique()
    days = grid["date"].nunique()
    print(f"Ward-days: {len(grid):,}  ({wards} wards x {days} days, from {START})   ward-days with a reported local flood: {int(grid['y'].sum()):,} ({100 * grid['y'].mean():.1f}%)")
    groups = grid["date"].dt.strftime("%Y-%m").to_numpy()
    y = grid["y"].to_numpy()
    n_splits = min(6, len(set(groups)))
    rows = []
    oof_all = {}
    for name, cols in FEATS.items():
        X = grid[cols].to_numpy(dtype=float)
        oof = np.zeros(len(grid))
        for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
            m = make(name).fit(X[tr], y[tr])
            oof[te] = m.predict_proba(X[te])[:, 1]
        oof_all[name] = oof
        tmp = grid[["date", "wardnum", "y", "obs_mean"]].copy()
        tmp["s"] = oof + RNG.uniform(0, 1e-9, len(oof))
        h10, nd = hit_at(tmp, "s", 10)
        h10w, ndw = hit_at(tmp, "s", 10, wet_only=True)
        rows.append({
            "model": name,
            "auc": round(roc_auc_score(y, oof), 3),
            "avg_precision": round(average_precision_score(y, oof), 3),
            "brier": round(brier_score_loss(y, oof), 4),
            "wards_found_in_top10_pct": round(100 * h10, 1),
            "same_on_wet_days_pct": round(100 * h10w, 1),
        })
    base_rate = brier_score_loss(y, np.full(len(y), y.mean()))
    r = pd.DataFrame(rows)
    print(f"\nWard-level results (each month held out in turn; Brier for always guessing the average = {base_rate:.4f}; random top-10 would find about {100 * 10 / wards:.0f}% of reported wards):")
    print(r.to_string(index=False))
    r.to_csv(OUT_METRICS, index=False)

    cols2 = FEATS["M2 rain + learned weights (logistic)"]
    X2 = grid[cols2].to_numpy(dtype=float)
    m2 = make("M2").fit(X2, y)
    coefs = pd.DataFrame({"feature": cols2, "standardized_weight": m2.named_steps["logisticregression"].coef_[0]})
    coefs["abs"] = coefs["standardized_weight"].abs()
    coefs = coefs.sort_values("abs", ascending=False).drop(columns="abs").round(3)
    print("\nWhat the logistic model learned (positive = more reports; sizes are comparable):")
    print(coefs.to_string(index=False))
    coefs.to_csv(OUT_COEF, index=False)

    cols3 = FEATS["M3 rain + learned weights (boosted trees)"]
    m3 = make("M3").fit(grid[cols3].to_numpy(dtype=float), y)

    med_area = float(np.log(st["km2"].clip(lower=0.05)).median())
    std = st.copy()
    std["rm"] = np.log1p(30.0); std["rx"] = np.log1p(80.0)
    std["larea"] = med_area
    std["y2023"] = 0; std["y2024"] = 0; std["y2025"] = 1
    for c in ("vuln", "terrain", "river", "larea"):
        std[f"rm_{c}"] = std["rm"] * std[c]
    std["learned_probability_on_a_30mm_day"] = m2.predict_proba(std[cols2].to_numpy(dtype=float))[:, 1]
    std["learned_rank_pct"] = std["learned_probability_on_a_30mm_day"].rank(pct=True).round(3)
    std["hand_rank_pct"] = std["vuln"].rank(pct=True).round(3)
    rho = std["learned_rank_pct"].corr(std["hand_rank_pct"], method="spearman")
    std[["wardnum", "name", "km2", "vuln", "terrain", "river", "learned_probability_on_a_30mm_day", "learned_rank_pct", "hand_rank_pct"]].round(4).to_csv(OUT_INDEX, index=False)
    print(f"\nLearned ward index vs your hand-set vulnerability: rank correlation {rho:.2f}")
    top = std.sort_values("learned_probability_on_a_30mm_day", ascending=False).head(10)[["name", "km2", "vuln", "terrain", "river", "learned_probability_on_a_30mm_day"]].round(3)
    print("\nTen wards the learned model rates most report-prone on a 30 mm day (ward size held constant):")
    print(top.to_string(index=False))

    OUT_MODEL.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "logistic": m2, "boosted": m3, "logistic_features": cols2, "boosted_features": cols3,
        "trained_from": START, "ward_static": st.to_dict(orient="records"),
        "note": "Trained on observed IMD rain and news-reported local floods (Groundsource). Year dummies absorb reporting growth; inference uses the 2025 level.",
    }, OUT_MODEL)
    print(f"\nSaved: {OUT_MODEL.relative_to(ROOT)}, {OUT_INDEX.relative_to(ROOT)}, {OUT_METRICS.relative_to(ROOT)}, {OUT_COEF.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
