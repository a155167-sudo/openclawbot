"""Privacy-safe one-line diagnostics for persisted nutrition drafts.

The helper is deliberately best-effort: malformed/legacy drafts produce null fields
rather than raising into the persistence path.  It never emits request text, user
identifiers, capabilities, provider traces, or unverified food names.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_FIELDS = ("calories_kcal", "protein_g", "fat_g", "carbohydrate_g")


def _catalog_allowlist() -> tuple[dict[str, str], dict[str, str]]:
    try:
        payload = json.loads(
            Path(__file__).with_name("nutrition_reference_data.json").read_text(encoding="utf-8")
        )
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            return {}, {}
        by_code: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            code = str(item.get("food_code") or "").strip()
            if not name:
                continue
            if code:
                by_code[code] = name
            by_name[name] = name
        return by_code, by_name
    except Exception:
        return {}, {}


_CATALOG_BY_CODE, _CATALOG_BY_NAME = _catalog_allowlist()


def _safe_unit(value):
    value = str(value or '').strip().lower()
    return value if value in {'g','ml','kg','l','piece','serving','package','natural','克','公克','毫升','顆','份'} else None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _point(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("estimate")
    return _finite(value)


def _trusted_catalog_name(source: Any) -> str | None:
    if not isinstance(source, Mapping):
        return None
    code = str(source.get("food_code") or "").strip()
    if code and code in _CATALOG_BY_CODE:
        return _CATALOG_BY_CODE[code]
    for key in ("source_name", "name"):
        name = str(source.get(key) or "").strip()
        if name in _CATALOG_BY_NAME:
            return _CATALOG_BY_NAME[name]
    return None


def _route(*, source_label: str, source: Any, provenance: Any) -> tuple[str, str | None]:
    matched = _trusted_catalog_name(source)
    if matched is not None:
        return "reference", matched
    label = source_label.lower()
    provenance = provenance if isinstance(provenance, Mapping) else {}
    method = str(provenance.get("method") or "").lower()
    provider = str(provenance.get("provider") or "").lower()
    if "ai" in label or "ai" in method or provider in {"openai", "anthropic", "gemini"}:
        return "ai", None
    return "private", None


def _safe_per100(nutrition: Any, divisor: float | None) -> dict[str, float | None]:
    result: dict[str, float | None] = {field: None for field in _FIELDS}
    if not isinstance(nutrition, Mapping) or divisor is None or divisor <= 0:
        return result
    for field in _FIELDS:
        point = _point(nutrition.get(field))
        result[field] = None if point is None else point / divisor
    return result


def _semantic_items(draft: Mapping[str, Any], estimate: Mapping[str, Any],
                    provenance: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    raw_items = provenance.get("items")
    if estimate.get("schema_version") != "semantic-meal-estimate-v1" or not isinstance(raw_items, list):
        return None
    multiplier = _finite(draft.get("portion_multiplier"))
    multiplier = 1.0 if multiplier is None else multiplier
    diagnostics = []
    for raw in raw_items:
        raw = raw if isinstance(raw, Mapping) else {}
        request = raw.get("request") if isinstance(raw.get("request"), Mapping) else raw
        amount = _finite(request.get("amount")) if isinstance(request, Mapping) else None
        factor = None if amount is None else (amount / 100.0) * multiplier
        divisor = None if amount is None else amount / 100.0
        source = raw.get("source")
        route, matched = _route(
            source_label=str(raw.get("source_label") or ""), source=source,
            provenance=provenance,
        )
        diagnostics.append({
            "route": route,
            "matched_item": matched,
            "basis100unit": (_safe_unit(request.get("unit"))
                             if isinstance(request, Mapping) else None),
            "per100": _safe_per100(raw.get("nutrition"), divisor),
            "factor": factor,
        })
    return diagnostics


def _legacy_item(draft: Mapping[str, Any], estimate: Mapping[str, Any],
                 provenance: Mapping[str, Any]) -> dict[str, Any]:
    raw_request = draft.get("request")
    request: Mapping[str, Any] = raw_request if isinstance(raw_request, Mapping) else {}
    basis = _finite(estimate.get("basis_amount"))
    if basis is None:
        basis = _finite(request.get("amount"))
    divisor = None if basis is None else basis / 100.0
    multiplier = _finite(draft.get("portion_multiplier"))
    multiplier = 1.0 if multiplier is None else multiplier
    factor = None if divisor is None else divisor * multiplier
    source = provenance.get("source")
    if not isinstance(source, Mapping):
        source = provenance
    route, matched = _route(
        source_label=str(provenance.get("source_label") or ""),
        source=source, provenance=provenance,
    )
    unit = _safe_unit(estimate.get("basis_unit") or request.get("unit"))
    return {
        "route": route, "matched_item": matched, "basis100unit": unit,
        "per100": _safe_per100(estimate, divisor), "factor": factor,
    }


def _payload(draft: Any) -> dict[str, list[dict[str, Any]]]:
    safe_draft: Mapping[str, Any] = draft if isinstance(draft, Mapping) else {}
    raw_estimate = safe_draft.get("estimate")
    estimate: Mapping[str, Any] = raw_estimate if isinstance(raw_estimate, Mapping) else {}
    raw_provenance = estimate.get("provenance")
    provenance: Mapping[str, Any] = (
        raw_provenance if isinstance(raw_provenance, Mapping) else {}
    )
    items = _semantic_items(safe_draft, estimate, provenance)
    if items is None:
        items = [_legacy_item(safe_draft, estimate, provenance)]
    return {"items": items}


def emit_draft_diagnostic(draft: Any) -> dict[str, list[dict[str, Any]]]:
    """Print exactly one allowlisted diagnostic line and return its payload.

    This function intentionally catches all draft-shape errors.  Diagnostic failure
    must not alter whether the caller persists a draft or classify unknown data as AI.
    """
    try:
        payload = _payload(draft)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except Exception:
        payload = {"items": [{
            "route": "private", "matched_item": None, "basis100unit": None,
            "per100": {field: None for field in _FIELDS}, "factor": None,
        }]}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        print("NUTRITION_DRAFT " + encoded, flush=True)
    except (OSError, ValueError):
        pass  # A closed logging stream must not invalidate a committed draft.
    return payload
