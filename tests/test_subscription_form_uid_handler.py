import asyncio
import json
import os
import socket
import sqlite3
from types import SimpleNamespace
from typing import Any, cast

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server

ACTUAL_UID_TITLE = "1. LINE UID (系統綁定用，請勿修改)"
VALID_UID = "U" + "a" * 32


def _dish(name, price):
    return {
        "name": name,
        "cal": 400,
        "pro": 30,
        "price": price,
        "ingredients": name,
        "category": "main",
        "carb_type": "高碳",
    }


def _request(payload):
    async def request_json():
        return payload

    return cast(Any, SimpleNamespace(json=request_json))


def _install_isolated_form_dependencies(tmp_path, monkeypatch):
    db_path = tmp_path / "subscription-form.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE subscription_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                customer_name TEXT DEFAULT '',
                meal_count INTEGER DEFAULT 0,
                address TEXT DEFAULT '',
                distance_text TEXT DEFAULT '',
                delivery_fee INTEGER DEFAULT 0,
                delivery_count INTEGER DEFAULT 0,
                meal_low_total INTEGER DEFAULT 0,
                meal_high_total INTEGER DEFAULT 0,
                delivery_total INTEGER DEFAULT 0,
                quote_low_total INTEGER DEFAULT 0,
                quote_high_total INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                admin_note TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                approved_at TEXT DEFAULT '',
                approved_by TEXT DEFAULT '',
                activated_at TEXT DEFAULT '',
                vip_code TEXT DEFAULT '',
                form_payload_json TEXT DEFAULT '',
                formalized_at TEXT DEFAULT ''
            )"""
        )
    pushes = []
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "MAIN_DISHES", [_dish("雞肉便當", 180), _dish("豬肉低碳", 190)])
    monkeypatch.setattr(server.random, "sample", lambda population, count: population[:count])
    monkeypatch.setattr(server, "get_line_display_name_safe", lambda _uid: "測試名稱")
    monkeypatch.setattr(server, "update_subscription_delivery_block", lambda *_args: None)
    monkeypatch.setattr(server, "notify_admin_pending_subscription_form", lambda *_args: None)
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda uid, message: pushes.append((uid, message.text)),
    )

    def blocked_network(*_args, **_kwargs):
        raise AssertionError("isolated form test attempted network access")

    monkeypatch.setattr(socket, "create_connection", blocked_network)
    return db_path, pushes


def test_registered_form_handler_accepts_actual_exact_uid_title_once(tmp_path, monkeypatch):
    db_path, pushes = _install_isolated_form_dependencies(tmp_path, monkeypatch)
    payload = {
        ACTUAL_UID_TITLE: VALID_UID,
        "您的稱呼 (姓名或暱稱)": "測試客戶",
        "本期取餐方式": "自取",
        "取餐日期": ["週一"],
    }

    result = asyncio.run(
        server.receive_form_data(_request(payload), cast(Any, SimpleNamespace()))
    )

    assert result["status"] == "pending"
    with sqlite3.connect(db_path) as conn:
        orders = conn.execute(
            "SELECT id, user_id, status, form_payload_json FROM subscription_orders"
        ).fetchall()
    assert len(orders) == 1
    assert orders[0][1:3] == (VALID_UID, "pending")
    assert json.loads(orders[0][3])["raw_form_data"][ACTUAL_UID_TITLE] == VALID_UID
    assert len(pushes) == 1
    assert pushes[0][0] == VALID_UID


@pytest.mark.parametrize(
    "payload",
    [
        {ACTUAL_UID_TITLE: ""},
        {ACTUAL_UID_TITLE: "invalid"},
        {"LINE UID 額外說明": VALID_UID},
        {"好友UID備註": VALID_UID},
    ],
)
def test_registered_form_handler_ignores_empty_invalid_and_unknown_uid_fields(
    payload, tmp_path, monkeypatch
):
    db_path, pushes = _install_isolated_form_dependencies(tmp_path, monkeypatch)

    result = asyncio.run(
        server.receive_form_data(_request(payload), cast(Any, SimpleNamespace()))
    )

    assert result == {"status": "ignored"}
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM subscription_orders").fetchone()[0] == 0
    assert pushes == []


def test_registered_form_handler_rejects_multiple_exact_uid_aliases(tmp_path, monkeypatch):
    db_path, pushes = _install_isolated_form_dependencies(tmp_path, monkeypatch)
    payload = {
        ACTUAL_UID_TITLE: VALID_UID,
        "UID": "U" + "b" * 32,
    }

    with pytest.raises(server.HTTPException) as exc_info:
        asyncio.run(
            server.receive_form_data(_request(payload), cast(Any, SimpleNamespace()))
        )

    assert exc_info.value.status_code == 422
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM subscription_orders").fetchone()[0] == 0
    assert pushes == []
