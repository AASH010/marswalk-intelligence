"""Validator for Normalized records (Normalizer v2.1 output contract).

Responsibility: validate individual JSONL records against
``normalized_schema.json``.  The schema file is loaded and applied as-is; no
rule is duplicated in Python.

This module does NOT filter, never repairs a record and never modifies its
input.  It does not import the Normalizer (``normalization_service``).

Public API
----------
validate_record(record, line_number=None)   raises NormalizedSchemaError
iter_record_errors(record, line_number=None) -> list[NormalizedSchemaError]
validate_jsonl_file(path)                   yields NormalizedSchemaError per bad line
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ._schema_engine import SchemaEngine, load_schema

__all__ = [
    "NORMALIZED_SCHEMA_PATH",
    "NORMALIZED_SCHEMA_VERSION",
    "NormalizedSchemaError",
    "validate_record",
    "iter_record_errors",
    "validate_jsonl_file",
]

NORMALIZED_SCHEMA_PATH = Path(__file__).with_name("normalized_schema.json")
_SCHEMA = load_schema(NORMALIZED_SCHEMA_PATH)
_ENGINE = SchemaEngine(_SCHEMA)

#: Version taken from the schema's own ``$id`` (".../v2.1" -> "2.1").
NORMALIZED_SCHEMA_VERSION = _SCHEMA["$id"].rsplit("/v", 1)[-1]


class NormalizedSchemaError(ValueError):
    """A Normalized record violates normalized_schema.json.

    Attributes
    ----------
    line_number : 1-based JSONL line number, or None when a dict was validated directly.
    field_path  : dotted path of the offending member (``measurements.pressure_pa.value``);
                  empty string / None for the record root.
    detail      : the schema violation text without location prefix.
    """

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


def iter_record_errors(record: Any, line_number: int | None = None) -> list[NormalizedSchemaError]:
    """Return every violation of one record (empty list = valid)."""
    return [
        NormalizedSchemaError(msg, line_number=line_number, field_path=path)
        for path, msg in _ENGINE.iter_errors(record)
    ]


def validate_record(record: Any, line_number: int | None = None) -> None:
    """Raise NormalizedSchemaError for the first violation; return None when valid."""
    errors = iter_record_errors(record, line_number)
    if errors:
        first = errors[0]
        if len(errors) > 1:
            raise NormalizedSchemaError(
                f"{first.detail} (+{len(errors) - 1} more violation(s))",
                line_number=line_number, field_path=first.field_path,
            )
        raise first


def validate_jsonl_file(path: str | Path) -> Iterator[NormalizedSchemaError]:
    """Lazily validate a Normalized JSONL file; yield one error per violation.

    Invalid JSON (including NaN/Infinity constants) is reported as an error for
    its line.  The file is opened read-only and is never modified.
    """
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
                yield NormalizedSchemaError(f"invalid JSON: {exc}", line_number=line_number)
                continue
            yield from iter_record_errors(record, line_number)
