
"""
Pune Flood Early Warning System: risk engine core.

This module:
1. Calculates the existing static susceptibility score.
2. Classifies rainfall only when its time window is verified.
3. Checks the configured dam-discharge corridor trigger.
4. Reports missing data explicitly.

It does NOT claim to produce a calibrated flood probability.
"""

from math import isfinite
from typing import Optional


# Existing prototype configuration
ELEVATION_WEIGHT = 0.30
INCIDENT_WEIGHT = 0.50
RIVER_PROXIMITY_WEIGHT = 0.20

DAM_DISCHARGE_THRESHOLD_CUSECS = 25_000.0


def _validate_score(name: str, value: Optional[float]) -> Optional[float]:
    """Validate a normalized score in the interval [0, 1]."""
    if value is None:
        return None

    value = float(value)

    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a finite value between 0 and 1.")

    return value


def calculate_static_susceptibility(
    elevation_risk_score: Optional[float],
    incident_risk_score: Optional[float],
    river_proximity_risk_score: Optional[float],
) -> Optional[float]:
    """
    Calculate the existing weighted static susceptibility score.

    All three inputs must already be normalized to [0, 1].
    If any input is missing, return None rather than silently
    changing the weights or treating missing data as zero.
    """
    elevation = _validate_score(
        "elevation_risk_score", elevation_risk_score
    )
    incidents = _validate_score(
        "incident_risk_score", incident_risk_score
    )
    river = _validate_score(
        "river_proximity_risk_score", river_proximity_risk_score
    )

    if elevation is None or incidents is None or river is None:
        return None

    score = (
        ELEVATION_WEIGHT * elevation
        + INCIDENT_WEIGHT * incidents
        + RIVER_PROXIMITY_WEIGHT * river
    )

    return round(score, 6)


def classify_imd_daily_rainfall(
    rainfall_mm: Optional[float],
    *,
    aligned_to_imd_24h_period: bool,
) -> str:
    """
    Classify rainfall using IMD daily rainfall bands.

    Only apply the bands when rainfall represents the relevant
    24-hour observation period ending at 08:30 IST.

    These are meteorological rainfall categories, not calibrated
    urban-flood-risk levels.
    """
    if rainfall_mm is None:
        return "UNAVAILABLE"

    rainfall_mm = float(rainfall_mm)

    if not isfinite(rainfall_mm) or rainfall_mm < 0:
        raise ValueError("rainfall_mm must be finite and non-negative.")

    if not aligned_to_imd_24h_period:
        return "WINDOW_UNVERIFIED"

    if rainfall_mm == 0:
        return "NO_RAIN_REPORTED"
    if rainfall_mm < 2.5:
        return "VERY_LIGHT"
    if rainfall_mm < 15.6:
        return "LIGHT"
    if rainfall_mm < 64.5:
        return "MODERATE"
    if rainfall_mm < 115.6:
        return "HEAVY"
    if rainfall_mm < 204.5:
        return "VERY_HEAVY"

    return "EXTREMELY_HEAVY"


def evaluate_ward(
    *,
    wardnum: int,
    ward_name: str,
    elevation_risk_score: Optional[float],
    incident_risk_score: Optional[float],
    river_proximity_risk_score: Optional[float],
    rainfall_mm_24h: Optional[float],
    rainfall_window_aligned_to_imd: bool,
    discharge_cusecs: Optional[float],
    in_downstream_corridor: Optional[bool],
    reporting_station_count: Optional[int] = None,
    expected_station_count: Optional[int] = None,
) -> dict:
    """
    Produce an explainable ward assessment from available inputs.

    The river threshold is the project's existing prototype
    threshold. It must be validated before operational deployment.

    'NO_CONFIGURED_TRIGGER' does not mean 'no flood risk'.
    It means no implemented trigger fired with the supplied data.
    """
    if not isinstance(wardnum, int) or isinstance(wardnum, bool):
        raise ValueError("wardnum must be an integer.")

    if not ward_name or not ward_name.strip():
        raise ValueError("ward_name must be a non-empty string.")

    if not isinstance(rainfall_window_aligned_to_imd, bool):
        raise ValueError("rainfall_window_aligned_to_imd must be boolean.")

    if in_downstream_corridor not in (True, False, None):
        raise ValueError("in_downstream_corridor must be True, False, or None.")

    # Validate the static score components and calculate the legacy score
    static_score = calculate_static_susceptibility(
        elevation_risk_score,
        incident_risk_score,
        river_proximity_risk_score,
    )

    rainfall_category = classify_imd_daily_rainfall(
        rainfall_mm_24h,
        aligned_to_imd_24h_period=rainfall_window_aligned_to_imd,
    )

    # Missing discharge is UNKNOWN, not zero
    if discharge_cusecs is None:
        discharge = None
        discharge_status = "UNKNOWN"
    else:
        discharge = float(discharge_cusecs)

        if not isfinite(discharge) or discharge < 0:
            raise ValueError(
                "discharge_cusecs must be finite and non-negative."
            )

        discharge_status = (
            "ACTIVE"
            if discharge >= DAM_DISCHARGE_THRESHOLD_CUSECS
            else "BELOW_THRESHOLD"
        )

    # Explicitly identify whether the configured river trigger applies
    if discharge_status == "UNKNOWN":
        river_corridor_trigger = None
    elif discharge_status == "BELOW_THRESHOLD":
        river_corridor_trigger = False
    elif in_downstream_corridor is None:
        river_corridor_trigger = None
    else:
        river_corridor_trigger = bool(in_downstream_corridor)

    # Optional gauge coverage, without inventing missing values
    coverage_fraction = None

    if reporting_station_count is not None or expected_station_count is not None:
        if reporting_station_count is None or expected_station_count is None:
            raise ValueError(
                "Provide both reporting_station_count and "
                "expected_station_count, or neither."
            )

        if (
            isinstance(reporting_station_count, bool)
            or isinstance(expected_station_count, bool)
            or not isinstance(reporting_station_count, int)
            or not isinstance(expected_station_count, int)
        ):
            raise ValueError("Station counts must be integers.")

        if expected_station_count <= 0:
            raise ValueError("expected_station_count must be positive.")

        if not 0 <= reporting_station_count <= expected_station_count:
            raise ValueError(
                "Station counts must satisfy 0 <= reporting <= expected."
            )

        coverage_fraction = round(
            reporting_station_count / expected_station_count, 4
        )

    alerts = []

    if rainfall_category == "WINDOW_UNVERIFIED":
        alerts.append("RAINFALL_WINDOW_NEEDS_VERIFICATION")
    elif rainfall_category == "UNAVAILABLE":
        alerts.append("RAINFALL_UNAVAILABLE")
    elif rainfall_category in {"HEAVY", "VERY_HEAVY", "EXTREMELY_HEAVY"}:
        alerts.append("HEAVY_RAINFALL_CATEGORY_REVIEW")

    if static_score is None:
        alerts.append("STATIC_SUSCEPTIBILITY_INCOMPLETE")

    if discharge_status == "UNKNOWN":
        alerts.append("DAM_DISCHARGE_UNKNOWN")

    if discharge_status == "ACTIVE" and in_downstream_corridor is None:
        alerts.append("CORRIDOR_MEMBERSHIP_UNKNOWN")

    if river_corridor_trigger is True:
        alerts.append("RIVER_CORRIDOR_TRIGGER")

    # We deliberately do not invent a weighted final flood-risk score.
    # The existing static score and the live hazard triggers remain
    # separate until their combination can be validated.
    if river_corridor_trigger is True:
        assessment_status = "HIGH_RIVER_CORRIDOR_TRIGGER"
    elif rainfall_category in {"HEAVY", "VERY_HEAVY", "EXTREMELY_HEAVY"}:
        assessment_status = "HEAVY_RAINFALL_REVIEW_REQUIRED"
    elif (
        static_score is None
        or rainfall_category in {"UNAVAILABLE", "WINDOW_UNVERIFIED"}
        or discharge_status == "UNKNOWN"
        or (
            discharge_status == "ACTIVE"
            and in_downstream_corridor is None
        )
    ):
        assessment_status = "INCOMPLETE_ASSESSMENT"
    else:
        assessment_status = "NO_CONFIGURED_TRIGGER"

    return {
        "wardnum": wardnum,
        "ward_name": ward_name,
        "static_susceptibility_score": static_score,
        "rainfall_mm_24h": rainfall_mm_24h,
        "rainfall_category": rainfall_category,
        "discharge_cusecs": discharge,
        "discharge_status": discharge_status,
        "in_downstream_corridor": in_downstream_corridor,
        "river_corridor_trigger": river_corridor_trigger,
        "reporting_station_count": reporting_station_count,
        "expected_station_count": expected_station_count,
        "station_coverage_fraction": coverage_fraction,
        "assessment_status": assessment_status,
        "alerts": alerts,
    }
