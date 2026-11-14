"""Validation report: assembled from the ExecutionContext only.

Every value comes from ``ctx``; there are no literals that claim success.  A
section whose facts could not be measured carries ``null`` plus a
``unavailable_reason`` instead of a made-up ``0`` / ``[]`` / ``"PASSED"``.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .context import ExecutionContext, jsonable

__all__ = ["REPORT_SCHEMA_VERSION", "build_report", "write_report_atomic", "failed_report_path", "default_report_path"]

REPORT_SCHEMA_VERSION = "2.0"


def default_report_path(output: Path) -> Path:
    return output.with_name(output.name + ".report.json")


def failed_report_path(report: Path, run_id: str) -> Path:
    stem = report.name[:-5] if report.name.endswith(".json") else report.name
    return report.with_name(f"{stem}.FAILED.{run_id}.json")


def _section_available(value: Any, reason: str) -> dict:
    return {"available": value is not None and value != {} and value != [], "unavailable_reason": None if value else reason}


def build_report(ctx: ExecutionContext) -> dict:
    products = [p.as_dict() for p in ctx.products.values()]
    by_status = Counter(p["parsing_status"] for p in products)
    events = [e.as_dict() for e in ctx.failures]
    unavailable = "stage not reached (see partial_execution.current_stage)"
    discovered = ctx.discovery.get("products_recognized")

    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "1_run_summary": {
            "run_id": ctx.run_id,
            "start_time": ctx.start_time,
            "end_time": ctx.end_time,
            "elapsed_seconds": ctx.elapsed_seconds,
            "current_stage_at_end": ctx.current_stage,
            "last_successful_stage": ctx.last_successful_stage,
            "stages": ctx.stages,
            "arguments": ctx.arguments,
            "exception": ctx.exception,
        },
        "2_acceptance_decision": ctx.acceptance,
        "3_publication_decision": {
            "dataset": ctx.publication,
            "report": ctx.report_publication,
            "note": "dataset and report states are independent",
        },
        "4_input_archives": ctx.input_archives,
        "5_archive_inspection": ctx.archive_inspection or None,
        "6_product_discovery": {
            **ctx.discovery,
            "products_discovered": discovered,
            "products_parsed": sum(1 for p in products if p["parsing_status"] in ("PARSED", "PARTIALLY_PARSED")),
            "products_failed": by_status.get("FAILED", 0),
            "products_quarantined": by_status.get("QUARANTINED", 0),
            "parsing_status_counts": dict(sorted(by_status.items())),
            "note": "discovered != parsed: see 7_product_details for each product",
        },
        "7_product_details": products,
        "8_scientific_validation": {
            "channels": ctx.channel_validation or None,
            "quality_flag_definitions": ctx.flag_validation or None,
            "status_vocabulary": ["SCIENTIFICALLY_VERIFIED", "NUMERICALLY_VALID", "PARTIALLY_VALID",
                                  "UNVERIFIED", "INVALID"],
            "unavailable_reason": None if ctx.channel_validation else unavailable,
        },
        "9_sentinel_registry_and_applied_rules": {
            "registry": ctx.sentinel_registry,
            "applied": ctx.sentinel_applied or None,
        },
        "10_normalization_summary": ctx.normalization or None,
        "11_sclk_validation": ctx.sclk_metrics or None,
        "12_provenance_validation": ctx.provenance_metrics or None,
        "13_record_accounting": ctx.accounting or None,
        "14_failure_events": events,
        "15_warnings": ctx.warnings,
        "16_excluded_data": ctx.exclusions,
        "17_quarantined_data": ctx.quarantined,
        "18_partial_execution_results": {
            "current_stage": ctx.current_stage,
            "last_successful_stage": ctx.last_successful_stage,
            **ctx.partial_results,
        },
        "19_test_results": {
            "scope": "mandatory validations executed in THIS run (unit tests are reported separately)",
            "checks": ctx.checks,
            "executed": sum(1 for c in ctx.checks if c["result"] != "NOT_EXECUTED"),
            "passed": sum(1 for c in ctx.checks if c["result"] == "PASS"),
            "failed": sum(1 for c in ctx.checks if c["result"] == "FAIL"),
            "not_executed": sum(1 for c in ctx.checks if c["result"] == "NOT_EXECUTED"),
        },
        "20_final_recommendations": ctx.recommendations,
    }
    return jsonable(report)


def write_report_atomic(path: Path, report: dict) -> None:
    """Write strict JSON (no NaN/Infinity) via a same-directory temp file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=False, allow_nan=False, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        replaced = True
    finally:
        if not replaced:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
