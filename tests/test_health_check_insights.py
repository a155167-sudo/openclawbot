import health_check_insights as hci
from dietitian_health_check_ai_draft import SYSTEM_PROMPT, build_ai_draft_input


def _m(date, slot, name, kcal, protein, own=False, hour=None):
    return {"date": date, "slot": slot, "name": name, "kcal": kcal, "protein": protein,
            "own_menu": own, "estimated": not own, "hour": hour}


def _three_days(extra=()):
    meals = []
    for d in ("2026-11-09", "2026-11-10", "2026-11-11"):
        meals += [_m(d, "午餐", "雞肉便當", 520, 35, own=True, hour=12),
                  _m(d, "晚餐", "鮭魚低碳", 480, 33, own=True, hour=19)]
    return meals + list(extra)


def test_protein_gap_is_translated_into_food():
    assert hci.protein_gap_as_food(37) == "雞胸肉一份（約 100 克）＋無糖豆漿一杯（約 400 毫升）"
    assert hci.protein_gap_as_food(8) == "茶葉蛋一顆"
    assert hci.protein_gap_as_food(2) == ""


def test_low_protein_without_breakfast_points_to_breakfast():
    out = hci.compute_insights(_three_days(), kcal_target=1300, protein_target=110, complete_days=3)
    focus = out["本週重點"]
    assert focus["最該先做的一件事"] == "蛋白質"
    assert "只到目標的 62%" in focus["依據"] and "三天都沒有記早餐" in focus["依據"]
    assert out["餐次分布"]["有記早餐的天數"] == 0
    assert out["便當與外食"]["包月便當_餐數"] == 6 and out["便當與外食"]["便當以外_筆數"] == 0


def test_drinks_and_snacks_become_the_focus_when_over_target():
    extra = [_m(d, "點心", "珍珠奶茶", 450, 2, hour=15) for d in ("2026-11-09", "2026-11-10", "2026-11-11")]
    extra += [_m("2026-11-09", "早餐", "蛋餅", 300, 12, hour=8), _m("2026-11-10", "早餐", "蛋餅", 300, 12, hour=8),
              _m("2026-11-11", "早餐", "蛋餅", 300, 12, hour=8)]
    out = hci.compute_insights(_three_days(extra), kcal_target=1500, protein_target=85, complete_days=3)
    assert out["飲料與點心"]["飲料_次數"] == 3
    assert out["飲料與點心"]["飲料加點心佔總熱量_%"] == 26
    assert out["本週重點"]["最該先做的一件事"] == "飲料與點心"
    assert out["餐次分布"]["最晚一餐的時間_點"] == 19


def test_outside_meals_are_separated_from_bento():
    extra = [_m(d, "晚餐", "炸雞排便當", 1100, 30, hour=21) for d in ("2026-11-09", "2026-11-10")]
    meals = [m for m in _three_days() if not (m["slot"] == "晚餐" and m["date"] != "2026-11-11")] + extra
    out = hci.compute_insights(meals, kcal_target=1200, protein_target=70, complete_days=3)
    split = out["便當與外食"]
    assert split["便當以外_筆數"] == 2 and split["便當以外_平均每筆熱量"] == 1100
    assert out["本週重點"]["最該先做的一件事"] == "便當以外那一餐"


def test_sparse_records_ask_for_more_logging_first():
    meals = [_m("2026-11-09", "午餐", "雞肉便當", 520, 35, own=True)]
    out = hci.compute_insights(meals, kcal_target=1800, protein_target=100, complete_days=1)
    assert out["資料可信度"]["等級"] == "低"
    assert out["本週重點"]["最該先做的一件事"] == "補齊紀錄"


def test_on_target_keeps_going():
    out = hci.compute_insights(_three_days(), kcal_target=1000, protein_target=70, complete_days=3)
    assert out["本週重點"]["最該先做的一件事"] == "維持現在的節奏"


def test_ai_payload_carries_insights_and_marks_sources():
    detail = {
        "status": "ready_for_review", "source_integrity": {"all_snapshots_available": True},
        "profile": {"goal": "減脂", "restrictions": "", "tdee": 1500, "protein": 100},
        "valid_days": [{"local_date": "2026-11-09", "completeness_status": "qualified"}],
        "source_logs": [
            {"log_id": "a", "local_date": "2026-11-09", "normalized_meal_slot": "午餐",
             "nutrition_snapshot": {"calories_kcal": 520, "protein_g": 35}},
            {"log_id": "b", "local_date": "2026-11-09", "normalized_meal_slot": "點心",
             "nutrition_snapshot": {"calories_kcal": 450, "protein_g": 2},
             "trust_type": "user_confirmed_ai_estimate"},
        ],
    }
    facts = {"a": {"name": "雞肉便當", "source_type": "planned_meal", "hour": 12},
             "b": {"name": "珍珠奶茶", "source_type": "user_meal_photo", "hour": 15}}
    payload = build_ai_draft_input(detail, facts)
    assert payload["分析指標"]["便當與外食"]["包月便當_餐數"] == 1
    assert payload["分析指標"]["飲料與點心"]["飲料_次數"] == 1
    meals = payload["三日紀錄"][0]["餐點"]
    assert meals[0]["來源"] == "包月便當" and meals[1]["來源"] == "顧客確認的照片估算"
    # 舊呼叫方式（只給品名）仍可用
    assert build_ai_draft_input(detail, {"a": "雞肉便當"})["三日紀錄"][0]["餐點"][0]["品名"] == "雞肉便當"


def test_prompt_asks_for_single_focus_and_forbids_recalculation():
    assert "這週只做這一件事：" in SYSTEM_PROMPT
    assert "不要自己加總" in SYSTEM_PROMPT and "你不是營養師" in SYSTEM_PROMPT
