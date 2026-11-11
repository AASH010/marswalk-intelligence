"""CLI: python -m meda_weather.cli <extracted_dataset_root> <output.jsonl>"""
import argparse
from .parser import iter_archive_rows
from .streaming import normalize_stream_to_jsonl

def main():
    p = argparse.ArgumentParser()
    p.add_argument("dataset_root")
    p.add_argument("output_jsonl")
    args = p.parse_args()
    parsed, normalized = normalize_stream_to_jsonl(
        iter_archive_rows(args.dataset_root), args.output_jsonl
    )
    print(f"Parsed product rows: {parsed:,}")
    print(f"Canonical records (exact Sol + SCLK): {normalized:,}")
    print(f"Output: {args.output_jsonl}")

if __name__ == "__main__":
    main()
