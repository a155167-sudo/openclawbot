"""包月估價三種取餐：一般外送(原運費) / 晚鳥外送(半價) / 自取；類型隨報價與訂單記錄。"""
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server

ADDRESS = "台北市松山區測試路1號"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    server.processed_messages.clear()
    server.pending_subscription_state.clear()
    server.subscription_delivery_blocked_users.clear()
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(server, "DB_PATH", db)
    with closing(sqlite3.connect(db)) as c:
        c.execute("""CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, customer_name TEXT, meal_count INTEGER, address TEXT, distance_text TEXT,
            delivery_fee INTEGER, delivery_count INTEGER, meal_low_total INTEGER, meal_high_total INTEGER,
            delivery_total INTEGER, quote_low_total INTEGER, quote_high_total INTEGER,
            status TEXT, created_at TEXT, form_payload_json TEXT DEFAULT '', delivery_type TEXT DEFAULT '')""")
        c.commit()
    monkeypatch.setattr(server, "get_customer_profile_for_order", lambda _uid: None)
    monkeypatch.setattr(server, "is_carpool_route", lambda *a, **k: False)
    monkeypatch.setattr(server, "get_distance", lambda *a, **k: (True, "2.1 公里", 2100, "8 分鐘"))
    yield db
    server.pending_subscription_state.clear()
    server.subscription_delivery_blocked_users.clear()


def _est(dtype, uid="U-opt"):
    return server.calculate_subscription_estimate(
        uid, 40, ADDRESS, delivery_count=20, pickup_method="外送",
        days_per_week=5, meals_per_day=2, delivery_type=dtype,
    )


def _event(i, text, uid):
    return SimpleNamespace(
        message=SimpleNamespace(id=i, text=text), source=SimpleNamespace(user_id=uid),
        reply_token=f"r-{i}",
    )


def test_regular_5_days_2_1_km_is_base_fee_no_discount():
    est = _est("一般外送")
    assert est["delivery_type"] == "一般外送"
    assert est["delivery_fee"] == 50 and est["delivery_total"] == 1000
    text = server.format_subscription_estimate(est)
    assert "🛵 一般外送" in text
    assert "距離 2.1 km｜運費 $50／趟" in text
    assert "🕐 約 11:30–12:00 送達（會盡量在 12:00 前送達，實際到達時間依當天配送狀況而定）" in text
    assert "外送 20 趟，外送費小計 $1,000" in text
    assert "折抵" not in text and "晚鳥" not in text
    flex = json.dumps(server.build_subscription_estimate_flex("U", est).as_json_dict(), ensure_ascii=False)
    assert "取餐方式：🛵 一般外送" in flex and "外送 20 趟，外送費小計 $1,000" in flex


def test_late_bird_same_trip_is_half():
    est = _est("晚鳥")
    assert est["delivery_fee"] == 25 and est["delivery_total"] == 500
    text = server.format_subscription_estimate(est)
    assert "🐦 晚鳥外送" in text and "原運費 $50／趟" in text and "🐦 晚鳥半價 → $25／趟" in text
    assert "外送 20 趟，外送費小計 $500" in text


def test_carpool_applies_to_both_types(monkeypatch):
    monkeypatch.setattr(server, "is_carpool_route", lambda *a, **k: True)
    assert _est("一般外送")["delivery_fee"] == 40
    assert _est("晚鳥")["delivery_fee"] == 15


def test_three_buttons_and_flow(monkeypatch):
    texts = [b.action.text for b in server.subscription_pickup_quick_reply().items]
    assert texts == ["包月取餐 外送", "包月取餐 晚鳥", "包月取餐 自取"]
    for prompt in (server.build_subscription_days_prompt_text(), server.build_subscription_pickup_choice_text(3)):
        assert server.REGULAR_CHOICE_TEXT in prompt and server.LATE_BIRD_CHOICE_TEXT in prompt

    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _u: False)
    monkeypatch.setattr(server, "is_text_command_allowed_without_vip", lambda *_a: False)
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    for kind, button in (("一般外送", "包月取餐 外送"), ("晚鳥", "包月取餐 晚鳥")):
        uid = f"U-flow-{kind}"
        for i, text in enumerate(("開始包月估價", "包月天數 5", button, ADDRESS)):
            server.handle_message(_event(f"{kind}-{i}", text, uid))
        est = server.pending_subscription_state[uid]["estimate"]
        assert est["delivery_type"] == kind
        assert est["delivery_total"] == (1000 if kind == "一般外送" else 500)
        with closing(sqlite3.connect(server.DB_PATH)) as c:
            assert c.execute(
                "SELECT delivery_type FROM subscription_delivery_choice WHERE user_id=?", (uid,)
            ).fetchone() == (kind,)


def test_delivery_type_persisted_on_orders(_env):
    order_id = server.create_subscription_order("U-p1", _est("晚鳥", "U-p1"))
    reg_id = server.create_subscription_order("U-p2", _est("一般外送", "U-p2"))
    with closing(sqlite3.connect(_env)) as c:
        got = dict(c.execute("SELECT id, delivery_type FROM subscription_orders").fetchall())
    assert got == {order_id: "晚鳥", reg_id: "一般外送"}


def test_form_path_resolves_saved_type_and_fee(_env):
    server.remember_subscription_delivery_choice("U-f", _est("晚鳥", "U-f"))
    assert server.resolve_form_delivery("U-f", True, ADDRESS, 50) == ("晚鳥", 25)
    assert server.resolve_form_delivery("U-f", False, "", 50) == ("自取", 0)
    # 沒有報價紀錄 -> 一般外送原運費
    assert server.resolve_form_delivery("U-none", True, ADDRESS, 50) == ("一般外送", 50)


def test_legacy_order_without_type_is_regular_not_late_bird(_env):
    with closing(sqlite3.connect(_env)) as c:
        c.execute("INSERT INTO subscription_orders (user_id,address,delivery_fee,delivery_count,status) "
                  "VALUES ('U-leg',?,?,20,'activated')", (ADDRESS, 30))
        c.commit()
    assert server.is_late_bird_customer(_env, "U-leg") is False
    assert server.list_active_delivery_addresses("U-x", delivery_type="一般外送") == [ADDRESS]
    assert server.list_active_delivery_addresses("U-x", delivery_type="晚鳥") == []


def _draft(assumption):
    estimate = {
        "food_name": "雞腿便當", "basis_amount": 1, "basis_unit": "serving",
        "calories_kcal": {"estimate": 700, "min": 600, "max": 800},
        "protein_g": {"estimate": 30, "min": 25, "max": 35},
        "fat_g": {"estimate": 20, "min": 15, "max": 25},
        "carbohydrate_g": {"estimate": 90, "min": 80, "max": 100},
        "provenance": {"method": "ai_estimate", "source_label": "AI估算"},
    }
    return {
        "token": "t" * 40, "version": 1, "portion_multiplier": 1.0, "meal_slot": "午餐",
        "estimate": estimate, "request": {"food_name": "雞腿便當", "portion_assumption": assumption},
    }


@pytest.mark.parametrize(("assumption", "shown"), [
    ("一份，一般份量約 300 g（AI估算，已依常見份量上限調整）", True),
    ("一份，一般份量約 300 g（AI估算）", False),
    ("", False),
])
def test_draft_card_shows_cap_note_only_when_capped(assumption, shown):
    msg = server.build_text_meal_estimate_flex(_draft(assumption))
    rendered = json.dumps(msg.as_json_dict(), ensure_ascii=False)
    assert ("一般份量已依常見份量上限調整" in rendered) is shown
