# IRIDE input availability S5-02

Daily public dashboard for IRIDE S5 input availability and latency checks across external data providers.

## What it does

- Runs the input availability audit against public provider APIs.
- Publishes a static GitHub Pages dashboard with the latest availability table.
- Computes latency summaries from retained audit CSV history.
- Publishes only sanitized CSV/JSON/HTML outputs. Credentials and downloaded samples are never committed.

## GitHub Pages setup

1. In repository settings, enable **Pages** with source **GitHub Actions**.
2. Add the required provider credentials as repository secrets.
3. Run **Input availability dashboard** manually once from the Actions tab.
4. The workflow then runs daily via cron.

## Required secrets

Set only the secrets for providers you want to check; missing credentials are skipped by the audit.

```text
ADS_KEY
CDS_KEY
CMEMS_USER
CMEMS_PASSWORD
EARTHDATA_USER
EARTHDATA_PASSWORD
CDSE_USER
CDSE_PASSWORD
EUMETSAT_CONSUMER_KEY
EUMETSAT_CONSUMER_SECRET
GPORTAL_USER
GPORTAL_PASSWORD
METEOHUB_USER
METEOHUB_PASSWORD
METEOHUB_ARCO_ACCESS_KEY
```

Public endpoint defaults such as ADS/CDS/MeteoHub URLs are defined in the workflow.

## Local run

```bash
python -m pip install -r requirements.txt
python input_availability/latest_input_audit.py
python input_availability/build_dashboard.py --output-dir site
```

Local credentials can be provided through environment variables. A local `input_availability/config.json` is also supported but intentionally ignored by git.

## Product 03 validation availability

The dedicated Product 03 check compares the temporal and spatial availability of
CM SAF SARAH-3 DNI input data with ERA5-Land
`surface_solar_radiation_downwards` ground truth.

Create a local sample file from the tracked example, then replace its dates, UTC
times and bounding boxes:

```bash
cp input_availability/product03_samples.example.csv input_availability/product03_samples.csv
python input_availability/product03_validation_availability.py \
  --samples input_availability/product03_samples.csv
```

To inspect files already downloaded without contacting the providers again:

```bash
python input_availability/product03_validation_availability.py \
  --samples input_availability/product03_samples.csv \
  --reuse-downloads
```

Credentials, downloaded provider files, local samples and generated results are
kept local and ignored by git.

## Outputs

The generated site includes:

- `index.html`
- `latest_input_audit_results.csv`
- `latest.json`

## Safety

Do not commit:

- `input_availability/config.json`
- `input_availability/runtime_downloads/`
- `input_availability/audit_results/`
- `input_availability/product03_samples.csv`
- `input_availability/product03_validation_downloads/`
- `input_availability/product03_validation_results/`
- provider data files such as GRIB/NetCDF/HDF

## Bulk upload repository secrets

If you have the private local audit `config.json`, repository secrets can be uploaded with:

```bash
python scripts/set_github_secrets_from_config.py --dry-run
python scripts/set_github_secrets_from_config.py
```

The script requires the GitHub CLI (`gh`) to be installed and authenticated. It prints only secret names, never values.
