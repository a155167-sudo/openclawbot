"""Authorized semantic meal parsing and official-first nutrition resolution.

This module is deliberately persistence-free.  The caller owns quota admission,
provider-start uncertainty, draft storage, and the existing confirmation boundary.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping


NUTRIENT_FIELDS = ("calories_kcal", "protein_g", "fat_g", "carbohydrate_g")
_ALLOWED_UNITS = {"g", "ml", "serving", "package", "natural"}
_MEAL_SLOT_EVIDENCE = (
    ("早餐", ("早餐", "早飯")),
    ("午餐", ("午餐", "中餐")),
    ("晚餐", ("晚餐",)),
    ("點心", ("點心", "宵夜", "下午")),
)
_MEASURE_RE = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>公斤|千克|kg|公克|克|g|毫升|ml|cc)",
    re.IGNORECASE,
)
_NATURAL_RE = re.compile(
    r"(?P<count>半|\d+(?:\.\d+)?|[一二兩三四五六七八九十])"
    r"(?P<classifier>份|個|杯|碗|顆|根|包|盒|瓶|片|塊|球|匙|盤)"
)
_CHINESE_COUNTS = {
    "半": Decimal("0.5"), "一": Decimal("1"), "二": Decimal("2"),
    "兩": Decimal("2"), "三": Decimal("3"), "四": Decimal("4"),
    "五": Decimal("5"), "六": Decimal("6"), "七": Decimal("7"),
    "八": Decimal("8"), "九": Decimal("9"), "十": Decimal("10"),
}


class PipelineDenied(PermissionError):
    """Raised when native admission refuses the batch before any AI call."""


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label}格式無效")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label}格式無效") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label}格式無效")
    return number


def _source_quantities(text: str) -> list[dict[str, Any]]:
    """Return quantities actually present in source text, in source order.

    This is deliberately evidence extraction, not intent routing: no food or eating
    verb dictionary participates in the decision.
    """
    evidence: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for match in _MEASURE_RE.finditer(text):
        number = Decimal(match.group("number"))
        raw_unit = match.group("unit").lower()
        if raw_unit in {"公斤", "千克", "kg"}:
            number *= 1000
            unit = "g"
        elif raw_unit in {"公克", "克", "g"}:
            unit = "g"
        else:
            unit = "ml"
        evidence.append({
            "start": match.start(), "amount": number, "unit": unit,
            "qty_origin": "explicit_measure", "qty_evidence": match.group(0),
        })
        occupied.append(match.span())
    for match in _NATURAL_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        raw_count = match.group("count")
        count = (_CHINESE_COUNTS[raw_count] if raw_count in _CHINESE_COUNTS
                 else Decimal(raw_count))
        evidence.append({
            "start": match.start(), "amount": count, "unit": "natural",
            "qty_origin": "natural_count", "qty_evidence": match.group(0),
        })
    evidence.sort(key=lambda item: item["start"])
    return evidence


def _evidenced_meal_slot(text: str) -> str:
    matches = [slot for slot, terms in _MEAL_SLOT_EVIDENCE if any(term in text for term in terms)]
    return matches[0] if len(set(matches)) == 1 else ""


def _normalized_parse(value: Mapping[str, Any], source_text: str = "") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("AI語意解析格式無效")
    intent = str(value.get("intent") or "").strip()
    if intent not in {"meal_log", "other", "clarification"}:
        raise ValueError("AI語意解析意圖無效")
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("AI語意解析品項格式無效")
    source_quantities = _source_quantities(source_text) if source_text else []
    items = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise ValueError("AI語意解析品項格式無效")
        # The parser is never a nutrition authority.
        if any(field in raw for field in NUTRIENT_FIELDS) or "nutrition" in raw:
            raise ValueError("AI語意解析不得輸出營養")
        # ``food_name`` is the semantic lookup key selected by the provider.
        # ``food_name_evidence`` is an audit quote and may include quantity or
        # classifier text, so it must never overwrite that key.  In particular,
        # do not reconstruct the key by stripping digits: digits may belong to a
        # brand, while state/variety qualifiers belong in the provider's name.
        name = " ".join(str(raw.get("food_name") or "").split())
        name_evidence = " ".join(str(raw.get("food_name_evidence") or "").split())
        food_state = " ".join(str(raw.get("food_state") or "").split())
        unit = str(raw.get("unit") or "").strip().lower()
        if unit == "cc":
            unit = "ml"
        if unit == "kg":
            unit = "g"
        if not name or unit not in _ALLOWED_UNITS:
            raise ValueError("AI語意解析品項格式無效")
        amount = _decimal(raw.get("amount"), "份量")
        assumption = " ".join(str(raw.get("portion_assumption") or "").split())
        if source_text:
            if index < len(source_quantities):
                quantity = source_quantities[index]
                amount = quantity["amount"]
                unit = quantity["unit"]
                qty_origin = quantity["qty_origin"]
                qty_evidence = quantity["qty_evidence"]
                if qty_origin == "explicit_measure":
                    # A verified measurement is evidence, not an assumption.
                    # Never preserve provider filler or a copy of the utterance
                    # in the semantic assumption field.
                    assumption = ""
            else:
                # No source span supports a measured provider quantity.  Preserve a
                # non-measured unit so downstream code asks rather than calculates.
                amount = Decimal("1")
                unit = "natural"
                qty_origin = "natural_count"
                qty_evidence = ""
        else:
            qty_origin = str(raw.get("qty_origin") or (
                "natural_count" if unit == "natural" else "explicit_measure"
            ))
            qty_evidence = " ".join(str(raw.get("qty_evidence") or "").split())
        item = {
            "food_name": name,
            "amount": float(amount),
            "unit": unit,
            "portion_assumption": assumption,
            "qty_origin": qty_origin,
            "qty_evidence": qty_evidence,
        }
        # Additive compatibility: legacy raw responses keep the established
        # normalized shape; new structured evidence is exposed when supplied.
        # Only expose evidence as a sourced audit field when it is grounded in
        # the user text.  The untouched provider JSON remains available for
        # auditing a hallucinated quote without treating it as source evidence.
        if name_evidence and (not source_text or name_evidence in source_text):
            item["food_name_evidence"] = name_evidence
        if food_state:
            item["food_state"] = food_state
        items.append(item)
    if intent == "meal_log" and not items:
        raise ValueError("AI語意解析未保留餐點品項")
    return {
        "intent": intent,
        "meal_slot": (_evidenced_meal_slot(source_text) if source_text else
                      " ".join(str(value.get("meal_slot") or "").split())),
        "items": items,
        "clarification": " ".join(str(value.get("clarification") or "").split()),
        "provider": str(value.get("provider") or ""),
        "model": str(value.get("model") or ""),
        "raw_trace_id": str(value.get("raw_trace_id") or ""),
        "_raw_provider_json": str(value.get("_raw_provider_json") or ""),
    }


def _scaled_value(value: Any, factor: Decimal) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (str(item) if key == "unit" else _scaled_value(item, factor))
            for key, item in value.items()
        }
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("營養格式無效")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("營養格式無效") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("營養格式無效")
    scaled = number * factor
    return int(scaled) if scaled == scaled.to_integral() else float(scaled)


def _scale_nutrition(nutrition: Mapping[str, Any], factor: Decimal) -> dict[str, Any]:
    if not isinstance(nutrition, Mapping):
        raise ValueError("營養格式無效")
    return {
        field: _scaled_value(nutrition.get(field), factor)
        for field in NUTRIENT_FIELDS
    }


def _audit_event(stage: str, batch_id: str, payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "stage": stage,
        "batch_id": batch_id,
        "provider": str(payload.get("provider") or ""),
        "model": str(payload.get("model") or ""),
        "raw_trace_id": str(payload.get("raw_trace_id") or ""),
    }


def _completion_payload(client: Any, *, model: str, messages: list[dict[str, str]],
                        response_format: Mapping[str, Any], max_tokens: int) -> tuple[dict[str, Any], dict[str, str]]:
    """Invoke an injected OpenAI-compatible client and decode strict JSON."""
    completion = getattr(getattr(getattr(client, "chat", None), "completions", None), "create", None)
    if not callable(completion):
        raise ValueError("AI client不支援chat completions")
    response = completion(model=model, messages=messages, response_format=response_format,
                          temperature=0, max_tokens=max_tokens, timeout=30)
    choices = getattr(response, "choices", None) or []
    if not choices or getattr(choices[0], "finish_reason", None) != "stop":
        raise ValueError("AI回應不完整")
    message = choices[0].message
    if getattr(message, "refusal", None):
        raise ValueError("AI拒絕回應")
    raw_content = str(getattr(message, "content", "") or "")
    try:
        payload = json.loads(raw_content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("AI回應不是有效JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("AI回應格式無效")
    return payload, {
        "provider": "openai", "model": str(getattr(response, "model", None) or model),
        "raw_trace_id": str(getattr(response, "id", None) or ""),
        "_raw_provider_json": raw_content,
    }


def parse_meal_semantics_openai(client: Any, text: str, batch_id: str, *,
                                model: str = "gpt-4o-mini") -> dict[str, Any]:
    """Semantic-only parser using an injected OpenAI-compatible client."""
    item_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["food_name", "food_name_evidence", "food_state", "amount", "unit",
                     "portion_assumption", "qty_origin", "qty_evidence"],
        "properties": {
            "food_name": {"type": "string"},
            "food_name_evidence": {"type": "string"},
            "food_state": {"type": "string"},
            "amount": {"type": "number"},
            "unit": {"type": "string", "enum": sorted(_ALLOWED_UNITS | {"cc", "kg"})},
            "portion_assumption": {"type": "string"},
            "qty_origin": {"type": "string", "enum": ["explicit_measure", "natural_count"]},
            "qty_evidence": {"type": "string"},
        },
    }
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["intent", "meal_slot", "items", "clarification"],
        "properties": {
            "intent": {"type": "string", "enum": ["meal_log", "other", "clarification"]},
            "meal_slot": {"type": "string", "enum": ["", "早餐", "午餐", "晚餐", "點心"]},
            "items": {"type": "array", "items": item_schema},
            "clarification": {"type": "string"},
        },
    }
    payload, trace = _completion_payload(
        client, model=model,
        messages=[
            {"role": "system", "content": (
                "判斷訊息是否描述已實際攝取的餐點，並依schema抽取所有品項。"
                "只做語意解析，不可估算或輸出任何營養。cc正規化為ml；"
                "餐別僅能依使用者原文明示輸出中文enum，未明示輸出空字串。"
                "每項以qty_origin與qty_evidence標出原文份量證據；明示g/ml時portion_assumption"
                "必須為空，該欄只能放真正採用的份量假設，不可複製品名、份量或整句。"
                "自然份量若無明確容量，unit用natural，amount保留自然計數，不可猜測g/ml容量，"
                "並在clarification明說容量未知且需補充。food_name_evidence逐字摘錄原文食品片語，"
                "food_name保留其中生熟、乾重、去皮、品種、調味等限定詞，不可自行刪除；"
                "food_state另列這些狀態證據，沒有則空字串。疑問、否定、尚未吃與一般聊天不得"
                "標成meal_log；但『尚未烹煮／還沒煮』若是在描述食材狀態或重量基準，不等於尚未吃，"
                "必須依整句語意判斷。不得宣稱已記錄。"
            )},
            {"role": "user", "content": str(text)},
        ],
        response_format={"type": "json_schema", "json_schema": {
            "name": "semantic_meal_parse", "strict": True, "schema": schema,
        }}, max_tokens=900,
    )
    payload.update(trace)
    return _normalized_parse(payload, str(text))


def estimate_per_100_openai(client: Any, requests: list[Mapping[str, Any]], batch_id: str,
                            *, model: str = "gpt-4o-mini") -> list[dict[str, Any]]:
    """Batch same-unit per-100 estimates using an injected OpenAI client."""
    def nutrient(unit: str) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "required": ["estimate", "min", "max", "unit"],
            "properties": {
                "estimate": {"type": "number"}, "min": {"type": "number"},
                "max": {"type": "number"}, "unit": {"type": "string", "enum": [unit]},
            },
        }
    fields = {
        "item_id": {"type": "string"}, "food_name": {"type": "string"},
        "basis_amount": {"type": "number"}, "basis_unit": {"type": "string"},
        "calories_kcal": nutrient("kcal"), "protein_g": nutrient("g"),
        "fat_g": nutrient("g"), "carbohydrate_g": nutrient("g"),
    }
    item_schema = {"type": "object", "additionalProperties": False,
                   "required": list(fields), "properties": fields}
    schema = {"type": "object", "additionalProperties": False, "required": ["items"],
              "properties": {"items": {"type": "array", "items": item_schema}}}
    safe_requests = [{"item_id": str(r.get("item_id") or ""),
                      "food_name": str(r.get("food_name") or ""), "amount": 100,
                      "unit": str(r.get("unit") or ""),
                      "food_state": str(r.get("food_state") or ""),
                      **({"food_name_evidence": str(r.get("food_name_evidence"))}
                         if r.get("food_name_evidence") else {})} for r in requests]
    payload, trace = _completion_payload(
        client, model=model,
        messages=[
            {"role": "system", "content": (
                "依schema估算各品項每100個請求單位的四項營養範圍。"
                "必須依user JSON的food_state與food_name_evidence（生熟、皮別、乾重/熟重、"
                "品種與加工狀態）估算，不得因food_name較短而改估其他狀態；"
                "food_state空字串時仍須保留evidence內的明示狀態。"
                "逐項原樣回傳item_id、food_name、basis_amount=100及相同basis_unit；"
                "不可把g與ml互換，不可計算整份。estimate必須介於min與max。"
            )},
            {"role": "user", "content": json.dumps(safe_requests, ensure_ascii=False)},
        ],
        response_format={"type": "json_schema", "json_schema": {
            "name": "meal_nutrition_per_100", "strict": True, "schema": schema,
        }}, max_tokens=max(900, 450 * len(safe_requests)),
    )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("AI每100單位估算格式無效")
    results = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ValueError("AI每100單位估算格式無效")
        results.append({
            "item_id": raw.get("item_id"), "food_name": raw.get("food_name"),
            "basis_amount": raw.get("basis_amount"), "basis_unit": raw.get("basis_unit"),
            "nutrition": {field: raw.get(field) for field in NUTRIENT_FIELDS}, **trace,
        })
    return results


def run_semantic_meal_pipeline(
    *,
    text: str,
    user_id: str,
    message_id: str,
    claim_batch: Callable[[str, str], Mapping[str, Any]],
    parse_semantics: Callable[[str, str], Mapping[str, Any]],
    find_reference_nutrition: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    estimate_per_100: Callable[[list[Mapping[str, Any]], str], list[Mapping[str, Any]]],
    audit: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one no-persistence batch after native admission claims its sole receipt."""
    uid, mid = str(user_id or "").strip(), str(message_id or "").strip()
    message = str(text or "").strip()
    if not uid or not mid or not message:
        raise ValueError("語意餐點請求缺少必要資料")

    claim = claim_batch(uid, mid)
    if not isinstance(claim, Mapping) or not claim.get("allowed"):
        raise PipelineDenied("AI額度或使用權限不足")
    batch_id = str(claim.get("batch_id") or "").strip()
    if not batch_id:
        raise ValueError("AI批次收據無效")

    parsed_raw = parse_semantics(message, batch_id)
    parsed = _normalized_parse(parsed_raw, message)
    audit_events = [_audit_event("semantic_parse", batch_id, parsed)]
    if audit:
        audit(audit_events[-1])
    if parsed["intent"] == "other":
        return {
            "status": "not_meal", "batch_id": batch_id, "meal_slot": "",
            "items": [], "clarifications": [], "writes_food_log": False,
            "audit": audit_events,
        }
    if parsed["intent"] == "clarification":
        return {
            "status": "clarification", "batch_id": batch_id,
            "meal_slot": parsed["meal_slot"], "items": [],
            "clarifications": [parsed["clarification"] or "請補充餐點與份量"],
            "writes_food_log": False, "audit": audit_events,
        }

    resolved: list[dict[str, Any] | None] = [None] * len(parsed["items"])
    misses: list[dict[str, Any]] = []
    miss_indexes: list[int] = []
    clarifications: list[str] = []
    unit_warnings: dict[int, str] = {}

    for index, request in enumerate(parsed["items"]):
        if request["unit"] == "natural":
            detail = request["portion_assumption"] or request["food_name"]
            clarifications.append(
                f"「{detail}」不是實測 g/ml，容量或重量未知；請補充實測份量。"
            )
            continue
        reference = find_reference_nutrition(dict(request))
        if reference and reference.get("status") == "unit_mismatch" and reference.get("food_exists"):
            units = "/".join(str(unit) for unit in reference.get("available_units") or []) or "其他單位"
            unit_warnings[index] = (
                f"「{request['food_name']}」的 {request['unit']} 與官方 {units} 不相容；"
                f"本草稿改用AI每100{request['unit']}估算，未把g與ml互換。"
            )
            reference = None
        if reference and reference.get("status", "matched") == "matched":
            basis_unit = str(reference.get("basis_unit") or "").lower()
            if basis_unit != request["unit"]:
                clarifications.append(
                    f"「{request['food_name']}」的 {request['unit']} 與官方 {basis_unit or '未知單位'} 不相容，請換單位。"
                )
                continue
            basis = _decimal(reference.get("basis_amount"), "官方基準")
            factor = Decimal(str(request["amount"])) / basis
            source = dict(reference.get("source") or {})
            source_label = str(source.get("source_label") or "官方資料")
            shown_request = dict(request)
            if source_label.startswith("固定參考"):
                shown_request["portion_assumption"] = "；".join(
                    str(source.get(key) or "") for key in ("state", "reference_label")
                    if source.get(key)
                )
            resolved[index] = {
                "request": shown_request,
                "nutrition": _scale_nutrition(reference.get("nutrition"), factor),
                "source_label": source_label,
                "source": source,
            }
            continue
        from nutrition_reference import is_fixed_core_request
        if is_fixed_core_request(request):
            clarifications.append(
                f"「{request['food_name']}」沒有符合此次單位／狀態的固定參考；不改用AI重估。"
                "請確認g/ml及生熟、皮別後重新描述；地瓜固定參考適用蒸熟去皮，品牌豆漿可提供營養標示。"
            )
            continue
        misses.append({**request, "item_id": f"item-{index}", "amount": 100})
        miss_indexes.append(index)

    if clarifications:
        return {
            "status": "clarification", "batch_id": batch_id,
            "meal_slot": parsed["meal_slot"],
            "items": [item for item in resolved if item is not None],
            "clarifications": clarifications, "writes_food_log": False,
            "audit": audit_events,
        }

    if misses:
        fallback = estimate_per_100(misses, batch_id)
        if not isinstance(fallback, list) or len(fallback) != len(misses):
            raise ValueError("AI每100單位估算未保留全部品項")
        for index, miss, estimate in zip(miss_indexes, misses, fallback):
            if not isinstance(estimate, Mapping):
                raise ValueError("AI每100單位估算格式無效")
            request = parsed["items"][index]
            if str(estimate.get("item_id") or "") != miss["item_id"]:
                raise ValueError("AI每100單位估算識別錯誤")
            if str(estimate.get("food_name") or "").strip() != request["food_name"]:
                raise ValueError("AI每100單位估算品項錯誤")
            if _decimal(estimate.get("basis_amount"), "AI基準") != Decimal("100"):
                raise ValueError("AI每100單位估算基準錯誤")
            if str(estimate.get("basis_unit") or "").strip().lower() != request["unit"]:
                raise ValueError("AI每100單位估算單位錯誤")
            from nutrition_plausibility import assess_per100
            assessment = assess_per100(request, estimate.get("nutrition"))
            if assessment["status"] == "requires_confirmation":
                clarifications.append(assessment["reason"])
                continue
            factor = Decimal(str(request["amount"])) / Decimal("100")
            resolved[index] = {
                "request": request,
                "nutrition": _scale_nutrition(estimate.get("nutrition"), factor),
                "source_label": "AI估算",
                "source": {
                    "provider": str(estimate.get("provider") or ""),
                    "model": str(estimate.get("model") or ""),
                    "raw_trace_id": str(estimate.get("raw_trace_id") or ""),
                    "basis_amount": 100,
                    "basis_unit": request["unit"],
                    "food_state": str(request.get("food_state") or ""),
                    "food_name_evidence": str(request.get("food_name_evidence") or ""),
                },
                "unit_warning": unit_warnings.get(index, ""),
            }
            event = _audit_event("nutrition_fallback", batch_id, estimate)
            audit_events.append(event)
            if audit:
                audit(event)

    if clarifications:
        return {
            "status": "clarification", "batch_id": batch_id,
            "meal_slot": parsed["meal_slot"], "items": [],
            "clarifications": clarifications, "writes_food_log": False,
            "audit": audit_events,
        }
    if any(item is None for item in resolved):
        raise ValueError("餐點解析結果不完整")
    return {
        "status": "draft", "batch_id": batch_id,
        "meal_slot": parsed["meal_slot"], "items": resolved,
        "clarifications": [], "writes_food_log": False,
        "audit": audit_events,
    }


def dispatch_semantic_meal_text(
    *, text: str, user_id: str, message_id: str,
    route_existing: Callable[[str], bool],
    reply: Callable[[Mapping[str, Any]], Any],
    claim_batch: Callable[[str, str], Mapping[str, Any]],
    parse_semantics: Callable[[str, str], Mapping[str, Any]],
    find_reference_nutrition: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    estimate_per_100: Callable[[list[Mapping[str, Any]], str], list[Mapping[str, Any]]],
    audit: Callable[[Mapping[str, Any]], None] | None = None,
) -> bool:
    """Executable handler seam: existing commands route before semantic AI.

    Production adapters should pass the existing command/state router as
    ``route_existing`` and render the returned draft/clarification in ``reply``.
    No persistence occurs here.
    """
    if route_existing(str(text)):
        return True
    result = run_semantic_meal_pipeline(
        text=text, user_id=user_id, message_id=message_id,
        claim_batch=claim_batch, parse_semantics=parse_semantics,
        find_reference_nutrition=find_reference_nutrition,
        estimate_per_100=estimate_per_100, audit=audit,
    )
    if result["status"] == "not_meal":
        return False
    reply(result)
    return True
