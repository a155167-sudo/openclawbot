"""Conservative policy guardrails for AI per-100 nutrition estimates.

These ranges are operational plausibility policy, not official composition data.
A failed or unclassified estimate is withheld for clarification; values are never
clipped into a range and g/ml are never converted.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

_FIELDS = ("calories_kcal", "protein_g", "fat_g", "carbohydrate_g")

# Broad policy bands intentionally catch category mistakes rather than assert a
# product's true composition.  Exact catalog matches bypass this AI-only guard.
_CATEGORY_POLICIES = (
    ("豆漿類", ("豆漿",), Decimal("25"), Decimal("60")),
    ("乳品飲料類", ("牛奶", "鮮奶", "乳飲", "優酪乳", "優格"), Decimal("25"), Decimal("150")),
    ("油脂類", ("食用油", "橄欖油", "沙拉油", "奶油", "豬油"), Decimal("500"), Decimal("950")),
    ("蛋類", ("雞蛋", "水煮蛋", "蛋白", "蛋黃", "煎蛋", "荷包蛋"), Decimal("40"), Decimal("400")),
    ("肉魚海鮮類", ("雞胸", "雞肉", "豬肉", "牛肉", "羊肉", "魚", "蝦", "蛤", "鮪魚", "鮭魚"), Decimal("30"), Decimal("500")),
    ("主食澱粉類", ("白飯", "米飯", "糙米", "麵", "吐司", "麵包", "燕麥", "地瓜", "馬鈴薯"), Decimal("20"), Decimal("500")),
    ("豆製品類", ("豆腐", "豆干", "毛豆", "黃豆"), Decimal("20"), Decimal("500")),
    ("蔬菜菇類", ("菜", "花椰", "菠菜", "菇", "番茄", "小黃瓜", "南瓜"), Decimal("3"), Decimal("250")),
    ("水果類", ("蘋果", "香蕉", "芭樂", "水果", "橘", "柳丁", "葡萄", "莓", "西瓜"), Decimal("5"), Decimal("250")),
    ("飲料類", ("果汁", "茶飲", "汽水", "可樂", "咖啡"), Decimal("0"), Decimal("200")),
)


def _number(value) -> Decimal | None:
    if isinstance(value, Mapping):
        estimate_number = _decimal_or_none(value.get("estimate"))
        low_number = _decimal_or_none(value.get("min"))
        high_number = _decimal_or_none(value.get("max"))
        if estimate_number is None or low_number is None or high_number is None:
            return None
        if not low_number <= estimate_number <= high_number:
            return None
        return estimate_number
    return _decimal_or_none(value)


def _decimal_or_none(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _category(name):
    for category, terms, minimum, maximum in _CATEGORY_POLICIES:
        if any(term in name for term in terms):
            return category, minimum, maximum
    return None


def _result(category, unit, *, valid, reason="", minimum=None, maximum=None):
    result = {
        "status": "pass" if valid else "requires_confirmation",
        "category": category,
        "basis_amount": 100,
        "basis_unit": unit or None,
        "reason": reason,
    }
    if minimum is not None:
        result.update(calories_min=float(minimum), calories_max=float(maximum))
    return result


def assess_per100(request, nutrition):
    """Assess an AI estimate without changing it.

    Unknown categories fail closed because there is no trustworthy policy band to
    apply.  All four P/F/C/energy values must be finite, non-negative, physically
    possible per 100 same-units, and within the selected category's energy band.
    """
    request = request if isinstance(request, Mapping) else {}
    name = " ".join(str(request.get("food_name") or "").split())
    unit = str(request.get("unit") or "").strip().lower()
    category_policy = _category(name)
    if category_policy is None:
        return _result(
            None, unit, valid=False,
            reason="食品類別無可信的合理範圍；請確認品項，或提供包裝營養標示。",
        )
    category, minimum, maximum = category_policy
    if not isinstance(nutrition, Mapping):
        return _result(
            category, unit, valid=False, minimum=minimum, maximum=maximum,
            reason=f"{category}每100{unit or '單位'}營養資料不足；請確認或提供包裝營養標示。",
        )
    values = {field: _number(nutrition.get(field)) for field in _FIELDS}
    if any(value is None for value in values.values()):
        return _result(
            category, unit, valid=False, minimum=minimum, maximum=maximum,
            reason=f"{category}每100{unit or '單位'}營養值缺漏、非有限值、負值或範圍無效；請確認。",
        )

    energy = values["calories_kcal"]
    protein = values["protein_g"]
    fat = values["fat_g"]
    carbs = values["carbohydrate_g"]
    assert energy is not None and protein is not None and fat is not None and carbs is not None
    # A 100 g/ml basis cannot contain over 100 g of P/F/C in aggregate.  The
    # energy ceiling also catches unit/basis mistakes without pretending that
    # Atwater factors exactly reproduce every label (fiber/polyols vary).
    impossible_macros = protein + fat + carbs > Decimal("100.5")
    macro_energy = protein * 4 + fat * 9 + carbs * 4
    impossible_energy_relation = macro_energy > energy * Decimal("1.40") + Decimal("20")
    if energy > Decimal("1000") or impossible_macros or impossible_energy_relation:
        return _result(
            category, unit, valid=False, minimum=minimum, maximum=maximum,
            reason=f"{category}每100{unit or '單位'}的能量或P/F/C組合不合理；請確認或提供包裝營養標示。",
        )
    if not minimum <= energy <= maximum:
        return _result(
            category, unit, valid=False, minimum=minimum, maximum=maximum,
            reason=f"{category}估算超出政策合理範圍；請確認食品類別、實際份量，或提供包裝營養標示。",
        )
    return _result(category, unit, valid=True, minimum=minimum, maximum=maximum)


def assess_draft_for_display(draft):
    """Re-check persisted AI drafts; never rewrite their original evidence."""
    estimate = draft.get('estimate') or {}
    provenance = estimate.get('provenance') or {}
    items = provenance.get('items')
    candidates = []
    if estimate.get('schema_version') == 'semantic-meal-estimate-v1' and isinstance(items, list):
        for item in items:
            if item.get('source_label') == 'AI估算':
                request = item.get('request') or {}
                candidates.append((request, request.get('amount'), item.get('nutrition') or {}))
    elif provenance.get('method') in {'text_meal_estimate', 'ai_text_estimate'}:
        request = dict(draft.get('request') or {})
        request['unit'] = estimate.get('basis_unit') or request.get('unit')
        candidates.append((request, estimate.get('basis_amount'), estimate))
    for request, basis, nutrition in candidates:
        amount = _decimal_or_none(basis)
        if amount is None or amount <= 0 or request.get('unit') not in {'g', 'ml'}:
            return {'status':'requires_confirmation','reason':'請確認食物重量或容量後重新描述餐點，或提供包裝營養標示。'}
        per100 = {}
        for field in _FIELDS:
            value = _number(nutrition.get(field))
            per100[field] = None if value is None else float(value * 100 / amount)
        assessment = assess_per100(request, per100)
        if assessment['status'] != 'pass':
            return assessment
    return {'status':'allowed'}
