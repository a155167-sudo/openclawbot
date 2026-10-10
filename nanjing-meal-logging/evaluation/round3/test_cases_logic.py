import importlib.util
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BANK = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
SPEC = importlib.util.spec_from_file_location("round3_runner_cases_logic", HERE / "runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

CORE_SOYMILK_TEXTS = {
    "無糖豆漿500ml",
    "午餐我喝了無糖豆漿500cc",
    "一杯大杯無糖豆漿",
}


def test_v3_bank_has_93_cases_and_minimum_three_phrasings_per_item():
    cases = BANK["cases"]
    per_item = Counter(case["item_key"] for case in cases)
    assert BANK["counts"] == {"items": 30, "cases": 93, "minimum_phrasings_per_item": 3}
    assert len(cases) == 93
    assert len(per_item) == 30
    assert min(per_item.values()) >= 3
    assert len({case["case_id"] for case in cases}) == len(cases)


def test_user_reported_soymilk_regressions_are_frozen_as_six_variants():
    soy = [case for case in BANK["cases"] if case["item_key"] == "unsweet_soymilk"]
    assert len(soy) == 6
    assert CORE_SOYMILK_TEXTS <= {case["user_text"] for case in soy}


def test_natural_counts_are_numbers_and_always_clarify_without_gram_fallback():
    natural = [case for case in BANK["cases"] if case["expected"]["item"]["unit"] == "natural"]
    assert natural
    for case in natural:
        amount = case["expected"]["item"]["amount"]
        assert isinstance(amount, (int, float)) and not isinstance(amount, bool), case["case_id"]
        assert case["expected"]["routing"]["status"] == "clarification", case["case_id"]
        assert case["expected"]["nutrition_evaluation"]["status"] == "NOT_SCORED", case["case_id"]


def test_relative_natural_semantics_are_never_saved_as_null_counts():
    relative_markers = ("半個", "一碗", "一杯", "一顆", "一根", "一份", "半條")
    relative = [
        case for case in BANK["cases"]
        if case["expected"]["item"]["unit"] == "natural"
        and any(marker in case["user_text"] for marker in relative_markers)
    ]
    assert relative
    assert all(case["expected"]["item"]["amount"] is not None for case in relative)


def test_runner_cardinality_contract_accepts_93_but_rejects_underfilled_items():
    runner.validate_case_bank(BANK)
    broken = json.loads(json.dumps(BANK))
    broken["cases"] = broken["cases"][:-1]
    broken["counts"]["cases"] -= 1
    try:
        runner.validate_case_bank(broken)
    except ValueError as exc:
        assert "minimum" in str(exc) or "cardinality" in str(exc)
    else:
        raise AssertionError("underfilled bank must fail")
