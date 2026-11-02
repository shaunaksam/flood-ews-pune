from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, brier_score_loss

ROOT = Path(__file__).resolve().parent
P = ROOT / "data" / "processed"
EVENTS = P / "groundsource_pune_events.csv"
IMD = P / "imd_pune_3x3_grid_daily_2021_2025.csv"
CV = P / "rain_model_cv_predictions_v1.csv"
OUT = P / "groundsource_validation_tables.csv"
LOCAL_KM2 = 25.0
MIN_EVENTS = 3


def load_events():
    ev = pd.read_csv(EVENTS, parse_dates=["start_date"])
    ev = ev[(ev["area_km2"] <= LOCAL_KM2) & (ev["n_wards_touched"] >= 1)].copy()
    ev["day"] = ev["start_date"].dt.normalize()
    return ev.groupby("day").size().rename("n_events")


def load_rain():
    g = pd.read_csv(IMD, parse_dates=["date"])
    d = g.groupby("date")["rainfall_mm"].agg(obs_mean="mean", obs_max="max").reset_index()
    d = d.sort_values("date").reset_index(drop=True)
    d["rain2"] = d["obs_mean"].rolling(2, min_periods=1).sum()
    d["rain3"] = d["obs_mean"].rolling(3, min_periods=1).sum()
    return d


def tidy(df):
    return df.round(3).to_string(index=False)


def main():
    counts = load_events()
    rain = load_rain()
    days = rain.merge(counts, left_on="date", right_index=True, how="left")
    days["n_events"] = days["n_events"].fillna(0).astype(int)
    days["near"] = days["n_events"].rolling(3, center=True, min_periods=1).max()
    days["F"] = (days["n_events"] >= MIN_EVENTS).astype(int)
    days["Fnear"] = (days["near"] >= MIN_EVENTS).astype(int)
    days["year"] = days["date"].dt.year
    print(f"Days with rain data: {len(days)}   flood-report days (>= {MIN_EVENTS} local events): {int(days['F'].sum())}   within +-1 day: {int(days['Fnear'].sum())}")
    tables = []

    bins = [-0.01, 2, 5, 10, 20, 35, 1000]
    days["rain_band"] = pd.cut(days["obs_mean"], bins, labels=["<2", "2-5", "5-10", "10-20", "20-35", "35+"])
    a = days.groupby("rain_band", observed=True).agg(days=("F", "size"), report_days=("F", "sum"), near_days=("Fnear", "sum"), avg_events=("n_events", "mean")).reset_index()
    a["share_report_days_pct"] = (100 * a["report_days"] / a["days"]).round(1)
    a["share_near_pct"] = (100 * a["near_days"] / a["days"]).round(1)
    print("\nA. How often do news-reported local floods happen at each observed-rain level? (regional average mm/day)")
    print(tidy(a))
    a.insert(0, "table", "A_rain_vs_reports")
    tables.append(a)

    wet = days[days["obs_mean"] >= 20]
    y = days.groupby("year").agg(days=("F", "size"), report_days=("F", "sum"), events=("n_events", "sum")).reset_index()
    y["rainy_days_20mm"] = days[days["obs_mean"] >= 20].groupby("year").size().reindex(y["year"]).fillna(0).astype(int).to_numpy()
    hit_rainy = days[(days["obs_mean"] >= 20) & (days["F"] == 1)].groupby("year").size().reindex(y["year"]).fillna(0).to_numpy()
    y["reported_share_of_rainy_pct"] = np.round(100 * hit_rainy / np.where(y["rainy_days_20mm"].to_numpy() == 0, np.nan, y["rainy_days_20mm"].to_numpy()), 1)
    print("\nB. Reporting volume by year (news coverage grows over time, so early years under-count):")
    print(tidy(y))
    t = y.copy(); t.insert(0, "table", "B_by_year"); tables.append(t)

    if CV.exists():
        cv = pd.read_csv(CV, parse_dates=["date"])
        m = cv.merge(days[["date", "n_events", "F", "Fnear"]], on="date", how="inner")
        pcol = "p_regional_mean_ge_20mm"
        if pcol in m.columns and len(m) > 20:
            m["band"] = pd.cut(m[pcol], [-0.01, 0.10, 0.25, 0.50, 1.01], labels=["under 10%", "10-25%", "25-50%", "50%+"])
            c = m.groupby("band", observed=True).agg(days=("F", "size"), report_days=("F", "sum"), near_days=("Fnear", "sum"), avg_events=("n_events", "mean")).reset_index()
            c["share_report_days_pct"] = (100 * c["report_days"] / c["days"]).round(1)
            c["share_near_pct"] = (100 * c["near_days"] / c["days"]).round(1)
            print(f"\nC. Your live model's chance-of-heavy-rain vs news-reported floods ({len(m)} forecast days, month held out):")
            print(tidy(c))
            t = c.copy(); t.insert(0, "table", "C_model_vs_reports"); tables.append(t)
            rows = []
            for lab in ("F", "Fnear"):
                if m[lab].nunique() == 2:
                    rows.append({"label": lab, "auc_model": round(roc_auc_score(m[lab], m[pcol]), 3), "auc_raw_forecast_24h": round(roc_auc_score(m[lab], m["local24_mm"]), 3)})
            for cut in (0.25, 0.50):
                pred = (m[pcol] >= cut).astype(int)
                for lab in ("F", "Fnear"):
                    tp = int(((pred == 1) & (m[lab] == 1)).sum()); fp = int(((pred == 1) & (m[lab] == 0)).sum()); fn = int(((pred == 0) & (m[lab] == 1)).sum())
                    rows.append({"label": lab, "alert_at_chance": cut, "hits": tp, "false_alarms": fp, "misses": fn, "POD": round(tp / (tp + fn), 3) if tp + fn else None, "FAR": round(fp / (tp + fp), 3) if tp + fp else None})
            r = pd.DataFrame(rows)
            print("\nD. Alert skill against news-reported flood days (forecast period only):")
            print(r.to_string(index=False))
            t = r.copy(); t.insert(0, "table", "D_alert_skill"); tables.append(t)
    else:
        print("\n(Skipping C and D: rain_model_cv_predictions_v1.csv not found.)")

    d = days[days["year"] >= 2022].reset_index(drop=True)
    X = np.log1p(d[["obs_mean", "obs_max", "rain2", "rain3"]].clip(lower=0).to_numpy())
    yv = d["F"].to_numpy()
    if yv.sum() >= 15:
        groups = d["date"].dt.strftime("%Y-%m").to_numpy()
        oof = np.zeros(len(d))
        for tr, te in GroupKFold(n_splits=min(6, len(set(groups)))).split(X, yv, groups):
            if len(set(yv[tr])) < 2:
                oof[te] = yv[tr].mean(); continue
            oof[te] = LogisticRegression(max_iter=2000).fit(X[tr], yv[tr]).predict_proba(X[te])[:, 1]
        e = pd.DataFrame([{"days_2022_2025": len(d), "report_days": int(yv.sum()), "auc": round(roc_auc_score(yv, oof), 3), "brier_model": round(brier_score_loss(yv, oof), 4), "brier_always_average": round(brier_score_loss(yv, np.full(len(d), yv.mean())), 4)}])
        print("\nE. Flood-report days predicted from OBSERVED rain, incl. rain over the last 2-3 days (best case, perfect forecast):")
        print(e.to_string(index=False))
        e.insert(0, "table", "E_report_model"); tables.append(e)
        e2 = []
        for name, cols in [("today only", [0, 1]), ("today + last 3 days", [0, 1, 2, 3])]:
            o = np.zeros(len(d))
            for tr, te in GroupKFold(n_splits=min(6, len(set(groups)))).split(X, yv, groups):
                o[te] = LogisticRegression(max_iter=2000).fit(X[tr][:, cols], yv[tr]).predict_proba(X[te][:, cols])[:, 1]
            e2.append({"inputs": name, "auc": round(roc_auc_score(yv, o), 3)})
        print("\nF. Does rain from the previous days help?")
        print(pd.DataFrame(e2).to_string(index=False))
    pd.concat(tables, ignore_index=True).to_csv(OUT, index=False)
    print(f"\nSaved: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
