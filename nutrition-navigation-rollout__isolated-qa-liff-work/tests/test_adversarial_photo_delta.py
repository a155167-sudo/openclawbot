import json
import os
import sqlite3
import sys
from types import SimpleNamespace

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
CANDIDATE = os.environ.get(
    "REVIEW_CANDIDATE", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, CANDIDATE)

import server
from meal_photo_system import (
    apply_meal_photo_action,
    apply_meal_photo_ai_adjustment,
    build_meal_photo_confirmation_bubble,
    get_meal_photo_draft,
    save_meal_photo_draft,
)


def _payload(items=None, *, total_cal=520, cal_min=400, cal_max=650,
             total_protein=31, protein_min=22, protein_max=40):
    items = items or [
        {"name": "米飯", "portion": "1碗", "calories_kcal": 220, "protein_g": 5},
        {"name": "豬肉", "portion": "1掌", "calories_kcal": 300, "protein_g": 26},
    ]
    items = [dict(item) for item in items]
    if (
        sum(item["calories_kcal"] for item in items) == total_cal
        and sum(item["protein_g"] for item in items) == total_protein
    ):
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
            {"name": x["name"], "category": "unknown", "confidence": 0.8}
            for x in items
        ],
        "uncertain_items": [], "starch_visibility": "visible",
        "oil_sauce_status": "unknown", "observed_at_confidence": 0.9,
        "ai_estimate": {
            "items": items,
            "calories_kcal": {"estimate": total_cal, "min": cal_min, "max": cal_max},
            "protein_g": {"estimate": total_protein, "min": protein_min, "max": protein_max},
            "confidence": 0.75,
            "provenance": {"provider": "fixture", "model": "offline", "method": "vision_model_estimate", "nutrition_basis": "unlabeled_meal_photo"},
        },
    }


def _postback(data, event_id="EVT", user_id="U1"):
    return SimpleNamespace(
        postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id=user_id),
        reply_token="reply", webhook_event_id=event_id, timestamp=1,
    )


def _actions(node):
    out = []
    if isinstance(node, dict):
        if isinstance(node.get("action"), dict):
            out.append(node["action"])
        for value in node.values():
            out.extend(_actions(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_actions(value))
    return out


def _legacy_payload():
    return {
        "status": "success", "image_type": "food_photo",
        "visible_items": [
            {"name": "高麗菜", "category": "vegetable", "confidence": 0.9},
            {"name": "青花菜", "category": "vegetable", "confidence": 0.8},
        ],
        "uncertain_items": [], "starch_visibility": "not_visible",
        "oil_sauce_status": "unknown", "observed_at_confidence": 0.9,
    }


def test_legacy_rendered_remove_callback_still_reaches_registered_handler(tmp_path, monkeypatch):
    db = tmp_path / "legacy-remove.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    replies = []
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda _token, reply: replies.append(reply))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="LEGACY", payload=_legacy_payload(),
            workflow_version="user_confirmed_ai_estimate_v1",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    card = build_meal_photo_confirmation_bubble(
        draft["payload"], token=token, consumed_at=draft["consumed_at"], version=draft["version"]
    )
    remove = next(a for a in _actions(card) if a.get("label") == "移除")
    assert ":remove:" in remove["data"]
    server.handle_postback_event(_postback(remove["data"], "LEGACY-REMOVE"))
    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
    assert [x["name"] for x in current["payload"]["visible_items"]] == ["青花菜"]


def test_later_photo_adjustment_cannot_resurrect_explicitly_removed_item(tmp_path):
    db = tmp_path / "resurrection.db"
    original = _payload()
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="RESURRECT", payload=original,
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        removed_id = next(x["item_id"] for x in draft["estimate"]["estimate_items"] if x["name"] == "豬肉")
        removed = apply_meal_photo_action(
            conn, event_id="REMOVE-PORK", user_id="U1", token=token,
            expected_version=draft["version"], action="remove_item", value=removed_id,
        )
        waiting = apply_meal_photo_action(
            conn, event_id="REQUEST-ADJUST", user_id="U1", token=token,
            expected_version=removed["draft"]["version"], action="request_adjust",
        )
        revised = apply_meal_photo_ai_adjustment(
            conn, event_id="ADJUST-AFTER-REMOVE", user_id="U1", token=token,
            expected_version=waiting["draft"]["version"], correction="米飯改成半碗",
            estimate_provider=lambda _ref, _current, _correction: original,
        )
    names = [x["name"] for x in revised["draft"]["estimate"]["estimate_items"]]
    assert "豬肉" not in names


def test_remove_recalculation_cannot_create_zero_total_with_nonzero_remaining_item(tmp_path):
    db = tmp_path / "zero-total.db"
    inconsistent_but_accepted_provider_shape = _payload(
        items=[
            {"name": "大份豬肉", "portion": "1份", "calories_kcal": 300, "protein_g": 30},
            {"name": "米飯", "portion": "半碗", "calories_kcal": 100, "protein_g": 10},
        ],
        total_cal=250, cal_min=100, cal_max=400,
        total_protein=40, protein_min=20, protein_max=60,
    )
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="ZERO", payload=inconsistent_but_accepted_provider_shape,
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
        removed_id = next(x["item_id"] for x in draft["estimate"]["estimate_items"] if x["name"] == "大份豬肉")
        with pytest.raises(ValueError, match="估算區間無法確定"):
            apply_meal_photo_action(
                conn, event_id="REMOVE-BIG", user_id="U1", token=token,
                expected_version=draft["version"], action="remove_item", value=removed_id,
            )
        unchanged = get_meal_photo_draft(conn, user_id="U1", token=token)
    assert unchanged["version"] == draft["version"]
    assert len(unchanged["estimate"]["estimate_items"]) == 2
    assert unchanged["estimate"]["calories_kcal"] == 250
