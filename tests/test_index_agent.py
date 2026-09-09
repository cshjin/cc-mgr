from backend import index_db


def test_reindex_all_agents_and_scoped_search(claude_home, gemini_home,
                                               codex_home, agy_home, monkeypatch, tmp_path):
    # point the index db at a tmp file so we don't touch the real cache
    monkeypatch.setattr(index_db, "db_path", lambda: tmp_path / "idx.db")
    stats = index_db.reindex(force=True)
    assert stats["indexed"] >= 4  # at least one session per active agent

    # search scoped to agy only returns agy hits
    res_agy = index_db.search("hello", agent="agy")
    assert res_agy
    assert all(r.get("agent") == "agy" for r in res_agy)

    # search scoped to gemini only returns gemini hits
    res = index_db.search("hello", agent="gemini")
    assert res
    assert all(r.get("agent") == "gemini" for r in res)

    # search scoped to claude returns claude hits, tagged
    res_c = index_db.search("hello", agent="claude")
    assert res_c and all(r.get("agent") == "claude" for r in res_c)

    # doc search finds the gemini root doc
    res_doc = index_db.search("beta", agent="gemini")
    assert any(r.get("source") == "agent_doc" for r in res_doc)

    # doc search finds agy doc / memory
    res_agy_doc = index_db.search("delta", agent="agy")
    assert any(r.get("agent") == "agy" for r in res_agy_doc)
