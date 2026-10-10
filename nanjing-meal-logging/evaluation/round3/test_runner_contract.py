import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("round3_runner", HERE / "runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
BANK = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def case_with(route):
    return next(c for c in BANK["cases"] if c["expected"]["routing"]["status"] == route)


def output_for(case, *, warning="官方僅有 g；改用 AI 每100 ml估算，未將 g 當 ml", basis_amount=100,
               source_type="ai", basis_unit=None, final_factor=None):
    exp = case["expected"]
    item = exp["item"]
    unit = basis_unit or item["unit"]
    nutrition = {"calories_kcal": 20, "protein_g": 1, "fat_g": 0.2, "carbohydrate_g": 4}
    factor = item["amount"] / 100 if final_factor is None else final_factor
    return {
        "execution": {"kind": "fake_harness", "provider_calls": 0},
        "raw": {"notice": "test"},
        "parsed": {"intent": exp["intent"], "meal_slot": exp["meal_slot"], "clarification": "",
                   "items": [{"food_name": item["acceptable_food_names"][0], "food_state": item["food_state"],
                              "amount": item["amount"], "unit": item["unit"],
                              "portion_assumption": "assumption" if item["portion_assumption"] != "empty" else ""}]},
        "basis": [{"status": exp["routing"]["status"], "basis_amount": basis_amount,
                   "basis_unit": unit, "nutrition": nutrition, "unit_warning": warning}],
        "final": [{"amount": item["amount"], "unit": item["unit"],
                   "nutrition": {k: v * factor for k, v in nutrition.items()}}],
        "source": [{"type": source_type, "provider": "test-provider", "model": "test-model",
                    "basis_unit": unit}],
        "model": {"semantic": "test", "nutrition": "test"},
    }


def test_unit_mismatch_ai_fallback_route_and_scaling_pass():
    case = case_with("unit_mismatch")
    result = runner.evaluate(case, output_for(case), 1)
    assert result["evaluation"]["routing"]["pass"] is True
    assert result["evaluation"]["nutrition"]["status"] == "NOT_SCORED"
    assert result["evaluation"]["nutrition"]["pass"] is None
    assert result["evaluation"]["scaling"]["scaling_arithmetic_pass"] is True


def test_fallback_eligible_checks_ai_basis_all_nutrients_and_scaling():
    case = next(c for c in BANK["cases"] if c["case_id"] == "R3-08-2")
    result = runner.evaluate(case, output_for(case, warning=""), 1)
    assert result["evaluation"]["routing"]["pass"] is True
    assert result["evaluation"]["scaling"]["scaling_arithmetic_pass"] is True


def test_unit_mismatch_masquerading_as_official_or_wrong_unit_fails():
    case = case_with("unit_mismatch")
    result = runner.evaluate(case, output_for(case, source_type="official", basis_unit="g"), 1)
    assert result["evaluation"]["routing"]["pass"] is False
    assert result["evaluation"]["critical_failures"]


def test_unit_mismatch_without_warning_fails():
    case = case_with("unit_mismatch")
    assert runner.evaluate(case, output_for(case, warning=""), 1)["evaluation"]["routing"]["pass"] is False


def test_ai_fallback_basis_must_be_100():
    case = case_with("unit_mismatch")
    assert runner.evaluate(case, output_for(case, basis_amount=500), 1)["evaluation"]["routing"]["pass"] is False


def test_ai_fallback_wrong_arithmetic_fails_scaling():
    case = case_with("unit_mismatch")
    result = runner.evaluate(case, output_for(case, final_factor=1), 1)
    assert result["evaluation"]["scaling"]["scaling_arithmetic_pass"] is False


def test_freeze_covers_runner_and_live_adapter():
    assert runner.EVALUATOR_PROJECTION == "v4"
    frozen = runner.read_freeze()
    assert "runner.py" in frozen
    assert "live_adapter.py" in frozen
    assert runner.verify_freeze()["runner.py"] == runner.sha256(HERE / "runner.py")


def test_clarification_route_accepts_only_real_pipeline_clarification_and_reports_it():
    case = case_with("clarification")
    output = output_for(case)
    output["parsed"]["clarification"] = ""
    output["pipeline"] = {"status": "clarification", "clarifications": ["請補充容量"]}
    output["final"] = []
    result = runner.evaluate(case, output, 1)
    assert result["pipeline"] == output["pipeline"]
    assert result["evaluation"]["routing"]["checks"]["clarification_present"] is True


def test_clarification_route_rejects_empty_or_wrongly_typed_pipeline_messages():
    case = case_with("clarification")
    for pipeline in (
        {"status": "clarification", "clarifications": ["  "]},
        {"status": "clarification", "clarifications": [{"message": "請補充容量"}]},
        {"status": "clarification", "clarifications": [123]},
        {"status": "not_meal", "clarifications": ["請補充容量"]},
    ):
        output = output_for(case)
        output["parsed"]["clarification"] = ""
        output["pipeline"] = pipeline
        output["final"] = []
        routing = runner.evaluate(case, output, 1)["evaluation"]["routing"]
        assert routing["checks"]["clarification_present"] is False
        assert routing["pass"] is False
