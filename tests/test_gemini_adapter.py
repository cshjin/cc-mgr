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


def test_prelude_session_context_excluded(gemini_home):
    """The line-1 $set prelude (<session_context>) must NOT appear as a turn."""
    a = GeminiAdapter()
    pname = a.list_projects()[0]["name"]
    sid = a.list_sessions(pname)[0].session_id
    conv = a.get_conversation(pname, sid)
    all_text = " ".join(b.get("text", "") for t in conv["turns"] for b in t["blocks"])
    assert "<session_context>" not in all_text
    # only the two real turns count
    assert conv["total"] == 2


def test_gemini_string_content_and_tokens(gemini_home):
    """A gemini turn whose content is a plain string is rendered, and its
    tokens.total drives context, tokens.output drives output count."""
    a = GeminiAdapter()
    pname = a.list_projects()[0]["name"]
    s = a.list_sessions(pname)[0]
    assert s.context_tokens == 107       # tokens.total of last gemini turn
    assert s.total_output_tokens == 7    # sum of tokens.output
    sid = s.session_id
    conv = a.get_conversation(pname, sid)
    asst = [t for t in conv["turns"] if t["role"] == "assistant"][0]
    assert asst["model"] == "gemini-3-flash-preview"
    assert asst["output_tokens"] == 7
    assert asst["blocks"][0]["text"] == "hello from gemini"


def test_gemini_function_response_and_toolcalls(gemini_home, tmp_path):
    """A real-shaped session with toolCalls + functionResponse parses into
    tool_use / tool_result blocks (not dropped)."""
    import json
    from pathlib import Path
    home = Path(__import__("os").environ["GEMINI_HOME"])
    chats = home / "tmp" / "repo_gemini" / "chats"
    sess = chats / "session-2026-06-05T00-00-deadbeef.jsonl"
    sess.write_text("\n".join([
        json.dumps({"sessionId": "deadbeef", "startTime": "2026-06-05T00:00:00Z",
                    "kind": "main"}),
        json.dumps({"id": "u1", "timestamp": "2026-06-05T00:00:01Z", "type": "user",
                    "content": [{"text": "run the tool"}]}),
        json.dumps({"id": "g1", "timestamp": "2026-06-05T00:00:02Z", "type": "gemini",
                    "content": "", "model": "gemini-3-flash-preview",
                    "toolCalls": [{"id": "tc1", "name": "list_dir",
                                   "args": {"path": "."},
                                   "result": [{"functionResponse": {"id": "tc1"}}]}]}),
        json.dumps({"id": "u2", "timestamp": "2026-06-05T00:00:03Z", "type": "user",
                    "content": [{"functionResponse": {"id": "tc1", "name": "list_dir",
                                 "response": {"output": "a.py\nb.py"}}}]}),
    ]), encoding="utf-8")
    a = GeminiAdapter()
    conv = a.get_conversation("repo_gemini", "session-2026-06-05T00-00-deadbeef")
    assert conv["total"] == 3
    # gemini turn surfaces a tool_use block
    g = [t for t in conv["turns"] if t["uuid"] == "g1"][0]
    assert any(b["type"] == "tool_use" and b["name"] == "list_dir" for b in g["blocks"])
    # the functionResponse user turn is classified as a 'tool' kind with tool_result
    u2 = [t for t in conv["turns"] if t["uuid"] == "u2"][0]
    assert u2["kind"] == "tool"
    assert any(b["type"] == "tool_result" and "a.py" in b["text"] for b in u2["blocks"])
