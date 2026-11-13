"""Fixtures that build synthetic agent homes in a tmp dir.

Each fixture returns the home Path and sets the matching env var so adapters
resolve into the tmp tree instead of the real ~/. No real user data touched.
"""
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

# Every env var an adapter consults to find real user data.
AGENT_ENV_VARS = (
    "CLAUDE_HOME", "CLAUDE_CONFIG_DIR", "AGY_HOME", "ANTIGRAVITY_HOME",
    "ANTIGRAVITY_CLI_HOME", "GEMINI_HOME", "CODEX_HOME", "COPILOT_HOME",
    "OPENCODE_DB", "OPENCODE_BASE", "XDG_DATA_HOME", "CC_MGR_TOKEN",
    "CC_MGR_READ_ONLY", "CC_MGR_ALLOWED_HOSTS",
)
AGENT_CLIS = {"opencode", "agy", "claude", "codex", "copilot", "gemini"}


@pytest.fixture(autouse=True)
def _isolate_from_real_agent_data(tmp_path_factory, monkeypatch):
    """Never let a test read real agent data or run a real agent CLI.

    Autouse fixtures run before the per-agent fixtures below, which then point
    their own env var at a synthetic home. Everything else resolves into an
    empty sandbox (HOME and XDG_DATA_HOME included), and agent CLIs are hidden
    from shutil.which and refused by subprocess.run.
    """
    sandbox = tmp_path_factory.mktemp("no_real_data")
    for var in AGENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(sandbox / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(sandbox / "xdg_data"))
    # The search index is cc_mgr's own cache under the repo's data/: never let
    # a test (e.g. an API delete, which purges index rows) touch the real one.
    from backend import index_db
    monkeypatch.setattr(index_db, "db_path", lambda: sandbox / "cc_mgr.db")

    real_which = shutil.which

    def which(cmd, *args, **kwargs):
        if Path(str(cmd)).name in AGENT_CLIS:
            return None
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)

    real_run = subprocess.run

    def run(args, *a, **kw):
        argv0 = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
        if Path(str(argv0)).name in AGENT_CLIS:
            raise AssertionError(f"test tried to run a real agent CLI: {args!r}")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


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
def copilot_store_home(tmp_path, monkeypatch):
    """GitHub Copilot CLI layout: session-store.db + session-state/<id>/events.jsonl."""
    home = tmp_path / "copilot_store"
    cwd1, cwd2 = tmp_path / "repo_cp", tmp_path / "repo_cp2"
    cwd1.mkdir()
    cwd2.mkdir()
    (cwd1 / "AGENTS.md").write_text("# Copilot agents doc\nkappa\n", encoding="utf-8")
    home.mkdir()
    conn = sqlite3.connect(home / "session-store.db")
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, host_type TEXT,
                               branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT, turn_index INTEGER,
                            user_message TEXT, assistant_response TEXT, timestamp TEXT);
        CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, session_id TEXT,
                            turn_index INTEGER, model TEXT, input_tokens INTEGER,
                            output_tokens INTEGER, cache_read_tokens INTEGER, created_at TEXT);
    """)
    conn.execute("INSERT INTO sessions VALUES ('aaaa-1', ?, 'me/repo', 'github', 'main', "
                 "'Fix the build', '2026-11-01T00:00:00.000Z', '2026-11-01T01:00:00.000Z')", (str(cwd1),))
    conn.execute("INSERT INTO sessions VALUES ('bbbb-2', ?, 'me/repo', 'github', 'dev', "
                 "'DB only', '2026-11-02T00:00:00.000Z', '2026-11-02T01:00:00.000Z')", (str(cwd1),))
    conn.execute("INSERT INTO turns VALUES (1, 'aaaa-1', 0, 'please fix the build', NULL, "
                 "'2026-11-01T00:00:01.000Z')")
    conn.execute("INSERT INTO turns VALUES (2, 'bbbb-2', 0, 'db prompt', NULL, '2026-11-02T00:00:01.000Z')")
    conn.execute("INSERT INTO turns VALUES (3, 'bbbb-2', 1, 'second', 'db reply', '2026-11-02T00:00:02.000Z')")
    conn.execute("INSERT INTO assistant_usage_events VALUES (1, 'bbbb-2', 1, 'gemini-4-argon', "
                 "45243, 391, 42740, '2026-11-02T00:00:03.000Z')")
    conn.commit()
    conn.close()

    def events(sid, cwd, evs):
        d = home / "session-state" / sid
        d.mkdir(parents=True)
        start = {"type": "session.start", "timestamp": "2026-11-01T00:00:00.000Z",
                 "data": {"sessionId": sid, "context": {"cwd": str(cwd), "branch": "main"}}}
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [start, *evs]),
                                        encoding="utf-8")

    events("aaaa-1", cwd1, [
        {"type": "user.message", "id": "u1", "timestamp": "2026-11-01T00:00:01.000Z",
         "data": {"content": "please fix the build", "attachments": []}},
        {"type": "assistant.message", "id": "a1", "timestamp": "2026-11-01T00:00:02.000Z",
         "data": {"model": "claude-sonnet-5.5", "reasoningText": "look at make", "content": "On it.",
                  "toolRequests": [{"toolCallId": "t1", "name": "bash", "arguments": {"command": "make"}}]}},
        {"type": "tool.execution_complete", "id": "r1",
         "data": {"toolCallId": "t1", "success": True, "result": {"content": "build ok"}}},
        {"type": "tool.execution_complete", "id": "r2",
         "data": {"toolCallId": "t2", "success": False, "error": {"message": "permission denied"}}},
        {"type": "session.usage_record",
         "data": {"usage": {"model": "claude-sonnet-5.5", "inputTokens": 249_000, "outputTokens": 1_000}}},
        {"type": "session.compaction_complete", "id": "c1",
         "data": {"success": True, "summaryContent": "summary of earlier work"}},
        {"type": "session.usage_record",
         "data": {"usage": {"model": "claude-sonnet-5.5", "inputTokens": 50_000, "outputTokens": 100}}},
        {"type": "system.message", "id": "s1", "data": {"content": "mode changed"}},
        {"type": "assistant.turn_end", "data": {}},
    ])
    events("cccc-3", cwd2, [
        {"type": "user.message", "id": "u9", "data": {"content": "events only"}},
    ])
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


@pytest.fixture
def opencode_v2_home(tmp_path, monkeypatch):
    """An OpenCode v2 database shaped like the real one (2.2.7).

    session_v2 + session_message (parts inlined in data.content), an empty-ish
    session_part, and the legacy v1 tables that v2 keeps after migrating.
    """
    home = tmp_path / "opencode_v2"
    home.mkdir()
    cwd = tmp_path / "repo_oc2"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("# OpenCode v2 doc\nzeta\n", encoding="utf-8")
    db_file = home / "opencode.db"
    conn = sqlite3.connect(db_file)
    conn.executescript("""
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
            directory TEXT NOT NULL, title TEXT, version TEXT, model TEXT, agent TEXT,
            tokens_input INTEGER DEFAULT 0, tokens_output INTEGER DEFAULT 0,
            time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE session_part (
            message_id TEXT NOT NULL, session_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            type TEXT NOT NULL, call_id TEXT, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (message_id, ordinal));
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT, title TEXT,
            model TEXT, tokens_input INTEGER, tokens_output INTEGER,
            time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                              time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                           time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE todo (session_id TEXT, content TEXT, status TEXT, priority TEXT,
                           position INTEGER, time_created INTEGER, time_updated INTEGER);
    """)
    t0 = 1_790_000_000_000
    claude = {"id": "claude-opus-5.5", "providerID": "github-copilot", "variant": "max"}
    gpt = {"id": "gpt-6-astra", "providerID": "github-copilot", "variant": "max"}
    sessions = [
        ("ses_v2_parent", None, "V2 Parent", json.dumps(claude), t0, t0 + 9000, None),
        ("ses_v2_child", "ses_v2_parent", "Subagent run", json.dumps(gpt), t0 + 100, t0 + 500, None),
        ("ses_old", None, "Migrated v1", json.dumps(claude), t0 - 50_000, t0 - 40_000, None),
        ("ses_archived", None, "Archived", json.dumps(claude), t0, t0 + 1, t0 + 2),
    ]
    for sid, parent, title, model, created, updated, archived in sessions:
        conn.execute(
            "INSERT INTO session_v2 (id, parent_id, directory, title, model, tokens_input, "
            "tokens_output, time_created, time_updated, time_archived) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, parent, str(cwd), title, model, 36, 99, created, updated, archived))
    conn.execute("INSERT INTO session (id, directory, title, time_created, time_updated) "
                 "VALUES ('ses_old', ?, 'Migrated v1', ?, ?)", (str(cwd), t0 - 50_000, t0 - 40_000))
    conn.execute("INSERT INTO todo VALUES ('ses_old', 'legacy task', 'pending', 'high', 0, ?, ?)",
                 (t0, t0))

    def msg(sid, seq, mtype, data, mid=None):
        conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                     (mid or f"msg_{sid}_{seq}", sid, mtype, seq, t0 + seq, t0 + seq, json.dumps(data)))

    p = "ses_v2_parent"
    msg(p, 1, "user", {"text": "hello v2", "files": [
        {"name": "paper.pdf", "mime": "application/pdf", "data": "JVBERi0xLjUK",
         "source": {"type": "inline"}}]})
    msg(p, 2, "assistant", {"model": gpt, "content": [
        {"type": "reasoning", "text": "thinking v2"},
        {"type": "text", "text": "answer v2"},
        {"type": "tool", "id": "t1", "name": "shell", "state": {
            "status": "completed", "input": {"command": "ls"},
            "content": [{"type": "text", "text": "a.py\nb.py"},
                        {"type": "file", "uri": "data:image/png;base64,AAAA", "mime": "image/png"}]}},
        {"type": "tool", "id": "t2", "name": "read", "state": {
            "status": "error", "input": {"path": "/nope"},
            "error": {"type": "tool.execution", "message": "File not found: /nope"}}},
    ], "tokens": {"input": 5, "output": 7, "reasoning": 0, "cache": {"read": 1000, "write": 10}}})
    msg(p, 3, "idle", {"outcome": "succeeded"})
    msg(p, 4, "compaction", {"summary": "## Objective\nsummary text", "reason": "auto",
                             "tokens": {"input": 250000, "output": 100, "cache": {"read": 0, "write": 0}}})
    msg(p, 5, "synthetic", {"text": "<shell id=\"sh_1\">done</shell>", "description": "bg"})
    msg(p, 6, "model-switched", {"model": claude, "previous": gpt})
    msg(p, 7, "user", {"text": "second prompt", "files": []})
    msg(p, 8, "assistant", {"model": claude, "content": [{"type": "text", "text": "final answer"}],
                            "tokens": {"input": 2, "output": 3, "cache": {"read": 150000, "write": 10}}})
    # still running: no tokens yet, so the context comes from seq 8
    msg(p, 9, "assistant", {"model": claude, "content": [
        {"type": "tool", "name": "shell", "state": {"status": "running", "input": {"command": "sleep 1"}}}]})

    c = "ses_v2_child"
    msg(c, 1, "user", {"text": "sub task"})
    msg(c, 2, "assistant", {"model": gpt, "content": [],
                            "tokens": {"input": 1, "output": 1, "cache": {"read": 10, "write": 0}}},
        mid="msg_child_answer")
    conn.execute("INSERT INTO session_part VALUES ('msg_child_answer', ?, 0, 'text', NULL, ?, ?, ?)",
                 (c, t0, t0, json.dumps({"type": "text", "text": "from session_part"})))

    msg("ses_old", 1, "user", {"text": "old prompt"})
    msg("ses_old", 2, "assistant", {"model": claude, "content": [{"type": "text", "text": "old answer"}],
                                    "tokens": {"input": 1, "output": 1, "cache": {"read": 0, "write": 0}}})
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db_file))
    return home

