"""store.save_session_as_memory (formerly the throwaway _test_mem.py script)."""
import json

from backend import store


def test_save_session_as_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    sid = "abcdef12-0000-0000-0000-000000000000"
    records = [
        {"type": "user", "uuid": "u1", "cwd": "C:/x", "gitBranch": "main",
         "message": {"role": "user", "content": "first prompt here"}},
        {"type": "assistant", "uuid": "a1", "attributionSkill": "deep-research",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                     "model": "claude-opus-4-7", "usage": {"output_tokens": 5, "input_tokens": 10}}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "last prompt here"}},
    ]
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    (tmp_path / "tasks" / sid).mkdir(parents=True)
    (tmp_path / "tasks" / sid / "1.json").write_text(
        json.dumps({"id": "1", "subject": "build thing", "status": "completed"}), encoding="utf-8")

    out = store.save_session_as_memory("demo", sid)
    content = out.read_text(encoding="utf-8")
    for needle in ("first prompt here", "last prompt here", "deep-research", "build thing"):
        assert needle in content
    index = proj / "memory" / "MEMORY.md"
    assert "session_abcdef12" in index.read_text(encoding="utf-8")

    store.save_session_as_memory("demo", sid)  # saving again must not duplicate the entry
    assert index.read_text(encoding="utf-8").count("session_abcdef12") == 1
