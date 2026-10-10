#!/usr/bin/env python3
"""Side-effect-free live adapter for the frozen Round 3 evaluator.

Only the public persistence-free semantic pipeline is called.  This module does
not import server, touch a DB/LINE/quota, or know evaluation expected values.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Mapping

from nutrition_reference import find_reference_nutrition
from semantic_meal_pipeline import (
    NUTRIENT_FIELDS,
    estimate_per_100_openai,
    parse_meal_semantics_openai,
    run_semantic_meal_pipeline,
)

MODEL_ENV = "ROUND3_OPENAI_MODEL"
DEFAULT_MODEL = "gpt-4o-mini"
MAX_TOKENS = 900
TIMEOUT_SECONDS = 20


def _openai_class():
    from openai import OpenAI
    return OpenAI


def _make_client():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for Round 3 live evaluation")
    return _openai_class()(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=0)


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if callable(getattr(usage, "model_dump", None)):
        value = usage.model_dump()
        return value if isinstance(value, dict) else {}
    return {name: getattr(usage, name) for name in
            ("prompt_tokens", "completion_tokens", "total_tokens")
            if getattr(usage, name, None) is not None}


class _TracingCompletions:
    def __init__(self, underlying: Any, traces: list[dict[str, Any]]):
        self._underlying = underlying
        self._traces = traces

    def create(self, **kwargs: Any) -> Any:
        # The public pipeline currently asks for timeout=30.  The evaluation
        # boundary is stricter and always wins.  One-case batches cap output.
        kwargs["timeout"] = TIMEOUT_SECONDS
        kwargs["max_tokens"] = min(int(kwargs.get("max_tokens", MAX_TOKENS)), MAX_TOKENS)
        response = self._underlying.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        usage = _usage_dict(getattr(response, "usage", None))
        self._traces.append({
            "request": {
                "model": kwargs.get("model"),
                "messages": kwargs.get("messages"),
                "response_format": kwargs.get("response_format"),
                "temperature": kwargs.get("temperature"),
                "max_tokens": kwargs.get("max_tokens"),
                "timeout": kwargs.get("timeout"),
            },
            "response_id": str(getattr(response, "id", "") or ""),
            "model": str(getattr(response, "model", "") or kwargs.get("model") or ""),
            "finish_reason": str(getattr(choices[0], "finish_reason", "") or "") if choices else "",
            "response_content": str(getattr(message, "content", "") or ""),
            "refusal": getattr(message, "refusal", None),
            "usage": usage,
        })
        return response


class _TracingClient:
    def __init__(self, client: Any, traces: list[dict[str, Any]]):
        completions = getattr(getattr(client, "chat", None), "completions", None)
        if not callable(getattr(completions, "create", None)):
            raise ValueError("AI client does not support chat completions")
        self.chat = SimpleNamespace(completions=_TracingCompletions(completions, traces))


def _estimate_value(value: Any) -> Any:
    return value.get("estimate") if isinstance(value, Mapping) else value


def _nutrition_estimates(value: Any) -> dict[str, Any]:
    nutrition = value if isinstance(value, Mapping) else {}
    return {field: _estimate_value(nutrition.get(field)) for field in NUTRIENT_FIELDS}


def _usage_total(traces: list[dict[str, Any]]) -> dict[str, Any]:
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    result: dict[str, Any] = {}
    for name in names:
        values = [trace.get("usage", {}).get(name) for trace in traces]
        if values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            result[name] = sum(values)
        else:
            result[name] = "unknown"
    return result


def run_case(user_text: str, context: dict[str, Any]) -> dict[str, Any]:
    """Run one live case; the only evaluated input is the caller's text string."""
    if context.get("mode") != "live":
        raise ValueError("live_adapter is permitted only in live mode")
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("user_text must be a non-empty string")

    model = os.environ.get(MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL
    traces: list[dict[str, Any]] = []
    client = _TracingClient(_make_client(), traces)
    parsed_holder: dict[str, Any] = {}
    reference_calls: list[dict[str, Any]] = []
    fallback_calls: list[dict[str, Any]] = []

    def parse(text: str, batch_id: str) -> Mapping[str, Any]:
        parsed = parse_meal_semantics_openai(client, text, batch_id, model=model)
        parsed_holder.update(parsed)
        return parsed

    def reference(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        result = find_reference_nutrition(dict(request))
        reference_calls.append({"request": dict(request), "result": result})
        return result

    def fallback(requests: list[Mapping[str, Any]], batch_id: str) -> list[Mapping[str, Any]]:
        result = estimate_per_100_openai(client, requests, batch_id, model=model)
        fallback_calls.extend({"request": dict(request), "result": estimate}
                              for request, estimate in zip(requests, result))
        return result

    result = run_semantic_meal_pipeline(
        text=user_text,
        user_id="round3-isolated",
        message_id="round3-case",
        claim_batch=lambda _uid, _mid: {"allowed": True, "batch_id": "round3-sandbox"},
        parse_semantics=parse,
        find_reference_nutrition=reference,
        estimate_per_100=fallback,
    )
    if len(traces) > 2:
        raise RuntimeError("Round 3 adapter exceeded two provider calls for one case")

    parsed_items = parsed_holder.get("items") if isinstance(parsed_holder.get("items"), list) else []
    pipeline_items = result.get("items") if isinstance(result.get("items"), list) else []
    basis: list[dict[str, Any]] = []
    final: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    fallback_by_name = {str(row["request"].get("food_name")): row["result"] for row in fallback_calls}
    reference_by_name = {str(row["request"].get("food_name")): row["result"] for row in reference_calls}
    pipeline_by_name = {str(row.get("request", {}).get("food_name")): row for row in pipeline_items}

    for request in parsed_items:
        name = str(request.get("food_name") or "")
        official = reference_by_name.get(name)
        estimate = fallback_by_name.get(name)
        resolved = pipeline_by_name.get(name)
        if resolved is None:
            basis.append({"status": "clarification"})
            continue
        if estimate is not None:
            mismatch = isinstance(official, Mapping) and official.get("status") == "unit_mismatch"
            basis.append({
                "status": "unit_mismatch" if mismatch else "fallback_eligible",
                "basis_amount": estimate.get("basis_amount"),
                "basis_unit": estimate.get("basis_unit"),
                "nutrition": _nutrition_estimates(estimate.get("nutrition")),
                "unit_warning": resolved.get("unit_warning", ""),
                "available_units": official.get("available_units", []) if mismatch else [],
            })
            sources.append({
                "type": "ai", "provider": estimate.get("provider"), "model": estimate.get("model"),
                "raw_trace_id": estimate.get("raw_trace_id"), "basis_unit": estimate.get("basis_unit"),
            })
        else:
            official_map = official if isinstance(official, Mapping) else {}
            source = dict(official_map.get("source") or {})
            source["type"] = "official"
            sources.append(source)
            basis.append({
                "status": "official_match", "basis_amount": official_map.get("basis_amount"),
                "basis_unit": official_map.get("basis_unit"),
                "nutrition": _nutrition_estimates(official_map.get("nutrition")), "unit_warning": "",
            })
        final.append({
            "amount": request.get("amount"), "unit": request.get("unit"),
            "nutrition": _nutrition_estimates(resolved.get("nutrition")),
        })

    if result.get("status") == "clarification" and not basis:
        basis = [{"status": "clarification"}]

    semantic_model = str(parsed_holder.get("model") or (traces[0].get("model") if traces else ""))
    nutrition_model = str(fallback_calls[0]["result"].get("model") or "") if fallback_calls else None
    return {
        "execution": {"kind": "live_ai", "provider_calls": len(traces), "estimated_cost_usd": "unknown"},
        "raw": traces,
        "parsed": parsed_holder,
        "pipeline": {
            "status": result.get("status"),
            "clarifications": result.get("clarifications")
            if isinstance(result.get("clarifications"), list) else [],
        },
        "basis": basis,
        "final": final,
        "source": sources,
        "model": {"semantic": semantic_model, "nutrition": nutrition_model},
        "usage": _usage_total(traces),
    }
