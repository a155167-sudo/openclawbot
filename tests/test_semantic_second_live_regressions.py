import json
from types import SimpleNamespace

from semantic_meal_pipeline import parse_meal_semantics_openai, run_semantic_meal_pipeline


# Exact semantic response bodies observed in the second live run.  These fixtures
# test the product parser contract without another provider call.
SECOND_LIVE_RAW = {
    "explicit_rice": (
        "早餐吃了150克白飯",
        '{"intent":"meal_log","meal_slot":"早餐","items":[{"food_name":"白飯","amount":150,"unit":"g","portion_assumption":"自然份量","qty_origin":"explicit_measure","qty_evidence":"150克"}],"clarification":""}',
    ),
    "explicit_soy": (
        "無糖豆漿500ml當早餐",
        '{"intent":"meal_log","meal_slot":"早餐","items":[{"food_name":"無糖豆漿","amount":500,"unit":"ml","portion_assumption":"無糖豆漿500ml","qty_origin":"explicit_measure","qty_evidence":"500ml"}],"clarification":""}',
    ),
    "natural_cup": (
        "下午一杯原味優格",
        '{"intent":"meal_log","meal_slot":"午餐","items":[{"food_name":"原味優格","amount":1,"unit":"natural","portion_assumption":"一杯","qty_origin":"natural_count","qty_evidence":"一杯原味優格"}],"clarification":""}',
    ),
}


class RawCompletion:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="gpt-4o-mini-2024-07-18", id="second-live-trace",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=self.raw, refusal=None),
            )],
        )


def replay(key):
    text, raw = SECOND_LIVE_RAW[key]
    completions = RawCompletion(raw)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return text, parse_meal_semantics_openai(client, text, key), completions


def test_explicit_measure_cannot_be_relabelled_as_a_portion_assumption():
    for key in ("explicit_rice", "explicit_soy"):
        _text, parsed, _ = replay(key)
        item = parsed["items"][0]
        assert item["qty_origin"] == "explicit_measure"
        assert item["qty_evidence"]
        assert item["portion_assumption"] == ""


def test_natural_cup_keeps_count_and_product_pipeline_asks_for_unknown_capacity():
    text, parsed, _ = replay("natural_cup")
    assert parsed["meal_slot"] == "點心"
    assert parsed["items"][0]["amount"] == 1
    assert parsed["items"][0]["unit"] == "natural"
    assert parsed["items"][0]["qty_evidence"] == "一杯"

    result = run_semantic_meal_pipeline(
        text=text, user_id="U", message_id="M",
        claim_batch=lambda *_: {"allowed": True, "batch_id": "B"},
        parse_semantics=lambda *_: json.loads(SECOND_LIVE_RAW["natural_cup"][1]),
        find_reference_nutrition=lambda *_: (_ for _ in ()).throw(AssertionError("no nutrition lookup")),
        estimate_per_100=lambda *_: (_ for _ in ()).throw(AssertionError("no nutrition estimate")),
    )
    assert result["status"] == "clarification"
    assert "一杯" in result["clarifications"][0]
    assert "g/ml" in result["clarifications"][0]


def test_schema_preserves_source_food_phrase_and_state_as_evidence():
    raw = json.dumps({
        "intent": "meal_log", "meal_slot": "晚餐", "clarification": "",
        "items": [{
            # Canonical lookup name must itself retain state qualifiers; the
            # evidence quote independently retains the exact quantity-bearing
            # source span and must not be promoted into the lookup key.
            "food_name": "去皮生雞胸", "food_name_evidence": "200g去皮生雞胸",
            "food_state": "去皮、生", "amount": 200, "unit": "g",
            "portion_assumption": "", "qty_origin": "explicit_measure",
            "qty_evidence": "200g",
        }],
    }, ensure_ascii=False)
    completions = RawCompletion(raw)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    parsed = parse_meal_semantics_openai(client, "200g去皮生雞胸算晚餐", "state")

    assert parsed["items"][0]["food_name"] == "去皮生雞胸"
    assert parsed["items"][0]["food_name_evidence"] == "200g去皮生雞胸"
    assert parsed["items"][0]["food_state"] == "去皮、生"

    schema = completions.calls[0]["response_format"]["json_schema"]["schema"]
    props = schema["properties"]["items"]["items"]["properties"]
    assert {"food_name_evidence", "food_state"} <= set(props)
    prompt = completions.calls[0]["messages"][0]["content"]
    assert "限定詞" in prompt
    assert "尚未烹煮" in prompt
