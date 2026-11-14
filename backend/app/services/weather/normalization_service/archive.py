"""Archive inspection and path safety.

``inspect_archive`` never raises for a damaged archive: it returns what could
be measured, so the report exists even when extraction later fails.  Nothing in
the result is pre-filled - every list is built from an actual scan.
"""
from __future__ import annotations

import re
import zipfile
import zlib
from collections import Counter
from pathlib import Path, PurePosixPath

__all__ = ["inspect_archive", "inspect_directory", "inspect_source", "check_paths", "PathSafetyError"]

_PRODUCT_RE = re.compile(r"(?<![A-Za-z0-9])(CAL|DER)_([A-Za-z0-9]+)", re.I)
_SOL_RE = re.compile(r"WE__(\d{4,5})", re.I)


class PathSafetyError(ValueError):
    """Two paths that must differ resolve to the same file."""


def _is_unsafe(name: str) -> str | None:
    if "\x00" in name:
        return "NUL byte in name"
    if "\\" in name:
        return "backslash in name"
    posix = PurePosixPath(name)
    if posix.is_absolute() or name.startswith("/"):
        return "absolute path"
    if re.match(r"^[A-Za-z]:", name):
        return "drive letter"
    if ".." in posix.parts:
        return "parent traversal"
    return None


def _pairing(names: list[str]) -> tuple[list[str], list[str]]:
    """(csv files without label, xml labels without csv), matched case-insensitively per directory."""
    csvs = {str(PurePosixPath(n).with_suffix("")).lower(): n for n in names if n.lower().endswith(".csv")}
    xmls = {str(PurePosixPath(n).with_suffix("")).lower(): n for n in names if n.lower().endswith(".xml")}
    return sorted(n for k, n in csvs.items() if k not in xmls), sorted(n for k, n in xmls.items() if k not in csvs)


def _expected_missing(names: list[str]) -> list[dict]:
    """Products present for some Sol but absent for another Sol of the same level.

    There is no official file manifest in the inputs, so "expected" is defined
    relative to the archive itself: the union of (product, level) over all Sols.
    """
    seen: dict[tuple[str, str], set[int]] = {}
    sols: set[int] = set()
    for n in names:
        if not n.lower().endswith(".csv"):
            continue
        base = PurePosixPath(n).name
        m, s = _PRODUCT_RE.search(base), _SOL_RE.search(base)
        if not (m and s):
            continue
        sols.add(int(s.group(1)))
        seen.setdefault((m.group(1).upper(), m.group(2).upper()), set()).add(int(s.group(1)))
    out = []
    for (level, prod), have in sorted(seen.items()):
        for sol in sorted(sols - have):
            out.append({"sol": sol, "level": level, "product": prod, "basis": "present for other Sols of this archive"})
    return out


def _empty(kind: str, path: Path) -> dict:
    return {
        "path": str(path), "name": path.name, "kind": kind,
        "total_entries": 0, "file_entries": 0, "csv_entries": 0, "xml_entries": 0,
        "expected_files_missing": [], "unreadable_entries": [], "corrupt_entries": [],
        "crc_validation": "NOT_EXECUTED", "extraction_errors": [], "duplicate_paths": [],
        "unsafe_paths": [], "encrypted_entries": [], "orphan_xml_labels": [],
        "csv_without_label": [], "discovery_errors": [], "bytes_uncompressed": 0, "ok": False,
    }


def inspect_archive(path: str | Path) -> dict:
    path = Path(path)
    info = _empty("zip", path)
    try:
        if not path.is_file() or not zipfile.is_zipfile(path):
            info["discovery_errors"].append("not a valid ZIP archive")
            info["crc_validation"] = "NOT_EXECUTED"
            return info
        with zipfile.ZipFile(path) as zf:
            members = zf.infolist()
            info["total_entries"] = len(members)
            files = [m for m in members if not m.is_dir()]
            names = [m.filename for m in files]
            info["file_entries"] = len(files)
            info["csv_entries"] = sum(n.lower().endswith(".csv") for n in names)
            info["xml_entries"] = sum(n.lower().endswith(".xml") for n in names)
            info["bytes_uncompressed"] = sum(m.file_size for m in files)
            for m in members:
                reason = _is_unsafe(m.filename)
                if reason:
                    info["unsafe_paths"].append({"entry": m.filename, "reason": reason})
            seen = Counter(n for n in names)
            seen_ci = Counter(n.lower() for n in names)
            info["duplicate_paths"] = sorted(
                {n for n, c in seen.items() if c > 1} | {n for n in names if seen_ci[n.lower()] > 1 and seen[n] == 1}
            )
            info["encrypted_entries"] = [m.filename for m in files if m.flag_bits & 0x1]
            bad_crc, unreadable = [], []
            for m in files:
                if m.flag_bits & 0x1:
                    continue
                try:
                    with zf.open(m) as stream:  # reading to the end verifies the CRC-32
                        while stream.read(1 << 20):
                            pass
                except (zipfile.BadZipFile, zlib.error) as exc:  # CRC mismatch / damaged deflate stream
                    bad_crc.append({"entry": m.filename, "error": str(exc)})
                except (OSError, EOFError, RuntimeError, NotImplementedError, ValueError) as exc:
                    unreadable.append({"entry": m.filename, "error": f"{type(exc).__name__}: {exc}"})
            info["corrupt_entries"], info["unreadable_entries"] = bad_crc, unreadable
            info["crc_validation"] = "FAILED" if (bad_crc or unreadable) else "PASSED"
            info["csv_without_label"], info["orphan_xml_labels"] = _pairing(names)
            info["expected_files_missing"] = _expected_missing(names)
    except (zipfile.BadZipFile, OSError) as exc:
        info["discovery_errors"].append(f"{type(exc).__name__}: {exc}")
        info["crc_validation"] = "NOT_EXECUTED"
        return info
    info["ok"] = not (info["corrupt_entries"] or info["unreadable_entries"] or info["duplicate_paths"]
                      or info["unsafe_paths"] or info["encrypted_entries"] or info["discovery_errors"])
    return info


def inspect_directory(path: str | Path) -> dict:
    path = Path(path)
    info = _empty("directory", path)
    names, unreadable = [], []
    for p in sorted(path.rglob("*")):
        if p.is_file():
            names.append(p.relative_to(path).as_posix())
            try:
                with p.open("rb") as fh:
                    fh.read(1)
            except OSError as exc:
                unreadable.append({"entry": names[-1], "error": str(exc)})
    info["total_entries"] = info["file_entries"] = len(names)
    info["csv_entries"] = sum(n.lower().endswith(".csv") for n in names)
    info["xml_entries"] = sum(n.lower().endswith(".xml") for n in names)
    info["unreadable_entries"] = unreadable
    info["crc_validation"] = "NOT_APPLICABLE_DIRECTORY"
    info["csv_without_label"], info["orphan_xml_labels"] = _pairing(names)
    info["expected_files_missing"] = _expected_missing(names)
    info["ok"] = not unreadable
    return info


def inspect_source(path: str | Path) -> dict:
    path = Path(path)
    return inspect_directory(path) if path.is_dir() else inspect_archive(path)


def check_paths(**named: Path | None) -> None:
    """Reject any two *distinct roles* that resolve to the same filesystem path.

    ``check_paths(output=o, report=r, calibrated=c, derived=d)``.  Symlinks and
    relative segments are resolved first.  Also rejects an output/report that
    lives INSIDE an input directory (it would be re-discovered/overwritten).
    """
    resolved: dict[str, Path] = {k: Path(v).resolve() for k, v in named.items() if v is not None}
    roles = list(resolved)
    for i, a in enumerate(roles):
        for b in roles[i + 1:]:
            if resolved[a] == resolved[b]:
                raise PathSafetyError(f"{a} and {b} resolve to the same path: {resolved[a]}")
    for out_role in ("output", "report", "summary_json"):
        if out_role not in resolved:
            continue
        for in_role in ("calibrated", "derived"):
            root = resolved.get(in_role)
            if root is not None and root.is_dir() and root in resolved[out_role].parents:
                raise PathSafetyError(f"{out_role} lies inside the {in_role} input directory: {resolved[out_role]}")
