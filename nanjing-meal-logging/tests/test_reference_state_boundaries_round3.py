import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from semantic_meal_pipeline import estimate_per_100_openai, run_semantic_meal_pipeline


ROOT = Path(__file__).resolve().parents[1]


def load_reference():
    path = ROOT / "nutrition_reference.py"
    spec = importlib.util.spec_from_file_location("reference_state_boundaries_round3", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path
    return module


def test_cooked_yellow_sweet_potato_cannot_use_raw_reference_but_raw_control_can():
    reference = load_reference()

    cooked = reference.find_reference_nutrition({
        "food_name": "黃肉甘藷", "amount": 100, "unit": "g", "food_state": "熟",
    })
    assert cooked == {
        "status": "state_mismatch", "food_exists": True, "available_units": ["g"]
    }

    cooked_evidence_only = reference.find_reference_nutrition({
        "food_name": "黃肉甘藷", "food_name_evidence": "熟黃肉甘藷100g",
        "amount": 100, "unit": "g", "food_state": "",
    })
    assert cooked_evidence_only["status"] == "state_mismatch"

    raw = reference.find_reference_nutrition({
        "food_name": "黃肉甘藷", "food_name_evidence": "生黃肉甘藷100g",
        "amount": 100, "unit": "g", "food_state": "生、去皮",
    })
    assert raw["status"] == "matched"
    assert raw["source"]["food_code"] == "B0400601"
    assert "樣品狀態:生" in raw["source"]["state"]


@pytest.mark.parametrize("food_name, food_state", [
    ("黃肉甘藷", "生、帶皮"),
    ("黃肉甘藷", "生、未去皮"),
    ("冬粉", "熟重"),
    ("黃肉甘藷", "生、去皮、紫肉品種"),
    ("黃肉甘藷", "處理過"),
    ("帶膜花生", "熟、帶膜"),
])
def test_explicit_skin_weight_variety_processing_or_unknown_state_conflicts_fail_closed(
    food_name, food_state
):
    result = load_reference().find_reference_nutrition({
        "food_name": food_name, "amount": 100, "unit": "g", "food_state": food_state,
    })
    assert result == {
        "status": "state_mismatch", "food_exists": True, "available_units": ["g"]
    }


def test_state_tokens_do_not_misread_peanut_as_raw_or_skinless_as_skin_on():
    reference = load_reference()
    peanut = reference.find_reference_nutrition({
        "food_name": "帶膜花生", "amount": 100, "unit": "g", "food_state": "帶膜、生",
    })
    assert peanut["status"] == "matched"
    assert peanut["source"]["food_code"] == "C1705301"

    skinless = reference.find_reference_nutrition({
        "food_name": "黃肉甘藷", "amount": 100, "unit": "g", "food_state": "生、去皮",
    })
    assert skinless["status"] == "matched"

    skin_on = reference.find_reference_nutrition({
        "food_name": "蒸鯖魚", "amount": 100, "unit": "g", "food_state": "蒸、未去皮",
    })
    assert skin_on["status"] == "matched"
    assert skin_on["source"]["food_code"] == "J0414810"


@pytest.mark.parametrize("food_name, code", [
    ("生去皮黃肉甘藷", "B0400601"),
    ("乾冬粉", "R4600201"),
    ("白飯", "A0550601"),
    ("香蕉", "D08001"),
])
def test_legacy_authorized_source_scoped_names_without_food_state_remain_compatible(food_name, code):
    result = load_reference().find_reference_nutrition({
        "food_name": food_name, "amount": 100, "unit": "g",
    })
    assert result["status"] == "matched"
    assert result["source"]["food_code"] == code


def test_state_rejected_reference_reaches_ai_fallback_with_state_preserved_in_request_and_output():
    reference = load_reference()
    fallback_requests = []

    def estimate(requests, _batch_id):
        fallback_requests.extend(requests)
        request = requests[0]
        return [{
            "item_id": request["item_id"], "food_name": request["food_name"],
            "basis_amount": 100, "basis_unit": "g",
            "nutrition": {
                "calories_kcal": 90, "protein_g": 2, "fat_g": 0.2,
                "carbohydrate_g": 21,
            },
            "provider": "fake", "model": "cooked-food-model", "raw_trace_id": "N-state",
        }]

    result = run_semantic_meal_pipeline(
        text="晚餐吃熟黃肉甘藷100g", user_id="U", message_id="M-state",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B-state"},
        parse_semantics=lambda *_: {
            "intent": "meal_log", "meal_slot": "晚餐", "clarification": "",
            "items": [{
                "food_name": "黃肉甘藷", "food_name_evidence": "熟黃肉甘藷",
                "food_state": "熟", "amount": 100, "unit": "g",
                "portion_assumption": "",
            }],
        },
        find_reference_nutrition=reference.find_reference_nutrition,
        estimate_per_100=estimate,
    )

    assert fallback_requests[0]["food_name"] == "黃肉甘藷"
    assert fallback_requests[0]["food_state"] == "熟"
    assert result["items"][0]["source_label"] == "AI估算"
    assert result["items"][0]["request"]["food_state"] == "熟"
    assert result["items"][0]["source"]["food_state"] == "熟"
    assert result["items"][0]["source"].get("publisher") != "TFDA"


def test_multi_item_states_stay_isolated_between_official_match_and_fallback_spy():
    reference = load_reference()
    fallback_requests = []

    def estimate(requests, _batch_id):
        fallback_requests.extend(requests)
        return [{
            "item_id": request["item_id"], "food_name": request["food_name"],
            "basis_amount": 100, "basis_unit": request["unit"],
            "nutrition": {
                "calories_kcal": 90, "protein_g": 2, "fat_g": 0.2,
                "carbohydrate_g": 21,
            },
        } for request in requests]

    result = run_semantic_meal_pipeline(
        text="生去皮黃肉甘藷100g和熟黃肉甘藷200g", user_id="U", message_id="M-multi-state",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B-multi-state"},
        parse_semantics=lambda *_: {
            "intent": "meal_log", "meal_slot": "晚餐", "clarification": "",
            "items": [
                {"food_name": "黃肉甘藷", "food_state": "生、去皮", "amount": 100,
                 "unit": "g", "portion_assumption": ""},
                {"food_name": "黃肉甘藷", "food_state": "熟", "amount": 200,
                 "unit": "g", "portion_assumption": ""},
            ],
        },
        find_reference_nutrition=reference.find_reference_nutrition,
        estimate_per_100=estimate,
    )

    assert [(request["item_id"], request["food_state"]) for request in fallback_requests] == [
        ("item-1", "熟")
    ]
    assert [item["source_label"] for item in result["items"]] == ["官方資料", "AI估算"]
    assert [item["request"]["food_state"] for item in result["items"]] == ["生、去皮", "熟"]
    assert result["items"][1]["nutrition"]["calories_kcal"] == 180


class CapturingCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = {"items": [{
            "item_id": "item-0", "food_name": "黃肉甘藷", "basis_amount": 100,
            "basis_unit": "g",
            "calories_kcal": {"estimate": 90, "min": 75, "max": 110, "unit": "kcal"},
            "protein_g": {"estimate": 2, "min": 1, "max": 3, "unit": "g"},
            "fat_g": {"estimate": 0.2, "min": 0, "max": 1, "unit": "g"},
            "carbohydrate_g": {"estimate": 21, "min": 17, "max": 26, "unit": "g"},
        }]}
        return SimpleNamespace(
            model="test-model", id="trace-state",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), refusal=None),
            )],
        )


def test_openai_per_100_transport_includes_food_state_in_actual_provider_request():
    completions = CapturingCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    estimate_per_100_openai(client, [{
        "item_id": "item-0", "food_name": "黃肉甘藷",
        "food_name_evidence": "熟黃肉甘藷100g", "food_state": "熟",
        "amount": 100, "unit": "g",
    }], "B-state")

    provider_requests = json.loads(completions.calls[0]["messages"][1]["content"])
    assert provider_requests == [{
        "item_id": "item-0", "food_name": "黃肉甘藷",
        "food_name_evidence": "熟黃肉甘藷100g", "food_state": "熟",
        "amount": 100, "unit": "g",
    }]
    assert "food_state" in completions.calls[0]["messages"][0]["content"]
