#!/usr/bin/env python3
"""Download only ground-truth datasets into stable per-product directories."""
from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
INPUT_AVAILABILITY = BASE.parent
sys.path.insert(0, str(INPUT_AVAILABILITY))
import latest_input_audit as audit  # noqa: E402
import product03_validation_availability as product03  # noqa: E402


def requirement(product: str) -> dict:
    return json.loads((BASE / product / "requirements.json").read_text())


def write_manifest(product: str, rows: list[dict]) -> None:
    target = BASE / product / "ground_truth" / "manifest.json"
    target.write_text(json.dumps({"product": product, "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                  "records": rows}, indent=2))
    print(f"{product}: {target}")


def download_03() -> list[dict]:
    samples = product03.load_samples(BASE / "03" / "samples.csv")
    config = INPUT_AVAILABILITY / "config.json"
    product03.load_credentials(config)
    req = requirement("03")["ground_truth"]
    paths, errors = product03.download_era5_days(
        samples,
        BASE / "03" / "ground_truth",
        req["dataset"],
        req["variable"],
    )
    days = sorted({sample.timestamp.date() for sample in samples})
    return [{"date": day.isoformat(), "status": "downloaded" if day in paths else "error",
             "file": str(paths[day].relative_to(BASE / "03")) if day in paths else "",
             "error": errors.get(day, "")} for day in days]


def download_06() -> list[dict]:
    import eumdac
    req = requirement("06")
    ground = next(item for item in req["datasets"] if item["role"] == "ground_truth")
    token = eumdac.AccessToken((os.environ["EUMETSAT_CONSUMER_KEY"], os.environ["EUMETSAT_CONSUMER_SECRET"]))
    collection = eumdac.DataStore(token).get_collection(ground["collection"])
    rows = []
    for day_text, hours in req["dates"].items():
        day = dt.date.fromisoformat(day_text)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
        products = list(collection.search(dtstart=start, dtend=start + dt.timedelta(days=1)))
        for hour in hours:
            marker = day.strftime("%Y%m%d") + f"{hour:02d}"
            candidates = [item for item in products if marker in str(item)]
            if not candidates:
                rows.append({"datetime_utc": f"{day_text}T{hour:02d}:00:00Z", "status": "missing", "file": ""})
                continue
            item = sorted(candidates, key=str)[0]
            target_dir = BASE / "06" / "ground_truth" / "msg_cloud_mask" / day_text
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / re.sub(r"[^A-Za-z0-9._,-]+", "_", str(item))
            if not target.exists():
                with item.open() as source, target.open("wb") as sink:
                    while chunk := source.read(1024 * 1024):
                        sink.write(chunk)
            rows.append({"datetime_utc": f"{day_text}T{hour:02d}:00:00Z", "status": "downloaded",
                         "file": str(target.relative_to(BASE / "06")), "bytes": target.stat().st_size})
    return rows


NIES_ROOT = "https://data2.gosat.nies.go.jp"
NIES_LOGIN = f"{NIES_ROOT}/GosatDataArchiveService/j_spring_security_check"
NIES_ENTRY = f"{NIES_ROOT}/GosatDataArchiveService/download.jsp"
NIES_DOWNLOAD = f"{NIES_ROOT}/GosatDataArchiveService/usr/download/ProductPage/download"


def nies_opener(user: str, password: str) -> urllib.request.OpenerDirector:
    """Create and authenticate a cookie-aware NIES GDAS HTTP client."""
    jar = http.cookiejar.CookieJar()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=context),
    )
    opener.addheaders = [("User-Agent", "curl/8.7.1")]
    opener.open(NIES_ENTRY, timeout=60).read()
    payload = urllib.parse.urlencode({
        "username": user,
        "j_password": password,
        "login": "Login",
    }).encode()
    request = urllib.request.Request(NIES_LOGIN, data=payload, headers={"Referer": NIES_ENTRY})
    page = opener.open(request, timeout=60).read()
    if b"GOSAT Data Download Page" not in page or b"Logout" not in page:
        raise RuntimeError("NIES authentication failed")
    return opener


def download_nies(product: str) -> list[dict]:
    req = requirement(product)
    ground = next(item for item in req["datasets"] if item["role"] == "ground_truth")
    user = os.environ.get("NIES_USER", "")
    password = os.environ.get("NIES_PASSWORD", "")
    if not user or not password:
        return [{"month": month, "status": "credentials_missing", "file": "",
                 "provider": ground["provider"], "dataset": ground["name"]} for month in req["months"]]

    code = "SWIRL3CH4" if product == "07" else "SWIRL3CO2"
    dataset_dir = "gosat_fts_l3_ch4_v03_05" if product == "07" else "gosat_fts_l3_co2_v03_05"
    opener = nies_opener(user, password)
    rows = []
    for month in req["months"]:
        compact_month = month.replace("-", "")
        filename = f"{code}_{compact_month}_V03.05.tar"
        remote_path = f"/data/GU/{code}/{month[:4]}/{filename}"
        target_dir = BASE / product / "ground_truth" / dataset_dir / month
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        if not target.exists() or target.stat().st_size == 0:
            url = NIES_DOWNLOAD + "?" + urllib.parse.urlencode({"filePath": remote_path})
            with opener.open(url, timeout=120) as source, target.open("wb") as sink:
                while chunk := source.read(1024 * 1024):
                    sink.write(chunk)
        import tarfile
        if not tarfile.is_tarfile(target):
            target.unlink(missing_ok=True)
            raise RuntimeError(f"NIES returned a non-tar response for {month}")
        rows.append({"month": month, "status": "downloaded", "file": str(target.relative_to(BASE / product)),
                     "bytes": target.stat().st_size, "provider": "NIES", "dataset": ground["name"],
                     "source_path": remote_path})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", nargs="+", choices=["03", "06", "07", "08"],
                        default=["03", "06", "07", "08"])
    args = parser.parse_args()
    audit.apply_config_to_environment(audit.load_config(INPUT_AVAILABILITY / "config.json"))
    for product in args.products:
        if product == "03":
            rows = download_03()
        elif product == "06":
            rows = download_06()
        else:
            rows = download_nies(product)
        write_manifest(product, rows)


if __name__ == "__main__":
    main()
