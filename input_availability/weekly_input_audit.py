#!/usr/bin/env python3
"""Build a search-only weekly availability matrix from the latest audit.

The matrix covers the previous seven complete UTC days, excluding today. It uses
latest-audit results as a shortcut:
- dates after the latest available date are marked missing;
- the latest available date itself is marked present;
- only older days are queried provider-by-provider.

No sample data are downloaded by this script.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable

BASE = Path(__file__).resolve().parent
RESULTS_ROOT = BASE / "audit_results"
FIELDS = [
    "run_at_utc", "adapter", "product", "input_name", "date", "status",
    "found", "files_found", "source", "notes",
]

WEEKLY_NOT_APPLICABLE_ADAPTERS = {
    # Monthly products: a daily seven-day availability matrix would be
    # misleading, even when their latest_date is represented as a full
    # timestamp rather than YYYY-MM.
    "cdsapi_cams_ghg",
    "s5p_pal_ch4",
}


def load_latest_module():
    spec = importlib.util.spec_from_file_location("latest_input_audit", BASE / "latest_input_audit.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load latest_input_audit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["latest_input_audit"] = module
    spec.loader.exec_module(module)
    return module


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def previous_complete_days(days: int = 7) -> list[dt.date]:
    today = dt.datetime.now(dt.timezone.utc).date()
    return [today - dt.timedelta(days=offset) for offset in range(days, 0, -1)]


def result_path_for_run(run_at_utc: str) -> Path:
    run_date, run_time = run_at_utc.split("T", 1)
    safe_time = run_time.replace(":", "").removesuffix("Z")
    return RESULTS_ROOT / run_date / f"weekly_input_availability_{run_date}_{safe_time}Z.csv"


def discover_latest_csv(results_dir: Path) -> Path | None:
    paths = sorted(results_dir.glob("**/latest_input_audit_results_*.csv"), key=lambda p: p.stat().st_mtime)
    return paths[-1] if paths else None


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


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


def bbox_wkt(audit) -> str:
    min_lon, min_lat, max_lon, max_lat = audit.AOI_ITALY_BBOX
    return f"POLYGON(({min_lon} {max_lat}, {max_lon} {max_lat}, {max_lon} {min_lat}, {min_lon} {min_lat}, {min_lon} {max_lat}))"


def cdse_has_day(audit, day: dt.date, product_type: str, contains_name: str | None = None, timeliness: str | None = "NT") -> tuple[str, str, str]:
    start = f"{day.isoformat()}T00:00:00.000Z"
    end = f"{day.isoformat()}T23:59:59.999Z"
    parts = ["Collection/Name eq 'SENTINEL-3'"]
    if contains_name:
        parts.append(f"contains(Name,'{contains_name}')")
    else:
        parts.append("Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '%s')" % product_type)
    parts.extend([f"ContentDate/Start ge {start}", f"ContentDate/Start le {end}"])
    if timeliness:
        parts.append(f"contains(Name,'_{timeliness}_')")
    parts.append(f"OData.CSC.Intersects(area=geography'SRID=4326;{bbox_wkt(audit)}')")
    url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?" + urllib.parse.urlencode({
        "$filter": " and ".join(parts), "$top": "1", "$orderby": "ContentDate/Start desc",
    })
    data = audit.json_get(url, verify_ssl=False)
    vals = data.get("value", [])
    return ("present" if vals else "missing", str(len(vals)), vals[0].get("Name", "") if vals else "")


def cmr_has_day(short_name: str, day: dt.date, version: str | None = None) -> tuple[str, str, str]:
    import requests
    params = {
        "short_name": short_name,
        "page_size": 1,
        "sort_key": "-start_date",
        "temporal": f"{day.isoformat()}T00:00:00Z,{day.isoformat()}T23:59:59Z",
    }
    if version:
        params["version"] = version
    response = requests.get("https://cmr.earthdata.nasa.gov/search/granules.json", params=params, timeout=60, verify=False)
    response.raise_for_status()
    entries = response.json().get("feed", {}).get("entry", [])
    title = entries[0].get("producer_granule_id") or entries[0].get("title", "") if entries else ""
    return ("present" if entries else "missing", str(len(entries)), title)


def modis_has_day(audit, short_name: str, version: str, day: dt.date) -> tuple[str, str, str]:
    from modis_tools.auth import ModisSession
    from modis_tools.resources import CollectionApi, GranuleApi
    session = ModisSession(username=os.environ["EARTHDATA_USER"], password=os.environ["EARTHDATA_PASSWORD"])
    collection = CollectionApi(session=session).query(short_name=short_name, version=version)[0]
    granule_client = GranuleApi.from_collection(collection, session=session)
    granules = list(granule_client.query(start_date=day.isoformat(), end_date=day.isoformat(), bounding_box=list(audit.AOI_ITALY_BBOX)))
    return ("present" if granules else "missing", str(len(granules)), f"{short_name}.{version}")


def eumdac_has_day(collection_id: str, day: dt.date, product_filter: Callable[[object], bool] | None = None) -> tuple[str, str, str]:
    import eumdac
    token = eumdac.AccessToken((os.environ["EUMETSAT_CONSUMER_KEY"], os.environ["EUMETSAT_CONSUMER_SECRET"]))
    collection = eumdac.DataStore(token).get_collection(collection_id)
    start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    products = list(collection.search(dtstart=start, dtend=end))
    if product_filter:
        products = [p for p in products if product_filter(p)]
    return ("present" if products else "missing", str(len(products)), f"{collection_id} | {collection.title}")


def meteohub_has_day(audit, day: dt.date) -> tuple[str, str, str]:
    base_url = os.environ.get("METEOHUB_BASE_URL", "https://meteohub.agenziaitaliameteo.it").rstrip("/")
    dataset = os.environ.get("METEOHUB_RADAR_DATASET", "radar_sri_dpc")
    data = audit.json_get(f"{base_url}/api/datasets/{dataset}/opendata", verify_ssl=False)
    matches = [item for item in data if str(item.get("date", "")).startswith(day.isoformat()) or day.strftime("%Y%m%d") in str(item.get("filename", ""))]
    return ("present" if matches else "missing", str(len(matches)), dataset)


def gportal_has_day(audit, dataset_path: tuple[str, ...], day: dt.date, params: dict | None = None, bbox: list[float] | None = None, product_filter: Callable[[object], bool] | None = None) -> tuple[str, str, str]:
    import gportal
    gportal.username = os.environ["GPORTAL_USER"]
    gportal.password = os.environ["GPORTAL_PASSWORD"]
    dataset_id = gportal.datasets()
    for part in dataset_path:
        dataset_id = dataset_id[part]
    kwargs = {"dataset_ids": dataset_id, "start_time": f"{day.isoformat()}T00:00:00", "end_time": f"{day.isoformat()}T23:59:59", "count": 20}
    if params:
        kwargs["params"] = params
    if bbox:
        kwargs["bbox"] = bbox
    products = list(gportal.search(**kwargs).products())
    if product_filter:
        products = [p for p in products if product_filter(p)]
    return ("present" if products else "missing", str(len(products)), "/".join(dataset_path))


def cmems_has_day(day: dt.date) -> tuple[str, str, str]:
    """Check CMEMS file availability for one date without downloading data."""
    import copernicusmarine

    dataset_id = "SST_MED_SST_L4_NRT_OBSERVATIONS_010_004_a_V2"
    pattern = f"*{day.strftime('%Y%m%d')}*"
    response = copernicusmarine.get(
        dataset_id=dataset_id,
        filter=pattern,
        dry_run=True,
        no_directories=True,
        disable_progress_bar=True,
        username=os.environ["CMEMS_USER"],
        password=os.environ["CMEMS_PASSWORD"],
    )
    text = str(response)
    missing_tokens = ["no file", "0 file", "total size of the download: 0", "files: []"]
    if any(token in text.lower() for token in missing_tokens):
        return "missing", "0", f"{dataset_id}; dry_run filter={pattern}"
    return "present", "1", f"{dataset_id}; dry_run filter={pattern}"


def _cds_retrieve_probe(audit, *, dataset: str, request: dict, dest_name: str, url_env: str, key_env: str) -> tuple[str, str, str]:
    """Run the smallest available CDS/ADS retrieval probe and delete its output."""
    probe_dir = BASE / "runtime_weekly_probe" / "cds_ads"
    probe_dir.mkdir(parents=True, exist_ok=True)
    dest = probe_dir / dest_name
    if dest.exists():
        dest.unlink()

    key = os.environ[key_env]
    url = os.environ[url_env]
    use_legacy_cdsapi = key.count(":") == 1 and key.split(":", 1)[0].isdigit()
    if use_legacy_cdsapi:
        import cdsapi
        client = cdsapi.Client(url=url, key=key)
        client.retrieve(dataset, request, str(dest))
    else:
        from ecmwf.datastores import Client
        client = Client(url=url, key=key)
        request = dict(request)
        request.pop("format", None)
        request.setdefault("download_format", "unarchived")
        client.retrieve(dataset, request, str(dest))

    present = dest.exists() and dest.stat().st_size > 0
    size = dest.stat().st_size if present else 0
    try:
        if dest.exists():
            dest.unlink()
    finally:
        # Clean empty probe folders opportunistically.
        shutil.rmtree(probe_dir, ignore_errors=True)
    return ("present" if present else "missing", "1" if present else "0", f"{dataset}; probe bytes={size}")


def era5_has_day(audit, day: dt.date, *, land: bool) -> tuple[str, str, str]:
    dataset = "reanalysis-era5-land" if land else "reanalysis-era5-single-levels"
    request = {
        "product_type": "reanalysis",
        "format": "netcdf",
        "variable": "skin_temperature",
        # Tiny point-like box over Italy; this is an availability probe, not a sample.
        "area": [42.1, 12.4, 41.9, 12.6],
        "day": [f"{day.day:02d}"],
        "month": f"{day.month:02d}",
        "year": str(day.year),
        "time": "10:00",
        "data_format": "netcdf",
    }
    return _cds_retrieve_probe(
        audit,
        dataset=dataset,
        request=request,
        dest_name=f"{dataset}_{day.isoformat()}.nc",
        url_env="CDS_URL",
        key_env="CDS_KEY",
    )


def cams_aod_has_day(audit, day: dt.date) -> tuple[str, str, str]:
    request = {
        "variable": "total_aerosol_optical_depth_550nm",
        "date": day.isoformat(),
        "time": "12:00",
        "leadtime_hour": "0",
        "type": "forecast",
        # Tiny box over Italy; enough to validate exact-date retrieval.
        "area": [42.1, 12.4, 41.9, 12.6],
        "format": "grib",
    }
    return _cds_retrieve_probe(
        audit,
        dataset="cams-global-atmospheric-composition-forecasts",
        request=request,
        dest_name=f"cams_aod_{day.isoformat()}.grib",
        url_env="ADS_URL",
        key_env="ADS_KEY",
    )


def s5p_monthly_has_day(day: dt.date) -> tuple[str, str, str]:
    # Monthly product: a daily weekly matrix is not meaningful. Keep explicit.
    return "not_applicable", "0", "monthly product"


def checker_for(adapter_name: str):
    return {
        "cdse_sentinel3_lst": lambda audit, day: cdse_has_day(audit, day, "LST"),
        "cdse_sentinel3_wst": lambda audit, day: cdse_has_day(audit, day, "WST"),
        "cdse_sentinel3_olci_snow": lambda audit, day: cdse_has_day(audit, day, "", contains_name="OL_2_LFR"),
        "cdse_sentinel3_synergy_aod": lambda audit, day: cdse_has_day(audit, day, "SY_2_AOD___"),
        "earthdata_modis_lst": lambda audit, day: modis_has_day(audit, "MOD11A1", "061", day),
        "earthdata_modis_aod": lambda audit, day: modis_has_day(audit, "MCD19A2", "061", day),
        "podaac_viirs_sst": lambda audit, day: cmr_has_day("VIIRS_NPP-STAR-L2P-v2.80", day),
        "earthaccess_viirs_snow": lambda audit, day: cmr_has_day("VNP10A1F", day),
        "cmr_oco2": lambda audit, day: cmr_has_day("OCO2_L2_Lite_FP", day, "11.2r"),
        "cmr_oco3": lambda audit, day: cmr_has_day("OCO3_L2_Lite_FP", day, "11r"),
        "eumdac_sarah3_dni": lambda audit, day: eumdac_has_day("EO:EUM:DAT:0863", day, product_filter=lambda p: str(p).startswith("DNIin")),
        "hsaf_h40": lambda audit, day: eumdac_has_day("EO:EUM:DAT:1086", day),
        "eumdac_mtg_cloudmask": lambda audit, day: eumdac_has_day("EO:EUM:DAT:0800", day),
        "meteohub_mistral_radar": lambda audit, day: meteohub_has_day(audit, day),
        "gportal_gcomc_l3_lst": lambda audit, day: gportal_has_day(audit, ("GCOM-C/SGLI", "LEVEL3", "Land area", "L3-LST"), day, params={"ProcessTimeUnit": "01D", "orbitDirection": "Descending"}, product_filter=lambda p: p.get("mapProjection") == "EQR"),
        "gportal_gcomc_l3_sst": lambda audit, day: gportal_has_day(audit, ("GCOM-C/SGLI", "LEVEL3", "Oceanic sphere", "L3-SST"), day),
        "gportal_gcomc_l2_aod": lambda audit, day: gportal_has_day(audit, ("GCOM-C/SGLI", "LEVEL2", "Atmosphere", "L2-ARNP"), day, bbox=list(audit.AOI_ITALY_BBOX)),
        "s5p_pal_ch4": lambda audit, day: s5p_monthly_has_day(day),
        "cdsapi_cams_ghg": lambda audit, day: ("not_applicable", "0", "monthly/retrieval product; weekly search-only not implemented"),
        "cdsapi_era5_land": lambda audit, day: era5_has_day(audit, day, land=True),
        "cdsapi_era5": lambda audit, day: era5_has_day(audit, day, land=False),
        "cdsapi_cams_aod": lambda audit, day: cams_aod_has_day(audit, day),
        "copernicusmarine_cmems": lambda audit, day: cmems_has_day(day),
    }.get(adapter_name)


def weekly_rows(latest_rows: list[dict], run_at: str) -> list[dict]:
    audit = load_latest_module()
    audit.apply_config_to_environment(audit.load_config())
    days = previous_complete_days(7)
    rows: list[dict] = []
    for latest in latest_rows:
        adapter = latest.get("adapter", "")
        latest_day, monthly = parse_latest_date(latest.get("latest_date", ""))
        checker = checker_for(adapter)
        for day in days:
            status = "unknown"
            files = "0"
            source = "weekly_probe"
            notes = ""
            if monthly or adapter in WEEKLY_NOT_APPLICABLE_ADAPTERS:
                status, files, notes = "not_applicable", "0", "monthly product"
                source = "classification"
            elif latest_day and day > latest_day:
                status, source = "missing", "inferred_from_latest"
                notes = f"latest available is {latest_day.isoformat()}"
            elif latest_day and day == latest_day and latest.get("found") == "yes":
                status, files, source = "present", latest.get("files_found") or "1", "latest_audit"
                notes = latest.get("notes", "")
            elif checker is None:
                status, source, notes = "unknown", "no_checker", "weekly checker not implemented"
            else:
                try:
                    status, files, notes = checker(audit, day)
                except Exception as exc:
                    status, files, notes = "error", "0", f"{type(exc).__name__}: {exc}"
            rows.append({
                "run_at_utc": run_at,
                "adapter": adapter,
                "product": latest.get("product", ""),
                "input_name": latest.get("input_name", ""),
                "date": day.isoformat(),
                "status": status,
                "found": "yes" if status == "present" else "no",
                "files_found": files,
                "source": source,
                "notes": notes,
            })
    return rows


def write_rows(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a search-only weekly input availability matrix.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_ROOT)
    parser.add_argument("--latest-csv", type=Path)
    args = parser.parse_args()
    latest_csv = args.latest_csv or discover_latest_csv(args.results_dir)
    if latest_csv is None:
        raise SystemExit("No latest_input_audit_results CSV found")
    run_at = now_utc()
    latest_rows = read_rows(latest_csv)
    rows = weekly_rows(latest_rows, run_at)
    out = result_path_for_run(run_at)
    write_rows(rows, out)
    print(f"Weekly availability rows written to {out}")
    print(f"Inputs: {len(latest_rows)} | days: 7 | cells: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
