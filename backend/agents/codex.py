"""CodexAdapter — ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

Turn-per-line JSONL (like Claude). Sessions are grouped into projects by their
`cwd` field (mangled to a folder-like name). When the sessions dir is absent
(as on machines that haven't run codex), every reader returns empty.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities
from .common import (
    SessionSummary, block_text, context_limit_for, iter_jsonl, read_doc_file,
    read_git_info, save_doc_file, structured_blocks,
)


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _mangle(cwd: str) -> str:
    return cwd.replace("/", "-").replace("\\", "-").replace(":", "-")


def _peek_cwd(path: Path) -> str:
    for rec in iter_jsonl(path):
        if rec.get("cwd"):
            return rec["cwd"]
    return ""


def _all_sessions(home: Path) -> list[Path]:
    root = home / "sessions"
    if not root.is_dir():
        return []
    return sorted(root.rglob("rollout-*.jsonl"))


class CodexAdapter(AgentAdapter):
    capabilities = Capabilities(
        agent_id="codex", label="Codex", doc_filename="AGENTS.md",
        has_memory=False, has_tasks=False, can_edit_doc=True,
        can_delete=False, can_export=False,
    )

    def _home(self) -> Path:
        return codex_home()

    def _project_map(self) -> dict[str, list[Path]]:
        """mangled-cwd -> [session paths]."""
        out: dict[str, list[Path]] = {}
        for s in _all_sessions(self._home()):
            cwd = _peek_cwd(s)
            key = _mangle(cwd) if cwd else "unknown"
            out.setdefault(key, []).append(s)
        return out

    def list_projects(self) -> list[dict[str, Any]]:
        out = []
        for name, sessions in self._project_map().items():
            cwd = _peek_cwd(sessions[0]) if sessions else ""
            mtimes = [s.stat().st_mtime for s in sessions]
            out.append({
                "name": name,
                "display_path": cwd or name,
                "cwd": cwd,
                "session_count": len(sessions),
                "has_memory": False,
                "has_claude_md": bool(cwd and (Path(cwd) / "AGENTS.md").is_file()),
                "open_tasks": 0, "total_tasks": 0,
                "git": read_git_info(cwd),
                "mtime": max(mtimes) if mtimes else 0.0,
            })
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def _sessions_for(self, project: str) -> list[Path]:
        return self._project_map().get(project, [])

    def _session_path(self, project: str, session_id: str) -> Path | None:
        for s in self._sessions_for(project):
            if s.stem == session_id:
                return s
        return None

    def list_sessions(self, project: str) -> list[SessionSummary]:
        out = []
        for s in self._sessions_for(project):
            st = s.stat()
            user_turns = asst_turns = msg_count = 0
            first = last = ""
            cwd = branch = ""
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
            summ = SessionSummary(
                session_id=s.stem, project=project, file=str(s),
                size_bytes=st.st_size, mtime=st.st_mtime,
                first_prompt=first, last_prompt=last, message_count=msg_count,
                user_turns=user_turns, assistant_turns=asst_turns,
                cwd=cwd, git_branch=branch, agent="codex",
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
        turns = []
        for rec in iter_jsonl(path):
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            msg = rec.get("message", {})
            blocks = structured_blocks(msg.get("content"))
            is_tool_only = (rtype == "user" and blocks
                            and all(b["type"] == "tool_result" for b in blocks))
            turns.append({
                "uuid": rec.get("uuid"),
                "role": rtype,
                "kind": "tool" if is_tool_only else rtype,
                "timestamp": rec.get("timestamp"),
                "model": msg.get("model", ""),
                "blocks": blocks,
                "attribution_skill": None,
                "attribution_plugin": None,
                "output_tokens": (msg.get("usage") or {}).get("output_tokens", 0),
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

    def _project_cwd(self, project: str) -> str:
        sessions = self._sessions_for(project)
        return _peek_cwd(sessions[0]) if sessions else ""

    def get_doc(self, project):
        return read_doc_file(self._project_cwd(project), "AGENTS.md")

    def save_doc(self, project, content):
        return save_doc_file(self._project_cwd(project), "AGENTS.md", content)
