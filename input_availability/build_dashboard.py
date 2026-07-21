#!/usr/bin/env python3
"""Build a static input-availability dashboard from audit CSV files."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

BASE = Path(__file__).resolve().parent
DEFAULT_RESULTS = BASE / "audit_results"
DEFAULT_SITE = BASE / "site"

EXPECTED_LATENCY_DAYS: dict[tuple[str, str], int | None] = {
    ("01/10", "Sentinel-3 LST"): 2,
    ("01", "GCOM-C L3 LST"): 3,
    ("01", "MODIS LST"): 3,
    ("01", "ERA5-Land skin temperature"): 6,
    ("01", "ERA5 skin temperature"): 6,
    ("02/11", "Sentinel-3 WST"): 2,
    ("02", "NPP/VIIRS SST"): 1,
    ("02", "GCOM-C L3 SST"): 3,
    ("02", "CMEMS-MED SST"): 1,
    ("03", "CM SAF SARAH-3 DNI"): 3,
    ("04", "MISTRAL radar"): 1,
    ("04", "H SAF H40B"): 0,
    ("05", "Sentinel-3 OLCI snow"): 2,
    ("05", "VIIRS snow"): 3,
    ("06", "MTG Cloud Mask"): 0,
    ("07/08", "CAMS GHG"): None,
    ("07", "S5P-PAL CH4"): 14,
    ("08", "OCO-2"): 30,
    ("08", "OCO-3"): 30,
    ("09", "CAMS atmospheric composition forecast"): 1,
    ("09", "Sentinel-3 SYNERGY AOD"): 2,
    ("09", "GCOM-C SGLI L2 Atmosphere ARNP"): 2,
    ("09", "MODIS AOD"): 3,
}


def parse_run_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_latest_date(value: str) -> tuple[dt.date | None, bool]:
    if not value:
        return None, False
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = map(int, value.split("-"))
        return dt.date(year, month, 1), True
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    if match:
        return dt.date.fromisoformat(match.group(1)), False
    return None, False


def discover_latest_per_day(results_dir: Path) -> list[Path]:
    latest_by_day: dict[str, Path] = {}
    for path in sorted(results_dir.glob("**/latest_input_audit_results_*.csv")):
        day = path.parent.name if re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.parent.name) else path.stem
        current = latest_by_day.get(day)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            latest_by_day[day] = path
    return [latest_by_day[key] for key in sorted(latest_by_day)]


def discover_latest_weekly_csv(results_dir: Path) -> Path | None:
    paths = sorted(
        results_dir.glob("**/weekly_input_availability_*.csv"),
        key=lambda path: path.stat().st_mtime,
    )
    return paths[-1] if paths else None


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def latency_for(row: dict) -> int | None:
    run_dt = parse_run_datetime(row.get("run_at_utc", ""))
    latest_date, monthly = parse_latest_date(row.get("latest_date", ""))
    if run_dt is None or latest_date is None or monthly:
        return None
    return (run_dt.date() - latest_date).days


def css_class_for(row: dict, latency: int | None) -> str:
    if row.get("found") != "yes":
        return "bad"
    threshold = EXPECTED_LATENCY_DAYS.get((row.get("product", ""), row.get("input_name", "")))
    if latency is not None and threshold is not None and latency > threshold:
        return "warn"
    return "ok"


def fmt_latency(value: int | float | None) -> str:
    if value is None:
        return "monthly" if value is None else "-"
    if isinstance(value, float):
        return f"{value:.1f}d"
    return f"{value}d"


def escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def summarize_history(csv_paths: list[Path], latest_rows: list[dict]) -> list[dict]:
    current_keys = {(row.get("product", ""), row.get("input_name", "")) for row in latest_rows}
    grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
    latest_seen: dict[tuple[str, str], str] = {}
    for path in csv_paths:
        for row in read_rows(path):
            key = (row.get("product", ""), row.get("input_name", ""))
            if key not in current_keys:
                continue
            lat = latency_for(row)
            if lat is not None:
                grouped[key].append(lat)
            if row.get("latest_date"):
                latest_seen[key] = row["latest_date"]

    out: list[dict] = []
    for product, input_name in sorted(current_keys):
        values = grouped.get((product, input_name), [])
        out.append({
            "product": product,
            "input_name": input_name,
            "avg_latency": mean(values) if values else None,
            "median_latency": median(values) if values else None,
            "min_latency": min(values) if values else None,
            "max_latency": max(values) if values else None,
            "latest_seen": latest_seen.get((product, input_name), ""),
        })
    return out


def render_status_badges(latest_rows: list[dict]) -> str:
    total = len(latest_rows)
    found = sum(row.get("found") == "yes" for row in latest_rows)
    stale = 0
    missing = total - found
    for row in latest_rows:
        if css_class_for(row, latency_for(row)) == "warn":
            stale += 1
    return f"""
    <section class="cards">
      <div class="card"><span>Total inputs</span><strong>{total}</strong></div>
      <div class="card ok"><span>Found</span><strong>{found}</strong></div>
      <div class="card warn"><span>Over threshold</span><strong>{stale}</strong></div>
      <div class="card bad"><span>Missing</span><strong>{missing}</strong></div>
    </section>
    """


def render_latest_table(rows: list[dict]) -> str:
    body = []
    for row in sorted(rows, key=lambda r: (r.get("product", ""), r.get("input_name", ""))):
        lat = latency_for(row)
        cls = css_class_for(row, lat)
        threshold = EXPECTED_LATENCY_DAYS.get((row.get("product", ""), row.get("input_name", "")))
        threshold_text = "monthly" if threshold is None and re.fullmatch(r"\d{4}-\d{2}", row.get("latest_date", "")) else ("-" if threshold is None else f"{threshold}d")
        body.append(f"""
          <tr class="{cls}">
            <td>{escape(row.get('product'))}</td>
            <td>{escape(row.get('input_name'))}</td>
            <td>{escape(row.get('status'))}</td>
            <td>{escape(row.get('latest_date') or '-')}</td>
            <td>{'-' if lat is None else f'{lat}d'}</td>
            <td>{threshold_text}</td>
            <td>{escape(row.get('files_found') or '0')}</td>
          </tr>
        """)
    return """
    <h2>Latest availability</h2>
    <table>
      <thead><tr><th>Product</th><th>Input</th><th>Status</th><th>Latest available</th><th>Latency</th><th>Expected</th><th>Files</th></tr></thead>
      <tbody>
    """ + "\n".join(body) + "</tbody></table>"


def render_availability_matrix(rows: list[dict], *, cadence: str, title: str, description: str) -> str:
    rows = [row for row in rows if row.get("cadence", "daily") == cadence]
    if not rows:
        return ""
    dates = sorted({row.get("date", "") for row in rows if row.get("date")})
    grouped: dict[tuple[str, str, str], dict[str, dict]] = defaultdict(dict)
    for row in rows:
        grouped[
            (row.get("product", ""), row.get("input_name", ""), row.get("adapter", ""))
        ][row.get("date", "")] = row

    labels = {
        "present": "✓",
        "missing": "×",
        "error": "!",
        "unknown": "?",
        "not_applicable": "—",
    }

    def cell(row: dict | None) -> str:
        if row is None:
            return '<td class="weekly unknown" title="not checked">?</td>'
        status = row.get("status", "unknown")
        notes = escape(row.get("notes", ""))
        files = escape(row.get("files_found", "0"))
        return (
            f'<td class="weekly {escape(status)}" '
            f'title="{notes}; files={files}">{labels.get(status, "?")}</td>'
        )

    def header_label(value: str) -> str:
        if cadence == "monthly":
            return value[2:] if re.fullmatch(r"\d{4}-\d{2}", value) else value
        return value[5:] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else value

    head = "".join(f"<th>{escape(header_label(day))}</th>" for day in dates)
    body = []
    for product, input_name, adapter in sorted(grouped):
        cells = "".join(cell(grouped[(product, input_name, adapter)].get(day)) for day in dates)
        body.append(f"<tr><td>{escape(product)}</td><td>{escape(input_name)}</td>{cells}</tr>")

    return f"""
    <h2>{escape(title)}</h2>
    <p class="hint">{escape(description)} Latest evidence is reused; older periods are checked search-only or with tiny provider probes where required. ✓ present · × missing or newer than latest available · ! provider/query error · ? not checked.</p>
    <table class="weekly-table">
      <thead><tr><th>Product</th><th>Input</th>{head}</tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def render_weekly_table(rows: list[dict]) -> str:
    return render_availability_matrix(
        rows,
        cadence="daily",
        title="Weekly availability",
        description="Daily products over the previous seven complete UTC days, excluding today.",
    )


def render_semestral_table(rows: list[dict]) -> str:
    return render_availability_matrix(
        rows,
        cadence="monthly",
        title="Semestral availability",
        description="Monthly products over the latest six calendar months.",
    )


def render_latency_table(rows: list[dict]) -> str:
    body = []
    for row in rows:
        body.append(f"""
          <tr>
            <td>{escape(row['product'])}</td>
            <td>{escape(row['input_name'])}</td>
            <td>{fmt_latency(row['avg_latency']) if row['avg_latency'] is not None else '-'}</td>
            <td>{fmt_latency(row['median_latency']) if row['median_latency'] is not None else '-'}</td>
            <td>{fmt_latency(row['min_latency']) if row['min_latency'] is not None else '-'}</td>
            <td>{fmt_latency(row['max_latency']) if row['max_latency'] is not None else '-'}</td>
            <td>{escape(row['latest_seen'] or '-')}</td>
          </tr>
        """)
    return """
    <h2>Historical latency summary</h2>
    <table>
      <thead><tr><th>Product</th><th>Input</th><th>Avg latency</th><th>Median</th><th>Min</th><th>Max</th><th>Latest seen</th></tr></thead>
      <tbody>
    """ + "\n".join(body) + "</tbody></table>"


def build_site(results_dir: Path, output_dir: Path) -> None:
    csv_paths = discover_latest_per_day(results_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not csv_paths:
        (output_dir / "index.html").write_text("<h1>No input availability results found</h1>")
        return

    latest_csv = max(csv_paths, key=lambda path: path.stat().st_mtime)
    latest_rows = read_rows(latest_csv)
    run_at = latest_rows[0].get("run_at_utc", "unknown") if latest_rows else "unknown"
    latency_rows = summarize_history(csv_paths, latest_rows)
    weekly_csv = discover_latest_weekly_csv(results_dir)
    weekly_rows = read_rows(weekly_csv) if weekly_csv else []

    shutil.copy2(latest_csv, output_dir / "latest_input_audit_results.csv")
    if weekly_csv:
        shutil.copy2(weekly_csv, output_dir / "weekly_input_availability.csv")
    (output_dir / "latest.json").write_text(json.dumps({
        "run_at_utc": run_at,
        "source_csv": str(latest_csv),
        "latest": latest_rows,
        "weekly_source_csv": str(weekly_csv) if weekly_csv else "",
        "weekly": weekly_rows,
        "latency_summary": latency_rows,
    }, indent=2))

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IRIDE input availability S5-02</title>
<style>
:root {{ color-scheme: light; --bg:#f6f8fb; --panel:#fff; --text:#172033; --muted:#637083; --ok:#edf8f1; --ok-border:#21a366; --warn:#fff4e5; --warn-border:#d97706; --bad:#fdecec; --bad-border:#dc2626; }}
body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }}
main {{ max-width:1180px; margin:0 auto; padding:32px 20px 56px; }}
h1 {{ margin:0 0 6px; font-size:34px; }}
h2 {{ margin-top:34px; }}
.meta {{ color:var(--muted); margin-bottom:22px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:14px; margin:24px 0; }}
.card {{ background:var(--panel); border:1px solid #d9e0ea; border-left:5px solid #8aa0b8; border-radius:12px; padding:16px; box-shadow:0 1px 2px rgba(15,23,42,.05); }}
.card span {{ display:block; color:var(--muted); font-size:13px; }}
.card strong {{ font-size:30px; }}
.card.ok {{ border-left-color:var(--ok-border); }} .card.warn {{ border-left-color:var(--warn-border); }} .card.bad {{ border-left-color:var(--bad-border); }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); border-radius:12px; overflow:hidden; box-shadow:0 1px 2px rgba(15,23,42,.05); }}
th,td {{ padding:10px 12px; text-align:left; border-bottom:1px solid #e6ebf2; font-size:14px; }}
th {{ background:#eaf0f7; color:#334155; font-weight:650; }}
tr.ok td:first-child {{ border-left:5px solid var(--ok-border); }}
tr.warn {{ background:var(--warn); }} tr.warn td:first-child {{ border-left:5px solid var(--warn-border); }}
tr.bad {{ background:var(--bad); }} tr.bad td:first-child {{ border-left:5px solid var(--bad-border); }}
.hint {{ color:var(--muted); font-size:14px; margin-top:-8px; }}
td.weekly {{ text-align:center; font-weight:800; font-size:16px; }}
td.weekly.present {{ background:var(--ok); color:#15803d; }}
td.weekly.missing {{ background:var(--bad); color:#b91c1c; }}
td.weekly.error {{ background:var(--warn); color:#b45309; }}
td.weekly.unknown {{ background:#f1f5f9; color:#64748b; }}
td.weekly.not_applicable {{ background:#f8fafc; color:#94a3b8; }}
a {{ color:#1d4ed8; }}
.footer {{ margin-top:28px; color:var(--muted); font-size:13px; }}
</style>
</head>
<body><main>
<h1>IRIDE input availability S5-02</h1>
<div class="meta">Last audit: <strong>{escape(run_at)}</strong> · Source: <code>{escape(latest_csv.name)}</code> · <a href="latest_input_audit_results.csv">download latest CSV</a> · <a href="latest.json">JSON</a></div>
{render_status_badges(latest_rows)}
{render_latest_table(latest_rows)}
{render_weekly_table(weekly_rows)}
{render_semestral_table(weekly_rows)}
{render_latency_table(latency_rows)}
<div class="footer">Generated automatically by GitHub Actions. Credentials and downloaded samples are not published.</div>
</main></body></html>
"""
    (output_dir / "index.html").write_text(html_doc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build static dashboard for input availability audit results.")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_SITE)
    args = parser.parse_args()
    build_site(args.results_dir, args.output_dir)
    print(f"Dashboard written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
