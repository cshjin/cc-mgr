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
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities
from .common import (
    SessionSummary, block_text, connect_sqlite_ro, context_window, iter_jsonl,
    read_doc_file, read_git_info, save_doc_file, structured_blocks,
)


def copilot_home() -> Path:
    env = os.environ.get("COPILOT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".copilot"


def _mangle(cwd: str) -> str:
    return cwd.replace("/", "-").replace("\\", "-").replace(":", "-")


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


class CopilotAdapter(AgentAdapter):
    capabilities = Capabilities(
        agent_id="copilot", label="Copilot", doc_filename="AGENTS.md",
        has_memory=False, has_tasks=False, can_edit_doc=True,
        can_delete=False, can_export=False,
    )

    def _home(self) -> Path:
        return copilot_home()

    def _history_root(self) -> Path:
        return self._home() / "history"

    def _store_sessions(self, project: str) -> list[dict[str, Any]]:
        return [s for s in _all_sessions(self._home()).values() if _mangle(s["cwd"]) == project]

    def list_projects(self) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for s in _all_sessions(self._home()).values():
            groups[s["cwd"]].append(s)
        out = []
        for cwd, sessions in groups.items():
            out.append({
                "name": _mangle(cwd),
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
