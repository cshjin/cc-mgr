from backend.agents.opencode import OpencodeAdapter


def test_opencode_caps(opencode_home):
    c = OpencodeAdapter().capabilities
    assert c.agent_id == "opencode"
    assert c.doc_filename == "AGENTS.md"
    assert c.can_edit_doc is True
    assert c.has_memory is False
    assert c.has_tasks is True
    assert c.can_delete is True
    assert c.can_export is True


def test_opencode_list_projects(opencode_home):
    projs = OpencodeAdapter().list_projects()
    assert len(projs) == 1
    assert projs[0]["cwd"].endswith("repo_opencode")
    assert projs[0]["session_count"] == 1
    assert projs[0]["has_claude_md"] is True
    assert projs[0]["total_tasks"] == 1
    assert projs[0]["open_tasks"] == 1


def test_opencode_list_sessions(opencode_home):
    a = OpencodeAdapter()
    pname = a.list_projects()[0]["name"]
    sessions = a.list_sessions(pname)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.agent == "opencode"
    assert s.session_id == "ses_test_1"
    assert s.title == "Test OpenCode Title"
    assert "hello opencode" in s.first_prompt
    assert s.message_count == 2
    assert s.user_turns == 1
    assert s.assistant_turns == 1
    assert s.total_tasks == 1
    assert s.open_tasks == 1


def test_opencode_get_conversation(opencode_home):
    a = OpencodeAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    conv = a.get_conversation(pname, sid)
    assert conv["total"] == 2
    turns = conv["turns"]
    assert turns[0]["role"] == "user" and turns[0]["kind"] == "user"
    assert "hello opencode" in turns[0]["blocks"][0]["text"]

    assert turns[1]["role"] == "assistant" and turns[1]["kind"] == "assistant"
    assert turns[1]["blocks"][0]["type"] == "thinking"
    assert turns[1]["blocks"][1]["type"] == "text"
    assert turns[1]["blocks"][2]["type"] == "tool_use"
    assert turns[1]["blocks"][2]["name"] == "bash"
    assert turns[1]["blocks"][3]["type"] == "tool_result"
    assert turns[1]["blocks"][3]["text"] == "file.txt"


def test_opencode_iter_turns(opencode_home):
    a = OpencodeAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    turns = list(a.iter_turns(pname, sid))
    assert len(turns) == 2
    assert turns[0]["seq"] == 0
    assert turns[1]["seq"] == 1


def test_opencode_doc(opencode_home):
    a = OpencodeAdapter()
    pname = a.list_projects()[0]["name"]
    doc = a.get_doc(pname)
    assert doc["exists"] is True
    assert "gamma" in doc["content"]

    a.save_doc(pname, "updated gamma")
    assert a.get_doc(pname)["content"] == "updated gamma"


def test_opencode_tasks(opencode_home):
    a = OpencodeAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id

    tasks = a.get_tasks(sid)
    assert len(tasks) == 1
    assert tasks[0]["subject"] == "test task 1"
    assert tasks[0]["status"] == "pending"

    ptasks = a.project_tasks(pname)
    assert len(ptasks) == 1

    updated = a.update_task_status(sid, "0", "completed")
    assert updated["status"] == "completed"
    assert a.get_tasks(sid)[0]["status"] == "completed"


def test_opencode_export_markdown(opencode_home):
    a = OpencodeAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    md = a.export_session_markdown(pname, sid)
    assert "# Session: Test OpenCode Title" in md
    assert "hello opencode" in md
    assert "hi from opencode" in md
    assert "tool: bash" in md


def test_opencode_delete_session(opencode_home):
    a = OpencodeAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id

    # Soft delete (archives session)
    res = a.delete_session(pname, sid, hard=False)
    assert res["deleted"] == [sid]
    # After archiving, it shouldn't show up in list_sessions
    assert len(a.list_sessions(pname)) == 0

    # Hard delete
    res_hard = a.delete_session(pname, sid, hard=True)
    assert res_hard["deleted"] == [sid]
