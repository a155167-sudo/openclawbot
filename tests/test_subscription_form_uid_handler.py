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
        conn.execute(
            """CREATE TABLE health_profile (
                user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL,
                goal TEXT, restrictions TEXT, summary_text TEXT, active_days TEXT,
                today_extra_cal INTEGER, today_date TEXT, sheet_name TEXT,
                is_coaching_enabled INTEGER, is_carb_cycling_enabled INTEGER,
                ai_silenced_until TEXT, user_level INTEGER, race_date TEXT,
                address TEXT, distance_text TEXT, distance_meters INTEGER,
                delivery_fee INTEGER, delivery_zone TEXT, route_group TEXT,
                delivery_note TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE usage (
                user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER,
                remaining_meals INTEGER, last_date TEXT, status TEXT,
                expiry_date TEXT, daily_chat_limit INTEGER
            )"""
        )
    pushes = []
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [_dish("雞肉便當", 180), _dish("豬肉低碳", 190), _dish("雞肉食蔬", 200)],
    )
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


def test_monthly_form_snapshot_is_plain_meal_plan_and_formalize_starts_no_training_thread(
    tmp_path, monkeypatch
):
    db_path, _pushes = _install_isolated_form_dependencies(tmp_path, monkeypatch)
    payload = {
        ACTUAL_UID_TITLE: VALID_UID,
        "您的稱呼 (姓名或暱稱)": "普通配餐客戶",
        "本期取餐方式": "自取",
        "第一週想取餐的日期": ["週一"],
        "第二週想取餐的日期": ["週二"],
        "第三週想取餐的日期": ["週三"],
        "第四週想取餐的日期": ["週四"],
        "規律運動": "有，請安排課表",
        "啟用碳循環": "是",
        "您的主食選擇（可複選）": ["都不挑食"],
        "您最喜歡的蛋白質是？（可複選）": ["雞肉", "豬肉"],
    }

    result = asyncio.run(
        server.receive_form_data(_request(payload), cast(Any, SimpleNamespace()))
    )

    with sqlite3.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT form_payload_json FROM subscription_orders WHERE id=?",
            (result["order_id"],),
        ).fetchone()[0]
    snapshot = json.loads(raw)
    assert snapshot["is_coaching_enabled"] == 0
    assert snapshot["is_carb_cycling_enabled"] == 0
    assert len(snapshot["master_api_rows"]) == 4
    assert all(row[6] == 0 and row[9] == "" and row[20] == 0 for row in snapshot["master_api_rows"])
    assert "2026/" in snapshot["schedule_text"]
    assert "午：" in snapshot["schedule_text"] and "晚：" in snapshot["schedule_text"]
    assert "課：" not in snapshot["schedule_text"]
    assert "高碳補糖" not in snapshot["schedule_text"]
    prices = {"雞肉便當": 180, "豬肉低碳": 190, "雞肉食蔬": 200}
    assert snapshot["total_price"] == sum(
        prices[meal]
        for row in snapshot["master_api_rows"]
        for meal in (row[3], row[4])
    )

    class ForbiddenThread:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("monthly formalization started an automatic training thread")

    monkeypatch.setattr(server.threading, "Thread", ForbiddenThread)
    ok, _message = server.formalize_subscription_snapshot(result["order_id"], snapshot)
    assert ok is True
    with sqlite3.connect(db_path) as conn:
        flags = conn.execute(
            "SELECT is_coaching_enabled, is_carb_cycling_enabled FROM health_profile WHERE user_id=?",
            (VALID_UID,),
        ).fetchone()
    assert flags == (0, 0)
