"""Focused tests for customer_reschedule_liff_routes.

Each test uses an in-memory SQLite fixture and a fake LINE token verifier so
no external network or production data is touched.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from customer_health_check_liff import (
    LineAuthenticationError,
    LineAuthenticationUnavailable,
)
from customer_reschedule_liff_routes import (
    create_customer_reschedule_router,
    attach_customer_reschedule_liff_routes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TAIPEI = ZoneInfo("Asia/Taipei")
CHANNEL_ID = "1234567890"
LIFF_ID = f"{CHANNEL_ID}-reschedule-stg"


def _fake_token_verifier(token: str, *, channel_id: str) -> str:
    """Deterministic fake: token "tok-Uxxxx" → user "Uxxxx"."""
    if not token or not token.startswith("tok-"):
        raise LineAuthenticationError("bad token")
    uid = token[4:]
    if not re.fullmatch(r"U[0-9a-fA-F]{32}", uid):
        raise LineAuthenticationError("bad uid in token")
    return uid


def _failing_verifier(token: str, *, channel_id: str) -> str:
    raise LineAuthenticationUnavailable("LINE down")


_USER_A = "U" + "a" * 32
_USER_B = "U" + "b" * 32
_ADMIN = "U" + "9" * 32
_TOKEN_A = f"tok-{_USER_A}"
_TOKEN_B = f"tok-{_USER_B}"
_TOKEN_ADMIN = f"tok-{_ADMIN}"


@pytest.fixture()
def db(tmp_path):
    """Create a temporary SQLite database with the minimal schema needed."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE subscription_orders (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'activated',
            meal_count INTEGER NOT NULL DEFAULT 10
        );
        CREATE TABLE usage (
            user_id TEXT PRIMARY KEY,
            remaining_meals INTEGER NOT NULL DEFAULT 10,
            last_date TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE subscription_service_calendar (
            order_id INTEGER NOT NULL,
            service_date TEXT NOT NULL,
            is_service_day INTEGER NOT NULL DEFAULT 0,
            schedule_label TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (order_id, service_date)
        );
        CREATE TABLE admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE customer_pair_reschedule_requests (
            request_id TEXT PRIMARY KEY NOT NULL,
            order_id INTEGER NOT NULL,
            owner_user_id TEXT NOT NULL,
            source_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending_admin','confirmed','sheet_unknown')),
            operation_id TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        INSERT INTO admin_settings (key, value) VALUES ('admin_id', 'U99999999999999999999999999999999');
        """
    )
    conn.close()
    return db_path


def _seed_user(db_path: str, user_id: str, order_id: int = 1):
    """Insert a minimal activated order + usage for the given user."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO subscription_orders (id, user_id, status, meal_count) VALUES (?,?,?,?)",
        (order_id, user_id, "activated", 10),
    )
    conn.execute(
        "INSERT INTO usage (user_id, remaining_meals, last_date, expiry_date, status) VALUES (?,?,?,?,?)",
        (user_id, 10, "2026-10-23", "2026-10-15", "active"),
    )
    # Seed service calendar for source and a few target dates
    for d in ("2026-10-23", "2026-10-28", "2026-11-15"):
        conn.execute(
            "INSERT INTO subscription_service_calendar (order_id, service_date, is_service_day, schedule_label) VALUES (?,?,?,?)",
            (order_id, d, 1, f"label-{d}"),
        )
    conn.commit()
    conn.close()


def _make_app(db_path: str, *, enabled: bool = True, token_verifier=None):
    """Create a FastAPI app with the reschedule router mounted."""
    app = FastAPI()
    create_customer_reschedule_router(
        liff_id=LIFF_ID,
        channel_id=CHANNEL_ID,
        db_path=db_path,
        app_env="staging",
        pair_reschedule_enabled=enabled,
        token_verifier=token_verifier or _fake_token_verifier,
        now_factory=lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=TAIPEI),
    )
    # Mount the router manually for test
    router = create_customer_reschedule_router(
        liff_id=LIFF_ID,
        channel_id=CHANNEL_ID,
        db_path=db_path,
        app_env="staging",
        pair_reschedule_enabled=enabled,
        token_verifier=token_verifier or _fake_token_verifier,
        now_factory=lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=TAIPEI),
    )
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Page serving tests
# ---------------------------------------------------------------------------


def test_page_serves_html_in_staging(db):
    app = FastAPI()
    router = create_customer_reschedule_router(
        liff_id=LIFF_ID,
        channel_id=CHANNEL_ID,
        db_path=db,
        app_env="staging",
        token_verifier=_fake_token_verifier,
    )
    app.include_router(router)
    client = TestClient(app)
    resp = client.get("/customer-reschedule")
    assert resp.status_code == 200
    assert "改期" in resp.text
    assert "html" in resp.text.lower()
    assert "/customer-reschedule/context" in resp.text


def test_router_creation_refuses_non_staging():
    with pytest.raises(RuntimeError, match="staging-only"):
        create_customer_reschedule_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            db_path=":memory:",
            app_env="production",
        )


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


def test_preview_requires_bearer_token(db):
    client = _make_app(db)
    resp = client.get("/customer-reschedule/preview", params={
        "order_id": 1, "source_date": "2026-10-23", "target_date": "2026-11-01"
    })
    assert resp.status_code == 401
    assert "LINE" in resp.json()["detail"]


def test_preview_rejects_malformed_bearer(db):
    client = _make_app(db)
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": "Basic abc"},
        params={"order_id": 1, "source_date": "2026-10-23", "target_date": "2026-11-01"},
    )
    assert resp.status_code == 401


def test_preview_rejects_invalid_token(db):
    client = _make_app(db)
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": "Bearer bad-token"},
        params={"order_id": 1, "source_date": "2026-10-23", "target_date": "2026-11-01"},
    )
    assert resp.status_code == 401


def test_preview_returns_503_when_line_unavailable(db):
    client = _make_app(db, token_verifier=_failing_verifier)
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        params={"order_id": 1, "source_date": "2026-10-23", "target_date": "2026-11-01"},
    )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Preview endpoint tests
# ---------------------------------------------------------------------------


def test_context_requires_bearer_token_before_database_read(db):
    client = _make_app(db)

    response = client.get("/customer-reschedule/context")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_context_lists_only_verified_customers_activated_orders_and_service_dates(db):
    _seed_user(db, _USER_A, order_id=1)
    _seed_user(db, _USER_B, order_id=2)
    client = _make_app(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO subscription_orders (id, user_id, status, meal_count) VALUES (?,?,?,?)",
        (3, _USER_A, "cancelled", 10),
    )
    conn.execute(
        "INSERT INTO subscription_service_calendar (order_id, service_date, is_service_day, schedule_label) VALUES (?,?,?,?)",
        (3, "2026-10-24", 1, "cancelled-order-date"),
    )
    conn.execute(
        "INSERT INTO subscription_service_calendar (order_id, service_date, is_service_day, schedule_label) VALUES (?,?,?,?)",
        (1, "2026-10-24", 0, "not-a-service-day"),
    )
    conn.execute(
        "INSERT INTO subscription_service_calendar (order_id, service_date, is_service_day, schedule_label) VALUES (?,?,?,?)",
        (1, "2026-09-27", 1, "past-service-day"),
    )
    conn.commit()
    conn.close()

    response = client.get(
        "/customer-reschedule/context?user_id=" + _USER_B,
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [order["order_id"] for order in body["orders"]] == [1]
    assert body["orders"][0]["source_dates"] == [
        {"date": "2026-10-23", "label": "label-2026-10-23"},
        {"date": "2026-10-28", "label": "label-2026-10-28"},
        {"date": "2026-11-15", "label": "label-2026-11-15"},
    ]
    assert "user_id" not in body
    assert "owner_user_id" not in response.text


def test_context_returns_safe_empty_orders_for_verified_customer_without_order(db):
    client = _make_app(db)

    response = client.get(
        "/customer-reschedule/context",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
    )

    assert response.status_code == 200
    assert response.json() == {"orders": []}


def test_admin_readback_requires_authenticated_admin(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)

    missing = client.get("/api/admin/customer-pair-reschedule-requests")
    customer = client.get(
        "/api/admin/customer-pair-reschedule-requests",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
    )

    assert missing.status_code == 401
    assert customer.status_code == 403


def test_admin_readback_lists_pending_admin_requests_without_owner_uid_leakage(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)
    submitted = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-23",
            "target_date": "2026-11-01",
            "request_id": "req-admin-readback-001",
        },
    )
    assert submitted.status_code == 200
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO customer_pair_reschedule_requests
               (request_id,order_id,owner_user_id,source_date,target_date,status,
                operation_id,created_at,expires_at)
               VALUES(?,?,?,?,?,'confirmed','op-confirmed',?,?)""",
            (
                "req-confirmed-hidden",
                1,
                _USER_A,
                "2026-10-28",
                "2026-11-02",
                "2026-09-28T12:01:00+08:00",
                "2026-09-28T12:16:00+08:00",
            ),
        )
        conn.commit()

    response = client.get(
        "/api/admin/customer-pair-reschedule-requests",
        headers={"Authorization": f"Bearer {_TOKEN_ADMIN}"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {
        "requests": [
            {
                "request_id": "req-admin-readback-001",
                "order_id": 1,
                "source_date": "2026-10-23",
                "target_date": "2026-11-01",
                "status": "pending_admin",
                "created_at": "2026-09-28T12:00:00+08:00",
                "expires_at": "2026-09-28T12:15:00+08:00",
                "admin_notification_status": "deferred",
                "admin_notification_last_error": "admin notification sender disabled",
                "admin_notification_attempted_at": "",
            }
        ]
    }
    assert "owner_user_id" not in response.text
    assert _USER_A not in response.text
    assert "req-confirmed-hidden" not in response.text


def test_admin_readback_is_staging_feature_gated(db):
    client = _make_app(db, enabled=False)

    response = client.get(
        "/api/admin/customer-pair-reschedule-requests",
        headers={"Authorization": f"Bearer {_TOKEN_ADMIN}"},
    )

    assert response.status_code == 503


def test_admin_readback_rejects_non_pending_status_filter(db):
    client = _make_app(db, enabled=True)

    response = client.get(
        "/api/admin/customer-pair-reschedule-requests?status=confirmed",
        headers={"Authorization": f"Bearer {_TOKEN_ADMIN}"},
    )

    assert response.status_code == 422


def test_preview_success(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db)
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        params={"order_id": 1, "source_date": "2026-10-23", "target_date": "2026-11-01"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["source_date"] == "2026-10-23"
    assert body["target_date"] == "2026-11-01"
    assert "window" in body


def test_preview_rejects_unauthorized_order(db):
    """User B cannot preview user A's order."""
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db)
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": f"Bearer {_TOKEN_B}"},
        params={"order_id": 1, "source_date": "2026-10-23", "target_date": "2026-11-01"},
    )
    assert resp.status_code == 403


def test_preview_rejects_occupied_target(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db)
    # 2026-10-28 is seeded as a service day
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        params={"order_id": 1, "source_date": "2026-10-23", "target_date": "2026-10-28"},
    )
    assert resp.status_code == 422
    assert "已有" in resp.json()["detail"] or "already" in resp.json()["detail"].lower()


def test_preview_rejects_source_date_not_in_verified_service_calendar(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db)
    response = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        params={"order_id": 1, "source_date": "2026-10-24", "target_date": "2026-11-01"},
    )
    assert response.status_code == 422
    assert "來源日期" in response.json()["detail"]


def test_preview_rejects_missing_order_id(db):
    _seed_user(db, _USER_A)
    client = _make_app(db)
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        params={"source_date": "2026-10-23", "target_date": "2026-11-01"},
    )
    assert resp.status_code == 422


def test_preview_rejects_malformed_date(db):
    _seed_user(db, _USER_A)
    client = _make_app(db)
    resp = client.get(
        "/customer-reschedule/preview",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        params={"order_id": 1, "source_date": "2026-10-23", "target_date": "2026/11/01"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Submit endpoint tests
# ---------------------------------------------------------------------------


def test_submit_creates_pending_admin_request(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-23",
            "target_date": "2026-11-01",
            "request_id": "req-test-001",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["request_id"] == "req-test-001"
    assert body["status"] == "pending_admin"
    assert body["owner_user_id"] == _USER_A


def test_submit_idempotent_replay(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)
    payload = {
        "order_id": 1,
        "source_date": "2026-10-23",
        "target_date": "2026-11-01",
        "request_id": "req-replay-001",
    }
    resp1 = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json=payload,
    )
    assert resp1.status_code == 200
    resp2 = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json=payload,
    )
    assert resp2.status_code == 200
    assert resp1.json()["request_id"] == resp2.json()["request_id"]


def test_submit_never_trusts_client_actor_id(db):
    """The body does not contain actor_id; the server derives it from the token."""
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-23",
            "target_date": "2026-11-01",
            "request_id": "req-owner-001",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["owner_user_id"] == _USER_A


def test_submit_rejects_wrong_user_for_order(db):
    """User B cannot submit for user A's order."""
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_B}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-23",
            "target_date": "2026-11-01",
            "request_id": "req-wrong-user",
        },
    )
    assert resp.status_code == 403


def test_submit_returns_503_when_feature_disabled(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=False)
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-23",
            "target_date": "2026-11-01",
            "request_id": "req-disabled",
        },
    )
    assert resp.status_code == 503


def test_submit_rejects_missing_fields(db):
    _seed_user(db, _USER_A)
    client = _make_app(db, enabled=True)
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={"order_id": 1, "source_date": "2026-10-23"},
    )
    assert resp.status_code == 422


def test_submit_rejects_invalid_request_id_format(db):
    _seed_user(db, _USER_A)
    client = _make_app(db, enabled=True)
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-23",
            "target_date": "2026-11-01",
            "request_id": "",
        },
    )
    assert resp.status_code == 422


def test_submit_rejects_oversized_body(db):
    _seed_user(db, _USER_A)
    client = _make_app(db, enabled=True)
    big_payload = "x" * 3000
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={
            "Authorization": f"Bearer {_TOKEN_A}",
            "Content-Length": str(len(big_payload.encode())),
        },
        content=big_payload.encode(),
    )
    assert resp.status_code == 422


def test_submit_rejects_occupied_target(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)
    resp = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-23",
            "target_date": "2026-10-28",
            "request_id": "req-occupied",
        },
    )
    assert resp.status_code == 422


def test_submit_rejects_source_date_not_in_verified_service_calendar(db):
    _seed_user(db, _USER_A, order_id=1)
    client = _make_app(db, enabled=True)
    response = client.post(
        "/customer-reschedule/pending-request",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
        json={
            "order_id": 1,
            "source_date": "2026-10-24",
            "target_date": "2026-11-01",
            "request_id": "req-invalid-source",
        },
    )
    assert response.status_code == 422
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customer_pair_reschedule_requests").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# attach_customer_reschedule_liff_routes integration
# ---------------------------------------------------------------------------


def test_attach_skips_in_legacy(monkeypatch, db):
    app = FastAPI()
    environ = {"APP_ENV": "legacy"}
    result = attach_customer_reschedule_liff_routes(
        app, enabled=True, environ=environ, db_path=db,
        token_verifier=_fake_token_verifier,
    )
    assert result is False


def test_attach_skips_when_disabled(monkeypatch, db):
    app = FastAPI()
    environ = {
        "APP_ENV": "staging",
        "CUSTOMER_RESCHEDULE_LIFF_ID": LIFF_ID,
        "CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
    }
    result = attach_customer_reschedule_liff_routes(
        app, enabled=False, environ=environ, db_path=db,
        token_verifier=_fake_token_verifier,
    )
    assert result is False


def test_attach_mounts_in_staging(monkeypatch, db):
    app = FastAPI()
    environ = {
        "APP_ENV": "staging",
        "CUSTOMER_RESCHEDULE_LIFF_ID": LIFF_ID,
        "CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
    }
    result = attach_customer_reschedule_liff_routes(
        app, enabled=True, environ=environ, db_path=db,
        token_verifier=_fake_token_verifier,
    )
    assert result is True
    client = TestClient(app)
    resp = client.get("/customer-reschedule")
    assert resp.status_code == 200


def test_attach_raises_when_env_missing_liff_id(monkeypatch, db):
    app = FastAPI()
    environ = {
        "APP_ENV": "staging",
        "CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
    }
    with pytest.raises(ValueError, match="CUSTOMER_RESCHEDULE_LIFF_ID"):
        attach_customer_reschedule_liff_routes(
            app, enabled=True, environ=environ, db_path=db,
            token_verifier=_fake_token_verifier,
        )


def test_context_accepts_existing_vip_usage_status(db):
    _seed_user(db, _USER_A)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE usage SET status='vip' WHERE user_id=?", (_USER_A,))
        conn.commit()
    client = _make_app(db)
    response = client.get(
        "/customer-reschedule/context",
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["orders"][0]["order_id"] == 1
    assert payload["orders"][0]["source_dates"][0]["date"] == "2026-10-23"
