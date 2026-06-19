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
    # Mirrors the REAL on-disk format: line0 meta, line1 $set prelude (synthetic
    # <session_context>, excluded from display), then bare top-level turn records
    # one per line. content is polymorphic; gemini answers are plain strings.
    sess.write_text("\n".join([
        json.dumps({"sessionId": "ab4cc0f2", "projectHash": "abc",
                    "startTime": "2026-06-03T23:38:00Z",
                    "lastUpdated": "2026-06-03T23:38:10Z", "kind": "main"}),
        json.dumps({"$set": {"messages": [
            {"id": "p0", "timestamp": "2026-06-03T23:38:00Z", "type": "user",
             "content": [{"text": "<session_context>\nThis is the Gemini CLI..."}]},
        ], "lastUpdated": "2026-06-03T23:38:00Z"}}),
        json.dumps({"id": "m1", "timestamp": "2026-06-03T23:38:03Z", "type": "user",
                    "content": [{"text": "hello gemini"}]}),
        json.dumps({"$set": {"lastUpdated": "2026-06-03T23:38:04Z"}}),
        json.dumps({"id": "m2", "timestamp": "2026-06-03T23:38:05Z", "type": "gemini",
                    "content": "hello from gemini", "model": "gemini-3-flash-preview",
                    "tokens": {"input": 100, "output": 7, "cached": 0, "total": 107}}),
        json.dumps({"$set": {"lastUpdated": "2026-06-03T23:38:06Z"}}),
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
    # Mirrors the REAL envelope format: {timestamp, type, payload}. cwd lives in
    # session_meta; conversation turns are response_item/message with content
    # parts typed input_text/output_text; tool calls/outputs are function_call*.
    _write_jsonl(sess, [
        {"timestamp": "2026-06-03T10:00:00Z", "type": "session_meta",
         "payload": {"id": "xyz", "timestamp": "2026-06-03T10:00:00Z",
                     "cwd": str(cwd), "originator": "codex-tui", "cli_version": "1"}},
        {"timestamp": "2026-06-03T10:00:00Z", "type": "turn_context", "payload": {}},
        {"timestamp": "2026-06-03T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "<permissions...>"}]}},
        {"timestamp": "2026-06-03T10:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello codex"}]}},
        {"timestamp": "2026-06-03T10:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "arguments": "{\"cmd\": \"ls\"}", "call_id": "c1"}},
        {"timestamp": "2026-06-03T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": "a.py\nb.py"}},
        {"timestamp": "2026-06-03T10:00:05Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "hi from codex"}]}},
        {"timestamp": "2026-06-03T10:00:06Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"input_tokens": 999,
                              "output_tokens": 99, "total_tokens": 9999},
                              "last_token_usage": {"input_tokens": 50,
                              "output_tokens": 9, "total_tokens": 59}}}},
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
