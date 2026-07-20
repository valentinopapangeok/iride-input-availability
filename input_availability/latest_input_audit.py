#!/usr/bin/env python3
"""Try the latest available input from each archive-backed ingestion source.

This script is intentionally credential-aware rather than credential-hungry:
- each adapter declares the environment variables it needs
- adapters with missing credentials are skipped cleanly
- results are written to a dated CSV under `audit_results/`

The adapter families mirror the original implementations recovered from
`IRIDE-master.zip`.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import warnings
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import urllib3

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
DOWNLOADS = BASE / "runtime_downloads"
RESULTS_ROOT = BASE / "audit_results"
AOI_ITALY_BBOX = (4.5, 35.27, 22.88, 47.81)  # min_lon, min_lat, max_lon, max_lat

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=FutureWarning, module=r"earthaccess\..*")
warnings.filterwarnings("ignore", message=r".*Unverified HTTPS request.*")

for logger_name in [
    "urllib3",
    "cdsapi",
    "copernicusmarine",
    "earthaccess",
    "eumdac",
    "paramiko",
]:
    logging.getLogger(logger_name).setLevel(logging.ERROR)

FIELDS = [
    "run_at_utc", "adapter", "product", "input_name", "found", "downloaded",
    "duration_seconds", "status", "latest_date", "files_found", "downloaded_file",
    "credential_status", "notes"
]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def result_path_for_run(run_at_utc: str) -> Path:
    run_date, run_time = run_at_utc.split("T", 1)
    safe_time = run_time.replace(":", "").removesuffix("Z")
    return RESULTS_ROOT / run_date / f"latest_input_audit_results_{run_date}_{safe_time}Z.csv"


def ensure_dirs(results_path: Path) -> None:
    DOWNLOADS.mkdir(exist_ok=True)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    if not results_path.exists():
        with results_path.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def reset_download_dir() -> None:
    """Start every audit with an empty temporary download area."""
    if DOWNLOADS.exists():
        shutil.rmtree(DOWNLOADS)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {"providers": {}}
    with path.open() as f:
        return json.load(f)


def apply_config_to_environment(config: dict) -> list[str]:
    """Load provider credentials/endpoints into os.environ.

    Existing environment variables win, so PyCharm run-config overrides still work.
    Returns the provider labels loaded from config for console visibility.
    """
    loaded_providers: list[str] = []
    for provider_id, provider in config.get("providers", {}).items():
        env = provider.get("env", {})
        if not env:
            continue
        loaded_providers.append(f"{provider_id}: {provider.get('provider_name', provider_id)}")
        for key, value in env.items():
            os.environ.setdefault(key, str(value))
    return loaded_providers


def print_config_summary(loaded_providers: list[str]) -> None:
    print("\nCredential config")
    print(f"  file: {CONFIG_PATH}")
    if not loaded_providers:
        print("  providers loaded: none")
        return
    print("  providers loaded:")
    for provider in loaded_providers:
        print(f"    - {provider}")


def write_result(row: dict, results_path: Path) -> None:
    ensure_dirs(results_path)
    with results_path.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)


def env_present(*names: str) -> bool:
    return all(os.getenv(name) for name in names)


def missing_env(*names: str) -> list[str]:
    return [name for name in names if not os.getenv(name)]


def product_keys(product: str) -> list[str]:
    return [part.strip() for part in product.split("/") if part.strip()]


def print_product_summary(rows: list[dict]) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for product in product_keys(row["product"]):
            grouped[product].append(row)

    print("\nAvailability summary by product")
    print("Product | Input | Found | Downloaded | Seconds | Latest available | Files | Sample")
    print("-" * 124)
    for product in sorted(grouped):
        for row in grouped[product]:
            downloaded = row.get("downloaded_file") or "-"
            if len(downloaded) > 58:
                downloaded = "…" + downloaded[-57:]
            print(
                f"{product:>7} | "
                f"{row['input_name'][:34]:34} | "
                f"{row.get('found', 'no')[:5]:5} | "
                f"{row.get('downloaded', 'no')[:9]:9} | "
                f"{row.get('duration_seconds', '0.00'):>7} | "
                f"{(row.get('latest_date') or '-')[:25]:25} | "
                f"{row.get('files_found', '0'):>5} | "
                f"{downloaded}"
            )


def bbox_to_wkt(bbox: tuple[float, float, float, float]) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"POLYGON(({min_lon} {max_lat}, {max_lon} {max_lat}, {max_lon} {min_lat}, {min_lon} {min_lat}, {min_lon} {max_lat}))"


def latest_day_candidates(days: int = 14) -> Iterable[dt.date]:
    today = dt.datetime.now(dt.timezone.utc).date()
    for offset in range(days):
        yield today - dt.timedelta(days=offset)


def latest_month_candidates(months: int = 24) -> Iterable[dt.date]:
    today = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    for offset in range(months):
        year = today.year
        month = today.month - offset
        while month <= 0:
            month += 12
            year -= 1
        yield dt.date(year, month, 1)


def end_of_month(day: dt.date) -> dt.date:
    next_month = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return next_month - dt.timedelta(days=1)


def json_get(url: str, verify_ssl: bool = True) -> dict | list:
    if verify_ssl:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.loads(response.read().decode())
    import requests
    response = requests.get(url, timeout=60, verify=False)
    response.raise_for_status()
    return response.json()


def download_url(url: str, dest: Path, headers: dict[str, str] | None = None) -> Path:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=300) as response, dest.open("wb") as f:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return dest


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._,-]+", "_", name).strip("._-") or "download"


@dataclass
class Adapter:
    name: str
    product: str
    input_name: str
    required_env: tuple[str, ...]
    runner: Callable[["Adapter"], dict]

    def run(self) -> dict:
        missing = missing_env(*self.required_env)
        if missing:
            return {
                "found": "no",
                "downloaded": "no",
                "duration_seconds": "0.00",
                "status": "skipped_missing_credentials",
                "latest_date": "",
                "files_found": "0",
                "downloaded_file": "",
                "credential_status": f"missing: {', '.join(missing)}",
                "notes": "",
            }
        start = time.perf_counter()
        try:
            result = self.runner(self)
            duration = time.perf_counter() - start
            result.setdefault("credential_status", "present")
            result.setdefault("status", "found")
            result["found"] = "yes" if result["status"] in {"found", "downloaded"} else "no"
            result["downloaded"] = "yes" if result["status"] == "downloaded" else "no"
            result["duration_seconds"] = f"{duration:.2f}"
            return result
        except Exception as exc:
            duration = time.perf_counter() - start
            return {
                "found": "no",
                "downloaded": "no",
                "duration_seconds": f"{duration:.2f}",
                "status": "error",
                "latest_date": "",
                "files_found": "0",
                "downloaded_file": "",
                "credential_status": "present" if self.required_env else "not_required",
                "notes": f"{type(exc).__name__}: {exc}",
            }


# ---------- Implemented adapters ----------

def run_cams_aod(adapter: Adapter) -> dict:
    import cdsapi  # optional dependency
    client = cdsapi.Client(url=os.environ["ADS_URL"], key=os.environ["ADS_KEY"])
    dest_dir = DOWNLOADS / "cams_aod"
    dest_dir.mkdir(exist_ok=True)
    for day in latest_day_candidates(10):
        dest = dest_dir / f"cams_aod_{day.isoformat()}.grib"
        request = {
            "variable": "total_aerosol_optical_depth_550nm",
            "date": day.isoformat(),
            "time": "12:00",
            "leadtime_hour": "0",
            "type": "forecast",
            "area": [47.81, 4.5, 35.27, 19.2],
            "format": "grib",
        }
        try:
            client.retrieve("cams-global-atmospheric-composition-forecasts", request, str(dest))
            if dest.exists() and dest.stat().st_size > 0:
                return {"status": "downloaded", "latest_date": day.isoformat(), "files_found": "1", "downloaded_file": str(dest), "notes": "CAMS AOD 550 nm"}
        except Exception:
            continue
    return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": "searched last 10 days"}


def run_cdse_query(
    adapter: Adapter,
    product_type: str,
    contains_name: str | None = None,
    timeliness: str | None = "NT",
    download_sample: bool = False,
    collection_name: str = "SENTINEL-3",
) -> dict:
    timeliness_token = f"_{timeliness}_" if timeliness else None
    for day in latest_day_candidates(14):
        start = f"{day.isoformat()}T00:00:00.000Z"
        end = f"{day.isoformat()}T23:59:59.999Z"
        parts = [f"Collection/Name eq '{collection_name}'"]
        if contains_name:
            parts.append(f"contains(Name,'{contains_name}')")
        else:
            parts.append("Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '%s')" % product_type)
        parts.extend([f"ContentDate/Start ge {start}", f"ContentDate/Start le {end}"])
        if timeliness_token:
            parts.append(f"contains(Name,'{timeliness_token}')")
        url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?" + urllib.parse.urlencode({
            "$filter": " and ".join(parts),
            "$top": "1",
            "$orderby": "ContentDate/Start desc",
        })
        data = json_get(url, verify_ssl=False)
        vals = data.get("value", [])
        if vals:
            downloaded_file = ""
            notes = f"timeliness={timeliness or 'any'}; {vals[0].get('Name', '')}"
            if download_sample:
                try:
                    import requests

                    token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
                    response = requests.post(
                        token_url,
                        data={
                            "client_id": "cdse-public",
                            "username": os.environ["CDSE_USER"],
                            "password": os.environ["CDSE_PASSWORD"],
                            "grant_type": "password",
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=60,
                        verify=False,
                    )
                    response.raise_for_status()
                    token = response.json()["access_token"]
                    product_id = vals[0]["Id"]
                    product_name = sanitize_filename(vals[0].get("Name", product_id))
                    dest_dir = DOWNLOADS / "cdse" / adapter.name
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / product_name
                    session = requests.Session()
                    session.headers.update({"Authorization": f"Bearer {token}"})
                    download_url = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products({product_id})/$value"
                    response = session.get(download_url, allow_redirects=False, stream=True, timeout=300, verify=False)
                    while response.status_code in (301, 302, 303, 307):
                        download_url = response.headers["Location"]
                        response = session.get(download_url, allow_redirects=False, stream=True, timeout=300, verify=False)
                    response.raise_for_status()
                    with dest.open("wb") as f:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    if dest.exists() and dest.stat().st_size > 0:
                        downloaded_file = str(dest)
                        notes = f"{notes}; sample downloaded"
                except Exception as exc:
                    notes = f"{notes}; sample download failed: {type(exc).__name__}"
            return {
                "status": "downloaded" if downloaded_file else "found",
                "latest_date": day.isoformat(),
                "files_found": str(len(vals)),
                "downloaded_file": downloaded_file,
                "credential_status": "not_required_for_search",
                "notes": notes,
            }
    return {
        "status": "no_data_found",
        "latest_date": "",
        "files_found": "0",
        "downloaded_file": "",
        "credential_status": "not_required_for_search",
        "notes": f"searched last 14 days for timeliness={timeliness or 'any'}",
    }


def run_s3_lstr(adapter: Adapter) -> dict:
    return run_cdse_query(adapter, product_type="LST", download_sample=True)


def run_s3_wst(adapter: Adapter) -> dict:
    return run_cdse_query(adapter, product_type="WST", download_sample=True)


def run_s3_synergy_aod(adapter: Adapter) -> dict:
    return run_cdse_query(adapter, product_type="SY_2_AOD___", download_sample=True)


def run_s3_olci_snow(adapter: Adapter) -> dict:
    return run_cdse_query(adapter, product_type="", contains_name="OL_2_LFR", download_sample=True)


def run_s2_l2a(adapter: Adapter) -> dict:
    """Check Sentinel-2 Level-2A availability without downloading a full SAFE archive.

    A complete Sentinel-2 SAFE product is commonly hundreds of MB.  The audit records
    the newest product identifier for targeted visual-band extraction, rather than
    treating an arbitrary full-scene archive as a lightweight sample download.
    """
    return run_cdse_query(
        adapter,
        product_type="S2MSI2A",
        timeliness=None,
        download_sample=False,
        collection_name="SENTINEL-2",
    )


def run_mistral_radar(adapter: Adapter) -> dict:
    import requests

    base_url = os.environ.get("METEOHUB_BASE_URL", "https://meteohub.agenziaitaliameteo.it").rstrip("/")
    dataset = os.environ.get("METEOHUB_RADAR_DATASET", "radar_sri_dpc")
    data = json_get(f"{base_url}/api/datasets/{dataset}/opendata", verify_ssl=False)
    if not data:
        return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "credential_status": "not_required", "notes": "empty listing"}
    latest = sorted(data, key=lambda x: x.get("date", ""), reverse=True)[0]
    filename = latest.get("filename", "")
    download_url = f"{base_url}/api/opendata/{filename}"
    response = requests.get(download_url, stream=True, timeout=60, verify=False)
    response.raise_for_status()
    size = int(response.headers.get("content-length") or 0)
    max_bytes = int(os.environ.get("METEOHUB_MAX_DOWNLOAD_MB", "100")) * 1024 * 1024
    if size and size > max_bytes:
        response.close()
        return {
            "status": "found",
            "latest_date": latest.get("date", ""),
            "files_found": str(len(data)),
            "downloaded_file": "",
            "credential_status": "not_required",
            "notes": f"{dataset} {filename}; download skipped because file is {size / 1024 / 1024:.1f} MB > METEOHUB_MAX_DOWNLOAD_MB",
        }

    dest_dir = DOWNLOADS / "meteohub_radar"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / filename
    with dest.open("wb") as f:
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)
    return {
        "status": "downloaded" if dest.exists() and dest.stat().st_size > 0 else "found",
        "latest_date": latest.get("date", ""),
        "files_found": str(len(data)),
        "downloaded_file": str(dest) if dest.exists() else "",
        "credential_status": "not_required",
        "notes": f"{dataset} {filename}",
    }


def run_era5_land(adapter: Adapter) -> dict:
    """Download the latest available ERA5-Land skin temperature slice from CDS.

    Mirrors the recovered Product 01 ingestion: reanalysis-era5-land,
    variable=skin_temperature, area=[50, 0, 30, 20], time=10:00.
    """
    cds_url = os.environ["CDS_URL"]
    cds_key = os.environ["CDS_KEY"]
    # Current CDS prefers the ecmwf-datastores client with a token-only key.
    # Older recovered IRIDE code used cdsapi with UID:APIKEY. Support both.
    use_legacy_cdsapi = cds_key.count(":") == 1 and cds_key.split(":", 1)[0].isdigit()
    if use_legacy_cdsapi:
        import cdsapi
        client = cdsapi.Client(url=cds_url, key=cds_key)

        def retrieve(dataset: str, request: dict, target: str) -> None:
            client.retrieve(dataset, request, target)
    else:
        from ecmwf.datastores import Client
        client = Client(url=cds_url, key=cds_key)

        def retrieve(dataset: str, request: dict, target: str) -> None:
            request = dict(request)
            request.pop("format", None)
            request.setdefault("download_format", "unarchived")
            client.retrieve(dataset, request, target)
    dest_dir = DOWNLOADS / "era5_land"
    dest_dir.mkdir(exist_ok=True)
    for day in latest_day_candidates(30):
        dest = dest_dir / f"era5_land_skin_temperature_{day.isoformat()}.nc"
        request = {
            "product_type": "reanalysis",
            "format": "netcdf",
            "variable": "skin_temperature",
            "area": [50, 0, 30, 20],
            "day": [f"{day.day:02d}"],
            "month": f"{day.month:02d}",
            "year": str(day.year),
            "time": "10:00",
            "data_format": "netcdf",
        }
        try:
            retrieve("reanalysis-era5-land", request, str(dest))
            if dest.exists() and dest.stat().st_size > 0:
                return {
                    "status": "downloaded",
                    "latest_date": day.isoformat(),
                    "files_found": "1",
                    "downloaded_file": str(dest),
                    "notes": "ERA5-Land skin_temperature 10:00",
                }
        except Exception as exc:
            msg = str(exc).lower()
            if any(token in msg for token in ["credential", "authentication", "unauthorized", "api endpoint not found", "key provided"]):
                raise
            continue
    return {
        "status": "no_data_found",
        "latest_date": "",
        "files_found": "0",
        "downloaded_file": "",
        "notes": "searched last 30 days",
    }


def run_era5_single_level(adapter: Adapter) -> dict:
    """Download the latest available ERA5 single-levels skin temperature slice from CDS."""
    cds_url = os.environ["CDS_URL"]
    cds_key = os.environ["CDS_KEY"]
    use_legacy_cdsapi = cds_key.count(":") == 1 and cds_key.split(":", 1)[0].isdigit()
    if use_legacy_cdsapi:
        import cdsapi
        client = cdsapi.Client(url=cds_url, key=cds_key)

        def retrieve(dataset: str, request: dict, target: str) -> None:
            client.retrieve(dataset, request, target)
    else:
        from ecmwf.datastores import Client
        client = Client(url=cds_url, key=cds_key)

        def retrieve(dataset: str, request: dict, target: str) -> None:
            request = dict(request)
            request.pop("format", None)
            request.setdefault("download_format", "unarchived")
            client.retrieve(dataset, request, target)

    dest_dir = DOWNLOADS / "era5"
    dest_dir.mkdir(exist_ok=True)
    for day in latest_day_candidates(30):
        dest = dest_dir / f"era5_skin_temperature_{day.isoformat()}.nc"
        request = {
            "product_type": "reanalysis",
            "format": "netcdf",
            "variable": "skin_temperature",
            "area": [50, 0, 30, 20],
            "day": [f"{day.day:02d}"],
            "month": f"{day.month:02d}",
            "year": str(day.year),
            "time": "10:00",
            "data_format": "netcdf",
        }
        try:
            retrieve("reanalysis-era5-single-levels", request, str(dest))
            if dest.exists() and dest.stat().st_size > 0:
                return {
                    "status": "downloaded",
                    "latest_date": day.isoformat(),
                    "files_found": "1",
                    "downloaded_file": str(dest),
                    "notes": "ERA5 skin_temperature 10:00",
                }
        except Exception as exc:
            msg = str(exc).lower()
            if any(token in msg for token in ["credential", "authentication", "unauthorized", "api endpoint not found", "key provided"]):
                raise
            continue
    return {
        "status": "no_data_found",
        "latest_date": "",
        "files_found": "0",
        "downloaded_file": "",
        "notes": "searched last 30 days",
    }


def run_cmems_med_sst(adapter: Adapter) -> dict:
    import copernicusmarine
    dest_dir = DOWNLOADS / "cmems_med_sst"
    dest_dir.mkdir(exist_ok=True)
    dataset_id = "SST_MED_SST_L4_NRT_OBSERVATIONS_010_004_a_V2"
    for day in latest_day_candidates(10):
        pattern = f"*{day.strftime('%Y%m%d')}*"
        try:
            result = copernicusmarine.get(
                dataset_id=dataset_id,
                filter=pattern,
                output_directory=str(dest_dir),
                no_directories=True,
                disable_progress_bar=True,
                username=os.environ["CMEMS_USER"],
                password=os.environ["CMEMS_PASSWORD"],
            )
            files = sorted(dest_dir.glob(f"*{day.strftime('%Y%m%d')}*"))
            if files:
                return {"status": "downloaded", "latest_date": day.isoformat(), "files_found": str(len(files)), "downloaded_file": str(files[0]), "notes": dataset_id}
        except Exception as exc:
            if type(exc).__name__ == "InvalidUsernameOrPassword":
                raise
            continue
    return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": "searched last 10 days"}



def earthaccess_login_from_env():
    """Authenticate earthaccess for sample downloads when Earthdata creds exist."""
    if not env_present("EARTHDATA_USER", "EARTHDATA_PASSWORD"):
        return False
    import earthaccess
    os.environ["EARTHDATA_USERNAME"] = os.environ["EARTHDATA_USER"]
    os.environ["EARTHDATA_PASSWORD"] = os.environ["EARTHDATA_PASSWORD"]
    earthaccess.login(strategy="environment", persist=False)
    return True

def run_viirs_snow(adapter: Adapter) -> dict:
    import earthaccess
    earthaccess_login_from_env()
    bbox = AOI_ITALY_BBOX
    for day in latest_day_candidates(14):
        start = day.isoformat()
        end = (day + dt.timedelta(days=1)).isoformat()
        results = earthaccess.search_data(short_name='VNP10A1F', temporal=(start, end), bounding_box=bbox)
        if results:
            dest_dir = DOWNLOADS / "earthaccess" / adapter.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            downloaded = earthaccess.download(results[:1], local_path=dest_dir, show_progress=False)
            downloaded_file = str(downloaded[0]) if downloaded else ""
            return {"status": "downloaded" if downloaded_file else "found", "latest_date": day.isoformat(), "files_found": str(len(results)), "downloaded_file": downloaded_file, "notes": "VNP10A1F"}
    return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": "searched last 14 days"}


def run_modis_latest(adapter: Adapter, short_name: str, version: str, notes: str) -> dict:
    import earthaccess
    from modis_tools.auth import ModisSession
    from modis_tools.resources import CollectionApi, GranuleApi
    session = ModisSession(username=os.environ['EARTHDATA_USER'], password=os.environ['EARTHDATA_PASSWORD'])
    collection = CollectionApi(session=session).query(short_name=short_name, version=version)[0]
    granule_client = GranuleApi.from_collection(collection, session=session)
    for day in latest_day_candidates(14):
        granules = list(granule_client.query(start_date=day.isoformat(), end_date=day.isoformat(), bounding_box=list(AOI_ITALY_BBOX)))
        if granules:
            downloaded_file = ""
            dest_dir = DOWNLOADS / "earthaccess" / adapter.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            try:
                if not earthaccess_login_from_env():
                    raise RuntimeError("missing Earthdata credentials for sample download")
                search_kwargs = {"short_name": short_name, "count": 1, "version": version}
                search_kwargs["temporal"] = (day.isoformat(), (day + dt.timedelta(days=1)).isoformat())
                search_kwargs["bounding_box"] = AOI_ITALY_BBOX
                results = earthaccess.search_data(**search_kwargs)
                if results:
                    downloaded = earthaccess.download(results[:1], local_path=dest_dir, show_progress=False)
                    if downloaded:
                        downloaded_file = str(downloaded[0])
            except Exception as exc:
                notes = f"{notes}; sample download failed: {type(exc).__name__}"
            status = "downloaded" if downloaded_file else "found"
            return {"status": status, "latest_date": day.isoformat(), "files_found": str(len(granules)), "downloaded_file": downloaded_file, "notes": notes}
    return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": "searched last 14 days"}


def run_modis_lst(adapter: Adapter) -> dict:
    return run_modis_latest(adapter, "MOD11A1", "061", "MOD11A1.061")


def run_modis_aod(adapter: Adapter) -> dict:
    return run_modis_latest(adapter, "MCD19A2", "061", "MCD19A2.061")


def run_cams_ghg(adapter: Adapter) -> dict:
    import cdsapi
    client = cdsapi.Client(url=os.environ["ADS_URL"], key=os.environ["ADS_KEY"])
    dest_dir = DOWNLOADS / "cams_ghg"
    dest_dir.mkdir(exist_ok=True)
    for day in latest_day_candidates(40):
        month_start = day.replace(day=1)
        month_end = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        dest = dest_dir / f"cams_ghg_{month_start.strftime('%Y_%m')}.zip"
        request = {
            "date": [f"{month_start.isoformat()}/{month_end.isoformat()}"],
            "leadtime_hour": ["0", "3", "6", "9", "12", "15", "18", "21"],
            "data_format": "netcdf_zip",
            "variable": ["ch4_column_mean_molar_fraction", "co2_column_mean_molar_fraction"],
            "area": [50, 5, 35, 20],
        }
        try:
            client.retrieve("cams-global-greenhouse-gas-forecasts", request).download(str(dest))
            if dest.exists() and dest.stat().st_size > 0:
                return {"status": "downloaded", "latest_date": month_start.strftime('%Y-%m'), "files_found": "1", "downloaded_file": str(dest), "notes": "CAMS GHG monthly bundle"}
        except Exception:
            continue
    return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": "searched recent months"}


def run_s5p_pal_ch4(adapter: Adapter) -> dict:
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = "https://data-portal.s5p-pal.com/api/s5p-l3/collections/ch4/items"
    params = {
        "limit": 100,
        "filter": "l3:period='month'",
        "filter-lang": "cql2-text",
    }
    response = requests.get(url, params=params, verify=False, timeout=60)
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": "no PAL CH4 monthly items"}

    def sort_key(feature: dict) -> str:
        props = feature.get("properties", {})
        return props.get("archive_date") or props.get("end_datetime") or props.get("datetime") or feature.get("id", "")

    latest = sorted(features, key=sort_key, reverse=True)[0]
    props = latest.get("properties", {})
    assets = latest.get("assets", {})
    asset_href = ""
    downloaded_file = ""
    dest_dir = DOWNLOADS / "s5p_pal" / adapter.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    for asset in assets.values():
        asset_href = asset.get("href", "")
        if asset_href:
            try:
                filename = sanitize_filename(Path(urllib.parse.urlparse(asset_href).path).name or latest.get("id", "download"))
                dest = dest_dir / filename
                response = requests.get(asset_href, stream=True, verify=False, timeout=300)
                response.raise_for_status()
                with dest.open("wb") as dst:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            dst.write(chunk)
                if dest.exists() and dest.stat().st_size > 0:
                    downloaded_file = str(dest)
            except Exception as exc:
                asset_href = f"{asset_href}; sample download failed: {type(exc).__name__}"
            break
    return {
        "status": "downloaded" if downloaded_file else "found",
        "latest_date": props.get("end_datetime") or props.get("datetime") or latest.get("id", ""),
        "files_found": str(len(features)),
        "downloaded_file": downloaded_file,
        "notes": asset_href or latest.get("id", ""),
    }


def run_cmr_latest(adapter: Adapter, short_name: str, version: str | None = None, download_sample: bool = False) -> dict:
    import requests
    import earthaccess
    month_scoped = short_name in {"OCO2_L2_Lite_FP", "OCO3_L2_Lite_FP"}
    entries = []
    latest = ""
    title = short_name
    if month_scoped:
        for month_start in latest_month_candidates(24):
            month_end = end_of_month(month_start)
            params = {
                "short_name": short_name,
                "page_size": 1,
                "sort_key": "-start_date",
                "temporal": f"{month_start.isoformat()}T00:00:00Z,{month_end.isoformat()}T23:59:59Z",
            }
            if version:
                params["version"] = version
            response = requests.get(
                "https://cmr.earthdata.nasa.gov/search/granules.json",
                params=params,
                timeout=60,
                verify=False,
            )
            response.raise_for_status()
            entries = response.json().get("feed", {}).get("entry", [])
            if entries:
                # Report the acquisition date of the granule, not the final
                # day of the month used as the CMR search window.
                latest = entries[0].get("time_start", "").split("T", 1)[0]
                break
    else:
        params = {"short_name": short_name, "page_size": 1, "sort_key": "-start_date"}
        if version:
            params["version"] = version
        response = requests.get(
            "https://cmr.earthdata.nasa.gov/search/granules.json",
            params=params,
            timeout=60,
            verify=False,
        )
        response.raise_for_status()
        entries = response.json().get("feed", {}).get("entry", [])
        if entries:
            latest = entries[0].get("time_start", "")
    if not entries:
        return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": short_name}
    entry = entries[0]
    if not title or title == short_name:
        title = entry.get("producer_granule_id") or entry.get("title", "")
    downloaded_file = ""
    status = "found"
    if download_sample:
        try:
            if not earthaccess_login_from_env():
                raise RuntimeError("missing Earthdata credentials for sample download")
            search_kwargs = {"short_name": short_name, "count": 1}
            if version:
                search_kwargs["version"] = version
            if latest:
                if month_scoped:
                    month_start = dt.date.fromisoformat(latest).replace(day=1)
                    month_end = end_of_month(month_start)
                    search_kwargs["temporal"] = (month_start.isoformat(), month_end.isoformat())
                else:
                    search_kwargs["temporal"] = (latest, latest)
            # Match the CMR latest-granule search above. These generic CMR
            # inputs are not AOI-filtered there, so adding a bbox here can make
            # sample download incorrectly disappear even when availability exists.
            results = earthaccess.search_data(**search_kwargs)
            if results:
                dest_dir = DOWNLOADS / "earthaccess" / adapter.name
                dest_dir.mkdir(parents=True, exist_ok=True)
                downloaded = earthaccess.download(results[:1], local_path=dest_dir, show_progress=False)
                if downloaded:
                    downloaded_file = str(downloaded[0])
                    status = "downloaded"
        except Exception as exc:
            title = f"{title}; sample download failed: {type(exc).__name__}"
    return {"status": status, "latest_date": latest, "files_found": "1", "downloaded_file": downloaded_file, "notes": title}


def run_oco2(adapter: Adapter) -> dict:
    return run_cmr_latest(adapter, "OCO2_L2_Lite_FP", "11.2r", download_sample=True)


def run_oco3(adapter: Adapter) -> dict:
    return run_cmr_latest(adapter, "OCO3_L2_Lite_FP", "11r", download_sample=True)


def run_viirs_sst(adapter: Adapter) -> dict:
    return run_cmr_latest(adapter, "VIIRS_NPP-STAR-L2P-v2.80", download_sample=True)


def run_eumdac_latest(
    adapter: Adapter,
    collection_id: str,
    download_sample: bool = False,
    search_days: int = 7,
    product_filter: Callable[[object], bool] | None = None,
) -> dict:
    import eumdac
    token = eumdac.AccessToken((os.environ["EUMETSAT_CONSUMER_KEY"], os.environ["EUMETSAT_CONSUMER_SECRET"]))
    datastore = eumdac.DataStore(token)
    collection = datastore.get_collection(collection_id)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=search_days)
    products = list(collection.search(dtstart=start, dtend=end))
    if product_filter:
        products = [product for product in products if product_filter(product)]
    if not products:
        return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": collection_id}
    latest = sorted(products, key=lambda p: getattr(p, "sensing_start", None) or str(p))[-1]
    downloaded_file = ""
    status = "found"
    if download_sample:
        dest_dir = DOWNLOADS / "eumdac" / adapter.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r"[^A-Za-z0-9._,-]+", "_", str(latest))
        dest = dest_dir / filename
        with latest.open() as src, dest.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        if dest.exists() and dest.stat().st_size > 0:
            downloaded_file = str(dest)
            status = "downloaded"
    latest_date = getattr(latest, "sensing_start", None) or str(latest)
    return {
        "status": status,
        "latest_date": str(latest_date),
        "files_found": str(len(products)),
        "downloaded_file": downloaded_file,
        "notes": f"{collection_id} | {collection.title}",
    }


def run_sarah3_dni(adapter: Adapter) -> dict:
    return run_eumdac_latest(
        adapter,
        "EO:EUM:DAT:0863",
        download_sample=True,
        search_days=7,
        product_filter=lambda product: str(product).startswith("DNIin"),
    )


def run_hsaf_h40b_datastore(adapter: Adapter) -> dict:
    return run_eumdac_latest(adapter, "EO:EUM:DAT:1086", download_sample=True, search_days=7)


def run_mtg_cloudmask(adapter: Adapter) -> dict:
    return run_eumdac_latest(adapter, "EO:EUM:DAT:0800", download_sample=True, search_days=2)


def run_eumdac_quicklook(
    adapter: Adapter,
    collection_id: str,
    entry_marker: str,
    search_days: int = 2,
) -> dict:
    """Download a small visual quicklook entry from the newest EUMETSAT product."""
    import eumdac

    token = eumdac.AccessToken((os.environ["EUMETSAT_CONSUMER_KEY"], os.environ["EUMETSAT_CONSUMER_SECRET"]))
    datastore = eumdac.DataStore(token)
    collection = datastore.get_collection(collection_id)
    end = dt.datetime.now(dt.timezone.utc)
    products = list(collection.search(dtstart=end - dt.timedelta(days=search_days), dtend=end))
    if not products:
        return {"status": "no_data_found", "latest_date": "", "files_found": "0", "downloaded_file": "", "notes": collection_id}

    latest = max(products, key=lambda product: getattr(product, "sensing_start", None) or str(product))
    entry = next((name for name in latest.entries if entry_marker in name), None)
    if not entry:
        return {
            "status": "found",
            "latest_date": str(getattr(latest, "sensing_start", None) or latest),
            "files_found": str(len(products)),
            "downloaded_file": "",
            "notes": f"{collection_id}; quicklook entry {entry_marker!r} not present",
        }

    dest_dir = DOWNLOADS / "eumdac" / adapter.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / sanitize_filename(Path(entry).name)
    with latest.open(entry=entry) as source, dest.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
    return {
        "status": "downloaded" if dest.exists() and dest.stat().st_size else "found",
        "latest_date": str(getattr(latest, "sensing_start", None) or latest),
        "files_found": str(len(products)),
        "downloaded_file": str(dest) if dest.exists() else "",
        "notes": f"{collection_id} | {collection.title} | {entry}",
    }


def run_mtg_fci_hrfi(adapter: Adapter) -> dict:
    """Fetch the RGB quicklook from MTG FCI's 500 m HRFI imagery collection."""
    return run_eumdac_quicklook(adapter, "EO:EUM:DAT:0665", "QCK-IMAGE-RGB1", search_days=2)


def run_gportal_search_download(
    adapter: Adapter,
    dataset_path: tuple[str, ...],
    notes: str,
    search_days: int = 14,
    params: dict | None = None,
    bbox: list[float] | None = None,
    product_filter: Callable[[object], bool] | None = None,
) -> dict:
    import gportal

    gportal.username = os.environ["GPORTAL_USER"]
    gportal.password = os.environ["GPORTAL_PASSWORD"]
    datasets = gportal.datasets()
    dataset_id = datasets
    for part in dataset_path:
        dataset_id = dataset_id[part]
    dest_dir = DOWNLOADS / "gportal" / adapter.name
    dest_dir.mkdir(parents=True, exist_ok=True)

    for day in latest_day_candidates(search_days):
        start = f"{day.isoformat()}T00:00:00"
        end = f"{day.isoformat()}T23:59:59"
        try:
            kwargs = {
                "dataset_ids": dataset_id,
                "start_time": start,
                "end_time": end,
                "count": 20,
            }
            if bbox:
                kwargs["bbox"] = bbox
            if params:
                kwargs["params"] = params
            res = gportal.search(**kwargs)
            products = list(res.products())
            if product_filter:
                products = [product for product in products if product_filter(product)]
            if not products:
                continue

            day_dir = dest_dir / day.isoformat()
            day_dir.mkdir(exist_ok=True)
            existing = sorted(p for p in day_dir.iterdir() if p.is_file())
            first_file = str(existing[0]) if existing else ""
            download_note = notes
            if not first_file:
                try:
                    downloaded = gportal.download(products[:1], local_dir=str(day_dir))
                    if isinstance(downloaded, str):
                        downloaded_files = [downloaded]
                    else:
                        downloaded_files = list(downloaded or [])
                    existing = sorted(p for p in day_dir.iterdir() if p.is_file())
                    first_file = downloaded_files[0] if downloaded_files else (str(existing[0]) if existing else "")
                except Exception as download_exc:
                    download_note = f"{notes}; search ok, download failed: {type(download_exc).__name__}"
            return {
                "status": "downloaded" if first_file else "found",
                "latest_date": day.isoformat(),
                "files_found": str(len(products)),
                "downloaded_file": first_file,
                "notes": download_note,
            }
        except Exception as exc:
            msg = str(exc).lower()
            if any(token in msg for token in ["auth", "login", "password", "permission", "forbidden", "unauthorized"]):
                raise
            continue

    return {
        "status": "no_data_found",
        "latest_date": "",
        "files_found": "0",
        "downloaded_file": "",
        "notes": "searched last 14 days",
    }


def run_gportal_gcomc_l3_lst(adapter: Adapter) -> dict:
    return run_gportal_search_download(
        adapter,
        ("GCOM-C/SGLI", "LEVEL3", "Land area", "L3-LST"),
        "GCOM-C/SGLI LEVEL3 Land area L3-LST",
        params={"ProcessTimeUnit": "01D", "orbitDirection": "Descending"},
        product_filter=lambda product: product.get("mapProjection") == "EQR",
    )


def run_gportal_gcomc_l3_sst(adapter: Adapter) -> dict:
    return run_gportal_search_download(
        adapter,
        ("GCOM-C/SGLI", "LEVEL3", "Oceanic sphere", "L3-SST"),
        "GCOM-C/SGLI LEVEL3 Oceanic sphere L3-SST",
    )


def run_gportal_gcomc_l2_aod(adapter: Adapter) -> dict:
    return run_gportal_search_download(
        adapter,
        ("GCOM-C/SGLI", "LEVEL2", "Atmosphere", "L2-ARNP"),
        "GCOM-C/SGLI LEVEL2 Atmosphere L2-ARNP",
        bbox=list(AOI_ITALY_BBOX),
    )


ADAPTERS = [
    Adapter("cdse_sentinel3_lst", "01/10", "Sentinel-3 LST", (), run_s3_lstr),
    Adapter("gportal_gcomc_l3_lst", "01", "GCOM-C L3 LST", ("GPORTAL_USER", "GPORTAL_PASSWORD"), run_gportal_gcomc_l3_lst),
    Adapter("earthdata_modis_lst", "01", "MODIS LST", ("EARTHDATA_USER", "EARTHDATA_PASSWORD"), run_modis_lst),
    Adapter("cdsapi_era5_land", "01", "ERA5-Land skin temperature", ("CDS_URL", "CDS_KEY"), run_era5_land),
    Adapter("cdsapi_era5", "01", "ERA5 skin temperature", ("CDS_URL", "CDS_KEY"), run_era5_single_level),
    Adapter("cdse_sentinel3_wst", "02/11", "Sentinel-3 WST", (), run_s3_wst),
    Adapter("podaac_viirs_sst", "02", "NPP/VIIRS SST", (), run_viirs_sst),
    Adapter("gportal_gcomc_l3_sst", "02", "GCOM-C L3 SST", ("GPORTAL_USER", "GPORTAL_PASSWORD"), run_gportal_gcomc_l3_sst),
    Adapter("copernicusmarine_cmems", "02", "CMEMS-MED SST", ("CMEMS_USER", "CMEMS_PASSWORD"), run_cmems_med_sst),
    Adapter("eumdac_sarah3_dni", "03", "CM SAF SARAH-3 DNI", ("EUMETSAT_CONSUMER_KEY", "EUMETSAT_CONSUMER_SECRET"), run_sarah3_dni),
    Adapter("meteohub_mistral_radar", "04", "MISTRAL radar", (), run_mistral_radar),
    Adapter("hsaf_h40", "04", "H SAF H40B", ("EUMETSAT_CONSUMER_KEY", "EUMETSAT_CONSUMER_SECRET"), run_hsaf_h40b_datastore),
    Adapter("cdse_sentinel3_olci_snow", "05", "Sentinel-3 OLCI snow", (), run_s3_olci_snow),
    Adapter("earthaccess_viirs_snow", "05", "VIIRS snow", ("EARTHDATA_USER", "EARTHDATA_PASSWORD"), run_viirs_snow),
    Adapter("eumdac_mtg_cloudmask", "06", "MTG Cloud Mask", ("EUMETSAT_CONSUMER_KEY", "EUMETSAT_CONSUMER_SECRET"), run_mtg_cloudmask),
    Adapter("cdsapi_cams_ghg", "07/08", "CAMS GHG", ("ADS_URL", "ADS_KEY"), run_cams_ghg),
    Adapter("s5p_pal_ch4", "07", "S5P-PAL CH4", (), run_s5p_pal_ch4),
    Adapter("cmr_oco2", "08", "OCO-2", (), run_oco2),
    Adapter("cmr_oco3", "08", "OCO-3", (), run_oco3),
    Adapter("cdsapi_cams_aod", "09", "CAMS atmospheric composition forecast", ("ADS_URL", "ADS_KEY"), run_cams_aod),
    Adapter("cdse_sentinel3_synergy_aod", "09", "Sentinel-3 SYNERGY AOD", (), run_s3_synergy_aod),
    Adapter("gportal_gcomc_l2_aod", "09", "GCOM-C SGLI L2 Atmosphere ARNP", ("GPORTAL_USER", "GPORTAL_PASSWORD"), run_gportal_gcomc_l2_aod),
    Adapter("earthdata_modis_aod", "09", "MODIS AOD", ("EARTHDATA_USER", "EARTHDATA_PASSWORD"), run_modis_aod),
]


def main() -> int:
    audit_started = time.perf_counter()
    run_at_utc = now_utc()
    results_path = result_path_for_run(run_at_utc)
    config = load_config()
    loaded_providers = apply_config_to_environment(config)
    ensure_dirs(results_path)
    reset_download_dir()
    print_config_summary(loaded_providers)
    print(f"\nTemporary download folder reset: {DOWNLOADS}")
    print("\nRunning adapters")
    rows: list[dict] = []
    for adapter in ADAPTERS:
        result = adapter.run()
        row = {
            "run_at_utc": run_at_utc,
            "adapter": adapter.name,
            "product": adapter.product,
            "input_name": adapter.input_name,
            **result,
        }
        write_result(row, results_path)
        rows.append(row)
        print(f"{adapter.name:28} {row['found']:5} {row['downloaded']:9} {row['duration_seconds']:>7}s {row['latest_date']} {row['credential_status']}")
    print_product_summary(rows)
    total_seconds = time.perf_counter() - audit_started
    print(f"\nResults written to {results_path}")
    print(f"Total audit time: {total_seconds:.2f}s")
    print(f"Downloaded samples are in {DOWNLOADS}; this folder is deleted and recreated at the start of every audit run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
