"""Bounded-memory SQLite staging, JSONL streaming and release publication."""
from __future__ import annotations
import json, os, sqlite3, tempfile, hashlib, shutil
from pathlib import Path

def json_safe(obj):
    return json.dumps(obj,ensure_ascii=False,allow_nan=False,separators=(",",":"),default=str)

def write_jsonl(path,items):
    with open(path,"w",encoding="utf-8",newline="\n") as f:
        for item in items: f.write(json_safe(item)+"\n")

def file_hash(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

class SQLiteStage:
    def __init__(self,directory=None,keep=False):
        fd,self.path=tempfile.mkstemp(prefix="meda_stage_",suffix=".sqlite",dir=directory); os.close(fd)
        self.keep=keep; self.db=sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL"); self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE INDEX idx_records_id ON records(id)")
    def insert_many(self,items): self.db.executemany("INSERT INTO records(payload) VALUES (?)",((json_safe(x),) for x in items))
    def commit(self): self.db.commit()
    def iter_records(self,batch=1000):
        cur=self.db.execute("SELECT payload FROM records ORDER BY id")
        while True:
            rows=cur.fetchmany(batch)
            if not rows: break
            for (payload,) in rows: yield json.loads(payload)
    def close(self):
        self.db.close()
        if not self.keep:
            for suffix in ("","-wal","-shm"):
                try: os.remove(self.path+suffix)
                except FileNotFoundError: pass

def publish_release(staging_dir,output_dir,required):
    """Publish one completed release directory via atomic directory rename.
    Existing release is retained until new directory is fully validated; backup swap is rollback-safe.
    """
    staging=Path(staging_dir); output=Path(output_dir); output.parent.mkdir(parents=True,exist_ok=True)
    for name in required:
        p=staging/name
        if not p.is_file(): raise FileNotFoundError(f"Missing staged artifact: {name}")
    candidate=output.with_name(output.name+".candidate")
    backup=output.with_name(output.name+".previous")
    if candidate.exists(): shutil.rmtree(candidate)
    os.replace(staging,candidate)
    if backup.exists(): shutil.rmtree(backup)
    had_old=output.exists()
    try:
        if had_old: os.replace(output,backup)
        os.replace(candidate,output)
    except Exception:
        if had_old and backup.exists() and not output.exists(): os.replace(backup,output)
        raise
    if backup.exists(): shutil.rmtree(backup)
    return str(output)
