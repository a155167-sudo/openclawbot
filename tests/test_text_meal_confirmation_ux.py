import json
import sqlite3
from types import SimpleNamespace

import pytest

import server


def _text_event(message_id, text, user_id="U-TEXT"):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{message_id}",
        webhook_event_id=f"webhook-{message_id}",
    )


def _postback(data, event_id, user_id="U-TEXT"):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data),
        source=SimpleNamespace(user_id=user_id),
        reply_token=f"reply-{event_id}",
        webhook_event_id=event_id,
        timestamp=0,
    )


def _db(tmp_path, monkeypatch, *, mock_quota=True):
    db = tmp_path / "text-meal.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    monkeypatch.setattr(server, "_refresh_health_check_after_food_log", lambda *_a, **_k: None)
    if mock_quota:
        # The native shared quota path reads its own transactional VIP row;
        # the legacy permission mock does not authorize that path.
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO usage
                   (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
                   VALUES ('U-TEXT',10,10,?,'vip','2099-12-31',10)""",
                (server.tw_today().isoformat(),),
            )
        monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "(test quota)"))
    monkeypatch.setattr(server, "build_dashboard_flex", lambda _uid: server.TextSendMessage(text="DASHBOARD"))
    return db


def test_explicit_user_values_reply_with_short_success_then_dashboard_and_replay_once(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    server.processed_messages.clear()
    event = _text_event("DIRECT-1", "鮪魚蛋吐司 熱量350大卡 蛋白質17g")

    server._handle_message_impl(event)
    server.processed_messages.clear()
    server._handle_message_impl(event)

    assert len(replies) == 2
    for payload in replies:
        assert isinstance(payload, list) and len(payload) == 2
        assert payload[0].text == "✅ 已記錄：鮪魚蛋吐司｜350 kcal｜蛋白質 17 g（使用者提供）"
        assert payload[1].text == "DASHBOARD"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs WHERE user_id='U-TEXT'").fetchone()[0] == 1


def test_unknown_food_creates_owner_bound_draft_then_confirm_commits_once(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    replies = []
    estimates = []
    quota_calls = []
    native_charge = server._charge_text_meal_estimate_quota
    def capture_native_charge(conn, **kwargs):
        quota_calls.append(kwargs["user_id"])
        return native_charge(conn, **kwargs)
    monkeypatch.setattr(server, "_charge_text_meal_estimate_quota", capture_native_charge)
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(
        server,
        "estimate_text_meal_nutrition",
        lambda request: estimates.append(request) or {
            "food_name": "鮪魚蛋吐司",
            "portion_assumption": "1 份（一般早餐店份量）",
            "calories_kcal": {"estimate": 350, "min": 300, "max": 420},
            "protein_g": {"estimate": 17, "min": 13, "max": 22},
            "provenance": {"provider": "test", "model": "mock", "method": "text_meal_estimate"},
        },
    )
    server.processed_messages.clear()
    server._handle_message_impl(_text_event("DRAFT-1", "早餐吃一份鮪魚蛋吐司"))

    assert len(estimates) == 1
    preview = replies[-1]
    rendered = json.dumps(preview.as_json_dict(), ensure_ascii=False)
    assert "份量假設" in rendered and "300–420 kcal" in rendered and "13–22 g" in rendered
    preview_json = preview.as_json_dict()
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
    walk(preview_json)
    confirm = next(data for data in actions if data.endswith(":confirm"))
    cancel = next(data for data in actions if data.endswith(":cancel"))
    assert confirm.startswith("tmest:v1:") and cancel.startswith("tmest:v1:")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT user_id,status FROM pending_text_meal_estimates").fetchone() == ("U-TEXT", "pending")

    event = _postback(confirm, "CONFIRM-1")
    server.handle_postback_event(event)
    server.handle_postback_event(event)
    assert len(estimates) == 1
    assert quota_calls == ["U-TEXT"]
    for payload in replies[-2:]:
        assert isinstance(payload, list) and len(payload) == 2
        assert "✅ 已記錄：鮪魚蛋吐司" in payload[0].text
        assert "AI估算" in payload[0].text
        assert payload[1].text == "DASHBOARD"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        source = conn.execute("SELECT original_nutrition_snapshot_json FROM food_logs").fetchone()[0]
        assert json.loads(source)["estimate_metadata"]["calories_kcal_range"] == {"min": 300.0, "max": 420.0}


def test_text_estimate_adjust_cancel_and_foreign_owner_fail_closed(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda _request: {
        "food_name": "鮪魚蛋吐司", "portion_assumption": "1份",
        "calories_kcal": {"estimate": 350, "min": 300, "max": 420},
        "protein_g": {"estimate": 17, "min": 13, "max": 22},
        "provenance": {"provider": "test", "model": "mock", "method": "text_meal_estimate"},
    })
    draft = server.create_text_meal_estimate_draft(
        user_id="U-TEXT", message_id="DRAFT-CANCEL", request={
            "food_name": "鮪魚蛋吐司", "amount": 1.0, "unit": "serving", "meal_slot": "早餐"
        }
    )
    adjust = f"tmest:v1:{draft['token']}:{draft['version']}:portion:1.5"
    server.handle_postback_event(_postback(adjust, "ADJUST-1"))
    adjusted = server.get_text_meal_estimate_draft("U-TEXT", draft["token"])
    assert adjusted["version"] == 2 and adjusted["portion_multiplier"] == 1.5
    assert "450–630 kcal" in json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)

    foreign = f"tmest:v1:{draft['token']}:2:confirm"
    server.handle_postback_event(_postback(foreign, "FOREIGN-1", user_id="U-OTHER"))
    assert "找不到" in replies[-1].text
    server.handle_postback_event(_postback(f"tmest:v1:{draft['token']}:2:cancel", "CANCEL-1"))
    server.handle_postback_event(_postback(f"tmest:v1:{draft['token']}:2:cancel", "CANCEL-1"))
    assert replies[-1].text == replies[-2].text == "✅ 已取消；這筆估算沒有寫入飲食紀錄。"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_food_questions_and_bare_non_food_do_not_start_text_estimate():
    assert server.parse_natural_food_log_intent("鮪魚蛋吐司熱量多少？") is None
    assert server.parse_natural_food_log_intent("早餐吃什麼？") is None
    assert server.parse_natural_food_log_intent("電話") is None
    assert server.parse_natural_food_log_intent("裸") is None


def _mock_estimate(food_name="鮪魚蛋吐司"):
    return {
        "food_name": food_name,
        "portion_assumption": "1 份（一般份量）",
        "calories_kcal": {"estimate": 350, "min": 300, "max": 420},
        "protein_g": {"estimate": 17, "min": 13, "max": 22},
        "provenance": {"provider": "test", "model": "mock", "method": "text_meal_estimate"},
    }


def test_draft_is_durable_and_duplicate_message_debits_real_quota_only_once(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, mock_quota=False)
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES ('U-QUOTA',2,10,?,'vip','2099-12-31',2)""",
            (today,),
        )
        conn.commit()
    calls = []
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda request: calls.append(dict(request)) or _mock_estimate(),
    )
    request = {"food_name": "鮪魚蛋吐司", "amount": 1.0, "unit": "serving", "meal_slot": "早餐"}

    first = server.create_text_meal_estimate_draft(
        user_id="U-QUOTA", message_id="DURABLE-1", request=request
    )
    second = server.create_text_meal_estimate_draft(
        user_id="U-QUOTA", message_id="DURABLE-1", request=request
    )

    assert first["token"] == second["token"]
    assert first["status"] == second["status"] == "pending"
    assert len(calls) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-QUOTA'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT user_id,status,version FROM pending_text_meal_estimates"
        ).fetchone() == ("U-QUOTA", "pending", 1)


def test_expired_stale_and_cancelled_drafts_never_commit(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda _request: _mock_estimate())
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "left"))

    expired = server.create_text_meal_estimate_draft(
        user_id="U-TEXT", message_id="EXPIRED-1",
        request={"food_name": "過期餐", "amount": 1.0, "unit": "serving", "meal_slot": "晚餐"},
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_text_meal_estimates SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (expired["token"],),
        )
        conn.commit()
    with pytest.raises(ValueError, match="逾時"):
        server.apply_text_meal_estimate_action(
            user_id="U-TEXT", token=expired["token"], expected_version=1, action="confirm"
        )

    cancelled = server.create_text_meal_estimate_draft(
        user_id="U-TEXT", message_id="CANCEL-THEN-CONFIRM",
        request={"food_name": "取消餐", "amount": 1.0, "unit": "serving", "meal_slot": "午餐"},
    )
    server.apply_text_meal_estimate_action(
        user_id="U-TEXT", token=cancelled["token"], expected_version=1, action="cancel"
    )
    with pytest.raises(ValueError, match="已處理"):
        server.apply_text_meal_estimate_action(
            user_id="U-TEXT", token=cancelled["token"], expected_version=1, action="confirm"
        )

    adjusted = server.create_text_meal_estimate_draft(
        user_id="U-TEXT", message_id="STALE-CONFIRM",
        request={"food_name": "調整餐", "amount": 1.0, "unit": "serving", "meal_slot": "早餐"},
    )
    server.apply_text_meal_estimate_action(
        user_id="U-TEXT", token=adjusted["token"], expected_version=1,
        action="portion", multiplier=1.5,
    )
    with pytest.raises(ValueError, match="最新版本"):
        server.apply_text_meal_estimate_action(
            user_id="U-TEXT", token=adjusted["token"], expected_version=1, action="confirm"
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert dict(conn.execute(
            "SELECT token,status FROM pending_text_meal_estimates"
        ).fetchall())[expired["token"]] == "expired"


def test_invalid_provider_shape_refunds_quota_and_never_fabricates_macros(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, mock_quota=False)
    today = server.tw_today().isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES ('U-BAD-AI',2,10,?,'vip','2099-12-31',2)""",
            (today,),
        )
        conn.commit()
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda _request: {"food_name": "不完整餐", "calories_kcal": 350},
    )

    with pytest.raises(ValueError, match="缺少餐名或份量假設"):
        server.create_text_meal_estimate_draft(
            user_id="U-BAD-AI", message_id="BAD-SHAPE-1",
            request={"food_name": "不完整餐", "amount": None, "unit": "", "meal_slot": "晚餐"},
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U-BAD-AI'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT status,estimate_json FROM pending_text_meal_estimates"
        ).fetchone() == ("failed", "{}")
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


@pytest.mark.parametrize("text", [
    "我要記錄飲食 鮪魚蛋吐司 每100g熱量350 蛋白質17",
    "我要記錄飲食 鮪魚蛋吐司 每一百公克熱量350 蛋白質17",
    "我沒吃鮪魚蛋吐司",
    "我沒有吃鮪魚蛋吐司",
    "鮪魚蛋吐司熱量多少？",
    "早餐吃什麼？",
])
def test_reference_values_negations_and_questions_cannot_start_estimate(text):
    assert server.parse_natural_food_log_intent(text) is None
