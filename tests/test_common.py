from pathlib import Path

from backend.agents import common


def test_context_limit_known_only_above_200k():
    assert common.context_limit_for(50_000) == {"limit": 200_000, "known": False}
    assert common.context_limit_for(250_000) == {"limit": 1_000_000, "known": True}


def test_iter_jsonl_skips_blank_and_bad_lines(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text('{"a":1}\n\nnot json\n{"b":2}\n', encoding="utf-8")
    rows = list(common.iter_jsonl(f))
    assert rows == [{"a": 1}, {"b": 2}]


def test_block_text_joins_text_blocks():
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert "hello" in common.block_text(content)
    assert "world" in common.block_text(content)


def test_structured_blocks_marks_tool_result():
    content = [{"type": "tool_result", "content": "out"}]
    blocks = common.structured_blocks(content)
    assert blocks[0]["type"] == "tool_result"


def test_save_doc_rejects_escape_and_writes(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    out = common.save_doc_file(str(base), "AGENTS.md", "body")
    assert out == base / "AGENTS.md"
    assert (base / "AGENTS.md").read_text() == "body"


def test_save_doc_missing_cwd_raises():
    import pytest
    with pytest.raises(FileNotFoundError):
        common.save_doc_file("", "AGENTS.md", "x")


def test_read_doc_file_absent(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    res = common.read_doc_file(str(base), "AGENTS.md")
    assert res["exists"] is False
    assert res["cwd"] == str(base)
