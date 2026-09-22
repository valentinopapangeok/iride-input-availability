# Ground-truth data provenance

This directory contains small, versioned fixtures used to reproduce IRIDE S5-02
validation runs. Provider ownership and terms remain applicable to every file.

| Product | Dataset | Provider | Attribution |
|---|---|---|---|
| 03 | ERA5-Land surface solar radiation downwards | Copernicus Climate Change Service / ECMWF | Generated using Copernicus Climate Change Service information. |
| 06 | MSG Cloud Mask | EUMETSAT | Contains modified EUMETSAT data. |
| 07 | GOSAT FTS L3 CH4 V03.05 | JAXA / NIES / MOE | Provided by JAXA/NIES/MOE. |
| 08 | GOSAT FTS L3 CO2 V03.05 | JAXA / NIES / MOE | Provided by JAXA/NIES/MOE. |

The GOSAT CH4 and CO2 files are Level-3 standard products. Article 14(3) of
the GOSAT Data Policy Revision B permits redistribution of standard products:
<https://www.gosat.nies.go.jp/eng/related/2015/download/GOSAT_Data_PolicyB_en.pdf>.

Source catalogues, requested periods and exact filenames are recorded in each
product's `requirements.json` and `ground_truth/manifest.json`. Credentials are
not included in the repository.
