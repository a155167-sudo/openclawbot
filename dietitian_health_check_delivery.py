"""Single-attempt delivery service for one persisted approved health-check report.

The service owns no approval input.  It reloads the immutable approved report and
canonical recipient from SQLite, holds a host-local operation lock through the
external call, and records the result through the existing health-check domain.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os
import sqlite3
import uuid

from vip_health_check import configure_vip_health_check_connection


class DeliveryConflict(ValueError):
    """Persisted delivery evidence is missing, malformed, or incoherent."""


_REPORT_FIELDS = ("good", "priority", "next_7_days", "limitations")
_REPORT_LABELS = (
    "做得好的地方",
    "目前優先事項",
    "接下來 7 天",
    "限制與提醒",
)
DELIVERY_RECOVERY_FLAG = "DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED"


def load_health_check_delivery_recovery_enabled(environ: Mapping[str, str]) -> bool:
    """Load the dedicated trigger flag; missing is off and unknown values fail closed."""
    raw = str(environ.get(DELIVERY_RECOVERY_FLAG) or "")
    if raw == "":
        return False
    if raw not in {"true", "false"}:
        raise ValueError(f"{DELIVERY_RECOVERY_FLAG} must be true or false")
    return raw == "true"


def _approved_report(raw: object) -> dict[str, str]:
    try:
        report = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError as exc:
        raise DeliveryConflict("approved report is invalid") from exc
    if not isinstance(report, dict) or set(report) != set(_REPORT_FIELDS):
        raise DeliveryConflict("approved report must contain exactly four fields")
    result: dict[str, str] = {}
    for field in _REPORT_FIELDS:
        value = report[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise DeliveryConflict("approved report is invalid")
        result[field] = value
    return result


def render_approved_health_check_flex(report: dict[str, str]) -> dict[str, object]:
    """Render only the four persisted approved fields as a LINE Flex bubble."""
    exact = _approved_report(json.dumps(report, ensure_ascii=False))
    sections = []
    for field, label in zip(_REPORT_FIELDS, _REPORT_LABELS, strict=True):
        sections.append(
            {
                "type": "box",
                "layout": "vertical",
                "spacing": "xs",
                "contents": [
                    {"type": "text", "text": label, "size": "sm", "weight": "bold", "color": "#0F766E"},
                    {"type": "text", "text": exact[field], "size": "sm", "wrap": True, "color": "#1F2937"},
                ],
            }
        )
    return {
        "type": "flex",
        "altText": "營養師三日飲食健檢報告",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "lg",
                "contents": sections,
            },
        },
    }


def _acquire_host_lock(database_path: str, delivery_key: str) -> int | None:
    lock_dir = Path(database_path).resolve().parent / ".vip_health_check_delivery_locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_name = hashlib.sha256(delivery_key.encode("utf-8")).hexdigest() + ".lock"
    fd = os.open(lock_dir / lock_name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _release_host_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _load_delivery(conn: sqlite3.Connection, delivery_key: str) -> dict[str, object]:
    rows = conn.execute(
        """SELECT d.delivery_id,d.report_id,d.user_id,d.status,d.attempts,
                  r.case_id,r.review_id,r.report_kind,r.report_version,r.report_json,
                  r.source_manifest_hash,v.status,v.source_manifest_hash,
                  v.approved_by,v.approved_at,c.user_id,c.status,c.source_manifest_hash
           FROM vip_health_check_deliveries d
           JOIN vip_health_check_reports r ON r.report_id=d.report_id
           JOIN vip_health_check_reviews v
             ON v.review_id=r.review_id AND v.case_id=r.case_id
           JOIN vip_health_check_cases c ON c.case_id=r.case_id
           WHERE d.delivery_key=?""",
        (delivery_key,),
    ).fetchall()
    if len(rows) != 1:
        raise DeliveryConflict("delivery intent is missing or ambiguous")
    row = rows[0]
    status_pair = (str(row[16]), str(row[3]))
    if (
        row[7] != "baseline_3day"
        or row[11] != "approved"
        or not str(row[13] or "").strip()
        or not str(row[14] or "").strip()
        or row[2] != row[15]
        or not row[2]
        or str(row[10]) != str(row[12])
        or str(row[10]) != str(row[17])
        or status_pair not in {
            ("approved_pending_delivery", "pending"),
            ("delivery_failed", "failed"),
            ("delivered", "delivered"),
        }
    ):
        raise DeliveryConflict("approved delivery evidence is incoherent")
    return {
        "delivery_id": str(row[0]),
        "report_id": str(row[1]),
        "recipient": str(row[2]),
        "status": str(row[3]),
        "attempts": int(row[4]),
        "case_id": str(row[5]),
        "review_id": str(row[6]),
        "report_version": int(row[8]),
        "report_json": str(row[9]),
        "manifest_hash": str(row[10]),
        "approved_by": str(row[13]),
        "approved_at": str(row[14]),
        "report": _approved_report(row[9]),
    }


def _claim_delivery(
    conn: sqlite3.Connection, delivery_key: str
) -> dict[str, object]:
    """Claim one generation while all authoritative bindings are write-locked."""
    claimed = _load_delivery(conn, delivery_key)
    if claimed["status"] == "delivered":
        return claimed
    generation = int(claimed["attempts"]) + 1
    changed = conn.execute(
        """UPDATE vip_health_check_deliveries
           SET attempts=?
           WHERE delivery_id=? AND delivery_key=? AND report_id=? AND user_id=?
             AND status=? AND attempts=?""",
        (
            generation,
            claimed["delivery_id"],
            delivery_key,
            claimed["report_id"],
            claimed["recipient"],
            claimed["status"],
            claimed["attempts"],
        ),
    )
    if changed.rowcount != 1:
        raise DeliveryConflict("delivery changed before provider call")
    claimed["attempts"] = generation
    return claimed


def _record_claimed_delivery(
    conn: sqlite3.Connection,
    *,
    delivery_key: str,
    claimed: dict[str, object],
    succeeded: bool,
    error: str,
    attempted_at: datetime,
) -> dict[str, object] | None:
    """Finish only the exact still-current generation; return None on a stale ack."""
    attempted_text = attempted_at.isoformat(timespec="seconds")
    expected_case_status = (
        "approved_pending_delivery" if claimed["status"] == "pending" else "delivery_failed"
    )
    requested_status = "delivered" if succeeded else "failed"
    requested_error = "" if succeeded else str(error or "")[:1000]
    delivered_at = attempted_text if succeeded else ""
    changed = conn.execute(
        """UPDATE vip_health_check_deliveries
           SET status=?,last_error=?,delivered_at=?
           WHERE delivery_id=? AND delivery_key=? AND report_id=? AND user_id=?
             AND status=? AND attempts=?
             AND EXISTS (
               SELECT 1
               FROM vip_health_check_reports r
               JOIN vip_health_check_reviews v
                 ON v.review_id=r.review_id AND v.case_id=r.case_id
               JOIN vip_health_check_cases c ON c.case_id=r.case_id
               WHERE r.report_id=? AND r.case_id=? AND r.review_id=?
                 AND r.report_kind='baseline_3day' AND r.report_version=?
                 AND r.report_json=? AND r.source_manifest_hash=?
                 AND v.status='approved' AND v.source_manifest_hash=?
                 AND v.approved_by=? AND v.approved_at=?
                 AND c.user_id=? AND c.status=? AND c.source_manifest_hash=?
             )""",
        (
            requested_status,
            requested_error,
            delivered_at,
            claimed["delivery_id"],
            delivery_key,
            claimed["report_id"],
            claimed["recipient"],
            claimed["status"],
            claimed["attempts"],
            claimed["report_id"],
            claimed["case_id"],
            claimed["review_id"],
            claimed["report_version"],
            claimed["report_json"],
            claimed["manifest_hash"],
            claimed["manifest_hash"],
            claimed["approved_by"],
            claimed["approved_at"],
            claimed["recipient"],
            expected_case_status,
            claimed["manifest_hash"],
        ),
    )
    if changed.rowcount != 1:
        return None
    next_case_status = "delivered" if succeeded else "delivery_failed"
    changed = conn.execute(
        """UPDATE vip_health_check_cases
           SET status=?,report_published_at=?,updated_at=?
           WHERE case_id=? AND user_id=? AND status=? AND source_manifest_hash=?""",
        (
            next_case_status,
            delivered_at,
            attempted_text,
            claimed["case_id"],
            claimed["recipient"],
            expected_case_status,
            claimed["manifest_hash"],
        ),
    )
    if changed.rowcount != 1:
        raise DeliveryConflict("case changed while recording delivery")
    conn.execute(
        """INSERT INTO vip_health_check_audit_log
           (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
           VALUES (?,'system','line_delivery',?,?,?,?)""",
        (
            claimed["case_id"],
            expected_case_status,
            next_case_status,
            "delivery_succeeded" if succeeded else "delivery_failed",
            attempted_text,
        ),
    )
    return {
        "report_id": claimed["report_id"],
        "status": requested_status,
        "attempts": claimed["attempts"],
    }


def _cleanup_result(cleanup: Callable[[str], object] | None, case_id: str) -> str:
    if cleanup is None:
        return "not_configured"
    try:
        result = cleanup(case_id)
    except Exception:
        return "retry_pending"
    if not isinstance(result, dict):
        return "completed" if result is None else "retry_pending"
    expected = {"deleted", "missing", "blocked", "retry_pending"}
    if set(result) != expected:
        return "retry_pending"
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in result.values()):
        return "retry_pending"
    return (
        "retry_pending"
        if result["blocked"] > 0 or result["retry_pending"] > 0
        else "completed"
    )


def create_health_check_delivery_service(
    database_path: str | Path,
    *,
    sender: Callable[[str, dict[str, object], str], None],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    cleanup: Callable[[str], object] | None = None,
):
    """Return an executable one-operation delivery entry point.

    ``sender`` is injected and receives ``(canonical_recipient, flex, retry_key)``.
    The stable retry key is derived only from the persisted delivery operation key.
    """
    path = str(database_path)

    def deliver(delivery_key: str) -> dict[str, object]:
        key = str(delivery_key or "").strip()
        if not key:
            raise DeliveryConflict("delivery key is required")
        lock_fd = _acquire_host_lock(path, key)
        if lock_fd is None:
            return {"delivery_key": key, "status": "in_progress"}
        try:
            with sqlite3.connect(path, timeout=10.0) as conn:
                configure_vip_health_check_connection(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    claimed = _claim_delivery(conn, key)
                    if claimed["status"] == "delivered":
                        conn.rollback()
                        cleanup_status = _cleanup_result(cleanup, str(claimed["case_id"]))
                        return {
                            "delivery_key": key,
                            "report_id": claimed["report_id"],
                            "status": "delivered",
                            "attempts": claimed["attempts"],
                            "cleanup": cleanup_status,
                        }
                    message = render_approved_health_check_flex(claimed["report"])
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
            try:
                sender(str(claimed["recipient"]), message, retry_key)
            except Exception as exc:
                with sqlite3.connect(path, timeout=10.0) as conn:
                    configure_vip_health_check_connection(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        recorded = _record_claimed_delivery(
                            conn,
                            delivery_key=key,
                            claimed=claimed,
                            succeeded=False,
                            error=str(exc),
                            attempted_at=now(),
                        )
                        if recorded is None:
                            conn.rollback()
                            return {
                                "delivery_key": key,
                                "report_id": claimed["report_id"],
                                "status": "failed_stale",
                                "attempts": claimed["attempts"],
                                "provider_accepted": False,
                                "cleanup": "not_applicable",
                            }
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                return {
                    "delivery_key": key,
                    "report_id": recorded["report_id"],
                    "status": recorded["status"],
                    "attempts": recorded["attempts"],
                    "cleanup": "not_applicable",
                }
            with sqlite3.connect(path, timeout=10.0) as conn:
                configure_vip_health_check_connection(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    recorded = _record_claimed_delivery(
                        conn,
                        delivery_key=key,
                        claimed=claimed,
                        succeeded=True,
                        error="",
                        attempted_at=now(),
                    )
                    if recorded is None:
                        conn.rollback()
                        return {
                            "delivery_key": key,
                            "report_id": claimed["report_id"],
                            "status": "accepted_stale",
                            "attempts": claimed["attempts"],
                            "provider_accepted": True,
                            "cleanup": "not_applicable",
                        }
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            cleanup_status = _cleanup_result(cleanup, str(claimed["case_id"]))
            return {
                "delivery_key": key,
                "report_id": recorded["report_id"],
                "status": recorded["status"],
                "attempts": recorded["attempts"],
                "cleanup": cleanup_status,
            }
        finally:
            _release_host_lock(lock_fd)

    return deliver
