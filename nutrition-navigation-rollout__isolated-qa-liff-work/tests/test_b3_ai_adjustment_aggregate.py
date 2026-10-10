import json
import os
import sqlite3
import sys

import pytest

os.environ.update({
    "APP_ENV": "legacy",
    "OPENAI_API_KEY": "test-no-network",
    "LINE_CHANNEL_ACCESS_TOKEN": "test-line-token-no-network",
    "LINE_CHANNEL_SECRET": "test-line-secret-no-network",
    "GOOGLE_CREDENTIALS": "{}",
    "SPREADSHEET_ID": "test-spreadsheet-no-network",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ENABLE_SCHEDULER": "false",
    "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
    "http_proxy": "", "https_proxy": "", "all_proxy": "",
})
CANDIDATE = os.environ.get("REVIEW_CANDIDATE", os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, CANDIDATE)

from meal_photo_system import (  # noqa: E402
    apply_meal_photo_action,
    apply_meal_photo_ai_adjustment,
    get_meal_photo_draft,
    save_meal_photo_draft,
)


def payload(items, *, cal=(400, 300, 500), protein=(25, 18, 32)):
    items = [dict(item) for item in items]
    for item in items:
        item["calories_kcal_range"] = {
            "min": max(0, item["calories_kcal"] - 10),
            "max": item["calories_kcal"] + 10,
        }
        item["protein_g_range"] = {
            "min": max(0, item["protein_g"] - 1),
            "max": item["protein_g"] + 1,
        }
    return {
        "status": "success", "image_type": "food_photo",
        "visible_items": [
            {"name": item["name"], "category": "unknown", "confidence": 0.8}
            for item in items
        ],
        "uncertain_items": [], "starch_visibility": "visible",
        "oil_sauce_status": "unknown", "observed_at_confidence": 0.9,
        "ai_estimate": {
            "items": items,
            "calories_kcal": {"estimate": cal[0], "min": cal[1], "max": cal[2]},
            "protein_g": {"estimate": protein[0], "min": protein[1], "max": protein[2]},
            "confidence": 0.75,
            "provenance": {"provider": "fixture", "model": "offline", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
        },
    }


def stage(conn, initial, source):
    token = save_meal_photo_draft(
        conn, user_id="U1", source_message_id=source, payload=initial,
        workflow_version="user_confirmed_ai_nutrition_v2",
    )
    draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    return token, draft


def remove_then_wait(conn, token, draft, name, suffix):
    removed_item = next(item for item in draft["estimate"]["estimate_items"] if item["name"] == name)
    removed = apply_meal_photo_action(
        conn, event_id=f"REMOVE-{suffix}", user_id="U1", token=token,
        expected_version=draft["version"], action="remove_item", value=removed_item["item_id"],
    )
    waiting = apply_meal_photo_action(
        conn, event_id=f"WAIT-{suffix}", user_id="U1", token=token,
        expected_version=removed["draft"]["version"], action="request_adjust",
    )
    return removed_item, removed, waiting


@pytest.mark.parametrize("correction", ["不要加豆漿", "上次說加豆漿但這次不要"])
def test_negative_or_quoted_add_word_does_not_authorize_tombstoned_soy(tmp_path, correction):
    original = payload([
        {"name": "米飯", "portion": "1碗", "calories_kcal": 200, "protein_g": 5},
        {"name": "豆漿", "portion": "1杯", "calories_kcal": 200, "protein_g": 20},
    ])
    with sqlite3.connect(tmp_path / "negative.db") as conn:
        token, draft = stage(conn, original, "NEG-" + str(abs(hash(correction))))
        _old, _removed, waiting = remove_then_wait(conn, token, draft, "豆漿", "NEG")
        revised = apply_meal_photo_ai_adjustment(
            conn, event_id="ADJUST-NEG-" + str(abs(hash(correction))), user_id="U1", token=token,
            expected_version=waiting["draft"]["version"], correction=correction,
            estimate_provider=lambda *_: original,
        )
    assert [item["name"] for item in revised["draft"]["estimate"]["estimate_items"]] == ["米飯"]


def test_alias_add_is_allowed_but_unrelated_removed_meat_stays_removed(tmp_path):
    initial = payload([
        {"name": "米飯", "portion": "1碗", "calories_kcal": 200, "protein_g": 5},
        {"name": "豬肉", "portion": "1份", "calories_kcal": 200, "protein_g": 20},
    ])
    provider = payload([
        {"name": "米飯", "portion": "半碗", "calories_kcal": 100, "protein_g": 2},
        {"name": "豬肉", "portion": "1份", "calories_kcal": 200, "protein_g": 20},
        {"name": "無糖豆漿", "portion": "1杯", "calories_kcal": 100, "protein_g": 9},
    ], cal=(400, 300, 500), protein=(31, 22, 40))
    with sqlite3.connect(tmp_path / "alias.db") as conn:
        token, draft = stage(conn, initial, "ALIAS")
        _old, _removed, waiting = remove_then_wait(conn, token, draft, "豬肉", "ALIAS")
        revised = apply_meal_photo_ai_adjustment(
            conn, event_id="ADJUST-ALIAS", user_id="U1", token=token,
            expected_version=waiting["draft"]["version"], correction="飯只吃一半，肉全吃，另豆漿",
            estimate_provider=lambda *_: provider,
        )
    names = [item["name"] for item in revised["draft"]["estimate"]["estimate_items"]]
    assert names == ["米飯", "無糖豆漿"]
    assert revised["draft"]["estimate"]["calories_kcal"] == 200
    assert revised["draft"]["estimate"]["protein_g"] == 11
    estimate = revised["draft"]["estimate"]
    assert estimate["calories_kcal_range"]["min"] <= 200 <= estimate["calories_kcal_range"]["max"]
    assert estimate["protein_g_range"]["min"] <= 11 <= estimate["protein_g_range"]["max"]
    assert sum(item["calories_kcal"] for item in estimate["estimate_items"]) == 200
    assert sum(item["protein_g"] for item in estimate["estimate_items"]) == 11


def test_explicit_readd_gets_new_identity_and_keeps_tombstone(tmp_path):
    original = payload([
        {"name": "米飯", "portion": "1碗", "calories_kcal": 200, "protein_g": 5},
        {"name": "豬肉", "portion": "1份", "calories_kcal": 200, "protein_g": 20},
    ])
    with sqlite3.connect(tmp_path / "readd.db") as conn:
        token, draft = stage(conn, original, "READD")
        old, _removed, waiting = remove_then_wait(conn, token, draft, "豬肉", "READD")
        revised = apply_meal_photo_ai_adjustment(
            conn, event_id="ADJUST-READD", user_id="U1", token=token,
            expected_version=waiting["draft"]["version"], correction="加回豬肉",
            estimate_provider=lambda *_: original,
        )
        tombstones = json.loads(conn.execute(
            "SELECT removed_items_json FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0])
    pork = next(item for item in revised["draft"]["estimate"]["estimate_items"] if item["name"] == "豬肉")
    assert pork["item_id"] != old["item_id"]
    assert any(item["item_id"] == old["item_id"] for item in tombstones)


def test_duplicate_name_remove_one_and_update_survivor_portion_preserves_survivor_id(tmp_path):
    original = payload([
        {"name": "豆腐", "portion": "半盒", "calories_kcal": 100, "protein_g": 10},
        {"name": "豆腐", "portion": "一盒", "calories_kcal": 300, "protein_g": 15},
    ])
    provider = payload([
        {"name": "豆腐", "portion": "三分之一盒", "calories_kcal": 80, "protein_g": 8},
        {"name": "豆腐", "portion": "一盒", "calories_kcal": 300, "protein_g": 15},
    ], cal=(380, 280, 480), protein=(23, 16, 30))
    with sqlite3.connect(tmp_path / "duplicates.db") as conn:
        token, draft = stage(conn, original, "DUP")
        items = draft["estimate"]["estimate_items"]
        survivor_id = items[0]["item_id"]
        removed = apply_meal_photo_action(
            conn, event_id="REMOVE-DUP", user_id="U1", token=token,
            expected_version=draft["version"], action="remove_item", value=items[1]["item_id"],
        )
        waiting = apply_meal_photo_action(
            conn, event_id="WAIT-DUP", user_id="U1", token=token,
            expected_version=removed["draft"]["version"], action="request_adjust",
        )
        revised = apply_meal_photo_ai_adjustment(
            conn, event_id="ADJUST-DUP", user_id="U1", token=token,
            expected_version=waiting["draft"]["version"], correction="豆腐改三分之一盒",
            estimate_provider=lambda *_: provider,
        )
    final_items = revised["draft"]["estimate"]["estimate_items"]
    assert len(final_items) == 1
    assert final_items[0]["item_id"] == survivor_id
    assert final_items[0]["portion"] == "三分之一盒"
    assert revised["draft"]["estimate"]["calories_kcal"] == 80
    assert revised["draft"]["estimate"]["protein_g"] == 8
    estimate = revised["draft"]["estimate"]
    assert estimate["calories_kcal_range"]["min"] <= 80 <= estimate["calories_kcal_range"]["max"]
    assert estimate["protein_g_range"]["min"] <= 8 <= estimate["protein_g_range"]["max"]
    assert sum(item["calories_kcal"] for item in final_items) == 80
    assert sum(item["protein_g"] for item in final_items) == 8


def test_reconciled_adjustment_confirms_but_tampered_reconciled_totals_fail_closed(tmp_path):
    initial = payload([
        {"name": "米飯", "portion": "1碗", "calories_kcal": 200, "protein_g": 5},
        {"name": "豬肉", "portion": "1份", "calories_kcal": 200, "protein_g": 20},
    ])
    provider = payload([
        {"name": "米飯", "portion": "半碗", "calories_kcal": 100, "protein_g": 2},
        {"name": "豬肉", "portion": "1份", "calories_kcal": 200, "protein_g": 20},
        {"name": "無糖豆漿", "portion": "1杯", "calories_kcal": 100, "protein_g": 9},
    ], cal=(400, 300, 500), protein=(31, 22, 40))
    with sqlite3.connect(tmp_path / "confirm-gate.db") as conn:
        token, draft = stage(conn, initial, "CONFIRM-GATE")
        _old, _removed, waiting = remove_then_wait(conn, token, draft, "豬肉", "CONFIRM-GATE")
        revised = apply_meal_photo_ai_adjustment(
            conn, event_id="ADJUST-CONFIRM-GATE", user_id="U1", token=token,
            expected_version=waiting["draft"]["version"], correction="飯半碗，另豆漿",
            estimate_provider=lambda *_: provider,
        )
        confirmed = apply_meal_photo_action(
            conn, event_id="CONFIRM-GOOD", user_id="U1", token=token,
            expected_version=revised["draft"]["version"], action="confirm_estimate",
        )
        assert confirmed["draft"]["status"] == "user_confirmed"

        token2, draft2 = stage(conn, initial, "TAMPER-GATE")
        _old, _removed, waiting2 = remove_then_wait(conn, token2, draft2, "豬肉", "TAMPER-GATE")
        revised2 = apply_meal_photo_ai_adjustment(
            conn, event_id="ADJUST-TAMPER-GATE", user_id="U1", token=token2,
            expected_version=waiting2["draft"]["version"], correction="飯半碗，另豆漿",
            estimate_provider=lambda *_: provider,
        )
        observed_json, estimate_json = conn.execute(
            "SELECT observed_payload_json,estimate_json FROM pending_meal_photo_drafts WHERE token=?",
            (token2,),
        ).fetchone()
        observed, estimate = json.loads(observed_json), json.loads(estimate_json)
        observed["ai_estimate"]["calories_kcal"] = {"estimate": 999, "min": 900, "max": 1100}
        estimate["calories_kcal"] = 999
        estimate["calories_kcal_range"] = {
            "min": 900, "max": 1100, "basis": "ai_vision_estimate_range_v1",
        }
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET observed_payload_json=?,estimate_json=? WHERE token=?",
            (json.dumps(observed), json.dumps(estimate), token2),
        )
        conn.commit()
        log_count_before = conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0]
        with pytest.raises(ValueError, match="明細與總營養不一致"):
            apply_meal_photo_action(
                conn, event_id="CONFIRM-TAMPERED", user_id="U1", token=token2,
                expected_version=revised2["draft"]["version"], action="confirm_estimate",
            )
        current = get_meal_photo_draft(conn, user_id="U1", token=token2)
        assert current["status"] == "estimated"
        assert current["version"] == revised2["draft"]["version"]
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone() == (log_count_before,)


def test_late_expired_state_wins_over_malformed_provider_payload(tmp_path):
    initial = payload([
        {"name": "米飯", "portion": "1碗", "calories_kcal": 200, "protein_g": 5},
        {"name": "豬肉", "portion": "1份", "calories_kcal": 200, "protein_g": 20},
    ])
    with sqlite3.connect(tmp_path / "late.db") as conn:
        token, draft = stage(conn, initial, "LATE")
        waiting = apply_meal_photo_action(
            conn, event_id="WAIT-LATE", user_id="U1", token=token,
            expected_version=draft["version"], action="request_adjust",
        )

        def expire_then_return_bad(*_args):
            conn.execute(
                "UPDATE pending_meal_photo_drafts SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?",
                (token,),
            )
            conn.commit()
            return {"not": "a valid payload"}

        with pytest.raises(ValueError, match="已取消或餐點草稿已更新"):
            apply_meal_photo_ai_adjustment(
                conn, event_id="ADJUST-LATE", user_id="U1", token=token,
                expected_version=waiting["draft"]["version"], correction="飯半碗",
                estimate_provider=expire_then_return_bad,
            )
        current = get_meal_photo_draft(conn, user_id="U1", token=token, allow_expired=True)
    assert current["status"] == "expired"
    assert current["version"] == waiting["draft"]["version"]
    assert current["payload"] == {}
    assert current["estimate"] == {}
