import copy
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("user_acceptance_v5", HERE / "user_acceptance_v5.py")
v5 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v5)
POLICY = json.loads((HERE / "user_acceptance_v5.json").read_text(encoding="utf-8"))


def _ai_output(case, nutrition=None, user_text="晚餐去皮生雞胸200g"):
    nutrition = nutrition or {"calories_kcal": 119, "protein_g": 23.3, "fat_g": 2.1, "carbohydrate_g": 0}
    return {
        "case_id": case["case_id"], "user_text": user_text, "status": "COMPLETED",
        "raw": {"provider": "captured"},
        "parsed": {"items": [{"food_name": "生去皮雞胸肉", "food_state": "生、去皮", "amount": 200, "unit": "g"}]},
        # These strings deliberately are not accepted as renderer proof.
        "pipeline": {"status": "draft", "card_labels": ["AI估算"]},
        "basis": [{"status": "fallback_eligible", "basis_amount": 100, "basis_unit": "g", "nutrition": nutrition}],
        "final": [{"amount": 200, "unit": "g", "nutrition": {k: value * 2 for k, value in nutrition.items()}}],
        "source": [{"type": "ai", "basis_unit": "g", "display_label": "AI估算"}],
    }


def _card_evidence(output, labels=("AI估算",)):
    return {
        "user_text": output["user_text"],
        "pipeline_status": output["pipeline"]["status"],
        "flex": {"type": "bubble", "body": {"type": "box", "contents": [
            {"type": "text", "text": label} for label in labels
        ]}},
    }


def test_policy_has_exactly_33_rows_in_three_authorized_categories():
    rows = POLICY["missing_33"]
    assert len(rows) == 33
    assert len({row["case_id"] for row in rows}) == 33
    assert {row["category"] for row in rows} == {1, 2, 3}
    assert {category: sum(row["category"] == category for row in rows) for category in (1, 2, 3)} == {1: 11, 2: 21, 3: 1}
    assert all(row["classification_basis"].strip() for row in rows)


def test_policy_pins_legacy_gold_without_modifying_it():
    assert POLICY["legacy_freeze"]["files"] == {
        "cases.json": "8d8a3acb940fcd7937977aff3eadc49074198e3341716523ba0f7d08f67203dc",
        "rubric.md": "86fb4bd9cd436dc5e9d61853f041f7ce231731ab74a3f405086f5352b9ecf943",
        "runner.py": "d6e4aede15005b628bad919fa78c24c1f0b8fab9bea11d423e85e0ae3b070183",
        "live_adapter.py": "9cadef0a729ef53913772fff15620e1d3cf5660da070a72556743343c927dd0a",
    }
    v5.verify_legacy_freeze(POLICY, HERE)


def test_policy_ranges_are_independent_and_unknown_is_not_passable():
    for benchmark in POLICY["benchmarks"].values():
        assert benchmark["derived_without_live_ai_output"] is True
        if benchmark["verification_status"] == "VERIFIED":
            assert benchmark["basis_unit"] in {"g", "ml"}
            assert set(benchmark["ranges"]) == set(v5.NUTRIENTS)
            assert all(r["min"] <= r["max"] for r in benchmark["ranges"].values())
            assert benchmark["sources"]
        else:
            assert benchmark["verification_status"] == "UNVERIFIED"
            assert benchmark["ranges"] is None


def test_category_two_real_pipeline_clarification_passes_without_fake_nutrition():
    case = next(row for row in POLICY["missing_33"] if row["category"] == 2)
    output = {
        "case_id": case["case_id"], "status": "COMPLETED", "raw": {},
        "parsed": {"clarification": "請補充品牌或完整食品狀態", "items": []},
        "pipeline": {"status": "clarification", "clarifications": ["請補充品牌或完整食品狀態"]},
        "basis": [{"status": "clarification"}], "final": [], "source": [],
    }
    result = v5.evaluate_result(case, output, POLICY)
    assert result["v5_verdict"] == "PASS"
    assert result["accepted_path"] == "clarification"


def test_clarification_rejects_non_string_parsed_payload():
    for invalid in ({"message": "請補充"}, 123, "   "):
        output = {"parsed": {"clarification": invalid}, "pipeline": {"status": "clarification", "clarifications": []}, "final": []}
        assert not v5._real_clarification(output)


def test_ai_estimate_requires_real_renderer_label_and_independent_bounds():
    case = next(row for row in POLICY["missing_33"] if row["case_id"] == "R3-04-2")
    good = _ai_output(case)
    assert v5.evaluate_result(case, good, POLICY, _card_evidence(good))["v5_verdict"] == "PASS"
    bad = v5.evaluate_result(case, good, POLICY, _card_evidence(good, ("估算結果",)))
    assert bad["v5_verdict"] == "FAIL"
    assert bad["reason"] == "ai_estimate_not_visibly_labeled"
    out_of_range = _ai_output(case, {"calories_kcal": 20, "protein_g": 2, "fat_g": 0.1, "carbohydrate_g": 0})
    assert v5.evaluate_result(case, out_of_range, POLICY, _card_evidence(out_of_range))["v5_verdict"] == "FAIL"


def test_card_evidence_must_bind_case_user_text_and_pipeline_status():
    case = next(row for row in POLICY["missing_33"] if row["case_id"] == "R3-04-2")
    output = _ai_output(case)
    for mutation in ({"user_text": "另一筆輸入"}, {"pipeline_status": "clarification"}, {"flex": "AI估算"}):
        evidence = _card_evidence(output)
        evidence.update(mutation)
        result = v5.evaluate_result(case, output, POLICY, evidence)
        assert result["v5_verdict"] == "FAIL"
        assert result["reason"] == "real_renderer_card_evidence_invalid"


def test_required_assumption_must_be_visible_on_card_not_only_hidden_in_parsed():
    case = next(row for row in POLICY["missing_33"] if row["case_id"] == "R3-04-1")
    output = _ai_output(case)
    output["parsed"]["items"][0]["portion_assumption"] = "假設去皮"
    result = v5.evaluate_result(case, output, POLICY, _card_evidence(output))
    assert result["v5_verdict"] == "FAIL"
    assert result["reason"] == "required_scope_assumption_not_visible"
    assert v5.evaluate_result(case, output, POLICY, _card_evidence(output, ("AI估算", "以生去皮雞胸估算")))["v5_verdict"] == "PASS"


def test_report_accepts_results_list_card_evidence_and_hash_binding():
    case = next(row for row in POLICY["missing_33"] if row["case_id"] == "R3-04-2")
    output = _ai_output(case)
    evidence = _card_evidence(output)
    evidence.pop("user_text")
    evidence.update({"case_id": case["case_id"], "user_text_sha256": v5.text_sha256(output["user_text"])})
    report = v5.evaluate_report({"results": [output]}, POLICY, {"results": [evidence]})
    row = next(row for row in report["results"] if row["case_id"] == case["case_id"])
    assert row["v5_verdict"] == "PASS"
    assert "release_decision" not in report["summary"]
    assert report["summary"]["phone_core_gate"] == "NOT_EVALUATED_BY_33_CASE_SIDECAR"


def test_zero_tolerance_hard_failures_override_other_passes():
    case = next(row for row in POLICY["missing_33"] if row["case_id"] == "R3-04-2")
    output = _ai_output(case)
    output["source"][0]["food_state"] = "熟、帶皮"
    output["basis"][0]["basis_unit"] = "ml"
    output["source"][0].update({"type": "official", "publisher": "TFDA", "food_code": "I04024", "basis_unit": "ml"})
    result = v5.evaluate_result(case, output, POLICY, _card_evidence(output))
    assert result["v5_verdict"] == "FAIL"
    assert {"state_or_edible_part_mismatch", "g_ml_basis_mismatch", "false_official"} <= set(result["critical_failures"])


def test_benchmark_state_difference_is_unverified_not_critical_source_mismatch():
    case = next(row for row in POLICY["missing_33"] if row["case_id"] == "R3-04-2")
    output = _ai_output(case)
    output["parsed"]["items"][0].update({"food_name": "熟雞胸肉", "food_state": "熟、去皮"})
    result = v5.evaluate_result(case, output, POLICY, _card_evidence(output))
    assert result["v5_verdict"] == "UNVERIFIED"
    assert result["reason"] == "benchmark_scope_not_comparable"
    assert "state_or_edible_part_mismatch" not in result["critical_failures"]


def test_state_comparison_is_bidirectional_and_peanut_is_not_raw_marker():
    assert v5._state_mismatch("生、去皮", "熟、去皮")
    assert v5._state_mismatch("熟、去皮", "生、去皮")
    assert v5._state_mismatch("生、去皮", "生、帶皮")
    assert v5._state_mismatch("生、帶皮", "生、去皮")
    assert not v5._state_mismatch("生花生仁、帶膜", "生花生仁、帶膜")


def test_unverified_benchmark_can_never_pass_ai_numbers():
    case = next(row for row in POLICY["missing_33"] if row["case_id"] == "R3-08-3")
    output = _ai_output(case, {"calories_kcal": 1, "protein_g": 0, "fat_g": 0, "carbohydrate_g": 0}, "無糖紅茶500cc，記點心")
    output["parsed"]["items"][0].update({"amount": 500, "unit": "ml", "food_name": "無糖紅茶", "food_state": ""})
    output["basis"][0]["basis_unit"] = "ml"
    output["source"][0]["basis_unit"] = "ml"
    output["final"] = [{"amount": 500, "unit": "ml", "nutrition": {k: value * 5 for k, value in output["basis"][0]["nutrition"].items()}}]
    result = v5.evaluate_result(case, output, POLICY, _card_evidence(output))
    assert result["v5_verdict"] == "UNVERIFIED"
    assert result["reason"] == "independent_comparable_range_unavailable"


def test_soymilk_reference_is_not_a_required_claim_about_unknown_brand_drink():
    benchmark = POLICY["benchmarks"]["packaged_unsweet_soymilk_ime_100ml"]
    assert benchmark["basis_unit"] == "ml"
    assert benchmark["label_serving"] == {"amount": 250, "unit": "ml"}
    assert benchmark["label_nutrition"] == {"calories_kcal": 84.9, "protein_g": 8.5, "fat_g": 4.3, "carbohydrate_g": 3.0}
    assert benchmark["prohibits_g_to_ml_conversion"] is True
    special = POLICY["phone_core_special_cases"]["generic_unsweet_soymilk_500ml"]
    assert special["verification_status"] == "UNVERIFIED"
    assert "義美" not in special["acceptance"]
