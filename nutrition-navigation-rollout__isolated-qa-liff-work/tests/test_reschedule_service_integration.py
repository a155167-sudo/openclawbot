from datetime import timedelta
import sqlite3

import pytest

from gspread_pair_reschedule_adapter import GspreadPairRescheduleAdapter, MASTER_API_HEADERS
from pair_reschedule_coordinator import verify_admin_context
from reschedule_service_integration import (
    RescheduleAuthorizationError,
    RescheduleFeatureUnavailable,
    RescheduleRequestConflict,
    approve_customer_pair_reschedule,
    list_pending_admin_customer_pair_reschedules,
    submit_customer_pair_reschedule,
    submit_customer_pair_reschedule_pending,
    verify_customer_reschedule_context,
    _ensure_request_schema,
)
from test_pair_reschedule_coordinator import (
    ADMIN,
    NOW,
    OWNER,
    SOURCE,
    TARGET,
    old_source_row,
    open_db,
)
from test_gspread_pair_reschedule_adapter import Spreadsheet, Worksheet, master_row


def sheet_fixture():
    schedule = Worksheet(
        "sheet-1", 17,
        [["profile"], old_source_row() + ["dispatch-old", "1", "order-1-v1"], ["tracking"]],
    )
    master = Worksheet(
        "Master_API_View", 202,
        [MASTER_API_HEADERS, master_row(SOURCE, lunch="午餐A", dinner="晚餐B", workout="長跑")],
    )
    book = Spreadsheet("book-1", [schedule, master])
    adapter = GspreadPairRescheduleAdapter(book, schedule, master, workbook_id="book-1")
    return adapter, book


def test_feature_default_off_is_precisely_unavailable_and_writes_nothing(tmp_path):
    conn, _ = open_db(tmp_path / "off.sqlite3")
    before = conn.total_changes
    context = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)

    with pytest.raises(RescheduleFeatureUnavailable, match="not enabled"):
        submit_customer_pair_reschedule(
            conn, context=context, source_date=SOURCE, target_date=TARGET,
            request_id="customer-request-1", now=NOW,
        )
    sheet, book = sheet_fixture()
    with pytest.raises(RescheduleFeatureUnavailable, match="not enabled"):
        approve_customer_pair_reschedule(
            conn, sheet, request_id="customer-request-1",
            admin_context=verify_admin_context(conn, ADMIN), now=NOW,
        )

    assert conn.total_changes == before
    assert book.batch_calls == []
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='customer_pair_reschedule_requests'"
    ).fetchone() is None


def test_verified_customer_request_then_verified_admin_handler_runs_real_coordinator(tmp_path):
    conn, _ = open_db(tmp_path / "flow.sqlite3")
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    request = submit_customer_pair_reschedule(
        conn, feature_enabled=True, context=customer, source_date=SOURCE,
        target_date=TARGET, request_id="customer-request-1", now=NOW,
    )
    assert request.status == "pending_admin"

    sheet, book = sheet_fixture()
    result = approve_customer_pair_reschedule(
        conn, sheet, feature_enabled=True, request_id="customer-request-1",
        admin_context=verify_admin_context(conn, ADMIN), now=NOW,
    )

    assert result.status == "confirmed"
    assert len(book.batch_calls) == 1
    assert tuple(conn.execute(
        "SELECT status,operation_id FROM customer_pair_reschedule_requests WHERE request_id=?",
        ("customer-request-1",),
    ).fetchone()) == ("confirmed", result.operation_id)
    replay = approve_customer_pair_reschedule(
        conn, sheet, feature_enabled=True, request_id="customer-request-1",
        admin_context=verify_admin_context(conn, ADMIN), now=NOW,
    )
    assert replay == result
    assert len(book.batch_calls) == 1


def test_pending_request_notifies_bound_admin_after_commit_with_safe_receipt(tmp_path):
    conn, _ = open_db(tmp_path / "notify.sqlite3")
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    calls = []

    def sender(recipient, text, retry_key):
        stored = conn.execute(
            "SELECT status FROM customer_pair_reschedule_requests WHERE request_id=?",
            ("customer-request-1",),
        ).fetchone()
        assert stored[0] == "pending_admin"
        calls.append((recipient, text, retry_key))

    result = submit_customer_pair_reschedule(
        conn,
        feature_enabled=True,
        notification_enabled=True,
        notification_sender=sender,
        context=customer,
        source_date=SOURCE,
        target_date=TARGET,
        request_id="customer-request-1",
        now=NOW,
    )

    assert result.status == "pending_admin"
    assert result.admin_notification_status == "delivered"
    assert len(calls) == 1
    recipient, text, retry_key = calls[0]
    assert recipient == ADMIN
    assert retry_key
    for needle in (
        "request_id: customer-request-1",
        "order_id: 1",
        f"source: {SOURCE}",
        f"target: {TARGET}",
        "status: pending_admin",
        "created: 2026-09-26T07:59:00+08:00",
        "expires: 2026-09-26T08:14:00+08:00",
        "source_meal: lunch=午餐A; dinner=晚餐B",
    ):
        assert needle in text
    assert OWNER not in text


def test_pending_notification_is_staging_gated_and_readback_surfaces_state(tmp_path):
    conn, _ = open_db(tmp_path / "deferred.sqlite3")
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    calls = []

    result = submit_customer_pair_reschedule_pending(
        conn,
        feature_enabled=True,
        notification_enabled=False,
        notification_sender=lambda *_args: calls.append(_args),
        context=customer,
        source_date=SOURCE,
        target_date=TARGET,
        request_id="customer-request-1",
        now=NOW,
    )

    assert calls == []
    assert result.admin_notification_status == "deferred"
    rows = list_pending_admin_customer_pair_reschedules(
        conn,
        admin_context=verify_admin_context(conn, ADMIN),
        feature_enabled=True,
    )
    assert rows[0].admin_notification_status == "deferred"
    assert "disabled" in rows[0].admin_notification_last_error
    assert rows[0].admin_notification_attempted_at == ""
    assert not hasattr(rows[0], "owner_user_id")


def test_unknown_notification_outcome_is_not_recorded_as_success(tmp_path):
    conn, _ = open_db(tmp_path / "unknown.sqlite3")
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)

    def sender(_recipient, _text, _retry_key):
        raise TimeoutError("line timeout")

    result = submit_customer_pair_reschedule(
        conn,
        feature_enabled=True,
        notification_enabled=True,
        notification_sender=sender,
        context=customer,
        source_date=SOURCE,
        target_date=TARGET,
        request_id="customer-request-1",
        now=NOW,
    )

    assert result.status == "pending_admin"
    assert result.admin_notification_status == "outcome_unknown"
    assert "line timeout" in result.admin_notification_last_error
    stored = conn.execute(
        """SELECT status,admin_notification_status,admin_notification_last_error
             FROM customer_pair_reschedule_requests WHERE request_id=?""",
        ("customer-request-1",),
    ).fetchone()
    assert tuple(stored) == (
        "pending_admin",
        "outcome_unknown",
        "TimeoutError: line timeout",
    )


def test_notification_is_not_attempted_when_pending_commit_fails(tmp_path):
    conn, _ = open_db(tmp_path / "commit-fails.sqlite3")
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    _ensure_request_schema(conn)
    conn.execute(
        """CREATE TRIGGER reject_pending_request
           BEFORE INSERT ON customer_pair_reschedule_requests
           BEGIN SELECT RAISE(ABORT,'reject pending insert'); END"""
    )
    calls = []

    with pytest.raises(sqlite3.IntegrityError, match="reject pending insert"):
        submit_customer_pair_reschedule(
            conn,
            feature_enabled=True,
            notification_enabled=True,
            notification_sender=lambda *_args: calls.append(_args),
            context=customer,
            source_date=SOURCE,
            target_date=TARGET,
            request_id="customer-request-1",
            now=NOW,
        )

    assert calls == []
    assert conn.execute(
        "SELECT 1 FROM customer_pair_reschedule_requests WHERE request_id=?",
        ("customer-request-1",),
    ).fetchone() is None


def test_verified_admin_can_read_pending_requests_without_owner_uid_leakage(tmp_path):
    conn, _ = open_db(tmp_path / "readback.sqlite3")
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    submit_customer_pair_reschedule(
        conn, feature_enabled=True, context=customer, source_date=SOURCE,
        target_date=TARGET, request_id="customer-request-1", now=NOW,
    )
    conn.execute(
        """INSERT INTO customer_pair_reschedule_requests
           (request_id,order_id,owner_user_id,source_date,target_date,status,
            operation_id,created_at,expires_at)
           VALUES(?,?,?,?,?,'confirmed','op-confirmed',?,?)""",
        (
            "confirmed-hidden", 1, OWNER, SOURCE, "2026-09-29",
            "2026-09-26T08:01:00+08:00", "2026-09-26T08:16:00+08:00",
        ),
    )
    conn.commit()

    rows = list_pending_admin_customer_pair_reschedules(
        conn,
        admin_context=verify_admin_context(conn, ADMIN),
        feature_enabled=True,
    )

    assert rows == [
        rows[0].__class__(
            request_id="customer-request-1",
            order_id=1,
            source_date=SOURCE,
            target_date=TARGET,
            status="pending_admin",
            created_at="2026-09-26T07:59:00+08:00",
            expires_at="2026-09-26T08:14:00+08:00",
            admin_notification_status="deferred",
            admin_notification_last_error="admin notification sender disabled",
            admin_notification_attempted_at="",
        )
    ]
    assert not hasattr(rows[0], "owner_user_id")
    with pytest.raises(RescheduleAuthorizationError, match="admin"):
        list_pending_admin_customer_pair_reschedules(
            conn,
            admin_context=verify_admin_context(conn, ADMIN).__class__(
                "U" + "2" * 32,
                "U" + "2" * 32,
            ),
            feature_enabled=True,
        )


def test_admin_readback_tolerates_legacy_schema_without_migration(tmp_path):
    conn, _ = open_db(tmp_path / "legacy-readback.sqlite3")
    conn.execute(
        """CREATE TABLE customer_pair_reschedule_requests (
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
    )
    conn.execute(
        """INSERT INTO customer_pair_reschedule_requests
           (request_id,order_id,owner_user_id,source_date,target_date,status,
            operation_id,created_at,expires_at)
           VALUES('legacy-request',1,?,?,?,'pending_admin',NULL,?,?)""",
        (
            OWNER,
            SOURCE,
            TARGET,
            "2026-09-26T07:59:00+08:00",
            "2026-09-26T08:14:00+08:00",
        ),
    )
    conn.commit()

    rows = list_pending_admin_customer_pair_reschedules(
        conn,
        admin_context=verify_admin_context(conn, ADMIN),
        feature_enabled=True,
    )

    assert rows[0].request_id == "legacy-request"
    assert rows[0].admin_notification_status == "not_sent"
    assert rows[0].admin_notification_last_error == ""
    assert not hasattr(rows[0], "owner_user_id")


def test_wrong_owner_unverified_calendar_expiry_and_changed_admin_fail_closed(tmp_path):
    conn, _ = open_db(tmp_path / "denials.sqlite3")
    with pytest.raises(RescheduleAuthorizationError, match="owner"):
        verify_customer_reschedule_context(conn, actor_id="U" + "2" * 32, order_id=1)

    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    conn.execute("DELETE FROM subscription_service_calendar WHERE service_date=?", (TARGET,))
    conn.commit()
    with pytest.raises(RescheduleRequestConflict, match="calendar"):
        submit_customer_pair_reschedule(
            conn, feature_enabled=True, context=customer, source_date=SOURCE,
            target_date=TARGET, request_id="bad-calendar", now=NOW,
        )

    conn.execute("INSERT INTO subscription_service_calendar VALUES(1,?,1,'第2週-一')", (TARGET,))
    conn.commit()
    submit_customer_pair_reschedule(
        conn, feature_enabled=True, context=customer, source_date=SOURCE,
        target_date=TARGET, request_id="expires", now=NOW,
    )
    sheet, book = sheet_fixture()
    with pytest.raises(RescheduleRequestConflict, match="expired"):
        approve_customer_pair_reschedule(
            conn, sheet, feature_enabled=True, request_id="expires",
            admin_context=verify_admin_context(conn, ADMIN), now=NOW + timedelta(minutes=16),
        )
    assert book.batch_calls == []

    trusted_admin = verify_admin_context(conn, ADMIN)
    conn.execute("UPDATE admin_settings SET value='revoked' WHERE key='admin_id'")
    conn.commit()
    with pytest.raises(RescheduleAuthorizationError, match="admin"):
        approve_customer_pair_reschedule(
            conn, sheet, feature_enabled=True, request_id="expires",
            admin_context=trusted_admin, now=NOW,
        )
