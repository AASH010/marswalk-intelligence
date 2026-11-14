"""MEDA Filtering Package: core filtering engine.

Pipeline per Normalized JSONL line
  1. parse strict JSON (NaN/Infinity refused);
  2. validate against normalized_schema.json (unless explicitly skipped);
  3. evaluate EVERY measurement with the active rules -> accepted / rejected / review_required;
  4. apply the explicit, configurable record-level source selection to accepted measurements;
  5. write a Filtered record that contains accepted measurements only
     (a record with no accepted measurement is not written - see README);
  6. validate each written record against filtered_schema.json;
  7. publish the output atomically (temp file + os.replace).

This module does NOT
  * import or execute the Normalizer (normalization_service);
  * modify the input file (opened read-only; output may not be the same path);
  * create, interpolate, convert or fabricate any value, timestamp or record;
  * add any field to a measurement object (accepted measurements are copied unchanged).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from .rules import (
    Decision,
    RuleResult,
    RuleSet,
    build_ruleset,
    evaluate_measurement,
    select_sources,
)
from .schemas.filtered_schema_validator import FilteredSchemaError, validate_record as validate_filtered
from .schemas.normalized_schema_validator import NormalizedSchemaError, validate_record as validate_normalized

__all__ = [
    "FILTERING_PACKAGE_VERSION",
    "FilteringError",
    "FilteringConfig",
    "FilteringStats",
    "filter_record",
    "filter_normalized_jsonl",
]

FILTERING_PACKAGE_VERSION = "1.0.0"

#: pseudo-rule that records a source-selection drop (not a quality rule)
SOURCE_SELECTION_RULE_ID = "SRC-SELECT"
SOURCE_SELECTION_REASON = "not_selected_by_source_policy"

_MAX_ERRORS_LISTED = 1000      # bound on validation errors kept verbatim in the report
_MAX_EXAMPLES_PER_REASON = 3   # bound on traceability examples per reason


class FilteringError(RuntimeError):
    """Fatal error of the filtering run (not a per-record validation error)."""


# ---------------------------------------------------------------------------
# Configuration and statistics
# ---------------------------------------------------------------------------


@dataclass
class FilteringConfig:
    """Runtime configuration.

    strict_validation       any input line that is invalid JSON or violates the Normalized Schema
                            aborts the run; no output is published.
    validate_output         validate every written record against filtered_schema.json (default on).
    skip_source_validation  do not validate input records against normalized_schema.json.
    ruleset                 resolved rules (default: build_ruleset()).
    """

    strict_validation: bool = False
    validate_output: bool = True
    skip_source_validation: bool = False
    ruleset: RuleSet | None = None


def _bump(d: dict, key: str, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


@dataclass
class FilteringStats:
    """Everything the report states is accumulated here during the run."""

    records_read: int = 0
    records_validation_errors: int = 0
    records_written: int = 0
    records_skipped_no_accepted: int = 0
    lines_blank: int = 0

    measurements_evaluated: int = 0
    measurements_accepted: int = 0
    measurements_rejected: int = 0
    measurements_review_required: int = 0

    by_variable: dict[str, dict[str, int]] = field(default_factory=dict)       # name -> decision -> n
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)         # "PRODUCT.level" -> decision -> n
    by_reason: dict[str, dict[str, int]] = field(default_factory=dict)         # decision -> reason_code -> n
    rule_evaluated: dict[str, int] = field(default_factory=dict)               # rule_id -> n measurements it applied to
    rule_triggered: dict[str, int] = field(default_factory=dict)               # rule_id -> n measurements it fired on
    examples_by_reason: dict[str, list[dict]] = field(default_factory=dict)    # "decision:reason" -> examples

    source_selection_unresolved_records: dict[str, int] = field(default_factory=dict)
    source_selection_dropped: dict[str, int] = field(default_factory=dict)

    validation_errors: list[dict] = field(default_factory=list)
    validation_errors_total: int = 0

    input_bytes: int | None = None
    input_sha256: str | None = None
    output_bytes: int | None = None
    output_sha256: str | None = None
    elapsed_seconds: float = 0.0
    completed: bool = False

    def note_error(self, line: int, message: str, field_path: str | None = None) -> None:
        self.records_validation_errors += 1
        self.validation_errors_total += 1
        if len(self.validation_errors) < _MAX_ERRORS_LISTED:
            entry = {"line": line, "error": message}
            if field_path:
                entry["field_path"] = field_path
            self.validation_errors.append(entry)

    def note_decision(
        self,
        name: str,
        product: str,
        level: str,
        decision: Decision,
        results: list[RuleResult],
        trigger: RuleResult | None,
        context: Mapping[str, Any],
    ) -> None:
        d = decision.value
        self.measurements_evaluated += 1
        if decision == Decision.ACCEPTED:
            self.measurements_accepted += 1
        elif decision == Decision.REJECTED:
            self.measurements_rejected += 1
        else:
            self.measurements_review_required += 1
        _bump(self.by_variable.setdefault(name, {}), d)
        _bump(self.by_source.setdefault(f"{product}.{level}", {}), d)
        for r in results:
            _bump(self.rule_evaluated, r.rule_id)
        if trigger is not None:
            _bump(self.rule_triggered, trigger.rule_id)
            _bump(self.by_reason.setdefault(d, {}), trigger.reason_code)
            bucket = self.examples_by_reason.setdefault(f"{d}:{trigger.reason_code}", [])
            if len(bucket) < _MAX_EXAMPLES_PER_REASON:
                bucket.append({"rule_id": trigger.rule_id, "canonical_name": name, **context, "detail": trigger.detail})


@dataclass
class RecordResult:
    evaluated: int = 0
    accepted: int = 0
    rejected: int = 0
    review_required: int = 0


# ---------------------------------------------------------------------------
# Record-level filter
# ---------------------------------------------------------------------------


def _trigger_of(results: list[RuleResult], final: Decision) -> RuleResult | None:
    if final == Decision.ACCEPTED:
        return None
    for r in results:
        if r.decision == final:
            return r
    return None


def filter_record(
    record: Mapping[str, Any],
    *,
    ruleset: RuleSet | None = None,
    stats: FilteringStats | None = None,
    line_number: int | None = None,
) -> tuple[dict | None, RecordResult]:
    """Filter ONE Normalized record (the argument is never modified).

    Returns ``(filtered_record_or_None, result)``; ``None`` when no measurement
    of the record was accepted.  When ``stats`` is given, every decision is
    accumulated into it.
    """
    rs = ruleset or build_ruleset()
    rules = rs.active
    res = RecordResult()
    ctx_base = {"sol": record.get("sol"), "sclk_s": record.get("sclk_s")}
    if line_number is not None:
        ctx_base["line"] = line_number

    decided: dict[str, tuple[Decision, list[RuleResult], Mapping[str, Any]]] = {}
    for name, meas in record["measurements"].items():
        final, results = evaluate_measurement(name, meas, record, rules)
        decided[name] = (final, results, meas)

    accepted_names = [n for n, (d, _, _) in decided.items() if d == Decision.ACCEPTED]
    dropped, unresolved = select_sources(accepted_names, rs.source_policies)
    if stats is not None:
        for gid in unresolved:
            _bump(stats.source_selection_unresolved_records, gid)
        for _, (gid, _kept) in dropped.items():
            _bump(stats.source_selection_dropped, gid)

    kept: dict[str, Any] = {}
    for name, (final, results, meas) in decided.items():
        trigger = _trigger_of(results, final)
        if name in dropped:
            gid, kept_name = dropped[name]
            final = Decision.REJECTED
            trigger = RuleResult(
                SOURCE_SELECTION_RULE_ID, Decision.REJECTED, SOURCE_SELECTION_REASON,
                f"source-selection group {gid!r}: {kept_name!r} was selected by the configured priority",
            )
        res.evaluated += 1
        if final == Decision.ACCEPTED:
            res.accepted += 1
            kept[name] = copy.deepcopy(meas)  # unchanged; the filter adds nothing to a measurement
        elif final == Decision.REJECTED:
            res.rejected += 1
        else:
            res.review_required += 1
        if stats is not None:
            stats.note_decision(
                name, str(meas.get("source_product")), str(meas.get("source_level")), final, results, trigger,
                {**ctx_base, "source_file": meas.get("source_file"), "source_row": meas.get("source_row")},
            )

    if not kept:
        return None, res

    out: dict[str, Any] = {
        "sol": record["sol"],
        "sclk_s": record["sclk_s"],
        "lmst": record["lmst"],
        "ltst": record["ltst"],
        "utc": record["utc"],
        "measurements": kept,
        "quality": copy.deepcopy(record["quality"]),
        "sources": copy.deepcopy(record["sources"]),
    }
    # Normalizer context that is preserved verbatim. raw_channels, raw_channel_issues,
    # unmapped_channels and quarantined_channels are deliberately NOT carried over:
    # they contain values that the filter never evaluates.
    for key in ("missing", "conflicts"):
        if key in record:
            out[key] = copy.deepcopy(record[key])
    out["filtering_metadata"] = {
        "ruleset_version": rs.version,
        "measurements_evaluated": res.evaluated,
        "measurements_accepted": res.accepted,
        "measurements_rejected": res.rejected,
        "measurements_review_required": res.review_required,
    }
    return out, res


# ---------------------------------------------------------------------------
# File-level pipeline
# ---------------------------------------------------------------------------


def _same_file(a: Path, b: Path) -> bool:
    try:
        if b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return a.resolve() == b.resolve()


def _read_lines(path: Path, stats: FilteringStats) -> Iterator[tuple[int, str | None]]:
    """Yield ``(line_number, text)`` from a binary read-only pass that also hashes the file.

    ``text`` is None when the line is not valid UTF-8.  The digest/size are stored
    on ``stats`` only after the whole file has been read.
    """
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for number, raw in enumerate(fh, start=1):
            sha.update(raw)
            size += len(raw)
            try:
                yield number, raw.decode("utf-8")
            except UnicodeDecodeError:
                yield number, None
    stats.input_bytes, stats.input_sha256 = size, sha.hexdigest()


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _stream_filter(
    input_path: Path, ruleset: RuleSet, config: FilteringConfig, stats: FilteringStats
) -> Iterator[tuple[int, dict | None]]:
    for number, text in _read_lines(input_path, stats):
        if text is None:
            stats.note_error(number, "line is not valid UTF-8")
            if config.strict_validation:
                raise FilteringError(f"line {number}: line is not valid UTF-8")
            continue
        text = text.rstrip("\r\n")
        if not text.strip():
            stats.lines_blank += 1
            continue
        try:
            record = json.loads(text, parse_constant=_reject_constant)
        except ValueError as exc:
            stats.note_error(number, f"invalid JSON: {exc}")
            if config.strict_validation:
                raise FilteringError(f"line {number}: invalid JSON: {exc}") from exc
            continue
        stats.records_read += 1

        if not config.skip_source_validation:
            try:
                validate_normalized(record, line_number=number)
            except NormalizedSchemaError as exc:
                stats.note_error(number, str(exc), exc.field_path)
                if config.strict_validation:
                    raise FilteringError(str(exc)) from exc
                continue

        try:
            filtered, res = filter_record(record, ruleset=ruleset, stats=stats, line_number=number)
        except (KeyError, TypeError, AttributeError) as exc:
            # only reachable with skip_source_validation: the record does not have the contract shape
            stats.note_error(number, f"record cannot be processed (not a Normalized record): {type(exc).__name__}: {exc}")
            if config.strict_validation:
                raise FilteringError(f"line {number}: record cannot be processed: {exc}") from exc
            continue

        if filtered is None:
            stats.records_skipped_no_accepted += 1
        yield number, filtered


def filter_normalized_jsonl(
    input_path: str | Path,
    output_path: str | Path,
    *,
    config: FilteringConfig | None = None,
    stats: FilteringStats | None = None,
) -> FilteringStats:
    """Filter a Normalized JSONL file into a Filtered JSONL file.

    The input is opened read-only and is never modified.  The output is written
    to a temporary file and published with ``os.replace`` only after every record
    has been written; on any failure no output file is left behind (an existing
    output file is untouched).

    Raises FilteringError for: missing/invalid input path, output path equal to
    the input path, strict-mode validation failure, output-validation failure.
    """
    cfg = config or FilteringConfig()
    st = stats if stats is not None else FilteringStats()
    ruleset = cfg.ruleset or build_ruleset()
    src, dst = Path(input_path), Path(output_path)

    if not src.exists():
        raise FilteringError(f"input file not found: {src}")
    if src.is_dir():
        raise FilteringError(f"input path is a directory: {src}")
    if _same_file(src, dst):
        raise FilteringError("output path must differ from the input path (the Normalized dataset is never modified)")

    dst.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
        temp_path = Path(temp_name)
        out_sha = hashlib.sha256()
        with os.fdopen(fd, "wb") as out:
            for number, filtered in _stream_filter(src, ruleset, cfg, st):
                if filtered is None:
                    continue
                if cfg.validate_output:
                    try:
                        validate_filtered(filtered, line_number=number)
                    except FilteredSchemaError as exc:
                        raise FilteringError(f"filtered record for input line {number} violates filtered_schema.json: {exc}") from exc
                payload = (json.dumps(filtered, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                out.write(payload)
                out_sha.update(payload)
                st.records_written += 1
            out.flush()
            os.fsync(out.fileno())
        st.output_bytes = temp_path.stat().st_size
        st.output_sha256 = out_sha.hexdigest()
        os.replace(temp_path, dst)
        temp_path = None
        st.completed = True
    finally:
        st.elapsed_seconds = time.monotonic() - started
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return st
