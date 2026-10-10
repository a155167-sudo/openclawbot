import json
import sqlite3

import pytest

import server
from tests.test_nanjing_meal_logging_flow import _postback, _postback_actions, _setup


def _texts(node):
    values = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            values.append(node.get("text", ""))
        for child in node.values():
            values.extend(_texts(child))
    elif isinstance(node, list):
        for child in node:
            values.extend(_texts(child))
    return values


def _semantic_draft(*, name, source_label, source, amount=150, unit="g"):
    nutrition = {
        "calories_kcal": 200,
        "protein_g": 10,
        "fat_g": 5,
        "carbohydrate_g": 30,
    }
    ranges = {
        field: {"estimate": value, "min": value, "max": value,
                "unit": "kcal" if field == "calories_kcal" else "g"}
        for field, value in nutrition.items()
    }
    request = {"food_name": name, "amount": amount, "unit": unit}
    return {
        "token": "a" * 40,
        "version": 1,
        "meal_slot": "午餐",
        "portion_multiplier": 1,
        "estimate": {
            "schema_version": "semantic-meal-estimate-v1",
            "food_name": name,
            "portion_assumption": f"{amount} {unit}（樣品狀態:熟；前處理:去皮；適用條件:測試）",
            "basis_amount": amount,
            "basis_unit": unit,
            **ranges,
            "assessment": {},
            "provenance": {
                "method": "semantic_meal_estimate",
                "source_label": source_label,
                "items": [{
                    "request": request,
                    "source_label": source_label,
                    "source": source,
                    "nutrition": ranges,
                }],
            },
        },
    }


@pytest.mark.parametrize(
    "draft, expected_source",
    [
        (
            _semantic_draft(
                name="無糖豆漿", amount=500, unit="ml", source_label="衛福部資料",
                source={"publisher": "TFDA", "density_g_per_ml": 1.0,
                        "card_note": "衛福部資料・以 1ml≈1g 換算"},
            ),
            "衛福部資料・以 1ml≈1g 換算",
        ),
        (
            _semantic_draft(
                name="白飯", source_label="官方資料",
                source={"publisher": "TFDA", "source_name": "白飯平均值",
                        "state": "樣品狀態:熟", "preparation_assumption": "前處理:混合"},
            ),
            "衛福部資料",
        ),
        (
            _semantic_draft(
                name="蒸地瓜", source_label="官方資料",
                source={"publisher": "日本文部科學省", "source_name": "蒸しさつまいも",
                        "card_note": "日本食品成分表參考"},
            ),
            "日本食品成分表參考",
        ),
    ],
)
def test_confirmation_flex_has_one_source_compact_portion_clean_nutrients_and_only_write_notice(
    draft, expected_source,
):
    payload = server.build_text_meal_estimate_flex(draft).as_json_dict()
    texts = _texts(payload)
    joined = "\n".join(texts)

    assert sum(expected_source in text for text in texts) == 1
    assert f"午餐・{draft['estimate']['basis_amount']:g} {draft['estimate']['basis_unit']}" in texts
    assert "確認後才會寫入" in texts
    assert not any("（確認後記錄）" in text for text in texts)
    assert not any(marker in joined for marker in ("樣品狀態", "前處理", "適用條件", "× 1", "份量假設：", "餐別："))
    assert not any(text.startswith(("參考範圍：", "處理／可食部：", "品種假設：", "版本／選樣：")) for text in texts)
    assert "確認後才會寫入；實際依店家與份量而異。" not in texts


def _fixed_revision_draft(*, user_id, message_id, amount, unit, source=None):
    return server.create_fixed_text_meal_draft(
        user_id=user_id,
        message_id=message_id,
        method="official_reference",
        request={
            "food_name": "無糖豆漿" if source else "顧客餐點",
            "amount": amount,
            "unit": unit,
            "meal_slot": "午餐",
            "calories_kcal": 100,
            "protein_g": 10,
            "fat_g": 5,
            "carbohydrate_g": 5,
            "source": source or {"publisher": "manual"},
        },
    )


def test_customer_revision_weight_warning_is_nonblocking_and_first_confirm_records_exactly_once(
    tmp_path, monkeypatch,
):
    db, replies = _setup(tmp_path, monkeypatch)
    draft = _fixed_revision_draft(
        user_id="U-NANJING", message_id="REV-WEIGHT", amount=150, unit="g"
    )
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-NANJING", token=draft["token"], expected_version=draft["version"],
        amount=150, unit="g", meal_slot="午餐",
        nutrition={
            "calories_kcal": 1000,
            "protein_g": 100,
            "fat_g": 60,
            "carbohydrate_g": None,
        },
    )
    revised = saved["draft"]
    texts = _texts(server.build_text_meal_estimate_flex(revised).as_json_dict())
    warning = "\n".join(texts)
    assert "非阻擋提醒" in warning
    assert "P+F+C" in warning and "150 g" in warning
    assert revised["estimate"]["carbohydrate_g"] is None

    confirm = next(action for action in _postback_actions(
        server.build_text_meal_estimate_flex(revised)
    ) if action.endswith(":confirm"))
    server.handle_postback_event(_postback(confirm, "REV-WEIGHT-CONFIRM"))
    server.handle_postback_event(_postback(confirm, "REV-WEIGHT-CONFIRM-REPLAY"))

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE user_id='U-NANJING'"
        ).fetchall()
        assert len(rows) == 1
        nutrition = json.loads(rows[0][0])
        assert nutrition["carbohydrate_g"] is None
    assert all(isinstance(reply, list) and len(reply) == 2 for reply in replies[-2:])


def test_customer_revision_energy_warning_uses_reported_threshold_and_does_not_block(tmp_path, monkeypatch):
    _db, _replies = _setup(tmp_path, monkeypatch)
    draft = _fixed_revision_draft(
        user_id="U-NANJING", message_id="REV-ENERGY", amount=150, unit="g"
    )
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-NANJING", token=draft["token"], expected_version=1,
        amount=150, unit="g", meal_slot="午餐",
        nutrition={"calories_kcal": 220, "protein_g": 10, "fat_g": 5, "carbohydrate_g": 20},
    )
    warning = "\n".join(_texts(server.build_text_meal_estimate_flex(saved["draft"]).as_json_dict()))
    assert "非阻擋提醒" in warning
    assert "max(50 kcal, 巨量營養素熱量的 20%)" in warning
    result = server.apply_text_meal_estimate_action(
        user_id="U-NANJING", token=saved["draft"]["token"],
        expected_version=saved["draft"]["version"], action="confirm",
    )
    assert result["kind"] == "confirmed"


@pytest.mark.parametrize(
    "source, expect_warning",
    [
        ({"publisher": "manual"}, False),
        ({"publisher": "TFDA", "source_type": "government_food_composition_density_derived",
          "density_g_per_ml": 1.0}, True),
    ],
)
def test_ml_weight_check_only_uses_approved_soy_density(tmp_path, monkeypatch, source, expect_warning):
    _db, _replies = _setup(tmp_path, monkeypatch)
    draft = _fixed_revision_draft(
        user_id="U-NANJING", message_id=f"REV-ML-{expect_warning}",
        amount=1, unit="ml", source=source,
    )
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-NANJING", token=draft["token"], expected_version=1,
        amount=1, unit="ml", meal_slot="午餐",
        nutrition={"calories_kcal": 8, "protein_g": 2, "fat_g": None, "carbohydrate_g": None},
    )
    warnings = saved["draft"]["estimate"]["assessment"].get("customer_plausibility_warnings", [])
    assert bool(warnings) is expect_warning
