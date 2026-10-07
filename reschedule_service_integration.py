"""Default-off application boundary for explicit customer/admin pair reschedules.

This module intentionally registers no HTTP, LINE, natural-language, Android, or
production configuration surface.  A caller must opt in with the literal boolean
``True`` and provide contexts derived from persisted authority.  The request body
contains only order-bound dates and an idempotency key; Sheet rows, profile data,
owner identity, service policy, and dispatch payload are never accepted here.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import uuid
import sqlite3

from pair_reschedule_coordinator import (
    PairRescheduleConflict,
    VerifiedAdminContext,
    _aware_now,
    _validate_admin,
    _validate_policy,
    ensure_pair_reschedule_schema,
    execute_pair_reschedule,
)
from reschedule_dispatch_versions import Reservation


class RescheduleFeatureUnavailable(RuntimeError):
    pass


class RescheduleAuthorizationError(RuntimeError):
    pass


class RescheduleRequestConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedCustomerRescheduleContext:
    actor_id: str
    order_id: int


@dataclass(frozen=True)
class CustomerPairRescheduleRequest:
    request_id: str
    order_id: int
    owner_user_id: str
    source_date: str
    target_date: str
    status: str
    created_at: str
    expires_at: str
    admin_notification_status: str = "not_sent"
    admin_notification_last_error: str = ""
    admin_notification_attempted_at: str = ""


@dataclass(frozen=True)
class AdminPairRescheduleReadback:
    request_id: str
    order_id: int
    source_date: str
    target_date: str
    status: str
    created_at: str
    expires_at: str
    admin_notification_status: str
    admin_notification_last_error: str
    admin_notification_attempted_at: str


_REQUEST_DDL = """CREATE TABLE customer_pair_reschedule_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    order_id INTEGER NOT NULL,
    owner_user_id TEXT NOT NULL,
    source_date TEXT NOT NULL,
    target_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_admin','confirmed','sheet_unknown')),
    operation_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    admin_notification_status TEXT NOT NULL DEFAULT 'not_sent'
        CHECK(admin_notification_status IN ('not_sent','deferred','delivered','failed','outcome_unknown')),
    admin_notification_last_error TEXT NOT NULL DEFAULT '',
    admin_notification_attempted_at TEXT NOT NULL DEFAULT ''
)"""

_LEGACY_REQUEST_DDL = """CREATE TABLE customer_pair_reschedule_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    order_id INTEGER NOT NULL,
    owner_user_id TEXT NOT NULL,
    source_date TEXT NOT NULL,
    target_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_admin','confirmed','sheet_unknown')),
    operation_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)"""

_NOTIFICATION_STATUSES = {
    "not_sent",
    "deferred",
    "delivered",
    "failed",
    "outcome_unknown",
}


def _enabled(value: object) -> None:
    if value is not True:
        raise RescheduleFeatureUnavailable("pair reschedule application flow is not enabled")


def _ensure_request_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='customer_pair_reschedule_requests'"
    ).fetchone()
    if row:
        installed = "".join(str(row[0] or "").split()).lower()
        expected = "".join(_REQUEST_DDL.split()).lower()
        legacy = "".join(_LEGACY_REQUEST_DDL.split()).lower()
        if installed != expected:
            if installed != legacy:
                raise RescheduleRequestConflict("customer reschedule request schema differs")
            conn.execute(
                """ALTER TABLE customer_pair_reschedule_requests
                   ADD COLUMN admin_notification_status TEXT NOT NULL DEFAULT 'not_sent'
                   CHECK(admin_notification_status IN ('not_sent','deferred','delivered','failed','outcome_unknown'))"""
            )
            conn.execute(
                """ALTER TABLE customer_pair_reschedule_requests
                   ADD COLUMN admin_notification_last_error TEXT NOT NULL DEFAULT ''"""
            )
            conn.execute(
                """ALTER TABLE customer_pair_reschedule_requests
                   ADD COLUMN admin_notification_attempted_at TEXT NOT NULL DEFAULT ''"""
            )
        return
    conn.execute(_REQUEST_DDL)


def _has_valid_request_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='customer_pair_reschedule_requests'"
    ).fetchone()
    if not row:
        return False
    installed = "".join(str(row[0] or "").split()).lower()
    expected = "".join(_REQUEST_DDL.split()).lower()
    legacy = "".join(_LEGACY_REQUEST_DDL.split()).lower()
    if installed != expected:
        if installed == legacy:
            return True
        raise RescheduleRequestConflict("customer reschedule request schema differs")
    return True


def _request_schema_has_notification_columns(conn: sqlite3.Connection) -> bool:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(customer_pair_reschedule_requests)")
    }
    return {
        "admin_notification_status",
        "admin_notification_last_error",
        "admin_notification_attempted_at",
    }.issubset(columns)


def verify_customer_reschedule_context(
    conn: sqlite3.Connection, *, actor_id: str, order_id: int,
) -> VerifiedCustomerRescheduleContext:
    if not actor_id or isinstance(order_id, bool) or not isinstance(order_id, int):
        raise RescheduleAuthorizationError("verified customer owner is required")
    rows = conn.execute(
        "SELECT user_id,status FROM subscription_orders WHERE id=?", (order_id,),
    ).fetchall()
    if len(rows) != 1 or tuple(rows[0]) != (actor_id, "activated"):
        raise RescheduleAuthorizationError("verified customer is not the activated order owner")
    return VerifiedCustomerRescheduleContext(actor_id, order_id)


def _revalidate_customer(
    conn: sqlite3.Connection, context: VerifiedCustomerRescheduleContext,
) -> str:
    if not isinstance(context, VerifiedCustomerRescheduleContext):
        raise RescheduleAuthorizationError("verified customer context is required")
    row = conn.execute(
        "SELECT user_id,status FROM subscription_orders WHERE id=?", (context.order_id,),
    ).fetchone()
    if not row or tuple(row) != (context.actor_id, "activated"):
        raise RescheduleAuthorizationError("verified customer owner authority changed")
    return context.actor_id


def _request_from_row(row: sqlite3.Row | tuple[object, ...]) -> CustomerPairRescheduleRequest:
    return CustomerPairRescheduleRequest(
        request_id=str(row[0]), order_id=int(row[1]), owner_user_id=str(row[2]),
        source_date=str(row[3]), target_date=str(row[4]), status=str(row[5]),
        created_at=str(row[7]), expires_at=str(row[8]),
        admin_notification_status=str(row[9]) if len(row) > 9 else "not_sent",
        admin_notification_last_error=str(row[10]) if len(row) > 10 else "",
        admin_notification_attempted_at=str(row[11]) if len(row) > 11 else "",
    )


def _notification_retry_key(request_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "customer-pair-reschedule-admin:" + request_id))


def _short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _load_request_meal_content(
    conn: sqlite3.Connection, *, order_id: int, owner: str, source_date: str, target_date: str
) -> dict[str, tuple[str, str]]:
    try:
        rows = conn.execute(
            """SELECT service_date,lunch,dinner
                 FROM subscription_dispatch_rows
                WHERE order_id=? AND customer_uid=? AND service_date IN (?,?)
                  AND publish_state='published'
                ORDER BY created_at DESC""",
            (order_id, owner, source_date, target_date),
        ).fetchall()
    except sqlite3.Error:
        return {}
    meals: dict[str, tuple[str, str]] = {}
    for row in rows:
        day = str(row[0])
        if day not in meals:
            meals[day] = (str(row[1] or ""), str(row[2] or ""))
    return meals


def render_admin_pair_reschedule_pending_receipt(
    request: CustomerPairRescheduleRequest,
    *,
    meal_content: dict[str, tuple[str, str]] | None = None,
) -> str:
    meals = meal_content or {}
    dishes = [name for name in meals.get(request.source_date, ("", "")) if name]
    expiry = datetime.fromisoformat(request.expires_at).strftime("%Y-%m-%d %H:%M")
    lines = [
        "餐點改期待確認",
        f"日期：{request.source_date} → {request.target_date}",
        f"餐點：{'、'.join(dishes) if dishes else '請開啟申請查看'}",
        f"請於 {expiry} 前處理（台北時間）",
    ]
    return "\n".join(lines)


def _record_admin_notification(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    status: str,
    error: str,
    attempted_at: str,
) -> None:
    if status not in _NOTIFICATION_STATUSES:
        raise RescheduleRequestConflict("invalid admin notification status")
    conn.execute(
        """UPDATE customer_pair_reschedule_requests
              SET admin_notification_status=?,
                  admin_notification_last_error=?,
                  admin_notification_attempted_at=?
            WHERE request_id=? AND status='pending_admin'""",
        (status, str(error or "")[:500], attempted_at, request_id),
    )
    conn.commit()


def _notify_admin_pair_reschedule_pending(
    conn: sqlite3.Connection,
    *,
    request: CustomerPairRescheduleRequest,
    notification_sender: Callable[[str, str, str], None] | None,
    notification_enabled: bool,
    now: datetime,
) -> CustomerPairRescheduleRequest:
    if not notification_enabled or notification_sender is None:
        _record_admin_notification(
            conn,
            request_id=request.request_id,
            status="deferred",
            error="admin notification sender disabled",
            attempted_at="",
        )
        return _request_from_row(conn.execute(
            """SELECT request_id,order_id,owner_user_id,source_date,target_date,status,
                      operation_id,created_at,expires_at,admin_notification_status,
                      admin_notification_last_error,admin_notification_attempted_at
                 FROM customer_pair_reschedule_requests WHERE request_id=?""",
            (request.request_id,),
        ).fetchone())
    row = conn.execute(
        "SELECT value,typeof(value) FROM admin_settings WHERE key='admin_id'"
    ).fetchone()
    if not row or row[1] != "text" or not str(row[0] or "").strip():
        _record_admin_notification(
            conn,
            request_id=request.request_id,
            status="deferred",
            error="bound admin LINE UID is unavailable",
            attempted_at="",
        )
        return _request_from_row(conn.execute(
            """SELECT request_id,order_id,owner_user_id,source_date,target_date,status,
                      operation_id,created_at,expires_at,admin_notification_status,
                      admin_notification_last_error,admin_notification_attempted_at
                 FROM customer_pair_reschedule_requests WHERE request_id=?""",
            (request.request_id,),
        ).fetchone())
    recipient = str(row[0])
    meal_content = _load_request_meal_content(
        conn,
        order_id=request.order_id,
        owner=request.owner_user_id,
        source_date=request.source_date,
        target_date=request.target_date,
    )
    text = render_admin_pair_reschedule_pending_receipt(request, meal_content=meal_content)
    attempted = now.isoformat(timespec="seconds")
    try:
        contextual_sender = getattr(notification_sender, "send_pending_request", None)
        if callable(contextual_sender):
            contextual_sender(recipient, text, _notification_retry_key(request.request_id), request_id=request.request_id)
        else:
            notification_sender(recipient, text, _notification_retry_key(request.request_id))
    except Exception as exc:
        status = "failed" if getattr(exc, "retryable", True) is False else "outcome_unknown"
        _record_admin_notification(
            conn,
            request_id=request.request_id,
            status=status,
            error=_short_error(exc),
            attempted_at=attempted,
        )
    else:
        _record_admin_notification(
            conn,
            request_id=request.request_id,
            status="delivered",
            error="",
            attempted_at=attempted,
        )
    return _request_from_row(conn.execute(
        """SELECT request_id,order_id,owner_user_id,source_date,target_date,status,
                  operation_id,created_at,expires_at,admin_notification_status,
                  admin_notification_last_error,admin_notification_attempted_at
             FROM customer_pair_reschedule_requests WHERE request_id=?""",
        (request.request_id,),
    ).fetchone())


def list_pending_admin_customer_pair_reschedules(
    conn: sqlite3.Connection,
    *,
    admin_context: VerifiedAdminContext,
    feature_enabled: bool = False,
    limit: int = 50,
) -> list[AdminPairRescheduleReadback]:
    """Return pending customer requests for the verified admin readback surface.

    This is intentionally read-only: it never installs schema, approves a
    request, constructs a Sheet adapter, or returns customer LINE UIDs.
    """
    _enabled(feature_enabled)
    try:
        _validate_admin(conn, admin_context)
    except PairRescheduleConflict as exc:
        raise RescheduleAuthorizationError("verified admin authority is invalid") from exc
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RescheduleRequestConflict("invalid request list limit")
    if not _has_valid_request_schema(conn):
        return []
    if _request_schema_has_notification_columns(conn):
        rows = conn.execute(
            """SELECT request_id,order_id,source_date,target_date,status,created_at,expires_at,
                      admin_notification_status,admin_notification_last_error,
                      admin_notification_attempted_at
                 FROM customer_pair_reschedule_requests
                WHERE status='pending_admin'
                ORDER BY created_at ASC, request_id ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT request_id,order_id,source_date,target_date,status,created_at,expires_at,
                      'not_sent','',''
                 FROM customer_pair_reschedule_requests
                WHERE status='pending_admin'
                ORDER BY created_at ASC, request_id ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        AdminPairRescheduleReadback(
            request_id=str(row[0]),
            order_id=int(row[1]),
            source_date=str(row[2]),
            target_date=str(row[3]),
            status=str(row[4]),
            created_at=str(row[5]),
            expires_at=str(row[6]),
            admin_notification_status=str(row[7]),
            admin_notification_last_error=str(row[8]),
            admin_notification_attempted_at=str(row[9]),
        )
        for row in rows
    ]


def submit_customer_pair_reschedule_pending(
    conn: sqlite3.Connection,
    *,
    context: VerifiedCustomerRescheduleContext,
    source_date: str,
    target_date: str,
    request_id: str,
    now: datetime,
    feature_enabled: bool = False,
    notification_enabled: bool = False,
    notification_sender: Callable[[str, str, str], None] | None = None,
) -> CustomerPairRescheduleRequest:
    """Persist a customer application without executing or approving a move.

    The customer application window is validated by the HTTP boundary. This
    pending-only function intentionally does not call the admin execution
    policy, because a customer may apply for a date beyond the current meal
    schedule and an admin must re-check the final plan before approval.
    """
    _enabled(feature_enabled)
    owner = _revalidate_customer(conn, context)
    local_now = _aware_now(now)
    if not request_id or source_date == target_date:
        raise RescheduleRequestConflict("customer reschedule request identity is invalid")
    _ensure_request_schema(conn)
    prior = conn.execute(
        """SELECT request_id,order_id,owner_user_id,source_date,target_date,status,
                  operation_id,created_at,expires_at,admin_notification_status,
                  admin_notification_last_error,admin_notification_attempted_at
             FROM customer_pair_reschedule_requests WHERE request_id=?""",
        (request_id,),
    ).fetchone()
    if prior:
        if tuple(prior[1:5]) != (context.order_id, owner, source_date, target_date):
            raise RescheduleRequestConflict("customer request replay binding differs")
        return _request_from_row(prior)
    created = local_now.isoformat(timespec="seconds")
    expires = (local_now + timedelta(hours=24)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO customer_pair_reschedule_requests
           (request_id,order_id,owner_user_id,source_date,target_date,status,
            operation_id,created_at,expires_at)
           VALUES(?,?,?,?,?,'pending_admin',NULL,?,?)""",
        (request_id, context.order_id, owner, source_date, target_date, created, expires),
    )
    conn.commit()
    committed = CustomerPairRescheduleRequest(
        request_id, context.order_id, owner, source_date, target_date,
        "pending_admin", created, expires,
    )
    return _notify_admin_pair_reschedule_pending(
        conn,
        request=committed,
        notification_sender=notification_sender,
        notification_enabled=notification_enabled,
        now=local_now,
    )


def submit_customer_pair_reschedule(
    conn: sqlite3.Connection,
    *,
    context: VerifiedCustomerRescheduleContext,
    source_date: str,
    target_date: str,
    request_id: str,
    now: datetime,
    feature_enabled: bool = False,
    notification_enabled: bool = False,
    notification_sender: Callable[[str, str, str], None] | None = None,
) -> CustomerPairRescheduleRequest:
    """Persist one explicit owner-bound request after current DB policy validation."""
    _enabled(feature_enabled)
    owner = _revalidate_customer(conn, context)
    local_now = _aware_now(now)
    if not request_id or source_date == target_date:
        raise RescheduleRequestConflict("customer reschedule request identity is invalid")
    try:
        _validate_policy(
            conn, order_id=context.order_id, owner=owner,
            source=source_date, target=target_date, now=local_now,
        )
    except PairRescheduleConflict as exc:
        message = str(exc)
        if "calendar" in message:
            message = "verified service calendar is required"
        raise RescheduleRequestConflict(message) from exc

    _ensure_request_schema(conn)
    prior = conn.execute(
        """SELECT request_id,order_id,owner_user_id,source_date,target_date,status,
                  operation_id,created_at,expires_at,admin_notification_status,
                  admin_notification_last_error,admin_notification_attempted_at
             FROM customer_pair_reschedule_requests WHERE request_id=?""",
        (request_id,),
    ).fetchone()
    if prior:
        if tuple(prior[1:5]) != (context.order_id, owner, source_date, target_date):
            raise RescheduleRequestConflict("customer request replay binding differs")
        return _request_from_row(prior)

    created = local_now.isoformat(timespec="seconds")
    expires = (local_now + timedelta(hours=24)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO customer_pair_reschedule_requests
           (request_id,order_id,owner_user_id,source_date,target_date,status,
            operation_id,created_at,expires_at)
           VALUES(?,?,?,?,?,'pending_admin',NULL,?,?)""",
        (request_id, context.order_id, owner, source_date, target_date, created, expires),
    )
    conn.commit()
    committed = CustomerPairRescheduleRequest(
        request_id, context.order_id, owner, source_date, target_date,
        "pending_admin", created, expires,
    )
    return _notify_admin_pair_reschedule_pending(
        conn,
        request=committed,
        notification_sender=notification_sender,
        notification_enabled=notification_enabled,
        now=local_now,
    )


def approve_customer_pair_reschedule(
    conn: sqlite3.Connection,
    sheet: object | None,
    *,
    request_id: str,
    admin_context: VerifiedAdminContext,
    now: datetime,
    feature_enabled: bool = False,
    sheet_factory: object | None = None,
    policy_validator: Callable[
        [sqlite3.Connection, int, str, str, str, datetime],
        tuple[str, str],
    ] | None = None,
) -> Reservation:
    """Registered admin adapter: stored request -> lease-backed coordinator."""
    _enabled(feature_enabled)
    try:
        _validate_admin(conn, admin_context)
    except PairRescheduleConflict as exc:
        raise RescheduleAuthorizationError("verified admin authority is invalid") from exc
    local_now = _aware_now(now)
    ensure_pair_reschedule_schema(conn)
    _ensure_request_schema(conn)
    conn.commit()
    row = conn.execute(
        """SELECT request_id,order_id,owner_user_id,source_date,target_date,status,
                  operation_id,created_at,expires_at,admin_notification_status,
                  admin_notification_last_error,admin_notification_attempted_at
             FROM customer_pair_reschedule_requests WHERE request_id=?""",
        (request_id,),
    ).fetchone()
    if not row:
        raise RescheduleRequestConflict("customer reschedule request is missing")
    if str(row[5]) == "pending_admin":
        try:
            expires_at = datetime.fromisoformat(str(row[8]))
        except ValueError as exc:
            raise RescheduleRequestConflict("customer reschedule request expiry is invalid") from exc
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise RescheduleRequestConflict("customer reschedule request expiry is invalid")
        if local_now > expires_at:
            raise RescheduleRequestConflict("customer reschedule request expired")

    # Re-check owner immediately before coordinator entry.  The coordinator then
    # derives all mutable policy/profile/dispatch authority again from SQLite.
    owner = conn.execute(
        "SELECT user_id,status FROM subscription_orders WHERE id=?", (int(row[1]),),
    ).fetchone()
    if not owner or tuple(owner) != (str(row[2]), "activated"):
        raise RescheduleAuthorizationError("customer owner authority changed")
    # Exact durable replays are resolved by the coordinator before Sheet access.
    # Only a still-pending request may construct the network-backed adapter.
    if str(row[5]) == "pending_admin":
        if sheet is None:
            if not callable(sheet_factory):
                raise RescheduleRequestConflict("reschedule sheet adapter is unavailable")
            sheet = sheet_factory(conn, int(row[1]))
    elif sheet is None:
        sheet = object()
    try:
        result = execute_pair_reschedule(
            conn, sheet, order_id=int(row[1]), source_date=str(row[3]),
            target_date=str(row[4]), request_id=str(row[0]),
            admin_context=admin_context, now=local_now,
            policy_validator=policy_validator,
        )
    except PairRescheduleConflict as exc:
        raise RescheduleRequestConflict(str(exc)) from exc
    conn.execute(
        """UPDATE customer_pair_reschedule_requests
              SET status=?,operation_id=?
            WHERE request_id=? AND order_id=? AND owner_user_id=?""",
        (result.status, result.operation_id, str(row[0]), int(row[1]), str(row[2])),
    )
    conn.commit()
    return result
