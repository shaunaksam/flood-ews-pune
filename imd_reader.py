import os
import re
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup

URL = "https://www.imdpune.gov.in/"
SOURCE = "IMD Pune website (automatic)"
IST = timezone(timedelta(hours=5, minutes=30))
TEMP = r"(-?\d+(?:\.\d+)?)\s*(?:o|\u00b0|\u00ba)?\s*C"


def parse_homepage(html):
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())
    d = re.search(r"Updated on:?\s*(\d{1,2})-(\d{1,2})-(\d{4})", text)
    if not d:
        return None
    day, month, year = int(d.group(1)), int(d.group(2)), int(d.group(3))
    m = re.search(rf"\bPune\s+{TEMP}\s+{TEMP}\s+(\d+(?:\.\d+)?|TRACE|NIL)\s*mm", text[d.end():], flags=re.I)
    if not m:
        return None
    raw = m.group(3).upper()
    rain = 0.0 if raw in ("TRACE", "NIL") else float(raw)
    if not 0 <= rain <= 600:
        return None
    try:
        updated = datetime(year, month, day).date()
    except ValueError:
        return None
    return {"updated_on": updated, "tmax": float(m.group(1)), "tmin": float(m.group(2)), "rain_mm": rain}


def fetch_homepage_reading():
    contact = os.getenv("IMD_CONTACT_EMAIL", "student project")
    headers = {"User-Agent": f"PuneRainSafety-academic-prototype ({contact})"}
    r = requests.get(URL, headers=headers, timeout=25)
    r.raise_for_status()
    out = parse_homepage(r.text)
    if out is None:
        print("IMD homepage: could not find the Pune rainfall line; the page layout may have changed.")
        return None
    today = datetime.now(IST).date()
    if not (today - timedelta(days=2) <= out["updated_on"] <= today):
        print(f"IMD homepage: reading dated {out['updated_on']} is not recent; ignoring it.")
        return None
    return out
