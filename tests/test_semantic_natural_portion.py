"""「排骨飯」沒寫克數：以一般份量估算成草稿，而不是要求實測 g/ml。"""
from semantic_meal_pipeline import run_semantic_meal_pipeline


def _run(text, items, grams=None, per100=None):
    calls = {"grams": [], "per100": []}

    def natural(asks, batch_id):
        calls["grams"].append(asks)
        return grams(asks) if callable(grams) else grams

    def estimate(misses, batch_id):
        calls["per100"].append(misses)
        return [{"item_id": m["item_id"], "food_name": m["food_name"], "basis_amount": 100,
                 "basis_unit": m["unit"], "nutrition": per100 or {
                     "calories_kcal": 180, "protein_g": 7, "fat_g": 6, "carbohydrate_g": 24}}
                for m in misses]

    result = run_semantic_meal_pipeline(
        text=text, user_id="u", message_id="m",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "b"},
        parse_semantics=lambda *_: {"intent": "meal_log", "meal_slot": "", "clarification": "",
                                    "items": items},
        find_reference_nutrition=lambda request: None,
        estimate_per_100=estimate,
        estimate_natural_grams=None if grams is None else natural,
    )
    return result, calls


PORK_RICE = [{"food_name": "排骨飯", "amount": 1, "unit": "natural", "portion_assumption": "一份"}]


def test_natural_portion_becomes_typical_grams_draft():
    result, calls = _run("午餐排骨飯", PORK_RICE,
                         grams=lambda asks: [{"item_id": a["item_id"], "grams_per_unit": 450} for a in asks])
    assert result["status"] == "draft"
    item = result["items"][0]
    assert item["request"]["unit"] == "g" and item["request"]["amount"] == 450
    assert "一般份量約 450 g（AI估算）" in item["request"]["portion_assumption"]
    assert item["nutrition"]["calories_kcal"] == 810  # 180 kcal/100 g × 450 g
    assert calls["per100"][0][0]["unit"] == "g"


def test_without_estimator_keeps_asking_for_measured_amount():
    result, _ = _run("午餐排骨飯", PORK_RICE)
    assert result["status"] == "clarification" and "不是實測 g/ml" in result["clarifications"][0]


def test_unreasonable_grams_asks_instead_of_guessing():
    result, calls = _run("午餐排骨飯", PORK_RICE,
                         grams=lambda asks: [{"item_id": a["item_id"], "grams_per_unit": 0} for a in asks])
    assert result["status"] == "clarification"
    assert "無法合理估算" in result["clarifications"][0] and not calls["per100"]


def test_measured_items_never_call_the_portion_estimator():
    result, calls = _run("午餐雞胸肉150g", [{"food_name": "雞胸肉", "amount": 150, "unit": "g"}],
                         grams=lambda asks: [])
    assert not calls["grams"] and result["status"] == "draft"


def test_mixed_dish_category_has_bounds():
    from nutrition_plausibility import assess_per100
    ok = assess_per100({"food_name": "排骨飯", "unit": "g"},
                       {"calories_kcal": 180, "protein_g": 8, "fat_g": 6, "carbohydrate_g": 24})
    too_high = assess_per100({"food_name": "排骨飯", "unit": "g"},
                             {"calories_kcal": 800, "protein_g": 8, "fat_g": 60, "carbohydrate_g": 24})
    assert ok["status"] == "pass" and ok["category"] == "外食便當／混合料理類"
    assert too_high["status"] == "requires_confirmation"
    # Specific categories still win (白飯 stays 主食澱粉類).
    assert assess_per100({"food_name": "白飯", "unit": "g"},
                         {"calories_kcal": 140, "protein_g": 2.5, "fat_g": 0.3, "carbohydrate_g": 31})["category"] == "主食澱粉類"


def test_overestimated_rice_is_shrunk_to_typical_cap():
    # 排骨飯 500 g × 180 kcal／P10 per 100 g = 900 kcal／P50 → capped at P35
    result, _ = _run("排骨飯", PORK_RICE,
                     grams=lambda asks: [{"item_id": a["item_id"], "grams_per_unit": 500} for a in asks],
                     per100={"calories_kcal": 180, "protein_g": 10, "fat_g": 6, "carbohydrate_g": 24})
    assert result["status"] == "draft"
    item = result["items"][0]
    assert item["request"]["amount"] == 350
    assert item["nutrition"]["calories_kcal"] == 630 and item["nutrition"]["protein_g"] == 35
    assert "一般份量約 350 g（AI估算，已依常見份量上限調整）" in item["request"]["portion_assumption"]


def test_overestimated_noodle_soup_uses_noodle_cap():
    # 牛肉麵 600 g × 180 kcal／P10 = 1080 kcal／P60 → capped by P35 (and ≤750 kcal)
    result, _ = _run("牛肉麵", [{"food_name": "牛肉麵", "amount": 1, "unit": "natural",
                                 "portion_assumption": "一碗"}],
                     grams=lambda asks: [{"item_id": a["item_id"], "grams_per_unit": 600} for a in asks],
                     per100={"calories_kcal": 180, "protein_g": 10, "fat_g": 6, "carbohydrate_g": 24})
    item = result["items"][0]
    assert result["status"] == "draft"
    assert item["nutrition"]["protein_g"] <= 35 and item["nutrition"]["calories_kcal"] <= 750


def test_reasonable_portion_is_untouched_and_measured_grams_never_capped():
    result, _ = _run("排骨飯", PORK_RICE,
                     grams=lambda asks: [{"item_id": a["item_id"], "grams_per_unit": 450} for a in asks],
                     per100={"calories_kcal": 160, "protein_g": 6, "fat_g": 6, "carbohydrate_g": 22})
    assert result["items"][0]["request"]["amount"] == 450
    assert "上限" not in result["items"][0]["request"]["portion_assumption"]
    measured, _ = _run("排骨飯600g", [{"food_name": "排骨飯", "amount": 600, "unit": "g"}],
                       per100={"calories_kcal": 180, "protein_g": 10, "fat_g": 6, "carbohydrate_g": 24})
    assert measured["items"][0]["nutrition"]["protein_g"] == 60


def test_caps_by_dish_type():
    from semantic_meal_pipeline import typical_portion_cap
    assert typical_portion_cap("牛肉麵") == (750, 35)
    assert typical_portion_cap("雞腿便當") == (850, 35)
    assert typical_portion_cap("菠蘿麵包") == (900, 40)
    assert typical_portion_cap("飯糰") == (900, 40)
