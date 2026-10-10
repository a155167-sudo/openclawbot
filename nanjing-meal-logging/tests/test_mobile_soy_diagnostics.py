import json

from nutrition_draft_diagnostics import emit_draft_diagnostic


def _printed_payload(capsys, draft):
    returned = emit_draft_diagnostic(draft)
    line = capsys.readouterr().out
    assert line.startswith("NUTRITION_DRAFT ")
    assert line.count("\n") == 1
    payload = json.loads(line.removeprefix("NUTRITION_DRAFT "))
    assert payload == returned
    return line, payload


def test_semantic_multi_item_diagnostic_is_one_privacy_safe_line(capsys):
    draft = {
        "token": "secret-token", "user_id": "U-0912345678", "source_message_id": "msg-secret",
        "portion_multiplier": 1,
        "request": {"food_name": "王小明0912345678的餐", "rawtext": "private text", "items": []},
        "estimate": {
            "schema_version": "semantic-meal-estimate-v1",
            "provenance": {"items": [
                {
                    "request": {"food_name": "無糖豆漿", "amount": 500, "unit": "ml"},
                    "nutrition": {"calories_kcal": 175, "protein_g": 18,
                                  "fat_g": 9.5, "carbohydrate_g": 3.5},
                    "source_label": "官方資料",
                    "source": {"food_code": "H1150201", "source_name": "豆漿(無糖)"},
                },
                {
                    "request": {"food_name": "王小明私房雞胸", "amount": 200, "unit": "g"},
                    "nutrition": {"calories_kcal": {"estimate": 238}, "protein_g": {"estimate": 46.6},
                                  "fat_g": {"estimate": 4.2}, "carbohydrate_g": None},
                    "source_label": "AI估算", "source": {"provider": "openai"},
                },
            ]},
        },
    }
    line, payload = _printed_payload(capsys, draft)
    assert payload == {"items": [
        {"route": "reference", "matched_item": "豆漿(無糖)", "basis100unit": "ml",
         "per100": {"calories_kcal": 35.0, "protein_g": 3.6, "fat_g": 1.9,
                    "carbohydrate_g": 0.7}, "factor": 5.0},
        {"route": "ai", "matched_item": None, "basis100unit": "g",
         "per100": {"calories_kcal": 119.0, "protein_g": 23.3, "fat_g": 2.1,
                    "carbohydrate_g": None}, "factor": 2.0},
    ]}
    for secret in ("secret-token", "U-0912345678", "msg-secret", "王小明", "0912345678", "private text"):
        assert secret not in line
    for forbidden_key in ("token", "user_id", "source_message_id", "rawtext", "auth"):
        assert forbidden_key not in payload


def test_old_total_basis_estimate_converts_to_per100_without_filling_unknown(capsys):
    draft = {
        "portion_multiplier": 1.2,
        "request": {"food_name": "private recipe", "amount": 250, "unit": "g"},
        "estimate": {
            "basis_amount": 250, "basis_unit": "g",
            "calories_kcal": {"estimate": 200}, "protein_g": {"estimate": 10},
            "fat_g": None, "carbohydrate_g": {"estimate": 30},
            "provenance": {"method": "user_provided_nutrition", "source": "private"},
        },
    }
    _, payload = _printed_payload(capsys, draft)
    assert payload["items"] == [{
        "route": "private", "matched_item": None, "basis100unit": "g",
        "per100": {"calories_kcal": 80.0, "protein_g": 4.0,
                   "fat_g": None, "carbohydrate_g": 12.0},
        "factor": 3.0,
    }]


def test_unverified_reference_name_is_not_echoed_and_logging_is_fail_safe(capsys):
    draft = {
        "request": {"food_name": "林小姐 0988777666"},
        "estimate": {"provenance": {"source_label": "官方資料", "source": {
            "source_name": "林小姐 0988777666 特餐"}}},
    }
    line, payload = _printed_payload(capsys, draft)
    assert payload["items"][0]["route"] == "private"
    assert payload["items"][0]["matched_item"] is None
    assert payload["items"][0]["per100"] == {
        "calories_kcal": None, "protein_g": None, "fat_g": None, "carbohydrate_g": None,
    }
    assert "林小姐" not in line and "0988777666" not in line
