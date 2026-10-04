from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import pandas as pd
import geopandas as gpd
import folium
import requests
from sqlalchemy import create_engine

app = FastAPI(title="Flood EWS Risk API")
engine = create_engine("postgresql://postgres:postgres@localhost:5432/flood_ews")

THRESHOLD_CUSECS = 25000
VERY_HEAVY_RAIN_MM = 200
RAIN_WEIGHT = 0.25

# Default discharge used when no live Khadakwasla feed exists 
DEFAULT_CUSECS = 15000

# Shivajinagar station 
STATION_LAT, STATION_LON = 18.5306, 73.8474

# Open-Meteo forecast rainfall was found in POC testing to underestimate real
# Pune monsoon rainfall by roughly 5-15x on historical test dates (logged finding).
# This is a rough, undocumented-precision correction factor, not a calibrated model -
# surfaced on the outlook page explicitly, not hidden.
OPEN_METEO_CORRECTION_FACTOR = 4.0


def compute_risk(current_cusecs: float, rain_24h_mm: float = 0.0):
    at_risk = pd.read_sql(f"""
        SELECT DISTINCT ward_id FROM dam_discharge_events
        WHERE cusecs >= {THRESHOLD_CUSECS}
    """, engine)
    flagged = at_risk["ward_id"].dropna().astype(int).tolist() if current_cusecs >= THRESHOLD_CUSECS else []

    wards = pd.read_sql("SELECT wardnum, name, elevation_mean, river_distance_m, dam_discharge_incident_count FROM wards", engine)
    wards["elevation_risk"] = (wards["elevation_mean"] - wards["elevation_mean"].max()) / (wards["elevation_mean"].min() - wards["elevation_mean"].max())
    wards["river_risk"] = (wards["river_distance_m"] - wards["river_distance_m"].max()) / (wards["river_distance_m"].min() - wards["river_distance_m"].max())
    wards["incident_risk"] = (wards["dam_discharge_incident_count"] - wards["dam_discharge_incident_count"].min()) / (wards["dam_discharge_incident_count"].max() - wards["dam_discharge_incident_count"].min())
    wards["base_risk"] = 0.3 * wards["elevation_risk"] + 0.5 * wards["incident_risk"] + 0.2 * wards["river_risk"]

    rain_factor = min(max(rain_24h_mm / VERY_HEAVY_RAIN_MM, 0.0), 1.0)
    wards["rain_risk"] = rain_factor * wards["elevation_risk"]
    wards["adjusted_risk"] = (wards["base_risk"] + RAIN_WEIGHT * wards["rain_risk"]).clip(upper=0.95)

    wards["corridor_flagged"] = wards["wardnum"].isin(flagged)
    wards["static_vulnerability"] = wards["base_risk"]
    wards["risk_score"] = wards.apply(lambda r: 1.0 if r["corridor_flagged"] else r["adjusted_risk"], axis=1)
    return wards


def fetch_forecast_rainfall():
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": STATION_LAT,
            "longitude": STATION_LON,
            "daily": "precipitation_sum",
            "timezone": "auto",
            "forecast_days": 1,
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        raw_mm = data["daily"]["precipitation_sum"][0] or 0.0
        corrected_mm = raw_mm * OPEN_METEO_CORRECTION_FACTOR
        return raw_mm, corrected_mm, None
    except Exception as e:
        return None, None, str(e)


def verdict_label(live_risk_avg: float):
    if live_risk_avg >= 0.6:
        return "HIGH", "#b0392b"
    elif live_risk_avg >= 0.4:
        return "MODERATE", "#e8a33d"
    else:
        return "LOW", "#2a9d8f"

@app.get("/risk/city")
def city_risk(current_cusecs: float = 15000, rain_24h_mm: float = 0.0):
    wards = compute_risk(current_cusecs, rain_24h_mm)
    return {
        "city_wide_live_risk": round(wards["risk_score"].max(), 3),
        "city_wide_avg_live_risk": round(wards["risk_score"].mean(), 3),
        "city_wide_static_vulnerability": round(wards["static_vulnerability"].max(), 3),
        "critical_wards": int((wards["risk_score"] >= 0.8).sum()),
        "corridor_flagged_wards": int(wards["corridor_flagged"].sum()),
    }


@app.get("/risk/wards")
def ward_risk(current_cusecs: float = 15000, rain_24h_mm: float = 0.0):
    wards = compute_risk(current_cusecs, rain_24h_mm)
    return wards.sort_values("risk_score", ascending=False)[["wardnum", "name", "risk_score", "static_vulnerability", "corridor_flagged"]].to_dict(orient="records")


@app.get("/forecast")
def forecast():
    raw_mm, corrected_mm, error = fetch_forecast_rainfall()
    return {
        "raw_forecast_mm": round(raw_mm, 1),
        "corrected_estimate_mm": round(corrected_mm, 1),
        "correction_factor": OPEN_METEO_CORRECTION_FACTOR,
        "note": "Correction factor is a rough adjustment based on POC testing (Open-Meteo under-reported real Pune monsoon rainfall 5-15x on historical test dates), not a precisely calibrated model.",
        "error": error,
    }


@app.get("/map", response_class=HTMLResponse)
def risk_map(current_cusecs: float = 15000, rain_24h_mm: float = 0.0):
    scores = compute_risk(current_cusecs, rain_24h_mm)
    geom = gpd.read_postgis("SELECT wardnum, geometry FROM wards", engine, geom_col="geometry")
    wards_map = geom.merge(scores[["wardnum", "name", "risk_score", "corridor_flagged"]], on="wardnum")

    m = folium.Map(location=[18.52, 73.85], zoom_start=11, tiles=None)
    folium.Choropleth(
        geo_data=wards_map,
        data=wards_map,
        columns=["wardnum", "risk_score"],
        key_on="feature.properties.wardnum",
        fill_color="YlOrRd",
        fill_opacity=0.75,
        line_opacity=0.2,
        legend_name=f"Flood Risk Score — {int(current_cusecs):,} cusecs, {rain_24h_mm:g} mm rain (24h)",
        bins=[0, 0.2, 0.4, 0.6, 0.8, 1.0],
    ).add_to(m)
    folium.GeoJson(
        wards_map,
        style_function=lambda x: {"fillOpacity": 0, "weight": 0},
        tooltip=folium.GeoJsonTooltip(
            fields=["name", "risk_score", "corridor_flagged"],
            aliases=["Ward:", "Risk Score:", "Active Dam Discharge:"],
        ),
    ).add_to(m)
    return m.get_root().render()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <title>Pune Flood Risk</title>
      <style>
        body { font-family: sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        h1 { margin-bottom: 4px; }
        .controls { background: white; padding: 16px 20px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .row { margin-bottom: 14px; }
        label { display: inline-block; width: 220px; font-weight: bold; }
        .stats { display: flex; gap: 20px; margin-top: 10px; flex-wrap: wrap; }
        .stat { background: #fafafa; border: 1px solid #ddd; border-radius: 6px; padding: 10px 16px; }
        .stat span { display: block; font-size: 22px; font-weight: bold; color: #b0392b; }
        .stat.static span { color: #6b7280; }
        iframe { width: 100%; height: 600px; border: 1px solid #ccc; border-radius: 6px; }
        a.outlook-link { display: inline-block; margin-bottom: 16px; }
      </style>
    </head>
    <body>
      <h1>Pune Flood Risk</h1>
      <p>Adjust discharge and rainfall to see the risk model respond in real time.</p>
      <a class="outlook-link" href="/outlook">&rarr; View Today's Automatic Outlook (no manual input)</a>
      <div class="controls">
        <div class="row">
          <label for="cusecs">Khadakwasla discharge (cusecs)</label>
          <input type="range" id="cusecs" min="0" max="50000" step="1000" value="15000">
          <span id="cusecsVal">15000</span>
        </div>
        <div class="row">
          <label for="rain">24h rainfall (mm)</label>
          <input type="range" id="rain" min="0" max="250" step="5" value="0">
          <span id="rainVal">0</span>
        </div>
        <div class="stats">
          <div class="stat">Live risk right now (max)<span id="cityMax">-</span></div>
          <div class="stat">Live risk right now (avg)<span id="cityAvg">-</span></div>
          <div class="stat">Critical wards<span id="critical">-</span></div>
          <div class="stat">Active discharge flags<span id="flagged">-</span></div>
          <div class="stat static">Baseline vulnerability (always present)<span id="staticVuln">-</span></div>
        </div>
      </div>
      <iframe id="mapFrame" src="/map?current_cusecs=15000&rain_24h_mm=0"></iframe>

      <script>
        const cusecs = document.getElementById('cusecs');
        const rain = document.getElementById('rain');

        async function update() {
          const c = cusecs.value, r = rain.value;
          document.getElementById('cusecsVal').innerText = c;
          document.getElementById('rainVal').innerText = r;
          document.getElementById('mapFrame').src = `/map?current_cusecs=${c}&rain_24h_mm=${r}`;

          const res = await fetch(`/risk/city?current_cusecs=${c}&rain_24h_mm=${r}`);
          const data = await res.json();
          document.getElementById('cityMax').innerText = data.city_wide_live_risk;
          document.getElementById('cityAvg').innerText = data.city_wide_avg_live_risk;
          document.getElementById('critical').innerText = data.critical_wards;
          document.getElementById('flagged').innerText = data.corridor_flagged_wards;
          document.getElementById('staticVuln').innerText = data.city_wide_static_vulnerability;
        }

        cusecs.oninput = update;
        rain.oninput = update;
        update();
      </script>
    </body>
    </html>
    """

@app.get("/outlook", response_class=HTMLResponse)
def outlook():
    raw_mm, corrected_mm, error = fetch_forecast_rainfall()
    cusecs_used = DEFAULT_CUSECS

    if error:
        return f"""
        <!DOCTYPE html>
        <html><body style="font-family:sans-serif; padding:20px;">
        <h1>Pune Flood Risk - Today's Outlook</h1>
        <p><a href="/">&larr; Back to manual dashboard</a></p>
        <div style="background:#6b7280; color:white; padding:24px; border-radius:8px;">
          <h2 style="margin:0;">Forecast Unavailable</h2>
          <p>Could not fetch live rainfall forecast right now ({error}). Rather than guess, no automatic verdict is shown.
          Use the manual dashboard to check risk for a specific rainfall/discharge scenario instead.</p>
        </div>
        </body></html>
        """

    wards = compute_risk(cusecs_used, corrected_mm)
    live_avg = round(wards["risk_score"].mean(), 3)
    live_max = round(wards["risk_score"].max(), 3)
    critical_count = int((wards["risk_score"] >= 0.8).sum())
    label, color = verdict_label(live_avg)

    forecast_note = (
        f'<p>Raw forecast: <b>{raw_mm:.1f}mm</b> &rarr; adjusted estimate used: <b>{corrected_mm:.1f}mm</b> '
        f'(a {OPEN_METEO_CORRECTION_FACTOR}x correction factor, based on known underestimation found during POC validation).</p>'
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <title>Pune Flood Risk - Today's Outlook</title>
      <style>
        body {{ font-family: sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        h1 {{ margin-bottom: 4px; }}
        .verdict {{ background: {color}; color: white; padding: 24px; border-radius: 8px; margin: 16px 0; text-align: center; }}
        .verdict h2 {{ margin: 0; font-size: 36px; }}
        .verdict p {{ margin: 6px 0 0; opacity: 0.9; }}
        .meta {{ background: white; padding: 16px 20px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .meta p {{ margin: 6px 0; }}
        iframe {{ width: 100%; height: 600px; border: 1px solid #ccc; border-radius: 6px; }}
        a.manual-link {{ display: inline-block; margin-bottom: 16px; }}
        .caveat {{ font-size: 13px; color: #6b7280; }}
      </style>
    </head>
    <body>
      <h1>Pune Flood Risk - Today's Outlook</h1>
      <a class="manual-link" href="/">&larr; Back to manual dashboard</a>
      <div class="verdict">
        <h2>{label}</h2>
        <p>City-wide live risk (avg): {live_avg} &nbsp;|&nbsp; Max: {live_max} &nbsp;|&nbsp; Critical wards: {critical_count}</p>
      </div>
      <div class="meta">
        {forecast_note}
        <p class="caveat">Discharge input: {cusecs_used:,} cusecs (no live Khadakwasla feed currently available)</p>
      </div>
      <iframe src="/map?current_cusecs={cusecs_used}&rain_24h_mm={corrected_mm}"></iframe>
    </body>
    </html>
    """
