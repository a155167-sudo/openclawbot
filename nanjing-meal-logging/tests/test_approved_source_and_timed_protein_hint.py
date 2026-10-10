import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import server
from dashboard_flex import compute
from tests.test_nanjing_meal_logging_flow import _setup


def _short_data(**overrides):
    data = {
        "target_kcal": 2000,
        "target_protein": 120,
        "records": [{"kcal": 2000, "protein": 80}],
        "sub_meals": [],
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "original_label",
    ["衛福部資料", "日本食品成分表", "AI估算"],
)
def test_customer_revision_success_source_keeps_original_label(original_label):
    estimate = {
        "provenance": {
            "method": "customer_revision",
            "source_label": "顧客修改",
            "original_estimate": {
                "provenance": {"source_label": original_label},
            },
        },
    }

    assert server._confirmed_text_meal_source_label(estimate) == f"{original_label}・顧客修改"


def test_customer_revision_source_prefers_original_official_card_note():
    estimate = {
        "provenance": {
            "method": "customer_revision",
            "source_label": "顧客修改",
            "original_estimate": {
                "provenance": {
                    "method": "official_reference",
                    "source_label": "衛福部資料",
                    "source": {"card_note": "衛福部資料・以 1ml≈1g 換算"},
                },
            },
        },
    }

    assert server._confirmed_text_meal_source_label(estimate) == "衛福部資料・以 1ml≈1g 換算・顧客修改"


def test_confirmed_revision_returns_original_source_and_keeps_edited_numbers(tmp_path, monkeypatch):
    db, _replies = _setup(tmp_path, monkeypatch)
    draft = server.create_fixed_text_meal_draft(
        user_id="U-NANJING",
        message_id="SOURCE-REVISION",
        method="official_reference",
        request={
            "food_name": "蒸地瓜", "amount": 150, "unit": "g", "meal_slot": "午餐",
            "calories_kcal": 180, "protein_g": 2, "fat_g": 0.3, "carbohydrate_g": 42,
            "source": {"publisher": "日本文部科學省", "card_note": "日本食品成分表"},
        },
    )
    saved = server.save_text_meal_draft_from_liff(
        user_id="U-NANJING", token=draft["token"], expected_version=draft["version"],
        amount=150, unit="g", meal_slot="午餐",
        nutrition={"calories_kcal": 199, "protein_g": 3, "fat_g": 0.4, "carbohydrate_g": 45},
    )["draft"]

    result = server.apply_text_meal_estimate_action(
        user_id="U-NANJING", token=saved["token"], expected_version=saved["version"], action="confirm",
    )

    assert result["source_label"] == "日本食品成分表・顧客修改"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE log_id=?",
            (result["log_id"],),
        ).fetchone()
    import json
    nutrition = json.loads(row[0])
    assert nutrition == {
        "calories_kcal": 199.0, "protein_g": 3.0,
        "fat_g": 0.4, "carbohydrate_g": 45.0,
    }


def test_protein_hint_before_17_keeps_existing_detail():
    result = compute(_short_data(), now=datetime(2026, 10, 8, 16, 59, tzinfo=ZoneInfo("Asia/Taipei")))
    assert result["hint"] == "熱量已達標，蛋白質還差 40.0 g。可以補一杯無糖豆漿或一顆茶葉蛋。"


def test_protein_hint_between_17_and_2059_is_gentle():
    result = compute(_short_data(), now=datetime(2026, 10, 8, 17, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    assert result["hint"] == "今天蛋白質攝取較少，可依食慾適量補充，不必一次補足目標。"


def test_protein_hint_at_21_is_hidden():
    result = compute(_short_data(), now=datetime(2026, 10, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    assert result["show_hint"] is False
    assert result["hint"] == ""


def test_protein_hint_converts_injected_instant_to_taipei():
    result = compute(_short_data(), now=datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc))
    assert result["hint"] == ""


def test_unknown_warning_still_precedes_evening_protein_copy():
    data = _short_data(records=[{"kcal": None, "protein": 80}])
    result = compute(data, now=datetime(2026, 10, 8, 18, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    assert result["hint"] == "部分紀錄的營養資料未知，暫時無法計算精確餘額。"


def test_subscription_over_warning_remains_primary_after_21_without_protein_copy():
    data = _short_data(
        target_kcal=1000, target_protein=120, records=[],
        sub_meals=[{"kcal": 1100, "protein": 20, "eaten": False}],
    )
    result = compute(data, now=datetime(2026, 10, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    assert result["hint"].startswith("今天的包月餐合計比目標多 100 kcal")
    assert "蛋白質" not in result["hint"]
