"""Filtering Report: assembled ONLY from values measured during a run.

Nothing in this module is a literal result.  Counts, hashes, timings and
examples come from ``FilteringStats`` and the resolved ``RuleSet``; descriptive
sections (rules, limitations) come from the rule registry and fixed text that
states design limits, never outcomes.  A run that failed says so and carries
the partial statistics that were accumulated before the failure.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

from .filter import FILTERING_PACKAGE_VERSION, FilteringConfig, FilteringStats
from .rules import (
    SOURCE_SELECTION_CANDIDATES,
    RuleSet,
    RuleStatus,
    build_ruleset,
)
from .schemas.filtered_schema_validator import FILTERED_SCHEMA_VERSION
from .schemas.normalized_schema_validator import NORMALIZED_SCHEMA_VERSION

__all__ = ["REPORT_SCHEMA_VERSION", "build_report", "write_report_atomic"]

REPORT_SCHEMA_VERSION = "1.0"

#: Design limits of this package version. They describe what the filter does NOT do.
KNOWN_LIMITATIONS = (
    "Only the 'measurements' member of each Normalized record is evaluated. ENG, RDS and ATS raw channels "
    "(raw_channels), raw_channel_issues, unmapped_channels and quarantined_channels hold values that are not "
    "evaluated; they are therefore NOT copied to the Filtered Dataset.",
    "No physical range limit, quality-flag policy, angle-domain rule or uncertainty criterion is active unless the "
    "operator supplied one with evidence in a rules configuration. See 'unsupported_rules' for what documentation is missing.",
    "Sentinel handling relies on the Normalizer's scoped registry. This package does not re-detect sentinel values; "
    "values it receives as 'unverified_sentinel_candidate' are held back for review, not accepted.",
    "Relative-humidity measurements carry no verified unit and are held back for review while FILT-005 is active.",
    "Calibrated and derived measurements are different canonical names and are never merged, averaged or converted. "
    "Unless a source-selection policy was configured, every accepted level is kept; see 'source_selection' for the "
    "number of records in which more than one candidate of a group was accepted.",
    "The uncertainty member of an accepted measurement is copied unchanged and its own validity is not evaluated.",
    "A record with no accepted measurement is not written, so the Filtered Dataset has no (Sol, SCLK) entry for it. "
    "'quality' and 'sources' of a written record are copied whole, including flags and sources of withheld measurements.",
    "UTC is null throughout (the Normalizer performs no SPICE correlation); Sol and SCLK are preserved unchanged.",
    "Output validation checks structure and counting invariants against filtered_schema.json; it is not a scientific validation.",
)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    return str(obj)


def _decision_counts(table: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return {
        key: {d: sub.get(d, 0) for d in ("accepted", "rejected", "review_required")}
        for key, sub in sorted(table.items())
    }


def build_report(
    stats: FilteringStats,
    config: FilteringConfig,
    *,
    input_path: str | Path | None = None,
    output_path: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    run_id: str | None = None,
    failure: str | None = None,
    extra_known_limitations: list[str] | None = None,
) -> dict:
    """Build the report dict (strict-JSON-safe) from one run's statistics."""
    rs: RuleSet = config.ruleset or build_ruleset()

    if failure is not None or not stats.completed:
        status = "failed"
    elif stats.records_validation_errors:
        status = "completed_with_validation_errors"
    else:
        status = "completed"

    active = [r for r in rs.rules if r.status == RuleStatus.ACTIVE]
    rules_applied = []
    for r in active:
        d = r.describe()
        d["measurements_evaluated"] = stats.rule_evaluated.get(r.rule_id, 0)
        d["measurements_triggered"] = stats.rule_triggered.get(r.rule_id, 0)
        rules_applied.append(d)

    source_selection = {
        "policies": {
            gid: {
                "policy": p.policy,
                **({"priority": list(p.priority), "evidence": p.evidence} if p.policy == "priority" else {}),
                "candidate_members": SOURCE_SELECTION_CANDIDATES[gid]["members"],
                "note": SOURCE_SELECTION_CANDIDATES[gid]["note"],
            }
            for gid, p in rs.source_policies.items()
        },
        "unresolved_records_by_group": dict(sorted(stats.source_selection_unresolved_records.items())),
        "measurements_dropped_by_group": dict(sorted(stats.source_selection_dropped.items())),
        "meaning": (
            "unresolved_records_by_group counts records in which two or more members of a group were accepted "
            "while the group's policy is keep_all: the choice between them was not made by this package."
        ),
    }

    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "identification": {
            "package_name": "meda_filtering",
            "package_version": FILTERING_PACKAGE_VERSION,
            "ruleset_version": rs.version,
            "input_schema_version": NORMALIZED_SCHEMA_VERSION,
            "output_schema_version": FILTERED_SCHEMA_VERSION,
            "run_id": run_id,
        },
        "processing_status": status,
        "failure_message": failure,
        "input": {
            "path": str(input_path) if input_path else None,
            "size_bytes": stats.input_bytes,
            "sha256": stats.input_sha256,
            "identification_note": "size and sha256 are computed during the filtering pass; null if the file was not read completely",
            "schema_file": "meda_filtering/schemas/normalized_schema.json",
            "input_schema_validation": "skipped" if config.skip_source_validation else ("strict" if config.strict_validation else "enabled"),
        },
        "output": {
            "path": str(output_path) if output_path and stats.completed else None,
            "size_bytes": stats.output_bytes,
            "sha256": stats.output_sha256,
            "schema_file": "meda_filtering/schemas/filtered_schema.json",
            "output_schema_validation": "enabled" if config.validate_output else "skipped",
        },
        "totals": {
            "records_read": stats.records_read,
            "records_with_validation_errors": stats.records_validation_errors,
            "records_written": stats.records_written,
            "records_not_written_no_accepted_measurement": stats.records_skipped_no_accepted,
            "measurements_evaluated": stats.measurements_evaluated,
            "measurements_accepted": stats.measurements_accepted,
            "measurements_rejected": stats.measurements_rejected,
            "measurements_review_required": stats.measurements_review_required,
        },
        "counts_by_variable": _decision_counts(stats.by_variable),
        "counts_by_reason": {d: dict(sorted(v.items())) for d, v in sorted(stats.by_reason.items())},
        "counts_by_source": _decision_counts(stats.by_source),
        "counts_by_source_note": "keys are '<source_product>.<source_level>' of each measurement (processing level preserved from the Normalizer)",
        "rules_applied": rules_applied,
        "unsupported_rules": [
            r.describe() for r in rs.rules if r.status == RuleStatus.PENDING_SCIENTIFIC_DOCUMENTATION
        ],
        "disabled_rules": [r.describe() for r in rs.rules if r.status == RuleStatus.DISABLED],
        "source_selection": source_selection,
        "rules_configuration": {
            "path": rs.config_path,
            "content": rs.config_echo,
        },
        "known_limitations": list(KNOWN_LIMITATIONS) + (extra_known_limitations or []),
        "traceability_examples": {k: v for k, v in sorted(stats.examples_by_reason.items())},
        "validation_errors": {
            "total": stats.validation_errors_total,
            "listed": len(stats.validation_errors),
            "truncated": stats.validation_errors_total > len(stats.validation_errors),
            "errors": stats.validation_errors,
        },
        "execution_metadata": {
            "start_time_utc": start_time,
            "end_time_utc": end_time,
            "elapsed_seconds": round(stats.elapsed_seconds, 3),
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "strict_validation": config.strict_validation,
        },
    }
    return _jsonable(report)


def now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def write_report_atomic(path: str | Path, report: dict) -> None:
    """Write the report as strict JSON via a temp file and os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    done = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, allow_nan=False, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        done = True
    finally:
        if not done:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
