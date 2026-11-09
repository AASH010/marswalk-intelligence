"""
crism_p95_overlap.py

Phase 1 + Phase 2 (first step) of the science pipeline for CRISM product
frt000047a3_07_brcarj_mtr3:

  1. Print the label fields that matter (scaling, special pixels, map
     projection) so they can be checked by a human, and verify that the
     scaling factors used in the handoff really appear in the label.
  2. Reproduce the handoff numbers (valid pixels, 255 count, percentile
     thresholds, mask coverage) as a reproducibility check.
  3. P90/P95/P99 spatial overlap between D2300 and BD1900_2:
     intersection, union, IoU, Dice, overlap fractions, connected
     components of the intersection, and a translation null model.
  4. Report whether BD2500_2 carries usable signal.

Everything is computed on pixels valid (!= 255) in BOTH bands.
Percentile masks are "scaled >= percentile" over valid pixels, which is
what reproduces the handoff coverage (5.26% / 5.40% at P95).

NOT a mineral classification. P-thresholds are relative to this one image.

Usage:
    python scripts/crism_p95_overlap.py
    python scripts/crism_p95_overlap.py --img X.img --lbl X.lbl --out out.json
"""

import argparse
import json
import re
from pathlib import Path

import numpy as np
from scipy import ndimage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRISM_DIR = PROJECT_ROOT / "data" / "raw" / "jezero" / "science" / "crism"
DEFAULT_IMG = CRISM_DIR / "frt000047a3_07_brcarj_mtr3.img"
DEFAULT_LBL = CRISM_DIR / "frt000047a3_07_brcarj_mtr3.lbl"
DEFAULT_OUT = PROJECT_ROOT / "data" / "interim" / "crism_p95_overlap.json"

LINES, SAMPLES, BANDS = 808, 847, 3
NODATA = 255

# Band order in the file (BSQ). Verified against the .hdr by validate_data.py
# for geometry; names are taken from the handoff and printed for checking.
BAND_NAMES = ["D2300", "BD2500_2", "BD1900_2"]

# Scaling used in the handoff. Must be confirmed against the label.
HANDOFF_SCALING = {"D2300": 1.3358e-4, "BD2500_2": 4.3298e-5, "BD1900_2": 1.8481e-4}
HANDOFF_OFFSET = 0.0

LABEL_KEYS = re.compile(
    r"SCALING|OFFSET|MISSING|NULL|SPECIAL|INVALID|VALID_M|BAND_NAME|"
    r"MAP_PROJECTION|RADIUS|CENTER_LAT|CENTER_LON|PARALLEL|MAP_SCALE|"
    r"MAP_RESOLUTION|UPPER_LEFT|LINE_PROJECTION|SAMPLE_PROJECTION|"
    r"PROJECTION_ROTATION|COORDINATE_SYSTEM|POSITIVE_LONGITUDE|"
    r"MINIMUM_LATITUDE|MAXIMUM_LATITUDE|WESTERNMOST|EASTERNMOST|"
    r"PIXEL_AVERAGING|LINES|LINE_SAMPLES|BANDS|SAMPLE_BITS|SAMPLE_TYPE",
    re.I,
)


# ---------------------------------------------------------------- label
def inspect_label(lbl_path):
    text = Path(lbl_path).read_text(errors="replace")
    lines = [l.rstrip() for l in text.splitlines() if LABEL_KEYS.search(l)]
    print("=== Label lines of interest (verify by eye) ===")
    for l in lines:
        print("  " + l.strip())

    scaling_text = "\n".join(l for l in lines if re.search(r"SCALING|OFFSET", l, re.I))
    floats = [float(x) for x in re.findall(r"[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?", scaling_text)]
    checks = {}
    for name, f in HANDOFF_SCALING.items():
        checks[name] = any(np.isclose(v, f, rtol=1e-3) for v in floats)
    print("\n=== Handoff scaling factors found in label text ===")
    for k, v in checks.items():
        print(f"  {k}: {HANDOFF_SCALING[k]:.4e} -> {'FOUND' if v else 'NOT FOUND'}")
    return {"label_lines": [l.strip() for l in lines], "scaling_found": checks}


# ----------------------------------------------------------------- data
def load(img_path):
    raw = np.fromfile(img_path, dtype=np.uint8)
    if raw.size != BANDS * LINES * SAMPLES:
        raise ValueError(f"Unexpected size {raw.size}")
    return raw.reshape(BANDS, LINES, SAMPLES)  # BSQ


def scaled(dn, band):
    return dn.astype(np.float64) * HANDOFF_SCALING[BAND_NAMES[band]] + HANDOFF_OFFSET


def percentile_mask(dn, valid, q):
    thr = np.percentile(dn[valid].astype(np.float64), q)
    return valid & (dn >= thr), thr


def component_stats(mask):
    lab, n = ndimage.label(mask)  # 4-connectivity, same as the handoff run
    sizes = np.bincount(lab.ravel())[1:] if n else np.array([], dtype=int)
    return lab, {
        "n_regions": int(n),
        "largest": int(sizes.max()) if n else 0,
        "ge_10": int((sizes >= 10).sum()),
        "ge_50": int((sizes >= 50).sum()),
        "ge_100": int((sizes >= 100).sum()),
    }


def iou_dice(a, b, domain):
    a = a & domain
    b = b & domain
    inter = int((a & b).sum())
    union = int((a | b).sum())
    na, nb = int(a.sum()), int(b.sum())
    return {
        "n_a": na, "n_b": nb, "intersection": inter, "union": union,
        "iou": inter / union if union else float("nan"),
        "dice": 2 * inter / (na + nb) if (na + nb) else float("nan"),
        "frac_of_a": inter / na if na else float("nan"),
        "frac_of_b": inter / nb if nb else float("nan"),
    }


def translation_null(a, b, valid, n_shifts=500, min_shift=40, seed=0):
    """
    IoU of a vs. translated copies of b (no wrap-around), computed only
    where both are inside the valid footprint. Preserves the spatial
    autocorrelation of b, destroys its alignment with a.
    """
    rng = np.random.default_rng(seed)
    h, w = a.shape
    ious = []
    tries = 0
    while len(ious) < n_shifts and tries < n_shifts * 20:
        tries += 1
        dy = int(rng.integers(-h // 2, h // 2))
        dx = int(rng.integers(-w // 2, w // 2))
        if max(abs(dy), abs(dx)) < min_shift:
            continue
        ys_a = slice(max(0, dy), min(h, h + dy))
        ys_b = slice(max(0, -dy), min(h, h - dy))
        xs_a = slice(max(0, dx), min(w, w + dx))
        xs_b = slice(max(0, -dx), min(w, w - dx))
        dom = valid[ys_a, xs_a] & valid[ys_b, xs_b]
        if dom.sum() < 0.2 * valid.sum():
            continue
        aa = a[ys_a, xs_a] & dom
        bb = b[ys_b, xs_b] & dom
        u = int((aa | bb).sum())
        if u:
            ious.append(int((aa & bb).sum()) / u)
    return np.array(ious)


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", default=str(DEFAULT_IMG))
    ap.add_argument("--lbl", default=str(DEFAULT_LBL))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    report = {"product": Path(args.img).stem}
    report["label"] = inspect_label(args.lbl) if Path(args.lbl).exists() else "label missing"

    data = load(args.img)
    d23, b25, b19 = data[0], data[1], data[2]

    # ---- validity
    valid_per_band = [(data[i] != NODATA) for i in range(BANDS)]
    same_footprint = bool(all((valid_per_band[0] == v).all() for v in valid_per_band[1:]))
    valid = valid_per_band[0] & valid_per_band[2]
    print("\n=== Validity ===")
    print(f"  255 per band: {[int((data[i] == NODATA).sum()) for i in range(BANDS)]}")
    print(f"  identical NoData footprint in all bands: {same_footprint}")
    print(f"  valid in D2300 & BD1900_2: {int(valid.sum()):,}  (handoff: 468,813)")
    report["validity"] = {
        "n255_per_band": [int((data[i] == NODATA).sum()) for i in range(BANDS)],
        "same_footprint": same_footprint,
        "valid_both": int(valid.sum()),
        "dn_max_non255_per_band": [int(data[i][data[i] != NODATA].max()) for i in range(BANDS)],
    }

    # ---- BD2500_2 usability
    v = b25[valid]
    vals, counts = np.unique(v, return_counts=True)
    print("\n=== BD2500_2 ===")
    print(f"  zero fraction: {(v == 0).mean():.4f}; nonzero pixels: {int((v > 0).sum()):,}")
    print(f"  distinct DN values: {vals.size}; max DN: {int(v.max())}")
    print(f"  most common DNs: {list(zip(vals[np.argsort(-counts)][:6].tolist(), np.sort(counts)[::-1][:6].tolist()))}")
    report["bd2500_2"] = {
        "zero_fraction": float((v == 0).mean()),
        "nonzero_pixels": int((v > 0).sum()),
        "distinct_dn": int(vals.size),
        "max_dn": int(v.max()),
        "dn_histogram_top": {int(a): int(b) for a, b in zip(vals[np.argsort(-counts)][:10], np.sort(counts)[::-1][:10])},
    }

    # ---- percentile masks and overlap
    report["overlap"] = {}
    print("\n=== D2300 vs BD1900_2 overlap (valid-in-both domain) ===")
    for q in (90, 95, 99):
        m1, t1 = percentile_mask(d23, valid, q)
        m2, t2 = percentile_mask(b19, valid, q)
        _, cs1 = component_stats(m1)
        _, cs2 = component_stats(m2)
        ov = iou_dice(m1, m2, valid)
        inter = m1 & m2
        _, cs_i = component_stats(inter)

        # expected IoU if the two masks were independent (same prevalence)
        N = int(valid.sum())
        p1, p2 = ov["n_a"] / N, ov["n_b"] / N
        e_inter = p1 * p2
        e_iou = e_inter / (p1 + p2 - e_inter)

        null = translation_null(m1, m2, valid)
        pval = float((1 + (null >= ov["iou"]).sum()) / (1 + null.size)) if null.size else float("nan")

        # fraction of D2300 regions (>=10 px) touched by any BD1900_2 pixel
        lab1, n1 = ndimage.label(m1)
        sizes1 = np.bincount(lab1.ravel())[1:]
        touched = np.unique(lab1[m1 & m2])
        touched = touched[touched > 0]
        big = np.where(sizes1 >= 10)[0] + 1
        frac_regions = float(np.isin(big, touched).mean()) if big.size else float("nan")

        res = {
            "threshold_scaled": {"D2300": float(t1 * HANDOFF_SCALING["D2300"]),
                                 "BD1900_2": float(t2 * HANDOFF_SCALING["BD1900_2"])},
            "threshold_dn": {"D2300": float(t1), "BD1900_2": float(t2)},
            "coverage_pct": {"D2300": 100 * p1, "BD1900_2": 100 * p2},
            "regions_D2300": cs1, "regions_BD1900_2": cs2,
            "overlap": ov, "intersection_regions": cs_i,
            "expected_iou_if_independent": e_iou,
            "null_translation": {
                "n": int(null.size),
                "mean": float(null.mean()) if null.size else None,
                "p95": float(np.percentile(null, 95)) if null.size else None,
                "max": float(null.max()) if null.size else None,
                "empirical_p": pval,
            },
            "frac_D2300_regions_ge10_touched_by_BD1900": frac_regions,
        }
        report["overlap"][f"P{q}"] = res
        print(f"\n  P{q}: thr DN D2300={t1:.1f} BD1900_2={t2:.1f} | "
              f"coverage {100*p1:.2f}% / {100*p2:.2f}%")
        print(f"    D2300 regions {cs1['n_regions']} (>=10: {cs1['ge_10']}, >=100: {cs1['ge_100']}), "
              f"BD1900_2 regions {cs2['n_regions']} (>=10: {cs2['ge_10']}, >=100: {cs2['ge_100']})")
        print(f"    intersection={ov['intersection']:,} union={ov['union']:,} "
              f"IoU={ov['iou']:.4f} Dice={ov['dice']:.4f}")
        print(f"    overlap/D2300={ov['frac_of_a']:.4f} overlap/BD1900_2={ov['frac_of_b']:.4f}")
        print(f"    IoU if independent={e_iou:.4f}; translation null: "
              f"mean={res['null_translation']['mean']:.4f} p95={res['null_translation']['p95']:.4f} "
              f"empirical p={pval:.4f}")
        print(f"    shared regions: {cs_i['n_regions']} (>=10: {cs_i['ge_10']}, >=50: {cs_i['ge_50']}, "
              f"largest {cs_i['largest']})")
        print(f"    D2300 regions >=10px touched by BD1900_2: {frac_regions:.3f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=float))
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
