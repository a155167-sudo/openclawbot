import json
import sqlite3
from types import SimpleNamespace

import pytest
import server


def _text(message_id, text):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id="U-NANJING"),
        reply_token=f"reply-{message_id}",
        webhook_event_id=f"webhook-{message_id}",
    )


def _postback(data, event_id):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data),
        source=SimpleNamespace(user_id="U-NANJING"),
        reply_token=f"reply-{event_id}",
        webhook_event_id=event_id,
        timestamp=0,
    )


def _actions(message):
    found = []
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") in {"postback", "uri"}:
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(message.as_json_dict())
    return found


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "official-menu-draft.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    now = server.tw_now().isoformat(timespec="seconds")
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO usage
               (user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit)
               VALUES ('U-NANJING',3,10,?,'vip','2099-12-31',3)""",
            (server.tw_today().isoformat(),),
        )
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,
                per_100_json,exchange_json,exchange_review_status,fingerprint,
                verification_status,created_at,updated_at,menu_category)
               VALUES ('menu_chicken_box','雞肉便當','label','system','public',
                       1,'serving',1,?,'{}','{}','approved','official-chicken-box',
                       'verified',?,?,'main')""",
            (json.dumps({"calories_kcal": 604.585, "protein_g": 43.16, "fat_g": 14.289, "carbohydrate_g": 77.2055}), now, now),
        )
        conn.commit()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, payload: replies.append(payload))
    monkeypatch.setattr(server, "_refresh_health_check_after_food_log", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "build_dashboard_flex", lambda _uid: server.TextSendMessage(text="既有總覽"))
    monkeypatch.setattr(server, "current_meal_slot", lambda *_a, **_k: "晚餐")
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("menu must not call AI")))
    monkeypatch.setattr(server, "_charge_text_meal_estimate_quota", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("menu must not debit quota")))
    server.processed_messages.clear()
    return db, replies


def test_registered_official_menu_one_serving_requires_confirm_and_replays_once(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)

    server.handle_message(_text("ENTER", "我要紀錄飲食"))
    server.handle_message(_text("FOOD", "雞肉便當"))
    picker = replies[-1]
    one = next(a["data"] for a in _actions(picker) if a.get("data", "").endswith(":servings:1:meal:晚餐"))
    server.handle_postback_event(_postback(one, "PICK-ONE"))

    draft = replies[-1]
    rendered = json.dumps(draft.as_json_dict(), ensure_ascii=False)
    assert all(text in rendered for text in ("尚未記錄", "雞肉便當", "1 份", "605", "43.2", "14.3", "77.2", "一日樂食餐點", "確認記錄", "修改", "取消")), rendered
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U-NANJING'").fetchone()[0] == 3

    confirm = next(a["data"] for a in _actions(draft) if a.get("data", "").endswith(":confirm"))
    event = _postback(confirm, "CONFIRM")
    server.handle_postback_event(event)
    server.handle_postback_event(event)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT consumed_amount,consumed_unit,nutrition_snapshot_json FROM food_logs").fetchall()
    assert len(rows) == 1
    assert rows[0][0:2] == (1.0, "serving")
    assert json.loads(rows[0][2]) == {"calories_kcal": 604.585, "protein_g": 43.16, "fat_g": 14.289, "carbohydrate_g": 77.2055}
    success = json.dumps(replies[-1][0].as_json_dict(), ensure_ascii=False)
    assert all(text in success for text in ("雞肉便當", "份量：1 份", "605", "一日樂食餐點"))


def test_registered_official_menu_cancel_writes_nothing(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    server.handle_postback_event(_postback("nlfood:v1:menu_chicken_box:servings:1:meal:晚餐", "PICK-CANCEL"))
    cancel = next(a["data"] for a in _actions(replies[-1]) if a.get("data", "").endswith(":cancel"))
    server.handle_postback_event(_postback(cancel, "CANCEL"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
    assert "沒有寫入" in replies[-1].text


def test_official_menu_liff_amount_edit_keeps_original_source(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    server.handle_postback_event(
        _postback("nlfood:v1:menu_chicken_box:servings:1:meal:晚餐", "PICK-EDIT")
    )
    action = next(a["data"] for a in _actions(replies[-1]) if a.get("data", "").endswith(":confirm"))
    parts = action.split(":")
    revised = server.apply_text_meal_custom_input(
        user_id="U-NANJING", token=parts[2], expected_version=int(parts[3]),
        mode="amount", text="1.5 份",
    )
    provenance = revised["estimate"]["provenance"]
    assert provenance["method"] == "official_menu_catalog"
    assert provenance["original_estimate"]["provenance"]["method"] == "official_menu_catalog"
    assert server._confirmed_text_meal_source_label(revised["estimate"]) == "一日樂食餐點"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U-NANJING'").fetchone()[0] == 3


def test_registered_cooked_multigrain_rice_180g_is_provisional_zero_ai_draft(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    server.handle_message(_text("ENTER-RICE", "記一餐"))
    server.handle_message(_text("RICE-180", "五穀飯 180 克"))

    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert all(text in rendered for text in (
        "尚未記錄", "五穀飯", "180 g", "292", "7.6", "1.6", "63.0",
        "一般參考值", "確認記錄", "修改", "取消",
    )), rendered
    assert all(text not in rendered for text in ("FatSecret", "TFDA", "使用者核准", "暫定來源")), rendered
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM food_catalog").fetchone()[0] == 1
        estimate = json.loads(conn.execute(
            "SELECT estimate_json FROM pending_text_meal_estimates WHERE source_message_id='RICE-180'"
        ).fetchone()[0])
    assert estimate["provenance"]["method"] == "official_reference"
    values = {
        key: estimate[key]["estimate"]
        for key in ("calories_kcal", "protein_g", "fat_g", "carbohydrate_g")
    }
    assert values["calories_kcal"] == 291.6
    assert values["protein_g"] == pytest.approx(7.56)
    assert values["fat_g"] == pytest.approx(1.62)
    assert values["carbohydrate_g"] == 63.0


def test_multigrain_rice_reference_rejects_ml_and_raw_state():
    from nutrition_reference import resolve_reference
    assert resolve_reference({"food_name": "五穀飯", "amount": 180, "unit": "ml"}) is None
    assert resolve_reference({
        "food_name": "五穀飯", "food_state": "生", "amount": 180, "unit": "g"
    }) is None
