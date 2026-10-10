#!/usr/bin/env python3
"""Frozen, isolated Round 3 evaluator. No product/server imports and no persistence."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any, cast

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CASES = HERE / "cases.json"
RUBRIC = HERE / "rubric.md"
FREEZE = HERE / "FREEZE.sha256"
SENSITIVE_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DATABASE", "DB_", "LINE_", "WEBHOOK", "COOKIE")
NUTRIENTS = ("calories_kcal", "protein_g", "fat_g", "carbohydrate_g")
EVALUATOR_PROJECTION = "v4"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_freeze() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in FREEZE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, name = line.split(None, 1)
        rows[name.strip()] = digest
    return rows


def verify_freeze() -> dict[str, str]:
    expected = read_freeze()
    required = {
        "cases.json": CASES,
        "rubric.md": RUBRIC,
        "runner.py": HERE / "runner.py",
        "live_adapter.py": HERE / "live_adapter.py",
    }
    errors = []
    for name, path in required.items():
        actual = sha256(path)
        if expected.get(name) != actual:
            errors.append(f"{name}: expected {expected.get(name)!r}, actual {actual}")
    if errors:
        raise SystemExit("FREEZE verification failed; create a new evaluation version instead of bypassing:\n" + "\n".join(errors))
    bank = json.loads(CASES.read_text(encoding="utf-8"))
    official = Path(bank["provenance"]["official_zip"])
    if not official.is_file():
        raise SystemExit(f"Official TFDA evidence missing: {official}")
    actual_zip = sha256(official)
    expected_zip = bank["provenance"]["official_zip_sha256"]
    if actual_zip != expected_zip:
        raise SystemExit(f"Official ZIP SHA mismatch: expected {expected_zip}, actual {actual_zip}")
    return {**expected, "official_zip": actual_zip}


def validate_case_bank(bank: dict[str, Any]) -> None:
    """Enforce the extensible bank contract without weakening per-item coverage."""
    counts = bank.get("counts")
    cases = bank.get("cases")
    if not isinstance(counts, dict) or not isinstance(cases, list):
        raise ValueError("case-bank cardinality metadata is missing")
    declared_items = counts.get("items")
    declared_cases = counts.get("cases")
    minimum = counts.get("minimum_phrasings_per_item")
    if not all(isinstance(value, int) and not isinstance(value, bool)
               for value in (declared_items, declared_cases, minimum)):
        raise ValueError("case-bank cardinality metadata must be integers")
    declared_items = cast(int, declared_items)
    declared_cases = cast(int, declared_cases)
    minimum = cast(int, minimum)
    per_item = Counter(case.get("item_key") for case in cases if isinstance(case, dict))
    case_ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if (declared_cases < 90 or declared_items < 30 or minimum < 3
            or len(cases) != declared_cases or len(per_item) != declared_items
            or len(case_ids) != len(cases) or len(set(case_ids)) != len(case_ids)
            or not per_item or min(per_item.values()) < minimum):
        raise ValueError("case-bank cardinality/minimum-per-item contract failed")


def canonical_text(value: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def close(actual: Any, expected: float, tolerance: float) -> bool:
    return finite_number(actual) and abs(float(actual) - expected) <= tolerance


def get_first(seq: Any) -> dict[str, Any]:
    return seq[0] if isinstance(seq, list) and seq and isinstance(seq[0], dict) else {}


def complete_nutrition(value: Any) -> bool:
    return isinstance(value, dict) and all(
        finite_number(value.get(nutrient)) and float(value[nutrient]) >= 0
        for nutrient in NUTRIENTS
    )


def arithmetic_scaling(basis: dict[str, Any], final: dict[str, Any], amount: Any,
                       unit: str) -> dict[str, Any]:
    basis_nutrition = basis.get("nutrition")
    final_nutrition = final.get("nutrition")
    checks = {nutrient: False for nutrient in NUTRIENTS}
    if (complete_nutrition(basis_nutrition) and complete_nutrition(final_nutrition)
            and finite_number(amount)):
        for nutrient in NUTRIENTS:
            expected = float(basis_nutrition[nutrient]) * float(amount) / 100
            checks[nutrient] = close(final_nutrition.get(nutrient), expected,
                                     max(.02, abs(expected) * .001))
    envelope_ok = (final.get("amount") == amount and final.get("unit") == unit)
    return {"status": "SCORED", "arithmetic_checks": checks,
            "final_envelope_pass": envelope_ok,
            "scaling_arithmetic_pass": envelope_ok and all(checks.values()),
            "final_gold_pass": None}


def portion_ok(kind: str, parsed: dict[str, Any], item: dict[str, Any]) -> bool:
    assumption = str(item.get("portion_assumption") or "").strip()
    clarification = str(parsed.get("clarification") or "").strip()
    if kind == "empty":
        return not assumption
    if kind == "nonempty":
        return bool(assumption)
    if kind == "clarification":
        return bool(clarification)
    if kind == "nonempty_or_clarification":
        return bool(assumption or clarification)
    return False


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def state_check(expected: str, actual: Any) -> tuple[str, str | None]:
    text = canonical_text(actual)
    exp = canonical_text(expected)
    if not text:
        return "REVIEW", "food_state missing"
    # Exact/containing expected text is never an obvious contradiction merely because
    # its explanatory clause mentions the opposite state (e.g.「不套用熟重」).
    if text == exp or exp in text:
        return "PASS", None
    if "生" in exp and "熟" in text and "未熟" not in text and "不套用熟重" not in text:
        return "FAIL", "expected raw state but output says cooked"
    if "熟" in exp and "生" in text and "生乳" not in text:
        return "FAIL", "expected cooked state but output says raw"
    if "無糖" in exp and any(word in text for word in ("全糖", "半糖", "加糖")):
        return "FAIL", "expected unsweetened state but output says sweetened"
    return "PASS", None


def redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if any(part in str(key).upper() for part in SENSITIVE_PARTS):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact(child, secrets)
        return out
    if isinstance(value, list):
        return [redact(x, secrets) for x in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value


def evaluate(case: dict[str, Any], output: dict[str, Any], latency_ms: float) -> dict[str, Any]:
    exp = case["expected"]
    parsed = output.get("parsed") if isinstance(output.get("parsed"), dict) else {}
    pipeline = cast(dict[str, Any], output.get("pipeline")) if isinstance(output.get("pipeline"), dict) else {}
    items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    actual_item = get_first(items)
    expected_item = exp["item"]
    state_status, state_note = state_check(expected_item["food_state"], actual_item.get("food_state"))
    names = {canonical_text(x) for x in expected_item["acceptable_food_names"]}
    amount_expected = expected_item["amount"]
    amount_actual = actual_item.get("amount")
    amount_pass = amount_actual is None if amount_expected is None else close(amount_actual, float(amount_expected), 1e-9)
    checks = {
        "intent": parsed.get("intent") == exp["intent"],
        "meal_slot": parsed.get("meal_slot") == exp["meal_slot"],
        "item_count": len(items) == exp["item_count"],
        "food_name": canonical_text(actual_item.get("food_name")) in names,
        "amount": amount_pass,
        "unit": actual_item.get("unit") == expected_item["unit"],
        "portion_assumption": portion_ok(expected_item["portion_assumption"], parsed, actual_item),
        "food_state_no_obvious_conflict": state_status != "FAIL",
    }
    basis = get_first(output.get("basis"))
    source = get_first(output.get("source"))
    final = get_first(output.get("final"))
    expected_route = exp["routing"]["status"]
    actual_route = basis.get("status")
    routing_checks: dict[str, bool] = {"status": actual_route == expected_route}
    critical: list[str] = []
    if expected_route == "official_match":
        gold = exp["nutrition_evaluation"]["gold_per_100"]
        routing_checks.update({
            "publisher": source.get("publisher") == "TFDA",
            "food_code": source.get("food_code") == gold["source"]["food_code"],
            "basis_amount": basis.get("basis_amount") == 100,
            "basis_unit": basis.get("basis_unit") == "g" and source.get("basis_unit") == "g",
        })
    elif expected_route in {"unit_mismatch", "fallback_eligible"}:
        requested_unit = expected_item["unit"]
        routing_checks.update({
            "basis_amount_100": basis.get("basis_amount") == 100,
            "basis_same_requested_unit": basis.get("basis_unit") == requested_unit,
            "all_four_basis_nutrients": complete_nutrition(basis.get("nutrition")),
            "actual_source_ai": canonical_text(source.get("type")) == "ai",
            "not_masquerading_as_official": canonical_text(source.get("publisher")) != "tfda" and not source.get("food_code"),
            "source_basis_same_unit": source.get("basis_unit") == requested_unit,
        })
        if expected_route == "unit_mismatch":
            routing_checks["g_available"] = "g" in exp["routing"]["available_units"]
            routing_checks["unit_warning_present"] = bool(str(basis.get("unit_warning") or "").strip())
            if basis.get("basis_unit") != requested_unit or canonical_text(source.get("type")) != "ai" or source.get("food_code"):
                critical.append("unit_mismatch_fallback_must_be_labeled_ai_in_requested_unit")
    elif expected_route == "clarification":
        pipeline_clarifications = pipeline.get("clarifications")
        pipeline_has_clarification = (
            pipeline.get("status") == "clarification"
            and isinstance(pipeline_clarifications, list)
            and any(nonempty_string(message) for message in pipeline_clarifications)
        )
        routing_checks["clarification_present"] = (
            nonempty_string(parsed.get("clarification")) or pipeline_has_clarification
        )
        routing_checks["no_final_nutrition"] = not final
    nutrition_status = exp["nutrition_evaluation"]["status"]
    nutrition_result: dict[str, Any] = {"status": nutrition_status}
    scaling_result: dict[str, Any] = {"status": "NOT_APPLICABLE"}
    if nutrition_status == "NOT_SCORED":
        nutrition_result["reason"] = exp["nutrition_evaluation"]["not_scored_reason"]
        nutrition_result["pass"] = None
        if expected_route in {"unit_mismatch", "fallback_eligible"}:
            scaling_result = arithmetic_scaling(basis, final, expected_item["amount"], expected_item["unit"])
    else:
        gold = exp["nutrition_evaluation"]["gold_per_100"]
        actual_nutrition = basis.get("nutrition") if isinstance(basis.get("nutrition"), dict) else {}
        nutrient_checks = {}
        for nutrient in NUTRIENTS:
            gold_value = float(gold["ranges"][nutrient]["min"])
            tolerance = max(2.0, abs(gold_value) * .02) if nutrient == "calories_kcal" else max(.2, abs(gold_value) * .02)
            nutrient_checks[nutrient] = {"actual": actual_nutrition.get(nutrient), "gold": gold_value,
                                         "tolerance": tolerance, "pass": close(actual_nutrition.get(nutrient), gold_value, tolerance)}
        source_basis_pass = all(routing_checks.values())
        nutrition_result.update({"source_basis_pass": source_basis_pass, "nutrients": nutrient_checks,
                                 "pass": source_basis_pass and all(x["pass"] for x in nutrient_checks.values())})
        requested = expected_item["amount"]
        if expected_item["unit"] == "g" and finite_number(requested):
            final_nutrition = final.get("nutrition") if isinstance(final.get("nutrition"), dict) else {}
            arithmetic_checks = {}
            final_gold_checks = {}
            for nutrient in NUTRIENTS:
                actual_basis = actual_nutrition.get(nutrient)
                actual_final = final_nutrition.get(nutrient)
                gold_basis = float(gold["ranges"][nutrient]["min"])
                expected_gold_final = gold_basis * float(requested) / 100
                tol_gold = max(.02, abs(expected_gold_final) * .001)
                arithmetic_expected = float(actual_basis) * float(requested) / 100 if finite_number(actual_basis) else math.nan
                tol_arithmetic = max(.02, abs(arithmetic_expected) * .001) if math.isfinite(arithmetic_expected) else .02
                arithmetic_checks[nutrient] = close(actual_final, arithmetic_expected, tol_arithmetic)
                final_gold_checks[nutrient] = close(actual_final, expected_gold_final, tol_gold)
            scaling_result = {"status": "SCORED", "arithmetic_checks": arithmetic_checks,
                              "final_gold_checks": final_gold_checks,
                              "scaling_arithmetic_pass": all(arithmetic_checks.values()),
                              "final_gold_pass": all(final_gold_checks.values())}
    execution = output.get("execution") if isinstance(output.get("execution"), dict) else {}
    return {
        "case_id": case["case_id"], "item_key": case["item_key"], "user_text": case["user_text"],
        "status": "COMPLETED", "latency_ms": round(latency_ms, 3), "error": None,
        "execution": execution, "raw": output.get("raw"), "parsed": parsed, "pipeline": pipeline,
        "basis": output.get("basis"), "final": output.get("final"), "source": output.get("source"),
        "model": output.get("model"), "usage": output.get("usage"),
        "evaluation": {"parsing": {"checks": checks, "pass": all(checks.values()),
                                      "food_state_status": state_status, "food_state_note": state_note},
                       "routing": {"expected": expected_route, "actual": actual_route,
                                   "checks": routing_checks, "pass": all(routing_checks.values())},
                       "nutrition": nutrition_result, "scaling": scaling_result,
                       "critical_failures": critical},
    }


def blocked(case: dict[str, Any], reason: str, error: str | None = None) -> dict[str, Any]:
    return {"case_id": case["case_id"], "item_key": case["item_key"], "user_text": case["user_text"],
            "status": "BLOCKED", "blocked_reason": reason, "latency_ms": None,
            "error": error, "raw": None, "parsed": None, "pipeline": None, "basis": None, "final": None,
            "source": None, "model": None, "usage": None,
            "expected_nutrition_status": case["expected"]["nutrition_evaluation"]["status"],
            "evaluation": None}


def summarize(results: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    completed = [r for r in results if r["status"] == "COMPLETED"]
    blocked_rows = [r for r in results if r["status"] == "BLOCKED"]
    scored_expected = [r for r in results if (r.get("expected_nutrition_status") == "SCORED" or
        (r.get("evaluation") and r["evaluation"]["nutrition"]["status"] == "SCORED"))]
    scored_completed = [r for r in completed if r["evaluation"]["nutrition"]["status"] == "SCORED"]
    not_scored = [r for r in completed if r["evaluation"]["nutrition"]["status"] == "NOT_SCORED"]
    fields = [value for r in completed for value in r["evaluation"]["parsing"]["checks"].values()]
    critical = [f"{r['case_id']}:{x}" for r in completed for x in r["evaluation"]["critical_failures"]]
    return {
        "truth_label": "HARNESS_FAKE—REPORT SHAPE ONLY; NO AI ACCURACY" if mode == "harness" else "LIVE_AI_RESULTS",
        "release_decision": "NOT_EVALUATED_BY_RUNNER",
        "cases": {"eligible": len(results), "completed": len(completed), "blocked": len(blocked_rows)},
        "parsing": {"whole_case_pass": sum(r["evaluation"]["parsing"]["pass"] for r in completed),
                    "completed_denominator": len(completed), "field_pass": sum(fields), "field_denominator": len(fields),
                    "food_state_review": [r["case_id"] for r in completed if r["evaluation"]["parsing"]["food_state_status"] == "REVIEW"]},
        "routing": {"pass": sum(r["evaluation"]["routing"]["pass"] for r in completed),
                    "completed_denominator": len(completed)},
        "nutrition": {"eligible_scored": len(scored_expected), "completed_scored": len(scored_completed),
                      "blocked_scored": len(scored_expected) - len(scored_completed),
                      "per100_pass": sum(r["evaluation"]["nutrition"].get("pass") is True for r in scored_completed),
                      "not_scored_completed": len(not_scored), "not_scored_is_pass": False},
        "scaling": {"eligible_completed": sum(r["evaluation"]["scaling"]["status"] == "SCORED" for r in completed),
                    "arithmetic_pass": sum(r["evaluation"]["scaling"].get("scaling_arithmetic_pass") is True for r in completed),
                    "final_gold_pass": sum(r["evaluation"]["scaling"].get("final_gold_pass") is True for r in completed)},
        "critical_failures": critical,
        "blocked_reasons": {reason: sum(r.get("blocked_reason") == reason for r in blocked_rows)
                            for reason in sorted({r.get("blocked_reason") for r in blocked_rows})},
    }


def safe_environment(pass_names: list[str], temp_home: str) -> tuple[dict[str, str], list[str]]:
    env = {"PATH": os.environ.get("PATH", ""), "LANG": os.environ.get("LANG", "C.UTF-8"),
           "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"), "HOME": temp_home,
           "PYTHONPATH": str(REPO), "EVAL_ISOLATED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    secrets = []
    for name in pass_names:
        if name not in os.environ:
            raise SystemExit(f"Requested --pass-env variable is absent: {name}")
        env[name] = os.environ[name]
        if any(part in name.upper() for part in SENSITIVE_PARTS):
            secrets.append(os.environ[name])
    return env, secrets


def run_worker(adapter: str, case: dict[str, Any], mode: str, timeout: float,
               pass_env: list[str]) -> tuple[dict[str, Any] | None, float, str | None, list[str]]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="r3-eval-") as td:
        root = Path(td)
        case_path = root / "case.json"
        result_path = root / "result.json"
        # A live adapter must never receive expected labels.  Keep even the
        # worker input file free of them; harness mode intentionally receives
        # the fixture so it can exercise report/evaluator shape.
        worker_input = case if mode == "harness" else {
            "user_text": case["user_text"], "case_id": case["case_id"]}
        case_path.write_text(json.dumps(worker_input, ensure_ascii=False), encoding="utf-8")
        env, secrets = safe_environment(pass_env, str(root / "home"))
        cmd = [sys.executable, str(Path(__file__).resolve()), "--_worker", "--adapter", adapter,
               "--case-file", str(case_path), "--result-file", str(result_path), "--mode", mode]
        try:
            proc = subprocess.run(cmd, cwd=td, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, (time.perf_counter() - started) * 1000, "timeout", secrets
        latency = (time.perf_counter() - started) * 1000
        if proc.returncode != 0 or not result_path.is_file():
            detail = proc.stderr or proc.stdout or f"worker exit {proc.returncode}"
            return None, latency, detail, secrets
        try:
            return json.loads(result_path.read_text(encoding="utf-8")), latency, None, secrets
        except Exception as exc:
            return None, latency, f"invalid worker JSON: {type(exc).__name__}: {exc}", secrets


def worker(args: argparse.Namespace) -> int:
    module_name, sep, function_name = args.adapter.partition(":")
    if not sep or not module_name or not function_name:
        raise ValueError("adapter must be module:function")
    worker_input = json.loads(Path(args.case_file).read_text(encoding="utf-8"))
    fn = getattr(importlib.import_module(module_name), function_name)
    adapter_input = worker_input if args.mode == "harness" else worker_input["user_text"]
    output = fn(adapter_input, {"mode": args.mode, "case_id": worker_input["case_id"], "isolated": True})
    if not isinstance(output, dict):
        raise TypeError("adapter must return a mapping/dict")
    Path(args.result_file).write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    return 0


def main(args: argparse.Namespace) -> int:
    freeze = verify_freeze()
    bank = json.loads(CASES.read_text(encoding="utf-8"))
    cases = bank["cases"]
    try:
        validate_case_bank(bank)
    except ValueError as exc:
        raise SystemExit(f"Frozen case-bank cardinality contract failed: {exc}") from exc
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise SystemExit(f"Refusing to overwrite report: {output_path}")
    checkpoint_path = output_path.with_name(output_path.name + ".checkpoint.jsonl")
    if checkpoint_path.exists():
        raise SystemExit(f"Refusing to overwrite checkpoint: {checkpoint_path}")
    if args.max_calls < 0 or args.requests_per_minute <= 0 or args.timeout_seconds <= 0:
        raise SystemExit("Invalid limits")
    if args.budget_cap_usd < 0 or args.estimated_cost_per_call_usd < 0:
        raise SystemExit("Invalid budget")
    results = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = checkpoint_path.open("x", encoding="utf-8")

    def append_result(row: dict[str, Any]) -> None:
        results.append(row)
        checkpoint.write(json.dumps(row, ensure_ascii=False) + "\n")
        checkpoint.flush()
        os.fsync(checkpoint.fileno())

    calls = 0
    estimated_reserved = 0.0
    last_start = 0.0
    discovered_cost = 0.0
    reported_cost_count = 0
    for case in cases:
        if calls >= args.max_calls:
            append_result(blocked(case, "max_calls")); continue
        if estimated_reserved + args.estimated_cost_per_call_usd > args.budget_cap_usd + 1e-12:
            append_result(blocked(case, "budget_cap")); continue
        interval = 60.0 / args.requests_per_minute
        wait = interval - (time.monotonic() - last_start)
        if wait > 0:
            time.sleep(wait)
        last_start = time.monotonic()
        calls += 1
        estimated_reserved += args.estimated_cost_per_call_usd
        raw_output, latency, error, secrets = run_worker(args.adapter, case, args.mode, args.timeout_seconds, args.pass_env)
        if error is not None:
            reason = "timeout" if error == "timeout" else "adapter_error"
            append_result(blocked(case, reason, redact(error, secrets))); continue
        execution = raw_output.get("execution") if isinstance(raw_output.get("execution"), dict) else {}
        expected_kind = "fake_harness" if args.mode == "harness" else "live_ai"
        if execution.get("kind") != expected_kind:
            append_result(blocked(case, "execution_kind_mismatch",
                                  f"mode {args.mode} requires execution.kind={expected_kind!r}")); continue
        cost = execution.get("estimated_cost_usd")
        if finite_number(cost) and cost >= 0:
            discovered_cost += float(cost)
            reported_cost_count += 1
        evaluated = evaluate(case, raw_output, latency)
        append_result(redact(evaluated, secrets))
    checkpoint.close()
    report = {
        "schema_version": "round3-eval-report-v1", "evaluator_projection": EVALUATOR_PROJECTION,
        "execution_mode": args.mode,
        "generated_at_epoch": time.time(), "adapter": args.adapter, "adapter_version": args.adapter_version,
        "freeze_sha256": freeze,
        "limits": {"max_adapter_invocations": args.max_calls, "requests_per_minute": args.requests_per_minute,
                   "timeout_seconds": args.timeout_seconds, "budget_cap_usd": args.budget_cap_usd,
                   "estimated_cost_per_call_usd": args.estimated_cost_per_call_usd},
        "usage": {"adapter_invocations": calls, "estimated_reserved_usd": estimated_reserved,
                  "adapter_reported_cost_count": reported_cost_count,
                  "adapter_reported_estimated_cost_usd": discovered_cost if reported_cost_count == calls else "unknown"},
        "summary": summarize(results, args.mode), "results": results,
    }
    with output_path.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps({"report": str(output_path), "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--adapter", required=True, help="side-effect-free module:function")
    p.add_argument("--mode", choices=("harness", "live"), required=True)
    p.add_argument("--output", default=str(HERE / "report.json"))
    p.add_argument("--adapter-version", default="unspecified")
    p.add_argument("--max-calls", type=int, default=93,
                   help="maximum adapter invocations (not provider calls inside an adapter)")
    p.add_argument("--requests-per-minute", type=float, default=30)
    p.add_argument("--timeout-seconds", type=float, default=30)
    p.add_argument("--budget-cap-usd", type=float, default=0)
    p.add_argument("--estimated-cost-per-call-usd", type=float, default=0)
    p.add_argument("--pass-env", action="append", default=[], metavar="NAME")
    p.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--case-file", help=argparse.SUPPRESS)
    p.add_argument("--result-file", help=argparse.SUPPRESS)
    return p


if __name__ == "__main__":
    parsed_args = parser().parse_args()
    raise SystemExit(worker(parsed_args) if parsed_args._worker else main(parsed_args))
