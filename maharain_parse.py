import re
from pathlib import Path
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "data" / "raw" / "maharain"
OUT = ROOT / "data" / "processed" / "maharain_circle_daily.csv"
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], start=1)}


def num(x):
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "--", "NA", "N/A", "NR", "nr", "nan"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_table(tbl):
    text = tbl.get_text(" ", strip=True)
    m = re.search(r"month of\s+([A-Za-z]+)\s*[-/ ]\s*(\d{4})", text, flags=re.I)
    if not m or m.group(1).lower() not in MONTHS:
        return None
    month, year = MONTHS[m.group(1).lower()], int(m.group(2))
    rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in tbl.find_all("tr")]
    hdr_i = next((i for i, r in enumerate(rows) if "Circle" in r and sum(1 for c in r if re.fullmatch(r"\d{1,2}", c)) >= 28), None)
    if hdr_i is None:
        return None
    hdr = rows[hdr_i]
    ci = hdr.index("Circle")
    ti = next((i for i, c in enumerate(hdr) if c.lower() == "taluka"), None)
    di = next((i for i, c in enumerate(hdr) if c.lower() == "district"), None)
    ni = next((i for i, c in enumerate(hdr) if "normal" in c.lower()), None)
    day_cols = {int(c): i for i, c in enumerate(hdr) if re.fullmatch(r"\d{1,2}", c)}
    out, district, taluka = [], None, None
    for r in rows[hdr_i + 1:]:
        if len(r) < len(hdr) - 3 or ci >= len(r) or not r[ci]:
            continue
        if di is not None and di < len(r) and r[di]:
            district = r[di]
        if ti is not None and ti < len(r) and r[ti]:
            taluka = r[ti]
        normal = num(r[ni]) if ni is not None and ni < len(r) else np.nan
        for d, i in day_cols.items():
            if i >= len(r):
                continue
            try:
                dt = pd.Timestamp(year=year, month=month, day=d)
            except ValueError:
                continue
            out.append({"date": dt, "district": district, "taluka": taluka, "circle": r[ci], "taluka_normal_month_mm": normal, "rain_mm": num(r[i])})
    return pd.DataFrame(out)


def main():
    files = sorted(list(SRC.glob("*.html")) + list(SRC.glob("*.htm")))
    if not files:
        print(f"No saved pages found in {SRC}. Save each month's results page there as HTML.")
        return
    frames = []
    for f in files:
        soup = BeautifulSoup(f.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        got = 0
        for tbl in soup.find_all("table"):
            df = parse_table(tbl)
            if df is not None and len(df):
                frames.append(df)
                got += 1
        print(f"{f.name}: {got} table(s) read")
    if not frames:
        print("No daily circle tables were found. If the table is missing from the saved file, copy the table's outerHTML from the browser's Elements panel into a .html file.")
        return
    all_df = pd.concat(frames, ignore_index=True).drop_duplicates(["date", "district", "circle"], keep="last").sort_values(["district", "circle", "date"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    all_df.to_csv(OUT, index=False)
    print(f"\nRows: {len(all_df):,}  circles: {all_df.groupby(['district', 'circle']).ngroups}  days: {all_df['date'].nunique()}  ({all_df['date'].min().date()} to {all_df['date'].max().date()})")
    print("Districts:", ", ".join(sorted(all_df["district"].dropna().unique())))
    city = all_df[all_df["taluka"].astype(str).str.contains("Pune city", case=False)]
    if len(city):
        print("Pune city circles:", ", ".join(sorted(city["circle"].unique())))
        top = city.groupby("date")["rain_mm"].mean().sort_values(ascending=False).head(8)
        print("\nWettest days (average of Pune city circles):")
        print(top.round(1).to_string())
    print(f"\nSaved: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
