"""
validate_data.py

Checks that every file listed in data/manifest.yaml exists and is sane
before any processing starts.

Usage:
    python scripts/validate_data.py

Exit code 0 = all checks passed, 1 = at least one failure.
"""

import re
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifest.yaml"

CRISM_EXPECTED = {"samples": 847, "lines": 808, "bands": 3}

failures = []


def fail(msg):
    failures.append(msg)
    print(f"  FAIL  {msg}")


def ok(msg):
    print(f"  ok    {msg}")


def check_exists(key, path):
    if not path.exists():
        fail(f"{key}: missing ({path})")
        return False
    if path.is_file() and path.stat().st_size == 0:
        fail(f"{key}: empty file ({path})")
        return False
    ok(f"{key}: {path.stat().st_size:,} bytes")
    return True


def parse_hdr(path):
    text = path.read_text(errors="replace")
    out = {}
    for key in ("samples", "lines", "bands", "data type", "byte order", "interleave"):
        m = re.search(rf"^\s*{key}\s*=\s*(\S+)", text, re.I | re.M)
        if m:
            out[key] = m.group(1)
    return out


def check_crism(files):
    img = files.get("crism_image")
    hdr = files.get("crism_header")
    if not (img and hdr and img.exists() and hdr.exists()):
        return
    h = parse_hdr(hdr)
    print(f"  hdr: {h}")
    for k in ("samples", "lines", "bands"):
        if int(h.get(k, -1)) != CRISM_EXPECTED[k]:
            fail(f"crism_header: {k}={h.get(k)} expected {CRISM_EXPECTED[k]}")
    if h.get("data type") != "1":
        fail(f"crism_header: data type={h.get('data type')} (expected 1 = uint8)")
    expected = CRISM_EXPECTED["samples"] * CRISM_EXPECTED["lines"] * CRISM_EXPECTED["bands"]
    size = img.stat().st_size
    if size == expected:
        ok(f"crism_image size matches {expected:,} uint8 values")
    else:
        fail(f"crism_image: {size:,} bytes != expected {expected:,}")


def check_raster(key, path):
    try:
        import rasterio
    except ImportError:
        print(f"  skip  {key}: rasterio not installed, open-check skipped")
        return
    try:
        with rasterio.open(path) as ds:
            ok(f"{key}: {ds.width}x{ds.height}, crs={'set' if ds.crs else 'MISSING'}")
            if not ds.crs:
                fail(f"{key}: raster has no CRS")
    except Exception as e:
        fail(f"{key}: cannot open ({e})")


def main():
    manifest = yaml.safe_load(MANIFEST_PATH.read_text())
    files = {}
    print("Raw files:")
    for key, entry in manifest.get("files", {}).items():
        path = PROJECT_ROOT / entry["target"]
        files[key] = path
        if check_exists(key, path) and path.suffix.lower() in (".tif", ".tiff"):
            check_raster(key, path)

    print("\nCRISM consistency:")
    check_crism(files)

    print("\nProcessed data:")
    for key, entry in manifest.get("bulk", {}).items():
        d = PROJECT_ROOT / entry["target"]
        if d.is_dir() and any(d.iterdir()):
            ok(f"{key}: {d}")
        else:
            fail(f"{key}: missing or empty ({d})")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
