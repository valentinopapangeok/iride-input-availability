# S5-02-03 hourly revalidation

`validate_hour.py` compares an hourly S5-02-03 TIFF with ERA5-Land `ssrd` on the ERA5 grid, following the supplied validation figure: both axes are in Wh/m² and `rrmse` is a fraction (e.g. 0.12 = 12%). The output consists of `matched_cells.csv` and `summary.json`.

The input CM SAF `DNIin` is direct normal irradiance in W/m². The product pipeline integrates it over an hour with `datetime_unit="h"`, giving direct normal exposure in Wh/m². ERA5-Land `ssrd` is accumulated in J/m². The validator subtracts consecutive ERA5 validity hours and divides by 3600 to obtain the energy for the product interval in Wh/m². A TIFF labelled 10:00 covers 10:00–11:00 and uses ERA5 validity hours 10:00 and 11:00. Hour 23 is rejected because the current pipeline integrates only until 23:30.

Example:

```bash
python /Users/alessandro/Desktop/Geo-K/iride-input-availability/input_availability/validation_data/03/validate_hour.py \
  --datetime 2022-08-03T10:00:00Z \
  --tiff /Users/alessandro/Desktop/Geo-K/iride-input-availability/input_availability/validation_data/03/evidence/workflow_outputs/IRIDE-S_S5-02-03_2022080310_V1.tif
```

The script uses `03/samples.csv` for a rectangular Italy extent. This is not any of the report's three VA polygons, and the default rRMSE normalization (reference mean) still needs confirmation against the report's method. Although both axes use Wh/m², DNI is direct normal radiation and SSRD is total downward radiation, so this is the requested empirical comparison rather than a comparison of identical physical quantities.

## Full-day validation on 2022-08-03

The available delivery contains 24 hourly TIFFs for 2022-08-03. `samples.csv` now lists every hour of that day. `validate_day.py` checks all 24 TIFFs, validates full-hour intervals with available ERA5 values, writes `all_hours.csv`, `ranking.csv`, `summary.json`, and per-hour matched cells, and records why other hours cannot be evaluated. The 23:00 TIFF is excluded: the pipeline integrates only 23:00–23:30 although its XML labels 23:00–00:00. A zero-reference hour has undefined rRMSE.

Run after obtaining the full-day ERA5 reference:

```bash
python /Users/alessandro/Desktop/Geo-K/iride-input-availability/input_availability/validation_data/03/validate_day.py \
  --date 2022-08-03 \
  --tiff-dir /Users/alessandro/Desktop/Geo-K/iride-input-availability/09-24-2026-17-03-30_files_list
```

The shared downloader reads all timestamps in `03/samples.csv` and can obtain the 24 ERA5 validity hours. It re-requests all eight dates in the CSV and uses existing daily filenames, so retain the backup before running it:

```bash
python /Users/alessandro/Desktop/Geo-K/iride-input-availability/input_availability/validation_data/download_ground_truth.py --products 03
```

The full-day ERA5 file contains all 24 validity hours for 2022-08-03. The complete run is recorded under `03/evidence/revalidation/20220803_full_day/`: 16 hours have a finite rRMSE, 7 nighttime hours have an undefined rRMSE because the ERA5 reference mean is zero, and hour 23 is excluded for the half-hour integration described above. `all_hours.csv` records every candidate and its status; `ranking.csv` orders the 16 finite results. The eight lowest rRMSE values average 0.28343 (28.343%), above the 0.20 (20%) threshold. These results use a rectangular Italy extent and compare different solar-radiation quantities, as noted above.
