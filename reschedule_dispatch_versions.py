"""Append-only dispatch versions and fail-closed reschedule export fencing.

This is an offline repository primitive.  It performs no Sheet, LINE, printer, or
network I/O and is intentionally not wired into the production export router yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import sqlite3
from typing import Any, Mapping


class RescheduleConflict(RuntimeError):
    """The requested transition cannot be proven safe."""


@dataclass(frozen=True)
class Reservation:
    operation_id: str
    request_id: str
    old_version_id: str
    new_version_id: str
    status: str
    expected_payload_hash: str


_DDL = (
    """CREATE TABLE reschedule_dispatch_schema_versions (
        version INTEGER PRIMARY KEY NOT NULL CHECK(version=1),
        applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE reschedule_dispatch_versions (
        version_id TEXT PRIMARY KEY NOT NULL,
        order_id INTEGER NOT NULL,
        owner_user_id TEXT NOT NULL,
        parent_version_id TEXT,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        created_at TEXT NOT NULL,
        FOREIGN KEY(parent_version_id) REFERENCES reschedule_dispatch_versions(version_id)
    )""",
    """CREATE TABLE reschedule_dispatch_operations (
        operation_id TEXT PRIMARY KEY NOT NULL,
        request_id TEXT NOT NULL UNIQUE,
        order_id INTEGER NOT NULL,
        owner_user_id TEXT NOT NULL,
        old_version_id TEXT NOT NULL,
        new_version_id TEXT NOT NULL UNIQUE,
        requested_by TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        source_date TEXT NOT NULL,
        target_date TEXT NOT NULL,
        expected_payload_hash TEXT NOT NULL CHECK(length(expected_payload_hash)=64),
        claim_token TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('pending','sheet_unknown','confirmed','rejected','manual_hold')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(old_version_id) REFERENCES reschedule_dispatch_versions(version_id),
        FOREIGN KEY(new_version_id) REFERENCES reschedule_dispatch_versions(version_id)
    )""",
    """CREATE TABLE reschedule_dispatch_supersessions (
        operation_id TEXT PRIMARY KEY NOT NULL,
        old_version_id TEXT NOT NULL,
        new_version_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES reschedule_dispatch_operations(operation_id),
        FOREIGN KEY(old_version_id) REFERENCES reschedule_dispatch_versions(version_id),
        FOREIGN KEY(new_version_id) REFERENCES reschedule_dispatch_versions(version_id)
    )""",
    """CREATE TABLE reschedule_dispatch_sheet_sync (
        operation_id TEXT PRIMARY KEY NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','sheet_unknown','confirmed')),
        expected_payload_hash TEXT NOT NULL CHECK(length(expected_payload_hash)=64),
        readback_payload_hash TEXT NOT NULL DEFAULT '',
        last_error_class TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES reschedule_dispatch_operations(operation_id)
    )""",
    """CREATE TABLE reschedule_dispatch_confirmations (
        version_id TEXT PRIMARY KEY NOT NULL,
        operation_id TEXT UNIQUE,
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        confirmed_at TEXT NOT NULL,
        FOREIGN KEY(version_id) REFERENCES reschedule_dispatch_versions(version_id),
        FOREIGN KEY(operation_id) REFERENCES reschedule_dispatch_operations(operation_id)
    )""",
    """CREATE UNIQUE INDEX reschedule_dispatch_one_open_order
        ON reschedule_dispatch_operations(order_id)
        WHERE status IN ('pending','sheet_unknown','manual_hold')""",
    """CREATE TRIGGER reschedule_dispatch_versions_no_update
        BEFORE UPDATE ON reschedule_dispatch_versions
        BEGIN SELECT RAISE(ABORT,'dispatch version is immutable'); END""",
    """CREATE TRIGGER reschedule_dispatch_versions_no_delete
        BEFORE DELETE ON reschedule_dispatch_versions
        BEGIN SELECT RAISE(ABORT,'dispatch version is immutable'); END""",
    """CREATE TRIGGER reschedule_dispatch_supersessions_no_update
        BEFORE UPDATE ON reschedule_dispatch_supersessions
        BEGIN SELECT RAISE(ABORT,'dispatch supersession is immutable'); END""",
    """CREATE TRIGGER reschedule_dispatch_supersessions_no_delete
        BEFORE DELETE ON reschedule_dispatch_supersessions
        BEGIN SELECT RAISE(ABORT,'dispatch supersession is immutable'); END""",
    """CREATE TRIGGER reschedule_dispatch_confirmations_no_update
        BEFORE UPDATE ON reschedule_dispatch_confirmations
        BEGIN SELECT RAISE(ABORT,'dispatch confirmation is immutable'); END""",
    """CREATE TRIGGER reschedule_dispatch_confirmations_no_delete
        BEFORE DELETE ON reschedule_dispatch_confirmations
        BEGIN SELECT RAISE(ABORT,'dispatch confirmation is immutable'); END""",
)

_OWNED_NAMES = {
    "reschedule_dispatch_schema_versions",
    "reschedule_dispatch_versions",
    "reschedule_dispatch_operations",
    "reschedule_dispatch_supersessions",
    "reschedule_dispatch_sheet_sync",
    "reschedule_dispatch_confirmations",
    "reschedule_dispatch_one_open_order",
    "reschedule_dispatch_versions_no_update",
    "reschedule_dispatch_versions_no_delete",
    "reschedule_dispatch_supersessions_no_update",
    "reschedule_dispatch_supersessions_no_delete",
    "reschedule_dispatch_confirmations_no_update",
    "reschedule_dispatch_confirmations_no_delete",
}

_EXPECTED_SQL = dict(zip(
    (
        "reschedule_dispatch_schema_versions",
        "reschedule_dispatch_versions",
        "reschedule_dispatch_operations",
        "reschedule_dispatch_supersessions",
        "reschedule_dispatch_sheet_sync",
        "reschedule_dispatch_confirmations",
        "reschedule_dispatch_one_open_order",
        "reschedule_dispatch_versions_no_update",
        "reschedule_dispatch_versions_no_delete",
        "reschedule_dispatch_supersessions_no_update",
        "reschedule_dispatch_supersessions_no_delete",
        "reschedule_dispatch_confirmations_no_update",
        "reschedule_dispatch_confirmations_no_delete",
    ),
    _DDL,
))


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        raise RescheduleConflict("dispatch payload must be an object")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RescheduleConflict("dispatch payload is not canonical JSON") from exc
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RescheduleConflict("timestamp must be aware ISO") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat(timespec="seconds") != value:
        raise RescheduleConflict("timestamp must be canonical aware ISO seconds")
    return value


def _full_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RescheduleConflict("reschedule date must be full ISO year date") from exc
    if parsed.isoformat() != value:
        raise RescheduleConflict("reschedule date must be full ISO year date")
    return value


def _schema_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE name LIKE 'reschedule_dispatch_%'
                   AND type IN ('table','index','trigger')"""
        )
        if not str(row[0]).startswith("sqlite_autoindex")
    }


def _sql_fingerprint(value: str | None) -> str:
    return "".join((value or "").split()).lower()


def _verify_schema(conn: sqlite3.Connection) -> None:
    if _schema_names(conn) != _OWNED_NAMES:
        raise RescheduleConflict("reschedule dispatch schema is partial or unknown")
    installed = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            """SELECT name,sql FROM sqlite_master
                 WHERE name LIKE 'reschedule_dispatch_%'
                   AND type IN ('table','index','trigger')"""
        )
    }
    for name, expected_sql in _EXPECTED_SQL.items():
        if _sql_fingerprint(installed.get(name)) != _sql_fingerprint(expected_sql):
            raise RescheduleConflict(f"reschedule dispatch schema mismatch: {name}")
    version = conn.execute(
        "SELECT version,typeof(version) FROM reschedule_dispatch_schema_versions"
    ).fetchall()
    if [tuple(row) for row in version] != [(1, "integer")]:
        raise RescheduleConflict("reschedule dispatch schema version mismatch")
    expected_columns = {
        "reschedule_dispatch_versions": 7,
        "reschedule_dispatch_operations": 15,
        "reschedule_dispatch_supersessions": 4,
        "reschedule_dispatch_sheet_sync": 6,
        "reschedule_dispatch_confirmations": 4,
    }
    for table, count in expected_columns.items():
        if len(conn.execute(f"PRAGMA table_xinfo({table})").fetchall()) != count:
            raise RescheduleConflict(f"reschedule dispatch schema mismatch: {table}")


def ensure_reschedule_dispatch_schema(conn: sqlite3.Connection) -> None:
    """Install the additive schema under a savepoint, preserving caller ownership."""
    savepoint = "ensure_reschedule_dispatch_v1"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        names = _schema_names(conn)
        if names:
            _verify_schema(conn)
        else:
            for statement in _DDL:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO reschedule_dispatch_schema_versions VALUES(1,?)",
                (datetime.now().astimezone().isoformat(timespec="seconds"),),
            )
            _verify_schema(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def create_initial_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    order_id: int,
    owner_user_id: str,
    payload: Mapping[str, Any],
    confirmed_at: str,
) -> None:
    """Import one already-confirmed baseline version without touching legacy receipts."""
    payload_json, payload_hash = _canonical_payload(payload)
    when = _timestamp(confirmed_at)
    if not version_id or isinstance(order_id, bool) or order_id < 1 or not owner_user_id:
        raise RescheduleConflict("initial dispatch identity is incomplete")
    conn.execute(
        """INSERT INTO reschedule_dispatch_versions
           (version_id,order_id,owner_user_id,parent_version_id,payload_json,payload_hash,created_at)
           VALUES(?,?,?,NULL,?,?,?)""",
        (version_id, order_id, owner_user_id, payload_json, payload_hash, when),
    )
    conn.execute(
        """INSERT INTO reschedule_dispatch_confirmations
           (version_id,operation_id,payload_hash,confirmed_at) VALUES(?,NULL,?,?)""",
        (version_id, payload_hash, when),
    )


def _reservation(row: sqlite3.Row | tuple) -> Reservation:
    return Reservation(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]))


def reserve_reschedule(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    request_id: str,
    old_version_id: str,
    new_version_id: str,
    order_id: int,
    owner_user_id: str,
    requested_by: str,
    approved_by: str,
    source_date: str,
    target_date: str,
    new_payload: Mapping[str, Any],
    claim_token: str,
    now: str,
) -> Reservation:
    """Atomically append a candidate and block both old and new from export."""
    if conn.in_transaction:
        raise RescheduleConflict("reservation requires transaction ownership")
    when = _timestamp(now)
    source = _full_date(source_date)
    target = _full_date(target_date)
    payload_json, payload_hash = _canonical_payload(new_payload)
    if source == target:
        raise RescheduleConflict("source and target dates must differ")
    if not all((operation_id, request_id, old_version_id, new_version_id, owner_user_id, claim_token)):
        raise RescheduleConflict("reschedule identity is incomplete")
    if requested_by != owner_user_id or not approved_by:
        raise RescheduleConflict("customer request or admin approval binding is invalid")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise RescheduleConflict("order identity is invalid")

    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = conn.execute(
            """SELECT operation_id,request_id,old_version_id,new_version_id,status,
                      expected_payload_hash,order_id,owner_user_id,requested_by,approved_by,
                      source_date,target_date,claim_token
                 FROM reschedule_dispatch_operations WHERE request_id=?""",
            (request_id,),
        ).fetchone()
        if replay:
            expected = (
                operation_id, request_id, old_version_id, new_version_id,
                order_id, owner_user_id, requested_by, approved_by,
                source, target, claim_token, payload_hash,
            )
            actual = (
                replay[0], replay[1], replay[2], replay[3], replay[6], replay[7],
                replay[8], replay[9], replay[10], replay[11], replay[12], replay[5],
            )
            if actual != expected:
                raise RescheduleConflict("request replay binding differs")
            result = _reservation(replay[:6])
            conn.commit()
            return result

        active = conn.execute(
            """SELECT 1 FROM reschedule_dispatch_operations
                 WHERE order_id=? AND status IN ('pending','sheet_unknown','manual_hold')""",
            (order_id,),
        ).fetchone()
        if active:
            raise RescheduleConflict("active reschedule already fences order")
        # v1 predecessor claims without scope are explicit order-wide manual holds.
        # v2 claims intersect only the two dates affected by this transition.
        printer_schema = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='printer_dispatch_claims'"
        ).fetchone()
        if printer_schema and conn.execute(
            """SELECT 1 FROM printer_dispatch_claims
                 WHERE order_id=? AND (
                    legacy_order_hold=1 OR service_date IN (?,?)
                 ) LIMIT 1""",
            (order_id, source, target),
        ).fetchone():
            raise RescheduleConflict("printer dispatch scope intersects reschedule")
        old = conn.execute(
            """SELECT v.order_id,v.owner_user_id
                 FROM reschedule_dispatch_versions v
                 JOIN reschedule_dispatch_confirmations c ON c.version_id=v.version_id
                WHERE v.version_id=?""",
            (old_version_id,),
        ).fetchone()
        if not old or tuple(old) != (order_id, owner_user_id):
            raise RescheduleConflict("old confirmed version binding differs")
        already_superseded = conn.execute(
            """SELECT 1 FROM reschedule_dispatch_supersessions s
                 JOIN reschedule_dispatch_operations o ON o.operation_id=s.operation_id
                WHERE s.old_version_id=? AND o.status='confirmed'""",
            (old_version_id,),
        ).fetchone()
        if already_superseded:
            raise RescheduleConflict("old version is no longer current")

        conn.execute(
            """INSERT INTO reschedule_dispatch_versions
               (version_id,order_id,owner_user_id,parent_version_id,payload_json,payload_hash,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (new_version_id, order_id, owner_user_id, old_version_id, payload_json, payload_hash, when),
        )
        conn.execute(
            """INSERT INTO reschedule_dispatch_operations
               (operation_id,request_id,order_id,owner_user_id,old_version_id,new_version_id,
                requested_by,approved_by,source_date,target_date,expected_payload_hash,
                claim_token,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (operation_id, request_id, order_id, owner_user_id, old_version_id,
             new_version_id, requested_by, approved_by, source, target, payload_hash,
             claim_token, when, when),
        )
        conn.execute(
            "INSERT INTO reschedule_dispatch_supersessions VALUES(?,?,?,?)",
            (operation_id, old_version_id, new_version_id, when),
        )
        conn.execute(
            """INSERT INTO reschedule_dispatch_sheet_sync
               (operation_id,state,expected_payload_hash,updated_at)
               VALUES(?,'pending',?,?)""",
            (operation_id, payload_hash, when),
        )
        conn.commit()
        return Reservation(operation_id, request_id, old_version_id, new_version_id, "pending", payload_hash)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise RescheduleConflict("reschedule reservation conflicts with durable state") from exc
    except Exception:
        conn.rollback()
        raise


def mark_sheet_unknown(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    claim_token: str,
    reason: str,
    now: str,
    caller_owned: bool = False,
) -> Reservation:
    when = _timestamp(now)
    if not reason:
        raise RescheduleConflict("sheet unknown reason is required")
    if caller_owned:
        if not conn.in_transaction:
            raise RescheduleConflict("caller-owned sheet transition requires an active transaction")
    else:
        if conn.in_transaction:
            raise RescheduleConflict("sheet transition requires transaction ownership")
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT operation_id,request_id,old_version_id,new_version_id,status,
                      expected_payload_hash,claim_token
                 FROM reschedule_dispatch_operations WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        if not row or row[6] != claim_token or row[4] not in ("pending", "sheet_unknown"):
            raise RescheduleConflict("sheet unknown claim or state differs")
        operation_changed = conn.execute(
            """UPDATE reschedule_dispatch_operations SET status='sheet_unknown',updated_at=?
                WHERE operation_id=? AND status IN ('pending','sheet_unknown') AND claim_token=?""",
            (when, operation_id, claim_token),
        ).rowcount
        sync_changed = conn.execute(
            """UPDATE reschedule_dispatch_sheet_sync
                  SET state='sheet_unknown',last_error_class=?,updated_at=?
                WHERE operation_id=? AND state IN ('pending','sheet_unknown')""",
            (reason[:80], when, operation_id),
        ).rowcount
        if operation_changed != 1 or sync_changed != 1:
            raise RescheduleConflict("sheet unknown operation or sync CAS differs")
        if not caller_owned:
            conn.commit()
        return Reservation(str(row[0]), str(row[1]), str(row[2]), str(row[3]), "sheet_unknown", str(row[5]))
    except Exception:
        if not caller_owned:
            conn.rollback()
        raise


def confirm_sheet_readback(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    claim_token: str,
    observed_payload: Mapping[str, Any],
    now: str,
    caller_owned: bool = False,
) -> Reservation:
    """Confirm only an exact readback; immutable supersession then becomes effective."""
    when = _timestamp(now)
    _payload_json, observed_hash = _canonical_payload(observed_payload)
    if caller_owned:
        if not conn.in_transaction:
            raise RescheduleConflict("caller-owned confirmation requires an active transaction")
    else:
        if conn.in_transaction:
            raise RescheduleConflict("sheet confirmation requires transaction ownership")
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT operation_id,request_id,old_version_id,new_version_id,status,
                      expected_payload_hash,claim_token
                 FROM reschedule_dispatch_operations WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        if not row or row[6] != claim_token:
            raise RescheduleConflict("sheet confirmation claim differs")
        if row[4] == "confirmed":
            confirmation = conn.execute(
                "SELECT payload_hash FROM reschedule_dispatch_confirmations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not confirmation or confirmation[0] != observed_hash:
                raise RescheduleConflict("confirmed replay readback differs")
            if not caller_owned:
                conn.commit()
            return Reservation(str(row[0]), str(row[1]), str(row[2]), str(row[3]), "confirmed", str(row[5]))
        if row[4] not in ("pending", "sheet_unknown") or row[5] != observed_hash:
            raise RescheduleConflict("sheet readback hash or state differs")
        conn.execute(
            """INSERT INTO reschedule_dispatch_confirmations
               (version_id,operation_id,payload_hash,confirmed_at) VALUES(?,?,?,?)""",
            (row[3], operation_id, observed_hash, when),
        )
        sync = conn.execute(
            """UPDATE reschedule_dispatch_sheet_sync
                  SET state='confirmed',readback_payload_hash=?,last_error_class='',updated_at=?
                WHERE operation_id=? AND state IN ('pending','sheet_unknown')
                  AND expected_payload_hash=?""",
            (observed_hash, when, operation_id, observed_hash),
        )
        changed = conn.execute(
            """UPDATE reschedule_dispatch_operations SET status='confirmed',updated_at=?
                WHERE operation_id=? AND status IN ('pending','sheet_unknown')
                  AND claim_token=? AND expected_payload_hash=?""",
            (when, operation_id, claim_token, observed_hash),
        )
        if sync.rowcount != 1 or changed.rowcount != 1:
            raise RescheduleConflict("sheet confirmation CAS lost")
        if not caller_owned:
            conn.commit()
        return Reservation(str(row[0]), str(row[1]), str(row[2]), str(row[3]), "confirmed", str(row[5]))
    except sqlite3.IntegrityError as exc:
        if not caller_owned:
            conn.rollback()
        raise RescheduleConflict("sheet confirmation conflicts with durable state") from exc
    except Exception:
        if not caller_owned:
            conn.rollback()
        raise


def exportable_versions(conn: sqlite3.Connection, *, order_id: int) -> list[sqlite3.Row]:
    """Return confirmed current versions unless any operation fences the order."""
    if conn.execute(
        """SELECT 1 FROM reschedule_dispatch_operations
             WHERE order_id=? AND status IN ('pending','sheet_unknown','manual_hold') LIMIT 1""",
        (order_id,),
    ).fetchone():
        return []
    return list(conn.execute(
        """SELECT v.version_id,v.order_id,v.owner_user_id,v.parent_version_id,
                  v.payload_json,v.payload_hash,c.confirmed_at
             FROM reschedule_dispatch_versions v
             JOIN reschedule_dispatch_confirmations c ON c.version_id=v.version_id
            WHERE v.order_id=?
              AND NOT EXISTS (
                  SELECT 1 FROM reschedule_dispatch_supersessions s
                  JOIN reschedule_dispatch_operations o ON o.operation_id=s.operation_id
                   WHERE s.old_version_id=v.version_id AND o.status='confirmed'
              )
            ORDER BY v.created_at,v.version_id""",
        (order_id,),
    ))
