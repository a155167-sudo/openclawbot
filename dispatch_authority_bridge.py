"""SQLite-only bridge from immutable v1 dispatch receipts to versioned dispatch.

No route or network surface is defined here.  The only version-construction inputs
are rows returned by the existing trusted publication parser; callers cannot supply
a payload, owner, payload hash, receipt, or dispatch-row identity.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any, Sequence

from reschedule_dispatch_versions import (
    Reservation,
    RescheduleConflict,
    create_initial_version,
    ensure_reschedule_dispatch_schema,
    exportable_versions,
    reserve_reschedule,
)
from subscription_dispatch_contract import (
    DispatchConflict,
    _trusted_publication_rows,
    _verify_dispatch_schema,
)


class DispatchAuthorityConflict(RuntimeError):
    """Persisted dispatch authority cannot prove the requested bridge operation."""


@dataclass(frozen=True)
class ImportedDispatchVersion:
    version_id: str
    order_id: int
    owner_user_id: str
    store_id: str
    menu_version: str
    payload_hash: str


@dataclass(frozen=True)
class AuthoritySnapshot:
    version_id: str
    order_id: int
    owner_user_id: str
    store_id: str
    menu_version: str
    workbook_id: str
    worksheet_id: int
    worksheet_title: str
    payload_hash: str
    root_receipt_set_hash: str
    rows: tuple[dict[str, Any], ...]


_DDL = (
    """CREATE TABLE dispatch_authority_bridge_schema_versions (
        version INTEGER PRIMARY KEY NOT NULL CHECK(version=1), applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE dispatch_authority_version_bindings (
        version_id TEXT PRIMARY KEY NOT NULL,
        order_id INTEGER NOT NULL,
        owner_user_id TEXT NOT NULL,
        store_id TEXT NOT NULL,
        menu_version TEXT NOT NULL,
        authority_payload_hash TEXT NOT NULL CHECK(length(authority_payload_hash)=64),
        receipt_set_hash TEXT NOT NULL CHECK(length(receipt_set_hash)=64),
        created_at TEXT NOT NULL,
        UNIQUE(order_id,menu_version),
        FOREIGN KEY(version_id) REFERENCES reschedule_dispatch_versions(version_id)
    )""",
    """CREATE TABLE dispatch_authority_version_rows (
        version_id TEXT NOT NULL,
        dispatch_row_id TEXT NOT NULL UNIQUE,
        receipt_id TEXT NOT NULL UNIQUE,
        source_payload_hash TEXT NOT NULL CHECK(length(source_payload_hash)=64),
        PRIMARY KEY(version_id,dispatch_row_id),
        FOREIGN KEY(version_id) REFERENCES dispatch_authority_version_bindings(version_id),
        FOREIGN KEY(dispatch_row_id) REFERENCES subscription_dispatch_rows(dispatch_row_id),
        FOREIGN KEY(receipt_id) REFERENCES subscription_dispatch_publication_receipts(receipt_id)
    )""",
    """CREATE TRIGGER dispatch_authority_bindings_no_update
        BEFORE UPDATE ON dispatch_authority_version_bindings
        BEGIN SELECT RAISE(ABORT,'dispatch authority binding is immutable'); END""",
    """CREATE TRIGGER dispatch_authority_bindings_no_delete
        BEFORE DELETE ON dispatch_authority_version_bindings
        BEGIN SELECT RAISE(ABORT,'dispatch authority binding is immutable'); END""",
    """CREATE TRIGGER dispatch_authority_rows_no_update
        BEFORE UPDATE ON dispatch_authority_version_rows
        BEGIN SELECT RAISE(ABORT,'dispatch authority row binding is immutable'); END""",
    """CREATE TRIGGER dispatch_authority_rows_no_delete
        BEFORE DELETE ON dispatch_authority_version_rows
        BEGIN SELECT RAISE(ABORT,'dispatch authority row binding is immutable'); END""",
)
_NAMES = (
    "dispatch_authority_bridge_schema_versions",
    "dispatch_authority_version_bindings",
    "dispatch_authority_version_rows",
    "dispatch_authority_bindings_no_update",
    "dispatch_authority_bindings_no_delete",
    "dispatch_authority_rows_no_update",
    "dispatch_authority_rows_no_delete",
)
_EXPECTED = dict(zip(_NAMES, _DDL))


def _fingerprint(sql: str | None) -> str:
    return "".join((sql or "").split()).lower()


def _bridge_rows(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            """SELECT name,sql FROM sqlite_master
                 WHERE name LIKE 'dispatch_authority_%'
                   AND type IN ('table','index','trigger')"""
        )
        if not str(row[0]).startswith("sqlite_autoindex")
    }


def _verify_bridge_schema(conn: sqlite3.Connection) -> None:
    installed = _bridge_rows(conn)
    if set(installed) != set(_NAMES):
        raise DispatchAuthorityConflict("dispatch authority bridge schema is partial or unknown")
    for name, expected in _EXPECTED.items():
        if _fingerprint(installed.get(name)) != _fingerprint(expected):
            raise DispatchAuthorityConflict(f"dispatch authority bridge schema mismatch: {name}")
    versions = conn.execute(
        "SELECT version,typeof(version) FROM dispatch_authority_bridge_schema_versions"
    ).fetchall()
    if [tuple(row) for row in versions] != [(1, "integer")]:
        raise DispatchAuthorityConflict("dispatch authority bridge schema version mismatch")
    if len(conn.execute("PRAGMA table_xinfo(dispatch_authority_version_bindings)").fetchall()) != 8:
        raise DispatchAuthorityConflict("dispatch authority binding columns mismatch")
    if len(conn.execute("PRAGMA table_xinfo(dispatch_authority_version_rows)").fetchall()) != 4:
        raise DispatchAuthorityConflict("dispatch authority row columns mismatch")


def ensure_dispatch_authority_bridge_schema(conn: sqlite3.Connection) -> None:
    """Install the isolated additive adapter; installation alone enables nothing."""
    try:
        _verify_dispatch_schema(conn)
    except (DispatchConflict, sqlite3.Error) as exc:
        raise DispatchAuthorityConflict("trusted subscription dispatch schema is required") from exc
    ensure_reschedule_dispatch_schema(conn)
    savepoint = "ensure_dispatch_authority_bridge_v1"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if _bridge_rows(conn):
            _verify_bridge_schema(conn)
        else:
            for statement in _DDL:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO dispatch_authority_bridge_schema_versions VALUES(1,datetime('now'))"
            )
            _verify_bridge_schema(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_value(value: object) -> str:
    return _digest(value if isinstance(value, str) else _canonical(value))


def _authority_snapshot(
    conn: sqlite3.Connection, *, order_id: int, store_id: str, menu_version: str,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise DispatchAuthorityConflict("dispatch authority order identity is invalid")
    if not store_id or not menu_version:
        raise DispatchAuthorityConflict("dispatch authority store/menu identity is incomplete")
    try:
        _verify_dispatch_schema(conn)
        records = _trusted_publication_rows(
            conn,
            """r.order_id=? AND r.store_id=? AND r.menu_version=?
                 ORDER BY r.service_date,r.dispatch_row_id""",
            (order_id, store_id, menu_version),
        )
    except (DispatchConflict, sqlite3.Error) as exc:
        raise DispatchAuthorityConflict("trusted dispatch authority cannot be parsed") from exc
    persisted_count = conn.execute(
        """SELECT count(*) FROM subscription_dispatch_rows
             WHERE order_id=? AND store_id=? AND menu_version=? AND publish_state='published'""",
        (order_id, store_id, menu_version),
    ).fetchone()[0]
    receipt_count = conn.execute(
        """SELECT count(*) FROM subscription_dispatch_publication_receipts
             WHERE order_id=? AND store_id=? AND menu_version=?""",
        (order_id, store_id, menu_version),
    ).fetchone()[0]
    if not records or len(records) != persisted_count or len(records) != receipt_count:
        raise DispatchAuthorityConflict("dispatch authority rows/receipts are missing or partial")
    owner = str(records[0]["customer_uid"])
    if any(
        int(row["order_id"]) != order_id
        or str(row["customer_uid"]) != owner
        or str(row["store_id"]) != store_id
        or str(row["menu_version"]) != menu_version
        for row in records
    ):
        raise DispatchAuthorityConflict("dispatch authority owner/store/menu binding differs")
    rows = [
        {
            "dispatch_row_id": str(row["dispatch_row_id"]),
            "receipt_id": str(row["receipt_id"]),
            "service_date": str(row["service_date"]),
            "source_payload_hash": str(row["source_payload_hash"]),
            "source_columns": row["source_columns"],
        }
        for row in records
    ]
    payload = {
        "authority": "subscription_dispatch_contract.publish_schedule/v1",
        "order_id": order_id,
        "owner_user_id": owner,
        "store_id": store_id,
        "menu_version": menu_version,
        "rows": rows,
    }
    payload_json = _canonical(payload)
    receipt_set_hash = _digest(_canonical([
        [row["receipt_id"], row["dispatch_row_id"], row["source_payload_hash"]]
        for row in rows
    ]))
    return owner, payload_json, receipt_set_hash, records


def _insert_binding(
    conn: sqlite3.Connection, *, version_id: str, order_id: int, owner: str,
    store_id: str, menu_version: str, payload_hash: str, receipt_set_hash: str,
    records: Sequence[dict[str, Any]], now: str,
) -> None:
    conn.execute(
        """INSERT INTO dispatch_authority_version_bindings
           (version_id,order_id,owner_user_id,store_id,menu_version,
            authority_payload_hash,receipt_set_hash,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (version_id, order_id, owner, store_id, menu_version,
         payload_hash, receipt_set_hash, now),
    )
    for row in records:
        conn.execute(
            """INSERT INTO dispatch_authority_version_rows
               (version_id,dispatch_row_id,receipt_id,source_payload_hash)
               VALUES(?,?,?,?)""",
            (version_id, row["dispatch_row_id"], row["receipt_id"], row["source_payload_hash"]),
        )


def import_initial_published_version(
    conn: sqlite3.Connection, *, order_id: int, store_id: str,
    menu_version: str, now: str,
) -> ImportedDispatchVersion:
    """Create the baseline version solely from persisted trusted receipt facts."""
    if conn.in_transaction:
        raise DispatchAuthorityConflict("dispatch authority import requires transaction ownership")
    ensure_dispatch_authority_bridge_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(
            "SELECT 1 FROM dispatch_authority_version_bindings WHERE order_id=?",
            (order_id,),
        ).fetchone():
            raise DispatchAuthorityConflict("dispatch authority order is already imported")
        owner, payload_json, receipt_set_hash, records = _authority_snapshot(
            conn, order_id=order_id, store_id=store_id, menu_version=menu_version,
        )
        payload = json.loads(payload_json)
        payload_hash = _digest(payload_json)
        version_id = "dispatch-authority-" + payload_hash
        create_initial_version(
            conn, version_id=version_id, order_id=order_id, owner_user_id=owner,
            payload=payload, confirmed_at=now,
        )
        _insert_binding(
            conn, version_id=version_id, order_id=order_id, owner=owner,
            store_id=store_id, menu_version=menu_version, payload_hash=payload_hash,
            receipt_set_hash=receipt_set_hash, records=records, now=now,
        )
        conn.commit()
        return ImportedDispatchVersion(
            version_id, order_id, owner, store_id, menu_version, payload_hash,
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DispatchAuthorityConflict("duplicate or conflicting dispatch authority version") from exc
    except Exception:
        conn.rollback()
        raise


def reserve_published_reschedule(
    conn: sqlite3.Connection, *, operation_id: str, request_id: str,
    old_version_id: str, order_id: int, requested_by: str, approved_by: str,
    source_date: str, target_date: str, store_id: str, menu_version: str,
    claim_token: str, now: str,
) -> Reservation:
    """Reserve a replacement whose payload is derived from trusted published rows.

    The version reservation commits first by design.  If the second exact authority
    read or binding append fails, the unbound pending operation remains a safe fence.
    """
    if conn.in_transaction:
        raise DispatchAuthorityConflict("published reschedule requires transaction ownership")
    ensure_dispatch_authority_bridge_schema(conn)
    owner, payload_json, receipt_set_hash, records = _authority_snapshot(
        conn, order_id=order_id, store_id=store_id, menu_version=menu_version,
    )
    payload = json.loads(payload_json)
    payload_hash = _digest(payload_json)
    new_version_id = "dispatch-authority-" + payload_hash
    try:
        reservation = reserve_reschedule(
            conn, operation_id=operation_id, request_id=request_id,
            old_version_id=old_version_id, new_version_id=new_version_id,
            order_id=order_id, owner_user_id=owner, requested_by=requested_by,
            approved_by=approved_by, source_date=source_date, target_date=target_date,
            new_payload=payload, claim_token=claim_token, now=now,
        )
    except RescheduleConflict as exc:
        raise DispatchAuthorityConflict(str(exc)) from exc
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh_owner, fresh_payload_json, fresh_receipt_set_hash, fresh_records = _authority_snapshot(
            conn, order_id=order_id, store_id=store_id, menu_version=menu_version,
        )
        stored = conn.execute(
            "SELECT payload_hash,owner_user_id FROM reschedule_dispatch_versions WHERE version_id=?",
            (new_version_id,),
        ).fetchone()
        if (
            fresh_owner != owner or fresh_payload_json != payload_json
            or fresh_receipt_set_hash != receipt_set_hash
            or not stored or tuple(stored) != (payload_hash, owner)
        ):
            raise DispatchAuthorityConflict("dispatch authority changed while reserving")
        _insert_binding(
            conn, version_id=new_version_id, order_id=order_id, owner=owner,
            store_id=store_id, menu_version=menu_version, payload_hash=payload_hash,
            receipt_set_hash=receipt_set_hash, records=fresh_records, now=now,
        )
        conn.commit()
        return reservation
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DispatchAuthorityConflict("duplicate or conflicting dispatch authority version") from exc
    except Exception:
        conn.rollback()
        raise


def _version_binding_is_valid(
    conn: sqlite3.Connection, *, version_id: str, order_id: int,
    owner_user_id: str, store_id: str,
) -> tuple[bool, str, list[dict[str, Any]]]:
    binding = conn.execute(
        """SELECT order_id,owner_user_id,store_id,menu_version,
                  authority_payload_hash,receipt_set_hash
             FROM dispatch_authority_version_bindings WHERE version_id=?""",
        (version_id,),
    ).fetchone()
    if not binding or tuple(binding[:3]) != (order_id, owner_user_id, store_id):
        return False, "", []
    try:
        owner, payload_json, receipt_set_hash, records = _authority_snapshot(
            conn, order_id=order_id, store_id=store_id, menu_version=str(binding[3]),
        )
    except DispatchAuthorityConflict:
        return False, "", []
    version = conn.execute(
        """SELECT owner_user_id,payload_json,payload_hash
             FROM reschedule_dispatch_versions WHERE version_id=? AND order_id=?""",
        (version_id, order_id),
    ).fetchone()
    stored_rows = conn.execute(
        """SELECT dispatch_row_id,receipt_id,source_payload_hash
             FROM dispatch_authority_version_rows WHERE version_id=? ORDER BY dispatch_row_id""",
        (version_id,),
    ).fetchall()
    expected_rows = sorted(
        (str(row["dispatch_row_id"]), str(row["receipt_id"]), str(row["source_payload_hash"]))
        for row in records
    )
    valid = bool(
        version
        and owner == owner_user_id
        and tuple(version) == (owner_user_id, payload_json, _digest(payload_json))
        and str(binding[4]) == _digest(payload_json)
        and str(binding[5]) == receipt_set_hash
        and [tuple(row) for row in stored_rows] == expected_rows
    )
    return valid, str(binding[3]), records


def validate_claim_authority_version(
    conn: sqlite3.Connection, *, version_id: str, order_id: int,
    owner_user_id: str, store_id: str,
) -> None:
    """Claim hook: when the bridge exists, unbound/synthetic authority is rejected."""
    if not _bridge_rows(conn):
        return
    _verify_bridge_schema(conn)
    valid, _menu, _records = _version_binding_is_valid(
        conn, version_id=version_id, order_id=order_id,
        owner_user_id=owner_user_id, store_id=store_id,
    )
    if not valid:
        valid = bool(_pair_snapshot_projection(
            conn, version_id=version_id, order_id=order_id,
            owner_user_id=owner_user_id, store_id=store_id,
        ))
    if not valid:
        raise DispatchAuthorityConflict("dispatch authority version binding is invalid")


def current_authority_snapshot(
    conn: sqlite3.Connection, *, version_id: str, order_id: int,
    owner_user_id: str, store_id: str, max_depth: int = 32,
) -> AuthoritySnapshot:
    """Resolve a fully verified legacy-root → immutable-snapshot authority chain."""
    if max_depth < 1:
        raise DispatchAuthorityConflict("dispatch authority chain depth is invalid")
    return _authority_chain_snapshot(
        conn, version_id=version_id, order_id=order_id,
        owner_user_id=owner_user_id, store_id=store_id,
        seen=frozenset(), remaining=max_depth,
    )


def _authority_chain_snapshot(
    conn: sqlite3.Connection, *, version_id: str, order_id: int,
    owner_user_id: str, store_id: str, seen: frozenset[str], remaining: int,
) -> AuthoritySnapshot:
    if remaining < 1 or version_id in seen:
        raise DispatchAuthorityConflict("dispatch authority chain cycle or max depth exceeded")
    valid, menu, records = _version_binding_is_valid(
        conn, version_id=version_id, order_id=order_id,
        owner_user_id=owner_user_id, store_id=store_id,
    )
    version = conn.execute(
        "SELECT payload_hash,payload_json FROM reschedule_dispatch_versions WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if valid and version:
        binding = conn.execute(
            """SELECT receipt_set_hash FROM dispatch_authority_version_bindings
                 WHERE version_id=?""", (version_id,),
        ).fetchone()
        first = records[0]
        return AuthoritySnapshot(
            version_id, order_id, owner_user_id, store_id, menu,
            str(first["workbook_id"]), int(first["worksheet_id"]),
            str(first["worksheet_title"]), str(version[0]), str(binding[0]),
            tuple(dict(row) for row in records),
        )
    projected = _pair_snapshot_projection(
        conn, version_id=version_id, order_id=order_id,
        owner_user_id=owner_user_id, store_id=store_id,
        _seen=seen, _remaining=remaining,
    )
    if not projected or not version:
        raise DispatchAuthorityConflict("dispatch authority version binding is invalid")
    payload = json.loads(str(version[1]))
    return AuthoritySnapshot(
        version_id, order_id, owner_user_id, store_id, str(payload["menu_version"]),
        str(payload["workbook_id"]), int(payload["worksheet_id"]),
        str(payload["worksheet_title"]), str(version[0]),
        str(payload["source_receipt_set_hash"]), tuple(projected),
    )


def _pair_snapshot_projection(
    conn: sqlite3.Connection, *, version_id: str, order_id: int,
    owner_user_id: str, store_id: str,
    _seen: frozenset[str] = frozenset(), _remaining: int = 32,
) -> list[dict[str, Any]]:
    """Validate and project one immutable pair snapshot; never accepts caller rows."""
    required = {
        "pair_reschedule_snapshot_receipts", "pair_reschedule_snapshot_rows",
        "pair_reschedule_view_rows",
    }
    installed = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pair_reschedule_%'"
    )}
    if not required.issubset(installed):
        return []
    version = conn.execute(
        """SELECT owner_user_id,parent_version_id,payload_json,payload_hash
             FROM reschedule_dispatch_versions WHERE version_id=? AND order_id=?""",
        (version_id, order_id),
    ).fetchone()
    receipt = conn.execute(
        """SELECT r.operation_id,r.request_id,r.order_id,r.owner_user_id,r.approved_by,
                  r.old_version_id,r.new_version_id,r.source_date,r.target_date,
                  r.authority_payload_hash,r.before_sheet_hash,r.after_sheet_hash,
                  r.snapshot_payload_hash,r.snapshot_receipt_hash,r.created_at,
                  o.status,c.payload_hash,c.confirmed_at
             FROM pair_reschedule_snapshot_receipts r
             JOIN reschedule_dispatch_operations o ON o.operation_id=r.operation_id
             JOIN reschedule_dispatch_confirmations c ON c.operation_id=r.operation_id
            WHERE r.new_version_id=?""",
        (version_id,),
    ).fetchone()
    if not version or not receipt or str(version[0]) != owner_user_id or not version[1]:
        return []
    if tuple(receipt[2:4]) != (order_id, owner_user_id) or str(receipt[6]) != version_id:
        return []
    if str(receipt[15]) != "confirmed" or str(receipt[16]) != str(version[3]):
        return []
    try:
        payload = json.loads(str(version[2]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    payload_json = _canonical(payload)
    if _digest(payload_json) != str(version[3]) or str(receipt[12]) != str(version[3]):
        return []
    if not isinstance(payload, dict) or payload.get("authority") != "pair_reschedule_snapshot/v1":
        return []
    if (
        payload.get("order_id") != order_id or payload.get("owner_user_id") != owner_user_id
        or payload.get("store_id") != store_id or payload.get("parent_version_id") != str(version[1])
        or payload.get("menu_version") is None or payload.get("workbook_id") is None
    ):
        return []

    try:
        parent = _authority_chain_snapshot(
            conn, version_id=str(version[1]), order_id=order_id,
            owner_user_id=owner_user_id, store_id=store_id,
            seen=_seen | {version_id}, remaining=_remaining - 1,
        )
    except (DispatchAuthorityConflict, TypeError, ValueError, KeyError):
        return []
    if (
        parent.payload_hash != str(receipt[9])
        or payload.get("source_authority_payload_hash") != parent.payload_hash
        or payload.get("source_receipt_set_hash") != parent.root_receipt_set_hash
    ):
        return []

    raw_rows = conn.execute(
        """SELECT service_date,row_json,row_hash FROM pair_reschedule_snapshot_rows
             WHERE operation_id=? ORDER BY service_date""", (receipt[0],),
    ).fetchall()
    view_rows = conn.execute(
        """SELECT view_name,service_date,row_json,row_hash FROM pair_reschedule_view_rows
             WHERE operation_id=? ORDER BY view_name,service_date""", (receipt[0],),
    ).fetchall()
    if len(raw_rows) < 2 or len(view_rows) != 4:
        return []
    rows: list[dict[str, Any]] = []
    payload_rows = payload.get("rows")
    if not isinstance(payload_rows, list) or len(payload_rows) != len(raw_rows):
        return []
    try:
        for raw, payload_row in zip(raw_rows, payload_rows):
            columns = json.loads(str(raw[1]))
            if (
                _digest(str(raw[1])) != str(raw[2]) or not isinstance(columns, list)
                or len(columns) != 17 or not isinstance(payload_row, dict)
                or str(raw[0]) != str(payload_row.get("service_date"))
                or columns != payload_row.get("columns")
                or columns[14] != payload_row.get("dispatch_row_id")
                or str(columns[15]) != str(order_id) or not str(columns[16])
            ):
                return []
            rows.append({
                "authority": "pair_reschedule_snapshot/v1",
                "receipt_id": str(receipt[13]), "receipt_version": 1,
                "trusted_writer": "pair_reschedule_snapshot/v1",
                "dispatch_row_id": str(columns[14]), "order_id": order_id,
                "customer_uid": owner_user_id, "store_id": store_id,
                "workbook_id": str(payload["workbook_id"]),
                "worksheet_id": int(payload["worksheet_id"]),
                "worksheet_title": str(payload["worksheet_title"]),
                "service_date": str(raw[0]), "menu_version": str(columns[16]),
                "source_payload_json": _canonical(columns[:14]),
                "source_payload_hash": _digest_value(columns[:14]),
                "published_at": str(receipt[17]), "receipt_hash": str(receipt[13]),
                "order_status": "activated", "lunch": str(columns[2]),
                "dinner": str(columns[5]), "source_columns": list(columns[:14]),
            })
        after_map = {str(raw[0]): json.loads(str(raw[1])) for raw in raw_rows}
        if _digest_value(after_map) != str(receipt[11]):
            return []
        schedule_views = [row for row in view_rows if str(row[0]) == "schedule"]
        master_views = [row for row in view_rows if str(row[0]) == "master"]
        if len(schedule_views) != 2 or len(master_views) != 2:
            return []
        for view_name, service_date, row_json, row_hash in view_rows:
            decoded = json.loads(str(row_json))
            expected_len = 17 if view_name == "schedule" else 21
            if _digest(str(row_json)) != str(row_hash) or not isinstance(decoded, list) or len(decoded) != expected_len:
                return []
            if view_name == "schedule" and decoded != after_map[str(service_date)]:
                return []
    except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError):
        return []
    receipt_body = list(receipt[:13]) + [receipt[14]]
    if _digest_value(receipt_body) != str(receipt[13]):
        return []
    return rows


def _successor_capability_active(conn: sqlite3.Connection, store_id: str) -> bool:
    printer_names = {
        str(row[0]) for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE name LIKE 'printer_dispatch_%' AND type IN ('table','index','trigger')"""
        ) if not str(row[0]).startswith("sqlite_autoindex")
    }
    if printer_names and "printer_dispatch_capability_events" not in printer_names:
        return False
    if "printer_dispatch_capability_events" not in printer_names:
        return False
    return conn.execute(
        """SELECT 1 FROM printer_dispatch_capability_events
             WHERE store_id=? AND state='v2_active' AND cache_cleared=1
               AND all_consumers_confirmed=1
             ORDER BY generation DESC LIMIT 1""",
        (store_id,),
    ).fetchone() is not None


def _filter_pair_projection_for_export(
    rows: Sequence[dict[str, Any]], *, where_sql: str, parameters: Sequence[object],
) -> list[dict[str, Any]]:
    """Apply the only export scope currently supported by the v1 parser seam.

    Snapshot rows do not exist in the legacy receipt tables, so they cannot be fed
    through ``_trusted_publication_rows``.  Keep this bridge fail-closed instead of
    pretending arbitrary caller SQL applies to Python records.
    """
    normalized = " ".join(where_sql.split())
    expected = (
        "r.store_id=? AND r.workbook_id=? AND r.service_date=? "
        "ORDER BY r.dispatch_row_id"
    )
    if normalized != expected or len(parameters) != 3:
        return []
    store_id, workbook_id, service_date = map(str, parameters)
    scoped = [
        row for row in rows
        if str(row["store_id"]) == store_id
        and str(row["workbook_id"]) == workbook_id
        and str(row["service_date"]) == service_date
        # A vacated source snapshot remains authoritative for claim binding, but
        # contains no meal and therefore is not an export/print candidate.
        and any(str(row[key] or "").strip() not in ("", "無") for key in ("lunch", "dinner"))
    ]
    return sorted(scoped, key=lambda row: str(row["dispatch_row_id"]))


def resolve_trusted_export_rows(
    conn: sqlite3.Connection, *, where_sql: str, parameters: Sequence[object],
) -> list[dict[str, Any]]:
    """Resolve v1-compatible rows through version/reschedule/claim fences.

    With no bridge facts this returns the existing parser output unchanged.  Once an
    order is imported, only its single current authority-bound version can project.
    Confirmed pair snapshots are discovered independently of legacy rows because a
    newly occupied target date intentionally has no legacy publication row.
    """
    requested = _trusted_publication_rows(conn, where_sql, parameters)
    bridge = _bridge_rows(conn)
    if not bridge:
        return requested
    _verify_bridge_schema(conn)
    if not conn.execute("SELECT 1 FROM dispatch_authority_version_bindings LIMIT 1").fetchone():
        return requested

    result: list[dict[str, Any]] = []
    by_order: dict[int, list[dict[str, Any]]] = {}
    for row in requested:
        by_order.setdefault(int(row["order_id"]), []).append(row)
    for order_id, rows in by_order.items():
        managed = conn.execute(
            "SELECT 1 FROM dispatch_authority_version_bindings WHERE order_id=? LIMIT 1",
            (order_id,),
        ).fetchone()
        has_reschedule = conn.execute(
            "SELECT 1 FROM reschedule_dispatch_operations WHERE order_id=? LIMIT 1",
            (order_id,),
        ).fetchone()
        if not managed:
            if not has_reschedule:
                result.extend(rows)
            continue
        if conn.execute(
            """SELECT 1 FROM reschedule_dispatch_operations
                 WHERE order_id=? AND status IN ('pending','sheet_unknown','manual_hold') LIMIT 1""",
            (order_id,),
        ).fetchone():
            continue
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='printer_dispatch_claims'"
        ).fetchone() and conn.execute(
            """SELECT 1 FROM printer_dispatch_claims
                 WHERE order_id=? AND (legacy_order_hold=1 OR service_date=?) LIMIT 1""",
            (order_id, str(parameters[2]) if len(parameters) == 3 else ""),
        ).fetchone():
            continue
        current = exportable_versions(conn, order_id=order_id)
        if len(current) != 1:
            continue
        version = current[0]
        valid, _menu, authoritative = _version_binding_is_valid(
            conn, version_id=str(version["version_id"]), order_id=order_id,
            owner_user_id=str(version["owner_user_id"]), store_id=str(rows[0]["store_id"]),
        )
        if not valid:
            continue
        if version["parent_version_id"] is not None and not _successor_capability_active(
            conn, str(rows[0]["store_id"])
        ):
            continue
        allowed = {str(row["dispatch_row_id"]) for row in authoritative}
        result.extend(row for row in rows if str(row["dispatch_row_id"]) in allowed)

    # Pair successors must be found from their immutable receipts, not from the old
    # publication rows: the target date is new by definition.  Every projection still
    # traverses the full snapshot/receipt/parent validation before it can be scoped.
    pair_tables = {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pair_reschedule_%'"
        )
    }
    if not {
        "pair_reschedule_snapshot_receipts", "pair_reschedule_snapshot_rows",
        "pair_reschedule_view_rows",
    }.issubset(pair_tables):
        return result
    snapshot_versions = conn.execute(
        """SELECT r.new_version_id,r.order_id,r.owner_user_id,v.payload_json
             FROM pair_reschedule_snapshot_receipts r
             JOIN reschedule_dispatch_operations o ON o.operation_id=r.operation_id
             JOIN reschedule_dispatch_versions v ON v.version_id=r.new_version_id
            WHERE o.status='confirmed' ORDER BY r.operation_id"""
    ).fetchall()
    seen = {str(row["dispatch_row_id"]) for row in result}
    for version_id, order_id, owner_user_id, payload_json in snapshot_versions:
        try:
            store_id = str(json.loads(str(payload_json))["store_id"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if not _successor_capability_active(conn, store_id):
            continue
        if conn.execute(
            """SELECT 1 FROM reschedule_dispatch_operations
                 WHERE order_id=? AND status IN ('pending','sheet_unknown','manual_hold') LIMIT 1""",
            (order_id,),
        ).fetchone():
            continue
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='printer_dispatch_claims'"
        ).fetchone() and conn.execute(
            """SELECT 1 FROM printer_dispatch_claims
                 WHERE order_id=? AND (legacy_order_hold=1 OR service_date=?) LIMIT 1""",
            (order_id, str(parameters[2]) if len(parameters) == 3 else ""),
        ).fetchone():
            continue
        current = exportable_versions(conn, order_id=int(order_id))
        if len(current) != 1 or str(current[0]["version_id"]) != str(version_id):
            continue
        projected = _pair_snapshot_projection(
            conn, version_id=str(version_id), order_id=int(order_id),
            owner_user_id=str(owner_user_id), store_id=store_id,
        )
        for row in _filter_pair_projection_for_export(
            projected, where_sql=where_sql, parameters=parameters,
        ):
            row_id = str(row["dispatch_row_id"])
            if row_id not in seen:
                result.append(row)
                seen.add(row_id)
    return result
