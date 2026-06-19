"""Agent-neutral helpers shared by all adapters."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

CTX_TIERS = (200_000, 1_000_000)
STD_WINDOW = 200_000


def context_limit_for(observed_tokens: int) -> dict[str, Any]:
    """Window tier provable only when usage crossed 200k; else unknown."""
    if observed_tokens > STD_WINDOW:
        return {"limit": 1_000_000, "known": True}
    return {"limit": STD_WINDOW, "known": False}


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def block_text(content: Any) -> str:
    """Flatten message content to plain text for summaries/indexing."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("text", "thinking"):
            parts.append(b.get("text", ""))
        elif t == "tool_use":
            parts.append(f"[tool:{b.get('name','')}]")
        elif t == "tool_result":
            inner = b.get("content")
            parts.append(inner if isinstance(inner, str) else block_text(inner))
    return "\n".join(p for p in parts if p)


def structured_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize message content into typed blocks for the viewer."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type", "text")
        if t == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif t == "thinking":
            out.append({"type": "thinking", "text": b.get("thinking", b.get("text", ""))})
        elif t == "tool_use":
            out.append({"type": "tool_use", "name": b.get("name", ""),
                        "input": b.get("input", {})})
        elif t == "tool_result":
            inner = b.get("content")
            out.append({"type": "tool_result",
                        "text": inner if isinstance(inner, str) else block_text(inner)})
        else:
            out.append({"type": "text", "text": block_text([b])})
    return out


def read_git_info(cwd: str | None) -> dict[str, Any] | None:
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
    if git.is_file():
        try:
            content = git.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("gitdir:"):
                gitdir = Path(content.split(":", 1)[1].strip())
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


def read_doc_file(cwd: str, filename: str) -> dict[str, Any]:
    """Read a root doc (CLAUDE.md/GEMINI.md/AGENTS.md) from a project's cwd."""
    if not cwd:
        return {"exists": False, "path": "", "content": "", "cwd": ""}
    path = Path(cwd) / filename
    if path.is_file():
        return {"exists": True, "path": str(path),
                "content": path.read_text(encoding="utf-8", errors="replace"),
                "cwd": cwd}
    return {"exists": False, "path": str(path), "content": "", "cwd": cwd}


def save_doc_file(cwd: str, filename: str, content: str) -> Path:
    """Write a root doc into a project's cwd, with a name guard."""
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise ValueError("invalid doc file name")
    if not cwd:
        raise FileNotFoundError("no working directory known for this project")
    base = Path(cwd)
    if not base.is_dir():
        raise FileNotFoundError(f"working directory does not exist: {cwd}")
    path = base / filename
    path.write_text(content, encoding="utf-8")
    return path


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
    context_tokens: int = 0
    context_limit: int = 200_000
    context_limit_known: bool = False
    total_output_tokens: int = 0
    model: str = ""
    git_branch: str = ""
    cwd: str = ""
    has_memory: bool = False
    open_tasks: int = 0
    total_tasks: int = 0
    agent: str = "claude"
