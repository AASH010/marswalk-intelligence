
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
    "crism_dual_band_overlay.png"
)


def normalize(values, valid):
    samples = values[valid]

    low, high = np.percentile(samples, [2, 98])

    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        raise ValueError("Invalid spectral display range")

    normalized = np.zeros(values.shape, dtype=np.float32)

    normalized[valid] = np.clip(
        (values[valid] - low) / (high - low),
        0,
        1,
    )

    return normalized, float(low), float(high)


def main():

    if not RASTER.exists():
        raise FileNotFoundError(RASTER)

    with rasterio.open(RASTER) as src:

        if src.count != 4:
            raise ValueError(
                f"Expected 4 bands, got {src.count}"
            )

        d2300 = src.read(1).astype(np.float32)
        bd1900 = src.read(2).astype(np.float32)

        valid = src.read(3).astype(bool)
        candidate = src.read(4).astype(bool)

        bounds = src.bounds

    valid &= np.isfinite(d2300) & np.isfinite(bd1900)
    candidate &= valid

    if not valid.any():
        raise ValueError("No valid CRISM pixels")

    d_norm, d_low, d_high = normalize(d2300, valid)
    b_norm, b_low, b_high = normalize(bd1900, valid)

    # RGB composite
    red = d_norm
    blue = b_norm
    green = 0.12 * np.maximum(d_norm, b_norm)

    rgb = np.stack(
        [red, green, blue],
        axis=-1,
    )

    rgba = np.zeros(
        (*valid.shape, 4),
        dtype=np.uint8,
    )

    rgba[..., :3] = np.clip(
        rgb * 255, 0, 255
    ).astype(np.uint8)

    intensity = np.maximum(d_norm, b_norm)

    rgba[..., 3] = np.where(
        valid,
        np.clip(50 + intensity * 140, 0, 255),
        0,
    ).astype(np.uint8)

    # Highlight dual-band candidate cells.
    rgba[candidate] = [255, 235, 80, 255]

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    Image.fromarray(rgba).save(OUTPUT)

    print("CRISM Dual-band Overlay generated")
    print("Output:", OUTPUT)
    print("Bounds:", bounds)
    print("Raster shape:", valid.shape)
    print("D2300 range:", d_low, d_high)
    print("BD1900_2 range:", b_low, b_high)
    print("Valid pixels:", int(valid.sum()))
    print("Candidate pixels:", int(candidate.sum()))


if __name__ == "__main__":
    main()
