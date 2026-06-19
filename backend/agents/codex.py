"""CodexAdapter — $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl.

Real on-disk format (verified against live data, NOT the original spec guess):

Every line is an envelope {timestamp, type, payload}. `type` is one of:
  - session_meta : payload has {id, timestamp, cwd, originator, cli_version, ...}.
                   This is the ONLY reliable source of the session's cwd.
  - turn_context : bookkeeping.
  - event_msg    : UI/telemetry; payload.type includes "token_count" which carries
                   info.total_token_usage.{input,output,total}_tokens.
  - response_item: the actual conversation. payload.type is one of:
      * message          : {role: user|assistant|developer,
                            content:[{type: input_text|output_text, text}]}
      * function_call    : {name, arguments(JSON string), call_id}  -> tool call
      * function_call_output : {call_id, output(string)}            -> tool result

Sessions are grouped into projects by their cwd (mangled to a folder-like name).
When the sessions dir is absent (machines that never ran codex), readers return [].
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities
from .common import (
    SessionSummary, context_limit_for, iter_jsonl, read_doc_file,
    read_git_info, save_doc_file,
)


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _mangle(cwd: str) -> str:
    return cwd.replace("/", "-").replace("\\", "-").replace(":", "-")


def _session_cwd(path: Path) -> str:
    """cwd from the session_meta envelope."""
    for rec in iter_jsonl(path):
        if rec.get("type") == "session_meta":
            return (rec.get("payload") or {}).get("cwd", "") or ""
    return ""


def _all_sessions(home: Path) -> list[Path]:
    root = home / "sessions"
    if not root.is_dir():
        return []
    return sorted(root.rglob("rollout-*.jsonl"))


def _text_parts(content: Any) -> str:
    """Join input_text/output_text/text parts of a message payload."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for c in content:
        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
            out.append(c.get("text", ""))
    return "\n".join(p for p in out if p)


def _envelope_to_turn(rec: dict) -> dict | None:
    """Map one {type,payload} envelope to a normalized turn, or None to skip.

    Returns a dict {role, kind, timestamp, blocks, output_tokens, model} or None.
    """
    if rec.get("type") != "response_item":
        return None
    p = rec.get("payload") or {}
    ptype = p.get("type")
    ts = rec.get("timestamp")

    if ptype == "message":
        role = p.get("role")
        if role == "developer":
            # system/permissions prelude — keep out of the conversation view
            return None
        norm = "user" if role == "user" else "assistant"
        text = _text_parts(p.get("content"))
        if not text.strip():
            return None
        return {"role": norm, "kind": norm, "timestamp": ts,
                "blocks": [{"type": "text", "text": text}],
                "output_tokens": 0, "model": ""}

    if ptype == "function_call":
        args = p.get("arguments")
        try:
            parsed = json.loads(args) if isinstance(args, str) else (args or {})
        except (json.JSONDecodeError, TypeError):
            parsed = {"raw": args}
        return {"role": "assistant", "kind": "tool", "timestamp": ts,
                "blocks": [{"type": "tool_use", "name": p.get("name", ""),
                            "input": parsed}],
                "output_tokens": 0, "model": ""}

    if ptype == "function_call_output":
        out = p.get("output")
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return {"role": "user", "kind": "tool", "timestamp": ts,
                "blocks": [{"type": "tool_result", "text": text}],
                "output_tokens": 0, "model": ""}

    return None


def _last_token_total(path: Path) -> int:
    """Context size at the last turn.

    token_count events carry both `total_token_usage` (cumulative over the whole
    session — NOT the context window) and `last_token_usage` (the window at that
    turn). We want the latter, from the final token_count event.
    """
    ctx = 0
    for rec in iter_jsonl(path):
        if rec.get("type") == "event_msg":
            p = rec.get("payload") or {}
            if p.get("type") == "token_count":
                info = p.get("info") or {}
                last = (info.get("last_token_usage") or {}).get("total_tokens", 0) or 0
                if last:
                    ctx = last
    return ctx


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
            cwd = _session_cwd(s)
            key = _mangle(cwd) if cwd else "unknown"
            out.setdefault(key, []).append(s)
        return out

    def list_projects(self) -> list[dict[str, Any]]:
        out = []
        for name, sessions in self._project_map().items():
            cwd = _session_cwd(sessions[0]) if sessions else ""
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
            cwd = ""
            for rec in iter_jsonl(s):
                if rec.get("type") == "session_meta":
                    cwd = (rec.get("payload") or {}).get("cwd", "") or cwd
                    continue
                turn = _envelope_to_turn(rec)
                if not turn:
                    continue
                msg_count += 1
                txt = turn["blocks"][0].get("text", "") if turn["blocks"] else ""
                if turn["role"] == "user" and turn["kind"] != "tool":
                    # skip synthetic <environment_context> prelude for the summary
                    if txt and not txt.lstrip().startswith("<environment_context>"):
                        if not first:
                            first = txt.strip()[:500]
                        last = txt.strip()[:500]
                    user_turns += 1
                elif turn["role"] == "assistant" and turn["kind"] != "tool":
                    asst_turns += 1
            ctx = _last_token_total(s)
            summ = SessionSummary(
                session_id=s.stem, project=project, file=str(s),
                size_bytes=st.st_size, mtime=st.st_mtime,
                first_prompt=first, last_prompt=last, message_count=msg_count,
                user_turns=user_turns, assistant_turns=asst_turns,
                cwd=cwd, git_branch="", context_tokens=ctx, agent="codex",
            )
            win = context_limit_for(ctx)
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
            turn = _envelope_to_turn(rec)
            if not turn:
                continue
            turns.append({
                "uuid": None,
                "role": turn["role"],
                "kind": turn["kind"],
                "timestamp": turn["timestamp"],
                "model": turn["model"],
                "blocks": turn["blocks"],
                "attribution_skill": None,
                "attribution_plugin": None,
                "output_tokens": turn["output_tokens"],
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
        return _session_cwd(sessions[0]) if sessions else ""

    def get_doc(self, project):
        return read_doc_file(self._project_cwd(project), "AGENTS.md")

    def save_doc(self, project, content):
        return save_doc_file(self._project_cwd(project), "AGENTS.md", content)
