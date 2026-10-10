import sqlite3
from types import SimpleNamespace

import pytest

from customer_navigation import (
    build_customer_function_menu_contents,
    build_customer_health_services_contents,
)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_health_service_grid_slot_is_absent_without_verified_eligibility():
    bubble = build_customer_function_menu_contents(health_services_available=False)
    assert "健康服務" not in str(bubble)
    assert "符合資格後顯示" not in str(bubble)


def test_qualified_health_service_only_exposes_authorized_existing_routes():
    menu = build_customer_function_menu_contents(health_services_available=True)
    service_action = next(
        node["action"] for node in _walk(menu)
        if node.get("type") == "button" and node.get("action", {}).get("label") == "健康服務"
    )
    assert service_action == {"type": "message", "label": "健康服務", "text": "健康服務"}

    contents = build_customer_health_services_contents({
        "training_available": True,
        "health_check_url": "https://liff.line.me/2009251085-customerCheckup",
    })
    actions = [node["action"] for node in _walk(contents) if node.get("type") == "button"]
    assert {action["text"] for action in actions if action["type"] == "message"} == {"運動"}
    assert {action["uri"] for action in actions if action["type"] == "uri"} == {
        "https://liff.line.me/2009251085-customerCheckup"
    }


def test_no_eligible_health_service_builds_no_usable_action():
    assert build_customer_health_services_contents({
        "training_available": False,
        "health_check_url": None,
    }) is None


def test_service_projection_uses_owner_health_case_and_valid_channel_bound_liff(monkeypatch):
    import server

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    server._customer_health_service_cache.clear()
    monkeypatch.setattr(server, "get_dashboard_data", lambda _uid: {"has_training_service": False})
    monkeypatch.setattr(server, "get_vip_health_check_state_for_user", lambda _uid: {"status": "delivered"})
    monkeypatch.setenv("VIP_HEALTH_CHECK_LIFF_ID", "2009251085-customerCheckup")
    monkeypatch.setenv("VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID", "2009251085")

    services = server.resolve_customer_health_services("U-OWNER", force_refresh=True)

    assert services == {
        "training_available": False,
        "health_check_url": "https://liff.line.me/2009251085-customerCheckup",
        "available": True,
    }


def test_service_projection_does_not_expose_mismatched_or_inactive_health_check(monkeypatch):
    import server

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    server._customer_health_service_cache.clear()
    monkeypatch.setattr(server, "get_dashboard_data", lambda _uid: {"has_training_service": False})
    monkeypatch.setattr(server, "get_vip_health_check_state_for_user", lambda _uid: {"status": "cancelled"})
    monkeypatch.setenv("VIP_HEALTH_CHECK_LIFF_ID", "2009251085-customerCheckup")
    monkeypatch.setenv("VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID", "9999999999")

    services = server.resolve_customer_health_services("U-OWNER", force_refresh=True)

    assert services == {
        "training_available": False,
        "health_check_url": None,
        "available": False,
    }


def test_service_projection_caches_sheet_eligibility_and_supports_forced_refresh(monkeypatch):
    import server

    now = [100.0]
    calls = []
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    server._customer_health_service_cache.clear()
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "get_dashboard_data", lambda _uid: calls.append(_uid) or {"has_training_service": True})

    first = server.resolve_customer_health_services("U-OWNER")
    second = server.resolve_customer_health_services("U-OWNER")
    refreshed = server.resolve_customer_health_services("U-OWNER", force_refresh=True)

    assert first["training_available"] is True
    assert second == first
    assert refreshed == first
    assert calls == ["U-OWNER", "U-OWNER"]


def test_service_projection_reads_only_same_owner_case_bound_coaching_order(
    tmp_path, monkeypatch,
):
    import server

    db_path = tmp_path / "coaching-status.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE vip_health_check_cases (
                case_id TEXT PRIMARY KEY, user_id TEXT NOT NULL
            );
            CREATE TABLE dietitian_coaching_orders (
                order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                case_id TEXT NOT NULL, product_type TEXT NOT NULL,
                status TEXT NOT NULL, starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO vip_health_check_cases(case_id,user_id) VALUES (?,?)",
            (("case-owner", "U-OWNER"), ("case-foreign", "U-OTHER")),
        )
        conn.executemany(
            """INSERT INTO dietitian_coaching_orders
               (order_id,user_id,case_id,product_type,status,starts_at,ends_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                (
                    "order-owner", "U-OWNER", "case-owner",
                    "dietitian_coaching_4w", "coaching_active",
                    "2026-09-01T00:00:00+00:00", "2026-09-29T00:00:00+00:00",
                    "2026-09-02T00:00:00+00:00",
                ),
                (
                    "order-owner-bad-case", "U-OWNER", "case-foreign",
                    "dietitian_coaching_4w", "coaching_completed", "", "",
                    "2026-09-04T00:00:00+00:00",
                ),
                (
                    "order-foreign", "U-OTHER", "case-foreign",
                    "dietitian_coaching_4w", "coaching_refunded", "", "",
                    "2026-09-05T00:00:00+00:00",
                ),
            ),
        )
        conn.commit()

    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(
        server, "get_dashboard_data", lambda _uid: {"has_training_service": False}
    )
    server._customer_health_service_cache.clear()

    services = server.resolve_customer_health_services("U-OWNER", force_refresh=True)

    assert services == {
        "training_available": False,
        "health_check_url": None,
        "coaching_order": {
            "status": "coaching_active",
            "starts_at": "2026-09-01T00:00:00+00:00",
            "ends_at": "2026-09-29T00:00:00+00:00",
        },
        "available": True,
    }


def test_coaching_projection_fails_closed_for_missing_database(tmp_path, monkeypatch):
    import server

    missing_db = tmp_path / "missing" / "health.db"
    monkeypatch.setattr(server, "DB_PATH", str(missing_db))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(
        server, "get_dashboard_data", lambda _uid: {"has_training_service": False}
    )
    server._customer_health_service_cache.clear()

    services = server.resolve_customer_health_services("U-OWNER", force_refresh=True)

    assert services == {
        "training_available": False,
        "health_check_url": None,
        "available": False,
    }
    assert not missing_db.exists()


def test_coaching_adapter_rejects_unknown_persisted_status():
    from vip_health_check import get_customer_coaching_order_status

    with sqlite3.connect(":memory:") as conn:
        conn.executescript(
            """
            CREATE TABLE vip_health_check_cases (case_id TEXT, user_id TEXT);
            CREATE TABLE dietitian_coaching_orders (
                order_id TEXT, user_id TEXT, case_id TEXT, product_type TEXT,
                status TEXT, starts_at TEXT, ends_at TEXT, updated_at TEXT
            );
            INSERT INTO vip_health_check_cases VALUES ('case-owner','U-OWNER');
            INSERT INTO dietitian_coaching_orders VALUES (
                'order-owner','U-OWNER','case-owner','dietitian_coaching_4w',
                'unexpected_status','','','2026-09-01T00:00:00+00:00'
            );
            """
        )

        assert get_customer_coaching_order_status(conn, user_id="U-OWNER") is None


@pytest.mark.parametrize(
    ("status", "copy"),
    (
        ("payment_pending", "等待付款"),
        ("payment_reported", "已回報付款，等待人工確認"),
        ("coaching_active", "陪跑進行中"),
        ("coaching_paused", "陪跑已暫停"),
        ("coaching_completed", "陪跑已完成"),
        ("coaching_refunded", "陪跑已退款"),
        ("coaching_cancelled", "陪跑已取消"),
        ("payment_rejected", "付款未通過"),
    ),
)
def test_coaching_order_renders_read_only_actual_status_without_fake_action(status, copy):
    contents = build_customer_health_services_contents({
        "training_available": False,
        "health_check_url": None,
        "coaching_order": {
            "status": status,
            "starts_at": "2026-09-01T00:00:00+00:00",
            "ends_at": "2026-09-29T00:00:00+00:00",
        },
    })

    assert contents is not None
    assert copy in str(contents)
    assert "footer" not in contents
    assert not any(node.get("type") == "button" for node in _walk(contents))


def test_registered_health_service_handler_renders_owned_coaching_status_without_action(
    monkeypatch,
):
    import server

    services = {
        "training_available": False,
        "health_check_url": None,
        "coaching_order": {
            "status": "payment_reported",
            "starts_at": "",
            "ends_at": "",
        },
        "available": True,
    }
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server, "resolve_customer_health_services", lambda _uid, force_refresh=False: services
    )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda token, message: replies.append((token, message.as_json_dict())),
    )
    server.processed_messages.clear()
    event = SimpleNamespace(
        message=SimpleNamespace(id="HEALTH-COACHING-1", text="健康服務"),
        source=SimpleNamespace(user_id="U-OWNER"),
        reply_token="reply-health-coaching",
    )

    server.handle_message(event)

    assert len(replies) == 1
    rendered = replies[0][1]["contents"]
    assert "已回報付款，等待人工確認" in str(rendered)
    assert not any(node.get("type") == "button" for node in _walk(rendered))
