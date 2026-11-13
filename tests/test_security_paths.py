"""Regression tests for #5: crafted ids must never reach the filesystem."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import store
from backend.agents.agy import AgyAdapter
from backend.agents.common import is_safe_id, is_safe_name, safe_join
from backend.app import app

client = TestClient(app)

# Percent-encoded so the test client sends them verbatim (it normalises a bare "..").
UNSAFE_IDS = ["%2E%2E", "%2E", "*", "a*b", "%3F", "%5Bx%5D", "-rf", ".hidden"]


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("sid", UNSAFE_IDS)
@pytest.mark.parametrize("agent", ["claude", "agy", "opencode"])
def test_delete_rejects_unsafe_session_ids(claude_home, agy_home, opencode_home, agent, sid):
    (agy_home / "conversations").mkdir(exist_ok=True)
    for name in ("c1.db", "c2.db", "c2.pb"):
        (agy_home / "conversations" / name).write_text("x")
    before = {h: _snapshot(h) for h in (claude_home, agy_home)}
    r = client.post(f"/api/projects/p1/sessions/{sid}/delete", params={"agent": agent},
                    json={"export_first": False, "hard": True})
    assert r.status_code == 400
    assert {h: _snapshot(h) for h in (claude_home, agy_home)} == before


@pytest.mark.parametrize("project", ["%2E%2E", "%2E"])
def test_project_traversal_rejected(claude_home, project):
    for path in (f"/api/projects/{project}/sessions", f"/api/projects/{project}/memory",
                 f"/api/projects/{project}/doc", f"/api/projects/{project}/tasks"):
        assert client.get(path).status_code == 400, path
    r = client.put(f"/api/projects/{project}/memory", json={"name": "x.md", "content": "pwned"})
    assert r.status_code == 400
    assert not (claude_home / "memory").exists()


def test_task_traversal_rejected(claude_home):
    settings = claude_home / "settings.json"
    settings.write_text("{}")
    r = client.patch("/api/sessions/%2E%2E/tasks/settings", json={"status": "completed"})
    assert r.status_code == 400
    assert settings.read_text() == "{}"


def test_store_and_agy_refuse_unsafe_ids_directly(claude_home, agy_home):
    with pytest.raises(ValueError):
        store.delete_session("-tmp-repo_claude", "..", hard=True)
    with pytest.raises(ValueError):
        store.update_task_status("..", "settings", "completed")
    with pytest.raises(ValueError):
        AgyAdapter().delete_session("x", "*", hard=True)
    assert (claude_home / "projects" / "-tmp-repo_claude" / "sess-c1.jsonl").is_file()


def test_legit_claude_delete_still_works(claude_home):
    r = client.post("/api/projects/-tmp-repo_claude/sessions/sess-c1/delete",
                    json={"export_first": False, "hard": False})
    assert r.status_code == 200, r.text
    assert r.json()["trash"]
    assert not (claude_home / "projects" / "-tmp-repo_claude" / "sess-c1.jsonl").exists()
    assert (claude_home / "projects").is_dir()


# ---- review follow-ups ----

def test_deleting_session_memory_cannot_remove_shared_memory(claude_home):
    mem = claude_home / "projects" / "-tmp-repo_claude" / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("keep me")
    r = client.post("/api/projects/-tmp-repo_claude/sessions/memory/delete",
                    json={"export_first": False, "hard": True})
    assert r.status_code == 400
    assert (mem / "MEMORY.md").read_text() == "keep me"


@pytest.mark.parametrize("agent", ["claude", "agy"])
def test_deleting_a_session_that_does_not_exist_is_404(claude_home, agy_home, agent):
    before = {h: _snapshot(h) for h in (claude_home, agy_home)}
    r = client.post("/api/projects/-tmp-repo_claude/sessions/nope/delete", params={"agent": agent},
                    json={"export_first": True, "hard": True})
    assert r.status_code == 404, r.text
    assert {h: _snapshot(h) for h in (claude_home, agy_home)} == before


def test_symlinked_project_dir_cannot_redirect_a_delete(claude_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.jsonl").write_text('{"type": "user", "message": {"content": "hi"}}')
    (claude_home / "projects" / "evil").symlink_to(outside, target_is_directory=True)
    r = client.post("/api/projects/evil/sessions/victim/delete",
                    json={"export_first": False, "hard": True})
    assert r.status_code == 400
    assert (outside / "victim.jsonl").exists()


def test_symlinked_memory_file_cannot_redirect_a_write(claude_home, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    mem = claude_home / "projects" / "-tmp-repo_claude" / "memory"
    mem.mkdir()
    (mem / "linked.md").symlink_to(outside)
    r = client.put("/api/projects/-tmp-repo_claude/memory",
                   json={"name": "linked.md", "content": "overwritten"})
    assert r.status_code == 400
    assert outside.read_text() == "original"


def test_exports_are_owner_only(claude_home, tmp_path, monkeypatch):
    import stat
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(tmp_path / "exports"))
    out = store.export_session_to_file("-tmp-repo_claude", "sess-c1")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "exports").stat().st_mode) == 0o700


def test_validators():
    assert is_safe_id("ses_e2e98941fffez5mDfexIFBs9vS")
    assert is_safe_id("session-2026-06-03T23-38-ab4cc0f2")
    assert not any(is_safe_id(v) for v in ("", ".", "..", "*", "a/b", "-x", "a b", None))
    assert is_safe_name("-global-cfs-cdirs-x") and is_safe_name("repo with space")
    assert not any(is_safe_name(v) for v in ("", ".", "..", "a/b", "a\\b", "a\x00b", None))
    with pytest.raises(ValueError):
        safe_join(Path("/tmp/root"), "..", "etc")
    assert safe_join(Path("/tmp/root"), "a", "b") == Path("/tmp/root/a/b")
