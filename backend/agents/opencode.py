"""OpencodeAdapter — OpenCode CLI (opencode.ai) sessions and projects.

Real on-disk format for OpenCode:
  - Database: opencode.db (SQLite with Drizzle ORM)
    * Location priority:
      1. OPENCODE_DB environment variable
      2. $XDG_DATA_HOME/opencode/opencode.db
      3. $OPENCODE_BASE/share/opencode/opencode.db
      4. ~/.local/share/opencode/opencode.db
      5. `opencode db path` CLI lookup
  - Tables:
    * `session`: id, project_id, directory (cwd), title, model, tokens_*, cost, time_created, time_updated, time_archived
    * `message`: id, session_id, time_created, time_updated, data (JSON: role, model, tokens, etc.)
    * `part`: id, message_id, session_id, time_created, data (JSON: type ∈ text, reasoning, tool, file, patch, step-*)
    * `todo`: session_id, content, status (pending/in_progress/completed), priority, position
    * `project`: id, worktree, name, time_created, time_updated
  - Documentation:
    * Project instruction doc: AGENTS.md in project working directory
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities, UnsupportedCapability
from .common import (
    SessionSummary, context_limit_for, read_doc_file,
    read_git_info, save_doc_file, block_text,
)


def opencode_db_path() -> Path | None:
    """Locate OpenCode SQLite database file."""
    # 1. Environment variable OPENCODE_DB
    env_db = os.environ.get("OPENCODE_DB")
    if env_db:
        p = Path(env_db).expanduser()
        if p.is_file():
            return p

    # 2. XDG_DATA_HOME
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        p = Path(xdg_data).expanduser() / "opencode" / "opencode.db"
        if p.is_file():
            return p

    # 3. OPENCODE_BASE
    base = os.environ.get("OPENCODE_BASE")
    if base:
        p = Path(base).expanduser() / "share" / "opencode" / "opencode.db"
        if p.is_file():
            return p

    # 4. Standard default ~/.local/share/opencode/opencode.db
    default_p = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if default_p.is_file():
        return default_p

    # 5. CLI fallback
    try:
        proc = subprocess.run(["opencode", "db", "path"], capture_output=True, text=True, timeout=2)
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if out:
                p = Path(out).expanduser()
                if p.is_file():
                    return p
    except Exception:
        pass

    return None


def _connect_ro(db_file: Path) -> sqlite3.Connection:
    """Connect to SQLite database in immutable read-only mode safe for Lustre/NFS."""
    uri = f"file:{db_file.resolve()}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_rw(db_file: Path) -> sqlite3.Connection:
    """Connect to SQLite database in read-write mode."""
    conn = sqlite3.connect(db_file, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _mangle(cwd: str) -> str:
    return cwd.replace("/", "-").replace("\\", "-").replace(":", "-")


def _unmangle_path(name: str) -> str:
    if "--" in name:
        name = name.replace("--", ":/", 1)
    return name.replace("-", "/")


def _format_model_name(model_raw: Any) -> str:
    if not model_raw:
        return ""
    if isinstance(model_raw, str):
        try:
            m_dict = json.loads(model_raw)
            if isinstance(m_dict, dict):
                return m_dict.get("id") or m_dict.get("modelID") or model_raw
        except Exception:
            return model_raw
    elif isinstance(model_raw, dict):
        return model_raw.get("id") or model_raw.get("modelID") or ""
    return str(model_raw)


def _parse_session_turns(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """Parse messages and parts for a session into normalized turns."""
    msgs = conn.execute(
        "SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created ASC, id ASC",
        (session_id,),
    ).fetchall()

    parts_rows = conn.execute(
        "SELECT id, message_id, time_created, data FROM part WHERE session_id = ? ORDER BY time_created ASC, id ASC",
        (session_id,),
    ).fetchall()

    parts_by_msg: dict[str, list[dict[str, Any]]] = {}
    for pr in parts_rows:
        try:
            p_data = json.loads(pr["data"])
            parts_by_msg.setdefault(pr["message_id"], []).append(p_data)
        except Exception:
            continue

    turns: list[dict[str, Any]] = []

    for m in msgs:
        mid = m["id"]
        try:
            m_data = json.loads(m["data"])
        except Exception:
            m_data = {}

        role = m_data.get("role") or "user"
        norm_role = "user" if role == "user" else "assistant"

        ts_ms = m["time_created"] or (m_data.get("time") or {}).get("created")
        ts_str = ""
        if ts_ms:
            try:
                ts_str = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc).isoformat()
            except Exception:
                pass

        tokens_info = m_data.get("tokens") or {}
        out_tokens = tokens_info.get("output", 0) if isinstance(tokens_info, dict) else 0
        model_name = _format_model_name(m_data.get("modelID") or m_data.get("model"))

        blocks: list[dict[str, Any]] = []
        msg_parts = parts_by_msg.get(mid, [])
        has_text_or_reasoning = False

        for p in msg_parts:
            ptype = p.get("type")
            if ptype == "text":
                txt = p.get("text", "")
                if txt:
                    blocks.append({"type": "text", "text": txt})
                    has_text_or_reasoning = True
            elif ptype == "reasoning":
                reasoning_txt = p.get("text", "")
                if reasoning_txt:
                    blocks.append({"type": "thinking", "text": reasoning_txt})
                    has_text_or_reasoning = True
            elif ptype == "tool":
                tool_name = p.get("tool", "")
                state = p.get("state") or {}
                tool_inp = state.get("input") or {}
                blocks.append({"type": "tool_use", "name": tool_name, "input": tool_inp})

                tool_out = state.get("output")
                if tool_out is not None:
                    out_str = tool_out if isinstance(tool_out, str) else json.dumps(tool_out, ensure_ascii=False)
                    blocks.append({"type": "tool_result", "text": out_str})
            elif ptype == "file":
                fname = p.get("filename") or ""
                url = p.get("url") or ""
                blocks.append({"type": "text", "text": f"[Attachment: {fname or url}]"})
            elif ptype == "patch":
                files = p.get("files") or []
                if files:
                    blocks.append({"type": "text", "text": f"[Modified files: {', '.join(files)}]"})
            elif ptype == "step-finish":
                sf_tokens = p.get("tokens") or {}
                if not out_tokens and isinstance(sf_tokens, dict):
                    out_tokens = sf_tokens.get("output", 0)

        if not blocks:
            summary = m_data.get("summary")
            if summary and isinstance(summary, str):
                blocks.append({"type": "text", "text": summary})
            else:
                continue

        if norm_role == "assistant":
            kind = "assistant" if has_text_or_reasoning else "tool"
        else:
            kind = "user"

        turns.append({
            "uuid": mid,
            "role": norm_role,
            "kind": kind,
            "timestamp": ts_str,
            "model": model_name,
            "blocks": blocks,
            "attribution_skill": None,
            "attribution_plugin": None,
            "output_tokens": out_tokens,
        })

    return turns


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
    )

    def _db_path(self) -> Path | None:
        return opencode_db_path()

    def _project_cwd(self, project: str) -> str:
        """Resolve mangled or direct project identifier to working directory."""
        db_file = self._db_path()
        if not db_file:
            return _unmangle_path(project)

        conn = _connect_ro(db_file)
        try:
            dirs = conn.execute(
                "SELECT DISTINCT directory FROM session WHERE directory IS NOT NULL AND directory != ''"
            ).fetchall()
            for r in dirs:
                d = r["directory"]
                if _mangle(d) == project or d == project:
                    return d
        finally:
            conn.close()

        return _unmangle_path(project)

    def list_projects(self) -> list[dict[str, Any]]:
        db_file = self._db_path()
        if not db_file:
            return []

        conn = _connect_ro(db_file)
        out: list[dict[str, Any]] = []
        try:
            rows = conn.execute("""
                SELECT session.directory,
                       COUNT(DISTINCT session.id) as session_count,
                       MAX(session.time_updated) / 1000.0 as mtime,
                       COUNT(todo.position) as total_tasks,
                       SUM(CASE WHEN todo.status != 'completed' THEN 1 ELSE 0 END) as open_tasks
                FROM session
                LEFT JOIN todo ON session.id = todo.session_id
                WHERE session.directory IS NOT NULL AND session.directory != ''
                  AND (session.time_archived IS NULL OR session.time_archived = 0)
                GROUP BY session.directory
                ORDER BY mtime DESC
            """).fetchall()

            for r in rows:
                cwd = r["directory"]
                name = _mangle(cwd)
                has_doc = bool(cwd and (Path(cwd) / "AGENTS.md").is_file())
                out.append({
                    "name": name,
                    "display_path": cwd,
                    "cwd": cwd,
                    "session_count": r["session_count"] or 0,
                    "has_memory": False,
                    "has_claude_md": has_doc,
                    "open_tasks": r["open_tasks"] or 0,
                    "total_tasks": r["total_tasks"] or 0,
                    "git": read_git_info(cwd),
                    "mtime": r["mtime"] or 0.0,
                })
        finally:
            conn.close()

        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def list_sessions(self, project: str) -> list[SessionSummary]:
        db_file = self._db_path()
        if not db_file:
            return []

        cwd = self._project_cwd(project)
        conn = _connect_ro(db_file)
        summaries: list[SessionSummary] = []
        try:
            sessions = conn.execute("""
                SELECT s.*,
                       COUNT(t.position) as total_tasks,
                       SUM(CASE WHEN t.status != 'completed' THEN 1 ELSE 0 END) as open_tasks
                FROM session s
                LEFT JOIN todo t ON s.id = t.session_id
                WHERE s.directory = ?
                  AND (s.time_archived IS NULL OR s.time_archived = 0)
                GROUP BY s.id
                ORDER BY s.time_updated DESC
            """, (cwd,)).fetchall()

            if not sessions:
                return []

            session_ids = [s["id"] for s in sessions]
            placeholders = ",".join("?" for _ in session_ids)

            # Fetch user messages with text parts to deduce first/last prompt and turn counts
            user_texts = conn.execute(f"""
                SELECT m.session_id, m.id, m.time_created, p.data as part_data
                FROM message m
                JOIN part p ON m.id = p.message_id
                WHERE m.session_id IN ({placeholders})
                  AND json_extract(m.data, '$.role') = 'user'
                  AND json_extract(p.data, '$.type') = 'text'
                ORDER BY m.time_created ASC, m.id ASC
            """, session_ids).fetchall()

            # Group prompts by session
            session_prompts: dict[str, list[str]] = {}
            for row in user_texts:
                sid = row["session_id"]
                try:
                    p_info = json.loads(row["part_data"])
                    txt = p_info.get("text", "").strip()
                    if txt:
                        session_prompts.setdefault(sid, []).append(txt)
                except Exception:
                    continue

            # Fetch message counts grouped by session and role
            role_counts = conn.execute(f"""
                SELECT m.session_id,
                       json_extract(m.data, '$.role') as role,
                       COUNT(*) as cnt
                FROM message m
                WHERE m.session_id IN ({placeholders})
                GROUP BY m.session_id, role
            """, session_ids).fetchall()

            counts_by_session: dict[str, dict[str, int]] = {}
            for rc in role_counts:
                sid = rc["session_id"]
                role = rc["role"] or "user"
                counts_by_session.setdefault(sid, {})[role] = rc["cnt"]

            git_info = read_git_info(cwd)
            git_branch = (git_info or {}).get("branch") or ""

            for s in sessions:
                sid = s["id"]
                prompts = session_prompts.get(sid, [])
                first_prompt = prompts[0][:500] if prompts else ""
                last_prompt = prompts[-1][:500] if prompts else ""

                s_counts = counts_by_session.get(sid, {})
                user_turns = s_counts.get("user", 0)
                asst_turns = s_counts.get("assistant", 0)
                msg_count = user_turns + asst_turns

                mtime = (s["time_updated"] or s["time_created"] or 0) / 1000.0
                ctx_tokens = (s["tokens_input"] or 0) + (s["tokens_output"] or 0)
                model_str = _format_model_name(s["model"])

                win = context_limit_for(ctx_tokens)
                summ = SessionSummary(
                    session_id=sid,
                    project=project,
                    file=str(db_file),
                    size_bytes=ctx_tokens,
                    mtime=mtime,
                    first_prompt=first_prompt,
                    last_prompt=last_prompt,
                    message_count=msg_count,
                    user_turns=user_turns,
                    assistant_turns=asst_turns,
                    context_tokens=ctx_tokens,
                    context_limit=win["limit"],
                    context_limit_known=win["known"],
                    total_output_tokens=s["tokens_output"] or 0,
                    model=model_str,
                    git_branch=git_branch,
                    cwd=cwd,
                    has_memory=False,
                    open_tasks=s["open_tasks"] or 0,
                    total_tasks=s["total_tasks"] or 0,
                    agent="opencode",
                    title=s["title"] or sid,
                )
                summaries.append(summ)

        finally:
            conn.close()

        summaries.sort(key=lambda x: x.mtime, reverse=True)
        return summaries

    def get_conversation(self, project: str, session_id: str,
                         offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        db_file = self._db_path()
        if not db_file:
            return {"total": 0, "offset": offset, "limit": limit, "turns": []}

        conn = _connect_ro(db_file)
        try:
            turns = _parse_session_turns(conn, session_id)
        finally:
            conn.close()

        total = len(turns)
        end = total if limit is None else min(total, offset + limit)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "turns": turns[offset:end],
        }

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

    def get_tasks(self, session_id: str) -> list[dict[str, Any]]:
        db_file = self._db_path()
        if not db_file:
            return []

        conn = _connect_ro(db_file)
        try:
            rows = conn.execute(
                "SELECT session_id, content, status, priority, position FROM todo WHERE session_id = ? ORDER BY position ASC",
                (session_id,),
            ).fetchall()
            tasks: list[dict[str, Any]] = []
            for r in rows:
                tasks.append({
                    "id": str(r["position"]),
                    "subject": r["content"],
                    "status": r["status"] or "pending",
                    "priority": r["priority"] or "medium",
                    "position": r["position"],
                    "session_id": r["session_id"],
                    "owner": "",
                    "blocks": [],
                    "blockedBy": [],
                })
            return tasks
        finally:
            conn.close()

    def project_tasks(self, project: str) -> list[dict[str, Any]]:
        db_file = self._db_path()
        if not db_file:
            return []

        cwd = self._project_cwd(project)
        conn = _connect_ro(db_file)
        try:
            rows = conn.execute("""
                SELECT t.session_id, t.content, t.status, t.priority, t.position
                FROM todo t
                JOIN session s ON t.session_id = s.id
                WHERE s.directory = ?
                  AND (s.time_archived IS NULL OR s.time_archived = 0)
                ORDER BY s.time_updated DESC, t.position ASC
            """, (cwd,)).fetchall()
            tasks: list[dict[str, Any]] = []
            for r in rows:
                tasks.append({
                    "id": str(r["position"]),
                    "subject": r["content"],
                    "status": r["status"] or "pending",
                    "priority": r["priority"] or "medium",
                    "position": r["position"],
                    "session_id": r["session_id"],
                    "owner": "",
                    "blocks": [],
                    "blockedBy": [],
                })
            return tasks
        finally:
            conn.close()

    def update_task_status(self, session_id: str, task_id: str, status: str) -> dict[str, Any]:
        valid = {"pending", "in_progress", "completed", "deleted"}
        if status not in valid:
            raise ValueError(f"invalid status: {status}")

        db_file = self._db_path()
        if not db_file:
            raise FileNotFoundError("opencode database not found")

        conn = _connect_rw(db_file)
        try:
            try:
                pos = int(task_id)
                row = conn.execute(
                    "SELECT session_id, content, status, priority, position FROM todo WHERE session_id = ? AND position = ?",
                    (session_id, pos),
                ).fetchone()
            except ValueError:
                row = None

            if not row:
                raise FileNotFoundError(f"task {task_id} not found for session {session_id}")

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            conn.execute(
                "UPDATE todo SET status = ?, time_updated = ? WHERE session_id = ? AND position = ?",
                (status, now_ms, session_id, pos),
            )
            conn.commit()
            return {
                "id": str(pos),
                "subject": row["content"],
                "status": status,
                "priority": row["priority"],
                "position": pos,
                "session_id": session_id,
            }
        finally:
            conn.close()

    def export_session_markdown(self, project: str, session_id: str) -> str:
        db_file = self._db_path()
        if not db_file:
            raise FileNotFoundError("opencode database not found")

        conn = _connect_ro(db_file)
        try:
            s = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
            if not s:
                raise FileNotFoundError(f"session {session_id} not found")
            turns = _parse_session_turns(conn, session_id)
        finally:
            conn.close()

        cwd = s["directory"] or self._project_cwd(project)
        model = _format_model_name(s["model"])
        created_str = ""
        if s["time_created"]:
            try:
                created_str = datetime.fromtimestamp(s["time_created"] / 1000.0, timezone.utc).isoformat()
            except Exception:
                pass

        lines = [
            f"# Session: {s['title'] or session_id}",
            "",
            f"- **Agent:** OpenCode",
            f"- **Session ID:** `{session_id}`",
            f"- **Created:** {created_str}",
            f"- **Model:** {model or 'unknown'}",
            f"- **Directory:** `{cwd}`",
            f"- **Total Turns:** {len(turns)}",
            "",
            "---",
            "",
        ]

        for t in turns:
            role = t["role"].capitalize()
            ts = f" ({t['timestamp']})" if t.get("timestamp") else ""
            lines.append(f"### {role}{ts}")
            lines.append("")
            for b in t["blocks"]:
                bt = b.get("type")
                if bt == "text":
                    lines.append(b.get("text", ""))
                    lines.append("")
                elif bt == "thinking":
                    indented = b.get("text", "").replace("\n", "\n> ")
                    lines.append(f"> **Thinking:**\n> {indented}")
                    lines.append("")
                elif bt == "tool_use":
                    inp = json.dumps(b.get("input", {}), indent=2, ensure_ascii=False)
                    lines.append(f"**→ tool: {b.get('name', '')}**\n\n```json\n{inp}\n```")
                    lines.append("")
                elif bt == "tool_result":
                    txt = b.get("text", "")
                    lines.append(f"**tool result**\n\n```\n{txt}\n```")
                    lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    def export_session_to_file(self, project: str, session_id: str, out_dir: Path | None = None) -> Path:
        if out_dir is None:
            out_dir = Path.cwd() / "exports" / project
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{session_id}.{stamp}.md"
        out_path.write_text(self.export_session_markdown(project, session_id), encoding="utf-8")
        return out_path

    def delete_session(self, project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
        db_file = self._db_path()
        if not db_file:
            raise FileNotFoundError("opencode database not found")

        if hard:
            cli = shutil.which("opencode")
            if cli:
                try:
                    proc = subprocess.run([cli, "session", "delete", session_id], capture_output=True, text=True, timeout=10)
                    if proc.returncode == 0:
                        return {"deleted": [session_id], "trash": None}
                except Exception:
                    pass

            conn = _connect_rw(db_file)
            try:
                conn.execute("DELETE FROM part WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM todo WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
                conn.commit()
            finally:
                conn.close()
            return {"deleted": [session_id], "trash": None}
        else:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            conn = _connect_rw(db_file)
            try:
                conn.execute("UPDATE session SET time_archived = ? WHERE id = ?", (now_ms, session_id))
                conn.commit()
            finally:
                conn.close()
            return {"deleted": [session_id], "trash": f"archived_{now_ms}"}
