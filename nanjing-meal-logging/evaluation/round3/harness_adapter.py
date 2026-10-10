"""Explicit fake adapter: report-shape harness only, never a provider evaluation."""
from __future__ import annotations


def run_case(case: dict, context: dict) -> dict:
    exp = case["expected"]
    item = exp["item"]
    routing = exp["routing"]["status"]
    gold = exp["nutrition_evaluation"]["gold_per_100"]
    parsed = {
        "intent": exp["intent"],
        "meal_slot": exp["meal_slot"],
        "items": [{
            "food_name": item["acceptable_food_names"][0],
            "food_state": item["food_state"],
            "amount": item["amount"],
            "unit": item["unit"],
            "portion_assumption": "fixture assumption" if item["portion_assumption"] in {"nonempty", "nonempty_or_clarification"} else "",
        }],
        "clarification": "請確認份量、容量或配方" if item["portion_assumption"] == "clarification" or routing == "clarification" else "",
    }
    basis = [{"status": routing}]
    final = []
    source = []
    if routing == "official_match" and gold:
        nutrition = {k: v["min"] for k, v in gold["ranges"].items()}
        basis[0].update({"basis_amount": 100, "basis_unit": "g", "nutrition": nutrition})
        amount = item["amount"]
        final = [{"amount": amount, "unit": item["unit"], "nutrition": {k: v * amount / 100 for k, v in nutrition.items()}}]
        source = [{"publisher": "TFDA", "food_code": gold["source"]["food_code"], "basis_unit": "g"}]
    elif routing in {"unit_mismatch", "fallback_eligible"}:
        # Synthetic values exercise only routing/schema/arithmetic.  They are
        # deliberately not asserted as nutrition quality.
        nutrition = {"calories_kcal": 20, "protein_g": 1, "fat_g": .2,
                     "carbohydrate_g": 4}
        basis[0].update({"basis_amount": 100, "basis_unit": item["unit"],
                         "nutrition": nutrition})
        if routing == "unit_mismatch":
            basis[0].update({"available_units": ["g"],
                             "unit_warning": "官方僅有 g；未換算，改用 AI 每100 ml估算"})
        amount = item["amount"]
        final = [{"amount": amount, "unit": item["unit"],
                  "nutrition": {k: v * amount / 100 for k, v in nutrition.items()}}]
        source = [{"type": "ai", "provider": "fake-harness", "model": "fake-harness",
                   "basis_unit": item["unit"]}]
    return {
        "execution": {"kind": "fake_harness", "provider_calls": 0, "estimated_cost_usd": 0},
        "raw": {"notice": "SYNTHETIC FIXTURE; NOT A PROVIDER RESPONSE", "case_id": case["case_id"]},
        "parsed": parsed,
        "basis": basis,
        "final": final,
        "source": source,
        "model": {"semantic": "fake-harness", "nutrition": "fake-harness" if gold else None},
    }
