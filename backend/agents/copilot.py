"""CopilotAdapter — GitHub Copilot CLI sessions under $COPILOT_HOME (~/.copilot).

Real on-disk format (verified with Copilot CLI 1.0.x):
  session-store.db (SQLite, WAL)
      sessions(id, cwd, repository, host_type, branch, summary, created_at, updated_at)
      turns(id, session_id, turn_index, user_message, assistant_response, timestamp)
          — every prompt; assistant_response is often NULL
      assistant_usage_events(session_id, model, input_tokens, output_tokens, …)
          — input_tokens already includes cache reads
  session-state/<session-id>/events.jsonl — the full transcript, when present:
      {type, data, id, timestamp, parentId}; the types used here:
        session.start                data.context{cwd, branch, …}
        user.message                 data.content, data.attachments[]
        assistant.message            data.content, data.reasoningText, data.model,
                                     data.toolRequests[{toolCallId, name, arguments}]
        tool.execution_complete      data.success, data.result{content}, data.error
        session.compaction_complete  data.summaryContent
        session.usage_record         data.usage{model, inputTokens (incl. cache), outputTokens}
        system.message               data.content
      (plus bookkeeping: turn_start/end, model.*, permission.*, …)
  A session can live in either source or both: events.jsonl wins for the
  conversation, the DB supplies the title (summary) and branch.

The older guess — history/<mangled-cwd>/*.jsonl with Claude-like records — is
still read when present (no Copilot version has been seen writing it).

The CLI has no command to delete a session (`copilot sessions` only has
`import`), so cc_mgr edits the store itself: every table with a session_id
column plus the sessions row, in one transaction, and session-state/<id>/.
A soft delete first saves those rows (db_rows.json) and the folder under
.cc_mgr_trash/. A session a running Copilot holds open
(session-state/<id>/inuse.<pid>.lock) or that was active moments ago is
refused.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities
from .common import (
    SessionSummary, block_text, connect_sqlite_ro, context_window, is_safe_id,
    is_safe_name, iter_jsonl, new_trash_dir, pid_alive, project_names, read_doc_file,
    read_git_info, resolves_inside, restore_dir, safe_join, save_doc_file, structured_blocks,
)


def copilot_home() -> Path:
    env = os.environ.get("COPILOT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".copilot"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iso_to_epoch(ts: Any) -> float:
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _peek_cwd(path: Path) -> str:
    for rec in iter_jsonl(path):
        if rec.get("cwd"):
            return rec["cwd"]
    return ""


def _turn(uuid: Any, role: str, kind: str, ts: Any, blocks: list[dict[str, Any]],
          model: str = "", output_tokens: int = 0) -> dict[str, Any]:
    return {"uuid": uuid, "role": role, "kind": kind, "timestamp": ts or "",
            "model": model, "blocks": blocks, "attribution_skill": None,
            "attribution_plugin": None, "output_tokens": output_tokens}


# ---------------------------------------------------------------------------
# session-store.db + session-state/<id>/events.jsonl
# ---------------------------------------------------------------------------

def _store_db(home: Path) -> Path | None:
    db = home / "session-store.db"
    return db if db.is_file() else None


def _events_file(home: Path, session_id: str) -> Path:
    return home / "session-state" / session_id / "events.jsonl"


def _events_head(path: Path) -> dict[str, str]:
    """cwd/branch from the session.start event at the top of events.jsonl."""
    for i, e in enumerate(iter_jsonl(path)):
        if e.get("type") == "session.start":
            ctx = (e.get("data") or {}).get("context") or {}
            return {"cwd": ctx.get("cwd") or "", "branch": ctx.get("branch") or ""}
        if i > 20:
            break
    return {"cwd": "", "branch": ""}


def _all_sessions(home: Path) -> dict[str, dict[str, Any]]:
    """id -> {id, cwd, branch, title, updated, events: Path | None}."""
    out: dict[str, dict[str, Any]] = {}
    db = _store_db(home)
    if db:
        conn = connect_sqlite_ro(db)
        try:
            for r in conn.execute("SELECT id, cwd, branch, summary, created_at, updated_at "
                                  "FROM sessions"):
                out[r["id"]] = {"id": r["id"], "cwd": r["cwd"] or "", "branch": r["branch"] or "",
                                "title": r["summary"] or "", "events": None,
                                "updated": _iso_to_epoch(r["updated_at"] or r["created_at"])}
        finally:
            conn.close()
    state = home / "session-state"
    if state.is_dir():
        for d in state.iterdir():
            ev = d / "events.jsonl"
            if not ev.is_file():
                continue
            info = out.get(d.name)
            if info is None:  # transcript without a DB row: take cwd from session.start
                head = _events_head(ev)
                info = out[d.name] = {"id": d.name, "cwd": head["cwd"], "branch": head["branch"],
                                      "title": "", "events": None, "updated": 0.0}
            info["events"] = ev
            info["updated"] = max(info["updated"], ev.stat().st_mtime)
    return {sid: s for sid, s in out.items() if s["cwd"]}


def _event_turns(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Conversation turns + usage {context, peak, output, model} from events.jsonl."""
    turns: list[dict[str, Any]] = []
    usage = {"context": 0, "peak": 0, "output": 0, "model": ""}
    for e in iter_jsonl(path):
        etype = e.get("type")
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        ts = e.get("timestamp")
        if etype == "user.message":
            blocks = [{"type": "text", "text": data["content"]}] if data.get("content") else []
            for a in data.get("attachments") or []:
                if isinstance(a, dict):
                    name = a.get("displayName") or a.get("path") or a.get("name") or a.get("type") or "file"
                    blocks.append({"type": "text", "text": f"[Attachment: {name}]"})
            if blocks:
                turns.append(_turn(e.get("id"), "user", "user", ts, blocks))
        elif etype == "assistant.message":
            blocks = []
            if data.get("reasoningText"):
                blocks.append({"type": "thinking", "text": data["reasoningText"]})
            if data.get("content"):
                blocks.append({"type": "text", "text": data["content"]})
            has_text = bool(blocks)
            for req in data.get("toolRequests") or []:
                if isinstance(req, dict):
                    blocks.append({"type": "tool_use", "name": req.get("name", ""),
                                   "input": req.get("arguments") or {}})
            if blocks:
                turns.append(_turn(e.get("id"), "assistant", "assistant" if has_text else "tool",
                                   ts, blocks, data.get("model") or ""))
        elif etype == "tool.execution_complete":
            result = data.get("result")
            if data.get("success") is False or data.get("error"):
                err = data.get("error")
                text = f"Error: {err.get('message') if isinstance(err, dict) else err or ''}".rstrip(": ")
            elif isinstance(result, dict):
                text = result.get("content") if isinstance(result.get("content"), str) \
                    else json.dumps(result, ensure_ascii=False)
            else:
                text = "" if result is None else str(result)
            turns.append(_turn(e.get("id"), "user", "tool", ts, [{"type": "tool_result", "text": text}]))
        elif etype == "session.compaction_complete" and data.get("summaryContent"):
            turns.append(_turn(e.get("id"), "system", "system", ts, [
                {"type": "text", "text": f"[compaction]\n{data['summaryContent']}"}]))
        elif etype == "system.message" and isinstance(data.get("content"), str) and data["content"]:
            turns.append(_turn(e.get("id"), "system", "system", ts,
                               [{"type": "text", "text": data["content"]}]))
        elif etype == "session.usage_record":
            u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            ctx = _int(u.get("inputTokens")) + _int(u.get("outputTokens"))
            if ctx:
                usage["context"] = ctx
                usage["peak"] = max(usage["peak"], ctx)
            usage["output"] += _int(u.get("outputTokens"))
            usage["model"] = u.get("model") or usage["model"]
    return turns, usage


def _db_turns(home: Path, session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fallback when there's no events.jsonl: prompts (+ replies if stored) and usage."""
    turns: list[dict[str, Any]] = []
    usage = {"context": 0, "peak": 0, "output": 0, "model": ""}
    db = _store_db(home)
    if not db:
        return turns, usage
    conn = connect_sqlite_ro(db)
    try:
        for r in conn.execute("SELECT id, turn_index, user_message, assistant_response, timestamp "
                              "FROM turns WHERE session_id = ? ORDER BY turn_index, id", (session_id,)):
            if r["user_message"]:
                turns.append(_turn(f"{r['id']}u", "user", "user", r["timestamp"],
                                   [{"type": "text", "text": r["user_message"]}]))
            if r["assistant_response"]:
                turns.append(_turn(f"{r['id']}a", "assistant", "assistant", r["timestamp"],
                                   [{"type": "text", "text": r["assistant_response"]}]))
        try:
            for r in conn.execute("SELECT model, input_tokens, output_tokens "
                                  "FROM assistant_usage_events WHERE session_id = ? ORDER BY id",
                                  (session_id,)):
                ctx = _int(r["input_tokens"]) + _int(r["output_tokens"])
                if ctx:
                    usage["context"] = ctx
                    usage["peak"] = max(usage["peak"], ctx)
                usage["output"] += _int(r["output_tokens"])
                usage["model"] = r["model"] or usage["model"]
        except Exception:  # older stores without usage events
            pass
    finally:
        conn.close()
    return turns, usage


def _session_turns(home: Path, info: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if info.get("events"):
        return _event_turns(info["events"])
    return _db_turns(home, info["id"])


# ---------------------------------------------------------------------------
# delete: the CLI can't, so edit session-store.db + session-state/ directly
# ---------------------------------------------------------------------------

def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _session_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, column) for every table holding a session's rows; the sessions
    row itself comes last, since the others reference it. FTS5 shadow tables
    (search_index_*) are maintained by search_index itself."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    out = [(t, "session_id") for t in tables
           if t != "sessions" and not t.startswith("search_index_")
           and "session_id" in {c[1] for c in conn.execute(f"PRAGMA table_info({_quote(t)})")}]
    return out + ([("sessions", "id")] if "sessions" in tables else [])


def _jsonable(value: Any) -> Any:
    return {"base64": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value


_LOCK_TIMEOUT = 10.0  # seconds to wait for Copilot's own writes to finish


def _delete_store_session(home: Path, db: Path | None, state_dir: Path, session_id: str,
                          hard: bool) -> dict[str, Any]:
    """Remove a session's rows (one transaction) and its session-state folder.

    The write transaction is taken first. Holding it, the session is checked
    for activity once more (anything Copilot did while we waited for the lock
    is visible now), and the backup is read: no writer can slip rows in
    between, so a soft delete saves exactly what it deletes. If anything fails
    before the commit, the transaction rolls back and the folder is put back
    (never over one that reappeared meanwhile — then the trash copy is kept).
    """
    conn = None
    if db is not None:
        conn = sqlite3.connect(db, timeout=_LOCK_TIMEOUT, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            conn.close()
            raise RuntimeError(f"session-store.db is busy ({e}); nothing was deleted") from e
    trash: Path | None = None
    moved = False
    deleted: list[str] = []
    try:
        if conn is not None:
            row = conn.execute("SELECT updated_at, created_at FROM sessions WHERE id = ?",
                               (session_id,)).fetchone()
            busy = _busy_reason(home, session_id,
                                _iso_to_epoch(row["updated_at"] or row["created_at"]) if row else 0.0)
            if busy:
                raise RuntimeError(f"not deleting Copilot session {session_id}: {busy}")
        tables = _session_tables(conn) if conn is not None else []
        if not hard:
            backup = {}
            for table, col in tables:
                rows = conn.execute(f"SELECT * FROM {_quote(table)} WHERE {col} = ?",
                                    (session_id,)).fetchall()
                if rows:
                    backup[table] = [{k: _jsonable(r[k]) for k in r.keys()} for r in rows]
            trash = new_trash_dir(home, session_id)
            (trash / "db_rows.json").write_text(
                json.dumps({"db": str(db), "session_id": session_id, "rows": backup},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            if state_dir.is_dir():
                os.rename(state_dir, trash / "session-state")  # same filesystem: both under home
                moved = True
                deleted.append(str(state_dir))
        rows = sum(conn.execute(f"DELETE FROM {_quote(table)} WHERE {col} = ?",
                                (session_id,)).rowcount for table, col in tables)
        if conn is not None:
            conn.execute("COMMIT")
    except BaseException as e:
        if conn is not None and conn.in_transaction:
            conn.execute("ROLLBACK")
        kept = ""
        if moved:
            try:
                restored = restore_dir(trash / "session-state", state_dir)
            except OSError:
                restored = False
            if not restored:
                kept = (f"; the session folder reappeared meanwhile (or couldn't be put back), "
                        f"so its previous copy is kept in {trash}")
        if trash is not None and not kept:
            shutil.rmtree(trash, ignore_errors=True)
        if isinstance(e, (sqlite3.Error, OSError)):
            raise RuntimeError(f"could not delete the session ({e}); nothing was deleted{kept}") from e
        raise
    finally:
        if conn is not None:
            conn.close()
    if rows:
        deleted.append(f"{db} ({rows} rows)")
    result: dict[str, Any] = {"deleted": deleted, "trash": str(trash) if trash else None}
    if trash:
        result["note"] = f"Moved to {trash} (its database rows are in db_rows.json)."
    if hard and state_dir.is_dir():
        try:
            shutil.rmtree(state_dir)
            deleted.append(str(state_dir))
        except OSError as e:
            result["warnings"] = [f"could not remove {state_dir}: {e}"]
    return result


_ACTIVE_SECONDS = 120


def _is_copilot_process(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    try:  # Linux: rule out an unrelated process that reused a stale lock's pid
        return b"copilot" in Path(f"/proc/{pid}/cmdline").read_bytes().lower()
    except FileNotFoundError:
        return not Path("/proc/self").exists()  # no /proc: can't tell, assume it is
    except OSError:
        return True


def _busy_reason(home: Path, session_id: str, updated: float = 0.0) -> str:
    """Why the session must not be deleted right now ("" if it may be).

    updated: the session's last activity as the store records it (sessions.
    updated_at, or the events.jsonl mtime), so DB-only sessions count too."""
    d = home / "session-state" / session_id
    if d.is_dir():
        for lock in d.glob("inuse.*.lock"):
            try:
                pid = int(lock.name.split(".")[1])
            except (IndexError, ValueError):
                continue
            if _is_copilot_process(pid):
                return f"it is open in a running Copilot CLI (pid {pid}); close it first"
    stamps = [updated] + [p.stat().st_mtime for p in (d, d / "events.jsonl") if p.exists()]
    if time.time() - max(stamps) < _ACTIVE_SECONDS:
        return ("it was active in the last two minutes; Copilot may still be running it "
                "(perhaps on another node), so try again once it is closed")
    return ""


class CopilotAdapter(AgentAdapter):
    capabilities = Capabilities(
        agent_id="copilot", label="Copilot", doc_filename="AGENTS.md",
        has_memory=False, has_tasks=False, can_edit_doc=True,
        can_delete=True, can_export=True,
        delete_note=("The Copilot CLI has no delete command, so cc_mgr edits its store: a soft "
                     "delete first saves the session's database rows (db_rows.json) and its "
                     "session-state folder to ~/.copilot/.cc_mgr_trash/. Sessions open in a "
                     "running Copilot, or active in the last two minutes, are refused."),
    )

    def _home(self) -> Path:
        return copilot_home()

    def has_data(self) -> bool:
        home = self._home()
        state, history = home / "session-state", self._history_root()
        return (_store_db(home) is not None
                or (state.is_dir() and any(state.glob("*/events.jsonl")))
                or (history.is_dir() and any(history.glob("*/*.jsonl"))))

    def _history_root(self) -> Path:
        return self._home() / "history"

    def _store_sessions(self, project: str) -> list[dict[str, Any]]:
        sessions = list(_all_sessions(self._home()).values())
        names = project_names({s["cwd"] for s in sessions})
        return [s for s in sessions if names[s["cwd"]] == project]

    def list_projects(self) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for s in _all_sessions(self._home()).values():
            groups[s["cwd"]].append(s)
        names = project_names(groups)
        out = []
        for cwd, sessions in groups.items():
            out.append({
                "name": names[cwd],
                "display_path": cwd,
                "cwd": cwd,
                "session_count": len(sessions),
                "has_memory": False,
                "has_claude_md": (Path(cwd) / "AGENTS.md").is_file(),
                "open_tasks": 0, "total_tasks": 0,
                "git": read_git_info(cwd),
                "mtime": max(s["updated"] for s in sessions),
            })
        names = {p["name"] for p in out}
        out.extend(p for p in self._history_projects() if p["name"] not in names)
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def list_sessions(self, project: str) -> list[SessionSummary]:
        sessions = self._store_sessions(project)
        if not sessions:
            return self._history_sessions(project)
        home = self._home()
        out = []
        for s in sessions:
            turns, usage = _session_turns(home, s)
            prompts = [t["blocks"][0]["text"].strip()[:500] for t in turns
                       if t["kind"] == "user" and t["blocks"]]
            win = context_window(usage["model"], usage["peak"])
            out.append(SessionSummary(
                session_id=s["id"], project=project,
                file=str(s["events"] or _store_db(home) or ""),
                # change key for the search index (with mtime)
                size_bytes=s["events"].stat().st_size if s["events"] else len(turns),
                mtime=s["updated"],
                first_prompt=prompts[0] if prompts else "",
                last_prompt=prompts[-1] if prompts else "",
                message_count=len(turns),
                user_turns=sum(1 for t in turns if t["kind"] == "user"),
                assistant_turns=sum(1 for t in turns if t["role"] == "assistant"),
                context_tokens=usage["context"],
                context_limit=win["limit"], context_limit_known=win["known"],
                total_output_tokens=usage["output"], model=usage["model"],
                git_branch=s["branch"], cwd=s["cwd"], agent="copilot", title=s["title"],
            ))
        out.sort(key=lambda x: x.mtime, reverse=True)
        return out

    def get_conversation(self, project, session_id, offset=0, limit=None):
        info = next((s for s in self._store_sessions(project) if s["id"] == session_id), None)
        if info is None:
            return self._history_conversation(project, session_id, offset, limit)
        turns, _ = _session_turns(self._home(), info)
        total = len(turns)
        end = total if limit is None else min(total, offset + limit)
        return {"total": total, "offset": offset, "limit": limit, "turns": turns[offset:end]}

    def iter_turns(self, project, session_id) -> Iterator[dict[str, Any]]:
        data = self.get_conversation(project, session_id, offset=0, limit=None)
        for seq, turn in enumerate(data["turns"]):
            yield {"seq": seq, "role": turn["role"], "kind": turn["kind"],
                   "timestamp": turn.get("timestamp", ""), "blocks": turn["blocks"]}

    def _project_cwd(self, project: str) -> str:
        sessions = self._store_sessions(project)
        if sessions:
            return sessions[0]["cwd"]
        d = self._history_root() / project
        if not d.is_dir():
            return ""
        files = sorted(d.glob("*.jsonl"), key=lambda s: s.stat().st_mtime, reverse=True)
        return _peek_cwd(files[0]) if files else ""

    def get_doc(self, project):
        return read_doc_file(self._project_cwd(project), "AGENTS.md")

    def save_doc(self, project, content):
        return save_doc_file(self._project_cwd(project), "AGENTS.md", content)

    def _session_title(self, project: str, session_id: str) -> str:
        info = next((s for s in self._store_sessions(project) if s["id"] == session_id), None)
        return (info or {}).get("title", "")

    def delete_session(self, project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
        if not is_safe_id(session_id):
            raise ValueError("invalid session id")
        home = self._home()
        info = next((s for s in self._store_sessions(project) if s["id"] == session_id), None)
        if info is None:
            return self._delete_history_session(project, session_id, hard)
        busy = _busy_reason(home, session_id, info.get("updated") or 0.0)
        if busy:
            raise RuntimeError(f"not deleting Copilot session {session_id}: {busy}")
        db = _store_db(home)
        if db is not None and not resolves_inside(db, home):
            raise ValueError("session-store.db resolves outside the Copilot home; refusing to modify it")
        return _delete_store_session(home, db, safe_join(home / "session-state", session_id),
                                     session_id, hard)

    def _delete_history_session(self, project: str, session_id: str, hard: bool) -> dict[str, Any]:
        """A session in the legacy history/<project>/ layout: just its file."""
        if not is_safe_name(project):
            raise ValueError("invalid project name")
        path = safe_join(self._history_root(), project, f"{session_id}.jsonl")
        if not path.is_file():
            raise FileNotFoundError(f"session {session_id} not found in {project}")
        if hard:
            path.unlink()
            return {"deleted": [str(path)], "trash": None}
        trash = new_trash_dir(self._home(), session_id)
        shutil.move(str(path), str(trash / path.name))
        return {"deleted": [str(path)], "trash": str(trash)}

    # ---- legacy history/<mangled-cwd>/*.jsonl (Claude-like records) ----

    def _history_projects(self) -> list[dict[str, Any]]:
        root = self._history_root()
        if not root.is_dir():
            return []
        out = []
        for d in root.iterdir():
            if not d.is_dir():
                continue
            sessions = list(d.glob("*.jsonl"))
            if not sessions:
                continue
            cwd = _peek_cwd(sorted(sessions, key=lambda s: s.stat().st_mtime, reverse=True)[0])
            out.append({
                "name": d.name,
                "display_path": cwd or d.name,
                "cwd": cwd,
                "session_count": len(sessions),
                "has_memory": False,
                "has_claude_md": bool(cwd and (Path(cwd) / "AGENTS.md").is_file()),
                "open_tasks": 0, "total_tasks": 0,
                "git": read_git_info(cwd),
                "mtime": max(s.stat().st_mtime for s in sessions),
            })
        return out

    def _history_sessions(self, project: str) -> list[SessionSummary]:
        d = self._history_root() / project
        if not d.is_dir():
            return []
        out = []
        for s in d.glob("*.jsonl"):
            st = s.stat()
            user_turns = asst_turns = msg_count = 0
            first = last = cwd = branch = ""
            for rec in iter_jsonl(s):
                rtype = rec.get("type")
                if rec.get("cwd"):
                    cwd = rec["cwd"]
                if rec.get("gitBranch"):
                    branch = rec["gitBranch"]
                if rtype == "user":
                    text = block_text(rec.get("message", {}).get("content"))
                    if text and not text.startswith("[tool_result"):
                        if not first:
                            first = text.strip()[:500]
                        last = text.strip()[:500]
                        user_turns += 1
                    msg_count += 1
                elif rtype == "assistant":
                    asst_turns += 1
                    msg_count += 1
            win = context_window("", 0)
            out.append(SessionSummary(
                session_id=s.stem, project=project, file=str(s),
                size_bytes=st.st_size, mtime=st.st_mtime,
                first_prompt=first, last_prompt=last, message_count=msg_count,
                user_turns=user_turns, assistant_turns=asst_turns,
                cwd=cwd, git_branch=branch, agent="copilot",
                context_limit=win["limit"], context_limit_known=win["known"],
            ))
        out.sort(key=lambda x: x.mtime, reverse=True)
        return out

    def _history_conversation(self, project, session_id, offset, limit):
        path = self._history_root() / project / f"{session_id}.jsonl"
        if not path.is_file():
            return {"total": 0, "offset": offset, "limit": limit, "turns": []}
        turns = []
        for rec in iter_jsonl(path):
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            msg = rec.get("message", {})
            blocks = structured_blocks(msg.get("content"))
            is_tool_only = (rtype == "user" and blocks
                            and all(b["type"] == "tool_result" for b in blocks))
            turns.append(_turn(rec.get("uuid"), rtype, "tool" if is_tool_only else rtype,
                               rec.get("timestamp"), blocks, msg.get("model", ""),
                               (msg.get("usage") or {}).get("output_tokens", 0)))
        total = len(turns)
        end = total if limit is None else min(total, offset + limit)
        return {"total": total, "offset": offset, "limit": limit, "turns": turns[offset:end]}
