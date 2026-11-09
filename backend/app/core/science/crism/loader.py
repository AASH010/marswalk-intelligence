import json
from pathlib import Path

import numpy as np
import rasterio

from app.core.science.registry import ScienceTargetRegistry
from app.core.science.target import (
    ScienceTarget,
    SpectralEvidence,
)


PROJECT_ROOT = Path(__file__).resolve().parents[5]

CRISM_PATH = (
    PROJECT_ROOT
    / "data/processed/jezero/science/crism/crism_aoi_20m.tif"
)

METADATA_PATH = (
    PROJECT_ROOT
    / "data/processed/jezero/science/crism/metadata.json"
)


def _relative_percentile(values):
    """
    Calculate relative percentile ranks among detected candidates.

    These ranks represent relative priority, not mineral confidence.
    """
    return np.array([
        np.mean(values <= value)
        for value in values
    ])


def _load_thresholds(metadata_path):
    with open(metadata_path, "r", encoding="utf-8") as file:
        metadata = json.load(file)

    thresholds = metadata["thresholds_dn"]

    if len(thresholds) != 2:
        raise ValueError("Expected two CRISM thresholds")

    return float(thresholds[0]), float(thresholds[1])


def load_crism_targets(
    registry=None,
    raster_path=CRISM_PATH,
    metadata_path=METADATA_PATH,
):
    """
    Load CRISM spectral candidates.

    science_value is the mean of the relative percentile ranks
    of D2300 and BD1900_2 among detected candidates.

    It is NOT mineral confidence or identification probability.
    """

    if not raster_path.exists():
        raise FileNotFoundError(raster_path)

    if not metadata_path.exists():
        raise FileNotFoundError(metadata_path)

    d2300_threshold, bd1900_threshold = _load_thresholds(
        metadata_path
    )

    if registry is None:
        registry = ScienceTargetRegistry()

    with rasterio.open(raster_path) as src:

        if src.count != 4:
            raise ValueError(
                f"Expected 4 CRISM bands, got {src.count}"
            )

        d2300 = src.read(1)
        bd1900 = src.read(2)
        valid = src.read(3).astype(bool)
        candidate = src.read(4).astype(bool)

        transform = src.transform

        if np.any(candidate & ~valid):
            raise ValueError(
                "Candidate mask contains invalid observations"
            )

        rows, cols = np.where(candidate)

        if len(rows) == 0:
            return registry

        candidate_d2300 = d2300[rows, cols]
        candidate_bd1900 = bd1900[rows, cols]

        if not (
            np.isfinite(candidate_d2300).all()
            and np.isfinite(candidate_bd1900).all()
        ):
            raise ValueError(
                "Candidate cells contain invalid spectral values"
            )

        # Verify that candidate masks satisfy stored thresholds.
        if np.any(candidate_d2300 < d2300_threshold):
            raise ValueError(
                "D2300 candidate values below configured threshold"
            )

        if np.any(candidate_bd1900 < bd1900_threshold):
            raise ValueError(
                "BD1900_2 candidate values below configured threshold"
            )

        # Relative ranks among detected candidates.
        d_rank = _relative_percentile(candidate_d2300)
        b_rank = _relative_percentile(candidate_bd1900)

        scores = (d_rank + b_rank) / 2.0

        for i, (row, col) in enumerate(zip(rows, cols)):

            x, y = rasterio.transform.xy(
                transform,
                int(row),
                int(col),
                offset="center",
            )

            evidence = (
                SpectralEvidence(
                    parameter="D2300",
                    value_dn=float(candidate_d2300[i]),
                    threshold_dn=d2300_threshold,
                    relative_percentile=float(d_rank[i]),
                    passed_threshold=bool(
                        candidate_d2300[i] >= d2300_threshold
                    ),
                ),
                SpectralEvidence(
                    parameter="BD1900_2",
                    value_dn=float(candidate_bd1900[i]),
                    threshold_dn=bd1900_threshold,
                    relative_percentile=float(b_rank[i]),
                    passed_threshold=bool(
                        candidate_bd1900[i] >= bd1900_threshold
                    ),
                ),
            )

            target = ScienceTarget(
                id=f"crism_{row}_{col}",
                name=f"CRISM Spectral Candidate {row}_{col}",
                target_type="spectral_candidate",
                source="CRISM",
                science_value=float(scores[i]),
                x=float(x),
                y=float(y),
                evidence=evidence,
            )

            registry.add(target)

    return registry