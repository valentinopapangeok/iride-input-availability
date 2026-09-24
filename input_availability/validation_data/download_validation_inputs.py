#!/usr/bin/env python3
"""Download validation inputs/supporting data for Products 03, 06, 07 and 08.

The downloader is resumable: existing non-empty files are retained. Each
product receives an ``inputs/manifest.json`` describing every requested period.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
IA = BASE.parent
sys.path.insert(0, str(IA))
import latest_input_audit as audit  # noqa: E402
import product03_validation_availability as p03  # noqa: E402


def req(product: str) -> dict:
    return json.loads((BASE / product / "requirements.json").read_text())


def save_manifest(product: str, rows: list[dict]) -> None:
    path = BASE / product / "inputs" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"product": product, "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "records": rows}, indent=2))
    print(f"{product}: {path}", flush=True)


def download_03() -> list[dict]:
    samples = p03.load_samples(BASE / "03" / "samples.csv")
    days = sorted({x.timestamp.date() for x in samples})
    paths, errors = p03.download_cmsaf_days(days, BASE / "03" / "inputs", "EO:EUM:DAT:0863")
    return [{"dataset": "CM SAF SARAH-3 DNI", "date": d.isoformat(),
             "status": "downloaded" if d in paths else "error",
             "file": str(paths[d].relative_to(BASE / "03")) if d in paths else "",
             "bytes": paths[d].stat().st_size if d in paths else 0,
             "error": errors.get(d, "")} for d in days]


def download_06() -> list[dict]:
    import eumdac
    spec = req("06")
    dataset = next(x for x in spec["datasets"] if x["role"] == "input")
    token = eumdac.AccessToken((os.environ["EUMETSAT_CONSUMER_KEY"], os.environ["EUMETSAT_CONSUMER_SECRET"]))
    store = eumdac.DataStore(token)
    collection = None
    last_error = None
    for attempt in range(4):
        try:
            collection = store.get_collection(dataset["collection"])
            _ = collection.search_options
            break
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    if collection is None:
        raise RuntimeError(f"Could not open EUMETSAT collection {dataset['collection']}") from last_error
    rows = []
    for date_text, hours in spec["dates"].items():
        day = dt.date.fromisoformat(date_text)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
        products = None
        for attempt in range(4):
            try:
                products = list(collection.search(dtstart=start, dtend=start + dt.timedelta(days=1)))
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        for hour in hours:
            marker = day.strftime("%Y%m%d") + f"{hour:02d}"
            matches = [x for x in products if marker in str(x)]
            if not matches:
                rows.append({"dataset": dataset["name"], "datetime_utc": f"{date_text}T{hour:02d}:00:00Z",
                             "status": "missing", "file": ""})
                continue
            item = sorted(matches, key=str)[0]
            target_dir = BASE / "06" / "inputs" / "mtg_cloud_mask" / date_text
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / re.sub(r"[^A-Za-z0-9._,-]+", "_", str(item))
            if not target.exists() or target.stat().st_size == 0:
                with item.open() as source, target.open("wb") as sink:
                    while chunk := source.read(1024 * 1024):
                        sink.write(chunk)
            rows.append({"dataset": dataset["name"], "datetime_utc": f"{date_text}T{hour:02d}:00:00Z",
                         "status": "downloaded", "file": str(target.relative_to(BASE / "06")),
                         "bytes": target.stat().st_size})
    return rows


def download_s5p() -> list[dict]:
    spec = req("07")
    url = "https://data-portal.s5p-pal.com/api/s5p-l3/collections/ch4/items"
    response = requests.get(url, params={"limit": 100, "filter": "l3:period='month'", "filter-lang": "cql2-text"},
                            verify=False, timeout=120)
    response.raise_for_status()
    features = response.json().get("features", [])
    rows = []
    for month in spec["months"]:
        matches = [x for x in features if f"-month-{month.replace('-', '')}01-" in x.get("id", "")]
        if not matches:
            rows.append({"dataset": "S5P-PAL L3 CH4", "month": month, "status": "missing", "file": ""})
            continue
        asset = matches[0]["assets"]["product"]
        filename = asset.get("file:local_path") or Path(asset["href"]).name
        target_dir = BASE / "07" / "inputs" / "s5p_pal_ch4" / month
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        if not target.exists() or target.stat().st_size != int(asset.get("file:size", 0)):
            with requests.get(asset["href"], stream=True, verify=False, timeout=600) as source:
                source.raise_for_status()
                with target.open("wb") as sink:
                    for chunk in source.iter_content(1024 * 1024):
                        if chunk:
                            sink.write(chunk)
        expected = asset.get("file:checksum", "")
        # S5P-PAL exposes MD5 as a hex-encoded multihash: d5 01 10 + 16 bytes.
        expected_digest = expected[6:] if expected.startswith("d50110") and len(expected) == 38 else expected
        algorithm = "sha1" if len(expected_digest) == 40 else "md5"
        digest = hashlib.new(algorithm)
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        checksum = digest.hexdigest()
        rows.append({"dataset": "S5P-PAL L3 CH4", "month": month,
                     "status": "downloaded" if not expected_digest or checksum == expected_digest else "checksum_failed",
                     "file": str(target.relative_to(BASE / "07")), "bytes": target.stat().st_size,
                     "checksum_algorithm": algorithm, "checksum": checksum,
                     "expected_checksum": expected, "expected_digest": expected_digest})
    return rows


def download_cams(product: str) -> list[dict]:
    import cdsapi
    spec = req(product)
    dataset = next(x for x in spec["datasets"] if x["provider"] == "ADS")
    client = cdsapi.Client(url=os.environ["ADS_URL"], key=os.environ["ADS_KEY"], quiet=True)
    rows = []
    for month in spec["months"]:
        start = dt.date.fromisoformat(month + "-01")
        end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        target_dir = BASE / product / "inputs" / "cams_ghg" / month
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"cams_ghg_{dataset['variable']}_{month}.zip"
        try:
            if not target.exists() or target.stat().st_size == 0:
                client.retrieve("cams-global-greenhouse-gas-forecasts", {
                    "date": [f"{start.isoformat()}/{end.isoformat()}"],
                    "leadtime_hour": ["0", "3", "6", "9", "12", "15", "18", "21"],
                    "data_format": "netcdf_zip", "variable": [dataset["variable"]],
                    "area": [50, 5, 35, 20],
                }).download(str(target))
            rows.append({"dataset": dataset["name"], "month": month, "status": "downloaded",
                         "file": str(target.relative_to(BASE / product)), "bytes": target.stat().st_size})
        except Exception as exc:
            rows.append({"dataset": dataset["name"], "month": month, "status": "error", "file": "",
                         "error": f"{type(exc).__name__}: {exc}"[:500]})
    return rows


def download_oco(product: str, smoke: bool) -> list[dict]:
    import earthaccess
    spec = req(product)
    if not audit.earthaccess_login_from_env():
        raise RuntimeError("Earthdata login failed")
    rows = []
    for dataset in (x for x in spec["datasets"] if x["provider"] == "CMR"):
        version = audit.cmr_latest_collection_version(dataset["short_name"])
        for month in spec["months"]:
            start = dt.date.fromisoformat(month + "-01")
            end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            results = earthaccess.search_data(short_name=dataset["short_name"], version=version,
                                              temporal=(start.isoformat(), end.isoformat()))
            selected_version = version
            # A newly published collection version can be incomplete for older
            # periods. In that case let CMR search every still-published version
            # instead of declaring the month missing solely because it is absent
            # from the latest version.
            if not results:
                results = earthaccess.search_data(short_name=dataset["short_name"],
                                                  temporal=(start.isoformat(), end.isoformat()))
                selected_version = "previous/any"
            chosen = results[:1] if smoke else results
            if not chosen:
                rows.append({"dataset": dataset["name"], "month": month, "version": selected_version,
                             "status": "missing", "files_found": 0, "files_downloaded": 0})
                save_manifest(product, rows)
                continue
            target_dir = BASE / product / "inputs" / dataset["short_name"].lower() / month
            target_dir.mkdir(parents=True, exist_ok=True)
            downloaded = earthaccess.download(chosen, local_path=target_dir, threads=24, show_progress=True)
            valid = [Path(x) for x in downloaded if Path(x).exists() and Path(x).stat().st_size]
            rows.append({"dataset": dataset["name"], "month": month, "version": selected_version,
                         "status": "downloaded" if len(valid) == len(chosen) else "partial",
                         "files_found": len(results), "files_downloaded": len(valid),
                         "bytes": sum(x.stat().st_size for x in valid),
                         "directory": str(target_dir.relative_to(BASE / product))})
            save_manifest(product, rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", nargs="+", choices=["03", "06", "07", "08"], default=["03", "06", "07", "08"])
    parser.add_argument("--oco-smoke", action="store_true", help="Download one OCO granule per available month instead of all granules")
    args = parser.parse_args()
    audit.apply_config_to_environment(audit.load_config(IA / "config.json"))
    for product in args.products:
        if product == "03": rows = download_03()
        elif product == "06": rows = download_06()
        elif product == "07": rows = download_s5p() + download_cams("07")
        else: rows = download_oco("08", args.oco_smoke) + download_cams("08")
        save_manifest(product, rows)


if __name__ == "__main__":
    main()
