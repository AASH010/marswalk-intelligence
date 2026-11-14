"""Disk-backed, memory-bounded normalisation of parsed MEDA rows into JSONL.

Pipeline position:  parser.py  ->  **streaming.py**  ->  normalize.py

What this module does
  1. Consumes parsed source rows (contract in ``parser.py``) lazily.
  2. Stages them in a *disposable* SQLite database whose key is the exact
     SCLK split into ``sclk_int INTEGER`` + ``sclk_frac TEXT`` - never a
     float, never an integer-truncated value.
  3. Reads them back ordered by ``(sol, sclk_int, sclk_frac, arrival id)``
     and hands each exact-time group to ``normalize.normalize_group``.
  4. Writes JSONL to a temporary file beside the final output, validates it,
     then publishes it with an atomic ``os.replace``.

Time alignment policy: **exact** (Sol, canonical SCLK) equality only.  There
is no tolerance, no nearest-neighbour/as-of join and no row-index alignment;
asynchronous measurements simply produce separate records.

Failure behaviour: any exception (including ``KeyboardInterrupt``) removes
the staging directory and the temporary output; a pre-existing final output
is left byte-for-byte untouched.  Re-running after a failure is always safe.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .normalize import NormalizationStats, normalize_group
from .registry import ChannelDecision, SentinelRegistry
from .parser import canonical_sclk, sclk_sort_parts

__all__ = [
    "StreamingError",
    "ContractError",
    "OutputValidationError",
    "StreamSummary",
    "stream_normalize",
    "normalize_stream_to_jsonl",
    "validate_jsonl",
    "publish_staged",
    "discard_staged",
]


class StreamingError(RuntimeError):
    """Base class for streaming-stage failures."""


class ContractError(StreamingError, ValueError):
    """A parsed row does not follow the parser->streaming contract."""


class OutputValidationError(StreamingError):
    """The JSONL that was just written failed validation (not published)."""


@dataclass
class StreamSummary:
    parsed_rows: int = 0
    records: int = 0
    rows_by_product: Counter = field(default_factory=Counter)
    records_by_sol: Counter = field(default_factory=Counter)
    normalization: NormalizationStats = field(default_factory=NormalizationStats)
    output_path: Path | None = None
    output_bytes: int = 0
    elapsed_seconds: float = 0.0
    # --- gate support (all measured during the run, none pre-filled) ---
    published: bool = False
    staged_path: Path | None = None
    rows_by_file: Counter = field(default_factory=Counter)
    sclk: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    jsonl: dict = field(default_factory=dict)
    measurements_in_records: int = 0

    def sol_coverage(self) -> dict[int, int]:
        return dict(sorted(self.records_by_sol.items()))

    def as_dict(self) -> dict:
        return {
            "parsed_rows": self.parsed_rows,
            "records": self.records,
            "rows_by_product": dict(sorted(self.rows_by_product.items())),
            "sol_coverage": self.sol_coverage(),
            "output_path": None if self.output_path is None else str(self.output_path),
            "output_bytes": self.output_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "published": self.published,
            "sclk": self.sclk,
            "provenance": self.provenance,
            "jsonl": self.jsonl,
            "measurements_in_records": self.measurements_in_records,
            "normalization": self.normalization.as_dict(),
        }


# --------------------------------------------------------------------------
# SQLite staging
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE sources (
    file_id     INTEGER PRIMARY KEY,
    source_json TEXT NOT NULL,      -- static provenance of the file
    columns     TEXT NOT NULL       -- JSON list of raw column names
);
CREATE TABLE source_rows (
    id        INTEGER PRIMARY KEY,  -- arrival order (deterministic)
    file_id   INTEGER NOT NULL REFERENCES sources(file_id),
    line      INTEGER,
    sol       INTEGER NOT NULL,
    sclk_int  INTEGER NOT NULL,     -- exact integer part of SCLK
    sclk_frac TEXT    NOT NULL,     -- fraction digits, no trailing zeros
    sclk      TEXT    NOT NULL,     -- canonical decimal string
    orig_sclk TEXT,
    lmst      TEXT,
    ltst      TEXT,
    vals      TEXT    NOT NULL      -- JSON list of typed values, header order
);
"""

_INSERT = (
    "INSERT INTO source_rows (file_id, line, sol, sclk_int, sclk_frac, sclk, "
    "orig_sclk, lmst, ltst, vals) VALUES (?,?,?,?,?,?,?,?,?,?)"
)

_STATIC_SOURCE_KEYS = ("source_level", "product", "source_file", "release", "source_archive")


class _Stage:
    """Disposable SQLite staging area keyed by exact SCLK."""

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        # MEMORY journal keeps ROLLBACK working; the DB is disposable so no
        # fsync is needed.  cache_size is negative = KiB (64 MiB cap).
        self.conn.execute("PRAGMA journal_mode=MEMORY")
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.execute("PRAGMA cache_size=-65536")
        self.conn.executescript(_SCHEMA)
        self._files: dict[tuple, int] = {}

    def close(self) -> None:
        self.conn.close()

    # -- ingest ----------------------------------------------------------
    def _file_id(self, row: Mapping[str, Any]) -> int:
        source = row["source"]
        columns = tuple(row["raw"].keys())
        key = tuple(source.get(k) for k in _STATIC_SOURCE_KEYS) + (columns,)
        file_id = self._files.get(key)
        if file_id is None:
            cursor = self.conn.execute(
                "INSERT INTO sources (source_json, columns) VALUES (?, ?)",
                (
                    json.dumps({k: source.get(k) for k in _STATIC_SOURCE_KEYS
                                if k != "source_archive" or source.get(k) is not None}),
                    json.dumps(columns),
                ),
            )
            file_id = cursor.lastrowid
            self._files[key] = file_id
        return file_id

    def to_tuple(self, row: Mapping[str, Any]) -> tuple:
        try:
            sol = row["sol"]
            raw_sclk = row["sclk"] if "sclk" in row else row["sclk_s"]
            source = row["source"]
            raw = row["raw"]
            for key in _STATIC_SOURCE_KEYS[:3]:
                if key not in source:
                    raise KeyError(f"source.{key}")
        except KeyError as exc:
            raise ContractError(f"parsed row lacks required key {exc}") from exc
        if isinstance(sol, bool) or not isinstance(sol, int):
            raise ContractError(f"sol must be int, got {sol!r}")
        if not isinstance(raw, Mapping):
            raise ContractError("row['raw'] must be a mapping")
        try:
            sclk = canonical_sclk(raw_sclk)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"bad SCLK {raw_sclk!r}: {exc}") from exc
        sclk_int, sclk_frac = sclk_sort_parts(sclk)
        return (
            self._file_id(row),
            source.get("source_row"),
            sol,
            sclk_int,
            sclk_frac,
            sclk,
            source.get("original_sclk"),
            row.get("lmst"),
            row.get("ltst"),
            json.dumps(list(raw.values()), allow_nan=False, ensure_ascii=False),
        )

    def insert_batch(self, batch: list[tuple]) -> None:
        """Insert one batch atomically: all rows or (on error) none."""
        with self.conn:  # COMMIT on success, ROLLBACK on exception
            self.conn.executemany(_INSERT, batch)

    def build_index(self) -> None:
        self.conn.execute(
            "CREATE INDEX idx_exact_time ON source_rows (sol, sclk_int, sclk_frac, id)"
        )
        self.conn.commit()

    def sclk_round_trip(self, expected_rows: int) -> dict:
        """Measure (not assume) that every staged SCLK survives SQLite exactly."""
        total = self.conn.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0]
        mismatches = self.conn.execute(
            "SELECT COUNT(*) FROM source_rows WHERE sclk != CASE WHEN sclk_frac = '' "
            "THEN CAST(sclk_int AS TEXT) ELSE CAST(sclk_int AS TEXT) || '.' || sclk_frac END"
        ).fetchone()[0]
        typed = self.conn.execute(
            "SELECT COUNT(*) FROM source_rows WHERE typeof(sclk_int) != 'integer' "
            "OR typeof(sclk_frac) != 'text' OR typeof(sclk) != 'text'"
        ).fetchone()[0]
        return {
            "rows_checked": total,
            "rows_expected": expected_rows,
            "reconstruction_mismatches": mismatches,
            "wrong_storage_type_rows": typed,
            "result": "PASSED" if (total == expected_rows and mismatches == 0 and typed == 0) else "FAILED",
        }

    # -- read back -------------------------------------------------------
    def iter_groups(self) -> Iterator[list[dict]]:
        """Yield one list of reconstructed rows per exact (sol, SCLK) group."""
        files: dict[int, tuple[dict, list[str]]] = {
            fid: (json.loads(src), json.loads(cols))
            for fid, src, cols in self.conn.execute("SELECT file_id, source_json, columns FROM sources")
        }
        cursor = self.conn.execute(
            "SELECT file_id, line, sol, sclk_int, sclk_frac, sclk, orig_sclk, lmst, ltst, vals "
            "FROM source_rows ORDER BY sol, sclk_int, sclk_frac, id"
        )
        current_key: tuple | None = None
        group: list[dict] = []
        for file_id, line, sol, s_int, s_frac, sclk, orig, lmst, ltst, vals in cursor:
            key = (sol, s_int, s_frac)
            if key != current_key and group:
                yield group
                group = []
            current_key = key
            static, columns = files[file_id]
            source = dict(static)
            source["source_row"] = line
            source["original_sclk"] = orig
            group.append(
                {
                    "sol": sol,
                    "sclk": sclk,
                    "lmst": lmst,
                    "ltst": ltst,
                    "source": source,
                    "raw": dict(zip(columns, json.loads(vals))),
                }
            )
        if group:
            yield group


# --------------------------------------------------------------------------
# Output validation
# --------------------------------------------------------------------------

_CANON_RE = re.compile(r"^(0|[1-9]\d*)(\.\d*[1-9])?$")
_REQUIRED_KEYS = ("sol", "sclk_s", "lmst", "ltst", "utc", "measurements", "quality", "sources")


def validate_jsonl(path: str | Path, expected_records: int | None = None, *, details: bool = False):
    """Check a normalised JSONL file; return the number of records.

    Verifies: every line is valid JSON (no NaN/Infinity), required keys exist,
    ``sclk_s`` is a canonical decimal string, ``utc`` is null, every
    measurement carries provenance, records are strictly increasing by exact
    (sol, SCLK), and the count matches ``expected_records``.
    """
    previous: tuple | None = None
    count = 0
    digest = hashlib.sha256()
    n_measurements = 0

    def _reject_constant(token: str) -> None:
        raise ValueError(f"non-finite constant {token}")

    with Path(path).open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            try:
                record = json.loads(line, parse_constant=_reject_constant)
            except ValueError as exc:
                raise OutputValidationError(f"line {number}: invalid JSON ({exc})") from exc
            for key in _REQUIRED_KEYS:
                if key not in record:
                    raise OutputValidationError(f"line {number}: missing key {key!r}")
            sclk = record["sclk_s"]
            if not isinstance(sclk, str) or not _CANON_RE.match(sclk):
                raise OutputValidationError(f"line {number}: sclk_s not canonical: {sclk!r}")
            if record["utc"] is not None:
                raise OutputValidationError(f"line {number}: utc must be null (no SPICE correlation)")
            for name, meas in record["measurements"].items():
                for key in ("source_product", "source_level", "source_file", "source_channel", "validity"):
                    if key not in meas:
                        raise OutputValidationError(f"line {number}: measurement {name!r} lacks {key!r}")
            if not record["sources"]:
                raise OutputValidationError(f"line {number}: empty sources")
            key = (record["sol"],) + sclk_sort_parts(sclk)
            if previous is not None and key <= previous:
                raise OutputValidationError(f"line {number}: records not strictly increasing")
            previous = key
            count += 1
            n_measurements += len(record["measurements"])
            digest.update(f"{record['sol']}:{sclk}\n".encode("ascii"))
    if expected_records is not None and count != expected_records:
        raise OutputValidationError(f"expected {expected_records} records, file has {count}")
    if details:
        return {"records": count, "measurements": n_measurements, "identity_sha256": digest.hexdigest()}
    return count


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


_PROV_KEYS = ("source_product", "source_level", "source_file", "source_channel")


def _provenance_of(
    record: Mapping[str, Any], require_archive: bool, excluded: frozenset | None = None
) -> tuple[int, int, int, int, int, int]:
    """(measurements, complete, incomplete, missing_block, unresolved_refs, isolation_leaks)."""
    sources = {s.get("source_file"): s for s in record["sources"]}
    total = complete = incomplete = missing = unresolved = leaks = 0
    for meas in record["measurements"].values():
        total += 1
        if excluded and (meas.get("source_file"), meas.get("source_channel")) in excluded:
            leaks += 1
        present = [k for k in _PROV_KEYS if meas.get(k) not in (None, "")]
        if not present and meas.get("source_row") is None:
            missing += 1
            continue
        ok = len(present) == len(_PROV_KEYS) and meas.get("source_row") is not None
        ok = ok and meas.get("source_level") in ("calibrated", "derived")
        ok = ok and bool(record.get("sclk_s"))
        src = sources.get(meas.get("source_file"))
        if src is None:
            unresolved += 1
            ok = False
        elif require_archive and not src.get("source_archive"):
            unresolved += 1
            ok = False
        if ok:
            complete += 1
        else:
            incomplete += 1
    return total, complete, incomplete, missing, unresolved, leaks


def publish_staged(staged: str | Path, output: str | Path) -> None:
    """Atomically replace ``output`` by ``staged`` (same filesystem, ``os.replace``)."""
    staged, output = Path(staged), Path(output)
    os.replace(staged, output)
    _fsync_directory(output.parent)


def discard_staged(staged: str | Path | None) -> None:
    if staged is not None:
        try:
            Path(staged).unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # e.g. Windows
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def stream_normalize(
    rows: Iterable[Mapping[str, Any]],
    output_path: str | Path,
    batch_size: int = 5000,
    *,
    workdir: str | Path | None = None,
    overwrite: bool = True,
    validate: bool = True,
    publish: bool = True,
    sentinel_registry: SentinelRegistry | None = None,
    channel_policy: Mapping[tuple[str, str], ChannelDecision] | None = None,
    require_archive_provenance: bool = False,
    summary: StreamSummary | None = None,
) -> StreamSummary:
    """Stage ``rows`` on disk, normalise exact-time groups, publish JSONL.

    ``rows`` is consumed lazily (a generator is fine).  ``workdir`` holds the
    staging database (default: next to the output).  With ``overwrite=False`` an
    existing output raises ``FileExistsError`` *before* any work is done.

    ``publish=False`` is the *gate* mode used by the CLI: the validated JSONL is
    left as a staging file beside the output (``summary.staged_path``, same
    filesystem) and the caller decides - after acceptance - to call
    ``publish_staged`` or ``discard_staged``.  Nothing touches ``output`` here.
    Pass your own ``summary`` to keep partial counters even if this raises.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    started = time.monotonic()
    output = Path(output_path)
    if output.is_dir():
        raise IsADirectoryError(f"Output path is a directory: {output}")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    work_parent = Path(workdir) if workdir is not None else output.parent
    work_parent.mkdir(parents=True, exist_ok=True)

    if summary is None:
        summary = StreamSummary()
    summary.output_path = output
    temp_output: Path | None = None
    stage: _Stage | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="meda_stage_", dir=str(work_parent)) as tmp:
            stage = _Stage(Path(tmp) / "stage.sqlite")

            # Phase 1 - lazy ingest in bounded, atomic batches.
            batch: list[tuple] = []
            sclk_total = sclk_frac_n = sclk_orig_mismatch = 0
            precision: Counter = Counter()
            for row in rows:
                tup = stage.to_tuple(row)
                batch.append(tup)
                source = row["source"]
                summary.rows_by_product[f"{source['product']}.{source['source_level']}"] += 1
                summary.rows_by_file[source["source_file"]] += 1
                summary.parsed_rows += 1
                sclk_total += 1
                if tup[4]:
                    sclk_frac_n += 1
                precision[len(tup[4])] += 1
                original = source.get("original_sclk")
                if original is not None:
                    try:
                        if canonical_sclk(str(original).strip()) != tup[5]:
                            sclk_orig_mismatch += 1
                    except ValueError:
                        sclk_orig_mismatch += 1
                if len(batch) >= batch_size:
                    stage.insert_batch(batch)
                    batch.clear()
            if batch:
                stage.insert_batch(batch)
                batch.clear()

            # Phase 2 - exact-time ordering via index.
            stage.build_index()
            round_trip = stage.sclk_round_trip(summary.parsed_rows)
            summary.sclk = {
                "total_sclk_values": sclk_total,
                "fractional_sclk_count": sclk_frac_n,
                "integer_sclk_count": sclk_total - sclk_frac_n,
                "precision_distribution_fraction_digits": {str(k): v for k, v in sorted(precision.items())},
                "original_vs_canonical_mismatches": sclk_orig_mismatch,
                "sqlite_round_trip": round_trip,
            }

            # Phase 3 - normalise one exact group at a time.
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
            )
            temp_output = Path(temp_name)
            writer_hash = hashlib.sha256()
            p_total = p_complete = p_incomplete = p_missing = p_unres = p_leaks = 0
            excluded_keys = frozenset(k for k, d in (channel_policy or {}).items() if d.action == "exclude")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                for group in stage.iter_groups():
                    record = normalize_group(
                        group, summary.normalization,
                        sentinel_registry=sentinel_registry, channel_policy=channel_policy,
                    )
                    out.write(json.dumps(record, ensure_ascii=False, allow_nan=False))
                    out.write("\n")
                    summary.records += 1
                    summary.records_by_sol[record["sol"]] += 1
                    writer_hash.update(f"{record['sol']}:{record['sclk_s']}\n".encode("ascii"))
                    t, c, i, m, u, lk = _provenance_of(record, require_archive_provenance, excluded_keys)
                    p_leaks += lk
                    p_total += t
                    p_complete += c
                    p_incomplete += i
                    p_missing += m
                    p_unres += u
                out.flush()
                os.fsync(out.fileno())
            summary.measurements_in_records = p_total
            summary.provenance = {
                "total_measurements": p_total,
                "measurements_with_complete_provenance": p_complete,
                "incomplete_provenance_count": p_incomplete,
                "missing_provenance_count": p_missing,
                "unresolved_source_references": p_unres,
                "excluded_channel_leaks": p_leaks,
                "archive_provenance_required": require_archive_provenance,
            }

            stage.close()
            stage = None

            # Phase 4 - validate, then publish atomically.
            if validate:
                details = validate_jsonl(temp_output, summary.records, details=True)
                summary.jsonl = {
                    "structural_validation": "PASSED",
                    "records_validated": details["records"],
                    "measurements_validated": details["measurements"],
                    "identity_sha256_written": writer_hash.hexdigest(),
                    "identity_sha256_read_back": details["identity_sha256"],
                    "jsonl_round_trip": "PASSED" if details["identity_sha256"] == writer_hash.hexdigest()
                    and details["measurements"] == p_total else "FAILED",
                }
                if summary.jsonl["jsonl_round_trip"] != "PASSED":
                    raise OutputValidationError("JSONL read-back differs from what was written")
            else:
                summary.jsonl = {"structural_validation": "NOT_EXECUTED", "jsonl_round_trip": "NOT_EXECUTED"}
            summary.output_bytes = temp_output.stat().st_size
            if publish:
                publish_staged(temp_output, output)
                summary.published = True
            else:
                summary.staged_path = temp_output
            temp_output = None
    finally:
        if stage is not None:
            stage.close()
        if temp_output is not None:
            try:
                temp_output.unlink()
            except FileNotFoundError:
                pass

    summary.elapsed_seconds = time.monotonic() - started
    return summary


def normalize_stream_to_jsonl(
    rows: Iterable[Mapping[str, Any]],
    output_path: str | Path,
    batch_size: int = 5000,
) -> tuple[int, int]:
    """Backwards-compatible wrapper. Returns ``(parsed_rows, canonical_records)``."""
    summary = stream_normalize(rows, output_path, batch_size)
    return summary.parsed_rows, summary.records
