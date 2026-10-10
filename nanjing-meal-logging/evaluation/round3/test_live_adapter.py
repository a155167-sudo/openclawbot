import importlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


class SpyCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        call = len(self.calls)
        if call == 1:
            payload = {
                "intent": "meal_log", "meal_slot": "早餐", "clarification": "",
                "items": [{"food_name": "無糖豆漿", "amount": 250, "unit": "ml", "portion_assumption": ""}],
            }
        else:
            nutrient = lambda value, unit: {"estimate": value, "min": value, "max": value, "unit": unit}
            payload = {"items": [{
                "item_id": "item-0", "food_name": "無糖豆漿", "basis_amount": 100, "basis_unit": "ml",
                "calories_kcal": nutrient(40, "kcal"), "protein_g": nutrient(3, "g"),
                "fat_g": nutrient(2, "g"), "carbohydrate_g": nutrient(2.5, "g"),
            }]}
        usage = SimpleNamespace(prompt_tokens=10 * call, completion_tokens=5 * call, total_tokens=15 * call)
        return SimpleNamespace(
            id=f"resp-{call}", model="provider-model", usage=usage,
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload), refusal=None))],
        )


class SpyClient:
    def __init__(self):
        self.spy = SpyCompletions()
        self.chat = SimpleNamespace(completions=self.spy)


class NaturalPortionSpyCompletions:
    def __init__(self, *, intent="meal_log"):
        self.calls = []
        self.intent = intent

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = {
            "intent": self.intent, "meal_slot": "早餐", "clarification": "",
            "items": ([{"food_name": "無糖豆漿", "amount": 1, "unit": "natural",
                       "portion_assumption": ""}] if self.intent == "meal_log" else []),
        }
        return SimpleNamespace(
            id="natural-response", model="provider-model", usage=None,
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=json.dumps(payload), refusal=None))],
        )


class NaturalPortionSpyClient:
    def __init__(self, *, intent="meal_log"):
        self.spy = NaturalPortionSpyCompletions(intent=intent)
        self.chat = SimpleNamespace(completions=self.spy)


def load_adapter(monkeypatch, client):
    adapter = importlib.import_module("evaluation.round3.live_adapter")
    monkeypatch.setattr(adapter, "_make_client", lambda: client)
    return adapter


def test_live_adapter_uses_only_user_text_and_real_pipeline_trace(monkeypatch):
    client = SpyClient()
    adapter = load_adapter(monkeypatch, client)
    output = adapter.run_case("早餐喝250ml無糖豆漿", {"mode": "live", "case_id": "SECRET-GOLD-ID"})

    assert len(client.spy.calls) == 2
    sent = json.dumps(client.spy.calls, ensure_ascii=False)
    assert "早餐喝250ml無糖豆漿" in sent
    assert "SECRET-GOLD-ID" not in sent
    assert "expected" not in sent.lower()
    assert all(call["timeout"] == 20 for call in client.spy.calls)
    assert all(call["max_tokens"] <= 900 for call in client.spy.calls)

    assert output["execution"]["kind"] == "live_ai"
    assert output["execution"]["provider_calls"] == 2
    assert output["execution"]["estimated_cost_usd"] == "unknown"
    assert output["parsed"]["items"][0]["food_name"] == "無糖豆漿"
    assert "food_state" not in output["parsed"]["items"][0]
    assert output["basis"][0]["status"] == "unit_mismatch"
    assert output["basis"][0]["basis_unit"] == "ml"
    assert output["basis"][0]["nutrition"]["calories_kcal"] == 40
    assert output["basis"][0]["unit_warning"]
    assert output["source"][0]["type"] == "ai"
    assert output["final"][0]["nutrition"]["calories_kcal"] == 100
    assert output["raw"][0]["response_id"] == "resp-1"
    assert output["raw"][1]["usage"]["total_tokens"] == 30
    assert output["model"] == {"semantic": "provider-model", "nutrition": "provider-model"}
    assert output["usage"]["total_tokens"] == 45

    # Exercise the real evaluator/report row shape with only spy-provider data.
    runner_path = Path(__file__).with_name("runner.py")
    spec = importlib.util.spec_from_file_location("round3_report_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    bank = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))
    row = runner.evaluate(bank["cases"][0], output, 12.5)
    report = {
        "schema_version": "round3-eval-report-v1",
        "execution_mode": "live",
        "usage": output["usage"],
        "summary": runner.summarize([row], "live"),
        "results": [row],
    }
    assert report["summary"]["truth_label"] == "LIVE_AI_RESULTS"
    assert set(report["results"][0]) >= {"raw", "parsed", "pipeline", "basis", "final", "source", "model", "usage", "evaluation"}
    assert report["results"][0]["usage"]["total_tokens"] == 45


def test_adapter_refuses_non_live_context_before_client(monkeypatch):
    adapter = importlib.import_module("evaluation.round3.live_adapter")
    monkeypatch.setattr(adapter, "_make_client", lambda: (_ for _ in ()).throw(AssertionError("must not create client")))
    with pytest.raises(ValueError, match="live"):
        adapter.run_case("早餐喝豆漿", {"mode": "harness"})


def test_adapter_client_configuration(monkeypatch):
    adapter = importlib.import_module("evaluation.round3.live_adapter")
    captured = {}

    class OpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "staging-secret")
    monkeypatch.setattr(adapter, "_openai_class", lambda: OpenAI)
    adapter._make_client()
    assert captured == {"api_key": "staging-secret", "timeout": 20, "max_retries": 0}


def test_natural_portion_pipeline_clarification_is_separate_and_traceable(monkeypatch):
    client = NaturalPortionSpyClient()
    adapter = load_adapter(monkeypatch, client)
    output = adapter.run_case("早餐喝一杯無糖豆漿", {"mode": "live", "case_id": "R3-02-6"})

    assert len(client.spy.calls) == 1
    assert output["parsed"]["clarification"] == ""
    assert output["parsed"]["items"][0]["unit"] == "natural"
    assert output["pipeline"]["status"] == "clarification"
    assert output["pipeline"]["clarifications"] == [
        "「無糖豆漿」不是實測 g/ml，容量或重量未知；請補充實測份量。"
    ]
    assert output["basis"] == [{"status": "clarification"}]
    assert output["final"] == []

    runner_path = Path(__file__).with_name("runner.py")
    spec = importlib.util.spec_from_file_location("round3_natural_runner", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    bank = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))
    case = next(row for row in bank["cases"] if row["case_id"] == "R3-02-6")
    evaluated = runner.evaluate(case, output, 1)
    assert evaluated["parsed"] == output["parsed"]
    assert evaluated["pipeline"] == output["pipeline"]
    assert evaluated["evaluation"]["routing"]["checks"]["clarification_present"] is True
    assert evaluated["evaluation"]["parsing"]["checks"]["portion_assumption"] is False


def test_not_meal_pipeline_is_not_mislabeled_as_clarification(monkeypatch):
    output = load_adapter(monkeypatch, NaturalPortionSpyClient(intent="other")).run_case(
        "今天天氣很好", {"mode": "live", "case_id": "negative"})
    assert output["pipeline"] == {"status": "not_meal", "clarifications": []}
    assert output["basis"] == []
