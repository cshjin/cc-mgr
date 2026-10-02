"""Fixtures that build synthetic agent homes in a tmp dir.

Each fixture returns the home Path and sets the matching env var so adapters
resolve into the tmp tree instead of the real ~/. No real user data touched.
"""
import json
import os
import sqlite3
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


@pytest.fixture
def agy_home(tmp_path, monkeypatch):
    import sqlite3
    home = tmp_path / "antigravity-cli"
    cwd = tmp_path / "repo_agy"
    cwd.mkdir()
    (cwd / "GEMINI.md").write_text("# AGY project doc\ndelta\n", encoding="utf-8")

    cid = "11111111-2222-3333-4444-555555555555"
    brain_dir = home / "brain" / cid / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    transcript = brain_dir / "transcript.jsonl"
    _write_jsonl(transcript, [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-09-01T10:00:00Z",
            "content": "<USER_REQUEST>\nhello agy\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-09-01T10:00:01Z",
            "thinking": "analyzing user request",
            "content": "hello from agy",
            "tool_calls": [{"name": "list_dir", "args": {"DirectoryPath": f'"{str(cwd)}"'}}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "created_at": "2026-09-01T10:00:02Z",
            "content": '{"name": "GEMINI.md"}',
        },
    ])

    # Artifact file in brain
    (home / "brain" / cid / "report.md").write_text("# Report\nsome findings\n", encoding="utf-8")

    # history.jsonl
    (home / "history.jsonl").write_text(
        json.dumps({
            "display": "hello agy",
            "timestamp": 1779822449094,
            "workspace": str(cwd),
            "conversationId": cid,
        }) + "\n",
        encoding="utf-8",
    )

    # conversation_summaries.db
    db_file = home / "conversation_summaries.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id text PRIMARY KEY, title text, preview text, step_count integer, "
        "last_modified_time datetime, workspace_uris text, status text, source text, "
        "project_id text, agent_name text, parent_conversation_id text, nesting_depth integer, "
        "battle_id text, winning_conversation_id text, not_fully_idle numeric, killed numeric, "
        "last_user_input_time datetime, last_user_input_step_index integer, app_data_dir text, "
        "raw_summary BLOB)"
    )
    conn.execute(
        "INSERT INTO conversation_summaries (conversation_id, title, preview, step_count, "
        "last_modified_time, workspace_uris) VALUES (?, ?, ?, ?, ?, ?)",
        (cid, "Test AGY Title", "hello agy", 3, "2026-09-01 10:00:02", json.dumps([f"file://{str(cwd)}"])),
    )
    conn.commit()
    conn.close()

    # settings.json
    (home / "settings.json").write_text(
        json.dumps({"model": "Gemini 3.8 Flash (High)", "trustedWorkspaces": [str(cwd)]}),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGY_HOME", str(home))
    return home


@pytest.fixture
def opencode_home(tmp_path, monkeypatch):
    home = tmp_path / "opencode"
    home.mkdir(parents=True, exist_ok=True)
    cwd = tmp_path / "repo_opencode"
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "AGENTS.md").write_text("# OpenCode project doc\ngamma\n", encoding="utf-8")

    db_file = home / "opencode.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            parent_id TEXT,
            slug TEXT,
            directory TEXT,
            title TEXT,
            version TEXT,
            share_url TEXT,
            summary_additions INTEGER,
            summary_deletions INTEGER,
            summary_files INTEGER,
            summary_diffs TEXT,
            revert TEXT,
            permission TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            time_compacting INTEGER,
            time_archived INTEGER,
            workspace_id TEXT,
            path TEXT,
            agent TEXT,
            model TEXT,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_write INTEGER,
            metadata TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE todo (
            session_id TEXT,
            content TEXT,
            status TEXT,
            priority TEXT,
            position INTEGER,
            time_created INTEGER,
            time_updated INTEGER
        )
    """)

    sid = "ses_test_1"
    now_ms = 1780000000000
    conn.execute(
        "INSERT INTO session (id, directory, title, model, tokens_input, tokens_output, time_created, time_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, str(cwd), "Test OpenCode Title", '{"id":"test-model"}', 100, 50, now_ms, now_ms + 1000),
    )

    m1_id = "msg_u1"
    m2_id = "msg_a1"
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        (m1_id, sid, now_ms, json.dumps({"role": "user", "time": {"created": now_ms}})),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        (m2_id, sid, now_ms + 500, json.dumps({
            "role": "assistant",
            "modelID": "test-model",
            "tokens": {"input": 100, "output": 50},
            "time": {"created": now_ms + 500},
        })),
    )

    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        ("prt_1", m1_id, sid, now_ms, json.dumps({"type": "text", "text": "hello opencode"})),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        ("prt_2", m2_id, sid, now_ms + 510, json.dumps({"type": "reasoning", "text": "thinking..."})),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        ("prt_3", m2_id, sid, now_ms + 520, json.dumps({"type": "text", "text": "hi from opencode"})),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        ("prt_4", m2_id, sid, now_ms + 530, json.dumps({
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "input": {"command": "ls"}, "output": "file.txt"},
        })),
    )

    conn.execute(
        "INSERT INTO todo (session_id, content, status, priority, position, time_created, time_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, "test task 1", "pending", "high", 0, now_ms, now_ms),
    )

    conn.commit()
    conn.close()

    monkeypatch.setenv("OPENCODE_DB", str(db_file))
    return home

