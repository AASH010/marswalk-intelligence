"""Command line interface: orchestration only (no science, no parsing here).

Run (from the directory that CONTAINS the ``meda_weather`` package folder,
e.g. ``backend/app/services/weather``; no particular working directory is
otherwise required)::

    python -m meda_weather.cli \\
        --calibrated /data/MEDA_Weather_ds.zip \\
        --derived    /data/derived.zip \\
        --output     /data/out/meda_normalized.jsonl

or, from ``backend/``::

    python -m app.services.weather.meda_weather.cli --calibrated ... --output ...

Each of ``--calibrated`` / ``--derived`` may be a ZIP archive or an already
extracted directory (any layout: ``sol_0230/`` folders, ``derived/<PROD>/``...).
The processing level of a file always comes from its own name (CAL_/DER_),
never from which option it was passed through.

Defaults: logging INFO, batch size 5000, all Sols found, system temp
directory as working directory, refuse to replace an existing output unless
``--overwrite`` is given, rejected CSV rows are counted and reported but do
not abort the run unless ``--strict`` is given.

Exit codes: 0 success | 1 unexpected error | 2 invalid input |
3 parsing failure | 4 normalisation failure | 5 output failure |
6 output validation failure.
"""
from __future__ import annotations

import argparse
import hashlib
import platform
import csv
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
import zipfile
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterator, Sequence

from .normalize import CHANNEL_SPECS, NormalizationError, verify_label_units
from .parser import (
    ArchiveError,
    DiscoveryResult,
    MedaParseError,
    ParseStats,
    discover_products,
    identify_file,
    iter_product_file_rows,
    parse_label,
    safe_extract_zip,
)
from .streaming import (
    ContractError,
    OutputValidationError,
    StreamSummary,
    stream_normalize,
)

LOG = logging.getLogger("meda_weather")


class ExitCode(IntEnum):
    SUCCESS = 0
    UNEXPECTED = 1
    INVALID_INPUT = 2
    PARSING_FAILURE = 3
    NORMALIZATION_FAILURE = 4
    OUTPUT_FAILURE = 5
    VALIDATION_FAILURE = 6


class InputValidationError(Exception):
    """Raised for bad arguments / unusable input paths (exit code 2)."""


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m meda_weather.cli",
        description=(
            "Parse MEDA calibrated + derived CSV/XML products, normalise them by exact "
            "(Sol, SCLK) and write a validated JSONL file."
        ),
        epilog=(
            "Example usage\n"
            "  python -m meda_weather.cli --calibrated MEDA_Weather_ds.zip "
            "--derived derived.zip --output out/meda.jsonl --sol-start 230 --sol-end 239\n\n"
            "exit codes: 0 ok, 1 unexpected, 2 invalid input, 3 parsing failure, "
            "4 normalisation failure, 5 output failure, 6 validation failure"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--calibrated", type=Path, help="Calibrated archive: ZIP file or extracted directory.")
    parser.add_argument("--derived", type=Path, help="Derived archive: ZIP file or extracted directory.")
    parser.add_argument("--output", type=Path, required=True, help="Path of the JSONL file to write.")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="Temporary working directory for extraction and staging (default: system temp).")
    parser.add_argument("--batch-size", type=int, default=5000, help="SQLite staging batch size (default: 5000).")
    parser.add_argument("--sol-start", type=int, default=None, help="First Sol to process (inclusive).")
    parser.add_argument("--sol-end", type=int, default=None, help="Last Sol to process (inclusive).")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity (default: INFO).")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing output file.")
    parser.add_argument("--strict", dest="strict", action="store_true", default=True,
                        help="Strict mode (default): abort on rejected CSV rows.")
    parser.add_argument("--no-strict", dest="strict", action="store_false",
                        help="Continue while recording rejected rows; scientific validation remains mandatory.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Validation report JSON (default: meda_validation_report.json beside output).")
    parser.add_argument("--summary-json", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _nearest_existing(path: Path) -> Path:
    path = path.resolve() if path.exists() else path.absolute()
    while not path.exists():
        if path.parent == path:
            break
        path = path.parent
    return path


def validate_arguments(args: argparse.Namespace) -> None:
    """Raise ``InputValidationError`` with a precise message on any problem."""
    if args.calibrated is None and args.derived is None:
        raise InputValidationError("At least one of --calibrated / --derived is required.")
    for flag in ("calibrated", "derived"):
        path: Path | None = getattr(args, flag)
        if path is None:
            continue
        if not path.exists():
            raise InputValidationError(f"--{flag} path does not exist: {path}")
        if path.is_file():
            if not zipfile.is_zipfile(path):
                raise InputValidationError(f"--{flag} is a file but not a valid ZIP archive: {path}")
        elif not path.is_dir():
            raise InputValidationError(f"--{flag} is neither a ZIP file nor a directory: {path}")
    if args.batch_size < 1:
        raise InputValidationError("--batch-size must be >= 1.")
    if args.sol_start is not None and args.sol_end is not None and args.sol_start > args.sol_end:
        raise InputValidationError("--sol-start must be <= --sol-end.")

    output: Path = args.output
    if output.is_dir():
        raise InputValidationError(f"--output is a directory, expected a file path: {output}")
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
# Orchestration
# --------------------------------------------------------------------------


def _resolve_root(label: str, source: Path, extract_parent: Path) -> Path:
    if source.is_dir():
        LOG.info("%s archive discovered (directory): %s", label, source)
        return source
    LOG.info("%s archive discovered (ZIP): %s - extracting", label, source)
    target = extract_parent / f"extracted_{label.lower()}"
    return safe_extract_zip(source, target)


def _check_labels(discoveries: Sequence[DiscoveryResult]) -> tuple[list[str], int]:
    """Cross-check the unit table against every label. Returns (issues, labels_checked)."""
    issues: list[str] = []
    checked = 0
    for discovery in discoveries:
        for product in discovery.products:
            if product.label_path is None:
                issues.append(f"{product.path.name}: no XML label found")
                continue
            label = parse_label(product.label_path)
            with product.path.open("r", encoding="utf-8-sig", newline="") as stream:
                header = [h.strip() for h in next(csv.reader(stream), [])]
            issues.extend(verify_label_units(product.product, product.source_level, header, label))
            checked += 1
    return sorted(set(issues)), checked


def _chain_rows(
    discoveries: Sequence[DiscoveryResult], stats: ParseStats, on_error: str
) -> Iterator[dict[str, Any]]:
    seen: set[Path] = set()
    for discovery in discoveries:
        for product in discovery.products:
            if product.path.resolve() in seen:  # same file reachable from both options
                continue
            seen.add(product.path.resolve())
            yield from iter_product_file_rows(product, stats=stats, on_error=on_error)


def run_pipeline(args: argparse.Namespace) -> tuple[dict, StreamSummary]:
    """Execute discovery -> parsing -> streaming -> normalisation -> output."""
    started = time.monotonic()
    LOG.info("Starting MEDA processing")
    sols = None
    if args.sol_start is not None or args.sol_end is not None:
        sols = (
            args.sol_start if args.sol_start is not None else 0,
            args.sol_end if args.sol_end is not None else 10**9,
        )
        LOG.info("Sol range filter: %s-%s", sols[0], sols[1])

    workdir_parent = args.workdir
    if workdir_parent is not None:
        workdir_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="meda_run_", dir=str(workdir_parent) if workdir_parent else None) as run_dir:
        run_path = Path(run_dir)

        roots: list[tuple[str, Path]] = []
        for label, source in (("Calibrated", args.calibrated), ("Derived", args.derived)):
            if source is None:
                LOG.warning("No %s archive supplied; output will contain no %s products", label, label.lower())
                continue
            roots.append((label, _resolve_root(label, source, run_path)))

        discoveries = [discover_products(root, sols=sols) for _, root in roots]
        csv_found = sum(d.csv_found for d in discoveries)
        recognized = sum(len(d.products) for d in discoveries)
        skipped = [s for d in discoveries for s in d.skipped]
        LOG.info("CSV files discovered: %d", csv_found)
        LOG.info("Recognized MEDA products: %d", recognized)
        LOG.info("Files skipped: %d", len(skipped))
        filtered = [s for s in skipped if s.reason == "outside_sol_range"]
        if filtered:
            LOG.info("Files outside the requested Sol range: %d", len(filtered))
        for item in skipped:
            if item.reason != "outside_sol_range":
                LOG.warning("Skipped %s (%s)", item.path.name, item.reason)
        if recognized == 0:
            raise InputValidationError("No recognised MEDA product files were found in the inputs.")
        for (label, _), discovery in zip(roots, discoveries):
            wrong = [p for p in discovery.products
                     if p.source_level != label.lower()]
            if wrong:
                LOG.warning("%s input contains %d product file(s) of the other processing level "
                            "(level is taken from the file name)", label, len(wrong))

        unit_issues, labels_checked = _check_labels(discoveries)
        for issue in unit_issues:
            LOG.warning("Label check: %s", issue)
        if args.strict and unit_issues:
            raise NormalizationError(
                "Scientific label/unit validation failed before publication: " +
                "; ".join(unit_issues[:5])
            )

        parse_stats = ParseStats()
        rows = _chain_rows(discoveries, parse_stats, "raise" if args.strict else "reject")
        summary = stream_normalize(
            rows,
            args.output,
            args.batch_size,
            workdir=run_path,
            overwrite=args.overwrite,
        )

    LOG.info("Parsing completed")
    LOG.info("Normalization completed")
    LOG.info("Output validation completed")

    by_level: dict[str, int] = {}
    for d in discoveries:
        for (product, level), count in d.counts_by_product().items():
            by_level[f"{product}.{level}"] = by_level.get(f"{product}.{level}", 0) + count

    report = {
        "csv_files_discovered": csv_found,
        "products_recognized": recognized,
        "products_recognized_by_type": dict(sorted(by_level.items())),
        "product_details": [
            {"product_identifier": p.path.stem, "source_archive": label,
             "product_type": p.product, "processing_level": p.source_level,
             "csv_file": p.path.name, "xml_label_file": p.label_path.name if p.label_path else None,
             "label_association": "ASSOCIATED" if p.label_path else "MISSING",
             "discovery_status": "DISCOVERED", "parsing_status": "PENDING",
             "relevant_metadata": {}}
            for (label, _), d in zip(roots, discoveries) for p in d.products
        ],
        "files_skipped": [{"file": s.path.name, "reason": s.reason} for s in skipped],
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
    }
    return report, summary


def _write_summary_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
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
    print(f"Canonical records (exact Sol + SCLK): {report['canonical_records']:,}")
    print(f"Sol coverage: {report['sol_coverage']}")
    print(f"Duration: {report['elapsed_seconds']} s")
    print(f"Output: {report['output']}")



def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_inspection(path: Path | None) -> dict:
    if path is None:
        return {"provided": False}
    item = {"provided": True, "name": path.name, "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path) if path.is_file() else None}
    if path.is_file():
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            item.update({"readable": True, "entries": len(zf.infolist()),
                         "relevant_meda_files": sum(bool(identify_file(Path(n))) for n in names),
                         "xml_labels": sum(n.lower().endswith(".xml") for n in names),
                         "csv_files": sum(n.lower().endswith(".csv") for n in names),
                         "unrelated_files_ignored": sum(not n.lower().endswith((".csv", ".xml")) for n in names),
                         "corrupt_or_unreadable_entries": []})
    else:
        files = [p for p in path.rglob("*") if p.is_file()]
        item.update({"readable": True, "entries": len(files),
                     "relevant_meda_files": sum(bool(identify_file(p)) for p in files),
                     "xml_labels": sum(p.suffix.lower()==".xml" for p in files),
                     "csv_files": sum(p.suffix.lower()==".csv" for p in files),
                     "unrelated_files_ignored": sum(p.suffix.lower() not in (".csv",".xml") for p in files),
                     "corrupt_or_unreadable_entries": []})
    return item


def _atomic_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        LOG.exception("Atomic report publication failed for %s", path)
        try: os.unlink(temp)
        except OSError: pass
        raise


def _base_validation_report(args: argparse.Namespace, started_at: str) -> dict:
    return {
        "report_schema_version": "1.0.0",
        "pipeline_package_version": "unknown",
        "execution": {"status": "FAILED", "started_at": started_at, "ended_at": None,
                      "duration_seconds": None, "python_version": sys.version,
                      "operating_system": platform.platform(), "strict_mode": bool(args.strict),
                      "cli_arguments": {"calibrated": str(args.calibrated) if args.calibrated else None,
                                        "derived": str(args.derived) if args.derived else None,
                                        "output": str(args.output), "batch_size": args.batch_size,
                                        "sol_start": args.sol_start, "sol_end": args.sol_end}},
        "input_archive_inspection": {},
        "products": {"discovered": 0, "parsed": 0, "details": []},
        "scientific_validation": {"status": "UNKNOWN", "label_unit_issues": [], "unknown_definitions_not_passed": True},
        "sentinel_and_missing_values": {"status": "UNKNOWN", "note": "Channel-specific sentinel accounting is not exposed by the current parser/normalizer."},
        "sclk_temporal_integrity": {"status": "UNKNOWN", "note": "Exact SCLK representation is retained by parser/streaming contracts; full distributions are not yet emitted."},
        "normalization_statistics": {},
        "provenance_validation": {"status": "UNKNOWN"},
        "final_dataset_integrity": {"output_exists": False, "publication_status": "NOT_PUBLISHED"},
        "warnings_and_errors": [],
        "verdict": "FAILED"
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point with a failure-safe, atomic validation report."""
    cli_parser = build_parser()
    try:
        args = cli_parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else int(ExitCode.INVALID_INPUT)

    report_path = args.report or (args.output.parent / "meda_validation_report.json")
    started_wall = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    validation = _base_validation_report(args, started_wall.isoformat())
    exit_code = int(ExitCode.SUCCESS)
    error = None
    logging.basicConfig(level=getattr(logging, args.log_level), format="[%(levelname)s] %(message)s",
                        stream=sys.stderr, force=True)
    try:
        # Input fingerprints/inspection are captured before processing so failure reports remain useful.
        validate_arguments(args)
        for key, path in (("calibrated", args.calibrated), ("derived", args.derived)):
            if path is not None:
                try:
                    validation["input_archive_inspection"][key] = _archive_inspection(path)
                except Exception as exc:
                    LOG.exception("Archive inspection failed for %s", path)
                    validation["input_archive_inspection"][key] = {
                        "name": path.name, "readable": False, "discovery_error": str(exc)}
                    raise
        run_report, summary = run_pipeline(args)
        validation["execution"]["status"] = "COMPLETED"
        validation["products"] = {
            "discovered": run_report["products_recognized"],
            "parsed": run_report["products_recognized"],
            "details": run_report["product_details"]
        }
        channel_mappings = []
        for (product_type, level), specs in sorted(CHANNEL_SPECS.items()):
            for spec in specs:
                channel_mappings.append({
                    "source_product_type": product_type,
                    "processing_level": level,
                    "source_channel_name": spec.raw_name,
                    "canonical_field_name": spec.canonical,
                    "source_unit": spec.label_unit,
                    "canonical_unit": spec.unit,
                    "unit_basis": spec.unit_basis,
                    "scientific_definition_status": (
                        "UNKNOWN" if spec.unit_basis == "not_stated" else
                        "PARTIALLY_DOCUMENTED" if spec.unit_basis == "bundle_description" else "LABEL_DEFINED"),
                    "evidence_reference": spec.note or "PDS4 XML label field definition",
                    "validation_result": "UNKNOWN" if spec.unit_basis == "not_stated" else "PARTIALLY_VALIDATED"
                })
        validation["scientific_validation"] = {
            "status": "PARTIALLY_VALIDATED" if not run_report["label_unit_issues"] else "FAILED",
            "label_unit_issues": run_report["label_unit_issues"],
            "labels_checked": run_report["labels_checked"],
            "channel_mappings": channel_mappings,
            "unknown_definitions_not_passed": True,
            "limitations": [
                "XML label/unit cross-check is executed, but semantic validation of every scientific quantity is not complete.",
                "Definitions with unit_basis=not_stated remain UNKNOWN and are not represented as PASSED."
            ]
        }
        validation["normalization_statistics"] = {
            "source_rows_encountered": run_report["rows_read"],
            "rows_accepted": run_report["rows_parsed"],
            "rows_rejected": run_report["rows_rejected"],
            "canonical_records": run_report["canonical_records"],
            "written_records": summary.records,
            "sol_coverage": run_report["sol_coverage"],
            "by_product_and_level": run_report["products_recognized_by_type"],
            "rejection_reasons": run_report["rejection_reasons"],
            "stream": run_report["stream"]
        }
        norm_stats = run_report["stream"].get("normalization", {})
        validation["sentinel_and_missing_values"] = {
            "status": "PARTIALLY_VALIDATED",
            "sentinel_rules_declared_by_current_normalizer": [str(v) for v in sorted(getattr(__import__("meda_weather.normalize", fromlist=["SENTINEL_VALUES"]), "SENTINEL_VALUES"))],
            "sentinel_rule_basis": "Existing normalize.py SENTINEL_BASIS; source documentation is not fully reconciled per product/channel.",
            "raw_channel_sentinels": norm_stats.get("raw_channel_sentinels", {}),
            "missing_counts": norm_stats.get("missing", {}),
            "invalid_counts": norm_stats.get("invalid_measurements", {}),
            "valid_numeric_counts": norm_stats.get("valid_measurements", {}),
            "duplicate_identical_count": norm_stats.get("duplicates_identical", 0),
            "conflicts": norm_stats.get("conflicts", {}),
            "unmapped_columns": norm_stats.get("unmapped_columns", {}),
            "limitations": ["Sentinel rules are inherited from the existing normalizer and require product/channel source-level audit."]
        }
        validation["sclk_temporal_integrity"] = {
            "status": "PARTIALLY_VALIDATED",
            "exact_string_preservation": True,
            "float_conversion_avoided": True,
            "sqlite_round_trip": "PASSED",
            "jsonl_serialization": "PASSED",
            "record_count": summary.records,
            "sol_coverage": run_report["sol_coverage"],
            "note": "Additional distributions and duplicate/group-conflict counts are not exposed by current pipeline counters."
        }
        validation["provenance_validation"] = {
            "status": "PARTIALLY_VALIDATED",
            "source_rows_with_provenance": run_report["rows_parsed"],
            "missing_provenance_count": None,
            "note": "Parser attaches source product/file/channel references; exhaustive per-measurement completeness counts are not currently exposed."
        }
        output = args.output
        validation["final_dataset_integrity"] = {
            "output_exists": output.is_file(),
            "jsonl_readable": True,
            "valid_json_lines": summary.records,
            "invalid_json_lines": 0,
            "required_fields_validation": "PASSED",
            "measurement_structure_validation": "PASSED",
            "atomic_publication": "PASSED",
            "publication_status": "PUBLISHED",
            "output_size_bytes": output.stat().st_size,
            "sha256": _sha256_file(output),
            "record_count_consistency": summary.records
        }
        validation["execution"]["output_path"] = str(output)
        validation["execution"]["output_size_bytes"] = output.stat().st_size
        validation["execution"]["output_sha256"] = _sha256_file(output)
        if run_report["rows_rejected"]:
            validation["warnings_and_errors"].append({
                "severity": "WARNING", "error_code": "CSV_ROWS_REJECTED", "stage": "PARSING",
                "description": "One or more source rows were rejected.", "count": run_report["rows_rejected"],
                "publication_prevented": False})
        if validation["scientific_validation"]["status"] == "FAILED":
            raise NormalizationError("Scientific label/unit validation failed: " +
                                     "; ".join(run_report["label_unit_issues"][:5]))
        if args.strict and run_report["rows_rejected"]:
            raise MedaParseError(f"Strict mode rejected {run_report['rows_rejected']} source row(s).")
        validation["execution"]["status"] = "SUCCESS"
        if validation["scientific_validation"]["status"] != "PASSED":
            validation["warnings_and_errors"].append({
                "severity": "WARNING", "error_code": "SCIENTIFIC_VALIDATION_INCOMPLETE",
                "stage": "SCIENTIFIC_VALIDATION",
                "description": "XML/unit checks passed, but not all scientific semantics have source-backed validation.",
                "publication_prevented": False})
        validation["verdict"] = "SUCCESS_WITH_WARNINGS" if validation["warnings_and_errors"] else "SUCCESS"
        if args.summary_json is not None:
            _write_summary_json(args.summary_json, run_report)
    except InputValidationError as exc:
        error, exit_code = exc, int(ExitCode.INVALID_INPUT)
    except ArchiveError as exc:
        error, exit_code = exc, int(ExitCode.INVALID_INPUT)
    except FileExistsError as exc:
        error, exit_code = exc, int(ExitCode.INVALID_INPUT)
    except MedaParseError as exc:
        error, exit_code = exc, int(ExitCode.PARSING_FAILURE)
    except (NormalizationError, ContractError) as exc:
        error, exit_code = exc, int(ExitCode.NORMALIZATION_FAILURE)
    except OutputValidationError as exc:
        error, exit_code = exc, int(ExitCode.VALIDATION_FAILURE)
    except (OSError, sqlite3.Error) as exc:
        error, exit_code = exc, int(ExitCode.OUTPUT_FAILURE)
    except Exception as exc:
        LOG.exception("Unexpected pipeline error: %s", exc)
        error, exit_code = exc, int(ExitCode.UNEXPECTED)

    ended = datetime.now(timezone.utc)
    validation["execution"]["ended_at"] = ended.isoformat()
    validation["execution"]["duration_seconds"] = round(time.monotonic() - started_mono, 6)
    if error is not None:
        validation["execution"]["status"] = "FAILED"
        validation["verdict"] = "FAILED"
        validation["warnings_and_errors"].append({
            "severity": "ERROR", "error_code": type(error).__name__.upper(),
            "stage": "PIPELINE", "description": str(error),
            "publication_prevented": not validation["final_dataset_integrity"].get("output_exists", False)})
        # Never claim a new dataset was published on a failed run.
        if not validation["final_dataset_integrity"].get("output_exists"):
            validation["final_dataset_integrity"]["publication_status"] = "NOT_PUBLISHED"
        LOG.error("MEDA Weather Pipeline: FAILED — %s", error)

    if error is not None:
        if isinstance(error, MedaParseError):
            LOG.error("Parsing failure: %s", error)
        elif isinstance(error, (NormalizationError, ContractError)):
            LOG.error("Normalization failure: %s", error)
        elif isinstance(error, (InputValidationError, ArchiveError, FileExistsError)):
            LOG.error("Invalid input: %s", error)
        elif isinstance(error, (OSError, sqlite3.Error)):
            LOG.error("Output/storage failure: %s", error)
        else:
            LOG.exception("Unexpected error: %s", error)

    try:
        _atomic_report(report_path, validation)
        print(f"Report: {report_path}")
    except Exception as report_exc:
        LOG.exception("Could not publish validation report %s: %s", report_path, report_exc)
        if exit_code == int(ExitCode.SUCCESS):
            exit_code = int(ExitCode.OUTPUT_FAILURE)

    if error is None:
        print("MEDA Weather Pipeline: SUCCESS")
        print(f"Scientific Validation: {validation['scientific_validation']['status']}")
        print("Structural Validation: PASSED")
        print("Data Integrity: PASSED")
        _print_report(run_report)
        print(f"Output: {args.output}")
        LOG.info("Processing finished")
    else:
        print("MEDA Weather Pipeline: FAILED")
        print(f"Stage: {validation['warnings_and_errors'][-1]['stage']}")
        print(f"Reason: {error}")
        print(f"Dataset Published: {'YES' if validation['final_dataset_integrity'].get('output_exists') else 'NO'}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
