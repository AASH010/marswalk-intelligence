
"""CLI: python -m meda_weather.cli <extracted_dataset_root> <output.jsonl>."""
import argparse
from pathlib import Path
import sqlite3
from .parser import iter_archive_rows
from .streaming import normalize_stream_to_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse and normalize MEDA weather CSV products into JSONL."
    )

    parser.add_argument(
        "dataset_root",
        type=Path,
        help="Path to the extracted MEDA dataset directory.",
    )

    parser.add_argument(
        "output_jsonl",
        type=Path,
        help="Path where the normalized JSONL file will be written.",
    )

    args = parser.parse_args()

    if not args.dataset_root.is_dir():
        parser.error(
            f"Dataset root is not a directory: {args.dataset_root}"
        )

    try:
        parsed, normalized = normalize_stream_to_jsonl(
            iter_archive_rows(args.dataset_root),
            args.output_jsonl,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(
            status=1,
            message=f"MEDA normalization failed: {exc}\n",
        )

    print(f"Parsed product rows: {parsed:,}")
    print(f"Canonical records (exact Sol + SCLK): {normalized:,}")
    print(f"Output: {args.output_jsonl}")


if __name__ == "__main__":
    main()
