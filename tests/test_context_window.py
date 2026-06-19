"""Context-window tier detection, including the post-compact case."""
import json

from backend import store


def _write_session(home, cwd, records):
    proj = home / "projects" / "-tmp-repo_ctx"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / "sess-ctx.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _assistant(usage):
    return {"type": "assistant", "timestamp": "2026-06-01T00:00:00Z",
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "content": [{"type": "text", "text": "ok"}], "usage": usage}}


def test_tier_known_when_peak_crossed_200k_even_after_compact(tmp_path, monkeypatch):
    """A session that crossed 200k then `compact`ed below it must still report
    the 1M tier as KNOWN — the proof is the peak, not the last turn."""
    home = tmp_path / "claude"
    cwd = tmp_path / "repo_ctx"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text("# doc\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _write_session(home, cwd, [
        {"type": "user", "cwd": str(cwd),
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        # turn that crossed the 200k standard window -> proves 1M tier
        _assistant({"input_tokens": 250_000, "output_tokens": 100}),
        # a /compact happens: live context drops far below 200k
        _assistant({"input_tokens": 12_000, "output_tokens": 50}),
    ])
    sess = store.list_sessions("-tmp-repo_ctx")[0]
    assert sess.context_limit == 1_000_000
    assert sess.context_limit_known is True
    # current/last context is still the post-compact value (for display)
    assert sess.context_tokens == 12_050


def test_tier_unknown_when_never_crossed_200k(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    cwd = tmp_path / "repo_ctx"
    cwd.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _write_session(home, cwd, [
        {"type": "user", "cwd": str(cwd),
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        _assistant({"input_tokens": 30_000, "output_tokens": 100}),
        _assistant({"input_tokens": 8_000, "output_tokens": 50}),
    ])
    sess = store.list_sessions("-tmp-repo_ctx")[0]
    assert sess.context_limit_known is False
    assert sess.context_tokens == 8_050
