
from __future__ import annotations

"""
MarsWalk Intelligence - Data Bootstrap

Downloads raw and processed datasets from Kaggle and places them
in the project directories defined by data/manifest.yaml.

Features:
- Validates manifest structure.
- Downloads raw files and verifies copied file sizes.
- Completes missing files in partially downloaded bulk datasets.
- Does not skip a bulk dataset merely because its directory exists.
- Preserves existing files and does not delete user data.
- Verifies copied files against the Kaggle cache.
- Reports failures and exits with a non-zero status.

Usage:
    python scripts/bootstrap_data.py
    python scripts/bootstrap_data.py --only ctx_mosaic
    python scripts/bootstrap_data.py --skip ctx_dem
    python scripts/bootstrap_data.py --raw-only
"""

import argparse
import shutil
import sys
import zipfile

from pathlib import Path, PurePosixPath

import yaml

try:
    import kagglehub
except ImportError:
    print(
        "ERROR: Missing dependency.\n"
        "Install it using:\n"
        "python -m pip install kagglehub pyyaml"
    )
    sys.exit(1)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifest.yaml"


class BootstrapError(Exception):
    """Raised when a data bootstrap operation fails."""


def project_path(relative_path: str) -> Path:
    """
    Resolve a manifest path and prevent paths escaping the project root.
    """

    path = (PROJECT_ROOT / relative_path).resolve()

    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise BootstrapError(
            f"Path escapes project directory: {relative_path}"
        ) from exc

    return path


def load_manifest() -> dict:
    """Load and validate the YAML manifest."""

    if not MANIFEST_PATH.is_file():
        raise BootstrapError(
            f"Manifest not found: {MANIFEST_PATH}"
        )

    try:
        with MANIFEST_PATH.open("r", encoding="utf-8") as file:
            manifest = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise BootstrapError(
            f"Invalid YAML manifest: {exc}"
        ) from exc

    if not isinstance(manifest, dict):
        raise BootstrapError(
            "Manifest must contain a YAML mapping."
        )

    dataset_slug = manifest.get("kaggle_dataset")

    if not isinstance(dataset_slug, str) or not dataset_slug.strip():
        raise BootstrapError(
            "Manifest is missing a valid 'kaggle_dataset'."
        )

    files = manifest.get("files", {})
    bulk = manifest.get("bulk", {})

    if not isinstance(files, dict):
        raise BootstrapError(
            "Manifest 'files' must be a mapping."
        )

    if not isinstance(bulk, dict):
        raise BootstrapError(
            "Manifest 'bulk' must be a mapping."
        )

    for key, entry in files.items():
        if not isinstance(entry, dict):
            raise BootstrapError(
                f"Invalid raw entry: {key}"
            )

        if not entry.get("source") or not entry.get("target"):
            raise BootstrapError(
                f"Raw entry '{key}' requires source and target."
            )

    for key, entry in bulk.items():
        if not isinstance(entry, dict):
            raise BootstrapError(
                f"Invalid bulk entry: {key}"
            )

        if not entry.get("kaggle_dataset") or not entry.get("target"):
            raise BootstrapError(
                f"Bulk entry '{key}' requires kaggle_dataset and target."
            )

    return manifest


def download_dataset(
    dataset_slug: str,
    source: str | None = None,
) -> Path:
    """Download a Kaggle dataset or a specific file."""

    try:
        if source:
            downloaded = kagglehub.dataset_download(
                dataset_slug,
                path=source,
            )
        else:
            downloaded = kagglehub.dataset_download(
                dataset_slug
            )
    except Exception as exc:
        raise BootstrapError(
            f"Kaggle download failed for '{dataset_slug}': {exc}"
        ) from exc

    downloaded_path = Path(downloaded)

    if not downloaded_path.exists():
        raise BootstrapError(
            f"Kaggle returned a path that does not exist: "
            f"{downloaded_path}"
        )

    return downloaded_path


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    """
    Extract a ZIP archive while preventing path traversal.
    """

    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for member in archive.infolist():
                member_path = PurePosixPath(member.filename)

                if member_path.is_absolute() or ".." in member_path.parts:
                    raise BootstrapError(
                        f"Unsafe ZIP member path: {member.filename}"
                    )

                target = (destination / Path(*member_path.parts)).resolve()

                try:
                    target.relative_to(destination_resolved)
                except ValueError as exc:
                    raise BootstrapError(
                        f"ZIP member escapes extraction directory: "
                        f"{member.filename}"
                    ) from exc

            archive.extractall(destination)

    except zipfile.BadZipFile as exc:
        raise BootstrapError(
            f"Invalid ZIP archive: {zip_path}"
        ) from exc


def verify_file(source: Path, target: Path) -> None:
    """Verify that a copied file exists and has the expected size."""

    if not target.is_file():
        raise BootstrapError(
            f"Expected file was not created: {target}"
        )

    if source.stat().st_size != target.stat().st_size:
        raise BootstrapError(
            "File size mismatch:\n"
            f"Source: {source} ({source.stat().st_size} bytes)\n"
            f"Target: {target} ({target.stat().st_size} bytes)"
        )


def copy_file_verified(source: Path, target: Path) -> bool:
    """
    Copy a file if missing or different in size.

    Returns True if a copy was performed.
    """

    if not source.is_file():
        raise BootstrapError(
            f"Downloaded source is not a file: {source}"
        )

    if target.exists() and not target.is_file():
        raise BootstrapError(
            f"Target exists but is not a file: {target}"
        )

    if target.is_file() and target.stat().st_size == source.stat().st_size:
        print(f"    verified, already present: {target.name}")
        return False

    target.parent.mkdir(parents=True, exist_ok=True)

    print(f"    copying: {source.name}")

    shutil.copy2(source, target)
    verify_file(source, target)

    return True


def resolve_downloaded_file(
    downloaded_path: Path,
    source_name: str,
) -> Path:
    """
    Resolve the requested file from the Kaggle download result.

    Kaggle may return a file directly or a containing directory.
    """

    if downloaded_path.is_file():
        return downloaded_path

    if downloaded_path.is_dir():
        direct = downloaded_path / source_name

        if direct.is_file():
            return direct

        matches = list(downloaded_path.rglob(Path(source_name).name))

        if len(matches) == 1 and matches[0].is_file():
            return matches[0]

        if len(matches) > 1:
            raise BootstrapError(
                f"Multiple files named '{source_name}' found in "
                f"{downloaded_path}"
            )

    raise BootstrapError(
        f"Could not locate downloaded file '{source_name}' "
        f"inside {downloaded_path}"
    )


def fetch_file(
    dataset_slug: str,
    key: str,
    entry: dict,
) -> None:
    """Download and verify one raw dataset entry."""

    source_name = entry["source"]
    target = project_path(entry["target"])

    print(f"\n  - {key}: {source_name}")

    downloaded_path = download_dataset(
        dataset_slug,
        source=source_name,
    )

    if downloaded_path.is_dir():
        resolved = resolve_downloaded_file(
            downloaded_path,
            source_name,
        )
    else:
        resolved = downloaded_path

    if resolved.suffix.lower() == ".zip":
        print("    ZIP archive detected.")

        if target.exists() and not target.is_dir():
            raise BootstrapError(
                f"ZIP target must be a directory: {target}"
            )

        safe_extract_zip(resolved, target)

        print(f"    extracted -> {target}")
        return

    if resolved.is_dir():
        if target.exists():
            raise BootstrapError(
                f"Directory target already exists: {target}"
            )

        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.copytree(resolved, target)

        print(f"    directory copied -> {target}")
        return

    copied = copy_file_verified(resolved, target)

    if copied:
        print(f"    verified -> {target}")


def get_dataset_files(root: Path) -> list[Path]:
    """Return all regular files recursively in a dataset directory."""

    if not root.is_dir():
        raise BootstrapError(
            f"Dataset directory does not exist: {root}"
        )

    files = sorted(
        path for path in root.rglob("*")
        if path.is_file()
    )

    if not files:
        raise BootstrapError(
            f"Downloaded dataset contains no files: {root}"
        )

    return files


def verify_bulk_dataset(
    source_root: Path,
    target_root: Path,
) -> tuple[int, int]:
    """
    Verify every source file exists in the target with matching size.

    Returns:
        (verified_file_count, missing_or_incorrect_count)
    """

    source_files = get_dataset_files(source_root)

    problems = []

    for source_file in source_files:
        relative = source_file.relative_to(source_root)
        target_file = target_root / relative

        if not target_file.is_file():
            problems.append(str(relative))
            continue

        if source_file.stat().st_size != target_file.stat().st_size:
            problems.append(str(relative))

    verified_count = len(source_files) - len(problems)

    return verified_count, len(problems)


def fetch_bulk(key: str, entry: dict) -> None:
    """
    Download and synchronize a complete Kaggle dataset.

    Existing target files are preserved unless their corresponding
    source file is missing or has a different size.
    """

    dataset_slug = entry["kaggle_dataset"]
    target = project_path(entry["target"])

    print(f"\n  - {key}: downloading bulk dataset")
    print(f"    dataset: {dataset_slug}")
    print(f"    target: {target}")

    cache_path = download_dataset(dataset_slug)

    if not cache_path.is_dir():
        raise BootstrapError(
            f"Expected a dataset directory, received: {cache_path}"
        )

    source_files = get_dataset_files(cache_path)

    print(
        f"    Kaggle dataset contains "
        f"{len(source_files)} files."
    )

    target.mkdir(parents=True, exist_ok=True)

    copied_count = 0
    unchanged_count = 0

    for source_file in source_files:
        relative = source_file.relative_to(cache_path)
        target_file = target / relative

        if target_file.exists() and not target_file.is_file():
            raise BootstrapError(
                f"Expected a file but found a directory: {target_file}"
            )

        if (
            target_file.is_file()
            and target_file.stat().st_size == source_file.stat().st_size
        ):
            unchanged_count += 1
            continue

        copy_file_verified(source_file, target_file)
        copied_count += 1

    verified_count, problem_count = verify_bulk_dataset(
        cache_path,
        target,
    )

    print("\n    Bulk verification:")
    print(f"      Source files:   {len(source_files)}")
    print(f"      Verified files: {verified_count}")
    print(f"      Copied files:   {copied_count}")
    print(f"      Unchanged:      {unchanged_count}")
    print(f"      Problems:       {problem_count}")

    if problem_count:
        raise BootstrapError(
            f"Bulk dataset verification failed for '{key}'."
        )

    print(f"    verified -> {target}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download and verify MarsWalk project datasets."
    )

    parser.add_argument(
        "--only",
        help="Comma-separated raw manifest keys to download.",
    )

    parser.add_argument(
        "--skip",
        help="Comma-separated raw manifest keys to skip.",
    )

    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="Skip bulk/processed dataset downloads.",
    )

    args = parser.parse_args()

    try:
        manifest = load_manifest()
    except BootstrapError as exc:
        print(f"\nFATAL: {exc}")
        return 1

    dataset_slug = manifest["kaggle_dataset"]
    files = manifest.get("files", {})
    bulk = manifest.get("bulk", {})

    only = (
        {item.strip() for item in args.only.split(",") if item.strip()}
        if args.only
        else None
    )

    skip = (
        {item.strip() for item in args.skip.split(",") if item.strip()}
        if args.skip
        else set()
    )

    if only is not None:
        unknown = only - set(files)

        if unknown:
            print(
                "ERROR: Unknown raw manifest keys: "
                + ", ".join(sorted(unknown))
            )
            return 1

    unknown_skip = skip - set(files)

    if unknown_skip:
        print(
            "ERROR: Unknown skip keys: "
            + ", ".join(sorted(unknown_skip))
        )
        return 1

    failures = []

    print("=" * 65)
    print("MarsWalk Intelligence - Data Bootstrap")
    print("=" * 65)

    print(f"\nRaw dataset: {dataset_slug}")

    for key, entry in files.items():
        if only is not None and key not in only:
            continue

        if key in skip:
            print(f"\n  - {key}: skipped by user")
            continue

        try:
            fetch_file(dataset_slug, key, entry)
        except Exception as exc:
            failures.append((key, str(exc)))
            print(f"\n    ERROR: {exc}")

    if not args.raw_only:
        print("\n" + "=" * 65)
        print("Processed (bulk) datasets")
        print("=" * 65)

        for key, entry in bulk.items():
            try:
                fetch_bulk(key, entry)
            except Exception as exc:
                failures.append((key, str(exc)))
                print(f"\n    ERROR: {exc}")

    else:
        print("\n--raw-only set: skipping bulk datasets.")

    print("\n" + "=" * 65)

    if failures:
        print("BOOTSTRAP FINISHED WITH ERRORS")
        print(f"Failed entries: {len(failures)}")

        for key, error in failures:
            print(f"\n  - {key}")
            print(f"    {error}")

        print("\nSome data may still have been downloaded.")
        print("Fix the reported errors and run the script again.")

        return 1

    print("BOOTSTRAP COMPLETED SUCCESSFULLY")
    print("All requested files passed the available verification checks.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
