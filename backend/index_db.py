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
from .agents import all_adapters


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


# Bump when the table/FTS schema changes; the DB is a rebuildable cache, so a
# mismatch just drops and rebuilds everything.
SCHEMA_VERSION = 3


def _schema_outdated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return (row[0] if row else 0) != SCHEMA_VERSION
    except sqlite3.OperationalError:
        return True


def init_db(conn: sqlite3.Connection) -> bool:
    """Create schema. Returns True if FTS5 is available.

    Drops and recreates tables when SCHEMA_VERSION changes.
    """
    fts = _has_fts5(conn)
    if _schema_outdated(conn):
        conn.executescript(
            "DROP TABLE IF EXISTS turns; DROP TABLE IF EXISTS sessions; "
            "DROP TABLE IF EXISTS docs;"
        )
        if fts:
            conn.executescript(
                "DROP TABLE IF EXISTS turns_fts; DROP TABLE IF EXISTS docs_fts;"
            )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            agent      TEXT,
            session_id TEXT,
            project    TEXT,
            mtime      REAL,
            size       INTEGER,
            turns      INTEGER,
            context_tokens INTEGER,
            last_prompt TEXT,
            PRIMARY KEY (agent, session_id)
        );
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent      TEXT,
            session_id TEXT,
            project    TEXT,
            seq        INTEGER,
            role       TEXT,
            kind       TEXT,
            timestamp  TEXT,
            text       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(agent, session_id);
        CREATE TABLE IF NOT EXISTS docs (
            agent   TEXT,
            project TEXT,
            source  TEXT,
            ref     TEXT,
            path    TEXT,
            text    TEXT,
            mtime   REAL,
            PRIMARY KEY (agent, project, source, ref)
        );
        """
    )
    if fts:
        # Standalone FTS tables (not external-content): deletes are a plain
        # DELETE, which keeps incremental reindexing simple and corruption-free.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
            "text, agent UNINDEXED, session_id UNINDEXED, project UNINDEXED, "
            "seq UNINDEXED, role UNINDEXED, turn_id UNINDEXED)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
            "text, agent UNINDEXED, project UNINDEXED, source UNINDEXED, "
            "ref UNINDEXED, path UNINDEXED)"
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
    """Walk every agent's projects/sessions and (re)index changed ones."""
    conn = _connect()
    fts = init_db(conn)
    indexed = skipped = total_turns = total_docs = 0

    for adapter in all_adapters():
        agent = adapter.capabilities.agent_id
        for proj in adapter.list_projects():
            pname = proj["name"]
            for summ in adapter.list_sessions(pname):
                row = conn.execute(
                    "SELECT mtime, size FROM sessions WHERE agent=? AND session_id=?",
                    (agent, summ.session_id),
                ).fetchone()
                if (row and not force and row["mtime"] == summ.mtime
                        and row["size"] == summ.size_bytes):
                    skipped += 1
                    continue

                conn.execute("DELETE FROM turns WHERE agent=? AND session_id=?",
                             (agent, summ.session_id))
                if fts:
                    conn.execute("DELETE FROM turns_fts WHERE agent=? AND session_id=?",
                                 (agent, summ.session_id))

                for turn in adapter.iter_turns(pname, summ.session_id):
                    text = _turn_text(turn)
                    if not text.strip():
                        continue
                    seq = turn["seq"]
                    cur = conn.execute(
                        "INSERT INTO turns(agent, session_id, project, seq, role, "
                        "kind, timestamp, text) VALUES(?,?,?,?,?,?,?,?)",
                        (agent, summ.session_id, pname, seq, turn["role"],
                         turn["kind"], turn.get("timestamp", ""), text),
                    )
                    if fts:
                        conn.execute(
                            "INSERT INTO turns_fts(text, agent, session_id, project, "
                            "seq, role, turn_id) VALUES(?,?,?,?,?,?,?)",
                            (text, agent, summ.session_id, pname, seq,
                             turn["role"], cur.lastrowid),
                        )
                    total_turns += 1

                conn.execute(
                    "INSERT OR REPLACE INTO sessions(agent, session_id, project, "
                    "mtime, size, turns, context_tokens, last_prompt) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (agent, summ.session_id, pname, summ.mtime, summ.size_bytes,
                     summ.message_count, summ.context_tokens, summ.last_prompt),
                )
                indexed += 1

            total_docs += _index_docs(conn, fts, adapter, pname)
            conn.commit()

    conn.commit()
    conn.close()
    return {"indexed": indexed, "skipped": skipped, "turns": total_turns,
            "docs": total_docs, "fts": fts}


def _index_docs(conn, fts, adapter, project) -> int:
    """Index an agent's project docs: Claude memory + its root doc; others one root doc."""
    agent = adapter.capabilities.agent_id
    conn.execute("DELETE FROM docs WHERE agent=? AND project=?", (agent, project))
    if fts:
        conn.execute("DELETE FROM docs_fts WHERE agent=? AND project=?", (agent, project))
    count = 0
    entries: list[tuple[str, str, str, str]] = []  # (source, ref, path, text)

    caps = adapter.capabilities
    if caps.has_memory:
        mem = adapter.get_memory(project)
        if mem.get("index"):
            entries.append(("memory", "MEMORY.md", mem.get("index_path", ""), mem["index"]))
        for f in mem.get("files", []):
            entries.append(("memory", f["name"], f.get("path", ""), f.get("content", "")))

    doc = adapter.get_doc(project)
    if doc.get("exists") and doc.get("content"):
        source = "claude_md" if agent == "claude" else "agent_doc"
        entries.append((source, caps.doc_filename, doc.get("path", ""), doc["content"]))

    for source, ref, path, text in entries:
        if not text.strip():
            continue
        conn.execute(
            "INSERT OR REPLACE INTO docs(agent, project, source, ref, path, text, mtime) "
            "VALUES(?,?,?,?,?,?,?)",
            (agent, project, source, ref, path, text, 0.0),
        )
        if fts:
            conn.execute(
                "INSERT INTO docs_fts(text, agent, project, source, ref, path) "
                "VALUES(?,?,?,?,?,?)",
                (text, agent, project, source, ref, path),
            )
        count += 1
    return count


def search(query, limit=50, project=None, agent=None):
    conn = _connect()
    fts = init_db(conn)
    try:
        if fts:
            try:
                out = _fts_search(conn, query, limit, project, agent)
            except sqlite3.OperationalError:
                out = _like_search(conn, query, limit, project, agent)
        else:
            out = _like_search(conn, query, limit, project, agent)
    finally:
        conn.close()
    return out


def _fts_search(conn, query, limit, project, agent=None):
    out = []
    clauses, args = [], []
    if project:
        clauses.append("project = ?"); args.append(project)
    if agent:
        clauses.append("agent = ?"); args.append(agent)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""

    turn_rows = conn.execute(
        "SELECT agent, session_id, project, seq, role, "
        "snippet(turns_fts, 0, '[', ']', ' … ', 12) AS snippet "
        f"FROM turns_fts WHERE turns_fts MATCH ?{extra} ORDER BY rank LIMIT ?",
        (query, *args, limit),
    ).fetchall()
    for r in turn_rows:
        d = dict(r); d["source"] = "conversation"; out.append(d)

    doc_rows = conn.execute(
        "SELECT agent, project, source, ref, path, "
        "snippet(docs_fts, 0, '[', ']', ' … ', 12) AS snippet "
        f"FROM docs_fts WHERE docs_fts MATCH ?{extra} ORDER BY rank LIMIT ?",
        (query, *args, limit),
    ).fetchall()
    for r in doc_rows:
        out.append(dict(r))
    return out


def _like_search(conn, query, limit, project=None, agent=None):
    like = f"%{query}%"
    out = []
    clauses, args = [], []
    if project:
        clauses.append("project = ?"); args.append(project)
    if agent:
        clauses.append("agent = ?"); args.append(agent)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""

    turn_rows = conn.execute(
        "SELECT agent, session_id, project, seq, role, "
        "substr(text, 1, 200) AS snippet FROM turns "
        f"WHERE text LIKE ?{extra} LIMIT ?",
        (like, *args, limit),
    ).fetchall()
    for r in turn_rows:
        d = dict(r); d["source"] = "conversation"; out.append(d)

    doc_rows = conn.execute(
        "SELECT agent, project, source, ref, path, "
        "substr(text, 1, 200) AS snippet FROM docs "
        f"WHERE text LIKE ?{extra} LIMIT ?",
        (like, *args, limit),
    ).fetchall()
    for r in doc_rows:
        out.append(dict(r))
    return out


def stats() -> dict[str, Any]:
    conn = _connect()
    init_db(conn)
    s = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
    t = conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"]
    d = conn.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"]
    conn.close()
    return {"sessions": s, "turns": t, "docs": d}
