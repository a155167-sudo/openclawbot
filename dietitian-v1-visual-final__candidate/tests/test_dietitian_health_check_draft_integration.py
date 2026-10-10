from __future__ import annotations

import importlib.util
import sqlite3
import threading
from pathlib import Path

import pytest


def _api_fixture_module():
    path = Path(__file__).with_name("test_dietitian_health_check_api.py")
    spec = importlib.util.spec_from_file_location("draft_api_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_db(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    module = _api_fixture_module()
    path = module._populated_db(tmp_path)
    return path


def _dump(conn):
    return tuple(conn.iterdump())


def _migrate(conn, **kwargs):
    from dietitian_health_check_migration import migrate_dietitian_health_check_draft_schema
    return migrate_dietitian_health_check_draft_schema(conn, **kwargs)


def _table_names(conn):
    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'dietitian_health_check_%'"
        )
    }


def test_formal_migration_handles_fresh_legal_predecessor_and_repeated_init(tmp_path):
    for predecessor in ("absent", "version-zero"):
        path = _base_db(tmp_path / predecessor)
        with sqlite3.connect(path) as conn:
            if predecessor == "version-zero":
                conn.execute("""CREATE TABLE dietitian_health_check_schema_versions (
                    component TEXT PRIMARY KEY NOT NULL, version INTEGER NOT NULL CHECK(version >= 0), updated_at TEXT NOT NULL
                )""")
                conn.execute("INSERT INTO dietitian_health_check_schema_versions VALUES('dietitian_health_check_draft',0,'v0')")
            conn.execute("INSERT INTO vip_health_check_cases SELECT 'case-2',user_id,benefit_key,first_vip_activation_id,activation_event_key,window_started_at,window_ends_at,status,valid_day_count,source_manifest_hash,submitted_at,report_published_at,created_at,updated_at FROM vip_health_check_cases WHERE case_id='case-1'")
            _migrate(conn)
            first = _dump(conn)
            _migrate(conn)
            second = _dump(conn)
            assert first == second
            assert _table_names(conn) == {
                "dietitian_health_check_schema_versions",
                "dietitian_health_check_source_revisions",
                "dietitian_health_check_draft_operations",
            }
            assert conn.execute(
                "SELECT version FROM dietitian_health_check_schema_versions WHERE component='dietitian_health_check_draft'"
            ).fetchone() == (1,)
            source_rows = conn.execute(
                "SELECT case_id,revision,manifest_hash FROM dietitian_health_check_source_revisions ORDER BY case_id"
            ).fetchall()
            assert [(row[0], row[1]) for row in source_rows] == [("case-1", 1), ("case-2", 1)]
            assert len({row[2] for row in source_rows}) == 1
            assert len(source_rows[0][2]) == 64


def test_formal_migration_rejects_partial_or_unknown_layout_before_mutation(tmp_path):
    for name, ddl in (
        ("one-table", "CREATE TABLE dietitian_health_check_source_revisions(case_id TEXT)"),
        ("version-without-tables", "CREATE TABLE dietitian_health_check_schema_versions(component TEXT PRIMARY KEY NOT NULL,version INTEGER NOT NULL CHECK(version >= 0),updated_at TEXT NOT NULL); INSERT INTO dietitian_health_check_schema_versions VALUES('dietitian_health_check_draft',1,'bad')"),
        ("unknown-prefixed", "CREATE TABLE dietitian_health_check_unknown(value TEXT)"),
    ):
        path = _base_db(tmp_path / name)
        with sqlite3.connect(path) as conn:
            conn.executescript(ddl)
            conn.commit()
            before = _dump(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _migrate(conn)
            assert _dump(conn) == before


_V1_LEDGER = """CREATE TABLE dietitian_health_check_schema_versions (
    component TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 0),
    updated_at TEXT NOT NULL
)"""
_V1_SOURCE = """CREATE TABLE dietitian_health_check_source_revisions (
    case_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    manifest_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
)"""
_V1_OPERATION = """CREATE TABLE dietitian_health_check_draft_operations (
    case_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    expected_source_token TEXT NOT NULL,
    expected_review_version INTEGER NOT NULL CHECK(expected_review_version >= 0),
    status TEXT NOT NULL CHECK(status IN ('pending','completed')),
    result_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(case_id,request_id),
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
)"""


def _installed_v1(ledger=_V1_LEDGER, source=_V1_SOURCE, operation=_V1_OPERATION, *, version="1", extra=""):
    return ";".join((
        ledger,
        source,
        operation,
        "INSERT INTO dietitian_health_check_schema_versions "
        "(component,version,updated_at) VALUES"
        f"('dietitian_health_check_draft',{version},'installed')",
        extra,
    ))


@pytest.mark.parametrize(
    ("name", "ddl"),
    (
        ("ledger-missing-check", _installed_v1(ledger=_V1_LEDGER.replace(" CHECK(version >= 0)", ""))),
        ("source-missing-check", _installed_v1(source=_V1_SOURCE.replace(" CHECK(revision >= 1)", ""))),
        ("source-missing-fk", _installed_v1(source=_V1_SOURCE.replace(",\n    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)", ""))),
        ("source-cascade-fk", _installed_v1(source=_V1_SOURCE.replace("REFERENCES vip_health_check_cases(case_id)", "REFERENCES vip_health_check_cases(case_id) ON DELETE CASCADE"))),
        ("operation-missing-version-check", _installed_v1(operation=_V1_OPERATION.replace(" CHECK(expected_review_version >= 0)", ""))),
        ("operation-weakened-status-check", _installed_v1(operation=_V1_OPERATION.replace("status IN ('pending','completed')", "status <> 'invalid'"))),
        ("operation-missing-fk", _installed_v1(operation=_V1_OPERATION.replace(",\n    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)", ""))),
        ("operation-cascade-fk", _installed_v1(operation=_V1_OPERATION.replace("REFERENCES vip_health_check_cases(case_id)", "REFERENCES vip_health_check_cases(case_id) ON DELETE CASCADE"))),
        ("operation-wrong-pk-order", _installed_v1(operation=_V1_OPERATION.replace("PRIMARY KEY(case_id,request_id)", "PRIMARY KEY(request_id,case_id)"))),
        ("operation-extra-unique", _installed_v1(extra="CREATE UNIQUE INDEX unexpected_actor_key ON dietitian_health_check_draft_operations(actor_id)")),
        ("unknown-version", _installed_v1(version="2")),
        ("non-integer-version", _installed_v1(version="1.5")),
        ("invalid-source-revision-row", _installed_v1(extra="PRAGMA ignore_check_constraints=ON; INSERT INTO dietitian_health_check_source_revisions VALUES('case-1',0,'hash','bad'); PRAGMA ignore_check_constraints=OFF")),
        ("invalid-operation-version-row", _installed_v1(extra="PRAGMA ignore_check_constraints=ON; INSERT INTO dietitian_health_check_draft_operations VALUES('case-1','bad-version','actor','payload','token',-1,'pending','','now',''); PRAGMA ignore_check_constraints=OFF")),
        ("invalid-operation-status-row", _installed_v1(extra="PRAGMA ignore_check_constraints=ON; INSERT INTO dietitian_health_check_draft_operations VALUES('case-1','bad-status','actor','payload','token',0,'evil','','now',''); PRAGMA ignore_check_constraints=OFF")),
        ("orphan-source-row", _installed_v1(extra="PRAGMA foreign_keys=OFF; INSERT INTO dietitian_health_check_source_revisions VALUES('missing-case',1,'hash','bad'); PRAGMA foreign_keys=ON")),
    ),
)
def test_installed_v1_contract_negative_matrix_rejects_without_writes(tmp_path, name, ddl):
    path = _base_db(tmp_path / name)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(ddl)
        conn.commit()
        before = _dump(conn)
        before_transaction = conn.in_transaction

        with pytest.raises(sqlite3.IntegrityError):
            _migrate(conn)

        assert conn.in_transaction is before_transaction
        assert _dump(conn) == before


def test_canonical_installed_v1_contract_is_accepted_repeatedly(tmp_path):
    path = _base_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_installed_v1())
        conn.commit()
        before = _dump(conn)
        _migrate(conn)
        _migrate(conn)
        assert _dump(conn) == before


def test_formal_migration_failure_restores_exact_snapshot_and_caller_transaction(tmp_path):
    path = _base_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE caller_sentinel(value TEXT)")
        conn.commit()
        conn.execute("INSERT INTO caller_sentinel VALUES('survive')")
        before = _dump(conn)

        def fail(stage, _conn):
            if stage == "source_revision_table_created":
                raise RuntimeError("injected migration failure")

        with pytest.raises(RuntimeError, match="injected migration failure"):
            _migrate(conn, failure_injector=fail)
        assert conn.in_transaction is True
        assert _dump(conn) == before
        assert conn.execute("SELECT value FROM caller_sentinel").fetchall() == [("survive",)]


def _ready_saver(tmp_path, *, failure_injector=None):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _base_db(tmp_path)
    with sqlite3.connect(path) as conn:
        _migrate(conn)
        conn.commit()
        token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    saver = create_health_check_draft_saver(path, failure_injector=failure_injector)
    return path, token, saver


def _save(saver, token, request_id="op-1", comment="點評", expected_review_version=2):
    return saver(
        "case-1",
        {"good": "做得好", "priority": "先改善", "next_7_days": "七天", "comment": comment},
        token,
        expected_review_version,
        request_id,
        "U11111111111111111111111111111111",
    )


@pytest.mark.parametrize("stage", ["operation_reserved", "domain_written", "result_stored"])
def test_injected_writer_stage_failure_rolls_back_operation_review_and_releases_lock(tmp_path, stage):
    def fail(actual, _conn):
        if actual == stage:
            raise RuntimeError(f"injected {stage}")

    path, token, saver = _ready_saver(tmp_path, failure_injector=fail)
    with sqlite3.connect(path) as conn:
        before_reviews = conn.execute("SELECT * FROM vip_health_check_reviews ORDER BY review_version").fetchall()
    with pytest.raises(RuntimeError, match=stage):
        _save(saver, token)
    with sqlite3.connect(path, timeout=0.1) as conn:
        assert conn.execute("SELECT * FROM vip_health_check_reviews ORDER BY review_version").fetchall() == before_reviews
        assert conn.execute("SELECT COUNT(*) FROM dietitian_health_check_draft_operations").fetchone() == (0,)
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


def test_two_real_sqlite_connections_same_key_have_exactly_one_effect(tmp_path):
    path, token, saver = _ready_saver(tmp_path)
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def worker():
        barrier.wait()
        try:
            results.append(_save(saver, token))
        except Exception as exc:  # evidence retains unexpected implementation errors
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert {result["created"] for result in results} == {True, False}
    assert len({(result["review_id"], result["review_version"]) for result in results}) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews WHERE case_id='case-1'").fetchone() == (3,)
        assert conn.execute("SELECT COUNT(*) FROM dietitian_health_check_draft_operations").fetchone() == (1,)


def test_request_reuse_conflicts_and_exact_retry_after_later_review_returns_original_version(tmp_path):
    from dietitian_health_check_draft import DraftConflict

    path, token, saver = _ready_saver(tmp_path)
    first = _save(saver, token, request_id="stable-key")
    later = _save(saver, token, request_id="later-key", comment="後續點評", expected_review_version=3)
    assert later["review_version"] == first["review_version"] + 1
    replay = _save(saver, token, request_id="stable-key")
    assert replay == {**first, "created": False}
    with pytest.raises(DraftConflict, match="binding conflict"):
        _save(saver, token, request_id="stable-key", comment="偷換 payload")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews WHERE case_id='case-1'").fetchone() == (4,)
