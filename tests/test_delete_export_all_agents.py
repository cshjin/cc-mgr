"""#24: Markdown export for every agent; delete for Codex, Copilot CLI and Gemini CLI."""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.agents import codex as cx
from backend.agents import copilot as cp
from backend.agents.codex import CodexAdapter
from backend.agents.common import render_markdown
from backend.agents.copilot import CopilotAdapter
from backend.agents.gemini import GeminiAdapter
from backend.app import app

client = TestClient(app)
GEMINI_SID = "session-2026-06-03T23-38-ab4cc0f2"
UUID = "019e6c93-e44b-7c33-ae62-48ccb9c2eb1e"
HAS_PROC = Path("/proc/self/cmdline").exists()


def _spawn_copilot_stand_in():
    """A live process whose command line mentions copilot. Popen can return
    while the child is still inside execve (its /proc cmdline empty), so wait."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "copilot"])
    deadline = time.time() + 10
    while b"copilot" not in Path(f"/proc/{proc.pid}/cmdline").read_bytes():
        assert time.time() < deadline, "the stand-in never showed its command line"
        time.sleep(0.05)
    return proc


def _first_project(adapter):
    return adapter.list_projects()[0]["name"]


# ---- export: every agent ----

def test_gemini_export(gemini_home):
    r = client.get(f"/api/projects/repo_gemini/sessions/{GEMINI_SID}/export", params={"agent": "gemini"})
    assert r.status_code == 200, r.text
    assert "hello gemini" in r.text and "hello from gemini" in r.text
    assert "<session_context>" not in r.text  # the synthetic prelude stays out
    assert client.get("/api/projects/repo_gemini/sessions/session-nope/export",
                      params={"agent": "gemini"}).status_code == 404


def test_codex_export(codex_home):
    md = CodexAdapter().export_session_markdown(_first_project(CodexAdapter()), "rollout-2026-06-03-xyz")
    assert "**Agent:** Codex" in md
    assert "hello codex" in md and "exec_command" in md and "a.py" in md


def test_copilot_export_uses_the_session_title(copilot_store_home):
    a = CopilotAdapter()
    pname = _cp_project(copilot_store_home)
    md = a.export_session_markdown(pname, "aaaa-1")
    assert md.startswith("# Session: Fix the build") and "please fix the build" in md
    with pytest.raises(FileNotFoundError):
        a.export_session_markdown(pname, "nope")


def test_export_to_file_is_private(codex_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    pname = _first_project(CodexAdapter())
    out = CodexAdapter().export_session_to_file(pname, "rollout-2026-06-03-xyz")
    assert out.parent == tmp_path / "exports" / pname
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and "hello codex" in out.read_text()


def test_markdown_fences_survive_backticks_in_content():
    md = render_markdown("X", "p", "s", [{"role": "user", "blocks": [
        {"type": "tool_result", "text": "```\nnot the end\n```"}]}])
    assert "````\n```\nnot the end\n```\n````" in md


# ---- Gemini CLI: retired, files are moved ----

def test_gemini_soft_delete_moves_the_chat_to_trash(gemini_home):
    r = client.post(f"/api/projects/repo_gemini/sessions/{GEMINI_SID}/delete",
                    params={"agent": "gemini"}, json={"export_first": False})
    assert r.status_code == 200, r.text
    trash = Path(r.json()["trash"])
    assert trash.parent == gemini_home / ".cc_mgr_trash"
    assert (trash / "tmp" / "repo_gemini" / "chats" / f"{GEMINI_SID}.jsonl").is_file()
    assert GeminiAdapter().list_sessions("repo_gemini") == []


def test_gemini_hard_delete_and_bad_ids(gemini_home):
    a = GeminiAdapter()
    with pytest.raises(ValueError):
        a.delete_session("..", GEMINI_SID, hard=True)
    with pytest.raises(FileNotFoundError):
        a.delete_session("repo_gemini", "session-nope", hard=True)
    a.delete_session("repo_gemini", GEMINI_SID, hard=True)
    assert not list((gemini_home / "tmp" / "repo_gemini" / "chats").iterdir())
    assert not (gemini_home / ".cc_mgr_trash").exists()


# ---- Codex: through the codex CLI ----

@pytest.fixture
def codex_uuid(codex_home):
    """The fixture's rollout, keyed by a real Codex (UUID) id."""
    src = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    lines = src.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["id"] = UUID
    dst = src.with_name(f"rollout-2026-06-03T10-00-00-{UUID}.jsonl")
    dst.write_text("\n".join([json.dumps(meta), *lines[1:]]), encoding="utf-8")
    src.unlink()
    return dst


@pytest.fixture
def fake_codex(codex_uuid, monkeypatch):
    """Stands in for the codex CLI: archive moves the rollout, delete removes it."""
    calls = []

    def run(args, home):
        calls.append((args, home))
        path = next(home.glob(f"sessions/**/rollout-*{args[-1]}.jsonl"))
        if args[0] == "archive":
            (home / "archived_sessions").mkdir(exist_ok=True)
            path.rename(home / "archived_sessions" / path.name)
        elif args[:2] == ["delete", "--force"]:
            path.unlink()

    monkeypatch.setattr(cx, "_run_codex", run)
    return calls


def _codex_delete(path, **body):
    return client.post(f"/api/projects/{_first_project(CodexAdapter())}/sessions/{path.stem}/delete",
                       params={"agent": "codex"}, json={"export_first": False, **body})


def test_codex_soft_delete_archives_through_the_cli(fake_codex, codex_uuid, codex_home):
    a = CodexAdapter()
    res = a.delete_session(_first_project(a), codex_uuid.stem, hard=False)
    assert fake_codex == [(["archive", UUID], codex_home)]
    assert f"codex unarchive {UUID}" in res["note"]
    assert a.list_projects() == []  # archived rollouts aren't listed


def test_codex_hard_delete_goes_through_the_cli(fake_codex, codex_uuid, codex_home):
    r = _codex_delete(codex_uuid, export_first=True, hard=True)
    assert r.status_code == 200, r.text
    assert fake_codex == [(["delete", "--force", UUID], codex_home)]
    assert Path(r.json()["export"]).is_file() and not codex_uuid.exists()


def test_codex_cli_that_did_nothing_is_an_error(codex_uuid, monkeypatch):
    monkeypatch.setattr(cx, "_run_codex", lambda args, home: None)
    r = _codex_delete(codex_uuid)
    assert r.status_code == 502 and "did not archive" in r.json()["detail"]
    assert codex_uuid.exists()


def test_codex_without_the_cli_is_refused(codex_uuid):
    r = _codex_delete(codex_uuid)  # the isolation fixture hides every agent CLI
    assert r.status_code == 404 and "codex" in r.json()["detail"]
    assert codex_uuid.exists()


def test_codex_session_without_a_uuid_is_not_sent_to_the_cli(codex_home, monkeypatch):
    monkeypatch.setattr(cx, "_run_codex", lambda args, home: pytest.fail("must not call codex"))
    r = client.post(f"/api/projects/{_first_project(CodexAdapter())}/sessions/rollout-2026-06-03-xyz/delete",
                    params={"agent": "codex"}, json={"export_first": False})
    assert r.status_code == 502 and "Codex id" in r.json()["detail"]


OTHER = "019e6c93-0000-7c33-ae62-48ccb9c2eb1e"


def _rekey(path, meta_id):
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["id"] = meta_id
    path.write_text("\n".join([json.dumps(meta), *lines[1:]]), encoding="utf-8")


def test_codex_refuses_ids_that_disagree_or_repeat(codex_uuid, codex_home, monkeypatch):
    monkeypatch.setattr(cx, "_run_codex", lambda args, home: pytest.fail("must not call codex"))
    a = CodexAdapter()
    pname = _first_project(a)
    _rekey(codex_uuid, OTHER)  # the file name says UUID, session_meta says OTHER
    with pytest.raises(RuntimeError, match="disagree"):
        a.delete_session(pname, codex_uuid.stem)
    _rekey(codex_uuid, UUID)
    archived = codex_home / "archived_sessions" / f"rollout-2026-01-01T00-00-00-{UUID}.jsonl"
    archived.parent.mkdir()
    shutil.copy(codex_uuid, archived)  # another rollout with the same id: ambiguous for the CLI
    with pytest.raises(RuntimeError, match="same Codex id"):
        a.delete_session(pname, codex_uuid.stem)
    assert codex_uuid.exists() and archived.exists()


def test_codex_collateral_removal_is_reported(codex_uuid, monkeypatch):
    other = codex_uuid.with_name(f"rollout-2026-06-03T11-00-00-{OTHER}.jsonl")
    shutil.copy(codex_uuid, other)
    _rekey(other, OTHER)

    def greedy(args, home):  # a CLI that removes more than it was asked to
        for p in home.glob("sessions/**/rollout-*.jsonl"):
            p.unlink()

    monkeypatch.setattr(cx, "_run_codex", greedy)
    with pytest.raises(RuntimeError, match="also removed other sessions"):
        CodexAdapter().delete_session(_first_project(CodexAdapter()), codex_uuid.stem, hard=True)


def test_run_codex_targets_cc_mgrs_codex_home_and_reports_failures(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "WARNING: proceeding, even though …\nError: failed to archive session\n"

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return Proc()

    monkeypatch.setattr(cx, "_codex_cli", lambda: "/fake/bin/codex")
    monkeypatch.setattr(cx.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Error: failed to archive session"):
        cx._run_codex(["archive", UUID], tmp_path)
    assert seen["cmd"] == ["/fake/bin/codex", "archive", UUID]
    assert seen["env"]["CODEX_HOME"] == str(tmp_path)


# ---- Copilot CLI: no delete command, so cc_mgr edits its store ----

def _cp_project(home):
    return str(home.parent / "repo_cp").replace("/", "-").replace("\\", "-").replace(":", "-")


def _age(home, sid, seconds=3600):
    """Make a session look idle: deletes refuse sessions active moments ago."""
    d = home / "session-state" / sid
    old = time.time() - seconds
    for p in ([d, *d.iterdir()] if d.is_dir() else []):
        os.utime(p, (old, old))


def _rows(home, sid):
    conn = sqlite3.connect(home / "session-store.db")
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} = ?", (sid,)).fetchone()[0]
                  for t, c in (("sessions", "id"), ("turns", "session_id"),
                               ("assistant_usage_events", "session_id"))}
        counts["search_index"] = [r[0] for r in conn.execute("SELECT session_id FROM search_index")]
        return counts
    finally:
        conn.close()


@pytest.fixture
def store(copilot_store_home):
    """The fixture store plus Copilot's FTS5 search_index (its shadow tables included)."""
    conn = sqlite3.connect(copilot_store_home / "session-store.db")
    conn.execute("CREATE VIRTUAL TABLE search_index USING fts5(content, session_id UNINDEXED, "
                 "source_type UNINDEXED, source_id UNINDEXED)")
    conn.executemany("INSERT INTO search_index VALUES (?, ?, 'turn', '1')",
                     [("fix the build", "aaaa-1"), ("db prompt", "bbbb-2")])
    conn.commit()
    conn.close()
    return copilot_store_home


def test_copilot_soft_delete_backs_up_then_removes(store):
    _age(store, "aaaa-1")
    res = CopilotAdapter().delete_session(_cp_project(store), "aaaa-1", hard=False)
    trash = Path(res["trash"])
    backup = json.loads((trash / "db_rows.json").read_text(encoding="utf-8"))
    assert backup["session_id"] == "aaaa-1"
    assert backup["rows"]["sessions"][0]["summary"] == "Fix the build"
    assert backup["rows"]["turns"][0]["user_message"] == "please fix the build"
    assert backup["rows"]["search_index"][0]["content"] == "fix the build"
    assert (trash / "session-state" / "events.jsonl").is_file()
    assert not (store / "session-state" / "aaaa-1").exists()
    assert _rows(store, "aaaa-1") == {"sessions": 0, "turns": 0, "assistant_usage_events": 0,
                                      "search_index": ["bbbb-2"]}
    assert _rows(store, "bbbb-2")["turns"] == 2  # other sessions untouched
    assert "aaaa-1" not in {s.session_id for s in CopilotAdapter().list_sessions(_cp_project(store))}


def test_copilot_hard_delete(store):
    r = client.post(f"/api/projects/{_cp_project(store)}/sessions/bbbb-2/delete",
                    params={"agent": "copilot"}, json={"export_first": False, "hard": True})
    assert r.status_code == 200, r.text
    assert r.json()["trash"] is None
    assert _rows(store, "bbbb-2") == {"sessions": 0, "turns": 0, "assistant_usage_events": 0,
                                      "search_index": ["aaaa-1"]}


@pytest.mark.skipif(not HAS_PROC, reason="needs /proc")
def test_copilot_refuses_a_session_open_in_a_running_copilot(store):
    holder = _spawn_copilot_stand_in()
    try:
        (store / "session-state" / "aaaa-1" / f"inuse.{holder.pid}.lock").write_text(str(holder.pid))
        _age(store, "aaaa-1")
        r = client.post(f"/api/projects/{_cp_project(store)}/sessions/aaaa-1/delete",
                        params={"agent": "copilot"}, json={"export_first": False})
        assert r.status_code == 502 and f"pid {holder.pid}" in r.json()["detail"]
        assert _rows(store, "aaaa-1")["sessions"] == 1
    finally:
        holder.kill()
        holder.wait()
    _age(store, "aaaa-1")  # the holder is gone: its stale lock no longer blocks the delete
    r = client.post(f"/api/projects/{_cp_project(store)}/sessions/aaaa-1/delete",
                    params={"agent": "copilot"}, json={"export_first": False})
    assert r.status_code == 200, r.text


@pytest.mark.skipif(not HAS_PROC, reason="needs /proc")
def test_only_a_live_copilot_process_counts():
    holder = _spawn_copilot_stand_in()
    try:
        assert cp._is_copilot_process(holder.pid)
        if b"copilot" not in Path("/proc/self/cmdline").read_bytes().lower():
            assert not cp._is_copilot_process(os.getpid())  # alive, but not Copilot (PID reuse)
    finally:
        holder.kill()
        holder.wait()
    assert not cp._is_copilot_process(holder.pid)


def test_copilot_refuses_a_session_active_moments_ago(store):
    with pytest.raises(RuntimeError, match="active in the last two minutes"):
        CopilotAdapter().delete_session(_cp_project(store), "aaaa-1")  # fixture files are fresh
    assert (store / "session-state" / "aaaa-1").is_dir()


def test_copilot_db_only_session_active_moments_ago_is_refused(store):
    # no session-state folder: only the store's own updated_at says it is in use
    conn = sqlite3.connect(store / "session-store.db")
    conn.execute("UPDATE sessions SET updated_at = ? WHERE id = 'bbbb-2'",
                 (time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),))
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="active in the last two minutes"):
        CopilotAdapter().delete_session(_cp_project(store), "bbbb-2", hard=True)
    assert _rows(store, "bbbb-2")["sessions"] == 1


def _refuse_deletes_from(store, table):
    """Make the store refuse a DELETE on one table, after others already went."""
    conn = sqlite3.connect(store / "session-store.db")
    conn.execute(f"CREATE TRIGGER keep BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'refused'); END")
    conn.commit()
    conn.close()


def test_copilot_puts_everything_back_if_the_store_refuses(store):
    _age(store, "aaaa-1")
    _refuse_deletes_from(store, "sessions")  # the last table: every other delete already ran
    with pytest.raises(RuntimeError, match="nothing was deleted"):
        CopilotAdapter().delete_session(_cp_project(store), "aaaa-1")
    assert (store / "session-state" / "aaaa-1" / "events.jsonl").is_file()
    assert not any((store / ".cc_mgr_trash").iterdir())
    assert _rows(store, "aaaa-1") == {"sessions": 1, "turns": 1, "assistant_usage_events": 0,
                                      "search_index": ["aaaa-1", "bbbb-2"]}  # all rolled back


def test_copilot_rollback_never_overwrites_a_folder_that_reappeared(store, monkeypatch):
    _age(store, "aaaa-1")
    _refuse_deletes_from(store, "sessions")
    state = store / "session-state" / "aaaa-1"
    real_rename = os.rename

    def rename_then_copilot_recreates_it(src, dst):
        real_rename(src, dst)
        if Path(src) == state:  # the forward move: Copilot writes the folder again meanwhile
            state.mkdir()
            (state / "new.txt").write_text("new")

    monkeypatch.setattr(cp.os, "rename", rename_then_copilot_recreates_it)
    with pytest.raises(RuntimeError, match="previous copy is kept in"):
        CopilotAdapter().delete_session(_cp_project(store), "aaaa-1")
    assert sorted(p.name for p in state.iterdir()) == ["new.txt"]  # not nested, not overwritten
    kept = next((store / ".cc_mgr_trash").iterdir())
    assert (kept / "session-state" / "events.jsonl").is_file() and (kept / "db_rows.json").is_file()
    assert _rows(store, "aaaa-1")["sessions"] == 1


def test_copilot_backup_and_delete_see_the_same_rows(store, monkeypatch):
    # The write lock is held from before the backup until the commit: Copilot
    # can't add a row in between that would be deleted without being backed up.
    _age(store, "aaaa-1")
    blocked = []
    real = cp.new_trash_dir

    def try_to_write_meanwhile(home, name):
        other = sqlite3.connect(store / "session-store.db", timeout=0)
        try:
            other.execute("INSERT INTO turns VALUES (99, 'aaaa-1', 9, 'late', NULL, 'x')")
            other.commit()
        except sqlite3.OperationalError as e:
            blocked.append(str(e))
        finally:
            other.close()
        return real(home, name)

    monkeypatch.setattr(cp, "new_trash_dir", try_to_write_meanwhile)
    res = CopilotAdapter().delete_session(_cp_project(store), "aaaa-1")
    assert blocked and "locked" in blocked[0]
    backup = json.loads((Path(res["trash"]) / "db_rows.json").read_text(encoding="utf-8"))
    assert [r["user_message"] for r in backup["rows"]["turns"]] == ["please fix the build"]


def test_copilot_store_busy_means_nothing_happens(store, monkeypatch):
    _age(store, "aaaa-1")
    monkeypatch.setattr(cp, "_LOCK_TIMEOUT", 0.1)
    holder = sqlite3.connect(store / "session-store.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # Copilot is writing
    try:
        with pytest.raises(RuntimeError, match="busy.*nothing was deleted"):
            CopilotAdapter().delete_session(_cp_project(store), "aaaa-1")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert (store / "session-state" / "aaaa-1").is_dir() and not (store / ".cc_mgr_trash").exists()


def test_copilot_refuses_a_store_that_resolves_outside_its_home(store, tmp_path):
    outside = tmp_path / "elsewhere.db"
    os.rename(store / "session-store.db", outside)
    (store / "session-store.db").symlink_to(outside)
    _age(store, "aaaa-1")
    r = client.post(f"/api/projects/{_cp_project(store)}/sessions/aaaa-1/delete",
                    params={"agent": "copilot"}, json={"export_first": False, "hard": True})
    assert r.status_code == 400 and "outside" in r.json()["detail"]
    conn = sqlite3.connect(outside)
    assert conn.execute("SELECT COUNT(*) FROM turns WHERE session_id = 'aaaa-1'").fetchone()[0] == 1
    conn.close()


def test_pid_alive_fails_closed_on_windows(monkeypatch):
    from backend.agents import common

    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(common.os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())
    assert common.pid_alive(4242) is True  # tasklist failed: unknown counts as alive
