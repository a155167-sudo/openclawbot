#!/usr/bin/env python3
"""Round-4 v5 sidecar acceptance evaluator.

This module never imports product code and never mutates the frozen v3/v4
case bank, rubric, runner, report, DB, or LINE.  It reads a LIVE-REPORT and
writes a separate v5 interpretation for the authorized 33 missing rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "user_acceptance_v5.json"
NUTRIENTS = ("calories_kcal", "protein_g", "fat_g", "carbohydrate_g")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_legacy_freeze(policy: dict[str, Any], directory: Path = HERE) -> None:
    errors = []
    for name, expected in policy["legacy_freeze"]["files"].items():
        path = directory / name
        actual = sha256(path)
        if actual != expected:
            errors.append(f"{name}: expected {expected}, actual {actual}")
    if errors:
        raise ValueError("legacy freeze changed; v5 must remain a sidecar:\n" + "\n".join(errors))


def _first(value: Any) -> dict[str, Any]:
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _visible_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_visible_text(child) for child in value.values())
    if isinstance(value, list):
        return " ".join(_visible_text(child) for child in value)
    return value if isinstance(value, str) else ""


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _flex_visible_text(value: Any) -> str:
    """Read only renderer-visible Flex `text` properties, never metadata strings."""
    if isinstance(value, list):
        return " ".join(_flex_visible_text(child) for child in value)
    if not isinstance(value, dict):
        return ""
    pieces = [value["text"]] if isinstance(value.get("text"), str) else []
    pieces.extend(_flex_visible_text(child) for key, child in value.items() if key != "text")
    return " ".join(pieces)


def _card_evidence_status(result: dict[str, Any], evidence: Any) -> tuple[bool, str]:
    if not isinstance(evidence, dict) or not isinstance(evidence.get("flex"), dict):
        return False, ""
    evidence_dict: dict[str, Any] = evidence
    flex: dict[str, Any] = evidence_dict["flex"]
    user_text = result.get("user_text")
    if not isinstance(user_text, str):
        return False, ""
    exact = evidence_dict.get("user_text") == user_text
    hashed = evidence_dict.get("user_text_sha256") == text_sha256(user_text)
    pipeline_value = result.get("pipeline")
    pipeline: dict[str, Any] = pipeline_value if isinstance(pipeline_value, dict) else {}
    case_bound = evidence_dict.get("case_id") in (None, result.get("case_id"))
    if not case_bound or not (exact or hashed) or evidence_dict.get("pipeline_status") != pipeline.get("status"):
        return False, ""
    return True, _flex_visible_text(flex)


def _real_clarification(result: dict[str, Any]) -> bool:
    parsed = result.get("parsed") if isinstance(result.get("parsed"), dict) else {}
    pipeline = result.get("pipeline") if isinstance(result.get("pipeline"), dict) else {}
    messages = pipeline.get("clarifications")
    clarification = parsed.get("clarification")
    has_message = (isinstance(clarification, str) and bool(clarification.strip())) or (
        isinstance(messages, list)
        and any(isinstance(message, str) and message.strip() for message in messages)
    )
    return pipeline.get("status") == "clarification" and has_message and not result.get("final")


def _state_features(value: str) -> set[str]:
    text = value.lower().replace("花生", "花_生")  # embedded 生 is not a raw-state marker
    features: set[str] = set()
    raw = any(token in text for token in ("生", "未熟", "未煮", "還沒煮", "尚未煮"))
    cooked = any(token in text for token in ("熟", "蒸", "煮", "烤", "煎", "炸", "燉", "炒"))
    if raw:
        features.add("raw")
    if cooked and not any(token in text for token in ("未熟", "未煮", "還沒煮", "尚未煮")):
        features.add("cooked")
    if any(token in text for token in ("去皮", "去膜")):
        features.add("skinless")
    if any(token in text for token in ("帶皮", "含皮", "帶膜", "含膜")):
        features.add("skin_on")
    if any(token in text for token in ("乾重", "未煮乾", "乾麵", "乾燥")):
        features.add("dry")
    if any(token in text for token in ("熟重", "泡發")):
        features.add("hydrated")
    return features


def _state_mismatch(expected: str, actual: str) -> bool:
    left, right = _state_features(expected), _state_features(actual)
    opposites = (("raw", "cooked"), ("skinless", "skin_on"), ("dry", "hydrated"))
    return any((a in left and b in right) or (b in left and a in right) for a, b in opposites)


def _source_state_text(source: dict[str, Any], basis: dict[str, Any]) -> str:
    keys = ("food_state", "state", "scope", "food_name", "display_name", "matched_name", "edible_part")
    return " ".join(str(container[key]) for container in (source, basis) for key in keys
                    if isinstance(container.get(key), str))


def _nutrition_checks(nutrition: Any, benchmark: dict[str, Any]) -> dict[str, bool]:
    if not isinstance(nutrition, dict) or benchmark.get("ranges") is None:
        return {nutrient: False for nutrient in NUTRIENTS}
    return {
        nutrient: (
            _finite(nutrition.get(nutrient))
            and benchmark["ranges"][nutrient]["min"] <= float(nutrition[nutrient]) <= benchmark["ranges"][nutrient]["max"]
        )
        for nutrient in NUTRIENTS
    }


def _scaling_ok(basis: dict[str, Any], final: dict[str, Any], item: dict[str, Any]) -> bool:
    amount = item.get("amount")
    basis_nutrition = basis.get("nutrition")
    final_nutrition = final.get("nutrition")
    if not (_finite(amount) and isinstance(basis_nutrition, dict) and isinstance(final_nutrition, dict)):
        return False
    if basis.get("basis_amount") != 100 or final.get("amount") != amount or final.get("unit") != item.get("unit"):
        return False
    for nutrient in NUTRIENTS:
        source = basis_nutrition.get(nutrient)
        actual = final_nutrition.get(nutrient)
        if not (_finite(source) and _finite(actual)):
            return False
        expected = float(source) * float(amount) / 100
        if abs(float(actual) - expected) > max(0.02, abs(expected) * 0.001):
            return False
    return True


def evaluate_result(case_policy: dict[str, Any], result: dict[str, Any], policy: dict[str, Any],
                    card_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply v5 without consulting the run's old PASS/FAIL or AI-declared ranges."""
    benchmark = policy["benchmarks"][case_policy["benchmark"]]
    parsed = result.get("parsed") if isinstance(result.get("parsed"), dict) else {}
    item = _first(parsed.get("items"))
    basis = _first(result.get("basis"))
    final = _first(result.get("final"))
    source = _first(result.get("source"))
    critical: list[str] = []

    # Explicit safety flags from the independent capture remain fail-closed.
    flags = result.get("safety_flags")
    if isinstance(flags, list):
        critical.extend(flag for flag in flags if flag in policy["hard_fail_zero_tolerance"])

    requested_state = " ".join(str(value) for value in (
        result.get("user_text"), item.get("food_name"), item.get("food_state")) if isinstance(value, str))
    actual_source_state = _source_state_text(source, basis)
    source_type = str(source.get("type") or "").lower()
    # Here benchmark scope describes the actual selected official row, not merely
    # the independent AI comparator, because the captured food_code binds it.
    if (source_type == "official" and benchmark.get("food_code")
            and source.get("food_code") == benchmark.get("food_code")):
        actual_source_state += " " + str(benchmark.get("scope") or "")
    if actual_source_state and _state_mismatch(requested_state, actual_source_state):
        critical.append("state_or_edible_part_mismatch")
    requested_unit = item.get("unit")
    actual_units = (basis.get("basis_unit"), source.get("basis_unit"))
    if requested_unit in {"g", "ml"} and any(unit in {"g", "ml"} and unit != requested_unit for unit in actual_units):
        critical.append("g_ml_basis_mismatch")
    if source_type == "official" and basis.get("status") != "official_match":
        critical.append("false_official")
    if source_type == "official" and source.get("publisher") == "TFDA" and benchmark.get("food_code") and source.get("food_code") != benchmark.get("food_code"):
        critical.append("false_official")
    if not _finite(item.get("amount")) and result.get("final"):
        critical.append("uncertain_quantity_forced_to_final")
    critical = list(dict.fromkeys(critical))

    base = {
        "case_id": case_policy["case_id"],
        "category": case_policy["category"],
        "classification_basis": case_policy["classification_basis"],
        "legacy_report_status": result.get("status"),
        "legacy_evaluation_preserved": result.get("evaluation"),
        "critical_failures": critical,
        "evidence": {key: result.get(key) for key in ("raw", "parsed", "pipeline", "basis", "final", "source")},
    }
    if critical:
        return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "zero_tolerance_hard_failure"}

    if _real_clarification(result):
        if case_policy["category"] == 2:
            return {**base, "v5_verdict": "PASS", "accepted_path": "clarification", "reason": "information_gap_safely_clarified"}
        return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "category_requires_estimate_or_safe_db_match"}

    if source_type == "official" and basis.get("status") == "official_match":
        checks = _nutrition_checks(basis.get("nutrition"), benchmark)
        passed = benchmark["verification_status"] == "VERIFIED" and all(checks.values()) and _scaling_ok(basis, final, item)
        return {**base, "v5_verdict": "PASS" if passed else "FAIL", "accepted_path": "safe_db_match" if passed else None,
                "reason": "compatible_official_source" if passed else "official_source_or_value_not_compatible", "range_checks": checks}

    if source_type != "ai":
        return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "estimate_source_not_explicitly_ai"}

    evidence_valid, disclosure_text = _card_evidence_status(result, card_evidence)
    if not evidence_valid:
        return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "real_renderer_card_evidence_invalid"}
    if "AI估算" not in disclosure_text:
        return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "ai_estimate_not_visibly_labeled"}

    if basis.get("basis_amount") != 100 or basis.get("basis_unit") != requested_unit or source.get("basis_unit") != requested_unit:
        return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "ai_basis_contract_failed"}
    if not _scaling_ok(basis, final, item):
        return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "final_not_deterministically_scaled"}

    if case_policy["category"] == 2 and case_policy.get("required_assumption"):
        # Required scope must be visible in the real card; the generic AI label
        # and a hidden parsed field are not themselves disclosure.
        assumption_disclosure = disclosure_text.replace("AI估算", "").strip()
        if not assumption_disclosure or all(token not in assumption_disclosure for token in ("假設", "估算", "以")):
            return {**base, "v5_verdict": "FAIL", "accepted_path": None, "reason": "required_scope_assumption_not_visible"}

    if _state_mismatch(requested_state, str(benchmark.get("scope") or "")):
        return {**base, "v5_verdict": "UNVERIFIED", "accepted_path": None,
                "reason": "benchmark_scope_not_comparable", "range_checks": None}

    if benchmark["verification_status"] != "VERIFIED" or benchmark.get("ranges") is None:
        return {**base, "v5_verdict": "UNVERIFIED", "accepted_path": None,
                "reason": "independent_comparable_range_unavailable", "range_checks": None}

    checks = _nutrition_checks(basis.get("nutrition"), benchmark)
    passed = all(checks.values())
    return {**base, "v5_verdict": "PASS" if passed else "FAIL", "accepted_path": "ai_estimate" if passed else None,
            "reason": "independent_range_pass" if passed else "outside_independent_range", "range_checks": checks}


def _card_evidence_by_id(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("results"), list):
        return {row["case_id"]: row for row in payload["results"]
                if isinstance(row, dict) and isinstance(row.get("case_id"), str)}
    return {case_id: row for case_id, row in payload.items()
            if isinstance(case_id, str) and isinstance(row, dict)}


def evaluate_report(report: dict[str, Any], policy: dict[str, Any], card_evidence: Any = None) -> dict[str, Any]:
    results = report.get("results") if isinstance(report.get("results"), list) else []
    by_id = {row.get("case_id"): row for row in results if isinstance(row, dict)}
    cards_by_id = _card_evidence_by_id(card_evidence)
    rows = []
    for case_policy in policy["missing_33"]:
        actual = by_id.get(case_policy["case_id"])
        if actual is None:
            rows.append({"case_id": case_policy["case_id"], "category": case_policy["category"],
                         "v5_verdict": "BLOCKED", "reason": "case_missing_from_live_report",
                         "critical_failures": [], "evidence": None})
        else:
            rows.append(evaluate_result(case_policy, actual, policy, cards_by_id.get(case_policy["case_id"])))
    counts = {verdict: sum(row["v5_verdict"] == verdict for row in rows)
              for verdict in ("PASS", "FAIL", "UNVERIFIED", "BLOCKED")}
    critical = [f"{row['case_id']}:{failure}" for row in rows for failure in row.get("critical_failures", [])]
    return {
        "schema_version": "round4-mobile-user-acceptance-v5-side-report",
        "truth_label": "V5_SIDECAR—LEGACY GOLD AND REPORT UNCHANGED",
        "input_report_sha256": None,
        "policy_sha256": sha256(POLICY_PATH),
        "summary": {"eligible": 33, **counts, "critical_failures": critical,
                    "missing_33_acceptance_gate": "PASS" if counts["FAIL"] == counts["UNVERIFIED"] == counts["BLOCKED"] == 0 and not critical else "FAIL",
                    "hard_safety_gate": "PASS" if not critical else "FAIL",
                    "phone_core_gate": "NOT_EVALUATED_BY_33_CASE_SIDECAR"},
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("live_report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--card-evidence", type=Path,
                        help="independent real_renderer_cards.json; case_id map or {'results': [...]} records")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite side report: {args.output}")
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    verify_legacy_freeze(policy)
    report = json.loads(args.live_report.read_text(encoding="utf-8"))
    cards = json.loads(args.card_evidence.read_text(encoding="utf-8")) if args.card_evidence else None
    side = evaluate_report(report, policy, cards)
    side["input_report_sha256"] = sha256(args.live_report)
    side["card_evidence_sha256"] = sha256(args.card_evidence) if args.card_evidence else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(side, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(side["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
