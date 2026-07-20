#!/usr/bin/env python3
"""Download MISTRAL radar_sri_dpc data as one local GRIB per day.

Why it chunks:
MeteoHub extraction jobs first write to the user's remote quota. A full day of
radar_sri_dpc is usually ~650-760 MB, which can exceed the available remote quota.
This script submits smaller time chunks, downloads each chunk immediately, appends
it to the local daily GRIB, and deletes the remote request before continuing.

Example:
  python download_mistral_daily.py --start-date 2026-06-01 --end-date 2026-06-30
  python download_mistral_daily.py --start-date 2026-06-27 --end-date 2026-06-28 --chunk-hours 1
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
DEFAULT_OUT_DIR = BASE_DIR / "runtime_downloads" / "mistral_daily"
DEFAULT_DATASET = "radar_sri_dpc"

STOP_REQUESTED = False


def _handle_stop(signum: int, frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"\nStop requested by signal {signum}; finishing current cleanup then exiting.", flush=True)


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


def load_env(config_path: Path) -> dict[str, str]:
    cfg = json.loads(config_path.read_text())
    return cfg["providers"]["meteohub"]["env"]


def iso_z(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def daterange(start: dt.date, end_inclusive: dt.date):
    current = start
    while current <= end_inclusive:
        yield current
        current += dt.timedelta(days=1)


def login(session: requests.Session, base: str, user: str, password: str) -> dict[str, str]:
    response = session.post(f"{base}/auth/login", json={"username": user, "password": password}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    token = payload if isinstance(payload, str) else payload.get("token")
    if not token:
        raise RuntimeError("MeteoHub login succeeded but no token was returned")
    return {"Authorization": f"Bearer {token}"}


def get_usage(session: requests.Session, base: str, headers: dict[str, str]) -> dict[str, Any]:
    response = session.get(f"{base}/api/usage", headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def submit_chunk(
    session: requests.Session,
    base: str,
    headers: dict[str, str],
    dataset: str,
    start: dt.datetime,
    end: dt.datetime,
) -> int:
    name = f"codex_mistral_{dataset}_{start:%Y%m%d_%H%M}_{end:%Y%m%d_%H%M}"
    payload = {
        "request_name": name,
        "reftime": {"from": iso_z(start), "to": iso_z(end)},
        "dataset_names": [dataset],
        "only_reliable": False,
    }
    response = session.post(f"{base}/api/data", json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    request_id = response.json().get("request_id")
    if request_id is None:
        raise RuntimeError(f"Missing request_id in response: {response.text[:500]}")
    return int(request_id)


def wait_for_success(
    session: requests.Session,
    base: str,
    headers: dict[str, str],
    request_id: int,
    poll_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_item: dict[str, Any] = {}
    while time.time() < deadline:
        if STOP_REQUESTED:
            raise KeyboardInterrupt("stop requested")
        response = session.get(f"{base}/api/requests", params={"id": request_id}, headers=headers, timeout=60)
        response.raise_for_status()
        rows = response.json()
        # MeteoHub may ignore id filtering in some cases; pick the exact id if present.
        matches = [row for row in rows if row.get("id") == request_id]
        last_item = matches[0] if matches else (rows[0] if rows else {})
        status = last_item.get("status")
        if status == "SUCCESS":
            return last_item
        if status in {"FAILURE", "REVOKED"}:
            raise RuntimeError(last_item.get("error_message") or f"Request {request_id} ended as {status}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"Request {request_id} did not finish within {timeout_seconds}s; last={last_item}")


def download_file(session: requests.Session, base: str, headers: dict[str, str], fileoutput: str, dest: Path) -> int:
    with session.get(f"{base}/api/data/{fileoutput}", headers=headers, stream=True, timeout=300) as response:
        response.raise_for_status()
        bytes_written = 0
        with dest.open("ab") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if STOP_REQUESTED:
                    raise KeyboardInterrupt("stop requested")
                if chunk:
                    handle.write(chunk)
                    bytes_written += len(chunk)
        return bytes_written


def delete_request(session: requests.Session, base: str, headers: dict[str, str], request_id: int) -> None:
    response = session.delete(f"{base}/api/requests/{request_id}", headers=headers, timeout=60)
    response.raise_for_status()


def write_manifest_row(manifest: Path, row: dict[str, Any]) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    exists = manifest.exists()
    with manifest.open("a", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "date",
                "chunk_start",
                "chunk_end",
                "request_id",
                "status",
                "remote_file",
                "remote_size",
                "downloaded_bytes",
                "local_file",
                "error",
            ],
        )
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def download_day(
    session: requests.Session,
    base: str,
    headers: dict[str, str],
    dataset: str,
    day: dt.date,
    output_dir: Path,
    manifest: Path,
    chunk_hours: float,
    poll_seconds: int,
    timeout_seconds: int,
    overwrite: bool,
) -> bool:
    local_file = output_dir / f"mistral_{dataset}_{day:%Y%m%d}.grib"
    tmp_file = local_file.with_suffix(".grib.part")

    if local_file.exists() and local_file.stat().st_size > 0 and not overwrite:
        print(f"SKIP {day} existing {local_file} bytes={local_file.stat().st_size}", flush=True)
        return True

    if overwrite and local_file.exists():
        local_file.unlink()
    if tmp_file.exists():
        tmp_file.unlink()

    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.timezone.utc)
    end_of_day = start + dt.timedelta(days=1)
    current = start
    while current < end_of_day:
        if STOP_REQUESTED:
            raise KeyboardInterrupt("stop requested")
        chunk_end = min(current + dt.timedelta(hours=chunk_hours), end_of_day)
        request_id: int | None = None
        row = {
            "date": day.isoformat(),
            "chunk_start": iso_z(current),
            "chunk_end": iso_z(chunk_end),
            "request_id": "",
            "status": "",
            "remote_file": "",
            "remote_size": "",
            "downloaded_bytes": "",
            "local_file": str(local_file),
            "error": "",
        }
        try:
            request_id = submit_chunk(session, base, headers, dataset, current, chunk_end)
            row["request_id"] = str(request_id)
            item = wait_for_success(session, base, headers, request_id, poll_seconds, timeout_seconds)
            fileoutput = item.get("fileoutput")
            if not fileoutput:
                raise RuntimeError(f"SUCCESS request {request_id} has no fileoutput")
            row["remote_file"] = fileoutput
            row["remote_size"] = str(item.get("filesize") or "")
            downloaded = download_file(session, base, headers, fileoutput, tmp_file)
            row["downloaded_bytes"] = str(downloaded)
            row["status"] = "downloaded"
            print(f"{day} {current:%H:%M}-{chunk_end:%H:%M} downloaded {downloaded} bytes", flush=True)
        except KeyboardInterrupt as exc:
            row["status"] = "interrupted"
            row["error"] = str(exc)
            write_manifest_row(manifest, row)
            raise
        except Exception as exc:
            row["status"] = "error"
            row["error"] = f"{type(exc).__name__}: {exc}"
            print(f"ERROR {day} {current:%H:%M}-{chunk_end:%H:%M}: {row['error']}", flush=True)
            write_manifest_row(manifest, row)
            return False
        finally:
            if request_id is not None:
                try:
                    delete_request(session, base, headers, request_id)
                except Exception as exc:
                    row["error"] = (row["error"] + "; " if row["error"] else "") + f"delete_failed={type(exc).__name__}: {exc}"
            if row["status"] == "downloaded":
                write_manifest_row(manifest, row)

        current = chunk_end

    tmp_file.rename(local_file)
    print(f"DONE {day} {local_file} bytes={local_file.stat().st_size}", flush=True)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download MISTRAL radar_sri_dpc as one GRIB per day.")
    parser.add_argument("--start-date", required=True, type=parse_date, help="First day to download, YYYY-MM-DD.")
    parser.add_argument("--end-date", required=True, type=parse_date, help="Last day to download, YYYY-MM-DD, inclusive.")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help=f"MeteoHub dataset name. Default: {DEFAULT_DATASET}")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"Output folder. Default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"Credential config JSON. Default: {DEFAULT_CONFIG}")
    parser.add_argument("--chunk-hours", type=float, default=float(os.environ.get("MISTRAL_CHUNK_HOURS", "1")), help="Chunk size in hours. Default: 1.")
    parser.add_argument("--poll-seconds", type=int, default=int(os.environ.get("MISTRAL_POLL_SECONDS", "3")))
    parser.add_argument("--job-timeout-seconds", type=int, default=int(os.environ.get("MISTRAL_JOB_TIMEOUT_SECONDS", "600")))
    parser.add_argument("--overwrite", action="store_true", help="Overwrite completed local daily files.")
    return parser


def main() -> int:
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    args = build_parser().parse_args()
    if args.end_date < args.start_date:
        raise SystemExit("--end-date must be >= --start-date")
    if args.chunk_hours <= 0 or args.chunk_hours > 24:
        raise SystemExit("--chunk-hours must be > 0 and <= 24")

    env = load_env(args.config)
    base = env.get("METEOHUB_BASE_URL", "https://meteohub.agenziaitaliameteo.it").rstrip("/")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.output_dir / "manifest.csv"

    session = requests.Session()
    session.verify = False
    headers = login(session, base, env["METEOHUB_USER"], env["METEOHUB_PASSWORD"])
    usage = get_usage(session, base, headers)
    quota = int(usage.get("quota") or 0)
    used = int(usage.get("used") or 0)
    print(f"MeteoHub quota: used={used / 1024 / 1024:.1f} MB / quota={quota / 1024 / 1024:.1f} MB")
    print(f"Output dir: {args.output_dir}")
    print(f"Date range: {args.start_date} -> {args.end_date} inclusive; chunk_hours={args.chunk_hours}")

    for day in daterange(args.start_date, args.end_date):
        try:
            ok = download_day(
                session=session,
                base=base,
                headers=headers,
                dataset=args.dataset,
                day=day,
                output_dir=args.output_dir,
                manifest=manifest,
                chunk_hours=args.chunk_hours,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.job_timeout_seconds,
                overwrite=args.overwrite,
            )
        except KeyboardInterrupt:
            print("Stopped. Partial current-day .grib.part file was left in place for inspection.", flush=True)
            return 130
        if not ok:
            print(f"Stopped after failure on {day}. Partial current-day .grib.part file was left in place.", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
