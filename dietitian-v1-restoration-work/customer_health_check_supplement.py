"""Authorized customer submission of a completed health-check supplement request."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3

from dietitian_health_check_migration import require_canonical_table_contract
from dietitian_health_check_supplement import _initialize_schema as _initialize_request_schema
from vip_health_check import (
    configure_vip_health_check_connection,
    health_check_source_token,
    refresh_case_source_manifest,
)


class CustomerSupplementNotFound(ValueError):
    """No active supplement request belongs to the verified customer."""


class CustomerSupplementConflict(ValueError):
    """The request, source observation, or replay binding is stale."""


_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SOURCE_TOKEN = re.compile(r"[0-9a-f]{64}")
_ACTION_HASH = hashlib.sha256(
    json.dumps(
        {"action": "submit_health_check_supplement_completion_v1"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
_SUBMISSION_SCHEMA = """CREATE TABLE IF NOT EXISTS health_check_supplement_submissions (
    request_id TEXT PRIMARY KEY NOT NULL,
    actor_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    supplement_request_id TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    observed_source_token TEXT NOT NULL,
    result_status TEXT NOT NULL CHECK(result_status='ready_for_review'),
    submitted_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
    FOREIGN KEY(supplement_request_id)
        REFERENCES health_check_supplement_requests(supplement_request_id)
)"""


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CustomerSupplementConflict(f"invalid {label}")
    return value.strip()


def _ensure_submission_schema(conn: sqlite3.Connection) -> None:
    conn.execute("SAVEPOINT customer_supplement_submission_schema")
    try:
        conn.execute(_SUBMISSION_SCHEMA)
        require_canonical_table_contract(
            conn,
            table="health_check_supplement_submissions",
            table_ddl=_SUBMISSION_SCHEMA,
            error_label="customer supplement submission",
        )
        if conn.execute(
            "PRAGMA foreign_key_check('health_check_supplement_submissions')"
        ).fetchone() is not None:
            raise sqlite3.IntegrityError("invalid customer supplement submission foreign key")
        if conn.execute(
            """SELECT 1 FROM health_check_supplement_submissions
               WHERE typeof(request_id)<>'text' OR typeof(actor_id)<>'text'
                  OR typeof(case_id)<>'text' OR typeof(supplement_request_id)<>'text'
                  OR typeof(payload_hash)<>'text' OR typeof(observed_source_token)<>'text'
                  OR typeof(result_status)<>'text' OR result_status<>'ready_for_review'
                  OR typeof(submitted_at)<>'text' LIMIT 1"""
        ).fetchone() is not None:
            raise sqlite3.IntegrityError("invalid customer supplement submission rows")
        invalid_row = conn.execute(
            """SELECT 1
               FROM health_check_supplement_submissions AS e
               LEFT JOIN vip_health_check_cases AS c ON c.case_id=e.case_id
               LEFT JOIN health_check_supplement_requests AS s
                 ON s.supplement_request_id=e.supplement_request_id
               WHERE c.case_id IS NULL OR s.supplement_request_id IS NULL
                  OR s.case_id<>e.case_id OR c.user_id<>e.actor_id
                  OR s.recipient_user_id<>e.actor_id OR s.status<>'resolved'
               LIMIT 1"""
        ).fetchone()
        if invalid_row is not None:
            raise sqlite3.IntegrityError("invalid customer supplement submission rows")
        for stored in conn.execute(
            """SELECT request_id,actor_id,case_id,supplement_request_id,payload_hash,
                      observed_source_token,submitted_at
               FROM health_check_supplement_submissions"""
        ).fetchall():
            try:
                submitted_at = datetime.fromisoformat(stored[6])
            except (TypeError, ValueError) as exc:
                raise sqlite3.IntegrityError(
                    "invalid customer supplement submission rows"
                ) from exc
            if (
                _REQUEST_ID.fullmatch(stored[0]) is None
                or not stored[1].strip()
                or not stored[2].strip()
                or _REQUEST_ID.fullmatch(stored[3]) is None
                or stored[4] != _ACTION_HASH
                or _SOURCE_TOKEN.fullmatch(stored[5]) is None
                or submitted_at.tzinfo is None
            ):
                raise sqlite3.IntegrityError("invalid customer supplement submission rows")
        conn.execute("RELEASE SAVEPOINT customer_supplement_submission_schema")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT customer_supplement_submission_schema")
        conn.execute("RELEASE SAVEPOINT customer_supplement_submission_schema")
        raise


def ensure_customer_supplement_completion_schema(conn: sqlite3.Connection) -> None:
    """Install request prerequisites and completion schema in one caller-safe unit."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT customer_supplement_completion_initializer")
    try:
        _initialize_request_schema(conn)
        _ensure_submission_schema(conn)
        conn.execute("RELEASE SAVEPOINT customer_supplement_completion_initializer")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT customer_supplement_completion_initializer")
        conn.execute("RELEASE SAVEPOINT customer_supplement_completion_initializer")
        if owns_transaction:
            conn.rollback()
        raise


def create_customer_supplement_completion_saver(
    database_path: str | Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
):
    """Create a writer bound to one isolated SQLite database path."""
    path = str(database_path)
    with sqlite3.connect(path) as conn:
        configure_vip_health_check_connection(conn)
        ensure_customer_supplement_completion_schema(conn)
        conn.commit()

    def submit(
        actor_id: str,
        request_id: str,
        expected_supplement_request_id: str,
        expected_source_token: str,
    ) -> dict[str, object]:
        actor = _required(actor_id, "actor")
        operation_id = str(request_id or "")
        if _REQUEST_ID.fullmatch(operation_id) is None:
            raise CustomerSupplementConflict("invalid request id")
        expected_supplement = str(expected_supplement_request_id or "")
        if _REQUEST_ID.fullmatch(expected_supplement) is None:
            raise CustomerSupplementConflict("invalid supplement request id")
        expected_source = str(expected_source_token or "")
        if _SOURCE_TOKEN.fullmatch(expected_source) is None:
            raise CustomerSupplementConflict("invalid expected source token")
        submitted_at = now().isoformat()

        with sqlite3.connect(path, timeout=10.0) as conn:
            configure_vip_health_check_connection(conn)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute(
                    """SELECT e.actor_id,e.case_id,e.supplement_request_id,e.payload_hash,
                              e.observed_source_token,e.result_status,
                              s.status AS supplement_status,s.expected_source_token,
                              c.user_id,c.status AS case_status
                       FROM health_check_supplement_submissions e
                       JOIN health_check_supplement_requests s
                         ON s.supplement_request_id=e.supplement_request_id
                       JOIN vip_health_check_cases c ON c.case_id=e.case_id
                       WHERE e.request_id=?""",
                    (operation_id,),
                ).fetchone()
                if prior is not None:
                    if (
                        prior["actor_id"] != actor
                        or prior["payload_hash"] != _ACTION_HASH
                        or prior["user_id"] != actor
                        or prior["supplement_request_id"] != expected_supplement
                        or prior["expected_source_token"] != expected_source
                    ):
                        raise CustomerSupplementConflict("request id binding conflict")
                    if (
                        prior["supplement_status"] != "resolved"
                        or prior["case_status"] != "ready_for_review"
                        or prior["result_status"] != "ready_for_review"
                    ):
                        raise CustomerSupplementConflict("submission is no longer current")
                    conn.rollback()
                    return {
                        "status": "ready_for_review",
                        "submitted": True,
                        "replayed": True,
                    }

                rows = conn.execute(
                    """SELECT s.supplement_request_id,s.case_id,s.expected_source_token,
                              s.expected_source_content_digest,
                              s.expected_review_version,s.review_id,s.recipient_user_id,
                              s.status AS supplement_status,c.status AS case_status,
                              c.source_manifest_hash,r.review_version,r.status AS review_status,
                              r.case_id AS review_case_id
                       FROM health_check_supplement_requests s
                       JOIN vip_health_check_cases c ON c.case_id=s.case_id
                       JOIN vip_health_check_reviews r ON r.review_id=s.review_id
                       WHERE c.user_id=? AND s.status='pending_customer'""",
                    (actor,),
                ).fetchall()
                if not rows:
                    raise CustomerSupplementNotFound("active request not found")
                if len(rows) != 1:
                    raise CustomerSupplementConflict("multiple active requests")
                row = rows[0]
                if (
                    row["supplement_request_id"] != expected_supplement
                    or row["expected_source_token"] != expected_source
                    or row["case_status"] != "needs_more_info"
                    or not str(row["recipient_user_id"] or "").strip()
                    or row["recipient_user_id"] != actor
                    or row["review_case_id"] != row["case_id"]
                    or row["review_version"] != row["expected_review_version"]
                    or row["review_status"] != "draft"
                ):
                    raise CustomerSupplementConflict("supplement request binding conflict")
                refreshed = refresh_case_source_manifest(
                    conn, case_id=row["case_id"], evaluated_at=now()
                )
                observed_token = health_check_source_token(
                    conn,
                    case_id=row["case_id"],
                    manifest_hash=str(refreshed["source_manifest_hash"]),
                )
                baseline_digest = str(row["expected_source_content_digest"] or "")
                if _SOURCE_TOKEN.fullmatch(baseline_digest) is None:
                    raise CustomerSupplementConflict("no trusted request-time source baseline")
                if str(refreshed["source_content_digest"]) == baseline_digest:
                    raise CustomerSupplementConflict("no verified source change")
                if refreshed["status"] != "needs_more_info" or int(refreshed["valid_day_count"]) < 3:
                    raise CustomerSupplementConflict("sources are not review-ready")

                changed_request = conn.execute(
                    """UPDATE health_check_supplement_requests
                       SET status='resolved',resolved_at=?
                       WHERE supplement_request_id=? AND case_id=?
                         AND expected_source_token=? AND expected_review_version=?
                         AND review_id=? AND recipient_user_id=?
                         AND status='pending_customer'""",
                    (
                        submitted_at, row["supplement_request_id"], row["case_id"],
                        row["expected_source_token"], row["expected_review_version"],
                        row["review_id"], actor,
                    ),
                )
                if changed_request.rowcount != 1:
                    raise CustomerSupplementConflict("supplement request changed")
                changed_case = conn.execute(
                    """UPDATE vip_health_check_cases
                       SET status='ready_for_review',updated_at=?
                       WHERE case_id=? AND user_id=? AND status='needs_more_info'
                         AND source_manifest_hash=?""",
                    (submitted_at, row["case_id"], actor, refreshed["source_manifest_hash"]),
                )
                if changed_case.rowcount != 1:
                    raise CustomerSupplementConflict("case changed")
                conn.execute(
                    """INSERT INTO health_check_supplement_submissions
                       (request_id,actor_id,case_id,supplement_request_id,payload_hash,
                        observed_source_token,result_status,submitted_at)
                       VALUES (?,?,?,?,?,?,'ready_for_review',?)""",
                    (
                        operation_id, actor, row["case_id"], row["supplement_request_id"],
                        _ACTION_HASH, observed_token, submitted_at,
                    ),
                )
                conn.execute(
                    """INSERT INTO vip_health_check_audit_log
                       (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
                       VALUES (?,'customer',?,'needs_more_info','ready_for_review',
                               'supplement_submitted',?)""",
                    (row["case_id"], actor, submitted_at),
                )
                conn.commit()
                return {
                    "status": "ready_for_review",
                    "submitted": True,
                    "replayed": False,
                }
            except Exception:
                conn.rollback()
                raise

    return submit
