# Multi-agent Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let cc_mgr browse and search conversations from Claude, Gemini, Codex, and Copilot via a brand-bar dropdown, with per-agent root-doc editing.

**Architecture:** Approach A — an `AgentAdapter` protocol + registry under `backend/agents/`. Today's `store.py` logic becomes `ClaudeAdapter`; Gemini/Codex/Copilot are smaller adapters; shared logic (mangling, git, jsonl, path-guarded doc writer, soft-delete, `SessionSummary`) lives in `common.py`. `app.py` routes each request to the active adapter via an `?agent=` param (default `claude`). The SQLite/FTS index gains an `agent` column (schema 2→3).

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, SQLite FTS5, vanilla JS frontend, pytest (added as dev dependency).

---

## File Structure

```
backend/
  agents/
    __init__.py    # AgentAdapter ABC, Capabilities, UnsupportedCapability, REGISTRY, get_adapter()
    common.py      # SessionSummary, mangling, git info, jsonl iter, context tiers,
                   #   structured blocks, path-guarded doc read/write, soft-delete helpers
    claude.py      # ClaudeAdapter (full features; today's store.py logic)
    gemini.py      # GeminiAdapter (event-sourced transcript parser; GEMINI.md)
    codex.py       # CodexAdapter  (rollout sessions reader; AGENTS.md)
    copilot.py     # CopilotAdapter (AGENTS.md; sessions when present)
  store.py         # thin shim: re-export claude_home/projects_dir/tasks_dir + ClaudeAdapter delegations for back-compat
  index_db.py      # gains `agent` column; reindex loops REGISTRY; search(agent=...)
  app.py           # routes resolve adapter via ?agent=; GET /api/agents
frontend/
  index.html       # agent dropdown next to brand
  app.js           # state.agent, api() seam, capability-driven chrome, search agent param
  style.css        # .agentsel styling
tests/
  conftest.py      # tmp-home fixtures synthesizing each agent's on-disk layout
  test_common.py
  test_claude_adapter.py
  test_gemini_adapter.py
  test_codex_adapter.py
  test_copilot_adapter.py
  test_registry.py
  test_index_agent.py
  test_app_routes.py
requirements-dev.txt # pytest, httpx (for FastAPI TestClient)
```

---

## Task 1: Dev test harness (pytest + dev deps)

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`

- [ ] **Step 1: Add dev requirements**

Create `requirements-dev.txt`:

```
pytest>=8.0
httpx>=0.27
```

- [ ] **Step 2: Install dev deps**

Run: `pip install -r requirements-dev.txt`
Expected: pytest and httpx install successfully.

- [ ] **Step 3: Create empty tests package marker**

Create `tests/__init__.py` with no content (empty file).

- [ ] **Step 4: Write shared fixtures synthesizing each agent's on-disk layout**

Create `tests/conftest.py`:

```python
"""Fixtures that build synthetic agent homes in a tmp dir.

Each fixture returns the home Path and sets the matching env var so adapters
resolve into the tmp tree instead of the real ~/. No real user data touched.
"""
import json
import os
from pathlib import Path

import pytest


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    cwd = tmp_path / "repo_claude"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text("# Claude project doc\nalpha\n", encoding="utf-8")
    proj = home / "projects" / "-tmp-repo_claude"
    _write_jsonl(proj / "sess-c1.jsonl", [
        {"type": "user", "uuid": "u1", "cwd": str(cwd), "gitBranch": "main",
         "timestamp": "2026-06-01T00:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello claude"}]}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-06-01T00:00:01Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "hi there"}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
    ])
    (home / "tasks" / "sess-c1").mkdir(parents=True)
    (home / "tasks" / "sess-c1" / "1.json").write_text(
        json.dumps({"id": "1", "subject": "do x", "status": "pending"}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


@pytest.fixture
def gemini_home(tmp_path, monkeypatch):
    home = tmp_path / "gemini"
    cwd = tmp_path / "repo_gemini"
    cwd.mkdir()
    (cwd / "GEMINI.md").write_text("# Gemini project doc\nbeta\n", encoding="utf-8")
    proj = home / "tmp" / "repo_gemini"
    (proj / "chats").mkdir(parents=True)
    (proj / ".project_root").write_text(str(cwd), encoding="utf-8")
    sess = proj / "chats" / "session-2026-06-03T23-38-ab4cc0f2.jsonl"
    sess.write_text("\n".join([
        json.dumps({"sessionId": "ab4cc0f2", "startTime": "2026-06-03T23:38:00Z",
                    "lastUpdated": "2026-06-03T23:38:10Z", "kind": "main"}),
        json.dumps({"$set": {"messages": [
            {"id": "m1", "timestamp": "2026-06-03T23:38:03Z", "type": "user",
             "content": [{"text": "hello gemini"}]},
            {"id": "m2", "timestamp": "2026-06-03T23:38:05Z", "type": "gemini",
             "content": [{"text": "hello from gemini"}]},
        ]}}),
    ]), encoding="utf-8")
    # history fallback for empty tmp .project_root case
    histroot = home / "history" / "repo_gemini"
    histroot.mkdir(parents=True)
    (histroot / ".project_root").write_text(str(cwd), encoding="utf-8")
    monkeypatch.setenv("GEMINI_HOME", str(home))
    return home


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    cwd = tmp_path / "repo_codex"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("# Codex agents doc\ngamma\n", encoding="utf-8")
    sess = home / "sessions" / "2026" / "06" / "03" / "rollout-2026-06-03-xyz.jsonl"
    _write_jsonl(sess, [
        {"type": "user", "uuid": "cu1", "cwd": str(cwd), "gitBranch": "main",
         "timestamp": "2026-06-03T10:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello codex"}]}},
        {"type": "assistant", "uuid": "ca1", "timestamp": "2026-06-03T10:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi from codex"}]}},
    ])
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def empty_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex_empty"
    (home / "tmp").mkdir(parents=True)
    (home / "config.toml").write_text("[features]\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def copilot_home(tmp_path, monkeypatch):
    home = tmp_path / "copilot"
    (home / "ide").mkdir(parents=True)
    monkeypatch.setenv("COPILOT_HOME", str(home))
    return home
```

- [ ] **Step 5: Verify pytest collects with no errors**

Run: `python -m pytest tests/ -q`
Expected: "no tests ran" (collected 0 items) — fixtures import cleanly, no errors.

- [ ] **Step 6: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py
git commit -m "test: add pytest harness with synthetic per-agent home fixtures"
```

---

## Task 2: Shared helpers module (`common.py`)

Moves agent-neutral logic out of `store.py` so adapters reuse one copy. This task COPIES the helpers into `common.py` (does not yet delete from store.py — that happens in Task 3).

**Files:**
- Create: `backend/agents/__init__.py` (empty for now — package marker)
- Create: `backend/agents/common.py`
- Test: `tests/test_common.py`

- [ ] **Step 1: Create package marker**

Create `backend/agents/__init__.py` as an empty file (its real contents arrive in Task 4).

- [ ] **Step 2: Write the failing test**

Create `tests/test_common.py`:

```python
from pathlib import Path

from backend.agents import common


def test_context_limit_known_only_above_200k():
    assert common.context_limit_for(50_000) == {"limit": 200_000, "known": False}
    assert common.context_limit_for(250_000) == {"limit": 1_000_000, "known": True}


def test_iter_jsonl_skips_blank_and_bad_lines(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text('{"a":1}\n\nnot json\n{"b":2}\n', encoding="utf-8")
    rows = list(common.iter_jsonl(f))
    assert rows == [{"a": 1}, {"b": 2}]


def test_block_text_joins_text_blocks():
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert "hello" in common.block_text(content)
    assert "world" in common.block_text(content)


def test_structured_blocks_marks_tool_result():
    content = [{"type": "tool_result", "content": "out"}]
    blocks = common.structured_blocks(content)
    assert blocks[0]["type"] == "tool_result"


def test_save_doc_rejects_escape_and_writes(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    out = common.save_doc_file(str(base), "AGENTS.md", "body")
    assert out == base / "AGENTS.md"
    assert (base / "AGENTS.md").read_text() == "body"


def test_save_doc_missing_cwd_raises():
    import pytest
    with pytest.raises(FileNotFoundError):
        common.save_doc_file("", "AGENTS.md", "x")


def test_read_doc_file_absent(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    res = common.read_doc_file(str(base), "AGENTS.md")
    assert res["exists"] is False
    assert res["cwd"] == str(base)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_common.py -q`
Expected: FAIL — `ModuleNotFoundError` / attribute errors (common.py not implemented).

- [ ] **Step 4: Write `common.py`**

Create `backend/agents/common.py` (port the existing store.py logic; keep behavior identical):

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_common.py -q`
Expected: PASS (7 passed).

- [ ] **Step 6: Commit**

```bash
git add backend/agents/__init__.py backend/agents/common.py tests/test_common.py
git commit -m "feat: add agent-neutral common helpers module"
```

---

## Task 3: Adapter ABC, Capabilities, registry

**Files:**
- Modify: `backend/agents/__init__.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry.py`:

```python
import pytest

from backend.agents import (
    AgentAdapter, Capabilities, UnsupportedCapability, get_adapter, list_agents,
)


def test_list_agents_returns_four_known_ids():
    ids = {a["agent_id"] for a in list_agents()}
    assert ids == {"claude", "gemini", "codex", "copilot"}


def test_get_adapter_defaults_to_claude_on_unknown():
    a = get_adapter("nope")
    assert a.capabilities.agent_id == "claude"


def test_get_adapter_none_defaults_to_claude():
    assert get_adapter(None).capabilities.agent_id == "claude"


def test_capabilities_shape():
    caps = get_adapter("gemini").capabilities
    assert caps.doc_filename == "GEMINI.md"
    assert caps.has_tasks is False
    assert caps.can_edit_doc is True


def test_base_unsupported_methods_raise():
    class Bare(AgentAdapter):
        capabilities = Capabilities(
            agent_id="bare", label="Bare", doc_filename="X.md",
            has_memory=False, has_tasks=False, can_edit_doc=True,
            can_delete=False, can_export=False)
        def list_projects(self): return []
        def list_sessions(self, project): return []
        def get_conversation(self, project, session_id, offset=0, limit=None):
            return {"total": 0, "offset": offset, "limit": limit, "turns": []}
        def iter_turns(self, project, session_id):
            return iter(())
    b = Bare()
    with pytest.raises(UnsupportedCapability):
        b.get_tasks("s")
    with pytest.raises(UnsupportedCapability):
        b.delete_session("p", "s")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_registry.py -q`
Expected: FAIL — ImportError (names not defined).

- [ ] **Step 3: Implement `backend/agents/__init__.py`**

Replace the empty file with:

```python
"""Agent adapter protocol + registry.

Each adapter knows one coding agent's on-disk layout. app.py routes requests
to an adapter selected by ?agent=. Default is claude.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .common import SessionSummary  # re-export-friendly


class UnsupportedCapability(Exception):
    """Raised when an adapter is asked for a feature it doesn't support."""


@dataclass(frozen=True)
class Capabilities:
    agent_id: str
    label: str
    doc_filename: str
    has_memory: bool
    has_tasks: bool
    can_edit_doc: bool
    can_delete: bool
    can_export: bool


class AgentAdapter(ABC):
    capabilities: Capabilities

    # ---- READ (required) ----
    @abstractmethod
    def list_projects(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def list_sessions(self, project: str) -> list[SessionSummary]: ...

    @abstractmethod
    def get_conversation(self, project: str, session_id: str,
                         offset: int = 0, limit: int | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def iter_turns(self, project: str, session_id: str) -> Iterator[dict[str, Any]]: ...

    # ---- doc (all adapters: read; write gated by can_edit_doc) ----
    def get_doc(self, project: str) -> dict[str, Any]:
        raise UnsupportedCapability("get_doc not implemented")

    def save_doc(self, project: str, content: str) -> Path:
        raise UnsupportedCapability("doc editing not supported")

    # ---- Claude-only extras (base raises) ----
    def get_memory(self, project: str) -> dict[str, Any]:
        raise UnsupportedCapability("memory not supported")

    def save_memory_file(self, project: str, name: str, content: str) -> Path:
        raise UnsupportedCapability("memory not supported")

    def get_tasks(self, session_id: str) -> list[dict[str, Any]]:
        raise UnsupportedCapability("tasks not supported")

    def project_tasks(self, project: str) -> list[dict[str, Any]]:
        raise UnsupportedCapability("tasks not supported")

    def update_task_status(self, session_id: str, task_id: str, status: str) -> dict[str, Any]:
        raise UnsupportedCapability("tasks not supported")

    def export_session_markdown(self, project: str, session_id: str) -> str:
        raise UnsupportedCapability("export not supported")

    def export_session_to_file(self, project: str, session_id: str, out_dir=None) -> Path:
        raise UnsupportedCapability("export not supported")

    def save_session_as_memory(self, project: str, session_id: str) -> Path:
        raise UnsupportedCapability("memory not supported")

    def delete_session(self, project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
        raise UnsupportedCapability("delete not supported")


# Registry is populated at import time below.
_REGISTRY: dict[str, AgentAdapter] = {}


def _register(adapter: AgentAdapter) -> None:
    _REGISTRY[adapter.capabilities.agent_id] = adapter


def get_adapter(agent_id: str | None) -> AgentAdapter:
    return _REGISTRY.get(agent_id or "claude", _REGISTRY["claude"])


def all_adapters() -> list[AgentAdapter]:
    return list(_REGISTRY.values())


def list_agents() -> list[dict[str, Any]]:
    return [asdict(a.capabilities) for a in _REGISTRY.values()]


# Import + register concrete adapters (after class defs to avoid cycles).
from .claude import ClaudeAdapter      # noqa: E402
from .gemini import GeminiAdapter      # noqa: E402
from .codex import CodexAdapter        # noqa: E402
from .copilot import CopilotAdapter    # noqa: E402

_register(ClaudeAdapter())
_register(GeminiAdapter())
_register(CodexAdapter())
_register(CopilotAdapter())
```

NOTE: this file imports the four adapter modules. They are created in Tasks 4–7. Until Task 7 lands, `python -m pytest tests/test_registry.py` will fail to import. Run Task 3's test only AFTER Tasks 4–7 are complete, OR temporarily stub the imports. To keep TDD honest, implement Tasks 4–7 first, then return to run Step 4 below. (Subagent-driven execution: treat Tasks 3–7 as a unit — write 3's code last among them.)

- [ ] **Step 4: Run test to verify it passes (after Tasks 4–7)**

Run: `python -m pytest tests/test_registry.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/__init__.py tests/test_registry.py
git commit -m "feat: add AgentAdapter ABC, Capabilities, and registry"
```

---

## Task 4: ClaudeAdapter (full features)

Wraps the existing store.py logic behind the adapter interface. Reuses `backend/store.py` functions to avoid duplicating the large, already-tested Claude logic — the adapter is a thin delegation layer.

**Files:**
- Create: `backend/agents/claude.py`
- Test: `tests/test_claude_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_adapter.py`:

```python
from backend.agents.claude import ClaudeAdapter


def test_caps(claude_home):
    a = ClaudeAdapter()
    c = a.capabilities
    assert c.agent_id == "claude"
    assert c.doc_filename == "CLAUDE.md"
    assert c.has_tasks and c.has_memory and c.can_delete and c.can_export


def test_list_projects_and_sessions(claude_home):
    a = ClaudeAdapter()
    projs = a.list_projects()
    assert len(projs) == 1
    pname = projs[0]["name"]
    sessions = a.list_sessions(pname)
    assert sessions[0].session_id == "sess-c1"
    assert sessions[0].agent == "claude"


def test_get_conversation_normalized(claude_home):
    a = ClaudeAdapter()
    pname = a.list_projects()[0]["name"]
    conv = a.get_conversation(pname, "sess-c1")
    assert conv["total"] == 2
    assert conv["turns"][0]["role"] == "user"


def test_get_and_save_doc(claude_home):
    a = ClaudeAdapter()
    pname = a.list_projects()[0]["name"]
    doc = a.get_doc(pname)
    assert doc["exists"] and "alpha" in doc["content"]
    a.save_doc(pname, "# new\nzeta\n")
    assert "zeta" in a.get_doc(pname)["content"]


def test_tasks(claude_home):
    a = ClaudeAdapter()
    tasks = a.get_tasks("sess-c1")
    assert tasks[0]["subject"] == "do x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claude_adapter.py -q`
Expected: FAIL — ModuleNotFoundError (claude.py missing).

- [ ] **Step 3: Implement `backend/agents/claude.py`**

```python
"""ClaudeAdapter — delegates to the existing store.py Claude logic."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from .. import store
from . import AgentAdapter, Capabilities
from .common import SessionSummary


class ClaudeAdapter(AgentAdapter):
    capabilities = Capabilities(
        agent_id="claude", label="Claude", doc_filename="CLAUDE.md",
        has_memory=True, has_tasks=True, can_edit_doc=True,
        can_delete=True, can_export=True,
    )

    def list_projects(self) -> list[dict[str, Any]]:
        return store.list_projects()

    def list_sessions(self, project: str) -> list[SessionSummary]:
        out = []
        for s in store.list_sessions(project):
            s.agent = "claude"
            out.append(s)
        return out

    def get_conversation(self, project, session_id, offset=0, limit=None):
        return store.get_conversation(project, session_id, offset=offset, limit=limit)

    def iter_turns(self, project, session_id) -> Iterator[dict[str, Any]]:
        data = store.get_conversation(project, session_id, offset=0, limit=None)
        for seq, turn in enumerate(data["turns"]):
            yield {"seq": seq, "role": turn["role"], "kind": turn["kind"],
                   "timestamp": turn.get("timestamp", ""), "blocks": turn["blocks"]}

    def get_doc(self, project):
        return store.get_claude_md(project)

    def save_doc(self, project, content):
        return store.save_claude_md(project, content)

    def get_memory(self, project):
        return store.get_memory(project)

    def save_memory_file(self, project, name, content):
        return store.save_memory_file(project, name, content)

    def get_tasks(self, session_id):
        return store.get_tasks(session_id)

    def project_tasks(self, project):
        return store.project_tasks(project)

    def update_task_status(self, session_id, task_id, status):
        return store.update_task_status(session_id, task_id, status)

    def export_session_markdown(self, project, session_id):
        return store.export_session_markdown(project, session_id)

    def export_session_to_file(self, project, session_id, out_dir=None):
        return store.export_session_to_file(project, session_id, out_dir)

    def save_session_as_memory(self, project, session_id):
        return store.save_session_as_memory(project, session_id)

    def delete_session(self, project, session_id, hard=False):
        return store.delete_session(project, session_id, hard=hard)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_claude_adapter.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/claude.py tests/test_claude_adapter.py
git commit -m "feat: add ClaudeAdapter delegating to store.py"
```

---

## Task 5: GeminiAdapter (event-sourced transcript)

**Files:**
- Create: `backend/agents/gemini.py`
- Test: `tests/test_gemini_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gemini_adapter.py`:

```python
from backend.agents.gemini import GeminiAdapter


def test_caps(gemini_home):
    c = GeminiAdapter().capabilities
    assert c.agent_id == "gemini"
    assert c.doc_filename == "GEMINI.md"
    assert c.can_edit_doc is True
    assert c.has_tasks is False and c.has_memory is False
    assert c.can_delete is False and c.can_export is False


def test_list_projects_uses_project_root(gemini_home):
    projs = GeminiAdapter().list_projects()
    assert len(projs) == 1
    assert projs[0]["name"] == "repo_gemini"
    assert projs[0]["cwd"].endswith("repo_gemini")


def test_list_sessions(gemini_home):
    a = GeminiAdapter()
    pname = a.list_projects()[0]["name"]
    sessions = a.list_sessions(pname)
    assert len(sessions) == 1
    assert sessions[0].agent == "gemini"
    assert sessions[0].cwd.endswith("repo_gemini")


def test_get_conversation_folds_event_messages(gemini_home):
    a = GeminiAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    conv = a.get_conversation(pname, sid)
    assert conv["total"] == 2
    roles = [t["role"] for t in conv["turns"]]
    assert roles[0] == "user" and roles[1] == "assistant"
    assert "hello gemini" in conv["turns"][0]["blocks"][0]["text"]
    assert "hello from gemini" in conv["turns"][1]["blocks"][0]["text"]


def test_iter_turns_yields_text(gemini_home):
    a = GeminiAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    texts = [t["blocks"][0]["text"] for t in a.iter_turns(pname, sid)]
    assert any("gemini" in t for t in texts)


def test_get_and_save_doc(gemini_home):
    a = GeminiAdapter()
    pname = a.list_projects()[0]["name"]
    assert "beta" in a.get_doc(pname)["content"]
    a.save_doc(pname, "# g\nomega\n")
    assert "omega" in a.get_doc(pname)["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gemini_adapter.py -q`
Expected: FAIL — ModuleNotFoundError (gemini.py missing).

- [ ] **Step 3: Implement `backend/agents/gemini.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gemini_adapter.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/gemini.py tests/test_gemini_adapter.py
git commit -m "feat: add GeminiAdapter with event-sourced transcript parser"
```

---

## Task 6: CodexAdapter (rollout sessions)

**Files:**
- Create: `backend/agents/codex.py`
- Test: `tests/test_codex_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_adapter.py`:

```python
from backend.agents.codex import CodexAdapter


def test_caps(codex_home):
    c = CodexAdapter().capabilities
    assert c.agent_id == "codex"
    assert c.doc_filename == "AGENTS.md"
    assert c.can_edit_doc is True
    assert c.has_tasks is False and c.can_delete is False


def test_list_projects_groups_by_cwd(codex_home):
    projs = CodexAdapter().list_projects()
    assert len(projs) == 1
    assert projs[0]["cwd"].endswith("repo_codex")


def test_list_sessions(codex_home):
    a = CodexAdapter()
    pname = a.list_projects()[0]["name"]
    sessions = a.list_sessions(pname)
    assert len(sessions) == 1
    assert sessions[0].agent == "codex"


def test_get_conversation(codex_home):
    a = CodexAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    conv = a.get_conversation(pname, sid)
    assert conv["total"] == 2
    assert "hello codex" in conv["turns"][0]["blocks"][0]["text"]


def test_empty_home_returns_no_projects(empty_codex_home):
    assert CodexAdapter().list_projects() == []


def test_get_and_save_doc(codex_home):
    a = CodexAdapter()
    pname = a.list_projects()[0]["name"]
    assert "gamma" in a.get_doc(pname)["content"]
    a.save_doc(pname, "# c\ndelta\n")
    assert "delta" in a.get_doc(pname)["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_codex_adapter.py -q`
Expected: FAIL — ModuleNotFoundError (codex.py missing).

- [ ] **Step 3: Implement `backend/agents/codex.py`**

Codex stores turn-per-line JSONL rollouts under `sessions/YYYY/MM/DD/rollout-*.jsonl`, each record carrying `cwd`. We group sessions into "projects" keyed by the mangled cwd (separators → `-`), so the UI shows one project per working directory like Claude. A project "name" is the mangled cwd; the real cwd comes from the session records.

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_codex_adapter.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/codex.py tests/test_codex_adapter.py
git commit -m "feat: add CodexAdapter for rollout sessions grouped by cwd"
```

---

## Task 7: CopilotAdapter (AGENTS.md; sessions when present)

Copilot has no CLI conversation dir on the target machine yet. The adapter ships and returns empty project/session lists gracefully. If a future Copilot version writes turn-per-line JSONL sessions under `~/.copilot/history/<mangled-cwd>/*.jsonl`, this adapter reads them like Codex; until then everything is empty-but-valid. Doc editing of `AGENTS.md` works whenever a project cwd is known.

**Files:**
- Create: `backend/agents/copilot.py`
- Test: `tests/test_copilot_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_copilot_adapter.py`:

```python
from backend.agents.copilot import CopilotAdapter


def test_caps(copilot_home):
    c = CopilotAdapter().capabilities
    assert c.agent_id == "copilot"
    assert c.doc_filename == "AGENTS.md"
    assert c.can_edit_doc is True
    assert c.has_tasks is False and c.can_delete is False


def test_empty_home_is_graceful(copilot_home):
    a = CopilotAdapter()
    assert a.list_projects() == []
    assert a.list_sessions("anything") == []
    conv = a.get_conversation("p", "s")
    assert conv == {"total": 0, "offset": 0, "limit": None, "turns": []}
    assert list(a.iter_turns("p", "s")) == []


def test_reads_history_when_present(copilot_home):
    import json
    cwd = copilot_home.parent / "repo_copilot"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("# copilot doc\nkappa\n", encoding="utf-8")
    proj = copilot_home / "history" / "-tmp-repo_copilot"
    proj.mkdir(parents=True)
    (proj / "sess-p1.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "uuid": "pu1", "cwd": str(cwd),
                    "timestamp": "2026-06-04T00:00:00Z",
                    "message": {"role": "user",
                                "content": [{"type": "text", "text": "hi copilot"}]}}),
        json.dumps({"type": "assistant", "uuid": "pa1",
                    "timestamp": "2026-06-04T00:00:01Z",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "hello!"}]}}),
    ]), encoding="utf-8")
    a = CopilotAdapter()
    projs = a.list_projects()
    assert len(projs) == 1
    sid = a.list_sessions(projs[0]["name"])[0].session_id
    assert "hi copilot" in a.get_conversation(projs[0]["name"], sid)["turns"][0]["blocks"][0]["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_copilot_adapter.py -q`
Expected: FAIL — ModuleNotFoundError (copilot.py missing).

- [ ] **Step 3: Implement `backend/agents/copilot.py`**

The reader logic mirrors Claude's turn-per-line layout. It uses `~/.copilot/history/<mangled-cwd>/*.jsonl` if present; the folder name is the mangled cwd and the true cwd is peeked from records (like Codex).

```python
"""CopilotAdapter — ~/.copilot/history/<mangled-cwd>/*.jsonl when present.

No CLI conversation history exists on machines that only used the IDE plugin,
so all readers return empty there. Turn-per-line JSONL like Claude/Codex.
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


def copilot_home() -> Path:
    env = os.environ.get("COPILOT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".copilot"


def _peek_cwd(path: Path) -> str:
    for rec in iter_jsonl(path):
        if rec.get("cwd"):
            return rec["cwd"]
    return ""


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

    def list_projects(self) -> list[dict[str, Any]]:
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
            cwd = _peek_cwd(sorted(sessions, key=lambda s: s.stat().st_mtime,
                                   reverse=True)[0])
            mtimes = [s.stat().st_mtime for s in sessions]
            out.append({
                "name": d.name,
                "display_path": cwd or d.name,
                "cwd": cwd,
                "session_count": len(sessions),
                "has_memory": False,
                "has_claude_md": bool(cwd and (Path(cwd) / "AGENTS.md").is_file()),
                "open_tasks": 0, "total_tasks": 0,
                "git": read_git_info(cwd),
                "mtime": max(mtimes),
            })
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def _project_dir(self, project: str) -> Path:
        return self._history_root() / project

    def list_sessions(self, project: str) -> list[SessionSummary]:
        d = self._project_dir(project)
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
            summ = SessionSummary(
                session_id=s.stem, project=project, file=str(s),
                size_bytes=st.st_size, mtime=st.st_mtime,
                first_prompt=first, last_prompt=last, message_count=msg_count,
                user_turns=user_turns, assistant_turns=asst_turns,
                cwd=cwd, git_branch=branch, agent="copilot",
            )
            win = context_limit_for(0)
            summ.context_limit = win["limit"]
            summ.context_limit_known = win["known"]
            out.append(summ)
        out.sort(key=lambda x: x.mtime, reverse=True)
        return out

    def get_conversation(self, project, session_id, offset=0, limit=None):
        path = self._project_dir(project) / f"{session_id}.jsonl"
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
        d = self._project_dir(project)
        if not d.is_dir():
            return ""
        sessions = sorted(d.glob("*.jsonl"), key=lambda s: s.stat().st_mtime,
                          reverse=True)
        return _peek_cwd(sessions[0]) if sessions else ""

    def get_doc(self, project):
        return read_doc_file(self._project_cwd(project), "AGENTS.md")

    def save_doc(self, project, content):
        return save_doc_file(self._project_cwd(project), "AGENTS.md", content)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_copilot_adapter.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the registry test now that all adapters exist**

Run: `python -m pytest tests/test_registry.py -q`
Expected: PASS (5 passed) — completes Task 3 Step 4.

- [ ] **Step 6: Commit**

```bash
git add backend/agents/copilot.py tests/test_copilot_adapter.py
git commit -m "feat: add CopilotAdapter (empty-but-valid until history exists)"
```

---

## Task 8: Confirm store.py back-compat shim

`backend/store.py` keeps all existing functions (ClaudeAdapter delegates to them, so no logic moved out). This task only verifies the public seam used elsewhere stays intact and adds the env-var note already present. No code change expected unless the full suite reveals a gap.

**Files:**
- Test: `tests/test_store_compat.py`

- [ ] **Step 1: Write the test**

Create `tests/test_store_compat.py`:

```python
from backend import store


def test_store_still_exposes_claude_home_and_dirs(claude_home):
    assert store.claude_home() == claude_home
    assert store.projects_dir() == claude_home / "projects"
    assert store.tasks_dir() == claude_home / "tasks"


def test_store_list_projects_unchanged(claude_home):
    projs = store.list_projects()
    assert len(projs) == 1
    assert projs[0]["has_claude_md"] is True
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/test_store_compat.py -q`
Expected: PASS (2 passed). If FAIL, do NOT modify store.py logic — investigate the fixture/env mismatch and fix the test setup.

- [ ] **Step 3: Commit**

```bash
git add tests/test_store_compat.py
git commit -m "test: lock store.py back-compat seam"
```

---

## Task 9: Index `agent` column + adapter-driven reindex/search

**Files:**
- Modify: `backend/index_db.py` (schema, `reindex`, `_index_docs`, `search`, `_fts_search`, `_like_search`)
- Test: `tests/test_index_agent.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_index_agent.py`:

```python
from backend import index_db


def test_reindex_all_agents_and_scoped_search(claude_home, gemini_home,
                                               codex_home, monkeypatch, tmp_path):
    # point the index db at a tmp file so we don't touch the real cache
    monkeypatch.setattr(index_db, "db_path", lambda: tmp_path / "idx.db")
    stats = index_db.reindex(force=True)
    assert stats["indexed"] >= 3  # at least one session per active agent

    # search scoped to gemini only returns gemini hits
    res = index_db.search("hello", agent="gemini")
    assert res
    assert all(r.get("agent") == "gemini" for r in res)

    # search scoped to claude returns claude hits, tagged
    res_c = index_db.search("hello", agent="claude")
    assert res_c and all(r.get("agent") == "claude" for r in res_c)

    # doc search finds the gemini root doc
    res_doc = index_db.search("beta", agent="gemini")
    assert any(r.get("source") == "agent_doc" for r in res_doc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_index_agent.py -q`
Expected: FAIL — `search()` has no `agent` kwarg / no `agent` column.

- [ ] **Step 3: Bump schema and add `agent` to tables**

In `backend/index_db.py`, change `SCHEMA_VERSION = 2` to `SCHEMA_VERSION = 3`.

Then update the `init_db` table definitions. Replace the `sessions`, `turns`, and `docs` CREATE statements and the two FTS CREATE statements with these (adds `agent`):

```python
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            agent      TEXT,
            session_id TEXT,
            project    TEXT,
            mtime      REAL,
            size       INTEGER,
            turns      INTEGER,
            context_tokens INTEGER,
            last_prompt TEXT,
            PRIMARY KEY (agent, session_id)
        );
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent      TEXT,
            session_id TEXT,
            project    TEXT,
            seq        INTEGER,
            role       TEXT,
            kind       TEXT,
            timestamp  TEXT,
            text       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(agent, session_id);
        CREATE TABLE IF NOT EXISTS docs (
            agent   TEXT,
            project TEXT,
            source  TEXT,
            ref     TEXT,
            path    TEXT,
            text    TEXT,
            mtime   REAL,
            PRIMARY KEY (agent, project, source, ref)
        );
        """
    )
```

And the FTS tables:

```python
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
            "text, agent UNINDEXED, session_id UNINDEXED, project UNINDEXED, "
            "seq UNINDEXED, role UNINDEXED, turn_id UNINDEXED)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
            "text, agent UNINDEXED, project UNINDEXED, source UNINDEXED, "
            "ref UNINDEXED, path UNINDEXED)"
        )
```

Also update the FTS drop on schema change to drop both (already drops both — leave as is).

- [ ] **Step 4: Rewrite `reindex` to loop adapters**

Replace the body of `reindex` with an adapter loop. Import the registry at the top of `index_db.py`: add `from .agents import all_adapters` near the existing `from . import store`.

```python
def reindex(force: bool = False) -> dict[str, Any]:
    """Walk every agent's projects/sessions and (re)index changed ones."""
    conn = _connect()
    fts = init_db(conn)
    indexed = skipped = total_turns = total_docs = 0

    for adapter in all_adapters():
        agent = adapter.capabilities.agent_id
        for proj in adapter.list_projects():
            pname = proj["name"]
            for summ in adapter.list_sessions(pname):
                row = conn.execute(
                    "SELECT mtime, size FROM sessions WHERE agent=? AND session_id=?",
                    (agent, summ.session_id),
                ).fetchone()
                if (row and not force and row["mtime"] == summ.mtime
                        and row["size"] == summ.size_bytes):
                    skipped += 1
                    continue

                conn.execute("DELETE FROM turns WHERE agent=? AND session_id=?",
                             (agent, summ.session_id))
                if fts:
                    conn.execute("DELETE FROM turns_fts WHERE agent=? AND session_id=?",
                                 (agent, summ.session_id))

                for turn in adapter.iter_turns(pname, summ.session_id):
                    text = _turn_text(turn)
                    if not text.strip():
                        continue
                    seq = turn["seq"]
                    cur = conn.execute(
                        "INSERT INTO turns(agent, session_id, project, seq, role, "
                        "kind, timestamp, text) VALUES(?,?,?,?,?,?,?,?)",
                        (agent, summ.session_id, pname, seq, turn["role"],
                         turn["kind"], turn.get("timestamp", ""), text),
                    )
                    if fts:
                        conn.execute(
                            "INSERT INTO turns_fts(text, agent, session_id, project, "
                            "seq, role, turn_id) VALUES(?,?,?,?,?,?,?)",
                            (text, agent, summ.session_id, pname, seq,
                             turn["role"], cur.lastrowid),
                        )
                    total_turns += 1

                conn.execute(
                    "INSERT OR REPLACE INTO sessions(agent, session_id, project, "
                    "mtime, size, turns, context_tokens, last_prompt) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (agent, summ.session_id, pname, summ.mtime, summ.size_bytes,
                     summ.message_count, summ.context_tokens, summ.last_prompt),
                )
                indexed += 1

            total_docs += _index_docs(conn, fts, adapter, pname)
            conn.commit()

    conn.commit()
    conn.close()
    return {"indexed": indexed, "skipped": skipped, "turns": total_turns,
            "docs": total_docs, "fts": fts}
```

Note: `_turn_text` already reads `turn["blocks"]`; `iter_turns` yields blocks, so it works unchanged.

- [ ] **Step 5: Rewrite `_index_docs` to be adapter-aware**

```python
def _index_docs(conn, fts, adapter, project) -> int:
    """Index an agent's project docs: Claude memory + its root doc; others one root doc."""
    agent = adapter.capabilities.agent_id
    conn.execute("DELETE FROM docs WHERE agent=? AND project=?", (agent, project))
    if fts:
        conn.execute("DELETE FROM docs_fts WHERE agent=? AND project=?", (agent, project))
    count = 0
    entries: list[tuple[str, str, str, str]] = []  # (source, ref, path, text)

    caps = adapter.capabilities
    if caps.has_memory:
        mem = adapter.get_memory(project)
        if mem.get("index"):
            entries.append(("memory", "MEMORY.md", mem.get("index_path", ""), mem["index"]))
        for f in mem.get("files", []):
            entries.append(("memory", f["name"], f.get("path", ""), f.get("content", "")))

    doc = adapter.get_doc(project)
    if doc.get("exists") and doc.get("content"):
        # claude keeps its historical source name; others use 'agent_doc'
        source = "claude_md" if agent == "claude" else "agent_doc"
        entries.append((source, caps.doc_filename, doc.get("path", ""), doc["content"]))

    for source, ref, path, text in entries:
        if not text.strip():
            continue
        conn.execute(
            "INSERT OR REPLACE INTO docs(agent, project, source, ref, path, text, mtime) "
            "VALUES(?,?,?,?,?,?,?)",
            (agent, project, source, ref, path, text, 0.0),
        )
        if fts:
            conn.execute(
                "INSERT INTO docs_fts(text, agent, project, source, ref, path) "
                "VALUES(?,?,?,?,?,?)",
                (text, agent, project, source, ref, path),
            )
        count += 1
    return count
```

- [ ] **Step 6: Add `agent` filtering to `search`**

Replace `search`, `_fts_search`, `_like_search` signatures and the SQL to thread `agent`:

```python
def search(query, limit=50, project=None, agent=None):
    conn = _connect()
    fts = init_db(conn)
    try:
        if fts:
            try:
                out = _fts_search(conn, query, limit, project, agent)
            except sqlite3.OperationalError:
                out = _like_search(conn, query, limit, project, agent)
        else:
            out = _like_search(conn, query, limit, project, agent)
    finally:
        conn.close()
    return out


def _fts_search(conn, query, limit, project, agent=None):
    out = []
    clauses, args = [], []
    if project:
        clauses.append("project = ?"); args.append(project)
    if agent:
        clauses.append("agent = ?"); args.append(agent)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""

    turn_rows = conn.execute(
        "SELECT agent, session_id, project, seq, role, "
        "snippet(turns_fts, 0, '[', ']', ' … ', 12) AS snippet "
        f"FROM turns_fts WHERE turns_fts MATCH ?{extra} ORDER BY rank LIMIT ?",
        (query, *args, limit),
    ).fetchall()
    for r in turn_rows:
        d = dict(r); d["source"] = "conversation"; out.append(d)

    doc_rows = conn.execute(
        "SELECT agent, project, source, ref, path, "
        "snippet(docs_fts, 0, '[', ']', ' … ', 12) AS snippet "
        f"FROM docs_fts WHERE docs_fts MATCH ?{extra} ORDER BY rank LIMIT ?",
        (query, *args, limit),
    ).fetchall()
    for r in doc_rows:
        out.append(dict(r))
    return out


def _like_search(conn, query, limit, project=None, agent=None):
    like = f"%{query}%"
    out = []
    clauses, args = [], []
    if project:
        clauses.append("project = ?"); args.append(project)
    if agent:
        clauses.append("agent = ?"); args.append(agent)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""

    turn_rows = conn.execute(
        "SELECT agent, session_id, project, seq, role, "
        "substr(text, 1, 200) AS snippet FROM turns "
        f"WHERE text LIKE ?{extra} LIMIT ?",
        (like, *args, limit),
    ).fetchall()
    for r in turn_rows:
        d = dict(r); d["source"] = "conversation"; out.append(d)

    doc_rows = conn.execute(
        "SELECT agent, project, source, ref, path, "
        "substr(text, 1, 200) AS snippet FROM docs "
        f"WHERE text LIKE ?{extra} LIMIT ?",
        (like, *args, limit),
    ).fetchall()
    for r in doc_rows:
        out.append(dict(r))
    return out
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_index_agent.py -q`
Expected: PASS (1 passed).

- [ ] **Step 8: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add backend/index_db.py tests/test_index_agent.py
git commit -m "feat: per-agent search index (agent column, adapter-driven reindex)"
```

---

## Task 10: Route requests through adapters + `/api/agents`

**Files:**
- Modify: `backend/app.py`
- Test: `tests/test_app_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_routes.py`:

```python
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_agents_endpoint_lists_four(claude_home):
    r = client.get("/api/agents")
    assert r.status_code == 200
    ids = {a["agent_id"] for a in r.json()}
    assert ids == {"claude", "gemini", "codex", "copilot"}


def test_projects_default_claude(claude_home):
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_projects_gemini(gemini_home, claude_home):
    r = client.get("/api/projects", params={"agent": "gemini"})
    assert r.status_code == 200
    assert r.json()[0]["name"] == "repo_gemini"


def test_doc_get_and_put_gemini(gemini_home, claude_home):
    pname = client.get("/api/projects", params={"agent": "gemini"}).json()[0]["name"]
    g = client.get(f"/api/projects/{pname}/doc", params={"agent": "gemini"})
    assert g.status_code == 200 and "beta" in g.json()["content"]
    p = client.put(f"/api/projects/{pname}/doc", params={"agent": "gemini"},
                   json={"content": "# g\nsigma\n"})
    assert p.status_code == 200
    assert "sigma" in client.get(f"/api/projects/{pname}/doc",
                                 params={"agent": "gemini"}).json()["content"]


def test_unsupported_tasks_on_gemini_404(gemini_home):
    pname = client.get("/api/projects", params={"agent": "gemini"}).json()[0]["name"]
    r = client.get(f"/api/projects/{pname}/tasks", params={"agent": "gemini"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app_routes.py -q`
Expected: FAIL — `/api/agents` and `/doc` routes don't exist; routes still call `store` directly.

- [ ] **Step 3: Rewrite `backend/app.py` routes to use adapters**

Replace the imports and route bodies. Keep the existing `claude-md` and `memory` routes as aliases that forward to the doc/memory adapter methods, so the current frontend keeps working until Task 11 migrates it. Add the generic `/doc` routes and `/api/agents`. Full replacement of `app.py`:

```python
"""FastAPI app: read-only viewer over coding-agent local project data."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import index_db, store
from .agents import UnsupportedCapability, get_adapter, list_agents

app = FastAPI(title="cc_mgr", version="0.3.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _adapter(agent: str | None):
    return get_adapter(agent)


@app.get("/api/agents")
def api_agents():
    return list_agents()


@app.get("/api/projects")
def api_projects(agent: str | None = None):
    return _adapter(agent).list_projects()


@app.get("/api/projects/{project}/sessions")
def api_sessions(project: str, agent: str | None = None):
    return [asdict(s) for s in _adapter(agent).list_sessions(project)]


@app.get("/api/projects/{project}/doc")
def api_get_doc(project: str, agent: str | None = None):
    try:
        return _adapter(agent).get_doc(project)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


class DocRequest(BaseModel):
    content: str


@app.put("/api/projects/{project}/doc")
def api_save_doc(project: str, req: DocRequest, agent: str | None = None):
    try:
        path = _adapter(agent).save_doc(project, req.content)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"saved": str(path)}


# --- back-compat aliases for the current Claude-only frontend ---
@app.get("/api/projects/{project}/claude-md")
def api_get_claude_md(project: str, agent: str | None = None):
    return api_get_doc(project, agent)


@app.put("/api/projects/{project}/claude-md")
def api_save_claude_md(project: str, req: DocRequest, agent: str | None = None):
    return api_save_doc(project, req, agent)


@app.get("/api/projects/{project}/memory")
def api_memory(project: str, agent: str | None = None):
    try:
        return _adapter(agent).get_memory(project)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


class MemoryFileRequest(BaseModel):
    name: str
    content: str


@app.put("/api/projects/{project}/memory")
def api_save_memory_file(project: str, req: MemoryFileRequest, agent: str | None = None):
    try:
        path = _adapter(agent).save_memory_file(project, req.name, req.content)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"saved": str(path)}


@app.get("/api/projects/{project}/tasks")
def api_project_tasks(project: str, agent: str | None = None):
    try:
        return _adapter(agent).project_tasks(project)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/projects/{project}/sessions/{session_id}")
def api_conversation(project: str, session_id: str, offset: int = 0,
                     limit: int = 40, agent: str | None = None):
    result = _adapter(agent).get_conversation(project, session_id,
                                              offset=offset, limit=limit)
    if result["total"] == 0:
        raise HTTPException(status_code=404, detail="session not found or empty")
    result["session_id"] = session_id
    return result


@app.get("/api/sessions/{session_id}/tasks")
def api_tasks(session_id: str, agent: str | None = None):
    try:
        return _adapter(agent).get_tasks(session_id)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


class TaskStatusRequest(BaseModel):
    status: str


@app.patch("/api/sessions/{session_id}/tasks/{task_id}")
def api_update_task(session_id: str, task_id: str, req: TaskStatusRequest,
                    agent: str | None = None):
    try:
        return _adapter(agent).update_task_status(session_id, task_id, req.status)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/projects/{project}/sessions/{session_id}/export",
         response_class=PlainTextResponse)
def api_export_inline(project: str, session_id: str, agent: str | None = None):
    try:
        md = _adapter(agent).export_session_markdown(project, session_id)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not md.strip():
        raise HTTPException(status_code=404, detail="session not found or empty")
    return md


class DeleteRequest(BaseModel):
    export_first: bool = True
    save_memory: bool = False
    hard: bool = False


@app.post("/api/projects/{project}/sessions/{session_id}/delete")
def api_delete(project: str, session_id: str, req: DeleteRequest,
               agent: str | None = None):
    ad = _adapter(agent)
    try:
        export_path = memory_path = None
        if req.export_first:
            export_path = str(ad.export_session_to_file(project, session_id))
        if req.save_memory:
            memory_path = str(ad.save_session_as_memory(project, session_id))
        result = ad.delete_session(project, session_id, hard=req.hard)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    result["export"] = export_path
    result["memory"] = memory_path
    return result


@app.post("/api/projects/{project}/sessions/{session_id}/save-memory")
def api_save_memory(project: str, session_id: str, agent: str | None = None):
    try:
        return {"memory": str(_adapter(agent).save_session_as_memory(project, session_id))}
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")


@app.get("/api/search")
def api_search(q: str, limit: int = 50, project: str | None = None,
               agent: str | None = None):
    if not q.strip():
        return {"query": q, "results": []}
    return {"query": q, "project": project, "agent": agent,
            "results": index_db.search(q, limit=limit, project=project, agent=agent)}


@app.post("/api/reindex")
def api_reindex(force: bool = False):
    return index_db.reindex(force=force)


@app.get("/api/index/stats")
def api_index_stats():
    return index_db.stats()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app_routes.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add backend/app.py tests/test_app_routes.py
git commit -m "feat: route API through agent adapters; add /api/agents and /doc"
```

---

## Task 11: Frontend agent dropdown + routing

The single `api()` helper is the seam: appending `agent=state.agent` to every request routes the whole app. Capability flags from `/api/agents` drive which nav buttons/labels show.

**Files:**
- Modify: `frontend/index.html` (add dropdown; update title/sub text)
- Modify: `frontend/app.js` (state.agent, api() seam, capabilities, doc rename, nav gating, search param)
- Modify: `frontend/style.css` (add `.agentsel`)

- [ ] **Step 1: Add the dropdown to index.html**

In `frontend/index.html`, replace the brand + sub block (lines 12-13) with:

```html
    <div class="brand">cc_mgr</div>
    <select id="agentSel" class="agentsel" title="Active coding agent"></select>
    <div class="sub" id="subLabel">local coding-agent viewer</div>
```

Also change the `<title>` (line 6) to:

```html
  <title>cc_mgr — coding-agent project viewer</title>
```

- [ ] **Step 2: Add `state.agent`, capabilities, and the api() seam**

In `frontend/app.js`, add to the `state` object (after `convTotal: 0,`):

```js
  agent: localStorage.getItem("cc_agent") || "claude",
  agents: [],        // [{agent_id,label,doc_filename,has_memory,has_tasks,...}]
  caps: null,        // capabilities of the active agent
```

Replace the `api()` function (lines 50-54) with one that injects the agent param:

```js
async function api(path) {
  const u = new URL(path, location.origin);
  if (!u.searchParams.has("agent")) u.searchParams.set("agent", state.agent);
  const r = await fetch(u);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}
```

- [ ] **Step 3: Load agents and wire the dropdown (call during init)**

Add these functions in app.js (near `loadProjects`):

```js
function currentCaps() {
  return state.agents.find((a) => a.agent_id === state.agent) || null;
}

async function loadAgents() {
  try {
    state.agents = await fetch("/api/agents").then((r) => r.json());
  } catch {
    state.agents = [{ agent_id: "claude", label: "Claude", doc_filename: "CLAUDE.md",
      has_memory: true, has_tasks: true, can_edit_doc: true,
      can_delete: true, can_export: true }];
  }
  state.caps = currentCaps();
  const sel = $("#agentSel");
  sel.innerHTML = state.agents
    .map((a) => `<option value="${a.agent_id}">${esc(a.label)}</option>`).join("");
  sel.value = state.agent;
  sel.addEventListener("change", async () => {
    state.agent = sel.value;
    localStorage.setItem("cc_agent", state.agent);
    state.caps = currentCaps();
    // reset view to a clean slate for the new agent
    state.activeProject = null;
    state.activeProjectMeta = null;
    state.activeSession = null;
    state.sessions = [];
    closeDetail();
    $("#projNav").hidden = true;
    $("#sessionPane").innerHTML =
      '<div class="empty">Select a project to view its sessions.</div>';
    updateSubLabel();
    await loadProjects();
  });
  updateSubLabel();
}

function updateSubLabel() {
  const label = (state.caps && state.caps.label) || "coding-agent";
  const el = $("#subLabel");
  if (el) el.textContent = `local ${label} project viewer`;
}
```

- [ ] **Step 4: Gate nav buttons by capabilities**

Replace `renderProjNav`'s `views` array construction (lines 179-184) so Tasks/Memory hide when unsupported and the doc button uses the agent's filename:

```js
  const caps = state.caps || {};
  const docName = caps.doc_filename || "CLAUDE.md";
  const views = [
    { key: "sessions", label: "Sessions", count: `<span class="pn-count">${m.session_count || 0}</span>` },
  ];
  if (caps.has_tasks) views.push({ key: "tasks", label: "Tasks", count: taskCount });
  if (caps.has_memory) views.push({ key: "memory", label: "Memory", count: "" });
  views.push({ key: "claude", label: docName, count: "" });
```

If `state.projView` points to a now-hidden view (e.g. switched from Claude with `tasks` active to Gemini), reset it. Add at the top of `renderProjNav`, right after `nav.hidden = false;`:

```js
  const allowed = new Set(["sessions", "claude"]);
  if ((state.caps || {}).has_tasks) allowed.add("tasks");
  if ((state.caps || {}).has_memory) allowed.add("memory");
  if (!allowed.has(state.projView)) state.projView = "sessions";
```

- [ ] **Step 5: Make the doc editor generic (filename + endpoint)**

In `renderClaudeMd` (lines 308-341), replace the hardcoded `CLAUDE.md` strings and the PUT URL so they use the active agent's doc filename and the new `/doc` endpoint:

```js
function renderClaudeMd(el, cm) {
  const docName = (state.caps && state.caps.doc_filename) || "CLAUDE.md";
  if (!cm.cwd) {
    el.innerHTML = '<div class="empty">No working directory known for this project (no session has a cwd yet).</div>';
    return;
  }
  const status = cm.exists ? "" : `${docName} does not exist yet — saving will create it.`;
  el.innerHTML = `
    <div class="editor-wrap">
      <div class="editor-head">
        <strong>${esc(docName)}</strong>
        <span class="ehpath">${esc(cm.path)}</span>
      </div>
      <textarea class="editor" id="cmEditor" spellcheck="false">${esc(cm.content)}</textarea>
      <div class="editor-actions">
        <button class="dbtn" id="cmSave">Save</button>
        <span class="editor-status" id="cmStatus">${esc(status)}</span>
      </div>
    </div>`;
  $("#cmSave").addEventListener("click", async () => {
    $("#cmStatus").textContent = "saving…";
    try {
      const u = new URL(`/api/projects/${encodeURIComponent(state.activeProject)}/doc`,
                        location.origin);
      u.searchParams.set("agent", state.agent);
      const r = await fetch(u, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: $("#cmEditor").value }),
      });
      if (!r.ok) throw new Error(`${r.status}`);
      const res = await r.json();
      $("#cmStatus").textContent = "saved ✓";
      flash(docName + " saved to " + res.saved);
    } catch (e) {
      $("#cmStatus").textContent = "failed: " + e.message;
    }
  });
}
```

Also, in `loadProjView` where the `claude` view fetches (around line 211), the call uses `api(.../claude-md)` which still works (back-compat alias) and now carries the agent param automatically. No change needed there, but verify it routes via `api()` not raw `fetch`.

- [ ] **Step 6: Thread agent into search**

Find `runSearch` (around line 815-818) where it builds `url = /api/search?q=...`. Add the agent param. Replace the url construction line with:

```js
  let url = `/api/search?q=${encodeURIComponent(q)}&limit=80&agent=${encodeURIComponent(state.agent)}`;
```

(The scope `project` param append that follows stays unchanged.)

- [ ] **Step 7: Call `loadAgents()` before `loadProjects()` at startup**

Find the init/bootstrap call near the bottom of app.js (where `loadProjects()` is first invoked on load). Make it await agents first. Replace that startup line/block with:

```js
(async () => {
  await loadAgents();
  await loadProjects();
})();
```

If there is already an `async function init()` or DOMContentLoaded handler calling `loadProjects()`, add `await loadAgents();` immediately before the `await loadProjects();` inside it instead of adding a second bootstrap.

- [ ] **Step 8: Add `.agentsel` styling**

In `frontend/style.css`, append:

```css
.agentsel {
  background: var(--panel, #1b1b1d);
  color: var(--fg, #eee);
  border: 1px solid var(--border, #333);
  border-radius: 6px;
  padding: 2px 6px;
  margin-left: 8px;
  font-size: 13px;
  cursor: pointer;
}
```

- [ ] **Step 9: Syntax-check the frontend**

Run: `node --check frontend/app.js`
Expected: no output (exit 0).

- [ ] **Step 10: Manual smoke test in the browser**

Run (in the conda env): `python cc-mgr.py run --background`
Then:
- Open `http://localhost:8765`, confirm the agent dropdown shows Claude/Gemini/Codex/Copilot.
- Claude: projects load, Tasks + Memory + CLAUDE.md tabs present, conversation opens, search works.
- Switch to Gemini: projects load (your real `~/.gemini/tmp/*`), only Sessions + GEMINI.md tabs show, a conversation renders, editing GEMINI.md saves.
- Switch to Codex/Copilot: empty projects pane with no error.
- Click `↻ index`, then search a known word with Gemini active — hits are Gemini-only.
Stop: `python cc-mgr.py stop`

Record anything broken in `docs/ISSUES-v0.3.0.md` (create it) rather than leaving it silent.

- [ ] **Step 11: Commit**

```bash
git add frontend/index.html frontend/app.js frontend/style.css
git commit -m "feat: agent dropdown + capability-driven UI routing"
```

---

## Task 12: Docs + final full verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Create (if issues found): `docs/ISSUES-v0.3.0.md`

- [ ] **Step 1: Update CLAUDE.md architecture section**

Add an entry under "Architecture" describing `backend/agents/` (adapter registry, `common.py`, four adapters) and note `app.py` routes via `?agent=` (default claude), index has an `agent` column. Add to the data-model section the Gemini/Codex/Copilot layouts from the spec table. Keep existing Claude content.

- [ ] **Step 2: Update README.md**

Update the one-liner and any "Claude project viewer" phrasing to "coding-agent project viewer (Claude, Gemini, Codex, Copilot)". Document the new env vars `GEMINI_HOME`, `CODEX_HOME`, `COPILOT_HOME` alongside `CLAUDE_HOME`.

- [ ] **Step 3: Run the entire test suite**

Run: `python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 4: Backend import sanity**

Run: `python -c "import backend.app, backend.index_db, backend.store; from backend.agents import list_agents; print([a['agent_id'] for a in list_agents()])"`
Expected: `['claude', 'gemini', 'codex', 'copilot']`

- [ ] **Step 5: Live reindex + multi-agent server smoke**

Run: `python cc-mgr.py run --background && sleep 1.5 && curl -s -X POST http://localhost:8765/api/reindex && echo && curl -s "http://localhost:8765/api/agents" && echo && curl -s "http://localhost:8765/api/projects?agent=gemini" | head -c 400 && echo && python cc-mgr.py stop`
Expected: reindex returns counts; agents lists four; gemini projects return JSON (your real data) without error.

- [ ] **Step 6: Commit docs**

```bash
git add CLAUDE.md README.md docs/ISSUES-v0.3.0.md 2>/dev/null; git add CLAUDE.md README.md
git commit -m "docs: document multi-agent support for v0.3.0"
```

---

## Self-Review Notes (for the implementer)

- **Spec coverage:** dropdown (T11), four adapters (T4–T7), event-sourced Gemini parser (T5), AGENTS.md/GEMINI.md doc editing (T4–T7, T10, T11), per-agent search via `agent` column (T9), switch-active-agent model (T11), empty-but-valid Codex/Copilot (T6/T7 tests), capability-gated UI (T11). All spec sections map to a task.
- **Type consistency:** `Capabilities` fields, `SessionSummary.agent`, the normalized turn shape `{uuid,role,kind,timestamp,model,blocks,...}`, and `iter_turns` yielding `{seq,role,kind,timestamp,blocks}` are used identically across adapters, index_db, and app.py.
- **Ordering caveat:** Task 3's `__init__.py` imports the four adapter modules, so its test passes only after T4–T7. The plan calls this out and runs T3's test in T7 Step 5. Implement T3's code body, then T4–T7, then verify.
- **Back-compat:** `store.py` untouched logically; `/claude-md` and `/memory` routes kept as aliases so nothing breaks mid-migration. Default `agent=claude` everywhere.
- **No real data mutated by tests:** every fixture sets `*_HOME` env vars into `tmp_path`; index tests monkeypatch `db_path`.
