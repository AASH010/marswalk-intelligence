#!/usr/bin/env python3
"""Recompute MEDA source flag counts from the original PDS CSV ZIP archives.

This script audits source files only. It does not normalize, filter, or modify data.
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import re
import zipfile
from pathlib import Path

PRODUCTS = {
    "CAL_WS": ["BOOM1_ALG_STATE_STABLE", "BOOM2_ALG_STATE_STABLE", "ROVER_STILL"],
    "CAL_RDS": ["ROVER_HGA_STILL", "ROVER_STILL"],
    "CAL_TIRS": [
        "RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV",
        "WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV",
        "SUN_OUTSIDE_TIRS_FOV",
        "ROVER_LOW_TILT",
        "TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW",
        "ROVER_HGA_OFF",
        "SKYCAM_OFF",
        "ROVER_STILL",
    ],
    "DER_WS": ["BOTH_BOOMS_USED_FOR_RETRIEVAL", "ROVER_STILL"],
    "DER_TIRS": [
        "RSM_HEAD_OUTSIDE_TIRS_UPWARD_LOOKING_FOV",
        "WHEEL_OUTSIDE_TIRS_DOWNWARD_LOOKING_FOV",
        "SUN_OUTSIDE_TIRS_FOV",
        "ROVER_LOW_TILT",
        "TIRS_GROUND_FOOTPRINT_NOT_IN_SHADOW",
        "ROVER_HGA_OFF",
        "SKYCAM_OFF",
        "ROVER_STILL",
    ],
}


def audit_zip(zip_path: Path, product_names: list[str]) -> dict:
    result = {}
    with zipfile.ZipFile(zip_path) as archive:
        for product in product_names:
            fields = PRODUCTS[product]
            counters = {field: collections.Counter() for field in fields}
            sol_rows: dict[str, int] = {}
            files = [name for name in archive.namelist() if name.endswith(".CSV") and product in name]
            total_rows = 0

            for name in sorted(files):
                match = re.search(r"WE__(\d{4})", name)
                sol = match.group(1) if match else "unknown"
                row_count = 0
                with archive.open(name) as source:
                    reader = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig", errors="replace"))
                    for row in reader:
                        total_rows += 1
                        row_count += 1
                        for field in fields:
                            value = (row.get(field) or "").strip()
                            counters[field][value if value else "<blank>"] += 1
                sol_rows[sol] = row_count

            result[product] = {
                "source_zip": zip_path.name,
                "rows": total_rows,
                "file_count": len(files),
                "sol_rows": dict(sorted(sol_rows.items())),
                "fields": {field: dict(counter) for field, counter in counters.items()},
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibrated", type=Path, required=True, help="MEDA calibrated source ZIP")
    parser.add_argument("--derived", type=Path, required=True, help="MEDA derived source ZIP")
    parser.add_argument("--output", type=Path, required=True, help="Output JSON path")
    args = parser.parse_args()

    result = {}
    result.update(audit_zip(args.calibrated, ["CAL_WS", "CAL_RDS", "CAL_TIRS"]))
    result.update(audit_zip(args.derived, ["DER_WS", "DER_TIRS"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}")
    for product, details in result.items():
        print(f"{product}: {details['rows']:,} rows across {details['file_count']} files")


if __name__ == "__main__":
    main()
