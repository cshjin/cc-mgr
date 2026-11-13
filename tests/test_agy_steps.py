"""#13: agy step types, model attribution, reversible soft delete (+ #12 parents)."""
import json
import sqlite3
from pathlib import Path

from backend.agents.agy import AgyAdapter

CID = "11111111-2222-3333-4444-555555555555"


def _append(agy_home, *records):
    path = agy_home / "brain" / CID / ".system_generated" / "logs" / "transcript.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write("\n" + json.dumps(r))


def _pname():
    return AgyAdapter().list_projects()[0]["name"]


def test_new_step_types_are_shown_not_dropped(agy_home):
    _append(agy_home,
            {"step_index": "3", "type": "CHECKPOINT", "source": "SYSTEM",
             "content": "# Resuming from a compaction\nsummary here"},
            {"step_index": "4", "type": "ERROR_MESSAGE", "source": "SYSTEM",
             "error": "invalid tool call", "content": "Created At: …"},
            {"step_index": "5", "type": "CONVERSATION_HISTORY", "source": "SYSTEM"},
            {"step_index": "6", "type": "BRAND_NEW_STEP", "source": "SYSTEM", "content": "future data"},
            {"step_index": "7", "type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": "note"})
    a = AgyAdapter()
    conv = a.get_conversation(_pname(), CID)
    texts = [t["blocks"][0]["text"] for t in conv["turns"][3:]]
    assert texts == ["[checkpoint]\n# Resuming from a compaction\nsummary here",
                     "[error] invalid tool call", "[BRAND_NEW_STEP] future data", "note"]
    assert all(t["kind"] == "system" for t in conv["turns"][3:])
    assert a.list_sessions(_pname())[0].message_count == conv["total"] == 7  # count == display


def test_model_is_not_borrowed_from_settings(agy_home):
    # settings.json holds today's default ("Gemini 3.8 Flash (High)"), not the session's model
    assert AgyAdapter().list_sessions(_pname())[0].model == ""


def test_soft_delete_is_reversible(agy_home):
    (agy_home / "annotations").mkdir()
    (agy_home / "annotations" / f"{CID}.pbtxt").write_text('title:"x"')
    (agy_home / "conversations").mkdir()
    (agy_home / "conversations" / f"{CID}.db").write_text("x")
    res = AgyAdapter().delete_session(_pname(), CID, hard=False)
    trash = Path(res["trash"])
    saved = json.loads((trash / "conversation_summary.json").read_text(encoding="utf-8"))
    assert saved["conversation_id"] == CID and saved["title"] == "Test AGY Title"
    assert (trash / "annotations" / f"{CID}.pbtxt").is_file()
    assert (trash / "conversations" / f"{CID}.db").is_file()
    assert (trash / "brain" / ".system_generated").is_dir()
    assert not (agy_home / "annotations" / f"{CID}.pbtxt").exists()
    assert "warnings" not in res
    conn = sqlite3.connect(agy_home / "conversation_summaries.db")
    assert conn.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 0
    conn.close()


def test_db_problems_are_reported_not_swallowed(agy_home):
    pname = _pname()
    (agy_home / "conversation_summaries.db").write_bytes(b"definitely not a sqlite database " * 40)
    res = AgyAdapter().delete_session(pname, CID, hard=True)
    assert res["warnings"] and "conversation_summaries.db" in res["warnings"][0]
    assert not (agy_home / "brain" / CID).exists()


def test_subagent_conversations_carry_their_parent(agy_home):
    child = "99999999-2222-3333-4444-555555555555"
    d = agy_home / "brain" / child / ".system_generated" / "logs"
    d.mkdir(parents=True)
    (d / "transcript.jsonl").write_text(json.dumps(
        {"step_index": "0", "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "sub"}))
    conn = sqlite3.connect(agy_home / "conversation_summaries.db")
    conn.execute("INSERT INTO conversation_summaries (conversation_id, title, parent_conversation_id) "
                 "VALUES (?, 'child', ?)", (child, CID))
    conn.commit()
    conn.close()
    a = AgyAdapter()
    projs = a.list_projects()
    assert len(projs) == 1 and projs[0]["session_count"] == 1  # listed under the parent, not counted
    by_id = {s.session_id: s for s in a.list_sessions(projs[0]["name"])}
    assert by_id[child].parent_id == CID and by_id[CID].parent_id == ""
