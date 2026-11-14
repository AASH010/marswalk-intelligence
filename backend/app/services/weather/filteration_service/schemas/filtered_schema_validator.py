"""Validator for Filtered records (meda_filtered.jsonl).

Responsibility: validate individual JSONL records against
``filtered_schema.json``.  The schema file is loaded and applied as-is.  In
addition it checks the three arithmetic invariants that JSON Schema cannot
express (see ``filtered_schema.json`` ``$comment``).

This module does NOT filter, never repairs a record and never modifies its
input.  It contains no filtering rules and does not import ``rules``/``filter``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ._schema_engine import SchemaEngine, load_schema

__all__ = [
    "FILTERED_SCHEMA_PATH",
    "FILTERED_SCHEMA_VERSION",
    "FilteredSchemaError",
    "validate_record",
    "iter_record_errors",
    "validate_jsonl_file",
]

FILTERED_SCHEMA_PATH = Path(__file__).with_name("filtered_schema.json")
_SCHEMA = load_schema(FILTERED_SCHEMA_PATH)
_ENGINE = SchemaEngine(_SCHEMA)

#: Version taken from the schema's own ``$id`` (".../v1.0" -> "1.0").
FILTERED_SCHEMA_VERSION = _SCHEMA["$id"].rsplit("/v", 1)[-1]


class FilteredSchemaError(ValueError):
    """A Filtered record violates filtered_schema.json (or its invariants)."""

    def __init__(self, detail: str, *, line_number: int | None = None, field_path: str | None = None) -> None:
        self.line_number = line_number
        self.field_path = field_path or None
        self.detail = detail
        where = []
        if line_number is not None:
            where.append(f"line {line_number}")
        if field_path:
            where.append(f"field '{field_path}'")
        super().__init__(f"[{', '.join(where)}] {detail}" if where else detail)


def _invariant_errors(record: Any) -> list[tuple[str, str]]:
    """Counting invariants; only evaluated when the structure is already valid."""
    meta = record["filtering_metadata"]
    out: list[tuple[str, str]] = []
    n_meas = len(record["measurements"])
    if meta["measurements_accepted"] != n_meas:
        out.append((
            "filtering_metadata.measurements_accepted",
            f"{meta['measurements_accepted']} accepted counted but measurements holds {n_meas}",
        ))
    total = meta["measurements_accepted"] + meta["measurements_rejected"] + meta["measurements_review_required"]
    if meta["measurements_evaluated"] != total:
        out.append((
            "filtering_metadata.measurements_evaluated",
            f"evaluated={meta['measurements_evaluated']} differs from accepted+rejected+review_required={total}",
        ))
    return out


def iter_record_errors(record: Any, line_number: int | None = None) -> list[FilteredSchemaError]:
    """Return every violation of one record (empty list = valid)."""
    pairs = list(_ENGINE.iter_errors(record))
    if not pairs:
        pairs = _invariant_errors(record)
    return [FilteredSchemaError(msg, line_number=line_number, field_path=path) for path, msg in pairs]


def validate_record(record: Any, line_number: int | None = None) -> None:
    """Raise FilteredSchemaError for the first violation; return None when valid."""
    errors = iter_record_errors(record, line_number)
    if errors:
        first = errors[0]
        if len(errors) > 1:
            raise FilteredSchemaError(
                f"{first.detail} (+{len(errors) - 1} more violation(s))",
                line_number=line_number, field_path=first.field_path,
            )
        raise first


def validate_jsonl_file(path: str | Path) -> Iterator[FilteredSchemaError]:
    """Lazily validate a Filtered JSONL file; yield one error per violation."""
    def _reject(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    with Path(path).open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                record = json.loads(line, parse_constant=_reject)
            except ValueError as exc:
                yield FilteredSchemaError(f"invalid JSON: {exc}", line_number=line_number)
                continue
            yield from iter_record_errors(record, line_number)
