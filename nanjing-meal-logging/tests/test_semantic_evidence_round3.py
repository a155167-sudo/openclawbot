import json
from types import SimpleNamespace

import pytest

from semantic_meal_pipeline import parse_meal_semantics_openai, run_semantic_meal_pipeline


# Exact first-live parser response bodies retained as regression fixtures.
FIRST_REAL_RAW = {
    "R3-02-4": (
        "無糖豆漿500ml",
        '{"intent":"meal_log","meal_slot":"breakfast","items":[{"food_name":"無糖豆漿","amount":500,"unit":"ml","portion_assumption":""}],"clarification":""}',
    ),
    "R3-02-5": (
        "午餐我喝了無糖豆漿500cc",
        '{"intent":"meal_log","meal_slot":"lunch","items":[{"food_name":"無糖豆漿","amount":500,"unit":"cc","portion_assumption":""}],"clarification":""}',
    ),
    "R3-02-6": (
        "一杯大杯無糖豆漿",
        '{"intent":"meal_log","meal_slot":"breakfast","items":[{"food_name":"無糖豆漿","amount":240,"unit":"ml","portion_assumption":"一杯大杯的容量假設為240ml"}],"clarification":""}',
    ),
}


class RawCompletion:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="gpt-4o-mini-2024-07-18",
            id="first-real-trace",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=self.raw, refusal=None),
            )],
        )


def replay(case_id):
    text, raw = FIRST_REAL_RAW[case_id]
    completions = RawCompletion(raw)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return text, parse_meal_semantics_openai(client, text, case_id), completions


def test_bare_product_keeps_explicit_500ml_but_drops_guessed_breakfast():
    _text, parsed, _calls = replay("R3-02-4")

    assert parsed["meal_slot"] == ""
    assert parsed["items"][0] == {
        "food_name": "無糖豆漿",
        "amount": 500.0,
        "unit": "ml",
        "portion_assumption": "",
        "qty_origin": "explicit_measure",
        "qty_evidence": "500ml",
    }


def test_explicit_lunch_is_normalized_to_supported_chinese_slot_and_cc_to_ml():
    _text, parsed, _calls = replay("R3-02-5")

    assert parsed["meal_slot"] == "午餐"
    assert parsed["items"][0]["amount"] == 500
    assert parsed["items"][0]["unit"] == "ml"
    assert parsed["items"][0]["qty_origin"] == "explicit_measure"
    assert parsed["items"][0]["qty_evidence"] == "500cc"


def test_guessed_240ml_is_replaced_by_evidenced_natural_count_and_clarification():
    text, parsed, _calls = replay("R3-02-6")

    assert parsed["meal_slot"] == ""
    assert parsed["items"][0]["amount"] == 1
    assert parsed["items"][0]["unit"] == "natural"
    assert parsed["items"][0]["qty_origin"] == "natural_count"
    assert parsed["items"][0]["qty_evidence"] == "一杯"

    nutrition_calls = []
    result = run_semantic_meal_pipeline(
        text=text, user_id="U", message_id="R3-02-6",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: json.loads(FIRST_REAL_RAW["R3-02-6"][1]),
        find_reference_nutrition=lambda request: nutrition_calls.append(("reference", request)),
        estimate_per_100=lambda requests, _batch: nutrition_calls.append(("estimate", requests)),
    )
    assert result["status"] == "clarification"
    assert result["meal_slot"] == ""
    assert nutrition_calls == []
    assert "不是實測 g/ml" in result["clarifications"][0]


def test_schema_requires_structured_quantity_evidence_and_chinese_meal_slot_enum():
    _text, _parsed, completions = replay("R3-02-5")
    schema = completions.calls[0]["response_format"]["json_schema"]["schema"]
    item_schema = schema["properties"]["items"]["items"]

    assert {"qty_origin", "qty_evidence"} <= set(item_schema["required"])
    assert item_schema["properties"]["qty_origin"]["enum"] == [
        "explicit_measure", "natural_count"
    ]
    assert schema["properties"]["meal_slot"]["enum"] == [
        "", "早餐", "午餐", "晚餐", "點心"
    ]


@pytest.mark.parametrize(
    ("text", "provider_item", "expected"),
    [
        (
            "無糖豆漿500ml",
            {"food_name": "無糖豆漿", "amount": 700, "unit": "ml", "portion_assumption": "",
             "qty_origin": "explicit_measure", "qty_evidence": "700ml"},
            (500.0, "ml", "explicit_measure", "500ml"),
        ),
        (
            "半份排骨便當",
            {"food_name": "排骨便當", "amount": 500, "unit": "g", "portion_assumption": "假設500g"},
            (0.5, "natural", "natural_count", "半份"),
        ),
        (
            "雞胸肉1.5kg",
            {"food_name": "雞胸肉", "amount": 1.5, "unit": "kg", "portion_assumption": ""},
            (1500.0, "g", "explicit_measure", "1.5kg"),
        ),
    ],
)
def test_program_guard_uses_source_quantity_evidence_not_provider_guesses(text, provider_item, expected):
    result = run_semantic_meal_pipeline(
        text=text, user_id="U", message_id=text,
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: {
            "intent": "meal_log", "meal_slot": "breakfast", "items": [provider_item],
            "clarification": "",
        },
        find_reference_nutrition=lambda _request: {
            "status": "matched", "basis_amount": 100,
            "basis_unit": expected[1],
            "nutrition": {"calories_kcal": 1, "protein_g": 1, "fat_g": 1, "carbohydrate_g": 1},
        } if expected[1] != "natural" else None,
        estimate_per_100=lambda *_: pytest.fail("unexpected nutrition fallback"),
    )

    assert result["meal_slot"] == ""
    if expected[1] == "natural":
        assert result["status"] == "clarification"
    else:
        assert result["status"] == "draft"
        item = result["items"][0]["request"]
        assert (item["amount"], item["unit"], item["qty_origin"], item["qty_evidence"]) == expected
