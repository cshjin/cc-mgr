"""#25 project-level delete; #26 projects whose working directory no longer exists."""
import hashlib
import itertools
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import index_db, store
from backend.agents import ProjectChanged, common
from backend.agents import copilot as cp
from backend.agents import opencode as oc
from backend.agents.agy import AgyAdapter
from backend.agents.common import mangle, project_names
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


def _cwd(project, agent="claude"):
    """The directory the sidebar shows for a project ("" if none or unknown)."""
    projects = client.get("/api/projects", params={"agent": agent}).json()
    return next((p.get("cwd") or "" for p in projects if p["name"] == project), "")


def _delete(project, agent="claude", **body):
    """POST a project delete the way the dialog does: confirming the directory
    the sidebar shows for the project, unless the test says otherwise."""
    body.setdefault("expect_cwd", _cwd(project, agent))
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


def _oc_project():
    return next(p["name"] for p in OpencodeAdapter().list_projects() if p["cwd"].endswith("repo_oc2"))


def test_opencode_subagents_are_exported_before_their_parent_takes_them(fake_opencode, tmp_path,
                                                                         monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    res = _delete(_oc_project(), agent="opencode", hard=True, export_first=True).json()
    assert sorted(Path(p).name.split(".")[0] for p in res["exported"]) == [
        "ses_old", "ses_v2_child", "ses_v2_parent"]


def test_opencode_cascade_into_another_directory_is_refused(fake_opencode, opencode_v2_home):
    conn = sqlite3.connect(opencode_v2_home / "opencode.db")
    conn.execute("UPDATE session_v2 SET directory = '/elsewhere' WHERE id = 'ses_v2_child'")
    conn.commit()
    conn.close()
    pname = _oc_project()
    pv = client.get(f"/api/projects/{pname}/delete-preview", params={"agent": "opencode"}).json()
    assert (pv["subagents"], pv["outside"]) == (0, 1)
    r = _delete(pname, agent="opencode", hard=True)
    assert r.status_code == 400 and "other directories" in r.json()["detail"]
    assert fake_opencode == []  # nothing was sent to OpenCode
    assert _delete(pname, agent="opencode").status_code == 200  # archiving doesn't cascade


def test_opencode_archived_subagents_are_counted_and_exported(fake_opencode, opencode_v2_home,
                                                              tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    conn = sqlite3.connect(opencode_v2_home / "opencode.db")
    conn.execute("UPDATE session_v2 SET time_archived = 5 WHERE id = 'ses_v2_child'")
    conn.commit()
    conn.close()
    pname = _oc_project()
    pv = client.get(f"/api/projects/{pname}/delete-preview", params={"agent": "opencode"}).json()
    assert (pv["sessions"], pv["subagents"], pv["hidden"]) == (2, 0, 1)
    res = _delete(pname, agent="opencode", hard=True, export_first=True).json()
    assert "ses_v2_child" in res["sessions"]
    assert "ses_v2_child" in {Path(p).name.split(".")[0] for p in res["exported"]}


def test_directories_that_mangle_alike_get_distinct_projects(copilot_store_home, tmp_path):
    names = project_names(["/w/repo-x", "/w/repo/x", "/w/other"])
    assert names["/w/other"] == "-w-other"  # a unique path keeps its plain name
    assert names["/w/repo-x"] != names["/w/repo/x"]
    assert project_names(["/w/repo/x", "/w/repo-x"]) == {k: names[k] for k in ("/w/repo/x", "/w/repo-x")}

    a_b, a_slash_b = str(tmp_path / "a-b"), str(tmp_path / "a" / "b")
    _cp_add(copilot_store_home, "cc-1", a_b)
    _cp_add(copilot_store_home, "dd-2", a_slash_b)
    by_cwd = {p["cwd"]: p["name"] for p in CopilotAdapter().list_projects()}
    assert by_cwd[a_b] != by_cwd[a_slash_b]
    res = _delete(by_cwd[a_b], agent="copilot").json()
    assert res["sessions"] == ["cc-1"] and res["project_removed"]  # the other one is untouched
    # without the collision, the survivor's name is plain again (the UI reloads the list)
    after = {p["cwd"]: p["name"] for p in CopilotAdapter().list_projects()}
    assert after[a_slash_b] == project_names([a_slash_b])[a_slash_b] and a_b not in after
    assert [s.session_id for s in CopilotAdapter().list_sessions(after[a_slash_b])] == ["dd-2"]


def _cp_add(home, sid, cwd, text="hi"):
    """A DB-only Copilot session (idle: last active long ago) in directory cwd."""
    conn = sqlite3.connect(home / "session-store.db")
    conn.execute("INSERT INTO sessions VALUES (?, ?, NULL, NULL, NULL, ?, "
                 "'2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')", (sid, cwd, sid))
    conn.execute("INSERT INTO turns (session_id, turn_index, user_message, timestamp) "
                 "VALUES (?, 0, ?, '2026-01-01T00:00:00.000Z')", (sid, text))
    conn.commit()
    conn.close()


def test_generated_names_never_meet_plain_ones():
    a, b = "/w/repo-x", "/w/repo/x"
    # a real directory named exactly like a's generated name, and one that merely looks generated
    c = a + "~" + hashlib.sha256(a.encode()).hexdigest()[:12]
    lone = "/w/notes~0123456789abcdef"
    expected = project_names([a, b, c, lone])
    for order in itertools.permutations([a, b, c, lone]):
        assert project_names(order) == expected  # the order doesn't matter
    assert len(set(expected.values())) == 4
    assert expected[a] == mangle(c)  # a's generated name is c's mangled path...
    assert expected[c] != mangle(c)  # ...so c can't have it: it looks generated, so it is hashed
    assert expected[lone].startswith(mangle(lone) + "~") and expected[lone] != mangle(lone)
    assert project_names(["/w/plain~notes"]) == {"/w/plain~notes": "-w-plain~notes"}


def test_clashing_hashes_grow_until_the_names_are_unique(monkeypatch):
    real = hashlib.sha256

    class Weak:  # the first 12 hex digits are the same for every path
        def __init__(self, data):
            self.digest = real(data).hexdigest()

        def hexdigest(self):
            return "0" * 12 + self.digest[12:]

    monkeypatch.setattr(common.hashlib, "sha256", Weak)
    names = project_names(["/w/repo-x", "/w/repo/x"])
    assert len(set(names.values())) == 2
    assert {len(n.rsplit("~", 1)[1]) for n in names.values()} == {16}

    class Constant:
        def __init__(self, data):
            pass

        def hexdigest(self):
            return "0" * 64

    monkeypatch.setattr(common.hashlib, "sha256", Constant)
    with pytest.raises(ValueError, match="not unique"):
        project_names(["/w/repo-x", "/w/repo/x"])


def test_a_stale_confirmation_never_deletes_another_directory(copilot_store_home, tmp_path):
    a, b = str(tmp_path / "a-b"), str(tmp_path / "a" / "b")
    _cp_add(copilot_store_home, "cc-1", a)
    name = next(p["name"] for p in CopilotAdapter().list_projects() if p["cwd"] == a)
    assert name == mangle(a)  # the dialog shows a under its plain name...
    _cp_add(copilot_store_home, "dd-2", b)  # ...then a directory that mangles alike appears
    hashed_a = next(p["name"] for p in CopilotAdapter().list_projects() if p["cwd"] == a)
    CopilotAdapter().delete_project(hashed_a, hard=True)  # and another client deletes a
    assert next(p["name"] for p in CopilotAdapter().list_projects() if p["cwd"] == b) == name
    r = _delete(name, agent="copilot", hard=True, expect_cwd=a)  # the old confirmation
    assert r.status_code == 409 and "reload" in r.json()["detail"]
    assert [s.session_id for s in CopilotAdapter().list_sessions(name)] == ["dd-2"]


def test_the_adapter_checks_the_directory_again_while_listing(copilot_store_home, monkeypatch):
    pname = str(copilot_store_home.parent / "repo_cp").replace("/", "-")
    with pytest.raises(ProjectChanged):
        CopilotAdapter().delete_project(pname, hard=True, expect_cwd="/somewhere/else")
    # the name moves between the route's check and the adapter's listing
    real = CopilotAdapter.list_projects
    monkeypatch.setattr(CopilotAdapter, "list_projects",
                        lambda self: [{**p, "cwd": "/confirmed"} for p in real(self)])
    r = _delete(pname, agent="copilot", hard=True, expect_cwd="/confirmed")
    assert r.status_code == 409
    assert sorted(s.session_id for s in CopilotAdapter().list_sessions(pname)) == ["aaaa-1", "bbbb-2"]


def test_opencode_checks_the_directory_before_anything(fake_opencode, tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    for hard in (False, True):
        with pytest.raises(ProjectChanged):
            OpencodeAdapter().delete_project(_oc_project(), hard=hard, export_first=True,
                                             expect_cwd="/somewhere/else")
    assert fake_opencode == [] and not (tmp_path / "exports").exists()


def _clone_session(db, src, **changes):
    """Another session_v2 row, like src but with changes (e.g. a new subagent)."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = {**dict(conn.execute("SELECT * FROM session_v2 WHERE id = ?", (src,)).fetchone()), **changes}
    conn.execute(f"INSERT INTO session_v2 ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                 list(row.values()))
    conn.commit()
    conn.close()


def test_opencode_refuses_subagents_that_joined_after_the_check(fake_opencode, opencode_v2_home,
                                                                tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    db = opencode_v2_home / "opencode.db"
    real_export = OpencodeAdapter.export_session_to_file

    def export_while_opencode_spawns_subagents(self, project, sid, out_dir=None):
        if sid == "ses_v2_parent":  # after the up-front cascade check, during the exports
            _clone_session(db, "ses_v2_child", id="ses_late_out", directory="/elsewhere")
            _clone_session(db, "ses_v2_child", id="ses_late_in", parent_id="ses_v2_child")
        return real_export(self, project, sid, out_dir)

    monkeypatch.setattr(OpencodeAdapter, "export_session_to_file", export_while_opencode_spawns_subagents)
    r = _delete(_oc_project(), agent="opencode", hard=True, export_first=True)
    assert r.status_code == 200, r.text
    res = r.json()
    assert sorted(res["errors"]) == ["ses_v2_child", "ses_v2_parent"]
    assert "ses_late_out" in res["errors"]["ses_v2_parent"]
    assert "ses_late_in" in res["errors"]["ses_v2_child"]
    assert fake_opencode == [["session", "delete", "ses_old"]] and res["sessions"] == ["ses_old"]
    conn = sqlite3.connect(db)
    left = {r[0] for r in conn.execute("SELECT id FROM session_v2")}
    conn.close()
    assert {"ses_v2_parent", "ses_v2_child", "ses_late_out", "ses_late_in"} <= left


def _move_during_export(monkeypatch, db, when, moved):
    """While `when` is being exported, OpenCode moves the session `moved` to
    another directory (approved by id a moment ago, not by place any more)."""
    real_export = OpencodeAdapter.export_session_to_file

    def export(self, project, sid, out_dir=None):
        if sid == when:
            conn = sqlite3.connect(db)
            conn.execute("UPDATE session_v2 SET directory = '/another/project' WHERE id = ?", (moved,))
            conn.commit()
            conn.close()
        return real_export(self, project, sid, out_dir)

    monkeypatch.setattr(OpencodeAdapter, "export_session_to_file", export)


def test_opencode_never_deletes_a_session_that_moved_away(fake_opencode, opencode_v2_home,
                                                          tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    db = opencode_v2_home / "opencode.db"
    _move_during_export(monkeypatch, db, "ses_v2_parent", "ses_v2_child")
    res = _delete(_oc_project(), agent="opencode", hard=True, export_first=True).json()
    # the parent would take the moved child along; the child itself is elsewhere now
    assert sorted(res["errors"]) == ["ses_v2_child", "ses_v2_parent"]
    assert "ses_v2_child" in res["errors"]["ses_v2_parent"]
    assert fake_opencode == [["session", "delete", "ses_old"]]
    conn = sqlite3.connect(db)
    left = {r[0]: r[1] for r in conn.execute("SELECT id, directory FROM session_v2")}
    conn.close()
    assert left["ses_v2_child"] == "/another/project" and "ses_v2_parent" in left


def test_opencode_never_archives_a_session_that_moved_away(fake_opencode, opencode_v2_home,
                                                           tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    _move_during_export(monkeypatch, opencode_v2_home / "opencode.db", "ses_v2_parent", "ses_old")
    res = _delete(_oc_project(), agent="opencode", export_first=True).json()
    assert list(res["errors"]) == ["ses_old"] and "moved" in res["errors"]["ses_old"]
    assert sorted(c[3] for c in fake_opencode) == ["sessionID=ses_v2_child", "sessionID=ses_v2_parent"]


def test_a_doc_editor_never_writes_into_another_directory(copilot_store_home, tmp_path):
    a, b = tmp_path / "a-b", tmp_path / "a" / "b"
    a.mkdir()
    b.mkdir(parents=True)
    (b / "AGENTS.md").write_text("b's doc")
    _cp_add(copilot_store_home, "cc-1", str(a))
    name = mangle(str(a))
    shown = client.get(f"/api/projects/{name}/doc", params={"agent": "copilot"}).json()
    assert shown["cwd"] == str(a)  # the editor shows a's AGENTS.md
    _cp_add(copilot_store_home, "dd-2", str(b))  # b appears, and another client deletes a:
    CopilotAdapter().delete_project(next(p["name"] for p in CopilotAdapter().list_projects()
                                         if p["cwd"] == str(a)), hard=True)
    assert next(p["name"] for p in CopilotAdapter().list_projects() if p["cwd"] == str(b)) == name

    def save(content, expect_cwd):
        return client.put(f"/api/projects/{name}/doc", params={"agent": "copilot"},
                          json={"content": content, "expect_cwd": expect_cwd})

    r = save("a's edit", shown["cwd"])  # the editor that still shows a
    assert r.status_code == 409 and "reload" in r.json()["detail"]
    assert (b / "AGENTS.md").read_text() == "b's doc" and not (a / "AGENTS.md").exists()
    assert save("b's edit", str(b)).status_code == 200  # an editor that shows b
    assert (b / "AGENTS.md").read_text() == "b's edit"
    r = client.put(f"/api/projects/{name}/doc", params={"agent": "copilot"}, json={"content": "x"})
    assert r.status_code == 422  # a save must say which directory it means


def test_claude_doc_and_memory_saves_check_what_the_editor_showed(claude_home):
    doc = client.get(f"/api/projects/{CLAUDE_PROJ}/doc").json()
    mem = client.get(f"/api/projects/{CLAUDE_PROJ}/memory").json()
    claude_md = Path(doc["cwd"]) / "CLAUDE.md"
    before = claude_md.read_text()
    r = client.put(f"/api/projects/{CLAUDE_PROJ}/doc", json={"content": "x", "expect_cwd": "/elsewhere"})
    assert r.status_code == 409 and claude_md.read_text() == before
    r = client.put(f"/api/projects/{CLAUDE_PROJ}/memory",
                   json={"name": "n.md", "content": "x", "expect_dir": "/elsewhere"})
    assert r.status_code == 409 and not (Path(mem["dir"]) / "n.md").exists()
    assert client.put(f"/api/projects/{CLAUDE_PROJ}/doc",
                      json={"content": "new", "expect_cwd": doc["cwd"]}).status_code == 200
    assert client.put(f"/api/projects/{CLAUDE_PROJ}/memory",
                      json={"name": "n.md", "content": "x", "expect_dir": mem["dir"]}).status_code == 200
    assert claude_md.read_text() == "new" and (Path(mem["dir"]) / "n.md").read_text() == "x"


def test_agy_memory_save_checks_the_directory(agy_home):
    pname = AgyAdapter().list_projects()[0]["name"]
    mem = client.get(f"/api/projects/{pname}/memory", params={"agent": "agy"}).json()
    rules = Path(mem["dir"]) / ".agents" / "rules"
    r = client.put(f"/api/projects/{pname}/memory", params={"agent": "agy"},
                   json={"name": "r.md", "content": "x", "expect_dir": "/elsewhere"})
    assert r.status_code == 409 and not (rules / "r.md").exists()
    r = client.put(f"/api/projects/{pname}/memory", params={"agent": "agy"},
                   json={"name": "r.md", "content": "x", "expect_dir": mem["dir"]})
    assert r.status_code == 200 and (rules / "r.md").read_text() == "x"


def test_the_index_follows_a_renamed_project(copilot_store_home, tmp_path):
    a, b = str(tmp_path / "a-b"), str(tmp_path / "a" / "b")
    _cp_add(copilot_store_home, "cc-1", a)
    _cp_add(copilot_store_home, "dd-2", b, text="zebrafish")
    index_db.reindex()
    hashed = {p["cwd"]: p["name"] for p in CopilotAdapter().list_projects()}
    assert [h["project"] for h in index_db.search("zebrafish", agent="copilot")] == [hashed[b]]
    CopilotAdapter().delete_project(hashed[a], hard=True)  # without a, b's name is plain again
    index_db.reindex()  # b's session is unchanged, but its project is now called mangle(b)
    assert [h["project"] for h in index_db.search("zebrafish", agent="copilot")] == [mangle(b)]
    assert index_db.search("zebrafish", agent="copilot", project=mangle(b))


def test_a_removed_projects_docs_leave_the_index(agy_home):
    index_db.reindex()
    assert index_db.search("findings", agent="agy")  # brain/<id>/report.md
    res = _delete(AgyAdapter().list_projects()[0]["name"], agent="agy").json()
    assert res["project_removed"] is True
    assert index_db.search("findings", agent="agy") == []


def test_one_failing_session_does_not_stop_the_rest(copilot_store_home, monkeypatch):
    monkeypatch.setattr(cp, "_busy_reason", lambda home, sid, updated=0.0:
                        "it is open in a running Copilot CLI" if sid == "aaaa-1" else "")
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
