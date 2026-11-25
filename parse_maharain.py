import re
from pathlib import Path

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "data" / "raw" / "maharain"
OUT_DIR = ROOT / "data" / "processed"

DAILY_OUT = OUT_DIR / "maharain_circle_daily.csv"
MONTHLY_OUT = OUT_DIR / "maharain_circle_monthly_summary.csv"
PERIOD_OUT = OUT_DIR / "maharain_circle_period_summary.csv"
CURRENT_OUT = OUT_DIR / "maharain_taluka_current_summary.csv"

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
MONTH_PATTERN = (
    r"january|february|march|april|may|june|july|august|"
    r"september|october|november|december"
)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def num(value):
    s = clean_text(value).replace(",", "")
    if s.lower() in ("", "-", "--", "na", "n/a", "nr", "nan", "none"):
        return np.nan
    # Do not treat symbols or labels as numbers.
    try:
        return float(s)
    except (TypeError, ValueError):
        return np.nan


def direct_table_rows(table):
    """Return rows belonging to this table, excluding any nested tables."""
    result = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all(["th", "td"], recursive=False)
        if cells:
            result.append(cells)
    return result


def expand_table(table):
    """Expand rowspan/colspan into a rectangular text grid."""
    raw_rows = direct_table_rows(table)
    grid = []
    active_spans = {}

    for cells in raw_rows:
        row_map = {}
        next_spans = {}

        # Carry cells down from prior rowspan cells.
        for col, (value, remaining) in active_spans.items():
            row_map[col] = value
            if remaining > 1:
                next_spans[col] = (value, remaining - 1)

        col = 0
        for cell in cells:
            while col in row_map:
                col += 1

            value = clean_text(cell.get_text(" ", strip=True))
            try:
                rowspan = max(1, int(cell.get("rowspan", 1) or 1))
            except (TypeError, ValueError):
                rowspan = 1
            try:
                colspan = max(1, int(cell.get("colspan", 1) or 1))
            except (TypeError, ValueError):
                colspan = 1

            for offset in range(colspan):
                target_col = col + offset
                row_map[target_col] = value
                if rowspan > 1:
                    next_spans[target_col] = (value, rowspan - 1)
            col += colspan

        active_spans = next_spans
        if row_map:
            width = max(row_map) + 1
            grid.append([row_map.get(i, "") for i in range(width)])
        else:
            grid.append([])

    width = max((len(row) for row in grid), default=0)
    return [row + [""] * (width - len(row)) for row in grid]


def parse_table(tbl):
    """Existing daily circle-wise parser. Kept compatible with the old script."""
    text = tbl.get_text(" ", strip=True)
    m = re.search(r"month of\s+([A-Za-z]+)\s*[-/ ]\s*(\d{4})", text, flags=re.I)
    if not m or m.group(1).lower() not in MONTHS:
        return None

    month, year = MONTHS[m.group(1).lower()], int(m.group(2))
    rows = [
        [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        for tr in tbl.find_all("tr")
        if tr.find_parent("table") is tbl
    ]

    hdr_i = next(
        (
            i for i, r in enumerate(rows)
            if "Circle" in r and sum(1 for c in r if re.fullmatch(r"\d{1,2}", c)) >= 28
        ),
        None,
    )
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

        for day, i in day_cols.items():
            if i >= len(r):
                continue
            try:
                dt = pd.Timestamp(year=year, month=month, day=day)
            except ValueError:
                continue
            out.append(
                {
                    "date": dt,
                    "district": district,
                    "taluka": taluka,
                    "circle": r[ci],
                    "taluka_normal_month_mm": normal,
                    "rain_mm": num(r[i]),
                }
            )

    return pd.DataFrame(out) if out else None


def metric_name(value):
    s = clean_text(value).lower().replace("%", "percent")
    if "normal" in s and "actual" not in s and "to normal" not in s:
        return "normal_rainfall_mm"
    if "actual" in s:
        return "actual_rainfall_mm"
    if "to normal" in s or "percent to normal" in s or "percent" in s:
        return "percent_to_normal"
    return None


def parse_month_year(value):
    m = re.search(rf"\b({MONTH_PATTERN})\s*,?\s*(20\d{{2}})\b", clean_text(value), re.I)
    if not m:
        return None
    return MONTHS[m.group(1).lower()], int(m.group(2)), m.group(1).title()


def parse_monthly_summary_table(table, source_file):
    """Parse circle-wise month totals and the cumulative June-to-date columns."""
    grid = expand_table(table)
    if len(grid) < 3:
        return None, None

    header_i = None
    circle_i = None
    for i, row in enumerate(grid[:-1]):
        circle_candidates = [
            j for j, value in enumerate(row)
            if clean_text(value).lower() == "circle"
        ]
        has_month = any(parse_month_year(value) for value in row)
        if circle_candidates and has_month:
            header_i = i
            circle_i = circle_candidates[0]
            break

    if header_i is None:
        return None, None

    # Find the subheader row immediately below (or shortly after) the month headings.
    sub_i = None
    for i in range(header_i + 1, min(header_i + 4, len(grid))):
        joined = " ".join(grid[i]).lower()
        if "normal" in joined and "actual" in joined and ("to normal" in joined or "percent" in joined):
            sub_i = i
            break
    if sub_i is None:
        return None, None

    header = grid[header_i]
    subheader = grid[sub_i]
    month_columns = {}
    period_columns = {}
    period_label = None

    period_re = re.compile(
        rf"1st\s+({MONTH_PATTERN})\s+to\s*(\d{{1,2}})\s*({MONTH_PATTERN})",
        re.I,
    )

    for col in range(circle_i + 1, max(len(header), len(subheader))):
        group_label = header[col] if col < len(header) else ""
        sub_label = subheader[col] if col < len(subheader) else ""
        metric = metric_name(sub_label)
        if not metric:
            continue

        parsed_month = parse_month_year(group_label)
        if parsed_month:
            month_num, year, month_name = parsed_month
            key = (year, month_num, month_name)
            month_columns.setdefault(key, {})[metric] = col
            continue

        pm = period_re.search(group_label)
        if pm:
            start_month_name = pm.group(1).title()
            end_day = int(pm.group(2))
            end_month_name = pm.group(3).title()
            period_label = clean_text(group_label)
            period_columns[metric] = (col, start_month_name, end_day, end_month_name)

    if not month_columns and not period_columns:
        return None, None

    monthly_records = []
    period_records = []
    for row in grid[sub_i + 1:]:
        if circle_i >= len(row):
            continue
        circle = clean_text(row[circle_i])
        if not circle or circle.lower() in {"circle", "total", "district", "pune"}:
            continue

        # Ignore non-data rows (e.g. repeated headers or separators).
        has_any_number = any(not pd.isna(num(v)) for v in row)
        if not has_any_number:
            continue

        for (year, month_num, month_name), columns in month_columns.items():
            values = {
                field: num(row[col]) if col < len(row) else np.nan
                for field, col in columns.items()
            }
            if not any(not pd.isna(v) for v in values.values()):
                continue
            monthly_records.append(
                {
                    "date_month": pd.Timestamp(year=year, month=month_num, day=1),
                    "year": year,
                    "month": month_name,
                    "circle": circle,
                    "normal_rainfall_mm": values.get("normal_rainfall_mm", np.nan),
                    "actual_rainfall_mm": values.get("actual_rainfall_mm", np.nan),
                    "percent_to_normal": values.get("percent_to_normal", np.nan),
                    "source_file": source_file,
                }
            )

        if period_columns:
            values = {}
            start_month_name = end_month_name = None
            end_day = None
            for field, info in period_columns.items():
                col, start_name, day, end_name = info
                values[field] = num(row[col]) if col < len(row) else np.nan
                start_month_name, end_day, end_month_name = start_name, day, end_name

            # Infer year from the monthly column headings.
            years = [key[0] for key in month_columns]
            year = years[0] if years else None
            if year and start_month_name and end_month_name:
                start_date = pd.Timestamp(year=year, month=MONTHS[start_month_name.lower()], day=1)
                end_date = pd.Timestamp(year=year, month=MONTHS[end_month_name.lower()], day=end_day)
                if any(not pd.isna(v) for v in values.values()):
                    period_records.append(
                        {
                            "period_label": period_label,
                            "period_start_date": start_date,
                            "period_end_date": end_date,
                            "circle": circle,
                            "normal_rainfall_mm": values.get("normal_rainfall_mm", np.nan),
                            "actual_rainfall_mm": values.get("actual_rainfall_mm", np.nan),
                            "percent_to_normal": values.get("percent_to_normal", np.nan),
                            "source_file": source_file,
                        }
                    )

    monthly_df = pd.DataFrame(monthly_records) if monthly_records else None
    period_df = pd.DataFrame(period_records) if period_records else None
    return monthly_df, period_df


def parse_report_timestamp(grid, table):
    text = " ".join(" ".join(row) for row in grid[:4]) + " " + table.get_text(" ", strip=True)[:300]
    match = re.search(
        r"(20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}\s*(?:am|pm)?)",
        text,
        re.I,
    )
    return match.group(1) if match else None


def parse_current_summary_table(table, source_file):
    """Parse the June-October / November / June-onwards current summary report."""
    grid = expand_table(table)
    if len(grid) < 3:
        return None

    header_i = None
    sub_i = None
    for i, row in enumerate(grid):
        joined = " ".join(row).lower().replace(" ", "")
        if "circle/taluka" not in joined:
            continue
        if "june" not in joined or "november" not in joined or "onwards" not in joined:
            continue
        header_i = i
        for j in range(i + 1, min(i + 4, len(grid))):
            sub_joined = " ".join(grid[j]).lower()
            if "till previous day" in sub_joined and "progressive total" in sub_joined:
                sub_i = j
                break
        break

    if header_i is None or sub_i is None:
        return None

    subheader = grid[sub_i]
    # Match the eleven numeric fields by their subheader positions.
    expected = [
        ("taluka normal rainfall", "june_october_normal_mm"),
        ("actual rainfall", "june_october_actual_mm"),
        ("% to normal", "june_october_percent_to_normal"),
        ("taluka normal rainfall", "november_normal_mm"),
        ("till previous day", "november_till_previous_day_mm"),
        ("on day of report", "november_on_report_day_mm"),
        ("progressive total", "november_progressive_total_mm"),
        ("% to normal", "november_percent_to_normal"),
        ("normal rainfall", "june_onwards_normal_mm"),
        ("actual rainfall", "june_onwards_actual_mm"),
        ("% to normal", "june_onwards_percent_to_normal"),
    ]

    # Prefer positional mapping: the report has 11 numeric subcolumns in this order.
    metric_indices = []
    for col, value in enumerate(subheader):
        s = clean_text(value).lower()
        if any(token in s for token in [
            "normal rainfall", "actual rainfall", "% to normal",
            "till previous day", "on day of report", "progressive total",
        ]):
            metric_indices.append(col)

    if len(metric_indices) != 11:
        return None

    timestamp = parse_report_timestamp(grid, table)
    records = []
    first_metric_col = min(metric_indices)

    for row in grid[sub_i + 1:]:
        labels = [clean_text(v) for v in row[:first_metric_col] if clean_text(v)]
        if not labels:
            continue
        # Skip repeated headers/footers.
        if any("circle / taluka" in label.lower() for label in labels):
            continue

        values = [num(row[col]) if col < len(row) else np.nan for col in metric_indices]
        if sum(not pd.isna(v) for v in values) < 8:
            continue

        if len(labels) == 1:
            parent_area = None
            area_name = labels[0]
        else:
            parent_area = labels[-2]
            area_name = labels[-1]

        record = {
            "report_timestamp": timestamp,
            "parent_area": parent_area,
            "area_name": area_name,
            "source_file": source_file,
        }
        record.update({field_name: value for (_, field_name), value in zip(expected, values)})
        records.append(record)

    return pd.DataFrame(records) if records else None


def save_frames(frames, path, sort_by=None):
    if not frames:
        print(f"No records to save for {path.name}")
        return None
    df = pd.concat(frames, ignore_index=True)
    if sort_by:
        available = [col for col in sort_by if col in df.columns]
        if available:
            df = df.sort_values(available)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"Saved {len(df):,} rows: {path.relative_to(ROOT)}")
    return df


def main():
    files = sorted(list(SRC.glob("*.html")) + list(SRC.glob("*.htm")))
    if not files:
        print(f"No saved pages found in {SRC}. Save each report page there as HTML.")
        return

    daily_frames = []
    monthly_frames = []
    period_frames = []
    current_frames = []

    for file_path in files:
        soup = BeautifulSoup(
            file_path.read_text(encoding="utf-8", errors="ignore"),
            "html.parser",
        )
        daily_count = monthly_count = period_count = current_count = 0

        for table in soup.find_all("table"):
            if table.find_parent("table") is not None:
                continue

            daily_df = parse_table(table)
            if daily_df is not None and not daily_df.empty:
                daily_frames.append(daily_df)
                daily_count += 1
                continue

            monthly_df, period_df = parse_monthly_summary_table(table, file_path.name)
            if monthly_df is not None and not monthly_df.empty:
                monthly_frames.append(monthly_df)
                monthly_count += 1
            if period_df is not None and not period_df.empty:
                period_frames.append(period_df)
                period_count += 1

            current_df = parse_current_summary_table(table, file_path.name)
            if current_df is not None and not current_df.empty:
                current_frames.append(current_df)
                current_count += 1

        print(
            f"{file_path.name}: daily={daily_count}, monthly={monthly_count}, "
            f"period={period_count}, current-summary={current_count}"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Preserve the existing daily output and its original columns/logic.
    daily_df = save_frames(
        daily_frames,
        DAILY_OUT,
        sort_by=["district", "taluka", "circle", "date"],
    )
    if daily_df is not None:
        # A circle name is not globally unique: Pune has a "Pargaon" entry
        # under both Ambegaon ghodegaon and Daund. Keep taluka in the key.
        daily_keys = ["date", "district", "taluka", "circle"]

        duplicate_mask = daily_df.duplicated(daily_keys, keep=False)
        if duplicate_mask.any():
            audit_path = OUT_DIR / "maharain_daily_duplicate_audit.csv"
            audit_df = daily_df.loc[duplicate_mask].sort_values(daily_keys)
            audit_df.to_csv(audit_path, index=False)

            conflict_counts = (
                audit_df.groupby(daily_keys, dropna=False)["rain_mm"]
                .nunique(dropna=False)
            )
            conflicting_keys = conflict_counts[conflict_counts > 1]

            print(
                f"WARNING: {len(audit_df):,} duplicate-key rows found using "
                f"date + district + taluka + circle. "
                f"Conflicting keys: {len(conflicting_keys):,}."
            )
            print(f"Duplicate audit saved: {audit_path.relative_to(ROOT)}")

            if len(conflicting_keys):
                raise ValueError(
                    "Conflicting rainfall values exist for the same full "
                    "date/district/taluka/circle key. No daily deduplication "
                    "was performed; inspect maharain_daily_duplicate_audit.csv."
                )

            # Only identical observations for the same full key are safe to collapse.
            daily_df = daily_df.drop_duplicates(daily_keys, keep="last")
        else:
            print("Daily duplicate check: no duplicate full keys found.")

        daily_df = daily_df.sort_values(
            ["district", "taluka", "circle", "date"]
        )
        daily_df.to_csv(DAILY_OUT, index=False)

        series_count = daily_df[
            ["district", "taluka", "circle"]
        ].drop_duplicates().shape[0]
        print(
            f"Daily stats: {len(daily_df):,} rows | "
            f"distinct district/taluka/circle series: {series_count} | "
            f"unique circle names: {daily_df['circle'].nunique()} | "
            f"talukas: {daily_df['taluka'].nunique()} | "
            f"days: {daily_df['date'].nunique()} | "
            f"{daily_df['date'].min().date()} to {daily_df['date'].max().date()}"
        )
        city = daily_df[
            daily_df["taluka"].astype(str).str.contains(
                r"Pune city|Haveli",
                case=False,
                na=False
            )
        ]

        if not city.empty:
            print(
                "Pune city + Haveli circles:",
                ", ".join(sorted(city["circle"].dropna().unique()))
            )

            print(
                "Distinct location series:",
                city[["district", "taluka", "circle"]]
                .drop_duplicates().shape[0]
            )

            top = (
                city.groupby("date")["rain_mm"]
                .mean()
                .sort_values(ascending=False)
                .head(8)
            )

            print("\nWettest days (average of Pune city + Haveli circles):")
            print(top.round(1).to_string())

    monthly_df = save_frames(
        monthly_frames,
        MONTHLY_OUT,
        sort_by=["circle", "date_month", "source_file"],
    )
    if monthly_df is not None:
        monthly_df = monthly_df.drop_duplicates(
            ["date_month", "circle", "source_file"], keep="last"
        )
        monthly_df.to_csv(MONTHLY_OUT, index=False)
        print(
            f"Monthly summary stats: {len(monthly_df):,} rows | "
            f"circles: {monthly_df['circle'].nunique()} | "
            f"months: {monthly_df['date_month'].nunique()}"
        )

    period_df = save_frames(
        period_frames,
        PERIOD_OUT,
        sort_by=["circle", "period_end_date", "source_file"],
    )
    if period_df is not None:
        period_df = period_df.drop_duplicates(
            ["period_start_date", "period_end_date", "circle", "source_file"],
            keep="last",
        )
        period_df.to_csv(PERIOD_OUT, index=False)

    current_df = save_frames(
        current_frames,
        CURRENT_OUT,
        sort_by=["parent_area", "area_name", "source_file"],
    )
    if current_df is not None:
        print(
            f"Current report stats: {len(current_df):,} rows | "
            f"areas: {current_df['area_name'].nunique()}"
        )

    print("\nFinished. Existing daily data is kept separate from summary reports.")


if __name__ == "__main__":
    main()
