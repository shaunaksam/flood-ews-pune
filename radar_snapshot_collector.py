#!/usr/bin/env python3
"""Read-only collector for public IMD radar mosaics.

This records fetch timestamps, HTTP metadata and image hashes. It deliberately
DOES NOT treat HTTP Last-Modified as an observation timestamp, infer rainfall,
estimate storm motion, modify the Flood EWS API, or send alerts.

Requirements:
    python -m pip install requests pillow

Examples:
    python radar_snapshot_collector.py --once
    python radar_snapshot_collector.py --samples 7 --interval-seconds 300
    python radar_snapshot_collector.py --interval-seconds 300
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageFile
from io import BytesIO

ImageFile.LOAD_TRUNCATED_IMAGES = False
IST = ZoneInfo("Asia/Kolkata")
SOURCES = {
    "CMAX": "https://mausam.imd.gov.in/Radar/cmax_mosaic.gif",
    "RainfallRate": "https://mausam.imd.gov.in/Radar/rr_mosaic.gif",
}
FIELDS = [
    "sample_number", "source", "fetched_at_ist", "url", "http_status",
    "content_type", "last_modified", "etag", "cache_control", "bytes",
    "width", "height", "file_sha256", "map_pixels_sha256",
    "file_changed_since_previous", "map_changed_since_previous",
    "map_unchanged_note", "image_path", "observation_time_verified", "error",
]


def now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_image(payload: bytes) -> tuple[int, int, str]:
    """Return dimensions and hash of map pixels with bottom 60 px excluded.

    The crop excludes the common timestamp/legend strip where possible; the
    remaining pixels may still include map labels and overlays, not only echoes.
    """
    with Image.open(BytesIO(payload)) as opened:
        opened.seek(0)
        rgb = opened.convert("RGB")
        width, height = rgb.size
        map_body = rgb.crop((0, 0, width, max(1, height - min(60, height - 1))))
        digest = sha256(map_body.tobytes())
    return width, height, digest


def run(args: argparse.Namespace) -> int:
    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    images_dir = root / "images"
    images_dir.mkdir(exist_ok=True)
    manifest = root / "radar_manifest.csv"
    new_manifest = not manifest.exists() or manifest.stat().st_size == 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": "PuneFloodEWS-ResearchPrototype/0.1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })

    previous_file_hash: dict[str, str] = {}
    previous_map_hash: dict[str, str] = {}
    saved_map_images: dict[str, dict[str, Path]] = {name: {} for name in SOURCES}
    sample_number = 0

    with manifest.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_manifest:
            writer.writeheader()
            handle.flush()

        try:
            while True:
                sample_number += 1
                loop_started = time.monotonic()
                fetched_at = now_ist()
                logging.info("Sample %s fetched at %s", sample_number, fetched_at)

                for name, url in SOURCES.items():
                    row: dict[str, Any] = {
                        "sample_number": sample_number,
                        "source": name,
                        "fetched_at_ist": fetched_at,
                        "url": url,
                        "http_status": "",
                        "content_type": "",
                        "last_modified": "",
                        "etag": "",
                        "cache_control": "",
                        "bytes": "",
                        "width": "",
                        "height": "",
                        "file_sha256": "",
                        "map_pixels_sha256": "",
                        "file_changed_since_previous": "",
                        "map_changed_since_previous": "",
                        "map_unchanged_note": "",
                        "image_path": "",
                        "observation_time_verified": False,
                        "error": "",
                    }
                    try:
                        response = session.get(url, timeout=(5, 20))
                        row["http_status"] = response.status_code
                        row["content_type"] = response.headers.get("Content-Type", "")
                        row["last_modified"] = response.headers.get("Last-Modified", "")
                        row["etag"] = response.headers.get("ETag", "")
                        row["cache_control"] = response.headers.get("Cache-Control", "")
                        response.raise_for_status()

                        payload = response.content
                        file_hash = sha256(payload)
                        width, height, map_hash = parse_image(payload)
                        row.update({
                            "bytes": len(payload),
                            "width": width,
                            "height": height,
                            "file_sha256": file_hash,
                            "map_pixels_sha256": map_hash,
                        })

                        if name in previous_file_hash:
                            row["file_changed_since_previous"] = (
                                file_hash != previous_file_hash[name]
                            )
                            row["map_changed_since_previous"] = (
                                map_hash != previous_map_hash[name]
                            )
                        previous_file_hash[name] = file_hash
                        previous_map_hash[name] = map_hash

                        # Save a unique map-pixel state. Manifest still records every poll.
                        if map_hash not in saved_map_images[name]:
                            safe_stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
                            path = images_dir / f"{name}_{safe_stamp}_{map_hash[:12]}.gif"
                            path.write_bytes(payload)
                            saved_map_images[name][map_hash] = path
                        row["image_path"] = str(saved_map_images[name][map_hash])
                        row["map_unchanged_note"] = (
                            "Pixel hash unchanged from earlier sample; Last-Modified alone is not proof of new observations."
                            if row["map_changed_since_previous"] is False
                            else "Pixel hash indicates an image-body change, not necessarily a rainfall-echo change."
                            if row["map_changed_since_previous"] is True
                            else "First sample; no previous image for comparison."
                        )
                        logging.info(
                            "%s HTTP %s | Last-Modified=%s | map_changed=%s",
                            name, response.status_code, row["last_modified"],
                            row["map_changed_since_previous"],
                        )
                    except (requests.RequestException, OSError, ValueError) as exc:
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        logging.warning("%s collection failed: %s", name, row["error"])

                    writer.writerow(row)
                    handle.flush()

                if args.once or (args.samples > 0 and sample_number >= args.samples):
                    break

                wait_seconds = max(0.0, args.interval_seconds - (time.monotonic() - loop_started))
                logging.info("Next sample in %.1f seconds", wait_seconds)
                time.sleep(wait_seconds)
        except KeyboardInterrupt:
            logging.info("Stopped by user; saved manifest remains available.")
        finally:
            session.close()

    print(f"Collector output: {root}")
    print(f"Manifest: {manifest}")
    print("No app files, API routes, database, Redis, or alert settings were changed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="radar_archive", help="Directory for images and CSV manifest")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Polling interval; minimum 60 seconds")
    parser.add_argument("--samples", type=int, default=0, help="Number of samples; 0 means run until Ctrl+C")
    parser.add_argument("--once", action="store_true", help="Capture one sample and exit")
    args = parser.parse_args()
    if args.interval_seconds < 60:
        parser.error("--interval-seconds must be at least 60 seconds")
    if args.samples < 0:
        parser.error("--samples cannot be negative")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
