"""Offline coordinator for trusted two-meal reschedule snapshots.

This module deliberately defines no HTTP, LINE, Google, printer, or deployment
surface.  The caller supplies only business identity and an already verified admin
context; all schedule payload and owner identity comes from immutable dispatch facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from dispatch_authority_bridge import (
    DispatchAuthorityConflict,
    _successor_capability_active,
    current_authority_snapshot,
    ensure_dispatch_authority_bridge_schema,
)
from collections.abc import Callable

from gspread_pair_reschedule_adapter import PersistedMasterProfile
from reschedule_dispatch_versions import (
    RescheduleConflict,
    Reservation,
    confirm_sheet_readback,
    ensure_reschedule_dispatch_schema,
    mark_sheet_unknown,
)
from workbook_write_lease import (
    WorkbookLeaseConflict,
    acquire_workbook_lease,
    ensure_workbook_lease_schema,
    release_workbook_lease,
)


class PairRescheduleConflict(RuntimeError):
    """The complete trusted reschedule transition cannot be proven."""


@dataclass(frozen=True)
class VerifiedAdminContext:
    actor_id: str
    persisted_admin_id: str


_DDL = (
    """CREATE TABLE pair_reschedule_schema_versions (
        version INTEGER PRIMARY KEY NOT NULL CHECK(version=1), applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE pair_reschedule_snapshot_receipts (
        operation_id TEXT PRIMARY KEY NOT NULL,
        request_id TEXT NOT NULL UNIQUE,
        order_id INTEGER NOT NULL,
        owner_user_id TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        old_version_id TEXT NOT NULL,
        new_version_id TEXT NOT NULL UNIQUE,
        source_date TEXT NOT NULL,
        target_date TEXT NOT NULL,
        authority_payload_hash TEXT NOT NULL CHECK(length(authority_payload_hash)=64),
        before_sheet_hash TEXT NOT NULL CHECK(length(before_sheet_hash)=64),
        after_sheet_hash TEXT NOT NULL CHECK(length(after_sheet_hash)=64),
        snapshot_payload_hash TEXT NOT NULL CHECK(length(snapshot_payload_hash)=64),
        snapshot_receipt_hash TEXT NOT NULL UNIQUE CHECK(length(snapshot_receipt_hash)=64),
        created_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES reschedule_dispatch_operations(operation_id),
        FOREIGN KEY(old_version_id) REFERENCES reschedule_dispatch_versions(version_id),
        FOREIGN KEY(new_version_id) REFERENCES reschedule_dispatch_versions(version_id)
    )""",
    """CREATE TABLE pair_reschedule_snapshot_rows (
        operation_id TEXT NOT NULL,
        service_date TEXT NOT NULL,
        row_json TEXT NOT NULL,
        row_hash TEXT NOT NULL CHECK(length(row_hash)=64),
        PRIMARY KEY(operation_id,service_date),
        FOREIGN KEY(operation_id) REFERENCES pair_reschedule_snapshot_receipts(operation_id)
    )""",
    """CREATE TABLE pair_reschedule_view_rows (
        operation_id TEXT NOT NULL,
        view_name TEXT NOT NULL CHECK(view_name IN ('schedule','master')),
        service_date TEXT NOT NULL,
        row_json TEXT NOT NULL,
        row_hash TEXT NOT NULL CHECK(length(row_hash)=64),
        PRIMARY KEY(operation_id,view_name,service_date),
        FOREIGN KEY(operation_id) REFERENCES pair_reschedule_snapshot_receipts(operation_id)
    )""",
    """CREATE TRIGGER pair_reschedule_receipts_no_update
        BEFORE UPDATE ON pair_reschedule_snapshot_receipts
        BEGIN SELECT RAISE(ABORT,'pair reschedule receipt is immutable'); END""",
    """CREATE TRIGGER pair_reschedule_receipts_no_delete
        BEFORE DELETE ON pair_reschedule_snapshot_receipts
        BEGIN SELECT RAISE(ABORT,'pair reschedule receipt is immutable'); END""",
    """CREATE TRIGGER pair_reschedule_rows_no_update
        BEFORE UPDATE ON pair_reschedule_snapshot_rows
        BEGIN SELECT RAISE(ABORT,'pair reschedule row is immutable'); END""",
    """CREATE TRIGGER pair_reschedule_rows_no_delete
        BEFORE DELETE ON pair_reschedule_snapshot_rows
        BEGIN SELECT RAISE(ABORT,'pair reschedule row is immutable'); END""",
    """CREATE TRIGGER pair_reschedule_view_rows_no_update
        BEFORE UPDATE ON pair_reschedule_view_rows
        BEGIN SELECT RAISE(ABORT,'pair reschedule view row is immutable'); END""",
    """CREATE TRIGGER pair_reschedule_view_rows_no_delete
        BEFORE DELETE ON pair_reschedule_view_rows
        BEGIN SELECT RAISE(ABORT,'pair reschedule view row is immutable'); END""",
)
_NAMES = (
    "pair_reschedule_schema_versions", "pair_reschedule_snapshot_receipts",
    "pair_reschedule_snapshot_rows", "pair_reschedule_view_rows",
    "pair_reschedule_receipts_no_update",
    "pair_reschedule_receipts_no_delete", "pair_reschedule_rows_no_update",
    "pair_reschedule_rows_no_delete", "pair_reschedule_view_rows_no_update",
    "pair_reschedule_view_rows_no_delete",
)
_EXPECTED = dict(zip(_NAMES, _DDL))
_TAIPEI = ZoneInfo("Asia/Taipei")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    text = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(value: str | None) -> str:
    return "".join((value or "").split()).lower()


def _full_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PairRescheduleConflict("reschedule date must be full ISO year date") from exc
    if parsed.isoformat() != value:
        raise PairRescheduleConflict("reschedule date must be full ISO year date")
    return value


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PairRescheduleConflict("now must be timezone-aware")
    local = value.astimezone(_TAIPEI)
    if value.utcoffset() != local.utcoffset():
        raise PairRescheduleConflict("now must use Asia/Taipei offset")
    return local


def _schema_rows(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            """SELECT name,sql FROM sqlite_master
                 WHERE name LIKE 'pair_reschedule_%'
                   AND type IN ('table','index','trigger')"""
        )
        if not str(row[0]).startswith("sqlite_autoindex")
    }


def _verify_schema(conn: sqlite3.Connection) -> None:
    installed = _schema_rows(conn)
    if set(installed) != set(_NAMES):
        raise PairRescheduleConflict("pair reschedule schema is partial or unknown")
    for name, expected in _EXPECTED.items():
        if _fingerprint(installed[name]) != _fingerprint(expected):
            raise PairRescheduleConflict(f"pair reschedule schema mismatch: {name}")
    versions = conn.execute(
        "SELECT version,typeof(version) FROM pair_reschedule_schema_versions"
    ).fetchall()
    if [tuple(row) for row in versions] != [(1, "integer")]:
        raise PairRescheduleConflict("pair reschedule schema version mismatch")


def ensure_pair_reschedule_schema(conn: sqlite3.Connection) -> None:
    ensure_dispatch_authority_bridge_schema(conn)
    ensure_reschedule_dispatch_schema(conn)
    conn.execute("SAVEPOINT ensure_pair_reschedule_v1")
    try:
        if _schema_rows(conn):
            _verify_schema(conn)
        else:
            for statement in _DDL:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO pair_reschedule_schema_versions VALUES(1,datetime('now'))"
            )
            _verify_schema(conn)
        conn.execute("RELEASE SAVEPOINT ensure_pair_reschedule_v1")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ensure_pair_reschedule_v1")
        conn.execute("RELEASE SAVEPOINT ensure_pair_reschedule_v1")
        raise


def verify_admin_context(conn: sqlite3.Connection, actor_id: str) -> VerifiedAdminContext:
    rows = conn.execute(
        "SELECT value,typeof(value) FROM admin_settings WHERE key='admin_id'"
    ).fetchall()
    if len(rows) != 1 or rows[0][1] != "text" or not rows[0][0] or rows[0][0] != actor_id:
        raise PairRescheduleConflict("admin context is not persisted authority")
    return VerifiedAdminContext(str(actor_id), str(rows[0][0]))


def _validate_admin(conn: sqlite3.Connection, context: VerifiedAdminContext) -> str:
    if not isinstance(context, VerifiedAdminContext) or not context.actor_id:
        raise PairRescheduleConflict("admin context is invalid")
    row = conn.execute(
        "SELECT value,typeof(value) FROM admin_settings WHERE key='admin_id'"
    ).fetchone()
    if not row or tuple(row) != (context.actor_id, "text") or context.persisted_admin_id != context.actor_id:
        raise PairRescheduleConflict("admin context no longer matches persisted authority")
    return context.actor_id


def _validate_policy(
    conn: sqlite3.Connection, *, order_id: int, owner: str,
    source: str, target: str, now: datetime,
) -> tuple[str, str]:
    order = conn.execute(
        "SELECT user_id,status,meal_count,typeof(meal_count) FROM subscription_orders WHERE id=?",
        (order_id,),
    ).fetchone()
    if not order or order[0] != owner or order[1] != "activated" or order[3] != "integer":
        raise PairRescheduleConflict("activated order authority is invalid")
    entitlement = conn.execute(
        """SELECT remaining_meals,last_date,expiry_date,status,typeof(remaining_meals)
             FROM usage WHERE user_id=?""", (owner,),
    ).fetchone()
    if (
        not entitlement or entitlement[4] != "integer" or isinstance(entitlement[0], bool)
        or int(entitlement[0]) <= 0 or entitlement[3] != "active"
    ):
        raise PairRescheduleConflict("active entitlement with remaining meals is required")
    try:
        start = _full_date(str(entitlement[1]))
        end = _full_date(str(entitlement[2]))
    except PairRescheduleConflict as exc:
        raise PairRescheduleConflict("active entitlement date range is invalid") from exc
    if not (start <= source <= end and start <= target <= end):
        raise PairRescheduleConflict("reschedule dates are outside active entitlement")

    labels: dict[str, str] = {}
    try:
        calendar_rows = conn.execute(
            """SELECT service_date,is_service_day,schedule_label,typeof(is_service_day)
                 FROM subscription_service_calendar
                WHERE order_id=? AND service_date IN (?,?)""",
            (order_id, source, target),
        ).fetchall()
    except sqlite3.Error as exc:
        raise PairRescheduleConflict("missing service calendar configuration") from exc
    for row in calendar_rows:
        if row[3] != "integer" or row[1] != 1 or not row[2]:
            raise PairRescheduleConflict("missing service calendar configuration")
        labels[str(row[0])] = str(row[2])
    if set(labels) != {source, target}:
        raise PairRescheduleConflict("missing service calendar configuration")

    today = now.date().isoformat()
    for service_date in (source, target):
        if service_date < today:
            raise PairRescheduleConflict("past service date cannot be rescheduled")
        if service_date == today and now.timetz().replace(tzinfo=None) >= time(8, 0):
            raise PairRescheduleConflict("same-day pair reschedule cutoff has passed")
    return labels[source], labels[target]


def _persisted_target_master_profile(
    conn: sqlite3.Connection, *, order_id: int, owner: str, target_date: str,
) -> PersistedMasterProfile:
    """Derive target Master metadata only from the formalized order snapshot."""
    try:
        row = conn.execute(
            "SELECT form_payload_json,typeof(form_payload_json) FROM subscription_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        payload = json.loads(str(row[0])) if row and row[1] == "text" else None
        candidates = payload.get("master_api_rows") if isinstance(payload, dict) else None
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PairRescheduleConflict("formalized target profile snapshot is unavailable") from exc
    matches = [
        list(item) for item in (candidates or [])
        if isinstance(item, list) and len(item) == 21
        and str(item[0]).replace("/", "-") == target_date and str(item[1]) == owner
    ]
    if len(matches) != 1:
        raise PairRescheduleConflict("single formalized target profile row is required")
    item = matches[0]
    return PersistedMasterProfile(
        owner_user_id=owner, tdee=item[2], tomorrow_training=item[5],
        is_coaching_enabled=item[6], plan_type=item[7], sport_type=item[8],
        plan_week=item[9], intervals_id=item[10], intervals_api_key=item[11],
        training_freq=item[12], normal_train_time=item[13], long_train_day=item[14],
        run_pace=item[15], bike_ftp=item[16], swim_pace=item[17],
        user_level=item[18], race_date=item[19], is_carb_cycling_enabled=item[20],
    )


def _replay(conn: sqlite3.Connection, request_id: str):
    return conn.execute(
        """SELECT r.operation_id,r.order_id,r.approved_by,r.source_date,r.target_date,
                  o.status,o.old_version_id,o.new_version_id,o.expected_payload_hash
             FROM pair_reschedule_snapshot_receipts r
             JOIN reschedule_dispatch_operations o ON o.operation_id=r.operation_id
            WHERE r.request_id=?""",
        (request_id,),
    ).fetchone()


def _result(row: Sequence[object], request_id: str) -> Reservation:
    return Reservation(str(row[0]), request_id, str(row[6]), str(row[7]), str(row[5]), str(row[8]))


def _sheet_rows_for_snapshot(conn: sqlite3.Connection, operation_id: str) -> dict[str, list[Any]]:
    rows = conn.execute(
        "SELECT service_date,row_json FROM pair_reschedule_snapshot_rows WHERE operation_id=?",
        (operation_id,),
    ).fetchall()
    return {str(row[0]): list(json.loads(row[1])) for row in rows}


def _view_rows_for_snapshot(conn: sqlite3.Connection, operation_id: str) -> dict[str, dict[str, list[Any]]]:
    rows = conn.execute(
        """SELECT view_name,service_date,row_json,row_hash
             FROM pair_reschedule_view_rows WHERE operation_id=?
             ORDER BY view_name,service_date""",
        (operation_id,),
    ).fetchall()
    result: dict[str, dict[str, list[Any]]] = {"schedule": {}, "master": {}}
    for view_name, service_date, row_json, row_hash in rows:
        if view_name not in result or _digest(str(row_json)) != str(row_hash):
            raise PairRescheduleConflict("persisted pair view snapshot is invalid")
        result[str(view_name)][str(service_date)] = list(json.loads(row_json))
    if any(len(result[name]) != 2 for name in result):
        raise PairRescheduleConflict("persisted pair view snapshot is incomplete")
    return result


def _exact_view_readback(sheet: object, *, owner: str, source: str, target: str,
                         expected: Mapping[str, Mapping[str, Sequence[object]]]) -> bool:
    try:
        observed = sheet.read_pair_rows(
            owner_user_id=owner, source_date=source, target_date=target,
        )
    except Exception:
        return False
    normalized = {
        name: {str(day): list(row) for day, row in rows.items()}
        for name, rows in observed.items()
    } if isinstance(observed, Mapping) else {}
    wanted = {
        name: {str(day): list(row) for day, row in rows.items()}
        for name, rows in expected.items()
    }
    return normalized == wanted


def _confirm_from_snapshot(
    conn: sqlite3.Connection, *, operation_id: str, now: datetime,
) -> Reservation:
    row = conn.execute(
        """SELECT o.claim_token,v.payload_json
             FROM reschedule_dispatch_operations o
             JOIN reschedule_dispatch_versions v ON v.version_id=o.new_version_id
            WHERE o.operation_id=?""", (operation_id,),
    ).fetchone()
    if not row:
        raise PairRescheduleConflict("reschedule operation is missing")
    try:
        return confirm_sheet_readback(
            conn, operation_id=operation_id, claim_token=str(row[0]),
            observed_payload=json.loads(row[1]), now=now.isoformat(timespec="seconds"),
        )
    except RescheduleConflict as exc:
        raise PairRescheduleConflict(str(exc)) from exc


def _execute_pair_reschedule_under_lease(
    conn: sqlite3.Connection,
    sheet: object,
    *,
    order_id: int,
    source_date: str,
    target_date: str,
    request_id: str,
    admin_context: VerifiedAdminContext,
    now: datetime,
    policy_validator: Callable[
        [sqlite3.Connection, int, str, str, str, datetime],
        tuple[str, str],
    ] | None = None,
) -> Reservation:
    """Internal transition.  The public adapter must hold the workbook lease."""
    if conn.in_transaction:
        raise PairRescheduleConflict("coordinator requires transaction ownership")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1 or not request_id:
        raise PairRescheduleConflict("reschedule request identity is invalid")
    source, target = _full_date(source_date), _full_date(target_date)
    if source == target:
        raise PairRescheduleConflict("source and target dates must differ")
    local_now = _aware_now(now)
    admin = _validate_admin(conn, admin_context)
    ensure_pair_reschedule_schema(conn)
    conn.commit()

    prior = _replay(conn, request_id)
    if prior:
        if tuple(prior[1:5]) != (order_id, admin, source, target):
            raise PairRescheduleConflict("request replay binding differs")
        return _result(prior, request_id)

    # Read-only external observation occurs before the durable reservation.  It never
    # authorizes payload data: source content is compared to the receipt-derived row.
    try:
        observed = sheet.read_rows([source, target])
    except Exception as exc:
        print(f"⚠️ pair reschedule sheet preflight failed type={type(exc).__name__} detail={exc}")
        raise PairRescheduleConflict("Sheet preflight unavailable before reservation") from exc

    conn.execute("BEGIN IMMEDIATE")
    try:
        admin = _validate_admin(conn, admin_context)
        replay = _replay(conn, request_id)
        if replay:
            if tuple(replay[1:5]) != (order_id, admin, source, target):
                raise PairRescheduleConflict("request replay binding differs")
            conn.commit()
            return _result(replay, request_id)
        bindings = conn.execute(
            """SELECT owner_user_id,store_id
                 FROM dispatch_authority_version_bindings WHERE order_id=?""",
            (order_id,),
        ).fetchall()
        if len(bindings) != 1:
            raise PairRescheduleConflict("single imported dispatch authority is required")
        owner, store_id = map(str, bindings[0])
        current = conn.execute(
            """SELECT v.version_id FROM reschedule_dispatch_versions v
                 JOIN reschedule_dispatch_confirmations c ON c.version_id=v.version_id
                WHERE v.order_id=? AND NOT EXISTS(
                  SELECT 1 FROM reschedule_dispatch_supersessions s
                  JOIN reschedule_dispatch_operations o ON o.operation_id=s.operation_id
                  WHERE s.old_version_id=v.version_id AND o.status='confirmed')""",
            (order_id,),
        ).fetchall()
        if len(current) != 1:
            raise PairRescheduleConflict("current dispatch version is ambiguous")
        old_version_id = str(current[0][0])
        try:
            authority = current_authority_snapshot(
                conn, version_id=old_version_id, order_id=order_id,
                owner_user_id=owner, store_id=store_id,
            )
        except DispatchAuthorityConflict as exc:
            raise PairRescheduleConflict("current dispatch authority binding is invalid")
        menu_version = authority.menu_version
        authority_records = list(authority.rows)
        if not _successor_capability_active(conn, store_id):
            raise PairRescheduleConflict("claim-aware consumer capability is not active")
        if conn.execute(
            """SELECT 1 FROM printer_dispatch_claims
                 WHERE order_id=? AND (legacy_order_hold=1 OR service_date IN (?,?)) LIMIT 1""",
            (order_id, source, target),
        ).fetchone():
            raise PairRescheduleConflict("printer dispatch scope intersects reschedule")
        if policy_validator is None:
            _source_label, target_label = _validate_policy(
                conn, order_id=order_id, owner=owner, source=source, target=target, now=local_now,
            )
        else:
            _source_label, target_label = policy_validator(
                conn, order_id, owner, source, target, local_now,
            )
        target_master_profile = _persisted_target_master_profile(
            conn, order_id=order_id, owner=owner, target_date=target,
        )
        matching = [row for row in authority_records if str(row["service_date"]) == source]
        target_authority = [row for row in authority_records if str(row["service_date"]) == target]
        if len(matching) != 1 or len(target_authority) > 1 or any(
            str(row.get(key) or "").strip() not in ("", "無")
            for row in target_authority for key in ("lunch", "dinner")
        ):
            raise PairRescheduleConflict("trusted source/empty target authority is invalid")
        trusted = matching[0]
        source_observed = observed.get(source) if isinstance(observed, Mapping) else None
        target_observed = observed.get(target) if isinstance(observed, Mapping) else None
        expected_source = list(trusted["source_columns"]) + [
            str(trusted["dispatch_row_id"]), str(order_id), menu_version,
        ]
        if list(source_observed or []) != expected_source:
            raise PairRescheduleConflict("Sheet source differs from trusted publication")
        if target_observed is not None:
            if len(target_observed) != 17:
                raise PairRescheduleConflict("target row schema is incomplete")
            if any(str(value or "").strip() for value in list(target_observed)[2:12]):
                raise PairRescheduleConflict("target meal cells are not empty")
            target_base = list(target_observed)
        else:
            target_base = [target, target_label] + [""] * 11 + ["待列印"] + ["", "", ""]
        if len(target_base) != 17:
            raise PairRescheduleConflict("target row schema is incomplete")

        successor_menu = f"order-{order_id}-reschedule-{_digest(request_id)[:16]}"
        source_id = "dispatch-reschedule-" + _digest([request_id, source, "source"])[:24]
        target_id = "dispatch-reschedule-" + _digest([request_id, target, "target"])[:24]
        source_after = list(expected_source)
        source_after[2:12] = [""] * 10
        source_after[14:] = [source_id, str(order_id), successor_menu]
        target_after = list(target_base)
        target_after[0], target_after[1] = target, target_label
        target_after[2:12] = list(trusted["source_columns"])[2:12]
        target_after[12], target_after[13] = "", "待列印"
        target_after[14:] = [target_id, str(order_id), successor_menu]
        complete_rows = {
            str(row["service_date"]): list(row["source_columns"]) + [
                str(row["dispatch_row_id"]), str(order_id), str(row["menu_version"]),
            ]
            for row in authority_records
        }
        complete_rows[source] = source_after
        complete_rows[target] = target_after
        after_rows = {source: source_after, target: target_after}
        try:
            sheet_plan = sheet.plan_pair_reschedule(
                workbook_id=str(trusted["workbook_id"]),
                worksheet_id=int(trusted["worksheet_id"]),
                owner_user_id=owner,
                source_date=source,
                target_date=target,
                schedule_rows=after_rows,
                expected_schedule_before={source: source_observed, target: target_observed},
                target_master_profile=target_master_profile,
            )
            expected_views = sheet_plan.expected_readback
            if (
                not isinstance(expected_views, Mapping)
                or set(expected_views) != {"schedule", "master"}
                or set(expected_views["schedule"]) != {source, target}
                or set(expected_views["master"]) != {source, target}
                or any(len(list(expected_views["schedule"][day])) != 17 for day in (source, target))
                or any(len(list(expected_views["master"][day])) != 21 for day in (source, target))
                or {day: list(expected_views["schedule"][day]) for day in (source, target)} != after_rows
            ):
                raise PairRescheduleConflict("Sheet planner returned an incomplete two-view generation")
        except PairRescheduleConflict:
            raise
        except Exception as exc:
            raise PairRescheduleConflict("Sheet two-view planner rejected before-image") from exc

        payload = {
            "authority": "pair_reschedule_snapshot/v1",
            "order_id": order_id,
            "owner_user_id": owner,
            "store_id": store_id,
            "workbook_id": str(trusted["workbook_id"]),
            "worksheet_id": int(trusted["worksheet_id"]),
            "worksheet_title": str(trusted["worksheet_title"]),
            "menu_version": successor_menu,
            "parent_version_id": old_version_id,
            "source_authority_payload_hash": authority.payload_hash,
            "source_receipt_set_hash": authority.root_receipt_set_hash,
            "rows": [
                {"service_date": day, "columns": complete_rows[day],
                 "dispatch_row_id": complete_rows[day][14]}
                for day in sorted(complete_rows)
            ],
        }
        payload_json = _canonical(payload)
        payload_hash = _digest(payload_json)
        new_version_id = "pair-reschedule-" + payload_hash
        operation_id = "pair-operation-" + _digest([request_id, order_id, source, target])
        claim_token = _digest(["pair-claim", operation_id, payload_hash])
        created_at = local_now.isoformat(timespec="seconds")
        before_hash = _digest({source: expected_source, target: target_observed})
        after_hash = _digest(complete_rows)
        receipt_body = [operation_id, request_id, order_id, owner, admin, old_version_id,
                        new_version_id, source, target, authority.payload_hash, before_hash,
                        after_hash, payload_hash, created_at]
        snapshot_receipt_hash = _digest(receipt_body)

        if conn.execute(
            """SELECT 1 FROM reschedule_dispatch_operations
                 WHERE order_id=? AND status IN ('pending','sheet_unknown','manual_hold')""",
            (order_id,),
        ).fetchone():
            raise PairRescheduleConflict("active reschedule already fences order")
        conn.execute(
            """INSERT INTO reschedule_dispatch_versions
               (version_id,order_id,owner_user_id,parent_version_id,payload_json,payload_hash,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (new_version_id, order_id, owner, old_version_id, payload_json, payload_hash, created_at),
        )
        conn.execute(
            """INSERT INTO reschedule_dispatch_operations
               (operation_id,request_id,order_id,owner_user_id,old_version_id,new_version_id,
                requested_by,approved_by,source_date,target_date,expected_payload_hash,
                claim_token,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?)""",
            (operation_id, request_id, order_id, owner, old_version_id, new_version_id,
             owner, admin, source, target, payload_hash, claim_token, created_at, created_at),
        )
        conn.execute(
            "INSERT INTO reschedule_dispatch_supersessions VALUES(?,?,?,?)",
            (operation_id, old_version_id, new_version_id, created_at),
        )
        conn.execute(
            """INSERT INTO reschedule_dispatch_sheet_sync
               (operation_id,state,expected_payload_hash,updated_at)
               VALUES(?,'pending',?,?)""",
            (operation_id, payload_hash, created_at),
        )
        conn.execute(
            """INSERT INTO pair_reschedule_snapshot_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (operation_id, request_id, order_id, owner, admin, old_version_id, new_version_id,
             source, target, authority.payload_hash, before_hash, after_hash, payload_hash,
             snapshot_receipt_hash, created_at),
        )
        for day in sorted(complete_rows):
            row_json = _canonical(complete_rows[day])
            conn.execute(
                "INSERT INTO pair_reschedule_snapshot_rows VALUES(?,?,?,?)",
                (operation_id, day, row_json, _digest(row_json)),
            )
        for view_name in ("schedule", "master"):
            for day in (source, target):
                row_json = _canonical(list(expected_views[view_name][day]))
                conn.execute(
                    "INSERT INTO pair_reschedule_view_rows VALUES(?,?,?,?,?)",
                    (operation_id, view_name, day, row_json, _digest(row_json)),
                )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PairRescheduleConflict("reschedule reservation conflicts with durable state") from exc
    except (DispatchAuthorityConflict, RescheduleConflict) as exc:
        conn.rollback()
        raise PairRescheduleConflict(str(exc)) from exc
    except Exception:
        conn.rollback()
        raise

    try:
        sheet.apply_pair_reschedule(sheet_plan)
        observed_views = sheet.read_pair_reschedule(sheet_plan)
        normalized_views = {
            name: {str(day): list(row) for day, row in rows.items()}
            for name, rows in observed_views.items()
        } if isinstance(observed_views, Mapping) else {}
        wanted_views = {
            name: {str(day): list(row) for day, row in rows.items()}
            for name, rows in expected_views.items()
        }
        if normalized_views != wanted_views:
            raise RuntimeError("exact personal/Master Sheet readback differs")
    except Exception as exc:
        try:
            return mark_sheet_unknown(
                conn, operation_id=operation_id, claim_token=claim_token,
                reason=type(exc).__name__, now=local_now.isoformat(timespec="seconds"),
            )
        except RescheduleConflict as state_exc:
            raise PairRescheduleConflict(str(state_exc)) from state_exc
    return _confirm_from_snapshot(conn, operation_id=operation_id, now=local_now)


class _LeaseTrackedSheet:
    def __init__(self, delegate: object):
        self._delegate = delegate
        self.write_started = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def apply_pair_reschedule(self, plan: object) -> None:
        # Once this boundary is entered, even a raised exception cannot prove that
        # Google did not apply the batch.  The lease therefore remains active.
        self.write_started = True
        self._delegate.apply_pair_reschedule(plan)


def _workbook_for_order(conn: sqlite3.Connection, order_id: int) -> str:
    rows = conn.execute(
        """SELECT DISTINCT r.workbook_id
             FROM dispatch_authority_version_bindings b
             JOIN dispatch_authority_version_rows br ON br.version_id=b.version_id
             JOIN subscription_dispatch_rows r ON r.dispatch_row_id=br.dispatch_row_id
            WHERE b.order_id=?""",
        (order_id,),
    ).fetchall()
    if len(rows) != 1 or not rows[0][0]:
        raise PairRescheduleConflict("single trusted workbook authority is required")
    return str(rows[0][0])


def execute_pair_reschedule(
    conn: sqlite3.Connection,
    sheet: object,
    *,
    order_id: int,
    source_date: str,
    target_date: str,
    request_id: str,
    admin_context: VerifiedAdminContext,
    now: datetime,
    policy_validator: Callable[
        [sqlite3.Connection, int, str, str, str, datetime],
        tuple[str, str],
    ] | None = None,
) -> Reservation:
    """Run read-plan-write-readback under one durable workbook lease."""
    if conn.in_transaction:
        raise PairRescheduleConflict("coordinator requires transaction ownership")
    local_now = _aware_now(now)
    admin = _validate_admin(conn, admin_context)
    ensure_pair_reschedule_schema(conn)
    ensure_workbook_lease_schema(conn)
    conn.commit()
    prior = _replay(conn, request_id)
    if prior:
        if tuple(prior[1:5]) != (order_id, admin, source_date, target_date):
            raise PairRescheduleConflict("request replay binding differs")
        return _result(prior, request_id)

    workbook_id = _workbook_for_order(conn, order_id)
    operation_id = "pair-operation-" + _digest([request_id, order_id, source_date, target_date])
    try:
        lease = acquire_workbook_lease(
            conn, workbook_id=workbook_id, writer_id="pair_reschedule",
            operation_id=operation_id, now=local_now, ttl_seconds=120,
        )
    except WorkbookLeaseConflict as exc:
        raise PairRescheduleConflict(str(exc)) from exc

    tracked = _LeaseTrackedSheet(sheet)
    try:
        result = _execute_pair_reschedule_under_lease(
            conn, tracked, order_id=order_id, source_date=source_date,
            target_date=target_date, request_id=request_id,
            admin_context=admin_context, now=local_now,
            policy_validator=policy_validator,
        )
    except Exception:
        if not tracked.write_started:
            release_workbook_lease(
                conn, lease_token=lease.lease_token,
                final_outcome="rejected_before_write", now=local_now,
            )
        raise
    if result.status == "confirmed":
        try:
            release_workbook_lease(
                conn, lease_token=lease.lease_token,
                final_outcome="confirmed", now=local_now,
            )
        except WorkbookLeaseConflict as exc:
            raise PairRescheduleConflict("confirmed operation lease could not be released") from exc
    return result


def reconcile_pair_reschedule_readback(
    conn: sqlite3.Connection,
    sheet: object,
    *,
    order_id: int,
    request_id: str,
    admin_context: VerifiedAdminContext,
    now: datetime,
) -> Reservation:
    """Read-only recovery: exact existing rows may confirm; this never writes Sheet."""
    local_now = _aware_now(now)
    admin = _validate_admin(conn, admin_context)
    ensure_pair_reschedule_schema(conn)
    conn.commit()
    replay = _replay(conn, request_id)
    if not replay or int(replay[1]) != order_id or str(replay[2]) != admin:
        raise PairRescheduleConflict("reconcile request binding differs")
    if str(replay[5]) == "confirmed":
        return _result(replay, request_id)
    expected = _view_rows_for_snapshot(conn, str(replay[0]))
    binding = conn.execute(
        """SELECT owner_user_id,source_date,target_date
             FROM pair_reschedule_snapshot_receipts WHERE operation_id=?""",
        (replay[0],),
    ).fetchone()
    if not binding or not _exact_view_readback(
        sheet, owner=str(binding[0]), source=str(binding[1]), target=str(binding[2]),
        expected=expected,
    ):
        try:
            row = conn.execute(
                "SELECT claim_token FROM reschedule_dispatch_operations WHERE operation_id=?",
                (replay[0],),
            ).fetchone()
            return mark_sheet_unknown(
                conn, operation_id=str(replay[0]), claim_token=str(row[0]),
                reason="reconcile_readback_mismatch", now=local_now.isoformat(timespec="seconds"),
            )
        except RescheduleConflict as exc:
            raise PairRescheduleConflict(str(exc)) from exc
    result = _confirm_from_snapshot(conn, operation_id=str(replay[0]), now=local_now)
    lease = conn.execute(
        """SELECT lease_token FROM workbook_write_leases
             WHERE operation_id=? AND status='active'""",
        (str(replay[0]),),
    ).fetchone()
    if not lease:
        raise PairRescheduleConflict("confirmed reconcile is missing its active workbook lease")
    try:
        release_workbook_lease(
            conn, lease_token=str(lease[0]), final_outcome="confirmed", now=local_now,
        )
    except WorkbookLeaseConflict as exc:
        raise PairRescheduleConflict("reconciled operation lease could not be released") from exc
    return result
