"""Shared execution context, failure model and acceptance calculation.

The context is created BEFORE the first stage and updated by every stage, so a
failure at any point still leaves everything that was measured up to then.
The final status (PASS / PARTIAL / FAIL) is computed by ``compute_acceptance``
from the recorded facts only - it is never assigned by hand.

Failure scopes   DATASET | PRODUCT | CHANNEL | RECORD
Severities       CRITICAL (-> FAIL) | ERROR | WARNING | INFO
Isolation        ISOLATED (valid remainder is publishable) | NOT_ISOLATABLE | NOT_APPLICABLE
"""
from __future__ import annotations

import datetime as _dt
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PASS", "PARTIAL", "FAIL",
    "FailureEvent", "ProductRecord", "ExecutionContext",
    "compute_acceptance", "jsonable",
]

PASS, PARTIAL, FAIL = "PASS", "PARTIAL", "FAIL"
CRITICAL, ERROR, WARNING, INFO = "CRITICAL", "ERROR", "WARNING", "INFO"

#: discovery skip reasons that are an explicit, requested filter (not a data loss)
BENIGN_SKIP_REASONS = ("outside_sol_range", "superseded_release")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def jsonable(obj: Any) -> Any:
    """Recursively make ``obj`` strict-JSON safe (no NaN/Infinity, no sets/Paths)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = sorted(obj, key=str) if isinstance(obj, (set, frozenset)) else obj
        return [jsonable(v) for v in items]
    if hasattr(obj, "as_dict"):
        return jsonable(obj.as_dict())
    if hasattr(obj, "__fspath__"):
        return str(obj)
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    return str(obj)


@dataclass
class FailureEvent:
    severity: str
    scope: str
    stage: str
    entity_id: str
    error_code: str
    description: str
    evidence: Any = None
    affected_count: int | None = None
    isolation_status: str = "NOT_APPLICABLE"
    recovery_recommendation: str = ""
    category: str = ""
    related_product: str | None = None
    related_channel: str | None = None
    event_id: str = ""

    def as_dict(self) -> dict:
        return jsonable(self.__dict__)


@dataclass
class ProductRecord:
    product_id: str  # source file name without extension
    product_name: str
    source_archive: str | None
    product_type: str
    processing_level: str
    sol: int
    source_csv: str
    xml_label: str | None
    discovery_status: str = "DISCOVERED"
    parsing_status: str = "NOT_STARTED"
    normalization_status: str = "NOT_STARTED"
    validation_status: str = "NOT_STARTED"
    publication_status: str = "NOT_PUBLISHED"
    rows_encountered: int | None = None
    rows_parsed: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    rows_quarantined: int = 0
    rows_not_processed: int | None = None
    rows_not_processed_method: str | None = None
    measurements_generated: int = 0
    measurements_published: int = 0
    measurements_excluded: int = 0
    label_declared_records: int | None = None
    channels_found: int = 0
    channels_accepted: int = 0
    channels_rejected: int = 0
    exclusion_reason: str | None = None
    evidence_references: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return jsonable(self.__dict__)


class ExecutionContext:
    """Mutable run state shared by every stage."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.start_time = _now()
        self._t0 = time.monotonic()
        self.end_time: str | None = None
        self.elapsed_seconds: float | None = None
        self.current_stage = "INIT"
        self.last_successful_stage: str | None = None
        self.stages: list[dict] = []
        self.arguments: dict = {}
        self.input_archives: list[dict] = []
        self.archive_inspection: list[dict] = []
        self.discovery: dict = {}
        self.products: dict[str, ProductRecord] = {}
        self.channel_validation: list[dict] = []
        self.flag_validation: list[dict] = []
        self.sentinel_registry: list[dict] = []
        self.sentinel_applied: dict = {}
        self.record_counters: dict = {}
        self.normalization: dict = {}
        self.sclk_metrics: dict = {}
        self.provenance_metrics: dict = {}
        self.accounting: dict = {}
        self.checks: list[dict] = []
        self.failures: list[FailureEvent] = []
        self.warnings: list[dict] = []
        self.exclusions: list[dict] = []
        self.quarantined: list[dict] = []
        self.partial_results: dict = {}
        self.exception: dict | None = None
        self.acceptance: dict = {"status": None, "reasons": []}
        self.publication: dict = {"status": "NOT_ATTEMPTED", "dataset_path": None, "staged_path": None,
                                  "previous_dataset_preserved": None, "reason": None}
        self.report_publication: dict = {"status": "NOT_ATTEMPTED", "path": None, "error": None}
        self.recommendations: list[str] = []
        self.strict = False
        self._n = 0

    # -- stages ------------------------------------------------------------
    def begin(self, stage: str) -> None:
        self.current_stage = stage
        self.stages.append({"stage": stage, "started": _now(), "status": "RUNNING"})

    def complete(self, stage: str | None = None) -> None:
        stage = stage or self.current_stage
        for entry in reversed(self.stages):
            if entry["stage"] == stage and entry["status"] == "RUNNING":
                entry["status"] = "COMPLETED"
                entry["ended"] = _now()
                break
        self.last_successful_stage = stage

    def record_exception(self, exc: BaseException) -> None:
        for entry in reversed(self.stages):
            if entry["status"] == "RUNNING":
                entry["status"] = "FAILED"
                entry["ended"] = _now()
                break
        self.exception = {"type": type(exc).__name__, "message": str(exc), "stage": self.current_stage}
        self.fail("CRITICAL", "DATASET", self.current_stage, "pipeline", "UNHANDLED_EXCEPTION",
                  f"{type(exc).__name__}: {exc}", evidence=self.exception,
                  isolation_status="NOT_ISOLATABLE",
                  recovery_recommendation="Fix the cause and re-run; the previous dataset was not touched.",
                  category="EXCEPTION")

    # -- events ------------------------------------------------------------
    def fail(self, severity: str, scope: str, stage: str, entity_id: str, error_code: str, description: str,
             **kw: Any) -> FailureEvent:
        self._n += 1
        event = FailureEvent(severity, scope, stage, entity_id, error_code, description,
                             event_id=f"F{self._n:04d}", **kw)
        self.failures.append(event)
        return event

    def warn(self, code: str, description: str, **kw: Any) -> None:
        self.warnings.append({"code": code, "description": description, "stage": self.current_stage, **kw})

    def exclude(self, scope: str, entity_id: str, reason: str, **kw: Any) -> None:
        self.exclusions.append({"scope": scope, "entity_id": entity_id, "reason": reason, "stage": self.current_stage, **kw})

    def check(self, name: str, passed: bool | None, detail: str, mandatory: bool = True, **evidence: Any) -> None:
        """Record the outcome of a validation that was ACTUALLY executed.

        ``passed=None`` means the check could not be executed (never reported as pass).
        """
        self.checks.append({"name": name, "mandatory": mandatory,
                            "result": "NOT_EXECUTED" if passed is None else ("PASS" if passed else "FAIL"),
                            "detail": detail, "evidence": jsonable(evidence)})

    def finish(self) -> None:
        self.end_time = _now()
        self.elapsed_seconds = round(time.monotonic() - self._t0, 3)


# --------------------------------------------------------------------------
# Acceptance
# --------------------------------------------------------------------------


def compute_acceptance(ctx: ExecutionContext) -> dict:
    """Derive PASS / PARTIAL / FAIL from the recorded facts.

    FAIL    any CRITICAL event, any failed/unexecuted mandatory check, an
            exception, no usable product, or --strict with any isolation.
    PARTIAL no FAIL cause, but data was isolated/quarantined/rejected or a
            channel/rule is unverified (all listed in ``reasons``).
    PASS    none of the above.
    """
    fail_reasons: list[str] = []
    partial_reasons: list[str] = []

    for ev in ctx.failures:
        if ev.severity == CRITICAL:
            fail_reasons.append(f"{ev.error_code}: {ev.description} [{ev.scope}/{ev.entity_id}]")
    for ev in ctx.failures:
        if ev.severity == ERROR and ev.isolation_status == "NOT_ISOLATABLE":
            fail_reasons.append(f"{ev.error_code}: {ev.description} [{ev.scope}/{ev.entity_id}] cannot be isolated")
    for chk in ctx.checks:
        if chk["mandatory"] and chk["result"] != "PASS":
            fail_reasons.append(f"mandatory check {chk['name']} -> {chk['result']}: {chk['detail']}")
    if ctx.exception is not None and not any("UNHANDLED_EXCEPTION" in r for r in fail_reasons):
        fail_reasons.append(f"exception {ctx.exception['type']} in stage {ctx.exception['stage']}")

    products = list(ctx.products.values())
    parsed = [p for p in products if p.parsing_status in ("PARSED", "PARTIALLY_PARSED")]
    if ctx.exception is None and not parsed:
        fail_reasons.append("no product was parsed successfully")

    bad_products = [p for p in products if p.parsing_status in ("FAILED", "QUARANTINED")]
    if bad_products:
        partial_reasons.append(f"{len(bad_products)} product(s) excluded: "
                               + ", ".join(sorted(p.product_id for p in bad_products)[:10]))
    isolated_errors = [e for e in ctx.failures if e.severity == ERROR and e.isolation_status == "ISOLATED"]
    if isolated_errors:
        kinds = sorted({e.error_code for e in isolated_errors})
        partial_reasons.append(f"{len(isolated_errors)} isolated failure event(s): {', '.join(kinds[:6])}")
    skipped = [e for e in ctx.exclusions if e["scope"] == "PRODUCT" and e.get("reason_code") not in BENIGN_SKIP_REASONS]
    if skipped and not bad_products:
        partial_reasons.append(f"{len(skipped)} file(s) skipped")
    rejected = sum(p.rows_rejected for p in products)
    if rejected:
        partial_reasons.append(f"{rejected} row(s) rejected")
    quarantined = [c for c in ctx.channel_validation if c.get("final_channel_status") == "INVALID"]
    if quarantined:
        partial_reasons.append(f"{len(quarantined)} channel(s) isolated (INVALID)")
    unverified = {(c["canonical_channel_name"]) for c in ctx.channel_validation
                  if c.get("final_channel_status") in ("UNVERIFIED", "PARTIALLY_VALID", "NUMERICALLY_VALID")}
    if unverified:
        partial_reasons.append(f"{len(unverified)} channel(s) not scientifically verified: "
                               + ", ".join(sorted(unverified)[:8]))
    if any(v for v in ctx.sentinel_applied.get("unverified_candidates", {}).values()):
        partial_reasons.append("values matching UNVERIFIED sentinel rules were kept and flagged")
    if any(w.get("partial") for w in ctx.warnings):
        partial_reasons.append("non-benign warnings present (see Warnings)")

    if ctx.strict and partial_reasons and not fail_reasons:
        fail_reasons.append("--strict: isolation/rejection is not allowed: " + "; ".join(partial_reasons))

    if fail_reasons:
        status, reasons = FAIL, fail_reasons
    elif partial_reasons:
        status, reasons = PARTIAL, partial_reasons
    else:
        status, reasons = PASS, ["every mandatory check passed and nothing was isolated"]
    result = {
        "status": status,
        "reasons": reasons,
        "publishable": status in (PASS, PARTIAL),
        "mandatory_checks_total": sum(1 for c in ctx.checks if c["mandatory"]),
        "mandatory_checks_passed": sum(1 for c in ctx.checks if c["mandatory"] and c["result"] == "PASS"),
        "computed_from": "ctx.failures, ctx.checks, ctx.products, ctx.channel_validation (not hand-set)",
    }
    ctx.acceptance = result
    return result
