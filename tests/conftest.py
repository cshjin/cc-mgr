"""Fixtures that build synthetic agent homes in a tmp dir.

Each fixture returns the home Path and sets the matching env var so adapters
resolve into the tmp tree instead of the real ~/. No real user data touched.
"""
import json
import os
from pathlib import Path

import pytest


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    cwd = tmp_path / "repo_claude"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text("# Claude project doc\nalpha\n", encoding="utf-8")
    proj = home / "projects" / "-tmp-repo_claude"
    _write_jsonl(proj / "sess-c1.jsonl", [
        {"type": "user", "uuid": "u1", "cwd": str(cwd), "gitBranch": "main",
         "timestamp": "2026-06-01T00:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello claude"}]}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-06-01T00:00:01Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "hi there"}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
    ])
    (home / "tasks" / "sess-c1").mkdir(parents=True)
    (home / "tasks" / "sess-c1" / "1.json").write_text(
        json.dumps({"id": "1", "subject": "do x", "status": "pending"}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


@pytest.fixture
def gemini_home(tmp_path, monkeypatch):
    home = tmp_path / "gemini"
    cwd = tmp_path / "repo_gemini"
    cwd.mkdir()
    (cwd / "GEMINI.md").write_text("# Gemini project doc\nbeta\n", encoding="utf-8")
    proj = home / "tmp" / "repo_gemini"
    (proj / "chats").mkdir(parents=True)
    (proj / ".project_root").write_text(str(cwd), encoding="utf-8")
    sess = proj / "chats" / "session-2026-06-03T23-38-ab4cc0f2.jsonl"
    sess.write_text("\n".join([
        json.dumps({"sessionId": "ab4cc0f2", "startTime": "2026-06-03T23:38:00Z",
                    "lastUpdated": "2026-06-03T23:38:10Z", "kind": "main"}),
        json.dumps({"$set": {"messages": [
            {"id": "m1", "timestamp": "2026-06-03T23:38:03Z", "type": "user",
             "content": [{"text": "hello gemini"}]},
            {"id": "m2", "timestamp": "2026-06-03T23:38:05Z", "type": "gemini",
             "content": [{"text": "hello from gemini"}]},
        ]}}),
    ]), encoding="utf-8")
    # history fallback for empty tmp .project_root case
    histroot = home / "history" / "repo_gemini"
    histroot.mkdir(parents=True)
    (histroot / ".project_root").write_text(str(cwd), encoding="utf-8")
    monkeypatch.setenv("GEMINI_HOME", str(home))
    return home


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    cwd = tmp_path / "repo_codex"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("# Codex agents doc\ngamma\n", encoding="utf-8")
    sess = home / "sessions" / "2026" / "06" / "03" / "rollout-2026-06-03-xyz.jsonl"
    _write_jsonl(sess, [
        {"type": "user", "uuid": "cu1", "cwd": str(cwd), "gitBranch": "main",
         "timestamp": "2026-06-03T10:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello codex"}]}},
        {"type": "assistant", "uuid": "ca1", "timestamp": "2026-06-03T10:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi from codex"}]}},
    ])
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def empty_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex_empty"
    (home / "tmp").mkdir(parents=True)
    (home / "config.toml").write_text("[features]\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def copilot_home(tmp_path, monkeypatch):
    home = tmp_path / "copilot"
    (home / "ide").mkdir(parents=True)
    monkeypatch.setenv("COPILOT_HOME", str(home))
    return home
