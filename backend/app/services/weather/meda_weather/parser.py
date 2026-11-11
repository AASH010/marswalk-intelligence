
"""Read MEDA calibrated and derived CSV products without joining by row order."""
from __future__ import annotations

import csv
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


# Prefer Sol embedded in the product filename, e.g. WE__0230...
# Fall back to a Sol directory name only when the filename has no Sol.
WE_SOL_RE = re.compile(r"WE__(\d{4})", re.I)
SOL_RE = re.compile(r"(?:sol[_-]?)(\d{4})", re.I)

PRODUCT_RE = re.compile(
    r"(?:CAL_|DER_)(ATS|ENG|PS|RDS|RHS|TIRS|WS|ANCILLARY)",
    re.I,
)


def identify_file(path: Path) -> tuple[int, str, str] | None:
    """Identify Sol, MEDA product, and source level from a product filename."""
    filename = path.name

    sol_match = WE_SOL_RE.search(filename)

    if sol_match is None:
        # Search directory components from nearest to farthest.
        for part in reversed(path.parts[:-1]):
            sol_match = SOL_RE.search(part)
            if sol_match:
                break

    product_match = PRODUCT_RE.search(filename)

    if sol_match is None or product_match is None:
        return None

    product = product_match.group(1).upper()
    source_level = (
        "derived" if "DER_" in filename.upper() else "calibrated"
    )

    if product == "ANCILLARY":
        product = "ANC"

    return int(sol_match.group(1)), product, source_level


def _to_value(value: str) -> Any:
    """Convert CSV values to numbers when possible; preserve other text."""
    value = value.strip()

    if value == "":
        return None

    try:
        number = float(value)
    except ValueError:
        return value

    if not math.isfinite(number):
        return None

    if number.is_integer():
        return int(number)

    return number


def _parse_sclk(
    value: str | None,
    file_path: Path,
    line_number: int,
) -> int | None:
    """
    Parse SCLK as an exact integer.

    Decimal is used here instead of float so a large integer SCLK cannot
    lose precision during parsing.
    """
    if value is None or value.strip() == "":
        return None

    try:
        parsed = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(
            f"Invalid SCLK in {file_path.name}, CSV line "
            f"{line_number}: {value!r}"
        ) from exc

    if not parsed.is_finite():
        raise ValueError(
            f"Non-finite SCLK in {file_path.name}, CSV line "
            f"{line_number}: {value!r}"
        )

    if parsed != parsed.to_integral_value():
        raise ValueError(
            f"Fractional SCLK would be truncated in {file_path.name}, "
            f"CSV line {line_number}: {value!r}"
        )

    return int(parsed)


def iter_product_rows(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield raw product rows with stable source metadata; never infer UTC."""
    file_path = Path(path)
    identity = identify_file(file_path)

    if identity is None:
        return

    sol, product, source_level = identity

    with file_path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        reader = csv.DictReader(stream)

        if not reader.fieldnames:
            return

        for line_number, row in enumerate(reader, start=2):
            sclk = _parse_sclk(
                row.get("SCLK"),
                file_path,
                line_number,
            )

            # Rows without SCLK cannot participate in exact-SCLK grouping.
            if sclk is None:
                continue

            yield {
                "sol": sol,
                "sclk_s": sclk,
                "lmst": row.get("LMST") or None,
                "ltst": row.get("LTST") or None,
                "source": {
                    "source_level": source_level,
                    "product": product,
                    "source_file": file_path.name,
                    "source_row": line_number,
                },
                "raw": {
                    key: _to_value(value or "")
                    for key, value in row.items()
                    if key not in {"SCLK", "LMST", "LTST"}
                    and key is not None
                },
            }



def iter_archive_rows(root):
    root = Path(root)

    for folder_name in ("calibrated", "derived"):
        folder = root / folder_name

        if not folder.is_dir():
            raise FileNotFoundError(
                f"Required dataset folder not found: {folder}"
            )

        paths = sorted(
            path for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() == ".csv"
        )

        for path in paths:
            yield from iter_product_rows(path)