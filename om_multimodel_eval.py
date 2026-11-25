import os
import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
import requests
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, brier_score_loss

ROOT = Path(__file__).resolve().parent
P = ROOT / "data" / "processed"
IMD = P / "imd_pune_3x3_grid_daily_2021_2025.csv"
EVENTS = P / "groundsource_pune_events.csv"
CACHE = P / "om_prev_runs_cache"
OUT_SKILL = P / "om_multimodel_model_skill.csv"
OUT_DAILY = P / "om_multimodel_daily_features.csv"
OUT_CV = P / "om_v2_cv_predictions.csv"
OUT_MODEL = ROOT / "models" / "rain_prob_v2.joblib"
URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
DEFAULT_MODELS = "ecmwf_ifs,ecmwf_ifs025,ecmwf_aifs025_single,gfs_seamless,icon_seamless,gem_global,meteofrance_arpege_world,ukmo_global_deterministic_10km,cma_grapes_global,jma_gsm"
MODELS = [m.strip() for m in os.getenv("OM_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
START = os.getenv("OM_START", "2024-01-01")
END = os.getenv("OM_END", "2025-12-31")
PAUSE = float(os.getenv("OM_PAUSE", "1.2"))
CASE_START, CASE_END, CASE_DAY = "2026-10-30", "2026-11-03", "2026-11-02"
CELLS = [(lat, lon) for lat in (18.25, 18.5, 18.75) for lon in (73.5, 73.75, 74.0)]
LEADS = (1, 2)


def fetch(model, start, end):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{model}_{start}_{end}.json"
    if f.exists():
        return json.loads(f.read_text())
    params = {
        "latitude": ",".join(str(c[0]) for c in CELLS),
        "longitude": ",".join(str(c[1]) for c in CELLS),
        "hourly": ",".join(f"precipitation_previous_day{n}" for n in LEADS),
        "models": model, "start_date": start, "end_date": end, "timezone": "GMT",
    }
    for attempt in range(6):
        try:
            r = requests.get(URL, params=params, timeout=120)
        except Exception as exc:
            print(f"  network problem ({type(exc).__name__}), retrying")
            time.sleep(10)
            continue
        if r.status_code == 200:
            data = r.json()
            data = data if isinstance(data, list) else [data]
            f.write_text(json.dumps(data))
            time.sleep(PAUSE)
            return data
        if r.status_code == 429:
            print("  rate limit, waiting")
            time.sleep(40 * (attempt + 1))
            continue
        if r.status_code in (400, 404):
            print(f"  {model}: not available for {start} to {end} ({r.text[:90]})")
            return None
        time.sleep(10)
    return None


def to_frame(resp, model):
    rows = []
    for (lat, lon), loc in zip(CELLS, resp):
        h = loc.get("hourly") or {}
        if "time" not in h:
            continue
        t = pd.to_datetime(h["time"])
        for lead in LEADS:
            key = next((k for k in h if k.startswith(f"precipitation_previous_day{lead}")), None)
            if key is not None:
                rows.append(pd.DataFrame({"t": t, "lat": lat, "lon": lon, "lead": lead, "model": model, "mm": pd.to_numeric(pd.Series(h[key]), errors="coerce").to_numpy()}))
    return pd.concat(rows, ignore_index=True) if rows else None


def chunks(start, end, days=31):
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    while s <= e:
        c = min(s + pd.Timedelta(days=days - 1), e)
        yield s.strftime("%Y-%m-%d"), c.strftime("%Y-%m-%d")
        s = c + pd.Timedelta(days=1)


def daily_cells(hourly, shift):
    d = hourly.dropna(subset=["mm"]).copy()
    d["day"] = (d["t"] - pd.Timedelta(hours=3)).dt.normalize() + pd.Timedelta(days=shift)
    g = d.groupby(["model", "lead", "lat", "lon", "day"])["mm"].agg(["sum", "count"]).reset_index()
    return g[g["count"] == 24].rename(columns={"sum": "mm"}).drop(columns="count")


def regional(dc):
    west = dc[dc["lon"] <= 73.55].groupby(["model", "lead", "day"])["mm"].max().rename("west")
    g = dc.groupby(["model", "lead", "day"])["mm"].agg(local="mean", maxcell="max")
    return g.join(west).reset_index()


def load_obs():
    g = pd.read_csv(IMD, parse_dates=["date"])
    west = g[g["longitude"] <= 73.55].groupby("date")["rainfall_mm"].max().rename("obs_west")
    o = g.groupby("date")["rainfall_mm"].agg(obs_mean="mean", obs_max="max").join(west).reset_index().rename(columns={"date": "day"})
    return o


def skill(y, pred):
    tp = int(((pred == 1) & (y == 1)).sum()); fp = int(((pred == 1) & (y == 0)).sum()); fn = int(((pred == 0) & (y == 1)).sum())
    return (tp / (tp + fn) if tp + fn else np.nan, fp / (tp + fp) if tp + fp else np.nan, tp / (tp + fp + fn) if tp + fp + fn else np.nan)


def main():
    obs = load_obs()
    obs = obs[(obs["day"] >= START) & (obs["day"] <= END)]
    print(f"Models: {', '.join(MODELS)}\nPeriod: {START} to {END}, {len(obs)} observed days\n")
    frames = {}
    for m in MODELS:
        parts = []
        print(f"Fetching {m} ...")
        for s, e in chunks(START, END):
            resp = fetch(m, s, e)
            if resp is None:
                parts = []
                break
            fr = to_frame(resp, m)
            if fr is not None:
                parts.append(fr)
        if parts:
            frames[m] = pd.concat(parts, ignore_index=True)
    if not frames:
        print("No model data could be fetched. Check your internet connection and try again.")
        return
    ref = "ecmwf_ifs" if "ecmwf_ifs" in frames else next(iter(frames))
    best_shift, best_r = 1, -2
    for sh in (0, 1):
        rg = regional(daily_cells(frames[ref][frames[ref]["lead"] == 1], sh))
        mm = rg.merge(obs, on="day")
        r = mm["local"].corr(mm["obs_mean"])
        print(f"Day-alignment check ({ref}): shift {sh} gives correlation {r:.3f}")
        if r > best_r:
            best_shift, best_r = sh, r
    print(f"Using shift {best_shift} (24 h ending 08:30 IST = shift 1)\n")
    reg = pd.concat([regional(daily_cells(fr, best_shift)) for fr in frames.values()], ignore_index=True)
    daily = reg.merge(obs, on="day")
    daily.to_csv(OUT_DAILY, index=False)

    rows = []
    for (m, lead), g in daily.groupby(["model", "lead"]):
        if len(g) < 100:
            continue
        y = (g["obs_mean"] >= 20).astype(int).to_numpy()
        if y.sum() < 5:
            continue
        pod, far, csi = skill(y, (g["local"] >= 20).astype(int).to_numpy())
        heavy = g[g["obs_mean"] >= 20]
        rows.append({"model": m, "lead_days": lead, "days": len(g), "heavy_days": int(y.sum()), "corr_regional": round(g["local"].corr(g["obs_mean"]), 3),
                     "auc_20mm": round(roc_auc_score(y, g["local"]), 3), "POD_20": round(pod, 3), "FAR_20": round(far, 3), "CSI_20": round(csi, 3),
                     "forecast_vs_observed_on_heavy_days": round(float(heavy["local"].sum() / heavy["obs_mean"].sum()), 2)})
    skill_df = pd.DataFrame(rows).sort_values(["lead_days", "auc_20mm"], ascending=[True, False])
    print("Which model sees Pune's heavy rain best? (regional average, forecast issued 1 or 2 days ahead)")
    print(skill_df.to_string(index=False))
    skill_df.to_csv(OUT_SKILL, index=False)

    d1 = daily[daily["lead"] == 1]
    total_days = d1["day"].nunique()
    good = [m for m in d1["model"].unique() if d1[d1["model"] == m]["day"].nunique() >= 0.85 * total_days]
    print(f"\nModels used in the ensemble (full coverage): {', '.join(good)}")
    if len(good) >= 2:
        loc = d1[d1["model"].isin(good)].pivot(index="day", columns="model", values="local")
        wst = d1[d1["model"].isin(good)].pivot(index="day", columns="model", values="west")
        loc, wst = loc.dropna(), wst.dropna()
        idx = loc.index.intersection(wst.index)
        loc, wst = loc.loc[idx], wst.loc[idx]
        feats = pd.DataFrame({"mean_local": loc.mean(axis=1), "max_local": loc.max(axis=1), "std_local": loc.std(axis=1, ddof=0),
                              "mean_west": wst.mean(axis=1), "max_west": wst.max(axis=1), "frac_ge10": (loc >= 10).mean(axis=1)})
        base_model = "ecmwf_ifs" if "ecmwf_ifs" in good else good[0]
        feats["base_local"] = loc[base_model]
        feats["base_west"] = wst[base_model]
        data = feats.join(obs.set_index("day")[["obs_mean", "obs_max"]], how="inner").reset_index()
        sets = {
            "A single model (as today)": ["base_local", "base_west"],
            "B model average": ["mean_local", "mean_west"],
            "C average, highest and disagreement": ["mean_local", "max_local", "std_local", "mean_west", "max_west", "frac_ge10"],
        }
        groups = data["day"].dt.strftime("%Y-%m").to_numpy()
        targets = {"regional_mean_ge_20mm": ("obs_mean", 20.0), "any_cell_ge_64_5mm": ("obs_max", 64.5)}
        res, cvout, fits = [], data[["day", "obs_mean", "obs_max"]].copy(), {}
        for tname, (col, thr) in targets.items():
            y = (data[col] >= thr).astype(int).to_numpy()
            if y.sum() < 8:
                print(f"{tname}: skipped, only {int(y.sum())} positive days")
                continue
            for sname, cols in sets.items():
                X = np.log1p(data[cols].clip(lower=0).to_numpy())
                oof = np.zeros(len(y))
                for tr, te in GroupKFold(n_splits=min(6, len(set(groups)))).split(X, y, groups):
                    if len(set(y[tr])) < 2:
                        oof[te] = y[tr].mean()
                        continue
                    oof[te] = LogisticRegression(max_iter=3000).fit(X[tr], y[tr]).predict_proba(X[te])[:, 1]
                pod, far, csi = skill(y, (oof >= 0.30).astype(int))
                res.append({"target": tname, "inputs": sname, "days": len(y), "positive_days": int(y.sum()), "auc": round(roc_auc_score(y, oof), 3),
                            "brier": round(brier_score_loss(y, oof), 4), "brier_always_average": round(brier_score_loss(y, np.full(len(y), y.mean())), 4),
                            "POD_at_30pct": round(pod, 3), "FAR_at_30pct": round(far, 3), "CSI_at_30pct": round(csi, 3)})
                cvout[f"p_{tname}_{sname[0]}"] = oof
                fits[(tname, sname)] = (LogisticRegression(max_iter=3000).fit(X, y), cols)
        r = pd.DataFrame(res)
        print("\nProbability of heavy rain: single model vs several models (each month held out)")
        print(r.to_string(index=False))
        cvout.to_csv(OUT_CV, index=False)
        if EVENTS.exists():
            ev = pd.read_csv(EVENTS, parse_dates=["start_date"])
            ev = ev[(ev["area_km2"] <= 25) & (ev["n_wards_touched"] >= 1)]
            cnt = ev.groupby(ev["start_date"].dt.normalize()).size()
            lab = (cvout["day"].map(cnt).fillna(0) >= 3).astype(int)
            if lab.nunique() == 2:
                print("\nAgainst news-reported flood days (3+ local reports):")
                for c in [c for c in cvout.columns if c.startswith("p_regional_mean_ge_20mm_")]:
                    print(f"  {c[-1]}: AUC {roc_auc_score(lab, cvout[c]):.3f}")
        best = r[r["target"] == "regional_mean_ge_20mm"].sort_values("auc", ascending=False).head(1)
        if len(best):
            sname = best.iloc[0]["inputs"]
            OUT_MODEL.parent.mkdir(parents=True, exist_ok=True)
            bundle = {"models_used": good, "alignment_shift": best_shift, "inputs": sname, "fits": {}}
            for tname in targets:
                if (tname, sname) in fits:
                    mdl, cols = fits[(tname, sname)]
                    bundle["fits"][tname] = {"model": mdl, "features": cols}
            joblib.dump(bundle, OUT_MODEL)
            print(f"\nBest inputs for the 20 mm target: {sname}. Saved models/rain_prob_v2.joblib (not used by the live site yet).")

    print("\nCase study: what did each model forecast for 2 November 2026? (IMD recorded 41.1 mm at Shivajinagar)")
    case = []
    for m in frames:
        resp = fetch(m, CASE_START, CASE_END)
        fr = to_frame(resp, m) if resp else None
        if fr is None:
            continue
        rg = regional(daily_cells(fr, best_shift))
        rg = rg[rg["day"] == CASE_DAY]
        for _, row in rg.iterrows():
            case.append({"model": m, "lead_days": int(row["lead"]), "regional_avg_mm": round(row["local"], 1), "highest_cell_mm": round(row["maxcell"], 1)})
    if case:
        print(pd.DataFrame(case).sort_values(["lead_days", "regional_avg_mm"], ascending=[True, False]).to_string(index=False))
    else:
        print("No case-study data returned.")
    print(f"\nSaved: {OUT_SKILL.name}, {OUT_DAILY.name}, {OUT_CV.name}")


if __name__ == "__main__":
    main()
