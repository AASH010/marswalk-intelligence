
"""Disk-backed normalization for the full multi-product MEDA collection."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from itertools import groupby
from pathlib import Path

from .normalize import normalize_rows


def normalize_stream_to_jsonl(
    rows,
    output_path: str | Path,
    batch_size: int = 5000,
) -> tuple[int, int]:
    """
    Stage source rows in SQLite, normalize exact Sol + SCLK groups,
    and publish the JSONL only after the complete operation succeeds.

    Returns:
        (parsed_source_rows, canonical_records)
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    parsed_count = 0
    record_count = 0

    # TemporaryDirectory is created under the output directory so os.replace
    # stays on the same filesystem.
    with tempfile.TemporaryDirectory(
        prefix="meda_stage_",
        dir=str(output.parent),
    ) as temporary_directory:

        temp_dir = Path(temporary_directory)
        db_path = temp_dir / "stage.sqlite"
        temporary_output = temp_dir / "normalized.jsonl"

        conn: sqlite3.Connection | None = None

        try:
            conn = sqlite3.connect(db_path)

            # This is a disposable staging DB, not the final data store.
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")

            conn.execute(
                """
                CREATE TABLE source_rows (
                    sol INTEGER NOT NULL,
                    sclk INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

            batch = []

            # Phase 1: ingest parser rows into SQLite in bounded batches.
            for row in rows:
                payload = json.dumps(
                    row,
                    ensure_ascii=False,
                    allow_nan=False,
                )

                batch.append(
                    (
                        row["sol"],
                        row["sclk_s"],
                        payload,
                    )
                )
                parsed_count += 1

                if len(batch) >= batch_size:
                    conn.executemany(
                        """
                        INSERT INTO source_rows (sol, sclk, payload)
                        VALUES (?, ?, ?)
                        """,
                        batch,
                    )
                    conn.commit()
                    batch.clear()

            if batch:
                conn.executemany(
                    """
                    INSERT INTO source_rows (sol, sclk, payload)
                    VALUES (?, ?, ?)
                    """,
                    batch,
                )
                conn.commit()

            # Phase 2: order by identity and insertion order.
            # rowid makes the order within an identical key deterministic.
            cursor = conn.execute(
                """
                SELECT sol, sclk, payload
                FROM source_rows
                ORDER BY sol, sclk, rowid
                """
            )

            # Phase 3: normalize one exact (Sol, SCLK) group at a time.
            with temporary_output.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as out:

                for (sol, sclk), group in groupby(
                    cursor,
                    key=lambda item: (item[0], item[1]),
                ):
                    source_rows = [
                        json.loads(item[2])
                        for item in group
                    ]

                    normalized_records = normalize_rows(source_rows)

                    for record in normalized_records:
                        out.write(
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                allow_nan=False,
                            )
                            + "\n"
                        )
                        record_count += 1

                out.flush()
                os.fsync(out.fileno())

            conn.close()
            conn = None

            # Publish only after parsing, normalization, and writing succeed.
            # If any earlier step fails, the previous output remains untouched.
            os.replace(temporary_output, output)

        finally:
            if conn is not None:
                conn.close()

    return parsed_count, record_count
