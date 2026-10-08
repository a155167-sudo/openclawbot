"""Regression coverage for narrow-phone text meal draft visibility."""

import json

import server
from linebot.models import FlexSendMessage


def test_draft_title_and_full_nutrient_ranges_wrap_without_truncation():
    draft = {
        "token": "a" * 40,
        "version": 1,
        "portion_multiplier": 1,
        "meal_slot": "午餐",
        "estimate": {
            "food_name": "500ml 飲品營養估算草稿",
            "portion_assumption": "500 ml",
            "calories_kcal": {"min": 1, "max": 159},
            "protein_g": {"min": 0.1, "max": 13.9},
            "fat_g": {"min": 0.1, "max": 7.9},
            "carbohydrate_g": {"min": 0.1, "max": 11.9},
            "assessment": {"requires_correction": False},
            "provenance": {"method": "text_meal_estimate", "source_label": "AI估算"},
        },
    }

    message = server.build_text_meal_estimate_flex(draft)
    assert isinstance(message, FlexSendMessage)
    serialized = message.as_json_dict()

    title = serialized["contents"]["header"]["contents"][0]
    assert title["text"] == "營養估算草稿（尚未記錄）"
    assert title["wrap"] is True
    assert "maxLines" not in title

    body = serialized["contents"]["body"]["contents"]
    expected = [
        "熱量：80 kcal（實際入帳）｜估算範圍 1–159",
        "蛋白質：7.0 g（實際入帳）｜估算範圍 0.1–13.9",
        "脂肪：4.0 g（實際入帳）｜估算範圍 0.1–7.9",
        "碳水：6.0 g（實際入帳）｜估算範圍 0.1–11.9",
    ]
    nutrients = [item for item in body if item.get("text") in expected]
    assert [item["text"] for item in nutrients] == expected
    assert all(item["wrap"] is True for item in nutrients)
    assert all("maxLines" not in item for item in nutrients)

    buttons = serialized["contents"]["footer"]["contents"]
    assert [button["action"]["label"] for button in buttons] == ["確認記錄", "修改", "取消"]
    assert json.dumps(serialized, ensure_ascii=False).count("實際入帳") == 4
