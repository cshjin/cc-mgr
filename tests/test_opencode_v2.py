"""OpenCode v2 schema (#9) and v2-aware writes (#10)."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import index_db
from backend.agents import UnsupportedCapability
from backend.agents import opencode as oc
from backend.agents.opencode import OpencodeAdapter
from backend.app import app


def _pname():
    return OpencodeAdapter().list_projects()[0]["name"]


def test_v2_projects_count_top_level_sessions_only(opencode_v2_home):
    projs = OpencodeAdapter().list_projects()
    assert len(projs) == 1
    p = projs[0]
    assert p["cwd"].endswith("repo_oc2") and p["has_claude_md"] is True
    assert p["session_count"] == 2  # parent + migrated; the child and the archived one don't count
    assert (p["total_tasks"], p["open_tasks"]) == (1, 1)  # legacy todo of the migrated session


def test_v2_sessions(opencode_v2_home):
    by_id = {s.session_id: s for s in OpencodeAdapter().list_sessions(_pname())}
    assert set(by_id) == {"ses_v2_parent", "ses_v2_child", "ses_old"}
    s = by_id["ses_v2_parent"]
    assert s.title == "V2 Parent"
    assert (s.first_prompt, s.last_prompt) == ("hello v2", "second prompt")
    assert (s.user_turns, s.assistant_turns) == (2, 3)
    assert s.context_tokens == 150_015  # newest finished assistant turn, cache included
    assert s.context_limit_known is True  # the compaction recorded 250k
    assert s.model == "claude-opus-5.5"
    assert by_id["ses_v2_child"].parent_id == "ses_v2_parent"
    assert by_id["ses_v2_parent"].parent_id == ""
    assert by_id["ses_old"].total_tasks == 1


def test_v2_conversation(opencode_v2_home):
    conv = OpencodeAdapter().get_conversation(_pname(), "ses_v2_parent")
    kinds = [(t["role"], t["kind"]) for t in conv["turns"]]
    assert kinds == [("user", "user"), ("assistant", "assistant"), ("system", "system"),
                     ("system", "system"), ("system", "system"), ("user", "user"),
                     ("assistant", "assistant"), ("assistant", "tool")]  # idle skipped
    first = conv["turns"][0]["blocks"]
    assert first[0]["text"] == "hello v2"
    assert first[1]["text"] == "[Attachment: paper.pdf (application/pdf)]"
    blocks = conv["turns"][1]["blocks"]
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use", "tool_result",
                                           "tool_use", "tool_result"]
    assert blocks[2]["name"] == "shell" and blocks[2]["input"] == {"command": "ls"}
    assert blocks[3]["text"] == "a.py\nb.py\n[file: image/png]"
    assert blocks[5]["text"] == "Error: File not found: /nope"
    assert conv["turns"][1]["model"] == "gpt-6-astra" and conv["turns"][1]["output_tokens"] == 7
    assert "summary text" in conv["turns"][2]["blocks"][0]["text"]
    assert conv["turns"][4]["blocks"][0]["text"] == "[model] gpt-6-astra → claude-opus-5.5"
    assert [b["type"] for b in conv["turns"][7]["blocks"]] == ["tool_use"]  # still running
    dumped = json.dumps(conv)
    assert "JVBERi0" not in dumped and "base64" not in dumped  # inline file data never shown


def test_v2_session_part_fallback(opencode_v2_home):
    conv = OpencodeAdapter().get_conversation(_pname(), "ses_v2_child")
    assert conv["turns"][1]["blocks"][0]["text"] == "from session_part"


def test_v2_export_and_iter_turns(opencode_v2_home):
    a = OpencodeAdapter()
    md = a.export_session_markdown(_pname(), "ses_v2_parent")
    assert "# Session: V2 Parent" in md and "### System" in md and "final answer" in md
    assert len(list(a.iter_turns(_pname(), "ses_v2_parent"))) == 8


def test_v2_tasks_are_legacy_and_read_only(opencode_v2_home):
    a = OpencodeAdapter()
    assert a.get_tasks("ses_old")[0]["subject"] == "legacy task"
    assert [t["subject"] for t in a.project_tasks(_pname())] == ["legacy task"]
    with pytest.raises(UnsupportedCapability):
        a.update_task_status("ses_old", "0", "completed")


def test_v2_api_routes(opencode_v2_home):
    c = TestClient(app)
    projs = c.get("/api/projects", params={"agent": "opencode"}).json()
    assert len(projs) == 1
    sessions = c.get(f"/api/projects/{projs[0]['name']}/sessions", params={"agent": "opencode"}).json()
    assert {s["parent_id"] for s in sessions} == {"", "ses_v2_parent"}
    r = c.post(f"/api/projects/{projs[0]['name']}/sessions/ses_v2_parent/delete",
               params={"agent": "opencode"}, json={"export_first": False})
    assert r.status_code == 404 and "read-only" in r.json()["detail"]  # no opencode CLI here


# ---- writes go through OpenCode itself ----

@pytest.fixture
def fake_opencode_cli(opencode_v2_home, monkeypatch):
    """Pretend the running OpenCode service holds this DB; record and apply its calls."""
    db = opencode_v2_home / "opencode.db"
    calls = []

    def fake_run(args):
        calls.append(args)
        conn = sqlite3.connect(db)
        if args[:2] == ["session", "delete"]:
            conn.execute("DELETE FROM session_message WHERE session_id = ?", (args[2],))
            conn.execute("DELETE FROM session_v2 WHERE id = ? OR parent_id = ?", (args[2], args[2]))
        elif args[:2] == ["api", "session.update"] and json.loads(args[5]).get("archived"):
            conn.execute("UPDATE session_v2 SET time_archived = 1 WHERE id = ?",
                         (args[3].split("=", 1)[1],))
        conn.commit()
        conn.close()

    monkeypatch.setattr(oc, "_opencode_cli", lambda: "/fake/bin/opencode")
    monkeypatch.setattr(oc, "_service_db_files", lambda: {os.path.realpath(db)})
    monkeypatch.setattr(oc, "_run_opencode", fake_run)
    return calls


def test_v2_soft_delete_archives_through_opencode(fake_opencode_cli):
    a = OpencodeAdapter()
    res = a.delete_session(_pname(), "ses_v2_parent", hard=False)
    assert fake_opencode_cli == [["api", "session.update", "--param", "sessionID=ses_v2_parent",
                                  "--data", '{"archived": true}']]
    assert res["trash"] and "Archived" in res["note"]
    assert "ses_v2_parent" not in {s.session_id for s in a.list_sessions(_pname())}


def test_v2_hard_delete_goes_through_opencode(fake_opencode_cli):
    a = OpencodeAdapter()
    res = a.delete_session(_pname(), "ses_v2_parent", hard=True)
    assert fake_opencode_cli == [["session", "delete", "ses_v2_parent"]]
    assert res["trash"] is None
    assert res["sessions"] == ["ses_v2_parent", "ses_v2_child"]  # cascaded descendants reported
    assert {s.session_id for s in a.list_sessions(_pname())} == {"ses_old"}  # child went too


def test_v2_hard_delete_purges_descendants_from_the_index(fake_opencode_cli):
    index_db.reindex()
    assert any(h["session_id"] == "ses_v2_child" for h in index_db.search("sub task", agent="opencode"))
    r = TestClient(app).post(f"/api/projects/{_pname()}/sessions/ses_v2_parent/delete",
                             params={"agent": "opencode"}, json={"export_first": False, "hard": True})
    assert r.status_code == 200, r.text
    assert index_db.search("sub task", agent="opencode") == []


def test_v2_delete_fails_loudly_if_opencode_did_nothing(opencode_v2_home, monkeypatch):
    db = opencode_v2_home / "opencode.db"
    monkeypatch.setattr(oc, "_opencode_cli", lambda: "/fake/bin/opencode")
    monkeypatch.setattr(oc, "_service_db_files", lambda: {os.path.realpath(db)})
    monkeypatch.setattr(oc, "_run_opencode", lambda args: None)
    with pytest.raises(RuntimeError):
        OpencodeAdapter().delete_session(_pname(), "ses_v2_parent", hard=True)


def test_v2_delete_refuses_a_db_the_service_does_not_hold(opencode_v2_home, monkeypatch, tmp_path):
    monkeypatch.setattr(oc, "_opencode_cli", lambda: "/fake/bin/opencode")
    monkeypatch.setattr(oc, "_service_db_files", lambda: {os.path.realpath(tmp_path / "other.db")})
    monkeypatch.setattr(oc, "_run_opencode", lambda args: pytest.fail("must not call opencode"))
    with pytest.raises(UnsupportedCapability):
        OpencodeAdapter().delete_session(_pname(), "ses_v2_parent", hard=True)


HOLDER = """
import sqlite3, sys, time
handles = [open(p, "rb") for p in sys.argv[1].split(",") if p]        # read-only, e.g. a backup
conns = []
for p in [p for p in sys.argv[2].split(",") if p]:                    # live WAL databases
    c = sqlite3.connect(p)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("CREATE TABLE IF NOT EXISTS t(x)")
    c.execute("INSERT INTO t VALUES (1)"); c.commit(); conns.append(c)
print("ready", flush=True)
time.sleep(60)
"""


def _service(read_only, live):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, ",".join(map(str, read_only)),
                             ",".join(map(str, live))], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_service_db_identity_comes_from_the_running_process(opencode_v2_home, monkeypatch, tmp_path):
    # OPENCODE_DB points cc_mgr at the fixture DB; "the service" (a real process)
    # writes live.db and merely reads backup.db — only live.db may be mutated.
    live, backup, live2 = tmp_path / "live.db", tmp_path / "backup.db", tmp_path / "live2.db"
    sqlite3.connect(backup).close()

    class Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(oc, "_opencode_cli", lambda: "/fake/bin/opencode")
    monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: Proc())
    monkeypatch.setattr(oc, "_run_opencode", lambda args: pytest.fail("must not call opencode"))
    for read_only, live_dbs, expected in (([backup], [live], {live}),
                                          ([], [live, live2], {live, live2})):
        holder = _service(read_only, live_dbs)
        try:
            Proc.stdout = json.dumps({"pid": holder.pid, "hostname": socket.gethostname()})
            assert oc._service_db_files() == {os.path.realpath(p) for p in expected}
            assert not oc._is_cli_managed(backup)  # only read: not the service's database
            assert oc._is_cli_managed(live) is (len(expected) == 1)  # two live DBs: ambiguous
            assert not oc._is_cli_managed(opencode_v2_home / "opencode.db")
            with pytest.raises(UnsupportedCapability):
                OpencodeAdapter().delete_session(_pname(), "ses_v2_parent", hard=True)
            Proc.stdout = json.dumps({"pid": holder.pid, "hostname": "another-node"})
            assert oc._service_db_files() is None  # a service elsewhere can't be verified
        finally:
            holder.kill()
            holder.wait()


# ---- context statistics (review follow-up) ----

def _v2_db(path, messages):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT,
            model TEXT, tokens_output INTEGER, time_created INTEGER, time_updated INTEGER,
            time_archived INTEGER);
        CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, time_updated INTEGER, data TEXT);
    """)
    conn.execute("INSERT INTO session_v2 VALUES ('s', NULL, '/w', 't', NULL, 0, 1, 2, NULL)")
    for seq, (mtype, data) in enumerate(messages, 1):
        conn.execute("INSERT INTO session_message VALUES (?, 's', ?, ?, ?, ?, ?)",
                     (f"m{seq}", mtype, seq, seq, seq, json.dumps(data)))
    conn.commit()
    conn.close()


def _turn(model, ctx=None):
    data = {"model": {"id": model}, "content": [{"type": "text", "text": "x"}]}
    if ctx is not None:
        data["tokens"] = {"input": ctx, "output": 0, "cache": {"read": 0, "write": 0}}
    return ("assistant", data)


@pytest.mark.parametrize("messages, expected", [
    # a Claude turn above 200k proves the tier even without a compaction afterwards
    ([_turn("claude-opus-5.5", 250_000), _turn("claude-opus-5.5", 100_000)], (100_000, True)),
    # a GPT compaction at 300k proves nothing about the Claude window
    ([("compaction", {"summary": "s", "model": {"id": "gpt-6-astra"},
                      "tokens": {"input": 300_000}}), _turn("claude-opus-5.5", 100_000)],
     (100_000, False)),
    # an earlier Claude peak proves nothing once the session runs on GPT
    ([_turn("claude-opus-5.5", 250_000), _turn("gpt-6-astra", 100_000)], (100_000, False)),
    # unfinished (token-less) turns don't hide the last real context
    ([_turn("claude-opus-5.5", 120_000)] + [_turn("claude-opus-5.5")] * 6, (120_000, False)),
])
def test_v2_context_uses_full_history_per_model(tmp_path, monkeypatch, messages, expected):
    db = tmp_path / "ctx.db"
    _v2_db(db, messages)
    monkeypatch.setenv("OPENCODE_DB", str(db))
    s = OpencodeAdapter().list_sessions("-w")[0]
    assert (s.context_tokens, s.context_limit_known) == expected


def test_v2_context_follows_turns_completed_in_place(tmp_path, monkeypatch):
    # OpenCode writes an assistant row while it streams and fills in its tokens
    # when the turn finishes, without changing the row's seq.
    db = tmp_path / "live.db"
    _v2_db(db, [_turn("claude-opus-5.5")])
    monkeypatch.setenv("OPENCODE_DB", str(db))
    a = OpencodeAdapter()
    assert a.list_sessions("-w")[0].context_tokens == 0
    conn = sqlite3.connect(db)
    conn.execute("UPDATE session_message SET time_updated = time_updated + 10, data = json_set(data, "
                 "'$.tokens', json('{\"input\": 250000, \"output\": 0}')) WHERE id = 'm1'")
    conn.execute("INSERT INTO session_message VALUES ('m2', 's', 'assistant', 2, 20, 20, ?)",
                 (json.dumps(_turn("claude-opus-5.5")[1]),))  # next turn already streaming
    conn.commit()
    conn.close()
    s = a.list_sessions("-w")[0]  # same process: the context cache must notice
    assert (s.context_tokens, s.context_limit_known) == (250_000, True)


def test_v2_mixed_part_storage_keeps_both(tmp_path, monkeypatch):
    db = tmp_path / "mixed.db"
    _v2_db(db, [("assistant", {"model": {"id": "m"}, "content": [{"type": "text", "text": "tail"}]})])
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, "
                 "type TEXT, call_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute("INSERT INTO session_part VALUES ('m1', 's', 0, 'text', NULL, 1, 1, ?)",
                 (json.dumps({"type": "text", "text": "prefix"}),))
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    blocks = OpencodeAdapter().get_conversation("-w", "s")["turns"][0]["blocks"]
    assert [b["text"] for b in blocks] == ["prefix", "tail"]


# ---- locating the DB in v2 ----

def test_db_path_falls_back_to_opencode_debug_paths(opencode_v2_home, monkeypatch):
    db = opencode_v2_home / "opencode.db"
    monkeypatch.delenv("OPENCODE_DB")
    monkeypatch.setattr(oc, "_cli_paths", lambda: {"db": str(db)})
    assert oc.opencode_db_path() == db


def test_cli_paths_parses_opencode_debug_paths(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "home       /h\ndb         /x/opencode.db\n"
        stderr = ""

    oc._cli_paths.cache_clear()
    monkeypatch.setattr(oc, "_opencode_cli", lambda: "/fake/bin/opencode")
    monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: Proc())
    try:
        assert oc._cli_paths() == {"home": "/h", "db": "/x/opencode.db"}
    finally:
        oc._cli_paths.cache_clear()
