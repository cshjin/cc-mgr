"""#25 project-level delete; #26 projects whose working directory no longer exists."""
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import index_db, store
from backend.agents import copilot as cp
from backend.agents import opencode as oc
from backend.agents.agy import AgyAdapter
from backend.agents.copilot import CopilotAdapter
from backend.agents.gemini import GeminiAdapter
from backend.agents.opencode import OpencodeAdapter
from backend.app import app

client = TestClient(app)
CLAUDE_PROJ = "-tmp-repo_claude"


def _claude_extras(claude_home, memory=True):
    """A message-less transcript, a sidecar dir and (optionally) project memory."""
    proj = claude_home / "projects" / CLAUDE_PROJ
    (proj / "empty-1.jsonl").write_text(json.dumps({"type": "last-prompt"}) + "\n")
    (proj / "sess-c1").mkdir()
    (proj / "sess-c1" / "tool.txt").write_text("x")
    if memory:
        (proj / "memory").mkdir()
        (proj / "memory" / "MEMORY.md").write_text("# Memory\n- keep me\n")
    return proj


def _delete(project, agent="claude", **body):
    return client.post(f"/api/projects/{project}/delete", params={"agent": agent},
                       json={"export_first": False, **body})


# ---- Claude: the whole project folder ----

def test_claude_project_counts_match_the_session_list(claude_home):
    proj = _claude_extras(claude_home)
    p = store.list_projects()[0]
    assert (p["session_count"], p["has_memory"]) == (1, True)  # empty-1 has no messages
    (proj / "memory" / "MEMORY.md").unlink()
    assert store.list_projects()[0]["has_memory"] is False  # an empty memory dir isn't memory


def test_claude_preview(claude_home):
    _claude_extras(claude_home)
    r = client.get(f"/api/projects/{CLAUDE_PROJ}/delete-preview")
    assert r.status_code == 200, r.text
    pv = r.json()
    assert (pv["sessions"], pv["subagents"], pv["hidden"], pv["memory_files"]) == (1, 0, 1, 1)
    assert pv["newest"] > 0


def test_claude_soft_project_delete_keeps_memory_by_default(claude_home):
    proj = _claude_extras(claude_home)
    index_db.reindex()
    assert index_db.search("hello", agent="claude")
    r = _delete(CLAUDE_PROJ)
    assert r.status_code == 200, r.text
    res = r.json()
    assert sorted(res["sessions"]) == ["empty-1", "sess-c1"] and res["kept"] == ["memory"]
    trash = Path(res["trash"][0])
    assert trash.parent == claude_home / ".cc_mgr_trash"
    for name in ("sess-c1.jsonl", "empty-1.jsonl", "sess-c1/tool.txt"):
        assert (trash / "project" / name).is_file(), name
    assert (trash / "tasks" / "sess-c1" / "1.json").is_file()
    assert sorted(p.name for p in proj.iterdir()) == ["memory"]
    assert not (claude_home / "tasks" / "sess-c1").exists()
    assert index_db.search("hello", agent="claude") == []  # purged right away
    p = store.list_projects()[0]
    assert (p["session_count"], p["has_memory"], res["project_removed"]) == (0, True, False)
    assert (claude_home.parent / "repo_claude" / "CLAUDE.md").is_file()  # the repo is never touched


def test_claude_project_delete_with_memory_removes_the_project(claude_home):
    _claude_extras(claude_home)
    index_db.reindex()
    assert index_db.search("keep", agent="claude")  # the memory file is indexed
    res = _delete(CLAUDE_PROJ, include_memory=True).json()
    assert res["project_removed"] and res["kept"] == []
    assert (Path(res["trash"][0]) / "project" / "memory" / "MEMORY.md").is_file()
    assert store.list_projects() == []
    assert index_db.search("keep", agent="claude") == []  # the project's docs went too


def test_claude_empty_memory_dir_goes_with_the_folder(claude_home):
    proj = _claude_extras(claude_home, memory=False)
    (proj / "memory").mkdir()
    res = _delete(CLAUDE_PROJ).json()
    assert res["project_removed"] and not proj.exists()


def test_claude_empty_and_memory_only_projects(claude_home):
    empty = claude_home / "projects" / "-tmp"  # like the real ~/.claude/projects/-tmp
    (empty / "memory").mkdir(parents=True)
    pv = client.get("/api/projects/-tmp/delete-preview").json()
    assert (pv["sessions"], pv["hidden"], pv["memory_files"]) == (0, 0, 0)
    assert _delete("-tmp").json()["project_removed"] and not empty.exists()

    mem_only = claude_home / "projects" / "-mem-only"
    (mem_only / "memory").mkdir(parents=True)
    (mem_only / "memory" / "MEMORY.md").write_text("notes")
    res = _delete("-mem-only").json()  # memory kept: nothing to do, and no empty trash folder
    assert res["sessions"] == [] and res["trash"] == [] and res["kept"] == ["memory"]
    assert (mem_only / "memory" / "MEMORY.md").is_file()
    assert _delete("-mem-only", include_memory=True).json()["project_removed"]
    assert not mem_only.exists()


def test_claude_hard_project_delete(claude_home):
    proj = _claude_extras(claude_home)
    res = _delete(CLAUDE_PROJ, hard=True).json()
    assert res["trash"] == [] and sorted(p.name for p in proj.iterdir()) == ["memory"]
    assert not (claude_home / "tasks" / "sess-c1").exists()
    assert not (claude_home / ".cc_mgr_trash").exists()


def test_claude_project_delete_exports_first(claude_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    _claude_extras(claude_home)
    res = _delete(CLAUDE_PROJ, export_first=True).json()
    assert [Path(p).name.split(".")[0] for p in res["exported"]] == ["sess-c1"]  # empty-1: nothing
    assert "hello claude" in Path(res["exported"][0]).read_text()


def test_claude_refuses_a_symlinked_project_folder(claude_home):
    (claude_home / "projects" / "alias").symlink_to(claude_home / "projects" / CLAUDE_PROJ,
                                                     target_is_directory=True)
    assert _delete("alias", hard=True).status_code == 400
    assert (claude_home / "projects" / CLAUDE_PROJ / "sess-c1.jsonl").is_file()


# ---- other agents ----

def test_gemini_project_delete_moves_the_whole_project(gemini_home):
    chats = gemini_home / "tmp" / "repo_gemini" / "chats"
    (chats / "session-2026-06-04T00-00-0000beef.jsonl").write_text(json.dumps({"sessionId": "0000beef"}))
    assert GeminiAdapter().list_projects()[0]["session_count"] == 1  # the turn-less file isn't counted
    pv = client.get("/api/projects/repo_gemini/delete-preview", params={"agent": "gemini"}).json()
    assert (pv["sessions"], pv["hidden"]) == (1, 1)
    res = _delete("repo_gemini", agent="gemini").json()
    trash = Path(res["trash"][0])
    assert (trash / "tmp" / "repo_gemini" / "chats").is_dir()
    assert (trash / "history" / "repo_gemini" / ".project_root").is_file()
    assert len(res["sessions"]) == 2 and GeminiAdapter().list_projects() == []


def test_agy_project_delete_goes_session_by_session(agy_home):
    a = AgyAdapter()
    res = _delete(a.list_projects()[0]["name"], agent="agy").json()
    assert res["sessions"] == ["11111111-2222-3333-4444-555555555555"] and len(res["trash"]) == 1
    assert a.list_projects() == []


@pytest.fixture
def fake_opencode(opencode_v2_home, monkeypatch):
    """The running OpenCode service holds the fixture DB; record and apply its calls."""
    db = opencode_v2_home / "opencode.db"
    calls = []

    def run(args):
        calls.append(args)
        conn = sqlite3.connect(db)
        if args[:2] == ["session", "delete"]:
            conn.execute("DELETE FROM session_v2 WHERE id = ? OR parent_id = ?", (args[2], args[2]))
        elif args[:2] == ["api", "session.update"]:
            conn.execute("UPDATE session_v2 SET time_archived = 1 WHERE id = ?",
                         (args[3].split("=", 1)[1],))
        conn.commit()
        conn.close()

    monkeypatch.setattr(oc, "_opencode_cli", lambda: "/fake/bin/opencode")
    monkeypatch.setattr(oc, "_service_db_files", lambda: {os.path.realpath(db)})
    monkeypatch.setattr(oc, "_run_opencode", run)
    return calls


def test_opencode_permanent_project_delete_lets_parents_take_their_subagents(fake_opencode):
    pname = OpencodeAdapter().list_projects()[0]["name"]
    res = _delete(pname, agent="opencode", hard=True).json()
    assert [c[2] for c in fake_opencode] == ["ses_v2_parent", "ses_old"]  # the child went along
    assert sorted(res["sessions"]) == ["ses_old", "ses_v2_child", "ses_v2_parent"]
    assert OpencodeAdapter().list_projects() == []


def test_opencode_soft_project_delete_archives_every_session(fake_opencode):
    pname = OpencodeAdapter().list_projects()[0]["name"]
    res = _delete(pname, agent="opencode").json()
    assert sorted(c[3] for c in fake_opencode) == [
        "sessionID=ses_old", "sessionID=ses_v2_child", "sessionID=ses_v2_parent"]
    assert sorted(res["sessions"]) == ["ses_old", "ses_v2_child", "ses_v2_parent"]
    assert OpencodeAdapter().list_projects() == []


def test_a_project_delete_that_would_fail_for_every_session_is_refused(opencode_v2_home):
    pname = OpencodeAdapter().list_projects()[0]["name"]
    r = _delete(pname, agent="opencode")  # no OpenCode service: the DB is read-only here
    assert r.status_code == 404 and "read-only" in r.json()["detail"]


def test_one_failing_session_does_not_stop_the_rest(copilot_store_home, monkeypatch):
    monkeypatch.setattr(cp, "_busy_reason",
                        lambda home, sid: "it is open in a running Copilot CLI" if sid == "aaaa-1" else "")
    pname = str(copilot_store_home.parent / "repo_cp").replace("/", "-")
    res = _delete(pname, agent="copilot").json()
    assert list(res["errors"]) == ["aaaa-1"] and "running Copilot" in res["errors"]["aaaa-1"]
    assert res["sessions"] == ["bbbb-2"]
    assert [s.session_id for s in CopilotAdapter().list_sessions(pname)] == ["aaaa-1"]


def test_project_delete_is_a_write(claude_home, monkeypatch):
    monkeypatch.setenv("CC_MGR_READ_ONLY", "1")
    assert _delete(CLAUDE_PROJ).status_code == 403
    assert (claude_home / "projects" / CLAUDE_PROJ / "sess-c1.jsonl").is_file()


@pytest.mark.parametrize("project", ["%2E%2E", "%2E"])
def test_project_delete_rejects_traversal(claude_home, project):
    assert _delete(project, hard=True).status_code == 400
    assert client.get(f"/api/projects/{project}/delete-preview").status_code == 400
    assert (claude_home / "projects" / CLAUDE_PROJ / "sess-c1.jsonl").is_file()


# ---- #26: the working directory is gone (renamed / moved / another machine) ----

def test_projects_whose_directory_is_gone_are_flagged(claude_home):
    assert client.get("/api/projects").json()[0]["cwd_missing"] is False
    shutil.rmtree(claude_home.parent / "repo_claude")  # renamed or moved away
    p = client.get("/api/projects").json()[0]
    assert p["cwd_missing"] is True and p["session_count"] == 1  # the sessions are still here
    assert _delete(p["name"]).status_code == 200  # ...and can be cleaned up


def test_an_unknown_directory_is_not_reported_missing(claude_home):
    (claude_home / "projects" / "-memory-only" / "memory").mkdir(parents=True)
    projects = {p["name"]: p for p in client.get("/api/projects").json()}
    assert projects["-memory-only"]["cwd"] == "" and projects["-memory-only"]["cwd_missing"] is False
