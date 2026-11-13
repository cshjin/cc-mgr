"""#8: live agent DBs are read with mode=ro (WAL-aware), not immutable=1."""
import sqlite3

import pytest

from backend.agents import common
from backend.agents.opencode import OpencodeAdapter


def _open_wal_writer(db):
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")  # keep new data in the -wal file only
    return w


def test_connect_sqlite_ro_sees_uncheckpointed_wal(tmp_path):
    db = tmp_path / "live.db"
    w = _open_wal_writer(db)
    w.execute("CREATE TABLE t(x)")
    w.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    w.commit()  # writer stays open, so nothing is checkpointed into live.db

    assert common.connect_sqlite_ro(db).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100

    # What the old immutable=1 connection saw: the stale main file.
    imm = sqlite3.connect(f"{db.resolve().as_uri()}?immutable=1", uri=True)
    try:
        stale = imm.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    except sqlite3.OperationalError:
        stale = None  # table not even visible
    assert stale != 100
    w.close()


def test_opencode_adapter_sees_sessions_still_in_the_wal(opencode_home, tmp_path):
    db = opencode_home / "opencode.db"
    w = _open_wal_writer(db)
    newdir = tmp_path / "fresh_repo"
    newdir.mkdir()
    w.execute("INSERT INTO session (id, directory, title, time_created, time_updated) "
              "VALUES ('ses_wal_1', ?, 'fresh', 1780000005000, 1780000005000)", (str(newdir),))
    w.commit()
    try:
        cwds = {p["cwd"] for p in OpencodeAdapter().list_projects()}
        assert str(newdir) in cwds
    finally:
        w.close()


def test_connect_sqlite_ro_falls_back_to_immutable(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    real_connect = sqlite3.connect

    def fake_connect(database, *args, **kwargs):
        if "mode=ro" in str(database):
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    conn = common.connect_sqlite_ro(db)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
