"""Fail-closed dispatch identity and printer export for paid subscriptions.

This module owns no Google or LINE credentials.  Its publisher accepts a narrow
sheet adapter so production and tests share the same staged -> write -> verify ->
published boundary without pretending SQLite can roll back a Sheet write.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import hmac
import json
import sqlite3
import unicodedata
from typing import Any, Callable, Iterable, Sequence

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Match

ORIGINAL_HEADERS = [
    "實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排",
    "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白",
    "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態",
]
DISPATCH_HEADERS = ["Dispatch_Row_ID", "Order_ID", "Menu_Version"]
NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
RECEIPT_VERSION = 1
TRUSTED_WRITER = "subscription_dispatch_contract.publish_schedule/v1"
PRINT_STATUS_POLICY = "external_mutable_not_publication_identity"


def _dispatch_http_error(status_code: int, detail: str, *, authenticate: bool = False) -> HTTPException:
    headers = dict(NO_STORE_HEADERS)
    if authenticate:
        headers["WWW-Authenticate"] = "Bearer"
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


class _PrivatePrinterRoute(APIRoute):
    """Keep framework-generated failures on this router private and non-cacheable."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request):
            try:
                result = await handler(request)
            except HTTPException as exc:
                headers = dict(NO_STORE_HEADERS)
                headers.update(exc.headers or {})
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)
            except Exception:
                return JSONResponse(
                    {"detail": "printer request failed"}, status_code=500,
                    headers=NO_STORE_HEADERS,
                )
            result.headers.update(NO_STORE_HEADERS)
            return result

        return private_handler


class DispatchError(RuntimeError):
    pass


class DispatchNotEligible(DispatchError):
    pass


class DispatchConflict(DispatchError):
    pass


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dispatch timestamp must include timezone")
    return value.isoformat(timespec="seconds")


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _service_date(value: object) -> str:
    raw = _text(value).replace("/", "-")
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise DispatchConflict("invalid service date") from exc


_SCHEMA_VERSION_DDL = """CREATE TABLE subscription_dispatch_schema_versions (
    version INTEGER PRIMARY KEY NOT NULL CHECK(version = 2), applied_at TEXT NOT NULL)"""
_SCHEMA_VERSION_V1_DDL = """CREATE TABLE subscription_dispatch_schema_versions(
    version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL)"""
_ROWS_V1_DDL = """CREATE TABLE subscription_dispatch_rows(
    dispatch_row_id TEXT PRIMARY KEY NOT NULL, order_id INTEGER NOT NULL,
    customer_uid TEXT NOT NULL, store_id TEXT NOT NULL CHECK(store_id = 'nanjing'),
    workbook_id TEXT NOT NULL, worksheet_id INTEGER, worksheet_title TEXT NOT NULL,
    service_date TEXT NOT NULL, lunch TEXT NOT NULL, dinner TEXT NOT NULL,
    menu_version TEXT NOT NULL, formalized_at TEXT NOT NULL DEFAULT '',
    publish_state TEXT NOT NULL CHECK(publish_state IN ('staged','published','superseded')),
    created_at TEXT NOT NULL, UNIQUE(order_id, menu_version, service_date))"""
_ROWS_V1_PAYLOAD_DDL = """CREATE TABLE subscription_dispatch_rows(
    dispatch_row_id TEXT PRIMARY KEY NOT NULL, order_id INTEGER NOT NULL,
    customer_uid TEXT NOT NULL, store_id TEXT NOT NULL CHECK(store_id = 'nanjing'),
    workbook_id TEXT NOT NULL, worksheet_id INTEGER, worksheet_title TEXT NOT NULL,
    service_date TEXT NOT NULL, lunch TEXT NOT NULL, dinner TEXT NOT NULL,
    menu_version TEXT NOT NULL, formalized_at TEXT NOT NULL DEFAULT '',
    publish_state TEXT NOT NULL CHECK(publish_state IN ('staged','published','superseded')),
    created_at TEXT NOT NULL, source_payload_json TEXT NOT NULL DEFAULT '',
    source_payload_hash TEXT NOT NULL DEFAULT '', UNIQUE(order_id, menu_version, service_date))"""
_ROWS_DDL = """CREATE TABLE subscription_dispatch_rows (
    dispatch_row_id TEXT PRIMARY KEY NOT NULL, order_id INTEGER NOT NULL,
    customer_uid TEXT NOT NULL, store_id TEXT NOT NULL CHECK(store_id = 'nanjing'),
    workbook_id TEXT NOT NULL, worksheet_id INTEGER, worksheet_title TEXT NOT NULL,
    service_date TEXT NOT NULL, lunch TEXT NOT NULL, dinner TEXT NOT NULL,
    menu_version TEXT NOT NULL, formalized_at TEXT NOT NULL DEFAULT '',
    publish_state TEXT NOT NULL CHECK(publish_state IN ('staged','published','superseded')),
    created_at TEXT NOT NULL, source_payload_json TEXT NOT NULL DEFAULT '',
    source_payload_hash TEXT NOT NULL DEFAULT '', UNIQUE(order_id, menu_version, service_date))"""
_RECEIPTS_DDL = """CREATE TABLE subscription_dispatch_publication_receipts (
    receipt_id TEXT PRIMARY KEY NOT NULL, receipt_version INTEGER NOT NULL CHECK(receipt_version = 1),
    trusted_writer TEXT NOT NULL CHECK(trusted_writer = 'subscription_dispatch_contract.publish_schedule/v1'),
    dispatch_row_id TEXT NOT NULL UNIQUE, order_id INTEGER NOT NULL, customer_uid TEXT NOT NULL,
    store_id TEXT NOT NULL CHECK(store_id = 'nanjing'), workbook_id TEXT NOT NULL,
    worksheet_id INTEGER NOT NULL CHECK(worksheet_id >= 0), worksheet_title TEXT NOT NULL,
    service_date TEXT NOT NULL, menu_version TEXT NOT NULL, source_payload_json TEXT NOT NULL,
    source_payload_hash TEXT NOT NULL CHECK(length(source_payload_hash) = 64), published_at TEXT NOT NULL,
    receipt_hash TEXT NOT NULL UNIQUE CHECK(length(receipt_hash) = 64),
    FOREIGN KEY(dispatch_row_id) REFERENCES subscription_dispatch_rows(dispatch_row_id),
    FOREIGN KEY(order_id) REFERENCES subscription_orders(id))"""
_INDEX_DDL = "CREATE INDEX subscription_dispatch_receipts_lookup ON subscription_dispatch_publication_receipts(store_id, workbook_id, service_date, dispatch_row_id)"
_TRIGGER_INSERT = """CREATE TRIGGER subscription_dispatch_receipts_no_update BEFORE UPDATE ON subscription_dispatch_publication_receipts BEGIN SELECT RAISE(ABORT, 'publication receipt is immutable'); END"""
_TRIGGER_DELETE = """CREATE TRIGGER subscription_dispatch_receipts_no_delete BEFORE DELETE ON subscription_dispatch_publication_receipts BEGIN SELECT RAISE(ABORT, 'publication receipt is immutable'); END"""


def _sql_fingerprint(sql: str | None) -> str:
    # Preserve quoted literal case: writer identity and CHECK values are semantic.
    return " ".join((sql or "").split())


def _canonical_ddl(sql: str | None) -> str:
    """Canonicalize DDL while treating comments as whitespace, never semantics."""
    source = sql or ""
    output: list[str] = []
    index = 0
    quote = ""
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            output.append(char)
            if char == quote:
                if following == quote:
                    output.append(following)
                    index += 1
                else:
                    quote = ""
        elif char in ("'", '"', "`"):
            quote = char
            output.append(char)
        elif char == "[":
            quote = "]"
            output.append(char)
        elif char == "-" and following == "-":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        elif char == "/" and following == "*":
            end = source.find("*/", index + 2)
            if end < 0:
                raise DispatchConflict("unterminated dispatch schema comment")
            index = end + 2
            continue
        elif not char.isspace():
            output.append(char.lower())
        index += 1
    if quote:
        raise DispatchConflict("unterminated dispatch schema quote")
    return "".join(output)


def _object_sql(conn: sqlite3.Connection, object_type: str, name: str) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)).fetchone()
    return None if row is None else row[0]


def _verify_dispatch_schema(conn: sqlite3.Connection) -> None:
    expected = {
        ("table", "subscription_dispatch_schema_versions"): _SCHEMA_VERSION_DDL,
        ("table", "subscription_dispatch_rows"): _ROWS_DDL,
        ("table", "subscription_dispatch_publication_receipts"): _RECEIPTS_DDL,
        ("index", "subscription_dispatch_receipts_lookup"): _INDEX_DDL,
        ("trigger", "subscription_dispatch_receipts_no_update"): _TRIGGER_INSERT,
        ("trigger", "subscription_dispatch_receipts_no_delete"): _TRIGGER_DELETE,
    }
    for (kind, name), ddl in expected.items():
        if _sql_fingerprint(_object_sql(conn, kind, name)) != _sql_fingerprint(ddl):
            raise DispatchConflict(f"dispatch schema mismatch: {name}")
    owned_objects = {(r[0], r[1]) for r in conn.execute("""
        SELECT type,name FROM sqlite_master
         WHERE sql IS NOT NULL AND type IN ('index','trigger')
           AND tbl_name IN ('subscription_dispatch_schema_versions',
                            'subscription_dispatch_rows',
                            'subscription_dispatch_publication_receipts')
    """)}
    if owned_objects != {
        ("index", "subscription_dispatch_receipts_lookup"),
        ("trigger", "subscription_dispatch_receipts_no_update"),
        ("trigger", "subscription_dispatch_receipts_no_delete"),
    }:
        raise DispatchConflict("unknown dispatch schema object")
    version_rows = conn.execute(
        "SELECT version,typeof(version) FROM subscription_dispatch_schema_versions"
    ).fetchall()
    if [tuple(row) for row in version_rows] != [(2, "integer")]:
        raise DispatchConflict("dispatch schema version mismatch")
    # Exercise complete SQLite metadata, not merely table presence/column names.
    expected_columns = {
        "subscription_dispatch_schema_versions": 2,
        "subscription_dispatch_rows": 16,
        "subscription_dispatch_publication_receipts": 16,
    }
    for table, count in expected_columns.items():
        if len(conn.execute(f"PRAGMA table_xinfo({table})").fetchall()) != count:
            raise DispatchConflict(f"dispatch column contract mismatch: {table}")
    fks = conn.execute("PRAGMA foreign_key_list(subscription_dispatch_publication_receipts)").fetchall()
    if {(r[2], r[3], r[4], r[5], r[6], r[7]) for r in fks} != {
        ("subscription_dispatch_rows", "dispatch_row_id", "dispatch_row_id", "NO ACTION", "NO ACTION", "NONE"),
        ("subscription_orders", "order_id", "id", "NO ACTION", "NO ACTION", "NONE"),
    }:
        raise DispatchConflict("dispatch receipt foreign-key contract mismatch")
    index_rows = conn.execute("PRAGMA index_xinfo(subscription_dispatch_receipts_lookup)").fetchall()
    if [r[2] for r in index_rows if r[5]] != ["store_id", "workbook_id", "service_date", "dispatch_row_id"]:
        raise DispatchConflict("dispatch receipt index contract mismatch")


def ensure_dispatch_schema(conn: sqlite3.Connection) -> None:
    """Failure-atomic v2 installer; never commits a caller-owned transaction."""
    savepoint = "ensure_dispatch_schema_v2"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'subscription_dispatch_%'"
        )}
        required = {"subscription_dispatch_schema_versions", "subscription_dispatch_rows",
                    "subscription_dispatch_publication_receipts"}
        if names and names != required:
            # Only the exact shipped v1 pair is migratable; it has no receipt and cannot
            # fabricate publication provenance.  Unknown/partial layouts fail closed.
            legacy = {"subscription_dispatch_schema_versions", "subscription_dispatch_rows"}
            if names != legacy:
                raise DispatchConflict("partial or unknown dispatch schema")
            row_ddl = _canonical_ddl(_object_sql(conn, "table", "subscription_dispatch_rows"))
            supported_rows = {
                _canonical_ddl(_ROWS_V1_DDL): 14,
                _canonical_ddl(_ROWS_V1_PAYLOAD_DDL): 16,
            }
            if row_ddl not in supported_rows:
                raise DispatchConflict("weakened or unsupported dispatch predecessor DDL")
            if _canonical_ddl(_object_sql(conn, "table", "subscription_dispatch_schema_versions")) != _canonical_ddl(_SCHEMA_VERSION_V1_DDL):
                raise DispatchConflict("unsupported dispatch predecessor version DDL")
            columns = [r[1] for r in conn.execute("PRAGMA table_xinfo(subscription_dispatch_rows)")]
            if len(columns) != supported_rows[row_ddl]:
                raise DispatchConflict("unsupported dispatch predecessor columns")
            legacy_objects = conn.execute("""
                SELECT type,name FROM sqlite_master
                 WHERE sql IS NOT NULL AND type IN ('index','trigger')
                   AND tbl_name IN ('subscription_dispatch_schema_versions',
                                    'subscription_dispatch_rows')
            """).fetchall()
            if legacy_objects:
                raise DispatchConflict("unsupported dispatch predecessor objects")
            old_versions = conn.execute(
                "SELECT version,typeof(version) FROM subscription_dispatch_schema_versions"
            ).fetchall()
            if [tuple(row) for row in old_versions] != [(1, "integer")]:
                raise DispatchConflict("unsupported dispatch predecessor version")
            conn.execute("DROP TABLE subscription_dispatch_schema_versions")
            conn.execute("ALTER TABLE subscription_dispatch_rows RENAME TO subscription_dispatch_rows_v1")
            conn.execute(_ROWS_DDL)
            source_projection = (
                ",source_payload_json,source_payload_hash" if len(columns) == 16
                else ",'' AS source_payload_json,'' AS source_payload_hash"
            )
            conn.execute("""INSERT INTO subscription_dispatch_rows
                (dispatch_row_id,order_id,customer_uid,store_id,workbook_id,worksheet_id,
                 worksheet_title,service_date,lunch,dinner,menu_version,formalized_at,
                 publish_state,created_at,source_payload_json,source_payload_hash)
                SELECT dispatch_row_id,order_id,customer_uid,store_id,workbook_id,worksheet_id,
                 worksheet_title,service_date,lunch,dinner,menu_version,formalized_at,
                 publish_state,created_at""" + source_projection + " FROM subscription_dispatch_rows_v1")
            conn.execute("DROP TABLE subscription_dispatch_rows_v1")
        elif names == required:
            _verify_dispatch_schema(conn)
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return
        elif names:
            raise DispatchConflict("partial dispatch schema")

        if not names:
            conn.execute(_ROWS_DDL)
        conn.execute(_SCHEMA_VERSION_DDL)
        conn.execute(_RECEIPTS_DDL)
        conn.execute(_INDEX_DDL)
        conn.execute(_TRIGGER_INSERT)
        conn.execute(_TRIGGER_DELETE)
        conn.execute("INSERT INTO subscription_dispatch_schema_versions(version,applied_at) VALUES(2,?)",
                     (datetime.now().astimezone().isoformat(timespec="seconds"),))
        _verify_dispatch_schema(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _validate_schedule(schedule_rows: Sequence[Sequence[object]]) -> None:
    if not schedule_rows or list(schedule_rows[0]) != ORIGINAL_HEADERS:
        raise DispatchConflict("schedule must have the exact original 14-column header")
    if len(schedule_rows) < 2:
        raise DispatchConflict("schedule has no rows")
    for row in schedule_rows[1:]:
        if len(row) != len(ORIGINAL_HEADERS):
            raise DispatchConflict("schedule row must retain exactly 14 original columns")


def _canonical_source_row(row: Sequence[object]) -> list[str]:
    if len(row) != len(ORIGINAL_HEADERS):
        raise DispatchConflict("schedule row must retain exactly 14 original columns")
    return [_text(cell) for cell in row]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_payload(row: Sequence[object]) -> tuple[str, str]:
    payload_json = _canonical_json(_canonical_source_row(row))
    return payload_json, _sha256_text(payload_json)


def _receipt_values(row: Sequence[object]) -> tuple[object, ...]:
    return tuple(row)


def _receipt_hash(values: Sequence[object]) -> str:
    return _sha256_text(_canonical_json(list(values)))


def _trusted_publication_rows(conn: sqlite3.Connection, where_sql: str, parameters: Sequence[object]):
    """Return only producer-receipted rows whose complete current binding still matches.

    The receipt is provenance from the restricted publish writer, not a MAC against an
    attacker with arbitrary database write access.  Its purpose is to seal the trusted
    producer path and keep legacy/manual ``published`` rows fail-closed.
    """
    names = (
        "receipt_id", "receipt_version", "trusted_writer", "dispatch_row_id", "order_id",
        "customer_uid", "store_id", "workbook_id", "worksheet_id", "worksheet_title",
        "service_date", "menu_version", "source_payload_json", "source_payload_hash",
        "published_at", "receipt_hash", "d_customer_uid", "d_store_id", "d_workbook_id",
        "d_worksheet_id", "d_worksheet_title", "d_service_date", "d_menu_version",
        "d_payload_json", "d_payload_hash", "d_formalized_at", "d_publish_state",
        "order_user_id", "order_status", "lunch", "dinner",
    )
    records = conn.execute(
        f"""SELECT r.receipt_id,r.receipt_version,r.trusted_writer,r.dispatch_row_id,
                   r.order_id,r.customer_uid,r.store_id,r.workbook_id,r.worksheet_id,
                   r.worksheet_title,r.service_date,r.menu_version,r.source_payload_json,
                   r.source_payload_hash,r.published_at,r.receipt_hash,
                   d.customer_uid,d.store_id,d.workbook_id,d.worksheet_id,d.worksheet_title,
                   d.service_date,d.menu_version,d.source_payload_json,d.source_payload_hash,
                   d.formalized_at,d.publish_state,o.user_id,o.status,d.lunch,d.dinner
              FROM subscription_dispatch_publication_receipts r
              JOIN subscription_dispatch_rows d ON d.dispatch_row_id=r.dispatch_row_id
              JOIN subscription_orders o ON o.id=d.order_id
             WHERE {where_sql}""",
        tuple(parameters),
    ).fetchall()
    trusted: list[dict[str, Any]] = []
    for raw in records:
        row: dict[str, Any] = dict(zip(names, tuple(raw)))
        source_columns: Any = None
        try:
            source_columns = json.loads(row["source_payload_json"])
            canonical_payload = _canonical_json(source_columns)
            receipt_values = tuple(row[name] for name in names[1:15])
            bindings = (
                row["receipt_version"] == RECEIPT_VERSION
                and row["trusted_writer"] == TRUSTED_WRITER
                and row["customer_uid"] == row["d_customer_uid"] == row["order_user_id"]
                and row["store_id"] == row["d_store_id"]
                and row["workbook_id"] == row["d_workbook_id"]
                and row["worksheet_id"] == row["d_worksheet_id"]
                and row["worksheet_title"] == row["d_worksheet_title"]
                and row["service_date"] == row["d_service_date"]
                and row["menu_version"] == row["d_menu_version"]
                and row["source_payload_json"] == row["d_payload_json"] == canonical_payload
                and row["source_payload_hash"] == row["d_payload_hash"] == _sha256_text(canonical_payload)
                and row["published_at"] == row["d_formalized_at"]
                and row["d_publish_state"] == "published"
                and row["order_status"] == "activated"
                and isinstance(source_columns, list)
                and len(source_columns) == len(ORIGINAL_HEADERS)
                and _service_date(source_columns[0]) == row["service_date"]
                and source_columns[2] == row["lunch"]
                and source_columns[5] == row["dinner"]
                and row["receipt_hash"] == _receipt_hash(receipt_values)
                and row["receipt_id"] == "receipt-" + row["receipt_hash"]
            )
        except (TypeError, ValueError, IndexError, json.JSONDecodeError, DispatchError):
            bindings = False
        if bindings:
            row["source_columns"] = source_columns
            trusted.append(row)
    return trusted


def _tagged_from_records(
    schedule_rows: Sequence[Sequence[object]], records: dict[str, sqlite3.Row | tuple]
) -> list[list[object]]:
    tagged: list[list[object]] = [ORIGINAL_HEADERS + DISPATCH_HEADERS]
    for row in schedule_rows[1:]:
        service_date = _service_date(row[0])
        record = records[service_date]
        tagged.append(list(row) + [record[0], str(record[1]), record[2]])
    return tagged


def stage_schedule(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    snapshot_uid: str,
    workbook_id: str,
    worksheet_title: str,
    schedule_rows: Sequence[Sequence[object]],
    now: datetime,
    id_factory: Callable[[], str],
    store_id: str = "nanjing",
) -> list[list[object]]:
    """Reserve stable row IDs only for an activated order owned by snapshot UID."""
    _validate_schedule(schedule_rows)
    created_at = _aware_iso(now)
    if store_id != "nanjing" or not workbook_id or not worksheet_title:
        raise DispatchNotEligible("dispatch target is not explicitly pinned")
    order = conn.execute(
        "SELECT user_id,status FROM subscription_orders WHERE id=?", (order_id,)
    ).fetchone()
    if not order or order[1] != "activated" or not snapshot_uid or order[0] != snapshot_uid:
        raise DispatchNotEligible("order is not activated or snapshot owner mismatches")
    menu_version = f"order-{order_id}-v1"
    dates = [_service_date(row[0]) for row in schedule_rows[1:]]
    if len(set(dates)) != len(dates):
        raise DispatchConflict("duplicate service date")

    existing_rows = conn.execute(
        """SELECT dispatch_row_id,order_id,menu_version,service_date,lunch,dinner,
                  workbook_id,worksheet_title,customer_uid,store_id,
                  source_payload_json,source_payload_hash
             FROM subscription_dispatch_rows
            WHERE order_id=? AND menu_version=? ORDER BY service_date""",
        (order_id, menu_version),
    ).fetchall()
    expected = {
        _service_date(row[0]): _source_payload(row)
        for row in schedule_rows[1:]
    }
    if existing_rows:
        actual = {row[3]: (row[10], row[11]) for row in existing_rows}
        bindings_ok = all(
            row[6] == workbook_id and row[7] == worksheet_title
            and row[8] == snapshot_uid and row[9] == store_id
            for row in existing_rows
        )
        if actual != expected or not bindings_ok:
            raise DispatchConflict("retry payload or target differs from staged identity")
        records = {row[3]: (row[0], row[1], row[2]) for row in existing_rows}
        return _tagged_from_records(schedule_rows, records)

    conn.execute("SAVEPOINT stage_dispatch")
    try:
        records: dict[str, tuple[str, int, str]] = {}
        for row in schedule_rows[1:]:
            service_date = _service_date(row[0])
            dispatch_row_id = str(id_factory()).strip()
            if not dispatch_row_id:
                raise DispatchConflict("empty dispatch row id")
            source_payload_json, source_payload_hash = _source_payload(row)
            conn.execute(
                """INSERT INTO subscription_dispatch_rows
                   (dispatch_row_id,order_id,customer_uid,store_id,workbook_id,
                    worksheet_id,worksheet_title,service_date,lunch,dinner,
                    menu_version,formalized_at,publish_state,created_at,
                    source_payload_json,source_payload_hash)
                   VALUES(?,?,?,?,?,NULL,?,?,?,?,?,'','staged',?,?,?)""",
                (dispatch_row_id, order_id, snapshot_uid, store_id, workbook_id,
                 worksheet_title, service_date, _text(row[2]), _text(row[5]),
                 menu_version, created_at, source_payload_json, source_payload_hash),
            )
            records[service_date] = (dispatch_row_id, order_id, menu_version)
        conn.execute("RELEASE SAVEPOINT stage_dispatch")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT stage_dispatch")
        conn.execute("RELEASE SAVEPOINT stage_dispatch")
        raise
    return _tagged_from_records(schedule_rows, records)


def _sheet_fingerprint(rows: Iterable[Sequence[object]]) -> list[list[str]]:
    return [[_text(cell) for cell in row] for row in rows]


def merge_tagged_schedule_rows(
    existing_rows: Sequence[Sequence[object]],
    tagged_rows: Sequence[Sequence[object]],
) -> list[list[object]]:
    """Upsert dispatch rows by durable ID without deleting unrelated Sheet rows.

    The physical row number is deliberately not an identity.  Existing print state
    is operator-owned and therefore survives a retry/update.
    """
    _validate_schedule([ORIGINAL_HEADERS] + [list(row[:14]) for row in tagged_rows[1:]])
    if list(tagged_rows[0]) != ORIGINAL_HEADERS + DISPATCH_HEADERS:
        raise DispatchConflict("tagged schedule header mismatch")
    result = [list(row) for row in existing_rows]
    positions: dict[str, int] = {}
    for index, row in enumerate(result):
        if len(row) > 14 and _text(row[14]):
            dispatch_id = _text(row[14])
            if dispatch_id in positions:
                raise DispatchConflict("duplicate dispatch row id in Sheet")
            positions[dispatch_id] = index

    header = list(tagged_rows[0])
    if header not in result:
        result.append(header)
    for incoming in tagged_rows[1:]:
        if len(incoming) != 17 or not _text(incoming[14]):
            raise DispatchConflict("tagged schedule row identity missing")
        dispatch_id = _text(incoming[14])
        replacement = list(incoming)
        if dispatch_id in positions:
            old = result[positions[dispatch_id]]
            if len(old) > 13 and _text(old[13]):
                replacement[13] = old[13]
            result[positions[dispatch_id]] = replacement
        else:
            positions[dispatch_id] = len(result)
            result.append(replacement)
    return result


def schedule_is_already_published(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    workbook_id: str,
    worksheet_title: str,
    tagged_rows: Sequence[Sequence[object]],
) -> bool:
    """Read-only exact-ID gate used before any replay side effect."""
    expected_ids = [str(row[-3]) for row in tagged_rows[1:]]
    trusted = _trusted_publication_rows(
        conn,
        "r.order_id=? AND r.workbook_id=? AND r.worksheet_title=? ORDER BY r.service_date",
        (order_id, workbook_id, worksheet_title),
    )
    by_id = {row["dispatch_row_id"]: row for row in trusted}
    return bool(
        expected_ids
        and set(by_id) == set(expected_ids)
        and all(
            by_id[str(tagged[14])]["source_payload_json"] == _source_payload(tagged[:14])[0]
            and str(tagged[15]) == str(order_id)
            and tagged[16] == by_id[str(tagged[14])]["menu_version"]
            for tagged in tagged_rows[1:]
        )
    )


def publish_schedule(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    sheet: object,
    tagged_rows: Sequence[Sequence[object]],
    now: datetime,
) -> None:
    """Replace deterministically, verify exact readback, then expose rows.

    A failed write/readback leaves DB rows staged.  A retry uses the same IDs and
    replaces the same schedule block, so a prior ambiguous Sheet success does not
    append duplicates.
    """
    formalized_at = _aware_iso(now)
    worksheet_id = getattr(sheet, "id", None)
    worksheet_title = str(getattr(sheet, "title", "") or "")
    if isinstance(worksheet_id, bool) or not isinstance(worksheet_id, int) or worksheet_id < 0:
        raise RuntimeError("worksheet numeric identity missing")
    sheet_ids = [str(row[-3]) for row in tagged_rows[1:]]

    # Fresh read-only preflight.  A completed exact retry must not touch Sheet.
    order = conn.execute("SELECT status FROM subscription_orders WHERE id=?", (order_id,)).fetchone()
    if not order or order[0] != "activated":
        raise DispatchNotEligible("order no longer activated")
    before = conn.execute(
        """SELECT dispatch_row_id,worksheet_id,worksheet_title,publish_state,
                  workbook_id,source_payload_json,source_payload_hash
             FROM subscription_dispatch_rows WHERE order_id=? ORDER BY service_date""",
        (order_id,),
    ).fetchall()
    if not before or [row[0] for row in before] != sheet_ids:
        raise DispatchConflict("dispatch rows do not match requested Sheet write")
    if any(row[2] != worksheet_title for row in before):
        raise DispatchConflict("dispatch worksheet title changed")
    tagged_payloads = {
        str(row[14]): _source_payload(row[:14]) for row in tagged_rows[1:]
    }
    if any(tagged_payloads[row[0]] != (row[5], row[6]) for row in before):
        raise DispatchConflict("dispatch complete payload changed")
    states = {row[3] for row in before}
    if states == {"published"}:
        if any(row[1] != worksheet_id for row in before):
            raise DispatchConflict("published worksheet identity changed")
        if not schedule_is_already_published(
            conn,
            order_id=order_id,
            workbook_id=before[0][4],
            worksheet_title=worksheet_title,
            tagged_rows=tagged_rows,
        ):
            raise DispatchConflict("published receipt binding is invalid")
        return
    if states != {"staged"}:
        raise DispatchConflict("dispatch publish states are inconsistent")

    try:
        expected_readback = sheet.replace_schedule(tagged_rows) or tagged_rows
        observed = sheet.read_schedule()
    except Exception as exc:
        raise RuntimeError("dispatch Sheet publish/readback failed") from exc
    if _sheet_fingerprint(observed) != _sheet_fingerprint(expected_readback):
        raise RuntimeError("dispatch Sheet readback mismatch")

    conn.execute("SAVEPOINT publish_dispatch")
    try:
        order = conn.execute(
            "SELECT user_id,status FROM subscription_orders WHERE id=?", (order_id,)
        ).fetchone()
        if not order or order[1] != "activated":
            raise DispatchNotEligible("order no longer activated")
        current = conn.execute(
            """SELECT dispatch_row_id,order_id,customer_uid,store_id,workbook_id,
                      worksheet_title,service_date,menu_version,source_payload_json,
                      source_payload_hash
                 FROM subscription_dispatch_rows
                WHERE order_id=? AND publish_state='staged' ORDER BY service_date""",
            (order_id,),
        ).fetchall()
        if (
            not current
            or [row[0] for row in current] != sheet_ids
            or any(row[5] != worksheet_title or row[2] != order[0] for row in current)
        ):
            raise DispatchConflict("staged rows do not match verified Sheet")
        for row in current:
            values = (
                RECEIPT_VERSION, TRUSTED_WRITER, row[0], row[1], row[2], row[3],
                row[4], worksheet_id, row[5], row[6], row[7], row[8], row[9], formalized_at,
            )
            receipt_hash = _receipt_hash(values)
            conn.execute(
                """INSERT INTO subscription_dispatch_publication_receipts
                   (receipt_id,receipt_version,trusted_writer,dispatch_row_id,order_id,
                    customer_uid,store_id,workbook_id,worksheet_id,worksheet_title,
                    service_date,menu_version,source_payload_json,source_payload_hash,
                    published_at,receipt_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("receipt-" + receipt_hash, *values, receipt_hash),
            )
        changed = conn.execute(
            """UPDATE subscription_dispatch_rows
                  SET worksheet_id=?, formalized_at=?, publish_state='published'
                WHERE order_id=? AND publish_state='staged'""",
            (worksheet_id, formalized_at, order_id),
        ).rowcount
        if changed != len(current):
            raise DispatchConflict("dispatch publish version conflict")
        conn.execute(
            "UPDATE subscription_orders SET formalized_at=? WHERE id=? AND status='activated'",
            (formalized_at, order_id),
        )
        conn.execute("RELEASE SAVEPOINT publish_dispatch")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT publish_dispatch")
        conn.execute("RELEASE SAVEPOINT publish_dispatch")
        raise


def create_dispatch_router(
    *,
    connection_factory: Callable[[], sqlite3.Connection],
    export_token: str,
    workbook_id: str,
    now_factory: Callable[[], datetime],
    allowed_date_window_days: int = 1,
    row_limit: int = 500,
) -> APIRouter:
    if len((export_token or "").encode("utf-8")) < 32:
        raise ValueError("dedicated printer export credential is required")
    if not workbook_id:
        raise ValueError("runtime workbook id is required")
    router = APIRouter(route_class=_PrivatePrinterRoute)

    @router.get("/internal/printer/v1/dispatch-contract")
    def export_dispatch_contract(
        request: Request,
        response: Response,
        store_id: str | None = Query(default=None),
        service_date: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ):
        response.headers.update(NO_STORE_HEADERS)
        if authorization is None:
            raise _dispatch_http_error(401, "authentication required", authenticate=True)
        scheme, separator, supplied = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.encode("utf-8"), export_token.encode("utf-8")
        ):
            raise _dispatch_http_error(403, "forbidden")
        if (set(request.query_params) != {"store_id", "service_date"}
                or len(request.query_params.multi_items()) != 2):
            raise _dispatch_http_error(422, "unsupported query")
        if not store_id or not service_date:
            raise _dispatch_http_error(422, "invalid query")
        if store_id != "nanjing":
            raise _dispatch_http_error(404, "not found")
        try:
            parsed_service_date = date.fromisoformat(service_date)
        except (TypeError, ValueError):
            raise _dispatch_http_error(422, "invalid query") from None
        now = now_factory()
        _aware_iso(now)
        if abs((parsed_service_date - now.date()).days) > allowed_date_window_days:
            raise _dispatch_http_error(422, "date outside allowed window")
        conn = connection_factory()
        try:
            rows = _trusted_publication_rows(
                conn,
                """r.store_id=? AND r.workbook_id=? AND r.service_date=?
                   ORDER BY r.dispatch_row_id LIMIT ?""",
                (store_id, workbook_id, parsed_service_date.isoformat(), row_limit + 1),
            )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise _dispatch_http_error(503, "dispatch storage unavailable") from exc
        finally:
            conn.close()
        if len(rows) > row_limit:
            raise _dispatch_http_error(503, "row limit exceeded")
        return {
            "contract_version": 1,
            "store_id": store_id,
            "workbook_id": workbook_id,
            "generated_at": _aware_iso(now),
            "rows": [
                {
                    "dispatch_row_id": row["dispatch_row_id"],
                    "worksheet_id": row["worksheet_id"],
                    "worksheet_title": row["worksheet_title"],
                    "order_id": row["order_id"],
                    "order_status": row["order_status"],
                    "customer_uid": row["customer_uid"],
                    "formalized_at": row["published_at"],
                    "menu_version": row["menu_version"],
                    "service_date": row["service_date"],
                    "lunch": row["lunch"],
                    "dinner": row["dinner"],
                    "source_columns": row["source_columns"],
                    "publication_print_status": row["source_columns"][13],
                    "print_status_policy": PRINT_STATUS_POLICY,
                    "receipt_version": row["receipt_version"],
                }
                for row in rows
            ],
        }

    async def reject_dispatch_method(_request: Request):
        return JSONResponse(
            {"detail": "method not allowed"},
            status_code=405,
            headers={**NO_STORE_HEADERS, "Allow": "GET"},
        )

    class _AllUnsupportedPrinterMethodsRoute(_PrivatePrinterRoute):
        """Path-scoped outer method boundary, including extension/WebDAV verbs."""

        def matches(self, scope):
            match, child_scope = super().matches(scope)
            if scope.get("method") == "GET":
                return Match.NONE, child_scope
            if match in (Match.FULL, Match.PARTIAL):
                return Match.FULL, child_scope
            return match, child_scope

        async def handle(self, scope, receive, send):
            # Bypass Route.handle's own finite methods set after our exact-path
            # matcher has accepted an arbitrary non-GET method.
            await self.app(scope, receive, send)

    # This APIRoute subclass survives FastAPI include_router. Policy does not
    # enumerate methods: every non-GET method on this exact path is promoted.
    router.routes.append(_AllUnsupportedPrinterMethodsRoute(
        path="/internal/printer/v1/dispatch-contract",
        endpoint=reject_dispatch_method,
        methods=["POST"],
        include_in_schema=False,
    ))
    return router
