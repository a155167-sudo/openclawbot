import asyncio
import base64
import hashlib
import os
import json
from tests.sdk_dashboard_reply_capture import capture_dashboard_reply
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server
from nutrition_system import (
    _confirmed_result,
    confirm_user_meal_photo_revision,
    confirm_pending_label,
    daily_consumed_totals,
    ensure_nutrition_schema,
    insert_approved_meal_photo_log,
    new_id,
    save_pending_label,
    set_nutrition_input_state,
    update_pending_consumption,
    user_confirmed_meal_photo_trust_projection,
    utcish_now,
)
from daily_health_report import (
    ensure_daily_health_schema,
    format_daily_health_report,
    get_daily_health_checkin,
)
from meal_photo_system import (
    apply_meal_photo_action,
    create_meal_photo_revision_draft,
    ensure_meal_photo_schema,
    get_meal_photo_draft,
    save_meal_photo_draft,
)
from test_meal_photo_system import _answer_all, ai_estimated_payload, sample_payload


def _expected_health_source_v2_hash(
    *, log_id, version, nutrition, consumed_at, meal_slot, trust_binding="",
):
    nutrition_value = json.loads(nutrition) if isinstance(nutrition, str) else nutrition
    canonical_nutrition = json.dumps(
        nutrition_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    payload = {
        "schema_version": "vip_health_check_source_v2",
        "food_log_id": str(log_id),
        "food_log_version": int(version),
        "nutrition_snapshot_json": canonical_nutrition,
        "local_date": datetime.fromisoformat(consumed_at).astimezone(
            timezone(timedelta(hours=8))
        ).date().isoformat(),
        "normalized_meal_slot": str(meal_slot or "").strip() or "unspecified",
        "trust_binding": str(trust_binding or ""),
    }
    canonical_payload = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


class FakeWorksheet:
    def __init__(self, records):
        self._records = records

    def get_all_records(self):
        return self._records


def test_nutrition_ws_appends_only_missing_known_trailing_headers(monkeypatch):
    expected = server.nutrition_sheet_specs()["飲食紀錄"]["headers"]

    class ExistingSheet:
        def __init__(self):
            self.headers = expected[:-2]
            self.updates = []

        def row_values(self, row):
            assert row == 1
            return list(self.headers)

        def update(self, *, values, range_name, value_input_option):
            self.updates.append((values, range_name, value_input_option))
            self.headers.extend(values[0])

    ws = ExistingSheet()
    monkeypatch.setattr(server, "sh", SimpleNamespace(worksheet=lambda title: ws))

    assert server._nutrition_ws("飲食紀錄") is ws
    assert ws.updates == [([["信任類型", "估算Schema版本"]], "AD1:AE1", "RAW")]
    assert server._nutrition_ws("飲食紀錄") is ws
    assert len(ws.updates) == 1


def test_nutrition_ws_rejects_nonempty_conflicting_trailing_header(monkeypatch):
    expected = server.nutrition_sheet_specs()["飲食紀錄"]["headers"]

    class ConflictingSheet:
        def row_values(self, _row):
            return expected[:-2] + ["人工既有欄位"]

        def update(self, **_kwargs):
            raise AssertionError("conflicting header must not be overwritten")

    monkeypatch.setattr(
        server, "sh", SimpleNamespace(worksheet=lambda title: ConflictingSheet())
    )

    with pytest.raises(RuntimeError, match="欄位標題衝突"):
        server._nutrition_ws("飲食紀錄")


@pytest.mark.parametrize(
    ("distance_meters", "expected_fee"),
    [
        (0, 20),
        (1, 20),
        (1000, 20),
        (1001, 25),
        (1500, 25),
        (1501, 30),
        (2000, 30),
        (2001, 40),
        (2500, 40),
        (2501, 50),
        (3000, 50),
    ],
)
def test_delivery_quote_uses_new_distance_fee_boundaries(
    monkeypatch, distance_meters, expected_fee,
):
    monkeypatch.setattr(
        server,
        "get_distance",
        lambda *_args, **_kwargs: (
            True, f"{distance_meters / 1000:g} 公里", distance_meters, "5 分鐘"
        ),
    )

    quote = server.calculate_delivery_quote("台北市測試路1號")

    assert quote["success"] is True
    assert quote["delivery_available"] is True
    assert quote["delivery_fee"] == expected_fee
    assert quote["delivery_fee_text"] == f"{expected_fee} 元"


def test_delivery_quote_rejects_addresses_over_three_kilometers(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_distance",
        lambda *_args, **_kwargs: (True, "3.1 公里", 3001, "12 分鐘"),
    )

    quote = server.calculate_delivery_quote("台北市測試路1號")

    assert quote["success"] is True
    assert quote["delivery_available"] is False
    assert quote["delivery_fee"] == 0
    assert quote["delivery_fee_text"] == "超過 3 公里，暫不提供外送"


def test_over_three_kilometers_estimate_card_cannot_open_subscription_form(monkeypatch):
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda _address: {
            "success": True,
            "delivery_available": False,
            "address": _address,
            "distance_text": "3.1 公里",
            "distance_meters": 3100,
            "duration_text": "12 分鐘",
            "delivery_fee": 0,
            "delivery_fee_text": "超過 3 公里，暫不提供外送",
            "hub_name": "",
            "route_group": "EAST",
            "delivery_zone": "UNAVAILABLE",
            "carpool_hint": "",
        },
    )
    est = server.calculate_subscription_estimate(
        "U1", 24, "台北市測試路1號", delivery_count=12,
        pickup_method="外送", days_per_week=3, meals_per_day=2,
    )

    flex = server.build_subscription_estimate_flex("U1", est)
    payload = json.loads(flex.as_json_string())
    rendered = json.dumps(payload, ensure_ascii=False)

    assert est["delivery_available"] is False
    assert "此地址暫不提供外送" in rendered
    assert "可以，建立包月資料" not in rendered
    assert server.get_subscription_form_link("U1") not in rendered


def plan_row(**overrides):
    row = {
        "plan_id": "plan_default",
        "User_ID": "U1",
        "版本": 1,
        "生效日期": "2026-07-01",
        "結束日期": "",
        "星期": "每日",
        "餐別": "全日",
        "熱量目標": 2000,
        "蛋白質目標g": 100,
        "脂肪目標g": 60,
        "碳水目標g": 250,
        "狀態": "active",
    }
    row.update(overrides)
    return row


def test_natural_servings_postback_preserves_meal_slot(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        server, "_quick_log_catalog_card_once",
        lambda **kwargs: captured.update(kwargs) or server.TextSendMessage(text="ok"),
    )
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda *_args: None)
    event = SimpleNamespace(
        postback=SimpleNamespace(
            data="nlfood:v1:food_private_soy:servings:1.5:meal:早餐"
        ),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply-natural-serving",
        webhook_event_id="EVENT-NATURAL-SERVING",
    )
    server.handle_meal_photo_postback(event)
    assert captured["servings"] == pytest.approx(1.5)
    assert captured["meal_slot"] == "早餐"
    assert captured["food_id"] == "food_private_soy"


def test_natural_food_log_transient_failure_releases_in_process_dedupe(monkeypatch):
    event = _text_event("NATURAL-LOCK-1", "我要紀錄飲食 無糖豆漿 400cc", user_id="U1")
    server.processed_messages.clear()
    monkeypatch.setattr(
        server, "build_natural_food_log_reply",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        server._handle_message_impl(event)
    assert event.message.id not in server.processed_messages


def test_ai_estimate_log_preserves_meal_and_returns_replayable_dashboard(
    tmp_path, monkeypatch,
):
    db = tmp_path / "ai-estimate-log.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    content = (
        "已用一般估算記錄。"
        "[LOG_NUTRITION: CAL=180, PRO=8, NAME=火星果汁 300ml]"
    )
    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: fake_response)
        )
    )
    monkeypatch.setattr(server, "client", fake_client)
    monkeypatch.setattr(server, "gc", None)
    answer, card = server.get_ai_response_with_memory(
        "U-AI-ESTIMATE",
        "請用一般估算記錄 早餐 火星果汁 300ml",
        "AI-ESTIMATE-1",
    )
    replay_card = server.load_ai_estimate_replay(
        "U-AI-ESTIMATE", "AI-ESTIMATE-1"
    )
    assert "LOG_NUTRITION" not in answer
    assert card is not None
    assert replay_card is not None
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT log_id,version,meal_slot FROM food_logs WHERE user_id='U-AI-ESTIMATE'"
        ).fetchone()
        food_log_count = conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U-AI-ESTIMATE'"
        ).fetchone()[0]
        frequent_count = conn.execute(
            "SELECT use_count FROM frequent_foods WHERE user_id='U-AI-ESTIMATE'"
        ).fetchone()[0]
    assert row[2] == "早餐"
    assert food_log_count == 1
    assert frequent_count == 1
    assert replay_card.as_json_string() == card.as_json_string()
    rendered = json.dumps(json.loads(card.as_json_string()), ensure_ascii=False)
    assert "今日總覽" in rendered
    assert "火星果汁 300ml" in rendered
    assert "我要修改飲食紀錄" in rendered
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE health_profile SET today_extra_cal=999 WHERE user_id='U-AI-ESTIMATE'"
        )
        conn.execute(
            "UPDATE daily_food_log_events SET result_json='{\"flex\":[]}' WHERE event_id='AI-ESTIMATE-1'"
        )
        conn.commit()
    repaired = server.load_ai_estimate_replay("U-AI-ESTIMATE", "AI-ESTIMATE-1")
    assert repaired is not None
    assert repaired.as_json_string() == card.as_json_string()
    with sqlite3.connect(db) as conn:
        repaired_state = json.loads(conn.execute(
            "SELECT result_json FROM daily_food_log_events WHERE event_id='AI-ESTIMATE-1'"
        ).fetchone()[0])
    assert repaired_state["flex"]["type"] == "flex"

    monkeypatch.setattr(
        server, "build_dashboard_flex",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("builder failed")),
    )
    _, fallback_card = server.get_ai_response_with_memory(
        "U-AI-ESTIMATE",
        "請用一般估算記錄 早餐 火星果汁 300ml",
        "AI-ESTIMATE-BUILDER-FAIL",
    )
    assert fallback_card is not None
    with sqlite3.connect(db) as conn:
        atomic_counts = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM food_logs WHERE operation_key='AI-ESTIMATE-BUILDER-FAIL'),
                 (SELECT COUNT(*) FROM daily_food_log_events
                  WHERE event_id='AI-ESTIMATE-BUILDER-FAIL' AND action='ai_estimate_log'),
                 (SELECT COUNT(*) FROM ai_food_log_replay_snapshots
                  WHERE operation_key='AI-ESTIMATE-BUILDER-FAIL')"""
        ).fetchone()
    assert atomic_counts == (1, 1, 1)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TRIGGER fail_ai_recent BEFORE INSERT ON recent_meal_logs
               BEGIN SELECT RAISE(ABORT,'forced recent failure'); END"""
        )
        conn.commit()
    _, failed_card = server.get_ai_response_with_memory(
        "U-AI-ESTIMATE",
        "請用一般估算記錄 早餐 火星果汁 300ml",
        "AI-ESTIMATE-RECENT-FAIL",
    )
    assert failed_card is None
    with sqlite3.connect(db) as conn:
        rolled_back = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM food_logs WHERE operation_key='AI-ESTIMATE-RECENT-FAIL'),
                 (SELECT COUNT(*) FROM daily_food_log_events WHERE event_id='AI-ESTIMATE-RECENT-FAIL'),
                 (SELECT COUNT(*) FROM ai_food_log_replay_snapshots WHERE operation_key='AI-ESTIMATE-RECENT-FAIL')"""
        ).fetchone()
    assert rolled_back == (0, 0, 0)


def test_explicit_text_nutrition_logs_once_and_returns_canonical_dashboard(
    tmp_path, monkeypatch,
):
    db = tmp_path / "explicit-text-dashboard.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    fixed_now = server.datetime(2026, 9, 17, 8, 15, tzinfo=server.TW_TZ)
    monkeypatch.setattr(server, "tw_now", lambda: fixed_now)
    server.init_db()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    event = _text_event(
        "TEXT-350-17", "鮪魚蛋吐司 熱量350 蛋白質17", user_id="U-TEXT"
    )

    server._handle_message_impl(event)
    server.processed_messages.discard(event.message.id)
    server._handle_message_impl(event)

    assert len(replies) == 2
    assert all(isinstance(payload, list) and len(payload) == 2 for payload in replies)
    first = [item.as_json_dict() for item in replies[0]]
    second = [item.as_json_dict() for item in replies[1]]
    assert first == second
    assert replies[0][0].text == (
        "✅ 已記錄：鮪魚蛋吐司｜350 kcal｜蛋白質 17 g（使用者提供）"
    )
    rendered = json.dumps(first, ensure_ascii=False)
    assert "今日總覽" in rendered
    assert "鮪魚蛋吐司" in rendered
    assert '"text": "今日已吃"' in rendered and '"text": "350"' in rendered
    assert '"text": "已吃 350"' in rendered
    assert '"text": "今日蛋白質"' in rendered and '"text": "17 g"' in rendered
    assert "目標未設定" in rendered
    assert "記錄成功" not in rendered
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            """SELECT fc.product_name,fl.meal_slot,fl.nutrition_snapshot_json,
                      fl.operation_key,fc.source_type
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.user_id='U-TEXT'"""
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0:2] == ("鮪魚蛋吐司", "早餐")
    assert json.loads(rows[0][2]) == {"calories_kcal": 350.0, "protein_g": 17.0}
    assert rows[0][3] == "line-text-nutrition:U-TEXT:TEXT-350-17"
    assert rows[0][4] == "user_provided_nutrition"


@pytest.mark.parametrize("text", [
    "幫我修改鮪魚蛋吐司 熱量350 蛋白質17",
    "如果鮪魚蛋吐司 熱量350 蛋白質17",
    "鮪魚蛋吐司大概 熱量350 蛋白質17",
    "鮪魚蛋吐司 每100g熱量350 蛋白質17",
    "鮪魚蛋吐司和豆漿 熱量350 蛋白質17",
    "鮪魚蛋吐司、豆漿 熱量350 蛋白質17",
    "我沒吃鮪魚蛋吐司 熱量350 蛋白質17",
    "鮪魚蛋吐司 熱量350 蛋白質17 嗎",
    "鮪魚蛋吐司 熱量350 蛋白質17\u0000",
    "鮪魚蛋吐司 熱量999999999999 蛋白質17",
    "鮪魚蛋吐司 熱量350 蛋白質999999999999",
])
def test_explicit_text_nutrition_parser_fails_closed_for_ambiguous_or_unsafe_text(text):
    assert server.parse_explicit_text_nutrition_log(text) is None


def test_explicit_text_nutrition_parser_accepts_zero_protein_and_full_single_meal_only():
    assert server.parse_explicit_text_nutrition_log(
        "點心：紅茶 熱量 5 kcal 蛋白質 0 g"
    ) == {
        "food_name": "紅茶", "meal_slot": "點心",
        "calories_kcal": 5.0, "protein_g": 0.0,
    }
    assert server.parse_explicit_text_nutrition_log(
        "午餐：雞胸便當 熱量 520 大卡，蛋白質 38 克"
    ) == {
        "food_name": "雞胸便當", "meal_slot": "午餐",
        "calories_kcal": 520.0, "protein_g": 38.0,
    }


@pytest.mark.parametrize("text", [
    "想知道鮪魚蛋吐司 熱量350 蛋白質17",
    "鮪魚蛋吐司 每一百公克 熱量350 蛋白質17",
    "我不 要記錄鮪魚蛋吐司 熱量350 蛋白質17",
    "鮪魚蛋吐司 蛋白質17 熱量350 蛋白質17",
    "鮪魚蛋吐司 豆漿 熱量350 蛋白質17",
    "鮪魚蛋吐司 熱量是多少 熱量350 蛋白質17",
])
def test_explicit_text_review_counterexamples_do_not_write_via_real_handler(
    tmp_path, monkeypatch, text,
):
    db = tmp_path / "explicit-text-review-counterexamples.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_args, **_kwargs: ("這段文字不夠明確，請確認後再記錄。", None),
    )

    event = _text_event("REVIEW-NOWRITE", text, user_id="U-REVIEW-NOWRITE")
    server.processed_messages.discard(event.message.id)
    server._handle_message_impl(event)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U-REVIEW-NOWRITE'"
        ).fetchone()[0] == 0
    assert all("已入帳" not in getattr(reply, "text", "") for reply in replies)


def test_explicit_text_consumption_does_not_publish_or_overwrite_private_food_card(
    tmp_path, monkeypatch,
):
    db = tmp_path / "explicit-text-no-auto-card.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U-NO-CARD", product_name="鮪魚蛋吐司", meal_slot="早餐",
            consumed_at="2026-09-17T07:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 300, "protein_g": 15},
            source_type="user_private_food", operation_key="saved-private-card",
        )
        conn.commit()

    for message_id, calories in (("TEXT-350", 350), ("TEXT-400", 400)):
        server.log_explicit_text_nutrition_once(
            user_id="U-NO-CARD", message_id=message_id,
            request={"food_name": "鮪魚蛋吐司", "meal_slot": "早餐",
                     "calories_kcal": calories, "protein_g": 17},
        )

    with sqlite3.connect(db) as conn:
        private_cards = conn.execute(
            """SELECT per_serving_json FROM food_catalog
               WHERE owner_user_id='U-NO-CARD' AND visibility='private'
                 AND product_name='鮪魚蛋吐司'"""
        ).fetchall()
        snapshots = [json.loads(row[0]) for row in conn.execute(
            """SELECT nutrition_snapshot_json FROM food_logs
               WHERE user_id='U-NO-CARD' AND operation_key LIKE 'line-text-nutrition:%'
               ORDER BY operation_key"""
        )]
    assert len(private_cards) == 1
    assert json.loads(private_cards[0][0])["calories_kcal"] == 300
    assert [item["calories_kcal"] for item in snapshots] == [350, 400]


def test_backdated_post_commit_dashboard_acknowledges_trusted_log_without_changing_today_total(
    tmp_path, monkeypatch,
):
    db = tmp_path / "backdated-post-commit.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "gc", None)
    fixed_now = server.datetime(2026, 9, 17, 8, 0, tzinfo=server.TW_TZ)
    monkeypatch.setattr(server, "tw_now", lambda: fixed_now)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,
                today_food_items,today_date)
               VALUES ('U-BACKDATED','測試',2000,100,0,0,'','2026-09-17')"""
        )
        result = server.create_daily_food_log(
            conn, user_id="U-BACKDATED", product_name="補登鮪魚蛋吐司", meal_slot="早餐",
            consumed_at="2026-09-16T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 350, "protein_g": 17},
            source_type="user_provided_nutrition", operation_key="backdated-ack",
            publish_catalog=False,
        )
        conn.commit()

    reply = server.build_post_commit_food_dashboard(
        "U-BACKDATED", committed_log_id=result["log_id"]
    )
    rendered = json.dumps(reply.as_json_dict(), ensure_ascii=False)
    assert "已補登 2026-09-16" in rendered
    assert "補登鮪魚蛋吐司" in rendered
    assert "350 kcal" in rendered
    assert "17 g" in rendered
    assert '"text": "熱量餘額"' in rendered and '"text": "2,000"' in rendered
    assert '"text": "已吃 0"' in rendered


@pytest.mark.parametrize(
    ("protein", "balance_display", "threshold"),
    [(79.94, "20.1", False), (79.95, "20", False),
     (79.96, "20", False), (80.0, "20", True)],
)
def test_dashboard_display_rounding_does_not_change_protein_threshold(
    tmp_path, monkeypatch, protein, balance_display, threshold,
):
    db = tmp_path / f"protein-boundary-{protein}.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO health_profile(user_id,name,tdee,protein,today_date) VALUES (?,?,?,?,?)",
            ("U-BOUNDARY", "邊界會員", 2000, 100, today),
        )
        server.create_daily_food_log(
            conn, user_id="U-BOUNDARY", product_name="邊界餐", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 350, "protein_g": protein},
            source_type="official_menu",
        )
        conn.commit()
    dashboard = server.get_dashboard_data("U-BOUNDARY")
    assert dashboard["extra_pro"] == pytest.approx(protein)
    assert dashboard["task_protein_80"] is threshold
    rendered = json.dumps(server.build_dashboard_flex("U-BOUNDARY").as_json_dict(), ensure_ascii=False)
    assert '"text": "蛋白質餘額"' in rendered
    assert f'"text": "{balance_display} g"' in rendered
    assert '"text": "/ 100 g"' in rendered


def test_explicit_text_dashboard_render_failure_reports_committed_without_retry_prompt(
    tmp_path, monkeypatch,
):
    db = tmp_path / "explicit-text-render-failure.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    monkeypatch.setattr(
        server, "build_dashboard_flex",
        lambda _uid: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    reply = server.log_explicit_text_nutrition_once(
        user_id="U-TEXT", message_id="TEXT-RENDER-FAIL",
        request={
            "food_name": "無糖茶", "meal_slot": "點心",
            "calories_kcal": 5.0, "protein_g": 0.0,
        },
    )

    assert isinstance(reply, list) and len(reply) == 2
    assert reply[0].text == "✅ 已記錄：無糖茶｜5 kcal｜蛋白質 0 g（使用者提供）"
    assert "已入帳" in reply[1].text
    assert "儀表板暫時無法顯示" in reply[1].text
    assert "重新記錄" not in reply[1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1


def test_explicit_text_nutrition_failure_never_returns_dashboard(tmp_path, monkeypatch):
    db = tmp_path / "explicit-text-failure.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    monkeypatch.setattr(
        server, "create_daily_food_log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("forced")),
    )

    with pytest.raises(sqlite3.OperationalError, match="forced"):
        server._handle_message_impl(
            _text_event("TEXT-FAIL", "鮪魚蛋吐司 熱量350 蛋白質17", user_id="U-TEXT")
        )

    assert replies == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_ai_estimate_button_records_midpoint_when_model_returns_only_ranges(
    tmp_path, monkeypatch,
):
    db = tmp_path / "ai-estimate-range.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    content = """茶葉蛋 2 顆的營養估算：
- 熱量：約 150～200 大卡
- 蛋白質：約 14～18 克

請問要記哪個熱量值？"""
    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    monkeypatch.setattr(
        server,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: fake_response
        ))),
    )
    monkeypatch.setattr(server, "gc", None)

    answer, card = server.get_ai_response_with_memory(
        "U-AI-RANGE",
        "請用一般估算記錄 午餐 茶葉蛋 2顆",
        "AI-ESTIMATE-RANGE-1",
    )

    assert card is not None
    assert card.alt_text == "今日總覽"
    assert "今日總覽" in json.dumps(
        json.loads(card.as_json_string()), ensure_ascii=False
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """SELECT fc.product_name,fl.meal_slot,fl.nutrition_snapshot_json
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.user_id='U-AI-RANGE'"""
        ).fetchone()
    assert row[:2] == ("茶葉蛋 2顆", "午餐")
    nutrition = json.loads(row[2])
    assert nutrition["calories_kcal"] == 175
    assert nutrition["protein_g"] == 16
    assert "請問要記哪個熱量值" not in answer


def test_ai_estimate_claim_is_durable_and_exclusive(tmp_path, monkeypatch):
    db = tmp_path / "ai-estimate-claim.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
    assert server.claim_ai_estimate_request("U1", "CLAIM-1") == "claimed"
    assert server.claim_ai_estimate_request("U1", "CLAIM-1", lease_seconds=0) == "pending"
    server.release_ai_estimate_claim("U1", "CLAIM-1")
    assert server.claim_ai_estimate_request("U1", "CLAIM-1") == "claimed"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE usage (
                 user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER,
                 daily_chat_limit INTEGER
               )"""
        )
        conn.execute("INSERT INTO usage VALUES ('U1',0,1)")
        conn.commit()
    server.fail_ai_estimate_request("U1", "CLAIM-1", refund_quota=True)
    with sqlite3.connect(db) as conn:
        quota, claim_count = conn.execute(
            """SELECT
                 (SELECT remaining_chat_quota FROM usage WHERE user_id='U1'),
                 (SELECT COUNT(*) FROM daily_food_log_events WHERE event_id='CLAIM-1')"""
        ).fetchone()
    assert (quota, claim_count) == (1, 0)


def test_ai_estimate_quota_exception_releases_claim(tmp_path, monkeypatch):
    db = tmp_path / "ai-estimate-quota-error.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
    monkeypatch.setattr(
        server, "check_permission_and_quota",
        lambda _uid: (_ for _ in ()).throw(sqlite3.OperationalError("forced quota read failure")),
    )
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    ai_calls = []
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_args, **_kwargs: ai_calls.append(True),
    )
    server.processed_messages.clear()
    event = _text_event(
        "AI-QUOTA-ERROR-1",
        "請用一般估算記錄 早餐 火星果汁 300ml",
        user_id="U-AI-QUOTA",
    )
    with pytest.raises(sqlite3.OperationalError, match="quota read failure"):
        server.handle_message(event)
    with sqlite3.connect(db) as conn:
        pending = conn.execute(
            """SELECT COUNT(*) FROM daily_food_log_events
               WHERE event_id='line-ai:U-AI-QUOTA:AI-QUOTA-ERROR-1'
                 AND action='ai_estimate_pending'"""
        ).fetchone()[0]
    assert pending == 0
    assert ai_calls == []


def test_ai_estimate_webhook_replay_precedes_quota_and_openai(tmp_path, monkeypatch):
    db = tmp_path / "ai-estimate-webhook-replay.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES ('U-AI-WEBHOOK',1,10,?,'vip','2099-12-31',1)""",
            (today,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO health_profile (user_id,tdee,protein) VALUES ('U-AI-WEBHOOK',2000,100)"
        )
        conn.commit()
    content = (
        "已用一般估算記錄。"
        "[LOG_NUTRITION: CAL=180, PRO=8, NAME=火星果汁 300ml]"
    )
    ai_calls = []
    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: ai_calls.append(kwargs) or fake_response
        ))
    )
    monkeypatch.setattr(server, "client", fake_client)
    monkeypatch.setattr(server, "gc", None)
    replies = []

    def flaky_reply(_token, message):
        replies.append(message.as_json_string())
        if len(replies) == 1:
            raise RuntimeError("simulated LINE reply failure")

    monkeypatch.setattr(server.line_bot_api, "reply_message", flaky_reply)
    event = _text_event(
        "AI-WEBHOOK-REPLAY-1",
        "請用一般估算記錄 早餐 火星果汁 300ml",
        user_id="U-AI-WEBHOOK",
    )
    server.processed_messages.clear()
    with pytest.raises(RuntimeError, match="LINE reply failure"):
        server.handle_message(event)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE health_profile SET today_extra_cal=999 WHERE user_id='U-AI-WEBHOOK'"
        )
        conn.commit()
    server.handle_message(event)
    with sqlite3.connect(db) as conn:
        quota = conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-AI-WEBHOOK'"
        ).fetchone()[0]
        log_count = conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U-AI-WEBHOOK'"
        ).fetchone()[0]
    assert quota == 0
    assert log_count == 1
    assert len(ai_calls) == 1
    assert len(replies) == 2
    assert replies[1] == replies[0]


def test_ai_food_log_gate_requires_explicit_estimate_recording_request():
    assert server.should_ai_create_food_log("無糖豆漿熱量多少？") is False
    assert server.should_ai_create_food_log("這個便當蛋白質多少") is False
    assert server.should_ai_create_food_log("請用一般估算記錄 火星果汁 300ml") is True


def test_parse_natural_food_log_intent_normalizes_volume_and_rejects_questions():
    parsed = server.parse_natural_food_log_intent("我要紀錄飲食 無糖豆漿 400cc")
    assert parsed == {
        "food_name": "無糖豆漿", "amount": 400.0,
        "unit": "ml", "meal_slot": "",
    }
    assert server.parse_natural_food_log_intent("幫我記 早餐 無糖豆漿 一瓶") == {
        "food_name": "無糖豆漿", "amount": 1.0,
        "unit": "package", "meal_slot": "早餐",
    }
    assert server.parse_natural_food_log_intent("我喝了無糖豆漿400毫升") == {
        "food_name": "無糖豆漿", "amount": 400.0,
        "unit": "ml", "meal_slot": "",
    }
    assert server.parse_natural_food_log_intent("早餐喝 無糖豆漿 1份") == {
        "food_name": "無糖豆漿", "amount": 1.0,
        "unit": "serving", "meal_slot": "早餐",
    }
    assert server.parse_natural_food_log_intent("無糖豆漿400cc熱量多少？") is None
    assert server.parse_natural_food_log_intent("我要紀錄飲食") is None
    assert server.parse_natural_food_log_intent("我要紀錄飲食嗎？") is None
    assert server.parse_natural_food_log_intent("我要記錄飲食 無糖豆漿熱量多少？") is None
    for question in (
        "我吃了蘋果嗎？", "我吃了蘋果會胖嗎？",
        "早餐吃了蘋果會胖嗎？", "幫我記一下蘋果熱量？",
        "幫我記一下蘋果熱量幾卡",
        "我吃了蘋果嗎。", "幫我記一下蘋果熱量多少。",
        "幫我記一下蘋果熱量幾卡！",
    ):
        assert server.parse_natural_food_log_intent(question) is None
    assert server.parse_natural_food_log_intent("我喝了500ml無糖豆漿") == {
        "food_name": "無糖豆漿", "amount": 500.0,
        "unit": "ml", "meal_slot": "",
    }
    assert server.parse_natural_food_log_intent("幫我記一下我吃了蘋果") == {
        "food_name": "蘋果", "amount": None,
        "unit": "", "meal_slot": "",
    }
    assert server.parse_natural_food_log_intent("幫我記一下無糖豆漿400ml") == {
        "food_name": "無糖豆漿", "amount": 400.0,
        "unit": "ml", "meal_slot": "",
    }
    assert server.parse_natural_food_log_intent("無糖豆漿喝了400ml") == {
        "food_name": "無糖豆漿", "amount": 400.0,
        "unit": "ml", "meal_slot": "",
    }
    assert server.ai_estimate_meal_slot(
        "請用一般估算記錄 早餐 火星果汁 300ml"
    ) == "早餐"
    with pytest.raises(ValueError, match="0.1"):
        server._natural_food_servings(
            {"product_name": "無糖豆漿", "servings_per_package": 1},
            0.00001, "serving",
        )

    item = {
        "product_name": "無糖豆漿", "package_amount": 375,
        "package_unit": "ml", "servings_per_package": 1,
    }
    assert server._natural_food_servings(item, 2, "package") == 2
    assert server._natural_food_servings(item, 1.5, "serving") == 1.5


def test_fallback_parses_multiline_meal_reply_when_hidden_tag_is_missing():
    answer = """好的，酪梨的熱量我來估算一下！
- 📝 品項：酪梨 90g
- 🔥 本次熱量：約 144 大卡
- 🥩 本次蛋白：未知
- 🔥 今日熱量結算：611 大卡
- 🥩 今日蛋白結算：11.5 克

*(此為一般預估，實際熱量依店家有異)*"""

    parsed = server.parse_log_nutrition_fallback(
        answer,
        "我要紀錄飲食 酪梨 90g",
    )

    assert parsed == {
        "source": "fallback",
        "match": None,
        "cal": 144,
        "pro": None,
        "name": "酪梨 90g",
    }


def test_natural_food_unit_mismatch_offers_executable_ai_estimate(tmp_path, monkeypatch):
    db = tmp_path / "natural-unit-mismatch.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,fingerprint,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,
                created_at,updated_at)
               VALUES ('AVOCADO-SERVING','酪梨','fixture-avocado-serving',
                       'manual','system','public',1,'份',1,
                       '{\"calories_kcal\":160,\"protein_g\":2}',?,?)""",
            (server.tw_now().isoformat(), server.tw_now().isoformat()),
        )
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES ('U-MISMATCH',1,10,?,'vip','2099-12-31',1)""",
            (server.tw_today().isoformat(),),
        )
        conn.commit()
    provider_calls = []
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(dict(request)) or {
            "food_name": "酪梨", "portion_assumption": "100g",
            "calories_kcal": {"estimate": 160, "min": 140, "max": 190},
            "protein_g": {"estimate": 2, "min": 1, "max": 3},
            "provenance": {"provider": "test", "model": "mock", "method": "text_meal_estimate"},
        },
    )

    reply = server.build_natural_food_log_reply(
        user_id="U-MISMATCH", message_id="M-MISMATCH",
        event=SimpleNamespace(webhook_event_id="E-MISMATCH"),
        request={
            "food_name": "酪梨", "amount": 100.0,
            "unit": "g", "meal_slot": "午餐",
        },
    )

    rendered = json.dumps(reply.as_json_dict(), ensure_ascii=False)
    assert "AI 營養估算（尚未記錄）" in rendered
    assert "酪梨" in rendered and "140–190 kcal" in rendered
    assert "確認後才會寫入" in rendered
    assert provider_calls == [{
        "food_name": "酪梨", "amount": 100.0,
        "unit": "g", "meal_slot": "午餐",
    }]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U-MISMATCH'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT user_id,status,meal_slot FROM pending_text_meal_estimates"
        ).fetchone() == ("U-MISMATCH", "pending", "午餐")
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-MISMATCH'"
        ).fetchone()[0] == 0


def _daily_ledger_db(tmp_path, monkeypatch, name="daily-ledger.db"):
    db = tmp_path / name
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """INSERT OR REPLACE INTO health_profile
               (user_id,name,today_extra_cal,today_extra_pro,today_food_items,
                today_date,tdee,protein,sheet_name)
               VALUES ('U1','',0,0,'',?,2000,100,'')""",
            (server.tw_today().isoformat(),),
        )
        conn.commit()
    return db


def test_daily_food_log_refreshes_open_health_check_case(tmp_path, monkeypatch):
    from vip_health_check import (
        configure_vip_health_check_connection,
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
    )

    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-refresh.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    started_at = server.tw_now() - server.timedelta(minutes=5)
    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn,
            user_id="U1",
            first_vip_activation_id="activation-live-refresh",
            activation_event_key="event-live-refresh",
            activated_at=started_at,
        )
        server.create_daily_food_log(
            conn,
            user_id="U1",
            product_name="測試早餐",
            meal_slot="早餐",
            consumed_at=server.tw_now().isoformat(timespec="seconds"),
            servings=1,
            nutrition={"calories_kcal": 300, "protein_g": 15},
            source_type="user_private_food",
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()[0] == 1


def test_maintenance_gate_refreshes_existing_case_while_enrollment_flag_is_off(
    tmp_path, monkeypatch,
):
    """The enrollment flag must not strand an already-created three-day case."""
    from vip_health_check import (
        configure_vip_health_check_connection,
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
    )

    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-maintenance-gate.db")
    now = server.tw_now().replace(microsecond=0)
    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn, user_id="EXISTING-CASE",
            first_vip_activation_id="activation-maintenance",
            activation_event_key="event-maintenance",
            activated_at=now - server.timedelta(days=3),
        )
        conn.commit()

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    with sqlite3.connect(db) as conn:
        for day_offset in (2, 1, 0):
            day = server.tw_today() - server.timedelta(days=day_offset)
            for hour, slot in ((8, "早餐"), (12, "午餐")):
                server.create_daily_food_log(
                    conn, user_id="EXISTING-CASE", product_name=f"{day}-{slot}",
                    meal_slot=slot,
                    consumed_at=f"{day.isoformat()}T{hour:02d}:00:00+08:00",
                    servings=1,
                    nutrition={"calories_kcal": 300, "protein_g": 15},
                    source_type="user_private_food",
                    operation_key=f"maintenance:{day}:{slot}",
                )
        # Maintenance refresh is lookup-only: a user without a case stays without one.
        server.create_daily_food_log(
            conn, user_id="NO-CASE", product_name="不應建案的餐", meal_slot="早餐",
            consumed_at=now.isoformat(), servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 10},
            source_type="user_private_food", operation_key="maintenance:no-case",
        )
        conn.commit()
        assert conn.execute(
            "SELECT status,valid_day_count FROM vip_health_check_cases WHERE case_id=?",
            (case["case_id"],),
        ).fetchone() == ("ready_for_review", 3)
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
            (case["case_id"],),
        ).fetchone() == (6,)
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_cases WHERE user_id='NO-CASE'"
        ).fetchone() == (0,)


def test_both_health_check_gates_off_remain_fail_closed(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-both-gates-off.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", False)
    calls = []
    monkeypatch.setattr(
        server, "refresh_user_health_check_case",
        lambda *_args, **_kwargs: calls.append(True),
    )
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U1", product_name="只進主帳本", meal_slot="早餐",
            consumed_at=server.tw_now().isoformat(), servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 10},
            source_type="user_private_food",
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone() == (1,)
    assert calls == []


def test_refresh_failure_leaves_visible_pending_reconciliation(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-refresh-pending.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    with sqlite3.connect(db) as conn:
        from vip_health_check import (
            configure_vip_health_check_connection,
            create_first_vip_health_check_case,
            ensure_vip_health_check_schema,
        )

        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn, user_id="U1", first_vip_activation_id="pending-activation",
            activation_event_key="pending-event",
            activated_at=server.tw_now() - server.timedelta(minutes=5),
        )
        conn.commit()
        monkeypatch.setattr(
            server, "refresh_user_health_check_case",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("refresh unavailable")
            ),
        )
        server.create_daily_food_log(
            conn, user_id="U1", product_name="待補償的餐", meal_slot="午餐",
            consumed_at=server.tw_now().isoformat(), servings=1,
            nutrition={"calories_kcal": 500, "protein_g": 25},
            source_type="user_private_food", operation_key="pending-reconciliation",
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone() == (1,)
        assert conn.execute(
            "SELECT case_id,status,attempts,last_error FROM health_check_refresh_reconciliation "
            "WHERE user_id='U1'"
        ).fetchone() == (case["case_id"], "pending", 1, "refresh unavailable")


def test_refresh_failure_does_not_create_pending_without_owned_active_case(
    tmp_path, monkeypatch,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-no-case-pending.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    monkeypatch.setattr(
        server, "refresh_user_health_check_case",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("refresh infrastructure unavailable")
        ),
    )
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="NO-CASE", product_name="仍應入帳", meal_slot="午餐",
            consumed_at=server.tw_now().isoformat(), servings=1,
            nutrition={"calories_kcal": 500, "protein_g": 25},
            source_type="user_private_food", operation_key="no-case-no-debt",
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='NO-CASE'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM health_check_refresh_reconciliation"
        ).fetchone() == (0,)


def test_noop_terminal_refresh_does_not_clear_case_scoped_pending(tmp_path, monkeypatch):
    from vip_health_check import (
        configure_vip_health_check_connection,
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
    )

    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-terminal-pending.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn, user_id="TERMINAL", first_vip_activation_id="terminal-activation",
            activation_event_key="terminal-event",
            activated_at=server.tw_now() - server.timedelta(minutes=5),
        )
        conn.execute(
            "UPDATE vip_health_check_cases SET status='cancelled' WHERE case_id=?",
            (case["case_id"],),
        )
        now = server.tw_now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO health_check_refresh_reconciliation
               (case_id,user_id,status,attempts,last_error,first_failed_at,last_failed_at)
               VALUES (?,?,'pending',1,'old failure',?,?)""",
            (case["case_id"], "TERMINAL", now, now),
        )
        conn.commit()
        server.create_daily_food_log(
            conn, user_id="TERMINAL", product_name="終態後記餐", meal_slot="午餐",
            consumed_at=server.tw_now().isoformat(), servings=1,
            nutrition={"calories_kcal": 500, "protein_g": 25},
            source_type="user_private_food", operation_key="terminal-no-clear",
        )
        conn.commit()
        assert conn.execute(
            "SELECT case_id,status FROM health_check_refresh_reconciliation"
        ).fetchall() == [(case["case_id"], "pending")]


def test_combo_commits_food_logs_when_refresh_and_pending_write_both_fail(
    tmp_path, monkeypatch, capsys,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "combo-pending-write-failure.db")
    case_id, log_ids = _seed_ready_health_check_case(db, monkeypatch)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    monkeypatch.setattr(server, "build_post_commit_food_dashboard", lambda _uid: "saved")
    with sqlite3.connect(db) as conn:
        food_ids = [row[0] for row in conn.execute(
            "SELECT food_id FROM food_logs ORDER BY log_id"
        )]
        for food_id, (name, _servings) in zip(
            food_ids, server.BREAKFAST_COMBOS["早餐1"]
        ):
            conn.execute(
                "UPDATE food_catalog SET product_name=? WHERE food_id=?", (name, food_id)
            )
        conn.execute("""CREATE TRIGGER reject_manifest_update
            BEFORE UPDATE ON vip_health_check_cases
            BEGIN SELECT RAISE(ABORT, 'refresh failed'); END""")
        conn.execute("DROP TABLE health_check_refresh_reconciliation")
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0]

    assert server._build_breakfast_combo_reply_once(
        "U1", "早餐1", "pending-write-failure"
    ) == "saved"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == (
            before + len(server.BREAKFAST_COMBOS["早餐1"])
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM combo_log_events WHERE user_id='U1'"
        ).fetchone() == (1,)
    assert "待補狀態未保存" in capsys.readouterr().err


def test_combo_never_claims_saved_if_pending_trigger_rolls_back_transaction(
    tmp_path, monkeypatch, capsys,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "combo-pending-rollback.db")
    _case_id, _log_ids = _seed_ready_health_check_case(db, monkeypatch)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    monkeypatch.setattr(server, "build_post_commit_food_dashboard", lambda _uid: "saved")
    with sqlite3.connect(db) as conn:
        food_ids = [row[0] for row in conn.execute(
            "SELECT food_id FROM food_logs ORDER BY log_id"
        )]
        for food_id, (name, _servings) in zip(
            food_ids, server.BREAKFAST_COMBOS["早餐1"]
        ):
            conn.execute(
                "UPDATE food_catalog SET product_name=? WHERE food_id=?", (name, food_id)
            )
        conn.execute("""CREATE TRIGGER reject_manifest_update_for_rollback_probe
            BEFORE UPDATE ON vip_health_check_cases
            BEGIN SELECT RAISE(ABORT, 'refresh failed'); END""")
        conn.execute("""CREATE TRIGGER rollback_pending_write
            BEFORE INSERT ON health_check_refresh_reconciliation
            BEGIN SELECT RAISE(ROLLBACK, 'pending rollback'); END""")
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0]

    with pytest.raises(RuntimeError, match="飲食紀錄未保存"):
        server._build_breakfast_combo_reply_once("U1", "早餐1", "pending-rollback")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == before
        assert conn.execute(
            "SELECT COUNT(*) FROM combo_log_events WHERE user_id='U1'"
        ).fetchone() == (0,)
    stderr = capsys.readouterr().err
    assert "待補狀態未保存" in stderr
    assert "交易已回滾" in stderr


def test_meal_photo_postback_selects_slot_before_confirm_and_refreshes_health_check(
    tmp_path, monkeypatch,
):
    from vip_health_check import (
        configure_vip_health_check_connection,
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
    )

    db = _daily_ledger_db(tmp_path, monkeypatch, "meal-photo-slot-health-check.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    monkeypatch.setattr(server, "_read_valid_nutrition_image", lambda _ref: b"safe")
    monkeypatch.setattr(
        server.client.chat.completions, "create",
        lambda **_kwargs: pytest.fail("meal-slot selection/confirmation must not rerun AI"),
    )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: capture_dashboard_reply(replies, message)
    )
    now = server.tw_now()
    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn, user_id="U1", first_vip_activation_id="activation-photo-slot",
            activation_event_key="event-photo-slot",
            activated_at=now - server.timedelta(minutes=5),
        )
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-SLOT-INTEGRATION",
            payload=ai_estimated_payload(),
            source_image_ref="nutrition-image:" + "f" * 32 + ".jpg",
            meal_slot="早餐", consumed_at=now.isoformat(timespec="seconds"),
            workflow_version="user_confirmed_ai_nutrition_v2",
        )

    def postback(data, event_id):
        server.handle_meal_photo_postback(SimpleNamespace(
            postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id="U1"),
            reply_token=f"reply-{event_id}", webhook_event_id=event_id,
            timestamp=int(now.timestamp() * 1000),
        ))

    postback(f"mp:v1:{token}:1:meal:晚餐", "PHOTO-SLOT-SELECT")
    selected_card = json.loads(replies[-1].contents.as_json_string())
    assert "餐別：晚餐（確認前可更改）" in json.dumps(selected_card, ensure_ascii=False)
    postback(f"mp:v1:{token}:2:confirm_estimate", "PHOTO-SLOT-CONFIRM")

    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        rows = conn.execute(
            "SELECT log_id,meal_slot FROM food_logs WHERE user_id='U1'"
        ).fetchall()
        source_rows = conn.execute(
            "SELECT food_log_id FROM vip_health_check_source_refs WHERE case_id=?",
            (case["case_id"],),
        ).fetchall()
    assert draft["status"] == "user_confirmed"
    assert rows == [(draft["confirmed_log_id"], "晚餐")]
    assert source_rows == [(draft["confirmed_log_id"],)]
    assert replies[-1].alt_text == "今日總覽"
    assert "今日總覽" in json.dumps(
        json.loads(replies[-1].as_json_string()), ensure_ascii=False
    )


def test_delayed_meal_slot_postback_after_adjust_and_confirm_replays_recorded_state(
    tmp_path, monkeypatch,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "meal-photo-delayed-slot-confirmed.db")
    image_ref = "nutrition-image:" + "d" * 32 + ".jpg"
    monkeypatch.setattr(server, "_read_valid_nutrition_image", lambda _ref: b"safe")
    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", True)
    provider_calls = []
    revised = ai_estimated_payload()

    def fake_provider(_image_ref, _original_payload, _correction):
        provider_calls.append(True)
        return revised

    monkeypatch.setattr(server, "_estimate_adjusted_meal_photo", fake_provider)
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: capture_dashboard_reply(replies, message)
    )
    consumed_at = "2026-09-13T18:20:00+08:00"
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-DELAYED-SLOT",
            payload=ai_estimated_payload(), source_image_ref=image_ref,
            meal_slot="早餐", consumed_at=consumed_at,
            workflow_version="user_confirmed_ai_nutrition_v2",
        )

    def postback(data, event_id):
        server.handle_meal_photo_postback(SimpleNamespace(
            postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id="U1"),
            reply_token=f"reply-{event_id}", webhook_event_id=event_id,
            timestamp=1789314000000,
        ))

    postback(f"mp:v1:{token}:1:meal:晚餐", "DELAYED-SLOT")
    postback(f"mp:v1:{token}:2:request_adjust", "DELAYED-ADJUST-REQUEST")
    server.processed_messages.clear()
    server._handle_message_impl(_text_event(
        "DELAYED-ADJUST-TEXT", "飯少一半", user_id="U1"
    ))
    assert len(provider_calls) == 1
    postback(f"mp:v1:{token}:4:confirm_estimate", "DELAYED-CONFIRM")
    postback(f"mp:v1:{token}:1:meal:晚餐", "DELAYED-SLOT")
    postback(f"mp:v1:{token}:4:confirm_estimate", "DELAYED-CONFIRM")

    assert len(provider_calls) == 1
    assert all(message.alt_text == "今日總覽" for message in replies[-3:])
    rendered_cards = [message.as_json_dict() for message in replies[-3:]]
    assert rendered_cards[0] == rendered_cards[1] == rendered_cards[2]
    for card in rendered_cards:
        rendered = json.dumps(card, ensure_ascii=False)
        assert "今日總覽" in rendered
        assert "我要修改飲食紀錄" in rendered
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        logs = conn.execute(
            "SELECT log_id,meal_slot,consumed_at FROM food_logs WHERE user_id='U1'"
        ).fetchall()
        saved_slot_result = json.loads(conn.execute(
            "SELECT result_json FROM meal_photo_events WHERE event_id='DELAYED-SLOT'"
        ).fetchone()[0])
    assert logs == [(draft["confirmed_log_id"], "晚餐", consumed_at)]
    assert draft["version"] == 5
    assert saved_slot_result == {"kind": "estimate", "version": 2}


@pytest.mark.parametrize("replay_action", ["slot", "adjust", "old_confirm", "new_confirm"])
def test_recorded_updated_replay_repairs_failed_health_check_projection(
    tmp_path, monkeypatch, replay_action,
):
    from vip_health_check import (
        configure_vip_health_check_connection,
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
    )

    db = _daily_ledger_db(tmp_path, monkeypatch, "meal-photo-revised-delayed-actions.db")
    image_ref = "nutrition-image:" + "e" * 32 + ".jpg"
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    monkeypatch.setattr(server, "_read_valid_nutrition_image", lambda _ref: b"safe")
    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", True)
    provider_calls = []

    def fake_provider(_image_ref, _original_payload, _correction):
        provider_calls.append(True)
        return ai_estimated_payload()

    monkeypatch.setattr(server, "_estimate_adjusted_meal_photo", fake_provider)
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: capture_dashboard_reply(replies, message)
    )
    now = server.tw_now().replace(microsecond=0)
    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn, user_id="U1", first_vip_activation_id=f"activation-{replay_action}",
            activation_event_key=f"activation-event-{replay_action}",
            activated_at=now - server.timedelta(minutes=5),
        )
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-REVISED-DELAYED",
            payload=ai_estimated_payload(), source_image_ref=image_ref,
            meal_slot="早餐", consumed_at=now.isoformat(),
            workflow_version="user_confirmed_ai_nutrition_v2",
        )

    def postback(data, event_id):
        server.handle_meal_photo_postback(SimpleNamespace(
            postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id="U1"),
            reply_token=f"reply-{event_id}", webhook_event_id=event_id,
            timestamp=1789314000000,
        ))

    postback(f"mp:v1:{token}:1:meal:晚餐", "REVISED-DELAYED-SLOT")
    postback(f"mp:v1:{token}:2:request_adjust", "REVISED-DELAYED-ADJUST")
    server.processed_messages.clear()
    server._handle_message_impl(_text_event(
        "REVISED-DELAYED-ADJUST-TEXT", "飯少一半", user_id="U1"
    ))
    postback(f"mp:v1:{token}:4:confirm_estimate", "REVISED-DELAYED-CONFIRM")
    assert len(provider_calls) == 1

    with sqlite3.connect(db) as conn:
        initial_ref = conn.execute(
            "SELECT food_log_version,source_hash FROM vip_health_check_source_refs "
            "WHERE case_id=?", (case["case_id"],),
        ).fetchone()
        assert initial_ref is not None and initial_ref[0] == 1
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        log_id = draft["confirmed_log_id"]
        revision2 = create_meal_photo_revision_draft(
            conn, user_id="U1", log_id=log_id, from_version=1,
            request_text="白飯改半碗",
            estimate=_dashboard_revision_estimate(conn, log_id, calories=510, protein=31),
        )
        confirm_user_meal_photo_revision(
            conn, event_id="REVISED-DELAYED-REVISION-2", user_id="U1",
            log_id=log_id, from_version=1, draft_token=revision2["token"],
        )
        revision3 = create_meal_photo_revision_draft(
            conn, user_id="U1", log_id=log_id, from_version=2,
            request_text="再加一顆蛋",
            estimate=_dashboard_revision_estimate(conn, log_id, calories=590, protein=38),
        )
        confirm_user_meal_photo_revision(
            conn, event_id="REVISED-DELAYED-REVISION-3", user_id="U1",
            log_id=log_id, from_version=2, draft_token=revision3["token"],
        )
        assert conn.execute(
            "SELECT version FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone() == (3,)
        stale_manifest = conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()[0]
        conn.execute("""CREATE TRIGGER reject_recorded_updated_refresh
            BEFORE UPDATE ON vip_health_check_cases
            BEGIN SELECT RAISE(ABORT, 'temporary recorded-updated refresh failure'); END""")
        conn.commit()

    replies.clear()
    replay = {
        "slot": (f"mp:v1:{token}:1:meal:晚餐", "REVISED-DELAYED-SLOT"),
        "adjust": (f"mp:v1:{token}:2:request_adjust", "REVISED-DELAYED-ADJUST"),
        "old_confirm": (f"mp:v1:{token}:4:confirm_estimate", "REVISED-DELAYED-CONFIRM"),
        "new_confirm": (f"mp:v1:{token}:4:confirm_estimate", "REVISED-DELAYED-LATE-CONFIRM"),
    }[replay_action]
    postback(*replay)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT food_log_version,source_hash FROM vip_health_check_source_refs "
            "WHERE case_id=?", (case["case_id"],),
        ).fetchone() == initial_ref
        conn.execute("DROP TRIGGER reject_recorded_updated_refresh")
        canonical_tables = (
            "food_logs", "daily_food_log_events", "nutrition_sheet_outbox",
            "pending_meal_photo_drafts", "meal_photo_events",
        )
        committed = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in canonical_tables
        }
        conn.commit()

    postback(*replay)

    assert len(provider_calls) == 1
    assert [message.type for message in replies] == ["flex", "flex"]
    assert all(message.alt_text == "今日總覽" for message in replies)
    payloads = [message.as_json_dict() for message in replies]
    rendered = json.dumps(payloads, ensure_ascii=False)

    def visible_texts(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                yield node["text"]
            for value in node.values():
                yield from visible_texts(value)
        elif isinstance(node, list):
            for value in node:
                yield from visible_texts(value)

    # Inspect displayed numbers, not CSS colours (e.g. #FF6B35) or IDs.
    import re as regex
    for payload in payloads:
        numeric_tokens = set(regex.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", "\n".join(visible_texts(payload))))
        # v53 displays latest meal calories plus remaining protein (100 - 38).
        # The canonical exact 590/38 snapshot remains asserted below from DB.
        assert {"590", "62"}.issubset(numeric_tokens)
        assert not {"680", "35", "510", "31"}.intersection(numeric_tokens)
    assert "mealrev:v1:" not in rendered
    assert "foodlog:v1:" not in rendered
    with sqlite3.connect(db) as conn:
        assert {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in canonical_tables
        } == committed
        current = user_confirmed_meal_photo_trust_projection(
            conn, log_id, "user_confirmed_ai_estimate",
        )
        canonical_row = conn.execute(
            "SELECT version,consumed_at,meal_slot FROM food_logs WHERE log_id=?",
            (log_id,),
        ).fetchone()
        expected_source_hash = _expected_health_source_v2_hash(
            log_id=log_id,
            version=canonical_row[0],
            nutrition=current["nutrition"],
            consumed_at=canonical_row[1],
            meal_slot=canonical_row[2],
            trust_binding=current["effective_revision_hash"],
        )
        refreshed_ref = conn.execute(
            "SELECT food_log_version,source_hash FROM vip_health_check_source_refs "
            "WHERE case_id=? AND food_log_id=?", (case["case_id"], log_id),
        ).fetchone()
        refreshed_manifest = conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()[0]
        assert refreshed_ref == (3, expected_source_hash)
        assert refreshed_manifest != stale_manifest
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events "
            "WHERE log_id=? AND action='confirm_ai_revision'", (log_id,)
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT version,nutrition_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone() == (3, json.dumps({"calories_kcal": 590.0, "protein_g": 38.0}, sort_keys=True))


@pytest.mark.parametrize("terminal_status", ["cancelled", "expired"])
def test_delayed_meal_slot_postback_renders_terminal_state_without_reviving_draft(
    tmp_path, monkeypatch, terminal_status,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, f"delayed-slot-{terminal_status}.db")
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    monkeypatch.setattr(
        server.client.chat.completions, "create",
        lambda **_kwargs: pytest.fail("terminal replay must not call AI"),
    )
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id=f"SLOT-{terminal_status}",
            payload=ai_estimated_payload(), meal_slot="早餐",
            consumed_at="2026-09-13T08:00:00+08:00",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        apply_meal_photo_action(
            conn, event_id=f"SLOT-EVENT-{terminal_status}", user_id="U1",
            token=token, expected_version=1, action="set_meal_slot", value="午餐",
        )
        if terminal_status == "cancelled":
            apply_meal_photo_action(
                conn, event_id="CANCEL-AFTER-SLOT", user_id="U1", token=token,
                expected_version=2, action="cancel",
            )
        else:
            conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET status='expired',observed_payload_json='{}',answers_json='{}',
                       estimate_json='{}',version=3 WHERE token=?""",
                (token,),
            )
            conn.commit()

    server.handle_meal_photo_postback(SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:meal:午餐"),
        source=SimpleNamespace(user_id="U1"), reply_token="terminal-replay",
        webhook_event_id=f"SLOT-EVENT-{terminal_status}", timestamp=1789314000000,
    ))

    assert len(replies) == 1
    assert replies[0].type == "text"
    assert ("已取消" if terminal_status == "cancelled" else "已逾時") in replies[0].text
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert draft["status"] == terminal_status
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone() == (0,)
        assert json.loads(conn.execute(
            "SELECT result_json FROM meal_photo_events WHERE event_id=?",
            (f"SLOT-EVENT-{terminal_status}",),
        ).fetchone()[0]) == {"kind": "estimate", "version": 2}


def test_daily_food_log_survives_best_effort_health_check_refresh_failure(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-refresh-failure.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    calls = []

    def fail_refresh(*args, **kwargs):
        calls.append((args, kwargs))
        raise sqlite3.OperationalError("refresh unavailable")

    monkeypatch.setattr(
        server, "refresh_user_health_check_case", fail_refresh, raising=False
    )
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn,
            user_id="U1",
            product_name="仍要保留的餐",
            meal_slot="午餐",
            consumed_at=server.tw_now().isoformat(timespec="seconds"),
            servings=1,
            nutrition={"calories_kcal": 500, "protein_g": 25},
            source_type="user_private_food",
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U1'"
        ).fetchone()[0] == 1
    assert len(calls) == 1


def _seed_ready_health_check_case(db, monkeypatch):
    from vip_health_check import (
        configure_vip_health_check_connection,
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
    )

    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    now = server.tw_now()
    log_ids = []
    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn,
            user_id="U1",
            first_vip_activation_id="activation-edit-refresh",
            activation_event_key="event-edit-refresh",
            activated_at=now - server.timedelta(days=3),
        )
        for day_offset in (2, 1, 0):
            day = server.tw_today() - server.timedelta(days=day_offset)
            for hour, slot in ((8, "早餐"), (12, "午餐")):
                result = server.create_daily_food_log(
                    conn,
                    user_id="U1",
                    product_name=f"{day.isoformat()}-{slot}",
                    meal_slot=slot,
                    consumed_at=f"{day.isoformat()}T{hour:02d}:00:00+08:00",
                    servings=1,
                    nutrition={"calories_kcal": 300, "protein_g": 15},
                    source_type="user_private_food",
                    operation_key=f"seed:{day.isoformat()}:{slot}",
                )
                log_ids.append(result["log_id"])
        conn.commit()
        state = conn.execute(
            "SELECT status,valid_day_count FROM vip_health_check_cases WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()
        assert state == ("ready_for_review", 3)
    return case["case_id"], log_ids


def test_food_log_edit_refreshes_manifest_and_delete_downgrades_case(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-edit-refresh.db")
    case_id, log_ids = _seed_ready_health_check_case(db, monkeypatch)
    edited_log = log_ids[0]
    with sqlite3.connect(db) as conn:
        before = conn.execute(
            "SELECT food_log_version,source_hash FROM vip_health_check_source_refs "
            "WHERE case_id=? AND food_log_id=?",
            (case_id, edited_log),
        ).fetchone()

    server.apply_daily_food_log_edit(
        user_id="U1", log_id=edited_log, expected_version=1,
        event_id="edit-health-check-1", action="correct_nutrition",
        field="calories_kcal", value=321,
    )
    with sqlite3.connect(db) as conn:
        after = conn.execute(
            "SELECT food_log_version,source_hash FROM vip_health_check_source_refs "
            "WHERE case_id=? AND food_log_id=?",
            (case_id, edited_log),
        ).fetchone()
        assert after[0] == 2
        assert after[1] != before[1]

    server.apply_daily_food_log_edit(
        user_id="U1", log_id=log_ids[1], expected_version=1,
        event_id="delete-health-check-1", action="delete",
    )
    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT status,valid_day_count FROM vip_health_check_cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        refs = conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
    assert state == ("collecting", 2)
    assert refs == 5


def test_clear_daily_food_ledger_refreshes_and_downgrades_case(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-clear-refresh.db")
    case_id, _log_ids = _seed_ready_health_check_case(db, monkeypatch)

    server.clear_daily_food_ledger("U1", event_id="clear-health-check-1")

    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT status,valid_day_count FROM vip_health_check_cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        refs = conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
    assert state == ("collecting", 2)
    assert refs == 4


def test_operation_key_replay_repairs_a_failed_health_check_refresh(tmp_path, monkeypatch):
    from vip_health_check import (
        configure_vip_health_check_connection,
        create_first_vip_health_check_case,
        ensure_vip_health_check_schema,
        refresh_user_health_check_case as real_refresh,
    )

    db = _daily_ledger_db(tmp_path, monkeypatch, "health-check-replay-repair.db")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    with sqlite3.connect(db) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        case = create_first_vip_health_check_case(
            conn, user_id="U1", first_vip_activation_id="activation-repair",
            activation_event_key="event-repair",
            activated_at=server.tw_now() - server.timedelta(minutes=5),
        )
        monkeypatch.setattr(
            server, "refresh_user_health_check_case",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("temporary refresh failure")
            ),
        )
        first = server.create_daily_food_log(
            conn, user_id="U1", product_name="可修復早餐", meal_slot="早餐",
            consumed_at=server.tw_now().isoformat(timespec="seconds"), servings=1,
            nutrition={"calories_kcal": 250, "protein_g": 12},
            source_type="user_private_food", operation_key="repair-op-1",
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT case_id,user_id,status FROM health_check_refresh_reconciliation
               WHERE case_id=?""",
            (case["case_id"],),
        ).fetchone() == (case["case_id"], "U1", "pending")
        monkeypatch.setattr(server, "refresh_user_health_check_case", real_refresh)
        replay = server.create_daily_food_log(
            conn, user_id="U1", product_name="可修復早餐", meal_slot="早餐",
            consumed_at=server.tw_now().isoformat(timespec="seconds"), servings=1,
            nutrition={"calories_kcal": 250, "protein_g": 12},
            source_type="user_private_food", operation_key="repair-op-1",
        )
        conn.commit()
        assert replay["replayed"] is True
        assert replay["log_id"] == first["log_id"]
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
            (case["case_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM health_check_refresh_reconciliation WHERE case_id=?",
            (case["case_id"],),
        ).fetchone() == (0,)


@pytest.mark.parametrize("operation", ["edit", "delete", "clear", "quick", "breakfast", "label"])
@pytest.mark.parametrize("fail_first_refresh", [False, True])
def test_food_entry_manifest_replay_and_failure_isolation(
    tmp_path, monkeypatch, operation, fail_first_refresh,
):
    """Real SQLite projection repair must not duplicate committed ledger/outbox events."""
    db = _daily_ledger_db(tmp_path, monkeypatch, f"manifest-{operation}.db")
    case_id, log_ids = _seed_ready_health_check_case(db, monkeypatch)
    token = ""
    with sqlite3.connect(db) as conn:
        food_ids = [row[0] for row in conn.execute(
            "SELECT food_id FROM food_logs ORDER BY log_id"
        )]
        if operation == "breakfast":
            for food_id, (name, _servings) in zip(food_ids, server.BREAKFAST_COMBOS["早餐1"]):
                conn.execute("UPDATE food_catalog SET product_name=? WHERE food_id=?", (name, food_id))
        if operation == "label":
            token = save_pending_label(conn, user_id="U1", payload=valid_label())
        before = conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        before_refs = conn.execute(
            "SELECT * FROM vip_health_check_source_refs WHERE case_id=? ORDER BY food_log_id", (case_id,)
        ).fetchall()
        if fail_first_refresh:
            conn.execute("""CREATE TRIGGER reject_manifest_update
                BEFORE UPDATE ON vip_health_check_cases
                BEGIN SELECT RAISE(ABORT, 'temporary refresh update failure'); END""")

    monkeypatch.setattr(server, "get_active_nutrition_target", lambda *_: None)
    monkeypatch.setattr(server, "sync_confirmed_nutrition_to_sheet", lambda *_: None)
    monkeypatch.setattr(server, "apply_confirmed_nutrition_to_legacy_dashboard", lambda *_: None)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    def run():
        if operation in {"edit", "delete"}:
            return server.apply_daily_food_log_edit(
                user_id="U1", log_id=log_ids[0], expected_version=1,
                event_id="manifest-mutation", action="delete" if operation == "delete" else "correct_nutrition",
                **({} if operation == "delete" else {"field": "calories_kcal", "value": 321}),
            )
        if operation == "clear":
            return server.clear_daily_food_ledger("U1", event_id="manifest-clear")
        if operation == "quick":
            return server._quick_log_catalog_card_once(
                user_id="U1", food_id=food_ids[0], servings=1,
                meal_slot="晚餐", event_ref="manifest-quick", display_quantity="1份",
            )
        if operation == "breakfast":
            return server._build_breakfast_combo_reply_once("U1", "早餐1", "manifest-breakfast")
        # Re-delivery gets through the transient in-memory duplicate cache.
        server.processed_messages.clear()
        return server._handle_message_impl(_text_event(
            "manifest-label", f"確認營養紀錄:{token}", user_id="U1",
        ))

    run()
    with sqlite3.connect(db) as conn:
        after = conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        assert (after == before) is fail_first_refresh
        if fail_first_refresh:
            assert conn.execute(
                "SELECT * FROM vip_health_check_source_refs WHERE case_id=? ORDER BY food_log_id", (case_id,)
            ).fetchall() == before_refs
            conn.execute("DROP TRIGGER reject_manifest_update")
        # Capture authoritative data, not just counts, before replay repairs the projection.
        tables = ["food_logs", "nutrition_sheet_outbox", "daily_food_log_events", "health_profile"]
        if operation == "breakfast":
            tables += ["combo_log_events", "frequent_foods"]
        committed = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    run()
    with sqlite3.connect(db) as conn:
        repaired = conn.execute(
            "SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        assert repaired != before
        assert {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables} == committed
        refs = conn.execute(
            "SELECT food_log_id,food_log_version FROM vip_health_check_source_refs WHERE case_id=? ORDER BY food_log_id", (case_id,)
        ).fetchall()
        expected = conn.execute(
            "SELECT log_id,version FROM food_logs WHERE user_id='U1' AND confirmation_status='confirmed' "
            "AND COALESCE(deleted_at,'')='' ORDER BY log_id"
        ).fetchall()
        assert refs == expected


def test_daily_food_ledger_separates_today_and_yesterday_and_keeps_unknown_na(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch)
    today = server.tw_today()
    yesterday = today - server.timedelta(days=1)
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U1", product_name="火腿蛋吐司", meal_slot="早餐",
            consumed_at=f"{today.isoformat()}T08:30:00+08:00", servings=1,
            nutrition={"calories_kcal": 208, "protein_g": None},
            source_type="ai_text_estimate",
        )
        server.create_daily_food_log(
            conn, user_id="U1", product_name="昨日優格", meal_slot="點心",
            consumed_at=f"{yesterday.isoformat()}T15:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 62, "protein_g": 4},
            source_type="user_private_food",
        )
        conn.commit()

    today_ledger = server.get_daily_food_ledger("U1", today.isoformat())
    yesterday_ledger = server.get_daily_food_ledger("U1", yesterday.isoformat())
    assert [row["product_name"] for row in today_ledger["items"]] == ["火腿蛋吐司"]
    assert [row["product_name"] for row in yesterday_ledger["items"]] == ["昨日優格"]
    assert today_ledger["totals"]["calories_kcal"] == 208
    assert today_ledger["totals"]["protein_g"] is None
    assert today_ledger["unknown_fields"] == {"protein_g", "fat_g", "carbohydrate_g"}

    flex = server.build_daily_food_ledger_flex("U1", "today", page=0)
    payload = flex.as_json_dict()
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "今日飲食總結" in rendered
    assert "火腿蛋吐司" in rendered
    assert "NA" in rendered
    assert "調整份量" in rendered
    assert "修正營養" in rendered


def test_daily_food_trust_tamper_is_explicit_in_ledger_ui_and_health_report(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-trust-projection.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-TRUST-UI",
            payload=meal_photo_payload(), consumed_at=f"{today}T12:10:00+08:00",
            meal_slot="午餐",
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "one_bowl"), ("vegetable_portion", "one_bowl"),
            ("cooking_oil", "unknown"), ("sauce_level", "unknown"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"TRUST-UI-ANSWER-{index}", user_id="U1",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )
        confirmed = apply_meal_photo_action(
            conn, event_id="TRUST-UI-CONFIRM", user_id="U1", token=token,
            expected_version=9, action="confirm_estimate",
        )
        log_id = confirmed["result"]["log_id"]
        server.create_daily_food_log(
            conn, user_id="U1", product_name="文字估算", meal_slot="點心",
            consumed_at=f"{today}T15:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 120, "protein_g": 6},
            source_type="ai_text_estimate",
        )
        server.create_daily_food_log(
            conn, user_id="U1", product_name="部署前合計（無逐筆明細）", meal_slot="",
            consumed_at=f"{today}T00:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 80, "protein_g": 4},
            source_type="legacy_daily_carryover",
        )
        conn.commit()

    valid = server.get_daily_food_ledger("U1", today)
    confirmed_item = next(item for item in valid["items"] if item["log_id"] == log_id)
    assert confirmed_item["trust_type"] == "user_confirmed_ai_estimate"
    assert "顧客確認・AI估算" in json.dumps(
        server._daily_food_item_bubble(confirmed_item), ensure_ascii=False
    )
    controls = {
        item["product_name"]: json.dumps(server._daily_food_item_bubble(item), ensure_ascii=False)
        for item in valid["items"] if item["log_id"] != log_id
    }
    assert "來源：AI 預估" in controls["文字估算"]
    assert "資料完整性驗證未通過" not in controls["文字估算"]
    assert "資料完整性驗證未通過" not in controls["部署前合計（無逐筆明細）"]

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE food_logs SET trust_type='' WHERE log_id=?", (log_id,))
        conn.commit()

    ledger = server.get_daily_food_ledger("U1", today)
    tampered = next(item for item in ledger["items"] if item["log_id"] == log_id)
    assert tampered["trust_type"] == "untrusted_user_confirmed_ai_estimate"
    assert tampered["trust_integrity_status"] == "integrity_verification_failed"
    assert all(tampered["nutrition"].get(field) is None for field in server.DAILY_FOOD_NUTRIENT_FIELDS)
    rendered = json.dumps(server._daily_food_item_bubble(tampered), ensure_ascii=False)
    assert "資料完整性驗證未通過，營養資料暫不可用" in rendered
    assert "🔥 NA" in rendered and "🥩 NA" in rendered
    assert "🔥 0" not in rendered and "🥩 0" not in rendered

    monkeypatch.setattr(server, "ADMIN_UID", "U1")
    monkeypatch.setattr(server, "fetch_daily_intervals_summary", lambda *_: None)
    monkeypatch.setattr(server, "get_daily_nutrition_target", lambda *_: None)
    report = server.build_jason_daily_health_report("U1", today)
    assert "資料完整性驗證未通過，營養資料暫不可用" in report
    tampered_line = next(line for line in report.splitlines() if "餐點照片" in line)
    assert "NA kcal｜蛋白質NAg" in tampered_line
    assert "0 kcal｜蛋白質0g" not in tampered_line


def test_daily_food_nutrition_correction_is_field_only_idempotent_and_recalculates_today(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-correction.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="火腿蛋吐司", meal_slot="早餐",
            consumed_at=f"{today}T08:30:00+08:00", servings=1,
            nutrition={"calories_kcal": 208, "protein_g": 12, "fat_g": 7, "carbohydrate_g": 27},
            source_type="ai_text_estimate",
        )
        conn.commit()

    first = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log["log_id"], expected_version=1,
        event_id="event-correct-1", action="correct_nutrition",
        field="calories_kcal", value=230,
    )
    replay = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log["log_id"], expected_version=1,
        event_id="event-correct-1", action="correct_nutrition",
        field="calories_kcal", value=230,
    )
    assert first["version"] == 2
    assert replay["replayed"] is True
    assert replay["version"] == 2
    assert first["nutrition"] == {
        "calories_kcal": 230.0, "protein_g": 12,
        "fat_g": 7, "carbohydrate_g": 27,
    }
    with sqlite3.connect(db) as conn:
        hp = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_food_items FROM health_profile WHERE user_id='U1'"
        ).fetchone()
        version = conn.execute("SELECT version FROM food_logs WHERE log_id=?", (log["log_id"],)).fetchone()[0]
    assert hp == (230.0, 12.0, "火腿蛋吐司")
    assert version == 2
    with pytest.raises(ValueError, match="已更新"):
        server.apply_daily_food_log_edit(
            user_id="U1", log_id=log["log_id"], expected_version=1,
            event_id="event-stale", action="correct_nutrition",
            field="calories_kcal", value=240,
        )


def test_daily_food_portion_adjustment_scales_all_known_nutrients(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-portion.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="火腿蛋吐司", meal_slot="早餐",
            consumed_at=f"{today}T08:30:00+08:00", servings=1,
            nutrition={"calories_kcal": 230, "protein_g": 12, "fat_g": 8, "carbohydrate_g": 28},
            source_type="user_correction",
        )
        conn.commit()

    result = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log["log_id"], expected_version=1,
        event_id="event-portion-1", action="set_servings", value=0.5,
    )
    assert result["servings"] == 0.5
    assert result["nutrition"] == {
        "calories_kcal": 115.0, "protein_g": 6.0,
        "fat_g": 4.0, "carbohydrate_g": 14.0,
    }


def test_daily_food_carousel_never_exceeds_line_limit_and_keeps_page_context(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-pages.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        for idx in range(11):
            server.create_daily_food_log(
                conn, user_id="U1", product_name=f"食品{idx + 1}", meal_slot="點心",
                consumed_at=f"{today}T{8 + idx:02d}:00:00+08:00", servings=1,
                nutrition={"calories_kcal": 100 + idx, "protein_g": 5},
                source_type="user_private_food",
            )
        conn.commit()

    flex = server.build_daily_food_ledger_flex("U1", "today", page=0)
    payload = flex.as_json_dict()
    bubbles = payload["contents"]["contents"]
    assert len(bubbles) == 12
    assert {bubble.get("size") for bubble in bubbles} == {"kilo"}
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "foodlog:v1:day:today:page:1" in rendered


def test_daily_food_line_nutrition_flow_requires_confirmation_and_applies_field_only(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-line-flow.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="火腿蛋吐司", meal_slot="早餐",
            consumed_at=f"{today}T08:30:00+08:00", servings=1,
            nutrition={"calories_kcal": 208, "protein_g": 12},
            source_type="ai_text_estimate",
        )
        conn.commit()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))

    field_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"foodlog:v1:{log['log_id']}:1:nutrition:field:calories_kcal"),
        source=SimpleNamespace(user_id="U1"), reply_token="field", webhook_event_id="FIELD-1",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(field_event)
    assert "請輸入正確的熱量" in replies[-1].text
    server.processed_messages.clear()
    server._handle_message_impl(_text_event("FOODLOG-NUM-1", "230", user_id="U1"))
    confirmation = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "208 kcal → 230 kcal" in confirmation
    assert f"foodlog:v1:{log['log_id']}:1:nutrition:apply:calories_kcal:230:once" in confirmation

    apply_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"foodlog:v1:{log['log_id']}:1:nutrition:apply:calories_kcal:230:once"),
        source=SimpleNamespace(user_id="U1"), reply_token="apply", webhook_event_id="APPLY-1",
        timestamp=1784740620001,
    )
    server.handle_meal_photo_postback(apply_event)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT nutrition_snapshot_json,version FROM food_logs WHERE log_id=?", (log["log_id"],)
        ).fetchone()
    nutrition = json.loads(row[0])
    assert nutrition["calories_kcal"] == 230
    assert nutrition["protein_g"] == 12
    assert row[1] == 2


def test_daily_food_rename_slot_and_soft_delete_are_versioned(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-more.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="吐司", meal_slot="早餐",
            consumed_at=f"{today}T08:30:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 10},
            source_type="ai_text_estimate",
        )
        conn.commit()
    renamed = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log["log_id"], expected_version=1,
        event_id="rename-1", action="rename", value="早餐店A火腿蛋吐司",
    )
    assert renamed["product_name"] == "早餐店A火腿蛋吐司"
    slotted = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log["log_id"], expected_version=2,
        event_id="slot-1", action="set_meal_slot", value="點心",
    )
    assert slotted["version"] == 3
    deleted = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log["log_id"], expected_version=3,
        event_id="delete-1", action="delete",
    )
    assert deleted["version"] == 4
    assert server.get_daily_food_ledger("U1", today)["items"] == []
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT deleted_at,version FROM food_logs WHERE log_id=?", (log["log_id"],)
        ).fetchone()
    assert row[0]
    assert row[1] == 4


def test_food_record_command_opens_today_yesterday_picker(tmp_path, monkeypatch):
    _daily_ledger_db(tmp_path, monkeypatch, "daily-picker.db")
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    server.processed_messages.clear()
    server._handle_message_impl(_text_event("FOODLOG-PICKER-1", "飲食紀錄", user_id="U1"))
    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "今天的紀錄" in rendered
    assert "昨天的紀錄" in rendered
    assert "foodlog:v1:day:today:page:0" in rendered
    assert "foodlog:v1:day:yesterday:page:0" in rendered


def test_legacy_today_totals_are_carried_once_without_inventing_item_details(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-migration.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U1", product_name="已知逐筆", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 10},
            source_type="user_private_food",
        )
        conn.execute(
            "UPDATE health_profile SET today_extra_cal=611,today_extra_pro=30,today_date=? WHERE user_id='U1'",
            (today,),
        )
        conn.commit()
        assert server.migrate_current_day_legacy_totals_to_ledger(conn) == 1
        assert server.migrate_current_day_legacy_totals_to_ledger(conn) == 0
    ledger = server.get_daily_food_ledger("U1", today)
    assert [item["product_name"] for item in ledger["items"]] == [
        "部署前合計（無逐筆明細）", "已知逐筆"
    ]
    assert ledger["known_totals"]["calories_kcal"] == 611
    assert ledger["known_totals"]["protein_g"] == 30
    rendered = json.dumps(server.build_daily_food_ledger_flex("U1", "today").as_json_dict(), ensure_ascii=False)
    assert "部署前合計" in rendered
    assert "無逐筆明細" in rendered


def test_daily_food_card_converts_utc_timestamp_to_taipei_time(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-timezone.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U1", product_name="時區測試", meal_slot="早餐",
            consumed_at=f"{today}T00:30:00+00:00", servings=1,
            nutrition={"calories_kcal": 100}, source_type="user_private_food",
        )
        conn.commit()
    rendered = json.dumps(server.build_daily_food_ledger_flex("U1", "today").as_json_dict(), ensure_ascii=False)
    assert "08:30｜早餐" in rendered


def test_text_edit_replays_confirmation_after_line_reply_failure_without_double_mutation(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-reply-replay.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="豆漿", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 10},
            source_type="user_private_food",
        )
        conn.commit()
    server.set_daily_food_edit_state("U1", log["log_id"], 1, "portion_value")
    event = _text_event("FOODLOG-REPLAY-1", "0.5", user_id="U1")
    calls = []

    def flaky_reply(_token, message):
        calls.append(message)
        if len(calls) == 1:
            raise RuntimeError("simulated LINE reply failure")

    monkeypatch.setattr(server.line_bot_api, "reply_message", flaky_reply)
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    server.processed_messages.clear()
    with pytest.raises(RuntimeError, match="simulated"):
        server.handle_message(event)
    assert server.get_daily_food_edit_state("U1")["input_type"] == "completed"

    server.handle_message(event)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT consumed_servings,nutrition_snapshot_json,version FROM food_logs WHERE log_id=?",
            (log["log_id"],),
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE log_id=?", (log["log_id"],)
        ).fetchone()[0]
    assert row[0] == 0.5
    assert json.loads(row[1])["calories_kcal"] == 100
    assert row[2] == 2
    assert event_count == 1
    assert len(calls) == 2
    assert server.get_daily_food_edit_state("U1") is None


def test_daily_food_edit_rolls_back_when_event_insert_fails(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-atomicity.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="原子餐", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 10}, source_type="user_private_food",
        )
        server._sync_health_profile_from_ledger_conn(conn, "U1", today)
        conn.execute("""CREATE TRIGGER fail_daily_event BEFORE INSERT ON daily_food_log_events
                        BEGIN SELECT RAISE(ABORT, 'forced event failure'); END""")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="forced event failure"):
        server.apply_daily_food_log_edit(
            user_id="U1", log_id=log["log_id"], expected_version=1,
            event_id="atomic-fail", action="set_servings", value=2,
        )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT consumed_servings,nutrition_snapshot_json,version FROM food_logs WHERE log_id=?",
            (log["log_id"],),
        ).fetchone()
        hp = conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'"
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM daily_food_log_events").fetchone()[0] == 0
    assert row[0] == 1
    assert json.loads(row[1])["calories_kcal"] == 200
    assert row[2] == 1
    assert hp == (200, 10)


def test_create_daily_food_log_operation_key_prevents_duplicate_rows(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-operation-key.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        first = server.create_daily_food_log(
            conn, user_id="U1", product_name="重送餐", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 300}, source_type="ai_text_estimate",
            operation_key="line-ai:U1:M1",
        )
        conn.commit()
        second = server.create_daily_food_log(
            conn, user_id="U1", product_name="重送餐", meal_slot="午餐",
            consumed_at=f"{today}T12:01:00+08:00", servings=1,
            nutrition={"calories_kcal": 300}, source_type="ai_text_estimate",
            operation_key="line-ai:U1:M1",
        )
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    assert second["log_id"] == first["log_id"]
    assert second["replayed"] is True


def test_legacy_recent_adjustments_update_the_linked_ledger_log(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-legacy-edit.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS recent_meal_logs (
            user_id TEXT PRIMARY KEY,meal_name TEXT,base_cal REAL,base_pro REAL,
            current_cal REAL,current_pro REAL,meal_date TEXT,source_text TEXT,
            updated_at TEXT,food_log_id TEXT DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS frequent_foods (
            user_id TEXT,meal_name TEXT,last_cal REAL,last_pro REAL,use_count INTEGER DEFAULT 1,
            last_used TEXT,PRIMARY KEY(user_id,meal_name))""")
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="原餐", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 10}, source_type="ai_text_estimate",
        )
        server._sync_health_profile_from_ledger_conn(conn, "U1", today)
        conn.execute(
            "INSERT INTO recent_meal_logs VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("U1", "原餐", 200, 10, 200, 10, today, "AI", server.tw_now().isoformat(), log["log_id"]),
        )
        conn.execute(
            "INSERT INTO frequent_foods VALUES (?,?,?,?,?,?)",
            ("U1", "新餐", 300, 20, 1, server.tw_now().isoformat()),
        )
        conn.commit()
    server.apply_portion_adjustment("U1", "少量")
    ledger = server.get_daily_food_ledger("U1", today)
    assert ledger["items"][0]["nutrition"]["calories_kcal"] == 140
    assert ledger["items"][0]["nutrition"]["protein_g"] == 7
    server.replace_recent_meal_with_name("U1", "新餐")
    ledger = server.get_daily_food_ledger("U1", today)
    assert ledger["items"][0]["product_name"] == "新餐"
    assert ledger["items"][0]["nutrition"]["calories_kcal"] == 300
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'"
        ).fetchone() == (300, 20)


def test_soft_delete_syncs_deleted_status_to_food_log_sheet(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-delete-sheet.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="刪除餐", meal_slot="晚餐",
            consumed_at=f"{today}T18:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 500}, source_type="user_private_food",
        )
        conn.commit()
    server.apply_daily_food_log_edit(
        user_id="U1", log_id=log["log_id"], expected_version=1,
        event_id="delete-sheet", action="delete",
    )
    rows = []
    class Sheet:
        def find(self, *_args, **_kwargs):
            return None
        def append_row(self, values, **_kwargs):
            rows.append(values)
    monkeypatch.setattr(server, "_nutrition_ws", lambda _title: Sheet())
    server._sync_food_log_outbox(log["log_id"])
    assert rows[0][26] == "deleted"


def test_verified_v2_food_log_sheet_projects_only_kcal_protein_and_source(tmp_path, monkeypatch):
    db = tmp_path / "ai-v2-sheet.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [{"name": "雞腿飯", "portion": "約1份", "calories_kcal": 680, "protein_g": 35}],
        "calories_kcal": {"estimate": 680, "min": 580, "max": 800},
        "protein_g": {"estimate": 35, "min": 29, "max": 43},
        "confidence": 0.78,
        "provenance": {"provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="V2-SHEET", payload=payload,
            source_image_ref="nutrition-image:" + "a" * 32 + ".jpg",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        result = apply_meal_photo_action(
            conn, event_id="V2-SHEET-CONFIRM", user_id="U1", token=token,
            expected_version=draft["version"], action="confirm_estimate",
        )
        log_id = result["result"]["log_id"]

    rows = []
    class Sheet:
        def find(self, *_args, **_kwargs): return None
        def append_row(self, values, **_kwargs): rows.append(values)
    monkeypatch.setattr(server, "_nutrition_ws", lambda _title: Sheet())
    server._sync_food_log_outbox(log_id)
    assert rows[-1][9:13] == [680.0, 35.0, "", ""]
    assert rows[-1][16:24] == [""] * 8
    assert rows[-1][29:] == ["user_confirmed_ai_estimate", "meal-photo-user-confirmation-v2"]

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE food_logs SET nutrition_snapshot_json='{\"calories_kcal\":999,\"protein_g\":35}' WHERE log_id=?", (log_id,))
        conn.commit()
    server._sync_food_log_outbox(log_id)
    assert rows[-1][9:24] == [""] * 15
    assert rows[-1][29:] == ["untrusted_user_confirmed_ai_estimate", ""]


def test_clear_daily_food_ledger_soft_deletes_today_only_and_is_replay_safe(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-clear.db")
    today = server.tw_today().isoformat()
    yesterday = (server.tw_today() - server.timedelta(days=1)).isoformat()
    with sqlite3.connect(db) as conn:
        today_log = server.create_daily_food_log(
            conn, user_id="U1", product_name="今日餐", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 500, "protein_g": 25}, source_type="ai_text_estimate",
        )
        yesterday_log = server.create_daily_food_log(
            conn, user_id="U1", product_name="昨日餐", meal_slot="晚餐",
            consumed_at=f"{yesterday}T18:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 600, "protein_g": 30}, source_type="ai_text_estimate",
        )
        server._sync_health_profile_from_ledger_conn(conn, "U1", today)
        conn.commit()
    result = server.clear_daily_food_ledger("U1", event_id="clear-M1")
    replay = server.clear_daily_food_ledger("U1", event_id="clear-M1")
    assert result["deleted_count"] == 1
    assert replay["deleted_count"] == 1 and replay["replayed"] is True
    assert server.get_daily_food_ledger("U1", today)["count"] == 0
    assert server.get_daily_food_ledger("U1", yesterday)["count"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT confirmation_status FROM food_logs WHERE log_id=?", (today_log["log_id"],)
        ).fetchone()[0] == "deleted"
        assert conn.execute(
            "SELECT confirmation_status FROM food_logs WHERE log_id=?", (yesterday_log["log_id"],)
        ).fetchone()[0] == "confirmed"
        assert conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_food_items FROM health_profile WHERE user_id='U1'"
        ).fetchone() == (0, 0, "")
        assert conn.execute(
            "SELECT COUNT(*) FROM nutrition_sheet_outbox WHERE entity_type='food_log' AND status='pending'"
        ).fetchone()[0] >= 1


def test_clear_daily_food_ledger_rolls_back_if_event_write_fails(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-clear-atomic.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        log = server.create_daily_food_log(
            conn, user_id="U1", product_name="不可半清", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 500, "protein_g": 25}, source_type="ai_text_estimate",
        )
        server._sync_health_profile_from_ledger_conn(conn, "U1", today)
        conn.execute("""CREATE TRIGGER fail_clear_event BEFORE INSERT ON daily_food_log_events
                        BEGIN SELECT RAISE(ABORT, 'forced clear event failure'); END""")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="forced clear event failure"):
        server.clear_daily_food_ledger("U1", event_id="clear-fail")
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT confirmation_status,deleted_at,version FROM food_logs WHERE log_id=?", (log["log_id"],)
        ).fetchone() == ("confirmed", "", 1)
        assert conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'"
        ).fetchone() == (500, 25)


def test_clear_daily_food_ledger_preserves_processing_lease_and_requeues_old_worker_result(
    tmp_path, monkeypatch
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "daily-clear-outbox-lease.db")
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        logs = [
            server.create_daily_food_log(
                conn, user_id="U1", product_name=f"餐點{index}", meal_slot="午餐",
                consumed_at=f"{today}T12:0{index}:00+08:00", servings=1,
                nutrition={"calories_kcal": 100 + index}, source_type="ai_text_estimate",
            )
            for index in range(3)
        ]
        ids = [item["log_id"] for item in logs]
        conn.execute(
            """UPDATE nutrition_sheet_outbox
               SET status='processing',attempts=4,last_error='old',
                   claimed_at='2026-09-13T10:00:00+08:00',lease_owner='old-worker'
               WHERE entity_type='food_log' AND entity_id=?""",
            (ids[0],),
        )
        conn.execute(
            """UPDATE nutrition_sheet_outbox
               SET status='pending',attempts=2,last_error='retry',
                   claimed_at='stale-claim',lease_owner='stale-owner'
               WHERE entity_type='food_log' AND entity_id=?""",
            (ids[1],),
        )
        conn.execute(
            """UPDATE nutrition_sheet_outbox SET status='synced',synced_at='old-sync'
               WHERE entity_type='food_log' AND entity_id=?""",
            (ids[2],),
        )
        conn.commit()

    first = server.clear_daily_food_ledger("U1", event_id="clear-lease-safe")
    replay = server.clear_daily_food_ledger("U1", event_id="clear-lease-safe")
    assert first["deleted_count"] == 3
    assert replay == {**first, "replayed": True}

    with sqlite3.connect(db) as conn:
        processing = conn.execute(
            """SELECT status,attempts,last_error,claimed_at,lease_owner,resync_required
               FROM nutrition_sheet_outbox WHERE entity_type='food_log' AND entity_id=?""",
            (ids[0],),
        ).fetchone()
        assert processing == (
            "processing", 4, "old", "2026-09-13T10:00:00+08:00", "old-worker", 1
        )
        assert conn.execute(
            """SELECT status,attempts,last_error,claimed_at,lease_owner,resync_required,synced_at
               FROM nutrition_sheet_outbox WHERE entity_type='food_log' AND entity_id=?""",
            (ids[1],),
        ).fetchone() == ("pending", 2, "", "", "", 0, "")
        assert conn.execute(
            """SELECT status,last_error,claimed_at,lease_owner,resync_required,synced_at
               FROM nutrition_sheet_outbox WHERE entity_type='food_log' AND entity_id=?""",
            (ids[2],),
        ).fetchone() == ("pending", "", "", "", 0, "")

        # The worker that owned the pre-delete snapshot completes late.  Its fenced
        # completion must expose the fresh delete as pending instead of losing it.
        conn.execute(
            """UPDATE nutrition_sheet_outbox
               SET status=CASE WHEN resync_required=1 THEN 'pending' ELSE 'synced' END,
                   synced_at=CASE WHEN resync_required=1 THEN '' ELSE 'late-sync' END,
                   resync_required=0,last_error='',claimed_at='',lease_owner=''
               WHERE entity_type='food_log' AND entity_id=?
                 AND status='processing' AND lease_owner='old-worker'""",
            (ids[0],),
        )
        conn.commit()
        assert conn.execute(
            "SELECT status,resync_required FROM nutrition_sheet_outbox WHERE entity_id=?",
            (ids[0],),
        ).fetchone() == ("pending", 0)

    synced = []
    monkeypatch.setattr(server, "sh", object())
    monkeypatch.setattr(server, "_sync_food_log_outbox", lambda entity_id: synced.append(entity_id))
    assert server.flush_nutrition_sheet_outbox() == 3
    assert set(synced) == set(ids)


def test_image_magic_and_opaque_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    jpeg = b"\xff\xd8\xff" + b"x" * 100
    assert server._validate_image_bytes(jpeg) == ".jpg"
    ref = server._store_nutrition_image(jpeg, ".jpg")
    assert ref.startswith("nutrition-image:")
    assert "U" not in ref
    path = server._nutrition_image_path(ref)
    assert path is not None
    assert path.startswith(str(tmp_path))


def test_partial_reserved_image_is_atomically_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
    path = server._nutrition_image_path(ref)
    assert path is not None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    image_bytes = b"\xff\xd8\xff" + b"x" * 100
    assert server._store_nutrition_image(image_bytes, ".jpg", ref) == ref
    with open(path, "rb") as image_file:
        assert image_file.read() == image_bytes
    assert not [name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")]


def test_plan_exact_meal_precedes_newer_wildcard(monkeypatch):
    rows = [
        plan_row(plan_id="wild", 版本=99, 星期="每日", 餐別="全日", 熱量目標=2200),
        plan_row(plan_id="exact", 版本=1, 星期="週二", 餐別="晚餐", 熱量目標=500),
    ]
    monkeypatch.setattr(server, "_nutrition_ws", lambda title: FakeWorksheet(rows))
    plan = server.get_active_nutrition_target("U1", "晚餐", "2026-07-21T18:00:00+08:00")
    assert plan is not None
    assert plan["plan_id"] == "exact"
    assert plan["targets"]["calories_kcal"] == 500
    assert plan["consumption_meal_slot"] == "晚餐"


def test_all_day_plan_uses_all_day_consumption(monkeypatch):
    monkeypatch.setattr(server, "_nutrition_ws", lambda title: FakeWorksheet([plan_row()]))
    plan = server.get_active_nutrition_target("U1", "晚餐", "2026-07-21")
    assert plan is not None
    assert plan["meal_slot"] == "全日"
    assert plan["consumption_meal_slot"] == ""


def test_daily_target_prefers_all_day_row_over_meal_rows(monkeypatch):
    rows = [
        plan_row(plan_id="daily", 餐別="全日", 星期="週二", 主食份=6, 低脂蛋白份=13),
        plan_row(plan_id="breakfast", 餐別="早餐", 星期="週二", 主食份=3, 低脂蛋白份=4),
    ]
    monkeypatch.setattr(server, "_nutrition_ws", lambda _: FakeWorksheet(rows))

    target = server.get_daily_nutrition_target("U1", "2026-07-21")

    assert target["starch_exchange"] == 6
    assert target["protein_low_exchange"] == 13


def test_daily_target_sums_latest_row_for_each_meal_when_no_all_day(monkeypatch):
    rows = [
        plan_row(plan_id="breakfast-old", 版本=1, 餐別="早餐", 星期="週二", 主食份=1, 低脂蛋白份=2),
        plan_row(plan_id="breakfast-new", 版本=2, 餐別="早餐", 星期="週二", 主食份=2, 低脂蛋白份=3),
        plan_row(plan_id="dinner", 版本=1, 餐別="晚餐", 星期="週二", 主食份=4, 低脂蛋白份=5),
    ]
    monkeypatch.setattr(server, "_nutrition_ws", lambda _: FakeWorksheet(rows))

    target = server.get_daily_nutrition_target("U1", "2026-07-21")

    assert target["starch_exchange"] == 6
    assert target["protein_low_exchange"] == 8


def test_legacy_outbox_migration_deduplicates_and_adds_unique_index(tmp_path):
    db = tmp_path / "legacy-outbox.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE nutrition_sheet_outbox (
            outbox_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '', created_at TEXT NOT NULL, synced_at TEXT DEFAULT '')""")
        conn.execute("INSERT INTO nutrition_sheet_outbox VALUES ('o1','food','f1','synced',1,'','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO nutrition_sheet_outbox VALUES ('o2','food','f1','pending',2,'','2026-01-02','')")
        ensure_nutrition_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM nutrition_sheet_outbox WHERE entity_type='food' AND entity_id='f1'").fetchone()[0] == 1
        server._queue_nutrition_outbox(conn, "food", "f1")
        conn.commit()
        assert conn.execute("SELECT status FROM nutrition_sheet_outbox WHERE entity_type='food' AND entity_id='f1'").fetchone()[0] == "pending"
        indexes = conn.execute("PRAGMA index_list(nutrition_sheet_outbox)").fetchall()
        assert any(row[1] == "idx_nutrition_outbox_entity" and row[2] == 1 for row in indexes)


def test_outbox_keeps_partial_failure_pending(tmp_path, monkeypatch):
    db = tmp_path / "outbox.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute("""INSERT INTO nutrition_sheet_outbox
            (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
            VALUES ('o1','food','f1','pending',0,'','2026-01-01','')""")
        conn.execute("""INSERT INTO nutrition_sheet_outbox
            (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
            VALUES ('o2','food_log','l1','pending',0,'','2026-01-01','')""")
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "sh", object())
    monkeypatch.setattr(server, "_sync_food_outbox", lambda entity_id: None)

    def fail(_):
        raise RuntimeError("temporary sheet failure")

    monkeypatch.setattr(server, "_sync_food_log_outbox", fail)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT outbox_id, status FROM nutrition_sheet_outbox"))
        attempts = conn.execute("SELECT attempts FROM nutrition_sheet_outbox WHERE outbox_id='o2'").fetchone()[0]
    assert rows == {"o1": "synced", "o2": "pending"}
    assert attempts == 1


def test_stale_outbox_lease_preserves_dirty_resync_signal(tmp_path, monkeypatch):
    db = tmp_path / "stale-outbox.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute(
            """INSERT INTO nutrition_sheet_outbox
               (outbox_id,entity_type,entity_id,status,attempts,last_error,claimed_at,
                lease_owner,resync_required,created_at,synced_at)
               VALUES ('stale','food','f1','processing',0,'','2000-01-01T00:00:00+08:00',
                       'dead-worker',1,'2000-01-01T00:00:00+08:00','')"""
        )
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "sh", object())
    monkeypatch.setattr(server, "_sync_food_outbox", lambda _: None)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        status = conn.execute(
            "SELECT status FROM nutrition_sheet_outbox WHERE outbox_id='stale'"
        ).fetchone()[0]
    assert status == "pending"


def test_legacy_dashboard_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute("""CREATE TABLE health_profile (
            user_id TEXT PRIMARY KEY, today_extra_cal REAL, today_extra_pro REAL,
            today_food_items TEXT, today_date TEXT, tdee REAL, protein REAL)""")
        conn.execute("INSERT INTO health_profile VALUES ('U1',0,0,'',?,2000,100)", (today,))
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,fingerprint,created_at,updated_at)
               VALUES ('f1','測試食品','fixture-f1',?,?)""",
            (today, today),
        )
        conn.execute("""INSERT INTO food_logs
            (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,consumed_amount,
             consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,source_image_ref,
             plan_id,confirmation_status,legacy_applied_at,created_at,updated_at)
            VALUES ('l1','U1','f1',?,'晚餐',1,1,'份','{}','{}','','','confirmed','',?,?)""",
            (today + "T18:00:00+08:00", today, today))
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "upsert_frequent_food", lambda *args: None)
    flex_calls = []
    monkeypatch.setattr(
        server, "build_meal_log_flex",
        lambda *args, **kwargs: flex_calls.append((args, kwargs)) or {"ok": True},
    )
    result = {
        "food": {"product_name": "豆漿"},
        "log": {
            "log_id": "l1", "consumed_at": today + "T18:00:00+08:00",
            "nutrition": {"calories_kcal": 190, "protein_g": 19},
            "exchange": {"protein_low_exchange": 2.71, "starch_exchange": 0.53},
        },
    }
    server.apply_confirmed_nutrition_to_legacy_dashboard("U1", result)
    server.apply_confirmed_nutrition_to_legacy_dashboard("U1", result)
    with sqlite3.connect(db) as conn:
        values = conn.execute("SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id='U1'").fetchone()
    assert values == (190.0, 19.0)
    assert flex_calls[0][1]["exchange_text"] == "低脂蛋白 2.71份｜主食 0.53份"


def test_add_frequent_food_resets_stale_dashboard_totals_before_first_log(tmp_path, monkeypatch):
    db = tmp_path / "frequent-food-cross-day.db"
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE health_profile (
                user_id TEXT PRIMARY KEY, today_extra_cal REAL, today_extra_pro REAL,
                today_food_items TEXT, today_date TEXT, tdee REAL, protein REAL);
            CREATE TABLE frequent_foods (
                user_id TEXT, meal_name TEXT, last_cal REAL, last_pro REAL,
                use_count INTEGER DEFAULT 1, last_used_at TEXT,
                PRIMARY KEY (user_id, meal_name));
            CREATE TABLE recent_meal_logs (
                user_id TEXT PRIMARY KEY, meal_name TEXT, base_cal REAL, base_pro REAL,
                current_cal REAL, current_pro REAL, meal_date TEXT,
                source_text TEXT, updated_at TEXT);
        """)
        conn.execute(
            "INSERT INTO health_profile VALUES ('U1',900,60,'昨天早餐、昨天晚餐','2000-01-01',2000,100)"
        )
        conn.execute(
            "INSERT INTO frequent_foods VALUES ('U1','無糖優格',62,4,1,'2000-01-01')"
        )
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "build_meal_log_flex", lambda *args, **kwargs: {"ok": True})

    server.add_frequent_food_to_today("U1", "無糖優格")

    with sqlite3.connect(db) as conn:
        values = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_food_items,today_date "
            "FROM health_profile WHERE user_id='U1'"
        ).fetchone()
    assert values == (62.0, 4.0, "無糖優格", today)


def test_mark_planned_meal_resets_stale_dashboard_totals_before_first_log(tmp_path, monkeypatch):
    db = tmp_path / "planned-meal-cross-day.db"
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE health_profile (
                user_id TEXT PRIMARY KEY, today_extra_cal REAL, today_extra_pro REAL,
                today_food_items TEXT, today_date TEXT, tdee REAL, protein REAL);
            CREATE TABLE planned_meal_checks (
                user_id TEXT, meal_date TEXT, meal_slot TEXT, meal_name TEXT,
                cal REAL, pro REAL, checked_at TEXT,
                PRIMARY KEY (user_id, meal_date, meal_slot));
            CREATE TABLE recent_meal_logs (
                user_id TEXT PRIMARY KEY, meal_name TEXT, base_cal REAL, base_pro REAL,
                current_cal REAL, current_pro REAL, meal_date TEXT,
                source_text TEXT, updated_at TEXT);
        """)
        conn.execute(
            "INSERT INTO health_profile VALUES ('U1',900,60,'昨天早餐、昨天晚餐','2000-01-01',2000,100)"
        )
        conn.commit()
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "get_dashboard_data",
        lambda user_id: {"today_lunch": "舒肥雞胸餐", "lunch_cal": 500, "lunch_pro": 40},
    )
    monkeypatch.setattr(server, "build_meal_log_flex", lambda *args, **kwargs: {"ok": True})

    server.mark_planned_meal_as_eaten("U1", "午餐")

    with sqlite3.connect(db) as conn:
        values = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_food_items,today_date "
            "FROM health_profile WHERE user_id='U1'"
        ).fetchone()
    assert values == (500.0, 40.0, "舒肥雞胸餐", today)


def test_init_db_migrates_health_profile_status_for_existing_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "volume-status-migration"
    data_dir.mkdir()
    db_path = data_dir / "health.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE health_profile (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                tdee INTEGER,
                protein REAL,
                goal TEXT,
                restrictions TEXT,
                summary_text TEXT,
                active_days TEXT
            )"""
        )

    monkeypatch.setattr(server, "DB_DIR", str(data_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(health_profile)")}
    assert "status" in columns


def test_health_check_uses_configured_sqlite_schema(tmp_path, monkeypatch):
    data_dir = tmp_path / "volume"
    db_path = data_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(data_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    assert server.health_check() == {"status": "ok", "service": "openclawbot"}
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "usage", "health_profile", "food_catalog", "food_logs",
        "nutrition_sheet_outbox", "pending_meal_photo_drafts", "meal_photo_events",
        "meal_photo_schema_versions",
    } <= tables
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE meal_photo_events")
        conn.commit()
    with pytest.raises(server.HTTPException) as exc_info:
        server.health_check()
    assert exc_info.value.status_code == 503


def test_health_check_rejects_meal_photo_schema_version_one(tmp_path, monkeypatch):
    data_dir = tmp_path / "volume-version"
    db_path = data_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(data_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE meal_photo_schema_versions SET version=1 WHERE component='meal_photo_system'"
        )
        conn.commit()
    with pytest.raises(server.HTTPException) as exc_info:
        server.health_check()
    assert exc_info.value.status_code == 503


def test_health_check_rejects_empty_database(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "empty.db"))
    with pytest.raises(server.HTTPException) as exc_info:
        server.health_check()
    assert exc_info.value.status_code == 503


def valid_label():
    return {
        "status": "success", "image_type": "nutrition_label", "product_name": "測試豆漿",
        "brand": "測試", "barcode": "123", "package_amount": 375, "package_unit": "ml",
        "servings_per_package": 1,
        "per_serving": {"calories_kcal": 190, "protein_g": 19, "fat_g": 9, "carbohydrate_g": 8},
        "per_100": {"calories_kcal": 50.67, "protein_g": 5.07, "fat_g": 2.4, "carbohydrate_g": 2.13},
        "confidence": 0.99,
    }



def test_meal_log_flex_can_show_pending_exchange_suggestion():
    flex = server.build_meal_log_flex(
        "測試豆漿", 190, 19, 190, 2000, 19, 100,
        exchange_text="低脂蛋白 2.71份｜主食 0.53份",
    )
    payload = str(flex.as_json_dict())
    assert "推算營養份數" in payload
    assert "低脂蛋白 2.71份" in payload
    assert "尚未扣入個人計畫" in payload


def test_failed_image_delete_keeps_reference_for_retry(tmp_path, monkeypatch):
    db = tmp_path / "cleanup.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = server._store_nutrition_image(b"\xff\xd8\xff" + b"x" * 100, ".jpg")
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U1", payload=valid_label(), source_image_ref=ref)
        conn.execute("UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?", (token,))
        conn.commit()
    original_unlink = server.safe_unlink_nutrition_image

    def fail_unlink(_root, _ref):
        raise OSError("busy")

    monkeypatch.setattr(server, "safe_unlink_nutrition_image", fail_unlink)
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        retained = conn.execute("SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (token,)).fetchone()[0]
    assert retained == ref
    monkeypatch.setattr(server, "safe_unlink_nutrition_image", original_unlink)
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        cleared = conn.execute("SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?", (token,)).fetchone()[0]
    assert cleared == ""


@pytest.mark.parametrize(
    ("root_state", "leaf_state", "expected"),
    [
        ("missing", "missing", False),
        ("directory", "regular", True),
        ("directory", "missing", True),
        ("directory", "symlink", False),
        ("symlink", "regular", False),
    ],
)
def test_delete_nutrition_image_uses_no_follow_root_and_leaf_semantics(
    tmp_path, monkeypatch, root_state, leaf_state, expected
):
    root = tmp_path / "nutrition_images"
    external = tmp_path / "external"
    external.mkdir()
    ref = "nutrition-image:" + "a" * 32 + ".jpg"
    filename = ref.removeprefix("nutrition-image:")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    if root_state == "directory":
        root.mkdir()
        if leaf_state == "regular":
            (root / filename).write_bytes(b"inside")
        elif leaf_state == "symlink":
            (root / filename).symlink_to(outside)
    elif root_state == "symlink":
        (external / filename).write_bytes(b"external")
        root.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))

    assert server._delete_nutrition_image(ref) is expected
    assert outside.read_bytes() == b"outside"
    if root_state == "symlink":
        assert (external / filename).read_bytes() == b"external"
    if leaf_state == "symlink":
        assert (root / filename).is_symlink()


def test_awaiting_identity_expiration_deletes_stored_back_image(tmp_path, monkeypatch):
    db = tmp_path / "awaiting-cleanup.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = server._store_nutrition_image(b"\xff\xd8\xff" + b"x" * 100, ".jpg")
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn, user_id="U1", payload=partial, source_image_ref=ref,
            allow_missing_identity=True,
        )
        conn.execute(
            "UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        status, stored_ref = conn.execute(
            "SELECT status,source_image_ref FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()
    assert status == "expired"
    assert stored_ref == ""
    image_path = server._nutrition_image_path(ref)
    assert image_path is not None
    assert not os.path.exists(image_path)


def test_expired_draft_payload_is_scrubbed_and_tombstone_is_purged(tmp_path, monkeypatch):
    db = tmp_path / "retention.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn, user_id="U_RETENTION", payload=partial,
            source_message_id="BACK_RETENTION", allow_missing_identity=True,
        )
        conn.execute(
            "UPDATE pending_nutrition_logs SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.execute(
            """INSERT INTO nutrition_message_events
               (message_id,user_id,event_type,token,created_at)
               VALUES ('EVENT_OLD','U_RETENTION','test',?,'2000-01-01T00:00:00+08:00')""",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        status, payload, retired_at = conn.execute(
            "SELECT status,label_payload_json,retired_at FROM pending_nutrition_logs WHERE token=?",
            (token,),
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE token=?", (token,)
        ).fetchone()[0]
    assert status == "expired"
    assert payload == "{}"
    assert retired_at
    assert event_count == 0
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_nutrition_logs SET retired_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0] == 0


def test_old_tombstone_keeps_parent_until_young_event_reaches_retention(tmp_path, monkeypatch):
    db = tmp_path / "retention-young-event.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn,
            user_id="U_YOUNG_EVENT",
            payload=partial,
            source_message_id="BACK_YOUNG_EVENT",
            allow_missing_identity=True,
        )
        conn.execute(
            """UPDATE pending_nutrition_logs
               SET status='expired',label_payload_json='{}',source_image_ref='',
                   retired_at='2000-01-01T00:00:00+08:00'
               WHERE token=?""",
            (token,),
        )
        conn.execute(
            """INSERT INTO nutrition_message_events
               (message_id,user_id,event_type,token,created_at)
               VALUES ('EVENT_YOUNG','U_YOUNG_EVENT','test',?,?)""",
            (token, server.tw_now().isoformat(timespec="seconds")),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE token=?", (token,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0] == 1
        conn.execute(
            "UPDATE nutrition_message_events SET created_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE token=?", (token,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0] == 0


def test_nutrition_cleanup_is_registered_hourly():
    calls = []

    class FakeScheduler:
        def add_job(self, func, trigger, **kwargs):
            calls.append((func, trigger, kwargs))

    server.register_nutrition_cleanup_job(FakeScheduler())
    matching = [item for item in calls if item[0] is server.cleanup_nutrition_images]
    assert matching == [(
        server.cleanup_nutrition_images,
        "interval",
        {"hours": 1, "max_instances": 1, "coalesce": True},
    )]


def test_confirmed_label_reference_protects_90_day_image_cleanup(tmp_path, monkeypatch):
    db = tmp_path / "confirmed-cleanup.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = server._store_nutrition_image(b"\xff\xd8\xff" + b"x" * 100, ".jpg")
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U1", payload=valid_label(), source_image_ref=ref)
        result = confirm_pending_label(conn, token=token, user_id="U1", plan_link_status="no_plan")
        conn.execute("UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?", (result["log"]["log_id"],))
        conn.execute("UPDATE nutrition_sheet_outbox SET status='synced'")
        conn.commit()
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        log_ref = conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0]
        food_ref = conn.execute("SELECT original_image_ref FROM food_catalog").fetchone()[0]
        states = dict(conn.execute("SELECT entity_type,status FROM nutrition_sheet_outbox"))
    assert log_ref == food_ref == ref
    assert states["food"] == states["food_log"] == "synced"


def test_plan_link_failure_is_retried(tmp_path, monkeypatch):
    db = tmp_path / "plan-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U1", payload=valid_label(), meal_slot="晚餐", consumed_at="2026-07-21T18:00:00+08:00")
        result = confirm_pending_label(conn, token=token, user_id="U1", plan_link_status="pending")
    monkeypatch.setattr(server, "get_active_nutrition_target", lambda *args: (_ for _ in ()).throw(RuntimeError("sheet down")))
    assert server.retry_pending_nutrition_plan_links() == 0
    monkeypatch.setattr(server, "get_active_nutrition_target", lambda *args: {"plan_id": "plan_recovered"})
    assert server.retry_pending_nutrition_plan_links() == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT plan_id,plan_link_status FROM food_logs WHERE log_id=?", (result["log"]["log_id"],)).fetchone()
        outbox = conn.execute("SELECT status FROM nutrition_sheet_outbox WHERE entity_type='food_log'").fetchone()[0]
    assert row == ("plan_recovered", "linked")
    assert outbox == "pending"


def test_transient_image_failure_allows_webhook_redelivery(tmp_path, monkeypatch):
    db = tmp_path / "transient-image-redelivery.db"
    message_id = "retry-image-1"
    event = SimpleNamespace(
        message=SimpleNamespace(id=message_id),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply",
    )
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    server.processed_messages.discard(message_id)
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(server.line_bot_api, "get_message_content", lambda _: (_ for _ in ()).throw(RuntimeError("temporary")))
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda *_args, **_kwargs: pytest.fail("transient download must not send a reply"),
    )

    # Simulate the provider redelivering the same image twice.  Each failure must
    # release its durable claim so the next delivery reaches the download again.
    for expected_attempts in (1, 2):
        with pytest.raises(RuntimeError, match="temporary"):
            server.handle_image_message(event)
        assert message_id not in server.processed_messages
        with sqlite3.connect(db) as conn:
            claim = conn.execute(
                """SELECT status,attempts,claim_token,lease_until
                   FROM meal_photo_image_events
                   WHERE user_id=? AND source_message_id=?""",
                ("U1", message_id),
            ).fetchone()
        assert claim == ("failed", expected_attempts, "", "")


def test_text_failure_discards_only_failed_event_id(monkeypatch):
    completed_id = "completed-event"
    failed_id = "failed-event"
    server.processed_messages.update({completed_id, failed_id})
    event = SimpleNamespace(
        message=SimpleNamespace(id=failed_id, text="測試"),
        source=SimpleNamespace(user_id="U1"),
    )
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "_handle_message_impl", lambda _: (_ for _ in ()).throw(RuntimeError("temporary")))
    with pytest.raises(RuntimeError):
        server.handle_message(event)
    assert completed_id in server.processed_messages
    assert failed_id not in server.processed_messages
    server.processed_messages.discard(completed_id)


def test_outbox_update_during_processing_is_resynced(tmp_path, monkeypatch):
    db = tmp_path / "outbox-resync.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "sh", object())
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        server._queue_nutrition_outbox(conn, "food", "f1")
        conn.commit()
    calls = []

    def sync_and_dirty(entity_id):
        calls.append(entity_id)
        if len(calls) == 1:
            with sqlite3.connect(db) as conn:
                server._queue_nutrition_outbox(conn, "food", entity_id)
                conn.commit()

    monkeypatch.setattr(server, "_sync_food_outbox", sync_and_dirty)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        first_state = conn.execute("SELECT status,resync_required FROM nutrition_sheet_outbox").fetchone()
    assert first_state == ("pending", 0)
    assert server.flush_nutrition_sheet_outbox() == 1
    with sqlite3.connect(db) as conn:
        final_state = conn.execute("SELECT status,resync_required FROM nutrition_sheet_outbox").fetchone()
    assert final_state == ("synced", 0)
    assert calls == ["f1", "f1"]


def test_server_stages_missing_identity_and_pairs_product_front():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    partial = {**valid_label(), "product_name": "", "brand": ""}
    staged = server.stage_nutrition_label(
        conn, user_id="U1", parsed=partial, source_message_id="BACK1",
        meal_slot="晚餐", consumed_at="2026-07-21T20:42:00+08:00",
    )
    assert staged["needs_identity"] is True
    assert staged["label"]["per_serving"]["calories_kcal"] == 190
    paired = server.pair_product_front(
        conn, user_id="U1", source_message_id="FRONT1",
        parsed={"status": "success", "image_type": "product_front", "product_name": "無加糖高蛋白豆漿", "brand": "測試品牌", "barcode": "123", "confidence": 0.98},
    )
    assert paired["token"] == staged["token"]
    assert paired["label"]["product_name"] == "無加糖高蛋白豆漿"
    assert conn.execute("SELECT status FROM pending_nutrition_logs WHERE token=?", (staged["token"],)).fetchone()[0] == "pending"


def test_photo_time_source_and_servings_survive_product_front_replay():
    conn = sqlite3.connect(":memory:")
    ensure_nutrition_schema(conn)
    partial = {
        **valid_label(),
        "product_name": "",
        "brand": "",
        "observed_at": "2026-07-21T18:00:00+08:00",
        "observed_at_confidence": 0.99,
    }
    staged = server.stage_nutrition_label(
        conn,
        user_id="U1",
        parsed=partial,
        source_message_id="BACK_REPLAY",
        meal_slot="晚餐",
        consumed_at="2026-07-21T18:00:00+08:00",
        consumed_time_source="photo_timestamp",
    )
    front = {
        "status": "success", "image_type": "product_front",
        "product_name": "高蛋白豆漿", "brand": "測試", "barcode": "123", "confidence": 0.95,
    }
    server.pair_product_front(
        conn, user_id="U1", parsed=front, source_message_id="FRONT_REPLAY"
    )
    update_pending_consumption(
        conn,
        user_id="U1",
        token=staged["token"],
        consumed_servings=2,
        consumed_at="2026-07-21T18:00:00+08:00",
        meal_slot="晚餐",
        consumed_time_source="manual",
    )
    replayed = server.pair_product_front(
        conn, user_id="U1", parsed=front, source_message_id="FRONT_REPLAY"
    )
    assert replayed["consumed_servings"] == 2
    assert replayed["consumed_at"] == "2026-07-21T18:00:00+08:00"
    assert replayed["consumed_time_source"] == "manual"


def test_nutrition_vision_prompt_supports_two_photo_flow():
    prompt = server.build_nutrition_vision_prompt()
    assert "product_front" in prompt
    assert "缺少品名仍回傳 status=success" in prompt
    assert "不可因缺少品名丟棄已讀到的營養資料" in prompt


def test_photo_timestamp_becomes_consumed_time_when_confident_and_reasonable():
    received = datetime(2026, 7, 22, 15, 0, tzinfo=server.TW_TZ)
    consumed, source = server.resolve_nutrition_consumed_at(
        {
            "observed_at": "2026-07-21T20:42:00+08:00",
            "observed_at_confidence": 0.98,
        },
        received_at=received,
    )
    assert consumed.isoformat() == "2026-07-21T20:42:00+08:00"
    assert source == "photo_timestamp"
    assert server.current_meal_slot(consumed) == "晚餐"
    prompt = server.build_nutrition_vision_prompt()
    assert "observed_at_confidence" in prompt
    assert "Asia/Taipei" in prompt


@pytest.mark.parametrize(
    "observed_at,confidence",
    [
        ("2026-07-21T20:42:00", 0.99),
        ("2026-07-21T20:42:00+08:00", 0.84),
        ("2026-07-22T15:11:00+08:00", 0.99),
        ("2026-06-21T14:59:59+08:00", 0.99),
        ("不是時間", 0.99),
        ("2026-07-21T20:42:00+08:00", float("nan")),
        ("2026-07-21T20:42:00+08:00", float("inf")),
    ],
)
def test_photo_timestamp_falls_back_for_low_confidence_or_implausible_values(
    observed_at, confidence
):
    received = datetime(2026, 7, 22, 15, 0, tzinfo=server.TW_TZ)
    consumed, source = server.resolve_nutrition_consumed_at(
        {"observed_at": observed_at, "observed_at_confidence": confidence},
        received_at=received,
    )
    assert consumed == received
    assert source == "line_timestamp"


def test_photo_timestamp_exact_reasonableness_boundaries_are_accepted():
    received = datetime(2026, 7, 22, 15, 0, tzinfo=server.TW_TZ)
    for value in (
        "2026-06-22T15:00:00+08:00",
        "2026-07-22T15:10:00+08:00",
        "2026-07-21T12:42:00Z",
    ):
        consumed, source = server.resolve_nutrition_consumed_at(
            {"observed_at": value, "observed_at_confidence": 0.99},
            received_at=received,
        )
        assert source == "photo_timestamp"
        assert consumed.tzinfo == server.TW_TZ


def test_parse_manual_nutrition_correction_command():
    assert server.parse_nutrition_correction_command("修正營養 鈉 48") == ("sodium_mg", 48.0)
    assert server.parse_nutrition_correction_command("修正營養 熱量 228") == ("calories_kcal", 228.0)
    assert server.parse_nutrition_correction_command("修正營養 蛋白質 21.2") == ("protein_g", 21.2)
    assert server.parse_nutrition_correction_command("修正營養 咖啡因 100") is None
    assert server.parse_nutrition_correction_command("商品名稱 高蛋白豆漿") is None


def test_parse_multiple_nutrition_corrections_accepts_common_chinese_punctuation():
    corrections, errors = server.parse_nutrition_corrections(
        "熱量 204、蛋白質 16.4；鈉：48mg"
    )
    assert errors == []
    assert corrections == [
        ("calories_kcal", 204.0),
        ("protein_g", 16.4),
        ("sodium_mg", 48.0),
    ]


def test_parse_multiple_nutrition_corrections_reports_invalid_fragment_atomically():
    corrections, errors = server.parse_nutrition_corrections(
        "熱量 204、蛋白質 很多、鈉 48"
    )
    assert corrections == [("calories_kcal", 204.0), ("sodium_mg", 48.0)]
    assert errors == ["蛋白質 很多"]


def test_parse_multiple_nutrition_corrections_rejects_conflicting_units():
    corrections, errors = server.parse_nutrition_corrections(
        "熱量 204g、鈉 48g、蛋白質 16.4mg"
    )
    assert corrections == []
    assert errors == ["熱量單位應為 kcal", "鈉單位應為 mg", "蛋白質單位應為 g"]


def _text_event(message_id, text, user_id="U_EDIT"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
    )


def test_distance_command_over_three_kilometers_explains_delivery_rejection(monkeypatch):
    replies = []
    server.processed_messages.clear()
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda _address: {
            "success": True,
            "delivery_available": False,
            "distance_text": "3.1 公里",
            "duration_text": "12 分鐘",
            "delivery_fee": 0,
            "delivery_fee_text": "超過 3 公里，暫不提供外送",
            "route_group": "EAST",
            "delivery_zone": "UNAVAILABLE",
            "carpool_hint": "",
        },
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message",
        lambda _token, message: replies.append(message.text),
    )

    server._handle_message_impl(
        _text_event("DELIVERY-DISTANCE-BLOCK", "測距 台北市測試路1號", "U1")
    )

    assert len(replies) == 1
    assert "此地址暫不提供外送" in replies[0]
    assert "目前外送範圍為門市 3 公里內" in replies[0]
    assert "想了解包月方案" not in replies[0]


def test_legacy_subscription_order_over_three_kilometers_is_not_created(monkeypatch):
    replies = []
    created = []
    server.processed_messages.clear()
    monkeypatch.setattr(server, "get_customer_profile_for_order", lambda _uid: None)
    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda _address: {
            "success": True,
            "delivery_available": False,
            "address": _address,
            "distance_text": "3.1 公里",
            "distance_meters": 3100,
            "duration_text": "12 分鐘",
            "delivery_fee": 0,
            "delivery_fee_text": "超過 3 公里，暫不提供外送",
            "hub_name": "",
            "route_group": "EAST",
            "delivery_zone": "UNAVAILABLE",
            "carpool_hint": "",
        },
    )
    monkeypatch.setattr(
        server, "create_subscription_order",
        lambda *_args, **_kwargs: created.append(True) or 999,
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message",
        lambda _token, message: replies.append(message.text),
    )

    server._handle_message_impl(
        _text_event(
            "DELIVERY-LEGACY-BLOCK", "訂購 24餐 台北市測試路1號", "U1"
        )
    )

    assert created == []
    assert len(replies) == 1
    assert "此地址暫不提供外送" in replies[0]
    assert "建立包月資料" not in replies[0]


def test_form_data_routes_delivery_quote_failure_to_manual_review(monkeypatch):
    created = []
    pushed = []

    async def request_json():
        return {
            "UID": "U11111111111111111111111111111111",
            "稱呼": "測試客戶",
            "本期取餐方式": "外送",
            "本期外送地址": "台北市測試路1號",
            "取餐日期": ["週一"],
        }

    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda _address: {
            "success": False,
            "delivery_available": None,
            "address": _address,
            "distance_text": "",
            "distance_meters": 0,
            "duration_text": "",
            "delivery_fee": 0,
            "delivery_fee_text": "地址查詢失敗",
            "delivery_zone": "未分類",
        },
    )
    monkeypatch.setattr(
        server,
        "create_pending_subscription_form_order",
        lambda _snapshot: created.append(True) or 988,
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda uid, message: pushed.append((uid, message.text)),
    )

    result = asyncio.run(
        server.receive_form_data(
            cast(Any, SimpleNamespace(json=request_json)),
            cast(Any, SimpleNamespace()),
        )
    )

    assert result == {
        "status": "pending_manual_delivery_review",
        "reason": "delivery_quote_failed",
    }
    assert created == []
    assert len(pushed) == 1
    assert pushed[0][0] == "U11111111111111111111111111111111"
    assert "運費待客服確認" in pushed[0][1]
    assert "不需要重新填寫表單" in pushed[0][1]
    assert "$0" not in pushed[0][1]


def test_form_data_rejects_missing_dates_before_delivery_side_effects(monkeypatch):
    quote_calls = []
    delivery_block_calls = []
    pushed = []

    async def request_json():
        return {
            "UID": "U22222222222222222222222222222222",
            "稱呼": "無日期",
            "本期取餐方式": "外送",
            "本期外送地址": "台北市測試路1號",
        }

    monkeypatch.setattr(
        server, "calculate_delivery_quote", lambda address: quote_calls.append(address)
    )
    monkeypatch.setattr(
        server, "update_subscription_delivery_block",
        lambda *args: delivery_block_calls.append(args),
    )
    monkeypatch.setattr(
        server.line_bot_api, "push_message", lambda *args: pushed.append(args)
    )

    with pytest.raises(server.HTTPException) as exc_info:
        asyncio.run(server.receive_form_data(
            cast(Any, SimpleNamespace(json=request_json)),
            cast(Any, SimpleNamespace()),
        ))
    assert exc_info.value.status_code == 422
    assert quote_calls == []
    assert delivery_block_calls == []
    assert pushed == []

    async def invalid_number_json():
        return {
            "UID": "U33333333333333333333333333333333",
            "稱呼": "非法數值",
            "本期取餐方式": "外送",
            "本期外送地址": "台北市測試路1號",
            "取餐": ["週一"],
            "體重": "not-a-number",
        }

    server.user_memory["U33333333333333333333333333333333"] = [{"role": "user", "content": "keep"}]
    with pytest.raises(server.HTTPException) as number_exc:
        asyncio.run(server.receive_form_data(
            cast(Any, SimpleNamespace(json=invalid_number_json)),
            cast(Any, SimpleNamespace()),
        ))
    assert number_exc.value.status_code == 422
    assert quote_calls == []
    assert delivery_block_calls == []
    assert pushed == []
    assert "U33333333333333333333333333333333" in server.user_memory

    for index, (label, value) in enumerate(
        (
            ("體重", -1), ("體重", 9999),
            ("身高", -1), ("身高", 9999),
            ("年齡", -1), ("年齡", 9999),
        )
    ):
        uid = f"U{index + 10:032x}"

        async def invalid_range_json(label=label, value=value, uid=uid):
            return {
                "UID": uid,
                "本期取餐方式": "外送",
                "本期外送地址": "台北市測試路1號",
                "取餐": ["週一"],
                label: value,
            }

        server.user_memory[uid] = [{"role": "user", "content": "keep"}]
        with pytest.raises(server.HTTPException) as range_exc:
            asyncio.run(server.receive_form_data(
                cast(Any, SimpleNamespace(json=invalid_range_json)),
                cast(Any, SimpleNamespace()),
            ))
        assert range_exc.value.status_code == 422
        assert uid in server.user_memory
    assert quote_calls == []
    assert delivery_block_calls == []
    assert pushed == []


def test_form_data_accepts_new_preference_columns_and_covers_light_bentos(monkeypatch):
    captured = []
    pushed = []

    def dish(name, price=180, ingredients=None):
        return {
            "name": name, "cal": 400, "pro": 30, "price": price,
            "ingredients": ingredients or name,
            "category": "main", "carb_type": "高碳",
        }

    async def request_json():
        return {
            "UID": "U44444444444444444444444444444444",
            "稱呼": "新版表單客戶",
            "本期取餐方式": "自取",
            "禁忌": "豆腐,羊肉,起司",
            "您不喜歡的蛋白質（可複選）": ["牛肉"],
            "您偏好的蛋白質種類（可複選）": ["雞肉", "豆腐"],
            "主食偏好": ["飯食派"],
            "您的主食選擇（可複選）": ["都不挑食"],
            "第一週想取餐的日期（可複選）": ["週一", "星期一"],
            "第二週想取餐的日期（可複選）": ["週二"],
            "第三週想取餐的日期（可複選）": ["週三"],
            "第四週想取餐的日期（可複選）": ["週四"],
        }

    monkeypatch.setattr(server, "MAIN_DISHES", [
        dish("豆腐食蔬", 155), dish("雞肉香料食蔬", 160, "雞肉,豆腐"),
        dish("香料便當", 100, "孜然羊肉"),
        dish("雞肉起司食蔬", 165, "雞肉,起司"),
        dish("雞肉便當", 180),
        dish("雞肉低碳", 190), dish("雞肉食蔬", 170),
    ])
    monkeypatch.setattr(server.random, "sample", lambda population, count: population[:count])
    monkeypatch.setattr(
        server,
        "create_pending_subscription_form_order",
        lambda snapshot: captured.append(snapshot) or 986,
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda uid, message: pushed.append((uid, message.text)),
    )
    monkeypatch.setattr(
        server, "get_line_display_name_safe", lambda _uid: "新版表單客戶"
    )

    result = asyncio.run(server.receive_form_data(
        cast(Any, SimpleNamespace(json=request_json)),
        cast(Any, SimpleNamespace()),
    ))

    assert result["status"] == "pending"
    assert len(captured) == 1
    snapshot = captured[0]
    assert snapshot["pref_staple"] == "都不挑食"
    assert snapshot["pref_protein"] == "雞肉,豆腐"
    rows = snapshot["schedule_sheet_rows"][1:]
    assert len(rows) == 4
    assert snapshot["total_price"] == 1440
    for week_number in range(1, 5):
        week_rows = [row for row in rows if row[1].startswith(f"第{week_number}週-")]
        assert len(week_rows) == 1
        assert sum("食蔬" in meal for row in week_rows for meal in (row[2], row[5])) == 1
        assert all("牛肉" not in meal for row in week_rows for meal in (row[2], row[5]))
        assert all(
            forbidden not in meal
            for row in week_rows
            for meal in (row[2], row[5])
            for forbidden in ("豆腐", "雞肉香料食蔬", "香料便當", "雞肉起司食蔬")
        )
    assert pushed and pushed[0][0] == "U44444444444444444444444444444444"

    async def legacy_request_json():
        return {
            "UID": "U55555555555555555555555555555555",
            "稱呼": "舊版表單客戶",
            "本期取餐方式": "自取",
            "取餐": ["週一", "週一", "週二", "週二"],
        }

    legacy_result = asyncio.run(server.receive_form_data(
        cast(Any, SimpleNamespace(json=legacy_request_json)),
        cast(Any, SimpleNamespace()),
    ))
    assert legacy_result["status"] == "pending"
    legacy_rows = captured[1]["schedule_sheet_rows"][1:]
    assert len(legacy_rows) == 4
    assert {row[1] for row in legacy_rows} == {
        "第1週-週一", "第2週-週一", "第1週-週二", "第2週-週二",
    }

    async def fully_restricted_request_json():
        return {
            "UID": "U66666666666666666666666666666666",
            "稱呼": "無安全餐客戶",
            "本期取餐方式": "自取",
            "禁忌": "豆腐和羊肉",
            "取餐日期": ["週一"],
        }

    delivery_block_calls = []
    monkeypatch.setattr(
        server, "update_subscription_delivery_block",
        lambda *args: delivery_block_calls.append(args),
    )
    monkeypatch.setattr(server, "MAIN_DISHES", [
        dish("豆腐食蔬", 155), dish("香料羊肉便當", 180),
    ])
    with pytest.raises(server.HTTPException) as exc_info:
        asyncio.run(server.receive_form_data(
            cast(Any, SimpleNamespace(json=fully_restricted_request_json)),
            cast(Any, SimpleNamespace()),
        ))
    assert exc_info.value.status_code == 422
    assert len(captured) == 2
    assert delivery_block_calls == []

    async def no_preferred_light_json():
        return {
            "UID": "U77777777777777777777777777777777",
            "稱呼": "無偏好輕便當",
            "本期取餐方式": "自取",
            "您的主食選擇（可複選）": ["都不挑食"],
            "您最喜歡的蛋白質是？（可複選）": ["雞肉"],
            "取餐": ["週一"],
        }

    monkeypatch.setattr(server, "MAIN_DISHES", [
        dish("雞肉便當", 180), dish("雞肉低碳", 190), dish("鱸魚食蔬", 170),
    ])
    with pytest.raises(server.HTTPException) as light_exc:
        asyncio.run(server.receive_form_data(
            cast(Any, SimpleNamespace(json=no_preferred_light_json)),
            cast(Any, SimpleNamespace()),
        ))
    assert light_exc.value.status_code == 422
    assert len(captured) == 2
    assert delivery_block_calls == []

    async def ambiguous_restrictions_json():
        return {
            "UID": "U99999999999999999999999999999999",
            "本期取餐方式": "自取",
            "取餐": ["週一"],
            "飲食禁忌補充": "牛肉",
            "過敏禁忌項目": "豬肉",
        }

    with pytest.raises(server.HTTPException) as restriction_exc:
        asyncio.run(server.receive_form_data(
            cast(Any, SimpleNamespace(json=ambiguous_restrictions_json)),
            cast(Any, SimpleNamespace()),
        ))
    assert restriction_exc.value.status_code == 422
    assert len(captured) == 2
    assert delivery_block_calls == []

    async def fake_uid_note_json():
        return {
            "UID補充備考": "U_VICTIM",
            "本期取餐方式": "自取",
            "取餐": ["週一"],
        }

    assert asyncio.run(server.receive_form_data(
        cast(Any, SimpleNamespace(json=fake_uid_note_json)),
        cast(Any, SimpleNamespace()),
    )) == {"status": "ignored"}
    assert len(captured) == 2
    assert delivery_block_calls == []

    async def one_dish_json():
        return {
            "UID": "Uaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "本期取餐方式": "自取",
            "您的主食選擇（可複選）": ["飯食派"],
            "取餐": ["週一"],
        }

    monkeypatch.setattr(server, "MAIN_DISHES", [dish("雞肉便當", 180)])
    with pytest.raises(server.HTTPException) as one_dish_exc:
        asyncio.run(server.receive_form_data(
            cast(Any, SimpleNamespace(json=one_dish_json)),
            cast(Any, SimpleNamespace()),
        ))
    assert one_dish_exc.value.status_code == 422
    assert len(captured) == 2
    assert delivery_block_calls == []


def test_form_data_rejects_over_three_kilometer_delivery_before_pending_order(monkeypatch):
    created = []
    pushed = []

    async def request_json():
        return {
            "UID": "U88888888888888888888888888888888",
            "稱呼": "測試客戶",
            "本期取餐方式": "外送",
            "本期外送地址": "台北市測試路1號",
            "取餐日期": ["週一"],
        }

    monkeypatch.setattr(
        server,
        "calculate_delivery_quote",
        lambda _address: {
            "success": True,
            "delivery_available": False,
            "address": _address,
            "distance_text": "3.1 公里",
            "distance_meters": 3100,
            "duration_text": "12 分鐘",
            "delivery_fee": 0,
            "delivery_fee_text": "超過 3 公里，暫不提供外送",
            "delivery_zone": "UNAVAILABLE",
        },
    )
    monkeypatch.setattr(
        server,
        "create_pending_subscription_form_order",
        lambda _snapshot: created.append(True) or 987,
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda uid, message: pushed.append((uid, message.text)),
    )

    result = asyncio.run(
        server.receive_form_data(
            cast(Any, SimpleNamespace(json=request_json)),
            cast(Any, SimpleNamespace()),
        )
    )

    assert result == {
        "status": "rejected",
        "reason": "delivery_out_of_range",
        "distance": "3.1 公里",
    }
    assert created == []
    assert pushed == [
        (
            "U88888888888888888888888888888888",
            "🚫 此地址暫不提供外送\n\n"
            "📍 台北市測試路1號\n"
            "📏 距本店距離：3.1 公里\n\n"
            "目前外送範圍為門市 3 公里內。\n"
            "這次沒有建立包月訂單，請改選自取或找客服協助。",
        )
    ]


def test_rejected_estimate_state_cannot_request_subscription_form_link(monkeypatch):
    uid = "U_FORM_LINK_BLOCK"
    replies = []
    server.processed_messages.clear()
    server.pending_subscription_state[uid] = {
        "step": "estimated",
        "estimate": {
            "pickup_method": "外送",
            "delivery_available": False,
            "address": "台北市測試路1號",
            "quote": {"distance_text": "3.1 公里"},
        },
    }
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message.text),
    )

    try:
        server._handle_message_impl(
            _text_event("DELIVERY-FORM-LINK-BLOCK", "我要填寫包月資料", uid)
        )
    finally:
        server.pending_subscription_state.pop(uid, None)

    assert len(replies) == 1
    assert "此地址暫不提供外送" in replies[0]
    assert "這次不會提供包月表單連結" in replies[0]
    assert server.get_subscription_form_link(uid) not in replies[0]


def test_rejected_estimate_state_cannot_use_alternate_form_command(monkeypatch):
    uid = "U_ALT_FORM_LINK_BLOCK"
    replies = []
    server.processed_messages.clear()
    server.pending_subscription_state[uid] = {
        "step": "estimated",
        "estimate": {
            "pickup_method": "外送",
            "delivery_available": False,
            "address": "台北市測試路1號",
            "quote": {"distance_text": "3.1 公里"},
        },
    }
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message.text),
    )

    try:
        server._handle_message_impl(
            _text_event("DELIVERY-ALT-FORM-BLOCK", "填寫體質表單", uid)
        )
    finally:
        server.pending_subscription_state.pop(uid, None)

    assert len(replies) == 1
    assert "此地址暫不提供外送" in replies[0]
    assert server.get_subscription_form_link(uid) not in replies[0]


def test_package_intro_reset_cannot_clear_delivery_form_block(monkeypatch):
    uid = "U_RESET_FORM_LINK_BLOCK"
    replies = []
    server.processed_messages.clear()
    server.pending_subscription_state[uid] = {
        "step": "estimated",
        "estimate": {
            "pickup_method": "外送",
            "delivery_available": False,
            "address": "台北市測試路1號",
            "quote": {"distance_text": "3.1 公里"},
        },
    }
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message),
    )

    try:
        server._handle_message_impl(
            _text_event("DELIVERY-PACKAGE-RESET", "包月方案", uid)
        )
        server._handle_message_impl(
            _text_event("DELIVERY-PACKAGE-AFTER-RESET", "我要填寫包月資料", uid)
        )
    finally:
        server.pending_subscription_state.pop(uid, None)
        blocked_users = getattr(server, "subscription_delivery_blocked_users", set())
        blocked_users.discard(uid)

    assert len(replies) == 2
    second_text = replies[1].text
    assert "此地址暫不提供外送" in second_text
    assert server.get_subscription_form_link(uid) not in second_text


@pytest.mark.parametrize(
    "message",
    ["飲食紀錄", "我要紀錄飲食", "運費怎麼算", "⚠️ 今日調整"],
)
def test_non_vip_pre_gate_text_commands_are_silent(message, monkeypatch):
    replies = []
    server.processed_messages.clear()
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False, raising=False)
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, reply: replies.append(reply),
    )

    server.handle_message(
        _text_event(f"NON-VIP-GLOBAL-{message}", message, "U_NON_VIP")
    )

    assert replies == []


def test_non_vip_image_message_is_silent_before_processing(monkeypatch):
    calls = []
    message_id = "NON-VIP-IMAGE"
    server.processed_messages.discard(message_id)
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(
        server,
        "cleanup_nutrition_images",
        lambda: calls.append("cleanup"),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "get_message_content",
        lambda _message_id: calls.append("download"),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, _message: calls.append("reply"),
    )
    event = SimpleNamespace(
        message=SimpleNamespace(id=message_id),
        source=SimpleNamespace(user_id="U_NON_VIP"),
        reply_token="reply-non-vip-image",
    )

    server.handle_image_message(event)

    assert calls == []
    assert message_id not in server.processed_messages


@pytest.mark.parametrize(
    "event_key",
    [
        "MessageEvent_StickerMessage",
        "MessageEvent_AudioMessage",
        "MessageEvent_VideoMessage",
        "MessageEvent_FileMessage",
        "MessageEvent_LocationMessage",
    ],
)
def test_unhandled_message_types_have_no_reply_handler(event_key):
    assert event_key not in server.handler._handlers


def test_non_vip_postback_is_silent_before_processing(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(
        server,
        "_quick_log_catalog_card_once",
        lambda **_kwargs: calls.append("write") or server.TextSendMessage(text="ok"),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, _message: calls.append("reply"),
    )
    event = SimpleNamespace(
        postback=SimpleNamespace(
            data="nlfood:v1:food_private_soy:servings:1.5:meal:早餐"
        ),
        source=SimpleNamespace(user_id="U_NON_VIP"),
        reply_token="reply-non-vip-postback",
        webhook_event_id="NON-VIP-POSTBACK",
    )

    server.handle_postback_event(event)

    assert calls == []


def test_active_vip_postback_reaches_processing(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server, "handle_meal_photo_postback", lambda event: seen.append(event.postback.data)
    )
    event = SimpleNamespace(
        postback=SimpleNamespace(data="foodlog:v1:day:today:page:1"),
        source=SimpleNamespace(user_id="U_VIP"),
    )

    assert server.handler._handlers["PostbackEvent"] is server.handle_postback_event
    server.handle_postback_event(event)

    assert seen == ["foodlog:v1:day:today:page:1"]


@pytest.mark.parametrize(
    "data",
    [
        "mpr:v1:abcdef123456:1:approve",
        "mprn:v1:abcdef123456:approved",
    ],
)
def test_unauthorized_admin_postback_is_silent_even_for_active_vip(
    data, monkeypatch
):
    seen = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN"
    )
    monkeypatch.setattr(
        server, "handle_meal_photo_postback", lambda event: seen.append(event.postback.data)
    )
    event = SimpleNamespace(
        postback=SimpleNamespace(data=data),
        source=SimpleNamespace(user_id="U_VIP"),
    )

    server.handle_postback_event(event)

    assert seen == []


def test_authorized_admin_postback_passes_without_vip(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(
        server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN"
    )
    monkeypatch.setattr(
        server, "handle_meal_photo_postback", lambda event: seen.append(event.postback.data)
    )
    event = SimpleNamespace(
        postback=SimpleNamespace(data="mpr:v1:abcdef123456:1:approve"),
        source=SimpleNamespace(user_id="U_ADMIN"),
    )

    server.handle_postback_event(event)

    assert seen == ["mpr:v1:abcdef123456:1:approve"]


def test_non_vip_carbon_cycle_command_is_silent(monkeypatch):
    replies = []
    server.processed_messages.clear()
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message),
    )

    server.handle_message(
        _text_event("NON-VIP-CARB-CYCLE", "碳循環", "U_NON_VIP")
    )

    assert replies == []


def test_active_vip_access_requires_vip_status_valid_expiry_and_meals(tmp_path, monkeypatch):
    db = tmp_path / "vip-access.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE usage (
                user_id TEXT PRIMARY KEY,
                remaining_meals INTEGER,
                status TEXT,
                expiry_date TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO usage VALUES (?, ?, ?, ?)",
            ("U1", 12, "vip", (server.tw_today() + timedelta(days=1)).isoformat()),
        )

    assert server.has_active_vip_access("U1") is True
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE usage SET status='inactive' WHERE user_id='U1'")
    assert server.has_active_vip_access("U1") is False
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE usage SET status='vip', remaining_meals=0 WHERE user_id='U1'")
    assert server.has_active_vip_access("U1") is False
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE usage SET remaining_meals=12, expiry_date=? WHERE user_id='U1'",
            ((server.tw_today() - timedelta(days=1)).isoformat(),),
        )
    assert server.has_active_vip_access("U1") is False


def test_non_vip_text_gate_only_permits_valid_activation_or_authorized_commands(
    tmp_path, monkeypatch
):
    db = tmp_path / "vip-command-gate.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    monkeypatch.setattr(server, "COACH_UIDS", ["U_COACH"])
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER)"
        )
        conn.execute(
            "CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT, status TEXT, formalized_at TEXT, vip_code TEXT)"
        )
        conn.executemany(
            "INSERT INTO vips VALUES (?, 24, 31, 20, ?)",
            [
                ("#VIP24-ABC123", 0),
                ("#VIP48-Z9Y8X7", 1),
                ("#VIPORDER-1A2B3C", 0),
                ("#VIPORDER-OWN123", 0),
            ],
        )
        conn.execute(
            "INSERT INTO subscription_orders VALUES (1, 'U_OTHER', 'activated', '2026-09-01', '#VIPORDER-1A2B3C')"
        )
        conn.execute(
            "INSERT INTO subscription_orders VALUES (2, 'U_CUSTOMER', 'activated', '2026-09-01', '#VIPORDER-OWN123')"
        )

    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "#VIP24-ABC123") is True
    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "#VIP48-Z9Y8X7") is False
    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "#VIPORDER-1A2B3C") is False
    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "#VIPORDER-OWN123") is True
    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "#VIP24-NOT999") is False
    for malformed in ("#VIP", "#VIP亂打", "#VIP24-ABC12", "#VIP24-ABC123 extra"):
        assert server.is_text_command_allowed_without_vip("U_CUSTOMER", malformed) is False
    assert server.is_text_command_allowed_without_vip("U_ADMIN", "#綁定老闆") is True
    assert server.is_text_command_allowed_without_vip("U_COACH", "#教練") is True
    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "#教練") is False
    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "#綁定老闆") is False
    assert server.is_text_command_allowed_without_vip("U_COACH", "#生24") is False
    assert server.is_text_command_allowed_without_vip("U_ADMIN", "#教練") is False
    assert server.is_text_command_allowed_without_vip("U_ADMIN", "碳循環") is False
    assert server.is_text_command_allowed_without_vip("U_CUSTOMER", "包月方案") is False


def test_non_vip_allowlisted_dietitian_can_open_health_check_without_ai_or_quota(monkeypatch):
    dietitian_uid = "U1234567890abcdef1234567890abcdef"
    liff_id = "2011528194-EsxeCZ2a"
    replies = []
    ai_calls = []
    quota_calls = []
    monkeypatch.setattr(server, "ADMIN_UID", "U_OTHER_ADMIN")
    monkeypatch.setattr(
        server, "DIETITIAN_HEALTH_CHECK_CONFIG", SimpleNamespace(enabled=True)
    )
    monkeypatch.setattr(
        server,
        "DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS",
        frozenset({dietitian_uid}),
        raising=False,
    )
    monkeypatch.setattr(
        server, "DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID", liff_id, raising=False
    )
    monkeypatch.setattr(
        server,
        "has_active_vip_access",
        lambda _uid: (_ for _ in ()).throw(
            AssertionError("dietitian command must return before VIP/DB gate")
        ),
    )
    monkeypatch.setattr(
        server, "get_ai_response_with_memory", lambda *_args, **_kwargs: ai_calls.append(True)
    )
    monkeypatch.setattr(
        server, "check_permission_and_quota", lambda *_args: quota_calls.append(True)
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message),
    )
    event = _text_event(
        "DIETITIAN-COMMAND-ALLOWLISTED",
        "#營養師健檢",
        dietitian_uid,
    )
    server.processed_messages.discard(event.message.id)

    server.handle_message(event)

    assert len(replies) == 1
    rendered = json.loads(replies[0].as_json_string())
    assert rendered["altText"] == "開啟營養師三日健檢"
    assert f"https://liff.line.me/{liff_id}" in json.dumps(
        rendered, ensure_ascii=False
    )
    assert ai_calls == []
    assert quota_calls == []


def test_allowlisted_dietitian_command_is_silent_when_read_feature_is_disabled(monkeypatch):
    dietitian_uid = "U1234567890abcdef1234567890abcdef"
    dispatched = []
    replies = []
    monkeypatch.setattr(
        server, "DIETITIAN_HEALTH_CHECK_CONFIG", SimpleNamespace(enabled=False)
    )
    monkeypatch.setattr(
        server,
        "DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS",
        frozenset({dietitian_uid}),
    )
    monkeypatch.setattr(
        server, "DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID", "2011528194-EsxeCZ2a"
    )
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: dispatched.append(event.message.text)
    )
    monkeypatch.setattr(
        server,
        "has_active_vip_access",
        lambda _uid: (_ for _ in ()).throw(
            AssertionError("disabled reserved command must return before VIP/DB gate")
        ),
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda *_args: replies.append(True)
    )
    event = _text_event(
        "DIETITIAN-COMMAND-DISABLED", "#營養師健檢", dietitian_uid
    )
    server.processed_messages.discard(event.message.id)

    server.handle_message(event)

    assert dispatched == []
    assert replies == []


def test_unauthorized_dietitian_commands_are_silent_before_vip_db_gate(monkeypatch):
    dispatched = []
    replies = []
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    monkeypatch.setattr(
        server,
        "DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS",
        frozenset({"U1234567890abcdef1234567890abcdef"}),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID",
        "2011528194-EsxeCZ2a",
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "has_active_vip_access",
        lambda _uid: (_ for _ in ()).throw(
            AssertionError("reserved command must return before VIP/DB gate")
        ),
    )
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: dispatched.append(event.message.text)
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda *_args: replies.append(True),
    )

    for index, message in enumerate(
        (
            "#營養師健檢",
            " #營養師健檢",
            "#營養師健檢 ",
            "\t#營養師健檢\n",
            "＃營養師健檢",
            "# 營養師健檢",
            "#營養師 健檢",
            "#營養師\u200b健檢",
            "#營養師\u034f健檢",
            "#營養師\ufe0f健檢",
            "#營養師\U000e0100健檢",
            "#營養師健檢現在",
            "#養師健檢",
            "#榮養師健檢",
            "#營養師健撿現在請處理",
            "#養師健檢現在請處理",
            "#生活#營養師健檢",
            "正常文字#營養師健檢",
            "#今天跑步#營養師健檢",
            "#營ﬃ養師健檢",
            "#營養#師健檢",
            "#營養﹟師健檢",
            "#營養＃師健檢",
        )
    ):
        event = _text_event(f"DIETITIAN-DENY-{index}", message, "U_ADMIN")
        server.processed_messages.discard(event.message.id)
        server.handle_message(event)

    assert dispatched == []
    assert replies == []


def test_active_vip_normal_dietitian_health_hashtags_reach_regular_handler(monkeypatch):
    dispatched = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "is_valid_vip_activation_command", lambda *_args: False)
    monkeypatch.setattr(
        server, "is_authorized_privileged_text_command", lambda *_args: False
    )
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: dispatched.append(event.message.text)
    )

    expected = [
        "#營養師健康資訊",
        "#營養師健康飲食",
        "#今天跑步心得" + ("很棒" * 100),
        "#營養師建議",
        "#營養師健身",
        "#營養師料理",
        "#營養師營養建議",
        "#今天跑步" + (" " * 128),
        "#今天跑步" + (" " * 129),
        "#今天跑步" + ("\t" * 128),
        "#今天跑步" + ("\n" * 128),
        "#今天跑步" + ("\r" * 128),
    ]
    for index, message in enumerate(expected):
        event = _text_event(f"DIETITIAN-NORMAL-{index}", message, "U_CUSTOMER")
        server.processed_messages.discard(event.message.id)
        server.handle_message(event)

    assert dispatched == expected


def test_shared_privileged_classifier_delegates_dietitian_namespace_without_hiding_other_admin_commands():
    for message in (
        "#營養師建議",
        "#營養師健身",
        "#營養師料理",
        "#營養師營養建議",
    ):
        assert server.is_privileged_command_intent(message) is False
    assert server.is_privileged_command_intent("#營養師健身#點數庫存") is True
    assert server.is_privileged_command_intent("#營養師健康飲食#點數庫存") is True


def test_public_dietitian_hashtag_cannot_hide_later_admin_command_before_vip_gate(monkeypatch):
    monkeypatch.setattr(
        server,
        "has_active_vip_access",
        lambda _uid: (_ for _ in ()).throw(
            AssertionError("admin intent must return before VIP/DB gate")
        ),
    )
    monkeypatch.setattr(
        server,
        "_handle_message_impl",
        lambda _event: (_ for _ in ()).throw(
            AssertionError("admin intent must not reach regular handler")
        ),
    )
    event = _text_event(
        "DIETITIAN-PUBLIC-THEN-ADMIN",
        "#營養師健康飲食#點數庫存",
        "U_CUSTOMER",
    )
    server.processed_messages.discard(event.message.id)
    server.handle_message(event)


def test_dietitian_command_is_silent_when_liff_is_not_configured(monkeypatch):
    dispatched = []
    replies = []
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    monkeypatch.setattr(
        server,
        "DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS",
        frozenset({"U_ADMIN"}),
        raising=False,
    )
    monkeypatch.setattr(
        server, "DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID", "", raising=False
    )
    monkeypatch.setattr(
        server,
        "has_active_vip_access",
        lambda _uid: (_ for _ in ()).throw(
            AssertionError("disabled command must return before VIP/DB gate")
        ),
    )
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: dispatched.append(event.message.text)
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda *_args: replies.append(True),
    )

    event = _text_event("DIETITIAN-NO-CONFIG", "#營養師健檢", "U_ADMIN")
    server.processed_messages.discard(event.message.id)
    server.handle_message(event)

    assert dispatched == []
    assert replies == []


def test_vip_activation_precheck_missing_db_fails_closed_without_creating_file(
    tmp_path, monkeypatch
):
    missing_db = tmp_path / "must-not-be-created.db"
    monkeypatch.setattr(server, "DB_PATH", str(missing_db))

    assert server.is_valid_vip_activation_command("U_CUSTOMER", "#VIP24-ABC123") is False
    assert missing_db.exists() is False


def test_non_vip_vip_code_passes_global_text_gate(tmp_path, monkeypatch):
    seen = []
    db = tmp_path / "valid-vip-command.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER)"
        )
        conn.execute(
            "INSERT INTO vips VALUES ('#VIP24-ABC123', 24, 31, 20, 0)"
        )
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(server, "_handle_message_impl", lambda event: seen.append(event.message.text))

    server.handle_message(
        _text_event("NON-VIP-ACTIVATION", "#VIP24-ABC123", "U_NON_VIP")
    )

    assert seen == ["#VIP24-ABC123"]


def test_non_vip_valid_code_runs_real_outer_handler_and_redeems_once(
    tmp_path, monkeypatch
):
    uid = "U_REAL_REDEEM"
    code = "#VIP24-REAL01"
    db = tmp_path / "real-outer-redeem.db"
    replies = []
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER)"
        )
        conn.execute(
            "CREATE TABLE usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)"
        )
        conn.execute("INSERT INTO vips VALUES (?, 24, 31, 20, 0)", (code,))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(
        server, "get_subscription_form_link", lambda _uid: "https://example.test/form"
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message.text),
    )
    event = _text_event("REAL-OUTER-REDEEM", code, uid)
    server.processed_messages.discard(event.message.id)

    server.handle_message(event)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT is_used FROM vips WHERE code=?", (code,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,remaining_meals FROM usage WHERE user_id=?", (uid,)
        ).fetchone() == ("vip", 24)
    assert len(replies) == 1
    assert "兌換成功" in replies[0]


def test_active_vip_valid_code_runs_outer_handler_and_renews(
    tmp_path, monkeypatch
):
    uid = "U_REAL_RENEW"
    code = "#VIP24-ABC123"
    db = tmp_path / "real-outer-renew.db"
    replies = []
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER)"
        )
        conn.execute(
            "CREATE TABLE usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)"
        )
        conn.execute("INSERT INTO vips VALUES (?, 24, 31, 20, 0)", (code,))
        conn.execute(
            "INSERT INTO usage VALUES (?, 20, 5, '2026-09-10', 'vip', '2099-01-01', 20)",
            (uid,),
        )
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(
        server, "get_subscription_form_link", lambda _uid: "https://example.test/form"
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message.text),
    )
    event = _text_event("REAL-OUTER-RENEW", code, uid)
    server.processed_messages.discard(event.message.id)

    server.handle_message(event)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT is_used FROM vips WHERE code=?", (code,)).fetchone() == (1,)
        assert conn.execute(
            "SELECT status,remaining_meals FROM usage WHERE user_id=?", (uid,)
        ).fetchone() == ("vip", 29)
    assert len(replies) == 1
    assert "兌換成功" in replies[0]


@pytest.mark.parametrize(
    "message",
    [
        "#教練", "＃教練", "# 教練", "#教 練", "#教", "#管",
        "#生24", "＃生24", "#生34",
        "#綁定老闆 錯誤參數", "#喚醒ai U123", "@ 靜音 王小明",
        "#更\u200b新菜單", "#喚醒\u2060AI U123", "＠解\ufeff除靜音 U123",
        "#更\u034f新菜單", "#喚醒\x00AI U123",
        "#更\u115f新菜單", "#更\u1160新菜單", "#更\u3164新菜單",
        "#更\uffa0新菜單", "#更\u2800新菜單", "#更abc新菜單",
        "#更" + "\u115f" * 10 + "新菜單", "#更" + "\u2800" * 10 + "新菜單",
        "x#更新菜單", "。#更新菜單", "#更新菜", "#更新菜単",
        "x#更abc新菜単", "。＃更\u2800新菜単",
        "。健康回報｜體重70",
        "健康回報｜體重70", "建康回報", "健回報｜體重70", "健庩回報｜體重70",
        "今日健康日報", "今日健康報", "今日健庩日報",
        "重新整理今日報告", "重新整理今日報", "重新整理今曰報告",
        "#教煉", "#教鍊", "#校練", "#生x2肆",
        "#教學#更新菜單", "#教我#綁定老闆", "#請教#生24",
        "#生活#喚醒AI U123", "#重訓#刪除檔案",
        "#重量訓練@靜音 王", "#管理飲食#點數庫存",
    ],
)
def test_active_vip_unauthorized_reserved_commands_are_silent(message, monkeypatch):
    seen = []
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    monkeypatch.setattr(server, "COACH_UIDS", ["U_COACH"])
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: seen.append(event.message.text)
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, reply: replies.append(reply),
    )

    server.handle_message(
        _text_event(f"UNAUTHORIZED-RESERVED-{message}", message, "U_CUSTOMER")
    )

    assert seen == []
    assert replies == []


@pytest.mark.parametrize(
    "message",
    [
        "#查狀態", "#重新排餐", "#延餐 5/31 午餐 -> 6/2 午餐", "#測距 台北市中山區",
        "#生活習慣想改善", "#生\u200b活習慣想改善",
        "#生活習慣改善30天", "#生活 2026目標",
        "我想改善健康並回報今天飲食", "#重訓", "#教學",
        "#教\u200b學今天怎麼安排？", "#重\u2060訓今天做幾組？", "＃教\u034f學",
        "#請教今天重訓怎麼練比較有效？",
        "#重量訓練怎麼設置組數？",
        "健康餐吃完多久可以回家運動？",
        "健康狀況多久才會回穩？",
        "健康回家運動",
        "健康回覆一下",
        "健康回答問題",
        "。健康回家",
        "健康回報內容要怎麼看",
        "健康回報告訴我今天狀況很好",
        "今日健康日報告何時產生",
        "重新整理今日報告訴我結果",
        "#教我怎麼安排今天的飲食",
        "#健康生活更新：今天菜單有雞胸肉",
        "#管理飲食", "#管理今天飲食",
        "#想知道剩餘點數，資料庫還有存嗎？",
        "#等待教練審視我的餐點",
        "#請問配送時間，明日會有提醒嗎？",
    ],
)
def test_active_vip_public_commands_still_pass_global_text_gate(message, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: seen.append(event.message.text)
    )

    server.handle_message(_text_event(f"PUBLIC-{message}", message, "U_VIP"))

    assert seen == [message]


@pytest.mark.parametrize(
    ("user_id", "message"),
    [("U_ADMIN", "#生24"), ("U_COACH", "#教練")],
)
def test_authorized_privileged_text_commands_pass_without_active_vip(
    user_id, message, monkeypatch
):
    seen = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    monkeypatch.setattr(server, "COACH_UIDS", ["U_COACH"])
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: seen.append(event.message.text)
    )

    server.handle_message(
        _text_event(f"AUTHORIZED-NO-VIP-{message}", message, user_id)
    )

    assert seen == [message]


@pytest.mark.parametrize(
    ("user_id", "message"),
    [("U_ADMIN", "#生24"), ("U_COACH", "#教練")],
)
def test_authorized_privileged_commands_pass_for_active_vip(
    user_id, message, monkeypatch
):
    seen = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    monkeypatch.setattr(server, "COACH_UIDS", ["U_COACH"])
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda event: seen.append(event.message.text)
    )

    server.handle_message(
        _text_event(f"AUTHORIZED-ACTIVE-VIP-{message}", message, user_id)
    )

    assert seen == [message]


@pytest.mark.parametrize("database_state", ["missing", "corrupt"])
def test_authorization_reads_fail_closed_without_creating_or_leaking_db_errors(
    database_state, tmp_path, monkeypatch
):
    db_path = tmp_path / "authorization.db"
    if database_state == "corrupt":
        db_path.write_bytes(b"not-a-sqlite-database")
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(
        server,
        "handle_meal_photo_postback",
        lambda _event: pytest.fail("unauthorized postback reached inner handler"),
    )

    assert server.has_active_vip_access("U_CUSTOMER") is False
    with pytest.raises(PermissionError):
        server.get_bound_admin_uid_for_authorization()

    server.handle_message(
        _text_event(f"DB-{database_state}", "今天吃雞胸肉", "U_CUSTOMER")
    )
    server.handle_image_message(SimpleNamespace(source=SimpleNamespace(user_id="U_CUSTOMER")))
    server.handle_postback_event(
        SimpleNamespace(
            source=SimpleNamespace(user_id="U_CUSTOMER"),
            postback=SimpleNamespace(data="mpr:v1:malformed"),
        )
    )

    if database_state == "missing":
        assert not db_path.exists()


@pytest.mark.parametrize(
    ("remaining_meals", "expiry_date"),
    [("oops", "2099-12-31"), (12, "not-a-date"), (12, None), (12, "")],
)
def test_semantically_invalid_vip_rows_fail_closed_at_all_registered_gates(
    remaining_meals, expiry_date, tmp_path, monkeypatch
):
    db_path = tmp_path / "invalid-vip-row.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE usage (
                user_id TEXT PRIMARY KEY,
                remaining_meals,
                status TEXT,
                expiry_date TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO usage VALUES ('U_CUSTOMER', ?, 'vip', ?)",
            (remaining_meals, expiry_date),
        )
    seen = []
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(
        server, "_handle_message_impl", lambda _event: seen.append("text")
    )
    monkeypatch.setattr(
        server, "cleanup_nutrition_images", lambda: seen.append("image")
    )
    monkeypatch.setattr(
        server, "handle_meal_photo_postback", lambda _event: seen.append("postback")
    )

    assert server.has_active_vip_access("U_CUSTOMER") is False
    server.handle_message(
        _text_event(f"INVALID-ROW-{remaining_meals}", "今天吃雞胸肉", "U_CUSTOMER")
    )
    server.handle_image_message(
        SimpleNamespace(source=SimpleNamespace(user_id="U_CUSTOMER"))
    )
    server.handle_postback_event(
        SimpleNamespace(
            source=SimpleNamespace(user_id="U_CUSTOMER"),
            postback=SimpleNamespace(data="nlfood:v1:anything"),
        )
    )

    assert seen == []


def test_privileged_intent_scan_handles_line_sized_introducer_flood_quickly():
    from time import perf_counter

    message = "#" * 5000 + "生活"
    started = perf_counter()
    result = server.is_privileged_command_intent(message)
    elapsed = perf_counter() - started

    assert result is True
    assert elapsed < 0.5


def test_selecting_self_pickup_clears_delivery_form_block(monkeypatch):
    uid = "U_SELF_PICKUP_UNBLOCK"
    replies = []
    server.processed_messages.clear()
    server.subscription_delivery_blocked_users.add(uid)
    server.pending_subscription_state[uid] = {"step": "pickup", "days_per_week": 3}
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message),
    )

    try:
        server._handle_message_impl(
            _text_event("DELIVERY-SELF-PICKUP", "包月取餐 自取", uid)
        )
        unblocked_after_selection = uid not in server.subscription_delivery_blocked_users
        server._handle_message_impl(
            _text_event("DELIVERY-SELF-PICKUP-FORM", "我要填寫包月資料", uid)
        )
    finally:
        server.pending_subscription_state.pop(uid, None)
        server.subscription_delivery_blocked_users.discard(uid)

    assert unblocked_after_selection is True
    assert server.get_subscription_form_link(uid) in replies[1].text


def test_map_lookup_manual_review_clears_delivery_form_block(monkeypatch):
    uid = "U_MAP_REVIEW_UNBLOCK"
    replies = []
    server.processed_messages.clear()
    server.subscription_delivery_blocked_users.add(uid)
    server.pending_subscription_state[uid] = {
        "step": "delivery_address",
        "days_per_week": 3,
        "pickup_method": "外送",
    }
    monkeypatch.setattr(
        server,
        "calculate_subscription_estimate",
        lambda *_args, **_kwargs: {
            "delivery_available": None,
            "pickup_method": "外送",
            "address": "台北市待確認路1號",
        },
    )
    monkeypatch.setattr(
        server,
        "build_subscription_estimate_flex",
        lambda _uid, _est: server.TextSendMessage(text="地圖待客服確認"),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message),
    )

    try:
        server._handle_message_impl(
            _text_event("DELIVERY-MAP-REVIEW", "台北市待確認路1號", uid)
        )
        unblocked_after_lookup = uid not in server.subscription_delivery_blocked_users
        server._handle_message_impl(
            _text_event("DELIVERY-MAP-REVIEW-FORM", "我要填寫包月資料", uid)
        )
    finally:
        server.pending_subscription_state.pop(uid, None)
        server.subscription_delivery_blocked_users.discard(uid)

    assert unblocked_after_lookup is True
    assert server.get_subscription_form_link(uid) in replies[1].text


def _seed_vip_redemption_db(path, code, linked_uid=None):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)"
        )
        conn.execute(
            "CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, vip_code TEXT, user_id TEXT, formalized_at TEXT, status TEXT, activated_at TEXT)"
        )
        conn.execute("INSERT INTO vips VALUES (?, 24, 31, 20, 0)", (code,))
        if linked_uid:
            conn.execute(
                "INSERT INTO subscription_orders VALUES (1, ?, ?, '2026-08-27 10:00:00', 'activated', '2026-08-27 09:00:00')",
                (code, linked_uid),
            )


@pytest.mark.parametrize("damage", ["duplicate_order", "malformed_formalized_at"])
def test_subscription_vip_order_semantic_damage_never_prechecks_or_redeems(
    damage, tmp_path, monkeypatch
):
    uid = "U_ORDER_OWNER"
    code = "#VIPORDER-BAD001"
    db = tmp_path / f"{damage}.db"
    _seed_vip_redemption_db(db, code, linked_uid=uid)
    with sqlite3.connect(db) as conn:
        if damage == "duplicate_order":
            conn.execute(
                """INSERT INTO subscription_orders
                   VALUES (2, ?, 'U_OTHER', '2026-08-27 10:00:00',
                           'activated', '2026-08-27 09:00:00')""",
                (code,),
            )
        else:
            conn.execute(
                "UPDATE subscription_orders SET formalized_at='not-a-date' WHERE vip_code=?",
                (code,),
            )
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)

    assert server.is_valid_vip_activation_command(uid, code) is False
    expiry, _message = server.redeem_code(uid, code)

    assert expiry is None
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT is_used FROM vips WHERE code=?", (code,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE user_id=?", (uid,)
        ).fetchone()[0] == 0


def test_null_is_used_vip_order_fails_outer_gate_without_side_effects(
    tmp_path, monkeypatch
):
    uid = "U_ORDER_OWNER"
    code = "#VIPORDER-NULL00"
    db = tmp_path / "null-is-used.db"
    _seed_vip_redemption_db(db, code, linked_uid=uid)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE vips SET is_used=NULL WHERE code=?", (code,))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", False)
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    replies = []
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, reply: replies.append(reply),
    )
    event = _text_event("NULL-IS-USED", code, uid)
    server.processed_messages.discard(event.message.id)

    assert server.is_valid_vip_activation_command(uid, code) is False
    server.handle_message(event)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT is_used FROM vips WHERE code=?", (code,)).fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
    assert replies == []
    assert event.message.id not in server.processed_messages


def test_vip_redemption_does_not_expose_form_link_for_blocked_delivery(tmp_path, monkeypatch):
    uid = "U_BLOCKED_VIP"
    code = "#VIP24-BLOCK1"
    db = tmp_path / "blocked-vip.db"
    _seed_vip_redemption_db(db, code)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.subscription_delivery_blocked_users.add(uid)

    try:
        expiry, message = server.redeem_code(uid, code)
        with sqlite3.connect(db) as conn:
            usage = conn.execute(
                "SELECT remaining_meals, status FROM usage WHERE user_id=?", (uid,)
            ).fetchone()
    finally:
        server.subscription_delivery_blocked_users.discard(uid)

    assert expiry
    assert usage == (24, "vip")
    assert "兌換成功" in message
    assert "此地址暫不提供外送" in message
    assert server.get_subscription_form_link(uid) not in message


def test_vip_redemption_still_exposes_form_link_when_not_delivery_blocked(tmp_path, monkeypatch):
    uid = "U_ALLOWED_VIP"
    code = "#VIP24-ALLOW1"
    db = tmp_path / "allowed-vip.db"
    _seed_vip_redemption_db(db, code)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.subscription_delivery_blocked_users.discard(uid)

    expiry, message = server.redeem_code(uid, code)

    assert expiry
    assert server.get_subscription_form_link(uid) in message


def test_linked_formalized_order_redemption_creates_current_menu_entitlement(tmp_path, monkeypatch):
    uid = "U_LINKED_CURRENT_PLAN"
    code = "#VIPORDER-LINKED"
    db = tmp_path / "linked-current-plan.db"
    _seed_vip_redemption_db(db, code, linked_uid=uid)
    monkeypatch.setattr(server, "DB_PATH", str(db))

    expiry, message = server.redeem_code(uid, code)

    with sqlite3.connect(db) as conn:
        entitlement = conn.execute(
            "SELECT order_id, status, expires_on FROM subscription_menu_entitlements WHERE user_id=?",
            (uid,),
        ).fetchone()
    assert expiry
    assert "不需要再填一次表單" in message
    assert entitlement == (1, "active", expiry)


def test_linked_order_code_cannot_be_consumed_by_another_user(tmp_path, monkeypatch):
    owner_uid = "U_ORDER_OWNER"
    wrong_uid = "U_WRONG_REDEEMER"
    code = "#VIPORDER-OWNER-ONLY"
    db = tmp_path / "owner-only-order-code.db"
    _seed_vip_redemption_db(db, code, linked_uid=owner_uid)
    monkeypatch.setattr(server, "DB_PATH", str(db))

    expiry, message = server.redeem_code(wrong_uid, code)

    with sqlite3.connect(db) as conn:
        is_used = conn.execute(
            "SELECT is_used FROM vips WHERE code=?", (code,)
        ).fetchone()[0]
        wrong_usage = conn.execute(
            "SELECT 1 FROM usage WHERE user_id=?", (wrong_uid,)
        ).fetchone()
    assert expiry is None
    assert "此開通碼不屬於目前帳號" in message
    assert is_used == 0
    assert wrong_usage is None


def test_linked_order_code_waits_until_order_is_activated_and_formalized(tmp_path, monkeypatch):
    uid = "U_ORDER_NOT_READY"
    code = "#VIPORDER-NOT-READY"
    db = tmp_path / "not-ready-order-code.db"
    _seed_vip_redemption_db(db, code, linked_uid=uid)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE subscription_orders SET status='pending_payment', formalized_at='' WHERE vip_code=?",
            (code,),
        )
    monkeypatch.setattr(server, "DB_PATH", str(db))

    expiry, message = server.redeem_code(uid, code)

    with sqlite3.connect(db) as conn:
        is_used = conn.execute(
            "SELECT is_used FROM vips WHERE code=?", (code,)
        ).fetchone()[0]
        usage = conn.execute(
            "SELECT 1 FROM usage WHERE user_id=?", (uid,)
        ).fetchone()
    assert expiry is None
    assert "訂單尚未完成付款確認與正式配餐" in message
    assert is_used == 0
    assert usage is None


def test_ordinary_vip_code_never_creates_menu_entitlement_even_if_linked(tmp_path, monkeypatch):
    uid = "U_ORDINARY_LINKED"
    code = "#VIP24-ORDINARY-LINKED"
    db = tmp_path / "ordinary-linked-code.db"
    _seed_vip_redemption_db(db, code, linked_uid=uid)
    with sqlite3.connect(db) as conn:
        server.ensure_subscription_menu_entitlement_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))

    expiry, message = server.redeem_code(uid, code)

    with sqlite3.connect(db) as conn:
        entitlements = conn.execute(
            "SELECT user_id FROM subscription_menu_entitlements"
        ).fetchall()
    assert expiry
    assert "兌換成功" in message
    assert entitlements == []


def test_unlinked_viporder_code_is_not_consumed(tmp_path, monkeypatch):
    uid = "U_UNLINKED_VIPORDER"
    code = "#VIPORDER-NO-ORDER"
    db = tmp_path / "unlinked-viporder.db"
    _seed_vip_redemption_db(db, code)
    monkeypatch.setattr(server, "DB_PATH", str(db))

    expiry, message = server.redeem_code(uid, code)

    with sqlite3.connect(db) as conn:
        is_used = conn.execute(
            "SELECT is_used FROM vips WHERE code=?", (code,)
        ).fetchone()[0]
        usage = conn.execute(
            "SELECT 1 FROM usage WHERE user_id=?", (uid,)
        ).fetchone()
    assert expiry is None
    assert "找不到對應的包月訂單" in message
    assert is_used == 0
    assert usage is None


def _seed_subscription_menu_db(
    path, uid, summary_text, remaining_meals=None, expiry_date=None,
    formalized_order=False, current_menu_entitlement=False,
):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE health_profile (user_id TEXT PRIMARY KEY, summary_text TEXT)"
        )
        conn.execute(
            "CREATE TABLE usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)"
        )
        conn.execute(
            "CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT, status TEXT, formalized_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE subscription_menu_entitlements (user_id TEXT PRIMARY KEY, order_id INTEGER, status TEXT, starts_on TEXT, expires_on TEXT)"
        )
        conn.execute(
            "INSERT INTO health_profile (user_id, summary_text) VALUES (?, ?)",
            (uid, summary_text),
        )
        if remaining_meals is not None:
            conn.execute(
                "INSERT INTO usage VALUES (?, 20, ?, ?, 'vip', ?, 20)",
                (uid, remaining_meals, server.tw_today().isoformat(), expiry_date),
            )
        if formalized_order:
            conn.execute(
                "INSERT INTO subscription_orders VALUES (1, ?, 'activated', '2026-08-01 12:00:00')",
                (uid,),
            )
        if current_menu_entitlement:
            conn.execute(
                "INSERT INTO subscription_menu_entitlements VALUES (?, 1, 'active', ?, ?)",
                (uid, server.tw_today().isoformat(), expiry_date),
            )


def _run_subscription_menu_command(uid, message_id, monkeypatch, command="查看包月菜單"):
    replies = []
    server.processed_messages.clear()
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message.text),
    )
    server._handle_message_impl(
        _text_event(message_id, command, uid)
    )
    return replies[0]


def test_expired_subscription_cannot_view_stale_menu(tmp_path, monkeypatch):
    uid = "U_EXPIRED_MENU"
    db = tmp_path / "expired-menu.db"
    expired = (server.tw_today() - server.timedelta(days=1)).isoformat()
    _seed_subscription_menu_db(db, uid, "不應顯示的舊菜單", 12, expired)
    monkeypatch.setattr(server, "DB_PATH", str(db))

    reply = _run_subscription_menu_command(uid, "EXPIRED-MENU", monkeypatch)

    assert "包月方案已到期" in reply
    assert "不應顯示的舊菜單" not in reply


def test_subscription_with_no_remaining_meals_cannot_view_stale_menu(tmp_path, monkeypatch):
    uid = "U_EMPTY_MENU"
    db = tmp_path / "empty-menu.db"
    valid_until = (server.tw_today() + server.timedelta(days=7)).isoformat()
    _seed_subscription_menu_db(db, uid, "不應顯示的舊菜單", 0, valid_until)
    monkeypatch.setattr(server, "DB_PATH", str(db))

    reply = _run_subscription_menu_command(uid, "EMPTY-MENU", monkeypatch)

    assert "包月餐數已使用完畢" in reply
    assert "不應顯示的舊菜單" not in reply


def test_user_without_active_subscription_cannot_view_stale_menu(tmp_path, monkeypatch):
    uid = "U_NO_PLAN_MENU"
    db = tmp_path / "no-plan-menu.db"
    _seed_subscription_menu_db(db, uid, "不應顯示的舊菜單")
    monkeypatch.setattr(server, "DB_PATH", str(db))

    reply = _run_subscription_menu_command(uid, "NO-PLAN-MENU", monkeypatch)

    assert "目前沒有有效的包月方案" in reply
    assert "不應顯示的舊菜單" not in reply


def test_active_subscription_with_remaining_meals_can_view_menu(tmp_path, monkeypatch):
    uid = "U_ACTIVE_MENU"
    db = tmp_path / "active-menu.db"
    valid_until = (server.tw_today() + server.timedelta(days=7)).isoformat()
    _seed_subscription_menu_db(
        db, uid, "本期有效菜單內容", 12, valid_until,
        formalized_order=True, current_menu_entitlement=True,
    )
    monkeypatch.setattr(server, "DB_PATH", str(db))

    reply = _run_subscription_menu_command(uid, "ACTIVE-MENU", monkeypatch)

    assert "本期有效菜單內容" in reply
    assert "還剩 12 餐" in reply


def test_active_menu_displays_plan_expiry_instead_of_generic_vip_expiry(tmp_path, monkeypatch):
    uid = "U_PLAN_EXPIRY"
    db = tmp_path / "plan-expiry.db"
    vip_expiry = (server.tw_today() + server.timedelta(days=30)).isoformat()
    plan_expiry = (server.tw_today() + server.timedelta(days=7)).isoformat()
    _seed_subscription_menu_db(
        db, uid, "本期菜單", 12, vip_expiry,
        formalized_order=True, current_menu_entitlement=True,
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE subscription_menu_entitlements SET expires_on=? WHERE user_id=?",
            (plan_expiry, uid),
        )
    monkeypatch.setattr(server, "DB_PATH", str(db))

    reply = _run_subscription_menu_command(uid, "PLAN-EXPIRY", monkeypatch)

    assert f"到期日 {plan_expiry}" in reply
    assert f"到期日 {vip_expiry}" not in reply


@pytest.mark.parametrize("command", ["查看菜單", "包月菜單", "查看包月菜單"])
def test_active_vip_without_formalized_order_cannot_view_stale_menu(tmp_path, monkeypatch, command):
    uid = "U_VIP_WITHOUT_PLAN"
    db = tmp_path / "vip-without-plan.db"
    valid_until = (server.tw_today() + server.timedelta(days=8)).isoformat()
    _seed_subscription_menu_db(db, uid, "不應顯示的上一期菜單", 662, valid_until)
    monkeypatch.setattr(server, "DB_PATH", str(db))

    reply = _run_subscription_menu_command(
        uid, f"VIP-WITHOUT-PLAN-{command}", monkeypatch, command=command
    )

    assert "目前沒有本期正式配餐" in reply
    assert "不應顯示的上一期菜單" not in reply


def test_old_formalized_order_without_current_entitlement_cannot_view_stale_menu(tmp_path, monkeypatch):
    uid = "U_OLD_FORMALIZED_PLAN"
    db = tmp_path / "old-formalized-plan.db"
    valid_until = (server.tw_today() + server.timedelta(days=8)).isoformat()
    _seed_subscription_menu_db(
        db, uid, "不應顯示的舊正式菜單", 48, valid_until,
        formalized_order=True,
    )
    monkeypatch.setattr(server, "DB_PATH", str(db))

    reply = _run_subscription_menu_command(uid, "OLD-FORMALIZED-PLAN", monkeypatch)

    assert "目前沒有本期正式配餐" in reply
    assert "不應顯示的舊正式菜單" not in reply


def test_entitlement_schema_does_not_infer_plan_from_vip_and_old_order(tmp_path):
    db = tmp_path / "menu-entitlement-no-inference.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE usage (user_id TEXT PRIMARY KEY, remaining_meals INTEGER, status TEXT, expiry_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id TEXT, status TEXT, formalized_at TEXT, activated_at TEXT, vip_code TEXT)"
        )
        conn.execute(
            "INSERT INTO usage VALUES ('U_GENERIC_VIP', 662, 'vip', '2026-09-26')"
        )
        conn.execute(
            "INSERT INTO subscription_orders VALUES (1, 'U_GENERIC_VIP', 'activated', 'done', '2026-08-27 10:00:00', '#VIPORDER-UNREDEEMED')"
        )

        server.ensure_subscription_menu_entitlement_schema(conn)

        rows = conn.execute(
            "SELECT user_id, order_id FROM subscription_menu_entitlements"
        ).fetchall()
    assert rows == []


def test_text_handler_completes_missing_product_name(tmp_path, monkeypatch):
    db = tmp_path / "edit-name.db"
    partial = {**valid_label(), "product_name": "", "brand": ""}
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn, user_id="U_EDIT", payload=partial, allow_missing_identity=True
        )
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(_text_event("edit-name-button", f"修改營養品名:{token}"))
    server._handle_message_impl(_text_event("edit-name-value", "商品名稱 無加糖高蛋白豆漿"))
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status,label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
    assert row[0] == "pending"
    assert "無加糖高蛋白豆漿" in row[1]
    assert state_count == 0
    assert len(replies) == 2


def test_text_handler_corrects_nutrient_and_recalculates_per100(tmp_path, monkeypatch):
    db = tmp_path / "edit-nutrient.db"
    payload = {
        **valid_label(), "package_amount": 400,
        "per_serving": {**valid_label()["per_serving"], "sodium_mg": 488},
        "per_100": {**valid_label()["per_100"], "sodium_mg": 122},
    }
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_EDIT", payload=payload)
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(_text_event("edit-nutrient-button", f"修改營養數字:{token}"))
    edit_event = _text_event("edit-nutrient-value", "鈉 48")
    server._handle_message_impl(edit_event)
    server.processed_messages.discard("edit-nutrient-value")
    server._handle_message_impl(edit_event)
    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0]
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE message_id='edit-nutrient-value'"
        ).fetchone()[0]
    label = __import__("json").loads(stored)
    assert label["per_serving"]["sodium_mg"] == 48
    assert label["per_100"]["sodium_mg"] == 12
    assert state_count == 0
    assert event_count == 1
    assert len(replies) == 3


def test_text_handler_applies_multiple_nutrients_in_one_atomic_message(tmp_path, monkeypatch):
    db = tmp_path / "edit-multiple-nutrients.db"
    payload = {
        **valid_label(), "package_amount": 400,
        "per_serving": {
            **valid_label()["per_serving"],
            "calories_kcal": 228,
            "protein_g": 21.2,
            "sodium_mg": 488,
        },
    }
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_EDIT", payload=payload)
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(_text_event("multi-edit-button", f"修改營養數字:{token}"))
    server._handle_message_impl(
        _text_event("multi-edit-value", "熱量 204、蛋白質 16.4；鈉：48mg")
    )
    with sqlite3.connect(db) as conn:
        stored = json.loads(conn.execute(
            "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0])
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
    assert stored["per_serving"]["calories_kcal"] == 204
    assert stored["per_serving"]["protein_g"] == 16.4
    assert stored["per_serving"]["sodium_mg"] == 48
    assert state_count == 0
    assert isinstance(replies[-1], list)
    assert "已更新 3 個項目" in replies[-1][0].text


def test_text_handler_does_not_partially_apply_malformed_multiple_edit(tmp_path, monkeypatch):
    db = tmp_path / "edit-multiple-invalid.db"
    payload = {
        **valid_label(),
        "per_serving": {**valid_label()["per_serving"], "calories_kcal": 228, "protein_g": 21.2},
    }
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_EDIT", payload=payload)
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(_text_event("invalid-multi-button", f"修改營養數字:{token}"))
    server._handle_message_impl(
        _text_event("invalid-multi-value", "熱量 204、蛋白質 很多")
    )
    with sqlite3.connect(db) as conn:
        stored = json.loads(conn.execute(
            "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=?", (token,)
        ).fetchone()[0])
        state_count = conn.execute("SELECT COUNT(*) FROM nutrition_input_states").fetchone()[0]
    assert stored["per_serving"]["calories_kcal"] == 228
    assert stored["per_serving"]["protein_g"] == 21.2
    assert state_count == 1
    assert "蛋白質 很多" in replies[-1].text


def test_committed_text_edit_reply_failure_is_retriable(tmp_path, monkeypatch):
    db = tmp_path / "edit-reply-failure.db"
    payload = {
        **valid_label(), "package_amount": 400,
        "per_serving": {**valid_label()["per_serving"], "sodium_mg": 488},
        "per_100": {**valid_label()["per_100"], "sodium_mg": 122},
    }
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_REPLY", payload=payload)
        set_nutrition_input_state(conn, user_id="U_REPLY", token=token, input_type="nutrient")
    event = _text_event("edit-reply-failure", "鈉 48", user_id="U_REPLY")
    monkeypatch.setattr(server, "DB_PATH", str(db))

    def fail_reply(*_):
        raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(reply_message=fail_reply))
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server._handle_message_impl(event)
    assert "edit-reply-failure" not in server.processed_messages
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nutrition_message_events WHERE message_id='edit-reply-failure'"
        ).fetchone()[0] == 1
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(event)
    assert len(replies) == 1
    assert isinstance(replies[0], list)
    assert "已更新 1 個項目（重送確認）" in replies[0][0].text
    assert "鈉：488 → 48 mg" in replies[0][0].text
    assert "0 個項目" not in replies[0][0].text

def test_committed_confirmation_reply_failure_is_retriable(tmp_path, monkeypatch):
    db = tmp_path / "confirm-reply-failure.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,sheet_name)
               VALUES ('U_CONFIRM_REPLY','',2000,100,'')"""
        )
        token = save_pending_label(conn, user_id="U_CONFIRM_REPLY", payload=valid_label())
    event = _text_event(
        "confirm-reply-failure",
        f"確認營養紀錄:{token}",
        user_id="U_CONFIRM_REPLY",
    )
    monkeypatch.setattr(server, "get_active_nutrition_target", lambda *_: None)
    monkeypatch.setattr(server, "sync_confirmed_nutrition_to_sheet", lambda *_: None)
    monkeypatch.setattr(server, "apply_confirmed_nutrition_to_legacy_dashboard", lambda *_: None)
    refresh_users = []
    monkeypatch.setattr(
        server,
        "_refresh_health_check_after_food_log",
        lambda _conn, *, user_id: refresh_users.append(user_id),
    )

    def fail_reply(*_):
        raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(reply_message=fail_reply))
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server._handle_message_impl(event)
    assert "confirm-reply-failure" not in server.processed_messages
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    server._handle_message_impl(event)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    assert len(replies) == 1
    assert replies[0].alt_text == "今日總覽"
    assert "今日總覽" in json.dumps(
        json.loads(replies[0].as_json_string()), ensure_ascii=False
    )
    assert refresh_users == ["U_CONFIRM_REPLY", "U_CONFIRM_REPLY"]


def test_exchange_review_admin_commands_list_approve_and_replay(tmp_path, monkeypatch):
    db = tmp_path / "exchange-admin.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    food_id = confirmed["food"]["food_id"]
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "flush_nutrition_sheet_outbox", lambda: 2)

    pending_text = server.handle_exchange_review_admin_command(
        "#待審營養份量", server.ADMIN_UID
    )
    assert "測試豆漿" in pending_text
    assert "低脂蛋白 2.71份｜主食 0.53份" in pending_text
    assert f"#核准營養份量 {food_id}" in pending_text

    with pytest.raises(PermissionError):
        server.handle_exchange_review_admin_command(
            f"#核准營養份量 {food_id}", "U_NOT_ADMIN"
        )

    approved_text = server.handle_exchange_review_admin_command(
        f"#核准營養份量 {food_id}", server.ADMIN_UID
    )
    replay_text = server.handle_exchange_review_admin_command(
        f"#核准營養份量 {food_id}", server.ADMIN_UID
    )
    assert "核准完成" in approved_text
    assert "更新 1 筆飲食紀錄" in approved_text
    assert "已經核准" in replay_text
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT exchange_review_status FROM food_catalog WHERE food_id=?", (food_id,)
        ).fetchone()[0] == "approved"


def test_exchange_review_commands_are_admin_only_and_intercepted_by_line_handler(tmp_path, monkeypatch):
    assert server.is_admin_only_command("#待審營養份量")
    assert server.is_admin_only_command("#核准營養份量 food_1234567890abcdef")
    db = tmp_path / "exchange-admin-handler.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message)),
    )
    event = _text_event("exchange-admin-list", "#待審營養份量", user_id=server.ADMIN_UID)
    server.processed_messages.discard("exchange-admin-list")
    server._handle_message_impl(event)
    assert len(replies) == 1
    assert "待審營養份量" in replies[0].text
    assert "#核准營養份量 food_" in replies[0].text
    replies.clear()
    denied = _text_event(
        "exchange-admin-denied",
        f"#核准營養份量 {confirmed['food']['food_id']}",
        user_id="U_NOT_ADMIN",
    )
    server.processed_messages.discard("exchange-admin-denied")
    server._handle_message_impl(denied)
    assert len(replies) == 1
    assert "管理員專用" in replies[0].text
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT exchange_review_status FROM food_catalog WHERE food_id=?",
            (confirmed["food"]["food_id"],),
        ).fetchone()[0] == "pending_review"


def test_exchange_approval_reply_failure_retries_idempotently(tmp_path, monkeypatch):
    db = tmp_path / "exchange-admin-retry.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    food_id = confirmed["food"]["food_id"]
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "flush_nutrition_sheet_outbox", lambda: 2)
    replies = []
    attempts = {"count": 0}

    def flaky_reply(_, message):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("LINE timeout")
        replies.append(message)

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(reply_message=flaky_reply))
    event = _text_event(
        "exchange-admin-retry",
        f"#核准營養份量 {food_id}",
        user_id=server.ADMIN_UID,
    )
    server.processed_messages.discard("exchange-admin-retry")
    with pytest.raises(RuntimeError, match="LINE timeout"):
        server._handle_message_impl(event)
    assert "exchange-admin-retry" not in server.processed_messages
    server._handle_message_impl(event)
    assert len(replies) == 1
    assert "已經核准" in replies[0].text
    with sqlite3.connect(db) as conn:
        suggestion_json, applied_json = conn.execute(
            """SELECT exchange_snapshot_json,approved_exchange_json
               FROM food_logs WHERE food_id=?""", (food_id,)
        ).fetchone()
    assert __import__("json").loads(suggestion_json)["_review_status"] == "pending_review"
    assert __import__("json").loads(applied_json)["_review_status"] == "approved"


def test_exchange_admin_error_reply_failure_allows_webhook_retry(monkeypatch):
    event = _text_event(
        "exchange-admin-error-retry",
        "#核准營養份量 food_missing",
        user_id=server.ADMIN_UID,
    )
    server.processed_messages.discard("exchange-admin-error-retry")
    monkeypatch.setattr(
        server,
        "handle_exchange_review_admin_command",
        lambda *_: (_ for _ in ()).throw(ValueError("找不到待審核食品")),
    )
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda *_: (_ for _ in ()).throw(RuntimeError("LINE timeout"))),
    )
    with pytest.raises(RuntimeError, match="LINE timeout"):
        server._handle_message_impl(event)
    assert "exchange-admin-error-retry" not in server.processed_messages


def test_meal_log_flex_distinguishes_pending_and_approved_exchange_status():
    args = ("測試食品", 100, 10, 100, 2000, 10, 100)
    pending = server.build_meal_log_flex(
        *args, exchange_text="主食 1份", exchange_review_status="pending_review"
    )
    approved = server.build_meal_log_flex(
        *args, exchange_text="主食 1份", exchange_review_status="approved"
    )
    pending_payload = __import__("json").dumps(pending.as_json_dict(), ensure_ascii=False)
    approved_payload = __import__("json").dumps(approved.as_json_dict(), ensure_ascii=False)
    assert "待營養師審核，尚未扣入個人計畫" in pending_payload
    assert "正式營養份數：主食 1份" in approved_payload
    assert "已納入個人計畫" in approved_payload


def test_food_log_sheet_exports_exchange_only_after_approval(tmp_path, monkeypatch):
    db = tmp_path / "exchange-sheet.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CUSTOMER", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CUSTOMER")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    captured = []
    monkeypatch.setattr(server, "_nutrition_ws", lambda _: object())
    monkeypatch.setattr(
        server, "_upsert_raw_sheet_row",
        lambda _ws, entity_id, values: captured.append((entity_id, values)),
    )

    server._sync_food_outbox(confirmed["food"]["food_id"])
    pending_food_row = captured[-1][1]
    assert pending_food_row[17:25] == [0] * 8
    server._sync_food_log_outbox(confirmed["log"]["log_id"])
    pending_row = captured[-1][1]
    assert pending_row[16:24] == [0] * 8

    with sqlite3.connect(db) as conn:
        server.approve_food_exchange_suggestion(
            conn, food_id=confirmed["food"]["food_id"], reviewer="ADMIN"
        )
    server._sync_food_outbox(confirmed["food"]["food_id"])
    approved_food_row = captured[-1][1]
    assert approved_food_row[18] == 2.71
    assert approved_food_row[21] == 0.53
    server._sync_food_log_outbox(confirmed["log"]["log_id"])
    approved_row = captured[-1][1]
    assert approved_row[17] == 2.71
    assert approved_row[20] == 0.53
    with sqlite3.connect(db) as conn:
        applied = __import__("json").loads(conn.execute(
            "SELECT approved_exchange_json FROM food_logs WHERE log_id=?",
            (confirmed["log"]["log_id"],),
        ).fetchone()[0])
        applied["protein_low_exchange"] = 99
        conn.execute(
            "UPDATE food_logs SET approved_exchange_json=? WHERE log_id=?",
            (__import__("json").dumps(applied), confirmed["log"]["log_id"]),
        )
        conn.commit()
    server._sync_food_log_outbox(confirmed["log"]["log_id"])
    assert captured[-1][1][16:24] == [0] * 8


def test_ordinary_approved_catalog_sheet_remains_valid_without_a_food_log(tmp_path, monkeypatch):
    db = tmp_path / "catalog-without-log.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(conn, user_id="U_CATALOG", payload=valid_label())
        confirmed = confirm_pending_label(conn, token=token, user_id="U_CATALOG")
        food_id = confirmed["food"]["food_id"]
        server.approve_food_exchange_suggestion(conn, food_id=food_id, reviewer="ADMIN")
        conn.execute("DELETE FROM food_logs WHERE food_id=?", (food_id,))
        conn.commit()

    monkeypatch.setattr(server, "DB_PATH", str(db))
    captured = []
    monkeypatch.setattr(server, "_nutrition_ws", lambda _: object())
    monkeypatch.setattr(
        server, "_upsert_raw_sheet_row",
        lambda _ws, entity_id, values: captured.append((entity_id, values)),
    )

    server._sync_food_outbox(food_id)

    assert captured[-1][0] == food_id
    assert captured[-1][1][18] == 2.71
    assert captured[-1][1][21] == 0.53
    assert captured[-1][1][25] == "approved"


def test_jason_health_checkin_is_admin_scoped_and_saved_for_taipei_date(tmp_path, monkeypatch):
    db = tmp_path / "health-checkin.db"
    with sqlite3.connect(db) as conn:
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_OTHER")
    now = datetime(2026, 7, 22, 22, 31, tzinfo=server.TW_TZ)
    text = "健康回報｜體重70.2｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好"

    confirmation = server.save_jason_health_checkin("U_JASON", text, now=now)

    assert "2026/07/22" in confirmation
    with sqlite3.connect(db) as conn:
        saved = get_daily_health_checkin(
            conn, user_id="U_JASON", report_date="2026-07-22"
        )
    assert saved["water_ml"] == 2500
    with pytest.raises(PermissionError):
        server.save_jason_health_checkin("U_OTHER", text, now=now)


def test_intervals_global_fallback_is_denied_for_non_jason_uid(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "jason-athlete")
    monkeypatch.setenv("INTERVALS_API_KEY", "secret-for-test")

    assert server._jason_intervals_credentials("U_OTHER", "2026-07-22") is None


def test_build_jason_daily_health_report_uses_db_plan_and_intervals(tmp_path, monkeypatch):
    db = tmp_path / "daily-report.db"
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(
        server, "fetch_daily_intervals_summary",
        lambda _uid, _date: {"items": [], "total_calories": 0, "total_duration_min": 0, "hr_load": 0},
    )
    monkeypatch.setattr(
        server, "get_daily_nutrition_target",
        lambda _uid, _date: {"starch_exchange": 6, "protein_low_exchange": 13},
    )

    report = server.build_jason_daily_health_report("U_JASON", "2026-07-22")

    assert "2026/07/22 一日健康日報" in report
    assert "今日無已確認飲食紀錄" in report
    assert "🥣 營養份量" not in report
    assert "主食：0／6｜尚缺6" not in report
    assert "蛋白質食物：0／13｜尚缺13" not in report
    assert "今日運動：無活動紀錄" in report
    assert "休息日" not in report
    with pytest.raises(PermissionError):
        server.build_jason_daily_health_report("U_OTHER", "2026-07-22")


def test_scheduled_daily_report_push_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "scheduled-report.db"
    with sqlite3.connect(db) as conn:
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_OTHER")
    monkeypatch.setattr(server, "build_jason_daily_health_report", lambda _uid, _date: "REPORT")

    class FakeLineApi:
        def __init__(self):
            self.sent = []

        def push_message(self, uid, message, timeout=None):
            self.sent.append((uid, message.text, timeout))

    fake = FakeLineApi()
    monkeypatch.setattr(server, "line_bot_api", fake)
    now = datetime(2026, 7, 22, 23, 30, tzinfo=server.TW_TZ)

    assert server.send_jason_daily_health_report(now=now) is True
    assert server.send_jason_daily_health_report(now=now) is False
    assert fake.sent == [("U_JASON", "REPORT", 12)]


def meal_photo_payload():
    return {
        "status": "success",
        "image_type": "food_photo",
        "visible_items": [
            {"name": "高麗菜", "category": "vegetable", "confidence": 0.98},
            {"name": "青花菜", "category": "vegetable", "confidence": 0.97},
        ],
        "uncertain_items": ["上方棕色主菜種類不明"],
        "starch_visibility": "not_visible",
        "oil_sauce_status": "unknown",
        "observed_at": "2026-07-22T22:37:00+08:00",
        "observed_at_confidence": 0.99,
    }


def _confirmed_v2_food_log(db, *, user_id="U1", source_message_id="V2-CONTAIN"):
    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [
            {"name": "雞腿飯", "portion": "約1份", "calories_kcal": 680, "protein_g": 35}
        ],
        "calories_kcal": {"estimate": 680, "min": 580, "max": 800},
        "protein_g": {"estimate": 35, "min": 29, "max": 43},
        "confidence": 0.78,
        "provenance": {
            "provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate",
            "nutrition_basis": "unlabeled_meal_photo",
        },
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id=user_id, source_message_id=source_message_id, payload=payload,
            source_image_ref="nutrition-image:" + "a" * 32 + ".jpg",
            meal_slot="午餐", consumed_at=f"{server.tw_today().isoformat()}T12:00:00+08:00",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id=user_id, token=token)
        confirmed = apply_meal_photo_action(
            conn, event_id=f"{source_message_id}-CONFIRM", user_id=user_id, token=token,
            expected_version=draft["version"], action="confirm_estimate",
        )
        conn.commit()
    return token, confirmed["result"]["log_id"]


@pytest.mark.parametrize(
    ("action", "field", "value"),
    [
        ("correct_nutrition", "calories_kcal", 700),
        ("patch_nutrition", "", {"calories_kcal": 700, "protein_g": 40}),
        ("set_servings", "", 1.5),
        ("set_meal_slot", "", "晚餐"),
        ("rename", "", "新名稱"),
        ("replace_item", "", {"name": "新品項", "nutrition": {"calories_kcal": 500}}),
    ],
)
def test_confirmed_v2_food_log_rejects_legacy_edits_without_mutating_evidence(
    tmp_path, monkeypatch, action, field, value,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, f"v2-contain-{action}.db")
    token, log_id = _confirmed_v2_food_log(db, source_message_id=f"V2-{action}")
    with sqlite3.connect(db) as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "food_logs", "food_catalog", "pending_meal_photo_drafts",
                "meal_photo_events", "daily_food_log_events", "nutrition_sheet_outbox",
            )
        }
        trust_before = conn.execute(
            "SELECT trust_payload_json,trust_hash,exchange_snapshot_json,nutrition_snapshot_json "
            "FROM food_logs WHERE log_id=?", (log_id,),
        ).fetchone()
        draft_before = get_meal_photo_draft(conn, user_id="U1", token=token)

    with pytest.raises(
        ValueError,
        match="此AI紀錄暫不支援確認後直接修改；可撤銷後重新記錄",
    ):
        server.apply_daily_food_log_edit(
            user_id="U1", log_id=log_id, expected_version=1,
            event_id=f"blocked-{action}", action=action, field=field, value=value,
        )

    with sqlite3.connect(db) as conn:
        assert {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in before
        } == before
        assert conn.execute(
            "SELECT trust_payload_json,trust_hash,exchange_snapshot_json,nutrition_snapshot_json "
            "FROM food_logs WHERE log_id=?", (log_id,),
        ).fetchone() == trust_before
        assert get_meal_photo_draft(conn, user_id="U1", token=token) == draft_before


def test_confirmed_v2_food_log_card_hides_legacy_edits_but_keeps_view_and_delete(
    tmp_path, monkeypatch,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "v2-contain-card.db")
    _token, log_id = _confirmed_v2_food_log(db, source_message_id="V2-CARD")
    item = next(
        item for item in server.get_daily_food_ledger("U1", server.tw_today().isoformat())["items"]
        if item["log_id"] == log_id
    )
    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", True)
    rendered = json.dumps(server._daily_food_item_bubble(item), ensure_ascii=False)
    assert "重新查看" in rendered
    assert "修改這餐" in rendered
    assert f"mealrev:v1:{log_id}:1:start" in rendered
    assert "撤銷紀錄" in rendered
    assert f"foodlog:v1:{log_id}:1:delete:ask" in rendered
    for hidden in ("調整份量", "修正營養", "更多操作", "修改品項"):
        assert hidden not in rendered

    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", False)
    disabled_rendered = json.dumps(server._daily_food_item_bubble(item), ensure_ascii=False)
    assert "修改這餐" not in disabled_rendered
    assert f"mealrev:v1:{log_id}:1:start" not in disabled_rendered
    assert "重新查看" in disabled_rendered
    assert "撤銷紀錄" in disabled_rendered

    # Even a historically damaged v2 row must not regain legacy edit controls.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json='{\"calories_kcal\":999}' WHERE log_id=?",
            (log_id,),
        )
        conn.commit()
    damaged_item = next(
        item for item in server.get_daily_food_ledger("U1", server.tw_today().isoformat())["items"]
        if item["log_id"] == log_id
    )
    monkeypatch.setattr(server, "CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED", True)
    damaged_rendered = json.dumps(server._daily_food_item_bubble(damaged_item), ensure_ascii=False)
    assert "撤銷紀錄" in damaged_rendered
    assert "修改這餐" not in damaged_rendered
    for hidden in ("調整份量", "修正營養", "更多操作", "修改品項"):
        assert hidden not in damaged_rendered


def test_confirmed_v2_food_log_delete_enforces_owner_and_version_then_replays_safely(
    tmp_path, monkeypatch,
):
    db = _daily_ledger_db(tmp_path, monkeypatch, "v2-contain-delete.db")
    _token, log_id = _confirmed_v2_food_log(db, source_message_id="V2-DELETE")
    with pytest.raises(ValueError, match="找不到"):
        server.apply_daily_food_log_edit(
            user_id="OTHER", log_id=log_id, expected_version=1,
            event_id="v2-delete-other", action="delete",
        )
    with pytest.raises(ValueError, match="已更新"):
        server.apply_daily_food_log_edit(
            user_id="U1", log_id=log_id, expected_version=2,
            event_id="v2-delete-stale", action="delete",
        )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE nutrition_sheet_outbox SET status='processing',synced_at='worker-old' "
            "WHERE entity_type='food_log' AND entity_id=?", (log_id,),
        )
        conn.commit()

    deleted = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log_id, expected_version=1,
        event_id="v2-delete", action="delete",
    )
    replay = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log_id, expected_version=1,
        event_id="v2-delete", action="delete",
    )
    assert deleted["action"] == "delete" and deleted["version"] == 2
    assert replay["replayed"] is True and replay["version"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT confirmation_status,version FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone() == ("deleted", 2)
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_food_log_events WHERE event_id='v2-delete'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,resync_required FROM nutrition_sheet_outbox "
            "WHERE entity_type='food_log' AND entity_id=?", (log_id,),
        ).fetchone() == ("processing", 1)


def test_vision_prompt_requests_bounded_ai_food_photo_nutrition_estimate():
    prompt = server.build_nutrition_vision_prompt()
    food_section = prompt.split("若只有餐盤照片", 1)[1]
    assert "visible_items" in food_section
    assert "uncertain_items" in food_section
    assert "ai_estimate" in food_section
    assert "calories_kcal" in food_section
    assert "protein_g" in food_section
    assert "合理區間" in food_section
    assert "看不清或關鍵歧義" in food_section
    assert '"calories_kcal":0' not in food_section


def test_unknown_image_reply_mentions_meal_photos(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(
        server.line_bot_api, "get_message_content",
        lambda _: SimpleNamespace(content=b"\xff\xd8\xff" + b"x" * 100),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "status": "success", "image_type": "unknown"
        })))]
    )
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **_: response)
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    event = SimpleNamespace(
        message=SimpleNamespace(id="unknown-image-message"),
        source=SimpleNamespace(user_id="U_UNKNOWN"), reply_token="reply-unknown",
        timestamp=1784740620000,
    )
    server.processed_messages.discard(event.message.id)

    server.handle_image_message(event)

    assert len(replies) == 1
    assert "餐點照片" in replies[0].text
    assert "Garmin" in replies[0].text
    assert "營養標示" in replies[0].text


def test_food_photo_image_handler_stages_durable_unknown_safe_flex(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-handler.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(
        server.line_bot_api,
        "get_message_content",
        lambda _: SimpleNamespace(content=b"\xff\xd8\xff" + b"x" * 100),
    )
    parsed = meal_photo_payload()
    parsed["ai_estimate"] = {
        "items": [{"name": "高麗菜", "portion": "約1碗", "calories_kcal": 80, "protein_g": 4}],
        "calories_kcal": {"estimate": 120, "min": 80, "max": 180},
        "protein_g": {"estimate": 6, "min": 4, "max": 9},
        "confidence": 0.75,
    }
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=__import__("json").dumps(parsed)))]
    )
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **_: response)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(
        message=SimpleNamespace(id="meal-photo-message"),
        source=SimpleNamespace(user_id="U_MEAL"),
        reply_token="reply",
        timestamp=1784740620000,
    )
    server.processed_messages.discard(event.message.id)

    server.handle_image_message(event)

    assert len(replies) == 1
    text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "照片估算" in text
    assert "約120 kcal" in text
    assert "確認記錄" in text
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        row = conn.execute(
            "SELECT token,source_image_ref,status FROM pending_meal_photo_drafts WHERE user_id='U_MEAL'"
        ).fetchone()
    assert row[1].startswith("nutrition-image:")
    assert row[2] == "estimated"
    image_path = server._nutrition_image_path(row[1])
    assert image_path is not None and os.path.exists(image_path)


def _ai_food_photo_handler_fixture(tmp_path, monkeypatch, message_id):
    db = tmp_path / f"{message_id}.db"
    image_bytes = b"\xff\xd8\xff" + b"x" * 100
    parsed = meal_photo_payload()
    parsed["ai_estimate"] = {
        "items": [{"name": "豆腐飯", "portion": "約1份", "calories_kcal": 420, "protein_g": 22}],
        "calories_kcal": {"estimate": 420, "min": 340, "max": 520},
        "protein_g": {"estimate": 22, "min": 17, "max": 29},
        "confidence": 0.74,
    }
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(parsed, ensure_ascii=False))
    )])
    model_calls = []
    replies = []
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(
        server.line_bot_api, "get_message_content",
        lambda _message_id: SimpleNamespace(content=image_bytes),
    )
    monkeypatch.setattr(
        server.client.chat.completions, "create",
        lambda **kwargs: (model_calls.append(kwargs) or response),
    )
    monkeypatch.setattr(
        server.line_bot_api, "reply_message",
        lambda _reply_token, message: replies.append(message),
    )
    event = SimpleNamespace(
        message=SimpleNamespace(id=message_id), source=SimpleNamespace(user_id="U_MEAL"),
        reply_token=f"reply-{message_id}", timestamp=1784740620000,
    )
    server.processed_messages.discard(message_id)
    return db, image_bytes, event, model_calls, replies


def test_food_photo_storage_failure_never_commits_confirmable_draft(tmp_path, monkeypatch):
    db, _image_bytes, event, model_calls, replies = _ai_food_photo_handler_fixture(
        tmp_path, monkeypatch, "PHOTO-STORE-FAIL"
    )
    monkeypatch.setattr(
        server, "_store_nutrition_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        server.handle_image_message(event)

    assert len(model_calls) == 1
    assert replies == []
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone() == (0,)
        assert conn.execute(
            "SELECT status,attempts FROM meal_photo_image_events WHERE user_id=? AND source_message_id=?",
            ("U_MEAL", event.message.id),
        ).fetchone() == ("failed", 1)


def test_food_photo_file_is_durable_before_draft_commit(tmp_path, monkeypatch):
    db, image_bytes, event, model_calls, _replies = _ai_food_photo_handler_fixture(
        tmp_path, monkeypatch, "PHOTO-BEFORE-DRAFT"
    )
    observed = {}

    def crash_before_draft_commit(conn, **kwargs):
        ref = kwargs["source_image_ref"]
        path = server._nutrition_image_path(ref)
        observed["ref"] = ref
        observed["bytes"] = server._read_valid_nutrition_image(ref)
        assert path and os.path.isfile(path)
        raise SystemExit("draft commit crash")

    monkeypatch.setattr(server, "save_meal_photo_draft", crash_before_draft_commit)

    with pytest.raises(SystemExit, match="draft commit crash"):
        server.handle_image_message(event)

    assert len(model_calls) == 1
    assert observed["bytes"] == image_bytes
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone() == (0,)
        assert conn.execute(
            "SELECT status,attempts FROM meal_photo_image_events WHERE user_id=? AND source_message_id=?",
            ("U_MEAL", event.message.id),
        ).fetchone() == ("processing", 1)


def test_food_photo_draft_commit_crash_replay_reuses_model_and_existing_file(tmp_path, monkeypatch):
    db, image_bytes, event, model_calls, replies = _ai_food_photo_handler_fixture(
        tmp_path, monkeypatch, "PHOTO-DRAFT-COMMITTED"
    )
    real_finish = server.finish_meal_photo_image_event
    monkeypatch.setattr(
        server, "finish_meal_photo_image_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("event completion crash")),
    )
    with pytest.raises(SystemExit, match="event completion crash"):
        server.handle_image_message(event)

    with sqlite3.connect(db) as conn:
        draft_row = conn.execute(
            "SELECT token,source_image_ref,status,version FROM pending_meal_photo_drafts"
        ).fetchone()
        assert draft_row and draft_row[2:] == ("estimated", 1)
    assert server._read_valid_nutrition_image(draft_row[1]) == image_bytes

    monkeypatch.setattr(server, "finish_meal_photo_image_event", real_finish)
    server.processed_messages.clear()
    server.handle_image_message(event)

    assert len(model_calls) == 1
    assert len(replies) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone() == (1,)
        status, attempts, result_json = conn.execute(
            "SELECT status,attempts,result_json FROM meal_photo_image_events"
        ).fetchone()
    assert status == "completed" and attempts == 1
    assert json.loads(result_json) == {
        "token": draft_row[0], "draft_status": "estimated", "draft_version": 1,
    }


def test_existing_draft_missing_photo_replay_refetches_source_without_model(tmp_path, monkeypatch):
    db, image_bytes, event, model_calls, replies = _ai_food_photo_handler_fixture(
        tmp_path, monkeypatch, "PHOTO-REFETCH"
    )
    with sqlite3.connect(db) as conn:
        claim = server.claim_meal_photo_image_event(
            conn, user_id="U_MEAL", source_message_id=event.message.id
        )
        payload = meal_photo_payload()
        payload["ai_estimate"] = {
            "items": [{"name": "豆腐飯", "portion": "約1份", "calories_kcal": 420, "protein_g": 22}],
            "calories_kcal": {"estimate": 420, "min": 340, "max": 520},
            "protein_g": {"estimate": 22, "min": 17, "max": 29},
            "confidence": 0.74,
            "provenance": {"provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
        }
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id=event.message.id, payload=payload,
            source_image_ref="nutrition-image:" + "c" * 32 + ".jpg",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        server.release_meal_photo_image_event(
            conn, user_id="U_MEAL", source_message_id=event.message.id,
            claim_token=claim["claim_token"],
        )
    monkeypatch.setattr(
        server.client.chat.completions, "create",
        lambda **_kwargs: pytest.fail("persisted estimate must not rerun the model"),
    )

    server.processed_messages.clear()
    server.handle_image_message(event)

    assert model_calls == []
    assert len(replies) == 1
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
        assert draft["status"] == "estimated" and draft["version"] == 1
        assert conn.execute("SELECT status FROM meal_photo_image_events").fetchone() == ("completed",)
    assert server._read_valid_nutrition_image(draft["source_image_ref"]) == image_bytes


def test_existing_draft_photo_refetch_failure_never_completes_or_replies_success(tmp_path, monkeypatch):
    db, _image_bytes, event, _model_calls, replies = _ai_food_photo_handler_fixture(
        tmp_path, monkeypatch, "PHOTO-REFETCH-FAIL"
    )
    with sqlite3.connect(db) as conn:
        claim = server.claim_meal_photo_image_event(
            conn, user_id="U_MEAL", source_message_id=event.message.id
        )
        payload = meal_photo_payload()
        payload["ai_estimate"] = {
            "items": [{"name": "豆腐飯", "portion": "約1份", "calories_kcal": 420, "protein_g": 22}],
            "calories_kcal": {"estimate": 420, "min": 340, "max": 520},
            "protein_g": {"estimate": 22, "min": 17, "max": 29}, "confidence": 0.74,
            "provenance": {"provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
        }
        save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id=event.message.id, payload=payload,
            source_image_ref="nutrition-image:" + "d" * 32 + ".jpg",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        server.release_meal_photo_image_event(
            conn, user_id="U_MEAL", source_message_id=event.message.id,
            claim_token=claim["claim_token"],
        )
    monkeypatch.setattr(
        server.line_bot_api, "get_message_content",
        lambda _message_id: (_ for _ in ()).throw(RuntimeError("LINE source unavailable")),
    )
    monkeypatch.setattr(
        server.client.chat.completions, "create",
        lambda **_kwargs: pytest.fail("persisted estimate must not rerun the model"),
    )

    server.processed_messages.clear()
    with pytest.raises(RuntimeError, match="LINE source unavailable"):
        server.handle_image_message(event)

    assert replies == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM meal_photo_image_events").fetchone() == ("failed",)


def test_confirm_handler_fails_closed_when_estimated_draft_photo_is_missing(tmp_path, monkeypatch):
    db = tmp_path / "confirm-missing-photo.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [{"name": "豆腐飯", "portion": "約1份", "calories_kcal": 420, "protein_g": 22}],
        "calories_kcal": {"estimate": 420, "min": 340, "max": 520},
        "protein_g": {"estimate": 22, "min": 17, "max": 29}, "confidence": 0.74,
        "provenance": {"provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="CONFIRM-MISSING", payload=payload,
            source_image_ref="nutrition-image:" + "e" * 32 + ".jpg",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    server.handle_meal_photo_postback(SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:confirm_estimate"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="confirm-missing",
        webhook_event_id="CONFIRM-MISSING-EVENT", timestamp=1784740620000,
    ))

    assert len(replies) == 1 and "原圖" in replies[0].text
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone() == (0,)
        assert conn.execute(
            "SELECT status,version FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone() == ("estimated", 1)


def test_ai_first_adjust_postback_and_text_reestimate_same_draft_via_real_handlers(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-adjust-handler.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    image_ref = "nutrition-image:" + "a" * 32 + ".jpg"
    image_path = server._nutrition_image_path(image_ref)
    assert image_path is not None
    os.makedirs(os.path.dirname(image_path), exist_ok=True)
    with open(image_path, "wb") as fh:
        fh.write(b"\xff\xd8\xff" + b"x" * 100)
    original = meal_photo_payload()
    original["ai_estimate"] = {
        "items": [
            {"name": "雞腿", "portion": "約1支", "calories_kcal": 330, "protein_g": 27},
            {"name": "白飯", "portion": "約1碗", "calories_kcal": 280, "protein_g": 5},
        ],
        "calories_kcal": {"estimate": 680, "min": 580, "max": 800},
        "protein_g": {"estimate": 35, "min": 29, "max": 43},
        "confidence": 0.78,
        "provenance": {
            "provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate",
            "nutrition_basis": "unlabeled_meal_photo",
        },
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_ADJUST", payload=original,
            source_image_ref=image_ref, workflow_version="user_confirmed_ai_nutrition_v2",
        )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    server.handle_meal_photo_postback(SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:request_adjust"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="adjust",
        webhook_event_id="WEBHOOK-ADJUST", timestamp=1784740620000,
    ))
    assert "一句話" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U_MEAL", token=token)["status"] == "awaiting_adjustment"

    calls = []
    revised = meal_photo_payload()
    revised["ai_estimate"] = {
        "items": [
            {"name": "雞腿", "portion": "約1支", "calories_kcal": 330, "protein_g": 27},
            {"name": "白飯", "portion": "約半碗", "calories_kcal": 140, "protein_g": 2.5},
            {"name": "無糖豆漿", "portion": "約1杯", "calories_kcal": 90, "protein_g": 9},
        ],
        "calories_kcal": {"estimate": 600, "min": 500, "max": 720},
        "protein_g": {"estimate": 40, "min": 33, "max": 48}, "confidence": 0.74,
    }
    def fake_create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(revised)))])
    monkeypatch.setattr(server.client.chat.completions, "create", fake_create)
    correction = "飯只吃一半，肉全吃，另豆漿"
    server.processed_messages.clear()
    server._handle_message_impl(_text_event("ADJUST-TEXT-1", correction, user_id="U_MEAL"))
    assert len(calls) == 1
    serialized_messages = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert correction in serialized_messages and "原始辨識payload" in serialized_messages
    assert calls[0]["messages"][0]["role"] == "system"
    assert correction not in calls[0]["messages"][0]["content"]
    image_url = calls[0]["messages"][1]["content"][0]["image_url"]["url"]
    assert base64.b64decode(image_url.split(",", 1)[1]) == b"\xff\xd8\xff" + b"x" * 100
    card = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "約600 kcal" in card and "無糖豆漿" in card
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
        assert draft["status"] == "estimated" and draft["version"] == 3
        assert draft["estimate"]["calories_kcal"] == 600
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0

    server.processed_messages.clear()
    server._handle_message_impl(_text_event("ADJUST-TEXT-1", correction, user_id="U_MEAL"))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "unsafe_image", ["root_symlink", "leaf_symlink", "oversized", "fifo", "invalid_magic"]
)
def test_ai_adjust_handler_rejects_unsafe_original_without_calling_provider(
    tmp_path, monkeypatch, unsafe_image
):
    db = tmp_path / f"meal-photo-adjust-{unsafe_image}.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    image_ref = "nutrition-image:" + "c" * 32 + ".jpg"
    image_name = image_ref.removeprefix("nutrition-image:")
    image_root = tmp_path / "nutrition_images"
    valid_image = b"\xff\xd8\xff" + b"x" * 100
    if unsafe_image == "root_symlink":
        external = tmp_path / "external-images"
        external.mkdir()
        (external / image_name).write_bytes(valid_image)
        image_root.symlink_to(external, target_is_directory=True)
    else:
        image_root.mkdir()
        if unsafe_image == "leaf_symlink":
            outside = tmp_path / "outside.jpg"
            outside.write_bytes(valid_image)
            (image_root / image_name).symlink_to(outside)
        else:
            unsafe_path = image_root / image_name
            if unsafe_image == "oversized":
                with open(unsafe_path, "wb") as image_file:
                    image_file.write(b"\xff\xd8\xff")
                    image_file.truncate(10 * 1024 * 1024 + 1)
            elif unsafe_image == "fifo":
                os.mkfifo(unsafe_path)
            else:
                unsafe_path.write_bytes(b"not-an-image" + b"x" * 100)

    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [{"name": "豆腐", "portion": "約1份", "calories_kcal": 180, "protein_g": 16}],
        "calories_kcal": {"estimate": 220, "min": 170, "max": 290},
        "protein_g": {"estimate": 18, "min": 14, "max": 24}, "confidence": 0.7,
        "provenance": {"provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_UNSAFE", source_message_id="UNSAFE-IMAGE", payload=payload,
            source_image_ref=image_ref, workflow_version="user_confirmed_ai_nutrition_v2",
        )
        apply_meal_photo_action(
            conn, event_id="REQUEST-UNSAFE", user_id="U_UNSAFE", token=token,
            expected_version=1, action="request_adjust",
        )

    provider_calls = []
    monkeypatch.setattr(
        server.client.chat.completions, "create",
        lambda **kwargs: provider_calls.append(kwargs) or pytest.fail("unsafe image must not reach provider"),
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    server.processed_messages.clear()
    server._handle_message_impl(
        _text_event(f"ADJUST-{unsafe_image}", "飯只吃一半", user_id="U_UNSAFE")
    )

    assert provider_calls == []
    assert len(replies) == 1 and replies[0].type == "text"
    assert "原估算已保留" in replies[0].text and "重新估算，請確認" not in replies[0].text
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_UNSAFE", token=token)
        assert draft["status"] == "awaiting_adjustment" and draft["version"] == 2
        assert draft["estimate"]["calories_kcal"] == 220
        assert conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE action='adjust_estimate'"
        ).fetchone() == (0,)


def test_ai_adjust_missing_original_image_keeps_old_estimate_and_consumes_text(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-adjust-no-image.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [{"name": "豆腐", "portion": "約1份", "calories_kcal": 180, "protein_g": 16}],
        "calories_kcal": {"estimate": 220, "min": 170, "max": 290},
        "protein_g": {"estimate": 18, "min": 14, "max": 24}, "confidence": 0.7,
        "provenance": {"provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="NO-IMAGE", payload=payload,
            source_image_ref="nutrition-image:" + "b" * 32 + ".jpg",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        apply_meal_photo_action(conn, event_id="REQ", user_id="U1", token=token, expected_version=1, action="request_adjust")
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **_: pytest.fail("model must not run"))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    server.processed_messages.clear()
    server._handle_message_impl(_text_event("NO-IMAGE-TEXT", "不是豬肉是豆腐", user_id="U1"))
    assert "原圖" in replies[-1].text and "原估算已保留" in replies[-1].text
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert draft["status"] == "awaiting_adjustment"
        assert draft["estimate"]["calories_kcal"] == 220
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_meal_photo_postback_request_add_then_text_updates_confirmation_card(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-add-item.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_ADD", payload=meal_photo_payload()
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    request_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:add"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-add-request",
        webhook_event_id="WEBHOOK-ADD-REQUEST", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(request_event)

    assert len(replies) == 1
    rendered_prompt = replies[0].as_json_dict()
    assert rendered_prompt["text"] == "請輸入要新增的食材與份量，可一次輸入多項。"
    assert rendered_prompt["quickReply"]["items"][0]["action"] == {
        "type": "postback",
        "label": "取消新增",
        "data": f"mp:v1:{token}:2:cancel_add",
        "displayText": "確認取消新增食材",
    }
    with sqlite3.connect(db) as conn:
        waiting = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert waiting["status"] == "awaiting_item_name"
    assert waiting["version"] == 2

    replies.clear()
    text_event = _text_event("ADD-ITEM-NAME", "玉米筍", user_id="U_MEAL")
    server.processed_messages.discard(text_event.message.id)
    server._handle_message_impl(text_event)

    assert len(replies) == 1
    assert "選擇食材分類" in replies[0].text
    category_actions = [item.action.data for item in replies[0].quick_reply.items]
    assert any(f"mp:v1:{token}:2:add_item:vegetable:" in data for data in category_actions)
    assert f"mp:v1:{token}:2:cancel_add" in category_actions
    with sqlite3.connect(db) as conn:
        still_waiting = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert still_waiting["status"] == "awaiting_item_name"
    assert still_waiting["version"] == 2

    replies.clear()
    vegetable_data = next(
        data for data in category_actions
        if f"mp:v1:{token}:2:add_item:vegetable:" in data
    )
    category_event = SimpleNamespace(
        postback=SimpleNamespace(data=vegetable_data),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-add-category",
        webhook_event_id="WEBHOOK-ADD-CATEGORY", timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(category_event)

    assert len(replies) == 1
    card_text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "玉米筍" in card_text
    assert f"mp:v1:{token}:3:start" in card_text
    with sqlite3.connect(db) as conn:
        updated = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert updated["status"] == "awaiting_confirmation"
    assert updated["version"] == 3
    assert updated["payload"]["visible_items"][-1] == {
        "name": "玉米筍", "category": "vegetable", "confidence": 1.0,
    }


def test_meal_photo_request_add_can_be_cancelled_from_quick_reply(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-cancel-add-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CANCEL", source_message_id="M_CANCEL", payload=meal_photo_payload()
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    request_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:request_add"),
        source=SimpleNamespace(user_id="U_CANCEL"), reply_token="reply-request-cancel",
        webhook_event_id="WEBHOOK-REQUEST-CANCEL", timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(request_event)

    cancel_data = f"mp:v1:{token}:2:cancel_add"
    assert cancel_data in json.dumps(replies[0].as_json_dict(), ensure_ascii=False)
    replies.clear()
    cancel_event = SimpleNamespace(
        postback=SimpleNamespace(data=cancel_data),
        source=SimpleNamespace(user_id="U_CANCEL"), reply_token="reply-cancel-add",
        webhook_event_id="WEBHOOK-CANCEL-ADD", timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(cancel_event)

    assert len(replies) == 1
    assert replies[0].type == "flex"
    with sqlite3.connect(db) as conn:
        restored = get_meal_photo_draft(conn, user_id="U_CANCEL", token=token)
    assert restored["status"] == "awaiting_confirmation"
    assert restored["version"] == 3


def test_meal_photo_typed_cancel_is_not_treated_as_an_ingredient(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-typed-cancel.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_TYPED_CANCEL", source_message_id="M_TYPED_CANCEL",
            payload=meal_photo_payload(),
        )
        apply_meal_photo_action(
            conn, event_id="REQUEST-TYPED-CANCEL", user_id="U_TYPED_CANCEL",
            token=token, expected_version=1, action="request_add",
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    text_event = _text_event("TYPED-CANCEL", "取消新增", user_id="U_TYPED_CANCEL")
    server.processed_messages.discard(text_event.message.id)

    server._handle_message_impl(text_event)

    assert len(replies) == 1
    assert replies[0].type == "flex"
    with sqlite3.connect(db) as conn:
        waiting = get_meal_photo_draft(conn, user_id="U_TYPED_CANCEL", token=token)
    assert (waiting["status"], waiting["version"]) == ("awaiting_confirmation", 3)
    assert [item["name"] for item in waiting["payload"]["visible_items"]] == [
        "高麗菜", "青花菜"
    ]


def test_meal_photo_add_item_rejects_name_when_encoded_postback_exceeds_line_limit(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-long-item.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_LONG", source_message_id="M_LONG", payload=meal_photo_payload()
        )
        apply_meal_photo_action(
            conn, event_id="REQUEST-LONG", user_id="U_LONG", token=token,
            expected_version=1, action="request_add",
        )
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    text_event = _text_event("ADD-LONG-NAME", "🍣" * 60, user_id="U_LONG")
    server.processed_messages.discard(text_event.message.id)

    server._handle_message_impl(text_event)

    assert len(replies) == 1
    assert "名稱過長" in replies[0].text
    assert replies[0].quick_reply.items[0].action.data == f"mp:v1:{token}:2:cancel_add"
    with sqlite3.connect(db) as conn:
        waiting = get_meal_photo_draft(conn, user_id="U_LONG", token=token)
    assert waiting["status"] == "awaiting_item_name"
    assert waiting["version"] == 2


def test_meal_photo_postback_finalizes_estimate(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-postback-final.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_MEAL")
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M1", payload=meal_photo_payload(),
            workflow_version="expert_review_v1",
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"PREP-{index}", user_id="U_MEAL", token=token,
                expected_version=index, action="answer", field=field, value=value,
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:8:answer:sauce_level:half"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-final",
        webhook_event_id="WEBHOOK-FINAL", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(event)

    assert len(replies) == 1
    estimate_text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "照片估算" in estimate_text
    assert "審核並加入" in estimate_text
    assert f"mpr:v1:{token}:9:start" in estimate_text
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
    assert draft["status"] == "estimated"
    assert draft["estimate"]["starch_exchange"] == {"min": 0.0, "max": 0.0, "basis": "user_confirmed_none"}


def test_customer_estimate_pushes_review_request_to_configured_admin(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-customer-push.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_FALLBACK_ADMIN")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_CUSTOMER",
            payload=meal_photo_payload(),
            source_image_ref="nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
            workflow_version="expert_review_v1",
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"CUSTOMER-PREP-{index}", user_id="U_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )
    replies = []
    pushes = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(
            reply_message=lambda _token, message: replies.append(message),
            push_message=lambda target, message, **_kwargs: pushes.append((target, message)),
        ),
    )
    monkeypatch.setattr(
        server,
        "build_meal_photo_image_url",
        lambda _draft, *, preview=False, now=None: (
            "https://example.test/meal-photo-image/token.jpg?preview="
            + ("1" if preview else "0")
        ),
    )
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:8:answer:sauce_level:half"),
        source=SimpleNamespace(user_id="U_CUSTOMER"), reply_token="reply-customer-final",
        webhook_event_id="WEBHOOK-CUSTOMER-FINAL", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(event)

    assert replies[0].alt_text == "餐點照片估算完成，等待營養師審核"
    customer_text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "審核並加入" not in customer_text
    assert len(pushes) == 2
    assert all(target == "U_ADMIN" for target, _message in pushes)
    image_message = pushes[0][1]
    assert isinstance(image_message, server.ImageSendMessage)
    assert str(image_message.original_content_url).endswith("preview=0")
    assert str(image_message.preview_image_url).endswith("preview=1")
    admin_payload = pushes[1][1]
    assert isinstance(admin_payload, list)
    assert len(admin_payload) == 2
    admin_text = json.dumps(
        [json.loads(str(message)) for message in admin_payload], ensure_ascii=False
    )
    assert "新的餐點審核需求" in admin_text
    assert f"mpr:v1:{token}:9:start" in admin_text


def test_new_customer_estimate_alt_text_still_prompts_confirmation(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-new-customer-alt-text.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_NEW_CUSTOMER", source_message_id="M_NEW_CUSTOMER",
            payload=meal_photo_payload(),
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"NEW-CUSTOMER-PREP-{index}", user_id="U_NEW_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(
            reply_message=lambda _token, message: replies.append(message),
            push_message=lambda *_args, **_kwargs: pytest.fail("new workflow must not push review"),
        ),
    )
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:8:answer:sauce_level:half"),
        source=SimpleNamespace(user_id="U_NEW_CUSTOMER"), reply_token="reply-new-customer-final",
        webhook_event_id="WEBHOOK-NEW-CUSTOMER-FINAL", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(event)

    assert replies[0].alt_text == "餐點照片估算完成，請確認記錄"
    customer_text = json.dumps(json.loads(str(replies[0].contents)), ensure_ascii=False)
    assert "確認記錄" in customer_text


def test_meal_photo_review_photo_failure_falls_back_to_text_and_card(monkeypatch):
    draft = {
        "token": "abcdef123456",
        "user_id": "U_CUSTOMER",
        "source_image_ref": "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        "payload": meal_photo_payload(),
        "meal_slot": "午餐",
        "consumed_at": "2026-08-15T12:00:00+08:00",
        "version": 8,
        "estimate": {},
    }
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(
        server,
        "build_meal_photo_estimate_bubble",
        lambda _draft, allow_admin_review=False: {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": []}},
    )
    monkeypatch.setattr(
        server,
        "build_meal_photo_image_url",
        lambda _draft, *, preview=False, now=None: (
            "https://example.test/meal-photo-image/token.jpg?preview="
            + ("1" if preview else "0")
        ),
    )
    pushes = []

    def push_message(target, messages, **_kwargs):
        pushes.append((target, messages))
        if len(pushes) == 1:
            raise RuntimeError("LINE image fetch failed")

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(push_message=push_message))

    assert server.push_meal_photo_review_request(draft) is True
    assert len(pushes) == 2
    assert isinstance(pushes[0][1], server.ImageSendMessage)
    assert len(pushes[1][1]) == 2
    assert not isinstance(pushes[1][1][0], server.ImageSendMessage)


def test_meal_photo_review_message_build_failure_never_escapes(monkeypatch):
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(
        server,
        "build_meal_photo_admin_review_messages",
        lambda _draft: (_ for _ in ()).throw(ValueError("bad draft")),
    )
    monkeypatch.setattr(
        server,
        "line_bot_api",
        SimpleNamespace(push_message=lambda *_args, **_kwargs: pytest.fail("must not push")),
    )
    assert server.push_meal_photo_review_request({"user_id": "U_CUSTOMER"}) is False


def test_meal_photo_image_url_rejects_weak_secret_and_non_https(monkeypatch):
    draft = {
        "token": "abcdef123456",
        "source_image_ref": "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        "expires_at": datetime.fromtimestamp(1_800_001_200, timezone.utc).isoformat(),
    }
    monkeypatch.setattr(server, "MEAL_PHOTO_IMAGE_SECRET", "weak-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://example.test")
    with pytest.raises(RuntimeError):
        server.build_meal_photo_image_url(draft, now=1_800_000_000)

    monkeypatch.setattr(server, "MEAL_PHOTO_IMAGE_SECRET", "s" * 32)
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "http://example.test/private")
    with pytest.raises(ValueError):
        server.build_meal_photo_image_url(draft, now=1_800_000_000)


def test_meal_photo_image_url_is_signed_expiring_and_bound_to_variant(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-image-url.db"
    image_root = tmp_path / "nutrition_images"
    image_root.mkdir()
    image_path = image_root / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
    image_path.write_bytes(b"\xff\xd8\xff" + b"x" * 200)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "MEAL_PHOTO_IMAGE_SECRET", "s" * 32)
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://example.test")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn,
            user_id="U_CUSTOMER",
            source_message_id="M_IMAGE",
            payload=meal_photo_payload(),
            source_image_ref="nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        )
        conn.execute(
            """UPDATE pending_meal_photo_drafts SET status='estimated',expires_at=?
               WHERE token=?""",
            (datetime.fromtimestamp(1_800_001_200, timezone.utc).isoformat(), token),
        )
        conn.commit()
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)

    url = server.build_meal_photo_image_url(draft, preview=True, now=1_800_000_000)
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path == f"/meal-photo-image/{token}.jpg"
    assert query["preview"] == ["1"]
    assert int(query["expires"][0]) == 1_800_000_600

    resolved = server._authorize_meal_photo_image_request(
        token=token,
        extension="jpg",
        expires=int(query["expires"][0]),
        signature=query["sig"][0],
        preview=True,
        now=1_800_000_001,
    )
    assert resolved == str(image_path)
    with pytest.raises(server.HTTPException) as tampered:
        server._authorize_meal_photo_image_request(
            token=token,
            extension="jpg",
            expires=int(query["expires"][0]),
            signature=query["sig"][0],
            preview=False,
            now=1_800_000_001,
        )
    assert tampered.value.status_code == 403
    with pytest.raises(server.HTTPException) as expired:
        server._authorize_meal_photo_image_request(
            token=token,
            extension="jpg",
            expires=int(query["expires"][0]),
            signature=query["sig"][0],
            preview=True,
            now=1_800_000_601,
        )
    assert expired.value.status_code == 410

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='approved' WHERE token=?",
            (token,),
        )
        conn.commit()
    with pytest.raises(server.HTTPException) as approved:
        server._authorize_meal_photo_image_request(
            token=token,
            extension="jpg",
            expires=int(query["expires"][0]),
            signature=query["sig"][0],
            preview=True,
            now=1_800_000_001,
        )
    assert approved.value.status_code == 410

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='estimated',expires_at=? WHERE token=?",
            (datetime.fromtimestamp(1_800_000_000, timezone.utc).isoformat(), token),
        )
        conn.commit()
    with pytest.raises(server.HTTPException) as draft_expired:
        server._authorize_meal_photo_image_request(
            token=token,
            extension="jpg",
            expires=int(query["expires"][0]),
            signature=query["sig"][0],
            preview=True,
            now=1_800_000_001,
        )
    assert draft_expired.value.status_code == 410

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='cancelled' WHERE token=?",
            (token,),
        )
        conn.commit()
    with pytest.raises(server.HTTPException) as cancelled:
        server._authorize_meal_photo_image_request(
            token=token,
            extension="jpg",
            expires=int(query["expires"][0]),
            signature=query["sig"][0],
            preview=True,
            now=1_800_000_001,
        )
    assert cancelled.value.status_code == 410


def test_owner_notification_retries_after_failure_and_then_deduplicates(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-notification-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_NOTIFY",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    attempts = {"count": 0}

    def flaky_push(_target, _message, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(push_message=flaky_push))
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is False
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert attempts["count"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM meal_photo_notification_events
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0] == 1


def test_notification_retry_postback_works_after_original_version_changes(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-notification-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_NOTIFY_POSTBACK",
            payload=meal_photo_payload(),
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='rejected',version=10 WHERE token=?",
            (token,),
        )
        conn.commit()
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    attempts = {"count": 0}
    replies = []

    def flaky_push(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")

    api = SimpleNamespace(
        push_message=flaky_push,
        reply_message=lambda _token, message: replies.append(message),
    )
    monkeypatch.setattr(server, "line_bot_api", api)
    assert server.push_meal_photo_return_to_owner(draft) is False
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mprn:v1:{token}:rejected"),
        source=SimpleNamespace(user_id="U_ADMIN"),
        reply_token="RETRY-REPLY",
    )
    server.handle_meal_photo_postback(event)
    assert attempts["count"] == 2
    assert replies[-1].text == "✅ 客戶通知已送達。"


def test_approved_notification_retry_uses_exact_protein_exchange_shape(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-approved-notification-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    review = {
        "protein_class": "medium", "protein_exchange": 2.5,
        "starch_exchange": 6, "vegetable_exchange": 2,
        "milk_exchange": 0, "fruit_exchange": 1,
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_APPROVED_NOTIFY_RETRY",
            payload=meal_photo_payload(),
        )
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET status='approved',version=15,review_json=? WHERE token=?""",
            (json.dumps(review), token),
        )
        conn.commit()
    pushes = []
    replies = []
    api = SimpleNamespace(
        push_message=lambda target, message, **kwargs: pushes.append((target, message, kwargs)),
        reply_message=lambda _token, message: replies.append(message),
    )
    monkeypatch.setattr(server, "line_bot_api", api)
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mprn:v1:{token}:approved"),
        source=SimpleNamespace(user_id="U_ADMIN"),
        reply_token="APPROVED-RETRY-REPLY",
    )
    server.handle_meal_photo_postback(event)
    assert len(pushes) == 1
    assert "估算熱量 698.5 kcal" in pushes[0][1].text
    assert "蛋白質 31.5g" in pushes[0][1].text
    assert replies[-1].text == "✅ 客戶通知已送達。"


def test_successful_push_with_marker_failure_is_not_immediately_released(
    tmp_path, monkeypatch,
):
    db = tmp_path / "meal-photo-marker-failure.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_MARKER_FAILURE",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    api_attempts = []

    def successful_push(_target, _message, **kwargs):
        api_attempts.append(kwargs.get("retry_key"))

    monkeypatch.setattr(
        server, "line_bot_api", SimpleNamespace(push_message=successful_push)
    )
    monkeypatch.setattr(
        server, "complete_meal_photo_notification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("marker unavailable")),
    )
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert server.push_meal_photo_approval_to_owner(draft, result) is False
    assert len(api_attempts) == 1
    assert api_attempts[0] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"meal-photo:{token}:owner_approved")
    )
    with sqlite3.connect(db) as conn:
        status = conn.execute(
            """SELECT status FROM meal_photo_notification_claims
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0]
    assert status == "sending"


def test_line_retry_conflict_with_accepted_request_id_completes_marker(
    tmp_path, monkeypatch,
):
    db = tmp_path / "meal-photo-accepted-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_ACCEPTED_RETRY",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)

    class AcceptedRetryConflict(Exception):
        status_code = 409
        accepted_request_id = "REQ-ALREADY-ACCEPTED"

    def accepted_conflict(*_args, **_kwargs):
        raise AcceptedRetryConflict("already accepted")

    monkeypatch.setattr(
        server, "line_bot_api", SimpleNamespace(push_message=accepted_conflict)
    )
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    with sqlite3.connect(db) as conn:
        marker_count = conn.execute(
            """SELECT COUNT(*) FROM meal_photo_notification_events
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0]
        claim_status = conn.execute(
            """SELECT status FROM meal_photo_notification_claims
               WHERE token=? AND notification_kind='owner_approved'""",
            (token,),
        ).fetchone()[0]
    assert marker_count == 1
    assert claim_status == "delivered"


def test_retry_key_does_not_leak_into_shared_line_client_headers(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-retry-header.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_RETRY_HEADER",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)

    class HeaderMutatingLineApi:
        def __init__(self):
            self.headers = {"Authorization": "Bearer test"}
            self.calls = []

        def push_message(self, target, message, **kwargs):
            retry_key = kwargs.get("retry_key")
            if retry_key:
                self.headers["X-Line-Retry-Key"] = retry_key
            self.calls.append((target, message, kwargs))

    api = HeaderMutatingLineApi()
    monkeypatch.setattr(server, "line_bot_api", api)
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    assert server.push_meal_photo_approval_to_owner(draft, result) is True
    assert len(api.calls) == 1
    assert "X-Line-Retry-Key" not in api.headers
    assert api.headers == {"Authorization": "Bearer test"}


def test_owner_notification_claim_blocks_concurrent_duplicate_push(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-notification-concurrent.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_NOTIFY_CONCURRENT",
            payload=meal_photo_payload(),
        )
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
    entered = threading.Event()
    release = threading.Event()
    attempts = []

    def blocking_push(*_args, **_kwargs):
        attempts.append(1)
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(server, "line_bot_api", SimpleNamespace(push_message=blocking_push))
    result = {
        "approved_exchange": {"starch_exchange": 1, "vegetable_exchange": 1},
        "estimated_nutrition": {"calories_kcal": 200, "protein_g": 10},
    }
    first_result = []
    worker = threading.Thread(
        target=lambda: first_result.append(
            server.push_meal_photo_approval_to_owner(draft, result)
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    second_result = server.push_meal_photo_approval_to_owner(draft, result)
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert first_result == [True]
    assert second_result is False
    assert len(attempts) == 1


def test_bound_admin_authorization_fails_closed_on_database_error(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "missing" / "db.sqlite"))
    monkeypatch.setattr(server, "ADMIN_UID", "U" + "a" * 32)
    with pytest.raises(PermissionError, match="無法驗證管理員"):
        server.get_bound_admin_uid_for_authorization()


def test_customer_meal_photo_confirm_postback_records_without_review_push(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-customer-confirm.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,sheet_name)
               VALUES ('U_CUSTOMER','',2000,100,'')"""
        )
        conn.commit()
    refresh_users, replies = [], []
    monkeypatch.setattr(server, "_prepare_health_check_refresh_connection", lambda _conn: True)
    monkeypatch.setattr(
        server, "_refresh_health_check_after_food_log",
        lambda _conn, *, user_id: refresh_users.append(user_id),
    )
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(
            reply_message=lambda _token, message: capture_dashboard_reply(replies, message),
            push_message=lambda *_args, **_kwargs: pytest.fail("customer confirmation must not push review"),
        ),
    )
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M-CUSTOMER-CONFIRM",
            payload=meal_photo_payload(), consumed_at="2026-09-12T12:10:00+08:00",
            meal_slot="午餐",
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "one_bowl"), ("vegetable_portion", "one_bowl"),
            ("cooking_oil", "unknown"), ("sauce_level", "unknown"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"CUSTOMER-CONFIRM-PREP-{index}", user_id="U_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:9:confirm_estimate"),
        source=SimpleNamespace(user_id="U_CUSTOMER"), reply_token="reply-customer-confirm",
        webhook_event_id="CUSTOMER-CONFIRM-EVENT", timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(event)
    assert replies[-1].alt_text == "今日總覽"
    assert "今日總覽" in json.dumps(
        json.loads(replies[-1].as_json_string()), ensure_ascii=False
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM food_exchange_approvals").fetchone()[0] == 0
        food_id, log_id = conn.execute("SELECT food_id,log_id FROM food_logs").fetchone()
    sheet_rows = {}
    class _Sheet:
        def __init__(self, title):
            self.title = title
        def find(self, *_args, **_kwargs):
            return None
        def append_row(self, values, **_kwargs):
            sheet_rows[self.title] = values
    monkeypatch.setattr(server, "_nutrition_ws", lambda title: _Sheet(title))
    server._sync_food_outbox(food_id)
    server._sync_food_log_outbox(log_id)
    assert sheet_rows["食品資料庫"][10:25] == [""] * 15
    assert sheet_rows["飲食紀錄"][9:24] == [""] * 15
    assert sheet_rows["飲食紀錄"][29:] == [
        "user_confirmed_ai_estimate", "meal-photo-user-confirmation-v1",
    ]
    server.handle_meal_photo_postback(event)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    assert refresh_users == ["U_CUSTOMER", "U_CUSTOMER"]

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE food_logs SET trust_hash='tampered' WHERE log_id=?", (log_id,))
        conn.commit()
        summary_item = server.daily_food_summary(
            conn, user_id="U_CUSTOMER", date_iso="2026-09-12"
        )["foods"][0]
    server._sync_food_outbox(food_id)
    server._sync_food_log_outbox(log_id)
    assert sheet_rows["食品資料庫"][10:25] == [""] * 15
    assert sheet_rows["食品資料庫"][25] == "untrusted_user_confirmed_ai_estimate"
    assert sheet_rows["飲食紀錄"][9:24] == [""] * 15
    assert sheet_rows["飲食紀錄"][29:] == [
        "untrusted_user_confirmed_ai_estimate", "",
    ]
    assert summary_item["trust_type"] == sheet_rows["飲食紀錄"][29]


def test_pending_meal_photo_admin_command_lists_cross_user_review_buttons(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-pending-command.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_BOUND_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_BOUND_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_PENDING",
            payload=meal_photo_payload(),
            workflow_version="expert_review_v1",
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
            ("sauce_level", "half"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"PENDING-PREP-{index}", user_id="U_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )

    with pytest.raises(PermissionError, match="管理員限定"):
        server.build_pending_meal_photo_review_message("U_FORGED")
    message = server.build_pending_meal_photo_review_message("U_BOUND_ADMIN")
    assert "待審餐點（1筆" in message.text
    assert "U_CUSTOMER"[-8:] in message.text
    actions = [item.action.data for item in message.quick_reply.items]
    assert actions == [f"mpr:v1:{token}:9:start"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET status='reviewing',version=9,review_json='{}' WHERE token=?""",
            (token,),
        )
        conn.commit()
    resumed = server.build_pending_meal_photo_review_message("U_BOUND_ADMIN")
    assert "審核中" in resumed.text
    assert resumed.quick_reply.items[0].action.data == f"mpr:v1:{token}:9:resume"


@pytest.mark.parametrize("refresh_mode", ["spy", "live", "repair"])
def test_admin_meal_photo_review_postbacks_apply_formal_totals(tmp_path, monkeypatch, refresh_mode):
    db = tmp_path / "meal-photo-admin-review.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    refresh_users = []
    case = {}
    if refresh_mode == "spy":
        monkeypatch.setattr(server, "_prepare_health_check_refresh_connection", lambda _conn: True)
        monkeypatch.setattr(
            server,
            "_refresh_health_check_after_food_log",
            lambda _conn, *, user_id: refresh_users.append(user_id),
        )
    else:
        from vip_health_check import (
            configure_vip_health_check_connection,
            create_first_vip_health_check_case,
            ensure_vip_health_check_schema,
        )

        monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
        with sqlite3.connect(db) as conn:
            server.ensure_daily_food_ledger_schema(conn)
            configure_vip_health_check_connection(conn)
            ensure_vip_health_check_schema(conn)
            case = create_first_vip_health_check_case(
                conn, user_id="U_CUSTOMER",
                first_vip_activation_id="approval-activation",
                activation_event_key="approval-activation-event",
                activated_at=datetime(2026, 7, 23, 7, tzinfo=server.TW_TZ),
            )
            if refresh_mode == "repair":
                conn.execute("""CREATE TRIGGER reject_health_check_source
                    BEFORE INSERT ON vip_health_check_source_refs
                    BEGIN SELECT RAISE(ABORT, 'temporary manifest failure'); END""")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_CUSTOMER", payload=meal_photo_payload(),
            consumed_at="2026-07-23T12:10:00+08:00", meal_slot="午餐",
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "one_half_bowl"),
            ("vegetable_portion", "none"), ("cooking_oil", "light"),
            ("sauce_level", "half"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"PREP-ADMIN-{index}", user_id="U_CUSTOMER", token=token,
                expected_version=index, action="answer", field=field, value=value,
            )
    replies = []
    pushes = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    monkeypatch.setattr(
        server.line_bot_api, "push_message",
        lambda target, message, **_kwargs: pushes.append((target, message)),
    )

    def send(data, event_id):
        event = SimpleNamespace(
            postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id="U_ADMIN"),
            reply_token=f"reply-{event_id}", webhook_event_id=event_id,
            timestamp=1784740620000,
        )
        server.handle_meal_photo_postback(event)
        return replies[-1]

    first = send(f"mpr:v1:{token}:9:start", "ADMIN-START")
    assert "蛋白質分類" in first.text
    assert any(":10:set:protein_class:medium" in item.action.data for item in first.quick_reply.items)
    resumed_step = send(f"mpr:v1:{token}:10:resume", "ADMIN-RESUME")
    assert "蛋白質分類" in resumed_step.text
    assert any(":10:set:protein_class:medium" in item.action.data for item in resumed_step.quick_reply.items)
    send(f"mpr:v1:{token}:10:set:protein_class:medium", "ADMIN-CLASS")
    send(f"mpr:v1:{token}:11:set:protein_exchange:2.5", "ADMIN-PROTEIN")
    send(f"mpr:v1:{token}:12:set:starch_exchange:6", "ADMIN-STARCH")
    send(f"mpr:v1:{token}:13:set:milk_exchange:0", "ADMIN-MILK")
    ready = send(f"mpr:v1:{token}:14:set:fruit_exchange:0", "ADMIN-FRUIT")
    ready_text = json.dumps(json.loads(str(ready.contents)), ensure_ascii=False)
    assert "最終核准份量" in ready_text
    assert f"mpr:v1:{token}:15:approve" in ready_text

    done = send(f"mpr:v1:{token}:15:approve", "ADMIN-APPROVE")
    done_text = json.dumps(json.loads(str(done.contents)), ensure_ascii=False)
    assert "已核准｜已計入正式份量" in done_text
    assert '"主食"' in done_text and '"6份"' in done_text
    assert '"中脂蛋白"' in done_text and '"2.5份"' in done_text
    with sqlite3.connect(db) as conn:
        totals = daily_consumed_totals(conn, user_id="U_CUSTOMER", date_iso="2026-07-23")
        admin_totals = daily_consumed_totals(conn, user_id="U_ADMIN", date_iso="2026-07-23")
        assert totals["starch_exchange"] == 6.0
        assert totals["protein_medium_exchange"] == 2.5
        assert admin_totals["starch_exchange"] == 0.0
    assert pushes and pushes[-1][0] == "U_CUSTOMER"
    assert "已由營養師核准" in pushes[-1][1].text
    push_count = len(pushes)
    if refresh_mode != "spy":
        with sqlite3.connect(db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM vip_health_check_source_refs WHERE case_id=?",
                (case["case_id"],),
            ).fetchone()[0] == (0 if refresh_mode == "repair" else 1)
            assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
            if refresh_mode == "repair":
                conn.execute("DROP TRIGGER reject_health_check_source")
    replayed_done = send(f"mpr:v1:{token}:15:approve", "ADMIN-APPROVE")
    assert "已核准｜已計入正式份量" in json.dumps(
        json.loads(str(replayed_done.contents)), ensure_ascii=False
    )
    assert len(pushes) == push_count
    if refresh_mode == "spy":
        assert refresh_users == ["U_CUSTOMER", "U_CUSTOMER"]
    else:
        with sqlite3.connect(db) as conn:
            refs = conn.execute(
                "SELECT food_log_id,food_log_version,source_hash "
                "FROM vip_health_check_source_refs WHERE case_id=?",
                (case["case_id"],),
            ).fetchall()
            assert len(refs) == 1
            log = conn.execute(
                "SELECT log_id,version,nutrition_snapshot_json,consumed_at,meal_slot "
                "FROM food_logs"
            ).fetchall()
            assert len(log) == 1
            assert refs[0][:2] == log[0][:2]
            assert refs[0][2] == _expected_health_source_v2_hash(
                log_id=log[0][0],
                version=log[0][1],
                nutrition=log[0][2],
                consumed_at=log[0][3],
                meal_slot=log[0][4],
            )
            assert conn.execute("SELECT COUNT(*) FROM food_exchange_approvals").fetchone()[0] == 1
    with sqlite3.connect(db) as conn:
        assert daily_consumed_totals(
            conn, user_id="U_CUSTOMER", date_iso="2026-07-23"
        )["starch_exchange"] == 6.0
        food_id, log_id, approval_id = conn.execute(
            "SELECT food_id,log_id,exchange_approval_id FROM food_logs "
            "WHERE user_id='U_CUSTOMER'"
        ).fetchone()
        conn.execute(
            "UPDATE food_exchange_approvals SET approved_exchange_json='[]' WHERE approval_id=?",
            (approval_id,),
        )
        conn.commit()

    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
        ledger_item = server._ledger_item_from_row(
            conn, server._daily_food_rows(conn, "U_CUSTOMER", "2026-07-23")[0]
        )
    assert ledger_item["nutrition"] == {}

    sheet_rows = {}

    class IntegritySheet:
        def __init__(self, title):
            self.title = title

        def find(self, *_args, **_kwargs):
            return None

        def append_row(self, values, **_kwargs):
            sheet_rows[self.title] = values

    monkeypatch.setattr(server, "_nutrition_ws", lambda title: IntegritySheet(title))
    server._sync_food_outbox(food_id)
    server._sync_food_log_outbox(log_id)
    assert sheet_rows["食品資料庫"][10:25] == [""] * 15
    assert sheet_rows["食品資料庫"][25] == "integrity_verification_failed"
    assert sheet_rows["飲食紀錄"][9:24] == [""] * 15


def test_admin_meal_photo_reject_returns_result_to_customer_without_formal_log(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-admin-reject.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_ADMIN")
    monkeypatch.setattr(server, "get_bound_admin_uid_for_authorization", lambda: "U_ADMIN")
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_CUSTOMER", source_message_id="M_REJECT",
            payload=meal_photo_payload(),
        )
        for index, (field, value) in enumerate((
            ("scope", "visible_only"), ("protein_type", "chicken"),
            ("protein_portion", "one_palm"), ("protein_more", "done"),
            ("starch_portion", "none"),
            ("vegetable_portion", "two_bowl"), ("cooking_oil", "light"),
            ("sauce_level", "half"),
        ), start=1):
            apply_meal_photo_action(
                conn, event_id=f"REJECT-PREP-{index}", user_id="U_CUSTOMER",
                token=token, expected_version=index, action="answer", field=field, value=value,
            )
    replies, pushes = [], []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(
            reply_message=lambda _token, message: replies.append(message),
            push_message=lambda target, message, **_kwargs: pushes.append((target, message)),
        ),
    )
    server.handle_meal_photo_postback(SimpleNamespace(
        postback=SimpleNamespace(data=f"mpr:v1:{token}:9:start"),
        source=SimpleNamespace(user_id="U_ADMIN"), reply_token="reject-start",
        webhook_event_id="REJECT-START", timestamp=1784740620000,
    ))
    server.handle_meal_photo_postback(SimpleNamespace(
        postback=SimpleNamespace(data=f"mpr:v1:{token}:10:reject"),
        source=SimpleNamespace(user_id="U_ADMIN"), reply_token="reject-final",
        webhook_event_id="REJECT-FINAL", timestamp=1784740620001,
    ))
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_CUSTOMER", token=token)
        assert draft["status"] == "rejected"
        assert conn.execute("SELECT COUNT(*) FROM food_logs WHERE user_id='U_CUSTOMER'").fetchone()[0] == 0
    assert "已退回客戶" in replies[-1].text
    assert pushes[-1][0] == "U_CUSTOMER"
    assert "退回" in pushes[-1][1].text


def test_meal_photo_postback_replays_after_reply_failure_without_double_update(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-postback.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_POST", payload=meal_photo_payload()
        )
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:answer:scope:visible_only"),
        source=SimpleNamespace(user_id="U_MEAL"),
        reply_token="reply-postback",
        webhook_event_id="WEBHOOK-EVENT-1",
        timestamp=1784740620000,
    )
    calls = {"count": 0}

    def fail_first_reply(_token, _message):
        calls["count"] += 1
        raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server.line_bot_api, "reply_message", fail_first_reply)
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server.handle_meal_photo_postback(event)
    with sqlite3.connect(db) as conn:
        draft = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
        events = conn.execute("SELECT COUNT(*) FROM meal_photo_events").fetchone()[0]
    assert draft["version"] == 2
    assert draft["answers"]["scope"] == "visible_only"
    assert events == 1

    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )
    server.handle_meal_photo_postback(event)
    with sqlite3.connect(db) as conn:
        replayed = get_meal_photo_draft(conn, user_id="U_MEAL", token=token)
        events = conn.execute("SELECT COUNT(*) FROM meal_photo_events").fetchone()[0]
    assert replayed["version"] == 2
    assert events == 1
    assert len(replies) == 1
    assert "主要蛋白質食物" in replies[0].text
    option_data = [item.action.data for item in replies[0].quick_reply.items]
    answer_data = [data for data in option_data if ":answer:" in data]
    assert answer_data and all(
        data.startswith(f"mp:v1:{token}:2:answer:protein_type:") for data in answer_data
    )
    assert f"mp:v1:{token}:2:cancel" in option_data


def test_cancel_meal_photo_deletes_file_before_clearing_reference(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-cancel.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn,
            user_id="U_MEAL",
            source_message_id="M2",
            payload=meal_photo_payload(),
            source_image_ref="nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        )
    deleted = []
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda ref: deleted.append(ref) or True)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(
        postback=SimpleNamespace(data=f"mp:v1:{token}:1:cancel"),
        source=SimpleNamespace(user_id="U_MEAL"), reply_token="reply-cancel",
        webhook_event_id="WEBHOOK-CANCEL", timestamp=1784740620000,
    )

    server.handle_meal_photo_postback(event)

    assert deleted == ["nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"]
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status,source_image_ref,observed_payload_json FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
    assert row == ("cancelled", "", "{}")
    assert "已取消" in replies[0].text


def test_cleanup_retries_expired_meal_photo_image_and_scrubs_payload(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-expiry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = "nutrition-image:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M3",
            payload=meal_photo_payload(), source_image_ref=ref,
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda _ref: False)

    server.cleanup_nutrition_images()

    with sqlite3.connect(db) as conn:
        status, payload, retained_ref = conn.execute(
            "SELECT status,observed_payload_json,source_image_ref FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
    assert (status, payload, retained_ref) == ("expired", "{}", ref)

    monkeypatch.setattr(server, "_delete_nutrition_image", lambda _ref: True)
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        cleared_ref = conn.execute(
            "SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0]
    assert cleared_ref == ""


def test_cleanup_expires_awaiting_adjustment_and_scrubs_private_evidence(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-awaiting-adjustment-expiry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = "nutrition-image:" + "b" * 32 + ".jpg"
    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [{"name": "豆腐飯", "portion": "約1份", "calories_kcal": 420, "protein_g": 22}],
        "calories_kcal": {"estimate": 420, "min": 340, "max": 520},
        "protein_g": {"estimate": 22, "min": 17, "max": 29},
        "confidence": 0.74,
        "provenance": {
            "provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate",
            "nutrition_basis": "unlabeled_meal_photo",
        },
    }
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_ADJUST_EXPIRED",
            payload=payload, source_image_ref=ref,
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        apply_meal_photo_action(
            conn, event_id="REQUEST-ADJUST-EXPIRED", user_id="U_MEAL", token=token,
            expected_version=1, action="request_adjust",
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda _ref: True)

    server.cleanup_nutrition_images()

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """SELECT status,observed_payload_json,answers_json,estimate_json,source_image_ref
               FROM pending_meal_photo_drafts WHERE token=?""",
            (token,),
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone() == (0,)
    assert row == ("expired", "{}", "{}", "{}", "")


def _stage_adjustment_cleanup_draft(conn, *, message_id):
    ref = "nutrition-image:" + "c" * 32 + ".jpg"
    payload = meal_photo_payload()
    payload["ai_estimate"] = {
        "items": [{"name": "豆腐飯", "portion": "約1份", "calories_kcal": 420, "protein_g": 22}],
        "calories_kcal": {"estimate": 420, "min": 340, "max": 520},
        "protein_g": {"estimate": 22, "min": 17, "max": 29},
        "confidence": 0.74,
        "provenance": {
            "provider": "openai", "model": "gpt-4o", "method": "vision_model_estimate",
            "nutrition_basis": "unlabeled_meal_photo",
        },
    }
    token = save_meal_photo_draft(
        conn, user_id="U_MEAL", source_message_id=message_id,
        payload=payload, source_image_ref=ref,
        workflow_version="user_confirmed_ai_nutrition_v2",
    )
    apply_meal_photo_action(
        conn, event_id=f"REQUEST-{message_id}", user_id="U_MEAL", token=token,
        expected_version=1, action="request_adjust",
    )
    return token, ref


def test_cleanup_expires_adjusting_after_worker_hard_crash_without_redelivery(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-adjusting-hard-crash.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    with sqlite3.connect(db) as conn:
        token, _ref = _stage_adjustment_cleanup_draft(conn, message_id="ADJUST-HARD-CRASH")
        with pytest.raises(SystemExit, match="hard crash"):
            server.apply_meal_photo_ai_adjustment(
                conn, event_id="ADJUST-HARD-CRASH-TEXT", user_id="U_MEAL", token=token,
                expected_version=2, correction="飯只吃一半",
                estimate_provider=lambda *_args: (_ for _ in ()).throw(SystemExit("hard crash")),
            )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda _ref: True)

    server.cleanup_nutrition_images()

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """SELECT status,observed_payload_json,estimate_json,source_image_ref
               FROM pending_meal_photo_drafts WHERE token=?""", (token,),
        ).fetchone()
        event = json.loads(conn.execute(
            "SELECT result_json FROM meal_photo_events WHERE event_id='ADJUST-HARD-CRASH-TEXT'"
        ).fetchone()[0])
    assert row == ("expired", "{}", "{}", "")
    assert event["state"] == "processing"


def test_late_adjustment_completion_after_cleanup_cannot_revive_expired_draft(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-adjusting-late-completion.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda _ref: True)
    with sqlite3.connect(db) as conn:
        token, _ref = _stage_adjustment_cleanup_draft(conn, message_id="ADJUST-LATE-CLEANUP")

        def cleanup_then_return(*_args):
            with sqlite3.connect(db) as other:
                other.execute(
                    "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
                    (token,),
                )
                other.commit()
            server.cleanup_nutrition_images()
            revised = meal_photo_payload()
            revised["ai_estimate"] = {
                "items": [{"name": "錯誤復活", "portion": "1份", "calories_kcal": 999, "protein_g": 99}],
                "calories_kcal": {"estimate": 999, "min": 900, "max": 1000},
                "protein_g": {"estimate": 99, "min": 90, "max": 100},
                "confidence": 0.9,
                "provenance": {
                    "provider": "openai", "model": "gpt-4o",
                    "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo",
                },
            }
            return revised

        with pytest.raises(ValueError, match="取消|更新"):
            server.apply_meal_photo_ai_adjustment(
                conn, event_id="ADJUST-LATE-CLEANUP-TEXT", user_id="U_MEAL", token=token,
                expected_version=2, correction="飯只吃一半", estimate_provider=cleanup_then_return,
            )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """SELECT status,observed_payload_json,answers_json,estimate_json,source_image_ref,version
               FROM pending_meal_photo_drafts WHERE token=?""", (token,),
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone() == (0,)
    assert row == ("expired", "{}", "{}", "{}", "", 2)


def test_unexpired_adjustment_is_not_cleaned_and_expired_io_failure_keeps_retry_ref(
    tmp_path, monkeypatch,
):
    db = tmp_path / "meal-photo-adjustment-cleanup-boundaries.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    with sqlite3.connect(db) as conn:
        token, ref = _stage_adjustment_cleanup_draft(conn, message_id="ADJUST-BOUNDARY")
    deleted = []
    monkeypatch.setattr(server, "_delete_nutrition_image", lambda image_ref: deleted.append(image_ref) or False)

    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        before_expiry = conn.execute(
            "SELECT status,observed_payload_json,estimate_json,source_image_ref FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()
    assert before_expiry[0] == "awaiting_adjustment"
    assert before_expiry[1] != "{}" and before_expiry[2] != "{}" and before_expiry[3] == ref
    assert deleted == []

    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        after_failure = conn.execute(
            "SELECT status,observed_payload_json,estimate_json,source_image_ref FROM pending_meal_photo_drafts WHERE token=?",
            (token,),
        ).fetchone()
    assert after_failure == ("expired", "{}", "{}", ref)
    assert deleted == [ref]

    monkeypatch.setattr(server, "_delete_nutrition_image", lambda image_ref: deleted.append(image_ref) or True)
    server.cleanup_nutrition_images()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT source_image_ref FROM pending_meal_photo_drafts WHERE token=?", (token,),
        ).fetchone() == ("",)
    assert deleted == [ref, ref]


def test_cleanup_removes_old_meal_photo_event_before_parent_tombstone(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-retention.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U_MEAL", source_message_id="M_OLD",
            payload=meal_photo_payload(),
        )
        apply_meal_photo_action(
            conn, event_id="OLD-CANCEL", user_id="U_MEAL", token=token,
            expected_version=1, action="cancel",
        )
        conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET retired_at='2000-01-01T00:00:00+08:00',source_image_ref=''
               WHERE token=?""", (token,)
        )
        conn.execute(
            "UPDATE meal_photo_events SET created_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (token,),
        )
        conn.commit()

    server.cleanup_nutrition_images()

    with sqlite3.connect(db) as conn:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM meal_photo_events WHERE token=?", (token,)
        ).fetchone()[0]
        draft_count = conn.execute(
            "SELECT COUNT(*) FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0]
    assert event_count == 0
    assert draft_count == 0


def test_register_daily_health_jobs_uses_retry_minutes_and_single_instance():
    jobs = []

    class FakeScheduler:
        def add_job(self, function, trigger, **kwargs):
            jobs.append((function, trigger, kwargs))

    server.register_daily_health_jobs(FakeScheduler())

    assert jobs == [
        (
            server.send_jason_health_checkin_prompt,
            "cron",
            {"hour": 22, "minute": "30,40,50", "max_instances": 1, "coalesce": True},
        ),
        (
            server.send_jason_daily_health_report,
            "cron",
            {"hour": 23, "minute": "30,40,50", "max_instances": 1, "coalesce": True},
        ),
    ]


def test_line_text_handler_saves_checkin_and_supports_manual_report(tmp_path, monkeypatch):
    db = tmp_path / "health-handler.db"
    with sqlite3.connect(db) as conn:
        ensure_daily_health_schema(conn)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_JASON")
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "U_OTHER")
    replies = []
    monkeypatch.setattr(
        server, "line_bot_api",
        SimpleNamespace(reply_message=lambda _, message: replies.append(message.text)),
    )
    text = "健康回報｜體重70.2｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好"
    checkin_event = _text_event("health-checkin", text, user_id="U_JASON")
    server.processed_messages.discard("health-checkin")

    server._handle_message_impl(checkin_event)

    assert "已更新" in replies[-1]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT water_ml FROM daily_health_checkins").fetchone()[0] == 2500

    monkeypatch.setattr(
        server, "build_jason_daily_health_report", lambda _uid, _date: "MANUAL REPORT"
    )
    report_event = _text_event("health-report", "今日健康日報", user_id="U_JASON")
    server.processed_messages.discard("health-report")
    server._handle_message_impl(report_event)
    assert replies[-1] == "MANUAL REPORT"


def test_search_category_button_lists_single_items_instead_of_searching_literal_label(
    tmp_path, monkeypatch,
):
    db = tmp_path / "search-menu-category.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [
            {
                "name": "雞肉", "category": "side", "calories_kcal": 85,
                "protein_g": 18, "fat_g": 2, "carbohydrate_g": 0,
            },
            {
                "name": "冷泡茶", "category": "drink", "calories_kcal": 2,
                "protein_g": 0, "fat_g": 0, "carbohydrate_g": 0,
            },
            {
                "name": "雞肉便當", "category": "main", "calories_kcal": 484,
                "protein_g": 35, "fat_g": 19, "carbohydrate_g": 17,
            },
        ],
    )
    server.sync_menu_to_food_catalog()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-MENU", "搜尋", user_id="U1")
    )
    menu_payload = json.loads(replies[-1].as_json_string())
    single_action = next(
        item["action"]["text"]
        for item in menu_payload["contents"]["body"]["contents"]
        if item.get("type") == "box"
        for item in item.get("contents", [])
        if item.get("action", {}).get("label") == "🔍 單品"
    )
    assert single_action == "搜尋 單品"

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-SIDE", single_action, user_id="U1")
    )
    assert replies[-1].type == "flex"
    result_payload = json.dumps(
        json.loads(replies[-1].as_json_string()), ensure_ascii=False
    )
    assert "雞肉" in result_payload
    assert "冷泡茶" not in result_payload
    assert "雞肉便當" not in result_payload
    assert "找不到「單品」" not in result_payload


def test_search_drink_category_excludes_single_items_and_main_dishes(tmp_path, monkeypatch):
    db = tmp_path / "search-drink-category.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [
            {
                "name": "豆腐", "category": "side", "calories_kcal": 137,
                "protein_g": 15, "fat_g": 6, "carbohydrate_g": 9,
            },
            {
                "name": "燕麥豆漿", "category": "drink", "calories_kcal": 287,
                "protein_g": 11.3, "fat_g": 5, "carbohydrate_g": 49,
            },
            {
                "name": "豆腐便當", "category": "main", "calories_kcal": 536,
                "protein_g": 32, "fat_g": 23, "carbohydrate_g": 26,
            },
        ],
    )
    server.sync_menu_to_food_catalog()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-DRINK", "搜尋 飲品", user_id="U1")
    )
    assert replies[-1].type == "flex"
    result_payload = json.dumps(
        json.loads(replies[-1].as_json_string()), ensure_ascii=False
    )
    assert "燕麥豆漿" in result_payload
    assert "豆腐便當" not in result_payload
    assert '"text": "豆腐"' not in result_payload


def test_search_single_item_category_paginates_without_losing_category(
    tmp_path, monkeypatch,
):
    db = tmp_path / "search-single-category-pages.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    side_names = [f"配菜{index:02d}" for index in range(1, 14)]
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [
            {
                "name": name, "category": "side", "calories_kcal": 50 + index,
                "protein_g": 5, "fat_g": 1, "carbohydrate_g": 5,
            }
            for index, name in enumerate(side_names, start=1)
        ],
    )
    server.sync_menu_to_food_catalog()
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message)
    )

    server._handle_message_impl(
        _text_event("SEARCH-CATEGORY-SIDE-P1", "搜尋 單品", user_id="U1")
    )
    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    assert len(page1) == 12
    next_action = page1[-1]["body"]["contents"][0]["action"]
    assert next_action["text"] == "搜尋下一頁 2 單品"
    page1_text = json.dumps(page1[:-1], ensure_ascii=False)

    server._handle_message_impl(
        _text_event(
            "SEARCH-CATEGORY-SIDE-P2", next_action["text"], user_id="U1"
        )
    )
    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    assert len(page2) == 2
    page2_text = json.dumps(page2, ensure_ascii=False)
    assert all((name in page1_text) != (name in page2_text) for name in side_names)


def test_menu_category_sync_never_publicizes_same_named_private_label(
    tmp_path, monkeypatch,
):
    db = tmp_path / "menu-category-private-collision.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,source_type,owner_user_id,visibility,
                per_serving_json,per_100_json,exchange_json,exchange_review_status,
                fingerprint,verification_status,created_at,updated_at)
               VALUES ('food_private_chicken','雞肉','label','U_PRIVATE','private',
                       '{}','{}','{}','approved','private-chicken','user_confirmed',?,?)""",
            (now, now),
        )
    monkeypatch.setattr(
        server,
        "MAIN_DISHES",
        [{
            "name": "雞肉", "category": "side", "calories_kcal": 85,
            "protein_g": 18, "fat_g": 2, "carbohydrate_g": 0,
        }],
    )

    server.sync_menu_to_food_catalog()

    with sqlite3.connect(db) as conn:
        private_row = conn.execute(
            """SELECT visibility,menu_category FROM food_catalog
               WHERE food_id='food_private_chicken'"""
        ).fetchone()
        official_row = conn.execute(
            """SELECT visibility,menu_category FROM food_catalog
               WHERE product_name='雞肉' AND owner_user_id='system'"""
        ).fetchone()
    assert private_row == ("private", "")
    assert official_row == ("public", "side")


def test_natural_food_log_not_found_or_incompatible_unit_requires_ai_confirmation(
    tmp_path, monkeypatch,
):
    db = tmp_path / "natural-food-safe-fallback.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,
                per_100_json,exchange_json,exchange_review_status,fingerprint,
                verification_status,created_at,updated_at)
               VALUES ('food_solid_bean','豆干','user_private_food','U1','private',
                       100,'g',1,'{"calories_kcal":150,"protein_g":15}',
                       '{}','{}','approved','solid-bean','user_confirmed',?,?)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,
                expiry_date,daily_chat_limit)
               VALUES ('U1',2,10,?,'vip','2099-12-31',2)""",
            (server.tw_today().isoformat(),),
        )
        conn.commit()
    replies = []
    estimates = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("general AI must not run")),
    )
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda request: estimates.append(dict(request)) or {
            "food_name": request["food_name"], "portion_assumption": "依描述份量",
            "calories_kcal": {"estimate": 350, "min": 300, "max": 420},
            "protein_g": {"estimate": 17, "min": 13, "max": 22},
            "provenance": {"provider": "test", "model": "mock", "method": "text_meal_estimate"},
        },
    )
    server.processed_messages.clear()

    server._handle_message_impl(
        _text_event(
            "NATURAL-MISSING-1", "我要記錄飲食 早餐 火星果汁 300ml", user_id="U1"
        )
    )
    first = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "AI 營養估算（尚未記錄）" in first
    assert "火星果汁" in first and "300–420 kcal" in first
    assert "確認後才會寫入" in first

    server._handle_message_impl(
        _text_event("NATURAL-UNIT-1", "我要記錄飲食 豆干 300ml", user_id="U1")
    )
    second = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "AI 營養估算（尚未記錄）" in second
    assert "豆干" in second and "確認後才會寫入" in second
    assert [item["food_name"] for item in estimates] == ["火星果汁", "豆干"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_text_meal_estimates WHERE user_id='U1' AND status='pending'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U1'"
        ).fetchone()[0] == 0


def test_quick_log_uses_one_timestamp_across_midnight(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "natural-midnight.db")
    refresh_users = []
    monkeypatch.setattr(
        server,
        "_refresh_health_check_after_food_log",
        lambda _conn, *, user_id: refresh_users.append(user_id),
    )
    logged_at = datetime(2026, 8, 10, 23, 59, 59, tzinfo=server.TW_TZ)
    monkeypatch.setattr(server, "tw_now", lambda: logged_at)
    monkeypatch.setattr(
        server, "tw_today", lambda: datetime(2026, 8, 11).date()
    )
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,
                per_100_json,exchange_json,exchange_review_status,fingerprint,
                verification_status,created_at,updated_at)
               VALUES ('food_midnight','午夜豆漿','label','U1','private',
                       400,'ml',1,'{"calories_kcal":200,"protein_g":10}',
                       '{}','{}','approved','midnight-food','user_confirmed',?,?)""",
            (now, now),
        )
        conn.commit()
    server._quick_log_catalog_card_once(
        user_id="U1", food_id="food_midnight", amount=400, amount_unit="ml",
        meal_slot="點心", event_ref="MIDNIGHT-1", display_quantity="400ml",
    )
    with sqlite3.connect(db) as conn:
        consumed_at = conn.execute(
            "SELECT consumed_at FROM food_logs WHERE user_id='U1'"
        ).fetchone()[0]
        profile = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_date FROM health_profile WHERE user_id='U1'"
        ).fetchone()
    assert consumed_at.startswith("2026-08-10T23:59:59")
    assert profile[:2] == pytest.approx((200.0, 10.0))
    assert profile[2] == "2026-08-10"
    assert refresh_users == ["U1"]


def test_natural_exact_filters_unit_before_private_priority(tmp_path, monkeypatch):
    db = _daily_ledger_db(tmp_path, monkeypatch, "natural-unit-priority.db")
    now = utcish_now()
    rows = [
        ("food_private_same", "同名飲品", "U1", "private", 100, "g", "private-same"),
        ("food_public_same", "同 名飲品", "system", "public", 400, "ml", "public-same"),
    ]
    with sqlite3.connect(db) as conn:
        for food_id, product_name, owner, visibility, package_amount, package_unit, fingerprint in rows:
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,
                    per_100_json,exchange_json,exchange_review_status,fingerprint,
                    verification_status,created_at,updated_at)
                   VALUES (?,?,'label',?,?,?, ?,1,
                           '{"calories_kcal":200,"protein_g":10}',
                           '{}','{}','approved',?,'user_confirmed',?,?)""",
                (
                    food_id, product_name, owner, visibility, package_amount, package_unit,
                    fingerprint, now, now,
                ),
            )
        for index in range(20):
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,
                    per_100_json,exchange_json,exchange_review_status,fingerprint,
                    verification_status,created_at,updated_at)
                   VALUES (?,?,'label','system','public',400,'ml',1,
                           '{"calories_kcal":100,"protein_g":5}',
                           '{}','{}','approved',?,'user_confirmed',?,?)""",
                (
                    f"food_fuzzy_{index:02d}", f"品牌{index:02d}同名飲品",
                    f"fuzzy-{index:02d}", now, f"9999-12-31T23:59:{index:02d}",
                ),
            )
        conn.commit()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    server.processed_messages.clear()
    server._handle_message_impl(
        _text_event("NATURAL-PUBLIC-UNIT-1", "幫我記同名飲品400ml", user_id="U1")
    )
    assert replies[-1].type == "flex"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT food_id,consumed_servings FROM food_logs WHERE user_id='U1'"
        ).fetchone()
    assert row[0] == "food_public_same"
    assert row[1] == pytest.approx(1.0)
    with sqlite3.connect(db) as conn:
        assert server.search_food_catalog(conn, user_id="U1", query="%", limit=20) == []
        assert server.search_food_catalog(conn, user_id="U1", query="_", limit=20) == []


def test_natural_food_log_ambiguous_choice_preserves_amount_and_logs_selected_food(
    tmp_path, monkeypatch,
):
    db = tmp_path / "natural-food-ambiguous.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    fixed_now = server.datetime(2026, 8, 10, 18, 45, tzinfo=server.TW_TZ)
    monkeypatch.setattr(server, "tw_now", lambda: fixed_now)
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """INSERT OR REPLACE INTO health_profile
               (user_id,name,today_extra_cal,today_extra_pro,today_food_items,
                today_date,tdee,protein,sheet_name)
               VALUES ('U1','',0,0,'','',2000,100,'')"""
        )
        for food_id, name, calories in (
            ("food_soy_a", "光泉無糖豆漿", 190.2),
            ("food_soy_b", "義美無糖豆漿", 180.0),
        ):
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,
                    per_100_json,exchange_json,exchange_review_status,fingerprint,
                    verification_status,created_at,updated_at)
                   VALUES (?,?,'user_private_food','U1','private',375,'ml',1,?,
                           '{}','{}','approved',?,'user_confirmed',?,?)""",
                (
                    food_id, name,
                    json.dumps({"calories_kcal": calories, "protein_g": 9}),
                    food_id, now, now,
                ),
            )
        conn.commit()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not call AI")),
    )
    server.processed_messages.clear()

    server._handle_message_impl(
        _text_event("NATURAL-AMBIG-1", "我要記錄飲食 無糖豆漿 400cc", user_id="U1")
    )

    payload = json.loads(replies[-1].as_json_string())
    rendered = json.dumps(payload, ensure_ascii=False)
    assert replies[-1].type == "flex"
    assert "光泉無糖豆漿" in rendered and "義美無糖豆漿" in rendered
    expected_data = "nlfood:v1:food_soy_a:amount:400:ml:meal:晚餐"
    assert expected_data in rendered
    tampered = SimpleNamespace(
        postback=SimpleNamespace(data=expected_data), source=SimpleNamespace(user_id="U2"),
        reply_token="reply-natural-tampered", webhook_event_id="NATURAL-TAMPER-1",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(tampered)
    assert "找不到可使用的食品資料" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0

    selected = SimpleNamespace(
        postback=SimpleNamespace(data=expected_data), source=SimpleNamespace(user_id="U1"),
        reply_token="reply-natural-choice", webhook_event_id="NATURAL-CHOICE-1",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(selected)
    success = json.dumps(json.loads(replies[-1].as_json_string()), ensure_ascii=False)
    assert "今日總覽" in success
    assert "光泉無糖豆漿" in success
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT food_id,consumed_amount,consumed_unit FROM food_logs WHERE user_id='U1'"
        ).fetchone()
    assert row == pytest.approx(("food_soy_a", 400.0, "ml"))


def test_natural_food_log_uses_private_exact_match_scales_ml_and_replays_once(
    tmp_path, monkeypatch,
):
    db = tmp_path / "natural-food-log.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    fixed_now = server.datetime(2026, 8, 10, 18, 30, tzinfo=server.TW_TZ)
    monkeypatch.setattr(server, "tw_now", lambda: fixed_now)
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """INSERT OR REPLACE INTO health_profile
               (user_id,name,today_extra_cal,today_extra_pro,today_food_items,
                today_date,tdee,protein,sheet_name)
               VALUES ('U1','',0,0,'','',2000,100,'')"""
        )
        foods = (
            ("food_private_soy", "U1", "private", 375, 190.2, 9.1, "private-soy"),
            ("food_public_soy", "system", "public", 400, 100, 1, "public-soy"),
        )
        for food_id, owner, visibility, package_amount, calories, protein, fingerprint in foods:
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,
                    per_100_json,exchange_json,exchange_review_status,fingerprint,
                    verification_status,created_at,updated_at)
                   VALUES (?,'無糖豆漿','光泉','user_private_food',?,?,?,'ml',1,?,
                           '{}','{}','approved',?,'user_confirmed',?,?)""",
                (
                    food_id, owner, visibility, package_amount,
                    json.dumps({"calories_kcal": calories, "protein_g": protein}),
                    fingerprint, now, now,
                ),
            )
        conn.commit()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    monkeypatch.setattr(
        server, "get_ai_response_with_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("natural log must not call AI")),
    )
    server.processed_messages.clear()
    no_amount_event = _text_event(
        "NATURAL-SOY-NO-AMOUNT", "我要紀錄飲食 無糖豆漿", user_id="U1"
    )
    server._handle_message_impl(no_amount_event)
    assert replies[-1].type == "text"
    assert "請選擇份量" in replies[-1].text
    assert any(
        item.action.data == "nlfood:v1:food_private_soy:servings:1:meal:晚餐"
        for item in replies[-1].quick_reply.items
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,
                per_100_json,exchange_json,exchange_review_status,fingerprint,
                verification_status,created_at,updated_at)
               VALUES ('ledger_legacy_soy','無糖豆漿','ai_text_estimate','U1','private',
                       1,'份',1,'{"calories_kcal":190,"protein_g":9}',
                       '{}','{}','approved','legacy-soy','user_confirmed',?,?)""",
            (now, now),
        )
        conn.commit()

    event = _text_event("NATURAL-SOY-1", "我要紀錄飲食 無糖豆漿 400cc", user_id="U1")

    server._handle_message_impl(event)

    assert replies[-1].type == "flex"
    card_text = json.dumps(json.loads(replies[-1].as_json_string()), ensure_ascii=False)
    assert "今日總覽" in card_text
    assert "無糖豆漿" in card_text
    assert '"text": "熱量餘額"' in card_text and '"text": "1,797"' in card_text
    assert '"text": "已吃 203"' in card_text
    assert '"text": "蛋白質餘額"' in card_text and '"text": "90.3 g"' in card_text
    server.processed_messages.discard(event.message.id)
    server._handle_message_impl(event)
    with sqlite3.connect(db) as conn:
        logs = conn.execute(
            """SELECT food_id,consumed_servings,consumed_amount,consumed_unit,meal_slot,
                      consumed_at,nutrition_snapshot_json FROM food_logs WHERE user_id='U1'"""
        ).fetchall()
        hp = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,today_date FROM health_profile WHERE user_id='U1'"
        ).fetchone()
        outbox_count = conn.execute(
            """SELECT COUNT(*) FROM nutrition_sheet_outbox
               WHERE entity_type='food_log' AND status='pending'"""
        ).fetchone()[0]
    assert len(logs) == 1
    assert logs[0][0] == "food_private_soy"
    assert logs[0][1] == pytest.approx(400 / 375)
    assert logs[0][2] == pytest.approx(400)
    assert logs[0][3:6] == ("ml", "晚餐", "2026-08-10T18:30:00+08:00")
    nutrition = json.loads(logs[0][6])
    assert nutrition["calories_kcal"] == pytest.approx(202.88)
    assert nutrition["protein_g"] == pytest.approx(9.7067)
    assert hp[0] == pytest.approx(202.88)
    assert hp[1] == pytest.approx(9.7067)
    assert hp[2] == "2026-08-10"
    assert outbox_count == 1


def test_search_and_quick_relog_creates_food_log(tmp_path, monkeypatch):
    db = tmp_path / "search-relog.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "ADMIN_UID", "U_ADMIN")
    fixed_now = server.datetime(2026, 8, 10, 0, 5, tzinfo=server.TW_TZ)
    monkeypatch.setattr(server, "tw_now", lambda: fixed_now)
    now = utcish_now()
    server.init_db()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        fid = new_id("food")
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                exchange_json,exchange_review_status,fingerprint,original_image_ref,
                recognition_confidence,verification_status,created_at,updated_at)
               VALUES (?,'舒肥雞胸','好市多','','user_private_food','U1','private',
                       100,'g',1,'{"calories_kcal":120,"protein_g":25}','{}',
                       '{"starch_exchange":0,"protein_medium_exchange":1}',
                       'approved','fp_chicken','',0,'user_confirmed',?,?)""",
            (fid, now, now),
        )
        conn.execute(
            """INSERT OR REPLACE INTO health_profile
               (user_id,name,today_extra_cal,today_extra_pro,today_food_items,
                today_date,tdee,protein,sheet_name)
               VALUES ('U1','',0,0,'','',2000,100,'')"""
        )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    search_event = _text_event("SEARCH-1", "搜尋 雞胸", user_id="U1")
    server._handle_message_impl(search_event)
    assert len(replies) == 1
    carousel = replies[0]
    assert carousel.type == "flex"
    # contents is a CarouselContainer; check structure via serialization
    raw = json.loads(carousel.as_json_string())
    bubble_inner = json.dumps(raw, ensure_ascii=False)
    assert "舒肥雞胸" in bubble_inner
    assert f"relog:v1:{fid}:start" in bubble_inner

    start_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"relog:v1:{fid}:start"),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply-relog-start", webhook_event_id="RELOG-START",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(start_event)
    assert "請選擇份量" in replies[-1].text
    assert any(f"relog:v1:{fid}:servings:1" in item.action.data for item in replies[-1].quick_reply.items)

    sv_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"relog:v1:{fid}:servings:1.5"),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply-relog-sv", webhook_event_id="RELOG-SV",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(sv_event)
    assert "1.5" in replies[-1].text and "餐別" in replies[-1].text

    meal_event = SimpleNamespace(
        postback=SimpleNamespace(data=f"relog:v1:{fid}:sv:1.5:meal:午餐"),
        source=SimpleNamespace(user_id="U1"),
        reply_token="reply-relog-meal", webhook_event_id="RELOG-MEAL",
        timestamp=1784740620000,
    )
    server.handle_meal_photo_postback(meal_event)
    assert replies[-1].type == "flex"
    payload = json.loads(replies[-1].as_json_string())
    card_text = json.dumps(payload, ensure_ascii=False)
    assert "今日總覽" in card_text
    assert "舒肥雞胸" in card_text
    assert '"text": "熱量餘額"' in card_text and '"text": "1,820"' in card_text
    assert '"text": "已吃 180"' in card_text
    assert '"text": "蛋白質餘額"' in card_text and '"text": "62.5 g"' in card_text
    assert "記錄成功" not in card_text
    server.handle_meal_photo_postback(meal_event)
    assert replies[-1].type == "flex"
    replay_text = json.dumps(json.loads(replies[-1].as_json_string()), ensure_ascii=False)
    assert replay_text == card_text
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT consumed_servings,meal_slot,consumed_at FROM food_logs WHERE user_id='U1'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == 1.5
        assert row[1] == "午餐"
        assert row[2] == "2026-08-10T00:05:00+08:00"


def test_search_my_food_natural_language_alias_returns_private_library(tmp_path, monkeypatch):
    db = tmp_path / "search-my-alias.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for owner, name in (("U1", "Jason私人雞胸"), ("U2", "別人的私人雞胸")):
            fid = new_id("food")
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food',?,'private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, name, owner, json.dumps({"calories_kcal": 123}), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(
        _text_event("SEARCH-MY-ALIAS-1", "搜尋 我的食物", user_id="U1")
    )

    assert len(replies) == 1
    assert replies[0].type == "flex"
    payload = json.loads(replies[0].as_json_string())
    text = json.dumps(payload, ensure_ascii=False)
    assert "Jason私人雞胸" in text
    assert "別人的私人雞胸" not in text


def test_my_food_search_paginates_with_context_and_within_line_carousel_limit(tmp_path, monkeypatch):
    db = tmp_path / "search-my-pagination.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for index in range(13):
            fid = new_id("food")
            timestamp = f"2026-07-{index + 1:02d}T08:00:00+08:00"
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, f"我的食物{index:02d}", json.dumps({"calories_kcal": 100 + index}), fid, timestamp, timestamp),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("SEARCH-MY-P1", "搜尋 _my", user_id="U1"))

    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    assert len(page1) == 12
    assert {bubble.get("size") for bubble in page1} == {"kilo"}
    next_action = page1[-1]["body"]["contents"][0]["action"]
    assert next_action["text"] == "搜尋下一頁 2 _my"

    server._handle_message_impl(
        _text_event("SEARCH-MY-P2", next_action["text"], user_id="U1")
    )

    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    page2_text = json.dumps(page2, ensure_ascii=False)
    assert len(page2) == 2
    assert "我的食物01" in page2_text
    assert "我的食物00" in page2_text


def test_keyword_food_search_second_page_does_not_repeat_first_page(tmp_path, monkeypatch):
    db = tmp_path / "search-keyword-pagination.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for index in range(13):
            fid = new_id("food")
            timestamp = f"2026-07-{index + 1:02d}T08:00:00+08:00"
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, f"雞胸餐{index:02d}", json.dumps({"calories_kcal": 100 + index}), fid, timestamp, timestamp),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("SEARCH-CHICKEN-P1", "搜尋 雞胸", user_id="U1"))
    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    next_action = page1[-1]["body"]["contents"][0]["action"]
    assert len(page1) == 12
    assert next_action["text"] == "搜尋下一頁 2 雞胸"

    server._handle_message_impl(
        _text_event("SEARCH-CHICKEN-P2", next_action["text"], user_id="U1")
    )
    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    page2_text = json.dumps(page2, ensure_ascii=False)
    assert len(page2) == 2
    assert "雞胸餐01" in page2_text
    assert "雞胸餐00" in page2_text
    assert "雞胸餐12" not in page2_text


def test_keyword_food_search_reaches_items_after_twentieth_result(tmp_path, monkeypatch):
    db = tmp_path / "search-keyword-page-three.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for index in range(30):
            fid = new_id("food")
            timestamp = f"2026-07-30T00:{index:02d}:00+08:00"
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, f"雞胸大量{index:02d}", json.dumps({"calories_kcal": 100 + index}), fid, timestamp, timestamp),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("SEARCH-30-P1", "搜尋 雞胸大量", user_id="U1"))
    page1 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    next2 = page1[-1]["body"]["contents"][0]["action"]["text"]
    server._handle_message_impl(_text_event("SEARCH-30-P2", next2, user_id="U1"))
    page2 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    next3 = page2[-1]["body"]["contents"][0]["action"]["text"]
    server._handle_message_impl(_text_event("SEARCH-30-P3", next3, user_id="U1"))

    page3 = json.loads(replies[-1].as_json_string())["contents"]["contents"]
    page3_text = json.dumps(page3, ensure_ascii=False)
    assert len(page3) == 8
    assert "雞胸大量07" in page3_text
    assert "雞胸大量00" in page3_text


def test_search_rejects_pathologically_large_page_number(tmp_path, monkeypatch):
    db = tmp_path / "search-invalid-page.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(
        _text_event("SEARCH-HUGE-PAGE", "搜尋下一頁 " + ("9" * 5000) + " _my", user_id="U1")
    )

    assert "頁碼" in replies[-1].text


def test_search_no_results_shows_guidance(tmp_path, monkeypatch):
    db = tmp_path / "search-empty.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))
    event = _text_event("SEARCH-EMPTY", "搜尋 不存在的食物", user_id="U1")
    server._handle_message_impl(event)
    assert "找不到" in replies[0].text


def test_breakfast_combo_logs_multiple_foods_at_once(tmp_path, monkeypatch):
    db = tmp_path / "combo.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,sheet_name)
               VALUES ('U1','',2000,100,'')"""
        )
        conn.commit()
    refresh_users = []
    monkeypatch.setattr(
        server,
        "_refresh_health_check_after_food_log",
        lambda _conn, *, user_id: refresh_users.append(user_id),
    )
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, exch, kcal in [
            ("穀麥高粱 OATS & HONEY", {"starch_exchange": 1.98}, 197),
            ("草莓穀物脆片", {"starch_exchange": 2.37}, 223),
            ("無糖優格", {"milk_exchange": 0.5}, 62),
        ]:
            fid = new_id("food")
            ps = json.dumps({"calories_kcal": kcal})
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',
                           100,'g',1,?, '{}',
                           ?,'approved',?,'',0,'user_confirmed',?,?)""",
                (fid, name, ps, json.dumps(exch), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    event = _text_event("COMBO-1", "早餐1", user_id="U1")
    server._handle_message_impl(event)
    assert len(replies) == 1
    reply = replies[0]
    assert reply.type == "flex"
    raw = json.loads(reply.as_json_string())
    bubble_text = json.dumps(raw, ensure_ascii=False)
    assert "今日總覽" in bubble_text
    assert "穀麥高粱" in bubble_text
    assert "無糖優格" in bubble_text

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT meal_slot FROM food_logs WHERE user_id='U1'"
        ).fetchall()
        assert len(rows) == 3
        assert all(r[0] == "早餐" for r in rows)
    assert refresh_users == ["U1"]


def _confirm_dashboard_ai_photo(
    conn, *, user_id="U-AI-DASHBOARD", source_message_id="AI-DASHBOARD-1",
    consumed_at, calories=680, protein=35,
):
    payload = ai_estimated_payload()
    payload["ai_estimate"]["calories_kcal"] = {
        "estimate": calories, "min": max(0, calories - 100), "max": calories + 120,
    }
    payload["ai_estimate"]["protein_g"] = {
        "estimate": protein, "min": max(0, protein - 6), "max": protein + 8,
    }
    token = save_meal_photo_draft(
        conn, user_id=user_id, source_message_id=source_message_id,
        payload=payload, source_image_ref=f"nutrition-image:{source_message_id}.jpg",
        meal_slot="午餐", consumed_at=consumed_at,
        workflow_version="user_confirmed_ai_nutrition_v2",
    )
    draft = get_meal_photo_draft(conn, user_id=user_id, token=token)
    confirmed = apply_meal_photo_action(
        conn, event_id=f"CONFIRM-{source_message_id}", user_id=user_id, token=token,
        expected_version=draft["version"], action="confirm_estimate",
    )
    return confirmed["result"]["log_id"]


def _dashboard_revision_estimate(conn, log_id, *, calories, protein):
    estimate = json.loads(conn.execute(
        "SELECT exchange_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
    ).fetchone()[0])
    estimate["calories_kcal"] = calories
    estimate["protein_g"] = protein
    estimate["calories_kcal_range"] = {
        "min": max(0, calories - 60), "max": calories + 70,
        "basis": "ai_vision_estimate_range_v1",
    }
    estimate["protein_g_range"] = {
        "min": max(0, protein - 4), "max": protein + 5,
        "basis": "ai_vision_estimate_range_v1",
    }
    estimate["estimate_items"] = [{
        "name": "儀表板修正版餐點", "portion": "測試修正",
        "calories_kcal": calories, "protein_g": protein,
    }]
    return estimate


def _dashboard_flex_text_nodes(message):
    payload = message.as_json_dict()
    assert payload["contents"]["type"] == "bubble"
    nodes = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "text":
                nodes.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return payload, nodes


def test_dashboard_counts_verified_ai_photo_and_labels_estimate_without_profile(tmp_path, monkeypatch):
    db_dir = tmp_path / "confirmed-ai-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    yesterday = (server.tw_today() - timedelta(days=1)).isoformat()

    with sqlite3.connect(db) as conn:
        # Preview is not a canonical meal and must not make a profile/dashboard appear.
        save_meal_photo_draft(
            conn, user_id="U-PREVIEW", source_message_id="AI-PREVIEW",
            payload=ai_estimated_payload(), source_image_ref="nutrition-image:preview.jpg",
            meal_slot="午餐", consumed_at=f"{today}T11:30:00+08:00",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        _confirm_dashboard_ai_photo(
            conn, consumed_at=f"{today}T12:00:00+08:00",
        )
        _confirm_dashboard_ai_photo(
            conn, user_id="U-AI-DASHBOARD", source_message_id="AI-DASHBOARD-OLD",
            consumed_at=f"{yesterday}T12:00:00+08:00", calories=900, protein=60,
        )
        _confirm_dashboard_ai_photo(
            conn, user_id="U-OTHER", source_message_id="AI-DASHBOARD-OTHER",
            consumed_at=f"{today}T12:30:00+08:00", calories=800, protein=50,
        )
        assert conn.execute(
            "SELECT 1 FROM health_profile WHERE user_id='U-AI-DASHBOARD'"
        ).fetchone() is None

    assert server.get_dashboard_data("U-PREVIEW") is None
    dashboard = server.get_dashboard_data("U-AI-DASHBOARD")
    replayed = server.get_dashboard_data("U-AI-DASHBOARD")

    assert dashboard["extra_cal"] == 680
    assert dashboard["extra_pro"] == 35
    assert dashboard["food_list"] == ["餐點照片：雞腿便當、白飯"]
    assert dashboard["recorded_count"] == 1
    assert dashboard["ai_estimated_cal"] == 680
    assert dashboard["ai_estimated_pro"] == 35
    assert dashboard["ai_estimated_count"] == 1
    assert replayed["extra_cal"] == 680
    assert replayed["recorded_count"] == 1

    payload, text_nodes = _dashboard_flex_text_nodes(
        server.build_dashboard_flex("U-AI-DASHBOARD")
    )
    texts = [node["text"] for node in text_nodes]
    assert payload["contents"]["body"]["contents"]
    assert "今日已吃" in texts and "680" in texts and "已吃 680" in texts
    assert "今日蛋白質" in texts and "35 g" in texts
    assert "含 AI 估算紀錄，非營養師審核結果" in texts
    assert texts.count("含 AI 估算紀錄，非營養師審核結果") == 1
    estimate_notice = next(node for node in text_nodes if node["text"] == "含 AI 估算紀錄，非營養師審核結果")
    assert estimate_notice.get("wrap") is True
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "今日已記錄（含 AI 照片估算）" not in rendered
    assert "AI 照片估算小計" not in rendered
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM health_profile WHERE user_id='U-AI-DASHBOARD'"
        ).fetchone() is None


def test_dashboard_uses_latest_verified_ai_revision_then_removes_with_delete(tmp_path, monkeypatch):
    db_dir = tmp_path / "revised-ai-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        log_id = _confirm_dashboard_ai_photo(
            conn, consumed_at=f"{today}T12:00:00+08:00",
        )
        genesis = server.get_dashboard_data("U-AI-DASHBOARD")
        assert genesis["extra_cal"] == 680
        assert genesis["extra_pro"] == 35
        assert genesis["recorded_count"] == 1
        revision2 = create_meal_photo_revision_draft(
            conn, user_id="U-AI-DASHBOARD", log_id=log_id, from_version=1,
            request_text="白飯只吃一半",
            estimate=_dashboard_revision_estimate(conn, log_id, calories=510, protein=31),
        )
        confirm_user_meal_photo_revision(
            conn, event_id="Z-DASHBOARD-REVISION", user_id="U-AI-DASHBOARD",
            log_id=log_id, from_version=1, draft_token=revision2["token"],
        )
        revision3 = create_meal_photo_revision_draft(
            conn, user_id="U-AI-DASHBOARD", log_id=log_id, from_version=2,
            request_text="再加一顆蛋",
            estimate=_dashboard_revision_estimate(conn, log_id, calories=590, protein=38),
        )
        confirm_user_meal_photo_revision(
            conn, event_id="A-DASHBOARD-REVISION", user_id="U-AI-DASHBOARD",
            log_id=log_id, from_version=2, draft_token=revision3["token"],
        )

    revised = server.get_dashboard_data("U-AI-DASHBOARD")
    assert revised["extra_cal"] == 590
    assert revised["extra_pro"] == 38
    assert revised["recorded_count"] == 1
    assert revised["ai_estimated_cal"] == 590

    server.apply_daily_food_log_edit(
        user_id="U-AI-DASHBOARD", log_id=log_id, expected_version=3,
        event_id="DELETE-AI-DASHBOARD", action="delete",
    )
    assert server.get_dashboard_data("U-AI-DASHBOARD") is None


def test_dashboard_does_not_treat_verified_v1_unknown_nutrition_as_zero(tmp_path, monkeypatch):
    db_dir = tmp_path / "v1-unknown-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U-V1-UNKNOWN", source_message_id="V1-UNKNOWN",
            payload=sample_payload(), source_image_ref="nutrition-image:v1.jpg",
            meal_slot="午餐", consumed_at=f"{today}T12:00:00+08:00",
        )
        estimated = _answer_all(conn, token, unknown=True, user_id="U-V1-UNKNOWN")
        confirmed = apply_meal_photo_action(
            conn, event_id="CONFIRM-V1-UNKNOWN", user_id="U-V1-UNKNOWN", token=token,
            expected_version=estimated["version"], action="confirm_estimate",
        )
        log_id = confirmed["result"]["log_id"]
        assert conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0] == "{}"

    assert server.get_dashboard_data("U-V1-UNKNOWN") is None


def test_dashboard_mixes_ordinary_approved_and_ai_once_and_fails_closed_on_ai_tamper(
    tmp_path, monkeypatch,
):
    db_dir = tmp_path / "mixed-authority-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U-MIXED','混合來源',2000,100,9999,9999,'舊快取不得重加',?)""",
            (today,),
        )
        server.create_daily_food_log(
            conn, user_id="U-MIXED", product_name="正常餐", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 100, "protein_g": 10},
            source_type="official_menu",
        )
        insert_approved_meal_photo_log(
            conn, token="abcdefabc123", user_id="U-MIXED", reviewer="U-MIXED",
            consumed_at=f"{today}T10:00:00+08:00", meal_slot="午餐",
            source_image_ref="mixed-approved.jpg",
            observed_payload={
                "visible_items": [{
                    "name": "核准雞胸", "category": "protein", "confidence": 0.9,
                }],
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0, "protein_low_exchange": 1,
                "protein_medium_exchange": 0, "protein_high_exchange": 0,
                "starch_exchange": 0, "vegetable_exchange": 0,
                "fruit_exchange": 0, "fat_exchange": 0,
            },
        )
        ai_log_id = _confirm_dashboard_ai_photo(
            conn, user_id="U-MIXED", source_message_id="AI-MIXED",
            consumed_at=f"{today}T12:00:00+08:00",
        )

    dashboard = server.get_dashboard_data("U-MIXED")
    assert dashboard["extra_cal"] == 835
    assert dashboard["extra_pro"] == 52
    assert dashboard["recorded_count"] == 3
    assert dashboard["ai_estimated_count"] == 1
    assert dashboard["ai_estimated_cal"] == 680

    _, text_nodes = _dashboard_flex_text_nodes(server.build_dashboard_flex("U-MIXED"))
    texts = [node["text"] for node in text_nodes]
    assert "熱量餘額" in texts and "1,165" in texts and "已吃 835" in texts
    assert "蛋白質餘額" in texts and "48 g" in texts and "/ 100 g" in texts
    assert texts.count("含 AI 估算紀錄，非營養師審核結果") == 1

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json=? WHERE log_id=?",
            (json.dumps({"calories_kcal": 0, "protein_g": 0}), ai_log_id),
        )
        conn.commit()

    failed_closed = server.get_dashboard_data("U-MIXED")
    assert failed_closed["extra_cal"] == 155
    assert failed_closed["extra_pro"] == 17
    assert failed_closed["recorded_count"] == 2
    assert failed_closed["ai_estimated_count"] == 0
    assert "餐點照片：雞腿便當、白飯" not in failed_closed["food_list"]


@pytest.mark.parametrize("tampered_source", ["official_menu", "user_private_food"])
def test_photo_source_type_tamper_cannot_downgrade_failed_trust_into_ordinary_dashboard(
    tmp_path, monkeypatch, tampered_source,
):
    db_dir = tmp_path / f"photo-source-tamper-{tampered_source}"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U-SOURCE-TAMPER','來源竄改',2000,100,680,35,'舊快取',?)""",
            (today,),
        )
        log_id = _confirm_dashboard_ai_photo(
            conn, user_id="U-SOURCE-TAMPER", source_message_id="SOURCE-TAMPER",
            consumed_at=f"{today}T12:00:00+08:00",
        )
        food_id = conn.execute(
            "SELECT food_id FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE food_catalog SET source_type=? WHERE food_id=?",
            (tampered_source, food_id),
        )
        conn.commit()

    ledger = server.get_daily_food_ledger("U-SOURCE-TAMPER", today)
    item = next(item for item in ledger["items"] if item["log_id"] == log_id)
    assert item["trust_integrity_status"] == "integrity_verification_failed"
    assert item["trust_type"] == "untrusted_user_confirmed_ai_estimate"
    assert item["nutrition_authority"] == ""
    assert all(item["nutrition"].get(field) is None for field in server.DAILY_FOOD_NUTRIENT_FIELDS)

    with sqlite3.connect(db) as conn:
        server._sync_health_profile_from_ledger_conn(conn, "U-SOURCE-TAMPER", today)
        conn.commit()
        cached = conn.execute(
            """SELECT today_extra_cal,today_extra_pro,today_food_items
               FROM health_profile WHERE user_id='U-SOURCE-TAMPER'"""
        ).fetchone()
    assert cached == (0.0, 0.0, "")

    dashboard = server.get_dashboard_data("U-SOURCE-TAMPER")
    assert dashboard["extra_cal"] == 0
    assert dashboard["extra_pro"] == 0
    assert dashboard["food_list"] == []
    assert dashboard["recorded_count"] == 0
    assert dashboard["task_logged_once"] is False
    assert dashboard["task_two_meals"] is False
    assert dashboard["ai_estimated_count"] == 0


@pytest.mark.parametrize("tamper_mode", ["latest_revision_source", "missing_revision_evidence"])
def test_revised_photo_fails_closed_when_latest_chain_or_source_evidence_is_missing(
    tmp_path, monkeypatch, tamper_mode,
):
    db_dir = tmp_path / f"revised-photo-{tamper_mode}"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        log_id = _confirm_dashboard_ai_photo(
            conn, user_id="U-REVISED-TAMPER", source_message_id="REVISED-TAMPER",
            consumed_at=f"{today}T12:00:00+08:00",
        )
        revision = create_meal_photo_revision_draft(
            conn, user_id="U-REVISED-TAMPER", log_id=log_id, from_version=1,
            request_text="飯量減半",
            estimate=_dashboard_revision_estimate(conn, log_id, calories=510, protein=31),
        )
        confirm_user_meal_photo_revision(
            conn, event_id="REVISED-TAMPER-CONFIRM", user_id="U-REVISED-TAMPER",
            log_id=log_id, from_version=1, draft_token=revision["token"],
        )
        if tamper_mode == "latest_revision_source":
            conn.execute(
                """UPDATE food_catalog SET source_type='official_menu'
                   WHERE food_id=(SELECT food_id FROM food_logs WHERE log_id=?)""",
                (log_id,),
            )
        else:
            conn.execute(
                "DELETE FROM pending_meal_photo_drafts WHERE token=?", (revision["token"],)
            )
        conn.commit()

    ledger = server.get_daily_food_ledger("U-REVISED-TAMPER", today)
    item = next(item for item in ledger["items"] if item["log_id"] == log_id)
    assert item["version"] == 2
    assert item["trust_integrity_status"] == "integrity_verification_failed"
    assert item["is_meal_photo_origin"] is True
    assert item["is_nutrition_countable"] is False
    assert item["nutrition"] == {}
    assert server.get_dashboard_data("U-REVISED-TAMPER") is None


def test_approval_and_source_tamper_cannot_fall_back_to_raw_snapshot(tmp_path, monkeypatch):
    db_dir = tmp_path / "approved-photo-source-tamper"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        approved = insert_approved_meal_photo_log(
            conn, token="aabbccdd0011", user_id="U-APPROVAL-TAMPER",
            reviewer="U-APPROVAL-TAMPER", consumed_at=f"{today}T12:00:00+08:00",
            meal_slot="午餐", source_image_ref="approval-tamper.jpg",
            observed_payload={
                "visible_items": [{"name": "核准照片", "category": "protein", "confidence": 0.9}]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0, "protein_low_exchange": 1,
                "protein_medium_exchange": 0, "protein_high_exchange": 0,
                "starch_exchange": 0, "vegetable_exchange": 0,
                "fruit_exchange": 0, "fat_exchange": 0,
            },
        )
        conn.commit()

    approved_dashboard = server.get_dashboard_data("U-APPROVAL-TAMPER")
    assert approved_dashboard["ai_estimated_count"] == 0
    _, approved_text_nodes = _dashboard_flex_text_nodes(
        server.build_dashboard_flex("U-APPROVAL-TAMPER")
    )
    approved_texts = [node["text"] for node in approved_text_nodes]
    assert "今日已吃" in approved_texts and "55" in approved_texts and "已吃 55" in approved_texts
    assert "今日蛋白質" in approved_texts and "7 g" in approved_texts
    assert "含 AI 估算，非營養師核准" not in approved_texts

    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE food_catalog SET source_type='official_menu'
               WHERE food_id=(SELECT food_id FROM food_logs WHERE log_id=?)""",
            (approved["log_id"],),
        )
        conn.execute(
            """UPDATE food_exchange_approvals SET approved_exchange_hash='tampered'
               WHERE approval_id=(SELECT exchange_approval_id FROM food_logs WHERE log_id=?)""",
            (approved["log_id"],),
        )
        conn.commit()

    ledger = server.get_daily_food_ledger("U-APPROVAL-TAMPER", today)
    item = next(item for item in ledger["items"] if item["log_id"] == approved["log_id"])
    assert item["is_meal_photo_origin"] is True
    assert item["is_nutrition_countable"] is False
    assert item["nutrition_authority"] == ""
    assert item["nutrition"] == {}
    assert server.get_dashboard_data("U-APPROVAL-TAMPER") is None


@pytest.mark.parametrize(
    "tamper_mode",
    ["source-only", "source-and-hash", "missing-approval", "cross-food-id", "owner"],
)
def test_approved_photo_integrity_tamper_fails_closed_in_totals_and_sheet_outbox(
    tmp_path, monkeypatch, tamper_mode,
):
    db = tmp_path / f"approved-photo-consumers-{tamper_mode}.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    user_id = "U-APPROVED-PHOTO-CONSUMERS"
    consumed_at = "2026-09-13T12:00:00+08:00"
    fixture_date = datetime.fromisoformat(consumed_at).date()
    monkeypatch.setattr(server, "tw_today", lambda: fixture_date)
    exchange = {
        "milk_exchange": 0, "protein_low_exchange": 1,
        "protein_medium_exchange": 0, "protein_high_exchange": 0,
        "starch_exchange": 0, "vegetable_exchange": 0,
        "fruit_exchange": 0, "fat_exchange": 0,
    }
    with sqlite3.connect(db) as conn:
        ordinary = server.create_daily_food_log(
            conn, user_id=user_id, product_name="一般餐", meal_slot="早餐",
            consumed_at="2026-09-13T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 100, "protein_g": 10},
            source_type="official_menu",
        )
        valid_approved = insert_approved_meal_photo_log(
            conn, token="feedface0001", user_id=user_id, reviewer="ADMIN",
            consumed_at="2026-09-13T10:00:00+08:00", meal_slot="點心",
            source_image_ref="valid-approved.jpg",
            observed_payload={"visible_items": [{
                "name": "合法核准照片", "category": "protein", "confidence": 0.9,
            }]}, answers={}, exact_exchange=exchange,
        )
        tampered = insert_approved_meal_photo_log(
            conn, token="feedface0002", user_id=user_id, reviewer="ADMIN",
            consumed_at=consumed_at, meal_slot="午餐",
            source_image_ref="tampered-approved.jpg",
            observed_payload={"visible_items": [{
                "name": "來源遭竄改照片", "category": "protein", "confidence": 0.9,
            }]}, answers={}, exact_exchange=exchange,
        )
        if tamper_mode.startswith("source"):
            conn.execute(
                "UPDATE food_catalog SET source_type='official_menu' WHERE food_id=?",
                (tampered["food_id"],),
            )
        if tamper_mode == "source-and-hash":
            conn.execute(
                "UPDATE food_exchange_approvals SET approved_exchange_hash='tampered' "
                "WHERE approval_id=?",
                (tampered["approval_id"],),
            )
        elif tamper_mode == "missing-approval":
            conn.execute(
                "DELETE FROM food_exchange_approvals WHERE approval_id=?",
                (tampered["approval_id"],),
            )
        elif tamper_mode == "cross-food-id":
            conn.execute(
                "UPDATE food_exchange_approvals SET food_id=? WHERE approval_id=?",
                (ordinary["food_id"], tampered["approval_id"]),
            )
        elif tamper_mode == "owner":
            conn.execute(
                "UPDATE food_catalog SET owner_user_id='U-OTHER' WHERE food_id=?",
                (tampered["food_id"],),
            )
        ai_log_id = _confirm_dashboard_ai_photo(
            conn, user_id=user_id, source_message_id=f"AI-CONTROL-{tamper_mode}",
            consumed_at="2026-09-13T14:00:00+08:00",
        )
        unknown_token = save_meal_photo_draft(
            conn, user_id=user_id, source_message_id=f"UNKNOWN-CONTROL-{tamper_mode}",
            payload=sample_payload(), source_image_ref="nutrition-image:unknown-control.jpg",
            meal_slot="晚餐", consumed_at="2026-09-13T18:00:00+08:00",
        )
        unknown_draft = _answer_all(conn, unknown_token, unknown=True, user_id=user_id)
        unknown = apply_meal_photo_action(
            conn, event_id=f"UNKNOWN-CONFIRM-{tamper_mode}", user_id=user_id,
            token=unknown_token, expected_version=unknown_draft["version"],
            action="confirm_estimate",
        )
        unknown_log_id = unknown["result"]["log_id"]
        ai_food_id = conn.execute(
            "SELECT food_id FROM food_logs WHERE log_id=?", (ai_log_id,)
        ).fetchone()[0]
        unknown_food_id = conn.execute(
            "SELECT food_id FROM food_logs WHERE log_id=?", (unknown_log_id,)
        ).fetchone()[0]
        conn.commit()

        totals = daily_consumed_totals(
            conn, user_id=user_id, date_iso="2026-09-13",
        )
        summary = server.daily_food_summary(
            conn, user_id=user_id, date_iso="2026-09-13",
        )
        replay_projection = _confirmed_result(
            conn, tampered["log_id"], already_confirmed=True,
        )

    # Formal totals count the ordinary log and the intact approved photo only.
    # AI nutrition remains in its separate estimated lane and unknown stays unknown.
    assert totals["calories_kcal"] == 155.0
    assert totals["protein_g"] == 17.0
    assert totals["protein_low_exchange"] == 1.0
    summary_by_name = {item["name"]: item for item in summary["foods"]}
    corrupted = summary_by_name["餐點照片：來源遭竄改照片"]
    assert corrupted["calories_kcal"] is None
    assert corrupted["protein_g"] is None
    assert corrupted["trust_integrity_status"] == "integrity_verification_failed"
    # Only the unrelated ordinary row still awaits exchange review; corruption
    # is reported as an integrity failure, not mislabeled as another review.
    assert summary["pending_reviews"] == 1
    assert summary["totals"]["calories_kcal"] == 155.0
    assert summary["estimated_totals"] == {
        "calories_kcal": 680.0, "protein_g": 35.0,
    }
    assert replay_projection["log"]["nutrition"] == {}
    assert replay_projection["log"]["approved_exchange"] == {}
    assert replay_projection["log"]["exchange_review_status"] == "integrity_verification_failed"

    ledger = server.get_daily_food_ledger(user_id, "2026-09-13")
    ledger_by_id = {item["log_id"]: item for item in ledger["items"]}
    corrupted_ledger = ledger_by_id[tampered["log_id"]]
    assert corrupted_ledger["nutrition"] == {}
    assert corrupted_ledger["is_nutrition_countable"] is False
    assert corrupted_ledger["trust_integrity_status"] == "integrity_verification_failed"
    corrupted_bubble = json.dumps(
        server._daily_food_item_bubble(corrupted_ledger), ensure_ascii=False,
    )
    assert "來源未驗證" in corrupted_bubble
    assert "資料完整性驗證未通過，營養資料暫不可用" in corrupted_bubble
    assert "🔥 NA" in corrupted_bubble and "🥩 NA" in corrupted_bubble
    assert "來源：official_menu" not in corrupted_bubble
    assert "核准" not in corrupted_bubble and "待審" not in corrupted_bubble
    for invalid_edit_entry in ("調整份量", "修正營養", "更多操作", "修改品項", "修改這餐"):
        assert invalid_edit_entry not in corrupted_bubble

    valid_ordinary_bubble = json.dumps(
        server._daily_food_item_bubble(ledger_by_id[ordinary["log_id"]]), ensure_ascii=False,
    )
    valid_approved_bubble = json.dumps(
        server._daily_food_item_bubble(ledger_by_id[valid_approved["log_id"]]), ensure_ascii=False,
    )
    valid_ai_bubble = json.dumps(
        server._daily_food_item_bubble(ledger_by_id[ai_log_id]), ensure_ascii=False,
    )
    assert "來源：official_menu" in valid_ordinary_bubble
    assert "來源：餐點照片" in valid_approved_bubble
    assert "來源：顧客確認・AI估算" in valid_ai_bubble
    assert ledger_by_id[ordinary["log_id"]]["trust_integrity_status"] == ""
    assert ledger_by_id[valid_approved["log_id"]]["trust_integrity_status"] == ""
    assert ledger_by_id[ai_log_id]["trust_integrity_status"] == "verified"
    assert ledger_by_id[unknown_log_id]["trust_integrity_status"] == "verified"
    assert ledger_by_id[unknown_log_id]["nutrition"] == {}
    assert ledger_by_id[valid_approved["log_id"]]["nutrition"]["calories_kcal"] == 55.0
    assert ledger_by_id[ai_log_id]["nutrition"]["calories_kcal"] == 680.0

    dashboard = server.get_dashboard_data(user_id)
    assert dashboard["extra_cal"] == 835.0
    assert dashboard["extra_pro"] == 52.0
    assert dashboard["recorded_count"] == 3
    assert "餐點照片：來源遭竄改照片" not in dashboard["food_list"]

    report = format_daily_health_report(
        report_date="2026-09-13", checkin=None, foods=summary["foods"],
        totals=summary["totals"], estimated_totals=summary["estimated_totals"],
        target=None, exercise=None, pending_reviews=summary["pending_reviews"],
    )
    corrupted_line = next(
        line for line in report.splitlines() if "來源遭竄改照片" in line
    )
    assert "NA kcal｜蛋白質NAg" in corrupted_line
    assert "55 kcal｜蛋白質7g" not in corrupted_line
    assert "資料完整性驗證未通過，營養資料暫不可用" in report
    assert "待營養師核准" not in report

    sheet_rows = {}

    class Sheet:
        def __init__(self, title):
            self.title = title

        def find(self, *_args, **_kwargs):
            return None

        def append_row(self, values, **_kwargs):
            sheet_rows[(self.title, values[0])] = values

    monkeypatch.setattr(server, "_nutrition_ws", lambda title: Sheet(title))
    for food_id in (
        ordinary["food_id"], valid_approved["food_id"], tampered["food_id"],
        ai_food_id, unknown_food_id,
    ):
        server._sync_food_outbox(food_id)
    for log_id in (
        ordinary["log_id"], valid_approved["log_id"], tampered["log_id"],
        ai_log_id, unknown_log_id,
    ):
        server._sync_food_log_outbox(log_id)

    food_sheet = "食品資料庫"
    log_sheet = "飲食紀錄"
    assert sheet_rows[(food_sheet, ordinary["food_id"])][10:12] == [100.0, 10.0]
    assert sheet_rows[(food_sheet, valid_approved["food_id"])][10:12] == [55.0, 7.0]
    assert sheet_rows[(food_sheet, valid_approved["food_id"])][18] == 1.0
    assert sheet_rows[(food_sheet, tampered["food_id"])][10:25] == [""] * 15
    assert sheet_rows[(food_sheet, tampered["food_id"])][25] == "integrity_verification_failed"
    assert sheet_rows[(food_sheet, ai_food_id)][10:25] == [""] * 15
    assert sheet_rows[(food_sheet, ai_food_id)][25] == "user_confirmed_ai_estimate"
    assert sheet_rows[(food_sheet, unknown_food_id)][10:25] == [""] * 15
    assert sheet_rows[(food_sheet, unknown_food_id)][25] == "user_confirmed_ai_estimate"

    assert sheet_rows[(log_sheet, ordinary["log_id"])][9:11] == [100.0, 10.0]
    assert sheet_rows[(log_sheet, valid_approved["log_id"])][9:11] == [55.0, 7.0]
    assert sheet_rows[(log_sheet, valid_approved["log_id"])][17] == 1.0
    # Upserts with blanks clear previously synced trusted values instead of
    # silently leaving them behind while the local outbox is marked synced.
    assert sheet_rows[(log_sheet, tampered["log_id"])][9:24] == [""] * 15
    assert sheet_rows[(log_sheet, ai_log_id)][9:13] == [680.0, 35.0, "", ""]
    assert sheet_rows[(log_sheet, ai_log_id)][16:24] == [""] * 8
    assert sheet_rows[(log_sheet, unknown_log_id)][9:24] == [""] * 15


def test_dashboard_uses_food_ledger_without_creating_placeholder_health_profile(tmp_path, monkeypatch):
    db_dir = tmp_path / "new-vip-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U-NEW-VIP", product_name="燕麥豆漿", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 287, "protein_g": 11.3},
            source_type="official_menu",
        )
        server.create_daily_food_log(
            conn, user_id="U-NEW-VIP", product_name="鮭魚食蔬", meal_slot="晚餐",
            consumed_at=f"{today}T18:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 376, "protein_g": 24},
            source_type="official_menu",
        )
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM health_profile WHERE user_id='U-NEW-VIP'"
        ).fetchone() is None

    dashboard = server.get_dashboard_data("U-NEW-VIP")

    assert dashboard is not None
    assert dashboard["name"] == "你"
    assert dashboard["tdee"] is None
    assert dashboard["protein_goal"] is None
    assert dashboard["extra_cal"] == 663
    assert dashboard["extra_pro"] == 35.3
    assert dashboard["food_list"] == ["燕麥豆漿", "鮭魚食蔬"]
    assert dashboard["recorded_count"] == 2
    assert dashboard["task_logged_once"] is True
    assert dashboard["task_two_meals"] is True

    flex = server.build_dashboard_flex("U-NEW-VIP")
    assert flex is not None
    rendered = json.dumps(flex.as_json_dict(), ensure_ascii=False)
    assert "燕麥豆漿" in rendered
    assert "鮭魚食蔬" in rendered
    assert "663" in rendered
    assert "35.3" in rendered
    assert '"text": "今日已吃"' in rendered and '"text": "663"' in rendered
    assert '"text": "今日蛋白質"' in rendered and '"text": "35.3 g"' in rendered
    assert "目標未設定" in rendered

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM health_profile WHERE user_id='U-NEW-VIP'"
        ).fetchone() is None


def test_dashboard_without_profile_keeps_delimiter_in_single_food_name(tmp_path, monkeypatch):
    db_dir = tmp_path / "delimiter-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U-SINGLE-FOOD", product_name="雞胸、青花菜", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 20},
            source_type="official_menu",
        )
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM health_profile WHERE user_id='U-SINGLE-FOOD'"
        ).fetchone() is None

    dashboard = server.get_dashboard_data("U-SINGLE-FOOD")

    assert dashboard is not None
    assert dashboard["food_list"] == ["雞胸、青花菜"]
    assert dashboard["recorded_count"] == 1
    assert dashboard["task_two_meals"] is False
    assert dashboard["extra_cal"] == 200
    assert dashboard["extra_pro"] == 20

    flex = server.build_dashboard_flex("U-SINGLE-FOOD")
    assert flex is not None
    rendered = json.dumps(flex.as_json_dict(), ensure_ascii=False)
    assert '"text": "早餐｜雞胸、青花菜"' in rendered
    assert "今日紀錄" in rendered
    assert '"text": "今日已吃"' in rendered and '"text": "200"' in rendered
    assert '"text": "今日蛋白質"' in rendered and '"text": "20 g"' in rendered
    assert "目標未設定" in rendered
    assert "含 AI 估算，非營養師核准" not in rendered
    assert "今日已記錄" not in rendered

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM health_profile WHERE user_id='U-SINGLE-FOOD'"
        ).fetchone() is None


def test_dashboard_large_nutrition_numbers_remain_complete_and_wrapped(tmp_path, monkeypatch):
    db_dir = tmp_path / "large-dashboard-values"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U-LARGE-DASHBOARD", product_name="大量測試餐", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 20},
            source_type="official_menu",
        )
        conn.commit()

    dashboard = server.get_dashboard_data("U-LARGE-DASHBOARD")
    dashboard.update({
        "extra_cal": 1234567.5,
        "tdee": 2000000,
        "extra_pro": 98765.25,
        "protein_goal": 100000,
        "balance_records": [{
            "slot": "早餐", "name": "大量測試餐",
            "kcal": 1234567.5, "protein": 98765.25,
            "source_type": "official_menu", "ai_estimated": False,
        }],
    })
    monkeypatch.setattr(server, "get_dashboard_data", lambda _user_id, *, scope="full": dashboard)

    _, text_nodes = _dashboard_flex_text_nodes(
        server.build_dashboard_flex("U-LARGE-DASHBOARD")
    )
    expected = {
        # v53 intentionally renders kcal as integers and grams to one decimal.
        "765,432", "目標 2,000,000", "已吃 1,234,568",
        "1234.8 g", "/ 100000 g",
    }
    matching = [node for node in text_nodes if node["text"] in expected]
    assert {node["text"] for node in matching} == expected
    for node in matching:
        if node['text'] == '已吃 1,234,568':
            # Compact legends retain the whole value on one line, including SDK replay.
            assert node.get('wrap') is False and node.get('maxLines') == 1
            assert node.get('size') == '7px'
        else:
            assert node.get('wrap') is True
            assert node.get('size') in {'xxs', 'sm'}


def test_dashboard_with_profile_uses_canonical_log_names_without_delimiter_xp(tmp_path, monkeypatch):
    db_dir = tmp_path / "profile-delimiter-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U-PROFILE','既有會員',1800,90,200,20,'雞胸、青花菜',?)""",
            (today,),
        )
        server.create_daily_food_log(
            conn, user_id="U-PROFILE", product_name="雞胸、青花菜", meal_slot="早餐",
            consumed_at=f"{today}T08:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 200, "protein_g": 20},
            source_type="official_menu",
        )
        conn.commit()

    dashboard = server.get_dashboard_data("U-PROFILE")

    assert dashboard["food_list"] == ["雞胸、青花菜"]
    assert dashboard["recorded_count"] == 1
    assert dashboard["task_two_meals"] is False
    assert dashboard["extra_cal"] == 200
    assert dashboard["extra_pro"] == 20
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COALESCE(SUM(today_xp_earned), 0) FROM achievement_daily_log WHERE user_id='U-PROFILE'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COALESCE(xp_total, 0) FROM user_achievements WHERE user_id='U-PROFILE'"
        ).fetchone()[0] == 0


def test_dashboard_keeps_empty_user_without_profile_hidden(tmp_path, monkeypatch):
    db_dir = tmp_path / "empty-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()

    assert server.get_dashboard_data("U-NO-PROFILE-NO-FOOD") is None


def test_dashboard_uses_canonical_totals_when_profile_projection_already_contains_photo(tmp_path, monkeypatch):
    db_dir = tmp_path / "profile-photo-projection-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U-PROJECTED-PHOTO','既有會員',1800,90,0,0,'',?)""",
            (today,),
        )
        server.create_daily_food_log(
            conn, user_id="U-PROJECTED-PHOTO", product_name="正常餐", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 100, "protein_g": 10},
            source_type="official_menu",
        )
        insert_approved_meal_photo_log(
            conn, token="feedface0001", user_id="U-PROJECTED-PHOTO",
            reviewer="U-PROJECTED-PHOTO", consumed_at=f"{today}T13:00:00+08:00",
            meal_slot="午餐", source_image_ref="projected-photo.jpg",
            observed_payload={
                "visible_items": [{"name": "照片餐", "category": "protein", "confidence": 0.9}]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0, "protein_low_exchange": 1,
                "protein_medium_exchange": 0, "protein_high_exchange": 0,
                "starch_exchange": 0, "vegetable_exchange": 0,
                "fruit_exchange": 0, "fat_exchange": 0,
            },
        )
        server._sync_health_profile_from_ledger_conn(
            conn, "U-PROJECTED-PHOTO", today, current_date=today
        )
        projected = conn.execute(
            """SELECT today_extra_cal,today_extra_pro
               FROM health_profile WHERE user_id='U-PROJECTED-PHOTO'"""
        ).fetchone()
        conn.commit()

    assert projected == (155, 17)
    dashboard = server.get_dashboard_data("U-PROJECTED-PHOTO")
    replayed = server.get_dashboard_data("U-PROJECTED-PHOTO")

    assert dashboard["extra_cal"] == 155
    assert dashboard["extra_pro"] == 17
    assert dashboard["food_list"] == ["正常餐", "餐點照片：照片餐"]
    assert dashboard["recorded_count"] == 2
    assert replayed["extra_cal"] == 155
    assert replayed["extra_pro"] == 17


def test_dashboard_counts_approved_meal_photo_estimates_once_including_legacy_na_snapshot(tmp_path, monkeypatch):
    db_dir = tmp_path / "photo-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U1','Jason',2000,100,0,0,'',?)""",
            (server.tw_today().isoformat(),),
        )
        ensure_nutrition_schema(conn)
        insert_approved_meal_photo_log(
            conn,
            token="abcdef123456",
            user_id="U1",
            reviewer="U1",
            consumed_at=server.tw_now().isoformat(),
            meal_slot="早餐",
            source_image_ref="photo.jpg",
            observed_payload={
                "visible_items": [
                    {"name": "雞胸肉", "category": "protein", "confidence": 0.9},
                    {"name": "青花菜", "category": "vegetable", "confidence": 0.9},
                ]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0,
                "protein_low_exchange": 0,
                "protein_medium_exchange": 2,
                "protein_high_exchange": 0,
                "starch_exchange": 1,
                "vegetable_exchange": 1,
                "fruit_exchange": 0,
                "fat_exchange": 0,
            },
        )
        # 模擬上線前已核准、但 nutrition_snapshot_json 仍為空的舊照片紀錄。
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json='{}' WHERE source_image_ref='photo.jpg'"
        )
        taipei_0030 = server.tw_now().replace(hour=0, minute=30, second=0, microsecond=0)
        insert_approved_meal_photo_log(
            conn,
            token="abcdef123457",
            user_id="U1",
            reviewer="U1",
            consumed_at=taipei_0030.astimezone(timezone.utc).isoformat(),
            meal_slot="早餐",
            source_image_ref="photo-utc.jpg",
            observed_payload={
                "visible_items": [
                    {"name": "UTC跨日雞胸", "category": "protein", "confidence": 0.9},
                ]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0,
                "protein_low_exchange": 1,
                "protein_medium_exchange": 0,
                "protein_high_exchange": 0,
                "starch_exchange": 0,
                "vegetable_exchange": 0,
                "fruit_exchange": 0,
                "fat_exchange": 0,
            },
        )
        conn.commit()

    dashboard = server.get_dashboard_data("U1")

    assert set(dashboard["food_list"]) == {
        "餐點照片：雞胸肉、青花菜",
        "餐點照片：UTC跨日雞胸",
    }
    assert dashboard["recorded_count"] == 2
    assert dashboard["extra_cal"] == 293.0
    assert dashboard["extra_pro"] == 24.0

    replayed_dashboard = server.get_dashboard_data("U1")
    assert replayed_dashboard["extra_cal"] == 293.0
    assert replayed_dashboard["extra_pro"] == 24.0

    with sqlite3.connect(db) as conn:
        log_id, approval_id, food_fingerprint, approved_json = conn.execute(
            """SELECT fl.log_id,a.approval_id,a.food_fingerprint,a.approved_exchange_json
               FROM food_logs fl JOIN food_exchange_approvals a
                 ON a.approval_id=fl.exchange_approval_id
               WHERE fl.source_image_ref='photo.jpg'"""
        ).fetchone()
        v2_hash = server.exchange_approval_hash(
            food_fingerprint, "meal-photo-admin-v2", json.loads(approved_json)
        )
        restored_nutrition = server.estimate_nutrition_from_exchanges(
            json.loads(approved_json)
        )
        conn.execute(
            """UPDATE food_exchange_approvals
               SET suggestion_rule_version='meal-photo-admin-v2',approved_exchange_hash=?
               WHERE approval_id=?""",
            (v2_hash, approval_id),
        )
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json=? WHERE log_id=?",
            (json.dumps(restored_nutrition, ensure_ascii=False), log_id),
        )
        conn.commit()

    serving_edit = server.apply_daily_food_log_edit(
        user_id="U1", log_id=log_id, expected_version=1,
        event_id="photo-serving-2", action="set_servings", value=2,
    )
    assert serving_edit["servings"] == 2
    assert serving_edit["nutrition"]["_estimate_type"] == "approved_exchange_estimate"
    assert serving_edit["nutrition"]["_rule_version"] == "tw-exchange-macros-v1"
    with sqlite3.connect(db) as conn:
        applied = json.loads(conn.execute(
            "SELECT approved_exchange_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0])
    assert applied["protein_medium_exchange"] == 4.0
    assert applied["starch_exchange"] == 2.0
    assert applied["vegetable_exchange"] == 2.0
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE food_logs SET nutrition_snapshot_json=? WHERE log_id=?""",
            (json.dumps({"calories_kcal": 999999, "protein_g": 888888}), log_id),
        )
        conn.commit()
        totals = daily_consumed_totals(
            conn, user_id="U1", date_iso=server.tw_today().isoformat()
        )
        summary = server.daily_food_summary(
            conn, user_id="U1", date_iso=server.tw_today().isoformat()
        )
    assert totals["calories_kcal"] == 531.0
    assert totals["protein_g"] == 41.0
    assert totals["protein_medium_exchange"] == 4.0
    assert totals["starch_exchange"] == 2.0
    assert totals["vegetable_exchange"] == 2.0
    assert summary["pending_reviews"] == 0
    summary_item = next(
        item for item in summary["foods"] if item["name"] == "餐點照片：雞胸肉、青花菜"
    )
    assert summary_item["calories_kcal"] == 476.0
    assert summary_item["protein_g"] == 34.0
    ledger = server.get_daily_food_ledger("U1", server.tw_today().isoformat())
    edited_item = next(item for item in ledger["items"] if item["log_id"] == log_id)
    assert edited_item["servings"] == 2.0
    assert edited_item["nutrition"]["calories_kcal"] == 476.0
    assert edited_item["nutrition"]["protein_g"] == 34.0

    class _Rows:
        def __init__(self):
            self.data = []

        def find(self, _entity_id, in_column=None):
            return None

        def append_row(self, values, value_input_option=None):
            self.data.append(values)

    outbox_rows = _Rows()
    monkeypatch.setattr(server, "_nutrition_ws", lambda _title: outbox_rows)
    server._sync_food_log_outbox(log_id)
    assert outbox_rows.data[0][9] == 476.0
    assert outbox_rows.data[0][10] == 34.0
    assert outbox_rows.data[0][18] == 4.0
    assert outbox_rows.data[0][20] == 2.0
    assert outbox_rows.data[0][21] == 2.0

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json='{' WHERE log_id=?", (log_id,)
        )
        conn.commit()
    malformed_ledger = server.get_daily_food_ledger(
        "U1", server.tw_today().isoformat()
    )
    malformed_item = next(
        item for item in malformed_ledger["items"] if item["log_id"] == log_id
    )
    assert malformed_item["nutrition"]["calories_kcal"] == 476.0
    outbox_rows.data.clear()
    server._sync_food_log_outbox(log_id)
    assert outbox_rows.data[0][9] == 476.0
    assert outbox_rows.data[0][10] == 34.0
    assert outbox_rows.data[0][18] == 4.0

    doubled_dashboard = server.get_dashboard_data("U1")
    assert set(doubled_dashboard["food_list"]) == {
        "餐點照片：雞胸肉、青花菜",
        "餐點照片：UTC跨日雞胸",
    }
    assert doubled_dashboard["recorded_count"] == 2
    assert doubled_dashboard["extra_cal"] == 531.0
    assert doubled_dashboard["extra_pro"] == 41.0

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE food_logs SET approved_exchange_json='[]' WHERE source_image_ref='photo.jpg'"
        )
        conn.commit()
    tampered_dashboard = server.get_dashboard_data("U1")
    assert tampered_dashboard["food_list"] == ["餐點照片：UTC跨日雞胸"]
    assert tampered_dashboard["recorded_count"] == 1
    assert tampered_dashboard["extra_cal"] == 55.0
    assert tampered_dashboard["extra_pro"] == 7.0


def test_dashboard_without_profile_excludes_deleted_confirmed_photo(tmp_path, monkeypatch):
    db_dir = tmp_path / "deleted-photo-dashboard"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()

    with sqlite3.connect(db) as conn:
        server.create_daily_food_log(
            conn, user_id="U-DELETED-PHOTO", product_name="正常餐", meal_slot="午餐",
            consumed_at=f"{today}T12:00:00+08:00", servings=1,
            nutrition={"calories_kcal": 100, "protein_g": 10},
            source_type="official_menu",
        )
        deleted = insert_approved_meal_photo_log(
            conn, token="deadbeef0001", user_id="U-DELETED-PHOTO",
            reviewer="U-DELETED-PHOTO", consumed_at=f"{today}T13:00:00+08:00",
            meal_slot="午餐", source_image_ref="deleted.jpg",
            observed_payload={
                "visible_items": [{"name": "已刪照片", "category": "protein", "confidence": 0.9}]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0, "protein_low_exchange": 1,
                "protein_medium_exchange": 0, "protein_high_exchange": 0,
                "starch_exchange": 0, "vegetable_exchange": 0,
                "fruit_exchange": 0, "fat_exchange": 0,
            },
        )
        conn.execute(
            "UPDATE food_logs SET deleted_at=? WHERE log_id=?",
            (server.tw_now().isoformat(), deleted["log_id"]),
        )
        conn.commit()

    dashboard = server.get_dashboard_data("U-DELETED-PHOTO")

    assert dashboard["food_list"] == ["正常餐"]
    assert dashboard["recorded_count"] == 1
    assert dashboard["extra_cal"] == 100
    assert dashboard["extra_pro"] == 10
    assert dashboard["task_two_meals"] is False


def test_dashboard_excludes_other_user_old_unconfirmed_and_non_photo_logs(tmp_path, monkeypatch):
    db_dir = tmp_path / "photo-dashboard-isolation"
    db = db_dir / "health.db"
    monkeypatch.setattr(server, "DB_DIR", str(db_dir))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    old_day = datetime.fromordinal(server.tw_today().toordinal() - 1).date().isoformat()

    def add_photo(conn, token, user_id, name, consumed_at):
        return insert_approved_meal_photo_log(
            conn, token=token, user_id=user_id, reviewer=user_id,
            consumed_at=consumed_at, meal_slot="早餐", source_image_ref=f"{token}.jpg",
            observed_payload={
                "visible_items": [{"name": name, "category": "protein", "confidence": 0.9}]
            },
            answers={},
            exact_exchange={
                "milk_exchange": 0, "protein_low_exchange": 1,
                "protein_medium_exchange": 0, "protein_high_exchange": 0,
                "starch_exchange": 0, "vegetable_exchange": 0,
                "fruit_exchange": 0, "fat_exchange": 0,
            },
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,today_extra_cal,today_extra_pro,today_food_items,today_date)
               VALUES ('U1','Jason',2000,100,0,0,'',?)""",
            (today,),
        )
        ensure_nutrition_schema(conn)
        add_photo(conn, "a00000000001", "U2", "別人的雞胸", f"{today}T08:00:00+08:00")
        add_photo(conn, "b00000000001", "U1", "昨天的雞胸", f"{old_day}T08:00:00+08:00")
        pending = add_photo(conn, "c00000000001", "U1", "未確認雞胸", f"{today}T09:00:00+08:00")
        conn.execute(
            "UPDATE food_logs SET confirmation_status='pending' WHERE log_id=?",
            (pending["log_id"],),
        )
        non_photo = add_photo(conn, "d00000000001", "U1", "一般食物卡", f"{today}T10:00:00+08:00")
        conn.execute(
            "UPDATE food_catalog SET source_type='user_private_food' WHERE food_id=?",
            (non_photo["food_id"],),
        )
        tampered = add_photo(conn, "e00000000001", "U1", "驗證鏈被改過", f"{today}T11:00:00+08:00")
        conn.execute(
            "UPDATE food_exchange_approvals SET approved_exchange_hash='tampered' WHERE approval_id=?",
            (tampered["approval_id"],),
        )
        fingerprint_tampered = add_photo(
            conn, "f00000000001", "U1", "指紋鏈被改過", f"{today}T12:00:00+08:00"
        )
        conn.execute(
            "UPDATE food_exchange_approvals SET food_fingerprint='tampered' WHERE approval_id=?",
            (fingerprint_tampered["approval_id"],),
        )
        conn.commit()

    dashboard = server.get_dashboard_data("U1")

    assert dashboard["food_list"] == []
    assert dashboard["recorded_count"] == 0
    assert dashboard["extra_cal"] == 0
    assert dashboard["extra_pro"] == 0


def test_meal_photo_final_confirmation_cards_show_exchange_estimate_and_confirm_button():
    draft = {
        "token": "abcdef123456",
        "version": 8,
        "consumed_at": "2026-08-02T12:00:00+08:00",
        "review": {
            "protein_class": "low",
            "protein_exchange": 3,
            "starch_exchange": 1.5,
            "vegetable_exchange": 0.5,
            "milk_exchange": 0,
            "fruit_exchange": 0,
        },
    }
    ready = server.build_meal_photo_review_ready_bubble(draft)
    ready_text = json.dumps(ready, ensure_ascii=False)
    assert "確認加入正式份量" in ready_text
    assert "代換估算" in ready_text
    assert "279 kcal" in ready_text
    assert "蛋白質 24.5g" in ready_text
    assert "NA" not in ready_text

    approved = server.build_meal_photo_approved_bubble(
        draft,
        {
            "approved_exchange": {
                "protein_low_exchange": 3,
                "starch_exchange": 1.5,
                "vegetable_exchange": 0.5,
                "milk_exchange": 0,
                "fruit_exchange": 0,
            },
            "estimated_nutrition": {
                "calories_kcal": 279,
                "protein_g": 24.5,
                "fat_g": 9,
                "carbohydrate_g": 25,
            },
        },
    )
    approved_text = json.dumps(approved, ensure_ascii=False)
    assert "代換估算" in approved_text
    assert "279 kcal" in approved_text
    assert "蛋白質 24.5g" in approved_text
    assert "NA" not in approved_text


def test_forged_meal_photo_review_postback_is_rejected_before_database_access(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_UID", "REAL_ADMIN")
    monkeypatch.setattr(
        server, "get_bound_admin_uid_for_authorization", lambda: "REAL_ADMIN"
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("非管理員請求不應進入資料庫審核流程")

    monkeypatch.setattr(server.sqlite3, "connect", forbidden_connect)
    event = SimpleNamespace(
        postback=SimpleNamespace(data="mpr:v1:abcdef123456:8:approve"),
        source=SimpleNamespace(user_id="REGULAR_USER"),
        reply_token="REPLY",
        webhook_event_id="FORGED-MPR-1",
        timestamp=0,
    )

    server.handle_meal_photo_postback(event)

    assert len(replies) == 1
    assert "管理員限定" in replies[0].text



def test_meal_photo_estimate_cards_disclose_low_fat_milk_assumption():
    draft = {
        "token": "abcdef123456",
        "version": 8,
        "consumed_at": "2026-08-02T12:00:00+08:00",
        "review": {
            "protein_class": "none",
            "protein_exchange": 0,
            "starch_exchange": 0,
            "vegetable_exchange": 0,
            "milk_exchange": 1,
            "fruit_exchange": 0,
        },
    }
    ready_text = json.dumps(server.build_meal_photo_review_ready_bubble(draft), ensure_ascii=False)
    approved_text = json.dumps(
        server.build_meal_photo_approved_bubble(
            draft,
            {
                "approved_exchange": {
                    "milk_exchange": 1,
                    "protein_low_exchange": 0,
                    "protein_medium_exchange": 0,
                    "protein_high_exchange": 0,
                    "starch_exchange": 0,
                    "vegetable_exchange": 0,
                    "fruit_exchange": 0,
                    "fat_exchange": 0,
                },
                "estimated_nutrition": {
                    "calories_kcal": 116,
                    "protein_g": 8,
                    "fat_g": 4,
                    "carbohydrate_g": 12,
                    "_warnings": ["milk_assumed_low_fat"],
                },
            },
        ),
        ensure_ascii=False,
    )
    assert "奶類以低脂奶估算" in ready_text
    assert "奶類以低脂奶估算" in approved_text


def test_dashboard_frequent_breakfast_combo_logs_three_foods(tmp_path, monkeypatch):
    db = tmp_path / "combo-from-dashboard.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, exch, kcal in [
            ("穀麥高粱 OATS & HONEY", {"starch_exchange": 1.98}, 197),
            ("草莓穀物脆片", {"starch_exchange": 2.37}, 223),
            ("無糖優格", {"milk_exchange": 0.5}, 62),
        ]:
            fid = new_id("food")
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',100,'g',1,?,'{}',
                           ?,'approved',?,'',0,'user_confirmed',?,?)""",
                (fid, name, json.dumps({"calories_kcal": kcal}), json.dumps(exch), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(
        _text_event("COMBO-DASHBOARD-1", "加入常吃：早餐1", user_id="U1")
    )

    assert len(replies) == 1
    assert replies[0].type == "flex"
    assert replies[0].alt_text == "今日總覽"
    assert "今日總覽" in json.dumps(
        json.loads(replies[0].as_json_string()), ensure_ascii=False
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U1' AND meal_slot='早餐'"
        ).fetchone()[0] == 3


def test_dashboard_breakfast_combo_retry_after_reply_failure_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "combo-retry.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, kcal in [
            ("穀麥高粱 OATS & HONEY", 197),
            ("草莓穀物脆片", 223),
            ("無糖優格", 62),
        ]:
            fid = new_id("food")
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                           'approved',?,'',1,'user_confirmed',?,?)""",
                (fid, name, json.dumps({"calories_kcal": kcal}), fid, now, now),
            )
    attempts = 0

    def flaky_reply(_token, _message):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("LINE unavailable")

    monkeypatch.setattr(server.line_bot_api, "reply_message", flaky_reply)
    with pytest.raises(RuntimeError, match="LINE unavailable"):
        server._handle_message_impl(
            _text_event("COMBO-RETRY-1", "加入常吃：早餐1", user_id="U1")
        )
    server._handle_message_impl(
        _text_event("COMBO-RETRY-1", "加入常吃：早餐1", user_id="U1")
    )

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM food_logs WHERE user_id='U1'"
        ).fetchone()[0] == 3


def test_breakfast_combo_missing_item_rolls_back_whole_combo(tmp_path, monkeypatch):
    db = tmp_path / "combo-atomic.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        fid = new_id("food")
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                exchange_json,exchange_review_status,fingerprint,original_image_ref,
                recognition_confidence,verification_status,created_at,updated_at)
               VALUES (?,?,'','','user_private_food','U1','private',1,'份',1,?,'{}','{}',
                       'approved',?,'',1,'user_confirmed',?,?)""",
            (fid, "穀麥高粱 OATS & HONEY", json.dumps({"calories_kcal": 197}), fid, now, now),
        )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    server._handle_message_impl(_text_event("COMBO-ATOMIC-1", "早餐1", user_id="U1"))

    assert "找不到" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_breakfast_combo2_logs_different_portions(tmp_path, monkeypatch):
    db = tmp_path / "combo2.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO health_profile
               (user_id,name,tdee,protein,sheet_name)
               VALUES ('U1','',2000,100,'')"""
        )
        conn.commit()
    now = utcish_now()
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        for name, exch, kcal in [
            ("穀麥高粱 OATS & HONEY", {"starch_exchange": 1.98}, 197),
            ("草莓穀物脆片", {"starch_exchange": 2.37}, 223),
            ("無糖優格", {"milk_exchange": 0.5}, 62),
        ]:
            fid = new_id("food")
            ps = json.dumps({"calories_kcal": kcal})
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,'','','user_private_food','U1','private',
                           100,'g',1,?, '{}',
                           ?,'approved',?,'',0,'user_confirmed',?,?)""",
                (fid, name, ps, json.dumps(exch), fid, now, now),
            )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _t, m: replies.append(m))

    event = _text_event("COMBO-2", "早餐2", user_id="U1")
    server._handle_message_impl(event)
    assert len(replies) == 1
    reply = replies[0]
    assert reply.type == "flex"
    raw = json.loads(reply.as_json_string())
    bubble_text = json.dumps(raw, ensure_ascii=False)
    assert "今日總覽" in bubble_text
    assert "679" in bubble_text or "678" in bubble_text or "677" in bubble_text

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT consumed_servings,meal_slot FROM food_logs WHERE user_id='U1' ORDER BY consumed_servings"
        ).fetchall()
        assert len(rows) == 3


def test_food_photo_restart_replay_does_not_call_model_again(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-restart.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(server.line_bot_api, "get_message_content", lambda _: SimpleNamespace(content=b"\xff\xd8\xff" + b"x" * 100))
    parsed = meal_photo_payload()
    parsed["ai_estimate"] = {
        "items": [{"name": "豆腐", "portion": "約1份", "calories_kcal": 180, "protein_g": 16}],
        "calories_kcal": {"estimate": 220, "min": 170, "max": 290},
        "protein_g": {"estimate": 18, "min": 14, "max": 24}, "confidence": 0.7,
    }
    calls = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(parsed)))])
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **kwargs: (calls.append(kwargs) or response))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(source=SimpleNamespace(user_id="U1"), message=SimpleNamespace(id="PHOTO-RESTART"),
                            reply_token="reply", timestamp=1784740620000)
    server.handle_image_message(event)
    server.processed_messages.clear()
    server.handle_image_message(event)
    assert len(calls) == 1
    assert len(replies) == 2
    assert json.loads(str(replies[0].contents)) == json.loads(str(replies[1].contents))


@pytest.mark.parametrize("interrupted_event_state", ["processing", "failed"])
def test_food_photo_recovers_committed_draft_before_model_after_event_completion_gap(
    tmp_path, monkeypatch, interrupted_event_state
):
    db = tmp_path / f"meal-photo-commit-gap-{interrupted_event_state}.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(
        server.line_bot_api, "get_message_content",
        lambda _: SimpleNamespace(content=b"\xff\xd8\xff" + b"x" * 100),
    )
    parsed = meal_photo_payload()
    parsed["ai_estimate"] = {
        "items": [{"name": "豆腐", "portion": "約1份", "calories_kcal": 180, "protein_g": 16}],
        "calories_kcal": {"estimate": 220, "min": 170, "max": 290},
        "protein_g": {"estimate": 18, "min": 14, "max": 24}, "confidence": 0.7,
    }
    model_calls = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(parsed)))])
    monkeypatch.setattr(
        server.client.chat.completions, "create",
        lambda **kwargs: (model_calls.append(kwargs) or response),
    )
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    event = SimpleNamespace(
        source=SimpleNamespace(user_id="U1"), message=SimpleNamespace(id="PHOTO-COMMIT-GAP"),
        reply_token="reply", timestamp=1784740620000,
    )
    server.processed_messages.discard(event.message.id)
    real_finish = server.finish_meal_photo_image_event
    monkeypatch.setattr(
        server, "finish_meal_photo_image_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash after draft commit")),
    )
    with pytest.raises(SystemExit, match="crash after draft commit"):
        server.handle_image_message(event)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone() == (1,)
        if interrupted_event_state == "failed":
            conn.execute(
                """UPDATE meal_photo_image_events
                   SET status='failed',claim_token='',lease_until=''
                   WHERE user_id='U1' AND source_message_id='PHOTO-COMMIT-GAP'"""
            )
            conn.commit()
        else:
            conn.execute(
                """UPDATE meal_photo_image_events SET lease_until='2000-01-01T00:00:00+08:00'
                   WHERE user_id='U1' AND source_message_id='PHOTO-COMMIT-GAP'"""
            )
            conn.commit()

    monkeypatch.setattr(server, "finish_meal_photo_image_event", real_finish)
    server.processed_messages.clear()  # process restart / LINE redelivery
    server.handle_image_message(event)

    assert len(model_calls) == 1
    assert len(replies) == 1 and replies[0].type == "flex"
    with sqlite3.connect(db) as conn:
        draft = conn.execute(
            "SELECT token,status,version FROM pending_meal_photo_drafts"
        ).fetchone()
        image_event = conn.execute(
            """SELECT status,attempts,result_json FROM meal_photo_image_events
               WHERE user_id='U1' AND source_message_id='PHOTO-COMMIT-GAP'"""
        ).fetchone()
    assert image_event[:2] == ("completed", 1)
    assert json.loads(image_event[2]) == {
        "token": draft[0], "draft_status": draft[1], "draft_version": draft[2]
    }


@pytest.mark.parametrize("draft_state", ["cancelled", "expired"])
def test_food_photo_commit_gap_replay_does_not_render_retired_draft_card(
    tmp_path, monkeypatch, draft_state
):
    db = tmp_path / f"meal-photo-retired-{draft_state}.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-RETIRED",
            payload={**meal_photo_payload(), "ai_estimate": {
                "items": [{"name": "豆腐", "portion": "1份", "calories_kcal": 100, "protein_g": 10}],
                "calories_kcal": {"estimate": 100, "min": 90, "max": 110},
                "protein_g": {"estimate": 10, "min": 9, "max": 11}, "confidence": 0.8,
                "provenance": {
                    "provider": "openai", "model": "gpt-4o",
                    "method": "vision_model_estimate",
                    "nutrition_basis": "unlabeled_meal_photo",
                },
            }}, workflow_version="user_confirmed_ai_nutrition_v2",
        )
        server.claim_meal_photo_image_event(
            conn, user_id="U1", source_message_id="PHOTO-RETIRED"
        )
        conn.execute(
            """UPDATE pending_meal_photo_drafts SET status=?,expires_at=? WHERE token=?""",
            (draft_state if draft_state == "cancelled" else "estimated",
             "2000-01-01T00:00:00+08:00" if draft_state == "expired" else "2999-01-01T00:00:00+08:00",
             token),
        )
        conn.execute(
            "UPDATE meal_photo_image_events SET status='failed',claim_token='',lease_until=''"
        )
        conn.commit()
    model_calls = []
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **kwargs: model_calls.append(kwargs))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    server.processed_messages.clear()
    server.handle_image_message(SimpleNamespace(
        source=SimpleNamespace(user_id="U1"), message=SimpleNamespace(id="PHOTO-RETIRED"),
        reply_token="reply", timestamp=1784740620000,
    ))
    assert model_calls == []
    assert len(replies) == 1 and replies[0].type == "text"
    assert ("取消" if draft_state == "cancelled" else "逾時") in replies[0].text
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status,attempts FROM meal_photo_image_events"
        ).fetchone() == ("completed", 1)
        assert conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone() == (draft_state,)


def test_food_photo_missing_ai_estimate_fails_closed_without_v1_draft(tmp_path, monkeypatch):
    db = tmp_path / "meal-photo-missing-ai.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "cleanup_nutrition_images", lambda: None)
    monkeypatch.setattr(server.line_bot_api, "get_message_content", lambda _: SimpleNamespace(content=b"\xff\xd8\xff" + b"x" * 100))
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(meal_photo_payload())))])
    monkeypatch.setattr(server.client.chat.completions, "create", lambda **_: response)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, message: replies.append(message))
    server.handle_image_message(SimpleNamespace(
        source=SimpleNamespace(user_id="U1"), message=SimpleNamespace(id="PHOTO-MISSING-AI"),
        reply_token="reply", timestamp=1784740620000,
    ))
    assert "無法安全辨識" in replies[-1].text
    with sqlite3.connect(db) as conn:
        ensure_meal_photo_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM pending_meal_photo_drafts").fetchone()[0] == 0


def test_authoritative_confirmed_image_holder_blocks_cleanup_without_outbox_requeue(
    tmp_path, monkeypatch,
):
    db = tmp_path / "confirmed-holder-cleanup.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    ref = server._store_nutrition_image(b"\xff\xd8\xff" + b"x" * 100, ".jpg")
    image_path = tmp_path / "nutrition_images" / ref.removeprefix("nutrition-image:")
    with sqlite3.connect(db) as conn:
        ensure_nutrition_schema(conn)
        token = save_pending_label(
            conn, user_id="U1", payload=valid_label(), source_image_ref=ref
        )
        result = confirm_pending_label(
            conn, token=token, user_id="U1", plan_link_status="no_plan"
        )
        conn.execute(
            "UPDATE food_logs SET created_at='2000-01-01T00:00:00+08:00' WHERE log_id=?",
            (result["log"]["log_id"],),
        )
        conn.execute("UPDATE nutrition_sheet_outbox SET status='synced'")
        conn.commit()

    server.cleanup_nutrition_images()

    with sqlite3.connect(db) as conn:
        log_ref = conn.execute("SELECT source_image_ref FROM food_logs").fetchone()[0]
        food_ref = conn.execute("SELECT original_image_ref FROM food_catalog").fetchone()[0]
        states = dict(conn.execute("SELECT entity_type,status FROM nutrition_sheet_outbox"))
    assert image_path.exists()
    assert log_ref == food_ref == ref
    assert states == {"food": "synced", "food_log": "synced"}


def test_vision_contract_bounds_ai_estimates_and_discloses_non_dietitian_status(
    tmp_path,
):
    prompt = server.build_nutrition_vision_prompt()
    food_section = prompt.split("若只有餐盤照片", 1)[1]
    assert "ai_estimate" in food_section
    assert "單值必須落在可信的合理區間內" in food_section
    assert "不可捏造精確量測或套用看不見的配方" in food_section
    assert "看不清或關鍵歧義" in food_section and "status=error" in food_section
    assert "未知不可用0代替" in food_section
    assert '"calories_kcal":0' not in food_section

    from meal_photo_system import build_meal_photo_estimate_bubble

    with sqlite3.connect(tmp_path / "bounded-estimate-disclosure.db") as conn:
        token = save_meal_photo_draft(
            conn,
            user_id="U1",
            source_message_id="AI-DISCLOSURE",
            payload=ai_estimated_payload(),
            source_image_ref="nutrition-image:retained.jpg",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    rendered = json.dumps(
        build_meal_photo_estimate_bubble(draft), ensure_ascii=False
    )
    assert "熱量：約680 kcal（580～800）" in rendered
    assert "蛋白質：約35 g（29～43）" in rendered
    assert "AI照片估算，非營養師核准" in rendered
