"""MEDA product discovery and CSV/XML parsing (calibrated + derived).

Responsibilities of this module (and nothing else):
  * discover MEDA CSV products and their PDS4 XML labels under any root,
  * identify product / processing level / Sol from the file name,
  * parse PDS4 labels (field names, types, units, declared record counts),
  * stream CSV rows as *parsed source rows* without ever joining rows,
  * define the single SCLK representation used by every other module.

PARSER -> STREAMING DATA CONTRACT ("parsed source row")
-------------------------------------------------------
Every yielded row is a plain, JSON-serialisable ``dict``:

    {
      "sol":   int,              # from the file name (REQUIRED)
      "sclk":  str,              # canonical decimal string, see below (REQUIRED)
      "lmst":  str | None,       # source string, never reinterpreted
      "ltst":  str | None,       # source string, never reinterpreted
      "source": {                # provenance (REQUIRED, all keys present)
          "source_level":  "calibrated" | "derived",
          "product":       "ATS"|"ENG"|"PS"|"RDS"|"RHS"|"TIRS"|"WS"|"ANC",
          "source_file":   str,   # file name
          "source_row":    int,   # physical CSV line number (header = line 1)
          "release":       int | None,   # "P02" -> 2
          "original_sclk": str,   # SCLK text exactly as found in the file
      },
      "raw": {                   # original column name -> typed value
          "<COLUMN>": None | int | float | str,
      },
    }

Typing of ``raw`` values: empty cell -> ``None`` (an *empty* cell, which is not
the same as a column that is absent from ``raw``); text that is a plain integer
-> ``int``; plain finite decimal -> ``float``; everything else (``nominal``,
``NaN``, ``inf``, ...) stays ``str``. Zero is a normal value, never "missing".

SCLK: the PDS label defines SCLK as ``ASCII_Real`` with thousandths of a
second, so it is *never* converted with ``int()`` or ``float()``.  It is
carried as a canonical decimal **string** (``canonical_sclk``): no sign, no
exponent, no leading zeros, no trailing fractional zeros (``"100.0"`` and
``"100"`` are the same canonical value ``"100"``; ``"100.010"`` becomes
``"100.01"``).  ``sclk_sort_parts`` gives an exact (int, str) ordering key.

Error behaviour: file-level problems raise a ``MedaParseError`` subclass.
Row-level problems are *counted and sampled* in ``ParseStats`` (never silently
dropped) and the row is skipped, unless ``on_error="raise"`` is requested.
"""
from __future__ import annotations

import csv
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

__all__ = [
    "MedaParseError",
    "UnsupportedProductError",
    "MalformedCsvError",
    "MissingColumnError",
    "InvalidSclkError",
    "InvalidSolError",
    "XmlLabelError",
    "DataTypeError",
    "ArchiveError",
    "FieldDef",
    "ProductLabel",
    "ProductFile",
    "SkippedFile",
    "DiscoveryResult",
    "ParseStats",
    "SUPPORTED_PRODUCTS",
    "parse_sclk",
    "canonical_sclk",
    "sclk_sort_parts",
    "identify_file",
    "parse_label",
    "discover_products",
    "iter_product_rows",
    "iter_product_file_rows",
    "preflight_product",
    "iter_archive_rows",
    "safe_extract_zip",
]

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class MedaParseError(ValueError):
    """Base class; always carries the source file and (when known) row."""

    def __init__(
        self,
        message: str,
        *,
        source_file: str | Path | None = None,
        line: int | None = None,
        value: str | None = None,
    ) -> None:
        self.source_file = None if source_file is None else Path(source_file).name
        self.line = line
        self.value = value
        context = []
        if self.source_file:
            context.append(f"file={self.source_file}")
        if line is not None:
            context.append(f"line={line}")
        if value is not None:
            context.append(f"value={value!r}")
        suffix = f" [{', '.join(context)}]" if context else ""
        super().__init__(f"{message}{suffix}")


class UnsupportedProductError(MedaParseError):
    """File name is not a supported MEDA product."""


class MalformedCsvError(MedaParseError):
    """CSV structure is unusable (empty, duplicate headers, bad encoding)."""


class MissingColumnError(MedaParseError):
    """A required column (SCLK) is absent from the CSV header."""


class InvalidSclkError(MedaParseError):
    """SCLK text is missing or not a plain non-negative decimal number."""


class InvalidSolError(MedaParseError):
    """Sol could not be established from the file name / directory."""


class XmlLabelError(MedaParseError):
    """PDS4 XML label is missing, unparsable or has no field definitions."""


class DataTypeError(MedaParseError):
    """A cell contradicts the numeric data type declared by the label."""


class ArchiveError(MedaParseError):
    """ZIP archive is invalid or unsafe."""


# --------------------------------------------------------------------------
# SCLK: the one precise representation shared by the whole package
# --------------------------------------------------------------------------

_SCLK_RE = re.compile(r"^(\d+)(?:\.(\d+))?$")
_INT64_MAX = 2**63 - 1


def canonical_sclk(value: Any) -> str:
    """Return the canonical decimal string of an SCLK value (idempotent).

    Accepts ``str``, ``int`` and ``Decimal``.  ``float`` is rejected on
    purpose: a float already lost precision and must never silently become a
    matching key.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(
            "SCLK must be str, int or Decimal; float would lose precision"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite SCLK: {value!r}")
        text = format(value, "f")
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise TypeError(f"unsupported SCLK type: {type(value).__name__}")

    match = _SCLK_RE.match(text)
    if match is None:
        raise ValueError(f"not a plain non-negative decimal SCLK: {text!r}")
    whole = match.group(1).lstrip("0") or "0"
    if int(whole) > _INT64_MAX:
        raise ValueError(f"SCLK integer part out of range: {text!r}")
    fraction = (match.group(2) or "").rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def parse_sclk(
    text: str | None,
    *,
    source_file: str | Path | None = None,
    line: int | None = None,
) -> Decimal:
    """Parse SCLK text exactly into a ``Decimal`` (canonical value).

    Raises ``InvalidSclkError`` for empty/None, signs, exponents, spaces
    inside the number, ``NaN``/``Infinity`` and anything else non-plain.
    """
    if text is None or text.strip() == "":
        raise InvalidSclkError(
            "Missing SCLK value", source_file=source_file, line=line
        )
    try:
        return Decimal(canonical_sclk(text.strip()))
    except (ValueError, TypeError) as exc:
        raise InvalidSclkError(
            f"Invalid SCLK ({exc})",
            source_file=source_file,
            line=line,
            value=text,
        ) from exc


def sclk_sort_parts(canonical: str) -> tuple[int, str]:
    """Exact ordering key ``(integer_part, fraction_digits)`` of a canonical SCLK.

    Because a canonical fraction has no trailing zeros, plain string
    comparison of the fraction digits equals numeric comparison
    (``"001" < "01" < "1" < "125"``), and ``""`` (no fraction) sorts first.
    The pair also works as an exact SQLite key (INTEGER + TEXT) with no float.
    """
    whole, _, fraction = canonical_sclk(canonical).partition(".")
    return int(whole), fraction


# --------------------------------------------------------------------------
# File identification
# --------------------------------------------------------------------------

SUPPORTED_PRODUCTS = frozenset({"ATS", "ENG", "PS", "RDS", "RHS", "TIRS", "WS", "ANC"})
_PRODUCT_ALIASES = {"ANCILLARY": "ANC"}

# Sol embedded in the product filename (preferred), else a ``sol_0230`` style
# directory name.
_WE_SOL_RE = re.compile(r"WE__(\d{4,5})", re.I)
_DIR_SOL_RE = re.compile(r"(?:sol[_-]?)(\d{4,5})", re.I)
# CAL_/DER_ prefix followed by a product token (token ends at '_' or '.').
_LEVEL_PRODUCT_RE = re.compile(r"(?<![A-Za-z0-9])(CAL|DER)_([A-Za-z0-9]+)", re.I)
_RELEASE_RE = re.compile(r"_P(\d+)\.[A-Za-z]+$", re.I)


def _split_name(path: Path) -> tuple[str, str, int | None] | None:
    """Return (level, raw_product_token_upper, release) or None."""
    match = _LEVEL_PRODUCT_RE.search(path.name)
    if match is None:
        return None
    level = "calibrated" if match.group(1).upper() == "CAL" else "derived"
    release_match = _RELEASE_RE.search(path.name)
    release = int(release_match.group(1)) if release_match else None
    return level, match.group(2).upper(), release


def _sol_of(path: Path) -> int | None:
    match = _WE_SOL_RE.search(path.name)
    if match is None:
        for part in reversed(path.parts[:-1]):
            match = _DIR_SOL_RE.search(part)
            if match:
                break
    return int(match.group(1)) if match else None


def identify_file(path: str | Path) -> tuple[int, str, str] | None:
    """Return ``(sol, product, source_level)`` or ``None`` if not a MEDA product.

    Same contract as the previous package version.  ``ANCILLARY`` maps to
    ``ANC``.  Tokens that are not supported products (e.g. unknown instrument
    codes) also return ``None``; use ``discover_products`` to learn *why*.
    """
    path = Path(path)
    parts = _split_name(path)
    sol = _sol_of(path)
    if parts is None or sol is None:
        return None
    level, token, _ = parts
    product = _PRODUCT_ALIASES.get(token, token)
    if product not in SUPPORTED_PRODUCTS:
        return None
    return sol, product, level


# --------------------------------------------------------------------------
# PDS4 label parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDef:
    """One ``Field_Delimited`` entry of a PDS4 label (verbatim, not interpreted)."""

    position: int  # 1-based field_number
    name: str
    data_type: str
    unit: str | None
    description: str


@dataclass(frozen=True)
class ProductLabel:
    path: Path
    logical_identifier: str | None
    version_id: str | None
    processing_level: str | None
    start_date_time: str | None
    stop_date_time: str | None
    declared_table_records: int | None
    fields: tuple[FieldDef, ...]

    def fields_for_columns(self, columns: Iterable[str]) -> dict[str, FieldDef]:
        """Map CSV column names -> label fields.

        Matching is by *position* (label ``field_number`` == CSV column index)
        because several real labels spell a field differently from the CSV
        header (e.g. ``BOOM1_ALG_IN_STABLE_STATE`` vs ``BOOM1_ALG_STATE_STABLE``).
        Returns ``{}`` when the column count differs from the label.
        """
        columns = list(columns)
        if len(columns) != len(self.fields):
            return {}
        return {col: fld for col, fld in zip(columns, self.fields)}

    def name_mismatches(self, columns: Iterable[str]) -> list[tuple[str, str]]:
        """(csv_name, label_name) pairs that differ at the same position."""
        columns = list(columns)
        if len(columns) != len(self.fields):
            return []
        return [(c, f.name) for c, f in zip(columns, self.fields) if c != f.name]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return " ".join((child.text or "").split()) or None
    return None


def _first_text(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        if _local(element.tag) == name:
            text = " ".join((element.text or "").split())
            if text:
                return text
    return None


def parse_label(path: str | Path) -> ProductLabel:
    """Parse a PDS4 XML label. Raises ``XmlLabelError`` on any problem."""
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise XmlLabelError(f"Cannot parse XML label: {exc}", source_file=path) from exc

    fields: list[FieldDef] = []
    declared: int | None = None
    for element in root.iter():
        tag = _local(element.tag)
        if tag == "Table_Delimited":
            text = _child_text(element, "records")
            if text and text.isdigit():
                declared = int(text)
        elif tag == "Field_Delimited":
            number = _child_text(element, "field_number")
            name = _child_text(element, "name")
            if name is None or number is None or not number.isdigit():
                raise XmlLabelError(
                    "Field_Delimited without name/field_number", source_file=path
                )
            fields.append(
                FieldDef(
                    position=int(number),
                    name=name,
                    data_type=_child_text(element, "data_type") or "",
                    unit=_child_text(element, "unit"),
                    description=_child_text(element, "description") or "",
                )
            )
    if not fields:
        raise XmlLabelError("Label has no Field_Delimited definitions", source_file=path)
    fields.sort(key=lambda f: f.position)
    return ProductLabel(
        path=path,
        logical_identifier=_first_text(root, "logical_identifier"),
        version_id=_child_text(
            next((e for e in root if _local(e.tag) == "Identification_Area"), root),
            "version_id",
        ),
        processing_level=_first_text(root, "processing_level"),
        start_date_time=_first_text(root, "start_date_time"),
        stop_date_time=_first_text(root, "stop_date_time"),
        declared_table_records=declared,
        fields=tuple(fields),
    )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductFile:
    path: Path
    label_path: Path | None
    sol: int
    product: str
    source_level: str
    release: int | None

    def read_label(self) -> ProductLabel:
        if self.label_path is None:
            raise XmlLabelError("No XML label next to CSV", source_file=self.path)
        return parse_label(self.label_path)


@dataclass(frozen=True)
class SkippedFile:
    path: Path
    reason: str


@dataclass
class DiscoveryResult:
    root: Path
    csv_found: int = 0
    products: list[ProductFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    orphan_labels: list[Path] = field(default_factory=list)

    def counts_by_product(self) -> Counter:
        return Counter((p.product, p.source_level) for p in self.products)

    def skipped_reasons(self) -> Counter:
        return Counter(s.reason.split(":", 1)[0] for s in self.skipped)

    def sols(self) -> list[int]:
        return sorted({p.sol for p in self.products})


def _label_for(csv_path: Path) -> Path | None:
    for suffix in (".xml", ".XML", ".Xml"):
        candidate = csv_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def discover_products(
    root: str | Path,
    *,
    sols: tuple[int, int] | None = None,
) -> DiscoveryResult:
    """Recursively discover MEDA CSV products under ``root``.

    Nothing is dropped silently: every CSV that is not returned as a product
    appears in ``skipped`` with a reason (``not_meda_product``,
    ``unsupported_product:<TOKEN>``, ``invalid_sol``, ``outside_sol_range``,
    ``superseded_release``).  If several releases of the same
    (Sol, product, level) exist, only the highest release is kept.

    Order is deterministic: (sol, level, product, file name).
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Dataset root is not a directory: {root_path}")

    result = DiscoveryResult(root=root_path)
    csv_files = sorted(
        p for p in root_path.rglob("*") if p.is_file() and p.suffix.lower() == ".csv"
    )
    result.csv_found = len(csv_files)

    candidates: dict[tuple[int, str, str], list[ProductFile]] = {}
    for path in csv_files:
        parts = _split_name(path)
        if parts is None:
            result.skipped.append(SkippedFile(path, "not_meda_product"))
            continue
        level, token, release = parts
        product = _PRODUCT_ALIASES.get(token, token)
        if product not in SUPPORTED_PRODUCTS:
            result.skipped.append(SkippedFile(path, f"unsupported_product:{token}"))
            continue
        sol = _sol_of(path)
        if sol is None:
            result.skipped.append(SkippedFile(path, "invalid_sol"))
            continue
        if sols is not None and not (sols[0] <= sol <= sols[1]):
            result.skipped.append(SkippedFile(path, "outside_sol_range"))
            continue
        candidates.setdefault((sol, product, level), []).append(
            ProductFile(path, _label_for(path), sol, product, level, release)
        )

    for key in sorted(candidates):
        group = sorted(candidates[key], key=lambda p: (p.release or -1, p.path.name))
        result.products.append(group[-1])
        for superseded in group[:-1]:
            result.skipped.append(SkippedFile(superseded.path, "superseded_release"))

    result.products.sort(key=lambda p: (p.sol, p.source_level, p.product, p.path.name))

    labelled = {p.label_path for p in result.products if p.label_path}
    result.orphan_labels = sorted(
        x
        for x in root_path.rglob("*")
        if x.is_file() and x.suffix.lower() == ".xml" and x not in labelled
        and not x.with_suffix(".CSV").is_file() and not x.with_suffix(".csv").is_file()
    )
    return result


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_NUMERIC_TYPES = frozenset({"ASCII_Real", "ASCII_Integer"})


def _to_value(value: str) -> Any:
    """Typed cell value: None (empty) | int | float | str (see module docs)."""
    value = value.strip()
    if value == "":
        return None
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        number = float(value)
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    return value


@dataclass
class ParseStats:
    """Mutable counters shared across files so nothing is lost silently."""

    files: int = 0
    rows_read: int = 0
    rows_yielded: int = 0
    rows_rejected: int = 0
    rejections: Counter = field(default_factory=Counter)
    rejection_samples: list[dict] = field(default_factory=list)
    warnings: Counter = field(default_factory=Counter)
    warning_samples: list[str] = field(default_factory=list)
    label_declared_records: dict[str, int | None] = field(default_factory=dict)
    rows_by_file: dict[str, int] = field(default_factory=dict)
    rows_read_by_file: Counter = field(default_factory=Counter)
    rows_rejected_by_file: Counter = field(default_factory=Counter)
    rejections_by_file: dict[str, Counter] = field(default_factory=dict)

    MAX_SAMPLES = 25

    def reject(self, reason: str, file: Path, line: int, detail: str = "") -> None:
        self.rows_rejected += 1
        self.rejections[reason] += 1
        self.rows_rejected_by_file[file.name] += 1
        self.rejections_by_file.setdefault(file.name, Counter())[reason] += 1
        if len(self.rejection_samples) < self.MAX_SAMPLES:
            self.rejection_samples.append(
                {"reason": reason, "source_file": file.name, "line": line, "detail": detail}
            )

    def warn(self, kind: str, detail: str = "") -> None:
        self.warnings[kind] += 1
        if detail and len(self.warning_samples) < self.MAX_SAMPLES:
            self.warning_samples.append(f"{kind}: {detail}")


def iter_product_rows(
    path: str | Path,
    *,
    stats: ParseStats | None = None,
    on_error: str = "reject",
    label: ProductLabel | None = None,
    use_label: bool = True,
) -> Iterator[dict[str, Any]]:
    """Stream parsed source rows (see module docstring) from one CSV file.

    ``on_error``: ``"reject"`` (default) counts/samples bad rows in ``stats``
    and continues; ``"raise"`` raises the specific ``MedaParseError``.
    File-level problems always raise.  If ``label`` is omitted and a sibling
    XML exists it is parsed and cross-checked (declared record count, field
    positions) - any difference is recorded in ``stats.warnings``.
    """
    if on_error not in ("reject", "raise"):
        raise ValueError("on_error must be 'reject' or 'raise'")
    file_path = Path(path)
    stats = stats if stats is not None else ParseStats()

    identity = identify_file(file_path)
    if identity is None:
        raise UnsupportedProductError(
            "Not a supported MEDA product file name", source_file=file_path
        )
    sol, product, level = identity
    parts = _split_name(file_path)
    release = parts[2] if parts else None

    if label is None and use_label:
        label_path = _label_for(file_path)
        if label_path is not None:
            label = parse_label(label_path)

    stats.files += 1
    rows_in_file = 0
    rows_seen = 0
    sol_text = f"{sol:05d}"

    try:
        stream = file_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise MalformedCsvError(f"Cannot open CSV: {exc}", source_file=file_path) from exc

    with stream:
        reader = csv.reader(stream)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise MalformedCsvError("Empty CSV file", source_file=file_path) from None
        except (csv.Error, UnicodeDecodeError) as exc:
            raise MalformedCsvError(f"Unreadable header: {exc}", source_file=file_path) from exc

        if len(set(header)) != len(header):
            raise MalformedCsvError("Duplicate column names in header", source_file=file_path)
        if "SCLK" not in header:
            raise MissingColumnError("Required column SCLK is missing", source_file=file_path, line=1)

        sclk_i = header.index("SCLK")
        lmst_i = header.index("LMST") if "LMST" in header else None
        ltst_i = header.index("LTST") if "LTST" in header else None
        skip = {sclk_i, lmst_i, ltst_i}
        data_cols = [(i, name) for i, name in enumerate(header) if i not in skip]

        numeric_cols: set[str] = set()
        if label is not None:
            by_col = label.fields_for_columns(header)
            if not by_col:
                stats.warn(
                    "label_column_count_mismatch",
                    f"{file_path.name}: csv={len(header)} label={len(label.fields)}",
                )
            else:
                for csv_name, label_name in label.name_mismatches(header):
                    stats.warn(
                        "label_name_mismatch",
                        f"{file_path.name}: csv {csv_name!r} vs label {label_name!r}",
                    )
                numeric_cols = {c for c, f in by_col.items() if f.data_type in _NUMERIC_TYPES}
            stats.label_declared_records[file_path.name] = label.declared_table_records
            if label.processing_level and label.processing_level.lower() != level:
                raise XmlLabelError(
                    f"Label processing_level {label.processing_level!r} contradicts "
                    f"file-name level {level!r}",
                    source_file=file_path,
                )

        base_source = {
            "source_level": level,
            "product": product,
            "source_file": file_path.name,
            "release": release,
        }

        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except (csv.Error, UnicodeDecodeError) as exc:
                raise MalformedCsvError(
                    f"CSV decoding error: {exc}", source_file=file_path, line=reader.line_num
                ) from exc

            line = reader.line_num
            stats.rows_read += 1
            stats.rows_read_by_file[file_path.name] += 1
            rows_seen += 1

            def _fail(error: MedaParseError, reason: str) -> bool:
                if on_error == "raise":
                    raise error
                stats.reject(reason, file_path, line, str(error))
                return True

            if not row or all(cell.strip() == "" for cell in row):
                _fail(MalformedCsvError("Blank CSV line", source_file=file_path, line=line), "blank_line")
                continue
            if len(row) != len(header):
                _fail(
                    MalformedCsvError(
                        f"Expected {len(header)} fields, found {len(row)}",
                        source_file=file_path,
                        line=line,
                    ),
                    "field_count_mismatch",
                )
                continue

            sclk_text = row[sclk_i].strip()
            try:
                sclk = canonical_sclk(parse_sclk(sclk_text, source_file=file_path, line=line))
            except InvalidSclkError as exc:
                _fail(exc, "missing_sclk" if sclk_text == "" else "invalid_sclk")
                continue

            lmst = (row[lmst_i].strip() or None) if lmst_i is not None else None
            ltst = (row[ltst_i].strip() or None) if ltst_i is not None else None
            if lmst and lmst[:5].isdigit() and lmst[5:6] == "M" and lmst[:5] != sol_text:
                stats.warn("lmst_sol_differs_from_file_sol", f"{file_path.name} line {line}")

            raw: dict[str, Any] = {}
            for index, name in data_cols:
                value = _to_value(row[index])
                if type(value) is str and name in numeric_cols:
                    error = DataTypeError(
                        f"Non-numeric text in numeric field {name}",
                        source_file=file_path,
                        line=line,
                        value=value,
                    )
                    if on_error == "raise":
                        raise error
                    stats.warn("non_numeric_in_numeric_field", f"{file_path.name} {name}")
                raw[name] = value

            source = dict(base_source)
            source["source_row"] = line
            source["original_sclk"] = sclk_text

            rows_in_file += 1
            stats.rows_yielded += 1
            yield {
                "sol": sol,
                "sclk": sclk,
                "lmst": lmst,
                "ltst": ltst,
                "source": source,
                "raw": raw,
            }

    stats.rows_by_file[file_path.name] = rows_in_file
    declared = stats.label_declared_records.get(file_path.name)
    if declared is not None and rows_seen != declared:
        # PDS Table_Delimited "records" excludes the header line.
        stats.warn(
            "rows_differ_from_label_declared_records",
            f"{file_path.name}: csv_data_rows={rows_seen} declared={declared}",
        )


def iter_product_file_rows(
    product_file: ProductFile, *, stats: ParseStats | None = None, on_error: str = "reject"
) -> Iterator[dict[str, Any]]:
    """Like ``iter_product_rows`` but for a discovered ``ProductFile``."""
    label = product_file.read_label() if product_file.label_path else None
    yield from iter_product_rows(
        product_file.path, stats=stats, on_error=on_error, label=label
    )


def iter_archive_rows(
    root: str | Path,
    *,
    sols: tuple[int, int] | None = None,
    stats: ParseStats | None = None,
    on_error: str = "reject",
    discovery: DiscoveryResult | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed source rows for every recognised product under ``root``.

    Works for any directory layout (``sol_0230/`` folders, ``derived/<PROD>/``
    folders, or a mix).  Rows are produced file by file in the deterministic
    discovery order and are never merged here.
    """
    found = discovery if discovery is not None else discover_products(root, sols=sols)
    stats = stats if stats is not None else ParseStats()
    for product_file in found.products:
        yield from iter_product_file_rows(product_file, stats=stats, on_error=on_error)


# --------------------------------------------------------------------------
# Product pre-flight (isolation support)
# --------------------------------------------------------------------------


def preflight_product(product_file: "ProductFile") -> dict:
    """Cheap whole-file check run BEFORE a product's rows enter staging.

    Raises the specific ``MedaParseError`` for anything that would make the
    product unreadable or uninterpretable (missing/unparsable label, label
    contradicting the file name, empty file, duplicate/missing SCLK header,
    undecodable bytes anywhere in the file).  Because the whole file is decoded
    here, a product that passes cannot fail half-way through the real parse,
    which is what makes excluding a damaged product *safe* (no half-staged data).

    Returns ``{"physical_rows": int, "header_columns": int}`` (data lines,
    header excluded).
    """
    path = product_file.path
    if product_file.label_path is None:
        raise XmlLabelError("No XML label next to CSV: units/types cannot be verified", source_file=path)
    label = parse_label(product_file.label_path)
    level = product_file.source_level
    if label.processing_level and label.processing_level.lower() != level:
        raise XmlLabelError(
            f"Label processing_level {label.processing_level!r} contradicts file-name level {level!r}",
            source_file=path,
        )
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise MalformedCsvError(f"Cannot open CSV: {exc}", source_file=path) from exc
    with stream:
        reader = csv.reader(stream)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise MalformedCsvError("Empty CSV file", source_file=path) from None
        except (csv.Error, UnicodeDecodeError) as exc:
            raise MalformedCsvError(f"Unreadable header: {exc}", source_file=path) from exc
        if len(set(header)) != len(header):
            raise MalformedCsvError("Duplicate column names in header", source_file=path)
        if "SCLK" not in header:
            raise MissingColumnError("Required column SCLK is missing", source_file=path, line=1)
        rows = 0
        try:
            for _ in reader:
                rows += 1
        except (csv.Error, UnicodeDecodeError) as exc:
            raise MalformedCsvError(f"CSV decoding error: {exc}", source_file=path, line=reader.line_num) from exc
    if not label.fields_for_columns(header):
        raise XmlLabelError(
            f"Label field count {len(label.fields)} differs from CSV column count {len(header)}", source_file=path
        )
    return {"physical_rows": rows, "header_columns": len(header)}


# --------------------------------------------------------------------------
# ZIP handling
# --------------------------------------------------------------------------


def safe_extract_zip(zip_path: str | Path, destination: str | Path, *, verify: bool = True) -> Path:
    """Extract ``zip_path`` into ``destination`` rejecting unsafe members.

    Raises ``ArchiveError`` for corrupt archives (CRC test when ``verify``),
    absolute paths and ``..`` traversal.
    """
    zip_path = Path(zip_path)
    destination = Path(destination)
    if not zip_path.is_file() or not zipfile.is_zipfile(zip_path):
        raise ArchiveError("Not a valid ZIP archive", source_file=zip_path)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if verify:
                bad = archive.testzip()
                if bad is not None:
                    raise ArchiveError(f"Corrupt member {bad!r}", source_file=zip_path)
            names = [m.filename for m in archive.infolist() if not m.is_dir()]
            lowered = Counter(n.lower() for n in names)
            clashes = sorted({n for n in names if lowered[n.lower()] > 1})
            if clashes:
                raise ArchiveError(
                    f"Duplicate (or case-colliding) archive paths: {clashes[:5]!r}", source_file=zip_path
                )
            for member in archive.infolist():
                if "\\" in member.filename or "\x00" in member.filename or re.match(r"^[A-Za-z]:", member.filename):
                    raise ArchiveError(
                        f"Unsafe path in archive: {member.filename!r}", source_file=zip_path
                    )
                posix = PurePosixPath(member.filename)
                if posix.is_absolute() or ".." in posix.parts:
                    raise ArchiveError(
                        f"Unsafe path in archive: {member.filename!r}", source_file=zip_path
                    )
                target = (root / Path(*posix.parts)).resolve() if posix.parts else root
                if root != target and root not in target.parents:
                    raise ArchiveError(
                        f"Path escapes destination: {member.filename!r}", source_file=zip_path
                    )
            archive.extractall(root)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Bad ZIP: {exc}", source_file=zip_path) from exc
    return root
