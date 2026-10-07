"""Offline coordinator for trusted two-meal reschedule snapshots.

This module deliberately defines no HTTP, LINE, Google, printer, or deployment
surface.  The caller supplies only business identity and an already verified admin
context; all schedule payload and owner identity comes from immutable dispatch facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import base64
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

from gspread_pair_reschedule_adapter import PersistedMasterProfile, pair_transport_views_equal
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
    require_full_writer_inventory,
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
    source_date: str | None = None, current_version_id: str | None = None,
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
    if len(matches) == 1:
        item = matches[0]
    elif len(matches) == 0 and source_date and current_version_id:
        # An empty target day has no Master row. For a fixed plan only, carry
        # the current source profile. A moved source comes from a confirmed
        # immutable two-view snapshot; the initial source comes from the
        # formalized order payload. Dynamic plans require target-day facts.
        moved = conn.execute('''SELECT v.row_json,v.row_hash FROM pair_reschedule_view_rows v
            JOIN pair_reschedule_snapshot_receipts r ON r.operation_id=v.operation_id
            JOIN reschedule_dispatch_operations o ON o.operation_id=r.operation_id
            WHERE r.new_version_id=? AND o.status='confirmed'
              AND v.view_name='master' AND v.service_date=?''',
            (current_version_id, source_date)).fetchall()
        if len(moved) == 1:
            if _digest(str(moved[0][0])) != str(moved[0][1]):
                raise PairRescheduleConflict('confirmed source Master snapshot hash differs')
            item = json.loads(str(moved[0][0]))
        else:
            sources = [list(row) for row in (candidates or [])
                       if isinstance(row, list) and len(row) == 21
                       and str(row[0]).replace('/', '-') == source_date and str(row[1]) == owner]
            if len(sources) != 1:
                raise PairRescheduleConflict('single formalized source profile row is required')
            item = sources[0]
        if len(item) != 21 or str(item[1]) != owner or str(item[6]).strip() != '0' or str(item[20]).strip() != '0':
            raise PairRescheduleConflict('dynamic target profile cannot be inferred')
        item = [target_date, *item[1:]]
    else:
        raise PairRescheduleConflict("single formalized target profile row is required")
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
    return pair_transport_views_equal(expected, observed, owner=owner, target=target)


def _confirm_from_snapshot(
    conn: sqlite3.Connection, *, operation_id: str, now: datetime,
    caller_owned: bool = False,
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
            caller_owned=caller_owned,
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
            source_date=source, current_version_id=old_version_id,
        )
        matching = [row for row in authority_records if str(row["service_date"]) == source]
        target_authority = [row for row in authority_records if str(row["service_date"]) == target]
        if len(matching) != 1 or len(target_authority) > 1 or any(
            str(row.get(key) or "").strip() not in ("", "無")
            for row in target_authority for key in ("lunch", "dinner")
        ):
            raise PairRescheduleConflict("trusted source/empty target authority is invalid")
        trusted = matching[0]
        if any(str(trusted.get(key) or '').strip() in ('', '無') for key in ('lunch', 'dinner')):
            raise PairRescheduleConflict('trusted source must contain confirmed lunch and dinner')
        source_observed = observed.get(source) if isinstance(observed, Mapping) else None
        target_observed = observed.get(target) if isinstance(observed, Mapping) else None
        expected_source = list(trusted["source_columns"]) + [
            str(trusted["dispatch_row_id"]), str(order_id), str(trusted["menu_version"]),
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
        if not pair_transport_views_equal(
            expected_views, observed_views, owner=owner, target=target,
        ):
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


def _reconcile_binding(conn: sqlite3.Connection, *, replay: Sequence[object],
                       request_id: str, order_id: int, admin: str) -> tuple[str, str]:
    """Validate the durable operation and return its existing lease token and status."""
    operation_id = str(replay[0])
    receipt = conn.execute(
        "SELECT * FROM pair_reschedule_snapshot_receipts WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    operation = conn.execute(
        "SELECT * FROM reschedule_dispatch_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if not receipt or not operation:
        raise PairRescheduleConflict("reconcile receipt or operation is missing")
    source, target = str(receipt["source_date"]), str(receipt["target_date"])
    owner = str(receipt["owner_user_id"])
    version = conn.execute(
        "SELECT * FROM reschedule_dispatch_versions WHERE version_id=?",
        (receipt["new_version_id"],),
    ).fetchone()
    sync = conn.execute(
        "SELECT * FROM reschedule_dispatch_sheet_sync WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    confirmation = conn.execute(
        "SELECT version_id,operation_id,payload_hash FROM reschedule_dispatch_confirmations "
        "WHERE version_id=? OR operation_id=?",
        (receipt["new_version_id"], operation_id),
    ).fetchall()
    order = conn.execute("SELECT user_id FROM subscription_orders WHERE id=?", (order_id,)).fetchone()
    receipt_body = [receipt[key] for key in (
        "operation_id", "request_id", "order_id", "owner_user_id", "approved_by",
        "old_version_id", "new_version_id", "source_date", "target_date",
        "authority_payload_hash", "before_sheet_hash", "after_sheet_hash",
        "snapshot_payload_hash", "created_at",
    )]
    if (
        receipt["request_id"] != request_id or receipt["order_id"] != order_id
        or receipt["approved_by"] != admin or not order or order[0] != owner
        or operation_id != "pair-operation-" + _digest([request_id, order_id, source, target])
        or receipt["snapshot_receipt_hash"] != _digest(receipt_body)
        or not version or not sync
        or any(operation[key] != receipt[key] for key in (
            "operation_id", "request_id", "order_id", "owner_user_id",
            "old_version_id", "new_version_id", "approved_by", "source_date", "target_date",
        ))
        or operation["requested_by"] != owner
        or version["order_id"] != order_id or version["owner_user_id"] != owner
        or version["parent_version_id"] != receipt["old_version_id"]
        or version["payload_hash"] != _digest(str(version["payload_json"]))
        or version["payload_hash"] != receipt["snapshot_payload_hash"]
        or operation["expected_payload_hash"] != version["payload_hash"]
        or sync["expected_payload_hash"] != version["payload_hash"]
        or operation["claim_token"] != _digest(["pair-claim", operation_id, version["payload_hash"]])
    ):
        raise PairRescheduleConflict("reconcile immutable request or payload binding differs")
    try:
        payload = json.loads(str(version["payload_json"]))
    except (TypeError, ValueError):
        raise PairRescheduleConflict("reconcile payload is invalid") from None
    workbook_id = _workbook_for_order(conn, order_id)
    if (
        not isinstance(payload, dict) or payload.get("authority") != "pair_reschedule_snapshot/v1"
        or payload.get("workbook_id") != workbook_id
        or payload.get("order_id") != order_id or payload.get("owner_user_id") != owner
        or payload.get("parent_version_id") != receipt["old_version_id"]
        or payload.get("source_authority_payload_hash") != receipt["authority_payload_hash"]
    ):
        raise PairRescheduleConflict("reconcile workbook or payload binding differs")
    snapshot_rows = conn.execute(
        "SELECT service_date,row_json,row_hash FROM pair_reschedule_snapshot_rows WHERE operation_id=?",
        (operation_id,),
    ).fetchall()
    try:
        rows = {str(row[0]): json.loads(str(row[1])) for row in snapshot_rows}
        payload_rows = {str(row["service_date"]): row["columns"] for row in payload["rows"]}
    except (TypeError, ValueError, KeyError):
        raise PairRescheduleConflict("reconcile snapshot rows are invalid") from None
    if (
        len(rows) != len(snapshot_rows) or set(rows) != set(payload_rows)
        or not {source, target}.issubset(rows)
        or any(_digest(str(row[1])) != row[2] for row in snapshot_rows)
        or rows != payload_rows or receipt["after_sheet_hash"] != _digest(rows)
    ):
        raise PairRescheduleConflict("reconcile snapshot row hash differs")
    status = str(operation["status"])
    if status == "confirmed":
        if (
            len(confirmation) != 1
            or tuple(confirmation[0]) != (receipt["new_version_id"], operation_id, version["payload_hash"])
            or sync["state"] != "confirmed"
            or sync["readback_payload_hash"] != version["payload_hash"]
        ):
            raise PairRescheduleConflict("confirmed reconcile confirmation or sync differs")
    elif status in ("pending", "sheet_unknown"):
        if confirmation or sync["state"] != status or sync["readback_payload_hash"]:
            raise PairRescheduleConflict("unconfirmed reconcile confirmation or sync differs")
    else:
        raise PairRescheduleConflict("reconcile operation state is not recoverable")
    lease = conn.execute(
        "SELECT * FROM workbook_write_leases WHERE workbook_id=?", (workbook_id,),
    ).fetchone()
    token = str(lease["lease_token"]) if lease else ""
    try:
        token_is_valid = (
            len(token) == 43
            and base64.urlsafe_b64encode(base64.urlsafe_b64decode(token + "=")).decode().rstrip("=") == token
        )
    except (ValueError, base64.binascii.Error):
        token_is_valid = False
    if (
        not lease or lease["writer_id"] != "pair_reschedule"
        or lease["operation_id"] != operation_id or not token_is_valid
        or (lease["status"] == "released" and
            (status != "confirmed" or lease["final_outcome"] != "confirmed" or not lease["released_at"]))
        or (lease["status"] == "active" and
            (lease["final_outcome"] is not None or lease["released_at"] is not None))
        or lease["status"] not in ("active", "released")
    ):
        raise PairRescheduleConflict("reconcile workbook lease binding differs")
    return token, str(lease["status"])


def _reconcile_request_binding(
    conn: sqlite3.Connection, *, operation_id: str, request_id: str,
    order_id: int, admin: str, required: bool,
) -> tuple[object, ...] | None:
    """Validate and capture the real customer request without creating its schema."""
    from reschedule_service_integration import _has_valid_request_schema

    if not _has_valid_request_schema(conn):
        if required:
            raise PairRescheduleConflict("registered customer request schema is missing")
        return None
    row = conn.execute(
        "SELECT * FROM customer_pair_reschedule_requests WHERE request_id=?", (request_id,),
    ).fetchone()
    if row is None:
        if required:
            raise PairRescheduleConflict("registered customer request is missing")
        return None
    operation = conn.execute(
        "SELECT * FROM reschedule_dispatch_operations WHERE operation_id=?", (operation_id,),
    ).fetchone()
    if (
        not operation or len(row) not in (9, 12)
        or row[0] != request_id or row[1] != order_id
        or row[2] != operation["owner_user_id"]
        or row[3] != operation["source_date"] or row[4] != operation["target_date"]
        or row[6] != operation_id
        or operation["request_id"] != request_id
        or operation["approved_by"] != admin
        or operation["order_id"] != order_id
        or operation["status"] not in ("pending", "sheet_unknown", "confirmed")
        or (operation["status"] == "confirmed" and row[5] not in ("sheet_unknown", "confirmed"))
        or (operation["status"] != "confirmed" and row[5] != "sheet_unknown")
    ):
        raise PairRescheduleConflict("customer request and operation binding differs")
    return tuple(row)


def _reconcile_frozen_state(conn: sqlite3.Connection, *, operation_id: str,
                            request_id: str, order_id: int, admin: str,
                            require_customer_request: bool) -> tuple[object, ...]:
    """Capture all mutable recovery bindings before external I/O for exact CAS."""
    order = conn.execute("SELECT * FROM subscription_orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "activated":
        raise PairRescheduleConflict("reconcile order is not active")
    owner = str(order["user_id"])
    usage = conn.execute("SELECT * FROM usage WHERE user_id=?", (owner,)).fetchone()
    # Normal orders use their immutable anchor and order-owned menu entitlement;
    # usage.status is a separate nutrition tier (e.g. 'vip'), not that authority.
    anchor = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='normal_reschedule_expiry_anchors'").fetchone():
        anchor = conn.execute("SELECT * FROM normal_reschedule_expiry_anchors WHERE order_id=?", (order_id,)).fetchone()
    entitlement = None
    if anchor is not None:
        if str(anchor["owner_user_id"]) != owner:
            raise PairRescheduleConflict("normal recovery anchor owner differs")
        entitlements = conn.execute("SELECT * FROM subscription_menu_entitlements WHERE order_id=?", (order_id,)).fetchall()
        if len(entitlements) != 1 or str(entitlements[0]["user_id"]) != owner or entitlements[0]["status"] != "active":
            raise PairRescheduleConflict("normal recovery entitlement is not active")
        entitlement = entitlements[0]
    if not usage or (anchor is None and usage["status"] != "active"):
        raise PairRescheduleConflict("reconcile owner is not active")
    workbook_id = _workbook_for_order(conn, order_id)
    try:
        require_full_writer_inventory(
            conn, workbook_id=workbook_id, writer_id="pair_reschedule",
        )
    except WorkbookLeaseConflict as exc:
        raise PairRescheduleConflict(str(exc)) from exc
    capability = conn.execute(
        "SELECT * FROM workbook_writer_capabilities WHERE workbook_id=?", (workbook_id,),
    ).fetchone()
    request = _reconcile_request_binding(
        conn, operation_id=operation_id, request_id=request_id,
        order_id=order_id, admin=admin, required=require_customer_request,
    )

    def one(sql: str, args: tuple[object, ...]) -> tuple[object, ...] | None:
        row = conn.execute(sql, args).fetchone()
        return tuple(row) if row is not None else None

    def many(sql: str, args: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        return tuple(tuple(row) for row in conn.execute(sql, args).fetchall())

    return (
        tuple(order), tuple(usage), workbook_id, tuple(capability), request,
        tuple(anchor) if anchor is not None else None,
        tuple(entitlement) if entitlement is not None else None,
        one("SELECT * FROM pair_reschedule_snapshot_receipts WHERE operation_id=?", (operation_id,)),
        one("SELECT * FROM reschedule_dispatch_operations WHERE operation_id=?", (operation_id,)),
        one("SELECT * FROM reschedule_dispatch_sheet_sync WHERE operation_id=?", (operation_id,)),
        one("SELECT * FROM workbook_write_leases WHERE workbook_id=?", (workbook_id,)),
        one("SELECT * FROM reschedule_dispatch_versions WHERE version_id="
            "(SELECT new_version_id FROM reschedule_dispatch_operations WHERE operation_id=?)", (operation_id,)),
        many("SELECT * FROM reschedule_dispatch_confirmations WHERE operation_id=? ORDER BY version_id", (operation_id,)),
        many("SELECT * FROM pair_reschedule_snapshot_rows WHERE operation_id=? ORDER BY service_date", (operation_id,)),
        many("SELECT * FROM pair_reschedule_view_rows WHERE operation_id=? ORDER BY view_name,service_date", (operation_id,)),
        one("SELECT * FROM reschedule_dispatch_operations WHERE request_id=?", (request_id,)),
    )


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
    require_customer_request: bool = False,
) -> Reservation:
    """Read-only recovery: exact existing rows may confirm; this never writes Sheet."""
    if conn.in_transaction:
        raise PairRescheduleConflict("reconcile requires transaction ownership")
    local_now = _aware_now(now)
    admin = _validate_admin(conn, admin_context)
    ensure_pair_reschedule_schema(conn)
    ensure_workbook_lease_schema(conn)
    conn.commit()
    # A short read transaction freezes one coherent pre-I/O snapshot, then
    # releases its SQLite lock before the external read begins.
    conn.execute("BEGIN")
    try:
        admin = _validate_admin(conn, admin_context)
        replay = _replay(conn, request_id)
        if not replay or int(replay[1]) != order_id or str(replay[2]) != admin:
            raise PairRescheduleConflict("reconcile request binding differs")
        token, lease_status = _reconcile_binding(
            conn, replay=replay, request_id=request_id, order_id=order_id, admin=admin,
        )
        operation_id = str(replay[0])
        frozen = _reconcile_frozen_state(
            conn, operation_id=operation_id, request_id=request_id, order_id=order_id,
            admin=admin, require_customer_request=require_customer_request,
        )
        expected = _view_rows_for_snapshot(conn, operation_id)
        snapshot_rows = _sheet_rows_for_snapshot(conn, operation_id)
        if any(expected["schedule"].get(day) != snapshot_rows.get(day)
               for day in (str(replay[3]), str(replay[4]))):
            raise PairRescheduleConflict("reconcile schedule view differs from immutable snapshot")
        binding = conn.execute(
            """SELECT owner_user_id,source_date,target_date
                 FROM pair_reschedule_snapshot_receipts WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    exact = bool(binding) and _exact_view_readback(
        sheet, owner=str(binding[0]), source=str(binding[1]), target=str(binding[2]),
        expected=expected,
    )
    # No transaction is held during the external read.  BEGIN IMMEDIATE now
    # serializes the complete compare/confirm/release sequence with other writers.
    conn.execute("BEGIN IMMEDIATE")
    try:
        _validate_admin(conn, admin_context)
        current = _replay(conn, request_id)
        if not current or tuple(current) != tuple(replay):
            raise PairRescheduleConflict("reconcile request changed during readback")
        if _reconcile_frozen_state(
            conn, operation_id=operation_id, request_id=request_id, order_id=order_id,
            admin=admin, require_customer_request=require_customer_request,
        ) != frozen:
            raise PairRescheduleConflict("reconcile binding changed during readback")
        checked_token, checked_status = _reconcile_binding(
            conn, replay=current, request_id=request_id, order_id=order_id, admin=admin,
        )
        if (checked_token, checked_status) != (token, lease_status):
            raise PairRescheduleConflict("reconcile lease changed during readback")
        if not exact:
            if str(current[5]) == "confirmed":
                raise PairRescheduleConflict("confirmed reconcile fresh readback differs")
            row = conn.execute(
                "SELECT claim_token FROM reschedule_dispatch_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            try:
                result = mark_sheet_unknown(
                    conn, operation_id=operation_id, claim_token=str(row[0]),
                    reason="reconcile_readback_mismatch", now=local_now.isoformat(timespec="seconds"),
                    caller_owned=True,
                )
            except RescheduleConflict as exc:
                raise PairRescheduleConflict(str(exc)) from exc
        elif str(current[5]) == "confirmed":
            result = _result(current, request_id)
        else:
            if lease_status != "active":
                raise PairRescheduleConflict("unconfirmed reconcile requires active workbook lease")
            result = _confirm_from_snapshot(
                conn, operation_id=operation_id, now=local_now, caller_owned=True,
            )
        if exact and lease_status == "active":
            try:
                release_workbook_lease(
                    conn, lease_token=token, final_outcome="confirmed", now=local_now,
                    caller_owned=True,
                )
            except WorkbookLeaseConflict as exc:
                raise PairRescheduleConflict("reconciled operation lease could not be released") from exc
        # The registered customer's request is part of the same commit as the
        # confirmation and exact original lease release.  Legacy direct calls
        # without a persisted request continue to have no request projection.
        frozen_request = frozen[4]
        if frozen_request is not None:
            request_columns = tuple(
                str(column[1]) for column in
                conn.execute("PRAGMA table_info(customer_pair_reschedule_requests)")
            )
            canonical_columns = (
                "request_id", "order_id", "owner_user_id", "source_date",
                "target_date", "status", "operation_id", "created_at", "expires_at",
            )
            notification_columns = (
                "admin_notification_status", "admin_notification_last_error",
                "admin_notification_attempted_at",
            )
            if request_columns not in (canonical_columns, canonical_columns + notification_columns):
                raise PairRescheduleConflict("customer request column order differs")
            current_request = conn.execute(
                "SELECT * FROM customer_pair_reschedule_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if current_request is None or tuple(current_request) != frozen_request:
                raise PairRescheduleConflict("customer request changed during confirmation")
        request = _reconcile_request_binding(
            conn, operation_id=operation_id, request_id=request_id,
            order_id=order_id, admin=admin, required=require_customer_request,
        )
        if request != frozen_request:
            raise PairRescheduleConflict("customer request binding changed during confirmation")
        if request is not None and result.status == "confirmed" and request[5] != "confirmed":
            # Column names come only from the verified canonical schema; each
            # predicate uses the original pre-I/O value, including NULLs.
            predicates = " AND ".join(f"{column} IS ?" for column in request_columns)
            changed = conn.execute(
                "UPDATE customer_pair_reschedule_requests "
                f"SET status='confirmed',operation_id=? WHERE {predicates}",
                (operation_id, *frozen_request),
            ).rowcount
            if changed != 1:
                raise PairRescheduleConflict("customer request projection CAS differs")
        if frozen_request is not None:
            expected_request = list(frozen_request)
            if result.status == "confirmed":
                expected_request[5] = "confirmed"
                expected_request[6] = operation_id
            final_request = conn.execute(
                "SELECT * FROM customer_pair_reschedule_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if final_request is None or tuple(final_request) != tuple(expected_request):
                raise PairRescheduleConflict("customer request projection changed unexpectedly")
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
