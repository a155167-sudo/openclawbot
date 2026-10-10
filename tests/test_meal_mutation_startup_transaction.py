from __future__ import annotations

import sqlite3

import pytest

import server


_MEAL_OBJECT_QUERY = """
SELECT type, name, tbl_name
FROM sqlite_master
WHERE (name LIKE 'meal_mutation_%'
       OR name = 'idx_meal_mutation_locks_operation')
  AND name NOT LIKE 'sqlite_autoindex_%'
ORDER BY type, name
"""


def _meal_objects(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(_MEAL_OBJECT_QUERY).fetchall()


def _meal_snapshot(path):
    with sqlite3.connect(path) as conn:
        objects = conn.execute(_MEAL_OBJECT_QUERY).fetchall()
        version_rows = conn.execute(
            "SELECT component, version, applied_at "
            "FROM meal_mutation_schema_versions ORDER BY component"
        ).fetchall()
    return objects, version_rows


def _configure_startup(monkeypatch, tmp_path, db_path):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "APP_ENV", "staging")


def test_later_initializer_failure_rolls_back_fresh_meal_schema(monkeypatch, tmp_path):
    db_path = tmp_path / "fresh-later-failure.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE caller_sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO caller_sentinel VALUES ('preexisting')")

    _configure_startup(monkeypatch, tmp_path, db_path)

    def fail_after_meal_component(_conn):
        raise RuntimeError("injected failure after meal component")

    monkeypatch.setattr(server, "ensure_meal_photo_schema", fail_after_meal_component)

    with pytest.raises(RuntimeError, match="injected failure after meal component"):
        server.init_db()

    assert _meal_objects(db_path) == []
    with sqlite3.connect(db_path, timeout=0.1) as conn:
        assert conn.execute("SELECT * FROM caller_sentinel").fetchall() == [
            ("preexisting",)
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


def test_fresh_startup_installs_meal_schema_and_current_rerun_has_zero_component_churn(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "fresh-success.sqlite"
    _configure_startup(monkeypatch, tmp_path, db_path)

    server.init_db()

    first = _meal_snapshot(db_path)
    assert len(first[0]) == 9
    assert [(component, version) for component, version, _ in first[1]] == [
        ("meal_mutation", 1)
    ]

    server.init_db()

    assert _meal_snapshot(db_path) == first


def test_later_initializer_base_exception_rolls_back_and_closes(monkeypatch, tmp_path):
    db_path = tmp_path / "base-exception.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE caller_sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO caller_sentinel VALUES ('preexisting')")
    _configure_startup(monkeypatch, tmp_path, db_path)

    def crash_after_meal_component(_conn):
        raise SystemExit("injected startup crash")

    monkeypatch.setattr(server, "ensure_meal_photo_schema", crash_after_meal_component)

    with pytest.raises(SystemExit, match="injected startup crash"):
        server.init_db()

    assert _meal_objects(db_path) == []
    with sqlite3.connect(db_path, timeout=0.1) as conn:
        assert conn.execute("SELECT * FROM caller_sentinel").fetchall() == [
            ("preexisting",)
        ]
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


class _FinalCommitFailureConnection(sqlite3.Connection):
    fail_final_commit = False

    def commit(self):
        if self.fail_final_commit:
            raise sqlite3.OperationalError("injected final commit failure")
        return super().commit()


def test_final_commit_failure_rolls_back_meal_schema(monkeypatch, tmp_path):
    db_path = tmp_path / "commit-failure.sqlite"
    real_connect = sqlite3.connect

    def connect_with_commit_fault(database, *args, **kwargs):
        kwargs["factory"] = _FinalCommitFailureConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(server.sqlite3, "connect", connect_with_commit_fault)
    with real_connect(db_path) as conn:
        conn.execute("CREATE TABLE caller_sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO caller_sentinel VALUES ('preexisting')")
    _configure_startup(monkeypatch, tmp_path, db_path)
    original_last_initializer = server.ensure_dietitian_health_check_draft_schema

    def arm_commit_failure(conn):
        original_last_initializer(conn)
        conn.fail_final_commit = True

    monkeypatch.setattr(
        server, "ensure_dietitian_health_check_draft_schema", arm_commit_failure
    )

    with pytest.raises(sqlite3.OperationalError, match="injected final commit failure"):
        server.init_db()

    with real_connect(db_path, timeout=0.1) as conn:
        assert conn.execute(_MEAL_OBJECT_QUERY).fetchall() == []
        assert conn.execute("SELECT * FROM caller_sentinel").fetchall() == [
            ("preexisting",)
        ]
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
