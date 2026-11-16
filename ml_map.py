import os
import sys
import math
import time
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()
ROOT = Path(__file__).resolve().parent
_shape = None


def _main_module():
    for name in ("api.main", "main", "__main__"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "compute_predictive_risk") and hasattr(mod, "engine"):
            return mod
    return None


def _round_coords(c):
    if isinstance(c[0], (int, float)):
        return [round(float(c[0]), 5), round(float(c[1]), 5)]
    return [_round_coords(x) for x in c]


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _b(x):
    try:
        if x is None or x != x:
            return False
        return bool(x)
    except Exception:
        return False


def _load_rivers(minx, miny, maxx, maxy):
    import geopandas as gpd
    from shapely.geometry import box
    for p in (ROOT / "data" / "raw" / "puneriver_v2.geojson", ROOT / "data" / "processed" / "puneriver_v2.geojson", ROOT / "puneriver_v2.geojson"):
        if not p.exists():
            continue
        r = gpd.read_file(p)
        r = r.set_crs(epsg=4326) if r.crs is None else r.to_crs(epsg=4326)
        clip = box(minx - 0.12, miny - 0.12, maxx + 0.12, maxy + 0.12)
        lines = []
        for geom in r.geometry:
            if geom is None or geom.is_empty:
                continue
            part = geom.intersection(clip)
            if part.is_empty:
                continue
            for q in getattr(part, "geoms", [part]):
                if q.geom_type != "LineString":
                    continue
                q = q.simplify(0.0004)
                if len(q.coords) > 1:
                    lines.append([[round(x, 5), round(y, 5)] for x, y in q.coords])
        return lines
    return []


def _build_shape(mod):
    import geopandas as gpd
    from shapely.geometry import mapping
    g = gpd.read_postgis("SELECT wardnum, geometry FROM wards", mod.engine, geom_col="geometry")
    if g.crs is not None and g.crs.to_epsg() not in (None, 4326):
        g = g.to_crs(epsg=4326)
    g["geometry"] = g.geometry.simplify(0.0002, preserve_topology=True)
    wards = []
    for _, row in g.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        m = mapping(geom)
        wards.append({"id": int(row["wardnum"]), "g": {"type": m["type"], "coordinates": _round_coords(m["coordinates"])}})
    minx, miny, maxx, maxy = g.total_bounds
    try:
        rivers = _load_rivers(minx, miny, maxx, maxy)
    except Exception:
        rivers = []
    return {"available": True, "wards": wards, "rivers": rivers}


_wm = None
_li = None


def _ward_bundle():
    global _wm
    if _wm is None:
        p = ROOT / "models" / "ward_report_v1.joblib"
        try:
            import joblib
            _wm = joblib.load(p) if p.exists() else False
        except Exception:
            _wm = False
    return _wm or None


def _learned_index():
    global _li
    if _li is None:
        p = ROOT / "data" / "processed" / "ward_learned_index_v1.csv"
        _li = {}
        try:
            if p.exists():
                import pandas as pd
                d = pd.read_csv(p)
                _li = {str(int(r.wardnum)): float(r.learned_rank_pct) for r in d.itertuples()}
        except Exception:
            _li = {}
    return _li


def _ward_chances(rm_mm, rx_mm):
    b = _ward_bundle()
    if b is None:
        return {}
    try:
        import numpy as np
        import pandas as pd
        f = pd.DataFrame(b["ward_static"])
        f["rm"] = float(np.log1p(max(float(rm_mm), 0.0)))
        f["rx"] = float(np.log1p(max(float(rx_mm), 0.0)))
        f["larea"] = np.log(f["km2"].clip(lower=0.05))
        f["y2023"] = 0
        f["y2024"] = 0
        f["y2025"] = 1
        for c in ("vuln", "terrain", "river", "larea"):
            f["rm_" + c] = f["rm"] * f[c]
        p = b["logistic"].predict_proba(f[b["logistic_features"]].to_numpy(dtype=float))[:, 1]
        return {str(int(w)): float(x) for w, x in zip(f["wardnum"], p)}
    except Exception:
        return {}


def _scores_payload(mod, src):
    df = mod.compute_predictive_risk(src, mod.DEFAULT_CUSECS)
    out = {}
    for _, r in df.iterrows():
        out[str(int(r["wardnum"]))] = {
            "n": str(r.get("name")),
            "s": _f(r.get("predictive_risk_score")),
            "l": None if r.get("predictive_risk_level") is None else str(r.get("predictive_risk_level")),
            "u": _f(r.get("urban_waterlogging_index")),
            "r": _f(r.get("river_corridor_index")),
            "v": _f(r.get("static_vulnerability")),
            "c": _b(r.get("corridor_flagged")),
            "f6": _f(r.get("forecast_6h_mm")),
            "f24": _f(r.get("forecast_24h_mm")),
            "h": _f(r.get("upstream_rainfall_hazard_score")),
        }
    try:
        rm = float(src.get("forecast_24h_mm") or 0.0)
        if src.get("source") == "Replay":
            rx = float(src.get("upstream_max_forecast_24h_mm") or rm)
        else:
            rx = _f(df["forecast_24h_mm"].max())
            rx = rm if rx is None else rx
        chances = _ward_chances(rm, rx)
        li = _learned_index()
        for k, v in out.items():
            v["p"] = chances.get(k)
            v["li"] = li.get(k)
    except Exception:
        pass
    return out


@router.get("/ml/map/shape")
def map_shape():
    global _shape
    mod = _main_module()
    if mod is None:
        return JSONResponse(status_code=503, content={"available": False, "reason": "Map source not ready."})
    try:
        if _shape is None:
            _shape = _build_shape(mod)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"available": False, "reason": f"Ward shapes unavailable: {type(exc).__name__}"})
    return JSONResponse(content=_shape, headers={"Cache-Control": "max-age=3600"})


@router.get("/ml/map/scores")
def map_scores(date: str = ""):
    mod = _main_module()
    if mod is None:
        return JSONResponse(status_code=503, content={"available": False, "reason": "Map source not ready."})
    import ml_outlook
    try:
        if date:
            df = ml_outlook._load_cv()
            hit = df[df["key"] == date]
            if hit.empty:
                return JSONResponse(status_code=404, content={"available": False, "reason": "Date not in replay data."})
            r = hit.iloc[0]
            local24 = float(r["local24_mm"])
            up24 = float(r["upstream24_mm"])
            src = {
                "source": "Replay",
                "data_type": "simulated_scenario_not_live",
                "forecast_6h_mm": 0.0,
                "forecast_24h_mm": local24,
                "upstream_forecast_available": True,
                "upstream_forecast_6h_mm": 0.0,
                "upstream_forecast_24h_mm": up24,
                "upstream_max_forecast_6h_mm": 0.0,
                "upstream_max_forecast_24h_mm": up24,
                "upstream_forecasts": [],
                "upstream_error": None,
                "error": None,
            }
            replay = True
            note = "Replay: that day's 24-hour forecast is applied evenly across the city, so differences between wards come from terrain, drainage and river proximity."
        else:
            weather = ml_outlook.get_weather()
            if not isinstance(weather, dict) or weather.get("error") or weather.get("ward_forecasts") is None:
                return JSONResponse(status_code=503, content={"available": False, "reason": "Ward forecasts unavailable."})
            src = weather
            replay = False
            note = "Experimental advisory index from local ward forecasts, upstream rainfall, terrain and river proximity. Not an official flood map."
        scores = _scores_payload(mod, src)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"available": False, "reason": f"Scores unavailable: {type(exc).__name__}"})
    return {"available": True, "replay": replay, "scores": scores, "note": note}


_bot = {"v": None, "t": 0.0}


def _bot_info():
    name = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    if not name:
        if time.time() - _bot["t"] < 3600:
            name = _bot["v"] or ""
        else:
            _bot["t"] = time.time()
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if token:
                try:
                    import requests
                    name = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=4).json().get("result", {}).get("username") or ""
                except Exception:
                    name = ""
            _bot["v"] = name
    return {"username": name, "link": f"https://t.me/{name}"} if name else None


@router.get("/ml/reports")
def ml_reports(hours: int = 6):
    from datetime import datetime, timedelta, timezone
    hours = max(1, min(int(hours), 48))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    agg = {}
    total = 0
    mod = _main_module()
    if mod is not None:
        try:
            from sqlalchemy import text
            with mod.engine.connect() as c:
                rows = c.execute(text("SELECT ward, severity, reporter, created_at FROM citizen_reports WHERE created_at >= :s"), {"s": now - timedelta(hours=hours)}).fetchall()
            for ward, sev, who, ts in rows:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                d = agg.setdefault(str(int(ward)), {"count": 0, "people": set(), "max_severity": 0, "latest": None})
                d["count"] += 1
                d["people"].add(who)
                d["max_severity"] = max(d["max_severity"], int(sev))
                if d["latest"] is None or ts > d["latest"]:
                    d["latest"] = ts
                total += 1
        except Exception:
            agg, total = {}, 0
    wards = {k: {"count": v["count"], "people": len(v["people"]), "max_severity": v["max_severity"], "minutes_ago": max(0, int((now - v["latest"]).total_seconds() // 60))} for k, v in agg.items()}
    rain, rain_total, recent_people, recent_wards = {}, 0, set(), set()
    if mod is not None:
        try:
            from sqlalchemy import text
            with mod.engine.connect() as c:
                rrows = c.execute(text("SELECT ward, intensity, reporter, created_at FROM rain_reports WHERE created_at >= :s"), {"s": now - timedelta(hours=3)}).fetchall()
            ragg = {}
            for ward, inten, who, ts in rrows:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                d = ragg.setdefault(str(int(ward)), {"count": 0, "people": set(), "max_intensity": 0, "latest": None})
                d["count"] += 1
                d["people"].add(who)
                d["max_intensity"] = max(d["max_intensity"], int(inten))
                if d["latest"] is None or ts > d["latest"]:
                    d["latest"] = ts
                rain_total += 1
                if int(inten) >= 2 and (now - ts) <= timedelta(minutes=90):
                    recent_people.add(who)
                    recent_wards.add(int(ward))
            rain = {k: {"count": v["count"], "people": len(v["people"]), "max_intensity": v["max_intensity"], "minutes_ago": max(0, int((now - v["latest"]).total_seconds() // 60))} for k, v in ragg.items()}
        except Exception:
            rain, rain_total, recent_people, recent_wards = {}, 0, set(), set()
    return JSONResponse(content={"available": True, "hours": hours, "total": total, "wards": wards, "rain": rain, "rain_total": rain_total, "rain_recent": {"people": len(recent_people), "wards": len(recent_wards)}, "bot": _bot_info()}, headers={"Cache-Control": "no-store"})


@router.get("/ml/observations")
def ml_observations(hours: int = 30):
    from datetime import datetime, timedelta, timezone
    hours = max(1, min(int(hours), 72))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    latest = {}
    mod = _main_module()
    if mod is not None:
        try:
            from sqlalchemy import text
            with mod.engine.connect() as c:
                try:
                    rows = c.execute(text("SELECT station, mm, entered_at, source FROM rain_observations WHERE entered_at >= :s ORDER BY entered_at"), {"s": now - timedelta(hours=hours)}).fetchall()
                except Exception:
                    rows = [tuple(r) + (None,) for r in c.execute(text("SELECT station, mm, entered_at FROM rain_observations WHERE entered_at >= :s ORDER BY entered_at"), {"s": now - timedelta(hours=hours)}).fetchall()]
            for station, mm, ts, src in rows:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                latest[str(station)] = {"station": str(station), "mm": float(mm), "minutes_ago": max(0, int((now - ts).total_seconds() // 60)), "source": src or "IMD station data (manual entry)"}
        except Exception:
            latest = {}
    stations = sorted((d for d in latest.values() if d["mm"] >= 1.0), key=lambda d: -d["mm"])
    sources = sorted({d["source"] for d in stations})
    return JSONResponse(content={"available": True, "stations": stations, "source": " · ".join(sources) if sources else None}, headers={"Cache-Control": "no-store"})


@router.get("/metrics")
def metrics():
    from datetime import datetime, timedelta, timezone
    from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response
    reg = CollectorRegistry()

    def g(name, doc, value):
        if value is not None:
            Gauge(name, doc, registry=reg).set(float(value))

    g("flood_ews_api_up", "API process is running", 1)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    mod = _main_module()
    db_ok, flood6, rain3, subs, obs_max = 0, None, None, None, None
    if mod is not None:
        try:
            from sqlalchemy import text
            with mod.engine.connect() as c:
                c.execute(text("SELECT 1"))
                db_ok = 1
                for label in ("flood6", "rain3", "subs", "obs"):
                    try:
                        if label == "flood6":
                            flood6 = c.execute(text("SELECT COUNT(*) FROM citizen_reports WHERE created_at >= :s"), {"s": now - timedelta(hours=6)}).scalar()
                        elif label == "rain3":
                            rain3 = c.execute(text("SELECT COUNT(*) FROM rain_reports WHERE created_at >= :s"), {"s": now - timedelta(hours=3)}).scalar()
                        elif label == "subs":
                            subs = c.execute(text("SELECT COUNT(*) FROM tg_subscribers")).scalar()
                        else:
                            latest = {}
                            for st, mm, ts in c.execute(text("SELECT station, mm, entered_at FROM rain_observations WHERE entered_at >= :s ORDER BY entered_at"), {"s": now - timedelta(hours=30)}).fetchall():
                                latest[st] = float(mm)
                            obs_max = max(latest.values()) if latest else 0.0
                    except Exception:
                        pass
        except Exception:
            db_ok = 0
    g("flood_ews_db_up", "Database reachable", db_ok)
    g("flood_ews_citizen_flood_reports_6h", "Citizen flood reports in the last 6 hours", flood6)
    g("flood_ews_citizen_rain_reports_3h", "Citizen rain reports in the last 3 hours", rain3)
    g("flood_ews_telegram_subscribers", "Active Telegram subscribers", subs)
    g("flood_ews_observed_rain_max_mm", "Highest recorded 24 h rain among stations entered in the last 30 hours (mm)", obs_max)
    redis_ok = 0
    try:
        import redis
        redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), socket_timeout=2).ping()
        redis_ok = 1
    except Exception:
        redis_ok = 0
    g("flood_ews_redis_up", "Redis reachable", redis_ok)
    try:
        import ml_outlook
        w = ml_outlook.get_weather(max_age=600)
        ok = isinstance(w, dict) and not w.get("error")
        g("flood_ews_forecast_fetch_ok", "Latest forecast fetch succeeded", 1 if ok else 0)
        if ok:
            g("flood_ews_forecast_age_seconds", "Age of the cached forecast in seconds", time.monotonic() - ml_outlook._wcache["t"])
            g("flood_ews_forecast_city_24h_mm", "City average forecast rain, next 24 h (mm)", w.get("forecast_24h_mm"))
            out = ml_outlook.compute_outlook(w)
            if out.get("available"):
                g("flood_ews_heavy_rain_probability", "Chance of 20 mm or more regional rain (0 to 1)", out["heavy_rain_chance"]["probability"])
                up = out.get("upstream_heavy_rain_chance")
                if up:
                    g("flood_ews_upstream_heavy_rain_probability", "Chance of 64.5 mm or more in a dam-side cell (0 to 1)", up["probability"])
    except Exception:
        g("flood_ews_forecast_fetch_ok", "Latest forecast fetch succeeded", 0)
    return Response(content=generate_latest(reg), media_type=CONTENT_TYPE_LATEST)
