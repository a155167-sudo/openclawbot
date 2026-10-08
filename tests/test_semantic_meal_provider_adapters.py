import json
from types import SimpleNamespace

from semantic_meal_pipeline import (
    estimate_per_100_openai,
    parse_meal_semantics_openai,
)


class FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="test-model",
            id="trace-1",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False), refusal=None),
            )],
        )


def _client(payload):
    completions = FakeCompletions(payload)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_parser_adapter_uses_strict_schema_without_nutrition_or_verb_dictionary():
    client, calls = _client({
        "intent": "meal_log", "meal_slot": "早餐", "clarification": "",
        "items": [{"food_name": "豆漿", "amount": 500, "unit": "cc",
                   "portion_assumption": "明示500cc"}],
    })
    parsed = parse_meal_semantics_openai(client, "早上喝豆漿500cc", "B1", model="m")
    assert parsed["items"][0]["unit"] == "ml"
    schema = calls.calls[0]["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]["items"]["items"]["properties"]) == {
        "food_name", "amount", "unit", "portion_assumption",
        "qty_origin", "qty_evidence", "food_name_evidence", "food_state",
    }
    assert "吃了" not in calls.calls[0]["messages"][0]["content"]


def test_fallback_adapter_batches_items_and_requires_identity_and_per_100_basis():
    payload = {"items": [{
        "item_id": "item-1", "food_name": "豆漿", "basis_amount": 100,
        "basis_unit": "ml",
        "calories_kcal": {"estimate": 35, "min": 25, "max": 45, "unit": "kcal"},
        "protein_g": {"estimate": 3, "min": 2, "max": 4, "unit": "g"},
        "fat_g": {"estimate": 2, "min": 1, "max": 3, "unit": "g"},
        "carbohydrate_g": {"estimate": 1, "min": 0.5, "max": 2, "unit": "g"},
    }]}
    client, calls = _client(payload)
    estimates = estimate_per_100_openai(
        client,
        [{"item_id": "item-1", "food_name": "豆漿", "amount": 100,
          "unit": "ml", "portion_assumption": ""}],
        "B1", model="m",
    )
    assert estimates[0]["basis_amount"] == 100
    assert estimates[0]["nutrition"]["calories_kcal"]["estimate"] == 35
    assert len(calls.calls) == 1
    assert calls.calls[0]["response_format"]["json_schema"]["strict"] is True