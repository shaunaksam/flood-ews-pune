"""Experimental Himawari-9 Band 13 cloud-top monitoring endpoint.

This module deliberately uses Satpy's AHI HSD reader for HSD decoding,
brightness-temperature calibration, and geostationary navigation. It does
not infer storm motion or issue official warnings.
"""

import bz2
import logging
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from satpy import Scene


router = APIRouter()

BUCKET = "noaa-himawari9"
BAND_PREFIX = "AHI-L1b-FLDK"
CACHE_TTL_SECONDS = 600
SLOT_INTERVAL_MINUTES = 10
MAX_AGE_MINUTES = 90
EXPECTED_SEGMENTS = 10

# Bounding boxes are (minimum, maximum) in degrees.
PUNE_LON = (73.5, 74.3)
PUNE_LAT = (18.2, 18.8)

# This broader regional box currently includes Pune. It is NOT an independent
# upstream/lead-time area and must not be interpreted as a storm trajectory.
UPSTREAM_LON = (73.0, 74.5)
UPSTREAM_LAT = (17.5, 19.2)

COLD_THRESH_K = 243.0
WARN_FRAC = 0.03

logger = logging.getLogger("flood_ews.himawari")

_cache = {"t": 0.0, "v": None}
_s3 = None
_SEGMENT_RE = re.compile(r"_S(?P<segment>\d{2})(?P<total>\d{2})\.DAT\.bz2$", re.IGNORECASE)


def _client():
    """Create the anonymous NOAA public-bucket S3 client lazily."""
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name="us-east-1",
            config=Config(signature_version=UNSIGNED),
        )
    return _s3


def _list_band13(slot: datetime) -> list[str]:
    """Return all Band 13 full-disk segment keys for one 10-minute slot."""
    prefix = slot.strftime(f"{BAND_PREFIX}/%Y/%m/%d/%H%M/")
    paginator = _client().get_paginator("list_objects_v2")
    keys = []

    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if "_B13_FLDK_R20_" in key and key.upper().endswith(".DAT.BZ2"):
                keys.append(key)

    # Order segments north-to-south according to their segment number.
    def segment_number(key: str) -> int:
        match = _SEGMENT_RE.search(key)
        return int(match.group("segment")) if match else 999

    return sorted(keys, key=segment_number)


def _latest_slot(max_age_min: int = MAX_AGE_MINUTES):
    """Find the newest slot with a complete set of ten B13 full-disk segments."""
    now = datetime.now(timezone.utc)
    rounded_now = now.replace(
        minute=(now.minute // SLOT_INTERVAL_MINUTES) * SLOT_INTERVAL_MINUTES,
        second=0,
        microsecond=0,
    )

    for delta in range(0, max_age_min + 1, SLOT_INTERVAL_MINUTES):
        slot = rounded_now - timedelta(minutes=delta)
        files = _list_band13(slot)
        logger.info(
            "Checking %s | Band 13 full-disk segments: %d",
            slot.isoformat(),
            len(files),
        )

        if len(files) == EXPECTED_SEGMENTS:
            return slot, files

        if files:
            logger.warning(
                "Ignoring incomplete Himawari slot %s: found %d/%d segments",
                slot.isoformat(),
                len(files),
                EXPECTED_SEGMENTS,
            )

    return None, []


def _download_segment_to_file(key: str, work_dir: Path) -> Path:
    """Download and decompress one bz2 HSD object to a temporary .DAT file."""
    filename = Path(key).name
    if not filename.lower().endswith(".dat.bz2"):
        raise ValueError(f"Unexpected Himawari object name: {filename}")

    output_path = work_dir / filename[:-4]  # remove only the trailing '.bz2'
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    response = _client().get_object(Bucket=BUCKET, Key=key)
    body = response["Body"]
    try:
        compressed = body.read()
    finally:
        body.close()

    raw = bz2.decompress(compressed)
    if not raw:
        raise ValueError(f"Downloaded empty HSD segment: {key}")

    output_path.write_bytes(raw)
    logger.info("Downloaded %s (%d decompressed bytes)", filename, len(raw))
    return output_path


def _finite_stats(data: np.ndarray, label: str) -> dict:
    """Compute cold-pixel fraction and minimum from valid BT pixels only."""
    values = np.asarray(data, dtype=np.float32)
    valid = values[np.isfinite(values)]

    if valid.size == 0:
        raise ValueError(f"No valid brightness-temperature pixels in {label} crop")

    return {
        "valid_pixels": int(valid.size),
        "cold_fraction": float(np.mean(valid < COLD_THRESH_K)),
        "minimum_k": float(np.min(valid)),
    }


def _analyse(slot: datetime, files: list[str]) -> dict:
    """Read complete HSD segments with Satpy and analyse two geographic crops."""
    if len(files) != EXPECTED_SEGMENTS:
        raise ValueError(
            f"Expected {EXPECTED_SEGMENTS} full-disk segments, received {len(files)}"
        )

    # TemporaryDirectory is intentionally used instead of writing under /app.
    # With Docker's read-only root filesystem, /tmp must be mounted as tmpfs.
    with tempfile.TemporaryDirectory(prefix="himawari_", dir="/tmp") as temp_dir:
        work_dir = Path(temp_dir)
        local_files = [
            str(_download_segment_to_file(key, work_dir))
            for key in files
        ]

        logger.info("Opening %d HSD segments using Satpy ahi_hsd", len(local_files))
        scene = Scene(filenames=local_files, reader="ahi_hsd")
        scene.load(["B13"], calibration="brightness_temperature")

        if "B13" not in scene:
            raise RuntimeError("Satpy did not load the B13 dataset")

        b13 = scene["B13"]
        calibration = b13.attrs.get("calibration")
        units = str(b13.attrs.get("units", "")).strip().lower()
        logger.info(
            "Satpy B13 loaded | calibration=%s | units=%s | dims=%s | shape=%s",
            calibration,
            units,
            b13.dims,
            b13.shape,
        )

        if calibration != "brightness_temperature":
            raise RuntimeError(
                f"Expected brightness_temperature calibration, got {calibration!r}"
            )
        if units not in {"k", "kelvin"}:
            raise RuntimeError(f"Expected Kelvin units, got {units!r}")

        # Satpy uses the projection and navigation parameters stored in HSD
        # headers to translate this longitude/latitude box to image pixels.
        regional_scene = scene.crop(
            ll_bbox=(
                UPSTREAM_LON[0],
                UPSTREAM_LAT[0],
                UPSTREAM_LON[1],
                UPSTREAM_LAT[1],
            )
        )
        pune_scene = scene.crop(
            ll_bbox=(
                PUNE_LON[0],
                PUNE_LAT[0],
                PUNE_LON[1],
                PUNE_LAT[1],
            )
        )

        if "B13" not in regional_scene or "B13" not in pune_scene:
            raise RuntimeError("Satpy returned an empty geographic crop")

        # .values computes the lazy dask-backed data before temporary files vanish.
        regional_data = np.asarray(regional_scene["B13"].compute().values)
        pune_data = np.asarray(pune_scene["B13"].compute().values)

    regional_stats = _finite_stats(regional_data, "regional")
    pune_stats = _finite_stats(pune_data, "Pune")

    regional_watch = regional_stats["cold_fraction"] >= WARN_FRAC
    pune_active = pune_stats["cold_fraction"] >= WARN_FRAC

    # A single image cannot show that a storm is moving toward Pune.
    # Use WATCH instead of the unsupported APPROACHING label.
    if pune_active:
        level = "ACTIVE"
    elif regional_watch:
        level = "WATCH"
    else:
        level = "CLEAR"

    return {
        "slot_utc": slot.strftime("%Y-%m-%dT%H:%M"),
        "slot_ist": (slot + timedelta(hours=5, minutes=30)).strftime(
            "%Y-%m-%d %H:%M IST"
        ),
        "level": level,
        "watch": regional_watch,
        "upstream_cold_frac": round(regional_stats["cold_fraction"], 4),
        "pune_cold_frac": round(pune_stats["cold_fraction"], 4),
        "upstream_min_bt_K": round(regional_stats["minimum_k"], 1),
        "pune_min_bt_K": round(pune_stats["minimum_k"], 1),
        "upstream_valid_pixels": regional_stats["valid_pixels"],
        "pune_valid_pixels": pune_stats["valid_pixels"],
        "threshold_K": COLD_THRESH_K,
        "threshold_frac": WARN_FRAC,
        "note": (
            "Experimental Himawari-9 Band 13 brightness-temperature snapshot. "
            "The broader regional box overlaps Pune; this does not infer storm "
            "movement, arrival time, rainfall amount, or flood risk. Not an official warning."
        ),
    }


def _unavailable(message: str) -> dict:
    return {"available": False, "reason": message}


@router.get("/storm/watch")
def storm_watch():
    now = time.monotonic()
    cached = _cache["v"]
    if cached is not None and now - _cache["t"] < CACHE_TTL_SECONDS:
        return JSONResponse(content=cached, headers={"Cache-Control": "no-store"})

    try:
        slot, files = _latest_slot()
        if slot is None or not files:
            result = _unavailable("No complete recent Himawari Band 13 slot found.")
        else:
            analysis = _analyse(slot, files)
            result = {"available": True, **analysis}

        _cache["t"] = now
        _cache["v"] = result
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    except Exception as exc:
        logger.exception("Himawari processing failed: %s", exc)
        result = _unavailable(f"Satellite processing failed: {type(exc).__name__}")
        # Do not cache unexpected failures; allow retry after code/data recovery.
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
