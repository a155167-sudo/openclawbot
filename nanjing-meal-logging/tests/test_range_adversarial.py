import json
import sqlite3

import pytest
import server
from meal_photo_system import build_meal_photo_estimate_bubble, get_meal_photo_draft
from tests.test_photo_ingredient_controls import _action, _postback, _text
from tests.test_photo_natural_ingredient_batch import _estimate, _setup, _enter_add


ROUNDED_DETAILS = [
    {"name": "米飯", "portion": "1碗", "calories_kcal": 80, "protein_g": 2},
    {"name": "豬肉", "portion": "1掌", "calories_kcal": 220, "protein_g": 22},
    {"name": "炒蛋", "portion": "1顆", "calories_kcal": 60, "protein_g": 6},
    {"name": "蔬菜", "portion": "半碗", "calories_kcal": 10, "protein_g": 1},
]


def test_existing_item_with_fixture_interval_requires_choice_then_preserves_aggregate(tmp_path, monkeypatch):
    """Even a safe zero-delta update is explicit; choosing modify preserves provider aggregate."""
    db, token, draft, replies = _setup(tmp_path, monkeypatch, ROUNDED_DETAILS)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))
    _enter_add(draft, replies)

    # _estimate(20g) returns the same 10 kcal / 1 g point as the old rounded row.
    server.handle_message(_text("蔬菜20g", "UPDATE-ROUNDED-DETAIL"))
    prompt = replies[-1].as_json_dict()
    assert prompt["text"] == "這餐已有蔬菜，你想怎麼處理？"
    server.handle_postback_event(_postback(_action(prompt, "修改原份量")["data"], "CHOOSE-ROUNDED-MODIFY"))

    with sqlite3.connect(db) as conn:
        current = get_meal_photo_draft(conn, user_id="U1", token=token)
    assert current["status"] == "estimated"
    assert len(calls) == 1
    assert current["estimate"]["calories_kcal"] == pytest.approx(520)
    assert current["estimate"]["protein_g"] == pytest.approx(31)
    assert current["estimate"]["calories_kcal_range"]["min"] == pytest.approx(400)
    assert current["estimate"]["calories_kcal_range"]["max"] == pytest.approx(650)
    assert current["estimate"]["protein_g_range"]["min"] == pytest.approx(22)
    assert current["estimate"]["protein_g_range"]["max"] == pytest.approx(40)


def test_declared_exact_item_sum_contradiction_is_not_normalized_by_add(tmp_path, monkeypatch):
    """items_sum_v1 is an exact contract, not an independent-provider rounding case."""
    db, token, draft, replies = _setup(tmp_path, monkeypatch, ROUNDED_DETAILS)
    provider_calls = []
    quota_calls = []
    monkeypatch.setattr(
        server, "estimate_text_meal_nutrition",
        lambda request: provider_calls.append(dict(request)) or _estimate(request),
    )
    monkeypatch.setattr(
        server, "check_permission_and_quota",
        lambda user_id: quota_calls.append(user_id) or (True, "left"),
    )
    _enter_add(draft, replies)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT observed_payload_json,version FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()
        observed = json.loads(row[0])
        observed["ai_estimate"]["reconciliation"] = "items_sum_v1"
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET observed_payload_json=? WHERE token=?",
            (json.dumps(observed, ensure_ascii=False, sort_keys=True), token),
        )
        conn.commit()
        before = get_meal_photo_draft(conn, user_id="U1", token=token)

    server.handle_message(_text("木耳20g", "ADD-TO-CONTRADICTED-EXACT"))

    with sqlite3.connect(db) as conn:
        after = get_meal_photo_draft(conn, user_id="U1", token=token)
    assert after["status"] == "awaiting_item_name"
    assert after["version"] == before["version"]
    assert after["estimate"] == before["estimate"]
    assert "原草稿" in replies[-1].text
    assert provider_calls == []
    assert quota_calls == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_nonzero_update_without_old_interval_fails_closed(tmp_path, monkeypatch):
    """A rounded old point is not a made-up exact interval for a replacement."""
    db, token, draft, replies = _setup(tmp_path, monkeypatch, ROUNDED_DETAILS)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", _estimate)
    with sqlite3.connect(db) as conn:
        raw = json.loads(conn.execute(
            "SELECT observed_payload_json FROM pending_meal_photo_drafts WHERE token=?", (token,)
        ).fetchone()[0])
        for item in raw["ai_estimate"]["items"]:
            item.pop("calories_kcal_range", None)
            item.pop("protein_g_range", None)
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET observed_payload_json=? WHERE token=?",
            (json.dumps(raw, ensure_ascii=False, sort_keys=True), token),
        )
        conn.commit()
    _enter_add(draft, replies)
    with sqlite3.connect(db) as conn:
        before = get_meal_photo_draft(conn, user_id="U1", token=token)

    server.handle_message(_text("蔬菜40g", "UPDATE-WITHOUT-OLD-RANGE"))
    prompt = replies[-1].as_json_dict()
    server.handle_postback_event(_postback(_action(prompt, "修改原份量")["data"], "CHOOSE-NONZERO-NO-RANGE"))

    assert "缺少個別營養區間" in replies[-1].text
    with sqlite3.connect(db) as conn:
        after = get_meal_photo_draft(conn, user_id="U1", token=token)
        assert (after["status"], after["version"], after["estimate"]) == (
            before["status"], before["version"], before["estimate"],
        )
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
