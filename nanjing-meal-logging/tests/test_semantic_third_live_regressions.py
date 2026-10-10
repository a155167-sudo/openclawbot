import json
from types import SimpleNamespace

import pytest

import nutrition_reference
from semantic_meal_pipeline import parse_meal_semantics_openai, run_semantic_meal_pipeline


class RawCompletion:
    def __init__(self, payload):
        self.raw = json.dumps(payload, ensure_ascii=False)

    def create(self, **_kwargs):
        return SimpleNamespace(
            model="gpt-4o-mini-2024-07-18",
            id="third-live-offline-replay",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=self.raw, refusal=None),
            )],
        )


def parse_raw(text, item, *, meal_slot="早餐"):
    payload = {
        "intent": "meal_log",
        "meal_slot": meal_slot,
        "items": [item],
        "clarification": "",
    }
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=RawCompletion(payload)),
    )
    return parse_meal_semantics_openai(client, text, "offline-replay")


def item(food_name, food_name_evidence, amount, unit, qty_evidence, *, food_state=""):
    return {
        "food_name": food_name,
        "food_name_evidence": food_name_evidence,
        "food_state": food_state,
        "amount": amount,
        "unit": unit,
        "portion_assumption": "" if unit != "natural" else qty_evidence,
        "qty_origin": "explicit_measure" if unit != "natural" else "natural_count",
        "qty_evidence": qty_evidence,
    }


def test_third_live_rice_keeps_canonical_lookup_name_and_separate_raw_evidence():
    parsed = parse_raw(
        "早餐吃了150克白飯",
        item("白飯", "150克白飯", 150, "g", "150克"),
    )

    assert parsed["items"][0]["food_name"] == "白飯"
    assert parsed["items"][0]["food_name_evidence"] == "150克白飯"
    assert parsed["items"][0]["qty_evidence"] == "150克"


def test_third_live_soy_lookup_receives_canonical_name_and_uses_fixed_ml_reference():
    raw_item = item("無糖豆漿", "無糖豆漿500ml", 500, "ml", "500ml")
    parsed = parse_raw("無糖豆漿500ml當早餐", raw_item)
    assert parsed["items"][0]["food_name"] == "無糖豆漿"
    assert parsed["items"][0]["food_name_evidence"] == "無糖豆漿500ml"

    looked_up = []
    fallback_requests = []

    def lookup(request):
        looked_up.append(request)
        basis_request = {**request, "amount": 100}
        resolved = nutrition_reference.resolve_reference(basis_request)
        assert resolved is not None
        return {
            **resolved,
            "status": "matched",
            "basis_amount": resolved["source"]["basis_amount"],
            "basis_unit": resolved["source"]["basis_unit"],
        }

    def fallback(requests, _batch_id):
        fallback_requests.extend(requests)
        pytest.fail("fixed soy reference must not invoke AI fallback")

    result = run_semantic_meal_pipeline(
        text="無糖豆漿500ml當早餐",
        user_id="U",
        message_id="M",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: {
            "intent": "meal_log", "meal_slot": "早餐", "items": [raw_item],
            "clarification": "",
        },
        find_reference_nutrition=lookup,
        estimate_per_100=fallback,
    )

    assert looked_up[0]["food_name"] == "無糖豆漿"
    assert looked_up[0]["food_name_evidence"] == "無糖豆漿500ml"
    assert fallback_requests == []
    soy = result["items"][0]
    assert soy["nutrition"] == pytest.approx({
        "calories_kcal": 175, "protein_g": 18,
        "fat_g": 9.5, "carbohydrate_g": 3.5,
    })
    assert soy["source"]["publisher"] == "TFDA"
    assert soy["source"]["source_type"] == "government_food_composition_density_derived"


def test_natural_count_evidence_does_not_replace_food_name_or_trigger_lookup():
    raw_item = item("原味優格", "一杯原味優格", 1, "natural", "一杯")
    parsed = parse_raw("下午一杯原味優格", raw_item, meal_slot="點心")
    assert parsed["items"][0]["food_name"] == "原味優格"
    assert parsed["items"][0]["food_name_evidence"] == "一杯原味優格"
    assert parsed["items"][0]["amount"] == 1
    assert parsed["items"][0]["unit"] == "natural"

    result = run_semantic_meal_pipeline(
        text="下午一杯原味優格", user_id="U", message_id="M",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: {
            "intent": "meal_log", "meal_slot": "點心", "items": [raw_item],
            "clarification": "",
        },
        find_reference_nutrition=lambda *_: pytest.fail("natural count must not be looked up"),
        estimate_per_100=lambda *_: pytest.fail("natural count must not be estimated"),
    )
    assert result["status"] == "clarification"
    assert "一杯" in result["clarifications"][0]


def test_stateful_canonical_name_is_not_replaced_by_quantity_bearing_evidence():
    parsed = parse_raw(
        "晚餐吃200g去皮生雞胸",
        item(
            "去皮生雞胸", "200g去皮生雞胸", 200, "g", "200g",
            food_state="去皮、生",
        ),
        meal_slot="晚餐",
    )

    assert parsed["items"][0]["food_name"] == "去皮生雞胸"
    assert parsed["items"][0]["food_name_evidence"] == "200g去皮生雞胸"
    assert parsed["items"][0]["food_state"] == "去皮、生"


def test_digits_inside_brand_name_are_not_stripped_or_reconstructed_from_evidence():
    parsed = parse_raw(
        "早餐喝500ml 3點1刻奶茶",
        item("3點1刻奶茶", "500ml 3點1刻奶茶", 500, "ml", "500ml"),
    )

    assert parsed["items"][0]["food_name"] == "3點1刻奶茶"
    assert parsed["items"][0]["food_name_evidence"] == "500ml 3點1刻奶茶"


def test_unsourced_food_name_evidence_stays_only_in_raw_provider_audit():
    parsed = parse_raw(
        "早餐吃150克白飯",
        item("白飯", "250克白飯", 150, "g", "150克"),
    )

    parsed_item = parsed["items"][0]
    assert parsed_item["food_name"] == "白飯"
    assert "food_name_evidence" not in parsed_item
    assert "250克白飯" in parsed["_raw_provider_json"]
