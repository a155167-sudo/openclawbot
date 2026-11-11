import sqlite3

import pytest

from db_util import db_session, describe_storage


def test_session_commits_and_closes(tmp_path):
    path = str(tmp_path / "a.db")
    with db_session(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # 已關閉
    with db_session(sqlite3.connect(path)) as check:
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_session_rolls_back_and_closes_on_error(tmp_path):
    path = str(tmp_path / "a.db")
    with db_session(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (x)")
    with pytest.raises(RuntimeError):
        with db_session(sqlite3.connect(path)) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with db_session(sqlite3.connect(path)) as check:
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_storage_check(tmp_path):
    vol = tmp_path / "vol"; (vol / "data").mkdir(parents=True)
    assert describe_storage(str(vol), {}) == (True, "not_on_railway")
    env = {"RAILWAY_ENVIRONMENT": "staging", "RAILWAY_VOLUME_MOUNT_PATH": str(vol)}
    assert describe_storage(str(vol), env)[0] is True
    assert describe_storage(str(vol / "data"), env)[0] is True
    assert describe_storage(str(tmp_path / "elsewhere"), env)[0] is False
    assert describe_storage(str(vol), {"RAILWAY_ENVIRONMENT": "staging"}) == (False, "no_volume_attached")
