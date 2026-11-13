"""#17: Claude adapter — summary cache, titles, attachments, empty sessions."""
import json

from fastapi.testclient import TestClient

from backend import store
from backend.app import app

PROJECT = "-tmp-repo_claude"


def _write(claude_home, sid, records):
    path = claude_home / "projects" / PROJECT / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def test_titles_prefer_custom_over_ai(claude_home):
    _write(claude_home, "titled", [_user("hi"), {"type": "ai-title", "aiTitle": "Generated"},
                                   {"type": "custom-title", "customTitle": "Mine"},
                                   {"type": "ai-title", "aiTitle": "Generated later"}])
    _write(claude_home, "ai-only", [_user("hi"), {"type": "ai-title", "aiTitle": "Only AI"}])
    by_id = {s.session_id: s for s in store.list_sessions(PROJECT)}
    assert by_id["titled"].title == "Mine"
    assert by_id["ai-only"].title == "Only AI"


def test_metadata_only_sessions_are_not_listed(claude_home):
    _write(claude_home, "empty", [{"type": "file-history-snapshot", "snapshot": {}},
                                  {"type": "ai-title", "aiTitle": "nothing here"}])
    assert "empty" not in {s.session_id for s in store.list_sessions(PROJECT)}


def test_document_blocks_become_placeholders(claude_home):
    _write(claude_home, "doc", [{"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "read this"},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": "JVBERi0xLjQK"}}]}}])
    blocks = store.get_conversation(PROJECT, "doc")["turns"][0]["blocks"]
    assert [b["text"] for b in blocks] == ["read this", "[document: application/pdf]"]
    assert "JVBERi0" not in json.dumps(blocks)


def test_sessions_api_reports_the_agent(claude_home):
    sessions = TestClient(app).get(f"/api/projects/{PROJECT}/sessions").json()
    assert sessions and all(s["agent"] == "claude" for s in sessions)


def test_summaries_are_cached_until_the_file_changes(claude_home, monkeypatch):
    parsed = []
    real = store._scan_session_uncached

    def counting(path, project, stat):
        parsed.append(path.stem)
        return real(path, project, stat)

    monkeypatch.setattr(store, "_scan_session_uncached", counting)
    monkeypatch.setattr(store, "_SCAN_CACHE", {})
    first = store.list_sessions(PROJECT)
    assert parsed == ["sess-c1"]
    first[0].has_memory = True  # callers get copies; the cache must not change
    store.list_sessions(PROJECT)
    assert parsed == ["sess-c1"]  # unchanged file: served from the cache
    path = claude_home / "projects" / PROJECT / "sess-c1.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + json.dumps(_user("more")),
                    encoding="utf-8")
    again = store.list_sessions(PROJECT)
    assert parsed == ["sess-c1", "sess-c1"] and again[0].user_turns == 2
    assert store._SCAN_CACHE[str(path)][2].has_memory is False
