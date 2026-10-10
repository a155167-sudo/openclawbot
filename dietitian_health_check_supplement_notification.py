"""Safe single-attempt LINE notification for a persisted supplement request."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import fcntl
import hashlib
import os
import sqlite3
import uuid

from vip_health_check import configure_vip_health_check_connection


class SupplementNotificationConflict(ValueError):
    """The persisted request is absent, terminal, superseded, or incoherent."""


def render_health_check_supplement_flex(reason: str, required_content: str) -> dict[str, object]:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise SupplementNotificationConflict("invalid persisted reason")
    if not isinstance(required_content, str) or not required_content.strip() or len(required_content) > 4000:
        raise SupplementNotificationConflict("invalid persisted required content")
    return {
        "type": "flex",
        "altText": "營養師請您補充三日飲食健檢資料",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "lg",
                "contents": [
                    {"type": "text", "text": "三日飲食健檢需要補充資料", "weight": "bold", "size": "lg", "wrap": True},
                    {"type": "text", "text": "補件理由", "weight": "bold", "size": "sm", "color": "#0F766E"},
                    {"type": "text", "text": reason, "size": "sm", "wrap": True},
                    {"type": "text", "text": "請補充", "weight": "bold", "size": "sm", "color": "#0F766E"},
                    {"type": "text", "text": required_content, "size": "sm", "wrap": True},
                    {"type": "text", "text": "補充完成後請直接在 LINE 回覆；案件會維持待補件，直到資料確認完成。", "size": "xs", "wrap": True, "color": "#6B7280"},
                ],
            },
        },
    }


def _acquire_lock(database_path: str, request_id: str) -> int | None:
    directory = Path(database_path).resolve().parent / ".vip_health_check_supplement_notification_locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    leaf = hashlib.sha256(request_id.encode("utf-8")).hexdigest() + ".lock"
    fd = os.open(directory / leaf, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def create_health_check_supplement_notification_service(
    database_path: str | Path,
    *,
    sender: Callable[[str, dict[str, object], str], None],
):
    """Notify from an immutable recipient binding using short fenced transactions."""
    path = str(database_path)

    def notify(case_id: str) -> dict[str, object]:
        case_key = str(case_id or "").strip()
        if not case_key:
            raise SupplementNotificationConflict("case id is required")
        with sqlite3.connect(path, timeout=10.0) as lookup:
            configure_vip_health_check_connection(lookup)
            candidates = lookup.execute(
                """SELECT supplement_request_id FROM health_check_supplement_requests
                   WHERE case_id=? AND status='pending_customer'""", (case_key,)
            ).fetchall()
        if len(candidates) != 1:
            raise SupplementNotificationConflict("no current pending supplement request")
        supplement_id = str(candidates[0][0])
        lock_fd = _acquire_lock(path, supplement_id)
        if lock_fd is None:
            return {"case_id": case_key, "status": "in_progress"}
        try:
            # Short transaction 1: validate all authoritative bindings and claim
            # one opaque generation. No provider/network work is allowed here.
            with sqlite3.connect(path, timeout=10.0) as conn:
                configure_vip_health_check_connection(conn)
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                try:
                    rows = conn.execute(
                        """SELECT s.supplement_request_id,s.request_id,s.actor_id,
                                  s.payload_hash,s.expected_source_token,
                                  s.expected_review_version,s.review_id,s.reason,
                                  s.required_content,s.status,s.notification_status,
                                  s.recipient_user_id,s.notification_generation,
                                  c.user_id,c.status AS case_status
                           FROM health_check_supplement_requests s
                           JOIN vip_health_check_cases c ON c.case_id=s.case_id
                           WHERE s.case_id=? AND s.supplement_request_id=?""",
                        (case_key, supplement_id),
                    ).fetchall()
                    if len(rows) != 1:
                        raise SupplementNotificationConflict("request changed")
                    row = rows[0]
                    if row["status"] != "pending_customer" or row["case_status"] != "needs_more_info":
                        raise SupplementNotificationConflict("request is no longer current")
                    if row["notification_status"] == "delivered":
                        conn.rollback()
                        return {"case_id": case_key, "supplement_request_id": supplement_id, "status": "delivered"}
                    recipient = str(row["recipient_user_id"] or "").strip()
                    if not recipient or recipient != str(row["user_id"] or "").strip():
                        raise SupplementNotificationConflict("recipient binding is incoherent")
                    if row["notification_status"] != "not_sent":
                        raise SupplementNotificationConflict("notification evidence is incoherent")
                    message = render_health_check_supplement_flex(row["reason"], row["required_content"])
                    generation = int(row["notification_generation"]) + 1
                    changed = conn.execute(
                        """UPDATE health_check_supplement_requests
                           SET notification_generation=?
                           WHERE supplement_request_id=? AND case_id=?
                             AND request_id=? AND actor_id=? AND payload_hash=?
                             AND expected_source_token=? AND expected_review_version=?
                             AND review_id=? AND reason=? AND required_content=?
                             AND recipient_user_id=? AND status='pending_customer'
                             AND notification_status='not_sent'
                             AND notification_generation=?
                             AND EXISTS (SELECT 1 FROM vip_health_check_cases c
                                         WHERE c.case_id=? AND c.user_id=?
                                           AND c.status='needs_more_info')""",
                        (
                            generation, supplement_id, case_key, row["request_id"],
                            row["actor_id"], row["payload_hash"],
                            row["expected_source_token"], row["expected_review_version"],
                            row["review_id"], row["reason"], row["required_content"],
                            recipient, row["notification_generation"], case_key, recipient,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise SupplementNotificationConflict("request changed before provider call")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL, "health-check-supplement:" + supplement_id))
            try:
                sender(recipient, message, retry_key)
            except Exception:
                return {"case_id": case_key, "supplement_request_id": supplement_id, "status": "not_sent"}

            # Short transaction 2: an accepted late response is historical fact,
            # but only the exact claimed request may receive the delivery marker.
            with sqlite3.connect(path, timeout=10.0) as conn:
                configure_vip_health_check_connection(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    changed = conn.execute(
                        """UPDATE health_check_supplement_requests
                           SET notification_status='delivered'
                           WHERE supplement_request_id=? AND case_id=?
                             AND recipient_user_id=? AND status='pending_customer'
                             AND notification_status='not_sent'
                             AND notification_generation=?
                             AND EXISTS (SELECT 1 FROM vip_health_check_cases c
                                         WHERE c.case_id=? AND c.user_id=?
                                           AND c.status='needs_more_info')""",
                        (supplement_id, case_key, recipient, generation, case_key, recipient),
                    )
                    if changed.rowcount != 1:
                        conn.rollback()
                        return {
                            "case_id": case_key,
                            "supplement_request_id": supplement_id,
                            "status": "accepted_stale",
                            "provider_accepted": True,
                        }
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return {"case_id": case_key, "supplement_request_id": supplement_id, "status": "delivered"}
        finally:
            _release_lock(lock_fd)

    return notify
