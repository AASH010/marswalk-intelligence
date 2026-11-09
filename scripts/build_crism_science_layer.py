
from pathlib import Path
import json

import numpy as np
import rasterio


ROOT = Path(__file__).resolve().parents[1]

NPZ_PATH = ROOT / "data/interim/crism_aoi_grids.npz"

DEM_PATH = (
    ROOT / "data/processed/jezero/terrain/"
    "jezero_aoi_ctx_dem_20m.tif"
)

OUT_DIR = ROOT / "data/processed/jezero/science/crism"

OUT_PATH = OUT_DIR / "crism_aoi_20m.tif"


def main():

    if not NPZ_PATH.exists():
        raise FileNotFoundError(NPZ_PATH)

    if not DEM_PATH.exists():
        raise FileNotFoundError(DEM_PATH)

    data = np.load(NPZ_PATH)

    required = {
        "valid",
        "d2300_mask",
        "bd1900_mask",
        "d2300_dn",
        "bd1900_dn",
    }

    missing = required - set(data.files)

    if missing:
        raise ValueError(f"Missing NPZ arrays: {missing}")

    valid = data["valid"].astype(bool)
    d2300 = data["d2300_dn"].astype(np.float32)
    bd1900 = data["bd1900_dn"].astype(np.float32)

    candidate = (
        data["d2300_mask"].astype(bool)
        & data["bd1900_mask"].astype(bool)
        & valid
    )

    with rasterio.open(DEM_PATH) as dem:

        profile = dem.profile.copy()
        dem_shape = dem.shape

    if valid.shape != dem_shape:
        raise ValueError(
            f"Grid mismatch: CRISM {valid.shape}, "
            f"DEM {dem_shape}"
        )

    # NoData for pixels without valid observations.
    d2300[~valid] = np.nan
    bd1900[~valid] = np.nan

    output = np.stack([
        d2300,
        bd1900,
        valid.astype(np.float32),
        candidate.astype(np.float32),
    ])

    profile.update(
        driver="GTiff",
        count=4,
        dtype="float32",
        nodata=np.nan,
        compress="deflate",
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with rasterio.open(OUT_PATH, "w", **profile) as dst:

        dst.write(output)

        dst.set_band_description(1, "D2300_DN")
        dst.set_band_description(2, "BD1900_2_DN")
        dst.set_band_description(3, "VALID_MASK")
        dst.set_band_description(4, "CANDIDATE_MASK")

    metadata = {
        "layer": "CRISM spectral signals",
        "source": "frt000047a3_07_brcarj_mtr3.img",
        "grid": "Jezero AOI 500x500 at 20m",
        "bands": {
            "1": "D2300_DN",
            "2": "BD1900_2_DN",
            "3": "VALID_MASK",
            "4": "CANDIDATE_MASK",
        },
        "candidate_definition": (
            "D2300 P95 mask AND BD1900_2 P95 mask "
            "AND valid observation"
        ),
        "thresholds_dn": data["thresholds_dn"].tolist(),
        "valid_cells": int(valid.sum()),
        "candidate_cells": int(candidate.sum()),
        "candidate_pct_of_valid": float(
            100 * candidate.sum() / max(valid.sum(), 1)
        ),
        "scientific_status": "Candidate spectral signal; "
        "not confirmed mineral identification",
    }

    (OUT_DIR / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    print("CRISM Science Layer created")
    print(f"Output: {OUT_PATH}")
    print(f"Valid cells: {valid.sum()}")
    print(f"Candidate cells: {candidate.sum()}")
    print(f"Coverage: {100 * valid.mean():.2f}%")


if __name__ == "__main__":
    main()
