"""
crism_project_to_aoi.py

Projects the CRISM browse bands onto the AOI grid (500x500, 20 m, the same
grid A* uses) and reports how much of the AOI is really covered. Also
draws the P95 masks over the CTX image so the alignment can be judged by eye.
"""

import argparse
import json
import re
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRISM_DIR = PROJECT_ROOT / "data" / "raw" / "jezero" / "science" / "crism"
DEFAULT_IMG = CRISM_DIR / "frt000047a3_07_brcarj_mtr3.img"
DEFAULT_META = PROJECT_ROOT / "data" / "processed" / "jezero" / "map_metadata.json"
DEFAULT_CTX = (PROJECT_ROOT / "data" / "raw" / "jezero" / "ctx_ortho"
               / "M20_JezeroCrater_CTXortho_mosaic_5m.tif")
DEFAULT_OUTDIR = PROJECT_ROOT / "data" / "interim"

LINES, SAMPLES, BANDS, NODATA = 808, 847, 3, 255

# Values read directly from the CRISM .LBL / PDS metadata specifications
CRISM_R = 3394839.8
CRISM_SCALE = 18.0
CRISM_CENTER_LAT = 15.0
CRISM_CENTER_LON = 0.0

CRISM_LINE_OFFSET = 61516.7555556
CRISM_SAMPLE_OFFSET = -246020.9888889


def parse_dem_crs(wkt):
    r = float(re.search(r'SPHEROID\["[^"]*",\s*([0-9.]+)', wkt).group(1))
    lat0 = float(re.search(r'standard_parallel_1",\s*([-0-9.]+)', wkt).group(1))
    return r, lat0


def aoi_cell_centres(bounds, rows, cols):
    res_x = (bounds["max_x"] - bounds["min_x"]) / cols
    res_y = (bounds["max_y"] - bounds["min_y"]) / rows
    xs = bounds["min_x"] + (np.arange(cols) + 0.5) * res_x
    ys = bounds["max_y"] - (np.arange(rows) + 0.5) * res_y
    return np.meshgrid(xs, ys)


def dem_xy_to_latlon(x, y, dem_r, dem_lat0):
    lon = np.degrees(x / (dem_r * np.cos(np.radians(dem_lat0))))
    lat = np.degrees(y / dem_r)
    return lat, lon


def latlon_to_crism_pixel(lat, lon):
    """
    Convert planetocentric latitude/longitude to CRISM
    PDS image coordinates using official offsets.
    """
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)

    phi = np.deg2rad(lat)
    lam = np.deg2rad(lon)

    phi0 = np.deg2rad(CRISM_CENTER_LAT)
    lam0 = np.deg2rad(CRISM_CENTER_LON)

    x = CRISM_R * np.cos(phi0) * (lam - lam0)
    y = CRISM_R * phi

    sample = CRISM_SAMPLE_OFFSET + x / CRISM_SCALE + 1.0
    line = CRISM_LINE_OFFSET - y / CRISM_SCALE + 1.0

    col = np.rint(sample).astype(np.int64) - 1
    row = np.rint(line).astype(np.int64) - 1

    inside = (
        (row >= 0) & (row < LINES) &
        (col >= 0) & (col < SAMPLES)
    )

    return line, sample, row, col, inside


def sample_to_aoi(data, row, col, inside):
    out = np.full(row.shape + (data.shape[0],), NODATA, dtype=np.uint8)
    r, c = row[inside], col[inside]
    for b in range(data.shape[0]):
        out[..., b][inside] = data[b, r, c]
    return out, inside


def project(data, bounds, rows, cols, dem_r, dem_lat0):
    X, Y = aoi_cell_centres(bounds, rows, cols)
    lat, lon = dem_xy_to_latlon(X, Y, dem_r, dem_lat0)
    line, sample, row, col, inside = latlon_to_crism_pixel(lat, lon)
    vals, inside_mask = sample_to_aoi(data, row, col, inside)
    return vals, inside_mask, row, col


def pct_thr(band, valid, q):
    return np.percentile(band[valid].astype(np.float64), q)


def read_ctx(ctx_path, bounds, size=1000):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds
    from rasterio.crs import CRS

    with rasterio.open(ctx_path) as ds:
        b = (bounds["min_x"], bounds["min_y"], bounds["max_x"], bounds["max_y"])
        note = "AOI and CTX assumed to share a CRS"
        if bounds.get("_wkt"):
            aoi_crs = CRS.from_wkt(bounds["_wkt"])
            if aoi_crs != ds.crs:
                b = transform_bounds(aoi_crs, ds.crs, *b)
                note = "AOI bounds reprojected to CTX CRS (bbox approx.)"
            else:
                note = "AOI and CTX CRS identical"
        win = from_bounds(*b, transform=ds.transform)
        img = ds.read(1, window=win, out_shape=(size, size),
                      resampling=Resampling.average, masked=True)
        return np.ma.filled(img.astype(np.float32), np.nan), note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", default=str(DEFAULT_IMG))
    ap.add_argument("--meta", default=str(DEFAULT_META))
    ap.add_argument("--ctx", default=str(DEFAULT_CTX))
    ap.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    ap.add_argument("--no-ctx", action="store_true")
    ap.add_argument("--pct", type=float, default=95.0)
    args = ap.parse_args()

    # Metadata metadata check test block
    test_lats = np.array([18.442814, 18.687974])
    test_lons = np.array([77.375951, 77.642025])
    lines, _, _, _, _ = latlon_to_crism_pixel(test_lats, np.full(2, 77.5))
    _, samples, _, _, _ = latlon_to_crism_pixel(np.full(2, 18.5), test_lons)
    print("Latitude bounds -> PDS lines:", lines)
    print("Longitude bounds -> PDS samples:", samples)
    resolution = (2 * np.pi * CRISM_R / (360 * CRISM_SCALE))
    print("Calculated MAP_RESOLUTION:", resolution)
    print("Label MAP_RESOLUTION:     ", 3291.7295604)

    meta = json.loads(Path(args.meta).read_text())
    bounds = dict(meta["bounds"])
    bounds["_wkt"] = meta.get("crs")
    rows, cols = meta["grid"]["rows"], meta["grid"]["cols"]
    dem_r, dem_lat0 = parse_dem_crs(meta["crs"])
    print(f"\nAOI: {rows}x{cols} @ {meta['grid']['resolution_meters']} m; "
          f"DEM sphere R={dem_r:.0f}, std parallel={dem_lat0}")

    raw = np.fromfile(args.img, dtype=np.uint8)
    assert raw.size == BANDS * LINES * SAMPLES, "unexpected CRISM size"
    data = raw.reshape(BANDS, LINES, SAMPLES)
    valid_full = (data[0] != NODATA) & (data[2] != NODATA)
    t23 = pct_thr(data[0], valid_full, args.pct)
    t19 = pct_thr(data[2], valid_full, args.pct)
    print(f"P{args.pct:g} thresholds (full image, DN): D2300={t23:.1f}, BD1900_2={t19:.1f}")

    results, grids = {}, {}
    vals, inside, row, col = project(data, bounds, rows, cols, dem_r, dem_lat0)
    valid = (vals[..., 0] != NODATA) & (vals[..., 2] != NODATA)
    m23 = valid & (vals[..., 0] >= t23)
    m19 = valid & (vals[..., 2] >= t19)
    both = m23 & m19
    union = m23 | m19
    n = rows * cols
    
    results["pds_official"] = {
        "aoi_cells": n,
        "inside_crism_rectangle_pct": float(100 * inside.mean()),
        "valid_crism_pct": float(100 * valid.mean()),
        "d2300_p_mask_cells": int(m23.sum()),
        "bd1900_p_mask_cells": int(m19.sum()),
        "shared_cells": int(both.sum()),
        "iou_in_aoi": float(both.sum() / union.sum()) if union.any() else None,
        "d2300_mask_pct_of_valid": float(100 * m23.sum() / max(valid.sum(), 1)),
        "bd1900_mask_pct_of_valid": float(100 * m19.sum() / max(valid.sum(), 1)),
    }
    
    r = results["pds_official"]
    print(f"\n[pds_official] AOI cells inside CRISM rectangle: {r['inside_crism_rectangle_pct']:.1f}% | "
          f"valid (non-255): {r['valid_crism_pct']:.1f}%")
    print(f"   D2300 mask {r['d2300_p_mask_cells']:,} cells ({r['d2300_mask_pct_of_valid']:.2f}% of valid), "
          f"BD1900_2 mask {r['bd1900_p_mask_cells']:,} ({r['bd1900_mask_pct_of_valid']:.2f}%), "
          f"shared {r['shared_cells']:,}, IoU in AOI={r['iou_in_aoi']}")

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "crism_aoi_stats.json").write_text(json.dumps(results, indent=2, default=float))
    np.savez_compressed(out / "crism_aoi_grids.npz",
                        valid=valid, d2300_mask=m23, bd1900_mask=m19,
                        d2300_dn=vals[..., 0], bd2500_dn=vals[..., 1],
                        bd1900_dn=vals[..., 2],
                        thresholds_dn=np.array([t23, t19]))

    # ---- overlay
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    extent = (bounds["min_x"], bounds["max_x"], bounds["min_y"], bounds["max_y"])
    fig, ax = plt.subplots(figsize=(9, 9), dpi=110)
    note = "no CTX background"
    if not args.no_ctx:
        try:
            ctx, note = read_ctx(args.ctx, bounds)
            lo, hi = np.nanpercentile(ctx, [2, 98])
            ax.imshow(ctx, extent=extent, cmap="gray", vmin=lo, vmax=hi, origin="upper")
        except Exception as e:
            note = f"CTX unavailable ({type(e).__name__}: {e})"
            ax.set_facecolor("#222")
    else:
        ax.set_facecolor("#222")
    print("\nBackground:", note)

    def layer(mask, rgb, alpha):
        rgba = np.zeros(mask.shape + (4,), dtype=np.float32)
        rgba[mask] = (*rgb, alpha)
        ax.imshow(rgba, extent=extent, origin="upper", interpolation="nearest")

    only23 = m23 & ~m19
    only19 = m19 & ~m23
    layer(only23, (1, 0.15, 0.15), 0.6)
    layer(only19, (0.2, 0.5, 1), 0.6)
    layer(m23 & m19, (1, 0.95, 0.1), 0.9)
    ax.contour(np.linspace(extent[0], extent[1], cols), np.linspace(extent[3], extent[2], rows),
               valid.astype(float), levels=[0.5], colors="cyan", linewidths=0.8)
    ax.set_title(f"CRISM P{args.pct:g} masks on AOI | red D2300, blue BD1900_2, "
                 f"yellow shared, cyan = valid footprint\n{note}", fontsize=8)
    ax.set_xlabel("x (m, project CRS)")
    ax.set_ylabel("y (m)")
    fig.tight_layout()
    fig.savefig(out / "crism_aoi_overlay.png")
    print(f"Saved: {out}/crism_aoi_stats.json, crism_aoi_overlay.png, crism_aoi_grids.npz")


if __name__ == "__main__":
    main()
