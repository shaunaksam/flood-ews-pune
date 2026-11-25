from pathlib import Path
import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, brier_score_loss

ROOT = Path(__file__).resolve().parent
P = ROOT / "data" / "processed"
MR = P / "maharain_circle_daily.csv"
FEAT = P / "om_multimodel_daily_features.csv"
EVENTS = P / "groundsource_pune_events.csv"
OUT_CV = P / "maharain_cv_predictions.csv"
OUT_MODEL = ROOT / "models" / "rain_prob_v3_circles.joblib"


def skill(y, pred):
    tp = int(((pred == 1) & (y == 1)).sum()); fp = int(((pred == 1) & (y == 0)).sum()); fn = int(((pred == 0) & (y == 1)).sum())
    return (tp / (tp + fn) if tp + fn else np.nan, fp / (tp + fp) if tp + fp else np.nan, tp / (tp + fp + fn) if tp + fp + fn else np.nan)


def unique_series(wide):
    seen, keep = set(), []
    for c in wide.columns:
        key = tuple(np.round(wide[c].fillna(-1).to_numpy(), 1))
        if key not in seen:
            seen.add(key)
            keep.append(c)
    return wide[keep]


def group_daily(wide):
    u = unique_series(wide)
    return pd.DataFrame({"mean": u.mean(axis=1, skipna=True), "max": u.max(axis=1, skipna=True), "n_series": u.notna().sum(axis=1)}), u


def main():
    mr = pd.read_csv(MR, parse_dates=["date"])
    mr["key"] = mr["taluka"].astype(str) + " | " + mr["circle"].astype(str)
    wide_all = mr.pivot_table(index="date", columns="key", values="rain_mm", aggfunc="max")
    tal = mr.drop_duplicates("key").set_index("key")["taluka"].astype(str).str.lower()
    groups = {
        "A Pune city taluka": [k for k in wide_all.columns if "pune city" in tal[k]],
        "B Pune city + Haveli": [k for k in wide_all.columns if "pune city" in tal[k] or "haveli" in tal[k]],
        "C whole Pune district": list(wide_all.columns),
    }
    print(f"Maharain days: {wide_all.index.nunique()} ({wide_all.index.min().date()} to {wide_all.index.max().date()}), circles: {wide_all.shape[1]}")
    daily = {}
    print("\nHow many independent gauges are there? (circles that share a station have identical readings)")
    for name, cols in groups.items():
        d, u = group_daily(wide_all[cols])
        daily[name] = d
        print(f"  {name}: {len(cols)} circles, {u.shape[1]} distinct reading series")
    hav = sorted(c.split(" | ")[1] for c in groups["B Pune city + Haveli"])
    print("  Pune city + Haveli circles:", ", ".join(hav))

    B = daily["B Pune city + Haveli"]
    print("\nHow local is heavy rain? Days when some gauge in Pune city + Haveli recorded 65 mm or more")
    _, uB = group_daily(wide_all[groups["B Pune city + Haveli"]])
    hv = uB[uB.max(axis=1) >= 65]
    if len(hv):
        f20 = (hv >= 20).sum(axis=1) / hv.notna().sum(axis=1)
        f65 = (hv >= 65).sum(axis=1) / hv.notna().sum(axis=1)
        print(f"  {len(hv)} such days. Share of gauges with 20 mm or more: median {f20.median():.0%} (middle half {f20.quantile(.25):.0%} to {f20.quantile(.75):.0%}); with 65 mm or more: median {f65.median():.0%}")
    else:
        print("  No such days in the data.")

    feat = pd.read_csv(FEAT, parse_dates=["day"])
    f1 = feat[feat["lead"] == 1]
    ref = "ecmwf_ifs025" if "ecmwf_ifs025" in f1["model"].unique() else f1["model"].iloc[0]
    best_s, best_r = 0, -2
    for s in (0, 1):
        g = f1[f1["model"] == ref].copy()
        g["date"] = g["day"] + pd.Timedelta(days=s)
        m = g.merge(B, left_on="date", right_index=True)
        r = m["local"].corr(m["mean"])
        print(f"Day alignment check ({ref}): shift {s} gives correlation {r:.3f}")
        if r > best_r:
            best_s, best_r = s, r
    print(f"Using shift {best_s}\n")
    feat["date"] = feat["day"] + pd.Timedelta(days=best_s)

    imd = feat.drop_duplicates("day")[["date", "obs_mean"]].merge(B, left_on="date", right_index=True)
    if len(imd) > 20:
        heavy = imd[imd["mean"] >= 20]
        print(f"IMD 0.25 degree grid vs Pune city gauges: correlation {imd['obs_mean'].corr(imd['mean']):.2f}; on days when the gauges averaged 20 mm or more, the grid showed {heavy['obs_mean'].sum() / heavy['mean'].sum():.2f} of the gauge amount ({len(heavy)} days)\n")

    rows = []
    for (m, lead), g in feat.groupby(["model", "lead"]):
        t = g.merge(B, left_on="date", right_index=True)
        if len(t) < 100:
            continue
        y = (t["mean"] >= 20).astype(int).to_numpy()
        if y.sum() < 5:
            continue
        pod, far, csi = skill(y, (t["local"] >= 20).astype(int).to_numpy())
        h = t[t["mean"] >= 20]
        rows.append({"model": m, "lead_days": lead, "days": len(t), "heavy_days": int(y.sum()), "corr": round(t["local"].corr(t["mean"]), 3),
                     "auc_20mm": round(roc_auc_score(y, t["local"]), 3), "POD_20": round(pod, 3), "FAR_20": round(far, 3), "CSI_20": round(csi, 3),
                     "forecast_vs_gauges_on_heavy_days": round(float(h["local"].sum() / h["mean"].sum()), 2)})
    print("Which model sees the heavy rain recorded by Pune city and Haveli gauges?")
    print(pd.DataFrame(rows).sort_values(["lead_days", "auc_20mm"], ascending=[True, False]).to_string(index=False))

    d1 = feat[feat["lead"] == 1]
    total = d1["date"].nunique()
    good = [m for m in d1["model"].unique() if d1[d1["model"] == m]["date"].nunique() >= 0.85 * total]
    loc = d1[d1["model"].isin(good)].pivot(index="date", columns="model", values="local").dropna()
    wst = d1[d1["model"].isin(good)].pivot(index="date", columns="model", values="west").dropna()
    idx = loc.index.intersection(wst.index)
    loc, wst = loc.loc[idx], wst.loc[idx]
    base = "ecmwf_ifs025" if "ecmwf_ifs025" in good else good[0]
    X0 = pd.DataFrame({"mean_local": loc.mean(axis=1), "max_local": loc.max(axis=1), "std_local": loc.std(axis=1, ddof=0), "mean_west": wst.mean(axis=1),
                       "max_west": wst.max(axis=1), "frac_ge10": (loc >= 10).mean(axis=1), "base_local": loc[base], "base_west": wst[base]})
    data = X0.join(B, how="inner").reset_index().rename(columns={"index": "date"})
    data = data.rename(columns={"mean": "g_mean", "max": "g_max"})
    sets = {"A single model": ["base_local", "base_west"], "B model average": ["mean_local", "mean_west"],
            "C average, highest and disagreement": ["mean_local", "max_local", "std_local", "mean_west", "max_west", "frac_ge10"]}
    targets = {"gauge_mean_ge_20mm": ("g_mean", 20.0), "any_gauge_ge_65mm": ("g_max", 65.0)}
    grp = data["date"].dt.strftime("%Y-%m").to_numpy()
    res, cv, fits = [], data[["date", "g_mean", "g_max"]].copy(), {}
    for tname, (col, thr) in targets.items():
        y = (data[col] >= thr).astype(int).to_numpy()
        if y.sum() < 8:
            print(f"\n{tname}: skipped, only {int(y.sum())} positive days")
            continue
        for sname, cols in sets.items():
            X = np.log1p(data[cols].clip(lower=0).to_numpy())
            oof = np.zeros(len(y))
            for tr, te in GroupKFold(n_splits=min(6, len(set(grp)))).split(X, y, grp):
                if len(set(y[tr])) < 2:
                    oof[te] = y[tr].mean()
                    continue
                oof[te] = LogisticRegression(max_iter=3000).fit(X[tr], y[tr]).predict_proba(X[te])[:, 1]
            pod, far, csi = skill(y, (oof >= 0.30).astype(int))
            res.append({"target": tname, "inputs": sname, "days": len(y), "positive_days": int(y.sum()), "auc": round(roc_auc_score(y, oof), 3),
                        "brier": round(brier_score_loss(y, oof), 4), "brier_always_average": round(brier_score_loss(y, np.full(len(y), y.mean())), 4),
                        "POD_30": round(pod, 3), "FAR_30": round(far, 3), "CSI_30": round(csi, 3)})
            cv[f"p_{tname}_{sname[0]}"] = oof
            fits[(tname, sname)] = (LogisticRegression(max_iter=3000).fit(X, y), cols)
    r = pd.DataFrame(res)
    print("\nProbability of heavy rain, trained and tested on Pune city + Haveli gauges (each month held out)")
    print(r.to_string(index=False))
    cv.to_csv(OUT_CV, index=False)
    t1 = r[r["target"] == "gauge_mean_ge_20mm"].sort_values("auc", ascending=False).head(1)
    if len(t1):
        sname = t1.iloc[0]["inputs"]
        y = (data["g_mean"] >= 20).astype(int).to_numpy()
        p = cv[f"p_gauge_mean_ge_20mm_{sname[0]}"].to_numpy()
        bands = pd.cut(p, [-0.01, 0.10, 0.25, 0.50, 1.01], labels=["under 10%", "10-25%", "25-50%", "50%+"])
        cal = pd.DataFrame({"band": bands, "y": y}).groupby("band", observed=True)["y"].agg(days="size", heavy_days="sum").reset_index()
        cal["share_pct"] = (100 * cal["heavy_days"] / cal["days"]).round(1)
        print(f"\nCalibration of '{sname}' (did the gauges record 20 mm or more?)")
        print(cal.to_string(index=False))
        if EVENTS.exists():
            ev = pd.read_csv(EVENTS, parse_dates=["start_date"])
            ev = ev[(ev["area_km2"] <= 25) & (ev["n_wards_touched"] >= 1)]
            cnt = ev.groupby(ev["start_date"].dt.normalize()).size()
            lab = (cv["date"].map(cnt).fillna(0) >= 3).astype(int)
            near = (cv["date"].map(cnt).reindex(cv.index).fillna(0) >= 3)
            if lab.nunique() == 2:
                print(f"\nAgainst news-reported flood days: AUC {roc_auc_score(lab, p):.3f} ({int(lab.sum())} report days of {len(lab)})")
        bundle = {"models_used": good, "alignment_shift": best_s, "inputs": sname, "truth": "Pune city + Haveli gauges (Maharain)", "trained_days": int(len(y)),
                  "date_range": [str(data["date"].min().date()), str(data["date"].max().date())], "fits": {}}
        for tname in targets:
            if (tname, sname) in fits:
                mdl, cols = fits[(tname, sname)]
                bundle["fits"][tname] = {"model": mdl, "features": cols}
        OUT_MODEL.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, OUT_MODEL)
        print(f"\nSaved models/rain_prob_v3_circles.joblib (not used by the live site) and {OUT_CV.name}")


if __name__ == "__main__":
    main()
