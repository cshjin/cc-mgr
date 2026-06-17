"""SQLite + FTS5 index over all conversations — the local knowledge base.

The index is a derived cache: it can be rebuilt from the JSONL transcripts at
any time. We track each session's file mtime/size so reindexing only re-reads
changed sessions.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator

from . import store


def db_path() -> Path:
    return Path.cwd() / "data" / "cc_mgr.db"


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def _has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def init_db(conn: sqlite3.Connection) -> bool:
    """Create schema. Returns True if FTS5 is available."""
    fts = _has_fts5(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project    TEXT,
            mtime      REAL,
            size       INTEGER,
            turns      INTEGER,
            context_tokens INTEGER,
            last_prompt TEXT
        );
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            project    TEXT,
            seq        INTEGER,
            role       TEXT,
            kind       TEXT,
            timestamp  TEXT,
            text       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        """
    )
    if fts:
        # Standalone FTS table (not external-content): deletes are a plain
        # DELETE, which keeps incremental reindexing simple and corruption-free.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
            "text, session_id UNINDEXED, project UNINDEXED, seq UNINDEXED, "
            "role UNINDEXED, turn_id UNINDEXED)"
        )
    conn.commit()
    return fts


def _turn_text(turn: dict[str, Any]) -> str:
    parts = []
    for b in turn["blocks"]:
        if b["type"] in ("text", "thinking"):
            parts.append(b.get("text", ""))
        elif b["type"] == "tool_use":
            parts.append(f"[tool:{b.get('name','')}]")
        elif b["type"] == "tool_result":
            parts.append(b.get("text", ""))
    return "\n".join(p for p in parts if p)


def reindex(force: bool = False) -> dict[str, Any]:
    """Walk all projects/sessions and (re)index changed ones."""
    conn = _connect()
    fts = init_db(conn)
    indexed = 0
    skipped = 0
    total_turns = 0

    for proj in store.list_projects():
        pname = proj["name"]
        for summ in store.list_sessions(pname):
            row = conn.execute(
                "SELECT mtime, size FROM sessions WHERE session_id=?",
                (summ.session_id,),
            ).fetchone()
            if row and not force and row["mtime"] == summ.mtime and row["size"] == summ.size_bytes:
                skipped += 1
                continue

            # purge old rows for this session
            conn.execute("DELETE FROM turns WHERE session_id=?", (summ.session_id,))
            if fts:
                conn.execute("DELETE FROM turns_fts WHERE session_id=?", (summ.session_id,))

            data = store.get_conversation(pname, summ.session_id, offset=0, limit=None)
            for seq, turn in enumerate(data["turns"]):
                text = _turn_text(turn)
                if not text.strip():
                    continue
                cur = conn.execute(
                    "INSERT INTO turns(session_id, project, seq, role, kind, timestamp, text) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (summ.session_id, pname, seq, turn["role"], turn["kind"],
                     turn.get("timestamp", ""), text),
                )
                if fts:
                    conn.execute(
                        "INSERT INTO turns_fts(text, session_id, project, seq, role, turn_id) "
                        "VALUES(?,?,?,?,?,?)",
                        (text, summ.session_id, pname, seq, turn["role"], cur.lastrowid),
                    )
                total_turns += 1

            conn.execute(
                "INSERT OR REPLACE INTO sessions"
                "(session_id, project, mtime, size, turns, context_tokens, last_prompt) "
                "VALUES(?,?,?,?,?,?,?)",
                (summ.session_id, pname, summ.mtime, summ.size_bytes,
                 summ.message_count, summ.context_tokens, summ.last_prompt),
            )
            indexed += 1
        conn.commit()

    conn.commit()
    conn.close()
    return {"indexed": indexed, "skipped": skipped, "turns": total_turns, "fts": fts}


def search(query: str, limit: int = 50, project: str | None = None) -> list[dict[str, Any]]:
    """Full-text search across indexed turns, optionally scoped to one project.

    Falls back to LIKE if FTS5 is unavailable.
    """
    conn = _connect()
    fts = init_db(conn)
    rows: list[sqlite3.Row]
    if fts:
        try:
            if project:
                rows = conn.execute(
                    "SELECT session_id, project, seq, role, "
                    "snippet(turns_fts, 0, '[', ']', ' … ', 12) AS snippet "
                    "FROM turns_fts WHERE turns_fts MATCH ? AND project = ? "
                    "ORDER BY rank LIMIT ?",
                    (query, project, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, project, seq, role, "
                    "snippet(turns_fts, 0, '[', ']', ' … ', 12) AS snippet "
                    "FROM turns_fts WHERE turns_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            rows = _like_search(conn, query, limit, project)
    else:
        rows = _like_search(conn, query, limit, project)
    out = [dict(r) for r in rows]
    conn.close()
    return out


def _like_search(conn: sqlite3.Connection, query: str, limit: int,
                 project: str | None = None) -> list[sqlite3.Row]:
    like = f"%{query}%"
    if project:
        return conn.execute(
            "SELECT session_id, project, seq, role, timestamp, "
            "substr(text, 1, 200) AS snippet FROM turns "
            "WHERE text LIKE ? AND project = ? LIMIT ?",
            (like, project, limit),
        ).fetchall()
    return conn.execute(
        "SELECT session_id, project, seq, role, timestamp, "
        "substr(text, 1, 200) AS snippet FROM turns "
        "WHERE text LIKE ? LIMIT ?",
        (like, limit),
    ).fetchall()


def stats() -> dict[str, Any]:
    conn = _connect()
    init_db(conn)
    s = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
    t = conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"]
    conn.close()
    return {"sessions": s, "turns": t}
