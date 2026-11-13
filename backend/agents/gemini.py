"""GeminiAdapter — ~/.gemini/tmp/<project>/chats/session-*.jsonl.

Real on-disk format (verified against live data, NOT the original spec guess):

  line 0: session meta {sessionId, projectHash, startTime, lastUpdated, kind}
  line 1: ONE {"$set": {"messages": [<session_context prelude>]}} — a single
          synthetic user turn; never the real conversation.
  later:  a mix of {"$set": {"lastUpdated": ...}} bookkeeping lines AND the real
          conversation as BARE TOP-LEVEL turn records appended one per line:
            {id, timestamp, type, content, thoughts?, tokens?, model?, toolCalls?}

Turn records: `type` is "user" or "gemini" (model). `content` is polymorphic —
a plain string (gemini answers), [{"text": ...}] (user prompts), or
[{"functionResponse": {...}}] (tool results, themselves type "user"). gemini
turns may also carry `toolCalls` and `tokens` ({input,output,cached,total}).

We surface bare turn records as the conversation; the line-1 `$set` prelude is
parsed but excluded from display (it's the synthetic context block). type
"gemini" normalizes to "assistant".
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities
from .common import (
    SessionSummary, context_window, is_safe_id, is_safe_name, iter_jsonl,
    new_trash_dir, read_doc_file, read_git_info, safe_join,
)


def gemini_home() -> Path:
    env = os.environ.get("GEMINI_HOME")
    return Path(env).expanduser() if env else Path.home() / ".gemini"


def _project_cwd(home: Path, project: str) -> str:
    """True cwd from tmp/<project>/.project_root, falling back to history/."""
    for base in (home / "tmp" / project, home / "history" / project):
        pr = base / ".project_root"
        if pr.is_file():
            txt = pr.read_text(encoding="utf-8", errors="replace").strip()
            if txt:
                return txt
    return ""


def _is_turn(rec: dict) -> bool:
    """A bare top-level conversation turn (not a $set/meta line)."""
    return (isinstance(rec, dict) and "$set" not in rec
            and "id" in rec and "type" in rec and "content" in rec)


def _read_turns(path: Path) -> list[dict]:
    """Real conversation turns in file order.

    Reads bare top-level turn records. Also pulls the line-1 $set.messages
    prelude turn(s) and prepends them, tagged so they can be skipped from
    display — they carry the synthetic <session_context> block.
    """
    prelude: list[dict] = []
    turns: list[dict] = []
    for rec in iter_jsonl(path):
        if not isinstance(rec, dict):
            continue
        if "$set" in rec and isinstance(rec["$set"], dict):
            msgs = rec["$set"].get("messages")
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, dict):
                        m = dict(m)
                        m["_prelude"] = True
                        prelude.append(m)
            continue
        if _is_turn(rec):
            turns.append(rec)
    return prelude + turns


def _norm_role(mtype: str) -> str:
    return "user" if mtype == "user" else "assistant"


def _turn_blocks(turn: dict) -> list[dict]:
    """Normalize a Gemini turn's polymorphic content into typed blocks.

    Handles: plain string, [{"text": ...}], [{"functionResponse": {...}}], plus
    a gemini turn's `toolCalls` (surfaced as tool_use blocks).
    """
    out: list[dict] = []
    content = turn.get("content")
    if isinstance(content, str):
        if content:
            out.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("text"):
                out.append({"type": "text", "text": part["text"]})
            elif "functionResponse" in part:
                fr = part["functionResponse"] or {}
                resp = fr.get("response", {})
                txt = resp.get("output") if isinstance(resp, dict) else None
                out.append({"type": "tool_result",
                            "text": txt if isinstance(txt, str) else _safe_str(fr)})
    for call in turn.get("toolCalls", []) or []:
        if isinstance(call, dict):
            out.append({"type": "tool_use", "name": call.get("name", ""),
                        "input": call.get("args", {})})
    return out


def _safe_str(obj: Any) -> str:
    try:
        import json as _json
        return _json.dumps(obj, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return str(obj)[:2000]


def _turn_text_preview(turn: dict) -> str:
    """First displayable text of a turn, for session summaries."""
    for b in _turn_blocks(turn):
        if b["type"] == "text" and b.get("text"):
            return b["text"]
    return ""


def _has_turns(path: Path) -> bool:
    """False for a session that only holds the startup <session_context> prelude."""
    return any(not t.get("_prelude") for t in _read_turns(path))


class GeminiAdapter(AgentAdapter):
    # Gemini CLI is retired; Antigravity CLI (agy) replaces it. Its history stays
    # browsable, exportable and deletable; GEMINI.md is shared with agy, which
    # edits it. There is no CLI left to go through, so deletes move the files.
    capabilities = Capabilities(
        agent_id="gemini", label="Gemini CLI (legacy)", doc_filename="GEMINI.md",
        has_memory=False, has_tasks=False, can_edit_doc=False,
        can_delete=True, can_export=True,
        delete_note=("Gemini CLI is retired, so cc_mgr moves the files itself: a soft delete "
                     "moves the chat file to ~/.gemini/.cc_mgr_trash/ (deleting a project "
                     "moves its whole tmp/<project>/ folder there)."),
    )

    def _home(self) -> Path:
        return gemini_home()

    def has_data(self) -> bool:
        root = self._home() / "tmp"
        return root.is_dir() and any(root.glob("*/chats/session-*.jsonl"))

    def list_projects(self) -> list[dict[str, Any]]:
        home = self._home()
        root = home / "tmp"
        if not root.is_dir():
            return []
        out = []
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            chats = d / "chats"
            files = list(chats.glob("session-*.jsonl")) if chats.is_dir() else []
            if not files:
                continue
            mtimes = [s.stat().st_mtime for s in files]
            cwd = _project_cwd(home, d.name)
            out.append({
                "name": d.name,
                "display_path": cwd or d.name,
                "cwd": cwd,
                # what the session list shows (prelude-only files are hidden there)
                "session_count": sum(1 for s in files if _has_turns(s)),
                "has_memory": False,
                "has_claude_md": bool(cwd and (Path(cwd) / "GEMINI.md").is_file()),
                "open_tasks": 0, "total_tasks": 0,
                "git": read_git_info(cwd),
                "mtime": max(mtimes),
            })
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def _chats_dir(self, project: str) -> Path:
        return self._home() / "tmp" / project / "chats"

    def _session_path(self, project: str, session_id: str) -> Path | None:
        chats = self._chats_dir(project)
        if not chats.is_dir():
            return None
        for s in chats.glob("session-*.jsonl"):
            if s.stem == session_id:
                return s
        return None

    def list_sessions(self, project: str) -> list[SessionSummary]:
        chats = self._chats_dir(project)
        if not chats.is_dir():
            return []
        cwd = _project_cwd(self._home(), project)
        git = read_git_info(cwd)
        branch = git["branch"] if git else ""
        out = []
        for s in chats.glob("session-*.jsonl"):
            st = s.stat()
            turns = [t for t in _read_turns(s) if not t.get("_prelude")]
            if not turns:
                continue  # started and quit: only the <session_context> prelude
            user_turns = sum(1 for t in turns if t.get("type") == "user")
            asst_turns = len(turns) - user_turns
            # first/last *displayable* user prompt (skip tool-result-only turns)
            def _user_text(t):
                if t.get("type") != "user":
                    return ""
                txt = _turn_text_preview(t)
                return txt[:500] if txt else ""
            first = ""
            last = ""
            model = ""
            total_output = 0
            last_total_ctx = 0
            for t in turns:
                ut = _user_text(t)
                if ut:
                    if not first:
                        first = ut
                    last = ut
                if t.get("model"):
                    model = t["model"]
                tok = t.get("tokens") or {}
                total_output += tok.get("output", 0) or 0
                if tok.get("total"):
                    last_total_ctx = tok["total"]
            summ = SessionSummary(
                session_id=s.stem, project=project, file=str(s),
                size_bytes=st.st_size, mtime=st.st_mtime,
                first_prompt=first, last_prompt=last,
                message_count=len(turns), user_turns=user_turns,
                assistant_turns=asst_turns, cwd=cwd, git_branch=branch,
                total_output_tokens=total_output, context_tokens=last_total_ctx,
                agent="gemini", model=model,
            )
            win = context_window(model, last_total_ctx)
            summ.context_limit = win["limit"]
            summ.context_limit_known = win["known"]
            out.append(summ)
        out.sort(key=lambda x: x.mtime, reverse=True)
        return out

    def get_conversation(self, project, session_id, offset=0, limit=None):
        path = self._session_path(project, session_id)
        if not path or not path.is_file():
            return {"total": 0, "offset": offset, "limit": limit, "turns": []}
        records = [t for t in _read_turns(path) if not t.get("_prelude")]
        turns = []
        for m in records:
            blocks = _turn_blocks(m)
            role = _norm_role(m.get("type", ""))
            is_tool_only = (role == "user" and blocks
                            and all(b["type"] == "tool_result" for b in blocks))
            tok = m.get("tokens") or {}
            turns.append({
                "uuid": m.get("id"),
                "role": role,
                "kind": "tool" if is_tool_only else role,
                "timestamp": m.get("timestamp"),
                "model": m.get("model", ""),
                "blocks": blocks,
                "attribution_skill": None,
                "attribution_plugin": None,
                "output_tokens": tok.get("output", 0) or 0,
            })
        total = len(turns)
        end = total if limit is None else min(total, offset + limit)
        return {"total": total, "offset": offset, "limit": limit,
                "turns": turns[offset:end]}

    def iter_turns(self, project, session_id) -> Iterator[dict[str, Any]]:
        data = self.get_conversation(project, session_id, offset=0, limit=None)
        for seq, turn in enumerate(data["turns"]):
            yield {"seq": seq, "role": turn["role"], "kind": turn["kind"],
                   "timestamp": turn.get("timestamp", ""), "blocks": turn["blocks"]}

    def get_doc(self, project):
        return read_doc_file(_project_cwd(self._home(), project), "GEMINI.md")

    # ---- delete: no CLI to go through, so move the files ----

    def _session_file(self, project: str, session_id: str) -> Path:
        """A session's chat file; refuses names that could escape tmp/."""
        if not is_safe_name(project) or not is_safe_id(session_id):
            raise ValueError("invalid project or session id")
        path = safe_join(self._home() / "tmp", project, "chats", f"{session_id}.jsonl")
        if not path.is_file():
            raise FileNotFoundError(f"session {session_id} not found in {project}")
        return path

    def delete_session(self, project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
        path = self._session_file(project, session_id)
        if hard:
            path.unlink()
            return {"deleted": [str(path)], "trash": None}
        trash = new_trash_dir(self._home(), session_id)
        dest = trash / "tmp" / project / "chats"
        dest.mkdir(parents=True)
        shutil.move(str(path), str(dest / path.name))
        return {"deleted": [str(path)], "trash": str(trash)}

    def project_delete_preview(self, project: str) -> dict[str, Any]:
        preview = super().project_delete_preview(project)
        chats = self._chats_dir(project)
        files = list(chats.glob("session-*.jsonl")) if chats.is_dir() else []
        preview["hidden"] = sum(1 for f in files if not _has_turns(f))
        return preview

    def delete_project(self, project: str, hard: bool = False, include_memory: bool = False,
                       export_first: bool = False) -> dict[str, Any]:
        """Remove the project's whole tmp/<project>/ folder (chats, logs, the
        prompt history in logs.json) and history/<project>/."""
        if not is_safe_name(project):
            raise ValueError("invalid project name")
        home = self._home()
        tmp_dir = safe_join(home / "tmp", project)
        if not tmp_dir.is_dir():
            raise FileNotFoundError(f"project {project} not found")
        dirs = [d for d in (tmp_dir, safe_join(home / "history", project)) if d.is_dir()]
        if any(d.is_symlink() for d in dirs):
            raise ValueError("the project folder is a symlink; refusing to delete through it")
        files = sorted((tmp_dir / "chats").glob("session-*.jsonl"))
        exported = self._export_all(project) if export_first else []
        trash = None
        if hard:
            for d in dirs:
                shutil.rmtree(d)
        else:
            trash = new_trash_dir(home, project)
            for d in dirs:
                (trash / d.parent.name).mkdir()
                shutil.move(str(d), str(trash / d.parent.name / d.name))
        return {"sessions": [f.stem for f in files], "deleted": [str(d) for d in dirs],
                "trash": [str(trash)] if trash else [], "exported": exported, "errors": {},
                "warnings": [], "project_removed": True}
