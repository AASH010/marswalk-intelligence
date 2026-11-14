"""Command line interface and run orchestration (no science, no parsing here).

Run (from the directory that CONTAINS the
``normalization_service`` package folder; no PYTHONPATH needed):

    python -m normalization_service.cli \\
        --calibrated /data/MEDA_Weather_ds.zip \\
        --derived    /data/derived.zip \\
        --output     /data/out/meda_normalized.jsonl

``--calibrated`` / ``--derived`` may each be a ZIP archive or an extracted
directory.  The processing level of a file always comes from its own name.

ACCEPTANCE MODEL (computed from recorded facts, never hand-set)
    PASS     every mandatory check passed, nothing isolated or unverified
    PARTIAL  mandatory checks passed, but data was isolated (product / channel /
             row), or a channel / sentinel rule is unverified.  Publishable;
             every limitation is listed in the report.
    FAIL     a critical failure, a failed mandatory check, or an exception.
             Nothing is published and the previous dataset is left untouched.

PUBLICATION GATE
    The validated JSONL is only *staged* beside the output; it replaces the
    output (atomic ``os.replace``) after ALL mandatory validations passed and
    the acceptance status is PASS or PARTIAL.  ``--no-strict`` / ``--strict``
    never bypass a mandatory check: ``--strict`` is stricter (any isolation or
    rejected row is a failure / fail-fast, exit 3), the default only decides
    whether *isolatable* problems (one product, one channel, one row) are
    isolated and reported (PARTIAL) instead of failing the whole run.

REPORT
    PASS / PARTIAL -> ``<output>.report.json`` (or ``--report``).
    FAIL           -> ``<report-stem>.FAILED.<run_id>.json`` so the report of the
                      last good dataset is never overwritten by a failed run.
    A complete staged dataset that is rejected by acceptance is kept as
    ``<output>.rejected.<run_id>.jsonl`` for diagnosis (``--discard-rejected``
    removes it).  A run refused for invalid *arguments* writes no report.

Exit codes: 0 PASS | 1 unexpected | 2 invalid input | 3 parsing failure |
4 normalisation failure | 5 output failure | 6 output validation failure |
10 PARTIAL (published, with documented limitations) | 11 FAIL by acceptance.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from collections import Counter
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterator, Sequence

from .archive import PathSafetyError, check_paths, inspect_source
from .context import (
    BENIGN_SKIP_REASONS,
    FAIL,
    PARTIAL,
    PASS,
    ExecutionContext,
    ProductRecord,
    compute_acceptance,
)
from .normalize import CHANNEL_SPECS, FLAG_DEFINITIONS, NormalizationError, verify_label_units
from .parser import (
    ArchiveError,
    DiscoveryResult,
    MedaParseError,
    ParseStats,
    ProductFile,
    discover_products,
    iter_product_file_rows,
    parse_label,
    preflight_product,
    safe_extract_zip,
)
from .registry import (
    DEFAULT_SENTINEL_REGISTRY,
    ChannelDecision,
    SentinelRegistry,
    verify_channels,
    verify_flag_definitions,
)
from .report import build_report, default_report_path, failed_report_path, write_report_atomic
from .streaming import (
    ContractError,
    OutputValidationError,
    StreamSummary,
    discard_staged,
    publish_staged,
    stream_normalize,
)

LOG = logging.getLogger("normalization_service")


class ExitCode(IntEnum):
    SUCCESS = 0
    UNEXPECTED = 1
    INVALID_INPUT = 2
    PARSING_FAILURE = 3
    NORMALIZATION_FAILURE = 4
    OUTPUT_FAILURE = 5
    VALIDATION_FAILURE = 6
    PARTIAL = 10  # published, with documented limitations
    ACCEPTANCE_FAILURE = 11  # complete run, rejected by the acceptance gate


class InputValidationError(Exception):
    """Raised for bad arguments / unusable input paths (exit code 2)."""


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m normalization_service.cli",
        description=(
            "Parse MEDA calibrated + derived CSV/XML products, normalise them by exact "
            "(Sol, SCLK), validate, and publish a JSONL dataset only if acceptance is PASS or PARTIAL."
        ),
        epilog=(
            "Example usage\n"
            "  python -m normalization_service.cli --calibrated MEDA_Weather_ds.zip "
            "--derived derived.zip --output out/meda.jsonl --sol-start 230 --sol-end 239\n\n"
            "exit codes: 0 ok (PASS), 1 unexpected, 2 invalid input, 3 parsing failure, "
            "4 normalisation failure, 5 output failure, 6 validation failure, "
            "10 PARTIAL (published with documented limitations), 11 FAIL (not published)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--calibrated", type=Path, help="Calibrated archive: ZIP file or extracted directory.")
    parser.add_argument("--derived", type=Path, help="Derived archive: ZIP file or extracted directory.")
    parser.add_argument("--output", type=Path, required=True, help="Path of the JSONL file to write.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Validation report path (default: <output>.report.json). A FAIL report is written "
                             "as <report-stem>.FAILED.<run_id>.json.")
    parser.add_argument("--sentinel-registry", type=Path, default=None,
                        help="JSON file replacing the default scoped sentinel registry.")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="Temporary working directory for extraction and staging (default: system temp).")
    parser.add_argument("--batch-size", type=int, default=5000, help="SQLite staging batch size (default: 5000).")
    parser.add_argument("--sol-start", type=int, default=None, help="First Sol to process (inclusive).")
    parser.add_argument("--sol-end", type=int, default=None, help="Last Sol to process (inclusive).")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity (default: INFO).")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing output file.")
    parser.add_argument("--strict", action=argparse.BooleanOptionalAction, default=False,
                        help="--strict: fail-fast - the first rejected row / unreadable product aborts the run "
                             "(exit 3, output untouched) and any isolation makes acceptance FAIL. "
                             "--no-strict (default) isolates isolatable problems (-> PARTIAL). Neither can bypass "
                             "a mandatory check.")
    parser.add_argument("--discard-rejected", action="store_true",
                        help="Delete the staged JSONL when acceptance is FAIL (default: keep it for diagnosis).")
    parser.add_argument("--summary-json", type=Path, default=None, help="Also write the run summary as JSON.")
    return parser


# --------------------------------------------------------------------------
# Validation of arguments (no report is written for a refusal at this stage)
# --------------------------------------------------------------------------


def _nearest_existing(path: Path) -> Path:
    path = path.resolve() if path.exists() else path.absolute()
    while not path.exists():
        if path.parent == path:
            break
        path = path.parent
    return path


def validate_arguments(args: argparse.Namespace) -> None:
    """Raise ``InputValidationError`` with a precise message on any problem.

    Archive *content* problems (corrupt / non-ZIP file) are NOT decided here:
    they are inspected inside the run so that a report with the inspection
    result exists even when extraction fails.
    """
    if args.calibrated is None and args.derived is None:
        raise InputValidationError("At least one of --calibrated / --derived is required.")
    for flag in ("calibrated", "derived"):
        path: Path | None = getattr(args, flag)
        if path is None:
            continue
        if not path.exists():
            raise InputValidationError(f"--{flag} path does not exist: {path}")
        if not (path.is_file() or path.is_dir()):
            raise InputValidationError(f"--{flag} is neither a ZIP file nor a directory: {path}")
    if args.batch_size < 1:
        raise InputValidationError("--batch-size must be >= 1.")
    if args.sol_start is not None and args.sol_end is not None and args.sol_start > args.sol_end:
        raise InputValidationError("--sol-start must be <= --sol-end.")
    if args.sentinel_registry is not None and not args.sentinel_registry.is_file():
        raise InputValidationError(f"--sentinel-registry is not a file: {args.sentinel_registry}")

    output: Path = args.output
    if output.is_dir():
        raise InputValidationError(f"--output is a directory, expected a file path: {output}")
    report = args.report if args.report is not None else default_report_path(output)
    try:  # input archives / output / report / summary must be four different files
        check_paths(output=output, report=report, calibrated=args.calibrated, derived=args.derived,
                    summary_json=args.summary_json)
    except PathSafetyError as exc:
        raise InputValidationError(f"Unsafe path configuration: {exc}") from exc
    anchor = _nearest_existing(output.parent)
    if not anchor.is_dir():
        raise InputValidationError(f"--output parent is not a directory: {anchor}")
    if not os.access(anchor, os.W_OK | os.X_OK):
        raise InputValidationError(f"Output directory is not writable: {anchor}")
    if output.exists() and not args.overwrite:
        raise InputValidationError(f"Output already exists (use --overwrite to replace): {output}")

    if args.workdir is not None:
        if args.workdir.exists() and not args.workdir.is_dir():
            raise InputValidationError(f"--workdir is not a directory: {args.workdir}")
        anchor = _nearest_existing(args.workdir)
        if not os.access(anchor, os.W_OK | os.X_OK):
            raise InputValidationError(f"--workdir is not writable: {anchor}")
    try:  # can temporary files really be created there?
        probe_dir = str(_nearest_existing(args.workdir)) if args.workdir else None
        with tempfile.TemporaryFile(dir=probe_dir):
            pass
    except OSError as exc:
        raise InputValidationError(f"Cannot create temporary files: {exc}") from exc


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _resolve_root(label: str, source: Path, extract_parent: Path) -> Path:
    if source.is_dir():
        LOG.info("%s archive discovered (directory): %s", label, source)
        return source
    LOG.info("%s archive discovered (ZIP): %s - extracting", label, source)
    target = extract_parent / f"extracted_{label.lower()}"
    return safe_extract_zip(source, target)


def _count_data_lines(path: Path) -> tuple[int | None, str]:
    """Best-effort physical data-line count of an unreadable CSV (header excluded)."""
    try:
        n = 0
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
        return max(n - 1, 0), "newline_count_binary (approximate for quoted newlines)"
    except OSError as exc:
        return None, f"not measurable: {type(exc).__name__}"


def _status_rank(status: str) -> int:
    order = ["SCIENTIFICALLY_VERIFIED", "NUMERICALLY_VALID", "PARTIALLY_VALID", "UNVERIFIED", "INVALID"]
    return order.index(status) if status in order else len(order)


def _check_labels(discoveries: Sequence[DiscoveryResult]) -> tuple[list[str], int]:
    """Cross-check the unit table against every label. Returns (issues, labels_checked)."""
    issues: list[str] = []
    checked = 0
    for discovery in discoveries:
        for product in discovery.products:
            if product.label_path is None:
                issues.append(f"{product.path.name}: no XML label found")
                continue
            try:
                label = parse_label(product.label_path)
                with product.path.open("r", encoding="utf-8-sig", newline="") as stream:
                    header = [h.strip() for h in next(csv.reader(stream), [])]
            except (MedaParseError, OSError, UnicodeDecodeError, csv.Error) as exc:
                issues.append(f"{product.path.name}: label check not possible ({type(exc).__name__})")
                continue
            issues.extend(verify_label_units(product.product, product.source_level, header, label))
            checked += 1
    return sorted(set(issues)), checked


def _record_for(product: ProductFile, archive_name: str | None) -> ProductRecord:
    return ProductRecord(
        product_id=product.path.stem,
        product_name=product.path.name,
        source_archive=archive_name,
        product_type=product.product,
        processing_level=product.source_level,
        sol=product.sol,
        source_csv=product.path.name,
        xml_label=product.label_path.name if product.label_path else None,
        evidence_references=[],
    )


def _isolate_product(ctx: ExecutionContext, rec: ProductRecord, product: ProductFile, exc: Exception,
                     stage: str) -> None:
    rows, method = _count_data_lines(product.path)
    rec.discovery_status = "DISCOVERED"
    rec.parsing_status = "FAILED"
    rec.normalization_status = "SKIPPED"
    rec.validation_status = "NOT_VALIDATED"
    rec.publication_status = "EXCLUDED"
    rec.rows_encountered = rows
    rec.rows_not_processed, rec.rows_not_processed_method = rows, method
    rec.rows_quarantined = rows or 0
    rec.exclusion_reason = f"{type(exc).__name__}: {exc}"
    rec.evidence_references.append({"type": "exception", "message": str(exc)})
    ctx.fail(
        "ERROR", "PRODUCT", stage, rec.product_id, type(exc).__name__.upper(), str(exc),
        evidence={"source_csv": rec.source_csv, "xml_label": rec.xml_label},
        affected_count=rows, isolation_status="ISOLATED", category="PRODUCT_UNREADABLE",
        related_product=f"{rec.product_type}.{rec.processing_level}",
        recovery_recommendation="Repair or re-download this product and re-run; all other products were kept.",
    )
    ctx.exclude("PRODUCT", rec.product_id, rec.exclusion_reason, reason_code=type(exc).__name__,
                rows_not_processed=rows, rows_not_processed_method=method)
    ctx.quarantined.append({"scope": "PRODUCT", "entity_id": rec.product_id, "rows": rows,
                            "reason": rec.exclusion_reason})
    LOG.warning("Product isolated: %s (%s)", rec.product_name, exc)


def _verify_products(
    ctx: ExecutionContext,
    products: list[tuple[ProductFile, ProductRecord]],
    strict: bool,
) -> tuple[list[tuple[ProductFile, ProductRecord]], dict[tuple[str, str], ChannelDecision], dict]:
    """Pre-flight + label verification of every product.

    Returns the products that may enter staging, the per-(file, channel)
    isolation policy, and the aggregated channel table.
    """
    ok: list[tuple[ProductFile, ProductRecord]] = []
    policy: dict[tuple[str, str], ChannelDecision] = {}
    table: dict[tuple[str, str, str], dict] = {}
    flags: dict[str, dict] = {}
    for product, rec in products:
        try:
            info = preflight_product(product)
            label = parse_label(product.label_path)  # type: ignore[arg-type]
            with product.path.open("r", encoding="utf-8-sig", newline="") as stream:
                header = [h.strip() for h in next(csv.reader(stream))]
        except (MedaParseError, OSError, UnicodeDecodeError, csv.Error, StopIteration) as exc:
            if strict and isinstance(exc, MedaParseError):
                raise
            _isolate_product(ctx, rec, product, exc, "product_preflight")
            continue
        rec.rows_encountered = info["physical_rows"]
        rec.label_declared_records = label.declared_table_records
        rec.evidence_references.append({"type": "xml_label", "file": product.label_path.name,  # type: ignore[union-attr]
                                        "fields": len(label.fields), "declared_records": label.declared_table_records})
        specs = CHANNEL_SPECS.get((product.product, product.source_level), ())
        results = verify_channels(product.product, product.source_level, header, label, specs,
                                  label_ref=product.label_path.name)  # type: ignore[union-attr]
        rec.channels_found = len(results)
        for res in results:
            decision: ChannelDecision = res["decision"]
            key = (res["product_type"], res["processing_level"], res["source_channel_name"])
            agg = table.get(key)
            if agg is None:
                agg = {k: v for k, v in res.items() if k != "decision"}
                agg.update({"files_checked": 0, "files_invalid": [], "label_references": []})
                table[key] = agg
            agg["files_checked"] += 1
            if len(agg["label_references"]) < 3:
                agg["label_references"].append(res["xml_label_reference"])
            if _status_rank(res["static_status"]) > _status_rank(agg["static_status"]):
                for k in ("static_status", "scientific_definition_status", "unit_compatibility_status",
                          "data_type_status", "source_unit", "source_data_type", "notes"):
                    agg[k] = res[k]
            if decision.action == "exclude":
                policy[(product.path.name, res["source_channel_name"])] = decision
                agg["files_invalid"].append(product.path.name)
                rec.channels_rejected += 1
                ctx.fail(
                    "ERROR", "CHANNEL", "label_verification", f"{product.path.name}:{res['source_channel_name']}",
                    "CHANNEL_INVALID", "; ".join(res["notes"]) or "channel failed label verification",
                    evidence=res["xml_label_reference"], isolation_status="ISOLATED",
                    category="CHANNEL_UNIT_OR_TYPE", related_product=f"{rec.product_type}.{rec.processing_level}",
                    related_channel=res["source_channel_name"],
                    recovery_recommendation="Channel values are kept raw under quarantined_channels; "
                                            "do not use them as measurements until the label is corrected.",
                )
            else:
                rec.channels_accepted += 1
        for fres in verify_flag_definitions(label, header, FLAG_DEFINITIONS):
            agg = flags.get(fres["flag"])
            if agg is None or (fres["definition_status"] != "VERIFIED" and agg["definition_status"] == "VERIFIED"):
                flags[fres["flag"]] = {**fres, "product_type": product.product, "processing_level": product.source_level}
        rec.parsing_status = "NOT_STARTED"
        ok.append((product, rec))
    ctx.channel_validation = list(table.values())
    ctx.flag_validation = sorted(flags.values(), key=lambda f: f["flag"])
    return ok, policy, table


def _chain_rows(
    products: Sequence[tuple[ProductFile, ProductRecord]], stats: ParseStats, on_error: str,
    ctx: ExecutionContext,
) -> Iterator[dict[str, Any]]:
    seen: set[Path] = set()
    for product, rec in products:
        key = product.path.resolve()
        if key in seen:  # same file reachable from both options
            rec.parsing_status = "SKIPPED"
            rec.exclusion_reason = "duplicate of a file already processed"
            rec.publication_status = "EXCLUDED"
            ctx.exclude("PRODUCT", rec.product_id, rec.exclusion_reason, reason_code="duplicate_path")
            continue
        seen.add(key)
        rec.parsing_status = "PARSING"
        archive = rec.source_archive
        for row in iter_product_file_rows(product, stats=stats, on_error=on_error):
            if archive is not None:
                row["source"]["source_archive"] = archive
            yield row
        name = product.path.name
        rec.rows_encountered = stats.rows_read_by_file.get(name, 0)
        rec.rows_parsed = stats.rows_by_file.get(name, 0)
        rec.rows_rejected = stats.rows_rejected_by_file.get(name, 0)
        rec.parsing_status = "PARTIALLY_PARSED" if rec.rows_rejected else "PARSED"


# --------------------------------------------------------------------------
# Post-staging validation and accounting (all from measured values)
# --------------------------------------------------------------------------


def _identity(ctx: ExecutionContext, rows: list[dict], name: str, expected: Any, actual: Any, explain: str) -> None:
    rows.append({"identity": name, "expected": expected, "actual": actual,
                 "holds": expected == actual, "rule": explain})


def _absorb(ctx: ExecutionContext, summary: StreamSummary, stats: ParseStats | None, published: bool) -> None:
    """Copy measured facts from the stream summary / parser stats into the context."""
    norm = summary.normalization
    ns = norm.as_dict()
    ctx.normalization = ns
    ctx.sclk_metrics = dict(summary.sclk)
    if ctx.sclk_metrics:
        ctx.sclk_metrics.update({
            "valid_sclk_values": summary.parsed_rows,
            "missing_sclk_values": (stats.rejections.get("missing_sclk", 0) if stats else None),
            "invalid_sclk_values": (stats.rejections.get("invalid_sclk", 0) if stats else None),
            "duplicate_identity_count": norm.duplicate_identity_rows,
            "grouping_conflicts": norm.conflicts.get("time_metadata", 0),
            "unresolved_temporal_records": summary.records,
            "unresolved_temporal_reason": "utc is null for every record: no SPICE SCLK/LSK kernels are available, "
                                          "so no absolute time is invented; SCLK is preserved exactly",
            "jsonl_round_trip": summary.jsonl.get("jsonl_round_trip", "NOT_EXECUTED"),
        })
    ctx.provenance_metrics = dict(summary.provenance)
    hits = Counter(norm.sentinel_hits)
    ctx.sentinel_applied = {
        "hits_by_rule": dict(sorted(hits.items())),
        "unverified_candidates": {k.split("|")[0]: v for k, v in hits.items() if k.endswith("|UNVERIFIED")},
        "verified_applied": {k.split("|")[0]: v for k, v in hits.items() if k.endswith("|VERIFIED")},
        "raw_channel_sentinels": dict(sorted(norm.raw_channel_sentinels.items())),
        "raw_values_preserved": True if hits else None,
    }
    for prod in ctx.products.values():
        name = prod.product_name
        prod.rows_accepted = summary.rows_by_file.get(name, 0)
        prod.measurements_generated = norm.created_by_file.get(name, 0)
        prod.measurements_excluded = (norm.quarantined_by_file.get(name, 0) + norm.dup_identical_by_file.get(name, 0)
                                      + norm.dup_conflict_by_file.get(name, 0))
        prod.measurements_published = prod.measurements_generated if published else 0
        if prod.parsing_status in ("PARSED", "PARTIALLY_PARSED"):
            prod.normalization_status = "NORMALIZED" if norm.groups or summary.records else "NOT_STARTED"
            prod.publication_status = "PUBLISHED" if published else "NOT_PUBLISHED"
    # channel final status from the measured value classes
    valid, inval = norm.valid_measurements, norm.invalid_measurements
    for ch in ctx.channel_validation:
        canon = ch["canonical_channel_name"]
        n_valid = valid.get(canon, 0)
        n_nonnum = inval.get(f"{canon}|non_numeric", 0)
        n_sent = inval.get(f"{canon}|invalid_sentinel", 0)
        n_cand = inval.get(f"{canon}|unverified_sentinel_candidate", 0)
        ch["value_validation"] = {"valid": n_valid, "non_numeric": n_nonnum, "verified_sentinel": n_sent,
                                  "unverified_sentinel_candidate": n_cand}
        ch["value_validation_status"] = (
            "NO_VALUES" if (n_valid + n_nonnum + n_sent + n_cand) == 0
            else "ALL_VALID" if (n_nonnum + n_sent + n_cand) == 0 else "SOME_INVALID_OR_UNVERIFIED")
        final = ch["static_status"]
        if final in ("SCIENTIFICALLY_VERIFIED", "NUMERICALLY_VALID") and ch["value_validation_status"] == "SOME_INVALID_OR_UNVERIFIED":
            final = "PARTIALLY_VALID"
        ch["final_channel_status"] = final
        ch["usable_in_analysis"] = {
            "INVALID": "NO - isolated, raw values kept under quarantined_channels",
            "UNVERIFIED": "ONLY WITH CAUTION - unit/meaning not proven (unit_verified=false)",
            "PARTIALLY_VALID": "YES for validity=='valid' values; flagged values must be excluded by the consumer",
            "NUMERICALLY_VALID": "YES numerically; scientific meaning not proven",
            "SCIENTIFICALLY_VERIFIED": "YES",
        }[final]
        ch["quarantined_cells"] = sum(v for k, v in norm.quarantined_channels.items() if k.endswith(f"|{canon}"))


def _run_accounting(ctx: ExecutionContext, summary: StreamSummary, stats: ParseStats) -> bool:
    norm = summary.normalization
    rows: list[dict] = []
    per_file: list[dict] = []
    processed = [p for p in ctx.products.values() if p.parsing_status in ("PARSED", "PARTIALLY_PARSED")]
    for p in processed:
        read = stats.rows_read_by_file.get(p.product_name, 0)
        parsed = stats.rows_by_file.get(p.product_name, 0)
        rej = stats.rows_rejected_by_file.get(p.product_name, 0)
        staged = summary.rows_by_file.get(p.product_name, 0)
        seen = norm.seen_by_file.get(p.product_name, 0)
        buckets = (norm.created_by_file.get(p.product_name, 0) + norm.dup_identical_by_file.get(p.product_name, 0)
                   + norm.dup_conflict_by_file.get(p.product_name, 0) + norm.quarantined_by_file.get(p.product_name, 0))
        entry = {
            "product_id": p.product_id,
            "rows_read_eq_parsed_plus_rejected": read == parsed + rej,
            "rows_read_eq_preflight_physical_rows": read == p.rows_encountered,
            "parsed_eq_staged_eq_normalized": parsed == staged == norm.rows_by_file.get(p.product_name, 0),
            "mapped_cells_eq_buckets": seen == buckets,
        }
        entry["holds"] = all(v for k, v in entry.items() if k != "product_id")
        per_file.append(entry)
    bad_files = [e["product_id"] for e in per_file if not e["holds"]]
    _identity(ctx, rows, "files_all_identities_hold", [], bad_files, "per-product identities (see per_product)")
    _identity(ctx, rows, "rows_read == rows_parsed + rows_rejected", stats.rows_read,
              stats.rows_yielded + stats.rows_rejected, "every CSV data row is parsed or rejected")
    _identity(ctx, rows, "rows_parsed == rows_staged_in_sqlite", stats.rows_yielded, summary.parsed_rows,
              "no row lost between parser and staging")
    _identity(ctx, rows, "rows_staged == rows_normalized", summary.parsed_rows, norm.rows,
              "every staged row reaches normalisation")
    _identity(ctx, rows, "records == exact (sol,sclk) groups", summary.records, norm.groups,
              "one canonical record per exact group")
    _identity(ctx, rows, "records == jsonl records validated", summary.records,
              summary.jsonl.get("records_validated"), "all written records read back")
    _identity(ctx, rows, "mapped non-empty cells == created + duplicates + quarantined",
              norm.mapped_cells_seen,
              norm.measurements_created + norm.cells_duplicate_identical + norm.cells_duplicate_conflict
              + norm.cells_quarantined, "a row yields 0..n measurements; every non-empty mapped cell is accounted for")
    _identity(ctx, rows, "measurements_created == measurements in written records", norm.measurements_created,
              summary.measurements_in_records, "no measurement lost in serialisation")
    _identity(ctx, rows, "measurements in records == jsonl measurements validated", summary.measurements_in_records,
              summary.jsonl.get("measurements_validated"), "read-back count")
    isolated = [p for p in ctx.products.values() if p.parsing_status == "FAILED"]
    leaked = [p.product_id for p in isolated if summary.rows_by_file.get(p.product_name, 0)]
    _identity(ctx, rows, "isolated products contributed no staged rows", [], leaked, "isolation is clean")
    holds = all(r["holds"] for r in rows)
    ctx.accounting = {
        "rules": [
            "rows_read = rows_parsed + rows_rejected (per file and in total)",
            "rows_parsed = rows_staged = rows_normalized",
            "records (exact Sol+SCLK groups) <= rows_parsed; the difference is explained by grouping of rows "
            "that share an exact Sol+SCLK",
            "a row yields zero or more measurements: non-empty mapped cells = measurements_created + "
            "identical duplicates + conflicting duplicates + quarantined cells",
            "measurements_published = measurements_created when published, else 0",
            "rows of isolated products are counted as rows_quarantined (not processed)",
        ],
        "dataset": {
            "rows_encountered": stats.rows_read,
            "rows_parsed": stats.rows_yielded,
            "rows_accepted": summary.parsed_rows,
            "rows_rejected": stats.rows_rejected,
            "rows_quarantined": sum(p.rows_quarantined for p in ctx.products.values()),
            "rows_quarantined_measurable": all(p.rows_not_processed is not None for p in isolated),
            "records_written": summary.records,
            "rows_to_records_explanation": f"{summary.parsed_rows} rows grouped into {summary.records} exact "
                                           f"(Sol, SCLK) records ({summary.parsed_rows - summary.records} rows share a "
                                           f"record with another row)",
            "measurements_generated": norm.measurements_created,
            "measurements_excluded": norm.cells_duplicate_identical + norm.cells_duplicate_conflict + norm.cells_quarantined,
            "measurements_excluded_breakdown": {
                "identical_duplicates": norm.cells_duplicate_identical,
                "conflicting_duplicates": norm.cells_duplicate_conflict,
                "quarantined_channel_cells": norm.cells_quarantined,
            },
            "rejection_reasons": dict(stats.rejections),
        },
        "identities": rows,
        "per_product": per_file,
        "unexplained_differences": [r for r in rows if not r["holds"]],
    }
    return holds


def _run_mandatory_checks(ctx: ExecutionContext, summary: StreamSummary, stats: ParseStats,
                          policy: dict) -> None:
    ins = ctx.archive_inspection
    bad = [i["name"] for i in ins if not i.get("ok", False)]
    ctx.check("archive_integrity", not bad and bool(ins), "every input archive scanned (CRC, paths, duplicates)"
              if not bad else f"archive problems: {bad}", archives=len(ins))
    ok = _run_accounting(ctx, summary, stats)
    ctx.check("record_accounting", ok, "all accounting identities hold" if ok
              else f"{len(ctx.accounting['unexplained_differences'])} unexplained difference(s)")
    j = summary.jsonl
    ctx.check("jsonl_structural_validation", j.get("structural_validation") == "PASSED",
              "every line valid JSON (no NaN/Infinity), keys present, order strictly increasing", **j)
    ctx.check("jsonl_round_trip", j.get("jsonl_round_trip") == "PASSED",
              "identity digest of (sol, sclk) read back equals the digest written")
    sc = summary.sclk or {}
    rt = sc.get("sqlite_round_trip", {})
    ctx.check("sclk_exact_preservation",
              rt.get("result") == "PASSED" and sc.get("original_vs_canonical_mismatches") == 0,
              "SCLK stored as exact INTEGER+TEXT; reconstruction from SQLite equals canonical string for every row",
              **{k: v for k, v in sc.items() if k != "precision_distribution_fraction_digits"})
    pv = summary.provenance or {}
    prov_ok = (bool(pv) and pv.get("incomplete_provenance_count") == 0 and pv.get("missing_provenance_count") == 0
               and pv.get("unresolved_source_references") == 0
               and pv.get("total_measurements") == pv.get("measurements_with_complete_provenance"))
    ctx.check("provenance_complete", prov_ok, "every written measurement links to archive, product, file, channel, row, "
              "SCLK and processing level", **pv)
    leak = (summary.provenance or {}).get("excluded_channel_leaks")
    ctx.check("channel_isolation_enforced", leak == 0,
              f"{len(policy)} (file, channel) pairs isolated; leaked into measurements: {leak}",
              isolated_pairs=len(policy))
    ctx.check("usable_data_present", summary.records > 0 and summary.parsed_rows > 0,
              f"{summary.records} records from {summary.parsed_rows} rows")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_pipeline(args: argparse.Namespace, ctx: ExecutionContext | None = None) -> tuple[dict, StreamSummary]:
    """Execute inspection -> discovery -> verification -> staging -> gate -> publication."""
    ctx = ctx if ctx is not None else ExecutionContext()
    started = time.monotonic()
    LOG.info("Starting MEDA processing (run %s)", ctx.run_id)
    ctx.strict = bool(getattr(args, "strict", False))
    sols = None
    if args.sol_start is not None or args.sol_end is not None:
        sols = (
            args.sol_start if args.sol_start is not None else 0,
            args.sol_end if args.sol_end is not None else 10**9,
        )
        LOG.info("Sol range filter: %s-%s", sols[0], sols[1])
    registry = (SentinelRegistry.from_json(args.sentinel_registry)
                if getattr(args, "sentinel_registry", None) else DEFAULT_SENTINEL_REGISTRY)
    ctx.sentinel_registry = registry.as_list()

    output: Path = args.output
    previous = None
    if output.exists():
        st = output.stat()
        previous = (st.st_size, st.st_mtime_ns)
    summary = StreamSummary()
    ctx.partial_results["stream_summary_ref"] = "see 10_normalization_summary / 11_sclk_validation"
    parse_stats = ParseStats()
    ctx._live = {"summary": summary, "stats": parse_stats}  # type: ignore[attr-defined]

    workdir_parent = args.workdir
    if workdir_parent is not None:
        workdir_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="meda_run_", dir=str(workdir_parent) if workdir_parent else None) as run_dir:
        run_path = Path(run_dir)

        # ---- archive inspection (never raises for a damaged archive) ----
        ctx.begin("archive_inspection")
        inputs: list[tuple[str, Path]] = []
        for label, source in (("Calibrated", args.calibrated), ("Derived", args.derived)):
            if source is None:
                LOG.warning("No %s archive supplied; output will contain no %s products", label, label.lower())
                continue
            inputs.append((label, source))
            info = inspect_source(source)
            info["label"] = label
            ctx.archive_inspection.append(info)
            ctx.input_archives.append({"label": label, "path": str(source), "name": source.name,
                                       "kind": info["kind"], "exists": source.exists(),
                                       "bytes": source.stat().st_size if source.is_file() else None})
        critical = []
        for info in ctx.archive_inspection:
            for key, code in (("corrupt_entries", "ARCHIVE_CORRUPT_ENTRY"), ("unreadable_entries", "ARCHIVE_UNREADABLE_ENTRY"),
                              ("duplicate_paths", "ARCHIVE_DUPLICATE_PATH"), ("unsafe_paths", "ARCHIVE_UNSAFE_PATH"),
                              ("encrypted_entries", "ARCHIVE_ENCRYPTED_ENTRY"), ("discovery_errors", "ARCHIVE_INVALID")):
                items = info[key]
                if items:
                    critical.append(f"{info['name']}: {key}={len(items)}")
                    ctx.fail("CRITICAL", "DATASET", "archive_inspection", info["name"], code,
                             f"{len(items)} {key.replace('_', ' ')} in {info['name']}",
                             evidence=items[:10], affected_count=len(items), isolation_status="NOT_ISOLATABLE",
                             category="SOURCE_INTEGRITY",
                             recovery_recommendation="Re-obtain the archive; source integrity is a precondition.")
            for item in info["csv_without_label"]:
                ctx.warn("CSV_WITHOUT_LABEL", f"{info['name']}: {item}", partial=True, entity=item)
            for item in info["orphan_xml_labels"]:
                ctx.warn("ORPHAN_XML_LABEL", f"{info['name']}: {item}", partial=True, entity=item)
            for item in info["expected_files_missing"][:50]:
                ctx.warn("EXPECTED_FILE_MISSING", f"{info['name']}: Sol {item['sol']} lacks "
                         f"{item['level']}_{item['product']}", partial=True, entity=item)
        if critical:
            raise ArchiveError("Archive inspection failed: " + "; ".join(critical))
        ctx.complete()

        # ---- extraction ----
        ctx.begin("extraction")
        roots: list[tuple[str, Path, Path]] = []
        for label, source in inputs:
            roots.append((label, source, _resolve_root(label, source, run_path)))
        ctx.complete()

        # ---- discovery ----
        ctx.begin("discovery")
        discoveries = [discover_products(root, sols=sols) for _, _, root in roots]
        csv_found = sum(d.csv_found for d in discoveries)
        recognized = sum(len(d.products) for d in discoveries)
        skipped = [(src.name, s) for (_, src, _), d in zip(roots, discoveries) for s in d.skipped]
        LOG.info("CSV files discovered: %d", csv_found)
        LOG.info("Recognized MEDA products: %d", recognized)
        LOG.info("Files skipped: %d", len(skipped))
        for archive_name, item in skipped:
            benign = item.reason.split(":", 1)[0] in BENIGN_SKIP_REASONS
            ctx.exclude("PRODUCT", item.path.name, item.reason, reason_code=item.reason.split(":", 1)[0],
                        source_archive=archive_name, benign=benign)
            if not benign:
                LOG.warning("Skipped %s (%s)", item.path.name, item.reason)
        if recognized == 0:
            raise InputValidationError("No recognised MEDA product files were found in the inputs.")
        for (label, _, _), discovery in zip(roots, discoveries):
            wrong = [p for p in discovery.products if p.source_level != label.lower()]
            if wrong:
                LOG.warning("%s input contains %d product file(s) of the other processing level "
                            "(level is taken from the file name)", label, len(wrong))
        by_level: dict[str, int] = {}
        for d in discoveries:
            for (prod, level), count in d.counts_by_product().items():
                by_level[f"{prod}.{level}"] = by_level.get(f"{prod}.{level}", 0) + count
        ctx.discovery = {
            "csv_files_discovered": csv_found, "products_recognized": recognized,
            "products_recognized_by_type": dict(sorted(by_level.items())),
            "files_skipped": [{"file": s.path.name, "reason": s.reason, "archive": a} for a, s in skipped],
            "orphan_labels_after_discovery": [str(p.name) for d in discoveries for p in d.orphan_labels],
        }
        ordered: list[tuple[ProductFile, ProductRecord]] = []
        for (label, src, _), discovery in zip(roots, discoveries):
            for product in discovery.products:
                rec = _record_for(product, src.name)
                ctx.products[rec.product_id + f"@{src.name}"] = rec
                ordered.append((product, rec))
        ctx.complete()

        # ---- pre-flight + label verification (product / channel isolation) ----
        ctx.begin("product_preflight_and_label_verification")
        unit_issues, labels_checked = _check_labels(discoveries)
        for issue in unit_issues:
            LOG.warning("Label check: %s", issue)
            ctx.warn("LABEL_UNIT_ISSUE", issue, partial=True)
        verified, policy, _table = _verify_products(ctx, ordered, ctx.strict)
        ctx.complete()

        # ---- staging (parse + normalise); NOTHING is published here ----
        ctx.begin("parsing_normalization_staging")
        rows = _chain_rows(verified, parse_stats, "raise" if ctx.strict else "reject", ctx)
        staged_ok = False
        try:
            summary = stream_normalize(
                rows, output, args.batch_size, workdir=run_path, overwrite=args.overwrite,
                publish=False, sentinel_registry=registry, channel_policy=policy,
                require_archive_provenance=True, summary=summary,
            )
            staged_ok = True
        finally:  # runs on any failure too: keep what was measured, then the exception propagates
            ctx._live = {"summary": summary, "stats": parse_stats}  # type: ignore[attr-defined]
            if not staged_ok:
                for rec in ctx.products.values():
                    if rec.parsing_status == "PARSING":
                        rec.parsing_status = "FAILED"
                        rec.exclusion_reason = "run aborted while this product was being parsed"
                ctx.partial_results["products_state_at_failure"] = dict(
                    Counter(p.parsing_status for p in ctx.products.values()))
                _absorb(ctx, summary, parse_stats, False)
        ctx.complete()
        LOG.info("Parsing completed")
        LOG.info("Normalization completed")

        # ---- mandatory validation, acceptance, publication gate ----
        ctx.begin("mandatory_validation")
        _absorb(ctx, summary, parse_stats, False)
        for rec in ctx.products.values():
            if rec.parsing_status in ("PARSED", "PARTIALLY_PARSED"):
                rec.validation_status = "VALIDATED"
        for rec in ctx.products.values():
            if rec.parsing_status == "PARTIALLY_PARSED":
                ctx.fail("WARNING", "RECORD", "parsing", rec.product_id, "ROWS_REJECTED",
                         f"{rec.rows_rejected} row(s) rejected", evidence=dict(parse_stats.rejections_by_file.get(rec.product_name, {})),
                         affected_count=rec.rows_rejected, isolation_status="ISOLATED", category="RECORD_INVALID",
                         related_product=f"{rec.product_type}.{rec.processing_level}",
                         recovery_recommendation="Rejected rows are listed by reason; the rest of the product is kept.")
        for key, count in parse_stats.warnings.items():
            if key == "label_name_mismatch":
                ctx.warn(key.upper(), f"{count} CSV column name(s) differ from the label spelling "
                         f"(matched by position)", count=count)
            else:
                ctx.warn(key.upper(), f"{count} occurrence(s)", count=count, partial=key not in ("label_name_mismatch",))
        for rec in ctx.products.values():
            declared = rec.label_declared_records
            if declared is not None and rec.rows_encountered is not None and rec.parsing_status in ("PARSED", "PARTIALLY_PARSED") \
                    and declared != rec.rows_encountered:
                ctx.fail("ERROR", "PRODUCT", "validation", rec.product_id, "LABEL_RECORD_COUNT_MISMATCH",
                         f"label declares {declared} records, CSV has {rec.rows_encountered}",
                         evidence={"declared": declared, "csv_rows": rec.rows_encountered},
                         affected_count=abs(declared - rec.rows_encountered), isolation_status="ISOLATED",
                         category="PRODUCT_METADATA", related_product=f"{rec.product_type}.{rec.processing_level}",
                         recovery_recommendation="Confirm with the data provider whether rows are missing.")
        _run_mandatory_checks(ctx, summary, parse_stats, policy)
        if all(ch["result"] == "PASS" for ch in ctx.checks if ch["mandatory"]):
            LOG.info("Output validation completed")
        ctx.complete()

        ctx.begin("acceptance")
        # acceptance for the products that failed to parse already holds failures; compute now
        compute_acceptance(ctx)
        ctx.complete()
        LOG.info("Acceptance: %s", ctx.acceptance["status"])

        ctx.begin("publication")
        staged = summary.staged_path
        pub = ctx.publication
        pub["dataset_path"], pub["staged_path"] = str(output), str(staged) if staged else None
        if ctx.acceptance["status"] in (PASS, PARTIAL) and staged is not None:
            size = staged.stat().st_size
            publish_staged(staged, output)
            summary.published = True
            ok = output.is_file() and output.stat().st_size == size and not staged.exists()
            ctx.check("atomic_publication", ok, "staged file replaced the output with os.replace; sizes equal",
                      expected_bytes=size, actual_bytes=output.stat().st_size if output.exists() else None)
            pub.update({"status": "PUBLISHED", "published_bytes": size, "previous_dataset_preserved": None,
                        "reason": f"acceptance {ctx.acceptance['status']}"})
            if not ok:
                compute_acceptance(ctx)
                pub["status"] = "PUBLICATION_VERIFICATION_FAILED"
            _absorb(ctx, summary, parse_stats, True)
        else:
            rejected = None
            if staged is not None and not args.discard_rejected:
                rejected = output.with_name(f"{output.name}.rejected.{ctx.run_id}.jsonl")
                os.replace(staged, rejected)
            else:
                discard_staged(staged)
            now = output.stat() if output.exists() else None
            pub.update({
                "status": "NOT_PUBLISHED", "reason": "; ".join(ctx.acceptance["reasons"])[:500],
                "rejected_staging_kept_as": str(rejected) if rejected else None,
                "previous_dataset_preserved": (None if previous is None else
                                               (now is not None and (now.st_size, now.st_mtime_ns) == previous)),
            })
            ctx.recommendations.append("Dataset NOT published; the previous dataset (if any) was left untouched.")
        ctx.complete()
        # staging directory removal happens when the context manager exits

    for fail_prod in (p for p in ctx.products.values() if p.parsing_status == "FAILED"):
        ctx.recommendations.append(f"Repair product {fail_prod.product_name}: {fail_prod.exclusion_reason}")
    if any(c.get("final_channel_status") == "UNVERIFIED" for c in ctx.channel_validation):
        ctx.recommendations.append("Obtain the SIS/label evidence for UNVERIFIED channel units (e.g. relative humidity) "
                                   "and record it in the channel table before treating them as verified.")
    if ctx.sentinel_applied.get("unverified_candidates"):
        ctx.recommendations.append("Attach the SIS excerpt for the UNVERIFIED sentinel rules and promote them to VERIFIED "
                                   "with --sentinel-registry; until then candidate values are kept and flagged.")

    sentinel_summary = ctx.sentinel_applied
    legacy = {
        "csv_files_discovered": csv_found,
        "products_recognized": recognized,
        "products_recognized_by_type": dict(sorted(by_level.items())),
        "files_skipped": [{"file": s.path.name, "reason": s.reason} for _, s in skipped],
        "labels_checked": labels_checked,
        "label_unit_issues": unit_issues,
        "rows_read": parse_stats.rows_read,
        "rows_parsed": parse_stats.rows_yielded,
        "rows_rejected": parse_stats.rows_rejected,
        "rejection_reasons": dict(parse_stats.rejections),
        "rejection_samples": parse_stats.rejection_samples,
        "parser_warnings": dict(parse_stats.warnings),
        "parser_warning_samples": parse_stats.warning_samples,
        "canonical_records": summary.records,
        "sol_coverage": summary.sol_coverage(),
        "output": str(args.output),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stream": summary.as_dict(),
        "acceptance": ctx.acceptance["status"],
        "acceptance_reasons": ctx.acceptance["reasons"],
        "run_id": ctx.run_id,
        "sentinel": sentinel_summary,
        "products_isolated": [p.product_id for p in ctx.products.values() if p.parsing_status == "FAILED"],
    }
    return legacy, summary


def _write_summary_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        os.replace(temp, path)
        replaced = True
    finally:  # no broad ``except``: clean the temp file on any failure, never swallow it
        if not replaced:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def _print_report(report: dict) -> None:
    print(f"Files discovered (CSV): {report['csv_files_discovered']:,}")
    print(f"Files recognized: {report['products_recognized']:,}")
    print(f"Files skipped: {len(report['files_skipped']):,}")
    print(f"Rows parsed: {report['rows_parsed']:,}")
    print(f"Rows rejected: {report['rows_rejected']:,} {report['rejection_reasons'] or ''}".rstrip())
    print(f"Products isolated: {len(report.get('products_isolated', []))}")
    print(f"Canonical records (exact Sol + SCLK): {report['canonical_records']:,}")
    print(f"Sol coverage: {report['sol_coverage']}")
    print(f"Acceptance: {report.get('acceptance')}")
    print(f"Duration: {report['elapsed_seconds']} s")
    print(f"Output: {report['output']}")


def _exit_for_exception(exc: BaseException) -> ExitCode:
    if isinstance(exc, (InputValidationError, ArchiveError, FileExistsError)):
        return ExitCode.INVALID_INPUT
    if isinstance(exc, MedaParseError):
        return ExitCode.PARSING_FAILURE
    if isinstance(exc, (NormalizationError, ContractError)):
        return ExitCode.NORMALIZATION_FAILURE
    if isinstance(exc, OutputValidationError):
        return ExitCode.VALIDATION_FAILURE
    if isinstance(exc, (OSError, sqlite3.Error)):
        return ExitCode.OUTPUT_FAILURE
    return ExitCode.UNEXPECTED


_LOG_LABEL = {
    InputValidationError: "Invalid input", ArchiveError: "Invalid archive", FileExistsError: "Invalid input",
    MedaParseError: "Parsing failure", NormalizationError: "Normalization failure",
    ContractError: "Normalization failure", OutputValidationError: "Output validation failure",
}


def _finalize(ctx: ExecutionContext, args: argparse.Namespace, exit_code: ExitCode) -> ExitCode:
    """Compute the final acceptance and ALWAYS try to write the report (PASS / PARTIAL / FAIL)."""
    ctx.finish()
    compute_acceptance(ctx)
    status = ctx.acceptance["status"]
    if status == FAIL and ctx.publication["status"] in ("NOT_ATTEMPTED",):
        ctx.publication.update({"status": "NOT_PUBLISHED", "reason": "; ".join(ctx.acceptance["reasons"])[:500],
                                "dataset_path": str(args.output)})
    live = getattr(ctx, "_live", None)
    if live is not None and ctx.exception is not None:
        ctx.partial_results["stream_summary"] = live["summary"].as_dict()
        st: ParseStats = live["stats"]
        ctx.partial_results["parse_stats"] = {"rows_read": st.rows_read, "rows_yielded": st.rows_yielded,
                                              "rows_rejected": st.rows_rejected, "rejections": dict(st.rejections)}
    report_path = args.report if args.report is not None else default_report_path(args.output)
    if status == FAIL:
        report_path = failed_report_path(report_path, ctx.run_id)
    ctx.report_publication["path"] = str(report_path)
    try:
        write_report_atomic(report_path, build_report(ctx))
        ctx.report_publication.update({"status": "WRITTEN", "error": None})
        # the report was written before this status existed: rewrite so it describes itself
        write_report_atomic(report_path, build_report(ctx))
    except (OSError, TypeError, ValueError) as exc:
        ctx.report_publication.update({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
        LOG.error("Report publication failed: %s (dataset state: %s)", exc, ctx.publication["status"])
        if exit_code in (ExitCode.SUCCESS, ExitCode.PARTIAL):
            exit_code = ExitCode.OUTPUT_FAILURE
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code (never calls sys.exit)."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help -> 0, usage error -> 2
        return int(exc.code) if isinstance(exc.code, int) else int(ExitCode.INVALID_INPUT)

    logging.basicConfig(level=getattr(logging, args.log_level), format="[%(levelname)s] %(message)s",
                        stream=sys.stderr, force=True)
    ctx = ExecutionContext()
    ctx.strict = bool(args.strict)
    ctx.arguments = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    ctx.begin("argument_validation")
    try:
        validate_arguments(args)
    except InputValidationError as exc:  # refused before any run: no report, nothing touched
        LOG.error("Invalid input: %s", exc)
        return int(ExitCode.INVALID_INPUT)
    ctx.check("path_safety", True, "output, report, summary and input archives are four distinct resolved paths; "
              "output/report are not inside an input directory")
    ctx.complete()

    legacy: dict | None = None
    exit_code = ExitCode.SUCCESS
    try:
        legacy, _summary = run_pipeline(args, ctx)
    except BaseException as exc:  # noqa: BLE001 - recorded, mapped, never swallowed
        if isinstance(exc, KeyboardInterrupt):
            ctx.record_exception(exc)
            _finalize(ctx, args, ExitCode.UNEXPECTED)
            raise
        ctx.record_exception(exc)
        exit_code = _exit_for_exception(exc)
        label = next((v for k, v in _LOG_LABEL.items() if isinstance(exc, k)), None)
        if label:
            LOG.error("%s: %s", label, exc)
        elif isinstance(exc, (OSError, sqlite3.Error)):
            LOG.error("Output/storage failure: %s", exc)
        else:
            LOG.exception("Unexpected error: %s", exc)
        _finalize(ctx, args, exit_code)
        return int(exit_code)

    status = ctx.acceptance["status"]
    exit_code = {PASS: ExitCode.SUCCESS, PARTIAL: ExitCode.PARTIAL, FAIL: ExitCode.ACCEPTANCE_FAILURE}[status]
    exit_code = _finalize(ctx, args, exit_code)
    status = ctx.acceptance["status"]
    legacy["acceptance"] = status
    legacy["report"] = ctx.report_publication.get("path")
    try:
        if args.summary_json is not None:
            _write_summary_json(args.summary_json, legacy)
    except OSError as exc:
        LOG.error("Summary JSON failed: %s", exc)
        exit_code = ExitCode.OUTPUT_FAILURE

    if legacy["rows_rejected"]:
        LOG.warning("%d row(s) were rejected; see the summary for reasons", legacy["rows_rejected"])
    LOG.info("Processing finished: acceptance=%s", status)
    _print_report(legacy)
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main())
