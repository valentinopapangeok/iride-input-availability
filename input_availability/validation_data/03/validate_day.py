#!/usr/bin/env python3
"""Run S5-02-03 hourly comparisons for every TIFF of one UTC day."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import h5py

BASE = Path(__file__).resolve().parent


def reference_hours(path: Path) -> set[datetime]:
    if not path.exists():
        return set()
    with h5py.File(path) as dataset:
        if "valid_time" not in dataset or "ssrd" not in dataset:
            raise ValueError(f"Missing valid_time or ssrd in {path}")
        return {datetime.fromtimestamp(int(x), timezone.utc) for x in dataset["valid_time"][:]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--tiff-dir", required=True, type=Path)
    parser.add_argument("--samples", type=Path, default=BASE / "samples.csv")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    day = args.date
    reference = args.reference or BASE / "ground_truth/era5_land" / (
        f"era5_land_surface_solar_radiation_downwards_{day:%Y%m%d}.nc"
    )
    output = args.output or BASE / "evidence/revalidation" / day.strftime("%Y%m%d_full_day")
    output.mkdir(parents=True, exist_ok=True)
    available = reference_hours(reference)
    rows: list[dict] = []
    for hour in range(24):
        start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        row = {"datetime_utc": start.isoformat(), "status": "", "rrmse": "",
               "rmse_wh_m2": "", "matched_cells": "", "reason": "", "tiff": ""}
        matches = sorted(args.tiff_dir.glob(f"IRIDE-S_S5-02-03_{day:%Y%m%d}{hour:02d}_*.tif"))
        if len(matches) != 1:
            row.update(status="missing_or_ambiguous_tiff", reason=f"Expected one TIFF, found {len(matches)}")
        else:
            row["tiff"] = str(matches[0])
            if hour == 23:
                row.update(status="excluded", reason="Pipeline integrates 23:00–23:30 only; TIFF metadata says 23:00–00:00")
            else:
                needed = [end] if hour == 0 else [start, end]
                missing = [stamp.strftime("%H:%M") for stamp in needed if stamp not in available]
                if missing:
                    row.update(status="missing_reference", reason="ERA5 validity hours missing: " + ", ".join(missing))
                else:
                    command = [sys.executable, str(BASE / "validate_hour.py"),
                               "--datetime", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "--tiff", str(matches[0]), "--samples", str(args.samples),
                               "--reference", str(reference),
                               "--output", str(output / f"{hour:02d}")]
                    result = subprocess.run(command, capture_output=True, text=True)
                    if result.returncode:
                        row.update(status="error", reason=(result.stderr or result.stdout).strip()[-1000:])
                    else:
                        details = json.loads(result.stdout)
                        row.update(status="validated" if details["rrmse"] is not None else "undefined_rrmse",
                                   rrmse=details["rrmse"] if details["rrmse"] is not None else "",
                                   rmse_wh_m2=details["rmse_wh_m2"],
                                   matched_cells=details["matched_cells"],
                                   reason="" if details["rrmse"] is not None else "Reference denominator is zero")
        rows.append(row)
        print(f"{hour:02d}:00 {row['status']} {row['rrmse']} {row['reason'][:100]}")

    fields = list(rows[0])
    with (output / "all_hours.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    ranking = sorted((r for r in rows if r["status"] == "validated"), key=lambda r: float(r["rrmse"]))
    with (output / "ranking.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["rank", *fields])
        writer.writeheader()
        for rank, row in enumerate(ranking, 1):
            writer.writerow({"rank": rank, **row})
    summary = {"date": day.isoformat(), "tiff_dir": str(args.tiff_dir),
               "reference": str(reference), "available_reference_hours": sorted(x.strftime("%H:%M") for x in available if x.date() == day),
               "counts": {status: sum(r["status"] == status for r in rows) for status in sorted({r["status"] for r in rows})},
               "method": "Each full-hour TIFF is compared to de-accumulated ERA5-Land SSRD in Wh/m2 on the ERA5 grid; rRMSE is RMSE divided by the mean reference value.",
               "limitations": ["Product DNI and ERA5 SSRD are different solar-radiation quantities.",
                               "The area is the Italy rectangle from samples.csv, filtered by valid TIFF coverage.",
                               "Hour 23 is excluded because the workflow integrates only half an hour.",
                               "Selecting only the lowest rRMSE values requires disclosure of the full candidate set."]}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "counts": summary["counts"]}))


if __name__ == "__main__":
    main()
