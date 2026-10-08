import pytest

from semantic_meal_pipeline import PipelineDenied, run_semantic_meal_pipeline


def test_ai_parse_official_first_and_one_batch_claim_without_persistence():
    calls = []

    def claim(user_id, message_id):
        calls.append(("claim", user_id, message_id))
        return {"allowed": True, "batch_id": "B1"}

    def parse(text, batch_id):
        calls.append(("parse", text, batch_id))
        return {
            "intent": "meal_log",
            "meal_slot": "午餐",
            "items": [{
                "food_name": "無糖豆漿", "amount": 500, "unit": "ml",
                "portion_assumption": "",
            }],
            "clarification": "",
            "provider": "fake", "model": "semantic-v1", "raw_trace_id": "P1",
        }

    def find_reference(request):
        calls.append(("reference", dict(request)))
        return {
            "status": "matched", "food_name": "無糖豆漿",
            "basis_amount": 100, "basis_unit": "ml",
            "nutrition": {
                "calories_kcal": 35, "protein_g": 3.3,
                "fat_g": 1.8, "carbohydrate_g": 1.2,
            },
            "source": {"dataset": "TFDA"},
        }

    def fallback(_misses, _batch_id):
        raise AssertionError("official match must not invoke nutrition fallback")

    result = run_semantic_meal_pipeline(
        text="午餐我喝了無糖豆漿500cc", user_id="U1", message_id="M1",
        claim_batch=claim, parse_semantics=parse,
        find_reference_nutrition=find_reference, estimate_per_100=fallback,
    )

    assert calls[0][0] == "claim"
    assert [call[0] for call in calls].count("claim") == 1
    assert result["status"] == "draft"
    assert result["items"][0]["nutrition"]["calories_kcal"] == 175
    assert result["items"][0]["source_label"] == "官方資料"
    assert result["writes_food_log"] is False
    assert result["audit"][0]["stage"] == "semantic_parse"


def test_true_misses_are_one_per_100_batch_and_scaled_in_program_code():
    fallback_calls = []

    def parse(_text, _batch_id):
        return {
            "intent": "meal_log", "meal_slot": "午餐", "clarification": "",
            "items": [
                {"food_name": "青菜", "amount": 250, "unit": "g", "portion_assumption": ""},
                {"food_name": "普通豆漿", "amount": 500, "unit": "ml", "portion_assumption": ""},
            ],
            "provider": "fake", "model": "parser", "raw_trace_id": "P2",
        }

    def estimate(misses, batch_id):
        fallback_calls.append((misses, batch_id))
        assert [(x["amount"], x["unit"]) for x in misses] == [(100, "g"), (100, "ml")]
        return [
            {"item_id": misses[0]["item_id"], "food_name": "青菜", "basis_amount": 100, "basis_unit": "g",
             "nutrition": {"calories_kcal": 40, "protein_g": 2, "fat_g": 1, "carbohydrate_g": 5},
             "provider": "fake", "model": "nutrition", "raw_trace_id": "N1"},
            {"item_id": misses[1]["item_id"], "food_name": "普通豆漿", "basis_amount": 100, "basis_unit": "ml",
             "nutrition": {"calories_kcal": 30, "protein_g": 3, "fat_g": 1.5, "carbohydrate_g": 2},
             "provider": "fake", "model": "nutrition", "raw_trace_id": "N2"},
        ]

    result = run_semantic_meal_pipeline(
        text="午餐青菜250g和普通豆漿500ml", user_id="U1", message_id="M2",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B2"},
        parse_semantics=parse, find_reference_nutrition=lambda _request: None,
        estimate_per_100=estimate,
    )

    assert len(fallback_calls) == 1
    assert len(result["items"]) == 2
    assert result["items"][0]["nutrition"]["calories_kcal"] == 100
    assert result["items"][1]["nutrition"]["protein_g"] == 15
    assert {item["source_label"] for item in result["items"]} == {"AI估算"}
    assert [event["stage"] for event in result["audit"]] == [
        "semantic_parse", "nutrition_fallback", "nutrition_fallback"
    ]


def test_incompatible_core_official_unit_requires_clarification_without_ai_fallback():
    fallback_requests = []

    def fallback(requests, _batch_id):
        fallback_requests.extend(requests)
        return [{
            "item_id": requests[0]["item_id"], "food_name": "白飯",
            "basis_amount": 100, "basis_unit": "ml",
            "nutrition": {
                "calories_kcal": {"estimate": 20, "min": 10, "max": 30, "unit": "kcal"},
                "protein_g": {"estimate": 1, "min": 0.5, "max": 2, "unit": "g"},
                "fat_g": {"estimate": 0.2, "min": 0.1, "max": 0.4, "unit": "g"},
                "carbohydrate_g": {"estimate": 4, "min": 2, "max": 6, "unit": "g"},
            },
        }]

    result = run_semantic_meal_pipeline(
        text="白飯500ml", user_id="U1", message_id="M3",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B3"},
        parse_semantics=lambda *_: {
            "intent": "meal_log", "meal_slot": "午餐", "clarification": "",
            "items": [{"food_name": "白飯", "amount": 500, "unit": "ml", "portion_assumption": ""}],
            "provider": "fake", "model": "parser", "raw_trace_id": "P3",
        },
        find_reference_nutrition=lambda _request: {
            "status": "unit_mismatch", "food_exists": True, "available_units": ["g"]
        },
        estimate_per_100=fallback,
    )

    assert result["status"] == "clarification"
    assert fallback_requests == []
    assert "ml" in result["clarifications"][0]
    assert "g" in result["clarifications"][0]


def test_denied_batch_never_calls_ai():
    called = []
    with pytest.raises(PipelineDenied):
        run_semantic_meal_pipeline(
            text="午餐豆漿500ml", user_id="U1", message_id="M4",
            claim_batch=lambda *_: {"allowed": False, "batch_id": ""},
            parse_semantics=lambda *_: called.append("parse"),
            find_reference_nutrition=lambda *_: called.append("reference"),
            estimate_per_100=lambda *_: called.append("estimate"),
        )
    assert called == []


def test_mixed_official_and_miss_keeps_original_item_alignment():
    parsed_items = [
        {"food_name": "白飯", "amount": 100, "unit": "g", "portion_assumption": ""},
        {"food_name": "豆漿", "amount": 500, "unit": "ml", "portion_assumption": ""},
    ]
    result = run_semantic_meal_pipeline(
        text="白飯100g和豆漿500ml", user_id="U", message_id="MIXED",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: {"intent": "meal_log", "meal_slot": "午餐",
            "items": parsed_items, "clarification": ""},
        find_reference_nutrition=lambda request: ({
            "status": "matched", "basis_amount": 100, "basis_unit": "g",
            "nutrition": {"calories_kcal": 180, "protein_g": 3, "fat_g": 0.3,
                          "carbohydrate_g": 40}, "source": {"dataset": "TFDA"},
        } if request["food_name"] == "白飯" else None),
        estimate_per_100=lambda misses, _batch: [{
            "item_id": misses[0]["item_id"], "food_name": "豆漿",
            "basis_amount": 100, "basis_unit": "ml",
            "nutrition": {"calories_kcal": 35, "protein_g": 3.3, "fat_g": 1.8,
                          "carbohydrate_g": 1.2},
        }],
    )
    assert [item["request"]["food_name"] for item in result["items"]] == ["白飯", "豆漿"]
    assert result["items"][1]["request"]["amount"] == 500
    assert result["items"][1]["nutrition"]["calories_kcal"] == 175


@pytest.mark.parametrize("changed", ["item_id", "food_name", "basis_amount", "basis_unit"])
def test_fallback_rejects_identity_or_basis_contract_violation(changed):
    def fallback(misses, _batch):
        estimate = {
            "item_id": misses[0]["item_id"], "food_name": "豆漿",
            "basis_amount": 100, "basis_unit": "ml",
            "nutrition": {"calories_kcal": 35, "protein_g": 3, "fat_g": 2,
                          "carbohydrate_g": 1},
        }
        estimate[changed] = {"item_id": "wrong", "food_name": "牛奶",
                             "basis_amount": 1, "basis_unit": "g"}[changed]
        return [estimate]

    with pytest.raises(ValueError, match="識別|基準|單位|品項"):
        run_semantic_meal_pipeline(
            text="豆漿500ml", user_id="U", message_id=f"BAD-{changed}",
            claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
            parse_semantics=lambda *_: {"intent": "meal_log", "meal_slot": "早餐",
                "items": [{"food_name": "豆漿", "amount": 500, "unit": "ml",
                           "portion_assumption": ""}], "clarification": ""},
            find_reference_nutrition=lambda _: None, estimate_per_100=fallback,
        )


def test_nested_range_units_are_preserved_while_numbers_scale():
    result = run_semantic_meal_pipeline(
        text="豆漿500ml", user_id="U", message_id="RANGE",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: {"intent": "meal_log", "meal_slot": "早餐",
            "items": [{"food_name": "豆漿", "amount": 500, "unit": "ml",
                       "portion_assumption": ""}], "clarification": ""},
        find_reference_nutrition=lambda _: None,
        estimate_per_100=lambda misses, _: [{
            "item_id": misses[0]["item_id"], "food_name": "豆漿",
            "basis_amount": 100, "basis_unit": "ml",
            "nutrition": {
                "calories_kcal": {"estimate": 35, "min": 25, "max": 45, "unit": "kcal"},
                "protein_g": {"estimate": 3, "min": 2, "max": 4, "unit": "g"},
                "fat_g": {"estimate": 2, "min": 1, "max": 3, "unit": "g"},
                "carbohydrate_g": {"estimate": 1, "min": 0, "max": 2, "unit": "g"},
            },
        }],
    )
    calories = result["items"][0]["nutrition"]["calories_kcal"]
    assert calories == {"estimate": 175, "min": 125, "max": 225, "unit": "kcal"}
