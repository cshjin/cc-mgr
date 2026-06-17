"""Data-access layer for Claude Code local project data.

Reads, on demand, from:
  ~/.claude/projects/<mangled-path>/
      <session-uuid>.jsonl          conversation transcript
      memory/MEMORY.md, memory/*.md per-project memory
      <session-uuid>/...            per-session sidecar dirs
  ~/.claude/tasks/<session-uuid>/N.json   per-session tasks

Nothing is mutated here; this module is read-only.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator


def claude_home() -> Path:
    """Root of the Claude Code data dir, overridable for remote/testing."""
    env = os.environ.get("CLAUDE_HOME")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def projects_dir() -> Path:
    return claude_home() / "projects"


def tasks_dir() -> Path:
    return claude_home() / "tasks"


def unmangle_path(folder_name: str) -> str:
    """Best-effort recovery of the original cwd from a mangled folder name.

    Claude mangles a path by replacing path separators (and ':') with '-'.
    The transform is lossy (a literal '-' in the path is indistinguishable
    from a separator), so this is display-only.
    """
    name = folder_name
    # Windows drive prefix like "G--My-Drive" -> "G:/My/Drive" is ambiguous;
    # show a readable approximation.
    name = name.replace("--", ":/", 1) if "--" in name else name
    return name.replace("-", "/") if False else folder_name


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _block_text(content: Any) -> str:
    """Flatten a message.content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if not isinstance(blk, dict):
                parts.append(str(blk))
                continue
            t = blk.get("type")
            if t == "text":
                parts.append(blk.get("text", ""))
            elif t == "thinking":
                parts.append(blk.get("thinking", ""))
            elif t == "tool_use":
                parts.append(f"[tool_use: {blk.get('name', '?')}]")
            elif t == "tool_result":
                parts.append("[tool_result]")
        return "\n".join(p for p in parts if p)
    return ""


@dataclass
class SessionSummary:
    session_id: str
    project: str
    file: str
    size_bytes: int
    mtime: float
    first_prompt: str = ""
    last_prompt: str = ""
    message_count: int = 0
    user_turns: int = 0
    assistant_turns: int = 0
    context_tokens: int = 0  # best estimate of live context window size
    total_output_tokens: int = 0
    model: str = ""
    git_branch: str = ""
    cwd: str = ""
    has_memory: bool = False
    open_tasks: int = 0
    total_tasks: int = 0


def list_projects() -> list[dict[str, Any]]:
    root = projects_dir()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        sessions = list(d.glob("*.jsonl"))
        if not sessions and not (d / "memory").is_dir():
            continue
        last_mtime = max((s.stat().st_mtime for s in sessions), default=d.stat().st_mtime)
        out.append({
            "name": d.name,
            "display_path": unmangle_path(d.name),
            "session_count": len(sessions),
            "has_memory": (d / "memory").is_dir(),
            "mtime": last_mtime,
        })
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out


def _scan_session(path: Path, project: str) -> SessionSummary:
    """Single pass over a JSONL to build a lightweight summary."""
    stat = path.stat()
    summ = SessionSummary(
        session_id=path.stem,
        project=project,
        file=str(path),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
    )
    last_assistant_context = 0
    first_user_seen = False
    for rec in _iter_jsonl(path):
        rtype = rec.get("type")
        if rtype == "user":
            msg = rec.get("message", {})
            text = _block_text(msg.get("content"))
            # skip tool_result-only user records for prompt display
            if text and not text.startswith("[tool_result"):
                if not first_user_seen:
                    summ.first_prompt = text.strip()[:500]
                    first_user_seen = True
                summ.last_prompt = text.strip()[:500]
                summ.user_turns += 1
            summ.message_count += 1
            if rec.get("gitBranch"):
                summ.git_branch = rec["gitBranch"]
            if rec.get("cwd"):
                summ.cwd = rec["cwd"]
        elif rtype == "assistant":
            summ.message_count += 1
            summ.assistant_turns += 1
            msg = rec.get("message", {})
            if msg.get("model"):
                summ.model = msg["model"]
            usage = msg.get("usage") or {}
            out_t = usage.get("output_tokens", 0) or 0
            summ.total_output_tokens += out_t
            # live context ~= input + cache_read + cache_creation + output
            ctx = (
                (usage.get("input_tokens", 0) or 0)
                + (usage.get("cache_read_input_tokens", 0) or 0)
                + (usage.get("cache_creation_input_tokens", 0) or 0)
                + out_t
            )
            if ctx:
                last_assistant_context = ctx
    summ.context_tokens = last_assistant_context
    return summ


def list_sessions(project: str) -> list[SessionSummary]:
    d = projects_dir() / project
    if not d.is_dir():
        return []
    has_mem = (d / "memory").is_dir()
    sessions = []
    for f in d.glob("*.jsonl"):
        summ = _scan_session(f, project)
        summ.has_memory = has_mem
        tdir = tasks_dir() / summ.session_id
        if tdir.is_dir():
            total, opn = _count_tasks(tdir)
            summ.total_tasks = total
            summ.open_tasks = opn
        sessions.append(summ)
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def _count_tasks(tdir: Path) -> tuple[int, int]:
    total = 0
    opn = 0
    for jf in tdir.glob("*.json"):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        total += 1
        if data.get("status") not in ("completed", "deleted"):
            opn += 1
    return total, opn


def get_tasks(session_id: str) -> list[dict[str, Any]]:
    tdir = tasks_dir() / session_id
    if not tdir.is_dir():
        return []
    tasks = []
    for jf in tdir.glob("*.json"):
        try:
            tasks.append(json.loads(jf.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    tasks.sort(key=lambda t: int(t.get("id", 0)) if str(t.get("id", "")).isdigit() else 0)
    return tasks


def get_memory(project: str) -> dict[str, Any]:
    mem_dir = projects_dir() / project / "memory"
    if not mem_dir.is_dir():
        return {"index": "", "files": []}
    index = ""
    idx_path = mem_dir / "MEMORY.md"
    if idx_path.is_file():
        index = idx_path.read_text(encoding="utf-8", errors="replace")
    files = []
    for f in sorted(mem_dir.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        files.append({
            "name": f.name,
            "content": f.read_text(encoding="utf-8", errors="replace"),
        })
    return {"index": index, "files": files}


def get_conversation(project: str, session_id: str) -> list[dict[str, Any]]:
    """Return ordered conversation turns with structured content blocks.

    Tool-result-only user records are merged as metadata rather than turns,
    keeping the viewer focused on prompts and responses.
    """
    path = projects_dir() / project / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    turns: list[dict[str, Any]] = []
    for rec in _iter_jsonl(path):
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        msg = rec.get("message", {})
        content = msg.get("content")
        blocks = _structured_blocks(content)
        # Classify: a user record whose only blocks are tool_results is a tool turn
        is_tool_only = (
            rtype == "user"
            and blocks
            and all(b["type"] == "tool_result" for b in blocks)
        )
        usage = msg.get("usage") or {}
        turns.append({
            "uuid": rec.get("uuid"),
            "role": rtype,
            "kind": "tool" if is_tool_only else rtype,
            "timestamp": rec.get("timestamp"),
            "model": msg.get("model", ""),
            "blocks": blocks,
            "attribution_skill": rec.get("attributionSkill"),
            "attribution_plugin": rec.get("attributionPlugin"),
            "output_tokens": usage.get("output_tokens", 0),
        })
    return turns


def _structured_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for blk in content:
        if not isinstance(blk, dict):
            out.append({"type": "text", "text": str(blk)})
            continue
        t = blk.get("type")
        if t == "text":
            out.append({"type": "text", "text": blk.get("text", "")})
        elif t == "thinking":
            out.append({"type": "thinking", "text": blk.get("thinking", "")})
        elif t == "tool_use":
            out.append({
                "type": "tool_use",
                "name": blk.get("name", "?"),
                "input": blk.get("input", {}),
            })
        elif t == "tool_result":
            res = blk.get("content")
            out.append({"type": "tool_result", "text": _block_text(res)})
    return out
