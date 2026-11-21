"""Tests for trajectory timing extraction (Input / Model [Thinking + Output] / Tools)."""
from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from backend.agents.agy import AgyAdapter
from backend.agents.claude import ClaudeAdapter
from backend.agents.codex import CodexAdapter
from backend.agents.common import (
    annotate_turn_timings,
    build_trajectory,
    parse_timestamp_ms,
)
from backend.agents.copilot import CopilotAdapter
from backend.agents.gemini import GeminiAdapter
from backend.agents.opencode import OpencodeAdapter
from backend.app import app

client = TestClient(app)


def test_parse_timestamp_ms_formats():
    assert parse_timestamp_ms(None) is None
    assert parse_timestamp_ms("") is None
    assert parse_timestamp_ms(False) is None
    assert parse_timestamp_ms("not-a-timestamp") is None
    assert parse_timestamp_ms(1_700_000_000) == 1_700_000_000_000.0
    assert parse_timestamp_ms(1_700_000_000_123) == 1_700_000_000_123.0
    assert parse_timestamp_ms("2026-06-01T00:00:01.500Z") == 1_780_272_001_500.0
    assert parse_timestamp_ms("2026-06-01T02:00:01.500+02:00") == 1_780_272_001_500.0


def test_annotate_turn_timings_splits_thinking_and_output_and_pairs_tools():
    turns = [
        {
            "role": "user",
            "kind": "user",
            "timestamp": "2026-06-01T00:00:00.000Z",
            "blocks": [{"type": "text", "text": "analyze and run"}],
            "output_tokens": 0,
        },
        {
            "role": "assistant",
            "kind": "assistant",
            "timestamp": "2026-06-01T00:00:04.000Z",
            "blocks": [
                {"type": "thinking", "text": "a" * 300},
                {"type": "text", "text": "b" * 100},
                {"type": "tool_use", "name": "bash", "input": {}},
            ],
            "output_tokens": 80,
        },
        {
            "role": "user",
            "kind": "tool",
            "timestamp": "2026-06-01T00:00:07.000Z",
            "blocks": [{"type": "tool_result", "text": "ok"}],
            "output_tokens": 0,
        },
        {
            "role": "assistant",
            "kind": "assistant",
            "timestamp": "2026-06-01T00:00:08.500Z",
            "blocks": [{"type": "text", "text": "done!"}],
            "output_tokens": 20,
        },
    ]
    annotate_turn_timings(turns)

    # 1. User input turn
    assert turns[0]["timing"]["duration_ms"] == 0.0
    assert turns[0]["timing"]["started_at"] == 1_780_272_000_000.0

    # 2. Model turn with both thinking and output (4000ms total)
    t1 = turns[1]["timing"]
    assert t1["duration_ms"] == 4000.0
    assert t1["thinking_ms"] is not None and t1["thinking_ms"] > 2500.0
    assert t1["output_ms"] is not None and t1["output_ms"] > 900.0
    assert round(t1["thinking_ms"] + t1["output_ms"], 1) == 4000.0
    assert t1["tools"] == ["bash"]
    assert t1["split_source"] == "estimated"

    # 3. Tool result turn inherits paired tool name from preceding tool_use (3000ms)
    t2 = turns[2]["timing"]
    assert t2["duration_ms"] == 3000.0
    assert t2["tool_ms"] == 3000.0
    assert t2["tools"] == ["bash"]

    # 4. Output-only assistant turn (1500ms: 0 thinking, 1500ms output)
    t3 = turns[3]["timing"]
    assert t3["duration_ms"] == 1500.0
    assert t3["thinking_ms"] == 0.0
    assert t3["output_ms"] == 1500.0
    assert t3["split_source"] == "recorded"

    # 5. Build session trajectory summary & 3-lane spans
    traj = build_trajectory(turns)
    s = traj["summary"]
    assert s["user_turns"] == 1
    assert s["model_turns"] == 2
    assert s["tool_calls"] == 1
    assert s["model_ms"] == 5500.0
    assert round(s["thinking_ms"] + s["output_ms"], 1) == 5500.0
    assert s["tool_ms"] == 3000.0
    assert s["active_ms"] == 8500.0
    assert s["wall_ms"] == 8500.0
    assert [sp["lane"] for sp in traj["spans"]] == [0, 1, 2, 1]
    assert [sp["kind"] for sp in traj["spans"]] == ["user", "model", "tool", "model"]


def test_token_ratio_split_when_reasoning_tokens_known():
    turns = [
        {
            "role": "user",
            "kind": "user",
            "timestamp": "2026-06-01T00:00:00Z",
            "blocks": [{"type": "text", "text": "q"}],
        },
        {
            "role": "assistant",
            "kind": "assistant",
            "timestamp": "2026-06-01T00:00:10Z",
            "blocks": [
                {"type": "thinking", "text": "short"},
                {"type": "text", "text": "much longer text"},
            ],
            "output_tokens": 25,
            "_raw_timing": {"reasoning_tokens": 75},
        },
    ]
    annotate_turn_timings(turns)
    tm = turns[1]["timing"]
    assert tm["duration_ms"] == 10000.0
    assert tm["thinking_ms"] == 7500.0
    assert tm["output_ms"] == 2500.0
    assert tm["split_source"] == "tokens"


def test_opencode_v2_part_timestamps_partition_thinking_output_and_tools(tmp_path, monkeypatch):
    """Real OpenCode v2 messages record per-part timestamps for reasoning and tool execution."""
    db_file = tmp_path / "oc_timing.db"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    conn = sqlite3.connect(db_file)
    conn.executescript("""
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL, title TEXT,
            model TEXT, tokens_input INTEGER, tokens_output INTEGER,
            time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    """)
    t0 = 1_790_000_000_000
    conn.execute("INSERT INTO session_v2 VALUES ('s1', NULL, ?, 'Timed', 'gpt', 10, 20, ?, ?, NULL)",
                 (str(cwd), t0, t0 + 10000))
    conn.execute("INSERT INTO session_message VALUES ('m1', 's1', 'user', 1, ?, ?, ?)",
                 (t0, t0, json.dumps({"text": "run build"})))
    # Turn starts at t0+1000, reasoning ends at t0+3500 (2500ms thinking),
    # tool runs from t0+4000 to t0+9000 (5000ms tool), turn completes at t0+9500 (1000ms output).
    asst_data = {
        "time": {"created": t0 + 1000, "streamed": t0 + 4000, "completed": t0 + 9500},
        "model": {"id": "deepseek-r1"},
        "tokens": {"input": 100, "output": 40, "reasoning": 120},
        "content": [
            {"type": "reasoning", "text": "plan steps",
             "time": {"created": t0 + 1200, "completed": t0 + 3500}},
            {"type": "text", "text": "running command"},
            {"type": "tool", "name": "bash",
             "time": {"created": t0 + 3600, "ran": t0 + 4000, "completed": t0 + 9000},
             "state": {"status": "completed", "input": {"command": "make"},
                       "content": [{"type": "text", "text": "ok"}]}},
        ],
    }
    conn.execute("INSERT INTO session_message VALUES ('m2', 's1', 'assistant', 2, ?, ?, ?)",
                 (t0 + 1000, t0 + 9500, json.dumps(asst_data)))
    conn.commit()
    conn.close()
    monkeypatch.setenv("OPENCODE_DB", str(db_file))

    ad = OpencodeAdapter()
    pname = ad.list_projects()[0]["name"]
    conv = ad.get_conversation(pname, "s1")
    tm = conv["turns"][1]["timing"]
    assert tm["duration_ms"] == 8500.0
    assert tm["thinking_ms"] == 2500.0
    assert tm["output_ms"] == 1000.0
    assert tm["tool_ms"] == 5000.0
    assert tm["split_source"] == "recorded"

    traj = build_trajectory(conv["turns"])
    assert traj["summary"]["thinking_ms"] == 2500.0
    assert traj["summary"]["output_ms"] == 1000.0
    assert traj["summary"]["model_ms"] == 3500.0
    assert traj["summary"]["tool_ms"] == 5000.0
    assert traj["summary"]["active_ms"] == 8500.0
    assert [sp["lane"] for sp in traj["spans"]] == [0, 1, 2]


def test_agy_start_stamped_planner_and_tool_window(agy_home):
    cid = "11111111-2222-3333-4444-555555555555"
    tfile = agy_home / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    records = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-10-12T03:38:27Z", "content": "<USER_REQUEST>check</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-10-12T03:38:27Z", "thinking": "thinking hard", "content": "let me check",
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}]},
        {"step_index": 2, "source": "MODEL", "type": "RUN_COMMAND",
         "created_at": "2026-10-12T03:38:31Z",
         "content": "Created At: 2026-10-12T03:38:31Z\nCompleted At: 2026-10-12T03:38:34Z\n\nfile.py"},
    ]
    tfile.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    ad = AgyAdapter()
    pname = ad.list_projects()[0]["name"]
    conv = ad.get_conversation(pname, cid)
    turns = conv["turns"]
    # PLANNER_RESPONSE ran from 03:38:27Z to 03:38:31Z (4000ms), subdivided into thinking + output
    assert turns[1]["timing"]["duration_ms"] == 4000.0
    assert turns[1]["timing"]["thinking_ms"] > 800.0
    assert turns[1]["timing"]["output_ms"] > 2500.0
    assert round(turns[1]["timing"]["thinking_ms"] + turns[1]["timing"]["output_ms"], 1) == 4000.0
    # RUN_COMMAND ran from 03:38:31Z to 03:38:34Z (3000ms)
    assert turns[2]["timing"]["tool_ms"] == 3000.0
    assert turns[2]["timing"]["tools"] == ["run_command"]


def test_trajectory_api_route_and_all_adapters(
    claude_home, agy_home, opencode_home, codex_home, copilot_store_home, gemini_home
):
    for agent_id, adapter in (
        ("claude", ClaudeAdapter()),
        ("agy", AgyAdapter()),
        ("opencode", OpencodeAdapter()),
        ("codex", CodexAdapter()),
        ("copilot", CopilotAdapter()),
        ("gemini", GeminiAdapter()),
    ):
        proj = adapter.list_projects()[0]["name"]
        sid = adapter.list_sessions(proj)[0].session_id
        r = client.get(f"/api/projects/{proj}/sessions/{sid}/trajectory", params={"agent": agent_id})
        assert r.status_code == 200, f"{agent_id} failed: {r.text}"
        body = r.json()
        assert body["session_id"] == sid
        assert body["total"] >= 2
        assert body["summary"]["user_turns"] >= 1
        assert body["summary"]["model_turns"] >= 1
        assert len(body["spans"]) >= 2

        # limit=0 returns all turns with timing attached
        r_all = client.get(f"/api/projects/{proj}/sessions/{sid}",
                           params={"agent": agent_id, "offset": 0, "limit": 0})
        assert r_all.status_code == 200
        turns = r_all.json()["turns"]
        assert len(turns) == body["total"]
        assert all("timing" in t for t in turns)
