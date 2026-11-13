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
from .parser import canonical_sclk, sclk_sort_parts

__all__ = [
    "StreamingError",
    "ContractError",
    "OutputValidationError",
    "StreamSummary",
    "stream_normalize",
    "normalize_stream_to_jsonl",
    "validate_jsonl",
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

_STATIC_SOURCE_KEYS = ("source_level", "product", "source_file", "release")


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
                    json.dumps({k: source.get(k) for k in _STATIC_SOURCE_KEYS}),
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


def validate_jsonl(path: str | Path, expected_records: int | None = None) -> int:
    """Check a normalised JSONL file; return the number of records.

    Verifies: every line is valid JSON (no NaN/Infinity), required keys exist,
    ``sclk_s`` is a canonical decimal string, ``utc`` is null, every
    measurement carries provenance, records are strictly increasing by exact
    (sol, SCLK), and the count matches ``expected_records``.
    """
    previous: tuple | None = None
    count = 0

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
    if expected_records is not None and count != expected_records:
        raise OutputValidationError(f"expected {expected_records} records, file has {count}")
    return count


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


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
) -> StreamSummary:
    """Stage ``rows`` on disk, normalise exact-time groups, publish JSONL.

    ``rows`` is consumed lazily (a generator is fine).  ``workdir`` holds the
    staging database (default: next to the output).  With ``overwrite=False`` an
    existing output raises ``FileExistsError`` *before* any work is done.
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

    summary = StreamSummary(output_path=output)
    temp_output: Path | None = None
    stage: _Stage | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="meda_stage_", dir=str(work_parent)) as tmp:
            stage = _Stage(Path(tmp) / "stage.sqlite")

            # Phase 1 - lazy ingest in bounded, atomic batches.
            batch: list[tuple] = []
            for row in rows:
                batch.append(stage.to_tuple(row))
                source = row["source"]
                summary.rows_by_product[f"{source['product']}.{source['source_level']}"] += 1
                summary.parsed_rows += 1
                if len(batch) >= batch_size:
                    stage.insert_batch(batch)
                    batch.clear()
            if batch:
                stage.insert_batch(batch)
                batch.clear()

            # Phase 2 - exact-time ordering via index.
            stage.build_index()

            # Phase 3 - normalise one exact group at a time.
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
            )
            temp_output = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                for group in stage.iter_groups():
                    record = normalize_group(group, summary.normalization)
                    out.write(json.dumps(record, ensure_ascii=False, allow_nan=False))
                    out.write("\n")
                    summary.records += 1
                    summary.records_by_sol[record["sol"]] += 1
                out.flush()
                os.fsync(out.fileno())

            stage.close()
            stage = None

            # Phase 4 - validate, then publish atomically.
            if validate:
                validate_jsonl(temp_output, summary.records)
            summary.output_bytes = temp_output.stat().st_size
            os.replace(temp_output, output)
            temp_output = None
            _fsync_directory(output.parent)
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
