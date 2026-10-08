import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from customer_reschedule_liff_routes import create_customer_reschedule_router
import server

LIFF = "2011528194-td43IPq1"
CHANNEL = "2011528194"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _draft(owner="U1", version=3):
    return {
        "token": "a" * 40,
        "user_id": owner,
        "version": version,
        "status": "pending",
        "expires_at": "2099-01-01T00:00:00+08:00",
        "request": {"amount": 500.0, "unit": "ml", "meal_slot": "午餐"},
        "estimate": {
            "food_name": "無糖豆漿",
            "basis_amount": 500.0,
            "basis_unit": "ml",
            "portion_assumption": "500 ml",
            "calories_kcal": {"estimate": 123.456, "min": 100.0, "max": 150.0},
            "protein_g": {"estimate": 12.34, "min": 10.0, "max": 14.0},
            "fat_g": None,
            "carbohydrate_g": {"estimate": 8.88, "min": 8.0, "max": 10.0},
            "provenance": {"method": "text_meal_estimate", "source_label": "AI估算"},
        },
    }


def _client(tmp_path, *, owner="U1", draft_loader=None, saver=None):
    html = tmp_path / "normal.html"
    html.write_text("<script>window.__RESCHEDULE_RUNTIME__ = null; /* __CUSTOMER_RESCHEDULE_RUNTIME__ */</script>RESCHEDULE", encoding="utf-8")
    meal_html = tmp_path / "meal.html"
    meal_html.write_text("<script>window.__MEAL_DRAFT_RUNTIME__ = null; /* __MEAL_DRAFT_RUNTIME_MARKER__ */</script>MEAL_EDIT", encoding="utf-8")
    app = FastAPI()
    app.include_router(create_customer_reschedule_router(
        liff_id=LIFF, channel_id=CHANNEL, db_path=str(tmp_path / "none.db"),
        app_env="staging", normal_flow=True, token_verifier=lambda token, **_: owner,
        now_factory=lambda: NOW, html_path=html, meal_edit_html_path=meal_html,
        meal_draft_loader=draft_loader or (lambda uid, token: _draft(uid)),
        meal_draft_saver=saver or (lambda **kw: {"draft": _draft(kw["user_id"], kw["expected_version"] + 1), "return_command": "#草稿回傳 " + "b" * 48, "receipt_id": "b" * 48}),
    ))
    return TestClient(app)


def test_view_branch_preserves_default_reschedule_page(tmp_path):
    client = _client(tmp_path)
    assert "RESCHEDULE" in client.get("/customer-reschedule").text
    meal = client.get("/customer-reschedule?view=meal-edit")
    assert meal.status_code == 200 and "MEAL_EDIT" in meal.text
    assert LIFF in meal.text and CHANNEL in meal.text


def test_owner_bound_read_never_accepts_body_uid_and_preserves_unknown(tmp_path):
    seen = []
    client = _client(tmp_path, owner="OWNER", draft_loader=lambda uid, token: seen.append((uid, token)) or _draft(uid))
    response = client.get("/customer-reschedule/meal-draft?token=" + "a" * 40 + "&user_id=ATTACKER", headers={"Authorization": "Bearer good"})
    assert response.status_code == 200
    assert seen == [("OWNER", "a" * 40)]
    body = response.json()
    assert body["nutrition"]["fat_g"] is None
    assert body["initial_ai"]["calories_kcal"] == 123.456
    assert body["display"]["calories_kcal"] == "125"
    assert body["display"]["protein_g"] == "12.0"


def test_save_uses_verified_owner_version_and_returns_opaque_command(tmp_path):
    calls = []
    def save(**kw):
        calls.append(kw)
        return {"draft": _draft(kw["user_id"], 4), "return_command": "#草稿回傳 " + "c" * 48, "receipt_id": "c" * 48}
    client = _client(tmp_path, owner="OWNER", saver=save)
    response = client.put("/customer-reschedule/meal-draft", headers={"Authorization": "Bearer good"}, json={
        "user_id": "ATTACKER", "token": "a" * 40, "version": 3,
        "amount": 400, "unit": "ml", "meal_slot": "午餐",
        "nutrition": {"calories_kcal": 100.25, "protein_g": 9.5, "fat_g": None, "carbohydrate_g": 7.0},
    })
    assert response.status_code == 200
    assert calls[0]["user_id"] == "OWNER" and "ATTACKER" not in json.dumps(calls[0])
    command = response.json()["return_command"]
    assert command == "#草稿回傳 " + "c" * 48
    assert "OWNER" not in command and "a" * 40 not in command


@pytest.mark.parametrize("error,status", [(ValueError("這張估算卡已更新"), 409), (PermissionError("forbidden"), 403)])
def test_save_fails_closed_for_stale_or_foreign(tmp_path, error, status):
    client = _client(tmp_path, saver=lambda **_: (_ for _ in ()).throw(error))
    response = client.put("/customer-reschedule/meal-draft", headers={"Authorization": "Bearer good"}, json={
        "token": "a" * 40, "version": 2, "amount": 400, "unit": "ml", "meal_slot": "午餐", "nutrition": {}
    })
    assert response.status_code == status


def test_complete_phone_sentence_strips_leading_verb_and_preserves_amount():
    parsed = server.parse_natural_food_log_intent("午餐喝了無糖豆漿500ml")
    assert parsed == {"food_name": "無糖豆漿", "amount": 500.0, "unit": "ml", "meal_slot": "午餐"}


def test_unified_card_title_three_primary_actions_and_display_rounding(tmp_path, monkeypatch):
    from tests.test_phone_meal_revision import _ai_draft
    _db, draft = _ai_draft(tmp_path, monkeypatch)
    card = server.build_text_meal_estimate_flex(draft).as_json_dict()
    text = json.dumps(card, ensure_ascii=False)
    labels = [x["action"]["label"] for x in card["contents"]["footer"]["contents"]]
    assert "營養估算草稿（尚未記錄）" in text
    assert "來源：" in text
    assert "實際入帳" in text
    assert labels == ["確認記錄", "修改", "取消"]
    assert card["contents"]["footer"]["contents"][1]["action"]["type"] == "uri"
    assert "view=meal-edit" in card["contents"]["footer"]["contents"][1]["action"]["uri"]


def test_real_liff_save_is_draft_only_replay_safe_and_command_owner_bound(tmp_path, monkeypatch):
    from tests.test_phone_meal_revision import _ai_draft
    db, draft = _ai_draft(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        before_quota = conn.execute("SELECT COUNT(*) FROM text_meal_estimate_quota_ledger").fetchone()[0]
    first = server.save_text_meal_draft_from_liff(
        user_id="U1", token=draft["token"], expected_version=1,
        amount=400, unit="ml", meal_slot="晚餐",
        nutrition={"calories_kcal": 180.25, "protein_g": 16.125,
                   "fat_g": None, "carbohydrate_g": 20.75},
    )
    replay = server.save_text_meal_draft_from_liff(
        user_id="U1", token=draft["token"], expected_version=1,
        amount=400, unit="ml", meal_slot="晚餐",
        nutrition={"calories_kcal": 180.25, "protein_g": 16.125,
                   "fat_g": None, "carbohydrate_g": 20.75},
    )
    assert replay["receipt_id"] == first["receipt_id"]
    assert first["draft"]["version"] == 2 and first["draft"]["meal_slot"] == "晚餐"
    assert first["draft"]["estimate"]["protein_g"]["estimate"] == 16.125
    assert first["draft"]["estimate"]["fat_g"] is None
    assert first["draft"]["estimate"]["provenance"]["original_estimate"]["protein_g"]["estimate"] == 12
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM text_meal_estimate_quota_ledger").fetchone()[0] == before_quota
        assert conn.execute("SELECT COUNT(*) FROM meal_draft_return_receipts").fetchone()[0] == 1
    with pytest.raises(PermissionError):
        server.consume_meal_draft_return_command("OTHER", first["return_command"])
    returned = server.consume_meal_draft_return_command("U1", first["return_command"])
    assert "營養估算草稿" in json.dumps(returned.as_json_dict(), ensure_ascii=False)


def test_liff_save_rejects_stale_expired_and_incompatible_unit(tmp_path, monkeypatch):
    from tests.test_phone_meal_revision import _ai_draft
    db, draft = _ai_draft(tmp_path, monkeypatch)
    common = dict(user_id="U1", token=draft["token"], expected_version=1,
                  amount=400, meal_slot="午餐", nutrition={})
    with pytest.raises(ValueError, match="份量或單位"):
        server.save_text_meal_draft_from_liff(unit="kg", **common)
    with pytest.raises(ValueError, match="原始估算一致"):
        server.save_text_meal_draft_from_liff(unit="g", **common)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE pending_text_meal_estimates SET expires_at='2000-01-01T00:00:00+08:00' WHERE token=?", (draft["token"],))
        conn.commit()
    with pytest.raises(ValueError, match="逾時"):
        server.save_text_meal_draft_from_liff(unit="ml", **common)


def _photo_draft(tmp_path, monkeypatch):
    from meal_photo_system import save_meal_photo_draft, get_meal_photo_draft
    from tests.test_meal_photo_system import ai_estimated_payload
    db = tmp_path / "photo-liff.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    payload = ai_estimated_payload()
    payload["ai_estimate"]["items"] = [
        {"name": "雞腿", "portion": "100 g", "calories_kcal": 330, "protein_g": 27},
        {"name": "豆漿", "portion": "200 ml", "calories_kcal": 280, "protein_g": 5},
    ]
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-LIFF-1", payload=payload,
            meal_slot="午餐", workflow_version="user_confirmed_ai_nutrition_v2",
        )
        draft = get_meal_photo_draft(conn, user_id="U1", token=token)
    return db, draft


def test_photo_card_uses_unified_title_and_three_primary_actions(tmp_path, monkeypatch):
    _db, draft = _photo_draft(tmp_path, monkeypatch)
    card = server.build_meal_photo_estimate_bubble(draft)
    text = json.dumps(card, ensure_ascii=False)
    actions = [item["action"] for item in card["footer"]["contents"]]
    assert "營養估算草稿（尚未記錄）" in text
    assert "來源：AI 照片估算" in text
    assert "實際入帳" in text
    assert [action["label"] for action in actions] == ["確認記錄", "修改", "取消"]
    assert actions[1]["type"] == "uri" and "view=meal-edit" in actions[1]["uri"]


def test_photo_liff_projection_keeps_each_ingredient_amount_unit_and_unknown(tmp_path, monkeypatch):
    _db, draft = _photo_draft(tmp_path, monkeypatch)
    projected = server.get_meal_draft_for_liff("U1", draft["token"])
    assert projected["draft_type"] == "photo"
    assert [(item["name"], item["amount"], item["unit"]) for item in projected["items"]] == [
        ("雞腿", 100.0, "g"), ("豆漿", 200.0, "ml")
    ]
    assert projected["nutrition"]["fat_g"] is None
    assert projected["initial_ai"]["fat_g"] is None


def test_photo_liff_save_scales_each_item_from_unrounded_original_without_logging(tmp_path, monkeypatch):
    db, draft = _photo_draft(tmp_path, monkeypatch)
    projected = server.get_meal_draft_for_liff("U1", draft["token"])
    items = projected["items"]
    changed = [
        {**items[0], "amount": 150.0},
        {**items[1], "amount": 100.0},
    ]
    first = server.save_photo_meal_draft_from_liff(
        user_id="U1", token=draft["token"], expected_version=1,
        meal_slot="晚餐", items=changed,
    )
    replay = server.save_photo_meal_draft_from_liff(
        user_id="U1", token=draft["token"], expected_version=1,
        meal_slot="晚餐", items=changed,
    )
    assert replay["receipt_id"] == first["receipt_id"]
    saved = first["draft"]
    assert saved["meal_slot"] == "晚餐" and saved["version"] == 2
    assert saved["estimate"]["calories_kcal"] == pytest.approx(635.0)
    assert saved["estimate"]["protein_g"] == pytest.approx(43.0)
    assert saved["estimate"]["fat_g"] is None
    assert saved["review"]["liff_initial_ai"]["calories_kcal"] == 680.0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM meal_draft_return_receipts").fetchone()[0] == 1
    with pytest.raises(PermissionError):
        server.consume_meal_draft_return_command("OTHER", first["return_command"])
    returned = server.consume_meal_draft_return_command("U1", first["return_command"])
    returned_text = json.dumps(returned.as_json_dict(), ensure_ascii=False)
    assert "營養估算草稿（尚未記錄）" in returned_text and "雞腿" in returned_text and "豆漿" in returned_text


def test_photo_liff_rejects_cross_item_unit_swap_and_stale_version(tmp_path, monkeypatch):
    _db, draft = _photo_draft(tmp_path, monkeypatch)
    items = server.get_meal_draft_for_liff("U1", draft["token"])["items"]
    bad = [{**items[0], "unit": "ml"}, items[1]]
    with pytest.raises(ValueError, match="各食材原始單位"):
        server.save_photo_meal_draft_from_liff(
            user_id="U1", token=draft["token"], expected_version=1,
            meal_slot="午餐", items=bad,
        )


def test_photo_liff_supports_each_original_natural_portion_unit(tmp_path, monkeypatch):
    from meal_photo_system import save_meal_photo_draft
    from tests.test_meal_photo_system import ai_estimated_payload
    db = tmp_path / "photo-natural-units.db"
    monkeypatch.setattr(server, "DB_PATH", str(db))
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn, user_id="U1", source_message_id="PHOTO-NATURAL-UNITS",
            payload=ai_estimated_payload(), meal_slot="午餐",
            workflow_version="user_confirmed_ai_nutrition_v2",
        )
    projected = server.get_meal_draft_for_liff("U1", token)
    assert [(item["amount"], item["unit"]) for item in projected["items"]] == [
        (1.0, "支"), (1.0, "碗")
    ]
