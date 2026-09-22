# Validation data availability

Exact input and ground-truth requirements for S5-02 Products 03, 06, 07 and 08.
Each product owns its requirements, evidence and ground-truth directory.

The collector reuses `input_availability/config.json` and the provider helpers in
`latest_input_audit.py`. It writes no credentials to evidence files.

```bash
python input_availability/validation_data/collect_availability.py
```

Download **ground truth only** into paths intended for future revalidation:

```bash
python input_availability/validation_data/download_ground_truth.py --products 03 06 07 08
```

Operational inputs and supporting datasets are never stored here. NIES GDAS
downloads use `NIES_USER` and `NIES_PASSWORD` from the ignored local
`input_availability/config.json`. Product 07 archives are stored under
`ground_truth/gosat_fts_l3_ch4_v03_05`; Product 08 archives are stored under
`ground_truth/gosat_fts_l3_co2_v03_05`.

Alessandro's Product 03 temporal/spatial validator remains the authoritative
file-level correspondence check. Its exact validation samples are in
`product03_samples.csv`:

```bash
python input_availability/product03_validation_availability.py \
  --samples input_availability/validation_data/03/samples.csv
```
