#!/usr/bin/env python3
"""Upload IRIDE input availability credentials to GitHub Actions secrets.

This script reads the local private config.json used by the audit and creates/updates
repository secrets via the GitHub CLI (`gh secret set`). It intentionally never
prints secret values.

Usage:
  python scripts/set_github_secrets_from_config.py \
    --config /Volumes/Extreme/IRIDE/MKPL/Utils/workflows/input_availability/config.json \
    --repo valentinopapangeok/iride-input-availability

Prerequisites:
  - GitHub CLI installed: https://cli.github.com/
  - Authenticated with access to the repo: gh auth login
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(
    "/Volumes/Extreme/IRIDE/MKPL/Utils/workflows/input_availability/config.json"
)
DEFAULT_REPO = "valentinopapangeok/iride-input-availability"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def provider_env(config: dict[str, Any], provider: str, key: str) -> str | None:
    providers = config.get("providers", {})
    value = providers.get(provider, {}).get("env", {}).get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def build_secret_mapping(config: dict[str, Any]) -> dict[str, str | None]:
    return {
        "ADS_KEY": provider_env(config, "ads", "ADS_KEY"),
        "CDS_KEY": provider_env(config, "cds", "CDS_KEY"),
        "CMEMS_USER": provider_env(config, "cmems", "CMEMS_USER"),
        "CMEMS_PASSWORD": provider_env(config, "cmems", "CMEMS_PASSWORD"),
        "EARTHDATA_USER": provider_env(config, "earthdata", "EARTHDATA_USER"),
        "EARTHDATA_PASSWORD": provider_env(config, "earthdata", "EARTHDATA_PASSWORD"),
        "CDSE_USER": provider_env(config, "cdse", "CDSE_USER"),
        "CDSE_PASSWORD": provider_env(config, "cdse", "CDSE_PASSWORD"),
        "EUMETSAT_CONSUMER_KEY": provider_env(
            config, "eumetsat", "EUMETSAT_CONSUMER_KEY"
        ),
        "EUMETSAT_CONSUMER_SECRET": provider_env(
            config, "eumetsat", "EUMETSAT_CONSUMER_SECRET"
        ),
        "GPORTAL_USER": provider_env(config, "gportal", "GPORTAL_USER"),
        "GPORTAL_PASSWORD": provider_env(config, "gportal", "GPORTAL_PASSWORD"),
        "METEOHUB_USER": provider_env(config, "meteohub", "METEOHUB_USER"),
        "METEOHUB_PASSWORD": provider_env(config, "meteohub", "METEOHUB_PASSWORD"),
        "METEOHUB_ARCO_ACCESS_KEY": provider_env(
            config, "meteohub", "METEOHUB_ARCO_ACCESS_KEY"
        ),
    }


def set_secret(repo: str, name: str, value: str, dry_run: bool) -> None:
    if dry_run:
        print(f"would set {name}")
        return

    result = subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo, "--body", value],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh secret set failed for {name}: {result.stderr.strip()}")
    print(f"set {name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which secret names would be uploaded without sending values.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.dry_run and shutil.which("gh") is None:
        raise SystemExit("GitHub CLI not found. Install gh and run `gh auth login` first.")

    config = load_config(args.config)
    mapping = build_secret_mapping(config)
    present = {name: value for name, value in mapping.items() if value}
    missing = sorted(name for name, value in mapping.items() if not value)

    print(f"Repository: {args.repo}")
    print(f"Config: {args.config}")
    if missing:
        print("Missing in config, skipped: " + ", ".join(missing))
    print(f"Secrets to set: {len(present)}")

    for name in sorted(present):
        set_secret(args.repo, name, present[name], args.dry_run)

    print("Done." if not args.dry_run else "Dry run complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
