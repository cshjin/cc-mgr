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
