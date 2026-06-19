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
