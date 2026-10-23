"""包月「晚鳥外送」定價：原運費分級 +$10、晚鳥半價（無條件進位）、取消四週外送費折抵。"""
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
def _clear_quote_globals(monkeypatch):
    server.processed_messages.clear()
    server.pending_subscription_state.clear()
    server.subscription_delivery_blocked_users.clear()
    monkeypatch.setattr(server, "get_customer_profile_for_order", lambda _uid: None)
    yield
    server.processed_messages.clear()
    server.pending_subscription_state.clear()
    server.subscription_delivery_blocked_users.clear()


def _distance(monkeypatch, meters):
    monkeypatch.setattr(
        server,
        "get_distance",
        lambda *_args, **_kwargs: (True, f"{meters / 1000:g} 公里", meters, "8 分鐘"),
    )


def _text_event(message_id, text, user_id):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
    )


@pytest.mark.parametrize(
    ("meters", "base_fee", "late_bird"),
    [
        (800, 30, 15),
        (1000, 30, 15),
        (1001, 35, 18),
        (1500, 35, 18),
        (1800, 40, 20),
        (2100, 50, 25),
        (2500, 50, 25),
        (2900, 60, 30),
        (3000, 60, 30),
    ],
)
def test_each_tier_base_fee_and_late_bird_half_price(monkeypatch, meters, base_fee, late_bird):
    _distance(monkeypatch, meters)

    quote = server.calculate_delivery_quote(ADDRESS)

    assert quote["delivery_available"] is True
    assert quote["delivery_fee"] == base_fee
    assert quote["late_bird_fee"] == late_bird
    assert server.late_bird_fee(base_fee) == late_bird


def test_late_bird_rounds_half_dollar_up():
    assert server.late_bird_fee(35) == 18
    assert server.late_bird_fee(0) == 0
    assert server.late_bird_fee(None) == 0


def test_over_three_km_is_unavailable(monkeypatch):
    _distance(monkeypatch, 3001)

    quote = server.calculate_delivery_quote(ADDRESS)

    assert quote["delivery_available"] is False
    assert quote["delivery_fee"] == 0
    assert quote["late_bird_fee"] == 0

    est = server.calculate_subscription_estimate(
        "U_LB_FAR", 40, ADDRESS, delivery_count=20, pickup_method="外送",
        days_per_week=5, meals_per_day=2, delivery_type="晚鳥",
    )
    assert est["delivery_total"] == 0
    assert server.subscription_delivery_decision(est) == "rejected"
    assert "暫不提供外送" in server.format_subscription_estimate(est)


def test_five_day_2_1_km_quote_charges_late_bird_without_discount(monkeypatch):
    _distance(monkeypatch, 2100)

    est = server.calculate_subscription_estimate(
        "U_LB_5D", 40, ADDRESS, delivery_count=20, pickup_method="外送",
        days_per_week=5, meals_per_day=2, delivery_type="晚鳥",
    )

    assert est["base_delivery_fee"] == 50
    assert est["delivery_fee"] == 25
    assert est["delivery_count"] == 20
    assert est["delivery_total"] == 500
    assert est["quote_low_total"] == 40 * server.SUBSCRIPTION_MEAL_LOW + 500
    assert est["quote_high_total"] == 40 * server.SUBSCRIPTION_MEAL_HIGH + 500
    assert not any("discount" in key for key in est)

    text = server.format_subscription_estimate(est)
    assert "距離 2.1 km｜原運費 $50／趟" in text
    assert "🐦 晚鳥半價 → $25／趟" in text
    assert "🕐 每天 13:00–14:00 送達（不指定時間）" in text
    assert "外送 20 趟，外送費小計 $500" in text
    assert "本期粗估合計：$6,100～$9,300" in text

    flex = json.dumps(server.build_subscription_estimate_flex("U_LB_5D", est).as_json_dict(), ensure_ascii=False)
    assert "🐦 晚鳥半價 → $25／趟" in flex
    assert "外送 20 趟，外送費小計 $500" in flex
    assert "本期合計：約 $6,100～$9,300" in flex


def test_persisted_quote_row_stores_charged_late_bird_fee(monkeypatch, tmp_path):
    db_path = tmp_path / "orders.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """CREATE TABLE subscription_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, customer_name TEXT,
                meal_count INTEGER, address TEXT, distance_text TEXT, delivery_fee INTEGER,
                delivery_count INTEGER, meal_low_total INTEGER, meal_high_total INTEGER,
                delivery_total INTEGER, quote_low_total INTEGER, quote_high_total INTEGER,
                status TEXT, created_at TEXT, delivery_type TEXT DEFAULT ''
            )"""
        )
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    _distance(monkeypatch, 2100)
    est = server.calculate_subscription_estimate(
        "U_LB_ROW", 40, ADDRESS, delivery_count=20, pickup_method="外送",
        days_per_week=5, meals_per_day=2, delivery_type="晚鳥",
    )

    order_id = server.create_subscription_order("U_LB_ROW", est)

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT delivery_fee, delivery_count, delivery_total, delivery_type FROM subscription_orders WHERE id=?",
            (order_id,),
        ).fetchone()
    assert row == (25, 20, 500, "晚鳥")


@pytest.mark.parametrize(("days", "bonus"), [(2, 0), (3, 1), (4, 1), (5, 2)])
def test_self_pickup_bonus_unchanged(days, bonus):
    assert server.get_subscription_self_pickup_bonus(days) == bonus
    est = server.calculate_subscription_estimate(
        "U_LB_SELF", days * 2 * 4, "", delivery_count=0, pickup_method="自取",
        days_per_week=days, meals_per_day=2,
    )
    assert est["self_pickup_bonus_count"] == bonus
    assert est["delivery_total"] == 0
    text = server.format_subscription_estimate(est)
    if bonus:
        assert f"送 {bonus} 次蛋白補充" in text
        assert f"自取送 {bonus} 次蛋白補充" in server.format_subscription_day_bonus(days)


def test_no_customer_facing_quote_text_mentions_delivery_discount(monkeypatch):
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(server, "is_text_command_allowed_without_vip", lambda *_args: False)
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    _distance(monkeypatch, 2100)

    for days in (2, 3, 4, 5):
        uid = f"U_LB_TEXT_{days}"
        for index, text in enumerate(("開始包月估價", f"包月天數 {days}", "包月取餐 晚鳥", ADDRESS)):
            server.handle_message(_text_event(f"LB-{days}-{index}", text, uid))

    assert len(replies) == 16
    rendered = [json.dumps(message.as_json_dict(), ensure_ascii=False) for message in replies]
    pickup_prompts = rendered[1::4]
    assert all("🐦 晚鳥外送　運費半價｜13:00–14:00 送達（不指定時間）" in text for text in pickup_prompts)
    assert all(server.REGULAR_CHOICE_TEXT in text for text in pickup_prompts)
    assert all("自取" in text for text in pickup_prompts)

    static_texts = [server.build_subscription_days_prompt_text(), server.build_subscription_intro_text("U")]
    static_texts += [server.format_subscription_day_bonus(days) for days in range(0, 7)]
    static_texts += [server.build_subscription_pickup_choice_text(days) for days in (2, 3, 4, 5)]
    for days in (3, 5):
        est = server.calculate_subscription_estimate(
            "U_LB_TEXT", days * 8, ADDRESS, delivery_count=days * 4, pickup_method="外送",
            days_per_week=days, meals_per_day=2, delivery_type="晚鳥",
        )
        static_texts.append(server.format_subscription_estimate(est))

    for text in rendered + static_texts:
        assert "折抵" not in text
        assert "運費折" not in text
    assert not hasattr(server, "get_subscription_delivery_discount")
