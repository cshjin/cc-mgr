"""Agent-neutral helpers shared by all adapters."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

CTX_TIERS = (200_000, 1_000_000)
STD_WINDOW = 200_000

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")


def is_safe_id(value: str | None) -> bool:
    """Session / task ids: a single plain path component, no glob characters."""
    return bool(value) and _SAFE_ID.fullmatch(value) is not None


def is_safe_name(value: str | None) -> bool:
    """Project names: one path component (mangled cwds may contain spaces, dots,
    unicode, …) but never a separator, NUL, or a dot-only name."""
    return (bool(value) and value not in (".", "..")
            and not any(c in value for c in ("/", "\\", "\x00")))


def safe_join(root: Path, *parts: str) -> Path:
    """Join parts under root, refusing any result that escapes root.

    Symlinks are resolved on both sides, so a symlinked project directory or
    memory file can't redirect a write/move/delete outside root.
    """
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, *parts))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"path escapes {root}: {parts!r}")
    return Path(os.path.abspath(os.path.join(root, *parts)))


def private_mkdir(path: Path) -> Path:
    """mkdir -p, creating missing directories owner-only (0700)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write_private(path: Path, text: str) -> Path:
    """Write text to a file only its owner can read (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


REPO_DIR = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """cc_mgr's own state (search index, pidfiles, logs): $CC_MGR_DATA_DIR or
    <repo>/data. Relocate it e.g. off a slow network filesystem."""
    env = os.environ.get("CC_MGR_DATA_DIR")
    return Path(env).expanduser() if env else REPO_DIR / "data"


def export_root() -> Path:
    """Where session exports go: $CC_MGR_EXPORT_DIR or <repo>/exports."""
    env = os.environ.get("CC_MGR_EXPORT_DIR")
    return Path(env).expanduser() if env else REPO_DIR / "exports"


def default_export_dir(project: str) -> Path:
    """<export_root>/<project>/; exports hold full conversations, so the root is
    created owner-only."""
    if not is_safe_name(project):
        raise ValueError("invalid project name")
    return safe_join(private_mkdir(export_root()), project)


def connect_sqlite_ro(db_file: Path, timeout: float = 5.0) -> sqlite3.Connection:
    """Open another tool's live SQLite DB read-only, WAL included.

    mode=ro takes the normal shared locks and reads the -wal file, so it sees
    every committed transaction and never returns torn pages. immutable=1 skips
    both: while the owner writes, it misses un-checkpointed data and can report
    "database disk image is malformed". So it is only a fallback for a file
    mode=ro can't *open* (e.g. no access to the -shm file) — never for lock
    contention, which must surface rather than turn into a stale read — and
    it keeps mode=ro so it can't create a file either.
    """
    path = Path(db_file)
    if not path.is_file():
        raise FileNotFoundError(f"no such database: {db_file}")
    uri = path.resolve().as_uri()
    try:
        conn = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=timeout)
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.Error:
            conn.close()
            raise
    except sqlite3.OperationalError as e:
        if not _cannot_open(e):
            raise
        logging.getLogger(__name__).warning(
            "%s: read-only open failed (%s); reading it as immutable, so recent "
            "writes may be missing", db_file, e)
        conn = sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cannot_open(e: sqlite3.Error) -> bool:
    name = getattr(e, "sqlite_errorname", "") or ""
    if name:
        return name.startswith(("SQLITE_CANTOPEN", "SQLITE_READONLY"))
    msg = str(e).lower()  # Python < 3.11 has no error names
    return "unable to open" in msg or "readonly" in msg


def context_limit_for(observed_tokens: int) -> dict[str, Any]:
    """Window tier provable only when usage crossed 200k; else unknown."""
    if observed_tokens > STD_WINDOW:
        return {"limit": 1_000_000, "known": True}
    return {"limit": STD_WINDOW, "known": False}


_CLAUDE_HINTS = ("claude", "opus", "sonnet", "haiku")


def is_claude_model(model: str | None) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _CLAUDE_HINTS)


def context_window(model: str | None, peak_tokens: int) -> dict[str, Any]:
    """Window to show for a session that peaked at peak_tokens.

    The 200k / 1M rule is Claude-specific: only for a Claude model does usage
    above 200k prove the 1M tier. For any other (or unknown) model no tier is
    ever claimed; the bar just scales to the tier above the observed usage.
    """
    if is_claude_model(model):
        return context_limit_for(peak_tokens)
    return {"limit": 1_000_000 if peak_tokens > STD_WINDOW else STD_WINDOW, "known": False}


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
                ref = head.split(":", 1)[1].strip()
                # keep slashes in branch names: refs/heads/feature/foo -> feature/foo
                branch = (ref[len("refs/heads/"):] if ref.startswith("refs/heads/")
                          else ref.rsplit("/", 1)[-1])
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
    title: str = ""
    parent_id: str = ""  # set for subagent (child) sessions
