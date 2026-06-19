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
