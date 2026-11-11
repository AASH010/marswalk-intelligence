"""Read MEDA calibrated and derived CSV products without joining by row order."""
from __future__ import annotations
import csv, re, math
from pathlib import Path
from typing import Iterator, Any

SOL_RE = re.compile(r"(?:sol[_-]?|WE__)(\d{4})", re.I)
PRODUCT_RE = re.compile(r"(?:CAL_|DER_)(ATS|ENG|PS|RDS|RHS|TIRS|WS|ANCILLARY)", re.I)

def identify_file(path: Path) -> tuple[int, str, str] | None:
    text = str(path).replace("\\", "/")
    sol_match = SOL_RE.search(text)
    product_match = PRODUCT_RE.search(path.name)
    if not sol_match or not product_match:
        return None
    product = product_match.group(1).upper()
    source_level = "derived" if "DER_" in path.name.upper() else "calibrated"
    if product == "ANCILLARY":
        product = "ANC"
    return int(sol_match.group(1)), product, source_level

def _to_value(value: str) -> Any:
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

def iter_product_rows(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield raw product rows with stable source metadata; never infer UTC."""
    file_path = Path(path)
    identity = identify_file(file_path)
    if identity is None:
        return
    sol, product, source_level = identity
    with file_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            return
        for line_number, row in enumerate(reader, start=2):
            sclk = _to_value(row.get("SCLK", ""))
            if sclk is None:
                continue
            yield {
                "sol": sol,
                "sclk_s": int(sclk),
                "lmst": row.get("LMST") or None,
                "ltst": row.get("LTST") or None,
                "source": {
                    "source_level": source_level,
                    "product": product,
                    "source_file": file_path.name,
                    "source_row": line_number,
                },
                "raw": {key: _to_value(value or "") for key, value in row.items()
                        if key not in {"SCLK", "LMST", "LTST"}},
            }

def iter_archive_rows(root: str | Path) -> Iterator[dict[str, Any]]:
    """Recursively read CSVs under an extracted dataset directory."""
    for path in sorted(Path(root).rglob("*.CSV")) + sorted(Path(root).rglob("*.csv")):
        yield from iter_product_rows(path)
