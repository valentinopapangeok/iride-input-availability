"""Run locally: python validate_month.py [--month 2024-07] [--help]."""
import argparse
import csv
import io
import json
from pathlib import Path
import tarfile

import h5py
import numpy as np
import rasterio

BASE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--month', default='2024-07')
    p.add_argument('--tiff', type=Path)
    p.add_argument('--reference', type=Path)
    p.add_argument('--field', default='Data/mixingRatio/XCH4', help='GOSAT field, e.g. Data/mixingRatio/XCH4BiasCorrected')
    p.add_argument('--normalization', choices=['mean', 'rms', 'range'], default='mean')
    p.add_argument('--aggregation', choices=['area', 'mean'], default='area')
    p.add_argument('--product-scale', type=float, default=1.0, help='Multiplier converting TIFF values to ppb')
    p.add_argument('--reference-scale', type=float, default=1000.0, help='Multiplier converting GOSAT ppm to ppb')
    p.add_argument('--min-coverage', type=float, default=0.0, help='Minimum valid TIFF area / full GOSAT cell area (0..1)')
    p.add_argument('--mask-value', type=int, default=None, help='Optional accepted GOSAT XCH4Mask value; unset retains valid field values')
    p.add_argument('--threshold', type=float, default=20.0)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    if not 0 <= args.min_coverage <= 1:
        p.error('--min-coverage must be in [0,1]')
    stamp = args.month.replace('-', '')
    tif = args.tiff or BASE / 'evidence/workflow_outputs' / f'IRIDE-S_S5-02-07_{stamp}_V1.tif'
    ref = args.reference or BASE / 'ground_truth/gosat_fts_l3_ch4_v03_05' / args.month / f'SWIRL3CH4_{stamp}_V03.05.tar'
    out = args.output or BASE / 'evidence/revalidation' / args.month / f'{args.field.split("/")[-1]}_{args.aggregation}_{args.normalization}_coverage{args.min_coverage:g}'
    with tarfile.open(ref) as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith('.h5'))
        with h5py.File(io.BytesIO(archive.extractfile(member).read())) as h:
            lat = h['Data/geolocation/latitude'][:]
            lon = h['Data/geolocation/longitude'][:]
            field = h[args.field]
            values = field[:].astype(float)
            invalid = float(np.asarray(field.attrs.get('invalidValue', -9999)).ravel()[0])
            mask = h['Data/maskInformation/XCH4Mask'][:]
    with rasterio.open(tif) as ds:
        if ds.crs.to_epsg() != 4326 or ds.transform.b or ds.transform.d or ds.transform.a <= 0 or ds.transform.e >= 0:
            raise ValueError('Expected north-up EPSG:4326 TIFF')
        data = ds.read(1, masked=True).astype(float) * args.product_scale
        valid = ~np.ma.getmaskarray(data) & np.isfinite(data.data)
        xs = ds.transform.c + np.arange(ds.width + 1) * ds.transform.a
        ys = ds.transform.f + np.arange(ds.height + 1) * ds.transform.e
    halfx = float(np.median(np.diff(lon[0]))) / 2
    halfy = abs(float(np.median(np.diff(lat[:, 0])))) / 2
    rows = []
    for i, j in np.ndindex(values.shape):
        if not np.isfinite(values[i,j]) or values[i,j] == invalid:
            continue
        if args.mask_value is not None and mask[i,j] != args.mask_value:
            continue
        west, east = lon[i,j] - halfx, lon[i,j] + halfx
        south, north = lat[i,j] - halfy, lat[i,j] + halfy
        cols = np.where((xs[:-1] < east) & (xs[1:] > west))[0]
        rr = np.where((ys[:-1] > south) & (ys[1:] < north))[0]
        if not len(cols) or not len(rr):
            continue
        # Exact overlap areas on a sphere; radius cancels in averages/coverage.
        dx = np.deg2rad(np.minimum(xs[cols+1], east) - np.maximum(xs[cols], west))
        dy = np.sin(np.deg2rad(np.minimum(ys[rr], north))) - np.sin(np.deg2rad(np.maximum(ys[rr+1], south)))
        areas = dy[:, None] * dx[None, :]
        good = valid[np.ix_(rr, cols)]
        weights = areas * good
        total = weights.sum()
        full = np.deg2rad(east-west) * (np.sin(np.deg2rad(north))-np.sin(np.deg2rad(south)))
        coverage = total / full
        if total <= 0 or coverage < args.min_coverage:
            continue
        selected = data.data[np.ix_(rr, cols)]
        if args.aggregation == 'area':
            pred = float(np.sum(selected[good] * areas[good]) / total)
        else:
            pred = float(selected[good].mean())
        truth = float(values[i,j] * args.reference_scale)
        rows.append(dict(month=args.month, latitude=float(lat[i,j]), longitude=float(lon[i,j]), product_ppb=pred, reference_ppb=truth, difference_ppb=pred-truth, coverage_fraction=float(coverage), valid_pixels=int(good.sum()), gosat_mask=int(mask[i,j])))
    if not rows:
        raise ValueError('No valid matched cells under selected settings')
    truth = np.array([r['reference_ppb'] for r in rows])
    errors = np.array([r['difference_ppb'] for r in rows])
    rmse = float(np.sqrt(np.mean(errors**2)))
    denominator = {'mean': abs(float(truth.mean())), 'rms':float(np.sqrt(np.mean(truth**2))), 'range':float(np.ptp(truth))}[args.normalization]
    relative = 100 * rmse / denominator if denominator > 0 else None
    summary = dict(month=args.month, matched_cells=len(rows), rmse_ppb=rmse, bias_ppb=float(errors.mean()), reference_mean_ppb=float(truth.mean()), normalization=args.normalization, denominator_ppb=denominator, relative_rmse_percent=relative, threshold_percent=args.threshold, preliminary_month_meets_threshold=relative <= args.threshold if relative is not None else None, settings={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}, tiff=str(tif), reference=str(ref), caveat='Single-month diagnostic. Equal weight per matched GOSAT cell; valid TIFF support may cover only part of a reference cell. Final protocol pools months. GOSAT field is interpolated L3; mask filtering disabled unless explicitly selected.')
    out.mkdir(parents=True, exist_ok=True)
    with (out/'matched_cells.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()
