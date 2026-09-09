from backend.agents.agy import AgyAdapter


def test_agy_caps(agy_home):
    c = AgyAdapter().capabilities
    assert c.agent_id == "agy"
    assert c.doc_filename == "GEMINI.md"
    assert c.can_edit_doc is True
    assert c.has_memory is True
    assert c.has_tasks is False
    assert c.can_delete is True
    assert c.can_export is True


def test_agy_list_projects(agy_home):
    projs = AgyAdapter().list_projects()
    assert len(projs) == 1
    assert projs[0]["cwd"].endswith("repo_agy")
    assert projs[0]["session_count"] == 1
    assert projs[0]["has_claude_md"] is True
    assert projs[0]["has_memory"] is True


def test_agy_list_sessions(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    sessions = a.list_sessions(pname)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.agent == "agy"
    assert s.session_id == "11111111-2222-3333-4444-555555555555"
    assert s.title == "Test AGY Title"
    assert "hello agy" in s.first_prompt
    assert s.message_count == 3
    assert s.user_turns == 1
    assert s.assistant_turns == 1


def test_agy_get_conversation(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    conv = a.get_conversation(pname, sid)
    assert conv["total"] == 3
    turns = conv["turns"]
    assert turns[0]["role"] == "user" and turns[0]["kind"] == "user"
    assert "hello agy" in turns[0]["blocks"][0]["text"]
    assert turns[1]["role"] == "assistant" and turns[1]["kind"] == "assistant"
    assert turns[1]["blocks"][0]["type"] == "thinking"
    assert turns[1]["blocks"][1]["type"] == "text"
    assert turns[1]["blocks"][2]["type"] == "tool_use"
    assert turns[2]["role"] == "user" and turns[2]["kind"] == "tool"
    assert turns[2]["blocks"][0]["type"] == "tool_result"


def test_agy_iter_turns(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    turns = list(a.iter_turns(pname, sid))
    assert len(turns) == 3
    assert turns[0]["role"] == "user"
    assert turns[1]["role"] == "assistant"


def test_agy_get_and_save_doc(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    doc = a.get_doc(pname)
    assert doc["exists"] is True
    assert "delta" in doc["content"]

    a.save_doc(pname, "# Updated Doc\nepsilon\n")
    doc2 = a.get_doc(pname)
    assert "epsilon" in doc2["content"]


def test_agy_get_memory_and_save(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    mem = a.get_memory(pname)
    assert len(mem["files"]) >= 1
    assert any("report.md" in f["name"] for f in mem["files"])
    assert "some findings" in mem["files"][0]["content"]

    # Save a memory file
    a.save_memory_file(pname, "custom_rule.md", "# Custom Rule\ntest")
    mem2 = a.get_memory(pname)
    assert any(f["name"] == "custom_rule.md" for f in mem2["files"])


def test_agy_export_session(agy_home, tmp_path):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    md = a.export_session_markdown(pname, sid)
    assert f"# Session {sid}" in md
    assert "hello agy" in md
    assert "hello from agy" in md

    out_file = a.export_session_to_file(pname, sid, out_dir=tmp_path / "exports")
    assert out_file.is_file()
    assert "hello agy" in out_file.read_text(encoding="utf-8")


def test_agy_save_session_as_memory(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    out = a.save_session_as_memory(pname, sid)
    assert out.is_file()
    assert f"session-{sid[:8]}" in out.read_text(encoding="utf-8")


def test_agy_delete_session_soft(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    res = a.delete_session(pname, sid, hard=False)
    assert res["trash"] is not None
    # Now sessions list should be empty
    assert len(a.list_sessions(pname)) == 0


def test_agy_delete_session_hard(agy_home):
    a = AgyAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    res = a.delete_session(pname, sid, hard=True)
    assert res["trash"] is None
    assert len(a.list_sessions(pname)) == 0
