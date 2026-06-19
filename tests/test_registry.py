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
