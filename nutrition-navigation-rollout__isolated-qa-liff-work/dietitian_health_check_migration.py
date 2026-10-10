"""Failure-atomic numeric migration for dietitian health-check draft writes."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import re
import sqlite3
import uuid

COMPONENT = "dietitian_health_check_draft"
SCHEMA_VERSION = 1
SCHEMA_LEDGER = "dietitian_health_check_schema_versions"
_TABLES = {
    SCHEMA_LEDGER,
    "dietitian_health_check_source_revisions",
    "dietitian_health_check_draft_operations",
}
_LEDGER_DDL = """CREATE TABLE dietitian_health_check_schema_versions (
    component TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 0),
    updated_at TEXT NOT NULL
)"""
_SOURCE_REVISION_DDL = """CREATE TABLE dietitian_health_check_source_revisions (
    case_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    manifest_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id)
)"""
_OPERATION_DDL = """CREATE TABLE dietitian_health_check_draft_operations (
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
_DDL_BY_TABLE = {
    SCHEMA_LEDGER: _LEDGER_DDL,
    "dietitian_health_check_source_revisions": _SOURCE_REVISION_DDL,
    "dietitian_health_check_draft_operations": _OPERATION_DDL,
}
_SQL_TOKEN = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[(?:\]\]|[^\]])*\]"
    r"|--[^\n]*(?:\n|$)|/\*.*?\*/|[A-Za-z_][A-Za-z0-9_$]*|\d+(?:\.\d+)?"
    r"|<=|>=|<>|!=|==|\|\||<<|>>|\S",
    re.DOTALL,
)


def _columns(conn: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (int(row[0]), row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]), int(row[6]))
        for row in conn.execute(f'PRAGMA table_xinfo("{table}")')
    )


def _sql_fingerprint(sql: str | None) -> tuple[str, ...] | None:
    if sql is None:
        return None
    tokens = []
    for match in _SQL_TOKEN.finditer(sql):
        token = match.group(0)
        if token.startswith(("'", '"', "`", "[", "--", "/*")):
            tokens.append(token)
        else:
            tokens.append(token.lower())
    return tuple(tokens)


def _foreign_keys(conn: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    grouped: dict[int, list[tuple[object, ...]]] = {}
    headers: dict[int, tuple[object, ...]] = {}
    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        fk_id = int(row[0])
        headers[fk_id] = (row[2], row[5], row[6], row[7])
        grouped.setdefault(fk_id, []).append((int(row[1]), row[3], row[4]))
    return tuple(sorted(headers[fk_id] + (tuple(sorted(grouped[fk_id])),) for fk_id in headers))


def _indexes(conn: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    contracts = []
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        name = str(row[1])
        terms = tuple(
            (int(term[1]), term[2], int(term[3]), term[4], int(term[5]))
            for term in conn.execute(f'PRAGMA index_xinfo("{name}")')
        )
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        contracts.append(
            (int(row[2]), str(row[3]), int(row[4]), terms, _sql_fingerprint(None if sql_row is None else sql_row[0]))
        )
    return tuple(sorted(contracts, key=repr))


def _table_contract(conn: sqlite3.Connection, table: str) -> tuple[object, ...]:
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    triggers = tuple(
        (row[0], _sql_fingerprint(row[1]))
        for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
            (table,),
        )
    )
    return (
        _columns(conn, table),
        _foreign_keys(conn, table),
        _indexes(conn, table),
        triggers,
        _sql_fingerprint(None if sql_row is None else sql_row[0]),
    )


def require_canonical_table_contract(
    conn: sqlite3.Connection,
    *,
    table: str,
    table_ddl: str,
    index_ddls: tuple[str, ...] = (),
    error_label: str,
) -> None:
    """Require one installed table to exactly match producer-owned canonical DDL."""
    with sqlite3.connect(":memory:") as canonical:
        canonical.execute(table_ddl)
        for ddl in index_ddls:
            canonical.execute(ddl)
        expected = _table_contract(canonical, table)
    if _table_contract(conn, table) != expected:
        raise sqlite3.IntegrityError(f"unsupported {error_label} schema contract")


_CANONICAL_CONTRACTS: dict[str, tuple[object, ...]] | None = None


def _canonical_contracts() -> dict[str, tuple[object, ...]]:
    global _CANONICAL_CONTRACTS
    if _CANONICAL_CONTRACTS is None:
        with sqlite3.connect(":memory:") as canonical:
            for ddl in _DDL_BY_TABLE.values():
                canonical.execute(ddl)
            _CANONICAL_CONTRACTS = {
                table: _table_contract(canonical, table) for table in _DDL_BY_TABLE
            }
    return _CANONICAL_CONTRACTS


def _require_contract(conn: sqlite3.Connection, table: str) -> None:
    if _table_contract(conn, table) != _canonical_contracts()[table]:
        raise sqlite3.IntegrityError(f"unsupported draft migration contract: {table}")


def _require_valid_v1_rows(conn: sqlite3.Connection) -> None:
    invalid = conn.execute(
        f"""SELECT 1 FROM {SCHEMA_LEDGER}
            WHERE component=? AND (typeof(version) <> 'integer' OR version <> ?)
            UNION ALL
            SELECT 1 FROM dietitian_health_check_source_revisions
            WHERE typeof(revision) <> 'integer' OR revision < 1
            UNION ALL
            SELECT 1 FROM dietitian_health_check_draft_operations
            WHERE typeof(expected_review_version) <> 'integer'
               OR expected_review_version < 0
               OR status NOT IN ('pending','completed')
            LIMIT 1""",
        (COMPONENT, SCHEMA_VERSION),
    ).fetchone()
    if invalid is not None:
        raise sqlite3.IntegrityError("invalid draft migration row contract")
    for table in (
        "dietitian_health_check_source_revisions",
        "dietitian_health_check_draft_operations",
    ):
        if conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchone() is not None:
            raise sqlite3.IntegrityError("invalid draft migration foreign key rows")


def _component_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'dietitian_health_check_%'"
        )
    }


def _preflight(conn: sqlite3.Connection) -> int | None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vip_health_check_cases'"
    ).fetchone() is None:
        raise sqlite3.IntegrityError("draft migration predecessor schema missing")
    tables = _component_tables(conn)
    if SCHEMA_LEDGER not in tables:
        if tables:
            raise sqlite3.IntegrityError("partial draft migration layout")
        return None
    _require_contract(conn, SCHEMA_LEDGER)
    row = conn.execute(
        f"SELECT version,typeof(version) FROM {SCHEMA_LEDGER} WHERE component=?", (COMPONENT,)
    ).fetchone()
    if row is not None and row[1] != "integer":
        raise sqlite3.IntegrityError("unsupported draft migration version marker")
    version = None if row is None else int(row[0])
    if version in (None, 0):
        if tables != {SCHEMA_LEDGER}:
            raise sqlite3.IntegrityError("partial draft migration layout")
        return version
    if version != SCHEMA_VERSION or tables != _TABLES:
        raise sqlite3.IntegrityError("unsupported draft migration layout")
    for table in _TABLES:
        _require_contract(conn, table)
    _require_valid_v1_rows(conn)
    return version


def migrate_dietitian_health_check_draft_schema(
    conn: sqlite3.Connection,
    *,
    failure_injector: Callable[[str, sqlite3.Connection], None] | None = None,
) -> None:
    """Advance absent/version-0 predecessor to v1; never commit caller state."""
    version = _preflight(conn)
    if version == SCHEMA_VERSION:
        return
    inject = failure_injector or (lambda _stage, _conn: None)
    savepoint = "dietitian_draft_schema_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if version is None:
            conn.execute(_LEDGER_DDL)
            inject("version_ledger_created", conn)
        conn.execute(_SOURCE_REVISION_DDL)
        inject("source_revision_table_created", conn)
        conn.execute(
            """INSERT INTO dietitian_health_check_source_revisions
               (case_id,revision,manifest_hash,updated_at)
               SELECT case_id,1,source_manifest_hash,updated_at FROM vip_health_check_cases"""
        )
        conn.execute(_OPERATION_DDL)
        inject("operation_table_created", conn)
        now = datetime.now(timezone.utc).isoformat()
        if version is None:
            conn.execute(
                f"INSERT INTO {SCHEMA_LEDGER}(component,version,updated_at) VALUES(?,?,?)",
                (COMPONENT, SCHEMA_VERSION, now),
            )
        else:
            changed = conn.execute(
                f"UPDATE {SCHEMA_LEDGER} SET version=?,updated_at=? WHERE component=? AND version=0",
                (SCHEMA_VERSION, now, COMPONENT),
            )
            if changed.rowcount != 1:
                raise sqlite3.IntegrityError("draft migration version changed")
        inject("version_recorded", conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    _preflight(conn)


def require_dietitian_health_check_draft_schema(conn: sqlite3.Connection) -> None:
    """Validate installed v1 without creating or committing schema."""
    if _preflight(conn) != SCHEMA_VERSION:
        raise sqlite3.IntegrityError("dietitian draft schema migration required")
