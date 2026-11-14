"""Command-line interface of the MEDA Filtering Package.

    python -m meda_filtering.cli \
        --input  meda_normalized.jsonl \
        --output meda_filtered.jsonl \
        --report filtering_report.json \
        [--rules rules_config.json] [--strict]

Exit codes: 0 completed, 1 completed with skipped invalid input lines, 2 fatal error.

Other services can bypass the CLI and call
``meda_filtering.filter.filter_normalized_jsonl`` or import the standalone JSON
schemas directly from ``meda_filtering/schemas/``.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from .filter import (
    FILTERING_PACKAGE_VERSION,
    FilteringConfig,
    FilteringError,
    FilteringStats,
    filter_normalized_jsonl,
)
from .report import build_report, now_utc, write_report_atomic
from .rules import RULESET_VERSION, RulesConfigError, build_ruleset, load_rules_config


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m meda_filtering.cli",
        description=(
            f"MEDA Filtering Package {FILTERING_PACKAGE_VERSION}: reads a Normalized JSONL dataset "
            "(Normalizer v2.1 output) and writes a Filtered JSONL dataset with accepted measurements only, "
            "plus a Filtering Report."
        ),
    )
    p.add_argument("--input", "-i", required=True, metavar="PATH", help="Normalized JSONL file (read-only).")
    p.add_argument("--output", "-o", metavar="PATH",
                   help="Filtered JSONL output. Default: <input stem>_filtered.jsonl next to the input.")
    p.add_argument("--report", "-r", metavar="PATH",
                   help="Filtering Report JSON. Default: <output>.report.json.")
    p.add_argument("--rules", metavar="PATH",
                   help="Optional rules-configuration JSON (policies enabled with evidence, source selection, "
                        "justified rule disabling). Without it only the default evidence-backed rules run.")
    p.add_argument("--strict", action="store_true",
                   help="Strict validation: the first invalid input line aborts the run and no output is published.")
    p.add_argument("--skip-input-validation", action="store_true",
                   help="Do not validate input records against normalized_schema.json.")
    p.add_argument("--no-output-validation", action="store_true",
                   help="Do not validate written records against filtered_schema.json.")
    p.add_argument("--version", action="version",
                   version=f"meda_filtering {FILTERING_PACKAGE_VERSION} (ruleset {RULESET_VERSION})")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    src = Path(args.input)
    if not src.is_file():
        print(f"ERROR: input file not found: {src}", file=sys.stderr)
        return 2
    dst = Path(args.output) if args.output else src.with_name(src.stem + "_filtered.jsonl")
    rep = Path(args.report) if args.report else dst.with_name(dst.name + ".report.json")
    for label, path in (("output", dst), ("report", rep)):
        if path.resolve() == src.resolve():
            print(f"ERROR: --{label} must not be the input file", file=sys.stderr)
            return 2
    if dst.resolve() == rep.resolve():
        print("ERROR: --output and --report must be different files", file=sys.stderr)
        return 2

    try:
        cfg_json = load_rules_config(args.rules) if args.rules else None
        ruleset = build_ruleset(cfg_json, config_path=args.rules)
    except RulesConfigError as exc:
        print(f"ERROR: rules configuration: {exc}", file=sys.stderr)
        return 2

    config = FilteringConfig(
        strict_validation=args.strict,
        validate_output=not args.no_output_validation,
        skip_source_validation=args.skip_input_validation,
        ruleset=ruleset,
    )
    run_id = uuid.uuid4().hex[:12]
    started = now_utc()
    print(f"meda_filtering {FILTERING_PACKAGE_VERSION} ruleset {RULESET_VERSION} run {run_id}", file=sys.stderr)

    stats = FilteringStats()
    failure: str | None = None
    try:
        filter_normalized_jsonl(src, dst, config=config, stats=stats)
    except FilteringError as exc:
        failure = str(exc)
    except Exception as exc:  # noqa: BLE001 - the run must still leave a report
        failure = f"{type(exc).__name__}: {exc}"

    ended = now_utc()
    code = 2 if failure else (1 if stats.records_validation_errors else 0)

    try:
        report = build_report(stats, config, input_path=src, output_path=dst, start_time=started,
                              end_time=ended, run_id=run_id, failure=failure)
        write_report_atomic(rep, report)
        print(f"report: {rep}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: report could not be written: {exc}", file=sys.stderr)
        code = 2

    if failure:
        print(f"FATAL: {failure}", file=sys.stderr)
    else:
        print(f"output: {dst}", file=sys.stderr)
    print(
        f"records read={stats.records_read} written={stats.records_written} "
        f"not_written={stats.records_skipped_no_accepted} invalid_lines={stats.records_validation_errors}; "
        f"measurements evaluated={stats.measurements_evaluated} accepted={stats.measurements_accepted} "
        f"rejected={stats.measurements_rejected} review_required={stats.measurements_review_required}",
        file=sys.stderr,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
