from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import pandas as pd
import geopandas as gpd
import folium
from sqlalchemy import create_engine

app = FastAPI(title="Flood EWS Risk API")
engine = create_engine("postgresql://postgres:postgres@localhost:5432/flood_ews")

THRESHOLD_CUSECS = 25000
VERY_HEAVY_RAIN_MM = 115.6
RAIN_WEIGHT = 0.4

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
    wards["adjusted_risk"] = (wards["base_risk"] + RAIN_WEIGHT * wards["rain_risk"]).clip(upper=1.0)

    wards["risk_score"] = wards.apply(lambda r: 1.0 if r["wardnum"] in flagged else r["adjusted_risk"], axis=1)
    return wards

@app.get("/risk/city")
def city_risk(current_cusecs: float = 15000, rain_24h_mm: float = 0.0):
    wards = compute_risk(current_cusecs, rain_24h_mm)
    return {
        "city_wide_risk_score": round(wards["risk_score"].max(), 3),
        "city_wide_avg_score": round(wards["risk_score"].mean(), 3),
        "critical_wards": int((wards["risk_score"] >= 0.8).sum()),
    }

@app.get("/risk/wards")
def ward_risk(current_cusecs: float = 15000, rain_24h_mm: float = 0.0):
    wards = compute_risk(current_cusecs, rain_24h_mm)
    return wards.sort_values("risk_score", ascending=False)[["wardnum", "name", "risk_score"]].to_dict(orient="records")

@app.get("/map", response_class=HTMLResponse)
def risk_map(current_cusecs: float = 15000, rain_24h_mm: float = 0.0):
    scores = compute_risk(current_cusecs, rain_24h_mm)
    geom = gpd.read_postgis("SELECT wardnum, geometry FROM wards", engine, geom_col="geometry")
    wards_map = geom.merge(scores[["wardnum", "name", "risk_score"]], on="wardnum")

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
        tooltip=folium.GeoJsonTooltip(fields=["name", "risk_score"], aliases=["Ward:", "Risk Score:"]),
    ).add_to(m)
    return m.get_root().render()