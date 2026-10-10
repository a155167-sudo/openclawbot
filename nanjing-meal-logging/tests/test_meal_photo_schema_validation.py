import multiprocessing as mp
import sqlite3

import pytest

from meal_photo_system import ensure_meal_photo_schema


CHOICE_TABLE_SQL = """
CREATE TABLE meal_photo_ingredient_choices (
    choice_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token TEXT NOT NULL,
    draft_version INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    parsed_items_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL DEFAULT '{}',
    collision_indexes_json TEXT NOT NULL,
    current_collision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, source_message_id),
    FOREIGN KEY(token) REFERENCES pending_meal_photo_drafts(token)
)
"""
OWNER_INDEX_SQL = """
CREATE INDEX idx_meal_photo_ingredient_choices_owner
ON meal_photo_ingredient_choices(user_id, token, status, created_at)
"""


def _dump(conn):
    return "\n".join(conn.iterdump())


def _v9_predecessor(path):
    with sqlite3.connect(path) as conn:
        ensure_meal_photo_schema(conn)
        conn.execute("DROP TABLE meal_photo_ingredient_choices")
        conn.execute(
            "UPDATE meal_photo_schema_versions SET version=9,updated_at='v9' "
            "WHERE component='meal_photo_system'"
        )
        conn.commit()


def _install_variant(conn, variant):
    table_sql = CHOICE_TABLE_SQL
    index_sql = OWNER_INDEX_SQL
    if variant == "extra_column":
        table_sql = table_sql.replace(
            "updated_at TEXT NOT NULL,", "updated_at TEXT NOT NULL,\n    poison TEXT,"
        )
    elif variant == "missing_fk":
        table_sql = table_sql.replace(
            ",\n    FOREIGN KEY(token) REFERENCES pending_meal_photo_drafts(token)", ""
        )
    elif variant == "weakened_constraints":
        table_sql = table_sql.replace("user_id TEXT NOT NULL", "user_id TEXT")
        table_sql = table_sql.replace(
            ",\n    UNIQUE(user_id, source_message_id)", ""
        )
    elif variant == "wrong_named_index":
        index_sql = "CREATE INDEX idx_meal_photo_ingredient_choices_owner ON meal_photo_ingredient_choices(choice_id)"
    else:
        raise AssertionError(variant)
    conn.execute(table_sql)
    conn.execute(index_sql)
    conn.commit()


@pytest.mark.parametrize(
    "variant",
    ["extra_column", "missing_fk", "weakened_constraints", "wrong_named_index"],
)
def test_choice_migration_rejects_noncanonical_owned_schema_without_mutation(tmp_path, variant):
    path = tmp_path / f"{variant}.db"
    _v9_predecessor(path)
    with sqlite3.connect(path) as conn:
        _install_variant(conn, variant)
        before = _dump(conn)
        changes_before = conn.total_changes

        with pytest.raises(RuntimeError, match="meal_photo_ingredient_choices schema mismatch"):
            ensure_meal_photo_schema(conn)

        assert _dump(conn) == before
        assert conn.total_changes == changes_before
        assert conn.execute(
            "SELECT version FROM meal_photo_schema_versions WHERE component='meal_photo_system'"
        ).fetchone() == (9,)


def test_unknown_schema_version_fails_closed_without_pollution(tmp_path):
    path = tmp_path / "unknown-version.db"
    _v9_predecessor(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meal_photo_schema_versions SET version=99,updated_at='unknown' "
            "WHERE component='meal_photo_system'"
        )
        conn.commit()
        before = _dump(conn)

        with pytest.raises(RuntimeError, match="unsupported meal-photo schema version: 99"):
            ensure_meal_photo_schema(conn)

        assert _dump(conn) == before
        assert conn.execute(
            "SELECT name FROM sqlite_schema WHERE name='meal_photo_ingredient_choices'"
        ).fetchone() is None


def test_canonical_v10_sql_whitespace_is_semantically_accepted_without_write(tmp_path):
    path = tmp_path / "canonical-v10.db"
    _v9_predecessor(path)
    with sqlite3.connect(path) as conn:
        conn.execute(CHOICE_TABLE_SQL.replace("CREATE TABLE", "CREATE    TABLE"))
        conn.execute(OWNER_INDEX_SQL.replace("CREATE INDEX", "CREATE   INDEX"))
        conn.execute(
            "UPDATE meal_photo_schema_versions SET version=10,updated_at='kept' "
            "WHERE component='meal_photo_system'"
        )
        conn.commit()
        before = _dump(conn)
        changes_before = conn.total_changes

        ensure_meal_photo_schema(conn)

        assert _dump(conn) == before
        assert conn.total_changes == changes_before


def test_failed_choice_index_ddl_rolls_back_schema_and_metadata(tmp_path):
    path = tmp_path / "ddl-failure.db"
    _v9_predecessor(path)
    with sqlite3.connect(path) as conn:
        before = _dump(conn)

        def deny_owner_index(action, arg1, _arg2, _db_name, _trigger):
            if action == sqlite3.SQLITE_CREATE_INDEX and arg1 == "idx_meal_photo_ingredient_choices_owner":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_owner_index)
        with pytest.raises(sqlite3.DatabaseError):
            ensure_meal_photo_schema(conn)
        conn.set_authorizer(None)

        assert _dump(conn) == before


def _concurrent_worker(path, start, output):
    try:
        with sqlite3.connect(path, timeout=10) as conn:
            start.wait()
            ensure_meal_photo_schema(conn)
        output.put(None)
    except Exception as exc:  # pragma: no cover - returned to parent process
        output.put(f"{type(exc).__name__}: {exc}")


def test_two_process_v9_upgrade_is_serialized_and_canonical(tmp_path):
    path = tmp_path / "concurrent.db"
    _v9_predecessor(path)
    ctx = mp.get_context("fork")
    start = ctx.Event()
    output = ctx.Queue()
    workers = [ctx.Process(target=_concurrent_worker, args=(path, start, output)) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(15)
    assert [output.get(timeout=2) for _ in workers] == [None, None]
    assert [worker.exitcode for worker in workers] == [0, 0]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version FROM meal_photo_schema_versions WHERE component='meal_photo_system'"
        ).fetchone() == (10,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
