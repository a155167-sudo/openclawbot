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
                     "calories_kcal": 180, "protein_g": 8, "fat_g": 6, "carbohydrate_g": 24}}
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
