
from collections import Counter
from pathlib import Path

from meda_weather.parser import identify_file, iter_archive_rows


dataset_root = Path(
    r"D:\projects\project MWI\marswalk-intelligence\resources\MEDA_Weather_ds"
)

csv_files = sorted(
    path
    for path in dataset_root.rglob("*")
    if path.is_file() and path.suffix.lower() == ".csv"
)

identified = [
    (path, identify_file(path))
    for path in csv_files
]

valid = [
    (path, identity)
    for path, identity in identified
    if identity is not None
]

skipped = [
    path
    for path, identity in identified
    if identity is None
]

print("\nUnrecognized CSV files:")

for path in skipped:
    print(path)

print(f"CSV files discovered: {len(csv_files):,}")
print(f"Recognized MEDA products: {len(valid):,}")
print(f"Unrecognized CSV files: {len(skipped):,}")

print("\nProducts by source level:")
counts = Counter(
    (identity[1], identity[2])
    for _, identity in valid
)

for key, count in sorted(counts.items()):
    print(f"{key[0]:<8} {key[1]:<12} {count:>5,}")

print("\nChecking parser row count...")
parsed_count = sum(
    1 for _ in iter_archive_rows(dataset_root)
)

print(f"Parsed product rows: {parsed_count:,}")

if len(csv_files) != len(valid) + len(skipped):
    raise RuntimeError("File discovery accounting mismatch.")

print("\nDiscovery test completed.")
