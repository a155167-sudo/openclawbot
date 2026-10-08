import copy
import json
import sqlite3

import pytest

import server
from dashboard_flex import build_dashboard_flex as render_overview
from tests.test_meal_confirmation_card_and_revision_warning import _semantic_draft, _texts
from tests.test_nanjing_meal_logging_flow import _setup


RAW = {
    "calories_kcal": 274.5,
    "protein_g": 4.65,
    "fat_g": 0.45,
    "carbohydrate_g": 61.5,
}


def _joined(payload):
    return "\n".join(_texts(payload))


def _rice_draft():
    draft = _semantic_draft(
        name="白飯",
        source_label="TFDA官方參考",
        source={"publisher": "TFDA", "source_name": "白飯平均值"},
        amount=150,
        unit="g",
    )
    for field, value in RAW.items():
        draft["estimate"][field] = {
            "estimate": value,
            "min": value,
            "max": value,
            "unit": "kcal" if field == "calories_kcal" else "g",
        }
    draft["estimate"]["provenance"]["method"] = "official_reference"
    return draft


def test_draft_renderer_uses_half_up_integer_calories_fixed_one_decimal_pfc_and_full_source():
    draft = _rice_draft()
    before = copy.deepcopy(draft)

    text = _joined(server.build_text_meal_estimate_flex(draft).as_json_dict())

    assert "熱量：275 kcal" in text
    assert "蛋白質：4.7 g" in text
    assert "脂肪：0.5 g" in text
    assert "碳水：61.5 g" in text
    assert "衛福部資料" in text and "TFDA" not in text
    assert draft == before


def _confirmed_log(tmp_path, monkeypatch):
    db, _replies = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        result = server.create_daily_food_log(
            conn,
            user_id="U-ROUND",
            product_name="白飯",
            meal_slot="午餐",
            consumed_at=f"{server.tw_today().isoformat()}T12:00:00+08:00",
            servings=1,
            nutrition=RAW,
            source_type="official_reference",
            operation_key="rounding-contract",
            publish_catalog=False,
            consumed_amount=150,
            consumed_unit="g",
        )
        conn.commit()
    return db, result["log_id"]


def test_confirmed_record_renderer_rounds_display_only_and_uses_health_ministry_source(tmp_path, monkeypatch):
    db, log_id = _confirmed_log(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "build_post_commit_food_dashboard", lambda *_a, **_k: "dashboard")

    card = server.build_food_log_success_messages(
        "U-ROUND", log_id, source_label="TFDA官方參考"
    )[0]
    text = _joined(card.as_json_dict())

    assert "熱量 275 kcal｜蛋白質 4.7 g" in text
    assert "脂肪 0.5 g｜碳水 61.5 g" in text
    assert "來源：衛福部資料" in text and "TFDA" not in text
    with sqlite3.connect(db) as conn:
        saved = json.loads(conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE log_id=?", (log_id,)
        ).fetchone()[0])
    assert saved == RAW


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ({"publisher": "TFDA"}, "衛福部資料"),
        ({"publisher": "TFDA", "card_note": "衛福部資料・以 1ml≈1g 換算"},
         "衛福部資料・以 1ml≈1g 換算"),
        ({"publisher": "日本文部科學省 MEXT", "source_label": "日本食品成分表參考"},
         "日本食品成分表參考"),
    ],
)
def test_confirmed_record_source_labels_are_complete_and_customer_safe(tmp_path, monkeypatch, source, expected):
    _db, log_id = _confirmed_log(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "build_post_commit_food_dashboard", lambda *_a, **_k: "dashboard")
    label = server._text_meal_source_label("official_reference", source)

    text = _joined(server.build_food_log_success_messages(
        "U-ROUND", log_id, source_label=label
    )[0].as_json_dict())

    assert f"來源：{expected}" in text
    assert "TFDA" not in text


def test_overview_renderer_uses_half_up_integer_calories_and_fixed_one_decimal_pfc():
    data = {
        "user_name": "測試會員",
        "date_label": "今天",
        "target_kcal": 0,
        "target_protein": 0,
        "records": [{
            "slot": "午餐", "name": "白飯", "kcal": RAW["calories_kcal"],
            "protein": RAW["protein_g"], "is_sub": False, "ai_estimated": False,
        }],
        "sub_meals": [],
    }

    text = _joined(render_overview(data))

    assert "今日已吃\n275\nkcal" in text
    assert "今日蛋白質\n4.7 g" in text
    assert "白飯\n275 kcal" in text


def test_today_detail_renderer_rounds_summary_and_item_without_mutating_exact_values(tmp_path, monkeypatch):
    db, _log_id = _confirmed_log(tmp_path, monkeypatch)
    before = db.read_bytes()

    payload = server.build_daily_food_ledger_flex("U-ROUND", "today").as_json_dict()
    text = _joined(payload)

    assert "🔥 熱量：275 kcal" in text
    assert "🥩 蛋白質：4.7 g" in text
    assert "🍚 碳水：61.5 g" in text
    assert "🥑 脂肪：0.5 g" in text
    assert "🔥 275 kcal" in text
    assert "🥩 4.7 g" in text
    assert "🍚 61.5 g｜🥑 0.5 g" in text
    assert db.read_bytes() == before


def test_customer_revision_source_is_not_presented_as_original_official_source(tmp_path, monkeypatch):
    _db, _replies = _setup(tmp_path, monkeypatch)
    original = server.create_fixed_text_meal_draft(
        user_id="U-ROUND",
        message_id="REV-SOURCE",
        method="official_reference",
        request={
            "food_name": "白飯", "amount": 150, "unit": "g", "meal_slot": "午餐",
            **RAW, "source": {"publisher": "TFDA"},
        },
    )
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-ROUND", token=original["token"], expected_version=original["version"],
        amount=150, unit="g", meal_slot="午餐", nutrition=RAW,
    )["draft"]

    text = _joined(server.build_text_meal_estimate_flex(saved).as_json_dict())

    assert "顧客修改" in text
    assert "衛福部資料" not in text and "TFDA" not in text
