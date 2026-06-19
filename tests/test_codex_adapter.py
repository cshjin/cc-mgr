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
