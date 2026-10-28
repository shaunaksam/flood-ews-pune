import sys
import math
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
