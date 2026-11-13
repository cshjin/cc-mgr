"""Agent adapter protocol + registry.

Each adapter knows one coding agent's on-disk layout. app.py routes requests
to an adapter selected by ?agent=. Default is claude.
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .common import (  # SessionSummary is re-exported
    SessionSummary, default_export_dir, is_safe_id, private_mkdir, render_markdown,
    write_private,
)


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
    # What a delete does for this agent, shown in the confirmation dialogs.
    delete_note: str = ""


class AgentAdapter(ABC):
    capabilities: Capabilities

    # ---- READ (required) ----
    @abstractmethod
    def list_projects(self) -> list[dict[str, Any]]: ...

    def has_data(self) -> bool:
        """Cheap check for "this agent has anything to show" (adapters override
        it with a probe much lighter than listing every project)."""
        return bool(self.list_projects())

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
        """The session as Markdown, rendered from get_conversation (adapters
        with a richer format override this). Unknown or empty sessions raise
        FileNotFoundError."""
        data = self.get_conversation(project, session_id, offset=0, limit=None)
        if not data.get("total"):
            raise FileNotFoundError(f"session {session_id} not found or empty")
        return render_markdown(self.capabilities.label, project, session_id, data["turns"],
                               self._session_title(project, session_id))

    def _session_title(self, project: str, session_id: str) -> str:
        return ""

    def export_session_to_file(self, project: str, session_id: str, out_dir=None) -> Path:
        """Write the Markdown export to <exports>/<project>/<session>.<stamp>.md (0600)."""
        if not is_safe_id(session_id):
            raise ValueError("invalid session id")
        markdown = self.export_session_markdown(project, session_id)
        if out_dir is None:
            out_dir = default_export_dir(project)
        private_mkdir(Path(out_dir))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return write_private(Path(out_dir) / f"{session_id}.{stamp}.md", markdown)

    def save_session_as_memory(self, project: str, session_id: str) -> Path:
        raise UnsupportedCapability("memory not supported")

    def delete_session(self, project: str, session_id: str, hard: bool = False) -> dict[str, Any]:
        raise UnsupportedCapability("delete not supported")

    # ---- whole-project delete ----
    def project_delete_preview(self, project: str) -> dict[str, Any]:
        """What deleting the whole project would remove, for the confirmation.

        hidden: files that are removed too but aren't listed as sessions (e.g.
        transcripts without any message); memory_files: project memory that is
        only removed on request."""
        sessions = self.list_sessions(project)
        return {"sessions": sum(1 for s in sessions if not s.parent_id),
                "subagents": sum(1 for s in sessions if s.parent_id),
                "newest": max((s.mtime for s in sessions), default=0.0),
                "hidden": 0, "memory_files": 0}

    def delete_project(self, project: str, hard: bool = False, include_memory: bool = False,
                       export_first: bool = False) -> dict[str, Any]:
        """Delete every session of a project through delete_session (soft by
        default), optionally exporting them all first.

        Exports all happen before any delete, and parents go first, so a
        permanent delete that cascades (OpenCode) takes its subagent sessions
        along only after they were exported; sessions already gone are skipped.
        A session that fails is reported in `errors` and the rest continue. If
        the first session is refused for a reason that refuses them all
        (UnsupportedCapability), that is raised instead.
        """
        if not self.capabilities.can_delete:
            raise UnsupportedCapability("delete not supported")
        sessions = sorted(self.list_sessions(project), key=lambda s: bool(s.parent_id))
        if not sessions:
            raise FileNotFoundError(f"no sessions to delete in {project}")
        result: dict[str, Any] = {"sessions": [], "deleted": [], "trash": [], "exported": [],
                                  "errors": {}, "warnings": []}
        if export_first:  # all of them before any delete: a cascade must not outrun its export
            for s in sessions:
                try:
                    result["exported"].append(str(self.export_session_to_file(project, s.session_id)))
                except FileNotFoundError:
                    pass  # nothing to export: the session has no turns
        gone: set[str] = set()
        for s in sessions:
            sid = s.session_id
            if sid in gone:
                continue
            try:
                res = self.delete_session(project, sid, hard=hard)
            except FileNotFoundError:  # already removed, e.g. along with its parent
                gone.add(sid)
                continue
            except UnsupportedCapability as e:
                if not result["sessions"]:
                    raise
                result["errors"][sid] = f"stopped: {e}"
                break
            except (RuntimeError, ValueError, OSError, sqlite3.Error) as e:
                result["errors"][sid] = str(e)
                continue
            removed = res.get("sessions") or [sid]
            gone.update(removed)
            result["sessions"].extend(removed)
            result["deleted"].extend(res.get("deleted") or [])
            if res.get("trash"):
                result["trash"].append(res["trash"])
            result["warnings"].extend(res.get("warnings") or [])
            if res.get("note"):
                result["note"] = res["note"]
        result["sessions"] = list(dict.fromkeys(result["sessions"]))
        result["project_removed"] = not any(p["name"] == project for p in self.list_projects())
        return result

    def _export_all(self, project: str) -> list[str]:
        """Export every listed session of the project (empty ones have nothing to export)."""
        out = []
        for s in self.list_sessions(project):
            try:
                out.append(str(self.export_session_to_file(project, s.session_id)))
            except FileNotFoundError:
                continue
        return out


# Registry is populated at import time below.
_REGISTRY: dict[str, AgentAdapter] = {}


def _register(adapter: AgentAdapter) -> None:
    _REGISTRY[adapter.capabilities.agent_id] = adapter


def get_adapter(agent_id: str | None) -> AgentAdapter:
    if agent_id == "antigravity":
        agent_id = "agy"
    elif agent_id in ("opencode", "open-code"):
        agent_id = "opencode"
    return _REGISTRY.get(agent_id or "claude", _REGISTRY["claude"])


def all_adapters() -> list[AgentAdapter]:
    return list(_REGISTRY.values())


def list_agents() -> list[dict[str, Any]]:
    out = []
    for a in _REGISTRY.values():
        info = asdict(a.capabilities)
        try:
            info["has_data"] = a.has_data()
        except Exception:  # noqa: BLE001 — a broken probe must not hide the agent list
            info["has_data"] = False
        out.append(info)
    return out


# Import + register concrete adapters (after class defs to avoid cycles).
from .claude import ClaudeAdapter      # noqa: E402
from .agy import AgyAdapter            # noqa: E402
from .opencode import OpencodeAdapter  # noqa: E402
from .gemini import GeminiAdapter      # noqa: E402
from .codex import CodexAdapter        # noqa: E402
from .copilot import CopilotAdapter    # noqa: E402

_register(ClaudeAdapter())
_register(AgyAdapter())
_register(OpencodeAdapter())
_register(CodexAdapter())
_register(CopilotAdapter())
_register(GeminiAdapter())  # legacy: Gemini CLI is retired, agy replaces it
