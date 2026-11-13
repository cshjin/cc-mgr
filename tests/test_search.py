"""#16: stale index rows, CJK search, highlight markers, LIKE escaping."""
import json

from fastapi.testclient import TestClient

from backend import index_db, store
from backend.agents import get_adapter
from backend.app import app

PROJECT = "-tmp-repo_claude"


def _add_claude_turn(claude_home, text, sid="sess-c2"):
    cwd = claude_home.parent / "repo_claude"
    rec = {"type": "user", "uuid": "u9", "cwd": str(cwd),
           "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    (claude_home / "projects" / PROJECT / f"{sid}.jsonl").write_text(json.dumps(rec), encoding="utf-8")


def test_deleted_sessions_are_pruned_on_reindex(claude_home):
    index_db.reindex()
    assert index_db.search("hello", agent="claude")
    store.delete_session(PROJECT, "sess-c1", hard=True)
    assert index_db.reindex()["pruned"] == 1
    assert index_db.search("hello", agent="claude") == []


def test_api_delete_purges_the_index_immediately(claude_home):
    index_db.reindex()
    c = TestClient(app)
    r = c.post(f"/api/projects/{PROJECT}/sessions/sess-c1/delete", json={"export_first": False})
    assert r.status_code == 200, r.text
    assert c.get("/api/search", params={"q": "hello", "agent": "claude"}).json()["results"] == []


def test_cjk_substring_search(claude_home):
    _add_claude_turn(claude_home, "这个repo有一阵子没有更新了，帮我检查数据库")
    index_db.reindex()
    for q in ("数据库", "更新"):  # 3 chars via FTS trigram, 2 chars via the LIKE fallback
        hits = index_db.search(q, agent="claude")
        assert hits and hits[0]["session_id"] == "sess-c2", q
        assert f"{index_db.HL_START}{q}{index_db.HL_END}" in hits[0]["snippet"], hits[0]["snippet"]


def test_highlight_markers_do_not_collide_with_brackets(claude_home):
    _add_claude_turn(claude_home, "see [link](http://x) and arr[0] = 1 zebradatabase")
    index_db.reindex()
    snip = index_db.search("zebradatabase", agent="claude")[0]["snippet"]
    assert "[link]" in snip and "arr[0]" in snip  # brackets stay literal text
    assert f"{index_db.HL_START}zebradatabase{index_db.HL_END}" in snip


def test_like_fallback_escapes_wildcards(claude_home):
    _add_claude_turn(claude_home, "I am 100% sure", sid="sess-pct")
    _add_claude_turn(claude_home, "no wildcard here", sid="sess-plain")
    index_db.reindex()
    assert {h["session_id"] for h in index_db.search("0%", agent="claude")} == {"sess-pct"}
    assert index_db.search("%", agent="claude")[0]["session_id"] == "sess-pct"


def test_one_broken_adapter_neither_stops_nor_prunes(claude_home, gemini_home, monkeypatch):
    index_db.reindex()
    before = index_db.search("gemini", agent="gemini")
    assert before

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(get_adapter("gemini"), "list_projects", boom)
    stats = index_db.reindex(force=True)
    assert "gemini" in stats["errors"]
    assert index_db.search("gemini", agent="gemini") == before  # not pruned
    assert index_db.search("hello", agent="claude")  # the other agents still indexed


def test_index_db_is_owner_only(claude_home):
    import stat
    index_db.reindex()
    assert stat.S_IMODE(index_db.db_path().stat().st_mode) == 0o600  # it copies every conversation
