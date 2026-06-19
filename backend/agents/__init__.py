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
