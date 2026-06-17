import os, tempfile, json, shutil
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["CLAUDE_HOME"] = str(tmp)
from backend import store

proj = tmp / "projects" / "demo"
proj.mkdir(parents=True)
sid = "abcdef12-0000-0000-0000-000000000000"
recs = [
    {"type": "user", "uuid": "u1", "cwd": "C:/x", "gitBranch": "main",
     "message": {"role": "user", "content": "first prompt here"}},
    {"type": "assistant", "uuid": "a1", "attributionSkill": "deep-research",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                 "model": "claude-opus-4-7", "usage": {"output_tokens": 5, "input_tokens": 10}}},
    {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "last prompt here"}},
]
(proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
(tmp / "tasks" / sid).mkdir(parents=True)
(tmp / "tasks" / sid / "1.json").write_text(json.dumps({"id": "1", "subject": "build thing", "status": "completed"}))

out = store.save_session_as_memory("demo", sid)
print("wrote:", out.name)
content = out.read_text(encoding="utf-8")
assert "first prompt here" in content
assert "last prompt here" in content
assert "deep-research" in content
assert "build thing" in content
print("--- memory file ---")
print(content)
idx = (proj / "memory" / "MEMORY.md").read_text(encoding="utf-8")
assert "session_abcdef12" in idx, idx
print("--- MEMORY.md ---")
print(idx)
shutil.rmtree(tmp)
print("ALL OK")
