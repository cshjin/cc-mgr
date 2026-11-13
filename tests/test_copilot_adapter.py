from backend.agents.copilot import CopilotAdapter


def test_caps(copilot_home):
    c = CopilotAdapter().capabilities
    assert c.agent_id == "copilot"
    assert c.doc_filename == "AGENTS.md"
    assert c.can_edit_doc is True
    assert c.has_tasks is False
    assert c.can_delete is True and c.can_export is True  # #24


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


# ---- the real Copilot CLI layout (#15) ----

def _proj(home, name):
    cwd = home.parent / name
    return str(cwd), str(cwd).replace("/", "-").replace("\\", "-").replace(":", "-")


def test_store_projects_from_db_and_events(copilot_store_home):
    projs = {p["cwd"]: p for p in CopilotAdapter().list_projects()}
    cwd1, _ = _proj(copilot_store_home, "repo_cp")
    cwd2, _ = _proj(copilot_store_home, "repo_cp2")
    assert set(projs) == {cwd1, cwd2}
    assert projs[cwd1]["session_count"] == 2 and projs[cwd1]["has_claude_md"] is True
    assert projs[cwd2]["session_count"] == 1  # events.jsonl without a DB row


def test_store_sessions(copilot_store_home):
    _, pname = _proj(copilot_store_home, "repo_cp")
    by_id = {s.session_id: s for s in CopilotAdapter().list_sessions(pname)}
    s = by_id["aaaa-1"]
    assert (s.title, s.git_branch, s.first_prompt) == ("Fix the build", "main", "please fix the build")
    assert s.model == "claude-sonnet-5.5"
    assert s.context_tokens == 50_100 and s.total_output_tokens == 1_100
    assert s.context_limit_known is True  # Claude, and a usage record crossed 200k
    db = by_id["bbbb-2"]
    assert (db.user_turns, db.assistant_turns) == (2, 1)
    assert (db.model, db.context_tokens, db.context_limit_known) == ("gemini-4-argon", 45_634, False)


def test_store_conversation_from_events(copilot_store_home):
    _, pname = _proj(copilot_store_home, "repo_cp")
    conv = CopilotAdapter().get_conversation(pname, "aaaa-1")
    assert [(t["role"], t["kind"]) for t in conv["turns"]] == [
        ("user", "user"), ("assistant", "assistant"), ("user", "tool"), ("user", "tool"),
        ("system", "system"), ("system", "system")]
    blocks = conv["turns"][1]["blocks"]
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[2] == {"type": "tool_use", "name": "bash", "input": {"command": "make"}}
    assert conv["turns"][2]["blocks"][0]["text"] == "build ok"
    assert conv["turns"][3]["blocks"][0]["text"] == "Error: permission denied"
    assert "summary of earlier work" in conv["turns"][4]["blocks"][0]["text"]


def test_store_conversation_from_db_turns(copilot_store_home):
    _, pname = _proj(copilot_store_home, "repo_cp")
    conv = CopilotAdapter().get_conversation(pname, "bbbb-2")
    assert [t["blocks"][0]["text"] for t in conv["turns"]] == ["db prompt", "second", "db reply"]


def test_store_doc_and_api(copilot_store_home):
    from fastapi.testclient import TestClient
    from backend.app import app
    _, pname = _proj(copilot_store_home, "repo_cp")
    assert "kappa" in CopilotAdapter().get_doc(pname)["content"]
    r = TestClient(app).get("/api/projects", params={"agent": "copilot"})
    assert r.status_code == 200 and len(r.json()) == 2
