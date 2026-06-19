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
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def claude_home() -> Path:
    """Root of the Claude Code data dir, overridable for remote/testing.

    Claude Code uses a single ``~/.claude`` directory on every platform
    (Windows, macOS, Linux) — there is no %APPDATA% / XDG split — and ``~``
    resolves correctly on all three via ``Path.home()``. Overrides, in order of
    precedence: ``CLAUDE_HOME`` (cc_mgr's own seam, used for remote/testing) then
    ``CLAUDE_CONFIG_DIR`` (Claude Code's official override for multi-account /
    relocated config dirs). ``expanduser`` so ``~``-based values work too.
    """
    for var in ("CLAUDE_HOME", "CLAUDE_CONFIG_DIR"):
        env = os.environ.get(var)
        if env:
            return Path(env).expanduser()
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


# Context-window tiers (tokens). Transcripts record only the resolved API model
# id (e.g. "claude-opus-4-8") — never the 1M-context "[1m]" alias and never an
# explicit window size. So the true window is genuinely NOT stored per session.
# The only thing we can assert from a transcript is a *lower bound*: if observed
# usage crossed 200k, the window must be the 1M tier. Below 200k it's ambiguous
# (could be a 200k model, or a 1M model that simply didn't fill up).
CTX_TIERS = (200_000, 1_000_000)
STD_WINDOW = 200_000


def context_limit_for(observed_tokens: int) -> dict[str, Any]:
    """Best-effort window for a session given its peak observed usage.

    Returns {limit, known}. `known` is True only when usage proves the tier
    (i.e. it exceeded the standard 200k window, so it must be the 1M variant).
    When False, `limit` is an assumed default (200k) for display, NOT a fact.
    """
    if observed_tokens > STD_WINDOW * 0.98:
        return {"limit": 1_000_000, "known": True}
    return {"limit": STD_WINDOW, "known": False}


def read_git_info(cwd: str | None) -> dict[str, Any] | None:
    """Read repo name + current branch from <cwd>/.git without invoking git.

    Returns None if cwd is missing or not a git working tree.
    """
    if not cwd:
        return None
    try:
        root = Path(cwd)
    except (TypeError, ValueError):
        return None
    git = root / ".git"
    if not git.exists():
        return None
    branch = None
    detached = False
    head_file = git / "HEAD" if git.is_dir() else None
    # Worktrees use a .git *file* pointing elsewhere; handle the common dir case.
    if git.is_file():
        try:
            content = git.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("gitdir:"):
                gitdir = Path(content.split(":", 1)[1].strip())
                # The gitdir may be relative to the .git file's own directory.
                if not gitdir.is_absolute():
                    gitdir = (git.parent / gitdir).resolve()
                head_file = gitdir / "HEAD"
        except OSError:
            head_file = None
    if head_file and head_file.is_file():
        try:
            head = head_file.read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref:"):
                branch = head.split("/")[-1]
            elif head:
                branch = head[:8]
                detached = True
        except OSError:
            pass
    return {"repo": root.name, "branch": branch, "detached": detached}


def _peek_session_meta(path: Path, max_lines: int = 40) -> dict[str, Any]:
    """Cheaply read cwd/gitBranch from the head of a transcript (no full parse)."""
    meta: dict[str, Any] = {"cwd": "", "git_branch": ""}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("cwd") and not meta["cwd"]:
                    meta["cwd"] = rec["cwd"]
                if rec.get("gitBranch") and not meta["git_branch"]:
                    meta["git_branch"] = rec["gitBranch"]
                if meta["cwd"] and meta["git_branch"]:
                    break
    except OSError:
        pass
    return meta


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
    context_tokens: int = 0  # peak observed context (input+cache+output) of last turn
    context_limit: int = 200_000  # window size; assumed default unless context_limit_known
    context_limit_known: bool = False  # True only when usage proves the tier
    total_output_tokens: int = 0
    model: str = ""
    git_branch: str = ""
    cwd: str = ""
    has_memory: bool = False
    open_tasks: int = 0
    total_tasks: int = 0


def list_projects() -> list[dict[str, Any]]:
    """Project rows with cheap aggregates: latest activity, task progress, git.

    Deliberately avoids full transcript parsing — only stat() and a short head
    peek of the most-recent session for cwd/git — so the list stays fast.
    """
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

        # newest session by mtime drives "active time" + cwd/git
        sessions_by_mtime = sorted(sessions, key=lambda s: s.stat().st_mtime, reverse=True)
        last_mtime = (
            sessions_by_mtime[0].stat().st_mtime if sessions_by_mtime else d.stat().st_mtime
        )

        # aggregate task progress across this project's sessions
        open_tasks = total_tasks = 0
        for s in sessions:
            tdir = tasks_dir() / s.stem
            if tdir.is_dir():
                t, o = _count_tasks(tdir)
                total_tasks += t
                open_tasks += o

        # cwd + git from the newest session (cheap head peek)
        cwd = ""
        git = None
        if sessions_by_mtime:
            meta = _peek_session_meta(sessions_by_mtime[0])
            cwd = meta.get("cwd", "")
            git = read_git_info(cwd)

        out.append({
            "name": d.name,
            "display_path": cwd or unmangle_path(d.name),
            "cwd": cwd,
            "session_count": len(sessions),
            "has_memory": (d / "memory").is_dir(),
            "has_claude_md": bool(cwd and (Path(cwd) / "CLAUDE.md").is_file()),
            "open_tasks": open_tasks,
            "total_tasks": total_tasks,
            "git": git,
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
    peak_assistant_context = 0
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
                if ctx > peak_assistant_context:
                    peak_assistant_context = ctx
    summ.context_tokens = last_assistant_context
    # Tier is proven by the PEAK context ever reached, not the last turn: a
    # `compact` drops the live context below 200k, but if usage ever crossed
    # 200k the window must be the 1M tier — that fact must not be forgotten.
    win = context_limit_for(peak_assistant_context)
    summ.context_limit = win["limit"]
    summ.context_limit_known = win["known"]
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


def update_task_status(session_id: str, task_id: str, status: str) -> dict[str, Any]:
    """Patch a single task's status in its JSON file. Returns the updated task."""
    valid = {"pending", "in_progress", "completed", "deleted"}
    if status not in valid:
        raise ValueError(f"invalid status: {status}")
    jf = tasks_dir() / session_id / f"{task_id}.json"
    if not jf.is_file():
        raise FileNotFoundError(f"task {task_id} not found for session {session_id}")
    data = json.loads(jf.read_text(encoding="utf-8"))
    data["status"] = status
    jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def get_memory(project: str) -> dict[str, Any]:
    mem_dir = projects_dir() / project / "memory"
    if not mem_dir.is_dir():
        return {"index": "", "index_path": "", "dir": str(mem_dir), "files": []}
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
            "path": str(f),
            "content": f.read_text(encoding="utf-8", errors="replace"),
        })
    return {
        "index": index,
        "index_path": str(idx_path) if idx_path.is_file() else str(idx_path),
        "dir": str(mem_dir),
        "files": files,
    }


def save_memory_file(project: str, name: str, content: str) -> Path:
    """Write a memory file (MEMORY.md or *.md) under the project's memory dir.

    `name` is a bare filename — path components are rejected to prevent escapes.
    """
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError("invalid memory file name")
    if not name.endswith(".md"):
        raise ValueError("memory files must end in .md")
    mem_dir = projects_dir() / project / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    out = mem_dir / name
    out.write_text(content, encoding="utf-8")
    return out


def _project_cwd(project: str) -> str:
    """Resolve the working directory for a project from its newest session."""
    d = projects_dir() / project
    sessions = sorted(d.glob("*.jsonl"), key=lambda s: s.stat().st_mtime, reverse=True)
    if not sessions:
        return ""
    return _peek_session_meta(sessions[0]).get("cwd", "")


def get_claude_md(project: str) -> dict[str, Any]:
    """Read the project's CLAUDE.md from its working directory, if present."""
    cwd = _project_cwd(project)
    if not cwd:
        return {"exists": False, "path": "", "content": "", "cwd": ""}
    path = Path(cwd) / "CLAUDE.md"
    if path.is_file():
        return {
            "exists": True,
            "path": str(path),
            "content": path.read_text(encoding="utf-8", errors="replace"),
            "cwd": cwd,
        }
    return {"exists": False, "path": str(path), "content": "", "cwd": cwd}


def save_claude_md(project: str, content: str) -> Path:
    """Write CLAUDE.md into the project's working directory."""
    cwd = _project_cwd(project)
    if not cwd:
        raise FileNotFoundError("no working directory known for this project")
    base = Path(cwd)
    if not base.is_dir():
        raise FileNotFoundError(f"working directory does not exist: {cwd}")
    path = base / "CLAUDE.md"
    path.write_text(content, encoding="utf-8")
    return path


def project_tasks(project: str) -> list[dict[str, Any]]:
    """All tasks across all sessions in a project, tagged with their session id."""
    d = projects_dir() / project
    out: list[dict[str, Any]] = []
    for s in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        for t in get_tasks(s.stem):
            t = dict(t)
            t["session_id"] = s.stem
            out.append(t)
    return out


def get_conversation(
    project: str,
    session_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a window of conversation turns with structured content blocks.

    Tool-result-only user records are classified as 'tool' turns rather than
    prompts, keeping the viewer focused on prompts and responses. Returns
    {total, offset, limit, turns} so the UI can lazily page huge sessions.
    """
    path = projects_dir() / project / f"{session_id}.jsonl"
    if not path.is_file():
        return {"total": 0, "offset": offset, "limit": limit, "turns": []}

    turns: list[dict[str, Any]] = []
    for rec in _iter_jsonl(path):
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        msg = rec.get("message", {})
        blocks = _structured_blocks(msg.get("content"))
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

    total = len(turns)
    end = total if limit is None else min(total, offset + limit)
    window = turns[offset:end]
    return {"total": total, "offset": offset, "limit": limit, "turns": window}


# ---------------------------------------------------------------------------
# Export + delete (the only mutating operations)
# ---------------------------------------------------------------------------

def export_session_markdown(project: str, session_id: str) -> str:
    """Render a full session transcript to Markdown text."""
    data = get_conversation(project, session_id, offset=0, limit=None)
    lines = [f"# Session {session_id}", f"_Project: {project}_", ""]
    for t in data["turns"]:
        role = t["kind"]
        ts = t.get("timestamp") or ""
        header = f"## {role}"
        if t.get("attribution_skill"):
            header += f"  ·  skill: {t['attribution_skill']}"
        if ts:
            header += f"  ·  {ts}"
        lines.append(header)
        for b in t["blocks"]:
            bt = b["type"]
            if bt == "text":
                lines.append(b["text"])
            elif bt == "thinking":
                lines.append(f"> _(thinking)_\n>\n> " + b["text"].replace("\n", "\n> "))
            elif bt == "tool_use":
                lines.append(f"**→ tool: {b['name']}**\n\n```json\n{json.dumps(b['input'], indent=2, ensure_ascii=False)}\n```")
            elif bt == "tool_result":
                txt = b.get("text", "")
                lines.append(f"**tool result**\n\n```\n{txt}\n```")
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def export_session_to_file(project: str, session_id: str, out_dir: Path | None = None) -> Path:
    """Write the session export to exports/<project>/<session>.md and return the path."""
    if out_dir is None:
        out_dir = Path.cwd() / "exports" / project
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{session_id}.{stamp}.md"
    out_path.write_text(export_session_markdown(project, session_id), encoding="utf-8")
    return out_path


def save_session_as_memory(project: str, session_id: str) -> Path:
    """Distill a session into a project-reference memory file.

    Deterministic summary (no model call): first/last prompt, turn/token stats,
    skills used, and task subjects. Written under the project's memory/ dir and
    registered in MEMORY.md so future sessions can recall it.
    """
    summ = None
    for s in list_sessions(project):
        if s.session_id == session_id:
            summ = s
            break
    if summ is None:
        raise FileNotFoundError(f"session {session_id} not found in {project}")

    data = get_conversation(project, session_id, offset=0, limit=None)
    skills = sorted({t["attribution_skill"] for t in data["turns"] if t.get("attribution_skill")})
    tasks = get_tasks(session_id)
    short = session_id[:8]

    body = [
        "---",
        f"name: session-{short}",
        f"description: archived summary of Claude session {short} in {project}",
        "metadata:",
        "  type: reference",
        "---",
        "",
        f"Archived summary of session `{session_id}` (project `{project}`), saved "
        f"before deletion via cc_mgr.",
        "",
        f"- **Turns:** {summ.message_count} ({summ.user_turns} user / {summ.assistant_turns} assistant)",
        f"- **Context size:** ~{summ.context_tokens} tokens; total output ~{summ.total_output_tokens}",
        f"- **Model:** {summ.model or 'n/a'}    **Git branch:** {summ.git_branch or 'n/a'}",
    ]
    if skills:
        body.append(f"- **Skills used:** {', '.join(skills)}")
    body.append("")
    body.append(f"**First prompt:** {summ.first_prompt or '(none)'}")
    body.append("")
    body.append(f"**Last prompt:** {summ.last_prompt or '(none)'}")
    if tasks:
        body.append("")
        body.append("**Tasks:**")
        for t in tasks:
            body.append(f"- [{t.get('status','?')}] {t.get('subject','(untitled)')}")
    body.append("")

    mem_dir = projects_dir() / project / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    out = mem_dir / f"session_{short}.md"
    out.write_text("\n".join(body), encoding="utf-8")

    # register in MEMORY.md (create if absent)
    idx = mem_dir / "MEMORY.md"
    line = f"- [Session {short}](session_{short}.md) — archived session summary saved by cc_mgr"
    if idx.is_file():
        existing = idx.read_text(encoding="utf-8")
        if line not in existing:
            idx.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")
    else:
        idx.write_text("# Memory Index\n\n" + line + "\n", encoding="utf-8")
    return out


def delete_session(project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
    """Remove a session's transcript, sidecar dir, and tasks.

    By default this is a soft delete: artifacts are moved to
    <claude_home>/.cc_mgr_trash/<timestamp>/ so the action is reversible.
    Set hard=True to permanently remove instead.
    """
    proj_dir = projects_dir() / project
    jsonl = proj_dir / f"{session_id}.jsonl"
    sidecar = proj_dir / session_id
    tdir = tasks_dir() / session_id

    moved: list[str] = []
    if hard:
        for p in (jsonl, sidecar, tdir):
            if p.is_file():
                p.unlink()
                moved.append(str(p))
            elif p.is_dir():
                shutil.rmtree(p)
                moved.append(str(p))
        return {"deleted": moved, "trash": None}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trash = claude_home() / ".cc_mgr_trash" / f"{session_id}.{stamp}"
    trash.mkdir(parents=True, exist_ok=True)
    if jsonl.is_file():
        shutil.move(str(jsonl), str(trash / jsonl.name))
        moved.append(str(jsonl))
    if sidecar.is_dir():
        shutil.move(str(sidecar), str(trash / f"sidecar_{session_id}"))
        moved.append(str(sidecar))
    if tdir.is_dir():
        shutil.move(str(tdir), str(trash / f"tasks_{session_id}"))
        moved.append(str(tdir))
    return {"deleted": moved, "trash": str(trash)}


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
