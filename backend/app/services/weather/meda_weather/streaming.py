"""Disk-backed normalization for the full multi-product MEDA collection."""
from __future__ import annotations
import json
import sqlite3
import tempfile
from pathlib import Path
from itertools import groupby
from .normalize import normalize_rows

def normalize_stream_to_jsonl(rows, output_path: str | Path, batch_size: int = 5000) -> tuple[int, int]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, db_name = tempfile.mkstemp(prefix="meda_stage_", suffix=".sqlite", dir=str(output.parent))
    import os
    os.close(fd)
    parsed_count = 0
    record_count = 0
    try:
        conn = sqlite3.connect(db_name)
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("CREATE TABLE source_rows (sol INTEGER NOT NULL, sclk INTEGER NOT NULL, payload TEXT NOT NULL)")
        batch = []
        for row in rows:
            batch.append((row["sol"], row["sclk_s"], json.dumps(row, ensure_ascii=False, allow_nan=False)))
            parsed_count += 1
            if len(batch) >= batch_size:
                conn.executemany("INSERT INTO source_rows VALUES (?, ?, ?)", batch)
                conn.commit()
                batch.clear()
        if batch:
            conn.executemany("INSERT INTO source_rows VALUES (?, ?, ?)", batch)
            conn.commit()
        conn.execute("CREATE INDEX idx_source_key ON source_rows(sol, sclk)")
        cursor = conn.execute("SELECT sol, sclk, payload FROM source_rows ORDER BY sol, sclk")
        with output.open("w", encoding="utf-8") as out:
            for (sol, sclk), group in groupby(cursor, key=lambda r: (r[0], r[1])):
                source_rows = [json.loads(item[2]) for item in group]
                for record in normalize_rows(source_rows):
                    out.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                    record_count += 1
        conn.close()
    finally:
        try:
            os.remove(db_name)
        except FileNotFoundError:
            pass
    return parsed_count, record_count
