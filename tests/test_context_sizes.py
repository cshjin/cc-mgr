"""#11: context = live context of the last turn; a tier is only claimed for Claude."""
import json
import sqlite3

from backend import store
from backend.agents import common
from backend.agents.agy import AgyAdapter
from backend.agents.codex import CodexAdapter
from backend.agents.opencode import OpencodeAdapter


def test_context_window_rule():
    assert common.context_window("claude-opus-5.5", 250_000) == {"limit": 1_000_000, "known": True}
    assert common.context_window("claude-opus-5.5", 150_000)["known"] is False
    for model in ("gpt-6-astra", "gemini-4-argon", "", None):
        assert common.context_window(model, 900_000) == {"limit": 1_000_000, "known": False}


def test_store_no_longer_claims_1m_just_below_200k(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    proj = home / "projects" / "-tmp-x"
    proj.mkdir(parents=True)
    records = [
        {"type": "user", "cwd": str(tmp_path), "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-8",
                                          "content": [{"type": "text", "text": "ok"}],
                                          "usage": {"input_tokens": 197_000, "output_tokens": 10}}},
    ]
    (proj / "s.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    s = store.list_sessions("-tmp-x")[0]
    assert s.context_tokens == 197_010 and s.context_limit_known is False  # was "proven 1M"


def _agy_transcript(agy_home):
    path = next((agy_home / "brain").glob("*/.system_generated/logs/transcript.jsonl"))
    return path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_agy_uses_recorded_tokens_and_never_claims_a_tier(agy_home):
    path, recs = _agy_transcript(agy_home)
    recs[1].update(input_tokens=3000, cache_read_tokens=297_000, output_tokens=500)
    recs.append({"step_index": "3", "type": "PLANNER_RESPONSE", "source": "MODEL",
                 "content": "later", "input_tokens": 2000, "cache_read_tokens": 140_000,
                 "output_tokens": 100})
    _write(path, recs)
    a = AgyAdapter()
    s = a.list_sessions(a.list_projects()[0]["name"])[0]
    assert s.context_tokens == 142_100  # the last recorded turn, not the whole transcript
    assert s.total_output_tokens == 600
    assert s.context_limit_known is False  # agy doesn't record which model a session used


def test_agy_estimate_restarts_after_a_checkpoint(agy_home):
    path, recs = _agy_transcript(agy_home)
    recs.append({"step_index": "3", "type": "CHECKPOINT", "source": "SYSTEM", "content": "x" * 400})
    recs.append({"step_index": "4", "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "y" * 400})
    _write(path, recs)
    a = AgyAdapter()
    s = a.list_sessions(a.list_projects()[0]["name"])[0]
    assert s.context_tokens == 200  # (400 + 400) // 4: text before the compaction no longer counts


def test_opencode_never_claims_a_tier_for_non_claude_models(opencode_v2_home):
    conn = sqlite3.connect(opencode_v2_home / "opencode.db")
    conn.execute("UPDATE session_message SET data = json_set(data, '$.tokens.cache.read', 400000) "
                 "WHERE id = 'msg_child_answer'")
    conn.commit()
    conn.close()
    a = OpencodeAdapter()
    child = [s for s in a.list_sessions(a.list_projects()[0]["name"])
             if s.session_id == "ses_v2_child"][0]
    assert child.model == "gpt-6-astra" and child.context_tokens == 400_002
    assert child.context_limit_known is False and child.context_limit == 1_000_000


def test_codex_reports_model_and_claims_no_tier(codex_home):
    sess = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    lines = sess.read_text(encoding="utf-8").splitlines()
    lines[1] = json.dumps({"timestamp": "2026-06-03T10:00:00Z", "type": "turn_context",
                           "payload": {"model": "gpt56terra"}})
    last = json.loads(lines[-1])
    last["payload"]["info"]["last_token_usage"]["total_tokens"] = 300_000
    lines[-1] = json.dumps(last)
    sess.write_text("\n".join(lines), encoding="utf-8")
    a = CodexAdapter()
    s = a.list_sessions(a.list_projects()[0]["name"])[0]
    assert (s.model, s.context_tokens, s.context_limit_known) == ("gpt56terra", 300_000, False)
