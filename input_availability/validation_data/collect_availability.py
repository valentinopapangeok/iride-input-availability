#!/usr/bin/env python3
"""Search exact validation periods for Products 06, 07 and 08."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests
import urllib3

BASE = Path(__file__).resolve().parent
INPUT_AVAILABILITY = BASE.parent
sys.path.insert(0, str(INPUT_AVAILABILITY))
import latest_input_audit as audit  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
REQUIREMENTS = {
    product: json.loads((BASE / product / "requirements.json").read_text())
    for product in ("06", "07", "08")
}


def make_row(product: str, dataset: dict, period: str, status: str, **details) -> dict:
    return {"product": product, "product_name": REQUIREMENTS[product]["name"],
            "role": dataset["role"], "dataset": dataset["name"],
            "provider": dataset["provider"], "period": period,
            "status": status, **details}


def next_month(start: dt.date) -> dt.date:
    return (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def check_eumetsat(product: str, dataset: dict, download: bool) -> list[dict]:
    import eumdac
    token = eumdac.AccessToken((os.environ["EUMETSAT_CONSUMER_KEY"], os.environ["EUMETSAT_CONSUMER_SECRET"]))
    collection = eumdac.DataStore(token).get_collection(dataset["collection"])
    rows = []
    for day_text, hours in REQUIREMENTS[product]["dates"].items():
        day = dt.date.fromisoformat(day_text)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
        products = list(collection.search(dtstart=start, dtend=start + dt.timedelta(days=1)))
        names = [str(item) for item in products]
        matched = [hour for hour in hours if any(day.strftime("%Y%m%d") + f"{hour:02d}" in name for name in names)]
        rows.append(make_row(product, dataset, day_text,
                             "available" if len(matched) == len(hours) else "partial_or_missing",
                             required_hours=hours, matched_hours=matched,
                             files_found=len(products)))
    return rows


def check_s5p(product: str, dataset: dict) -> list[dict]:
    response = requests.get("https://data-portal.s5p-pal.com/api/s5p-l3/collections/ch4/items",
                            params={"limit": 100, "filter": "l3:period='month'", "filter-lang": "cql2-text"},
                            verify=False, timeout=60)
    response.raise_for_status()
    ids = [feature.get("id", "") for feature in response.json().get("features", [])]
    rows = []
    for month in REQUIREMENTS[product]["months"]:
        matches = [item for item in ids if f"-month-{month.replace('-', '')}01-" in item]
        rows.append(make_row(product, dataset, month, "available" if matches else "missing",
                             files_found=len(matches), evidence=matches))
    return rows


def check_cmr(product: str, dataset: dict) -> list[dict]:
    version = audit.cmr_latest_collection_version(dataset["short_name"])
    rows = []
    for month in REQUIREMENTS[product]["months"]:
        start = dt.date.fromisoformat(month + "-01")
        end = next_month(start) - dt.timedelta(days=1)
        response = requests.get("https://cmr.earthdata.nasa.gov/search/granules.json",
                                params={"short_name": dataset["short_name"], "version": version,
                                        "page_size": 200, "temporal": f"{start}T00:00:00Z,{end}T23:59:59Z"},
                                verify=False, timeout=60)
        response.raise_for_status()
        entries = response.json().get("feed", {}).get("entry", [])
        rows.append(make_row(product, dataset, month, "available" if entries else "missing",
                             version=version, files_found=len(entries),
                             evidence=[entry.get("producer_granule_id", "") for entry in entries[:3]]))
    return rows


def check_ads(product: str, dataset: dict, probe: bool) -> list[dict]:
    if not probe:
        return [make_row(product, dataset, month, "probe_not_run",
                         note="Run with --probe-supporting for an exact-month ADS retrieval")
                for month in REQUIREMENTS[product]["months"]]
    import cdsapi
    client = cdsapi.Client(url=os.environ["ADS_URL"], key=os.environ["ADS_KEY"], quiet=True)
    rows = []
    for month in REQUIREMENTS[product]["months"]:
        target_dir = BASE / product / "evidence" / "ads_probes"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"cams_ghg_{dataset['variable']}_{month}.zip"
        try:
            client.retrieve("cams-global-greenhouse-gas-forecasts",
                            {"date": [month + "-01"], "leadtime_hour": ["0"],
                             "data_format": "netcdf_zip", "variable": [dataset["variable"]],
                             "area": [42.1, 12.4, 41.9, 12.6]}).download(str(target))
            rows.append(make_row(product, dataset, month,
                                 "available" if target.exists() and target.stat().st_size else "missing",
                                 downloaded_file=str(target.relative_to(BASE))))
        except Exception as exc:
            rows.append(make_row(product, dataset, month, "error",
                                 error=f"{type(exc).__name__}: {exc}"[:400]))
    return rows


def check_nies(product: str, dataset: dict) -> list[dict]:
    text = requests.get(dataset["url"], timeout=60).text
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    rows = []
    for month in REQUIREMENTS[product]["months"]:
        date = dt.date.fromisoformat(month + "-01")
        pattern = rf"{month_names[date.month - 1]}\.?\s*{date.year}[\s\S]{{0,300}}?V03\.05"
        present = bool(re.search(pattern, text, re.IGNORECASE))
        rows.append(make_row(product, dataset, month,
                             "catalogue_available" if present else "not_confirmed",
                             version="V03.05", evidence_url=dataset["url"],
                             note="NIES GDAS login required for file download"))
    return rows


def collect(products: list[str], probe_supporting: bool) -> list[dict]:
    audit.apply_config_to_environment(audit.load_config(INPUT_AVAILABILITY / "config.json"))
    rows = []
    for product in products:
        for dataset in REQUIREMENTS[product]["datasets"]:
            provider = dataset["provider"]
            if provider == "EUMETSAT":
                rows.extend(check_eumetsat(product, dataset, False))
            elif provider == "S5P-PAL":
                rows.extend(check_s5p(product, dataset))
            elif provider == "CMR":
                rows.extend(check_cmr(product, dataset))
            elif provider == "ADS":
                rows.extend(check_ads(product, dataset, probe_supporting))
            elif provider == "NIES":
                rows.extend(check_nies(product, dataset))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--products", nargs="+", choices=sorted(REQUIREMENTS), default=sorted(REQUIREMENTS))
    parser.add_argument("--probe-supporting", action="store_true")
    args = parser.parse_args()
    rows = collect(args.products, args.probe_supporting)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for product in args.products:
        selected = [row for row in rows if row["product"] == product]
        output = BASE / product / "evidence" / f"availability_{stamp}.json"
        output.write_text(json.dumps(selected, indent=2))
        print(f"Results: {output}")
        available = sum(row["status"] in {"available", "catalogue_available"} for row in selected)
        print(f"{product}: {available}/{len(selected)} checks available or catalogue-confirmed")


if __name__ == "__main__":
    main()
