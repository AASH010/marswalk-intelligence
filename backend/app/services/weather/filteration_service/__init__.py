"""meda_filtering - MEDA Weather Filtering Package (MarsWalk Intelligence).

Independent service that consumes the JSONL output of MEDA Weather Normalizer v2.1
and produces a Filtered JSONL dataset containing accepted measurements only.

This package never imports or executes the Normalizer (``normalization_service``).

    from meda_filtering.filter import filter_normalized_jsonl, FilteringConfig

Data contracts (standalone JSON Schema files, usable without importing Python):
    meda_filtering/schemas/normalized_schema.json
    meda_filtering/schemas/filtered_schema.json
"""
from .filter import (
    FILTERING_PACKAGE_VERSION,
    FilteringConfig,
    FilteringError,
    FilteringStats,
    filter_normalized_jsonl,
    filter_record,
)
from .report import REPORT_SCHEMA_VERSION, build_report, write_report_atomic
from .rules import (
    RULESET_VERSION,
    Decision,
    RuleSet,
    RulesConfigError,
    build_ruleset,
    evaluate_measurement,
    load_rules_config,
)

__version__ = FILTERING_PACKAGE_VERSION
__all__ = [
    "__version__", "FILTERING_PACKAGE_VERSION", "RULESET_VERSION", "REPORT_SCHEMA_VERSION",
    "FilteringConfig", "FilteringError", "FilteringStats", "filter_normalized_jsonl", "filter_record",
    "Decision", "RuleSet", "RulesConfigError", "build_ruleset", "evaluate_measurement", "load_rules_config",
    "build_report", "write_report_atomic",
]
