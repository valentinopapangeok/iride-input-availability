#!/usr/bin/env python3
"""Compare hourly S5-02-03 DNI exposure with ERA5-Land SSRD in Wh/m2.

The product interval begins at --datetime. ERA5-Land SSRD is de-accumulated
for that hour and converted from J/m2 to Wh/m2. DNI and SSRD are different
solar-radiation quantities; this reproduces the stated comparison method.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import h5py
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

BASE = Path(__file__).resolve().parent
DEFAULT_SAMPLES = BASE / "samples.csv"


def utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a UTC offset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("ERA5-Land comparison requires an exact UTC hour")
    return parsed


def sample_bbox(path: Path, instant: datetime, area: str | None) -> tuple[str, tuple[float, float, float, float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    matches = [
        row for row in rows
        if utc_timestamp(row["datetime_utc"]) == instant
        and (area is None or row["area"] == area)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one matching row in {path} for {instant.isoformat()} "
            f"and area {area!r}; found {len(matches)}"
        )
    row = matches[0]
    bounds = tuple(float(row[key]) for key in ("west", "south", "east", "north"))
    if not bounds[0] < bounds[2] or not bounds[1] < bounds[3]:
        raise ValueError("Invalid sample bounds")
    return row["area"], bounds


def read_ssrd(path: Path, instant: datetime) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with h5py.File(path, "r") as dataset:
        if "ssrd" not in dataset or "valid_time" not in dataset:
            raise ValueError(f"{path} must contain ssrd and valid_time")
        field = dataset["ssrd"]
        units = field.attrs.get("units", b"")
        units = units.decode() if isinstance(units, bytes) else str(units)
        if units.replace(" ", "") not in {"Jm**-2", "Jm-2"}:
            raise ValueError(f"Unexpected ssrd units in {path}: {units!r}")
        time_units = dataset["valid_time"].attrs.get("units", b"")
        time_units = time_units.decode() if isinstance(time_units, bytes) else str(time_units)
        if time_units != "seconds since 1970-01-01":
            raise ValueError(f"Unexpected valid_time units in {path}: {time_units!r}")
        target = int(instant.timestamp())
        indices = np.flatnonzero(dataset["valid_time"][:] == target)
        if len(indices) != 1:
            raise ValueError(f"{path} has {len(indices)} records for {instant.isoformat()}")
        values = np.asarray(field[int(indices[0])], dtype=np.float64)
        fill = field.attrs.get("_FillValue")
        if fill is not None:
            values[values == float(np.asarray(fill).flat[0])] = np.nan
        latitude = np.asarray(dataset["latitude"][:], dtype=np.float64)
        longitude = np.asarray(dataset["longitude"][:], dtype=np.float64)
    if values.shape != (len(latitude), len(longitude)):
        raise ValueError(f"Unexpected ssrd grid in {path}")
    return values, latitude, longitude


def grid_transform(latitude: np.ndarray, longitude: np.ndarray):
    if len(latitude) < 2 or len(longitude) < 2:
        raise ValueError("ERA5-Land grid must have at least two points per axis")
    dy = np.diff(latitude)
    dx = np.diff(longitude)
    if not (np.all(dy < 0) and np.all(dx > 0)):
        raise ValueError("Expected descending latitude and ascending longitude")
    if not (np.allclose(dy, dy[0], atol=1e-6) and np.allclose(dx, dx[0], atol=1e-6)):
        raise ValueError("Expected regular ERA5-Land latitude/longitude grid")
    return from_origin(
        float(longitude[0] - dx[0] / 2),
        float(latitude[0] - dy[0] / 2),
        float(dx[0]),
        float(-dy[0]),
    )


def product_on_reference_grid(path: Path, shape: tuple[int, int], transform):
    product = np.full(shape, np.nan, dtype=np.float64)
    coverage = np.zeros(shape, dtype=np.float32)
    with rasterio.open(path) as source:
        if source.count != 1 or source.crs is None:
            raise ValueError("Product must be a single-band georeferenced raster")
        raw = source.read(1, masked=True)
        valid = ~np.ma.getmaskarray(raw) & np.isfinite(raw.data)
        if not np.any(valid):
            raise ValueError("Product has no valid pixels")
        values = np.where(valid, raw.data, np.nan).astype(np.float64)
        reproject(
            source=values, destination=product,
            src_transform=source.transform, src_crs=source.crs,
            src_nodata=np.nan, dst_transform=transform, dst_crs="EPSG:4326",
            dst_nodata=np.nan, resampling=Resampling.average,
        )
        reproject(
            source=valid.astype(np.float32), destination=coverage,
            src_transform=source.transform, src_crs=source.crs,
            dst_transform=transform, dst_crs="EPSG:4326",
            resampling=Resampling.average,
        )
    return product, np.clip(coverage, 0, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datetime", required=True, help="Product interval start in UTC, e.g. 2022-08-03T10:00:00Z")
    parser.add_argument("--tiff", type=Path, required=True, help="S5-02-03 output raster for this hour")
    parser.add_argument("--product-quantity", choices=["direct_normal"], default="direct_normal",
                        help="S5-02-03 output quantity (default: direct_normal)")
    parser.add_argument("--product-unit", choices=["Wh/m2"], default="Wh/m2",
                        help="S5-02-03 hourly integrated unit (default: Wh/m2)")
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--area", help="Area label in the samples CSV")
    parser.add_argument("--reference", type=Path, help="ERA5-Land file for --datetime")
    parser.add_argument("--previous-reference", type=Path, help="ERA5-Land file for the preceding hour")
    parser.add_argument("--normalization", choices=["mean", "rms", "range"], default="mean")
    parser.add_argument("--min-coverage", type=float, default=0.0)
    parser.add_argument("--threshold", type=float, default=20.0)
    parser.add_argument("--output", type=Path, help="Output directory")
    args = parser.parse_args()

    if not 0 <= args.min_coverage <= 1:
        parser.error("--min-coverage must be in [0,1]")
    if args.threshold <= 0:
        parser.error("--threshold must be positive")
    instant = utc_timestamp(args.datetime)
    area, bbox = sample_bbox(args.samples, instant, args.area)
    if instant.hour == 23:
        parser.error("The pipeline integrates only 23:00-23:30 for hour 23; this hour needs separate handling")
    end = instant + timedelta(hours=1)
    stamp = end.strftime("%Y%m%d")
    ref_path = args.reference or BASE / "ground_truth/era5_land" / (
        f"era5_land_surface_solar_radiation_downwards_{stamp}.nc"
    )
    current, lat, lon = read_ssrd(ref_path, end)
    if end.hour == 1:
        hourly_j = current
        previous_path = None
    else:
        previous = instant
        previous_path = args.previous_reference or (
            ref_path if previous.date() == instant.date()
            else BASE / "ground_truth/era5_land" /
            f"era5_land_surface_solar_radiation_downwards_{previous:%Y%m%d}.nc"
        )
        try:
            prior, prior_lat, prior_lon = read_ssrd(previous_path, previous)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Cannot de-accumulate SSRD for {instant.isoformat()} to {end.isoformat()}: "
                f"the preceding ERA5-Land hour {previous.isoformat()} is required. "
                "Download that hour or pass --previous-reference."
            ) from exc
        if not (np.array_equal(lat, prior_lat) and np.array_equal(lon, prior_lon)):
            raise ValueError("Current and preceding ERA5-Land grids differ")
        hourly_j = current - prior
    if np.any(hourly_j[np.isfinite(hourly_j)] < -1):
        raise ValueError("Negative hourly SSRD after de-accumulation; verify source times and files")
    reference = np.maximum(hourly_j, 0) / 3600.0  # J/m2 -> Wh/m2

    transform = grid_transform(lat, lon)
    product, coverage = product_on_reference_grid(args.tiff, reference.shape, transform)
    west, south, east, north = bbox
    region = (
        (lon[None, :] >= west) & (lon[None, :] <= east)
        & (lat[:, None] >= south) & (lat[:, None] <= north)
    )
    valid = region & np.isfinite(reference) & np.isfinite(product) & (
        coverage > 0
    ) & (coverage >= args.min_coverage)
    rr, cc = np.where(valid)
    if not len(rr):
        raise ValueError("No valid matched ERA5-Land cells in the selected area")
    truth = reference[rr, cc]
    estimate = product[rr, cc]
    error = estimate - truth
    rmse = float(np.sqrt(np.mean(error ** 2)))
    denominator = {
        "mean": abs(float(np.mean(truth))),
        "rms": float(np.sqrt(np.mean(truth ** 2))),
        "range": float(np.ptp(truth)),
    }[args.normalization]
    relative = 100 * rmse / denominator if denominator > 0 else None
    output = args.output or BASE / "evidence/revalidation" / instant.strftime("%Y%m%dT%H%MZ")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "matched_cells.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("datetime_utc", "area", "latitude", "longitude",
                         "product_wh_m2", "reference_wh_m2", "difference_wh_m2",
                         "coverage_fraction"))
        for r, c, predicted, observed, difference in zip(rr, cc, estimate, truth, error):
            writer.writerow((instant.isoformat(), area, lat[r], lon[c],
                             predicted, observed, difference, coverage[r, c]))
    summary = {
        "datetime_utc": instant.isoformat(), "interval_end_utc": end.isoformat(),
        "era5_validity_utc": end.isoformat(), "area": area, "bounds": bbox,
        "matched_cells": len(rr), "rmse_wh_m2": rmse,
        "bias_wh_m2": float(np.mean(error)),
        "reference_mean_wh_m2": float(np.mean(truth)),
        "normalization": args.normalization, "denominator_wh_m2": denominator,
        "rrmse": relative / 100 if relative is not None else None,
        "relative_rmse_percent": relative, "threshold_percent": args.threshold,
        "preliminary_hour_meets_threshold": (
            relative <= args.threshold if relative is not None else None
        ),
        "min_coverage": args.min_coverage, "product_unit": args.product_unit,
        "product_quantity": args.product_quantity,
        "tiff": str(args.tiff), "reference": str(ref_path),
        "previous_reference": str(previous_path) if previous_path else None,
        "caveat": (
            "Single-hour diagnostic on ERA5-Land grid cells whose centres lie in "
            "the samples CSV rectangle. This rectangle is not a VA polygon or Italy "
            "land mask. DNI is direct normal radiation, whereas ERA5 SSRD is "
            "total downward solar radiation; equal units do not make them the "
            "same physical quantity. Confirm the report normalization and VA areas "
            "before formal validation."
        ),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
