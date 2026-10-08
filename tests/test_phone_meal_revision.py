import json
import sqlite3
import pytest
import server
from tests.test_nanjing_meal_logging_flow import _setup, _postback, _text_event, _postback_actions


def _ai_draft(tmp_path, monkeypatch):
    db, _ = _setup(tmp_path, monkeypatch)
    estimate = {
        "schema_version": "text-meal-v2", "food_name": "豆漿",
        "portion_assumption": "500 ml", "basis_amount": 500, "basis_unit": "ml",
        "calories_kcal": {"estimate": 250, "min": 100, "max": 300, "unit": "kcal"},
        "protein_g": {"estimate": 12, "min": 10, "max": 20, "unit": "g"},
        "fat_g": {"estimate": 7, "min": 4, "max": 8, "unit": "g"},
        "carbohydrate_g": {"estimate": 30, "min": 20, "max": 40, "unit": "g"},
        "assessment": {"requires_correction": False},
        "provenance": {"provider": "fake", "model": "fake", "method": "text_meal_estimate"},
    }
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda *_a, **_k: estimate)
    monkeypatch.setattr(server, "_charge_text_meal_estimate_quota", lambda *_a, **_k: True)
    draft = server.create_text_meal_estimate_draft(user_id="U1", message_id="M1", request={
        "food_name": "豆漿", "amount": 500, "unit": "ml", "meal_slot": "午餐"})
    return db, draft


def test_phone_card_has_slot_and_exact_four_actions(tmp_path, monkeypatch):
    _db, draft = _ai_draft(tmp_path, monkeypatch)
    card = server.build_text_meal_estimate_flex(draft).as_json_dict()
    text = json.dumps(card, ensure_ascii=False)
    assert "餐別：午餐" in text
    labels = [x["action"]["label"] for x in card["contents"]["footer"]["contents"]]
    # Historical ID preserved; approved Round2 contract replaces four buttons.
    assert labels == ["確認記錄", "修改", "取消"]
    edit = card["contents"]["footer"]["contents"][1]["action"]
    assert edit["type"] == "uri"
    assert "view=meal-edit" in edit["uri"]
    assert draft["token"] in edit["uri"]


def test_confirm_uses_true_range_midpoint_not_provider_estimate(tmp_path, monkeypatch):
    db, draft = _ai_draft(tmp_path, monkeypatch)
    result = server.apply_text_meal_estimate_action(user_id="U1", token=draft["token"], expected_version=1, action="confirm")
    with sqlite3.connect(db) as conn:
        nutrition = json.loads(conn.execute("select nutrition_snapshot_json from food_logs where log_id=?", (result["log_id"],)).fetchone()[0])
    assert nutrition == {"calories_kcal": 200.0, "protein_g": 15.0, "fat_g": 6.0, "carbohydrate_g": 30.0}


def test_custom_amount_scales_from_original_basis_without_provider(tmp_path, monkeypatch):
    _db, draft = _ai_draft(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda *_a, **_k: calls.append(1))
    revised = server.apply_text_meal_custom_input(user_id="U1", token=draft["token"], expected_version=1, mode="amount", text="400 ml")
    assert calls == []
    assert revised["version"] == 2
    assert revised["estimate"]["basis_amount"] == 400
    assert revised["estimate"]["calories_kcal"]["min"] == 80
    assert revised["estimate"]["calories_kcal"]["max"] == 240


def test_manual_values_preserve_ai_original_and_do_not_copy_unknown_macros(tmp_path, monkeypatch):
    _db, draft = _ai_draft(tmp_path, monkeypatch)
    revised = server.apply_text_meal_custom_input(user_id="U1", token=draft["token"], expected_version=1, mode="nutrition", text="180 大卡 16 克")
    assert revised["estimate"]["calories_kcal"]["estimate"] == 180
    assert revised["estimate"]["protein_g"]["estimate"] == 16
    assert revised["estimate"]["fat_g"] is None
    assert revised["estimate"]["carbohydrate_g"] is None
    provenance = revised["estimate"]["provenance"]
    assert provenance["method"] == "customer_revision"
    assert provenance["original_estimate"]["calories_kcal"]["estimate"] == 250


def test_new_drafts_use_rollback_opaque_40_hex_tokens(tmp_path, monkeypatch):
    _db, draft = _ai_draft(tmp_path, monkeypatch)
    assert len(draft["token"]) == 40
    assert all(character in "0123456789abcdef" for character in draft["token"])
    actions = _postback_actions(server.build_text_meal_estimate_flex(draft))
    assert actions and all(action.startswith("tmest:v3:") for action in actions)


def test_editing_legacy_32_token_rotates_atomically_and_fences_old_card(tmp_path, monkeypatch):
    db, draft = _ai_draft(tmp_path, monkeypatch)
    legacy_token = "a" * 32
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_text_meal_estimates SET token=? WHERE token=?",
            (legacy_token, draft["token"]),
        )
        conn.execute(
            "UPDATE text_meal_provider_attempts SET token=? WHERE token=?",
            (legacy_token, draft["token"]),
        )
        quota_before = conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U1'"
        ).fetchone()
        conn.commit()

    revised = server.apply_text_meal_custom_input(
        user_id="U1", token=legacy_token, expected_version=1,
        mode="nutrition", text="180 大卡 16 克",
    )

    assert len(revised["token"]) == 40 and revised["token"] != legacy_token
    assert revised["version"] == 2
    assert all(
        action.startswith(f"tmest:v3:{revised['token']}:2:")
        for action in _postback_actions(server.build_text_meal_estimate_flex(revised))
    )
    with pytest.raises(ValueError, match="找不到"):
        server.apply_text_meal_estimate_action(
            user_id="U1", token=legacy_token, expected_version=1, action="confirm"
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_text_meal_estimates WHERE source_message_id='M1'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U1'"
        ).fetchone() == quota_before
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_original_ai_estimate_is_immutable_across_amount_and_repeated_manual_edits(tmp_path, monkeypatch):
    _db, draft = _ai_draft(tmp_path, monkeypatch)
    amount = server.apply_text_meal_custom_input(
        user_id="U1", token=draft["token"], expected_version=1,
        mode="amount", text="400 ml",
    )
    first = server.apply_text_meal_custom_input(
        user_id="U1", token=amount["token"], expected_version=2,
        mode="nutrition", text="180 大卡 16 克",
    )
    second = server.apply_text_meal_custom_input(
        user_id="U1", token=first["token"], expected_version=3,
        mode="nutrition", text="175 大卡 15 克",
    )
    original = second["estimate"]["provenance"]["original_estimate"]
    assert original["calories_kcal"]["estimate"] == 250
    assert original["protein_g"]["estimate"] == 12
    assert second["estimate"]["fat_g"] is None
    assert second["estimate"]["carbohydrate_g"] is None


def test_custom_input_rejects_expired_foreign_and_nonfinite_or_negative_values(tmp_path, monkeypatch):
    db, draft = _ai_draft(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="找不到"):
        server.apply_text_meal_custom_input(
            user_id="U-FOREIGN", token=draft["token"], expected_version=1,
            mode="nutrition", text="180 大卡 16 克",
        )
    for invalid in ("-1 大卡 16 克", "nan 大卡 16 克", "inf 大卡 16 克"):
        with pytest.raises(ValueError, match="請輸入熱量與蛋白質"):
            server.apply_text_meal_custom_input(
                user_id="U1", token=draft["token"], expected_version=1,
                mode="nutrition", text=invalid,
            )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_text_meal_estimates SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
            (draft["token"],),
        )
        conn.commit()
    with pytest.raises(ValueError, match="逾時"):
        server.apply_text_meal_custom_input(
            user_id="U1", token=draft["token"], expected_version=1,
            mode="amount", text="400 ml",
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_finite_nonnegative_anomaly_requires_second_confirm_instead_of_blocking(tmp_path, monkeypatch):
    _db, draft = _ai_draft(tmp_path, monkeypatch)
    with sqlite3.connect(server.DB_PATH) as conn:
        estimate = draft["estimate"]
        estimate["assessment"] = {"requires_correction": True, "status": "inconsistent"}
        conn.execute("update pending_text_meal_estimates set estimate_json=? where token=?",
                     (json.dumps(estimate, ensure_ascii=False), draft["token"]))
        conn.commit()
    first = server.apply_text_meal_estimate_action(user_id="U1", token=draft["token"], expected_version=1, action="confirm")
    assert first["kind"] == "preview"
    assert first["draft"]["version"] == 2
    second = server.apply_text_meal_estimate_action(user_id="U1", token=draft["token"], expected_version=2, action="confirm")
    assert second["kind"] == "confirmed"


def test_real_handlers_amount_followup_then_confirm_reply_record_card_before_dashboard(tmp_path, monkeypatch):
    db, replies = _setup(tmp_path, monkeypatch)
    estimate = {
        "schema_version": "text-meal-v2", "food_name": "豆漿", "portion_assumption": "500 ml",
        "basis_amount": 500, "basis_unit": "ml",
        "calories_kcal": {"estimate": 250, "min": 100, "max": 300},
        "protein_g": {"estimate": 12, "min": 10, "max": 20},
        "fat_g": {"estimate": 7, "min": 4, "max": 8},
        "carbohydrate_g": {"estimate": 30, "min": 20, "max": 40},
        "assessment": {"requires_correction": False},
        "provenance": {"provider": "fake", "model": "fake", "method": "text_meal_estimate"},
    }
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda *_a, **_k: estimate)
    monkeypatch.setattr(server, "_charge_text_meal_estimate_quota", lambda *_a, **_k: True)
    draft = server.create_text_meal_estimate_draft(user_id="U-NANJING", message_id="PHONE-1", request={
        "food_name": "豆漿", "amount": 500, "unit": "ml", "meal_slot": "午餐"})
    card = server.build_text_meal_estimate_flex(draft)
    # New cards open LIFF. Preserve this real-handler regression for an old
    # already-issued chat-edit callback rather than fabricate a new UI button.
    assert "view=meal-edit" in json.dumps(card.as_json_dict())
    amount_action = f"tmest:v3:{draft['token']}:{draft['version']}:amount"
    server.handle_postback_event(_postback(amount_action, "AMOUNT-START"))
    state = server.get_daily_food_edit_state("U-NANJING")
    assert state["input_type"] == "text_meal_amount"
    server._handle_message_impl(_text_event("AMOUNT-TEXT", "400 ml"))
    revised = server.get_text_meal_estimate_draft("U-NANJING", draft["token"])
    assert revised["estimate"]["basis_amount"] == 400
    confirm = next(x for x in _postback_actions(replies[-1]) if x.endswith(":confirm"))
    server.handle_postback_event(_postback(confirm, "CONFIRM"))
    assert isinstance(replies[-1], list) and len(replies[-1]) == 2
    assert "記錄成功" in replies[-1][0].alt_text
    assert replies[-1][1].text == "南京今日總覽（四鈕不變）"
    with sqlite3.connect(db) as conn:
        row = conn.execute("select consumed_amount,nutrition_snapshot_json from food_logs").fetchone()
    assert row[0] == 400
    assert json.loads(row[1])["calories_kcal"] == 160


def test_today_detail_edit_keeps_ai_original_and_customer_revision_history(tmp_path, monkeypatch):
    db, draft = _ai_draft(tmp_path, monkeypatch)
    revised = server.apply_text_meal_custom_input(
        user_id="U1", token=draft["token"], expected_version=1,
        mode="nutrition", text="180 大卡 16 克",
    )
    confirmed = server.apply_text_meal_estimate_action(
        user_id="U1", token=draft["token"], expected_version=revised["version"], action="confirm")
    server.apply_daily_food_log_edit(
        user_id="U1", log_id=confirmed["log_id"], expected_version=1,
        event_id="TODAY-DETAIL-EDIT", action="correct_nutrition",
        field="calories_kcal", value=175,
    )
    with sqlite3.connect(db) as conn:
        current, history = conn.execute(
            "select nutrition_snapshot_json,original_nutrition_snapshot_json from food_logs where log_id=?",
            (confirmed["log_id"],),
        ).fetchone()
    assert json.loads(current)["calories_kcal"] == 175
    metadata = json.loads(history)
    assert metadata["estimate_metadata"]["provenance"]["method"] == "customer_revision"
    assert metadata["estimate_metadata"]["provenance"]["original_estimate"]["calories_kcal"]["estimate"] == 250
