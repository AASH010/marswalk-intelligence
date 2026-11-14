"""Data contracts and validators of the MEDA Filtering Package.

Standalone JSON artifacts (consumable without importing Python):
    normalized_schema.json   input contract  (Normalizer v2.1 output)
    filtered_schema.json     output contract (meda_filtered.jsonl)

Validators (apply the JSON files; contain no filtering logic):
    normalized_schema_validator
    filtered_schema_validator
"""
from .normalized_schema_validator import (
    NORMALIZED_SCHEMA_VERSION,
    NormalizedSchemaError,
    validate_record as validate_normalized_record,
    validate_jsonl_file as validate_normalized_jsonl,
)
from .filtered_schema_validator import (
    FILTERED_SCHEMA_VERSION,
    FilteredSchemaError,
    validate_record as validate_filtered_record,
    validate_jsonl_file as validate_filtered_jsonl,
)

__all__ = [
    "NORMALIZED_SCHEMA_VERSION", "NormalizedSchemaError",
    "validate_normalized_record", "validate_normalized_jsonl",
    "FILTERED_SCHEMA_VERSION", "FilteredSchemaError",
    "validate_filtered_record", "validate_filtered_jsonl",
]
