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

Deletes go through the Codex CLI itself (verified with codex-cli 0.167.0):
`codex archive <id>` moves the rollout to archived_sessions/ and flags the
thread in state_*.sqlite (`codex unarchive <id>` restores it); `codex delete
--force <id>` removes both. cc_mgr passes its own CODEX_HOME, so the CLI acts
on exactly the data shown here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator

from . import AgentAdapter, Capabilities, UnsupportedCapability
from .common import (
    SessionSummary, context_window, is_safe_id, iter_jsonl, project_names, read_doc_file,
    read_git_info, save_doc_file,
)

_UUID_AT_END = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _meta_id(path: Path) -> str:
    """The UUID in the rollout's session_meta (lower-case), or ""."""
    for rec in iter_jsonl(path):
        if rec.get("type") == "session_meta":
            sid = (rec.get("payload") or {}).get("id")
            return sid.lower() if isinstance(sid, str) and _UUID_AT_END.fullmatch(sid) else ""
    return ""


def _name_id(path: Path) -> str:
    m = _UUID_AT_END.search(path.stem)
    return m.group(0).lower() if m else ""


def _all_rollouts(home: Path) -> list[Path]:
    """Every rollout Codex could resolve an id to: active and archived."""
    return [p for d in ("sessions", "archived_sessions") if (home / d).is_dir()
            for p in (home / d).rglob("rollout-*.jsonl")]


def _codex_cli() -> str | None:
    return shutil.which("codex")


def _run_codex(args: list[str], home: Path) -> None:
    """Run `codex <args>` against cc_mgr's CODEX_HOME; RuntimeError on failure."""
    cli = _codex_cli()
    if not cli:
        raise UnsupportedCapability("the `codex` CLI isn't available here, so Codex sessions "
                                    "can't be archived or deleted")
    try:
        proc = subprocess.run([cli, *args], capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL, env={**os.environ, "CODEX_HOME": str(home)})
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"`codex {args[0]}` failed: {e}") from e
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or proc.stdout or "").strip().splitlines()
                 if ln.strip() and not ln.startswith("WARNING:")]
        raise RuntimeError(f"`codex {args[0]}` failed: "
                           f"{lines[-1] if lines else f'exit {proc.returncode}'}")


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
        can_delete=True, can_export=True,
        delete_note=("Goes through the Codex CLI: a soft delete runs `codex archive` (undo with "
                     "`codex unarchive <id>`), a permanent delete `codex delete`."),
    )

    def _home(self) -> Path:
        return codex_home()

    def has_data(self) -> bool:
        root = self._home() / "sessions"
        return root.is_dir() and any(root.rglob("rollout-*.jsonl"))

    def _project_map(self) -> dict[str, list[Path]]:
        """project name -> [session paths]; one name per cwd (see project_names)."""
        by_cwd: dict[str, list[Path]] = {}
        for s in _all_sessions(self._home()):
            by_cwd.setdefault(_session_cwd(s), []).append(s)
        names = project_names(cwd for cwd in by_cwd if cwd)
        return {(names[cwd] if cwd else "unknown"): paths for cwd, paths in by_cwd.items()}

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
            model = ""
            for rec in iter_jsonl(s):
                if rec.get("type") == "session_meta":
                    cwd = (rec.get("payload") or {}).get("cwd", "") or cwd
                    continue
                if rec.get("type") == "turn_context":
                    model = (rec.get("payload") or {}).get("model") or model
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
                model=model,
            )
            win = context_window(model, ctx)
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

    def save_doc(self, project, content, expect_cwd=None):
        return save_doc_file(self._project_cwd(project), "AGENTS.md", content, expect_cwd)

    def _codex_target(self, path: Path) -> str:
        """The id to hand the codex CLI, only if it unambiguously means this
        rollout: the ids in the file name and in session_meta agree, and no
        other rollout (active or archived) carries the same id."""
        meta, name = _meta_id(path), _name_id(path)
        if meta and name and meta != name:
            raise RuntimeError(f"{path.name}: the id in its file name and in its session_meta "
                               f"disagree ({name} vs {meta}); not handing it to codex")
        thread = meta or name
        if not thread:
            raise RuntimeError(f"can't tell the Codex id of {path.name}")
        for other in _all_rollouts(self._home()):
            if other != path and (thread in other.name.lower() or _meta_id(other) == thread):
                raise RuntimeError(f"another rollout ({other.name}) carries the same Codex id "
                                   f"{thread}; not handing it to codex")
        return thread

    def delete_session(self, project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
        if not is_safe_id(session_id):
            raise ValueError("invalid session id")
        path = self._session_path(project, session_id)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"session {session_id} not found in {project}")
        home = self._home()
        thread = self._codex_target(path)
        before = set(_all_sessions(home))
        _run_codex(["delete", "--force", thread] if hard else ["archive", thread], home)
        after = set(_all_sessions(home))
        if path in after:  # the CLI reported success: make sure it really acted
            raise RuntimeError(f"Codex did not {'delete' if hard else 'archive'} session {thread}")
        collateral = sorted(p.name for p in before - after - {path})
        if collateral:
            raise RuntimeError(f"Codex also removed other sessions: {', '.join(collateral)}")
        if hard:
            return {"deleted": [str(path)], "trash": None, "note": "Deleted with `codex delete`."}
        return {"deleted": [str(path)], "trash": "codex-archive",
                "note": f"Archived in Codex; restore it with `codex unarchive {thread}`."}
