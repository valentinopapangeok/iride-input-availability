#!/usr/bin/env python3
"""Summarize input availability latency from collected audit CSV files.

Default behavior:
- reads audit_results/**/latest_input_audit_results_*.csv next to this script;
- uses only the latest CSV per audit date, so repeated runs on the same day do not overweight that day;
- keeps only product/input pairs still present in the latest audit CSV, so retired audit rows do not skew averages;
- computes latency as audit run date minus latest available input date;
- excludes monthly-only latest values such as YYYY-MM from the numeric average;
- prints latency by product/input by default, so each row shows the input name and the related product.

Examples:
  python latency_summary.py
  python latency_summary.py --product-summary
  python latency_summary.py --all-runs --include-monthly
  python latency_summary.py --results-dir /path/to/audit_results
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median


ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Expected availability latency thresholds in days.
# These are intentionally explicit and local so they can be adjusted when provider SLAs change.
# A latency value is highlighted only when it is strictly greater than the threshold.
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
    ("07/08", "CAMS GHG"): None,  # monthly value; excluded from numeric averages by default
    ("07", "S5P-PAL CH4"): 14,
    ("08", "OCO-2"): 30,
    ("08", "OCO-3"): 30,
    ("08", "OCO-2 Forward"): 7,
    ("08", "OCO-3 Forward"): 7,
    ("09", "CAMS atmospheric composition forecast"): 1,
    ("09", "Sentinel-3 SYNERGY AOD"): 2,
    ("09", "GCOM-C SGLI L2 Atmosphere ARNP"): 2,
    ("09", "MODIS AOD"): 3,
}


@dataclass(frozen=True)
class Sample:
    product: str
    input_name: str
    run_date: dt.date
    latest_raw: str
    latest_date: dt.date | None
    latency_days: int | None
    monthly_value: bool
    status: str
    source_file: Path


def parse_run_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_latest_date(value: str) -> tuple[dt.date | None, bool]:
    """Return (date, is_monthly_value)."""
    if not value:
        return None, False
    value = value.strip()

    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = map(int, value.split("-"))
        return dt.date(year, month, 1), True

    # Handles YYYY-MM-DD, YYYY-MM-DD HH:MM:SS, and ISO datetimes.
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    if match:
        try:
            return dt.date.fromisoformat(match.group(1)), False
        except ValueError:
            return None, False

    return None, False


def discover_csvs(results_dir: Path, all_runs: bool) -> list[Path]:
    files = sorted(results_dir.glob("**/latest_input_audit_results_*.csv"))
    if all_runs:
        return files

    latest_by_day: dict[str, Path] = {}
    for path in files:
        # Parent folder is expected to be YYYY-MM-DD. Fall back to filename ordering.
        day_key = path.parent.name if re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.parent.name) else path.stem
        current = latest_by_day.get(day_key)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            latest_by_day[day_key] = path
    return [latest_by_day[key] for key in sorted(latest_by_day)]


def current_keys_from_latest_csv(csv_paths: list[Path]) -> set[tuple[str, str]]:
    if not csv_paths:
        return set()
    latest_path = max(csv_paths, key=lambda path: path.stat().st_mtime)
    keys: set[tuple[str, str]] = set()
    with latest_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            product = row.get("product", "").strip() or "unknown"
            input_name = row.get("input_name", "").strip() or "unknown"
            keys.add((product, input_name))
    return keys


def load_samples(
    csv_paths: list[Path],
    include_monthly: bool,
    current_keys: set[tuple[str, str]] | None = None,
) -> list[Sample]:
    samples: list[Sample] = []
    for path in csv_paths:
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                run_dt = parse_run_datetime(row.get("run_at_utc", ""))
                if run_dt is None:
                    continue
                product = row.get("product", "").strip() or "unknown"
                input_name = row.get("input_name", "").strip() or "unknown"
                if current_keys is not None and (product, input_name) not in current_keys:
                    continue

                latest_raw = row.get("latest_date", "")
                latest_date, monthly = parse_latest_date(latest_raw)
                if monthly and not include_monthly:
                    latency = None
                elif latest_date is None:
                    latency = None
                else:
                    latency = (run_dt.date() - latest_date).days

                samples.append(
                    Sample(
                        product=product,
                        input_name=input_name,
                        run_date=run_dt.date(),
                        latest_raw=latest_raw,
                        latest_date=latest_date,
                        latency_days=latency,
                        monthly_value=monthly,
                        status=row.get("status", ""),
                        source_file=path,
                    )
                )
    return samples


def fmt_days(value: float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}d"
    return f"{value}d"


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def color_red(value: str, enabled: bool) -> str:
    if not enabled or value == "-":
        return value
    return f"{ANSI_RED}{value}{ANSI_RESET}"


def maybe_highlight_days(value: float | int | None, threshold: int | None, enabled: bool) -> str:
    rendered = fmt_days(value)
    if value is None or threshold is None:
        return rendered
    return color_red(rendered, enabled and value > threshold)


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(strip_ansi(cell)))

    def pad(cell: str, width: int) -> str:
        return cell + " " * (width - len(strip_ansi(cell)))

    def render(row: list[str]) -> str:
        return " | ".join(pad(cell, widths[i]) for i, cell in enumerate(row))

    print(render(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(render(row))


def summarize(samples: list[Sample], key_fn, include_counts: bool = False, color: bool = True) -> list[list[str]]:
    grouped: dict[tuple[str, ...], list[Sample]] = defaultdict(list)
    for sample in samples:
        grouped[key_fn(sample)].append(sample)

    rows: list[list[str]] = []
    for key in sorted(grouped):
        group = grouped[key]
        latencies = [s.latency_days for s in group if s.latency_days is not None]
        missing = len(group) - len(latencies)
        latest_sample = max(group, key=lambda s: (s.run_date, s.latest_date or dt.date.min))
        if latencies:
            avg = mean(latencies)
            med = median(latencies)
            min_v = min(latencies)
            max_v = max(latencies)
        else:
            avg = med = min_v = max_v = None

        threshold = EXPECTED_LATENCY_DAYS.get((key[0], key[1])) if len(key) >= 2 else None
        row = [
            *key,
            maybe_highlight_days(avg, threshold, color),
            maybe_highlight_days(med, threshold, color),
            maybe_highlight_days(min_v, threshold, color),
            maybe_highlight_days(max_v, threshold, color),
            latest_sample.latest_raw or "-",
        ]
        if include_counts:
            row = [
                *key,
                str(len(group)),
                str(len(latencies)),
                str(missing),
                maybe_highlight_days(avg, threshold, color),
                maybe_highlight_days(med, threshold, color),
                maybe_highlight_days(min_v, threshold, color),
                maybe_highlight_days(max_v, threshold, color),
                latest_sample.latest_raw or "-",
            ]
        rows.append(row)
    return rows


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Average availability latency from audit result CSVs.")
    parser.add_argument("--results-dir", type=Path, default=script_dir / "audit_results")
    parser.add_argument("--all-runs", action="store_true", help="Use every CSV instead of only the latest CSV per day.")
    parser.add_argument("--include-monthly", action="store_true", help="Include YYYY-MM values as first day of month in averages.")
    parser.add_argument("--include-retired", action="store_true", help="Include product/input pairs that are no longer present in the latest audit CSV.")
    parser.add_argument("--product-summary", action="store_true", help="Also print an aggregate product-level latency table.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI red highlighting for threshold breaches.")
    args = parser.parse_args()

    csv_paths = discover_csvs(args.results_dir, args.all_runs)
    current_keys = None if args.include_retired else current_keys_from_latest_csv(csv_paths)
    samples = load_samples(csv_paths, args.include_monthly, current_keys=current_keys)

    print(f"Results dir: {args.results_dir}")
    print(f"CSV files used: {len(csv_paths)}")
    print(f"Rows loaded: {len(samples)}")
    if args.include_retired:
        print("Retired/renamed product-input pairs are included.")
    else:
        print("Only product-input pairs present in the latest audit CSV are included. Use --include-retired to include older retired rows.")
    if not args.include_monthly:
        print("Monthly YYYY-MM values are excluded from numeric averages. Use --include-monthly to include them as day 1 of the month.")
    if not args.no_color:
        print("Latency cells above the expected threshold are highlighted in red. Use --no-color to disable.")
    print()

    input_headers = ["Product", "Input", "Avg latency", "Median", "Min", "Max", "Latest seen"]
    input_rows = summarize(samples, lambda s: (s.product, s.input_name), color=not args.no_color)
    print("Average latency by product/input")
    print_table(input_headers, input_rows)

    if args.product_summary:
        print()
        product_headers = ["Product", "Avg latency", "Median", "Min", "Max", "Latest seen"]
        product_rows = summarize(samples, lambda s: (s.product,), color=not args.no_color)
        print("Average latency by product")
        print_table(product_headers, product_rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
