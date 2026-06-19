from backend.agents.copilot import CopilotAdapter


def test_caps(copilot_home):
    c = CopilotAdapter().capabilities
    assert c.agent_id == "copilot"
    assert c.doc_filename == "AGENTS.md"
    assert c.can_edit_doc is True
    assert c.has_tasks is False and c.can_delete is False


def test_empty_home_is_graceful(copilot_home):
    a = CopilotAdapter()
    assert a.list_projects() == []
    assert a.list_sessions("anything") == []
    conv = a.get_conversation("p", "s")
    assert conv == {"total": 0, "offset": 0, "limit": None, "turns": []}
    assert list(a.iter_turns("p", "s")) == []


def test_reads_history_when_present(copilot_home):
    import json
    cwd = copilot_home.parent / "repo_copilot"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("# copilot doc\nkappa\n", encoding="utf-8")
    proj = copilot_home / "history" / "-tmp-repo_copilot"
    proj.mkdir(parents=True)
    (proj / "sess-p1.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "uuid": "pu1", "cwd": str(cwd),
                    "timestamp": "2026-06-04T00:00:00Z",
                    "message": {"role": "user",
                                "content": [{"type": "text", "text": "hi copilot"}]}}),
        json.dumps({"type": "assistant", "uuid": "pa1",
                    "timestamp": "2026-06-04T00:00:01Z",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "hello!"}]}}),
    ]), encoding="utf-8")
    a = CopilotAdapter()
    projs = a.list_projects()
    assert len(projs) == 1
    sid = a.list_sessions(projs[0]["name"])[0].session_id
    assert "hi copilot" in a.get_conversation(projs[0]["name"], sid)["turns"][0]["blocks"][0]["text"]
