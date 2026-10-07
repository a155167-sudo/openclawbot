import json
import sqlite3
from datetime import timedelta

import pytest

import server
from meal_photo_system import build_meal_photo_estimate_bubble, get_meal_photo_draft
from tests.test_photo_ingredient_controls import _action, _postback, _text
from tests.test_photo_natural_ingredient_batch import _estimate, _setup


def _install_usage(db, quota=10):
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS usage (
               user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER,
               remaining_meals INTEGER, last_date TEXT, status TEXT,
               expiry_date TEXT, daily_chat_limit INTEGER)"""
        )
        conn.execute(
            # Positive AI cases need the same native VIP/expiry contract as the
            # real membership writer; 'active' with no expiry is not authorized.
            "INSERT OR REPLACE INTO usage VALUES ('U1',?,?,?,'vip','2099-12-31',?)",
            (quota, 10, server.tw_today().isoformat(), quota),
        )
        conn.commit()


def _enter_add(draft, replies, message_id="REQUEST-SINGLE-QUOTA"):
    card = build_meal_photo_estimate_bubble(draft)
    server.handle_postback_event(
        _postback(_action(card, "➕ 新增食材")["data"], message_id)
    )
    rendered = replies[-1].as_json_dict()
    assert rendered["text"] == "請輸入要新增的食材與份量，可一次輸入多項。"
    assert _action(rendered, "取消新增")["displayText"] == "確認取消新增食材"


def test_four_ai_items_with_only_one_quota_succeed_and_charge_once(tmp_path, monkeypatch):
    # Photo providers may round item rows independently from the aggregate.
    # Exercise that production shape while proving the batch still charges once.
    rounded_details = [
        {"name": "米飯", "portion": "1碗", "calories_kcal": 80, "protein_g": 2},
        {"name": "豬肉", "portion": "1掌", "calories_kcal": 220, "protein_g": 22},
        {"name": "炒蛋", "portion": "1顆", "calories_kcal": 60, "protein_g": 6},
        {"name": "蔬菜", "portion": "半碗", "calories_kcal": 10, "protein_g": 1},
    ]
    db, token, draft, replies = _setup(tmp_path, monkeypatch, rounded_details)
    _install_usage(db, quota=1)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    calls = []
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda req: calls.append(req["food_name"]) or _estimate(req),
    )
    _enter_add(draft, replies)

    server.handle_message(_text("木耳20g、番茄20g、金針菇20g、青菜20g", "AI4-ONE-QUOTA"))

    assert calls == ["木耳", "番茄", "金針菇", "青菜"]
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["status"] == "estimated"
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status"
        ).fetchall() == [("charged", 1)]
        assert conn.execute(
            "SELECT status FROM photo_ingredient_batch_quota_ops"
        ).fetchall() == [("committed",)]


def test_all_catalog_items_charge_zero_and_mixed_batch_charges_one(tmp_path, monkeypatch):
    db, _token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req["food_name"]) or _estimate(req))
    with sqlite3.connect(db) as conn:
        for food_id, name in (("known-a", "已知甲"), ("known-b", "已知乙")):
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (food_id, name, "", "", "user_private_food", "U1", "private", 100, "g", 1,
                 json.dumps({"calories_kcal": 50, "protein_g": 5}), "{}", "{}", "pending",
                 food_id, "", 1, "user_confirmed", "2026-01-01", "2026-01-01"),
            )
        conn.commit()
    _enter_add(draft, replies)
    server.handle_message(_text("已知甲20g、已知乙20g", "KNOWN-ZERO"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM photo_ingredient_batch_quota_ops").fetchone()[0] == 0
    assert calls == []

    current = get_meal_photo_draft(sqlite3.connect(db), user_id="U1", token=draft["token"])
    card = build_meal_photo_estimate_bubble(current)
    server.handle_postback_event(_postback(_action(card, "➕ 新增食材")["data"], "REQUEST-MIXED"))
    server.handle_message(_text("已知甲40g、未知20g", "MIXED-ONE"))
    duplicate_prompt = replies[-1].as_json_dict()
    assert duplicate_prompt["text"] == "這餐已有已知甲，你想怎麼處理？"
    server.handle_postback_event(
        _postback(_action(duplicate_prompt, "修改原份量")["data"], "MIXED-CHOOSE-MODIFY")
    )
    assert calls == ["未知"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 1


def test_second_provider_failure_refunds_once_then_new_batch_success_has_one_net_charge(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    should_fail = {"value": True}
    calls = []

    def provider(req):
        calls.append(req["food_name"])
        if should_fail["value"] and req["food_name"] == "壞掉":
            raise RuntimeError("provider failed")
        return _estimate(req)

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", provider)
    _enter_add(draft, replies)
    waiting_version = draft["version"] + 1
    server.handle_message(_text("木耳20g、壞掉20g", "FAILED-BATCH"))
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (current["status"], current["version"]) == ("awaiting_item_name", waiting_version)
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 2
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status"
        ).fetchall() == [("refunded", 1)]

    should_fail["value"] = False
    server.handle_message(_text("木耳20g、青菜20g", "RETRY-AS-NEW-BATCH"))
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["status"] == "estimated"
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status ORDER BY status"
        ).fetchall() == [("charged", 1), ("refunded", 1)]


def test_quota_denial_happens_before_any_provider_call(tmp_path, monkeypatch):
    db, _token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=0)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))
    _enter_add(draft, replies)
    server.handle_message(_text("木耳20g、青菜20g", "NO-QUOTA"))
    assert calls == []
    assert "估算失敗" in replies[-1].text
    assert "已扣" not in replies[-1].text


def test_active_batch_claim_is_single_flight_and_forged_child_scope_cannot_run_provider(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    parsed = server.parse_photo_ingredient_batch("木耳20g、青菜20g")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="CONCURRENT", parsed_items=parsed,
        )
        with pytest.raises(ValueError, match="正在估算"):
            server._claim_photo_ingredient_batch_quota(
                conn, user_id="U1", token=token, expected_version=draft["version"],
                message_id="CONCURRENT", parsed_items=parsed,
            )
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))
    with pytest.raises(ValueError, match="額度範圍無效"):
        server.create_text_meal_estimate_draft(
            user_id="FOREIGN", message_id=f"photo-add-batch:{token}:{draft['version']}:CONCURRENT:0",
            request={"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""},
            quota_batch_key=batch_key, quota_batch_owner=owner,
        )
    assert calls == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 1


def test_crash_after_first_child_reuses_child_and_keeps_one_net_charge(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    calls = []

    def crashing_provider(req):
        calls.append(req["food_name"])
        if req["food_name"] == "青菜":
            raise KeyboardInterrupt("worker died")
        return _estimate(req)

    monkeypatch.setattr(server, "estimate_text_meal_nutrition", crashing_provider)
    _enter_add(draft, replies, "REQUEST-CRASH")
    event = _text("木耳20g、青菜20g", "CRASH-BATCH")
    with pytest.raises(KeyboardInterrupt, match="worker died"):
        server.handle_message(event)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_text_meal_estimates WHERE status='pending'"
        ).fetchone()[0] == 1

    clock[0] += timedelta(seconds=server.TEXT_MEAL_ESTIMATE_LEASE_SECONDS + 1)
    server.processed_messages.clear()  # new process has no in-memory duplicate cache
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda req: calls.append(req["food_name"]) or _estimate(req),
    )
    server.handle_message(event)
    assert calls == ["木耳", "青菜", "青菜"]
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["status"] == "estimated"
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status ORDER BY status"
        ).fetchall() == [("charged", 1), ("refunded", 1)]
