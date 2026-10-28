from pathlib import Path
import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, brier_score_loss

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "data" / "processed" / "forecast_validation" / "openmeteo_ifs_paired_observed_forecast.csv"
OUT_MODEL = ROOT / "models" / "rain_prob_v1.joblib"
OUT_METRICS = ROOT / "data" / "processed" / "rain_model_metrics_v1.csv"
OUT_CV = ROOT / "data" / "processed" / "rain_model_cv_predictions_v1.csv"
FEATURES = ["local24_mm", "upstream24_mm"]
TARGETS = {
    "regional_mean_ge_20mm": ("obs_mean", 20.0, "local24_mm"),
    "regional_mean_ge_50mm": ("obs_mean", 50.0, "local24_mm"),
    "any_cell_ge_64_5mm": ("obs_max", 64.5, "upstream24_mm"),
}
EVENT_DAYS = ["2024-07-25", "2024-08-25", "2025-08-20"]
CUTOFF = 0.30


def build_daily():
    df = pd.read_csv(SRC, parse_dates=["date"])
    df = df.dropna(subset=["forecast_imd_day_approx_mm", "rainfall_mm"])
    g = df.groupby("date")
    daily = pd.DataFrame({
        "n_cells": g.size(),
        "local24_mm": g["forecast_imd_day_approx_mm"].mean(),
        "obs_mean": g["rainfall_mm"].mean(),
        "obs_max": g["rainfall_mm"].max(),
    })
    west = df[df["grid_longitude"] <= 73.55].groupby("date")["forecast_imd_day_approx_mm"].max()
    daily["upstream24_mm"] = west
    daily = daily[daily["n_cells"] == 9].dropna().reset_index()
    return daily


def make_model():
    return LogisticRegression(C=1.0, max_iter=2000)


def transform(frame):
    return np.log1p(frame[FEATURES].clip(lower=0).to_numpy(dtype=float))


def skill(y, pred):
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    pod = tp / (tp + fn) if tp + fn else float("nan")
    far = fp / (tp + fp) if tp + fp else float("nan")
    csi = tp / (tp + fp + fn) if tp + fp + fn else float("nan")
    return pod, far, csi


def main():
    daily = build_daily()
    X = transform(daily)
    groups = daily["date"].dt.strftime("%Y-%m").to_numpy()
    n_splits = min(5, len(set(groups)))
    print(f"Days used: {len(daily)}  ({daily['date'].min().date()} to {daily['date'].max().date()})")
    rows, bundle_models = [], {}
    cv_out = daily[["date", "local24_mm", "upstream24_mm", "obs_mean", "obs_max"]].copy()
    for name, (col, thr, rule_feature) in TARGETS.items():
        y = (daily[col] >= thr).astype(int).to_numpy()
        pos = int(y.sum())
        if pos < 8 or len(y) - pos < 8:
            print(f"{name}: skipped (only {pos} positive days of {len(y)})")
            continue
        oof = np.zeros(len(y))
        for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
            if len(set(y[tr])) < 2:
                oof[te] = y[tr].mean()
                continue
            m = make_model().fit(X[tr], y[tr])
            oof[te] = m.predict_proba(X[te])[:, 1]
        rule_pred = (daily[rule_feature].to_numpy() >= thr).astype(int)
        model_pred = (oof >= CUTOFF).astype(int)
        r_pod, r_far, r_csi = skill(y, rule_pred)
        m_pod, m_far, m_csi = skill(y, model_pred)
        rows.append({
            "target": name, "days": len(y), "positive_days": pos,
            "auc": round(roc_auc_score(y, oof), 3),
            "brier_model": round(brier_score_loss(y, oof), 4),
            "brier_always_climatology": round(brier_score_loss(y, np.full(len(y), y.mean())), 4),
            "rule_pod": round(r_pod, 3), "rule_far": round(r_far, 3), "rule_csi": round(r_csi, 3),
            "model_pod": round(m_pod, 3), "model_far": round(m_far, 3), "model_csi": round(m_csi, 3),
        })
        cv_out[f"p_{name}"] = oof
        bundle_models[name] = make_model().fit(X, y)
    if not rows:
        print("No target had enough positive days; nothing saved.")
        return
    metrics = pd.DataFrame(rows)
    OUT_MODEL.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "models": bundle_models,
        "features": FEATURES,
        "targets": {k: {"column": v[0], "threshold_mm": v[1]} for k, v in TARGETS.items() if k in bundle_models},
        "source": "Archived ECMWF IFS forecasts (about 2-day lead) vs IMD gridded rainfall, Pune region",
        "trained_days": len(daily),
        "date_range": [str(daily["date"].min().date()), str(daily["date"].max().date())],
        "cv_cutoff_used_for_skill": CUTOFF,
    }, OUT_MODEL)
    metrics.to_csv(OUT_METRICS, index=False)
    cv_out.to_csv(OUT_CV, index=False)
    print("\nCross-validated results (each month held out in turn):")
    print(metrics.to_string(index=False))
    print("\nOut-of-fold probabilities on documented event days:")
    ev = cv_out[cv_out["date"].dt.strftime("%Y-%m-%d").isin(EVENT_DAYS)]
    pcols = [c for c in ev.columns if c.startswith("p_")]
    if len(ev):
        show = ev[["date", "local24_mm", "upstream24_mm", "obs_mean", "obs_max"] + pcols].copy()
        for c in pcols:
            show[c] = (show[c] * 100).round(0).astype(int).astype(str) + "%"
        show = show.round({"local24_mm": 1, "upstream24_mm": 1, "obs_mean": 1, "obs_max": 1})
        show["date"] = show["date"].dt.strftime("%Y-%m-%d")
        print(show.to_string(index=False))
    else:
        print("Event days not in dataset.")
    print("\nFile headers (for the next step):")
    for rel in ["imd_pune_3x3_grid_daily_2021_2025.csv", "kumar_station_rainfall_2026_clean.csv"]:
        p = ROOT / "data" / "processed" / rel
        if p.exists():
            h = pd.read_csv(p, nrows=2)
            print(rel, "->", list(h.columns))
            print(h.to_string(index=False))
    print(f"\nSaved: {OUT_MODEL.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
