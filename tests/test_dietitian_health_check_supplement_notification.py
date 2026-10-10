from datetime import datetime, timezone
import pathlib
import runpy
import sqlite3
import threading

import pytest

from dietitian_health_check_api import load_health_check_detail
from dietitian_health_check_supplement import create_health_check_supplement_saver
from vip_health_check import health_check_source_token
from dietitian_health_check_supplement_notification import (
    SupplementNotificationConflict,
    create_health_check_supplement_notification_service,
)


def _pending_request(tmp_path):
    fixture = runpy.run_path(str(pathlib.Path(__file__).with_name("test_dietitian_health_check_api.py")))
    path = fixture["_populated_db"](tmp_path)
    saver = create_health_check_supplement_saver(
        path, now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc)
    )
    with sqlite3.connect(path) as conn:
        manifest = conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()[0]
        token = health_check_source_token(conn, case_id="case-1", manifest_hash=manifest)
    saver(
        "case-1", "運動日前後資訊不足", "請補飲料與點心時間份量", token, 2,
        "more-info-1", "U12345678901234567890123456789012",
    )
    return path


def test_persisted_request_sends_flex_and_projects_notified_true(tmp_path):
    path = _pending_request(tmp_path)
    calls = []
    service = create_health_check_supplement_notification_service(
        path, sender=lambda recipient, message, retry_key: calls.append((recipient, message, retry_key))
    )

    result = service("case-1")

    assert result["status"] == "delivered"
    assert len(calls) == 1
    recipient, flex, retry_key = calls[0]
    assert recipient == "U11111111111111111111111111111111"
    assert flex["altText"] == "營養師請您補充三日飲食健檢資料"
    rendered = str(flex)
    assert "運動日前後資訊不足" in rendered
    assert "請補飲料與點心時間份量" in rendered
    assert retry_key
    with sqlite3.connect(path) as conn:
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail["status"] == "needs_more_info"
    assert detail["supplement_request"]["notification_status"] == "delivered"
    assert detail["supplement_request"]["notified"] is True


def test_failure_stays_not_sent_and_retry_reuses_key_without_duplicate_after_success(tmp_path):
    path = _pending_request(tmp_path)
    keys = []
    attempts = 0
    def sender(_recipient, _message, retry_key):
        nonlocal attempts
        attempts += 1
        keys.append(retry_key)
        if attempts == 1:
            raise RuntimeError("provider unavailable")
    service = create_health_check_supplement_notification_service(path, sender=sender)

    first = service("case-1")
    with sqlite3.connect(path) as conn:
        first_projection = load_health_check_detail(conn, case_id="case-1")["supplement_request"]
    second = service("case-1")
    replay = service("case-1")

    assert first["status"] == "not_sent"
    assert first_projection["notified"] is False
    assert second["status"] == replay["status"] == "delivered"
    assert keys[0] == keys[1]
    assert attempts == 2


def test_marker_failure_retries_with_same_provider_key(tmp_path):
    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TRIGGER reject_supplement_delivery_marker
            BEFORE UPDATE OF notification_status ON health_check_supplement_requests
            WHEN NEW.notification_status='delivered'
            BEGIN SELECT RAISE(ABORT, 'marker failure'); END""")
        conn.commit()
    keys = []
    service = create_health_check_supplement_notification_service(
        path, sender=lambda _recipient, _message, retry_key: keys.append(retry_key)
    )

    with pytest.raises(sqlite3.IntegrityError, match="marker failure"):
        service("case-1")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT notification_status FROM health_check_supplement_requests"
        ).fetchone() == ("not_sent",)
        conn.execute("DROP TRIGGER reject_supplement_delivery_marker")
        conn.commit()
    assert service("case-1")["status"] == "delivered"
    assert keys[0] == keys[1]


def test_terminal_or_superseded_request_is_not_sent(tmp_path):
    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE health_check_supplement_requests SET status='cancelled' WHERE case_id='case-1'")
        conn.execute("UPDATE vip_health_check_cases SET status='cancelled' WHERE case_id='case-1'")
        conn.commit()
    calls = []
    service = create_health_check_supplement_notification_service(
        path, sender=lambda *args: calls.append(args)
    )
    with pytest.raises(SupplementNotificationConflict, match="no current pending supplement request"):
        service("case-1")
    assert calls == []


def test_request_recipient_binding_blocks_case_owner_tamper_before_sender(tmp_path):
    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE vip_health_check_cases SET user_id=? WHERE case_id='case-1'",
            ("U99999999999999999999999999999999",),
        )
        conn.commit()
    calls = []
    service = create_health_check_supplement_notification_service(
        path, sender=lambda *args: calls.append(args)
    )

    with pytest.raises(SupplementNotificationConflict, match="recipient binding"):
        service("case-1")

    assert calls == []
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT recipient_user_id,notification_status "
            "FROM health_check_supplement_requests WHERE case_id='case-1'"
        ).fetchone() == ("U11111111111111111111111111111111", "not_sent")


def test_delayed_provider_ack_allows_other_connection_cancel_and_fences_late_ack(tmp_path):
    path = _pending_request(tmp_path)
    sender_entered = threading.Event()
    release_sender = threading.Event()
    outcome = {}

    def sender(_recipient, _message, _retry_key):
        sender_entered.set()
        assert release_sender.wait(timeout=5)

    service = create_health_check_supplement_notification_service(path, sender=sender)

    def run_notification():
        try:
            outcome["result"] = service("case-1")
        except BaseException as exc:  # surfaced in the test thread below
            outcome["error"] = exc

    worker = threading.Thread(target=run_notification)
    worker.start()
    assert sender_entered.wait(timeout=5)
    competing_error = None
    try:
        with sqlite3.connect(path, timeout=0.2) as conn:
            conn.execute(
                "UPDATE health_check_supplement_requests SET status='cancelled' "
                "WHERE case_id='case-1' AND status='pending_customer'"
            )
            columns = [
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info('health_check_supplement_requests')"
                )
            ]
            source = conn.execute(
                "SELECT * FROM health_check_supplement_requests WHERE case_id='case-1'"
            ).fetchone()
            replacement = dict(zip(columns, source))
            replacement.update(
                supplement_request_id="replacement-request",
                request_id="replacement-operation",
                status="pending_customer",
                notification_status="not_sent",
            )
            conn.execute(
                f"INSERT INTO health_check_supplement_requests ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(replacement[column] for column in columns),
            )
            conn.commit()
    except BaseException as exc:
        competing_error = exc
    finally:
        release_sender.set()
        worker.join(timeout=5)

    assert competing_error is None
    assert worker.is_alive() is False
    assert "error" not in outcome
    assert outcome["result"]["status"] == "accepted_stale"
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT supplement_request_id,status,notification_status,notification_generation "
            "FROM health_check_supplement_requests ORDER BY supplement_request_id"
        ).fetchall()
    assert rows[0] == ("replacement-request", "pending_customer", "not_sent", 1)
    assert rows[1][0].startswith("vhcs_")
    assert rows[1][1:] == ("cancelled", "not_sent", 1)
