import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import server


def _text_event(message_id, text, user_id="U-NANJING"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
        webhook_event_id=f"webhook-{message_id}",
    )


def _postback(data, event_id, user_id="U-NANJING"):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{event_id}",
        webhook_event_id=event_id,
        timestamp=0,
    )


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "nanjing-flow.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES (?,?,?,?,?,?,?)""",
            ("U-NANJING", 3, 10, server.tw_today().isoformat(), "vip", "2099-12-31", 3),
        )
        conn.commit()
    monkeypatch.setattr(server, "_refresh_health_check_after_food_log", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "build_dashboard_flex", lambda _uid: server.TextSendMessage(text="南京今日總覽（四鈕不變）"))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, payload: replies.append(payload))
    server.processed_messages.clear()
    return db, replies


def _estimate(name="無糖豆漿"):
    return {
        "schema_version": "text-meal-estimate-v2",
        "food_name": name,
        "portion_assumption": "500 ml",
        "basis_amount": 500.0,
        "basis_unit": "ml",
        "calories_kcal": {"estimate": 165, "min": 140, "max": 190},
        "protein_g": {"estimate": 16, "min": 14, "max": 18},
        "fat_g": {"estimate": 7, "min": 5, "max": 9},
        "carbohydrate_g": {"estimate": 9, "min": 7, "max": 11},
        "assessment": {"status": "consistent", "requires_correction": False},
        "provenance": {"provider": "offline", "model": "fake", "method": "text_meal_estimate"},
    }


def _postback_actions(message):
    actions = []
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "postback":
                actions.append(value.get("data"))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(message.as_json_dict())
    return actions


def test_nanjing_four_slot_time_boundaries():
    expected = {
        0: "點心", 4: "點心", 5: "早餐", 10: "早餐",
        11: "午餐", 13: "午餐", 14: "點心", 16: "點心",
        17: "晚餐", 20: "晚餐", 21: "點心", 23: "點心",
    }
    for hour, slot in expected.items():
        assert server.current_meal_slot(datetime(2026, 10, 8, hour, 0)) == slot


def test_entry_durably_awaits_bare_next_food_and_restart_replay_is_one_draft(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda request: calls.append(dict(request)) or _estimate(request['food_name']))
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "left"))

    server._handle_message_impl(_text_event("ENTER-1", "我要紀錄飲食"))
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT user_id,status,version FROM pending_text_meal_inputs WHERE user_id=?",
            ("U-NANJING",),
        ).fetchone()
    assert row == ("U-NANJING", "awaiting_food", 1)
    assert "下一則" in replies[-1].text

    # Keep testing the AI lane and durable replay. Generic soy now has a
    # cited ml reference; its zero-provider entry is tested separately.
    followup = _text_event("FOOD-1", "測試豆飲500ml")
    server._handle_message_impl(followup)
    server.processed_messages.clear()  # 模擬程序重啟後 LINE redelivery
    server._handle_message_impl(followup)

    assert len(calls) == 1
    assert replies[-1].as_json_dict() == replies[-2].as_json_dict()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM pending_text_meal_inputs WHERE user_id=?", ("U-NANJING",)
        ).fetchone()[0] == "consumed"


def test_explicit_values_make_fixed_zero_ai_draft_then_record_card_and_dashboard(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    provider_calls = []
    quota_calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda request: provider_calls.append(request))
    monkeypatch.setattr(server, "check_permission_and_quota", lambda uid: (quota_calls.append(uid) or True, "left"))

    server._handle_message_impl(
        _text_event("VALUES-1", "午餐：鮪魚蛋吐司 熱量350大卡 蛋白質17g")
    )

    preview = replies[-1]
    rendered = json.dumps(preview.as_json_dict(), ensure_ascii=False)
    assert "尚未記錄" in rendered and "使用者提供" in rendered
    assert provider_calls == [] and quota_calls == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        row = conn.execute(
            "SELECT status,meal_slot,estimate_json FROM pending_text_meal_estimates"
        ).fetchone()
    assert row[0:2] == ("pending", "午餐")
    assert json.loads(row[2])["provenance"]["method"] == "user_provided_nutrition"

    confirm = next(action for action in _postback_actions(preview) if action.endswith(":confirm"))
    server.handle_postback_event(_postback(confirm, "CONFIRM-VALUES"))

    success = replies[-1]
    assert isinstance(success, list) and len(success) == 2
    assert success[0].as_json_dict()["type"] == "flex"
    success_json = json.dumps(success[0].as_json_dict(), ensure_ascii=False)
    assert all(text in success_json for text in ("記錄成功", "鮪魚蛋吐司", "午餐", "350", "17", "使用者提供"))
    assert success[1].text == "南京今日總覽（四鈕不變）"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1


def test_log_nutrition_decimal_is_preserved_and_legacy_ai_result_is_only_a_draft(tmp_path, monkeypatch):
    parsed = server.parse_log_nutrition_tag(
        "ok [LOG_NUTRITION: CAL=180.5, PRO=11.3, NAME=豆漿]"
    )
    assert parsed["cal"] == 180.5
    assert parsed["pro"] == 11.3

    db, _replies = _setup(tmp_path, monkeypatch)
    content = "先確認。[LOG_NUTRITION: CAL=180.5, PRO=11.3, NAME=豆漿]"
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
    answer, preview = server.get_ai_response_with_memory(
        "U-NANJING", "請用一般估算記錄 早餐 豆漿", "AI-DRAFT-1"
    )

    assert "LOG_NUTRITION" not in answer
    assert "尚未記錄" in json.dumps(preview.as_json_dict(), ensure_ascii=False)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        estimate_json = conn.execute(
            "SELECT estimate_json FROM pending_text_meal_estimates WHERE source_message_id='AI-DRAFT-1'"
        ).fetchone()[0]
    estimate = json.loads(estimate_json)
    assert estimate["protein_g"]["estimate"] == 11.3


def test_awaiting_cancel_is_owner_scoped_and_never_calls_provider(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda request: calls.append(request))

    server._handle_message_impl(_text_event("ENTER-C", "我要紀錄飲食", "U-A"))
    server._handle_message_impl(_text_event("OTHER", "無糖豆漿500ml", "U-B"))
    assert calls == []
    server._handle_message_impl(_text_event("CANCEL", "取消", "U-A"))

    assert "取消" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM pending_text_meal_inputs WHERE user_id='U-A'"
        ).fetchone()[0] == "cancelled"
