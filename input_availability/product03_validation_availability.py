#!/usr/bin/env python3
"""Check Product 03 input/ground-truth availability for planned samples.

For every row in a samples CSV, the script checks:

* CM SAF SARAH-3 DNI temporal and spatial availability;
* ERA5-Land temporal and spatial availability;
* whether both sources are available for the same sample.

The existing ``input_availability/config.json`` credential schema is reused.
No credentials are written to the output.

Example:

    python input_availability/product03_validation_availability.py \
      --samples input_availability/product03_samples.csv

Required CSV columns:

    area,datetime_utc,west,south,east,north
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import time
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE / "config.json"
DEFAULT_DOWNLOAD_DIR = BASE / "product03_validation_downloads"
DEFAULT_COLLECTION = "EO:EUM:DAT:0863"
DEFAULT_CDS_DATASET = "reanalysis-era5-land"
DEFAULT_CDS_VARIABLE = "surface_solar_radiation_downwards"


@dataclass(frozen=True)
class Sample:
    area: str
    timestamp: dt.datetime
    west: float
    south: float
    east: float
    north: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north


def parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def load_samples(path: Path) -> list[Sample]:
    required = {"area", "datetime_utc", "west", "south", "east", "north"}
    samples: list[Sample] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, start=2):
            try:
                sample = Sample(
                    area=(row["area"] or "").strip(),
                    timestamp=parse_timestamp(row["datetime_utc"]),
                    west=float(row["west"]),
                    south=float(row["south"]),
                    east=float(row["east"]),
                    north=float(row["north"]),
                )
            except Exception as exc:
                raise ValueError(f"Invalid sample at CSV line {line_number}: {exc}") from exc
            if not sample.area:
                raise ValueError(f"Empty area at CSV line {line_number}")
            if sample.west >= sample.east or sample.south >= sample.north:
                raise ValueError(f"Invalid bbox at CSV line {line_number}: {sample.bbox}")
            samples.append(sample)
    if not samples:
        raise ValueError("The samples CSV contains no rows")
    return samples


def load_credentials(config_path: Path) -> None:
    if not config_path.exists():
        raise FileNotFoundError(f"Credential config not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for provider in config.get("providers", {}).values():
        for key, value in provider.get("env", {}).items():
            os.environ.setdefault(key, str(value))


def require_env(*names: str) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing credential variables: {', '.join(missing)}")


def utc_day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    return start, start + dt.timedelta(days=1)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "download"


def download_cmsaf_days(
    days: Iterable[dt.date],
    download_dir: Path,
    collection_id: str,
) -> tuple[dict[dt.date, Path], dict[dt.date, str]]:
    import eumdac

    require_env("EUMETSAT_CONSUMER_KEY", "EUMETSAT_CONSUMER_SECRET")
    token = eumdac.AccessToken(
        (os.environ["EUMETSAT_CONSUMER_KEY"], os.environ["EUMETSAT_CONSUMER_SECRET"])
    )
    store = eumdac.DataStore(token)
    collection = None
    last_error = None
    for attempt in range(4):
        try:
            collection = store.get_collection(collection_id)
            # Force the metadata request here so transient catalogue failures
            # are retried before processing individual validation dates.
            _ = collection.search_options
            break
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    if collection is None:
        raise RuntimeError(f"Could not open EUMETSAT collection {collection_id}") from last_error
    paths: dict[dt.date, Path] = {}
    errors: dict[dt.date, str] = {}
    destination = download_dir / "cmsaf"
    destination.mkdir(parents=True, exist_ok=True)

    for day in sorted(set(days)):
        start, end = utc_day_bounds(day)
        try:
            products = None
            for attempt in range(4):
                try:
                    products = [
                        product
                        for product in collection.search(dtstart=start, dtend=end)
                        if str(product).startswith(f"DNIin{day:%Y%m%d}")
                    ]
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(2 ** attempt)
            if not products:
                errors[day] = "DNI product not found"
                continue
            product = sorted(products, key=str)[0]
            path = destination / safe_name(str(product))
            with product.open() as source, path.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
            if not path.exists() or path.stat().st_size == 0:
                errors[day] = "downloaded DNI file is empty"
                continue
            paths[day] = path
        except Exception as exc:
            errors[day] = f"{type(exc).__name__}: {exc}"
    return paths, errors


def request_bbox(samples: Iterable[Sample]) -> tuple[float, float, float, float]:
    rows = list(samples)
    return (
        min(row.west for row in rows),
        min(row.south for row in rows),
        max(row.east for row in rows),
        max(row.north for row in rows),
    )


def download_era5_days(
    samples: list[Sample],
    download_dir: Path,
    dataset: str,
    variable: str,
) -> tuple[dict[dt.date, Path], dict[dt.date, str]]:
    import cdsapi

    require_env("CDS_KEY")
    client_kwargs: dict[str, str] = {"key": os.environ["CDS_KEY"]}
    if os.environ.get("CDS_URL"):
        client_kwargs["url"] = os.environ["CDS_URL"]
    client = cdsapi.Client(**client_kwargs)
    paths: dict[dt.date, Path] = {}
    errors: dict[dt.date, str] = {}
    destination = download_dir / "era5_land"
    destination.mkdir(parents=True, exist_ok=True)

    by_day: dict[dt.date, list[Sample]] = {}
    for sample in samples:
        by_day.setdefault(sample.timestamp.date(), []).append(sample)

    for day, day_samples in sorted(by_day.items()):
        hours = sorted({sample.timestamp.strftime("%H:00") for sample in day_samples})
        west, south, east, north = request_bbox(day_samples)
        path = destination / f"era5_land_{variable}_{day:%Y%m%d}.nc"
        request = {
            "variable": [variable],
            "year": [f"{day.year:04d}"],
            "month": [f"{day.month:02d}"],
            "day": [f"{day.day:02d}"],
            "time": hours,
            "data_format": "netcdf",
            "download_format": "unarchived",
            "area": [north, west, south, east],
        }
        try:
            client.retrieve(dataset, request, str(path))
            if not path.exists() or path.stat().st_size == 0:
                errors[day] = "downloaded ERA5-Land file is empty"
                continue
            paths[day] = path
        except Exception as exc:
            errors[day] = f"{type(exc).__name__}: {exc}"
    return paths, errors


def coordinate_values(dataset: Any, candidates: tuple[str, ...]):
    import numpy as np

    for name in candidates:
        if name in dataset.variables:
            values = np.asarray(dataset.variables[name][:], dtype=float)
            values = values[np.isfinite(values)]
            if values.size:
                return values
    return None


def dataset_bbox(dataset: Any) -> tuple[float, float, float, float] | None:
    import numpy as np

    lon = coordinate_values(dataset, ("lon", "longitude", "x"))
    lat = coordinate_values(dataset, ("lat", "latitude", "y"))
    if lon is not None and lat is not None:
        return float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat))

    attrs = {
        name: getattr(dataset, name, None)
        for name in (
            "geospatial_lon_min",
            "geospatial_lat_min",
            "geospatial_lon_max",
            "geospatial_lat_max",
        )
    }
    if all(value is not None for value in attrs.values()):
        return (
            float(attrs["geospatial_lon_min"]),
            float(attrs["geospatial_lat_min"]),
            float(attrs["geospatial_lon_max"]),
            float(attrs["geospatial_lat_max"]),
        )
    return None


def dataset_times(dataset: Any) -> list[dt.datetime]:
    import netCDF4

    for name in ("time", "valid_time", "datetime"):
        variable = dataset.variables.get(name)
        if variable is None or not getattr(variable, "units", None):
            continue
        calendar = getattr(variable, "calendar", "standard")
        decoded = netCDF4.num2date(
            variable[:],
            units=variable.units,
            calendar=calendar,
            only_use_cftime_datetimes=False,
            only_use_python_datetimes=True,
        )
        result: list[dt.datetime] = []
        for value in decoded.ravel() if hasattr(decoded, "ravel") else [decoded]:
            parsed = value
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            result.append(parsed.astimezone(dt.timezone.utc))
        return result
    return []


def bbox_contains(
    available: tuple[float, float, float, float] | None,
    required: tuple[float, float, float, float],
    tolerance: float = 0.11,
) -> bool:
    if available is None:
        return False
    aw, ass, ae, an = available
    rw, rs, re, rn = required
    # CDS subsets are aligned to the ERA5-Land 0.1-degree grid.  The returned
    # edge coordinates can therefore be up to one grid cell inside the exact
    # requested bbox even though the requested area is available.
    return (
        aw <= rw + tolerance
        and ass <= rs + tolerance
        and ae >= re - tolerance
        and an >= rn - tolerance
    )


def hour_present(times: list[dt.datetime], timestamp: dt.datetime) -> bool:
    start = timestamp.replace(minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(hours=1)
    return any(start <= value < end for value in times)


def inspect_file(path: Path) -> tuple[list[dt.datetime], tuple[float, float, float, float] | None, str]:
    try:
        import netCDF4
    except ImportError as exc:
        raise RuntimeError("netCDF4 is required for temporal/spatial checks") from exc

    netcdf_path = path
    temporary_path: Path | None = None
    try:
        # EUMETSAT Data Store returns SARAH-3 products as ZIP packages whose
        # archive name has no .zip suffix.  Extract the contained NetCDF before
        # inspecting temporal and spatial coordinates.
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = [name for name in archive.namelist() if name.lower().endswith(".nc")]
                if not members:
                    return [], None, "ZIP package contains no NetCDF file"
                with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as target:
                    with archive.open(members[0]) as source:
                        shutil.copyfileobj(source, target)
                    temporary_path = Path(target.name)
            netcdf_path = temporary_path

        with netCDF4.Dataset(netcdf_path, "r") as dataset:
            return dataset_times(dataset), dataset_bbox(dataset), ""
    except Exception as exc:
        return [], None, f"{type(exc).__name__}: {exc}"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def status(value: bool) -> str:
    return "present" if value else "not_present"


def build_results(
    samples: list[Sample],
    cmsaf_paths: dict[dt.date, Path],
    cmsaf_errors: dict[dt.date, str],
    era5_paths: dict[dt.date, Path],
    era5_errors: dict[dt.date, str],
) -> list[dict[str, str]]:
    cmsaf_info = {day: inspect_file(path) for day, path in cmsaf_paths.items()}
    era5_info = {day: inspect_file(path) for day, path in era5_paths.items()}
    rows: list[dict[str, str]] = []

    for sample in samples:
        day = sample.timestamp.date()
        cmsaf_times, cmsaf_bbox, cmsaf_inspect_error = cmsaf_info.get(day, ([], None, ""))
        era5_times, era5_bbox, era5_inspect_error = era5_info.get(day, ([], None, ""))

        cmsaf_temporal = bool(cmsaf_paths.get(day)) and hour_present(cmsaf_times, sample.timestamp)
        cmsaf_spatial = bool(cmsaf_paths.get(day)) and bbox_contains(cmsaf_bbox, sample.bbox)
        era5_temporal = bool(era5_paths.get(day)) and hour_present(era5_times, sample.timestamp)
        era5_spatial = bool(era5_paths.get(day)) and bbox_contains(era5_bbox, sample.bbox)
        input_present = cmsaf_temporal and cmsaf_spatial
        truth_present = era5_temporal and era5_spatial
        correspondence = input_present and truth_present

        notes = [
            value
            for value in (
                f"CMSAF: {cmsaf_errors.get(day)}" if cmsaf_errors.get(day) else "",
                f"CMSAF inspect: {cmsaf_inspect_error}" if cmsaf_inspect_error else "",
                f"ERA5-Land: {era5_errors.get(day)}" if era5_errors.get(day) else "",
                f"ERA5-Land inspect: {era5_inspect_error}" if era5_inspect_error else "",
            )
            if value
        ]
        rows.append(
            {
                "area": sample.area,
                "datetime_utc": sample.timestamp.isoformat().replace("+00:00", "Z"),
                "west": str(sample.west),
                "south": str(sample.south),
                "east": str(sample.east),
                "north": str(sample.north),
                "cmsaf_temporal": status(cmsaf_temporal),
                "cmsaf_spatial": status(cmsaf_spatial),
                "input": status(input_present),
                "era5_land_temporal": status(era5_temporal),
                "era5_land_spatial": status(era5_spatial),
                "ground_truth": status(truth_present),
                "correspondence": "yes" if correspondence else "no",
                "notes": " | ".join(notes),
            }
        )
    return rows


def write_results(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def reuse_downloaded_files(
    samples: list[Sample],
    download_dir: Path,
    era5_variable: str,
) -> tuple[dict[dt.date, Path], dict[dt.date, str], dict[dt.date, Path], dict[dt.date, str]]:
    days = sorted({sample.timestamp.date() for sample in samples})
    cmsaf_paths: dict[dt.date, Path] = {}
    cmsaf_errors: dict[dt.date, str] = {}
    era5_paths: dict[dt.date, Path] = {}
    era5_errors: dict[dt.date, str] = {}
    for day in days:
        cmsaf_candidates = sorted((download_dir / "cmsaf").glob(f"DNIin{day:%Y%m%d}*"))
        if cmsaf_candidates:
            cmsaf_paths[day] = cmsaf_candidates[0]
        else:
            cmsaf_errors[day] = "downloaded DNI file not found"

        era5_path = download_dir / "era5_land" / f"era5_land_{era5_variable}_{day:%Y%m%d}.nc"
        if era5_path.exists() and era5_path.stat().st_size > 0:
            era5_paths[day] = era5_path
        else:
            era5_errors[day] = "downloaded ERA5-Land file not found"
    return cmsaf_paths, cmsaf_errors, era5_paths, era5_errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Product 03 CMSAF/ERA5-Land temporal-spatial availability."
    )
    parser.add_argument("--samples", required=True, type=Path, help="CSV with planned samples")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Credential config JSON")
    parser.add_argument("--output", type=Path, default=None, help="Result CSV path")
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--cmsaf-collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--era5-dataset", default=DEFAULT_CDS_DATASET)
    parser.add_argument("--era5-variable", default=DEFAULT_CDS_VARIABLE)
    parser.add_argument(
        "--reuse-downloads",
        action="store_true",
        help="Reuse existing CMSAF and ERA5-Land files without calling providers",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    samples = load_samples(args.samples)
    run_timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or BASE / "product03_validation_results" / f"availability_{run_timestamp}.csv"
    args.download_dir.mkdir(parents=True, exist_ok=True)

    if args.reuse_downloads:
        cmsaf_paths, cmsaf_errors, era5_paths, era5_errors = reuse_downloaded_files(
            samples,
            args.download_dir,
            args.era5_variable,
        )
    else:
        load_credentials(args.config)
        cmsaf_paths, cmsaf_errors = download_cmsaf_days(
            (sample.timestamp.date() for sample in samples),
            args.download_dir,
            args.cmsaf_collection,
        )
        era5_paths, era5_errors = download_era5_days(
            samples,
            args.download_dir,
            args.era5_dataset,
            args.era5_variable,
        )
    rows = build_results(samples, cmsaf_paths, cmsaf_errors, era5_paths, era5_errors)
    write_results(output, rows)

    matches = sum(row["correspondence"] == "yes" for row in rows)
    print(f"Samples checked: {len(rows)}")
    print(f"Input/ground-truth correspondence: {matches}/{len(rows)}")
    print(f"Results: {output}")
    return 0 if matches == len(rows) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
