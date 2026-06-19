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


def test_list_sessions_counts_and_tokens(codex_home):
    a = CodexAdapter()
    pname = a.list_projects()[0]["name"]
    s = a.list_sessions(pname)[0]
    # developer prelude excluded; 1 user + 1 assistant message turn
    assert s.user_turns == 1
    assert s.assistant_turns == 1
    assert s.first_prompt == "hello codex"
    assert s.context_tokens == 59  # from token_count event


def test_get_conversation_real_envelope(codex_home):
    a = CodexAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    conv = a.get_conversation(pname, sid)
    # user msg + function_call + function_call_output + assistant msg = 4
    # (developer prelude excluded)
    assert conv["total"] == 4
    kinds = [t["kind"] for t in conv["turns"]]
    assert kinds == ["user", "tool", "tool", "assistant"]
    assert conv["turns"][0]["blocks"][0]["text"] == "hello codex"
    # function_call -> tool_use block with parsed args
    tc = conv["turns"][1]
    assert tc["blocks"][0]["type"] == "tool_use"
    assert tc["blocks"][0]["name"] == "exec_command"
    assert tc["blocks"][0]["input"] == {"cmd": "ls"}
    # function_call_output -> tool_result block
    tr = conv["turns"][2]
    assert tr["blocks"][0]["type"] == "tool_result"
    assert "a.py" in tr["blocks"][0]["text"]
    assert conv["turns"][3]["blocks"][0]["text"] == "hi from codex"


def test_developer_prelude_excluded(codex_home):
    a = CodexAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    conv = a.get_conversation(pname, sid)
    allbtext = " ".join(b.get("text", "") for t in conv["turns"] for b in t["blocks"])
    assert "permissions" not in allbtext


def test_empty_home_returns_no_projects(empty_codex_home):
    assert CodexAdapter().list_projects() == []


def test_get_and_save_doc(codex_home):
    a = CodexAdapter()
    pname = a.list_projects()[0]["name"]
    assert "gamma" in a.get_doc(pname)["content"]
    a.save_doc(pname, "# c\ndelta\n")
    assert "delta" in a.get_doc(pname)["content"]
