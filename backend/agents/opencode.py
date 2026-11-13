"""OpencodeAdapter — OpenCode (opencode.ai) sessions from its SQLite database.

Database location (first hit wins):
  1. $OPENCODE_DB
  2. $XDG_DATA_HOME/opencode/opencode.db
  3. $OPENCODE_BASE/share/opencode/opencode.db
  4. ~/.local/share/opencode/opencode.db
  5. the `db` line of `opencode debug paths` (OpenCode v2; `opencode db path`
     no longer exists)

Schemas (verified against real data, OpenCode 2.2.7):
  v2  `session_v2` (one row per session; `parent_id` is set for subagent runs)
      and `session_message(id, session_id, type, seq, data)`, ordered by `seq`:
        user        {text, files[{name, mime, data}]}
        assistant   {model{id,…}, content[], tokens{input, output, reasoning,
                     cache{read, write}}}; content parts are inlined:
                     reasoning{text} · text{text} · tool{name, state{status,
                     input, content[{type: text|file}] | error{message}}}
        compaction  {summary, reason, tokens} · synthetic / system {text}
        shell       {command, exit, output{output}}
        model-switched / location-switched / idle — bookkeeping
      `session_part` exists but is empty today; it is read as a fallback for an
      assistant message's content.
  v1  `session`, `message` (data.role, data.tokens), `part` (data.type: text,
      reasoning, tool{tool, state.output}, file, patch, step-finish), `todo`.
  v2 migrated every v1 session into session_v2 (same ids), so once session_v2
  exists it is the only source of sessions. `todo` has no v2 equivalent (v2 has
  no todo tool); it still supplies read-only tasks for old sessions.

Writes: archive and delete go through OpenCode itself (`opencode api
session.update`, `opencode session delete`), and only when the running
OpenCode service on this host has exactly this database file open (checked
via `opencode api server.info` + /proc/<pid>/fd). Any other v2 database is
read-only — raw SQL into a DB owned by a running v2 server would bypass its
bookkeeping. Legacy v1-only databases keep the direct-SQL behaviour.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import socket
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import AgentAdapter, Capabilities, UnsupportedCapability
from .common import (
    STD_WINDOW, SessionSummary, connect_sqlite_ro, default_export_dir,
    is_claude_model, is_safe_id, private_mkdir, read_doc_file,
    read_git_info, save_doc_file, write_private,
)


# ---------------------------------------------------------------------------
# Locating the database / talking to the CLI
# ---------------------------------------------------------------------------

def _opencode_cli() -> str | None:
    return shutil.which("opencode")


@functools.lru_cache(maxsize=1)
def _cli_paths() -> dict[str, str]:
    """`opencode debug paths` as {name: path}; {} if the CLI is unavailable."""
    cli = _opencode_cli()
    if not cli:
        return {}
    try:
        proc = subprocess.run([cli, "debug", "paths"], capture_output=True, text=True,
                              timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    paths: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, _, value = line.strip().partition(" ")
        if name and value.strip():
            paths[name] = value.strip()
    return paths


def opencode_db_path() -> Path | None:
    """Locate OpenCode's SQLite database (order in the module docstring)."""
    candidates: list[Path] = []
    env_db = os.environ.get("OPENCODE_DB")
    if env_db:
        candidates.append(Path(env_db).expanduser())
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        candidates.append(Path(xdg_data).expanduser() / "opencode" / "opencode.db")
    base = os.environ.get("OPENCODE_BASE")
    if base:
        candidates.append(Path(base).expanduser() / "share" / "opencode" / "opencode.db")
    candidates.append(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
    for p in candidates:
        if p.is_file():
            return p
    cli_db = _cli_paths().get("db")
    if cli_db and Path(cli_db).expanduser().is_file():
        return Path(cli_db).expanduser()
    return None


def _is_cli_managed(db_file: Path) -> bool:
    """True only if db_file is *the* database the running OpenCode service — the
    process `opencode session delete` / `opencode api` talk to — is writing.

    `opencode debug paths` can't tell: it inherits our environment, so with
    OPENCODE_DB set it just echoes our override back, while the shared
    service may be using a different database.
    """
    files = _service_db_files()
    return files == {os.path.realpath(db_file)}


def _service_db_files() -> set[str] | None:
    """The SQLite databases the running OpenCode service is actively writing.

    Asks the service for its pid and host (`opencode api server.info`), then
    keeps the *.db files it has open read-write *and* whose -wal file it has
    open too (a live WAL database), so a backup it merely reads doesn't count.
    None when that can't be established: no CLI, no service, a service on
    another host, or no /proc.
    """
    cli = _opencode_cli()
    if not cli:
        return None
    try:
        proc = subprocess.run([cli, "api", "server.info"], capture_output=True, text=True,
                              timeout=30, stdin=subprocess.DEVNULL)
        info = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(info, dict) and isinstance(info.get("data"), dict):
        info = info["data"]
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return None
    if str(info.get("hostname", "")).split(".")[0] != socket.gethostname().split(".")[0]:
        return None
    pid = info["pid"]
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return None
    writable: set[str] = set()
    open_files: set[str] = set()
    for fd in fds:
        try:
            target = os.path.realpath(os.readlink(fd))
            flags = next((int(line.split()[1], 8) for line in
                          Path(f"/proc/{pid}/fdinfo/{fd.name}").read_text().splitlines()
                          if line.startswith("flags:")), 0)
        except (OSError, ValueError, IndexError):
            continue
        open_files.add(target)
        if target.endswith(".db") and flags & os.O_ACCMODE == os.O_RDWR:
            writable.add(target)
    return {db for db in writable if db + "-wal" in open_files}


def _run_opencode(args: list[str]) -> None:
    """Run `opencode <args>`; raise RuntimeError with its message on failure."""
    cli = _opencode_cli()
    if not cli:
        raise UnsupportedCapability("the `opencode` CLI is not available")
    what = " ".join(args[:2])
    try:
        proc = subprocess.run([cli, *args], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"`opencode {what}` failed: {e}") from e
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"`opencode {what}` failed: "
                           f"{lines[-1] if lines else f'exit {proc.returncode}'}")


def _connect_ro(db_file: Path) -> sqlite3.Connection:
    """Read-only connection to the live DB (mode=ro; see connect_sqlite_ro)."""
    return connect_sqlite_ro(db_file)


def _connect_rw(db_file: Path) -> sqlite3.Connection:
    """Read-write connection (legacy v1-only databases)."""
    conn = sqlite3.connect(db_file, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _is_v2(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "session_v2") and _has_table(conn, "session_message")


def _session_table(conn: sqlite3.Connection) -> str:
    return "session_v2" if _is_v2(conn) else "session"


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------

def _mangle(cwd: str) -> str:
    return cwd.replace("/", "-").replace("\\", "-").replace(":", "-")


def _json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ms_to_iso(ms: Any) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _context_from_tokens(tokens: Any) -> int:
    """Live context of one turn: input + cache read/write + output."""
    t = _json(tokens)
    if not t:
        return 0
    cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
    total = (_int(t.get("input")) + _int(t.get("output"))
             + _int(cache.get("read")) + _int(cache.get("write")))
    return total or _int(t.get("total"))


def _format_model_name(model_raw: Any) -> str:
    if not model_raw:
        return ""
    if isinstance(model_raw, str):
        try:
            m_dict = json.loads(model_raw)
        except (TypeError, ValueError):
            return model_raw
        if isinstance(m_dict, dict):
            return m_dict.get("id") or m_dict.get("modelID") or ""
        return model_raw
    if isinstance(model_raw, dict):
        return model_raw.get("id") or model_raw.get("modelID") or ""
    return str(model_raw)


def _file_label(f: dict[str, Any]) -> str:
    """Name an attachment without ever echoing inline base64 / data: URIs."""
    name = f.get("filename") or f.get("name") or f.get("path")
    uri = f.get("url") or f.get("uri") or ""
    if not name and isinstance(uri, str) and uri and not uri.startswith("data:"):
        name = uri
    mime = f.get("mime") or ""
    if name and mime:
        return f"{name} ({mime})"
    return str(name or mime or "file")


def _tool_result_text(state: dict[str, Any]) -> str | None:
    if state.get("status") == "error" or state.get("error"):
        err = state.get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        return f"Error: {msg}" if msg else "Error"
    out = state.get("output")  # v1
    if out is not None:
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    content = state.get("content")  # v2
    if isinstance(content, list):
        pieces = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                pieces.append(c.get("text") or "")
            elif c.get("type") == "file":
                pieces.append(f"[file: {_file_label(c)}]")
        return "\n".join(pieces)
    return None


def _content_blocks(parts: Iterable[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Normalize OpenCode parts (v1 rows or v2 inline content) into blocks.

    Returns (blocks, has_text) where has_text means text or reasoning exists.
    """
    blocks: list[dict[str, Any]] = []
    has_text = False
    for p in parts:
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "text":
            if p.get("text"):
                blocks.append({"type": "text", "text": p["text"]})
                has_text = True
        elif ptype == "reasoning":
            if p.get("text"):
                blocks.append({"type": "thinking", "text": p["text"]})
                has_text = True
        elif ptype == "tool":
            state = p.get("state") if isinstance(p.get("state"), dict) else {}
            blocks.append({"type": "tool_use", "name": p.get("name") or p.get("tool") or "",
                           "input": state.get("input") or {}})
            result = _tool_result_text(state)
            if result is not None:
                blocks.append({"type": "tool_result", "text": result})
        elif ptype == "file":
            blocks.append({"type": "text", "text": f"[Attachment: {_file_label(p)}]"})
        elif ptype == "patch":
            files = p.get("files") or []
            if files:
                blocks.append({"type": "text",
                               "text": f"[Modified files: {', '.join(map(str, files))}]"})
    return blocks, has_text


def _turn(uuid: str, role: str, kind: str, timestamp: str, blocks: list[dict[str, Any]],
          model: str = "", output_tokens: int = 0) -> dict[str, Any]:
    return {"uuid": uuid, "role": role, "kind": kind, "timestamp": timestamp,
            "model": model, "blocks": blocks, "attribution_skill": None,
            "attribution_plugin": None, "output_tokens": output_tokens}


def _chunks(seq: list[str], size: int = 500) -> Iterator[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# v2: session_v2 + session_message
# ---------------------------------------------------------------------------

def _v2_turn(mid: str, mtype: str, data: dict[str, Any], created_ms: Any,
             extra_parts: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    ts = _ms_to_iso(created_ms or (data.get("time") or {}).get("created"))
    if mtype == "user":
        blocks = [{"type": "text", "text": data["text"]}] if data.get("text") else []
        for f in data.get("files") or []:
            if isinstance(f, dict):
                blocks.append({"type": "text", "text": f"[Attachment: {_file_label(f)}]"})
        return _turn(mid, "user", "user", ts, blocks) if blocks else None
    if mtype == "assistant":
        # Stored parts (session_part) come first, then the inline tail.
        blocks, has_text = _content_blocks([*(extra_parts or []), *(data.get("content") or [])])
        if not blocks and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            blocks, has_text = [{"type": "text", "text": f"[error] {msg or err}"}], True
        if not blocks:
            return None
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        return _turn(mid, "assistant", "assistant" if has_text else "tool", ts, blocks,
                     _format_model_name(data.get("model")), _int(tokens.get("output")))
    if mtype == "compaction":
        if not data.get("summary"):
            return None
        text = f"[compaction · {data.get('reason') or 'auto'}]\n{data['summary']}"
        return _turn(mid, "system", "system", ts, [{"type": "text", "text": text}])
    if mtype in ("synthetic", "system"):
        text = data.get("text") or data.get("description")
        return _turn(mid, "system", "system", ts, [{"type": "text", "text": text}]) if text else None
    if mtype == "shell":
        out = data.get("output")
        out_text = out.get("output") if isinstance(out, dict) else out
        blocks = [{"type": "tool_use", "name": "shell", "input": {"command": data.get("command", "")}}]
        if out_text is not None or data.get("exit") is not None:
            text = str(out_text or "")
            if data.get("exit") not in (None, 0):
                text += f"\n[exit {data['exit']}]"
            blocks.append({"type": "tool_result", "text": text})
        return _turn(mid, "user", "tool", ts, blocks)
    if mtype == "model-switched":
        new = _format_model_name(data.get("model")) or "?"
        old = _format_model_name(data.get("previous")) or "?"
        return _turn(mid, "system", "system", ts, [{"type": "text", "text": f"[model] {old} → {new}"}])
    if mtype == "location-switched":
        new = (data.get("location") or {}).get("directory") or "?"
        old = ((data.get("previous") or {}).get("location") or {}).get("directory") or "?"
        return _turn(mid, "system", "system", ts, [{"type": "text", "text": f"[directory] {old} → {new}"}])
    return None  # idle and other bookkeeping


def _v2_turns(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    parts_by_msg: dict[str, list[dict[str, Any]]] = {}
    if _has_table(conn, "session_part"):
        for pr in conn.execute(
                "SELECT message_id, type, data FROM session_part WHERE session_id = ? "
                "ORDER BY message_id, ordinal", (session_id,)):
            part = dict(_json(pr["data"]))
            part.setdefault("type", pr["type"])
            parts_by_msg.setdefault(pr["message_id"], []).append(part)
    turns = []
    for r in conn.execute(
            "SELECT id, type, time_created, data FROM session_message WHERE session_id = ? "
            "ORDER BY seq", (session_id,)):
        turn = _v2_turn(r["id"], r["type"], _json(r["data"]), r["time_created"],
                        parts_by_msg.get(r["id"]))
        if turn:
            turns.append(turn)
    return turns


def _v2_session_stats(conn: sqlite3.Connection, ids: list[str],
                      db_key: str = "") -> dict[str, dict[str, Any]]:
    stats = {sid: {"prompts": [], "user": 0, "assistant": 0, "context": 0, "peak": 0,
                   "peak_claude": 0, "model": ""} for sid in ids}
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT session_id, json_extract(data, '$.text') AS text FROM session_message "
                f"WHERE session_id IN ({ph}) AND type = 'user' ORDER BY session_id, seq", chunk):
            text = (r["text"] or "").strip()
            if text:
                stats[r["session_id"]]["prompts"].append(text[:500])
        for r in conn.execute(
                f"SELECT session_id, type, COUNT(*) AS n FROM session_message "
                f"WHERE session_id IN ({ph}) AND type IN ('user', 'assistant') "
                f"GROUP BY session_id, type", chunk):
            stats[r["session_id"]][r["type"]] = r["n"]
    for sid in ids:
        stats[sid].update(_v2_context(conn, db_key, sid))
    return stats


# (db, session) -> ((row count, newest time_created, newest time_updated),
# context stats). Reading every turn's tokens means parsing every message's
# JSON (seconds on big sessions), so it is done once. Afterwards, if rows were
# only appended or updated in place (OpenCode fills in a turn's tokens when it
# finishes), just those are re-read; anything else — e.g. a revert deleting
# turns, possibly replacing them — rebuilds the stats from scratch.
_CTX_CACHE: dict[tuple[str, str], tuple[tuple[int, int, int], dict[str, Any]]] = {}

_CTX_ROWS = "FROM session_message WHERE session_id = ? AND type IN ('assistant', 'compaction')"


def _v2_context(conn: sqlite3.Connection, db_key: str, sid: str) -> dict[str, Any]:
    """context = the newest finished assistant turn's tokens; model = the newest
    assistant turn's model; peak_claude = the largest context any Claude turn
    (or Claude compaction) reached — only that can prove the 1M tier; peak =
    the largest context overall."""
    cached = _CTX_CACHE.get((db_key, sid))
    old_n, old_created, old_updated = cached[0] if cached else (0, 0, 0)
    n, created, updated, n_new = conn.execute(
        f"SELECT COUNT(*), MAX(time_created), MAX(time_updated), SUM(time_created > ?) "
        f"{_CTX_ROWS}", (old_created, sid)).fetchone()
    key = (n or 0, created or 0, updated or 0)
    if cached and cached[0] == key:
        return dict(cached[1])
    # Every row seen before is still there iff the count grew by exactly the
    # rows created since; a deleted row (even one replaced by a new row) breaks that.
    only_appended = (cached is not None and key[0] == old_n + (n_new or 0)
                     and key[1] >= old_created and key[2] >= old_updated)
    st = {"context": 0, "context_seq": -1, "model": "", "model_seq": -1,
          "peak": 0, "peak_claude": 0}
    query = (f"SELECT type, seq, json_extract(data, '$.tokens') AS tokens, "
             f"json_extract(data, '$.model') AS model {_CTX_ROWS}")
    args: tuple = (sid,)
    if only_appended:  # re-read just the rows created or updated since
        st = dict(cached[1])
        query += " AND (time_created > ? OR time_updated >= ?)"
        args = (sid, old_created, old_updated)
    for r in conn.execute(query + " ORDER BY seq", args):
        ctx = _context_from_tokens(r["tokens"])
        model = _format_model_name(r["model"])
        if r["type"] == "assistant":
            if model and r["seq"] >= st["model_seq"]:
                st["model"], st["model_seq"] = model, r["seq"]
            if ctx and r["seq"] >= st["context_seq"]:
                st["context"], st["context_seq"] = ctx, r["seq"]
        if ctx:
            st["peak"] = max(st["peak"], ctx)
            if is_claude_model(model):
                st["peak_claude"] = max(st["peak_claude"], ctx)
    _CTX_CACHE[(db_key, sid)] = (key, dict(st))
    return st


# ---------------------------------------------------------------------------
# v1 (legacy): session + message + part
# ---------------------------------------------------------------------------

def _v1_turns(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    parts_by_msg: dict[str, list[dict[str, Any]]] = {}
    for pr in conn.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created, id",
            (session_id,)):
        parts_by_msg.setdefault(pr["message_id"], []).append(_json(pr["data"]))
    turns = []
    for m in conn.execute(
            "SELECT id, time_created, data FROM message WHERE session_id = ? "
            "ORDER BY time_created, id", (session_id,)):
        data = _json(m["data"])
        parts = parts_by_msg.get(m["id"], [])
        role = "user" if (data.get("role") or "user") == "user" else "assistant"
        blocks, has_text = _content_blocks(parts)
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        out_tokens = _int(tokens.get("output"))
        if not out_tokens:
            for p in parts:
                if p.get("type") == "step-finish" and isinstance(p.get("tokens"), dict):
                    out_tokens = _int(p["tokens"].get("output"))
                    if out_tokens:
                        break
        if not blocks:
            summary = data.get("summary")
            if not (isinstance(summary, str) and summary):
                continue
            blocks = [{"type": "text", "text": summary}]
        kind = "user" if role == "user" else ("assistant" if has_text else "tool")
        ts = _ms_to_iso(m["time_created"] or (data.get("time") or {}).get("created"))
        turns.append(_turn(m["id"], role, kind, ts, blocks,
                           _format_model_name(data.get("modelID") or data.get("model")), out_tokens))
    return turns


def _v1_session_stats(conn: sqlite3.Connection, ids: list[str],
                      db_key: str = "") -> dict[str, dict[str, Any]]:
    stats = {sid: {"prompts": [], "user": 0, "assistant": 0, "context": 0, "peak": 0,
                   "peak_claude": 0, "model": ""} for sid in ids}
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT m.session_id, p.data AS part_data FROM message m "
                f"JOIN part p ON m.id = p.message_id "
                f"WHERE m.session_id IN ({ph}) AND json_extract(m.data, '$.role') = 'user' "
                f"AND json_extract(p.data, '$.type') = 'text' "
                f"ORDER BY m.time_created, m.id", chunk):
            text = (_json(r["part_data"]).get("text") or "").strip()
            if text:
                stats[r["session_id"]]["prompts"].append(text[:500])
        # v1 message rows are small (parts live elsewhere), so walk them all.
        for r in conn.execute(
                f"SELECT session_id, data FROM message WHERE session_id IN ({ph}) "
                f"ORDER BY time_created, id", chunk):
            st = stats[r["session_id"]]
            data = _json(r["data"])
            role = data.get("role") or "user"
            if role in ("user", "assistant"):
                st[role] += 1
            if role == "assistant":
                model = _format_model_name(data.get("modelID"))
                ctx = _context_from_tokens(data.get("tokens"))
                if ctx:
                    st["context"] = ctx
                    st["peak"] = max(st["peak"], ctx)
                    if is_claude_model(model):
                        st["peak_claude"] = max(st["peak_claude"], ctx)
                st["model"] = model or st["model"]
    return stats


def _todo_counts(conn: sqlite3.Connection, ids: list[str]) -> dict[str, tuple[int, int]]:
    if not _has_table(conn, "todo"):
        return {}
    out: dict[str, tuple[int, int]] = {}
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT session_id, COUNT(*) AS total, "
                f"SUM(CASE WHEN status IN ('completed', 'cancelled') THEN 0 ELSE 1 END) AS open "
                f"FROM todo WHERE session_id IN ({ph}) GROUP BY session_id", chunk):
            out[r["session_id"]] = (r["total"] or 0, r["open"] or 0)
    return out


def _todo_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(r["position"]),
        "subject": r["content"],
        "status": r["status"] or "pending",
        "priority": r["priority"] or "medium",
        "position": r["position"],
        "session_id": r["session_id"],
        "owner": "",
        "blocks": [],
        "blockedBy": [],
    }


class OpencodeAdapter(AgentAdapter):
    capabilities = Capabilities(
        agent_id="opencode",
        label="OpenCode",
        doc_filename="AGENTS.md",
        has_memory=False,
        has_tasks=True,
        can_edit_doc=True,
        can_delete=True,
        can_export=True,
        delete_note=("Goes through OpenCode itself: a soft delete archives the session (restore "
                     "it in OpenCode), a permanent delete also removes its subagent sessions. "
                     "Only possible while the OpenCode service on this host owns this database."),
    )

    def _db_path(self) -> Path | None:
        return opencode_db_path()

    def has_data(self) -> bool:
        return self._db_path() is not None

    def _project_cwd(self, project: str) -> str:
        """Resolve a project name (mangled cwd) back to its real directory."""
        db_file = self._db_path()
        if not db_file:
            return ""
        conn = _connect_ro(db_file)
        try:
            table = _session_table(conn)
            for r in conn.execute(f"SELECT DISTINCT directory FROM {table} "
                                  f"WHERE directory IS NOT NULL AND directory != ''"):
                d = r["directory"]
                if _mangle(d) == project or d == project:
                    return d
        finally:
            conn.close()
        return ""

    def list_projects(self) -> list[dict[str, Any]]:
        db_file = self._db_path()
        if not db_file:
            return []
        conn = _connect_ro(db_file)
        try:
            table = _session_table(conn)
            rows = conn.execute(f"""
                SELECT directory,
                       SUM(CASE WHEN parent_id IS NULL OR parent_id = '' THEN 1 ELSE 0 END)
                           AS session_count,
                       MAX(time_updated) AS last_ms
                FROM {table}
                WHERE directory IS NOT NULL AND directory != ''
                  AND (time_archived IS NULL OR time_archived = 0)
                GROUP BY directory
            """).fetchall()
            todos: dict[str, tuple[int, int]] = {}
            if _has_table(conn, "todo"):
                for r in conn.execute(f"""
                    SELECT s.directory AS directory, COUNT(*) AS total,
                           SUM(CASE WHEN t.status IN ('completed', 'cancelled') THEN 0 ELSE 1 END)
                               AS open
                    FROM todo t JOIN {table} s ON s.id = t.session_id
                    WHERE s.time_archived IS NULL OR s.time_archived = 0
                    GROUP BY s.directory
                """):
                    todos[r["directory"]] = (r["total"] or 0, r["open"] or 0)
        finally:
            conn.close()

        out: list[dict[str, Any]] = []
        for r in rows:
            cwd = r["directory"]
            total, opn = todos.get(cwd, (0, 0))
            out.append({
                "name": _mangle(cwd),
                "display_path": cwd,
                "cwd": cwd,
                "session_count": r["session_count"] or 0,
                "has_memory": False,
                "has_claude_md": (Path(cwd) / "AGENTS.md").is_file(),
                "open_tasks": opn,
                "total_tasks": total,
                "git": read_git_info(cwd),
                "mtime": (r["last_ms"] or 0) / 1000.0,
            })
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def list_sessions(self, project: str) -> list[SessionSummary]:
        db_file = self._db_path()
        if not db_file:
            return []
        cwd = self._project_cwd(project)
        if not cwd:
            return []
        conn = _connect_ro(db_file)
        try:
            v2 = _is_v2(conn)
            table = "session_v2" if v2 else "session"
            sessions = conn.execute(
                f"SELECT * FROM {table} WHERE directory = ? "
                f"AND (time_archived IS NULL OR time_archived = 0) ORDER BY time_updated DESC",
                (cwd,)).fetchall()
            ids = [s["id"] for s in sessions]
            stats = (_v2_session_stats if v2 else _v1_session_stats)(conn, ids, str(db_file))
            todos = _todo_counts(conn, ids)
        finally:
            conn.close()

        git_branch = (read_git_info(cwd) or {}).get("branch") or ""
        summaries: list[SessionSummary] = []
        for s in sessions:
            sid = s["id"]
            keys = s.keys()
            st = stats.get(sid, {})
            prompts = st.get("prompts") or []
            user_turns = st.get("user", 0)
            asst_turns = st.get("assistant", 0)
            ctx = st.get("context", 0)
            total, opn = todos.get(sid, (0, 0))
            model = st.get("model") or _format_model_name(s["model"] if "model" in keys else "")
            # Only Claude usage proves the 1M tier, and only for a Claude session.
            peak = max(ctx, st.get("peak", 0))
            win = {"limit": 1_000_000 if peak > STD_WINDOW else STD_WINDOW,
                   "known": is_claude_model(model) and st.get("peak_claude", 0) > STD_WINDOW}
            updated = s["time_updated"] or s["time_created"] or 0
            summaries.append(SessionSummary(
                session_id=sid,
                project=project,
                file=str(db_file),
                # Change key for the search index (mtime + this); not a byte size.
                size_bytes=user_turns + asst_turns,
                mtime=updated / 1000.0,
                first_prompt=prompts[0] if prompts else "",
                last_prompt=prompts[-1] if prompts else "",
                message_count=user_turns + asst_turns,
                user_turns=user_turns,
                assistant_turns=asst_turns,
                context_tokens=ctx,
                context_limit=win["limit"],
                context_limit_known=win["known"],
                total_output_tokens=(s["tokens_output"] if "tokens_output" in keys else 0) or 0,
                model=model,
                git_branch=git_branch,
                cwd=cwd,
                has_memory=False,
                open_tasks=opn,
                total_tasks=total,
                agent="opencode",
                title=(s["title"] if "title" in keys else "") or sid,
                parent_id=(s["parent_id"] if "parent_id" in keys else "") or "",
            ))
        summaries.sort(key=lambda x: x.mtime, reverse=True)
        return summaries

    def _turns(self, db_file: Path, session_id: str) -> list[dict[str, Any]]:
        conn = _connect_ro(db_file)
        try:
            return _v2_turns(conn, session_id) if _is_v2(conn) else _v1_turns(conn, session_id)
        finally:
            conn.close()

    def get_conversation(self, project: str, session_id: str,
                         offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        db_file = self._db_path()
        if not db_file:
            return {"total": 0, "offset": offset, "limit": limit, "turns": []}
        turns = self._turns(db_file, session_id)
        total = len(turns)
        end = total if limit is None else min(total, offset + limit)
        return {"total": total, "offset": offset, "limit": limit, "turns": turns[offset:end]}

    def iter_turns(self, project: str, session_id: str) -> Iterator[dict[str, Any]]:
        data = self.get_conversation(project, session_id, offset=0, limit=None)
        for seq, turn in enumerate(data["turns"]):
            yield {
                "seq": seq,
                "role": turn["role"],
                "kind": turn["kind"],
                "timestamp": turn.get("timestamp", ""),
                "blocks": turn["blocks"],
            }

    def get_doc(self, project: str) -> dict[str, Any]:
        return read_doc_file(self._project_cwd(project), "AGENTS.md")

    def save_doc(self, project: str, content: str) -> Path:
        return save_doc_file(self._project_cwd(project), "AGENTS.md", content)

    # ---- tasks: the legacy v1 `todo` table (OpenCode v2 has no todo tool) ----

    def get_tasks(self, session_id: str) -> list[dict[str, Any]]:
        db_file = self._db_path()
        if not db_file:
            return []
        conn = _connect_ro(db_file)
        try:
            if not _has_table(conn, "todo"):
                return []
            rows = conn.execute(
                "SELECT session_id, content, status, priority, position FROM todo "
                "WHERE session_id = ? ORDER BY position ASC", (session_id,)).fetchall()
            return [_todo_row(r) for r in rows]
        finally:
            conn.close()

    def project_tasks(self, project: str) -> list[dict[str, Any]]:
        db_file = self._db_path()
        if not db_file:
            return []
        cwd = self._project_cwd(project)
        conn = _connect_ro(db_file)
        try:
            if not cwd or not _has_table(conn, "todo"):
                return []
            table = _session_table(conn)
            rows = conn.execute(f"""
                SELECT t.session_id, t.content, t.status, t.priority, t.position
                FROM todo t JOIN {table} s ON t.session_id = s.id
                WHERE s.directory = ? AND (s.time_archived IS NULL OR s.time_archived = 0)
                ORDER BY s.time_updated DESC, t.position ASC
            """, (cwd,)).fetchall()
            return [_todo_row(r) for r in rows]
        finally:
            conn.close()

    def update_task_status(self, session_id: str, task_id: str, status: str) -> dict[str, Any]:
        valid = {"pending", "in_progress", "completed", "deleted"}
        if status not in valid:
            raise ValueError(f"invalid status: {status}")
        db_file = self._db_path()
        if not db_file:
            raise FileNotFoundError("opencode database not found")
        conn = _connect_ro(db_file)
        try:
            v2 = _is_v2(conn)
            has_todo = _has_table(conn, "todo")
        finally:
            conn.close()
        if v2:
            raise UnsupportedCapability(
                "OpenCode v2 no longer tracks todos; the legacy todo list is read-only")
        if not has_todo:
            raise FileNotFoundError(f"task {task_id} not found for session {session_id}")
        try:
            pos = int(task_id)
        except ValueError:
            raise FileNotFoundError(f"task {task_id} not found for session {session_id}")

        conn = _connect_rw(db_file)
        try:
            row = conn.execute(
                "SELECT session_id, content, status, priority, position FROM todo "
                "WHERE session_id = ? AND position = ?", (session_id, pos)).fetchone()
            if not row:
                raise FileNotFoundError(f"task {task_id} not found for session {session_id}")
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            conn.execute("UPDATE todo SET status = ?, time_updated = ? "
                         "WHERE session_id = ? AND position = ?", (status, now_ms, session_id, pos))
            conn.commit()
            return {"id": str(pos), "subject": row["content"], "status": status,
                    "priority": row["priority"], "position": pos, "session_id": session_id}
        finally:
            conn.close()

    # ---- export / delete ----

    def export_session_markdown(self, project: str, session_id: str) -> str:
        db_file = self._db_path()
        if not db_file:
            raise FileNotFoundError("opencode database not found")
        conn = _connect_ro(db_file)
        try:
            table = _session_table(conn)
            s = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (session_id,)).fetchone()
            if not s:
                raise FileNotFoundError(f"session {session_id} not found")
            turns = _v2_turns(conn, session_id) if table == "session_v2" else _v1_turns(conn, session_id)
        finally:
            conn.close()

        cwd = s["directory"] or self._project_cwd(project)
        model = _format_model_name(s["model"] if "model" in s.keys() else "")
        lines = [
            f"# Session: {s['title'] or session_id}",
            "",
            "- **Agent:** OpenCode",
            f"- **Session ID:** `{session_id}`",
            f"- **Created:** {_ms_to_iso(s['time_created'])}",
            f"- **Model:** {model or 'unknown'}",
            f"- **Directory:** `{cwd}`",
            f"- **Total Turns:** {len(turns)}",
            "",
            "---",
            "",
        ]
        for t in turns:
            ts = f" ({t['timestamp']})" if t.get("timestamp") else ""
            lines.append(f"### {t['role'].capitalize()}{ts}")
            lines.append("")
            for b in t["blocks"]:
                bt = b.get("type")
                if bt == "text":
                    lines.append(b.get("text", ""))
                elif bt == "thinking":
                    indented = b.get("text", "").replace("\n", "\n> ")
                    lines.append(f"> **Thinking:**\n> {indented}")
                elif bt == "tool_use":
                    inp = json.dumps(b.get("input", {}), indent=2, ensure_ascii=False)
                    lines.append(f"**→ tool: {b.get('name', '')}**\n\n```json\n{inp}\n```")
                elif bt == "tool_result":
                    lines.append(f"**tool result**\n\n```\n{b.get('text', '')}\n```")
                lines.append("")
            lines.append("---")
            lines.append("")
        return "\n".join(lines)

    def export_session_to_file(self, project: str, session_id: str, out_dir: Path | None = None) -> Path:
        if not is_safe_id(session_id):
            raise ValueError("invalid session id")
        markdown = self.export_session_markdown(project, session_id)  # 404s unknown sessions
        if out_dir is None:
            out_dir = default_export_dir(project)
        private_mkdir(out_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return write_private(out_dir / f"{session_id}.{stamp}.md", markdown)

    def delete_session(self, project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
        if not is_safe_id(session_id):
            raise ValueError("invalid session id")
        db_file = self._db_path()
        if not db_file:
            raise FileNotFoundError("opencode database not found")
        conn = _connect_ro(db_file)
        try:
            v2 = _is_v2(conn)
            table = "session_v2" if v2 else "session"
            if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (session_id,)).fetchone():
                raise FileNotFoundError(f"session {session_id} not found")
        finally:
            conn.close()
        if v2:
            return self._delete_v2(db_file, session_id, hard)
        return self._delete_v1(db_file, session_id, hard)

    def _delete_v2(self, db_file: Path, session_id: str, hard: bool) -> dict[str, Any]:
        if not _opencode_cli() or not _is_cli_managed(db_file):
            raise UnsupportedCapability(
                "this OpenCode v2 database isn't the one the running OpenCode service "
                "has open, so cc_mgr shows it read-only")
        conn = _connect_ro(db_file)
        try:  # `opencode session delete` also removes every descendant session
            tree = [r[0] for r in conn.execute(
                "WITH RECURSIVE tree(id) AS (SELECT ? UNION "
                "SELECT s.id FROM session_v2 s JOIN tree t ON s.parent_id = t.id) "
                "SELECT id FROM tree", (session_id,))]
        finally:
            conn.close()
        if hard:
            _run_opencode(["session", "delete", session_id])
        else:
            _run_opencode(["api", "session.update", "--param", f"sessionID={session_id}",
                           "--data", json.dumps({"archived": True})])
        conn = _connect_ro(db_file)
        try:
            row = conn.execute("SELECT time_archived FROM session_v2 WHERE id = ?",
                               (session_id,)).fetchone()
        finally:
            conn.close()
        if hard and row is not None:
            raise RuntimeError(f"OpenCode did not delete session {session_id}")
        if not hard and not (row and row["time_archived"]):
            raise RuntimeError(f"OpenCode did not archive session {session_id}")
        if hard:
            return {"deleted": [session_id], "trash": None, "sessions": tree,
                    "note": "Deleted in OpenCode (its subagent sessions too)."}
        return {"deleted": [session_id], "trash": "opencode-archive", "sessions": [session_id],
                "note": "Archived in OpenCode; restore it from OpenCode's session list."}

    def _delete_v1(self, db_file: Path, session_id: str, hard: bool) -> dict[str, Any]:
        """Legacy v1-only database: there is no v2 server to go through."""
        conn = _connect_rw(db_file)
        try:
            if hard:
                conn.execute("DELETE FROM part WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
                if _has_table(conn, "todo"):
                    conn.execute("DELETE FROM todo WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
            else:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                conn.execute("UPDATE session SET time_archived = ? WHERE id = ?",
                             (now_ms, session_id))
            conn.commit()
        finally:
            conn.close()
        if hard:
            return {"deleted": [session_id], "trash": None}
        return {"deleted": [session_id], "trash": "opencode-archive",
                "note": "Archived (legacy OpenCode database)."}
