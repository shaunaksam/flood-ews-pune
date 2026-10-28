#!/usr/bin/env python3
"""Pune rain-forecast backtest against the existing 3x3 IMD daily rainfall grid.

Source: Open-Meteo Single Runs API, archived ECMWF IFS HRES 9 km runs.
For each target day D, retrieves the 12 UTC run from D-2. That run is initialized
30.5 hours before D starts in Asia/Kolkata; Open-Meteo says global runs are
usually available 4-6 hours after initialization, giving an approximate
24.5-26.5 hour operational lead before the target day begins.

This is a rainfall forecast evaluation, NOT flood prediction accuracy and NOT
an exact replay of the current production scheduler. Data dates are limited to
monsoon months (June-October) in 2024-2025 by default. The script requests the
nine IMD grid coordinates in a single multi-location API request per target day.

Two forecast accumulation windows are compared. Open-Meteo hourly timestamps for
Asia/Kolkata follow UTC-aligned model hours, so the local timestamp labels are
at :30 (e.g. 01:30 IST), not :00. The requested 24 labels are therefore shifted
by 30 minutes to match the returned series:
- calendar_day: approximate 24 hourly bins labelled D 01:30 through D+1 00:30.
- imd_observation_day_approx: bins labelled D-1 09:30 through D 08:30, an
  hourly approximation to the IMD daily rainfall window ending around 08:30 IST.

Open-Meteo source documentation: https://open-meteo.com/en/docs/single-runs-api
Attribution: Open-Meteo (CC BY 4.0; verify current data terms before redistribution).
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
DEFAULT_OBS = Path("data/processed/imd_pune_3x3_grid_daily_2021_2025.csv")
DEFAULT_OUT = Path("data/processed/forecast_validation")

# Diagnostic rainfall thresholds, not calibrated flood or official warning thresholds.
RAIN_THRESHOLDS_MM = (20.0, 50.0, 64.5)
TIMEZONE = "Asia/Kolkata"
MODEL = "ecmwf_ifs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", default=str(DEFAULT_OBS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--start-date", default="2024-06-01", help="First target date; default 2024-06-01")
    parser.add_argument("--end-date", default="2025-10-31", help="Last target date; default 2025-10-31")
    parser.add_argument("--all-months", action="store_true", help="Include all target dates between start and end, not just Jun-Oct")
    parser.add_argument("--request-pause", type=float, default=0.25, help="Seconds between API requests")
    parser.add_argument(
        "--retry-unavailable", action="store_true",
        help="Retry dates previously logged as unavailable in the Single Runs archive.",
    )
    parser.add_argument(
        "--retry-incomplete", action="store_true",
        help="Retry dates previously logged with incomplete hourly coverage.",
    )
    return parser.parse_args()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Pune-Flood-EWS-Research-Prototype/0.2 (non-commercial student project)",
        "Accept": "application/json",
    })
    retry = Retry(
        total=5,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def load_observations(path: Path) -> tuple[pd.DataFrame, list[tuple[float, float]]]:
    if not path.exists():
        raise FileNotFoundError(f"Observed rainfall file not found: {path}")
    df = pd.read_csv(path)
    required = {"date", "latitude", "longitude", "rainfall_mm"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Observation file is missing required columns: {sorted(missing)}")

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce").round(4)
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce").round(4)
    df["rainfall_mm"] = pd.to_numeric(df["rainfall_mm"], errors="coerce")
    df = df.dropna(subset=["date", "latitude", "longitude"])
    # Drop duplicate date/cell rows conservatively; duplicates should not be averaged silently.
    duplicates = df.duplicated(["date", "latitude", "longitude"], keep=False)
    if duplicates.any():
        print(f"WARNING: {int(duplicates.sum())} duplicate date/cell rows found; using the first occurrence.")
        df = df.drop_duplicates(["date", "latitude", "longitude"], keep="first")
    coords = sorted({(float(r.latitude), float(r.longitude)) for r in df[["latitude", "longitude"]].itertuples(index=False)})
    if len(coords) != 9:
        raise ValueError(f"Expected the existing 3x3 grid (9 unique coordinates), found {len(coords)}: {coords}")
    return df, coords


def date_range(start: date, end: date, all_months: bool) -> list[date]:
    if end < start:
        raise ValueError("--end-date must be on or after --start-date")
    result = []
    d = start
    while d <= end:
        if all_months or d.month in (6, 7, 8, 9, 10):
            result.append(d)
        d += timedelta(days=1)
    if not result:
        raise ValueError("No target dates remain after applying date and monsoon-month filters.")
    return result


def parse_hourly_sum(hourly: dict[str, Any], target_timestamps: list[str]) -> tuple[float, int]:
    times = hourly.get("time") or []
    values = hourly.get("precipitation") or []
    if len(times) != len(values):
        return math.nan, 0
    wanted = set(target_timestamps)
    selected: dict[str, float] = {}
    for timestamp, value in zip(times, values):
        stamp = str(timestamp)[:16]
        if stamp not in wanted or value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            selected[stamp] = numeric
    count = len(selected)
    # Require all 24 hourly values. Don't quietly interpret missing forecast hours as zero.
    if count != 24 or len(wanted) != 24:
        return math.nan, count
    return max(0.0, sum(selected.values())), count


def extract_daily_windows(location_payload: dict[str, Any], target_day: date) -> tuple[float, int, float, int]:
    hourly = location_payload.get("hourly") or {}

    # Open-Meteo precipitation is an hourly accumulation; timestamp labels the ending hour.
    # Forecast model hours are UTC-aligned. India is UTC+05:30, so their local
    # labels are at :30. Match the actual returned timestamps, not whole-hour
    # labels; previously this mismatch yielded zero matched forecast hours.
    calendar_times = [
        datetime.combine(target_day, datetime.min.time())
        + timedelta(hours=h, minutes=30)
        for h in range(1, 25)
    ]
    calendar_keys = [dt.strftime("%Y-%m-%dT%H:%M") for dt in calendar_times]

    # Approximate the IMD daily observation day ending around 08:30 IST using hourly bins
    # ending 09:00 on D-1 through 09:00 on D (timestamps D-1 09:00 ... D 08:00).
    imd_start = (
        datetime.combine(target_day - timedelta(days=1), datetime.min.time())
        + timedelta(hours=9, minutes=30)
    )
    imd_keys = [(imd_start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24)]

    cal_total, cal_hours = parse_hourly_sum(hourly, calendar_keys)
    imd_total, imd_hours = parse_hourly_sum(hourly, imd_keys)
    return cal_total, cal_hours, imd_total, imd_hours


class ModelRunUnavailable(RuntimeError):
    """The archive explicitly reports that this exact model run is unavailable."""


def request_run(session: requests.Session, coords: list[tuple[float, float]], run_utc: str) -> list[dict[str, Any]]:
    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in coords),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in coords),
        "run": run_utc,
        "models": MODEL,
        "hourly": "precipitation",
        "forecast_days": 5,
        "timezone": TIMEZONE,
        "timeformat": "iso8601",
    }
    response = session.get(API_URL, params=params, timeout=(10, 75))
    response_text = response.text[:700].replace("\n", " ")
    if "requested model run is not available" in response_text.lower():
        raise ModelRunUnavailable(
            f"Open-Meteo archive has no {MODEL} run at {run_utc} "
            f"(HTTP {response.status_code})."
        )
    if not response.ok:
        raise RuntimeError(f"Open-Meteo HTTP {response.status_code} for run={run_utc}: {response_text}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Open-Meteo returned non-JSON content for run={run_utc}: {response_text}"
        ) from exc
    if isinstance(payload, dict) and payload.get("error") is True:
        reason = str(payload.get("reason", ""))
        if "requested model run is not available" in reason.lower():
            raise ModelRunUnavailable(
                f"Open-Meteo archive has no {MODEL} run at {run_utc}: {reason}"
            )
        raise RuntimeError(f"Open-Meteo API error for run={run_utc}: {reason[:500]}")
    if isinstance(payload, list):
        locations = payload
    elif isinstance(payload, dict) and isinstance(payload.get("hourly"), dict):
        locations = [payload]
    else:
        raise RuntimeError(f"Unexpected API response schema for run={run_utc}: {str(payload)[:500]}")
    if len(locations) != len(coords):
        raise RuntimeError(
            f"Expected {len(coords)} location responses for run={run_utc}, received {len(locations)}. "
            "No partial rows were saved for this day."
        )
    return locations


def metric_row(values: pd.DataFrame, forecast_col: str, threshold: float | None, scope: str, label: str) -> dict[str, Any]:
    d = values.copy()
    d["obs"] = pd.to_numeric(d["rainfall_mm"], errors="coerce")
    d["fcst"] = pd.to_numeric(d[forecast_col], errors="coerce")
    d = d.dropna(subset=["obs", "fcst"])
    result: dict[str, Any] = {
        "alignment": label,
        "scope": scope,
        "threshold_mm": threshold,
        "samples": int(len(d)),
        "obs_mean_mm": math.nan,
        "forecast_mean_mm": math.nan,
        "bias_mm": math.nan,
        "mae_mm": math.nan,
        "rmse_mm": math.nan,
        "hits": None,
        "misses": None,
        "false_alarms": None,
        "correct_negatives": None,
        "pod_recall": math.nan,
        "false_alarm_ratio": math.nan,
        "critical_success_index": math.nan,
        "frequency_bias": math.nan,
    }
    if d.empty:
        return result

    error = d["fcst"].to_numpy(dtype=float) - d["obs"].to_numpy(dtype=float)
    result.update({
        "obs_mean_mm": float(d["obs"].mean()),
        "forecast_mean_mm": float(d["fcst"].mean()),
        "bias_mm": float(error.mean()),
        "mae_mm": float(abs(error).mean()),
        "rmse_mm": float(math.sqrt((error ** 2).mean())),
    })
    if threshold is not None:
        obs_event = d["obs"] >= threshold
        fcst_event = d["fcst"] >= threshold
        hits = int((obs_event & fcst_event).sum())
        misses = int((obs_event & ~fcst_event).sum())
        false_alarms = int((~obs_event & fcst_event).sum())
        correct_negatives = int((~obs_event & ~fcst_event).sum())
        result.update({
            "hits": hits,
            "misses": misses,
            "false_alarms": false_alarms,
            "correct_negatives": correct_negatives,
            "pod_recall": hits / (hits + misses) if hits + misses else math.nan,
            "false_alarm_ratio": false_alarms / (hits + false_alarms) if hits + false_alarms else math.nan,
            "critical_success_index": hits / (hits + misses + false_alarms) if hits + misses + false_alarms else math.nan,
            "frequency_bias": (hits + false_alarms) / (hits + misses) if hits + misses else math.nan,
        })
    return result


def calculate_metrics(joined: pd.DataFrame, coords_count: int) -> pd.DataFrame:
    output: list[dict[str, Any]] = []
    for alignment, fc_col in [
        ("calendar_day", "forecast_calendar_day_mm"),
        ("imd_observation_day_approx", "forecast_imd_day_approx_mm"),
    ]:
        valid = joined.dropna(subset=["rainfall_mm", fc_col]).copy()
        output.append(metric_row(valid, fc_col, None, "grid_cell_day", alignment))

        for threshold in RAIN_THRESHOLDS_MM:
            output.append(metric_row(valid, fc_col, threshold, "grid_cell_day", alignment))

        # A city/region day is included only if all nine observed and forecast cells exist.
        per_day = joined.groupby("date", as_index=False).agg(
            observed_cells=("rainfall_mm", "count"),
            forecast_cells=(fc_col, "count"),
            rainfall_mm=("rainfall_mm", "max"),
            **{fc_col: (fc_col, "max")},
        )
        per_day = per_day[
            (per_day["observed_cells"] == coords_count)
            & (per_day["forecast_cells"] == coords_count)
        ].copy()
        output.append(metric_row(per_day, fc_col, None, "region_day_max_of_9_cells", alignment))
        for threshold in RAIN_THRESHOLDS_MM:
            output.append(metric_row(per_day, fc_col, threshold, "region_day_max_of_9_cells", alignment))

    return pd.DataFrame(output)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    obs_path = Path(args.observations).resolve()
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    obs, coords = load_observations(obs_path)
    target_days = date_range(start, end, args.all_months)

    # Restrict run dates to days actually represented in the observation dataset.
    observed_dates = set(obs["date"].dropna().astype(str))
    target_days = [d for d in target_days if d.isoformat() in observed_dates]
    if not target_days:
        raise RuntimeError("No target dates overlap the observed rainfall dataset.")

    cache_path = out_dir / "openmeteo_ifs_daily_forecast_cache.csv"
    paired_path = out_dir / "openmeteo_ifs_paired_observed_forecast.csv"
    metrics_path = out_dir / "openmeteo_ifs_verification_metrics.csv"
    unavailable_path = out_dir / "openmeteo_ifs_unavailable_dates.csv"
    incomplete_path = out_dir / "openmeteo_ifs_incomplete_dates.csv"

    # Resume safely after interruption: a date is complete only if a row for every grid cell exists.
    cache = pd.read_csv(cache_path) if cache_path.exists() else pd.DataFrame()
    expected_coords = {(round(lat, 4), round(lon, 4)) for lat, lon in coords}
    completed: set[str] = set()
    required_cache_cols = {
        "target_date", "grid_latitude", "grid_longitude",
        "forecast_calendar_day_mm", "forecast_calendar_hours",
        "forecast_imd_day_approx_mm", "forecast_imd_day_approx_hours",
    }
    if not cache.empty and required_cache_cols.issubset(cache.columns):
        # A cached date is complete only when every cell has 24 matched values
        # and both totals are non-missing. This intentionally invalidates the
        # previous broken cache whose timestamps matched zero hours.
        cache["grid_latitude"] = pd.to_numeric(cache["grid_latitude"], errors="coerce").round(4)
        cache["grid_longitude"] = pd.to_numeric(cache["grid_longitude"], errors="coerce").round(4)
        cache["forecast_calendar_day_mm"] = pd.to_numeric(cache["forecast_calendar_day_mm"], errors="coerce")
        cache["forecast_calendar_hours"] = pd.to_numeric(cache["forecast_calendar_hours"], errors="coerce")
        cache["forecast_imd_day_approx_mm"] = pd.to_numeric(cache["forecast_imd_day_approx_mm"], errors="coerce")
        cache["forecast_imd_day_approx_hours"] = pd.to_numeric(cache["forecast_imd_day_approx_hours"], errors="coerce")
        for day, group in cache.groupby("target_date"):
            good = group[
                (group["forecast_calendar_hours"] >= 24)
                & (group["forecast_imd_day_approx_hours"] >= 24)
                & group["forecast_calendar_day_mm"].notna()
                & group["forecast_imd_day_approx_mm"].notna()
            ]
            have = set(zip(good["grid_latitude"], good["grid_longitude"]))
            if expected_coords.issubset(have):
                completed.add(str(day)[:10])

    unavailable_df = pd.read_csv(unavailable_path) if unavailable_path.exists() else pd.DataFrame(
        columns=["target_date", "run_utc", "reason", "recorded_at_local"]
    )
    unavailable_dates: set[str] = set()
    if not unavailable_df.empty and "target_date" in unavailable_df.columns:
        unavailable_dates = set(unavailable_df["target_date"].astype(str).str[:10])
    unavailable_dates_to_skip = set() if args.retry_unavailable else set(unavailable_dates)

    incomplete_df = pd.read_csv(incomplete_path) if incomplete_path.exists() else pd.DataFrame(
        columns=["target_date", "run_utc", "reason", "recorded_at_local"]
    )
    incomplete_dates: set[str] = set()
    if not incomplete_df.empty and "target_date" in incomplete_df.columns:
        incomplete_dates = set(incomplete_df["target_date"].astype(str).str[:10])
    incomplete_dates_to_skip = set() if args.retry_incomplete else set(incomplete_dates)

    session = make_session()
    run_count = 0
    new_rows_total = 0
    target_date_strings = {d.isoformat() for d in target_days}
    todo = [
        d for d in target_days
        if d.isoformat() not in completed
        and d.isoformat() not in unavailable_dates_to_skip
        and d.isoformat() not in incomplete_dates_to_skip
    ]
    print("\nPUNE FORECAST VERIFICATION")
    print(f"Observation file: {obs_path}")
    print(f"Grid cells: {len(coords)}")
    print(f"Target dates: {len(target_days)} ({target_days[0]} to {target_days[-1]})")
    print("Months: June-October only unless --all-months is supplied.")
    print("Forecast source: Open-Meteo Single Runs archive, ECMWF IFS HRES 9 km.")
    print("Run selection: 12:00 UTC on target date minus two days.")
    print("Approximate issue lead: 24.5-26.5 hours before target local day begins, assuming usual 4-6h publication delay.")
    print("This evaluates rainfall, not flooding; it is not an exact replay of the production scheduler.")
    print(f"Previously cached dates skipped: {len(completed.intersection(target_date_strings))}")
    print(f"Previously known unavailable dates skipped: {len(unavailable_dates_to_skip.intersection(target_date_strings))}")
    print(f"Previously known incomplete-coverage dates skipped: {len(incomplete_dates_to_skip.intersection(target_date_strings))}")
    print(f"Unavailable-date log: {unavailable_path}")
    print(f"Incomplete-coverage log: {incomplete_path}")

    for i, target_day in enumerate(todo, start=1):
        # 12 UTC run two dates earlier: target day's start is 30.5h after initialization in IST.
        run_day = target_day - timedelta(days=2)
        run_utc = run_day.strftime("%Y-%m-%dT12:00")
        try:
            locations = request_run(session, coords, run_utc)
        except ModelRunUnavailable as exc:
            logging.warning("Skipping target date %s because its requested archive run is unavailable: %s", target_day, exc)
            record = {
                "target_date": target_day.isoformat(),
                "run_utc": run_utc,
                "reason": str(exc),
                "recorded_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            unavailable_df = pd.concat([unavailable_df, pd.DataFrame([record])], ignore_index=True)
            unavailable_df = unavailable_df.drop_duplicates("target_date", keep="last")
            unavailable_df.to_csv(unavailable_path, index=False)
            unavailable_dates.add(target_day.isoformat())
            if i == 1 or i % 10 == 0 or i == len(todo):
                logging.info(
                    "Handled %d/%d target dates (requested run unavailable; %d forecasts fetched so far)",
                    i, len(todo), run_count,
                )
            if args.request_pause > 0 and i < len(todo):
                time.sleep(args.request_pause)
            continue
        except Exception as exc:
            logging.error("Stopped at target date %s, run %s: %s", target_day, run_utc, exc)
            logging.error("Earlier dates remain cached. Rerun the same command to resume after resolving the error.")
            return 2

        # If a previously missing date is now retrievable, remove its stale unavailable marker.
        if target_day.isoformat() in unavailable_dates:
            unavailable_df = unavailable_df[
                unavailable_df["target_date"].astype(str).str[:10] != target_day.isoformat()
            ]
            unavailable_df.to_csv(unavailable_path, index=False)
            unavailable_dates.discard(target_day.isoformat())

        rows = []
        for idx, (lat, lon) in enumerate(coords):
            cal_mm, cal_n, imd_mm, imd_n = extract_daily_windows(locations[idx], target_day)
            rows.append({
                "target_date": target_day.isoformat(),
                "model": "ECMWF IFS HRES 9 km via Open-Meteo Single Runs API",
                "model_run_utc": run_utc,
                "grid_latitude": lat,
                "grid_longitude": lon,
                "forecast_calendar_day_mm": cal_mm,
                "forecast_calendar_hours": cal_n,
                "forecast_imd_day_approx_mm": imd_mm,
                "forecast_imd_day_approx_hours": imd_n,
                "fetched_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
                "api_source": API_URL,
            })

        part = pd.DataFrame(rows)
        calendar_counts = pd.to_numeric(part["forecast_calendar_hours"], errors="coerce").fillna(0)
        imd_counts = pd.to_numeric(part["forecast_imd_day_approx_hours"], errors="coerce").fillna(0)
        valid_totals = part["forecast_calendar_day_mm"].notna() & part["forecast_imd_day_approx_mm"].notna()
        if (calendar_counts < 24).any() or (imd_counts < 24).any() or (~valid_totals).any():
            reason = (
                f"Incomplete hourly coverage: calendar matched={sorted(calendar_counts.astype(int).unique().tolist())}; "
                f"IMD-window matched={sorted(imd_counts.astype(int).unique().tolist())}. "
                "No forecast rows were saved for this date."
            )
            logging.warning("Skipping target date %s, run %s: %s", target_day, run_utc, reason)
            record = {
                "target_date": target_day.isoformat(),
                "run_utc": run_utc,
                "reason": reason,
                "recorded_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            incomplete_df = pd.concat([incomplete_df, pd.DataFrame([record])], ignore_index=True)
            incomplete_df = incomplete_df.drop_duplicates("target_date", keep="last")
            incomplete_df.to_csv(incomplete_path, index=False)
            incomplete_dates.add(target_day.isoformat())
            if args.request_pause > 0 and i < len(todo):
                time.sleep(args.request_pause)
            continue

        # Clear a previous incomplete marker if this date now succeeds (e.g. after --retry-incomplete).
        if target_day.isoformat() in incomplete_dates:
            incomplete_df = incomplete_df[
                incomplete_df["target_date"].astype(str).str[:10] != target_day.isoformat()
            ]
            incomplete_df.to_csv(incomplete_path, index=False)
            incomplete_dates.discard(target_day.isoformat())

        first_write = not cache_path.exists() or cache_path.stat().st_size == 0
        part.to_csv(cache_path, mode="a", header=first_write, index=False)
        new_rows_total += len(rows)
        run_count += 1
        if i == 1 or i % 10 == 0 or i == len(todo):
            logging.info(
                "Handled %d/%d target dates (%d forecasts fetched; latest %s)",
                i, len(todo), run_count, target_day,
            )
        if args.request_pause > 0 and i < len(todo):
            time.sleep(args.request_pause)

    # Reload after fetch so both new and previously cached forecast dates are evaluated.
    cache = pd.read_csv(cache_path)
    cache["target_date"] = pd.to_datetime(cache["target_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    cache["grid_latitude"] = pd.to_numeric(cache["grid_latitude"], errors="coerce").round(4)
    cache["grid_longitude"] = pd.to_numeric(cache["grid_longitude"], errors="coerce").round(4)
    cache = cache.drop_duplicates(["target_date", "grid_latitude", "grid_longitude"], keep="last")

    obs_sub = obs.copy()
    obs_sub["date"] = obs_sub["date"].astype(str)
    obs_sub = obs_sub.rename(columns={"date": "target_date", "latitude": "grid_latitude", "longitude": "grid_longitude"})
    obs_sub["grid_latitude"] = obs_sub["grid_latitude"].round(4)
    obs_sub["grid_longitude"] = obs_sub["grid_longitude"].round(4)
    obs_sub = obs_sub[["target_date", "grid_latitude", "grid_longitude", "rainfall_mm"]]

    paired = cache.merge(
        obs_sub,
        on=["target_date", "grid_latitude", "grid_longitude"],
        how="left",
        validate="one_to_one",
    )
    paired = paired.rename(columns={"target_date": "date"})
    paired.to_csv(paired_path, index=False)

    # Keep metric dates within the requested window only, even if cache has other dates.
    paired_dates = pd.to_datetime(paired["date"], errors="coerce")
    paired = paired[(paired_dates.dt.date >= start) & (paired_dates.dt.date <= end)].copy()
    if not args.all_months:
        paired = paired[pd.to_datetime(paired["date"]).dt.month.isin([6, 7, 8, 9, 10])].copy()
    paired = paired.rename(columns={"date": "target_date"})
    paired["date"] = paired["target_date"]
    metrics = calculate_metrics(paired, len(coords))
    metrics.insert(0, "model", MODEL)
    metrics.insert(1, "target_period", f"{start.isoformat()} to {end.isoformat()}")
    metrics.to_csv(metrics_path, index=False)

    print("\nRESULTS SAVED")
    print(f"Forecast cache: {cache_path}")
    print(f"Paired forecast + observed data: {paired_path}")
    print(f"Verification metrics: {metrics_path}")
    print(f"New forecast dates fetched this run: {run_count}; forecast rows added: {new_rows_total}")
    final_unavailable = sorted(unavailable_dates.intersection(target_date_strings))
    print(f"Dates skipped because the requested model run is unavailable: {len(final_unavailable)}")
    if final_unavailable:
        print("Unavailable dates:", ", ".join(final_unavailable))
    final_incomplete = sorted(incomplete_dates.intersection(target_date_strings))
    print(f"Dates skipped because of incomplete hourly coverage: {len(final_incomplete)}")
    if final_incomplete:
        print("Incomplete-coverage dates:", ", ".join(final_incomplete))
    print(f"Unavailable-date log: {unavailable_path}")
    print(f"Incomplete-coverage log: {incomplete_path}")
    print("\nMETRICS PREVIEW (diagnostic rainfall thresholds, not official warning thresholds)")
    with pd.option_context("display.max_rows", 40, "display.max_columns", 20, "display.width", 220):
        print(metrics.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("\nInterpretation limits:")
    print("- These are rain-threshold detection metrics against the nine daily IMD grid cells, not flood-event accuracy.")
    print("- The nine cells are spatially correlated and must not be treated as independent samples.")
    print("- Both calendar-day and approximate IMD observation-day alignment are reported because daily accumulation conventions may differ.")
    print("- The ECMWF single-run archive is a reproducible forecast baseline, but it is not an exact replay of the live best-match model or alert scheduler.")
    print("- Missing forecast hours remain missing; they are never silently counted as zero rainfall.")
    print("Source files and the live app were not modified. Only the forecast-validation output files were created/updated.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Completed dates remain in the forecast cache; rerun the same command to resume.")
        sys.exit(130)
