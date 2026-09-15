"""Authorized transaction adapter for approving one persisted health-check draft.

The HTTP boundary authenticates first. This adapter derives the review, source
manifest, report payload, and recipient only from canonical SQLite rows. It
creates a pending delivery intent through the existing vip_health_check domain;
it never sends LINE or calls an external provider.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3

from vip_health_check import (
    approve_health_check_review,
    configure_vip_health_check_connection,
    ensure_dietitian_health_check_draft_schema,
    health_check_source_token,
    probe_current_health_check_source_manifest,
)


class ApprovalNotFound(ValueError):
    """The requested case does not exist."""


class ApprovalConflict(ValueError):
    """The case, source, review, or durable request binding is stale."""


_OPERATION_DDL = """CREATE TABLE vip_health_check_approval_operations (
    case_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    expected_source_token TEXT NOT NULL,
    expected_review_version INTEGER NOT NULL CHECK(expected_review_version >= 1),
    review_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','completed')),
    result_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(case_id,request_id),
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
    FOREIGN KEY(review_id) REFERENCES vip_health_check_reviews(review_id)
)"""
_PAYLOAD_HASH = hashlib.sha256(b'{"action":"approve_saved_review_v1"}').hexdigest()
_REVIEW_FIELDS = ("good", "priority", "next_7_days", "comment")


def _ensure_operation_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='vip_health_check_approval_operations'"
    ).fetchone()
    if row is None:
        conn.execute(_OPERATION_DDL)
    columns = tuple(
        (item[1], str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(vip_health_check_approval_operations)")
    )
    expected = (
        ("case_id", "TEXT", 1, 1), ("request_id", "TEXT", 1, 2),
        ("actor_id", "TEXT", 1, 0), ("payload_hash", "TEXT", 1, 0),
        ("expected_source_token", "TEXT", 1, 0),
        ("expected_review_version", "INTEGER", 1, 0),
        ("review_id", "TEXT", 1, 0), ("status", "TEXT", 1, 0),
        ("result_json", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0),
        ("completed_at", "TEXT", 1, 0),
    )
    foreign_keys = {
        (row[2], row[3], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(vip_health_check_approval_operations)")
    }
    if columns != expected or foreign_keys != {
        ("vip_health_check_cases", "case_id", "case_id"),
        ("vip_health_check_reviews", "review_id", "review_id"),
    }:
        raise sqlite3.IntegrityError("unsupported approval operation schema")


def _stored_result(raw: object) -> dict[str, object]:
    try:
        result = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError as exc:
        raise sqlite3.IntegrityError("invalid stored approval result") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"report_id", "delivery_key", "delivery_status", "created"}
        or not isinstance(result["report_id"], str)
        or not result["report_id"]
        or not isinstance(result["delivery_key"], str)
        or not result["delivery_key"]
        or result["delivery_status"] not in {"pending", "failed", "delivered"}
        or not isinstance(result["created"], bool)
    ):
        raise sqlite3.IntegrityError("invalid stored approval result")
    return {**result, "created": False}


def _report_from_saved_review(raw: object) -> dict[str, str]:
    try:
        review = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError as exc:
        raise ApprovalConflict("stored review is invalid") from exc
    if not isinstance(review, dict) or set(review) != set(_REVIEW_FIELDS):
        raise ApprovalConflict("stored review is incomplete")
    values: dict[str, str] = {}
    for field in _REVIEW_FIELDS:
        value = review[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ApprovalConflict("stored review is incomplete")
        values[field] = value
    return {
        "good": values["good"],
        "priority": values["priority"],
        "next_7_days": values["next_7_days"],
        "limitations": values["comment"],
    }


def create_health_check_approval_saver(
    database_path: str | Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    failure_injector: Callable[[str, sqlite3.Connection], None] | None = None,
):
    """Create a durable, CAS-fenced adapter around approve_health_check_review."""
    path = str(database_path)
    inject = failure_injector or (lambda _stage, _conn: None)
    with sqlite3.connect(path) as conn:
        configure_vip_health_check_connection(conn)
        ensure_dietitian_health_check_draft_schema(conn)
        _ensure_operation_schema(conn)
        conn.commit()

    def approve(
        case_id: str,
        expected_source_token: str,
        expected_review_version: int,
        request_id: str,
        actor_id: str,
    ) -> dict[str, object]:
        actor_id = str(actor_id or "").strip()
        if not actor_id:
            raise ApprovalConflict("verified actor is required")
        with sqlite3.connect(path, timeout=10.0) as conn:
            configure_vip_health_check_connection(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = conn.execute(
                    """SELECT actor_id,payload_hash,expected_source_token,
                              expected_review_version,status,result_json
                       FROM vip_health_check_approval_operations
                       WHERE case_id=? AND request_id=?""",
                    (case_id, request_id),
                ).fetchone()
                binding = (
                    actor_id, _PAYLOAD_HASH, expected_source_token,
                    expected_review_version,
                )
                if operation is not None:
                    if tuple(operation[:4]) != binding or operation[4] != "completed":
                        raise ApprovalConflict("request id binding conflict")
                    result = _stored_result(operation[5])
                    conn.rollback()
                    return result

                case = conn.execute(
                    "SELECT status,source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if case is None:
                    raise ApprovalNotFound("case not found")
                if case[0] not in {"ready_for_review", "needs_more_info"}:
                    raise ApprovalConflict("case is not approvable")
                current_token = health_check_source_token(
                    conn, case_id=case_id, manifest_hash=str(case[1])
                )
                if current_token != expected_source_token:
                    raise ApprovalConflict("source version is stale")
                approved_at = now()
                authoritative = probe_current_health_check_source_manifest(
                    conn, case_id=case_id, evaluated_at=approved_at
                )
                if (
                    authoritative["status"] != case[0]
                    or str(authoritative["source_manifest_hash"]) != str(case[1])
                ):
                    raise ApprovalConflict("canonical sources changed")

                review = conn.execute(
                    """SELECT review_id,review_version,status,review_json,source_manifest_hash
                       FROM vip_health_check_reviews WHERE case_id=?
                       ORDER BY review_version DESC LIMIT 1""",
                    (case_id,),
                ).fetchone()
                if (
                    review is None
                    or review[1] != expected_review_version
                    or review[2] != "draft"
                    or str(review[4]) != str(case[1])
                ):
                    raise ApprovalConflict("review version is stale")
                report = _report_from_saved_review(review[3])
                created_at = approved_at.isoformat()
                conn.execute(
                    """INSERT INTO vip_health_check_approval_operations
                       (case_id,request_id,actor_id,payload_hash,expected_source_token,
                        expected_review_version,review_id,status,result_json,created_at,completed_at)
                       VALUES (?,?,?,?,?,?,?,'pending','',?,'')""",
                    (
                        case_id, request_id, actor_id, _PAYLOAD_HASH,
                        expected_source_token, expected_review_version, review[0], created_at,
                    ),
                )
                inject("operation_reserved", conn)
                try:
                    result = approve_health_check_review(
                        conn,
                        case_id=case_id,
                        review_id=str(review[0]),
                        expected_version=expected_review_version,
                        approved_by=actor_id,
                        approved_at=approved_at,
                        report=report,
                    )
                except ValueError as exc:
                    raise ApprovalConflict("approval changed") from exc
                inject("domain_written", conn)
                result_json = json.dumps(
                    result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                changed = conn.execute(
                    """UPDATE vip_health_check_approval_operations
                       SET status='completed',result_json=?,completed_at=?
                       WHERE case_id=? AND request_id=? AND status='pending'""",
                    (result_json, created_at, case_id, request_id),
                )
                if changed.rowcount != 1:
                    raise sqlite3.IntegrityError("approval operation completion failed")
                inject("result_stored", conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    return approve
