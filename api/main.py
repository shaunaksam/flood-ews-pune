from fastapi import FastAPI
import pandas as pd
from sqlalchemy import create_engine

app = FastAPI(title="Flood EWS Risk API")
engine = create_engine("postgresql://postgres:postgres@localhost:5432/flood_ews")

THRESHOLD_CUSECS = 25000

def compute_risk(current_cusecs: float):
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
    wards["risk_score"] = wards.apply(lambda r: 1.0 if r["wardnum"] in flagged else r["base_risk"], axis=1)
    return wards

@app.get("/risk/city")
def city_risk(current_cusecs: float = 15000):
    wards = compute_risk(current_cusecs)
    return {
        "city_wide_risk_score": round(wards["risk_score"].max(), 3),
        "city_wide_avg_score": round(wards["risk_score"].mean(), 3),
    }

@app.get("/risk/wards")
def ward_risk(current_cusecs: float = 15000):
    wards = compute_risk(current_cusecs)
    return wards.sort_values("risk_score", ascending=False)[["wardnum", "name", "risk_score"]].to_dict(orient="records")