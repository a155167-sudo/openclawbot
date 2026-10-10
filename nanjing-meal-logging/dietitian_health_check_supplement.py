"""Durable authorized transition from review-ready to customer supplement pending.

The registered HTTP boundary authenticates the dietitian before invoking this
adapter. This module writes only canonical SQLite state; it does not enqueue or
send a LINE notification.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3

from dietitian_health_check_migration import require_canonical_table_contract
from vip_health_check import (
    configure_vip_health_check_connection,
    ensure_dietitian_health_check_draft_schema,
    ensure_vip_health_check_notification_schema,
    health_check_source_token,
    probe_current_health_check_source_manifest,
)


class SupplementNotFound(ValueError):
    """The requested health-check case does not exist."""


class SupplementConflict(ValueError):
    """The transition or durable request binding is stale."""


_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SCHEMA_V1 = """CREATE TABLE IF NOT EXISTS health_check_supplement_requests (
    supplement_request_id TEXT PRIMARY KEY NOT NULL,
    case_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    expected_source_token TEXT NOT NULL,
    expected_review_version INTEGER NOT NULL CHECK(expected_review_version >= 1),
    review_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    required_content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_customer','resolved','cancelled')),
    notification_status TEXT NOT NULL CHECK(notification_status IN ('not_sent','queued','delivered')),
    requested_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    UNIQUE(case_id,request_id),
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
    FOREIGN KEY(review_id) REFERENCES vip_health_check_reviews(review_id)
)"""
_SCHEMA_V2 = """CREATE TABLE IF NOT EXISTS health_check_supplement_requests (
    supplement_request_id TEXT PRIMARY KEY NOT NULL,
    case_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    expected_source_token TEXT NOT NULL,
    expected_review_version INTEGER NOT NULL CHECK(expected_review_version >= 1),
    review_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    required_content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_customer','resolved','cancelled')),
    notification_status TEXT NOT NULL CHECK(notification_status IN ('not_sent','queued','delivered')),
    requested_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    recipient_user_id TEXT NOT NULL DEFAULT '',
    notification_generation INTEGER NOT NULL DEFAULT 0 CHECK(notification_generation >= 0),
    UNIQUE(case_id,request_id),
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
    FOREIGN KEY(review_id) REFERENCES vip_health_check_reviews(review_id)
)"""
_SCHEMA = """CREATE TABLE IF NOT EXISTS health_check_supplement_requests (
    supplement_request_id TEXT PRIMARY KEY NOT NULL,
    case_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    expected_source_token TEXT NOT NULL,
    expected_review_version INTEGER NOT NULL CHECK(expected_review_version >= 1),
    review_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    required_content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_customer','resolved','cancelled')),
    notification_status TEXT NOT NULL CHECK(notification_status IN ('not_sent','queued','delivered')),
    requested_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    recipient_user_id TEXT NOT NULL DEFAULT '',
    notification_generation INTEGER NOT NULL DEFAULT 0 CHECK(notification_generation >= 0),
    expected_source_content_digest TEXT NOT NULL DEFAULT '',
    UNIQUE(case_id,request_id),
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
    FOREIGN KEY(review_id) REFERENCES vip_health_check_reviews(review_id)
)"""
_ACTIVE_INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS idx_vip_health_check_supplement_active
ON health_check_supplement_requests(case_id) WHERE status='pending_customer'"""
_RECIPIENT_IMMUTABLE_TRIGGER = """CREATE TRIGGER health_check_supplement_recipient_immutable
BEFORE UPDATE OF recipient_user_id ON health_check_supplement_requests
WHEN OLD.recipient_user_id IS NOT NEW.recipient_user_id
BEGIN SELECT RAISE(ABORT, 'supplement recipient is immutable'); END"""


def _payload_hash(reason: str, required_content: str) -> str:
    encoded = json.dumps(
        {
            "action": "request_health_check_more_info_v1",
            "reason": reason,
            "required_content": required_content,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise SupplementConflict(f"invalid {field}")
    return value


def _require_valid_rows(conn: sqlite3.Connection, *, version: int) -> None:
    table = "health_check_supplement_requests"
    if conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchone() is not None:
        raise sqlite3.IntegrityError("invalid supplement foreign key rows")
    text_columns = (
        "supplement_request_id", "case_id", "request_id", "actor_id", "payload_hash",
        "expected_source_token", "review_id", "reason", "required_content", "status",
        "notification_status", "requested_at", "resolved_at",
    )
    invalid_terms = [f"typeof(s.{column}) <> 'text'" for column in text_columns]
    invalid_terms.extend((
        "typeof(s.expected_review_version) <> 'integer'",
        "s.expected_review_version < 1",
        "s.status NOT IN ('pending_customer','resolved','cancelled')",
        "s.notification_status NOT IN ('not_sent','queued','delivered')",
    ))
    join = ""
    if version >= 2:
        invalid_terms.extend((
            "typeof(s.recipient_user_id) <> 'text'",
            "typeof(s.notification_generation) <> 'integer'",
            "s.notification_generation < 0",
            "(s.recipient_user_id <> '' AND s.recipient_user_id IS NOT c.user_id)",
        ))
        join = " JOIN vip_health_check_cases c ON c.case_id=s.case_id"
    if version >= 3:
        invalid_terms.extend((
            "typeof(s.expected_source_content_digest) <> 'text'",
            "(s.expected_source_content_digest <> '' AND length(s.expected_source_content_digest) <> 64)",
            "(s.expected_source_content_digest <> '' AND s.expected_source_content_digest GLOB '*[^0-9a-f]*')",
        ))
    invalid = conn.execute(
        f"SELECT 1 FROM {table} s{join} WHERE {' OR '.join(invalid_terms)} LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise sqlite3.IntegrityError("invalid supplement row contract")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='health_check_supplement_requests'"
    ).fetchone() is not None
    if exists:
        try:
            require_canonical_table_contract(
                conn, table="health_check_supplement_requests", table_ddl=_SCHEMA,
                index_ddls=(_ACTIVE_INDEX, _RECIPIENT_IMMUTABLE_TRIGGER),
                error_label="supplement",
            )
        except sqlite3.IntegrityError:
            try:
                require_canonical_table_contract(
                    conn, table="health_check_supplement_requests", table_ddl=_SCHEMA_V2,
                    index_ddls=(_ACTIVE_INDEX, _RECIPIENT_IMMUTABLE_TRIGGER),
                    error_label="supplement",
                )
            except sqlite3.IntegrityError:
                # Exact shipped v1/v2 only.  Historical rows receive an empty
                # baseline rather than one reconstructed from current sources.
                require_canonical_table_contract(
                    conn, table="health_check_supplement_requests", table_ddl=_SCHEMA_V1,
                    index_ddls=(_ACTIVE_INDEX,), error_label="supplement",
                )
                _require_valid_rows(conn, version=1)
                source_version = 1
            else:
                _require_valid_rows(conn, version=2)
                source_version = 2
        else:
            _require_valid_rows(conn, version=3)
            return
        savepoint = "health_check_supplement_schema_v3"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            if source_version == 1:
                conn.execute(
                    "ALTER TABLE health_check_supplement_requests "
                    "ADD COLUMN recipient_user_id TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "ALTER TABLE health_check_supplement_requests "
                    "ADD COLUMN notification_generation INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(notification_generation >= 0)"
                )
                conn.execute(_RECIPIENT_IMMUTABLE_TRIGGER)
            conn.execute(
                "ALTER TABLE health_check_supplement_requests "
                "ADD COLUMN expected_source_content_digest TEXT NOT NULL DEFAULT ''"
            )
            require_canonical_table_contract(
                conn, table="health_check_supplement_requests", table_ddl=_SCHEMA,
                index_ddls=(_ACTIVE_INDEX, _RECIPIENT_IMMUTABLE_TRIGGER),
                error_label="supplement",
            )
            _require_valid_rows(conn, version=3)
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return
    savepoint = "health_check_supplement_schema"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(_SCHEMA)
        conn.execute(_ACTIVE_INDEX)
        conn.execute(_RECIPIENT_IMMUTABLE_TRIGGER)
        require_canonical_table_contract(
            conn, table="health_check_supplement_requests", table_ddl=_SCHEMA,
            index_ddls=(_ACTIVE_INDEX, _RECIPIENT_IMMUTABLE_TRIGGER),
            error_label="supplement",
        )
        _require_valid_rows(conn, version=3)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _initialize_schema(conn: sqlite3.Connection) -> None:
    """Install all prerequisites atomically without committing caller state."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    savepoint = "health_check_supplement_initializer"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='health_check_supplement_requests'"
        ).fetchone() is not None:
            _ensure_schema(conn)
        ensure_vip_health_check_notification_schema(conn)
        ensure_dietitian_health_check_draft_schema(conn)
        _ensure_schema(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if owns_transaction:
            conn.rollback()
        raise


def create_health_check_supplement_saver(
    database_path: str | Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
):
    """Create the transaction adapter; schema installation is local and additive."""
    path = str(database_path)
    with sqlite3.connect(path) as conn:
        configure_vip_health_check_connection(conn)
        _initialize_schema(conn)
        conn.commit()

    def request_more_info(
        case_id: str,
        reason: str,
        required_content: str,
        expected_source_token: str,
        expected_review_version: int,
        request_id: str,
        actor_id: str,
    ) -> dict[str, object]:
        reason = _required(reason, "reason")
        required_content = _required(required_content, "required_content")
        actor_id = _required(actor_id, "actor_id")
        if not _REQUEST_ID.fullmatch(str(request_id or "")):
            raise SupplementConflict("invalid request id")
        payload_hash = _payload_hash(reason, required_content)
        binding = (
            actor_id,
            payload_hash,
            expected_source_token,
            expected_review_version,
        )
        with sqlite3.connect(path, timeout=10.0) as conn:
            configure_vip_health_check_connection(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = conn.execute(
                    """SELECT s.actor_id,s.payload_hash,s.expected_source_token,
                              s.expected_review_version,s.recipient_user_id,s.status,
                              s.notification_status,c.status,c.user_id
                       FROM health_check_supplement_requests s
                       JOIN vip_health_check_cases c ON c.case_id=s.case_id
                       WHERE s.case_id=? AND s.request_id=?""",
                    (case_id, request_id),
                ).fetchone()
                if operation is not None:
                    if tuple(operation[:4]) != binding or operation[5] != "pending_customer":
                        raise SupplementConflict("request id binding conflict")
                    if not str(operation[4] or "").strip() or operation[4] != operation[8]:
                        raise SupplementConflict("request recipient binding conflict")
                    current_status = str(operation[7])
                    if current_status not in {
                        "needs_more_info", "approved_pending_delivery", "delivery_failed",
                        "delivered", "expired", "cancelled",
                    }:
                        raise SupplementConflict("request is no longer current")
                    conn.rollback()
                    result: dict[str, object] = {
                        "status": current_status,
                        "created": False,
                    }
                    if current_status == "needs_more_info":
                        result["notification_status"] = operation[6]
                    return result

                case = conn.execute(
                    "SELECT status,source_manifest_hash,user_id FROM vip_health_check_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if case is None:
                    raise SupplementNotFound("case not found")
                if case[0] != "ready_for_review":
                    raise SupplementConflict("case is not supplement-requestable")
                recipient_user_id = _required(case[2], "case recipient")
                if health_check_source_token(
                    conn, case_id=case_id, manifest_hash=str(case[1])
                ) != expected_source_token:
                    raise SupplementConflict("source version is stale")
                requested_at = now()
                authoritative = probe_current_health_check_source_manifest(
                    conn, case_id=case_id, evaluated_at=requested_at
                )
                if (
                    authoritative["status"] != case[0]
                    or str(authoritative["source_manifest_hash"]) != str(case[1])
                ):
                    raise SupplementConflict("canonical sources changed")
                review = conn.execute(
                    """SELECT review_id,review_version,status,source_manifest_hash
                       FROM vip_health_check_reviews WHERE case_id=?
                       ORDER BY review_version DESC LIMIT 1""",
                    (case_id,),
                ).fetchone()
                if (
                    review is None
                    or isinstance(expected_review_version, bool)
                    or review[1] != expected_review_version
                    or review[2] != "draft"
                    or str(review[3]) != str(case[1])
                ):
                    raise SupplementConflict("review version is stale")
                requested_text = requested_at.isoformat()
                supplement_id = "vhcs_" + hashlib.sha256(
                    f"{case_id}:{request_id}".encode("utf-8")
                ).hexdigest()[:32]
                conn.execute(
                    """INSERT INTO health_check_supplement_requests
                       (supplement_request_id,case_id,request_id,actor_id,payload_hash,
                        expected_source_token,expected_review_version,review_id,reason,
                        required_content,status,notification_status,requested_at,resolved_at,
                        recipient_user_id,notification_generation,expected_source_content_digest)
                       VALUES (?,?,?,?,?,?,?,?,?,?,'pending_customer','not_sent',?,'',?,0,?)""",
                    (
                        supplement_id, case_id, request_id, actor_id, payload_hash,
                        expected_source_token, expected_review_version, review[0], reason,
                        required_content, requested_text, recipient_user_id,
                        authoritative["source_content_digest"],
                    ),
                )
                changed = conn.execute(
                    """UPDATE vip_health_check_cases SET status='needs_more_info',updated_at=?
                       WHERE case_id=? AND status='ready_for_review'
                         AND source_manifest_hash=?""",
                    (requested_text, case_id, case[1]),
                )
                if changed.rowcount != 1:
                    raise SupplementConflict("case transition changed")
                conn.execute(
                    """INSERT INTO vip_health_check_audit_log
                       (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
                       VALUES (?,'dietitian',?,'ready_for_review','needs_more_info',
                               'supplement_requested',?)""",
                    (case_id, actor_id, requested_text),
                )
                conn.commit()
                return {
                    "status": "needs_more_info",
                    "notification_status": "not_sent",
                    "created": True,
                }
            except Exception:
                conn.rollback()
                raise

    return request_more_info
