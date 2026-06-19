"""GeminiAdapter — ~/.gemini/tmp/<project>/chats/session-*.jsonl.

Transcript is event-sourced: line 1 is session meta; subsequent lines are
{"$set": {"messages": [...]}}. We fold to the LAST messages[] array. Each
message is {type, content:[{text}]} where type is 'user' for the human and
something else (e.g. 'gemini') for the model — normalized to user/assistant.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities
from .common import (
    SessionSummary, context_limit_for, iter_jsonl, read_doc_file, read_git_info,
    save_doc_file,
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


def _fold_messages(path: Path) -> tuple[dict, list[dict]]:
    """Return (session_meta, latest messages[]) from an event-sourced file."""
    meta: dict[str, Any] = {}
    messages: list[dict] = []
    for rec in iter_jsonl(path):
        if "$set" in rec and isinstance(rec["$set"], dict):
            m = rec["$set"].get("messages")
            if isinstance(m, list):
                messages = m
        elif "sessionId" in rec and not meta:
            meta = rec
        elif "$append" in rec and isinstance(rec["$append"], dict):
            m = rec["$append"].get("messages")
            if isinstance(m, list):
                messages.extend(m)
    return meta, messages


def _norm_role(mtype: str) -> str:
    return "user" if mtype == "user" else "assistant"


def _msg_blocks(msg: dict) -> list[dict]:
    out = []
    for part in msg.get("content", []) or []:
        if isinstance(part, dict) and part.get("text"):
            out.append({"type": "text", "text": part["text"]})
    return out


class GeminiAdapter(AgentAdapter):
    capabilities = Capabilities(
        agent_id="gemini", label="Gemini", doc_filename="GEMINI.md",
        has_memory=False, has_tasks=False, can_edit_doc=True,
        can_delete=False, can_export=False,
    )

    def _home(self) -> Path:
        return gemini_home()

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
            sessions = list(chats.glob("session-*.jsonl")) if chats.is_dir() else []
            if not sessions:
                continue
            mtimes = [s.stat().st_mtime for s in sessions]
            cwd = _project_cwd(home, d.name)
            out.append({
                "name": d.name,
                "display_path": cwd or d.name,
                "cwd": cwd,
                "session_count": len(sessions),
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
            if s.stem == session_id or session_id in s.stem:
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
            meta, messages = _fold_messages(s)
            user_turns = sum(1 for m in messages if m.get("type") == "user")
            asst_turns = len(messages) - user_turns
            first = next((m for m in messages if m.get("type") == "user"), None)
            last = next((m for m in reversed(messages) if m.get("type") == "user"), None)
            def _txt(m):
                if not m:
                    return ""
                b = _msg_blocks(m)
                return b[0]["text"][:500] if b else ""
            summ = SessionSummary(
                session_id=s.stem, project=project, file=str(s),
                size_bytes=st.st_size, mtime=st.st_mtime,
                first_prompt=_txt(first), last_prompt=_txt(last),
                message_count=len(messages), user_turns=user_turns,
                assistant_turns=asst_turns, cwd=cwd, git_branch=branch,
                agent="gemini",
            )
            win = context_limit_for(0)
            summ.context_limit = win["limit"]
            summ.context_limit_known = win["known"]
            out.append(summ)
        out.sort(key=lambda x: x.mtime, reverse=True)
        return out

    def get_conversation(self, project, session_id, offset=0, limit=None):
        path = self._session_path(project, session_id)
        if not path or not path.is_file():
            return {"total": 0, "offset": offset, "limit": limit, "turns": []}
        _, messages = _fold_messages(path)
        turns = []
        for m in messages:
            blocks = _msg_blocks(m)
            role = _norm_role(m.get("type", ""))
            turns.append({
                "uuid": m.get("id"),
                "role": role,
                "kind": role,
                "timestamp": m.get("timestamp"),
                "model": "",
                "blocks": blocks,
                "attribution_skill": None,
                "attribution_plugin": None,
                "output_tokens": 0,
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

    def save_doc(self, project, content):
        return save_doc_file(_project_cwd(self._home(), project), "GEMINI.md", content)
