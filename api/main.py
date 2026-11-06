from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import pandas as pd
import geopandas as gpd
import folium
import requests
import asyncio 
from pathlib import Path
import logging
from sqlalchemy import create_engine, text
import os
import json
import hashlib
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
import redis
from fastapi import HTTPException


app = FastAPI(title="Pune Flood EWS — Forecast, Telegram Scheduler, and IMD Nowcast Connector V2.8")
engine = create_engine(os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/flood_ews"))

THRESHOLD_CUSECS = 25000
VERY_HEAVY_RAIN_MM = 200
RAIN_WEIGHT = 0.25

# Default discharge used when no live Khadakwasla feed exists 
DEFAULT_CUSECS = 15000

# Shivajinagar reference location (legacy endpoints only)
STATION_LAT, STATION_LON = 18.5306, 73.8474

# Representative weather-forecast points beside the four-dam Khadakwasla
# system. These are NOT catchment polygons or a catchment-weighted rainfall
# average. Forecasts are model estimates at the listed coordinates.
# Coordinates are based on public map/location references; verify against an
# authoritative Maharashtra Water Resources Department GIS layer when available.
UPSTREAM_CATCHMENT_POINTS = [
    {"name": "Khadakwasla", "latitude": 18.442697, "longitude": 73.763713},
    {"name": "Panshet", "latitude": 18.378060, "longitude": 73.613460},
    {"name": "Varasgaon", "latitude": 18.386940, "longitude": 73.612500},
    {"name": "Temghar", "latitude": 18.452580, "longitude": 73.555780},
]

# Open-Meteo forecast rainfall was found in POC testing to underestimate real
# Pune monsoon rainfall by roughly 5-15x on historical test dates (logged finding).
# This is a rough, undocumented-precision correction factor, not a calibrated model -
# surfaced on the outlook page explicitly, not hidden.
OPEN_METEO_CORRECTION_FACTOR = 4.0


def _open_meteo_get(url, **kwargs):
    """GET an Open-Meteo endpoint with one cautious retry for transient failures.

    Retries only network timeouts/connections and HTTP 429/5xx responses.
    Other HTTP responses are returned immediately so callers can handle them.
    This helper does not use cached or stale forecasts; if both attempts fail,
    the existing caller error handling remains fail-closed.
    """
    import time

    max_attempts = 2
    last_network_error = None
    for attempt in range(max_attempts):
        try:
            response = requests.get(url, **kwargs)
            if (response.status_code == 429 or response.status_code >= 500) and attempt < max_attempts - 1:
                logging.warning(
                    "Open-Meteo returned HTTP %s; retrying once (%s/%s).",
                    response.status_code, attempt + 1, max_attempts,
                )
                time.sleep(1)
                continue
            return response
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_network_error = exc
            if attempt >= max_attempts - 1:
                raise
            logging.warning(
                "Open-Meteo network request failed (%s); retrying once (%s/%s).",
                type(exc).__name__, attempt + 1, max_attempts,
            )
            time.sleep(1)

    # Defensive fallback; normal failures raise above or return a response.
    if last_network_error is not None:
        raise last_network_error
    raise RuntimeError("Open-Meteo request failed without a response.")


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

# -------------------------------------------------------
# FORECAST-DRIVEN PREDICTIVE RISK INDEX (PROTOTYPE V2.3)
# -------------------------------------------------------

# Transparent research heuristics, not fitted probabilities.
# 115.5 mm is the upper bound of IMD's daily "heavy rainfall" category.
RAIN_HAZARD_REFERENCE_24H_MM = 115.5
# Provisional six-hour scoring reference. This is NOT an official IMD
# six-hour threshold; it is deliberately kept separate from the 24-hour
# threshold because hourly/daily rainfall cannot be scaled linearly by time.
RAIN_HAZARD_REFERENCE_6H_MM = 64.5
PREDICTIVE_WARD_ALERT_THRESHOLD = 0.35  # provisional; must be back-tested


def _clamp01(value):
    return min(max(float(value), 0.0), 1.0)


def compute_predictive_risk(forecast: dict, current_cusecs: float = DEFAULT_CUSECS):
    """Calculate ward risk from forecast rainfall and fixed ward susceptibility.

    If ``forecast['ward_forecasts']`` is present, each ward uses its own
    centroid forecast. Otherwise a city-wide rainfall scenario is supported
    for controlled simulation. Scores are 0..1 prototype indices, not
    calibrated flood probabilities.
    """
    import math

    if not isinstance(forecast, dict):
        raise ValueError("Forecast payload must be a dictionary.")
    if forecast.get("error"):
        raise ValueError(f"Forecast unavailable: {forecast['error']}")

    discharge = float(current_cusecs)
    if not math.isfinite(discharge) or discharge < 0:
        raise ValueError("Discharge scenario must be finite and non-negative.")

    spatial_rows = forecast.get("ward_forecasts")
    if spatial_rows is not None:
        if not isinstance(spatial_rows, list) or not spatial_rows:
            raise ValueError("ward_forecasts must be a non-empty list.")
        rain_df = pd.DataFrame(spatial_rows)
        needed = {"wardnum", "forecast_6h_mm", "forecast_24h_mm"}
        if not needed.issubset(rain_df.columns):
            raise ValueError(f"ward_forecasts must contain {sorted(needed)}")
        rain_df["wardnum"] = pd.to_numeric(rain_df["wardnum"], errors="coerce")
        for col in ["forecast_6h_mm", "forecast_24h_mm"]:
            rain_df[col] = pd.to_numeric(rain_df[col], errors="coerce")
            if rain_df[col].isna().any():
                raise ValueError(f"Spatial forecast contains missing {col} values.")
            if (~rain_df[col].map(lambda x: math.isfinite(float(x)) and float(x) >= 0)).any():
                raise ValueError(f"Spatial forecast contains invalid {col} values.")
        if rain_df["wardnum"].isna().any() or rain_df["wardnum"].duplicated().any():
            raise ValueError("Spatial forecast has missing or duplicate ward numbers.")
        rain_df["wardnum"] = rain_df["wardnum"].astype(int)
        if len(rain_df) != rain_df["wardnum"].nunique():
            raise ValueError("Spatial forecast does not have unique ward entries.")
        average_rain24 = float(rain_df["forecast_24h_mm"].mean())
        wards = compute_risk(discharge, average_rain24).copy()
        wards = wards.merge(
            rain_df[["wardnum", "forecast_6h_mm", "forecast_24h_mm"]],
            on="wardnum", how="left", validate="one_to_one"
        )
        if wards[["forecast_6h_mm", "forecast_24h_mm"]].isna().any().any():
            missing_wards = wards.loc[
                wards["forecast_6h_mm"].isna() | wards["forecast_24h_mm"].isna(),
                "wardnum"
            ].astype(int).tolist()
            raise ValueError(f"Spatial forecast missing wards: {missing_wards}")
        rain24_severity = (wards["forecast_24h_mm"] / RAIN_HAZARD_REFERENCE_24H_MM).clip(0, 1)
        rain6_severity = (wards["forecast_6h_mm"] / RAIN_HAZARD_REFERENCE_6H_MM).clip(0, 1)
        rainfall_hazard = pd.concat([rain24_severity, rain6_severity], axis=1).max(axis=1)
    else:
        raw_6h = forecast.get("forecast_6h_mm")
        raw_24h = forecast.get("forecast_24h_mm")
        if raw_6h is None or raw_24h is None:
            raise ValueError("Both 6-hour and 24-hour forecasts are required.")
        rain6, rain24 = float(raw_6h), float(raw_24h)
        for label, value in (("6-hour rainfall", rain6), ("24-hour rainfall", rain24)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be finite and non-negative.")
        wards = compute_risk(discharge, rain24).copy()
        rainfall_hazard_value = max(
            _clamp01(rain24 / RAIN_HAZARD_REFERENCE_24H_MM),
            _clamp01(rain6 / RAIN_HAZARD_REFERENCE_6H_MM),
        )
        rainfall_hazard = pd.Series(rainfall_hazard_value, index=wards.index)
        wards["forecast_6h_mm"] = rain6
        wards["forecast_24h_mm"] = rain24

    elevation = pd.to_numeric(wards["elevation_risk"], errors="coerce").fillna(0).clip(0, 1)
    river = pd.to_numeric(wards["river_risk"], errors="coerce").fillna(0).clip(0, 1)
    incidents = pd.to_numeric(wards["incident_risk"], errors="coerce").fillna(0).clip(0, 1)

    # Local rainfall and upstream catchment rainfall are kept as separate signals.
    # Local rainfall drives the urban-waterlogging pathway; rainfall at the
    # reservoir-adjacent upstream reference points drives river-corridor vigilance.
    raw_upstream_6h = forecast.get("upstream_forecast_6h_mm")
    raw_upstream_24h = forecast.get("upstream_forecast_24h_mm")
    upstream_error = forecast.get("upstream_error")
    upstream_available = (
        raw_upstream_6h is not None
        and raw_upstream_24h is not None
        and not upstream_error
    )
    upstream_hazard_value = 0.0
    if upstream_available:
        upstream_6h = float(raw_upstream_6h)
        upstream_24h = float(raw_upstream_24h)
        for label, value in (("upstream 6-hour rainfall", upstream_6h),
                             ("upstream 24-hour rainfall", upstream_24h)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be finite and non-negative.")
        upstream_hazard_value = max(
            _clamp01(upstream_6h / RAIN_HAZARD_REFERENCE_6H_MM),
            _clamp01(upstream_24h / RAIN_HAZARD_REFERENCE_24H_MM),
        )

    wards["upstream_forecast_available"] = bool(upstream_available)
    wards["upstream_rainfall_hazard_score"] = upstream_hazard_value
    wards["urban_susceptibility_proxy"] = (0.7 * elevation + 0.3 * river).clip(0, 1)
    wards["urban_waterlogging_index"] = (rainfall_hazard * wards["urban_susceptibility_proxy"]).clip(0, 1)

    if THRESHOLD_CUSECS > DEFAULT_CUSECS:
        discharge_hazard = _clamp01(
            (discharge - DEFAULT_CUSECS) / (THRESHOLD_CUSECS - DEFAULT_CUSECS)
        )
    else:
        discharge_hazard = 1.0 if discharge >= THRESHOLD_CUSECS else 0.0

    wards["river_corridor_susceptibility_proxy"] = (0.6 * incidents + 0.4 * river).clip(0, 1)
    # Deliberately do not use city-local rainfall as a substitute for upstream rain.
    # If upstream rainfall is unavailable, this branch relies only on the configured
    # discharge scenario and the direct high-discharge corridor override, while the
    # urban branch can still use the available ward-centroid rainfall forecast.
    river_pressure = max(upstream_hazard_value, discharge_hazard)
    wards["river_corridor_index"] = (
        river_pressure * wards["river_corridor_susceptibility_proxy"]
    ).clip(0, 1)

    if discharge >= THRESHOLD_CUSECS:
        override = wards["corridor_flagged"].astype(bool)
        wards.loc[override, "river_corridor_index"] = 1.0

    wards["rainfall_hazard_score"] = rainfall_hazard
    wards["predictive_risk_score"] = pd.concat([
        wards["urban_waterlogging_index"], wards["river_corridor_index"]
    ], axis=1).max(axis=1).clip(0, 1)

    def risk_level(score):
        if score >= 0.8:
            return "CRITICAL"
        if score >= 0.6:
            return "HIGH"
        if score >= 0.4:
            return "MODERATE"
        return "LOW"

    wards["predictive_risk_level"] = wards["predictive_risk_score"].apply(risk_level)
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
        r = _open_meteo_get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        raw_mm = data["daily"]["precipitation_sum"][0] or 0.0
        corrected_mm = raw_mm * OPEN_METEO_CORRECTION_FACTOR
        return raw_mm, corrected_mm, None
    except Exception as e:
        return None, None, str(e)


def fetch_model_rainfall_24h():
    """
    Fetch the latest model-estimated rainfall for the previous 24 hours.

    Important: this is weather-model data, NOT a rain-gauge observation.
    Errors and missing values are never silently converted to zero.
    """
    import math
    import requests
    from datetime import datetime
    from zoneinfo import ZoneInfo

    timezone_name = "Asia/Kolkata"

    try:
        response = _open_meteo_get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": STATION_LAT,
                "longitude": STATION_LON,
                "current": "precipitation,rain,showers",
                "hourly": "precipitation",
                "past_hours": 24,
                "forecast_hours": 1,
                "timezone": timezone_name,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()

        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        values = hourly.get("precipitation", [])
        current = payload.get("current", {})
        current_time_text = current.get("time")

        if not current_time_text:
            raise ValueError("The API did not provide a current model timestamp.")

        def parse_local_time(value):
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
            return dt.astimezone(ZoneInfo(timezone_name))

        model_valid_time = parse_local_time(current_time_text)

        # Keep only hourly entries up to the latest model timestamp.
        past_samples = []

        for timestamp, value in zip(times, values):
            sample_time = parse_local_time(timestamp)

            if sample_time <= model_valid_time:
                past_samples.append((sample_time, value))

        # Require a complete 24-hour window.
        past_samples = past_samples[-24:]

        if len(past_samples) != 24:
            raise ValueError(
                f"Expected 24 hourly rainfall values; "
                f"received {len(past_samples)}."
            )

        rainfall_values = []

        for _, value in past_samples:
            if value is None:
                raise ValueError(
                    "Rainfall data contains a missing hourly value."
                )

            amount = float(value)

            if not math.isfinite(amount) or amount < 0:
                raise ValueError(
                    f"Invalid hourly rainfall value: {value}"
                )

            rainfall_values.append(amount)

        return {
            "source": "Open-Meteo weather model",
            "data_type": "model_estimate_not_gauge_observation",
            "latitude": STATION_LAT,
            "longitude": STATION_LON,
            "rainfall_24h_mm": round(sum(rainfall_values), 2),
            "window_start_local": past_samples[0][0].isoformat(),
            "window_end_local": past_samples[-1][0].isoformat(),
            "model_valid_time_local": model_valid_time.isoformat(),
            "fetched_at_local": datetime.now(
                ZoneInfo(timezone_name)
            ).isoformat(timespec="seconds"),
            "hours_counted": len(rainfall_values),
            "error": None,
        }

    except Exception as exc:
        return {
            "source": "Open-Meteo weather model",
            "data_type": "model_estimate_not_gauge_observation",
            "rainfall_24h_mm": None,
            "model_valid_time_local": None,
            "fetched_at_local": None,
            "hours_counted": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def fetch_model_rainfall_forecast():
    """
    Fetch upcoming 6-hour and 24-hour rainfall estimates.

    These are weather-model forecasts, not physical gauge readings.
    Missing or incomplete forecast data must not be treated as zero.
    """
    import math
    import requests
    from datetime import datetime
    from zoneinfo import ZoneInfo

    timezone_name = "Asia/Kolkata"
    tz = ZoneInfo(timezone_name)

    try:
        response = _open_meteo_get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": STATION_LAT,
                "longitude": STATION_LON,
                "current": "precipitation",
                "hourly": "precipitation",
                "forecast_days": 2,
                "timezone": timezone_name,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()

        current = payload.get("current", {})
        hourly = payload.get("hourly", {})

        model_time_text = current.get("time")
        times = hourly.get("time", [])
        values = hourly.get("precipitation", [])

        if not model_time_text:
            raise ValueError("Current model timestamp is unavailable.")

        if len(times) != len(values):
            raise ValueError("Hourly forecast timestamps and values do not match.")

        def parse_local_time(value):
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(tz)

        model_time = parse_local_time(model_time_text)

        # Retain only forecast hours after the current model timestamp.
        future = []

        for timestamp, value in zip(times, values):
            sample_time = parse_local_time(timestamp)

            if sample_time > model_time:
                future.append((sample_time, value))

        future.sort(key=lambda item: item[0])

        # Require complete windows; never fill missing values with zero.
        if len(future) < 24:
            raise ValueError(
                f"Only {len(future)} future hourly values available; "
                "24 are required."
            )

        next_24h = future[:24]
        next_6h = next_24h[:6]

        def validated_sum(samples, expected_hours):
            if len(samples) != expected_hours:
                raise ValueError("Incomplete hourly forecast window.")

            rainfall_values = []

            for _, value in samples:
                if value is None:
                    raise ValueError("Hourly forecast contains a missing value.")

                amount = float(value)

                if not math.isfinite(amount) or amount < 0:
                    raise ValueError(
                        f"Invalid hourly precipitation value: {value}"
                    )

                rainfall_values.append(amount)

            return round(sum(rainfall_values), 2)

        return {
            "source": "Open-Meteo weather model",
            "data_type": "forecast_not_gauge_observation",
            "latitude": STATION_LAT,
            "longitude": STATION_LON,
            "model_valid_time_local": model_time.isoformat(),
            "forecast_6h_mm": validated_sum(next_6h, 6),
            "forecast_24h_mm": validated_sum(next_24h, 24),
            "forecast_start_local": next_24h[0][0].isoformat(),
            "forecast_end_local": next_24h[-1][0].isoformat(),
            "hours_counted": len(next_24h),
            "fetched_at_local": datetime.now(tz).isoformat(
                timespec="seconds"
            ),
            "error": None,
        }

    except Exception as exc:
        return {
            "source": "Open-Meteo weather model",
            "data_type": "forecast_not_gauge_observation",
            "forecast_6h_mm": None,
            "forecast_24h_mm": None,
            "model_valid_time_local": None,
            "forecast_start_local": None,
            "forecast_end_local": None,
            "hours_counted": 0,
            "fetched_at_local": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def fetch_ward_rainfall_forecasts():
    """Fetch hourly model forecasts at all ward centroids in one request.

    Open-Meteo supports comma-separated latitude/longitude coordinates and
    returns a list of location payloads. Missing location/hour values cause
    the full spatial forecast to fail closed; they are never treated as zero.
    """
    import math
    import geopandas as gpd
    import requests
    from datetime import datetime
    from zoneinfo import ZoneInfo

    timezone_name = "Asia/Kolkata"
    tz = ZoneInfo(timezone_name)

    try:
        geom = gpd.read_postgis(
            "SELECT wardnum, geometry FROM wards",
            engine,
            geom_col="geometry",
        )
        if geom.empty:
            raise ValueError("No ward geometries found in PostgreSQL.")
        if geom["wardnum"].duplicated().any():
            raise ValueError("Duplicate ward numbers in ward geometries.")
        if geom.crs is None:
            geom = geom.set_crs(epsg=4326)
        projected = geom.to_crs(epsg=32643)
        centroids = projected.geometry.centroid.to_crs(epsg=4326)

        locations = []
        for wardnum, point in zip(geom["wardnum"], centroids):
            if point is None or point.is_empty:
                raise ValueError(f"Ward {wardnum} has an empty centroid.")
            lat, lon = float(point.y), float(point.x)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"Invalid centroid for ward {wardnum}.")
            locations.append({
                "wardnum": int(wardnum),
                "latitude": round(lat, 5),
                "longitude": round(lon, 5),
            })

        response = _open_meteo_get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": ",".join(str(p["latitude"]) for p in locations),
                "longitude": ",".join(str(p["longitude"]) for p in locations),
                "current": "precipitation",
                "hourly": "precipitation",
                "forecast_days": 2,
                "timezone": timezone_name,
            },
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        payloads = payload if isinstance(payload, list) else [payload]
        if len(payloads) != len(locations):
            raise ValueError(
                f"Forecast returned {len(payloads)} locations for {len(locations)} wards."
            )

        def parse_local_time(value):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)

        ward_forecasts = []
        starts, ends, valid_times = [], [], []

        for location, item in zip(locations, payloads):
            current = item.get("current", {})
            hourly = item.get("hourly", {})
            model_time_text = current.get("time")
            times = hourly.get("time", [])
            values = hourly.get("precipitation", [])

            if not model_time_text:
                raise ValueError(f"No current model timestamp for ward {location['wardnum']}.")
            if len(times) != len(values):
                raise ValueError(f"Hourly timestamp/value mismatch for ward {location['wardnum']}.")

            model_time = parse_local_time(model_time_text)
            future = []
            for timestamp, value in zip(times, values):
                sample_time = parse_local_time(timestamp)
                if sample_time > model_time:
                    future.append((sample_time, value))
            future.sort(key=lambda pair: pair[0])
            if len(future) < 24:
                raise ValueError(
                    f"Only {len(future)} future hourly values for ward {location['wardnum']}."
                )

            next_24 = future[:24]
            rain_values = []
            for _, value in next_24:
                if value is None:
                    raise ValueError(f"Missing hourly rainfall for ward {location['wardnum']}.")
                amount = float(value)
                if not math.isfinite(amount) or amount < 0:
                    raise ValueError(f"Invalid hourly rainfall for ward {location['wardnum']}: {value}")
                rain_values.append(amount)

            rain6 = round(sum(rain_values[:6]), 2)
            rain24 = round(sum(rain_values), 2)
            ward_forecasts.append({
                **location,
                "forecast_6h_mm": rain6,
                "forecast_24h_mm": rain24,
            })
            starts.append(next_24[0][0])
            ends.append(next_24[-1][0])
            valid_times.append(model_time)

        rain6_values = [row["forecast_6h_mm"] for row in ward_forecasts]
        rain24_values = [row["forecast_24h_mm"] for row in ward_forecasts]
        return {
            "source": "Open-Meteo weather model",
            "data_type": "multi_location_forecast_not_gauge_observation",
            "spatial_method": "forecast queried at each ward polygon centroid",
            "ward_forecasts": ward_forecasts,
            "ward_count": len(ward_forecasts),
            "forecast_6h_mm": round(sum(rain6_values) / len(rain6_values), 2),
            "forecast_24h_mm": round(sum(rain24_values) / len(rain24_values), 2),
            "max_forecast_6h_mm": max(rain6_values),
            "max_forecast_24h_mm": max(rain24_values),
            "wards_over_6h_threshold": sum(v >= RAIN_WATCH_THRESHOLD_6H_MM for v in rain6_values),
            "wards_over_24h_threshold": sum(v >= RAIN_WATCH_THRESHOLD_MM for v in rain24_values),
            "forecast_start_local": min(starts).isoformat(),
            "forecast_end_local": max(ends).isoformat(),
            "model_valid_time_local": max(valid_times).isoformat(),
            "fetched_at_local": datetime.now(tz).isoformat(timespec="seconds"),
            "hours_counted": 24,
            "error": None,
        }
    except Exception as exc:
        logging.exception("Ward-centroid rainfall forecast failed")
        return {
            "source": "Open-Meteo weather model",
            "data_type": "multi_location_forecast_not_gauge_observation",
            "spatial_method": "forecast queried at each ward polygon centroid",
            "ward_forecasts": None,
            "ward_count": 0,
            "forecast_6h_mm": None,
            "forecast_24h_mm": None,
            "max_forecast_6h_mm": None,
            "max_forecast_24h_mm": None,
            "model_valid_time_local": None,
            "fetched_at_local": None,
            "hours_counted": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def fetch_upstream_catchment_forecasts():
    """Fetch next-6h and next-24h rainfall at four reservoir-adjacent points.

    This is a proxy for upstream rainfall conditions, not a hydrological
    catchment-area average and not a prediction of dam gate operation or discharge.
    """
    import math
    import requests
    from datetime import datetime
    from zoneinfo import ZoneInfo

    timezone_name = "Asia/Kolkata"
    tz = ZoneInfo(timezone_name)

    def parse_local_time(value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)

    try:
        response = _open_meteo_get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": ",".join(str(p["latitude"]) for p in UPSTREAM_CATCHMENT_POINTS),
                "longitude": ",".join(str(p["longitude"]) for p in UPSTREAM_CATCHMENT_POINTS),
                "current": "precipitation",
                "hourly": "precipitation",
                "forecast_days": 2,
                "timezone": timezone_name,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        payloads = payload if isinstance(payload, list) else [payload]
        if len(payloads) != len(UPSTREAM_CATCHMENT_POINTS):
            raise ValueError(
                f"Upstream forecast returned {len(payloads)} locations; "
                f"expected {len(UPSTREAM_CATCHMENT_POINTS)}."
            )

        rows = []
        valid_times = []
        starts = []
        ends = []

        for point, item in zip(UPSTREAM_CATCHMENT_POINTS, payloads):
            current = item.get("current", {})
            hourly = item.get("hourly", {})
            model_time_text = current.get("time")
            times = hourly.get("time", [])
            values = hourly.get("precipitation", [])

            if not model_time_text:
                raise ValueError(f"No current model timestamp for {point['name']}.")
            if len(times) != len(values):
                raise ValueError(f"Timestamp/value mismatch for {point['name']}.")

            model_time = parse_local_time(model_time_text)
            future = []
            for timestamp, value in zip(times, values):
                sample_time = parse_local_time(timestamp)
                if sample_time > model_time:
                    future.append((sample_time, value))
            future.sort(key=lambda item: item[0])
            if len(future) < 24:
                raise ValueError(f"Fewer than 24 future hourly values for {point['name']}.")

            next_24 = future[:24]
            rain_values = []
            for _, value in next_24:
                if value is None:
                    raise ValueError(f"Missing hourly rainfall at {point['name']}.")
                amount = float(value)
                if not math.isfinite(amount) or amount < 0:
                    raise ValueError(f"Invalid hourly rainfall at {point['name']}: {value}")
                rain_values.append(amount)

            rows.append({
                **point,
                "forecast_6h_mm": round(sum(rain_values[:6]), 2),
                "forecast_24h_mm": round(sum(rain_values), 2),
                "model_valid_time_local": model_time.isoformat(),
                "forecast_start_local": next_24[0][0].isoformat(),
                "forecast_end_local": next_24[-1][0].isoformat(),
            })
            valid_times.append(model_time)
            starts.append(next_24[0][0])
            ends.append(next_24[-1][0])

        six = [row["forecast_6h_mm"] for row in rows]
        twenty_four = [row["forecast_24h_mm"] for row in rows]
        peak6 = max(rows, key=lambda row: row["forecast_6h_mm"])
        peak24 = max(rows, key=lambda row: row["forecast_24h_mm"])
        return {
            "upstream_forecast_available": True,
            "upstream_source": "Open-Meteo weather model",
            "upstream_spatial_method": (
                "Four representative reservoir-adjacent forecast points; "
                "not a catchment-polygon or catchment-weighted rainfall estimate"
            ),
            "upstream_forecasts": rows,
            "upstream_forecast_6h_mm": round(sum(six) / len(six), 2),
            "upstream_forecast_24h_mm": round(sum(twenty_four) / len(twenty_four), 2),
            "upstream_max_forecast_6h_mm": max(six),
            "upstream_max_forecast_24h_mm": max(twenty_four),
            "upstream_peak_6h_location": peak6["name"],
            "upstream_peak_24h_location": peak24["name"],
            "upstream_model_valid_time_local": max(valid_times).isoformat(),
            "upstream_forecast_start_local": min(starts).isoformat(),
            "upstream_forecast_end_local": max(ends).isoformat(),
            "upstream_fetched_at_local": datetime.now(tz).isoformat(timespec="seconds"),
            "upstream_error": None,
        }
    except Exception as exc:
        logging.exception("Upstream reservoir-adjacent rainfall forecast failed")
        return {
            "upstream_forecast_available": False,
            "upstream_source": "Open-Meteo weather model",
            "upstream_spatial_method": (
                "Four representative reservoir-adjacent forecast points; "
                "not a catchment-polygon or catchment-weighted rainfall estimate"
            ),
            "upstream_forecasts": None,
            "upstream_forecast_6h_mm": None,
            "upstream_forecast_24h_mm": None,
            "upstream_max_forecast_6h_mm": None,
            "upstream_max_forecast_24h_mm": None,
            "upstream_peak_6h_location": None,
            "upstream_peak_24h_location": None,
            "upstream_model_valid_time_local": None,
            "upstream_forecast_start_local": None,
            "upstream_forecast_end_local": None,
            "upstream_fetched_at_local": None,
            "upstream_error": f"{type(exc).__name__}: {exc}",
        }


def fetch_integrated_rainfall_forecasts():
    """Fetch ward-local and upstream reservoir-adjacent rainfall in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        ward_future = pool.submit(fetch_ward_rainfall_forecasts)
        upstream_future = pool.submit(fetch_upstream_catchment_forecasts)
        ward_data = ward_future.result()
        upstream_data = upstream_future.result()

    ward_data.update(upstream_data)
    ward_data["data_type"] = "ward_forecast_plus_upstream_reference_forecasts"
    return ward_data


# -------------------------------------------------------
# REDIS-BASED RAINFALL ALERTS
# -------------------------------------------------------

# Prototype thresholds only; NOT official warning criteria.
RAIN_WATCH_THRESHOLD_MM = 64.5
# Keep the existing experimental early-watch trigger separate from the
# scoring reference. This 28.875 mm value is not an official 6-hour threshold.
RAIN_WATCH_THRESHOLD_6H_MM = RAIN_HAZARD_REFERENCE_24H_MM * 6 / 24
# Experimental upstream watch trigger; not an official IMD or dam-operation rule.
UPSTREAM_RAIN_WATCH_THRESHOLD_6H_MM = RAIN_WATCH_THRESHOLD_6H_MM
UPSTREAM_RAIN_WATCH_THRESHOLD_24H_MM = RAIN_WATCH_THRESHOLD_MM
WARD_ALERT_RISK_THRESHOLD = 0.60  # retained for legacy endpoints only

ALERTS_REDIS_KEY = "floodews:alerts"
ALERT_DEDUPE_SECONDS = 6 * 60 * 60
ALERT_RETENTION_SECONDS = 7 * 24 * 60 * 60

alerts_redis = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)

# Background alert scheduler
ALERT_CHECK_INTERVAL_SECONDS = 15 * 60
ALERT_SCHEDULER_LOCK_KEY = "floodews:alert_scheduler_lock"
ALERT_SCHEDULER_LOCK_TTL_SECONDS = 120

alert_scheduler_task = None

ALERT_SCHEDULER_STATUS = {
    "enabled": True,
    "interval_minutes": 15,
    "last_checked_at_local": None,
    "last_result": "not_started",
    "last_error": None,
    "last_forecast_24h_mm": None,
    "last_upstream_max_24h_mm": None,
    "last_advisory_run_id": None,
    "last_alert_created": False,
    "last_telegram_status": "not_checked",
    "last_telegram_error": None,
    "last_telegram_sent": False,
    "last_telegram_trigger_reason_codes": [],
}



def build_advisory_payload(forecast: dict, wards: pd.DataFrame, current_cusecs: float):
    """Turn forecast-driven indices into explainable, non-official advisory text.

    This generates SMS-length previews only. It does not send messages and it
    never infers gate operation or dam discharge from rainfall.
    """
    generated_at = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
    upstream_available = bool(forecast.get("upstream_forecast_available")) and not forecast.get("upstream_error")
    upstream_max6 = forecast.get("upstream_max_forecast_6h_mm") if upstream_available else None
    upstream_max24 = forecast.get("upstream_max_forecast_24h_mm") if upstream_available else None
    upstream_watch = bool(
        upstream_available and (
            float(upstream_max6) >= UPSTREAM_RAIN_WATCH_THRESHOLD_6H_MM
            or float(upstream_max24) >= UPSTREAM_RAIN_WATCH_THRESHOLD_24H_MM
        )
    )

    local_watch_wards = set()
    reason_by_ward = {}
    for _, row in wards.iterrows():
        wardnum = int(row["wardnum"])
        local6 = float(row.get("forecast_6h_mm", 0.0))
        local24 = float(row.get("forecast_24h_mm", 0.0))
        reasons = []
        if local6 >= RAIN_WATCH_THRESHOLD_6H_MM or local24 >= RAIN_WATCH_THRESHOLD_MM:
            local_watch_wards.add(wardnum)
            reasons.append({
                "code": "LOCAL_RAINFALL_WATCH",
                "description": "local ward forecast crosses an experimental rainfall-watch threshold",
            })
        if upstream_watch and float(row.get("river_corridor_index", 0.0)) >= 0.20:
            reasons.append({
                "code": "UPSTREAM_CATCHMENT_RAIN_WATCH",
                "description": "rain is forecast at reservoir-adjacent upstream reference points; dam release is not inferred",
            })
        if float(row.get("elevation_risk", 0.0)) >= 0.70 and float(row.get("urban_waterlogging_index", 0.0)) >= PREDICTIVE_WARD_ALERT_THRESHOLD:
            reasons.append({
                "code": "ELEVATION_SUSCEPTIBILITY",
                "description": "elevation-derived susceptibility increases the local waterlogging index",
            })
        if float(row.get("river_risk", 0.0)) >= 0.70 and float(row.get("predictive_risk_score", 0.0)) >= PREDICTIVE_WARD_ALERT_THRESHOLD:
            reasons.append({
                "code": "RIVER_PROXIMITY_SUSCEPTIBILITY",
                "description": "river proximity contributes to this ward's risk index",
            })
        if bool(row.get("corridor_flagged", False)) and float(current_cusecs) >= THRESHOLD_CUSECS:
            reasons.append({
                "code": "HISTORICAL_DISCHARGE_LINK_AND_SCENARIO_THRESHOLD",
                "description": "ward is linked to a historical discharge event and the manual discharge scenario crosses the configured threshold",
            })
        if not reasons and float(row.get("predictive_risk_score", 0.0)) >= PREDICTIVE_WARD_ALERT_THRESHOLD:
            reasons.append({
                "code": "COMBINED_SUSCEPTIBILITY_SCORE",
                "description": "combined model index exceeds the prototype advisory-priority threshold",
            })
        reason_by_ward[wardnum] = reasons

    risk_priority = wards[wards["predictive_risk_score"] >= PREDICTIVE_WARD_ALERT_THRESHOLD].copy()
    local_rain_priority = wards[wards["wardnum"].astype(int).isin(local_watch_wards)].copy()
    priority_parts = [risk_priority, local_rain_priority]
    if upstream_watch:
        upstream_priority = wards[wards["river_corridor_index"] >= 0.20].copy()
        priority_parts.append(upstream_priority)
    priority = pd.concat(priority_parts).drop_duplicates(subset=["wardnum"])
    priority = priority.sort_values("predictive_risk_score", ascending=False).head(8)

    any_high = bool((wards["predictive_risk_score"] >= 0.60).any())
    if upstream_watch or local_watch_wards or any_high:
        status = "WATCH"
    elif bool((wards["predictive_risk_score"] >= PREDICTIVE_WARD_ALERT_THRESHOLD).any()):
        status = "MONITOR"
    else:
        status = "ROUTINE_MONITORING"

    local_average6 = float(forecast.get("forecast_6h_mm") or 0.0)
    local_average24 = float(forecast.get("forecast_24h_mm") or 0.0)
    local_max6 = float(forecast.get("max_forecast_6h_mm") or 0.0)
    local_max24 = float(forecast.get("max_forecast_24h_mm") or 0.0)

    reason_codes = []
    if local_watch_wards:
        reason_codes.append("LOCAL_RAINFALL_WATCH")
    if upstream_watch:
        reason_codes.append("UPSTREAM_CATCHMENT_RAIN_WATCH")
    if any_high:
        reason_codes.append("HIGH_PREDICTIVE_INDEX")

    if upstream_available:
        upstream_summary = (
            f"Four upstream reference points: mean {float(forecast.get('upstream_forecast_6h_mm') or 0):.1f} mm/6h "
            f"and {float(forecast.get('upstream_forecast_24h_mm') or 0):.1f} mm/24h; "
            f"maximum {float(upstream_max6):.1f} mm/6h at {forecast.get('upstream_peak_6h_location') or 'a reference point'} "
            f"and {float(upstream_max24):.1f} mm/24h at {forecast.get('upstream_peak_24h_location') or 'a reference point'}."
        )
        upstream_status = "WATCH" if upstream_watch else "AVAILABLE_BELOW_EXPERIMENTAL_WATCH_TRIGGER"
    else:
        upstream_summary = "Upstream rainfall forecast unavailable. Dam gate and discharge status are also unverified."
        upstream_status = "UNAVAILABLE"

    if upstream_watch and local_watch_wards:
        city_message = (
            f"Combined rainfall watch: local ward forecasts cross experimental rainfall-watch thresholds "
            f"in {len(local_watch_wards)} ward(s). Local forecast average: {local_average6:.1f} mm/6h "
            f"and {local_average24:.1f} mm/24h; maximum: {local_max6:.1f} mm/6h "
            f"and {local_max24:.1f} mm/24h. Upstream signal: {upstream_summary} "
            "Upstream rain may affect catchment inflows, but does not establish gate opening or downstream flooding. "
            "Dam discharge is not verified; follow official updates."
        )
    elif upstream_watch:
        city_message = (
            f"Upstream rainfall watch: rainfall is forecast at one or more reservoir-adjacent points. "
            f"{upstream_summary} This may affect catchment inflows, but does not establish gate opening or downstream flooding. "
            "Dam discharge is not verified; follow official updates."
        )
    elif local_watch_wards:
        city_message = (
            f"Local rainfall watch for {len(local_watch_wards)} ward(s). "
            f"Average ward-centroid forecast is {local_average6:.1f} mm/6h and {local_average24:.1f} mm/24h; "
            f"maximum is {local_max6:.1f} mm/6h and {local_max24:.1f} mm/24h. "
            "These are model estimates, not observed rainfall or confirmation of flooding."
        )
    elif any_high:
        city_message = (
            "Some ward risk indices are high under the current forecast and susceptibility assumptions. "
            "Review the listed reasons and check official weather and disaster-management updates."
        )
    else:
        city_message = (
            f"Current ward forecasts average {local_average6:.1f} mm/6h and {local_average24:.1f} mm/24h. "
            "No experimental local or upstream rainfall-watch trigger is currently met. This is routine monitoring, not a guarantee of safety."
        )

    simulated = "simulated" in str(forecast.get("data_type", "")).lower()
    sms_previews = []
    for _, row in priority.iterrows():
        wardnum = int(row["wardnum"])
        ward_name = str(row["name"])
        score = float(row["predictive_risk_score"])
        level = str(row["predictive_risk_level"])
        local6 = float(row.get("forecast_6h_mm", 0.0))
        local24 = float(row.get("forecast_24h_mm", 0.0))
        explanations = [item["description"] for item in reason_by_ward.get(wardnum, [])]
        if not explanations:
            explanations = ["the forecast combined with mapped ward susceptibility"]
        reason_text = "; ".join(explanations[:2])
        upstream_caveat = (
            " Upstream rain is also elevated, but dam gate/discharge status is unavailable."
            if upstream_watch else " Dam gate/discharge status is not verified."
        )
        valid_to = forecast.get("forecast_end_local") or "the forecast-window end"
        simulation_prefix = "SIMULATION ONLY | " if simulated else "PUNE FLOOD WATCH | "
        message = (
            f"{simulation_prefix}{ward_name}: model forecast {local6:.0f} mm/6h, {local24:.0f} mm/24h. "
            f"Prototype index {score:.2f} ({level}); {reason_text}."
            f"{upstream_caveat} Avoid flooded roads and riverbanks; follow official PMC/disaster updates. "
            f"Forecast window ends {valid_to}. Experimental advisory, not an official warning."
        )
        sms_previews.append({
            "target_type": "ward",
            "wardnum": wardnum,
            "ward_name": ward_name,
            "risk_level": level,
            "risk_index": round(score, 3),
            "forecast_6h_mm": round(local6, 2),
            "forecast_24h_mm": round(local24, 2),
            "reason_codes": [item["code"] for item in reason_by_ward.get(wardnum, [])],
            "message": message,
        })

    upstream_sms = None
    if upstream_watch:
        if simulated:
            upstream_sms = (
                f"SIMULATION ONLY | Upstream rainfall scenario: {float(upstream_max6):.0f} mm/6h and "
                f"{float(upstream_max24):.0f} mm/24h. This is not a live weather forecast and does not tell us "
                "whether dam gates will open or what discharge will be. No message was sent."
            )
        else:
            upstream_sms = (
                f"PUNE UPSTREAM RAINFALL WATCH | Four reservoir-adjacent weather-model points show a maximum "
                f"{float(upstream_max6):.0f} mm/6h and {float(upstream_max24):.0f} mm/24h; "
                f"mean {float(forecast.get('upstream_forecast_6h_mm') or 0):.0f} mm/6h and "
                f"{float(forecast.get('upstream_forecast_24h_mm') or 0):.0f} mm/24h. "
                "Rainfall may affect catchment inflows, but does not tell us whether dam gates will open or what discharge will be. "
                "Discharge is unverified. Downstream riverside communities should monitor official updates and avoid riverbanks/flooded roads. "
                f"Updated {forecast.get('upstream_fetched_at_local') or generated_at}. Prototype advisory, not an official warning."
            )

    return {
        "advisory_version": "rule_based_advisory_v1",
        "mode": "SIMULATION_ONLY" if simulated else "FORECAST_ADVISORY_PREVIEW",
        "generated_at_local": generated_at,
        "status": status,
        "trigger_reason_codes": reason_codes,
        "city_message": city_message,
        "local_rainfall_summary": {
            "mean_6h_mm": round(local_average6, 2),
            "max_6h_mm": round(local_max6, 2),
            "mean_24h_mm": round(local_average24, 2),
            "max_24h_mm": round(local_max24, 2),
            "wards_crossing_local_watch_threshold": len(local_watch_wards),
            "thresholds_experimental": {
                "6h_mm": RAIN_WATCH_THRESHOLD_6H_MM,
                "24h_mm": RAIN_WATCH_THRESHOLD_MM,
            },
        },
        "upstream_rainfall": {
            "status": upstream_status,
            "available": upstream_available,
            "mean_6h_mm": forecast.get("upstream_forecast_6h_mm"),
            "max_6h_mm": upstream_max6,
            "mean_24h_mm": forecast.get("upstream_forecast_24h_mm"),
            "max_24h_mm": upstream_max24,
            "peak_6h_location": forecast.get("upstream_peak_6h_location"),
            "peak_24h_location": forecast.get("upstream_peak_24h_location"),
            "points": forecast.get("upstream_forecasts"),
            "error": forecast.get("upstream_error"),
            "method": forecast.get("upstream_spatial_method"),
            "watch_triggered": upstream_watch,
            "watch_thresholds_experimental": {
                "6h_mm": UPSTREAM_RAIN_WATCH_THRESHOLD_6H_MM,
                "24h_mm": UPSTREAM_RAIN_WATCH_THRESHOLD_24H_MM,
            },
        },
        "sms_previews": sms_previews,
        "upstream_sms_preview": upstream_sms,
        "delivery": {
            "sent": False,
            "provider_connected": False,
            "note": "Preview only; no SMS or public alert was sent.",
        },
        "discharge": {
            "scenario_cusecs": float(current_cusecs),
            "verified_live_reading": False,
            "gate_status_inferred": False,
        },
        "limitations": [
            "Scores are experimental indices, not calibrated flood probabilities.",
            "Weather-model forecasts are not gauge observations and different coordinates may map to the same model grid cell.",
            "The four upstream locations are representative points beside reservoir sites, not a catchment-area weighted average.",
            "Rainfall does not establish dam-gate operations or actual discharge; no verified live discharge feed is connected.",
            "This preview does not confirm flooding and is not an official warning.",
        ],
    }


def ensure_advisory_history_tables():
    """Create small, idempotent PostgreSQL tables for forecast snapshots and feedback."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS flood_ews_advisory_runs (
                run_id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                triggered_by TEXT NOT NULL DEFAULT 'api_request',
                model_version TEXT NOT NULL,
                discharge_scenario_cusecs DOUBLE PRECISION NOT NULL,
                local_mean_6h_mm DOUBLE PRECISION,
                local_max_6h_mm DOUBLE PRECISION,
                local_mean_24h_mm DOUBLE PRECISION,
                local_max_24h_mm DOUBLE PRECISION,
                upstream_mean_6h_mm DOUBLE PRECISION,
                upstream_max_6h_mm DOUBLE PRECISION,
                upstream_mean_24h_mm DOUBLE PRECISION,
                upstream_max_24h_mm DOUBLE PRECISION,
                model_valid_time_local TEXT,
                forecast_start_local TEXT,
                forecast_end_local TEXT,
                fetched_at_local TEXT,
                advisory_status TEXT NOT NULL,
                advisory_payload JSONB NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS flood_ews_advisory_feedback (
                feedback_id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                advisory_run_id BIGINT REFERENCES flood_ews_advisory_runs(run_id) ON DELETE SET NULL,
                observed_date DATE NOT NULL,
                wardnum INTEGER,
                observed_outcome TEXT NOT NULL CHECK (
                    observed_outcome IN ('flood_confirmed', 'waterlogging_reported', 'no_flood_reported', 'unverified', 'other')
                ),
                source TEXT,
                notes TEXT
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_flood_ews_advisory_runs_created_at
            ON flood_ews_advisory_runs (created_at DESC)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_flood_ews_advisory_feedback_observed_date
            ON flood_ews_advisory_feedback (observed_date DESC)
        """))


def record_advisory_run(forecast: dict, advisory: dict, current_cusecs: float, triggered_by: str = "api_request"):
    """Persist a compact forecast/advisory snapshot; returns the generated run ID."""
    ensure_advisory_history_tables()
    payload_to_store = {
        "status": advisory.get("status"),
        "trigger_reason_codes": advisory.get("trigger_reason_codes", []),
        "city_message": advisory.get("city_message"),
        "local_rainfall_summary": advisory.get("local_rainfall_summary"),
        "upstream_rainfall": advisory.get("upstream_rainfall"),
        "sms_previews": advisory.get("sms_previews", []),
        "upstream_sms_preview": advisory.get("upstream_sms_preview"),
        "delivery": advisory.get("delivery"),
        "limitations": advisory.get("limitations", []),
    }
    params = {
        "triggered_by": triggered_by,
        "model_version": "rule_based_predictive_index_v2_3_upstream_advisory",
        "discharge": float(current_cusecs),
        "local_mean6": forecast.get("forecast_6h_mm"),
        "local_max6": forecast.get("max_forecast_6h_mm"),
        "local_mean24": forecast.get("forecast_24h_mm"),
        "local_max24": forecast.get("max_forecast_24h_mm"),
        "upstream_mean6": forecast.get("upstream_forecast_6h_mm"),
        "upstream_max6": forecast.get("upstream_max_forecast_6h_mm"),
        "upstream_mean24": forecast.get("upstream_forecast_24h_mm"),
        "upstream_max24": forecast.get("upstream_max_forecast_24h_mm"),
        "model_valid": forecast.get("model_valid_time_local"),
        "start": forecast.get("forecast_start_local"),
        "end": forecast.get("forecast_end_local"),
        "fetched": forecast.get("fetched_at_local"),
        "status": advisory.get("status", "UNKNOWN"),
        "payload": json.dumps(payload_to_store, ensure_ascii=False),
    }
    with engine.begin() as conn:
        run_id = conn.execute(text("""
            INSERT INTO flood_ews_advisory_runs (
                triggered_by, model_version, discharge_scenario_cusecs,
                local_mean_6h_mm, local_max_6h_mm, local_mean_24h_mm, local_max_24h_mm,
                upstream_mean_6h_mm, upstream_max_6h_mm, upstream_mean_24h_mm, upstream_max_24h_mm,
                model_valid_time_local, forecast_start_local, forecast_end_local, fetched_at_local,
                advisory_status, advisory_payload
            ) VALUES (
                :triggered_by, :model_version, :discharge,
                :local_mean6, :local_max6, :local_mean24, :local_max24,
                :upstream_mean6, :upstream_max6, :upstream_mean24, :upstream_max24,
                :model_valid, :start, :end, :fetched, :status, CAST(:payload AS JSONB)
            ) RETURNING run_id
        """), params).scalar_one()
        # Keep 90 days of advisory history in this academic prototype.
        conn.execute(text("""
            DELETE FROM flood_ews_advisory_runs
            WHERE created_at < now() - INTERVAL '90 days'
        """))
    return int(run_id)


def save_advisory_feedback(payload: dict):
    if not isinstance(payload, dict):
        raise ValueError("Feedback body must be a JSON object.")
    required = {"observed_date", "observed_outcome"}
    if not required.issubset(payload):
        raise ValueError("observed_date and observed_outcome are required.")
    from datetime import date as date_type
    try:
        observed_date = date_type.fromisoformat(str(payload["observed_date"]))
    except ValueError as exc:
        raise ValueError("observed_date must use YYYY-MM-DD format.") from exc

    allowed = {"flood_confirmed", "waterlogging_reported", "no_flood_reported", "unverified", "other"}
    outcome = str(payload["observed_outcome"]).strip().lower()
    if outcome not in allowed:
        raise ValueError(f"observed_outcome must be one of: {', '.join(sorted(allowed))}.")
    wardnum = payload.get("wardnum")
    if wardnum is not None:
        try:
            wardnum = int(wardnum)
        except (TypeError, ValueError) as exc:
            raise ValueError("wardnum must be an integer or null.") from exc
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM wards WHERE wardnum = :wardnum"), {"wardnum": wardnum}).first()
        if exists is None:
            raise ValueError(f"Ward {wardnum} does not exist in the wards table.")

    source = str(payload.get("source") or "").strip() or None
    notes = str(payload.get("notes") or "").strip() or None
    if notes and len(notes) > 2000:
        raise ValueError("notes must be 2000 characters or fewer.")
    if outcome in {"flood_confirmed", "waterlogging_reported", "no_flood_reported"} and not source:
        raise ValueError("source is required for a reported/verified outcome; use 'unverified' when there is no supporting source.")
    run_id = payload.get("advisory_run_id")
    if run_id is not None:
        try:
            run_id = int(run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("advisory_run_id must be an integer or null.") from exc

    ensure_advisory_history_tables()
    if run_id is not None:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM flood_ews_advisory_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).first()
        if exists is None:
            raise ValueError(f"advisory_run_id {run_id} does not exist.")
    with engine.begin() as conn:
        feedback_id = conn.execute(text("""
            INSERT INTO flood_ews_advisory_feedback (
                advisory_run_id, observed_date, wardnum, observed_outcome, source, notes
            ) VALUES (:run_id, :observed_date, :wardnum, :outcome, :source, :notes)
            RETURNING feedback_id
        """), {
            "run_id": run_id,
            "observed_date": observed_date,
            "wardnum": wardnum,
            "outcome": outcome,
            "source": source,
            "notes": notes,
        }).scalar_one()
    return int(feedback_id)


def evaluate_rainfall_alert(forecast):
    """
    Evaluate a model rainfall forecast and store a ward watch in Redis.

    This is an experimental, model-based watch, not an official warning.
    Spatial forecasts use each ward centroid to reduce reliance on one
    city-wide reference point. Model rainfall is still not gauge-measured.
    """
    import math

    if not isinstance(forecast, dict):
        return {
            "status": "unavailable",
            "alert_created": False,
            "message": "Forecast payload is invalid; no alert evaluated.",
            "error": "Forecast payload must be a dictionary.",
        }

    rainfall_24h = forecast.get("forecast_24h_mm")
    rainfall_6h = forecast.get("forecast_6h_mm")

    if forecast.get("error") or rainfall_24h is None or rainfall_6h is None:
        return {
            "status": "unavailable",
            "alert_created": False,
            "message": "Complete 6-hour and 24-hour rainfall forecasts are required; no alert evaluated.",
            "error": forecast.get("error"),
        }

    readiness_blocker = _live_forecast_readiness_blocker(forecast)
    if readiness_blocker:
        return {
            "status": "unavailable",
            "alert_created": False,
            "message": "Forecast failed live-readiness checks; no alert created.",
            "error": readiness_blocker,
        }

    try:
        rainfall_24h = float(rainfall_24h)
        rainfall_6h = float(rainfall_6h)
    except (TypeError, ValueError, OverflowError):
        return {
            "status": "invalid_data",
            "alert_created": False,
            "message": "Forecast values are invalid; no alert created.",
        }

    if (
        not math.isfinite(rainfall_24h)
        or not math.isfinite(rainfall_6h)
        or rainfall_24h < 0
        or rainfall_6h < 0
    ):
        return {
            "status": "invalid_data",
            "alert_created": False,
            "message": "Forecast values are invalid; no alert created.",
        }

    # Local-rain and upstream-catchment watches are distinct triggers.
    spatial_rows = forecast.get("ward_forecasts")
    if spatial_rows:
        trigger_wards = [
            row for row in spatial_rows
            if float(row.get("forecast_24h_mm", -1)) >= RAIN_WATCH_THRESHOLD_MM
            or float(row.get("forecast_6h_mm", -1)) >= RAIN_WATCH_THRESHOLD_6H_MM
        ]
        local_watch = bool(trigger_wards)
    else:
        trigger_wards = []
        local_watch = (
            rainfall_24h >= RAIN_WATCH_THRESHOLD_MM
            or rainfall_6h >= RAIN_WATCH_THRESHOLD_6H_MM
        )

    upstream_available = bool(forecast.get("upstream_forecast_available")) and not forecast.get("upstream_error")
    upstream_max6 = forecast.get("upstream_max_forecast_6h_mm") if upstream_available else None
    upstream_max24 = forecast.get("upstream_max_forecast_24h_mm") if upstream_available else None
    upstream_watch = bool(
        upstream_available and (
            float(upstream_max6) >= UPSTREAM_RAIN_WATCH_THRESHOLD_6H_MM
            or float(upstream_max24) >= UPSTREAM_RAIN_WATCH_THRESHOLD_24H_MM
        )
    )
    should_trigger = local_watch or upstream_watch

    if not should_trigger:
        return {
            "status": "no_trigger",
            "alert_created": False,
            "forecast_6h_mm": rainfall_6h,
            "forecast_24h_mm": rainfall_24h,
            "max_forecast_6h_mm": forecast.get("max_forecast_6h_mm", rainfall_6h),
            "max_forecast_24h_mm": forecast.get("max_forecast_24h_mm", rainfall_24h),
            "threshold_6h_mm": RAIN_WATCH_THRESHOLD_6H_MM,
            "threshold_24h_mm": RAIN_WATCH_THRESHOLD_MM,
            "upstream_forecast_available": upstream_available,
            "upstream_max_forecast_6h_mm": upstream_max6,
            "upstream_max_forecast_24h_mm": upstream_max24,
            "upstream_error": forecast.get("upstream_error"),
            "message": "No local or upstream forecast crossed the experimental watch thresholds.",
        }

    # Discharge is the configured scenario, not a verified live reading.
    # Priority now uses the forecast-driven predictive index v1.
    try:
        wards = compute_predictive_risk(forecast, DEFAULT_CUSECS)
    except (ValueError, KeyError, TypeError) as exc:
        return {
            "status": "prediction_unavailable",
            "alert_created": False,
            "forecast_24h_mm": rainfall_24h,
            "message": "Forecast inputs could not be scored; no alert created.",
            "error": str(exc),
        }

    priority = wards.loc[
        wards["predictive_risk_score"] >= PREDICTIVE_WARD_ALERT_THRESHOLD,
        ["wardnum", "name", "predictive_risk_score", "predictive_risk_level"],
    ].copy()

    priority = priority.sort_values(
        "predictive_risk_score", ascending=False
    )

    if priority.empty and upstream_watch:
        # For an upstream watch, show a few most river-susceptible wards even
        # if the provisional composite-score threshold is not crossed.
        priority = wards.nlargest(3, "river_corridor_susceptibility_proxy")[
            ["wardnum", "name", "predictive_risk_score", "predictive_risk_level"]
        ].copy()
    if priority.empty:
        return {
            "status": "no_priority_wards",
            "alert_created": False,
            "forecast_24h_mm": rainfall_24h,
            "threshold_mm": RAIN_WATCH_THRESHOLD_MM,
            "upstream_watch": upstream_watch,
            "message": "A rainfall trigger was met, but no wards could be prioritised by the current prototype rules.",
        }

    priority_wards = [
        {
            "wardnum": int(row["wardnum"]),
            "name": str(row["name"]),
            # Keep risk_score for backward-compatible dashboard rendering.
            "risk_score": round(float(row["predictive_risk_score"]), 3),
            "predictive_risk_score": round(float(row["predictive_risk_score"]), 3),
            "risk_level": str(row["predictive_risk_level"]),
        }
        for _, row in priority.iterrows()
    ]

    ward_ids = sorted(item["wardnum"] for item in priority_wards)
    alert_type = (
        "RAINFALL_AND_UPSTREAM_CATCHMENT_WATCH" if (local_watch and upstream_watch)
        else "UPSTREAM_CATCHMENT_RAIN_WATCH" if upstream_watch
        else "HEAVY_RAINFALL_WATCH"
    )

    # Dedupe separately by advisory type and priority ward set.
    signature = hashlib.sha256(
        f"{alert_type}:{','.join(map(str, ward_ids))}".encode()
    ).hexdigest()[:24]

    dedupe_key = f"floodews:dedupe:{signature}"

    now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(
        timespec="seconds"
    )

    alert = {
        "id": secrets.token_hex(8),
        "type": alert_type,
        "severity": "WATCH",
        "created_at_local": now,
        "source": forecast.get("source", "Open-Meteo weather model"),
        "forecast_6h_mm": rainfall_6h,
        "forecast_24h_mm": rainfall_24h,
        "threshold_6h_mm": RAIN_WATCH_THRESHOLD_6H_MM,
        "threshold_24h_mm": RAIN_WATCH_THRESHOLD_MM,
        "threshold_mm": RAIN_WATCH_THRESHOLD_MM,
        "prediction_model": "rule_based_predictive_index_v2_3_upstream_advisory",
        "max_forecast_6h_mm": forecast.get("max_forecast_6h_mm", rainfall_6h),
        "max_forecast_24h_mm": forecast.get("max_forecast_24h_mm", rainfall_24h),
        "threshold_triggered_ward_count": len(trigger_wards),
        "priority_score_threshold": PREDICTIVE_WARD_ALERT_THRESHOLD,
        "discharge_input": "default scenario, not live telemetry",
        "model_valid_time_local": forecast.get("model_valid_time_local"),
        "forecast_start_local": forecast.get("forecast_start_local"),
        "forecast_end_local": forecast.get("forecast_end_local"),
        "rainfall_spatial_method": forecast.get("spatial_method", "single reference location"),
        "upstream_forecast_available": upstream_available,
        "upstream_forecast_6h_mm": forecast.get("upstream_forecast_6h_mm"),
        "upstream_max_forecast_6h_mm": upstream_max6,
        "upstream_forecast_24h_mm": forecast.get("upstream_forecast_24h_mm"),
        "upstream_max_forecast_24h_mm": upstream_max24,
        "upstream_forecasts": forecast.get("upstream_forecasts"),
        "upstream_watch_triggered": upstream_watch,
        "upstream_error": forecast.get("upstream_error"),
        "priority_wards": priority_wards,
        "status": "MODEL_BASED_WATCH_NOT_OFFICIAL_WARNING",
        "message": (
            f"Ward-centroid model forecast mean: {rainfall_6h:.1f} mm/6h and {rainfall_24h:.1f} mm/24h. "
            f"Upstream max: {upstream_max6 if upstream_available else 'unavailable'} mm/6h and "
            f"{upstream_max24 if upstream_available else 'unavailable'} mm/24h. "
            + ("Upstream rain may affect catchment inflows, but does not confirm dam-gate operation or discharge. " if upstream_watch else "")
            + "Listed wards were prioritised by the experimental risk rules. This is a model-based watch, not confirmation of flooding or an official warning."
        ),
    }

    try:
        alerts_redis.ping()

        # NX ensures the same ward alert isn't repeated within the cooldown.
        created = alerts_redis.set(
            dedupe_key,
            alert["id"],
            nx=True,
            ex=ALERT_DEDUPE_SECONDS,
        )

        if not created:
            return {
                "status": "duplicate_suppressed",
                "alert_created": False,
                "forecast_24h_mm": rainfall_24h,
                "message": "A matching ward watch was already recorded recently.",
            }

        try:
            alerts_redis.lpush(
                ALERTS_REDIS_KEY,
                json.dumps(alert),
            )
            alerts_redis.ltrim(ALERTS_REDIS_KEY, 0, 49)
            alerts_redis.expire(
                ALERTS_REDIS_KEY,
                ALERT_RETENTION_SECONDS,
            )
        except redis.RedisError:
            # Allow a future retry if storing the alert failed.
            alerts_redis.delete(dedupe_key)
            raise

        return {
            "status": "alert_created",
            "alert_created": True,
            "alert": alert,
        }

    except redis.RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Redis alert storage is unavailable: {exc}",
        )


@app.post("/alerts/evaluate")
def evaluate_alert_endpoint():
    """Fetch ward-local and upstream reference forecasts and evaluate watch rules."""
    forecast = fetch_integrated_rainfall_forecasts()
    return evaluate_rainfall_alert(forecast)


@app.post("/alerts/test")
def create_simulated_test_alert():
    """
    Development-only test alert.
    This is simulated and must never be presented as a real warning.
    """
    if os.getenv("FLOOD_EWS_ENV", "development").lower() == "production":
        raise HTTPException(
            status_code=403,
            detail="Simulated alerts are disabled in production.",
        )

    # Deliberately simulated local heavy rain. Upstream rain is set to zero
    # as an explicit test scenario; it is not a statement about live conditions.
    simulated_rain_mm = 80.0
    scenario = {
        "source": "Manual test scenario",
        "data_type": "simulated_scenario_not_live",
        "forecast_6h_mm": 0.0,
        "forecast_24h_mm": simulated_rain_mm,
        "upstream_forecast_available": True,
        "upstream_forecast_6h_mm": 0.0,
        "upstream_forecast_24h_mm": 0.0,
        "upstream_max_forecast_6h_mm": 0.0,
        "upstream_max_forecast_24h_mm": 0.0,
        "upstream_forecasts": [],
        "upstream_error": None,
        "error": None,
    }
    wards = compute_predictive_risk(scenario, DEFAULT_CUSECS)
    priority = wards.loc[
        wards["predictive_risk_score"] >= PREDICTIVE_WARD_ALERT_THRESHOLD,
        ["wardnum", "name", "predictive_risk_score", "predictive_risk_level"],
    ].sort_values("predictive_risk_score", ascending=False)

    # If no wards cross the priority index threshold, show the top five indices.
    if priority.empty:
        priority = wards.nlargest(5, "predictive_risk_score")[
            ["wardnum", "name", "predictive_risk_score", "predictive_risk_level"]
        ]

    priority_wards = [
        {
            "wardnum": int(row["wardnum"]),
            "name": str(row["name"]),
            "risk_score": round(float(row["predictive_risk_score"]), 3),
            "risk_level": str(row["predictive_risk_level"]),
        }
        for _, row in priority.iterrows()
    ]

    alert = {
        "id": secrets.token_hex(8),
        "type": "SIMULATED_TEST_ALERT",
        "severity": "TEST ONLY",
        "is_simulated": True,
        "created_at_local": datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).isoformat(timespec="seconds"),
        "source": "Manual test scenario",
        "forecast_24h_mm": simulated_rain_mm,
        "priority_wards": priority_wards,
        "status": "SIMULATION_NOT_A_REAL_WARNING",
        "message": (
            "TEST ONLY: simulated 80 mm/24h local rainfall with zero upstream-rainfall scenario. "
            "Scores use the V2.3 predictive index. These values are not live forecasts and do not indicate actual flooding."
        ),
    }

    try:
        alerts_redis.ping()
        alerts_redis.lpush(ALERTS_REDIS_KEY, json.dumps(alert))
        alerts_redis.ltrim(ALERTS_REDIS_KEY, 0, 49)
        alerts_redis.expire(ALERTS_REDIS_KEY, ALERT_RETENTION_SECONDS)

        return {
            "status": "simulated_test_alert_created",
            "alert": alert,
        }

    except redis.RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Redis alert storage is unavailable: {exc}",
        )


@app.get("/alerts")
def get_recent_alerts():
    """Return the 20 most recent stored alerts."""
    try:
        alerts_redis.ping()
        stored = alerts_redis.lrange(ALERTS_REDIS_KEY, 0, 19)

        return {
            "count": len(stored),
            "alerts": [json.loads(item) for item in stored],
        }

    except redis.RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Redis alert storage is unavailable: {exc}",
        )


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


@app.get("/prediction/forecast")
def forecast_prediction(current_cusecs: float = DEFAULT_CUSECS):
    """Return ward-level forecast-driven flood-risk indices (prototype v2.3).

    `current_cusecs` is a user-supplied scenario because a verified live
    Khadakwasla discharge feed is not connected. Scores are indices, not
    calibrated flood probabilities or official warnings.
    """
    import math

    if not math.isfinite(current_cusecs) or current_cusecs < 0:
        raise HTTPException(
            status_code=422,
            detail="current_cusecs must be a finite, non-negative scenario value.",
        )

    weather = fetch_integrated_rainfall_forecasts()
    if weather.get("error") or weather.get("forecast_6h_mm") is None or weather.get("forecast_24h_mm") is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Forecast unavailable; no prediction was generated.",
                "error": weather.get("error") or "Forecast values are missing.",
            },
        )

    try:
        wards = compute_predictive_risk(weather, current_cusecs)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail=f"Prediction could not be computed: {exc}")

    ranked = wards.sort_values("predictive_risk_score", ascending=False)
    score = pd.to_numeric(ranked["predictive_risk_score"], errors="coerce").fillna(0)
    city_average = float(score.mean()) if len(score) else 0.0
    city_max = float(score.max()) if len(score) else 0.0

    prediction_wards = []
    for _, row in ranked.iterrows():
        prediction_wards.append({
            "wardnum": int(row["wardnum"]),
            "name": str(row["name"]),
            "predictive_risk_score": round(float(row["predictive_risk_score"]), 3),
            "risk_level": str(row["predictive_risk_level"]),
            "urban_waterlogging_index": round(float(row["urban_waterlogging_index"]), 3),
            "river_corridor_index": round(float(row["river_corridor_index"]), 3),
            "static_vulnerability": round(float(row["static_vulnerability"]), 3),
            "corridor_flagged": bool(row["corridor_flagged"]),
            "upstream_rainfall_hazard_score": round(float(row["upstream_rainfall_hazard_score"]), 3),
            "upstream_forecast_available": bool(row["upstream_forecast_available"]),
            "forecast_6h_mm": round(float(row["forecast_6h_mm"]), 2),
            "forecast_24h_mm": round(float(row["forecast_24h_mm"]), 2),
        })

    generated_at = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
    advisory_preview = build_advisory_payload(weather, wards, current_cusecs)
    return {
        "status": "prototype_prediction",
        "model_version": "rule_based_predictive_index_v2_3_upstream_advisory",
        "generated_at_local": generated_at,
        "city_summary": {
            "ward_count": len(prediction_wards),
            "average_predictive_risk_index": round(city_average, 3),
            "maximum_predictive_risk_index": round(city_max, 3),
            "high_or_critical_wards": int((score >= 0.6).sum()),
            "critical_wards": int((score >= 0.8).sum()),
        },
        "inputs": {
            "rainfall_source": weather.get("source"),
            "rainfall_data_type": weather.get("data_type"),
            "forecast_6h_mm": weather.get("forecast_6h_mm"),
            "forecast_24h_mm": weather.get("forecast_24h_mm"),
            "forecast_start_local": weather.get("forecast_start_local"),
            "forecast_end_local": weather.get("forecast_end_local"),
            "model_valid_time_local": weather.get("model_valid_time_local"),
            "fetched_at_local": weather.get("fetched_at_local"),
            "rainfall_spatial_method": weather.get("spatial_method"),
            "forecast_ward_count": weather.get("ward_count"),
            "average_forecast_6h_mm": weather.get("forecast_6h_mm"),
            "average_forecast_24h_mm": weather.get("forecast_24h_mm"),
            "max_forecast_6h_mm": weather.get("max_forecast_6h_mm"),
            "max_forecast_24h_mm": weather.get("max_forecast_24h_mm"),
            "wards_over_6h_threshold": weather.get("wards_over_6h_threshold"),
            "wards_over_24h_threshold": weather.get("wards_over_24h_threshold"),
            "upstream_forecast_available": weather.get("upstream_forecast_available", False),
            "upstream_forecast_6h_mm": weather.get("upstream_forecast_6h_mm"),
            "upstream_max_forecast_6h_mm": weather.get("upstream_max_forecast_6h_mm"),
            "upstream_forecast_24h_mm": weather.get("upstream_forecast_24h_mm"),
            "upstream_max_forecast_24h_mm": weather.get("upstream_max_forecast_24h_mm"),
            "upstream_forecasts": weather.get("upstream_forecasts"),
            "upstream_error": weather.get("upstream_error"),
            "discharge_cusecs": float(current_cusecs),
            "discharge_source": "scenario_only_no_verified_live_feed",
        },
        "method": {
            "rainfall_severity": "per ward: max(24h forecast / 115.5 mm, 6h forecast / 64.5 mm), capped at 1; the 6h reference is experimental, not an official threshold",
            "urban_waterlogging_index": "rainfall severity × (0.7 × elevation susceptibility + 0.3 × river proximity susceptibility)",
            "river_corridor_index": "max(upstream reservoir-adjacent rainfall severity when available, configured discharge scenario pressure) × (0.6 × historical discharge-incident susceptibility + 0.4 × river proximity susceptibility)",
            "final_index": "maximum of the urban and river-corridor indices, with existing high-discharge corridor override retained",
            "score_interpretation": "0–1 prototype index, not a calibrated flood probability",
            "alert_priority_threshold": PREDICTIVE_WARD_ALERT_THRESHOLD,
        },
        "limitations": [
            "Rainfall forecasts are queried at ward centroids from a weather model; they are not gauge observations and may share underlying grid cells.",
            "Upstream rainfall is sampled at four reservoir-adjacent reference points, not computed as a catchment-weighted rainfall average.",
            "Discharge is a scenario input; no verified live Khadakwasla feed is connected, and rainfall is not used to infer gate status.",
            "No ward-level drainage capacity, current river level, or live waterlogging sensors are currently modelled.",
            "Weights and thresholds are provisional and require event-based back-testing.",
            "This is an academic prototype, not an official flood warning.",
        ],
        "advisory_preview": advisory_preview,
        "wards": prediction_wards,
    }




@app.get("/prediction/scenario")
def prediction_scenario(
    rain_6h_mm: float = 0.0,
    rain_24h_mm: float = 0.0,
    upstream_6h_mm: float = 0.0,
    upstream_24h_mm: float = 0.0,
    current_cusecs: float = DEFAULT_CUSECS,
):
    """Run a labelled manual scenario without fetching weather or storing alerts."""
    import math

    for label, value in (("rain_6h_mm", rain_6h_mm),
                         ("rain_24h_mm", rain_24h_mm),
                         ("upstream_6h_mm", upstream_6h_mm),
                         ("upstream_24h_mm", upstream_24h_mm),
                         ("current_cusecs", current_cusecs)):
        if not math.isfinite(value) or value < 0:
            raise HTTPException(status_code=422, detail=f"{label} must be finite and non-negative.")

    scenario = {
        "source": "Manual simulation",
        "data_type": "simulated_scenario_not_live",
        "forecast_6h_mm": rain_6h_mm,
        "forecast_24h_mm": rain_24h_mm,
        "upstream_forecast_available": True,
        "upstream_forecast_6h_mm": upstream_6h_mm,
        "upstream_forecast_24h_mm": upstream_24h_mm,
        "upstream_max_forecast_6h_mm": upstream_6h_mm,
        "upstream_max_forecast_24h_mm": upstream_24h_mm,
        "upstream_forecasts": [],
        "upstream_error": None,
        "error": None,
    }
    wards = compute_predictive_risk(scenario, current_cusecs)
    ranked = wards.sort_values("predictive_risk_score", ascending=False)
    score = pd.to_numeric(ranked["predictive_risk_score"], errors="coerce").fillna(0)
    return {
        "status": "SIMULATION_ONLY_NO_ALERT_CREATED",
        "inputs": {
            "rain_6h_mm": rain_6h_mm,
            "rain_24h_mm": rain_24h_mm,
            "upstream_6h_mm": upstream_6h_mm,
            "upstream_24h_mm": upstream_24h_mm,
            "discharge_cusecs": current_cusecs,
        },
        "city_summary": {
            "ward_count": len(ranked),
            "average_predictive_risk_index": round(float(score.mean()), 3),
            "maximum_predictive_risk_index": round(float(score.max()), 3),
            "high_or_critical_wards": int((score >= 0.6).sum()),
            "critical_wards": int((score >= 0.8).sum()),
            "city_wide_static_vulnerability": round(
                float(pd.to_numeric(ranked["static_vulnerability"], errors="coerce").max()), 3
            ),
            "corridor_flagged_wards": int(ranked["corridor_flagged"].astype(bool).sum()),
        },
        "wards": [
            {
                "wardnum": int(row["wardnum"]),
                "name": str(row["name"]),
                "predictive_risk_score": round(float(row["predictive_risk_score"]), 3),
                "risk_level": str(row["predictive_risk_level"]),
                "urban_waterlogging_index": round(float(row["urban_waterlogging_index"]), 3),
                "river_corridor_index": round(float(row["river_corridor_index"]), 3),
                "upstream_rainfall_hazard_score": round(float(row["upstream_rainfall_hazard_score"]), 3),
                "static_vulnerability": round(float(row["static_vulnerability"]), 3),
                "corridor_flagged": bool(row["corridor_flagged"]),
            }
            for _, row in ranked.iterrows()
        ],
    }



# -------------------------------------------------------
# MSG91 SMS TEST ADAPTER — SAFE BY DEFAULT
# -------------------------------------------------------

MSG91_TEST_MESSAGE_TEMPLATE = (
    "PUNE FLOOD EWS: Rain forecast for {#var#} is {#var#} mm in next 6h. "
    "Not an official warning. Avoid flooded roads; follow official updates."
)
MSG91_TEST_COOLDOWN_SECONDS = 300
MSG91_TEST_COOLDOWN_KEY = "floodews:msg91:test_sms_cooldown"


def _msg91_test_config():
    """Read SMS credentials at request time; never expose secret values."""
    authkey = os.getenv("MSG91_AUTHKEY", "").strip()
    template_id = os.getenv("MSG91_TEMPLATE_ID", "").strip()
    mobile = os.getenv("MSG91_TEST_MOBILE", "").strip().replace("+", "")
    enabled = os.getenv("MSG91_ENABLE_LIVE_TEST", "false").strip().lower() in {"1", "true", "yes", "on"}
    location = os.getenv("MSG91_TEST_LOCATION", "Kothrud").strip()[:40] or "Kothrud"
    rain_mm = os.getenv("MSG91_TEST_RAIN_MM", "25").strip()[:40] or "25"
    return {
        "authkey": authkey,
        "template_id": template_id,
        "mobile": mobile,
        "enabled": enabled,
        "location": location,
        "rain_mm": rain_mm,
    }


def _mask_mobile(mobile: str) -> str | None:
    if not mobile:
        return None
    return f"********{mobile[-4:]}"


@app.get("/sms/status")
def sms_status():
    """Show SMS readiness without revealing credentials or sending anything."""
    cfg = _msg91_test_config()
    missing = []
    if not cfg["authkey"]:
        missing.append("MSG91_AUTHKEY")
    if not cfg["template_id"]:
        missing.append("MSG91_TEMPLATE_ID")
    if not cfg["mobile"]:
        missing.append("MSG91_TEST_MOBILE")
    elif not __import__("re").fullmatch(r"91[6-9][0-9]{9}", cfg["mobile"]):
        missing.append("MSG91_TEST_MOBILE (use India format 91XXXXXXXXXX)")
    return {
        "provider": "MSG91",
        "mode": "single_number_live_test_enabled" if cfg["enabled"] else "dry_run_only",
        "live_test_enabled": cfg["enabled"],
        "credentials": {
            "authkey_configured": bool(cfg["authkey"]),
            "template_id_configured": bool(cfg["template_id"]),
            "test_recipient_configured": bool(cfg["mobile"]),
            "test_recipient_masked": _mask_mobile(cfg["mobile"]),
        },
        "missing_or_invalid_configuration": missing,
        "message_template": MSG91_TEST_MESSAGE_TEMPLATE,
        "automatic_forecast_sending": False,
        "production_broadcast_enabled": False,
        "note": "The /sms/test route can target only MSG91_TEST_MOBILE. The advisory scheduler and forecast endpoints do not send SMS.",
    }


@app.post("/sms/test")
def sms_test():
    """Dry-run by default. Live mode can send one test SMS to one env-configured number only."""
    cfg = _msg91_test_config()
    test_message = MSG91_TEST_MESSAGE_TEMPLATE.replace("{#var#}", cfg["location"], 1).replace(
        "{#var#}", cfg["rain_mm"], 1
    )
    preview = {
        "VAR1": cfg["location"],
        "VAR2": cfg["rain_mm"],
        "message": test_message,
    }

    if not cfg["enabled"]:
        return {
            "provider": "MSG91",
            "mode": "dry_run",
            "outbound_request_made": False,
            "sent": False,
            "recipient": _mask_mobile(cfg["mobile"]),
            "preview": preview,
            "configuration_ready_for_live_test": bool(
                cfg["authkey"]
                and cfg["template_id"]
                and __import__("re").fullmatch(r"91[6-9][0-9]{9}", cfg["mobile"])
            ),
            "note": "Dry run only. No provider request or SMS occurred. Configure an approved DLT template before enabling a single-number live test.",
        }

    missing = []
    if not cfg["authkey"]:
        missing.append("MSG91_AUTHKEY")
    if not cfg["template_id"]:
        missing.append("MSG91_TEMPLATE_ID")
    if not __import__("re").fullmatch(r"91[6-9][0-9]{9}", cfg["mobile"]):
        missing.append("MSG91_TEST_MOBILE (use India format 91XXXXXXXXXX)")
    if missing:
        raise HTTPException(status_code=503, detail={
            "message": "Live test blocked; fix configuration first.",
            "missing_or_invalid_configuration": missing,
            "outbound_request_made": False,
        })

    # Fail closed: a live test requires Redis to be healthy, then allows at most
    # one test request every five minutes. The destination is not API-controlled.
    try:
        alerts_redis.ping()
        lock_value = secrets.token_urlsafe(12)
        if not alerts_redis.set(MSG91_TEST_COOLDOWN_KEY, lock_value, ex=MSG91_TEST_COOLDOWN_SECONDS, nx=True):
            raise HTTPException(status_code=429, detail={
                "message": "SMS test cooldown active; wait five minutes before another attempt.",
                "outbound_request_made": False,
            })
    except HTTPException:
        raise
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail={
            "message": "SMS test blocked because Redis could not enforce the safety cooldown.",
            "error_type": type(exc).__name__,
            "outbound_request_made": False,
        })

    payload = {
        "template_id": cfg["template_id"],
        "short_url": "0",
        "realTimeResponse": "1",
        "recipients": [{
            "mobiles": cfg["mobile"],
            "VAR1": cfg["location"],
            "VAR2": cfg["rain_mm"],
        }],
    }
    try:
        response = requests.post(
            "https://control.msg91.com/api/v5/flow",
            headers={
                "accept": "application/json",
                "authkey": cfg["authkey"],
                "content-type": "application/json",
            },
            json=payload,
            timeout=15,
        )
    except requests.RequestException as exc:
        # Keep cooldown after ambiguous network failures so a retry can't
        # accidentally duplicate a request that the provider already received.
        logging.exception("MSG91 test request failed")
        raise HTTPException(status_code=502, detail={
            "message": "MSG91 request could not be confirmed. Cooldown retained to prevent accidental duplicate sends.",
            "error_type": type(exc).__name__,
            "outbound_request_made": True,
            "recipient": _mask_mobile(cfg["mobile"]),
        })

    try:
        provider_response = response.json()
    except ValueError:
        provider_response = response.text[:1000]

    accepted = 200 <= response.status_code < 300
    logging.info(
        "MSG91 test request completed; http_status=%s accepted=%s recipient=%s",
        response.status_code, accepted, _mask_mobile(cfg["mobile"])
    )
    return {
        "provider": "MSG91",
        "mode": "single_number_live_test",
        "outbound_request_made": True,
        "provider_accepted_request": accepted,
        "status": "submitted_to_provider" if accepted else "provider_rejected",
        "sent": False,
        "delivery_confirmed": False,
        "provider_http_status": response.status_code,
        "recipient": _mask_mobile(cfg["mobile"]),
        "preview": preview,
        "provider_response": provider_response,
        "note": "A successful API response does not prove handset delivery. Check MSG91 SMS logs/delivery reports. This route can only target the configured test number; automated forecast SMS remains disabled.",
    }


# -------------------------------------------------------
# TELEGRAM NOTIFICATION TEST ADAPTER — SAFE BY DEFAULT
# -------------------------------------------------------

TELEGRAM_TEST_MESSAGE_TEMPLATE = (
    "PUNE FLOOD EWS: Rain forecast for {location} is {rain_mm} mm in the next 6h. "
    "This is a prototype advisory, not an official warning. Avoid flooded roads; "
    "follow official updates."
)
TELEGRAM_TEST_COOLDOWN_SECONDS = 300
TELEGRAM_TEST_COOLDOWN_KEY = "floodews:telegram:test_message_cooldown"


def _telegram_test_config():
    """Read Telegram test settings at request time; never expose the bot token."""
    return {
        "token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": os.getenv("TELEGRAM_TEST_CHAT_ID", "").strip(),
        "enabled": os.getenv("TELEGRAM_ENABLE_LIVE_TEST", "false").strip().lower()
        in {"1", "true", "yes", "on"},
        "location": os.getenv("TELEGRAM_TEST_LOCATION", "Kothrud").strip()[:40] or "Kothrud",
        "rain_mm": os.getenv("TELEGRAM_TEST_RAIN_MM", "25").strip()[:20] or "25",
    }


def _mask_chat_id(chat_id: str) -> str | None:
    if not chat_id:
        return None
    if len(chat_id) <= 4:
        return "*" * len(chat_id)
    return f"{'*' * (len(chat_id) - 4)}{chat_id[-4:]}"


@app.get("/telegram/status")
def telegram_status():
    """Show Telegram readiness without exposing secrets or sending a message."""
    cfg = _telegram_test_config()
    scheduled_cfg = _telegram_scheduled_config()
    missing = []
    if not cfg["token"]:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not cfg["chat_id"]:
        missing.append("TELEGRAM_TEST_CHAT_ID")
    elif not __import__("re").fullmatch(r"-?[0-9]+", cfg["chat_id"]):
        missing.append("TELEGRAM_TEST_CHAT_ID (must be a numeric Telegram chat ID)")
    return {
        "provider": "Telegram Bot API",
        "mode": (
            "single_chat_live_test_enabled" if cfg["enabled"]
            else "scheduled_alerts_enabled" if scheduled_cfg["enabled"]
            else "dry_run_only"
        ),
        "live_test_enabled": cfg["enabled"],
        "credentials": {
            "bot_token_configured": bool(cfg["token"]),
            "test_chat_id_configured": bool(cfg["chat_id"]),
            "test_chat_id_masked": _mask_chat_id(cfg["chat_id"]),
        },
        "missing_or_invalid_configuration": missing,
        "message_template": TELEGRAM_TEST_MESSAGE_TEMPLATE.format(
            location=cfg["location"], rain_mm=cfg["rain_mm"]
        ),
        "scheduled_delivery_enabled": bool(scheduled_cfg["enabled"]),
        "automatic_forecast_sending": bool(scheduled_cfg["enabled"]),
        "scheduled_private_chat_id_masked": _mask_chat_id(scheduled_cfg["chat_id"]),
        "production_broadcast_enabled": False,
        "note": "Manual test routes target TELEGRAM_TEST_CHAT_ID only. Scheduled messages are separately opt-in via TELEGRAM_ENABLE_SCHEDULED_ALERTS=true and require a fresh live local/upstream rainfall-watch trigger plus Redis deduplication; simulations and stale forecasts are blocked.",
    }


@app.post("/telegram/test")
def telegram_test():
    """Dry-run by default; live test targets only the one configured chat ID."""
    cfg = _telegram_test_config()
    message = TELEGRAM_TEST_MESSAGE_TEMPLATE.format(
        location=cfg["location"], rain_mm=cfg["rain_mm"]
    )
    preview = {
        "location": cfg["location"],
        "forecast_6h_mm": cfg["rain_mm"],
        "message": message,
    }

    valid_chat_id = bool(__import__("re").fullmatch(r"-?[0-9]+", cfg["chat_id"]))
    if not cfg["enabled"]:
        return {
            "provider": "Telegram Bot API",
            "mode": "dry_run",
            "outbound_request_made": False,
            "sent": False,
            "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
            "preview": preview,
            "configuration_ready_for_live_test": bool(cfg["token"] and valid_chat_id),
            "note": "Dry run only. No Telegram API request or message occurred. Live sending requires TELEGRAM_ENABLE_LIVE_TEST=true and a verified chat ID.",
        }

    missing = []
    if not cfg["token"]:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not valid_chat_id:
        missing.append("TELEGRAM_TEST_CHAT_ID (must be a numeric Telegram chat ID)")
    if missing:
        raise HTTPException(status_code=503, detail={
            "message": "Telegram live test blocked; fix configuration first.",
            "missing_or_invalid_configuration": missing,
            "outbound_request_made": False,
        })

    # Fail closed: Redis must be healthy to enforce cooldown before any send.
    try:
        alerts_redis.ping()
        lock_value = secrets.token_urlsafe(12)
        if not alerts_redis.set(
            TELEGRAM_TEST_COOLDOWN_KEY, lock_value,
            ex=TELEGRAM_TEST_COOLDOWN_SECONDS, nx=True
        ):
            raise HTTPException(status_code=429, detail={
                "message": "Telegram test cooldown active; wait five minutes before another attempt.",
                "outbound_request_made": False,
            })
    except HTTPException:
        raise
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail={
            "message": "Telegram test blocked because Redis could not enforce the safety cooldown.",
            "error_type": type(exc).__name__,
            "outbound_request_made": False,
        })

    # The destination and content are environment-configured; request input cannot change either.
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": cfg["chat_id"], "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as exc:
        # Keep cooldown after ambiguous network errors to avoid accidental duplicates.
        logging.exception("Telegram test request failed")
        raise HTTPException(status_code=502, detail={
            "message": "Telegram request could not be confirmed. Cooldown retained to prevent accidental duplicate messages.",
            "error_type": type(exc).__name__,
            "outbound_request_made": True,
            "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
        })

    try:
        provider_response = response.json()
    except ValueError:
        provider_response = {"description": response.text[:500]}

    accepted = 200 <= response.status_code < 300 and bool(
        provider_response.get("ok") if isinstance(provider_response, dict) else False
    )
    logging.info(
        "Telegram test request completed; http_status=%s accepted=%s chat_id=%s",
        response.status_code, accepted, _mask_chat_id(cfg["chat_id"])
    )
    result = {
        "provider": "Telegram Bot API",
        "mode": "single_chat_live_test",
        "outbound_request_made": True,
        "provider_accepted_request": accepted,
        "status": "submitted_to_telegram" if accepted else "provider_rejected",
        "telegram_api_accepted": accepted,
        "sent": accepted,
        "delivery_confirmed": False,
        "provider_http_status": response.status_code,
        "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
        "preview": preview,
        "note": "Telegram accepted the sendMessage request, but this does not confirm the recipient read or saw a device notification." if accepted else "Telegram did not accept the message; inspect provider_error without sharing the bot token.",
    }
    if not accepted:
        # Provider descriptions are useful diagnostically; never return a URL/token.
        description = provider_response.get("description") if isinstance(provider_response, dict) else None
        result["provider_error"] = str(description or "Telegram API rejected the request")[:300]
    return result


@app.get("/advisory/forecast")
def advisory_forecast(current_cusecs: float = DEFAULT_CUSECS):
    """Return live forecast-derived human-readable advisory previews; sends no messages."""
    import math
    if not math.isfinite(current_cusecs) or current_cusecs < 0:
        raise HTTPException(status_code=422, detail="current_cusecs must be finite and non-negative.")
    weather = fetch_integrated_rainfall_forecasts()
    if weather.get("error") or not weather.get("ward_forecasts"):
        raise HTTPException(status_code=503, detail={
            "message": "Ward forecast unavailable; no advisory generated.",
            "error": weather.get("error") or "Ward forecast locations are missing.",
        })
    try:
        wards = compute_predictive_risk(weather, current_cusecs)
        advisory = build_advisory_payload(weather, wards, current_cusecs)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail=f"Advisory could not be generated: {exc}")

    try:
        run_id = record_advisory_run(weather, advisory, current_cusecs, triggered_by="api_request")
        advisory["history_run_id"] = run_id
        advisory["history_saved"] = True
    except Exception as exc:
        logging.exception("Could not persist advisory history snapshot")
        advisory["history_run_id"] = None
        advisory["history_saved"] = False
        advisory["history_error"] = f"{type(exc).__name__}: {exc}"
    advisory["input_metadata"] = {
        "local_source": weather.get("source"),
        "local_data_type": weather.get("data_type"),
        "local_model_valid_time_local": weather.get("model_valid_time_local"),
        "local_fetched_at_local": weather.get("fetched_at_local"),
        "local_forecast_start_local": weather.get("forecast_start_local"),
        "local_forecast_end_local": weather.get("forecast_end_local"),
        "upstream_model_valid_time_local": weather.get("upstream_model_valid_time_local"),
        "upstream_fetched_at_local": weather.get("upstream_fetched_at_local"),
        "discharge_scenario_cusecs": float(current_cusecs),
        "discharge_is_verified": False,
    }
    return advisory


@app.get("/advisory/scenario")
def advisory_scenario(
    local_6h_mm: float = 0.0,
    local_24h_mm: float = 0.0,
    upstream_6h_mm: float = 0.0,
    upstream_24h_mm: float = 0.0,
    current_cusecs: float = DEFAULT_CUSECS,
):
    """Create advisory/SMS previews from an explicitly simulated scenario; no messages are sent."""
    import math
    values = {
        "local_6h_mm": local_6h_mm,
        "local_24h_mm": local_24h_mm,
        "upstream_6h_mm": upstream_6h_mm,
        "upstream_24h_mm": upstream_24h_mm,
        "current_cusecs": current_cusecs,
    }
    for name, value in values.items():
        if not math.isfinite(value) or value < 0:
            raise HTTPException(status_code=422, detail=f"{name} must be finite and non-negative.")
    from datetime import timedelta
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    scenario = {
        "source": "Manual simulation",
        "data_type": "simulated_scenario_not_live",
        "spatial_method": "same simulated local rainfall applied to all wards; upstream rainfall supplied as a separate scenario",
        "forecast_6h_mm": local_6h_mm,
        "forecast_24h_mm": local_24h_mm,
        "max_forecast_6h_mm": local_6h_mm,
        "max_forecast_24h_mm": local_24h_mm,
        "forecast_start_local": now.isoformat(timespec="seconds"),
        "forecast_end_local": (now + timedelta(hours=24)).isoformat(timespec="seconds"),
        "model_valid_time_local": now.isoformat(timespec="seconds"),
        "fetched_at_local": now.isoformat(timespec="seconds"),
        "upstream_forecast_available": True,
        "upstream_forecast_6h_mm": upstream_6h_mm,
        "upstream_forecast_24h_mm": upstream_24h_mm,
        "upstream_max_forecast_6h_mm": upstream_6h_mm,
        "upstream_max_forecast_24h_mm": upstream_24h_mm,
        "upstream_forecasts": [],
        "upstream_fetched_at_local": now.isoformat(timespec="seconds"),
        "upstream_spatial_method": "manual scenario, not live catchment data",
        "upstream_error": None,
        "error": None,
    }
    try:
        wards = compute_predictive_risk(scenario, current_cusecs)
        advisory = build_advisory_payload(scenario, wards, current_cusecs)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Scenario advisory failed: {exc}")
    advisory["status_mode"] = "SIMULATION_ONLY_NO_ALERT_CREATED"
    advisory["inputs"] = values
    advisory["delivery"] = {
        "sent": False,
        "provider_connected": False,
        "note": "Simulation only; no SMS, database record, or alert was created.",
    }
    return advisory


# -------------------------------------------------------
# FORECAST-DERIVED TELEGRAM PREVIEW / ONE-CHAT TEST
# -------------------------------------------------------

TELEGRAM_FORECAST_TEST_COOLDOWN_SECONDS = 900


def _latest_live_advisory_for_telegram():
    """Create a fresh forecast advisory; never use the simulation endpoint."""
    advisory = advisory_forecast(current_cusecs=DEFAULT_CUSECS)
    if advisory.get("mode") != "FORECAST_ADVISORY_PREVIEW":
        raise HTTPException(status_code=409, detail={
            "message": "Only a fresh live forecast advisory can be considered for delivery.",
            "outbound_request_made": False,
        })
    return advisory


def _telegram_ward_preview_state(advisory: dict, wardnum: int):
    """Resolve a selected ward's preview and whether the current live data permits a test."""
    reasons = advisory.get("trigger_reason_codes", [])
    local_watch = "LOCAL_RAINFALL_WATCH" in reasons
    previews = advisory.get("sms_previews", [])
    selected = next(
        (item for item in previews if int(item.get("wardnum", -1)) == int(wardnum)),
        None,
    )
    if advisory.get("mode") == "SIMULATION_ONLY":
        blocker = "Simulation outputs are never eligible for Telegram delivery."
    elif not local_watch:
        blocker = "The latest live forecast has no local-rainfall watch trigger; no ward alert should be sent."
    elif selected is None:
        blocker = "The selected ward has no generated preview in the current live advisory."
    else:
        blocker = None
    return selected, bool(local_watch and selected is not None), blocker


@app.get("/telegram/forecast-preview")
def telegram_forecast_preview(wardnum: int = 34):
    """Generate a fresh live forecast and preview one ward; this route never sends."""
    if wardnum < 1 or wardnum > 1000:
        raise HTTPException(status_code=422, detail="wardnum must be a positive ward number.")
    advisory = _latest_live_advisory_for_telegram()
    selected, eligible, blocker = _telegram_ward_preview_state(advisory, wardnum)
    return {
        "provider": "Telegram Bot API",
        "mode": "forecast_preview_only",
        "outbound_request_made": False,
        "sent": False,
        "selected_wardnum": wardnum,
        "selected_ward_name": selected.get("ward_name") if selected else None,
        "advisory_status": advisory.get("status"),
        "trigger_reason_codes": advisory.get("trigger_reason_codes", []),
        "forecast_generated_at_local": advisory.get("generated_at_local"),
        "history_run_id": advisory.get("history_run_id"),
        "message_preview": selected.get("message") if selected else None,
        "available_preview_wards": [
            {"wardnum": int(item.get("wardnum")), "ward_name": item.get("ward_name"),
             "risk_level": item.get("risk_level"), "risk_index": item.get("risk_index")}
            for item in advisory.get("sms_previews", [])
        ],
        "eligible_for_single_chat_test": eligible,
        "blocking_reason": blocker,
        "automatic_forecast_sending": False,
        "production_broadcast_enabled": False,
        "note": "Fresh live forecast only; this endpoint never sends. Simulated scenarios are not accepted by this route.",
    }


@app.post("/telegram/forecast-test")
def telegram_forecast_test(wardnum: int = 34):
    """Explicit test of a current live ward advisory to TELEGRAM_TEST_CHAT_ID only."""
    cfg = _telegram_test_config()
    valid_chat_id = bool(__import__("re").fullmatch(r"-?[0-9]+", cfg["chat_id"]))
    missing = []
    if not cfg["token"]:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not valid_chat_id:
        missing.append("TELEGRAM_TEST_CHAT_ID (must be a numeric Telegram chat ID)")
    if missing:
        raise HTTPException(status_code=503, detail={
            "message": "Telegram forecast test blocked; fix configuration first.",
            "missing_or_invalid_configuration": missing,
            "outbound_request_made": False,
        })
    if not cfg["enabled"]:
        return {
            "provider": "Telegram Bot API",
            "mode": "dry_run",
            "outbound_request_made": False,
            "sent": False,
            "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
            "note": "Live test disabled. Set TELEGRAM_ENABLE_LIVE_TEST=true to permit an explicitly requested test to your configured chat.",
        }
    if wardnum < 1 or wardnum > 1000:
        raise HTTPException(status_code=422, detail="wardnum must be a positive ward number.")

    # Fetch the current real forecast, never the manual scenario endpoint.
    advisory = _latest_live_advisory_for_telegram()
    selected, eligible, blocker = _telegram_ward_preview_state(advisory, wardnum)
    if not eligible or selected is None:
        return {
            "provider": "Telegram Bot API",
            "mode": "blocked_no_current_ward_watch",
            "outbound_request_made": False,
            "sent": False,
            "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
            "advisory_status": advisory.get("status"),
            "trigger_reason_codes": advisory.get("trigger_reason_codes", []),
            "selected_wardnum": wardnum,
            "selected_ward_name": selected.get("ward_name") if selected else None,
            "message_preview": selected.get("message") if selected else None,
            "available_preview_wards": [
                {"wardnum": int(item.get("wardnum")), "ward_name": item.get("ward_name"),
                 "risk_level": item.get("risk_level"), "risk_index": item.get("risk_index")}
                for item in advisory.get("sms_previews", [])
            ],
            "blocking_reason": blocker,
            "note": "No message was sent because the current live forecast does not justify a local ward rainfall-watch message.",
        }

    # Explicit test wording prevents confusing a developer's own-chat check with a public warning.
    message = (
        "DEVELOPER TEST — NOT AN OFFICIAL OR PUBLIC WARNING\n\n"
        + str(selected["message"])
    )
    cooldown_key = f"floodews:telegram:forecast_ward_test:{int(wardnum)}"
    try:
        alerts_redis.ping()
        lock_value = secrets.token_urlsafe(12)
        if not alerts_redis.set(
            cooldown_key, lock_value,
            ex=TELEGRAM_FORECAST_TEST_COOLDOWN_SECONDS, nx=True
        ):
            raise HTTPException(status_code=429, detail={
                "message": "Forecast advisory test cooldown active for this ward; wait 15 minutes before retrying.",
                "outbound_request_made": False,
            })
    except HTTPException:
        raise
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail={
            "message": "Telegram forecast test blocked because Redis could not enforce the duplicate-send cooldown.",
            "error_type": type(exc).__name__,
            "outbound_request_made": False,
        })

    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": cfg["chat_id"], "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as exc:
        logging.exception("Telegram forecast-advisory test request failed")
        raise HTTPException(status_code=502, detail={
            "message": "Telegram request could not be confirmed. Cooldown retained to prevent accidental duplicates.",
            "error_type": type(exc).__name__,
            "outbound_request_made": True,
            "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
        })

    try:
        provider_response = response.json()
    except ValueError:
        provider_response = {"description": response.text[:500]}
    accepted = 200 <= response.status_code < 300 and bool(
        provider_response.get("ok") if isinstance(provider_response, dict) else False
    )
    result = {
        "provider": "Telegram Bot API",
        "mode": "single_chat_live_forecast_test",
        "outbound_request_made": True,
        "provider_accepted_request": accepted,
        "status": "submitted_to_telegram" if accepted else "provider_rejected",
        "telegram_api_accepted": accepted,
        "sent": accepted,
        "delivery_confirmed": False,
        "provider_http_status": response.status_code,
        "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
        "selected_wardnum": wardnum,
        "selected_ward_name": selected.get("ward_name"),
        "advisory_status": advisory.get("status"),
        "history_run_id": advisory.get("history_run_id"),
        "message_preview": message,
        "automatic_forecast_sending": False,
        "production_broadcast_enabled": False,
        "note": "This endpoint only targets the configured developer chat and only after a fresh live local-rainfall watch. Telegram API acceptance does not confirm that the message was read." if accepted else "Telegram rejected the request; inspect provider_error without sharing the bot token.",
    }
    if not accepted:
        description = provider_response.get("description") if isinstance(provider_response, dict) else None
        result["provider_error"] = str(description or "Telegram API rejected the request")[:300]
    logging.info(
        "Telegram live forecast test completed; http_status=%s accepted=%s chat_id=%s wardnum=%s",
        response.status_code, accepted, _mask_chat_id(cfg["chat_id"]), wardnum
    )
    return result


# -------------------------------------------------------
# SCHEDULED TELEGRAM ALERTS — ONE PRIVATE CHAT, OFF BY DEFAULT
# -------------------------------------------------------

TELEGRAM_SCHEDULED_DEDUPE_SECONDS = 6 * 60 * 60
TELEGRAM_SCHEDULED_MAX_FETCH_AGE_SECONDS = 30 * 60
TELEGRAM_SCHEDULED_MAX_MODEL_AGE_SECONDS = 3 * 60 * 60
TELEGRAM_SCHEDULED_DEDUPE_PREFIX = "floodews:telegram:scheduled_watch:"


def _telegram_scheduled_config():
    """Separate opt-in for automated scheduler messages; never expose the token."""
    test_cfg = _telegram_test_config()
    return {
        "token": test_cfg["token"],
        # Keep automated delivery confined to the one explicitly configured private chat.
        "chat_id": test_cfg["chat_id"],
        "enabled": os.getenv("TELEGRAM_ENABLE_SCHEDULED_ALERTS", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    }


def _parse_scheduler_timestamp(value, field_name):
    if not value:
        raise ValueError(f"{field_name} is missing")
    parsed = datetime.fromisoformat(str(value))
    tz = ZoneInfo("Asia/Kolkata")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _live_forecast_readiness_blocker(forecast: dict, now=None):
    """Fail closed before storing or sending alerts on incomplete/stale forecasts."""
    # ALERT PIPELINE HARDENING v1
    import math
    from datetime import timedelta
    if not isinstance(forecast, dict):
        return "Forecast data is missing or invalid."
    if forecast.get("error"):
        return "Fresh ward forecasts are unavailable; no scheduled message is permitted."
    data_type = str(forecast.get("data_type", "")).lower()
    source = str(forecast.get("source", "")).lower()
    if "simulat" in data_type or "simulat" in source or "manual simulation" in source:
        return "Simulated inputs are never eligible for scheduled Telegram delivery."
    ward_forecasts = forecast.get("ward_forecasts")
    if not isinstance(ward_forecasts, list) or not ward_forecasts:
        return "A complete, non-empty live ward forecast is required."
    try:
        declared_count = float(forecast.get("ward_count"))
    except (TypeError, ValueError, OverflowError):
        return "Reported ward count is missing or invalid."
    if (not math.isfinite(declared_count) or not declared_count.is_integer()
            or int(declared_count) != len(ward_forecasts)):
        return "Ward forecast count does not match the received ward rows."

    now = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    else:
        now = now.astimezone(ZoneInfo("Asia/Kolkata"))
    try:
        fetched_at = _parse_scheduler_timestamp(forecast.get("fetched_at_local"), "fetched_at_local")
    except (TypeError, ValueError, OverflowError):
        return "Forecast fetch timestamp is missing or invalid."
    fetch_age = (now - fetched_at).total_seconds()
    if fetch_age < -300:
        return "Forecast fetch timestamp is unexpectedly in the future."
    if fetch_age > TELEGRAM_SCHEDULED_MAX_FETCH_AGE_SECONDS:
        return "Forecast is older than the 30-minute freshness limit."

    try:
        model_valid_at = _parse_scheduler_timestamp(
            forecast.get("model_valid_time_local"), "model_valid_time_local"
        )
    except (TypeError, ValueError, OverflowError):
        return "Weather-model valid time is missing or invalid."
    model_age = (now - model_valid_at).total_seconds()
    if model_age < -3600:
        return "Weather-model valid time is unexpectedly far in the future."
    if model_age > TELEGRAM_SCHEDULED_MAX_MODEL_AGE_SECONDS:
        return "Weather-model timestamp is older than the 3-hour freshness limit."

    # Validate the forecast window itself, not just the time the HTTP response arrived.
    try:
        forecast_start = _parse_scheduler_timestamp(
            forecast.get("forecast_start_local"), "forecast_start_local"
        )
        forecast_end = _parse_scheduler_timestamp(
            forecast.get("forecast_end_local"), "forecast_end_local"
        )
    except (TypeError, ValueError, OverflowError):
        return "Forecast window start/end timestamp is missing or invalid."
    if forecast_start <= model_valid_at:
        return "Forecast window does not start after the model valid time."
    window_hours = (forecast_end - forecast_start).total_seconds() / 3600.0
    if forecast_end <= forecast_start or not 22.0 <= window_hours <= 24.5:
        return "Forecast window does not cover approximately 24 hourly samples."
    if forecast_start < now - timedelta(minutes=30):
        return "Forecast window starts more than 30 minutes in the past."
    if forecast_start > now + timedelta(hours=2):
        return "Forecast window starts unexpectedly far in the future."
    if forecast_end < now + timedelta(hours=18):
        return "Forecast horizon ends too soon to support a 24-hour outlook."

    # Never allow absent or invalid rain values to be silently interpreted as zero.
    try:
        for row in ward_forecasts:
            for field in ("forecast_6h_mm", "forecast_24h_mm"):
                value = float(row[field])
                if not math.isfinite(value) or value < 0:
                    return "Ward forecast contains invalid rainfall values."
    except (KeyError, TypeError, ValueError):
        return "Ward forecast has missing or invalid rainfall values."
    return None


def _build_telegram_scheduler_message(advisory: dict, forecast: dict) -> str:
    """Build a concise prototype WATCH message; never claim an official warning."""
    reason_codes = set(advisory.get("trigger_reason_codes", []))
    summary = advisory.get("local_rainfall_summary") or {}
    upstream = advisory.get("upstream_rainfall") or {}
    lines = [
        "PUNE FLOOD EWS — PROTOTYPE WATCH",
        "Model-based rainfall advisory; NOT an official warning.",
        f"Forecast fetched: {forecast.get('fetched_at_local') or 'time unavailable'}",
        f"Forecast model valid at: {forecast.get('model_valid_time_local') or 'time unavailable'}",
        f"Forecast window (local): {forecast.get('forecast_start_local') or 'time unavailable'} to {forecast.get('forecast_end_local') or 'time unavailable'}",
    ]
    if "LOCAL_RAINFALL_WATCH" in reason_codes:
        lines.append(
            "Local rain watch: max "
            f"{float(summary.get('max_6h_mm') or 0):.1f} mm/6h, "
            f"{float(summary.get('max_24h_mm') or 0):.1f} mm/24h; "
            f"{int(summary.get('wards_crossing_local_watch_threshold') or 0)} ward(s) cross experimental thresholds."
        )
    if "UPSTREAM_CATCHMENT_RAIN_WATCH" in reason_codes:
        lines.append(
            "Upstream reference-point rain watch: max "
            f"{float(upstream.get('max_6h_mm') or 0):.1f} mm/6h and "
            f"{float(upstream.get('max_24h_mm') or 0):.1f} mm/24h."
        )
        lines.append("Upstream rain does not establish dam-gate opening, discharge, or downstream flooding.")

    try:
        from ml_outlook import telegram_line
        ml_line = telegram_line(forecast)
        if ml_line:
            lines.append(ml_line)
    except Exception:
        pass

    previews = advisory.get("sms_previews") or []
    if previews:
        lines.append("Priority ward previews:")
        for item in previews[:3]:
            try:
                rain6 = float(item.get("forecast_6h_mm", 0))
                rain24 = float(item.get("forecast_24h_mm", 0))
            except (TypeError, ValueError):
                rain6, rain24 = 0.0, 0.0
            wardnum = item.get("wardnum", "?")
            ward_name = str(item.get("ward_name") or f"Ward {wardnum}").replace("\n", " ")[:90]
            lines.append(
                f"- {ward_name} (Ward #{wardnum}): "
                f"forecast {rain6:.1f} mm/6h and {rain24:.1f} mm/24h."
            )
    lines.extend([
        "Avoid flooded roads and riverbanks; follow official PMC/IMD/disaster updates.",
        "Rainfall forecasts are model estimates, not gauge observations or confirmation of flooding.",
    ])
    return "\n".join(lines)[:3900]


def _evaluate_telegram_scheduler_forecast(forecast: dict, advisory: dict | None = None):
    """Return eligibility and message for a live forecast; never sends by itself."""
    blocker = _live_forecast_readiness_blocker(forecast)
    if blocker:
        return {
            "status": "blocked_forecast_not_fresh",
            "eligible": False,
            "would_notify": False,
            "trigger_reason_codes": [],
            "message_preview": None,
            "blocking_reason": blocker,
        }
    try:
        if advisory is None:
            wards = compute_predictive_risk(forecast, DEFAULT_CUSECS)
            advisory = build_advisory_payload(forecast, wards, DEFAULT_CUSECS)
    except Exception as exc:
        return {
            "status": "blocked_advisory_error",
            "eligible": False,
            "would_notify": False,
            "trigger_reason_codes": [],
            "message_preview": None,
            "blocking_reason": f"Advisory generation failed ({type(exc).__name__}).",
        }
    if advisory.get("mode") != "FORECAST_ADVISORY_PREVIEW":
        return {
            "status": "blocked_not_live_forecast",
            "eligible": False,
            "would_notify": False,
            "trigger_reason_codes": [],
            "message_preview": None,
            "blocking_reason": "Only fresh live forecast-derived advisories can be sent.",
        }

    reasons = list(advisory.get("trigger_reason_codes") or [])
    rainfall_reasons = [code for code in reasons if code in {
        "LOCAL_RAINFALL_WATCH", "UPSTREAM_CATCHMENT_RAIN_WATCH"
    }]
    if not rainfall_reasons:
        return {
            "status": "no_rainfall_watch_trigger",
            "eligible": False,
            "would_notify": False,
            "advisory_status": advisory.get("status"),
            "trigger_reason_codes": reasons,
            "message_preview": None,
            "blocking_reason": "No experimental local or upstream rainfall-watch threshold is met.",
            "forecast_fetched_at_local": forecast.get("fetched_at_local"),
            "forecast_model_valid_time_local": forecast.get("model_valid_time_local"),
            "forecast_start_local": forecast.get("forecast_start_local"),
            "forecast_end_local": forecast.get("forecast_end_local"),
        }
    message = _build_telegram_scheduler_message(advisory, forecast)
    return {
        "status": "watch_eligible",
        "eligible": True,
        "would_notify": True,
        "advisory_status": advisory.get("status"),
        "trigger_reason_codes": rainfall_reasons,
        "message_preview": message,
        "blocking_reason": None,
        "forecast_fetched_at_local": forecast.get("fetched_at_local"),
        "forecast_model_valid_time_local": forecast.get("model_valid_time_local"),
        "forecast_end_local": forecast.get("forecast_end_local"),
    }


def _telegram_scheduled_dedupe_key(chat_id: str) -> str:
    digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]
    return f"{TELEGRAM_SCHEDULED_DEDUPE_PREFIX}{digest}"


# A new model-based update is allowed only when a second independent watch
# category becomes active during the existing six-hour private-chat cooldown.
# This is NOT a severity scale and does not use the uncalibrated risk index.
_TELEGRAM_SCHEDULED_RESERVATION_LUA = r"""
local current = redis.call('GET', KEYS[1])
local new_mask = tonumber(ARGV[1])
local ttl_seconds = tonumber(ARGV[2])
if not new_mask or not ttl_seconds then
    return redis.error_reply('invalid reservation arguments')
end
if not current then
    local created = redis.call('SET', KEYS[1], tostring(new_mask), 'NX', 'EX', ttl_seconds)
    if created then return 1 end
    return 0
end

-- Legacy random-token values are deliberately treated as an active cooldown.
local old_mask = tonumber(current)
if not old_mask then return 0 end

-- Permit a single additional-signal update: local-only -> combined, or
-- upstream-only -> combined. Preserve the original cooldown expiry.
if (old_mask == 1 or old_mask == 2) and new_mask == 3 then
    local remaining_ms = redis.call('PTTL', KEYS[1])
    if remaining_ms > 0 then
        redis.call('SET', KEYS[1], tostring(new_mask), 'XX', 'PX', remaining_ms)
        return 2
    end
end
return 0
"""


def _telegram_trigger_mask(reason_codes):
    """Bit 1=local rainfall watch; bit 2=upstream rainfall watch."""
    reasons = set(reason_codes or [])
    mask = 0
    if "LOCAL_RAINFALL_WATCH" in reasons:
        mask |= 1
    if "UPSTREAM_CATCHMENT_RAIN_WATCH" in reasons:
        mask |= 2
    return mask


def _reserve_telegram_scheduled_alert_slot(redis_client, key, trigger_mask, cooldown_seconds):
    """Return 1 for a new watch, 2 for a new combined-signal update, 0 if suppressed."""
    mask = int(trigger_mask)
    if mask not in (1, 2, 3):
        raise ValueError("A qualifying local and/or upstream rainfall trigger is required.")
    result = redis_client.eval(
        _TELEGRAM_SCHEDULED_RESERVATION_LUA,
        1,
        key,
        str(mask),
        int(cooldown_seconds),
    )
    return int(result or 0)


def _process_scheduled_telegram_alert(forecast: dict, advisory: dict | None = None):
    """Scheduler-only send path: private chat, live rainfall watch, Redis dedupe."""
    cfg = _telegram_scheduled_config()
    evaluation = _evaluate_telegram_scheduler_forecast(forecast, advisory)
    base = {
        "eligible": bool(evaluation.get("eligible")),
        "would_notify": bool(evaluation.get("would_notify")),
        "trigger_reason_codes": evaluation.get("trigger_reason_codes", []),
        "message_preview": evaluation.get("message_preview"),
        "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
        "outbound_request_made": False,
        "sent": False,
        "scheduled_delivery_enabled": bool(cfg["enabled"]),
        "production_broadcast_enabled": False,
    }
    if not evaluation.get("eligible"):
        return {**base, "status": evaluation.get("status"), "blocking_reason": evaluation.get("blocking_reason")}
    if not cfg["enabled"]:
        return {**base, "status": "dry_run_triggered", "note": "A live rainfall-watch preview exists, but scheduled sending is disabled. No Telegram request was made."}

    valid_chat_id = bool(__import__("re").fullmatch(r"[0-9]+", cfg["chat_id"])) and int(cfg["chat_id"] or "0") > 0
    if not cfg["token"] or not valid_chat_id:
        return {
            **base,
            "status": "blocked_configuration_missing",
            "blocking_reason": "Scheduled Telegram needs a bot token and one numeric private TELEGRAM_TEST_CHAT_ID.",
        }

    dedupe_key = _telegram_scheduled_dedupe_key(cfg["chat_id"])
    trigger_mask = _telegram_trigger_mask(evaluation.get("trigger_reason_codes", []))
    if trigger_mask not in (1, 2, 3):
        return {
            **base,
            "status": "blocked_invalid_trigger_state",
            "blocking_reason": "No recognized local/upstream rainfall trigger was available for deduplication.",
        }
    try:
        alerts_redis.ping()
        reservation = _reserve_telegram_scheduled_alert_slot(
            alerts_redis,
            dedupe_key,
            trigger_mask,
            TELEGRAM_SCHEDULED_DEDUPE_SECONDS,
        )
        if reservation == 0:
            return {
                **base,
                "status": "duplicate_suppressed",
                "blocking_reason": "The private-chat six-hour cooldown is active. Only a newly combined local+upstream trigger may generate one additional update; legacy cooldowns remain suppressed until expiry.",
            }
    except redis.RedisError as exc:
        return {
            **base,
            "status": "blocked_redis_unavailable",
            "blocking_reason": "Redis is required to enforce duplicate suppression; no Telegram request was made.",
            "error_type": type(exc).__name__,
        }

    alert_update = reservation == 2
    message_to_send = evaluation["message_preview"]
    if alert_update:
        message_to_send = (
            "UPDATE — Both local and upstream rainfall-watch triggers are now active. "
            "This adds a second model-based signal; it does not confirm flooding.\n\n"
            + message_to_send
        )
        base["message_preview"] = message_to_send
        base["update_kind"] = "additional_trigger_category"
    else:
        base["update_kind"] = "initial_watch"

    # Keep the reservation for all outcomes, including explicit provider rejection or
    # ambiguous network failure. This is conservative: do not retry automatically.
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
            json={"chat_id": cfg["chat_id"], "text": message_to_send, "disable_web_page_preview": True},
            timeout=15,
        )
        try:
            provider_response = response.json()
        except ValueError:
            provider_response = {"description": response.text[:300]}
        accepted = 200 <= response.status_code < 300 and bool(
            provider_response.get("ok") if isinstance(provider_response, dict) else False
        )
    except requests.RequestException as exc:
        logging.exception("Scheduled Telegram request outcome is ambiguous; dedupe retained.")
        return {
            **base,
            "status": "request_outcome_unknown",
            "outbound_request_made": True,
            "blocking_reason": "Telegram request outcome could not be confirmed; the six-hour duplicate guard remains active.",
            "error_type": type(exc).__name__,
        }

    response_result = {
        **base,
        "status": "submitted_to_telegram" if accepted else "provider_rejected",
        "outbound_request_made": True,
        "provider_http_status": response.status_code,
        "provider_accepted_request": accepted,
        "telegram_api_accepted": accepted,
        "sent": accepted,
        "delivery_confirmed": False,
        "dedupe_cooldown_seconds": TELEGRAM_SCHEDULED_DEDUPE_SECONDS,
        "note": "Telegram accepted the request; this does not confirm the recipient read or saw a device notification." if accepted else "Telegram rejected the request. Duplicate guard retained for safety; inspect logs without sharing secrets.",
    }
    if not accepted:
        description = provider_response.get("description") if isinstance(provider_response, dict) else None
        response_result["provider_error"] = str(description or "Telegram API rejected the request")[:250]
    logging.info(
        "Scheduled Telegram check: status=%s accepted=%s chat_id=%s trigger_codes=%s",
        response_result["status"], accepted, _mask_chat_id(cfg["chat_id"]), evaluation.get("trigger_reason_codes", []),
    )
    return response_result


@app.get("/telegram/scheduler/scenario-preview")
def telegram_scheduler_scenario_preview(
    local_6h_mm: float = 35.0,
    local_24h_mm: float = 80.0,
    upstream_6h_mm: float = 35.0,
    upstream_24h_mm: float = 80.0,
    current_cusecs: float = DEFAULT_CUSECS,
):
    """Test message formatting/rainfall triggers with simulated inputs; never delivery-eligible."""
    import math
    from datetime import timedelta
    values = {
        "local_6h_mm": local_6h_mm,
        "local_24h_mm": local_24h_mm,
        "upstream_6h_mm": upstream_6h_mm,
        "upstream_24h_mm": upstream_24h_mm,
        "current_cusecs": current_cusecs,
    }
    for name, value in values.items():
        if not math.isfinite(value) or value < 0:
            raise HTTPException(status_code=422, detail=f"{name} must be finite and non-negative.")
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    scenario = {
        "source": "Manual simulation",
        "data_type": "simulated_scenario_not_live",
        "spatial_method": "same simulated local rainfall applied to all wards; upstream rainfall supplied as a separate scenario",
        "forecast_6h_mm": local_6h_mm,
        "forecast_24h_mm": local_24h_mm,
        "max_forecast_6h_mm": local_6h_mm,
        "max_forecast_24h_mm": local_24h_mm,
        "forecast_start_local": now.isoformat(timespec="seconds"),
        "forecast_end_local": (now + timedelta(hours=24)).isoformat(timespec="seconds"),
        "model_valid_time_local": now.isoformat(timespec="seconds"),
        "fetched_at_local": now.isoformat(timespec="seconds"),
        "upstream_forecast_available": True,
        "upstream_forecast_6h_mm": upstream_6h_mm,
        "upstream_forecast_24h_mm": upstream_24h_mm,
        "upstream_max_forecast_6h_mm": upstream_6h_mm,
        "upstream_max_forecast_24h_mm": upstream_24h_mm,
        "upstream_forecasts": [],
        "upstream_fetched_at_local": now.isoformat(timespec="seconds"),
        "upstream_spatial_method": "manual scenario, not live catchment data",
        "upstream_error": None,
        "error": None,
    }
    try:
        wards = compute_predictive_risk(scenario, current_cusecs)
        advisory = build_advisory_payload(scenario, wards, current_cusecs)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Scenario preview failed ({type(exc).__name__}).")
    trigger_codes = [
        code for code in (advisory.get("trigger_reason_codes") or [])
        if code in {"LOCAL_RAINFALL_WATCH", "UPSTREAM_CATCHMENT_RAIN_WATCH"}
    ]
    would_match = bool(trigger_codes)
    message = _build_telegram_scheduler_message(advisory, scenario) if would_match else None
    if message:
        message = "SIMULATION ONLY — NO DELIVERY\n\n" + message
    return {
        "provider": "Telegram Bot API",
        "mode": "simulation_preview_only",
        "inputs": values,
        "advisory_status": advisory.get("status"),
        "trigger_reason_codes": trigger_codes,
        "would_match_schedule_trigger": would_match,
        "eligible_for_delivery": False,
        "blocking_reason": "Simulated inputs are never eligible for scheduler delivery.",
        "message_preview": message,
        "scheduled_delivery_enabled": bool(_telegram_scheduled_config()["enabled"]),
        "outbound_request_made": False,
        "sent": False,
        "production_broadcast_enabled": False,
        "note": "This endpoint tests trigger logic and message formatting only. It always remains simulation-only even if scheduled delivery is enabled.",
    }


@app.get("/telegram/scheduler/preview")
def telegram_scheduler_preview():
    """Fetch fresh live forecast and show what scheduled Telegram would do; never sends."""
    forecast = fetch_integrated_rainfall_forecasts()
    evaluation = _evaluate_telegram_scheduler_forecast(forecast)
    cfg = _telegram_scheduled_config()
    dedupe_active = None
    redis_check_error = None
    if evaluation.get("eligible") and cfg["chat_id"]:
        try:
            alerts_redis.ping()
            dedupe_active = bool(alerts_redis.exists(_telegram_scheduled_dedupe_key(cfg["chat_id"])))
        except redis.RedisError as exc:
            redis_check_error = type(exc).__name__
    return {
        "provider": "Telegram Bot API",
        "mode": "scheduler_preview_only",
        **evaluation,
        "scheduled_delivery_enabled": bool(cfg["enabled"]),
        "recipient_chat_id": _mask_chat_id(cfg["chat_id"]),
        "dedupe_cooldown_seconds": TELEGRAM_SCHEDULED_DEDUPE_SECONDS,
        "dedupe_cooldown_active": dedupe_active,
        "redis_check_error_type": redis_check_error,
        "outbound_request_made": False,
        "sent": False,
        "automatic_forecast_sending": bool(cfg["enabled"]),
        "production_broadcast_enabled": False,
        "note": "Fresh live forecast only. This endpoint never sends and rejects simulated, incomplete, or stale forecasts.",
    }


@app.post("/telegram/scheduler/dedup-test")
def telegram_scheduler_dedup_test():
    """Check Redis SET NX duplicate-suppression behavior using a disposable test key; sends nothing."""
    test_key = f"floodews:telegram:scheduler:dedup_self_test:{secrets.token_hex(12)}"
    try:
        alerts_redis.ping()
        first = alerts_redis.set(test_key, "first", nx=True, ex=60)
        second = alerts_redis.set(test_key, "second", nx=True, ex=60)
        passed = bool(first) and not bool(second)
        return {
            "mode": "redis_dedup_test_only",
            "first_reservation_succeeded": bool(first),
            "second_reservation_succeeded": bool(second),
            "duplicate_suppression_passed": passed,
            "outbound_request_made": False,
            "sent": False,
            "note": "Uses a disposable Redis test key that is removed afterward. Does not contact Telegram or touch the production alert cooldown.",
        }
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail={
            "message": "Redis duplicate-suppression test failed; scheduled delivery must remain disabled.",
            "error_type": type(exc).__name__,
            "outbound_request_made": False,
        })
    finally:
        try:
            alerts_redis.delete(test_key)
        except redis.RedisError:
            logging.warning("Could not remove disposable Telegram dedup test key.")


@app.get("/telegram/scheduler/status")
def telegram_scheduler_status():
    cfg = _telegram_scheduled_config()
    valid_chat_id = bool(__import__("re").fullmatch(r"[0-9]+", cfg["chat_id"])) and int(cfg["chat_id"] or "0") > 0
    return {
        "provider": "Telegram Bot API",
        "scheduled_delivery_enabled": bool(cfg["enabled"]),
        "bot_token_configured": bool(cfg["token"]),
        "private_chat_id_configured": valid_chat_id,
        "private_chat_id_masked": _mask_chat_id(cfg["chat_id"]),
        "interval_minutes": ALERT_CHECK_INTERVAL_SECONDS // 60,
        "dedupe_cooldown_seconds": TELEGRAM_SCHEDULED_DEDUPE_SECONDS,
        "last_checked_at_local": ALERT_SCHEDULER_STATUS.get(
            "last_checked_at_local"
            ),
        "last_result": ALERT_SCHEDULER_STATUS.get("last_result"),
        "last_error": ALERT_SCHEDULER_STATUS.get("last_error"),
        "last_telegram_error": ALERT_SCHEDULER_STATUS.get(
            "last_telegram_error"
            ),
        "last_scheduler_status": ALERT_SCHEDULER_STATUS.get("last_telegram_status"),
        "last_scheduler_sent": ALERT_SCHEDULER_STATUS.get("last_telegram_sent", False),
        "last_scheduler_trigger_reason_codes": ALERT_SCHEDULER_STATUS.get("last_telegram_trigger_reason_codes", []),
        "automatic_forecast_sending": bool(cfg["enabled"]),
        "production_broadcast_enabled": False,
        "note": "Scheduled delivery is separately opt-in via TELEGRAM_ENABLE_SCHEDULED_ALERTS=true. It can only use the configured private chat; fresh live rainfall-watch triggers only; simulations and stale data are blocked.",
    }


@app.get("/advisory/history")
def advisory_history(limit: int = 20):
    """Retrieve recent stored forecast/advisory snapshots."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100.")
    try:
        ensure_advisory_history_tables()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT run_id, created_at, triggered_by, model_version,
                       discharge_scenario_cusecs, local_mean_6h_mm, local_max_6h_mm,
                       local_mean_24h_mm, local_max_24h_mm,
                       upstream_mean_6h_mm, upstream_max_6h_mm,
                       upstream_mean_24h_mm, upstream_max_24h_mm,
                       model_valid_time_local, forecast_start_local, forecast_end_local,
                       fetched_at_local, advisory_status, advisory_payload
                FROM flood_ews_advisory_runs
                ORDER BY created_at DESC LIMIT :limit
            """), {"limit": limit}).mappings().all()
        result = []
        for row in rows:
            item = dict(row)
            item["created_at"] = item["created_at"].isoformat() if item.get("created_at") else None
            if isinstance(item.get("advisory_payload"), str):
                try:
                    item["advisory_payload"] = json.loads(item["advisory_payload"])
                except json.JSONDecodeError:
                    pass
            result.append(item)
        return {"count": len(result), "runs": result}
    except Exception as exc:
        logging.exception("Could not load advisory history")
        raise HTTPException(status_code=503, detail=f"Advisory history unavailable: {exc}")


@app.post("/advisory/feedback")
def advisory_feedback(payload: dict):
    """Record a manually verified real-world outcome for later model evaluation."""
    try:
        feedback_id = save_advisory_feedback(payload)
        return {
            "status": "feedback_recorded",
            "feedback_id": feedback_id,
            "note": "Feedback is a record for evaluation; it does not automatically retrain or alter the scoring model.",
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logging.exception("Could not save advisory feedback")
        raise HTTPException(status_code=503, detail=f"Feedback could not be saved: {exc}")


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


@app.get("/weather/recent-rainfall")
def recent_model_rainfall():
    return fetch_model_rainfall_24h()


@app.get("/weather/upstream-rainfall")
def upstream_model_rainfall():
    """Inspect rainfall forecasts at the four reservoir-adjacent reference points."""
    return fetch_upstream_catchment_forecasts()


@app.get("/map", response_class=HTMLResponse)
def risk_map(
    current_cusecs: float = DEFAULT_CUSECS,
    rain_6h_mm: float = 0.0,
    rain_24h_mm: float = 0.0,
    upstream_6h_mm: float = 0.0,
    upstream_24h_mm: float = 0.0,
    prediction_mode: str = "scenario",
):
    """Map the V2.3 predictive index for either a manual scenario or live forecast."""
    import math
    from html import escape

    if not math.isfinite(current_cusecs) or current_cusecs < 0:
        raise HTTPException(status_code=422, detail="current_cusecs must be finite and non-negative.")
    if not math.isfinite(rain_6h_mm) or rain_6h_mm < 0:
        raise HTTPException(status_code=422, detail="rain_6h_mm must be finite and non-negative.")
    if not math.isfinite(rain_24h_mm) or rain_24h_mm < 0:
        raise HTTPException(status_code=422, detail="rain_24h_mm must be finite and non-negative.")
    if not math.isfinite(upstream_6h_mm) or upstream_6h_mm < 0:
        raise HTTPException(status_code=422, detail="upstream_6h_mm must be finite and non-negative.")
    if not math.isfinite(upstream_24h_mm) or upstream_24h_mm < 0:
        raise HTTPException(status_code=422, detail="upstream_24h_mm must be finite and non-negative.")
    if prediction_mode not in {"scenario", "forecast"}:
        raise HTTPException(status_code=422, detail="prediction_mode must be 'scenario' or 'forecast'.")

    if prediction_mode == "forecast":
        weather = fetch_integrated_rainfall_forecasts()
        if weather.get("error") or weather.get("ward_forecasts") is None:
            error_text = escape(str(weather.get("error") or "Ward forecasts unavailable."))
            return HTMLResponse(
                content=f"<html><body style='font-family:Segoe UI,Arial;padding:24px;color:#991b1b'><h2>Predictive map unavailable</h2><p>{error_text}</p><p>No risk map was generated from missing rainfall data.</p></body></html>",
                status_code=503,
            )
        scores = compute_predictive_risk(weather, current_cusecs)
        legend_text = (
            f"V2.3 advisory index · local ward forecast + upstream reference points · "
            f"discharge scenario {current_cusecs:,.0f} cusecs"
        )
    else:
        scenario = {
            "source": "Manual simulation",
            "data_type": "simulated_scenario_not_live",
            "forecast_6h_mm": rain_6h_mm,
            "forecast_24h_mm": rain_24h_mm,
            "upstream_forecast_available": True,
            "upstream_forecast_6h_mm": upstream_6h_mm,
            "upstream_forecast_24h_mm": upstream_24h_mm,
            "upstream_max_forecast_6h_mm": upstream_6h_mm,
            "upstream_max_forecast_24h_mm": upstream_24h_mm,
            "upstream_forecasts": [],
            "upstream_error": None,
            "error": None,
        }
        scores = compute_predictive_risk(scenario, current_cusecs)
        legend_text = (
            f"V2.3 scenario · local {rain_6h_mm:g} mm/6h, {rain_24h_mm:g} mm/24h; "
            f"upstream {upstream_6h_mm:g} mm/6h, {upstream_24h_mm:g} mm/24h · {current_cusecs:,.0f} cusecs scenario"
        )

    geom = gpd.read_postgis(
        "SELECT wardnum, geometry FROM wards",
        engine,
        geom_col="geometry",
    )
    score_columns = [
        "wardnum", "name", "predictive_risk_score", "predictive_risk_level",
        "urban_waterlogging_index", "river_corridor_index", "static_vulnerability",
        "corridor_flagged", "forecast_6h_mm", "forecast_24h_mm",
        "upstream_rainfall_hazard_score",
    ]
    wards_map = geom.merge(
        scores[score_columns],
        on="wardnum",
        how="left",
        validate="one_to_one",
    )
    if wards_map["predictive_risk_score"].isna().any():
        raise HTTPException(status_code=503, detail="Some wards could not be scored; map generation stopped.")

    m = folium.Map(
        location=[18.52, 73.85],
        zoom_start=11,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    folium.Choropleth(
        geo_data=wards_map,
        data=wards_map,
        columns=["wardnum", "predictive_risk_score"],
        key_on="feature.properties.wardnum",
        fill_color="YlOrRd",
        fill_opacity=0.68,
        line_opacity=0.4,
        line_weight=1,
        legend_name=legend_text,
        bins=[0, 0.2, 0.4, 0.6, 0.8, 1.0],
        name="V2.3 forecast-driven risk index",
        show=True,
    ).add_to(m)

    folium.GeoJson(
        wards_map,
        name="Ward details",
        style_function=lambda feature: {
            "fillOpacity": 0,
            "color": "#475569",
            "weight": 0.6,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=[
                "name", "predictive_risk_score", "predictive_risk_level",
                "urban_waterlogging_index", "river_corridor_index",
                "forecast_6h_mm", "forecast_24h_mm", "upstream_rainfall_hazard_score", "corridor_flagged",
            ],
            aliases=[
                "Ward:", "Predictive index:", "Risk band:",
                "Urban waterlogging index:", "River-corridor index:",
                "Local forecast 6h (mm):", "Local forecast 24h (mm):", "Upstream rainfall hazard index:", "Historical corridor association:",
            ],
            localize=True,
        ),
    ).add_to(m)

    project_root = Path(__file__).resolve().parents[1]
    candidates = [
        project_root / "data" / "raw" / "puneriver_v2.geojson",
        project_root / "data" / "processed" / "puneriver_v2.geojson",
        project_root / "puneriver_v2.geojson",
    ]
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__"}
    river_path = next((path for path in candidates if path.is_file()), None)
    if river_path is None:
        river_path = next(
            (
                path for path in project_root.rglob("puneriver_v2.geojson")
                if not any(part.lower() in excluded for part in path.parts)
            ),
            None,
        )

    if river_path is not None:
        try:
            rivers = gpd.read_file(river_path)
            if not rivers.empty:
                rivers = rivers.set_crs(epsg=4326) if rivers.crs is None else rivers.to_crs(epsg=4326)
                min_lon, min_lat, max_lon, max_lat = rivers.total_bounds
                is_pune_area = (
                    73.3 <= min_lon <= 74.3 and 73.3 <= max_lon <= 74.3
                    and 18.2 <= min_lat <= 19.0 and 18.2 <= max_lat <= 19.0
                )
                if is_pune_area:
                    folium.GeoJson(
                        rivers,
                        name="Mula–Mutha river network",
                        style_function=lambda feature: {
                            "color": "#0878F9", "weight": 4, "opacity": 0.95, "fillOpacity": 0,
                        },
                        highlight_function=lambda feature: {
                            "color": "#004DB3", "weight": 6, "opacity": 1.0,
                        },
                        show=True,
                    ).add_to(m)
                else:
                    logging.warning("River overlay skipped due to unexpected bounds: %s", rivers.total_bounds)
        except Exception:
            logging.exception("Could not add river overlay to predictive map")
    else:
        logging.warning("River overlay not added: puneriver_v2.geojson not found.")

    folium.LayerControl(collapsed=False, position="topright").add_to(m)
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
      <p>Explore the V2.3 local-rain and upstream-catchment scenario model. For live forecasts and SMS advisory previews, open Today's Automatic Outlook.</p>
      <a href="/citizen" style="display:inline-block;margin:0 14px 16px 0;padding:10px 15px;border-radius:999px;background:#0b6b70;color:white;text-decoration:none;font-weight:800;">☔ Open citizen rain dashboard</a>
      <a class="outlook-link" href="/outlook">&rarr; View Today's Automatic Outlook</a>
      <div class="controls">
        <div class="row">
          <label for="cusecs">Discharge scenario (cusecs; not live)</label>
          <input type="range" id="cusecs" min="0" max="50000" step="1000" value="15000">
          <span id="cusecsVal">15000</span>
        </div>
        <div class="row">
          <label for="rain6">Local rainfall scenario, next 6h (mm)</label>
          <input type="range" id="rain6" min="0" max="100" step="0.5" value="0">
          <span id="rain6Val">0</span>
        </div>
        <div class="row">
          <label for="rain">Local rainfall scenario, next 24h (mm)</label>
          <input type="range" id="rain" min="0" max="250" step="0.5" value="0">
          <span id="rainVal">0</span>
        </div>
        <div class="row">
          <label for="upstream6">Upstream rain scenario, next 6h (mm)</label>
          <input type="range" id="upstream6" min="0" max="100" step="0.5" value="0">
          <span id="upstream6Val">0</span>
        </div>
        <div class="row">
          <label for="upstream24">Upstream rain scenario, next 24h (mm)</label>
          <input type="range" id="upstream24" min="0" max="250" step="0.5" value="0">
          <span id="upstream24Val">0</span>
        </div>
        <p style="font-size:12px;color:#64748b;margin-top:-4px">These sliders are scenarios for testing. Live ward and upstream forecasts are shown in Today's Automatic Outlook.</p>
        <div class="stats">
          <div class="stat">Scenario risk (max)<span id="cityMax">-</span></div>
          <div class="stat">Scenario risk (avg)<span id="cityAvg">-</span></div>
          <div class="stat">Critical wards (scenario)<span id="critical">-</span></div>
          <div class="stat">Discharge-linked wards flagged<span id="flagged">-</span></div>
          <div class="stat static">Baseline vulnerability (always present)<span id="staticVuln">-</span></div>
        </div>
      </div>
      
<div id="alertsPanel" style="
    margin:18px 0;
    padding:16px 18px;
    background:#fff;
    border:1px solid #e2e8f0;
    border-radius:10px;
">
    <div style="
        display:flex;
        align-items:center;
        justify-content:space-between;
        flex-wrap:wrap;
        gap:10px;
    ">
        <div>
            <strong style="font-size:16px;">Ward Alerts</strong>
            <div style="font-size:12px;color:#64748b;margin-top:4px;">
                Rainfall-watch events stored in Redis
            </div>
        </div>
        <span id="alertCount" style="
            padding:5px 9px;
            background:#f1f5f9;
            border-radius:5px;
            font-size:12px;
        ">Loading alerts…</span>
    </div>

    <p id="alertMessage" style="
        font-size:12px;
        color:#64748b;
        margin:12px 0;
    ">Connecting to alert service…</p>

    <div style="
        display:flex;
        flex-wrap:wrap;
        gap:8px;
        margin-bottom:12px;
    ">
        <button id="checkAlertsButton" type="button">
            Check forecast now
        </button>
        <button id="testAlertButton" type="button">
            Create test alert
        </button>
    </div>

    <div id="alertList"></div>
</div>


<div id="schedulerStatusCard"
     style="margin:10px 0;padding:10px 14px;
            border:1px solid #dbe3ee;border-radius:10px;
            background:#f8fafc;color:#334155;
            display:flex;align-items:center;gap:16px;
            flex-wrap:wrap;font-size:13px;">

  <div style="display:flex;align-items:center;gap:9px;">
    <strong>Automatic checks</strong>
    <span id="schedulerBadge"
          style="padding:4px 9px;border-radius:20px;
                 background:#334155;color:white;font-size:11px;">
      Checking...
    </span>
  </div>

  <span id="schedulerLastCheck">
    Last check: Waiting for first check
  </span>

  <span id="schedulerRainfall">
    Local 24-hour forecast: Loading...
  </span>
  <span id="schedulerUpstream">Upstream rainfall: Loading...</span>

  <span id="schedulerMessage"
        style="color:#64748b;">
    Checking scheduler health...
  </span>
</div>

      <iframe id="mapFrame" src="/map?prediction_mode=scenario&current_cusecs=15000&rain_6h_mm=0&rain_24h_mm=0&upstream_6h_mm=0&upstream_24h_mm=0"></iframe>

      <script>
        const cusecs = document.getElementById('cusecs');
        const rain6 = document.getElementById('rain6');
        const rain = document.getElementById('rain');
        const upstream6 = document.getElementById('upstream6');
        const upstream24 = document.getElementById('upstream24');

        async function update() {
          const c = cusecs.value, l6 = rain6.value, r = rain.value, u6 = upstream6.value, u24 = upstream24.value;
          document.getElementById('cusecsVal').innerText = c;
          document.getElementById('rain6Val').innerText = l6;
          document.getElementById('rainVal').innerText = r;
          document.getElementById('upstream6Val').innerText = u6;
          document.getElementById('upstream24Val').innerText = u24;
          document.getElementById('mapFrame').src = `/map?prediction_mode=scenario&current_cusecs=${encodeURIComponent(c)}&rain_6h_mm=${encodeURIComponent(l6)}&rain_24h_mm=${encodeURIComponent(r)}&upstream_6h_mm=${encodeURIComponent(u6)}&upstream_24h_mm=${encodeURIComponent(u24)}`;

          try {
            const res = await fetch(`/prediction/scenario?rain_6h_mm=${encodeURIComponent(l6)}&rain_24h_mm=${encodeURIComponent(r)}&upstream_6h_mm=${encodeURIComponent(u6)}&upstream_24h_mm=${encodeURIComponent(u24)}&current_cusecs=${encodeURIComponent(c)}`, { cache: 'no-store' });
            if (!res.ok) throw new Error(`Scenario API returned HTTP ${res.status}`);
            const data = await res.json();
            const summary = data.city_summary;
            document.getElementById('cityMax').innerText = summary.maximum_predictive_risk_index;
            document.getElementById('cityAvg').innerText = summary.average_predictive_risk_index;
            document.getElementById('critical').innerText = summary.critical_wards;
            document.getElementById('flagged').innerText = summary.corridor_flagged_wards;
            document.getElementById('staticVuln').innerText = summary.city_wide_static_vulnerability;
          } catch (error) {
            console.error('Predictive scenario update failed:', error);
            document.getElementById('cityMax').innerText = 'Unavailable';
            document.getElementById('cityAvg').innerText = 'Unavailable';
            document.getElementById('critical').innerText = '—';
            document.getElementById('flagged').innerText = '—';
            document.getElementById('staticVuln').innerText = '—';
          }
        }

        cusecs.oninput = update;
        upstream6.oninput = () => { automaticRainEnabled = false; manualButton.hidden = false; update(); setRainStatus("manual", "MANUAL SCENARIO", "Using simulated local/upstream rainfall values; these are not live data."); };
        upstream24.oninput = () => { automaticRainEnabled = false; manualButton.hidden = false; update(); setRainStatus("manual", "MANUAL SCENARIO", "Using simulated local/upstream rainfall values; these are not live data."); };

let automaticRainEnabled = true;

const rainRow = rain.closest(".row");

const rainStatus = document.createElement("div");
rainStatus.style.cssText = `
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 9px;
    margin-top: 7px;
    padding: 8px 11px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 7px;
    color: #475569;
    font-size: 12px;
    line-height: 1.5;
`;

const statusDot = document.createElement("span");
statusDot.style.cssText = `
    width: 7px;
    height: 7px;
    flex: 0 0 7px;
    border-radius: 50%;
    background: #16a34a;
`;

const statusLabel = document.createElement("span");
statusLabel.style.cssText = `
    padding: 3px 6px;
    border-radius: 4px;
    background: #dcfce7;
    color: #166534;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.4px;
`;

const statusDetail = document.createElement("span");

rainStatus.append(statusDot, statusLabel, statusDetail);
rainRow.appendChild(rainStatus);

const manualButton = document.createElement("button");
manualButton.textContent = "Resume automatic forecasts";
manualButton.hidden = true;
manualButton.style.cssText = `
    margin-top: 7px;
    padding: 6px 10px;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    background: white;
    cursor: pointer;
`;
rainRow.appendChild(manualButton);

function formatStatusTime(value) {
    if (!value) return "Unavailable";

    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Unavailable";

    return new Intl.DateTimeFormat("en-IN", {
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
        timeZone: "Asia/Kolkata"
    }).format(date);
}

function setRainStatus(mode, label, detail) {
    const styles = {
        auto: {
            dot: "#16a34a",
            background: "#dcfce7",
            color: "#166534"
        },
        manual: {
            dot: "#d97706",
            background: "#fef3c7",
            color: "#92400e"
        },
        error: {
            dot: "#dc2626",
            background: "#fee2e2",
            color: "#991b1b"
        }
    };

    const style = styles[mode];

    statusDot.style.background = style.dot;
    statusLabel.style.background = style.background;
    statusLabel.style.color = style.color;

    statusLabel.textContent = label;
    statusDetail.textContent = detail;
}

async function refreshAutomaticRainfall() {
    try {
        const response = await fetch(
            "/prediction/forecast",
            { cache: "no-store" }
        );

        const data = await response.json();
        const forecastInputs = data.inputs || {};
        const localForecast6 = forecastInputs.average_forecast_6h_mm;
        const rainfall = forecastInputs.average_forecast_24h_mm;
        const upstreamForecast6 = forecastInputs.upstream_forecast_6h_mm;
        const upstreamForecast24 = forecastInputs.upstream_forecast_24h_mm;

        if (!response.ok || !Number.isFinite(localForecast6) || !Number.isFinite(rainfall)) {
            setRainStatus(
                "error",
                "LOCAL FEED ISSUE",
                "Ward rainfall forecast unavailable. Previous scenario values retained and may be stale."
            );
            return;
        }
        const upstreamAvailable = Number.isFinite(upstreamForecast6) && Number.isFinite(upstreamForecast24);

        const checkedTime = formatStatusTime(forecastInputs.fetched_at_local);
        const modelTime = formatStatusTime(forecastInputs.model_valid_time_local);

        // Continue checking the feed even while manual mode is active,
        // but do not overwrite the user's manual scenario.
        if (!automaticRainEnabled) {
            setRainStatus(
                "manual",
                "MANUAL TEST",
                `Scenario input active · Latest local forecast mean ${rainfall.toFixed(1)} mm/24h · Model ${modelTime}`
            );
            return;
        }

        const minValue = Number(rain.min || 0);
        const maxValue = Number(rain.max || 250);
        const max6Value = Number(rain6.max || 100);
        rain6.value = String(Math.min(max6Value, Math.max(0, localForecast6)));
        rain.value = String(Math.min(maxValue, Math.max(minValue, rainfall)));
        if (upstreamAvailable) {
            upstream6.value = String(Math.min(Number(upstream6.max), Math.max(0, upstreamForecast6)));
            upstream24.value = String(Math.min(Number(upstream24.max), Math.max(0, upstreamForecast24)));
        }
        await update();

        if (upstreamAvailable) {
            setRainStatus(
                "auto",
                "AUTO FORECAST",
                `Next 6h/24h local mean: ${localForecast6.toFixed(1)} / ${rainfall.toFixed(1)} mm · Upstream mean: ${upstreamForecast6.toFixed(1)} mm/6h, ${upstreamForecast24.toFixed(1)} mm/24h · Checked ${checkedTime} · Model ${modelTime} · Not gauge readings`
            );
        } else {
            setRainStatus(
                "error",
                "PARTIAL FORECAST",
                `Local forecast refreshed (${localForecast6.toFixed(1)} mm/6h, ${rainfall.toFixed(1)} mm/24h means), but upstream forecast is unavailable. Upstream scenario sliders were not overwritten. Checked ${checkedTime}.`
            );
        }

    } catch (error) {
        setRainStatus(
            "error",
            "FEED ISSUE",
            "Could not refresh rainfall. Previous value retained; it may be stale."
        );
        console.error("Rainfall refresh failed:", error);
    }
}

// Moving the slider switches to manual scenario mode.
function switchToManualScenario() {
    automaticRainEnabled = false;
    manualButton.hidden = false;
    update();
    setRainStatus(
        "manual",
        "MANUAL SCENARIO",
        "Using simulated local and/or upstream rainfall values. These are not live data."
    );
}
rain.oninput = switchToManualScenario;
rain6.oninput = switchToManualScenario;

manualButton.onclick = async () => {
    automaticRainEnabled = true;
    manualButton.hidden = true;
    await refreshAutomaticRainfall();
};

// Initial dashboard load and periodic rainfall refresh.
update();
refreshAutomaticRainfall();
setInterval(refreshAutomaticRainfall, 15 * 60 * 1000);


function escapeAlertText(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[char]);
}


async function refreshAlerts() {
    const message = document.getElementById("alertMessage");
    const list = document.getElementById("alertList");
    const count = document.getElementById("alertCount");

    try {
        const response = await fetch("/alerts", {
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error("Alert service unavailable");
        }

        const data = await response.json();
        const alerts = data.alerts || [];

        // Show total stored events, but render only the newest one.
        count.textContent = `${data.count} stored`;
        list.replaceChildren();

        if (alerts.length === 0) {
            message.textContent =
                "No alerts recorded. No trigger may have been met.";
            return;
        }

        message.textContent =
            "Latest alert · Older events stored";

        // Redis stores newest alerts first. Show only the first.
        const alert = alerts[0];

        const simulated =
            alert.type === "SIMULATED_TEST_ALERT" ||
            alert.is_simulated === true;

        const card = document.createElement("div");

        card.style.cssText = `
            margin-top:8px;
            padding:10px 12px;
            border:1px solid ${simulated ? "#f59e0b" : "#e2e8f0"};
            border-left:4px solid ${simulated ? "#d97706" : "#0f766e"};
            border-radius:8px;
            background:${simulated ? "#fffbeb" : "#f8fafc"};
        `;

        const wardText = (alert.priority_wards || [])
            .map(w =>
                `${escapeAlertText(w.name)} (Ward ${escapeAlertText(w.wardnum)})`
            )
            .join(", ");

        card.innerHTML = `
            <div style="font-size:10px;font-weight:800;letter-spacing:.4px;">
                ${simulated
                    ? "SIMULATED TEST — NOT A REAL WARNING"
                    : escapeAlertText(alert.severity || "WATCH")}
            </div>

            <div style="font-size:13px;font-weight:700;margin-top:4px;">
                ${escapeAlertText(alert.type || "Ward alert")}
            </div>

            <div style="font-size:12px;color:#475569;margin-top:4px;line-height:1.5;">
                ${escapeAlertText(alert.message || "No details provided.")}
            </div>

            <div style="font-size:12px;margin-top:6px;line-height:1.5;">
                <strong>Wards:</strong>
                ${wardText || "No priority wards listed"}
            </div>

            <div style="font-size:10px;color:#64748b;margin-top:5px;">
                ${escapeAlertText(alert.created_at_local || "")}
            </div>
        `;

        list.appendChild(card);

    } catch (error) {
        count.textContent = "Unavailable";
        message.textContent =
            "Could not load alerts. Check the API and Redis service.";
    }
}


async function evaluateForecastNow() {
    const message = document.getElementById("alertMessage");
    const button = document.getElementById("checkAlertsButton");

    button.disabled = true;
    message.textContent = "Checking the latest rainfall forecast…";

    try {
        const response = await fetch("/alerts/evaluate", {
            method: "POST"
        });
        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.detail || "Evaluation failed");
        }

        message.textContent =
            result.status === "no_trigger"
                ? `No alert triggered. Forecast: ${result.forecast_24h_mm} mm; watch threshold: ${result.threshold_mm} mm.`
                : `Evaluation result: ${result.status}`;

        await refreshAlerts();
    } catch (error) {
        message.textContent = `Could not evaluate forecast: ${error.message}`;
    } finally {
        button.disabled = false;
    }
}

async function createTestAlert() {
    const message = document.getElementById("alertMessage");
    const button = document.getElementById("testAlertButton");

    if (!confirm(
        "Create a clearly labelled SIMULATED test alert in Redis? " +
        "This is not a real flood warning."
    )) return;

    button.disabled = true;
    message.textContent = "Creating simulated test alert…";

    try {
        const response = await fetch("/alerts/test", {
            method: "POST"
        });
        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.detail || "Test alert failed");
        }

        message.textContent =
            "Simulated test alert created. It is not a real warning.";

        await refreshAlerts();
    } catch (error) {
        message.textContent = `Could not create test alert: ${error.message}`;
    } finally {
        button.disabled = false;
    }
}

document.getElementById("checkAlertsButton")
    .addEventListener("click", evaluateForecastNow);

document.getElementById("testAlertButton")
    .addEventListener("click", createTestAlert);

refreshAlerts();
setInterval(refreshAlerts, 30000);

async function refreshSchedulerStatus() {
    const badge = document.getElementById("schedulerBadge");
    const lastCheck = document.getElementById("schedulerLastCheck");
    const rainfall = document.getElementById("schedulerRainfall");
    const upstream = document.getElementById("schedulerUpstream");
    const message = document.getElementById("schedulerMessage");

    try {
        const response = await fetch("/alerts/status", {
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error("Status endpoint returned HTTP " + response.status);
        }

        const data = await response.json();

        lastCheck.textContent =
            "Last check: " +
            (data.last_checked_at_local || "Not checked yet");

        rainfall.textContent =
            "Local 24-hour forecast: " +
            (data.last_forecast_24h_mm == null
                ? "Unavailable"
                : data.last_forecast_24h_mm + " mm");
        upstream.textContent =
            "Upstream max 24-hour forecast: " +
            (data.last_upstream_max_24h_mm == null
                ? "Unavailable"
                : data.last_upstream_max_24h_mm + " mm");

        if (!data.enabled) {
            badge.textContent = "DISABLED";
            badge.style.background = "#475569";
            message.textContent = "Automatic forecast checks are disabled.";

        } else if (
            data.last_error ||
            data.last_result === "error" ||
            data.last_result === "unavailable"
        ) {
            badge.textContent = "FEED ISSUE";
            badge.style.background = "#92400e";
            const detail = data.last_error || data.last_telegram_error;

            message.textContent =
            "Forecast unavailable. The system has not confirmed a new alert." +
            (detail
            ? " Last error: " + String(detail).slice(0, 180)
            : "");
           

        } else if (!data.last_checked_at_local ||
                   data.last_result === "not_started") {
            badge.textContent = "STARTING";
            badge.style.background = "#475569";
            message.textContent = "Waiting for the first scheduled check.";

        } else {
            badge.textContent = "RUNNING";
            badge.style.background = "#166534";
            message.textContent =
                "Checks every " + data.interval_minutes +
                " minutes. Latest result: " + data.last_result + ".";
        }

    } catch (error) {
        badge.textContent = "STATUS UNKNOWN";
        badge.style.background = "#92400e";
        message.textContent =
            "Could not read scheduler status: " + error.message;
    }
}

refreshSchedulerStatus();
setInterval(refreshSchedulerStatus, 30000);


      </script>
    </body>
    </html>
    """


# PUNE CITIZEN DASHBOARD V2 — SIMPLIFIED
@app.get("/citizen", response_class=HTMLResponse)
def citizen_dashboard():
    """Simple citizen view; read-only and never sends alerts."""
    return r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b6670">
<title>Pune Rain & Safety</title>
<style>
:root{--ink:#17334a;--muted:#617585;--teal:#086a70;--line:#d9e6ec;--bg:#f3f8fa;--good:#13805e;--amber:#925b09;--amberbg:#fff4df}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 95% 0,#dff5f0 0,transparent 30rem),var(--bg);color:var(--ink);font:16px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:auto;padding:22px 16px 38px}.nav{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:20px}.brand{font-weight:850;letter-spacing:.02em}.nav a,.link{color:var(--teal);font-weight:800;text-decoration:none}.nav a:hover,.link:hover{text-decoration:underline}
.hero{background:linear-gradient(125deg,#0b5f68,#147f82);color:#fff;border-radius:24px;padding:clamp(24px,5vw,40px);box-shadow:0 14px 34px #124b5922}.kicker{font-size:.76rem;font-weight:850;letter-spacing:.12em;text-transform:uppercase;color:#b9f1e4}h1{font-size:clamp(2rem,5.5vw,3.25rem);letter-spacing:-.04em;line-height:1.05;margin:10px 0 12px}.hero p{max-width:620px;margin:0;color:#e4f5f3}.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:20px}.button{display:inline-block;border:1px solid #ffffff80;border-radius:999px;padding:10px 16px;font:inherit;font-weight:800;text-decoration:none;cursor:pointer}.primary{background:white;color:#0b5f68;border:0}.secondary{background:#ffffff18;color:white}.meta{margin-top:14px!important;font-size:.84rem;color:#e0f2f1!important}
.card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:0 4px 16px #18334a08;margin-top:16px}.card h2{font-size:1.12rem;margin:0 0 8px}.muted{color:var(--muted);margin:4px 0}.status{display:flex;gap:10px;align-items:flex-start}.dot{width:10px;height:10px;border-radius:100%;background:#96a5b0;margin-top:8px;flex:0 0 auto}.dot.good{background:var(--good)}.dot.watch{background:#d58b20}.dot.bad{background:#b93942}.statusTitle{font-size:1.13rem;font-weight:850;margin:0}.statusText{margin:4px 0 0;color:var(--muted)}
.rainGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.rainBox{padding:18px;border:1px solid var(--line);border-radius:16px;background:#fbfdfe}.rainLabel{color:var(--muted);font-size:.82rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em}.rainValue{font-size:clamp(2rem,5vw,2.7rem);font-weight:900;letter-spacing:-.04em;line-height:1.15;margin-top:6px}.rainBox p{margin:5px 0 0;color:var(--muted);font-size:.88rem}.select{width:100%;padding:12px;border:1px solid #cbdbe3;border-radius:12px;background:white;color:var(--ink);font:inherit;margin-top:7px}.areaStats{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}.areaStat{background:#f5f9fa;border:1px solid #e5eef1;border-radius:12px;padding:10px 13px;flex:1;min-width:130px}.areaStat strong{display:block;font-size:1.18rem}.areaStat span{font-size:.84rem;color:var(--muted)}.small{font-size:.86rem}.notice{margin-top:14px;background:var(--amberbg);border:1px solid #efd9a9;border-radius:13px;padding:12px 14px;color:#754707}.list{margin:8px 0 0;padding-left:21px}.list li{margin:7px 0;color:#435b6a}.officialDetail{color:var(--muted);margin:8px 0 0;white-space:pre-line}.footer{color:var(--muted);font-size:.82rem;border-top:1px solid var(--line);padding-top:15px;margin-top:20px}
@media(max-width:640px){.wrap{padding:14px 11px 28px}.hero{border-radius:20px}.card{padding:16px}.rainGrid{gap:8px}.rainBox{padding:14px}.nav{margin-bottom:14px}}
</style>
</head>
<body>
<div class="wrap">
  <nav class="nav"><div class="brand">🌦️ PUNE · RAIN & SAFETY</div><div><a href="/" style="margin-right:14px">Technical console</a><a href="https://mausam.imd.gov.in/" target="_blank" rel="noopener noreferrer">Official IMD ↗</a></div></nav>
  <header class="hero">
    <div class="kicker">Local rain outlook</div>
    <h1>Stay rain-ready, Pune.</h1>
    <p>A simple rainfall forecast and practical safety advice for Pune. Work in progress.</p>
    <div class="actions"><button id="refreshBtn" class="button primary" type="button">↻ Refresh</button><a class="button secondary" href="https://mausam.imd.gov.in/" target="_blank" rel="noopener noreferrer">Official IMD updates ↗</a></div>
    <p id="meta" class="meta">Loading forecast…</p>
  </header>

  <main>
    <section class="card" aria-live="polite">
      <div class="status"><span id="outlookDot" class="dot"></span><div><h2 id="outlookTitle" class="statusTitle">Checking outlook…</h2><p id="outlookText" class="statusText">Fetching the latest available model forecast.</p></div></div>
    </section>

    <section class="card">
      <h2>Rain forecast for Pune</h2>
      <div class="rainGrid">
        <div class="rainBox"><div class="rainLabel">Next 6 hours</div><div id="city6" class="rainValue">—</div><p>Forecast rainfall</p></div>
        <div class="rainBox"><div class="rainLabel">Next 24 hours</div><div id="city24" class="rainValue">—</div><p>Forecast rainfall</p></div>
      </div>
      <p class="muted small">Source: Open-Meteo · Average of forecasts at Pune ward centres.</p>
    </section>

    <section class="card">
      <h2>Check your area for forecast estimate</h2>
      <label for="locality" class="muted">Choose a Pune ward</label>
      <select id="locality" class="select"><option value="">Loading areas…</option></select>
      <div class="areaStats"><div class="areaStat"><span>Next 6 hours</span><strong id="area6">—</strong></div><div class="areaStat"><span>Next 24 hours</span><strong id="area24">—</strong></div></div>
      <p id="areaNote" class="muted small">Estimate for the selected ward centre, not an exact street location.</p>
    </section>

    <section class="card" aria-live="polite">
      <h2>Official IMD warning & nowcast</h2>
      <div class="status"><span id="imdDot" class="dot"></span><div><p id="imdTitle" class="statusTitle">Checking official data…</p><p id="imdText" class="statusText">This project may not have access to the official feed yet.</p></div></div>
      <p id="imdDetail" class="officialDetail small"></p>
      <p style="margin:12px 0 0"><a class="link" href="https://mausam.imd.gov.in/" target="_blank" rel="noopener noreferrer">Check current official IMD updates ↗</a></p>
    </section>

    <section class="card">
      <h2>✅ Remember</h2>
      <ul class="list"><li>Avoid flooded roads and underpasses; never walk or drive through floodwater.</li><li>Stay away from riverbanks and fast-flowing water.</li><li>Follow official IMD and local-authority advice; move to safer ground if water is rising.</li></ul>
      <div class="notice">This is an experimental weather-based estimate, not an official warning. It may miss local rain or get the amount wrong. A lack of an alert does not guarantee safe conditions    .</div>
    </section>
  </main>
  <footer class="footer">Forecasts are estimates, not rain-gauge readings. Check official sources during severe weather.</footer>
  <script src="/ml/widget.js"></script>
</div>
<script>
(() => {
  const $ = id => document.getElementById(id);
  let wards = [];
  const fmt = value => value != null && value !== "" && Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} mm` : "Unavailable";
  const fmtTime = value => {
    if (!value) return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString("en-IN", {day:"numeric",month:"short",year:"numeric",hour:"numeric",minute:"2-digit"});
  };
  async function getJson(url) {
    const r = await fetch(url, {cache:"no-store"});
    let d; try { d = await r.json(); } catch (_) { throw new Error("Invalid response"); }
    if (!r.ok) throw new Error("Request failed");
    return d;
  }
  function setStatus(dotId, titleId, textId, title, text, tone="") {
    $(dotId).className = `dot ${tone}`;
    $(titleId).textContent = title;
    $(textId).textContent = text;
  }
  function updateArea() {
    const w = wards.find(row => String(row.wardnum) === $("locality").value);
    $("area6").textContent = w ? fmt(w.forecast_6h_mm) : "—";
    $("area24").textContent = w ? fmt(w.forecast_24h_mm) : "—";
    $("areaNote").textContent = w ? `${w.name} · Ward ${w.wardnum}. This is a ward-centre estimate, not an exact street reading.` : "Estimates based on open-source data";
  }
  function renderForecast(data) {
    const input = data.inputs || {};
    const advisory = data.advisory_preview || {};
    const reasons = advisory.trigger_reason_codes || [];
    const local = reasons.includes("LOCAL_RAINFALL_WATCH");
    const upstream = reasons.includes("UPSTREAM_CATCHMENT_RAIN_WATCH");
    if (!input.fetched_at_local || !input.model_valid_time_local || input.average_forecast_6h_mm == null || input.average_forecast_24h_mm == null) throw new Error("Forecast or timestamp unavailable");
    $("city6").textContent = fmt(input.average_forecast_6h_mm);
    $("city24").textContent = fmt(input.average_forecast_24h_mm);
    $("meta").textContent = `Updated ${fmtTime(input.fetched_at_local)} · Source: Open-Meteo · Forecast on data from ${fmtTime(input.model_valid_time_local)}`;
    if (local || upstream) {
      const title = local && upstream ? "Rainfall watch suggested" : local ? "Rain may be significant in some areas" : "Rain is forecast near upstream reservoir areas";
      const detail = local && upstream ? "A project-generated watch is triggered for local and upstream rainfall. This is not an official warning." : local ? "A project-generated rainfall watch is triggered for some ward centres. This is not an official warning." : "An upstream rainfall signal is elevated. This does not confirm dam releases or downstream flooding.";
      setStatus("outlookDot","outlookTitle","outlookText",title,detail,"watch");
    } else {
      setStatus("outlookDot","outlookTitle","outlookText","No rain alert from this forecast right now","No watch threshold is crossed in the current forecast.");
    }
    wards = Array.isArray(data.wards) ? data.wards : [];
    const select = $("locality"); const old = select.value; select.replaceChildren();
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Choose a Pune ward"; select.append(placeholder);
    wards.slice().sort((a,b)=>String(a.name).localeCompare(String(b.name))).forEach(w=>{const o=document.createElement("option");o.value=String(w.wardnum);o.textContent=w.name;select.append(o);});
    if (wards.some(w=>String(w.wardnum)===old)) select.value=old;
    updateArea();
  }
  function summarizeOfficial(warningData, nowcastData) {
    const details = [];
    let warningState = "Check IMD directly for current warnings.";
    let tone = "";
    const records = warningData && Array.isArray(warningData.records) ? warningData.records : [];
    if (warningData && warningData.available && records.length) {
      const r = records[0];
      if (r.issue_age_assessment !== "issued_within_24h" || !r.issued_at_local) {
        warningState = "The latest bulletin is old or its issue time cannot be verified. Check IMD directly."; tone = "watch";
      } else {
        const days = (r.days || []).filter(d=>d.warning_present);
        const labels = days.flatMap(d=>(d.warnings||[]).filter(w=>w.code!==1).map(w=>`Day ${d.day_number}: ${w.label}`));
        warningState = labels.length ? `IMD district bulletin: ${labels.join(" · ")}` : "Recent IMD district bulletin lists no coded warnings in its outlook. Keep checking official updates.";
        tone = labels.length ? "watch" : "good";
        details.push(`Bulletin issued ${fmtTime(r.issued_at_local)} · district-level information.`);
      }
    } else if (warningData && warningData.status !== "authentication_required") {
      warningState = "Official warning data is temporarily unavailable. Check IMD directly.";
    }
    const nowRecords = nowcastData && Array.isArray(nowcastData.records) ? nowcastData.records : [];
    if (nowcastData && nowcastData.available && nowRecords.length) {
      const fresh = nowRecords.filter(r=>r.freshness==="recent_issue_time" && !r.expired_by_validity_time && r.issue_time_local);
      if (!fresh.length) details.push("District nowcast issue time is old or unverified.");
      else {
        const labels = fresh.flatMap(r=>(r.categories||[]).filter(c=>c.category_number!==1).map(c=>c.label));
        details.push(`District nowcast: ${labels.length ? [...new Set(labels)].join(" · ") : "No coded warning in the returned record"} (issued ${fmtTime(fresh[0].issue_time_local)}).`);
      }
    } else if (nowcastData && nowcastData.status !== "authentication_required") {
      details.push("District nowcast is temporarily unavailable.");
    }
    setStatus("imdDot","imdTitle","imdText",warningState,tone==="watch" ? "Check the official source for the latest instructions." : "Official information can change, confirm directly with IMD.",tone);
    $("imdDetail").textContent = details.join("\n");
  }
  async function refresh() {
    $("refreshBtn").disabled=true; $("refreshBtn").textContent="Refreshing…";
    try {
      const results = await Promise.allSettled([getJson("/prediction/forecast"),getJson("/nowcast/imd/district-warning"),getJson("/nowcast/imd")]);
      if (results[0].status === "fulfilled") {
        try { renderForecast(results[0].value); }
        catch (_) { $("city6").textContent="Unavailable"; $("city24").textContent="Unavailable"; $("meta").textContent="Forecast freshness could not be verified."; setStatus("outlookDot","outlookTitle","outlookText","Forecast unavailable","No current estimate is shown. Please check official weather updates.","bad"); }
      } else {
        $("city6").textContent="Unavailable"; $("city24").textContent="Unavailable"; $("meta").textContent="Forecast freshness could not be verified."; setStatus("outlookDot","outlookTitle","outlookText","Forecast unavailable","No current estimate is shown. Please check official weather updates.","bad");
      }
      summarizeOfficial(results[1].status==="fulfilled" ? results[1].value : {available:false,status:"source_error"},results[2].status==="fulfilled" ? results[2].value : {available:false,status:"source_error"});
    } finally { $("refreshBtn").disabled=false; $("refreshBtn").textContent="↻ Refresh"; }
  }
  $("locality").addEventListener("change",updateArea);
  $("refreshBtn").addEventListener("click",()=>refresh().catch(()=>{}));
  refresh().catch(()=>setStatus("outlookDot","outlookTitle","outlookText","Outlook unavailable","Please check official IMD updates.","bad"));
})();
</script>
</body></html>"""


@app.get("/outlook", response_class=HTMLResponse)
def outlook(current_cusecs: float = DEFAULT_CUSECS):
    """Display ward-local and upstream rainfall forecasts plus advisory previews."""
    import math
    from html import escape

    if not math.isfinite(current_cusecs) or current_cusecs < 0:
        raise HTTPException(status_code=422, detail="current_cusecs must be finite and non-negative.")

    weather = fetch_integrated_rainfall_forecasts()
    if weather.get("error") or not weather.get("ward_forecasts"):
        message = escape(str(weather.get("error") or "Ward-centroid rainfall forecast is unavailable."))
        return HTMLResponse(
            content=f"""<!doctype html><html><head><meta charset="utf-8"><title>Pune Flood Outlook — unavailable</title>
            <style>body{{font-family:Segoe UI,Arial,sans-serif;background:#f1f5f9;color:#172033;padding:28px}}.panel{{max-width:760px;margin:30px auto;background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:24px}}.err{{background:#fef2f2;color:#991b1b;padding:14px;border-radius:8px;overflow-wrap:anywhere}}a{{color:#0f766e}}</style></head>
            <body><div class="panel"><h1>Pune Flood Early Warning — Outlook</h1><div class="err"><b>Forecast unavailable</b><p>{message}</p>No current predictive verdict has been generated. Missing weather data was not replaced with zero.</div><p><a href="/">Back to scenario dashboard</a></p></div></body></html>""",
            status_code=503,
        )

    try:
        wards = compute_predictive_risk(weather, current_cusecs)
    except (ValueError, KeyError, TypeError) as exc:
        message = escape(str(exc))
        return HTMLResponse(
            content=f"<html><body style='font-family:Segoe UI,Arial;padding:24px'><h1>Prediction unavailable</h1><p>{message}</p><a href='/'>Back to dashboard</a></body></html>",
            status_code=503,
        )

    ranked = wards.sort_values("predictive_risk_score", ascending=False).copy()
    average_score = float(ranked["predictive_risk_score"].mean())
    max_score = float(ranked["predictive_risk_score"].max())
    high_count = int((ranked["predictive_risk_score"] >= 0.6).sum())
    critical_count = int((ranked["predictive_risk_score"] >= 0.8).sum())
    top = ranked.head(10)
    advisory = build_advisory_payload(weather, ranked, current_cusecs)
    advisory_title = escape(str(advisory.get("status", "MONITORING")))
    advisory_message = escape(str(advisory.get("city_message", "")))
    preview_cards = []
    for preview in advisory.get("sms_previews", []):
        preview_cards.append(
            "<article class='sms-card'>"
            f"<div class='sms-head'>Ward {int(preview['wardnum'])} · {escape(str(preview['ward_name']))} · {escape(str(preview['risk_level']))}</div>"
            f"<pre>{escape(str(preview['message']))}</pre>"
            "</article>"
        )
    if advisory.get("upstream_sms_preview"):
        preview_cards.insert(0,
            "<article class='sms-card upstream'>"
            "<div class='sms-head'>Upstream catchment advisory preview</div>"
            f"<pre>{escape(str(advisory['upstream_sms_preview']))}</pre>"
            "</article>"
        )
    if not preview_cards:
        preview_cards.append("<p class='muted'>No SMS advisory preview was generated because no experimental watch/priority trigger was met.</p>")
    advisory_cards_html = "\n".join(preview_cards)
    upstream_note = (
        f"Four reservoir-adjacent points: mean {float(weather.get('upstream_forecast_6h_mm') or 0):.1f} mm/6h and "
        f"{float(weather.get('upstream_forecast_24h_mm') or 0):.1f} mm/24h; max "
        f"{float(weather.get('upstream_max_forecast_6h_mm') or 0):.1f} / "
        f"{float(weather.get('upstream_max_forecast_24h_mm') or 0):.1f} mm. "
        if weather.get("upstream_forecast_available") else
        "Upstream forecast unavailable. River-corridor score uses discharge scenario only until data returns."
    )
    upstream_note = escape(upstream_note)
    advisory_block = f"""
<section class="panel"><h2>Human-readable advisory previews · {advisory_title}</h2><div class="panel-body">
<p>{advisory_message}</p><p class="muted">Upstream rainfall input: {upstream_note}</p>
<div class="warning">Gate status and actual discharge are unknown. Rainfall does not prove that gates will open. These messages are previews only; no SMS has been sent.</div>
<div class="sms-list">{advisory_cards_html}</div>
<p class="muted">Advisory-history endpoint: <a href="/advisory/history">recent forecast/advisory runs</a>. Feedback endpoint: POST a manually verified outcome to <code>/advisory/feedback</code>.</p>
</div></section>
"""

    table_rows = []
    for _, row in top.iterrows():
        table_rows.append(
            "<tr>"
            f"<td>{int(row['wardnum'])}</td>"
            f"<td>{escape(str(row['name']))}</td>"
            f"<td><b>{float(row['predictive_risk_score']):.3f}</b></td>"
            f"<td>{escape(str(row['predictive_risk_level']))}</td>"
            f"<td>{float(row['forecast_6h_mm']):.1f}</td>"
            f"<td>{float(row['forecast_24h_mm']):.1f}</td>"
            "</tr>"
        )
    rows_html = "\n".join(table_rows)

    forecast_start = escape(str(weather.get("forecast_start_local") or "Unavailable"))
    forecast_end = escape(str(weather.get("forecast_end_local") or "Unavailable"))
    model_valid = escape(str(weather.get("model_valid_time_local") or "Unavailable"))
    fetched_at = escape(str(weather.get("fetched_at_local") or "Unavailable"))
    avg6 = float(weather.get("forecast_6h_mm") or 0.0)
    avg24 = float(weather.get("forecast_24h_mm") or 0.0)
    max6 = float(weather.get("max_forecast_6h_mm") or 0.0)
    max24 = float(weather.get("max_forecast_24h_mm") or 0.0)
    top_ward = ranked.iloc[0]
    top_ward_name = escape(str(top_ward["name"]))
    top_ward_level = escape(str(top_ward["predictive_risk_level"]))
    top_score = float(top_ward["predictive_risk_score"])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pune Flood Early Warning — Forecast Outlook</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:#f1f5f9;color:#172033}}
.page{{max-width:1500px;margin:auto;padding:24px}}.header{{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}}
.kicker{{font-size:11px;text-transform:uppercase;letter-spacing:1.3px;font-weight:800;color:#0f766e}}h1{{margin:4px 0;font-size:30px}}.muted{{color:#64748b;font-size:13px;line-height:1.6}}
a{{color:#0f766e}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:11px;padding:16px;min-width:0}}.label{{color:#64748b;font-size:12px;margin-bottom:8px}}.value{{font-size:27px;font-weight:750;overflow-wrap:anywhere}}
.panel{{background:#fff;border:1px solid #e2e8f0;border-radius:11px;overflow:hidden;margin:16px 0}}.panel h2{{font-size:16px;margin:0;padding:16px 18px;border-bottom:1px solid #edf1f5}}
.panel-body{{padding:14px 18px}}.warning{{background:#fff7ed;color:#9a3412;border-radius:8px;padding:12px;font-size:12px;line-height:1.6;margin-top:10px}}
iframe{{width:100%;height:650px;border:0;display:block;background:#e2e8f0}}.sms-list{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:14px}}.sms-card{{border:1px solid #dbe3ec;border-radius:8px;background:#f8fafc;padding:12px;min-width:0}}.sms-card.upstream{{border-color:#f59e0b;background:#fffbeb}}.sms-head{{font-weight:700;font-size:12px;margin-bottom:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;font-size:12px;line-height:1.55;margin:0}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:10px 9px;text-align:left;border-bottom:1px solid #edf1f5}}th{{font-size:11px;text-transform:uppercase;color:#64748b;letter-spacing:.4px}}tr:last-child td{{border-bottom:0}}
@media(max-width:850px){{.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.page{{padding:15px}}iframe{{height:560px}}.sms-list{{grid-template-columns:1fr}}}}@media(max-width:500px){{.metrics{{grid-template-columns:1fr 1fr}}.value{{font-size:22px}}}}
</style></head><body><main class="page">
<header class="header"><div><div class="kicker">Pune · Flood Early Warning Prototype</div><h1>Ward-level forecast outlook</h1><div class="muted">V2.3: ward-local rainfall + upstream rainfall vigilance · 58 wards</div></div><a href="/">← Scenario dashboard</a></header>
<section class="metrics">
<div class="card"><div class="label">City average index</div><div class="value">{average_score:.3f}</div><div class="muted">Mean across ward indices</div></div>
<div class="card"><div class="label">Highest ward index</div><div class="value">{top_score:.3f}</div><div class="muted">{top_ward_name} · {top_ward_level}</div></div>
<div class="card"><div class="label">High / critical wards</div><div class="value">{high_count}</div><div class="muted">Index ≥ 0.60; critical ≥ 0.80 ({critical_count})</div></div>
<div class="card"><div class="label">Average rainfall forecast</div><div class="value">{avg24:.1f} mm</div><div class="muted">Next 24 hours; maximum ward forecast {max24:.1f} mm</div></div>
</section>
<section class="panel"><h2>Forecast input status</h2><div class="panel-body">
<p class="muted">Source: {escape(str(weather.get('source') or 'Open-Meteo weather model'))} · forecast locations: {int(weather.get('ward_count') or 0)}.</p>
<p class="muted">Next-6-hour average / maximum: <b>{avg6:.1f} / {max6:.1f} mm</b>. Next-24-hour average / maximum: <b>{avg24:.1f} / {max24:.1f} mm</b>.</p>
<p class="muted">Forecast window: {forecast_start} to {forecast_end}. Model valid: {model_valid}. Fetched: {fetched_at}.</p>
<p class="muted">Discharge input: {current_cusecs:,.0f} cusecs — configured scenario only; no verified live Khadakwasla discharge feed is connected.</p>
<div class="warning"><b>Interpretation:</b> scores are experimental 0–1 indices, not calibrated flood probabilities. Ward-centroid rainfall forecasts may share the same underlying weather-model grid cells. There is no live river-level, ward drainage-capacity, or waterlogging-sensor input. This is an academic prototype, not an official warning.</div>
</div></section>
{advisory_block}
<section class="panel"><h2>V2.3 predictive ward map</h2><iframe title="Ward-level predictive index map" src="/map?prediction_mode=forecast&amp;current_cusecs={current_cusecs}"></iframe></section>
<section class="panel"><h2>Top 10 ward scores</h2><div class="panel-body" style="overflow-x:auto"><table><thead><tr><th>Ward</th><th>Name</th><th>Index</th><th>Band</th><th>Forecast 6h (mm)</th><th>Forecast 24h (mm)</th></tr></thead><tbody>{rows_html}</tbody></table></div></section>
<p class="muted">Rainfall forecasts do not guarantee that flooding will occur. Thresholds and weights remain provisional and require independent event-based validation.</p>
</main></body></html>"""


# -------------------------------------------------------
# AUTOMATIC BACKGROUND ALERT SCHEDULER
# -------------------------------------------------------

def release_alert_scheduler_lock(token):
    """Release the Redis lock only if this scheduler owns it."""
    alerts_redis.eval(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """,
        1,
        ALERT_SCHEDULER_LOCK_KEY,
        token,
    )


async def alert_scheduler_loop():
    """
    Check the forecast immediately, then every 15 minutes.
    Runs independently of whether the dashboard is open.
    """
    while True:
        lock_token = secrets.token_hex(16)

        try:
            # Avoid simultaneous scheduled checks by multiple workers.
            lock_acquired = await asyncio.to_thread(
                alerts_redis.set,
                ALERT_SCHEDULER_LOCK_KEY,
                lock_token,
                nx=True,
                ex=ALERT_SCHEDULER_LOCK_TTL_SECONDS,
            )

            if not lock_acquired:
              logging.info(
              "[Flood EWS] Scheduler lock is occupied; retrying in 30 seconds."
              )
              await asyncio.sleep(30)
              continue

            try:
                # Fetch the latest rainfall forecast.
                forecast = await asyncio.to_thread(
                    fetch_integrated_rainfall_forecasts
                )

                # Reuse the existing alert evaluation function.
                result = await asyncio.to_thread(
                    evaluate_rainfall_alert,
                    forecast,
                )

                # Record what the scheduled run saw and which advisory it produced.
                # History failures must not stop the alert loop.
                advisory_run_id = None
                advisory_preview = None
                try:
                    if (
                        forecast.get("ward_forecasts")
                        and not forecast.get("error")
                        and not _live_forecast_readiness_blocker(forecast)
                    ):
                        scored_wards = await asyncio.to_thread(
                            compute_predictive_risk, forecast, DEFAULT_CUSECS
                        )
                        advisory_preview = build_advisory_payload(
                            forecast, scored_wards, DEFAULT_CUSECS
                        )
                        advisory_run_id = await asyncio.to_thread(
                            record_advisory_run, forecast, advisory_preview,
                            DEFAULT_CUSECS, "scheduler"
                        )
                except Exception:
                    logging.exception("Could not record scheduled advisory snapshot")

                # Separate Telegram opt-in; disabled by default. Evaluates the same
                # fresh forecast and never sends simulations, stale data, or routine checks.
                telegram_result = await asyncio.to_thread(
                    _process_scheduled_telegram_alert, forecast, advisory_preview
                )

                checked_at = datetime.now(
                    ZoneInfo("Asia/Kolkata")
                ).isoformat(timespec="seconds")

                ALERT_SCHEDULER_STATUS.update({
                    "enabled": True,
                    "interval_minutes": 15,
                    "last_checked_at_local": checked_at,
                    "last_result": result.get(
                        "status", "completed"
                    ),
                    "last_error": (
                        result.get("error")
                        or forecast.get("error")
                    ),
                    "last_forecast_24h_mm": result.get(
                        "forecast_24h_mm",
                        forecast.get("forecast_24h_mm"),
                    ),
                    "last_upstream_max_24h_mm": forecast.get("upstream_max_forecast_24h_mm"),
                    "last_advisory_run_id": advisory_run_id,
                    "last_alert_created": bool(
                        result.get("alert_created", False)
                    ),
                    "last_telegram_status": telegram_result.get("status", "unknown"),
                    "last_telegram_error": telegram_result.get("blocking_reason") or telegram_result.get("provider_error"),
                    "last_telegram_sent": bool(telegram_result.get("sent", False)),
                    "last_telegram_trigger_reason_codes": telegram_result.get("trigger_reason_codes", []),
                })

                logging.info(
                    "[Flood EWS] Scheduled check completed: "
                    "status=%s, rainfall=%s mm, alert_created=%s, telegram_status=%s, telegram_sent=%s",
                    ALERT_SCHEDULER_STATUS["last_result"],
                    ALERT_SCHEDULER_STATUS["last_forecast_24h_mm"],
                    ALERT_SCHEDULER_STATUS["last_alert_created"],
                    ALERT_SCHEDULER_STATUS["last_telegram_status"],
                    ALERT_SCHEDULER_STATUS["last_telegram_sent"],
                )

            finally:
                await asyncio.to_thread(
                    release_alert_scheduler_lock,
                    lock_token,
                )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            checked_at = datetime.now(
                ZoneInfo("Asia/Kolkata")
            ).isoformat(timespec="seconds")

            ALERT_SCHEDULER_STATUS.update({
                "last_checked_at_local": checked_at,
                "last_result": "error",
                "last_error": str(exc),
                "last_alert_created": False,
                "last_telegram_status": "scheduler_error",
                "last_telegram_error": str(exc),
                "last_telegram_sent": False,
                "last_telegram_trigger_reason_codes": [],
            })

            logging.exception(
                "[Flood EWS] Background alert check failed"
            )

        # Wait 15 minutes before the next scheduled evaluation.
        await asyncio.sleep(ALERT_CHECK_INTERVAL_SECONDS)


@app.on_event("startup")
async def start_alert_scheduler():
    global alert_scheduler_task

    # Optional switch to disable automatic checks during development.
    if os.getenv(
        "FLOOD_EWS_DISABLE_SCHEDULER", "false"
    ).lower() == "true":
        ALERT_SCHEDULER_STATUS["enabled"] = False
        logging.info(
            "[Flood EWS] Background alert scheduler disabled."
        )
        return

    ALERT_SCHEDULER_STATUS["enabled"] = True

    if (
        alert_scheduler_task is None
        or alert_scheduler_task.done()
    ):
        alert_scheduler_task = asyncio.create_task(
            alert_scheduler_loop()
        )

        logging.info(
            "[Flood EWS] Background alert scheduler started. "
            "Interval: 15 minutes."
        )


@app.on_event("shutdown")
async def stop_alert_scheduler():
    global alert_scheduler_task

    if alert_scheduler_task is not None:
        alert_scheduler_task.cancel()

        try:
            await alert_scheduler_task
        except asyncio.CancelledError:
            pass

        alert_scheduler_task = None

    logging.info(
        "[Flood EWS] Background alert scheduler stopped."
    )


@app.get("/alerts/status")
def get_alert_scheduler_status():
    return dict(ALERT_SCHEDULER_STATUS)

# -------------------------------------------------------
# OFFICIAL IMD DISTRICT NOWCAST CONNECTOR (READ-ONLY V2.8)
# -------------------------------------------------------
# This is an optional, read-only integration. It does not feed the existing
# experimental ward risk model or Telegram scheduler. IMD API access may
# require a registered account / authorization depending on the access route.
# Never put an access token in source code or return it in an API response.
IMD_NOWCAST_DEFAULT_ENDPOINT = "https://api.imd.gov.in/api/v1/districtnowcast"
IMD_NOWCAST_PUBLIC_PAGE = "https://mausam.imd.gov.in/responsive/districtWiseNowcastGIS.php"
IMD_API_PORTAL = "https://api.imd.gov.in/public/index.php"

IMD_NOWCAST_CATEGORY_LABELS = {
    1: "No weather warning",
    2: "Light rain (under 5 mm/hour)",
    3: "Light snow",
    4: "Light thunderstorm (gusts under 40 km/h)",
    5: "Slight dust storm",
    6: "Low lightning probability (under 30%)",
    7: "Moderate rain (5–15 mm/hour)",
    8: "Moderate snow",
    9: "Moderate thunderstorm (gusts around 41–61 km/h)",
    10: "Moderate dust storm",
    11: "Moderate lightning probability (30–60%)",
    12: "Heavy rain (over 15 mm/hour)",
    13: "Heavy snow",
    14: "Severe thunderstorm (gusts around 62–87 km/h)",
    15: "Very severe thunderstorm (gusts over 87 km/h)",
    16: "Other weather warning",
    17: "Thunderstorm with hail",
    18: "Severe dust storm",
    19: "High lightning probability (over 60%)",
}


def _imd_nowcast_config():
    """Read IMD connector settings from environment; never expose credential values."""
    return {
        "endpoint": os.getenv("IMD_NOWCAST_ENDPOINT", IMD_NOWCAST_DEFAULT_ENDPOINT).strip(),
        "token": os.getenv("IMD_API_TOKEN", "").strip(),
        "auth_header": os.getenv("IMD_API_AUTH_HEADER", "Authorization").strip() or "Authorization",
        "auth_scheme": os.getenv("IMD_API_AUTH_SCHEME", "Bearer").strip(),
        "district_filter": os.getenv("IMD_NOWCAST_DISTRICT_FILTER", "PUNE").strip().upper() or "PUNE",
    }


def _imd_casefold_get(row, *names):
    """Get a row value case-insensitively, tolerating API schema capitalization."""
    if not isinstance(row, dict):
        return None
    lowered = {str(k).strip().lower().replace(" ", "_"): v for k, v in row.items()}
    for name in names:
        key = str(name).strip().lower().replace(" ", "_")
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


def _imd_extract_records(payload):
    """Normalize known API wrappers to a list of record dictionaries."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "records", "results", "result", "items", "districtnowcast", "district_nowcast"):
        value = _imd_casefold_get(payload, key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _imd_extract_records(value)
            if nested:
                return nested
    # Some APIs return one record directly.
    row_keys = {str(k).strip().lower().replace(" ", "_") for k in payload.keys()}
    if row_keys.intersection({"district", "station", "state_district", "districtname", "date"}):
        return [payload]
    # Explore nested response objects without interpreting arbitrary scalars as rows.
    for value in payload.values():
        if isinstance(value, (dict, list)):
            nested = _imd_extract_records(value)
            if nested:
                return nested
    return []


def _imd_row_matches_district(row, district_filter="PUNE"):
    """Match Pune district by named fields, not by guessing numeric district IDs."""
    candidates = [
        _imd_casefold_get(row, "District", "DistrictName", "District_Name"),
        _imd_casefold_get(row, "Station", "Station_Name", "StationName"),
        _imd_casefold_get(row, "State_District", "StateDistrict"),
        _imd_casefold_get(row, "District Name"),
    ]
    haystack = " ".join(str(value) for value in candidates if value is not None).upper()
    # Require the requested district name to appear in an explicit location field.
    return district_filter.upper() in haystack


def _imd_value_is_populated(value):
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "none", "null", "0", "-", "--", "nil", "na", "n/a"}


def _imd_active_categories(row):
    """Translate present CatN fields using the documented IMD category legend."""
    active = []
    no_weather = False
    for category_number, label in IMD_NOWCAST_CATEGORY_LABELS.items():
        value = _imd_casefold_get(row, f"Cat{category_number}")
        if not _imd_value_is_populated(value):
            continue
        value_text = str(value).strip()
        if category_number == 1 and value_text == "1":
            no_weather = True
            continue
        if category_number == 16:
            # Cat16 may carry free-text warning detail rather than a numeric code.
            active.append({"category_number": category_number, "label": label, "value": value_text})
        else:
            active.append({"category_number": category_number, "label": label, "value": value_text})
    if not active and no_weather:
        return [{"category_number": 1, "label": IMD_NOWCAST_CATEGORY_LABELS[1], "value": "1"}]
    return active


def _imd_parse_ist_datetime(date_value, time_value=None):
    """Parse IMD issue/validity fields conservatively as IST where parseable."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    if date_value is None:
        return None
    date_text = str(date_value).strip()
    time_text = str(time_value or "").strip()
    if time_text:
        # Common IMD format is HHmm; tolerate HH:mm and HHmmss.
        time_digits = time_text.replace(":", "")
        if time_digits.isdigit() and len(time_digits) == 4:
            time_text = time_digits[:2] + ":" + time_digits[2:]
        elif time_digits.isdigit() and len(time_digits) == 6:
            time_text = time_digits[:2] + ":" + time_digits[2:4] + ":" + time_digits[4:]
    formats = []
    if time_text:
        formats.extend(["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"])
    formats.extend(["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"])
    for fmt in formats:
        candidate = f"{date_text} {time_text}" if "%H" in fmt else date_text
        try:
            value = datetime.strptime(candidate, fmt).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            return value
        except ValueError:
            continue
    return None


def _imd_nowcast_response_error(status_code, status_text=""):
    """Sanitize source failures; never echo request URLs or authorization details."""
    if status_code == 401:
        return {
            "status": "authentication_required",
            "message": "IMD's data endpoint rejected unauthenticated access. Register/sign in through the official IMD API portal and configure approved access credentials locally.",
        }
    if status_code == 403:
        return {
            "status": "access_not_permitted",
            "message": "IMD returned HTTP 403. This account, endpoint, or network may not be authorized for this data product; check access terms with IMD.",
        }
    if status_code == 404:
        return {"status": "endpoint_not_found", "message": "The configured IMD endpoint was not found. Check IMD's current API documentation."}
    if status_code == 429:
        return {"status": "rate_limited", "message": "IMD rate-limited this request. Retry later; no cached data is substituted."}
    if status_code and status_code >= 500:
        return {"status": "source_unavailable", "message": f"IMD returned HTTP {status_code}. No nowcast data was generated."}
    return {"status": "source_error", "message": f"IMD request failed (HTTP {status_code or 'unknown'}). {status_text}".strip()}


@app.get("/nowcast/imd/status")
def imd_nowcast_status():
    """Read-only configuration check for the official IMD district-nowcast API."""
    cfg = _imd_nowcast_config()
    return {
        "provider": "India Meteorological Department (IMD) district-wise nowcast API",
        "mode": "read_only_configuration_status",
        "configured_endpoint_host": cfg["endpoint"].split("/")[2] if "://" in cfg["endpoint"] else "custom_endpoint",
        "api_token_configured": bool(cfg["token"]),
        "auth_header_name": cfg["auth_header"],
        "auth_scheme_configured": bool(cfg["auth_scheme"]),
        "district_filter": cfg["district_filter"],
        "fetch_enabled": bool(cfg["token"]),
        "telegram_delivery_connected": False,
        "source_documentation": "https://api.imd.gov.in/public/api_reference.html",
        "account_portal": IMD_API_PORTAL,
        "public_nowcast_page": IMD_NOWCAST_PUBLIC_PAGE,
        "note": "This connector is read-only and does not change the forecast model or send Telegram messages. API access is required; never share IMD credentials in chat.",
    }


@app.get("/nowcast/imd")
def imd_nowcast_latest():
    """Fetch and normalize the official district-level IMD nowcast when access is configured.

    This endpoint intentionally does not infer rain-cell motion, locality-level rain,
    arrival time, flooding, or dam operation. It only exposes the official district row.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    cfg = _imd_nowcast_config()
    fetched_at = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
    base = {
        "provider": "India Meteorological Department (IMD)",
        "product": "district-wise nowcast",
        "coverage_level": "district, not street/locality-level",
        "fetched_at_local": fetched_at,
        "source_url": IMD_NOWCAST_PUBLIC_PAGE,
        "api_documentation_url": "https://api.imd.gov.in/public/api_reference.html",
        "telegram_delivery_connected": False,
        "automatic_alerts_changed": False,
    }
    if not cfg["token"]:
        return {
            **base,
            "mode": "setup_required",
            "status": "authentication_required",
            "available": False,
            "district": cfg["district_filter"],
            "records": [],
            "error": "IMD_API_TOKEN is not configured in the API process environment.",
            "next_step": "Register/sign in at the official IMD API portal and obtain the authorized access method for the districtnowcast product. Then configure IMD_API_TOKEN locally; do not paste it into chat.",
            "public_reference_page": IMD_API_PORTAL,
        }

    headers = {"Accept": "application/json", "User-Agent": "Pune-Flood-EWS-Prototype/2.8"}
    auth_value = f"{cfg['auth_scheme']} {cfg['token']}".strip() if cfg["auth_scheme"] else cfg["token"]
    headers[cfg["auth_header"]] = auth_value
    try:
        response = requests.get(cfg["endpoint"], headers=headers, timeout=(5, 15))
    except requests.exceptions.Timeout:
        return {**base, "mode": "source_error", "status": "timeout", "available": False,
                "district": cfg["district_filter"], "records": [],
                "error": "IMD nowcast request timed out. No stale or simulated data was substituted."}
    except requests.exceptions.RequestException as exc:
        return {**base, "mode": "source_error", "status": "connection_error", "available": False,
                "district": cfg["district_filter"], "records": [],
                "error": f"Could not reach the IMD nowcast endpoint ({type(exc).__name__}). No data was generated."}

    if not response.ok:
        safe_error = _imd_nowcast_response_error(response.status_code)
        return {**base, "mode": "source_error", "status": safe_error["status"], "available": False,
                "district": cfg["district_filter"], "records": [],
                "http_status": response.status_code, "error": safe_error["message"],
                "next_step": "Check the official IMD API portal, endpoint access, and any account/IP allow-list requirements. Do not send secrets in chat."}

    try:
        payload = response.json()
    except ValueError:
        return {**base, "mode": "source_error", "status": "invalid_json", "available": False,
                "district": cfg["district_filter"], "records": [],
                "http_status": response.status_code,
                "error": "IMD endpoint returned a non-JSON response; no nowcast was generated."}

    records = _imd_extract_records(payload)
    matches = [row for row in records if _imd_row_matches_district(row, cfg["district_filter"])]
    if not matches:
        return {**base, "mode": "no_matching_district", "status": "no_pune_record_found", "available": False,
                "district": cfg["district_filter"], "records_received": len(records), "records": [],
                "error": "The endpoint responded, but no record matched the configured district name. Numeric district IDs are not guessed.",
                "diagnostic_sample_fields": sorted(str(k) for k in records[0].keys())[:40] if records else [],
                "next_step": "Review IMD's returned schema and district names; configure an official district ID only after confirming it from the API documentation."}

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    normalized = []
    for row in matches:
        issue_date = _imd_casefold_get(row, "Date", "full_date", "date_of_issue")
        issue_time = _imd_casefold_get(row, "toi", "TOI", "TimeOfIssue", "Time of Issue")
        valid_time = _imd_casefold_get(row, "Vupto", "VUpTo", "valid_upto", "Valid Until")
        issue_dt = _imd_parse_ist_datetime(issue_date, issue_time)
        valid_dt = _imd_parse_ist_datetime(issue_date, valid_time)
        if issue_dt and valid_dt and valid_dt < issue_dt:
            from datetime import timedelta
            valid_dt = valid_dt + timedelta(days=1)
        if issue_dt:
            age_minutes = max(0, int((now_ist - issue_dt).total_seconds() / 60))
            if age_minutes > 360:
                freshness = "stale_over_6h"
            elif issue_dt > now_ist.replace(minute=now_ist.minute) and (issue_dt - now_ist).total_seconds() > 600:
                freshness = "future_issue_time_check_clock"
            else:
                freshness = "recent_issue_time"
        else:
            age_minutes = None
            freshness = "unknown_issue_time"
        expired = bool(valid_dt and now_ist > valid_dt)
        category_rows = _imd_active_categories(row)
        message = _imd_casefold_get(row, "message", "Message", "consolidated_message", "WarningsDetails")
        normalized.append({
            "district": _imd_casefold_get(row, "District", "DistrictName", "State_District", "Station", "Station_Name") or cfg["district_filter"],
            "issued_date_raw": issue_date,
            "issue_time_raw": issue_time,
            "issue_time_local": issue_dt.isoformat(timespec="minutes") if issue_dt else None,
            "valid_until_raw": valid_time,
            "valid_until_local": valid_dt.isoformat(timespec="minutes") if valid_dt else None,
            "age_minutes_at_fetch": age_minutes,
            "freshness": freshness,
            "expired_by_validity_time": expired,
            "warning_color_code": _imd_casefold_get(row, "color", "Color", "color_code"),
            "categories": category_rows,
            "message": str(message).strip() if message is not None else None,
        })

    return {
        **base,
        "mode": "live_read_only",
        "status": "available",
        "available": True,
        "district": cfg["district_filter"],
        "records_received": len(records),
        "matching_records": len(normalized),
        "records": normalized,
        "interpretation_limits": [
            "This is an official district-level nowcast/warning product, not a street-by-street rainfall observation.",
            "A district nowcast does not provide a storm-cell track or a locality arrival time.",
            "Do not turn expired or stale records into active alerts.",
            "This endpoint does not send Telegram messages and does not alter scheduled delivery.",
        ],
    }
# ---------------------------------------------------------------------------
# IMD DISTRICT-WISE WARNING CONNECTOR (READ-ONLY V1)
# ---------------------------------------------------------------------------
# Separate from the district-nowcast connector. Uses the same locally stored
# IMD token/header settings, but its own endpoint override. Never sends alerts.
IMD_DISTRICT_WARNING_DEFAULT_ENDPOINT = "https://api.imd.gov.in/api/v1/districtwarning"

IMD_DISTRICT_WARNING_CODES = {
    1: "No warning",
    2: "Heavy rain",
    3: "Heavy snow",
    4: "Thunderstorm and lightning / squall",
    5: "Hailstorm",
    6: "Dust storm",
    7: "Dust-raising winds",
    8: "Strong surface winds",
    9: "Heat wave",
    10: "Hot day",
    11: "Warm night",
    12: "Cold wave",
    13: "Cold day",
    14: "Ground frost",
    15: "Fog",
    16: "Very heavy rain",
    17: "Extremely heavy rain",
}
IMD_DISTRICT_WARNING_COLOR_CODES = {
    "1": "red",
    "2": "orange",
    "3": "yellow",
    "4": "green",
}


def _imd_parse_district_warning_issue_time(date_value, utc_time_value=None):
    """Parse the district-warning issue date + UTC clock into an aware datetime."""
    from datetime import datetime, timezone
    import re

    if date_value is None:
        return None
    date_text = str(date_value).strip()
    time_text = str(utc_time_value or "").strip()
    compact = time_text.replace(":", "")
    if compact.isdigit() and len(compact) == 4:
        time_text = compact[:2] + ":" + compact[2:]
    elif compact.isdigit() and len(compact) == 6:
        time_text = compact[:2] + ":" + compact[2:4] + ":" + compact[4:]

    date_formats = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")
    time_formats = ("%H:%M:%S", "%H:%M", "%H%M%S", "%H%M")
    for date_format in date_formats:
        try:
            parsed_date = datetime.strptime(date_text, date_format).date()
        except ValueError:
            continue
        if not time_text:
            return None
        for time_format in time_formats:
            try:
                parsed_time = datetime.strptime(time_text, time_format).time()
                return datetime.combine(parsed_date, parsed_time, tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _imd_district_warning_code_details(value):
    """Translate a warning-code field, preserving unknown numeric codes."""
    import re

    if value is None:
        return []
    text_value = str(value).strip()
    if not text_value or text_value.lower() in {"none", "null", "nil", "na", "n/a", "-", "--"}:
        return []
    codes = []
    for token in re.findall(r"\d+", text_value):
        code = int(token)
        if code not in codes:
            codes.append(code)
    if not codes:
        return [{"code": None, "label": text_value}]
    if 1 in codes and len(codes) > 1:
        codes = [code for code in codes if code != 1]
    return [
        {
            "code": code,
            "label": IMD_DISTRICT_WARNING_CODES.get(code, f"Unmapped warning code {code}"),
        }
        for code in codes
    ]


@app.get("/nowcast/imd/district-warning")
def imd_district_warning_latest():
    """Return normalized official five-day district warnings; never sends alerts."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    cfg = _imd_nowcast_config()
    endpoint = os.getenv(
        "IMD_DISTRICT_WARNING_ENDPOINT", IMD_DISTRICT_WARNING_DEFAULT_ENDPOINT
    ).strip()
    fetched_at = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
    base = {
        "provider": "India Meteorological Department (IMD)",
        "product": "district-wise warning",
        "coverage_level": "district, five-day warning product; not street/locality-level",
        "fetched_at_local": fetched_at,
        "configured_endpoint_host": endpoint.split("/")[2] if "://" in endpoint else "custom_endpoint",
        "district": cfg["district_filter"],
        "telegram_delivery_connected": False,
        "automatic_alerts_changed": False,
    }

    if not cfg["token"]:
        return {
            **base,
            "mode": "setup_required",
            "status": "authentication_required",
            "available": False,
            "records": [],
            "error": "IMD_API_TOKEN is not configured in the API process environment.",
            "next_step": "Wait for IMD authorization, then configure the approved token locally. Do not paste credentials into chat.",
        }

    headers = {"Accept": "application/json", "User-Agent": "Pune-Flood-EWS-Prototype/2.9"}
    auth_value = f"{cfg['auth_scheme']} {cfg['token']}".strip() if cfg["auth_scheme"] else cfg["token"]
    headers[cfg["auth_header"]] = auth_value

    try:
        response = requests.get(endpoint, headers=headers, timeout=(5, 15))
    except requests.exceptions.Timeout:
        return {**base, "mode": "source_error", "status": "timeout", "available": False,
                "records": [], "error": "IMD district-warning request timed out; no cached or simulated warning was substituted."}
    except requests.exceptions.RequestException as exc:
        return {**base, "mode": "source_error", "status": "connection_error", "available": False,
                "records": [], "error": f"Could not reach the IMD district-warning endpoint ({type(exc).__name__})."}

    if not response.ok:
        safe_error = _imd_nowcast_response_error(response.status_code)
        return {**base, "mode": "source_error", "status": safe_error["status"], "available": False,
                "records": [], "http_status": response.status_code, "error": safe_error["message"]}

    try:
        payload = response.json()
    except ValueError:
        return {**base, "mode": "source_error", "status": "invalid_json", "available": False,
                "records": [], "http_status": response.status_code,
                "error": "IMD district-warning endpoint returned non-JSON data."}

    source_records = _imd_extract_records(payload)
    district_filter = str(cfg["district_filter"]).strip().upper()
    matches = []
    for row in source_records:
        district = _imd_casefold_get(row, "District", "DistrictName", "District_Name")
        # Exact field match only: do not accidentally match a station or another district.
        if district is not None and str(district).strip().upper() == district_filter:
            matches.append(row)

    if not matches:
        return {
            **base,
            "mode": "no_matching_district",
            "status": "no_pune_record_found",
            "available": False,
            "records_received": len(source_records),
            "records": [],
            "diagnostic_sample_fields": sorted(str(k) for k in source_records[0].keys())[:40] if source_records else [],
            "error": "The endpoint responded, but no exact district-name match was found. No numeric district ID was guessed.",
        }

    now_utc = datetime.now(timezone.utc)
    normalized = []
    for row in matches:
        issue_date = _imd_casefold_get(row, "Date", "Date of Issue", "date_of_issue")
        issue_time_utc_raw = _imd_casefold_get(row, "UTC", "TimeUTC", "time_of_issue_utc")
        issued_at_utc = _imd_parse_district_warning_issue_time(issue_date, issue_time_utc_raw)

        age_minutes = None
        issue_age_assessment = "unknown_issue_time"
        issued_at_local = None
        if issued_at_utc is not None:
            age_seconds = (now_utc - issued_at_utc).total_seconds()
            age_minutes = int(age_seconds / 60)
            issued_at_local = issued_at_utc.astimezone(ZoneInfo("Asia/Kolkata")).isoformat(timespec="minutes")
            if age_seconds < -600:
                issue_age_assessment = "future_issue_time_check_clock"
            elif age_seconds < 24 * 60 * 60:
                issue_age_assessment = "issued_within_24h"
            else:
                issue_age_assessment = "older_than_24h"

        day_rows = []
        for day_number in range(1, 6):
            raw_codes = _imd_casefold_get(row, f"Day_{day_number}", f"Day{day_number}")
            color_raw = _imd_casefold_get(row, f"Day{day_number}_Color", f"Day_{day_number}_Color")
            warning_codes = _imd_district_warning_code_details(raw_codes)
            warning_present = any(item.get("code") != 1 for item in warning_codes)
            color_key = str(color_raw).strip() if color_raw is not None else ""
            day_rows.append({
                "day_number": day_number,
                "warning_codes_raw": raw_codes,
                "warnings": warning_codes,
                "warning_present": warning_present,
                "color_code_raw": color_raw,
                "color_label": IMD_DISTRICT_WARNING_COLOR_CODES.get(color_key),
            })

        normalized.append({
            "district": _imd_casefold_get(row, "District", "DistrictName", "District_Name"),
            "issue_date_raw": issue_date,
            "issue_time_utc_raw": issue_time_utc_raw,
            "issued_at_utc": issued_at_utc.isoformat(timespec="minutes") if issued_at_utc else None,
            "issued_at_local": issued_at_local,
            "issue_age_minutes": age_minutes,
            "issue_age_assessment": issue_age_assessment,
            "days": day_rows,
            "warning_present_in_any_day": any(item["warning_present"] for item in day_rows),
        })

    return {
        **base,
        "mode": "live_read_only",
        "status": "available",
        "available": True,
        "records_received": len(source_records),
        "matching_records": len(normalized),
        "records": normalized,
        "interpretation_limits": [
            "This is an official district-level five-day warning product, not a street-level rain observation.",
            "Issue age describes when the bulletin was issued; it does not by itself prove each forecast day's current validity.",
            "Official warning codes and colors are preserved separately from experimental project-generated risk scores.",
            "This endpoint is read-only and does not send Telegram messages or alter scheduled delivery.",
        ],
    }

from ml_outlook import router as ml_router, register_fetcher as ml_register_fetcher
ml_register_fetcher(fetch_integrated_rainfall_forecasts)
app.include_router(ml_router)

