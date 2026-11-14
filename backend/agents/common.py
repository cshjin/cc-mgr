"""Agent-neutral helpers shared by all adapters."""
from __future__ import annotations

import errno
import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

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
    """mkdir -p owner-only (0700). An existing directory we own is tightened
    too (an upgrade may find it 0755), unless it's a shared sticky dir like /tmp."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        st = path.stat()
        if st.st_uid == os.getuid() and not st.st_mode & stat.S_ISVTX:
            os.chmod(path, 0o700)
    except (OSError, AttributeError):  # AttributeError: no os.getuid on Windows
        pass
    return path


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def write_private(path: Path, text: str) -> Path:
    """Write text to a file only its owner can read (0600): never through a
    symlink, and tightening a file an older version left world-readable."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if hasattr(os, "fchmod"):
            os.fchmod(fh.fileno(), 0o600)
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


def new_trash_dir(home: Path, name: str) -> Path:
    """A fresh owner-only <home>/.cc_mgr_trash/<name>.<UTC stamp>/ for a soft
    delete (name: a session id or project name, one path component)."""
    root = private_mkdir(Path(home) / ".cc_mgr_trash")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path, n = safe_join(root, f"{name}.{stamp}"), 1
    while path.exists():  # two deletes within the same second
        path, n = safe_join(root, f"{name}.{stamp}.{n}"), n + 1
    return private_mkdir(path)


def pid_alive(pid: int) -> bool:
    """True if a process with this pid exists on this host. Never signals it
    (on Windows os.kill(pid, 0) would terminate the process). When it can't
    tell, it says alive: callers use it to refuse destructive actions."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return True
        if proc.returncode != 0:  # the query failed: unknown, so alive
            return True
        return str(pid) in proc.stdout.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists but belongs to someone else
        return True
    return True


def resolves_inside(path: Path, root: Path) -> bool:
    """Whether path, with symlinks resolved, is root or lies under it."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def mangle(cwd: str) -> str:
    """A working directory as a single folder-like name (lossy: / \\ : → -)."""
    return cwd.replace("/", "-").replace("\\", "-").replace(":", "-")


# How every generated project name ends; no plain name may end like this.
_GENERATED_NAME = re.compile(r"~[0-9a-f]{12,}$")


def project_names(cwds: Iterable[str]) -> dict[str, str]:
    """A unique project name for each working directory.

    The name is the mangled path, unless another directory mangles alike
    (repo-x vs repo/x) or the mangled path itself ends like a generated name;
    then it is the mangled path plus "~" and 12 hex digits of the path's
    sha256. Plain names never end in "~<12+ hex digits>" and generated ones
    always do, so the two kinds can't meet; generated names that still clash
    get longer hashes. Every final name is unique (else ValueError), and the
    result doesn't depend on the order of cwds.
    """
    cwds = sorted(set(cwds))
    mangled = {cwd: mangle(cwd) for cwd in cwds}
    count = Counter(mangled.values())
    names = dict(mangled)
    pending = [c for c in cwds if count[mangled[c]] > 1 or _GENERATED_NAME.search(mangled[c])]
    for length in (12, 16, 24, 32, 64):
        if not pending:
            return names
        for cwd in pending:
            digest = hashlib.sha256(cwd.encode("utf-8", "surrogatepass")).hexdigest()
            names[cwd] = f"{mangled[cwd]}~{digest[:length]}"
        taken = Counter(names.values())
        pending = [c for c in cwds if taken[names[c]] > 1]
    if pending:  # needs equal 256-bit hashes of distinct paths
        raise ValueError("project names are not unique")
    return names


def restore_dir(src: Path, dst: Path) -> bool:
    """Move the directory src back to dst, never replacing anything there.

    One atomic no-replace rename where the system supports it; otherwise (e.g.
    Lustre, which lacks RENAME_NOREPLACE) dst is claimed with mkdir and src's
    entries move in one by one, each with a step that can't replace anything
    either: a hard link for files and symlinks (then the old name goes), the
    same claim for directories. Whatever can't go back without replacing
    something — dst existed, even as an empty directory (a plain os.rename
    would silently replace that); an entry of that name appeared meanwhile;
    the filesystem refused — stays in src. src is never followed through a
    symlink. True when everything went back and src is gone.
    """
    src, dst = os.fspath(src), os.fspath(dst)
    if os.path.islink(src) or not os.path.isdir(src):
        return False
    try:
        if rename_noreplace(src, dst):
            return True
    except FileExistsError:
        return False
    return _merge_back(src, dst)


def _merge_back(src: str, dst: str) -> bool:
    try:
        os.mkdir(dst)  # the claim: fails if anything is at dst
    except FileExistsError:
        return False
    st = os.lstat(src)
    complete = True
    for name in sorted(os.listdir(src)):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        try:
            if stat.S_ISDIR(os.lstat(s).st_mode):
                complete = _merge_back(s, d) and complete
            else:
                os.link(s, d, follow_symlinks=False)  # fails if d exists
                os.unlink(s)
        except OSError:  # d appeared meanwhile, or this filesystem has no hard links
            complete = False
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    if complete:
        os.rmdir(src)
    else:
        try:
            os.rmdir(dst)  # nothing got in: leave no empty stand-in behind
        except OSError:
            pass
    return complete


@functools.lru_cache(maxsize=1)
def _renameat2():
    """(libc renameat2, ctypes.get_errno), or None: not Linux, no ctypes, or a
    libc without renameat2."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except (ImportError, OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    return fn, ctypes.get_errno


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def rename_noreplace(src, dst) -> bool:
    """Rename src to dst in one atomic step that never replaces anything at dst
    (FileExistsError if something is there, even an empty directory). False if
    this system or filesystem can't do that; then nothing happened."""
    if os.name == "nt":  # Windows' rename never replaces an existing file or directory
        os.rename(src, dst)
        return True
    found = _renameat2()
    if found is None:
        return False
    renameat2, get_errno = found
    if renameat2(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
        return True
    err = get_errno()
    if err in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(err, os.strerror(err), os.fspath(dst))
    if err in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):  # the flag isn't supported here
        return False
    raise OSError(err, os.strerror(err), os.fspath(src))


def _fenced(text: str, lang: str = "") -> str:
    """A Markdown code fence that the text itself can't close early."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{lang}\n{text}\n{fence}"


def render_markdown(agent_label: str, project: str, session_id: str,
                    turns: list[dict[str, Any]], title: str = "") -> str:
    """A conversation (normalized turns, as get_conversation returns them) as
    Markdown — the export format for adapters without a richer one."""
    lines = [f"# Session: {title or session_id}", "",
             f"- **Agent:** {agent_label}", f"- **Session ID:** `{session_id}`",
             f"- **Project:** `{project}`", f"- **Turns:** {len(turns)}", "", "---", ""]
    for t in turns:
        ts = f" ({t['timestamp']})" if t.get("timestamp") else ""
        lines += [f"### {str(t.get('role') or '?').capitalize()}{ts}", ""]
        for b in t.get("blocks") or []:
            bt = b.get("type")
            if bt == "text":
                lines.append(str(b.get("text", "")))
            elif bt == "thinking":
                lines.append("> **Thinking:**\n> " + str(b.get("text", "")).replace("\n", "\n> "))
            elif bt == "tool_use":
                args = json.dumps(b.get("input", {}), indent=2, ensure_ascii=False, default=str)
                lines.append(f"**→ tool: {b.get('name', '')}**\n\n{_fenced(args, 'json')}")
            elif bt == "tool_result":
                lines.append(f"**tool result**\n\n{_fenced(str(b.get('text', '')))}")
            lines.append("")
        lines += ["---", ""]
    return "\n".join(lines)


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


class ProjectChanged(Exception):
    """A project name now stands for a different directory than the caller
    saw (names can shift when a directory that mangles alike appears or goes)."""


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


def check_cwd(cwd: str, expect_cwd: str | None) -> None:
    """ProjectChanged unless the project's directory is still the one the
    caller showed the user (None: no expectation)."""
    if expect_cwd is not None and cwd != expect_cwd:
        raise ProjectChanged(f"this project now refers to {cwd or 'no directory'}, not "
                             f"{expect_cwd or 'no directory'}; reload and try again")


def save_doc_file(cwd: str, filename: str, content: str, expect_cwd: str | None = None) -> Path:
    """Write a root doc into a project's cwd, with a name guard. expect_cwd: the
    directory the editor showed; a project that means another one by now is
    refused (ProjectChanged) rather than written."""
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise ValueError("invalid doc file name")
    check_cwd(cwd, expect_cwd)
    if not cwd:
        raise FileNotFoundError("no working directory known for this project")
    base = Path(cwd)
    if not base.is_dir():
        raise FileNotFoundError(f"working directory does not exist: {cwd}")
    path = safe_join(base, filename)  # refuses a doc symlinked outside the project
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
