"""Narrow adapter from the v52 dashboard projection to the balance-card contract."""
from __future__ import annotations

from decimal import Decimal


def _identifier(value) -> str:
    return str(value or "").strip()


def _number(value):
    return float(value) if isinstance(value, Decimal) else value


def adapt_dashboard_data(data: dict) -> dict:
    """Map canonical eaten records and today's subscription schedule without guessing.

    Durable subscription meal IDs win. Legacy matching is allowed only when the
    eaten record lacks an ID and is explicitly marked ``planned_meal``;
    the enclosing dashboard query already binds user, Taipei date, and meal slot.
    """
    raw_records = data.get("balance_records")
    if raw_records is None:
        names = [str(name).strip() for name in data.get("food_list") or [] if str(name).strip()]
        raw_records = [] if not names else [{
            "slot": "其他", "name": "、".join(names),
            "kcal": data.get("extra_cal"), "protein": data.get("extra_pro"),
            "source_type": "legacy_dashboard_projection",
            "ai_estimated": bool(data.get("ai_estimated_count")),
        }]

    records = []
    for raw in raw_records:
        source_type = _identifier(raw.get("source_type"))
        meal_id = _identifier(raw.get("subscription_meal_id"))
        records.append({
            "slot": str(raw.get("slot") or "其他"),
            "name": str(raw.get("name") or "未命名紀錄"),
            "kcal": _number(raw.get("kcal")),
            "protein": _number(raw.get("protein")),
            "is_sub": source_type == "planned_meal",
            "ai_estimated": bool(raw.get("ai_estimated")),
            "subscription_meal_id": meal_id,
            "source_type": source_type,
        })

    raw_sub_meals = data.get("balance_sub_meals")
    if raw_sub_meals is None:
        raw_sub_meals = []
        for slot, prefix in (("午餐", "lunch"), ("晚餐", "dinner")):
            meal = str(data.get(f"today_{prefix}") or "").strip()
            if meal in {"", "無", "尚未安排"}:
                continue
            raw_sub_meals.append({
                "slot": slot, "name": meal,
                "kcal": data.get(f"{prefix}_cal"),
                "protein": data.get(f"{prefix}_pro"),
                "eaten": bool(data.get(f"{prefix}_checked")),
                "subscription_meal_id": data.get(f"{prefix}_subscription_meal_id"),
            })

    used_records = set()
    sub_meals = []
    for raw in raw_sub_meals:
        meal_id = _identifier(raw.get("subscription_meal_id"))
        slot = str(raw.get("slot") or "其他")
        match = -1 if raw.get("eaten") else None
        if match is None and meal_id:
            match = next((index for index, record in enumerate(records)
                          if index not in used_records
                          and record["subscription_meal_id"] == meal_id), None)
        if match is None:
            match = next((index for index, record in enumerate(records)
                          if index not in used_records
                          and not record["subscription_meal_id"]
                          and record["source_type"] == "planned_meal"
                          and record["slot"] == slot), None)
        if match is not None and match >= 0:
            used_records.add(match)
        sub_meals.append({
            "slot": slot,
            "name": str(raw.get("name") or "未命名包月餐"),
            "kcal": _number(raw.get("kcal")),
            "protein": _number(raw.get("protein")),
            "eaten": match is not None,
            "subscription_meal_id": meal_id,
        })

    return {
        "user_name": str(data.get("name") or "你"),
        "date_label": str(data.get("today_label") or "今天"),
        "target_kcal": _number(data.get("tdee")),
        "target_protein": _number(data.get("protein_goal")),
        "records": records,
        "sub_meals": sub_meals,
    }
