
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]

RASTER = (
    ROOT / "data/processed/jezero/science/crism/"
    "crism_aoi_20m.tif"
)

OUTPUT = (
    ROOT / "data/processed/jezero/science/crism/"
    "crism_d2300_overlay.png"
)


def main():
    with rasterio.open(RASTER) as src:
        d2300 = src.read(1)
        valid = src.read(3).astype(bool)

        bounds = src.bounds

    
    valid_values = d2300[valid]

    low = np.percentile(valid_values, 2)
    high = np.percentile(valid_values, 98)

    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        raise ValueError("Invalid CRISM display range")

    # Normalize valid pixels only.
    normalized = np.zeros(d2300.shape, dtype=np.float32)

    normalized[valid] = np.clip(
        (d2300[valid] - low) / (high - low),
        0,
        1,
    )

    rgba = np.zeros((*d2300.shape, 4), dtype=np.uint8)

    intensity = np.round(normalized * 255).astype(np.uint8)

    rgba[..., 0] = intensity
    rgba[..., 1] = intensity
    rgba[..., 2] = intensity

    rgba[..., 3] = np.where(valid, 180, 0).astype(np.uint8)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    Image.fromarray(rgba).save(OUTPUT)


    print("CRISM overlay generated")
    print("Output:", OUTPUT)
    print("Display range:", low, high)
    print("Valid pixels:", valid.sum())


if __name__ == "__main__":
    main()
