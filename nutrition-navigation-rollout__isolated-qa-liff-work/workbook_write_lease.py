"""Durable fail-closed lease boundary for writers sharing one workbook.

This module is intentionally not wired into production.  A lease is available only
after an operator records the complete controlled-writer inventory and evidence that
external writers were disabled or joined to the same protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
from typing import Sequence


class WorkbookLeaseConflict(RuntimeError):
    pass


# Frozen P0-4 inventory. External entries are deliberately required even
# though their real cutover evidence is a separate P0-5 release gate. Tests
# may use complete fixture-only facts; they are not production proof.
FULL_REQUIRED_WRITER_INVENTORY = frozenset({
    "pair_reschedule",
    "subscription_formalization",
    "deferred_meal",
    "meal_swap",
    "master_api_mutation",
    "garmin_sheet",
    "customer_list",
    "training_assignment",
    "legacy_receive_form",
    "personal_sheet_rebuild",
    "four_week_background",
    "workout_food_tracking",
    "weekly_coach",
    "nutrition_outbox",
    "android_printer_status",
    "apps_script_forms_addons",
    "manual_editors",
    "other_service_accounts",
    "legacy_server_deployments",
})


@dataclass(frozen=True)
class WorkbookLease:
    workbook_id: str
    writer_id: str
    operation_id: str
    lease_token: str
    expires_at: str


def _instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkbookLeaseConflict("lease clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def ensure_workbook_lease_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS workbook_writer_capabilities (
        workbook_id TEXT PRIMARY KEY NOT NULL,
        controlled_writers_json TEXT NOT NULL,
        external_writer_state TEXT NOT NULL CHECK(external_writer_state IN ('unknown','disabled','controlled')),
        cutover_evidence TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS workbook_write_leases (
        workbook_id TEXT PRIMARY KEY NOT NULL,
        writer_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        lease_token TEXT NOT NULL UNIQUE,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','released')),
        final_outcome TEXT,
        released_at TEXT
    )""")


def configure_workbook_writer_capability(
    conn: sqlite3.Connection, *, workbook_id: str,
    controlled_writers: Sequence[str], external_writer_state: str,
    cutover_evidence: str,
) -> None:
    writers = sorted({str(item).strip() for item in controlled_writers if str(item).strip()})
    if not workbook_id or external_writer_state not in {"unknown", "disabled", "controlled"}:
        raise WorkbookLeaseConflict("workbook capability is invalid")
    conn.execute("""INSERT INTO workbook_writer_capabilities VALUES(?,?,?,?)
        ON CONFLICT(workbook_id) DO UPDATE SET
          controlled_writers_json=excluded.controlled_writers_json,
          external_writer_state=excluded.external_writer_state,
          cutover_evidence=excluded.cutover_evidence""",
        (workbook_id, json.dumps(writers, separators=(",", ":")),
         external_writer_state, str(cutover_evidence).strip()),
    )


def require_full_writer_inventory(
    conn: sqlite3.Connection, *, workbook_id: str, writer_id: str,
) -> None:
    """Fail closed unless the frozen complete inventory names the caller."""
    row = conn.execute(
        "SELECT controlled_writers_json,external_writer_state,cutover_evidence "
        "FROM workbook_writer_capabilities WHERE workbook_id=?", (workbook_id,),
    ).fetchone()
    try:
        writers = json.loads(row[0]) if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        writers = []
    if not isinstance(writers, list) or not all(
        isinstance(item, str) and item for item in writers
    ):
        raise WorkbookLeaseConflict("full writer inventory is not proven")
    if set(writers) != FULL_REQUIRED_WRITER_INVENTORY:
        raise WorkbookLeaseConflict("full writer inventory is not proven")
    if writer_id not in FULL_REQUIRED_WRITER_INVENTORY:
        raise WorkbookLeaseConflict("writer is not named in full writer inventory")
    if row[1] not in {"disabled", "controlled"} or not str(row[2]).strip():
        raise WorkbookLeaseConflict("exclusive workbook writer capability is not proven")


def acquire_workbook_lease(
    conn: sqlite3.Connection, *, workbook_id: str, writer_id: str,
    operation_id: str, now: datetime, ttl_seconds: int,
) -> WorkbookLease:
    instant = _instant(now)
    if conn.in_transaction or not all((workbook_id, writer_id, operation_id)):
        raise WorkbookLeaseConflict("lease acquisition requires clean transaction ownership")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise WorkbookLeaseConflict("lease TTL is invalid")
    conn.execute("BEGIN IMMEDIATE")
    try:
        require_full_writer_inventory(
            conn, workbook_id=workbook_id, writer_id=writer_id,
        )
        current = conn.execute(
            "SELECT status,expires_at FROM workbook_write_leases WHERE workbook_id=?",
            (workbook_id,),
        ).fetchone()
        if current and current[0] == "active":
            expiry = datetime.fromisoformat(str(current[1]))
            if expiry <= instant:
                raise WorkbookLeaseConflict("existing lease expired outcome is unknown")
            raise WorkbookLeaseConflict("workbook is already leased")
        token = secrets.token_urlsafe(32)
        expires = instant + timedelta(seconds=ttl_seconds)
        conn.execute("""INSERT INTO workbook_write_leases
            (workbook_id,writer_id,operation_id,lease_token,acquired_at,expires_at,status,final_outcome,released_at)
            VALUES(?,?,?,?,?,?,'active',NULL,NULL)
            ON CONFLICT(workbook_id) DO UPDATE SET
              writer_id=excluded.writer_id,operation_id=excluded.operation_id,
              lease_token=excluded.lease_token,acquired_at=excluded.acquired_at,
              expires_at=excluded.expires_at,status='active',final_outcome=NULL,released_at=NULL""",
            (workbook_id, writer_id, operation_id, token, instant.isoformat(), expires.isoformat()),
        )
        conn.commit()
        return WorkbookLease(workbook_id, writer_id, operation_id, token, expires.isoformat())
    except Exception:
        conn.rollback()
        raise


def release_workbook_lease(conn: sqlite3.Connection, *, lease_token: str,
                           final_outcome: str, now: datetime) -> None:
    instant = _instant(now)
    if final_outcome not in {"confirmed", "rejected_before_write"}:
        raise WorkbookLeaseConflict("release requires a known final outcome")
    if conn.in_transaction:
        raise WorkbookLeaseConflict("lease release requires clean transaction ownership")
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute("""UPDATE workbook_write_leases
            SET status='released',final_outcome=?,released_at=?
            WHERE lease_token=? AND status='active'""",
            (final_outcome, instant.isoformat(), lease_token),
        ).rowcount
        if changed != 1:
            raise WorkbookLeaseConflict("active lease token is invalid")
        conn.commit()
    except Exception:
        conn.rollback()
        raise