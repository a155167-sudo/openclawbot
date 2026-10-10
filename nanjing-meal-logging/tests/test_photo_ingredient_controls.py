import json
import sqlite3
from types import SimpleNamespace

import server
from meal_photo_system import build_meal_photo_estimate_bubble, get_meal_photo_draft, save_meal_photo_draft


def _payload(items=None):
    items = items or [
        {"name": "米飯", "portion": "1碗", "calories_kcal": 120, "protein_g": 2},
        {"name": "豬肉", "portion": "1掌", "calories_kcal": 300, "protein_g": 22},
        {"name": "炒蛋", "portion": "1顆", "calories_kcal": 80, "protein_g": 6},
        {"name": "蔬菜", "portion": "半碗", "calories_kcal": 20, "protein_g": 1},
    ]
    items = [dict(item) for item in items]
    for item in items:
        item.setdefault("calories_kcal_range", {
            "min": item["calories_kcal"], "max": item["calories_kcal"],
        })
        item.setdefault("protein_g_range", {
            "min": item["protein_g"], "max": item["protein_g"],
        })
    return {
        "status": "success", "image_type": "food_photo",
        "visible_items": [{"name": item["name"], "category": "unknown", "confidence": 0.8} for item in items],
        "uncertain_items": [], "starch_visibility": "visible", "oil_sauce_status": "unknown",
        "observed_at_confidence": 0.9,
        "ai_estimate": {
            "items": items,
            "calories_kcal": {"estimate": 520, "min": 400, "max": 650},
            "protein_g": {"estimate": 31, "min": 22, "max": 40},
            "confidence": 0.75,
            "provenance": {"provider": "fixture", "model": "offline", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
        },
    }


def _postback(data, event_id, user_id="U1"):
    return SimpleNamespace(postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id=user_id),
                           reply_token=f"reply-{event_id}", webhook_event_id=event_id, timestamp=1)


def _text(text, message_id, user_id="U1"):
    return SimpleNamespace(message=SimpleNamespace(id=message_id, text=text), source=SimpleNamespace(user_id=user_id),
                           reply_token=f"reply-{message_id}")


def _actions(node):
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("action"), dict):
            found.append(node["action"])
        for value in node.values():
            found.extend(_actions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_actions(value))
    return found


def _action(card, label, occurrence=0):
    matches = [action for action in _actions(card) if action.get("label") == label]
    assert len(matches) > occurrence, (label, matches)
    return matches[occurrence]


def _setup(tmp_path, monkeypatch, items=None):
    db = tmp_path / "photo-controls.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    server.init_db()
    image_dir = tmp_path / "nutrition_images"
    image_dir.mkdir()
    image_name = "a" * 32 + ".jpg"
    (image_dir / image_name).write_bytes(b"\xff\xd8\xff" + b"0" * 200)
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-CONTROLS", payload=_payload(items),
            source_image_ref=f"nutrition-image:{image_name}", meal_slot="午餐",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        conn.execute(
            """INSERT INTO usage
               (user_id,status,expiry_date,remaining_meals,remaining_chat_quota,
                daily_chat_limit,last_date)
               VALUES ('U1','vip','2099-12-31',20,10,10,?)""",
            (server.tw_today().isoformat(),),
        )
        conn.commit()
    server.processed_messages.clear()
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    monkeypatch.setattr(server, "get_ai_response_with_memory", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("generic AI forbidden")))
    return db, token, draft, replies


def test_real_rendered_remove_and_add_routes_update_one_draft_then_confirm_once(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    first_card = build_meal_photo_estimate_bubble(draft)
    assert _action(first_card, "移除")["type"] == "postback"
    remove_data = _action(first_card, "移除", occurrence=1)["data"]  # 豬肉
    assert ":remove_item:" in remove_data

    server.handle_postback_event(_postback(remove_data, "REMOVE-PORK"))
    updated = replies[-1].as_json_dict()["contents"]
    rendered = json.dumps(updated, ensure_ascii=False)
    assert "豬肉" not in rendered
    assert "熱量：約220 kcal（100～350）" in rendered
    assert "蛋白質：約9 g（0～18）" in rendered
    assert _action(updated, "➕ 新增食材")["type"] == "postback"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0

    server.handle_postback_event(_postback(_action(updated, "➕ 新增食材")["data"], "REQUEST-ADD"))
    add_prompt = replies[-1].as_json_dict()
    assert add_prompt["text"] == "請輸入要新增的食材與份量，可一次輸入多項。"
    assert _action(add_prompt, "取消新增")["displayText"] == "確認取消新增食材"
    server.handle_message(_text("無糖豆漿｜1杯｜100｜9", "ADD-SOY"))
    added = replies[-1].as_json_dict()["contents"]
    added_text = json.dumps(added, ensure_ascii=False)
    assert "無糖豆漿：1杯（約100 kcal／蛋白質9 g）" in added_text
    assert "熱量：約320 kcal（200～450）" in added_text
    assert "蛋白質：約18 g（9～27）" in added_text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0

    confirm = _action(added, "確認記錄")
    server.handle_postback_event(_postback(confirm["data"], "CONFIRM-ONCE"))
    assert not getattr(replies[-1], "text", "").startswith("⚠️"), getattr(replies[-1], "text", "")
    server.handle_postback_event(_postback(confirm["data"], "CONFIRM-ONCE"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM meal_photo_events WHERE action='confirm_estimate'").fetchone()[0] == 1
        nutrition = json.loads(conn.execute("SELECT nutrition_snapshot_json FROM food_logs").fetchone()[0])
        assert nutrition == {"calories_kcal": 320.0, "protein_g": 18.0}


def test_duplicate_names_use_stable_item_ids_and_stale_or_foreign_buttons_fail_closed(tmp_path, monkeypatch):
    duplicate = [
        {
            "name": "豆腐", "portion": "半盒", "calories_kcal": 100, "protein_g": 10,
            "calories_kcal_range": {"min": 80, "max": 120},
            "protein_g_range": {"min": 8, "max": 12},
        },
        {
            "name": "豆腐", "portion": "一盒", "calories_kcal": 200, "protein_g": 20,
            "calories_kcal_range": {"min": 160, "max": 240},
            "protein_g_range": {"min": 16, "max": 24},
        },
    ]
    db, token, draft, replies = _setup(tmp_path, monkeypatch, duplicate)
    card = build_meal_photo_estimate_bubble(draft)
    buttons = [a for a in _actions(card) if a.get("label") == "移除"]
    assert len(buttons) == 2 and buttons[0]["data"] != buttons[1]["data"]

    server.handle_postback_event(_postback(buttons[1]["data"], "REMOVE-SECOND"))
    latest = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "豆腐：半盒" in latest and "豆腐：一盒" not in latest
    server.handle_postback_event(_postback(buttons[0]["data"], "STALE-REMOVE"))
    assert "已更新" in replies[-1].text
    server.handle_postback_event(_postback(_action(build_meal_photo_estimate_bubble(draft), "➕ 新增食材")["data"], "FOREIGN", user_id="U2"))
    assert "找不到" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_add_malformed_or_invalid_nutrition_keeps_original_estimate_and_pending_draft(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    add = _action(build_meal_photo_estimate_bubble(draft), "➕ 新增食材")
    server.handle_postback_event(_postback(add["data"], "REQUEST-MISSING"))
    server.handle_message(_text("酪梨｜半顆｜", "MALFORMED-NUTRITION"))
    assert "請使用2欄或4欄格式" in replies[-1].text
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "awaiting_item_name"
        assert current["estimate"]["calories_kcal"] == 520
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
    server.handle_message(_text("酪梨｜半顆｜不是數字｜3", "BAD-NUTRITION"))
    assert "熱量與蛋白質必須是合理數字" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["estimate"]["calories_kcal"] == 520


def test_remove_cannot_create_empty_confirmable_meal(tmp_path, monkeypatch):
    only = [{"name": "米飯", "portion": "1碗", "calories_kcal": 520, "protein_g": 31}]
    db, _token, draft, replies = _setup(tmp_path, monkeypatch, only)
    server.handle_postback_event(_postback(_action(build_meal_photo_estimate_bubble(draft), "移除")["data"], "REMOVE-LAST"))
    assert "至少要保留一項食材" in replies[-1].text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def _ai_item_estimate(name="酪梨"):
    return {
        "food_name": name,
        "portion_assumption": "半顆",
        "basis_amount": 0.5,
        "basis_unit": "顆",
        "calories_kcal": {"estimate": 120, "min": 90, "max": 150},
        "protein_g": {"estimate": 2, "min": 1, "max": 3},
        "fat_g": {"estimate": 10, "min": 8, "max": 12},
        "carbohydrate_g": {"estimate": 6, "min": 4, "max": 8},
        "provenance": {"provider": "test", "model": "mock", "method": "text_meal_estimate"},
    }


def test_missing_nutrition_uses_fenced_ai_child_estimate_only_in_photo_draft(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda request: calls.append(dict(request)) or _ai_item_estimate())
    add = _action(build_meal_photo_estimate_bubble(draft), "➕ 新增食材")
    server.handle_postback_event(_postback(add["data"], "REQUEST-AI-ADD"))
    server.handle_message(_text("酪梨｜半顆", "AI-ADD-ITEM"))

    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert len(calls) == 1
    assert "酪梨：半顆（AI估算約120 kcal／90～150；蛋白質2 g／1～3）" in rendered
    assert "熱量：約640 kcal（490～800）" in rendered
    assert "蛋白質：約33 g（23～43）" in rendered
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "estimated"
        assert current["estimate"]["calories_kcal_range"]["min"] == 490.0
        assert current["estimate"]["calories_kcal_range"]["max"] == 800.0
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM pending_text_meal_estimates").fetchone()[0] == "consumed_by_photo_draft"

    confirm = _action(replies[-1].as_json_dict()["contents"], "確認記錄")
    confirm_event = _postback(confirm["data"], "CONFIRM-AI-ADD")
    server.handle_postback_event(confirm_event)
    server.handle_postback_event(confirm_event)
    assert len(calls) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
        snapshot = json.loads(conn.execute("SELECT nutrition_snapshot_json FROM food_logs").fetchone()[0])
        assert snapshot == {"calories_kcal": 640.0, "protein_g": 33.0}


def test_missing_nutrition_prefers_reliable_owner_catalog_without_ai_or_quota(tmp_path, monkeypatch):
    db, _token, draft, replies = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
        now = server.tw_now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                exchange_json,exchange_review_status,fingerprint,original_image_ref,
                recognition_confidence,verification_status,created_at,updated_at)
               VALUES ('private-avocado','酪梨','','','user_private_food','U1','private',
                       1,'顆',1,?,'{}','{}','pending_review','fp','',1,
                       'user_confirmed',?,?)""",
            (json.dumps({"calories_kcal": 110, "protein_g": 1.5}), now, now),
        )
        conn.commit()
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda _r: (_ for _ in ()).throw(AssertionError("AI forbidden")))
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _u: (_ for _ in ()).throw(AssertionError("quota forbidden")))
    add = _action(build_meal_photo_estimate_bubble(draft), "➕ 新增食材")
    server.handle_postback_event(_postback(add["data"], "REQUEST-CATALOG-ADD"))
    server.handle_message(_text("酪梨｜半顆", "CATALOG-ADD-ITEM"))

    rendered = json.dumps(replies[-1].as_json_dict(), ensure_ascii=False)
    assert "酪梨：半顆（私人食材資料約55 kcal／蛋白質0.75 g）" in rendered
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_missing_nutrition_provider_error_preserves_photo_draft_and_pending_input(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda _request: (_ for _ in ()).throw(RuntimeError("offline provider failure")))
    add = _action(build_meal_photo_estimate_bubble(draft), "➕ 新增食材")
    server.handle_postback_event(_postback(add["data"], "REQUEST-FAILED-ADD"))
    server.handle_message(_text("酪梨｜半顆", "FAILED-ADD-ITEM"))

    assert "結果待確認" in replies[-1].text and "原餐點草稿未變" in replies[-1].text
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["status"] == "awaiting_item_name"
        assert current["version"] == draft["version"] + 1
        assert current["estimate"]["calories_kcal"] == 520
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 9
        assert conn.execute("SELECT status FROM photo_ingredient_batch_quota_ops").fetchone()[0] == "provider_unknown"


def test_ai_add_late_result_cannot_restore_a_newer_photo_draft(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    add = _action(build_meal_photo_estimate_bubble(draft), "➕ 新增食材")
    server.handle_postback_event(_postback(add["data"], "REQUEST-STALE-ADD"))

    def provider(_request):
        with sqlite3.connect(db) as conn:
            conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET version=version+1,status='estimated'
                   WHERE token=? AND user_id='U1'""",
                (token,),
            )
            conn.commit()
        return _ai_item_estimate()

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    server.handle_message(_text("酪梨｜半顆", "STALE-AI-ADD"))

    assert "結果待確認" in replies[-1].text
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert current["version"] == draft["version"] + 2
        assert all(item["name"] != "酪梨" for item in current["estimate"]["estimate_items"])
        assert conn.execute("SELECT status FROM pending_text_meal_estimates").fetchone()[0] == "pending"
        assert conn.execute("SELECT status FROM photo_ingredient_batch_quota_ops").fetchone()[0] == "provider_unknown"
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
