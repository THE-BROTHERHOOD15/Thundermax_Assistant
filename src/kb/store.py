"""SQLite access, stable IDs, JSONL manifests."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from kb.paths import CONTROLLED_TAGS, kb_db, kb_root
from kb.schema import SCHEMA_SQL

ID_WIDTH = {
    "file": 6,
    "chunk": 6,
    "claim": 5,
    "observation": 5,
    "run": 6,
    "manifest": 5,
}


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(root=None, create=True):
    db = kb_db(root)
    if create:
        db.parent.mkdir(parents=True, exist_ok=True)
    if not db.exists() and not create:
        raise FileNotFoundError(f"KB database missing: {db}")
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    if create:
        con.executescript(SCHEMA_SQL)
        for tag in CONTROLLED_TAGS:
            con.execute("INSERT OR IGNORE INTO tags(tag) VALUES (?)", (tag,))
        con.commit()
    return con


def next_id(con, kind):
    row = con.execute("SELECT n FROM id_counters WHERE kind=?", (kind,)).fetchone()
    n = (row["n"] + 1) if row else 1
    if row:
        con.execute("UPDATE id_counters SET n=? WHERE kind=?", (n, kind))
    else:
        con.execute("INSERT INTO id_counters(kind, n) VALUES (?, ?)", (kind, n))
    width = ID_WIDTH.get(kind, 6)
    return f"{kind}-{n:0{width}d}"


def append_jsonl(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def status(root=None):
    base = kb_root(root)
    db = kb_db(root)
    out = {
        "root": str(base),
        "db": str(db),
        "exists": db.exists(),
        "files": 0,
        "chunks": 0,
        "embeddings": 0,
        "claims": 0,
        "quarantine": 0,
        "validated_files": 0,
    }
    if not db.exists():
        return out
    con = connect(root, create=False)
    try:
        out["files"] = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        out["chunks"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        out["embeddings"] = con.execute("SELECT COUNT(*) FROM embedding_records").fetchone()[0]
        out["claims"] = con.execute("SELECT COUNT(*) FROM knowledge_claims").fetchone()[0]
        out["quarantine"] = con.execute(
            "SELECT COUNT(*) FROM files WHERE parser_status IN "
            "('corrupt','unsupported','duplicate','suspicious')"
        ).fetchone()[0]
        out["validated_files"] = con.execute(
            "SELECT COUNT(*) FROM files WHERE validated=1"
        ).fetchone()[0]
    finally:
        con.close()
    return out
