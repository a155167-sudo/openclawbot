"""一日樂食營養辨識、食品資料庫與配餐推薦的純資料層。"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence


NUTRIENT_KEYS = (
    "calories_kcal",
    "protein_g",
    "fat_g",
    "saturated_fat_g",
    "trans_fat_g",
    "cholesterol_mg",
    "carbohydrate_g",
    "sugar_g",
    "fiber_g",
    "sodium_mg",
)

EXCHANGE_KEYS = (
    "milk_exchange",
    "protein_low_exchange",
    "protein_medium_exchange",
    "protein_high_exchange",
    "starch_exchange",
    "vegetable_exchange",
    "fruit_exchange",
    "fat_exchange",
)


NUTRIENT_LIMITS = {
    "calories_kcal": 20000,
    "protein_g": 2000,
    "fat_g": 2000,
    "saturated_fat_g": 2000,
    "trans_fat_g": 500,
    "cholesterol_mg": 100000,
    "carbohydrate_g": 5000,
    "sugar_g": 5000,
    "fiber_g": 2000,
    "sodium_mg": 200000,
}

NUTRIENT_LABELS = {
    "calories_kcal": "熱量",
    "protein_g": "蛋白質",
    "fat_g": "脂肪",
    "saturated_fat_g": "飽和脂肪",
    "trans_fat_g": "反式脂肪",
    "cholesterol_mg": "膽固醇",
    "carbohydrate_g": "碳水",
    "sugar_g": "糖",
    "fiber_g": "膳食纖維",
    "sodium_mg": "鈉",
}


def _number(
    value: Any, field: str, *, allow_zero: bool = True, max_value: float | None = None
) -> float:
    if value in (None, ""):
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必須是數字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} 必須是有限數字")
    if result < 0 or (not allow_zero and result == 0):
        raise ValueError(f"{field} 不可為負數或零" if not allow_zero else f"{field} 不可為負數")
    if max_value is not None and result > max_value:
        raise ValueError(f"{field} 超過合理範圍")
    return result


MEAL_PHOTO_NUTRITION_RULE_VERSION = "tw-exchange-macros-v1"

# 每一核准交換份的巨量營養素基準。奶類在現行審核流程沒有脂肪等級欄位，
# 因此採低脂奶基準並在快照中明確留下警示；油脂份依產品規則維持不計。
MEAL_PHOTO_EXCHANGE_MACROS = {
    "milk_exchange": {"protein_g": 8.0, "fat_g": 4.0, "carbohydrate_g": 12.0},
    "protein_low_exchange": {"protein_g": 7.0, "fat_g": 3.0, "carbohydrate_g": 0.0},
    "protein_medium_exchange": {"protein_g": 7.0, "fat_g": 5.0, "carbohydrate_g": 0.0},
    "protein_high_exchange": {"protein_g": 7.0, "fat_g": 12.0, "carbohydrate_g": 0.0},
    "starch_exchange": {"protein_g": 2.0, "fat_g": 0.0, "carbohydrate_g": 15.0},
    "vegetable_exchange": {"protein_g": 1.0, "fat_g": 0.0, "carbohydrate_g": 5.0},
    "fruit_exchange": {"protein_g": 0.0, "fat_g": 0.0, "carbohydrate_g": 15.0},
    "fat_exchange": {"protein_g": 0.0, "fat_g": 5.0, "carbohydrate_g": 0.0},
}


def estimate_nutrition_from_exchanges(exchanges: Mapping[str, Any]) -> dict[str, Any]:
    """將營養師核准的交換份換成可追溯的估計營養快照。"""
    portions = {
        key: _number(exchanges.get(key, 0), key, max_value=100)
        for key in EXCHANGE_KEYS
    }
    if portions["fat_exchange"] != 0:
        raise ValueError("目前規則不計油脂交換份")
    totals = {"protein_g": 0.0, "fat_g": 0.0, "carbohydrate_g": 0.0}
    for exchange_key, portion in portions.items():
        for nutrient_key, per_exchange in MEAL_PHOTO_EXCHANGE_MACROS[exchange_key].items():
            totals[nutrient_key] += portion * per_exchange
    totals = {key: round(value, 4) for key, value in totals.items()}
    calories = round(
        totals["protein_g"] * 4
        + totals["carbohydrate_g"] * 4
        + totals["fat_g"] * 9,
        4,
    )
    warnings = ["estimated_from_approved_exchanges", "unquantified_oil_and_sauce_excluded"]
    if portions["milk_exchange"] > 0:
        warnings.append("milk_assumed_low_fat")
    return {
        "calories_kcal": calories,
        **totals,
        "_estimate_type": "approved_exchange_estimate",
        "_rule_version": MEAL_PHOTO_NUTRITION_RULE_VERSION,
        "_warnings": warnings,
    }


def _normalize_nutrients(values: Mapping[str, Any] | None) -> dict[str, float]:
    values = values or {}
    return {
        key: _number(values.get(key, 0), key, max_value=NUTRIENT_LIMITS[key])
        for key in NUTRIENT_KEYS
    }


def normalize_label_payload(
    payload: Mapping[str, Any], *, require_product_name: bool = True
) -> dict[str, Any]:
    """驗證並正規化 Vision 回傳的營養標示資料。"""
    if payload.get("status") not in (None, "success"):
        raise ValueError(str(payload.get("message") or "營養標示辨識失敗"))
    if payload.get("image_type") not in (None, "nutrition_label"):
        raise ValueError("圖片不是營養標示")

    product_name = str(payload.get("product_name") or "").strip()
    if require_product_name and not product_name:
        raise ValueError("product_name 不可空白")
    if len(product_name) > 120:
        raise ValueError("product_name 過長")

    package_amount = _number(
        payload.get("package_amount"), "package_amount", allow_zero=False, max_value=100000
    )
    package_unit = str(payload.get("package_unit") or "").strip().lower()
    package_unit = {
        "毫升": "ml", "公撮": "ml", "cc": "ml",
        "公克": "g", "克": "g", "公斤": "kg", "升": "l",
    }.get(package_unit, package_unit)
    if package_unit not in {"g", "kg", "ml", "l", "份", "顆", "包", "瓶", "盒"}:
        raise ValueError("package_unit 不支援")

    servings = _number(
        payload.get("servings_per_package", 1), "servings_per_package", allow_zero=False, max_value=1000
    )
    confidence = _number(payload.get("confidence", 0), "confidence", max_value=1.0)
    try:
        observed_at_confidence = _number(
            payload.get("observed_at_confidence", 0),
            "observed_at_confidence",
            max_value=1.0,
        )
    except ValueError:
        observed_at_confidence = 0.0
    per_serving = _normalize_nutrients(payload.get("per_serving"))
    per_100 = _normalize_nutrients(payload.get("per_100"))
    if per_serving["calories_kcal"] <= 0 or not any(
        per_serving[key] > 0 for key in ("protein_g", "fat_g", "carbohydrate_g")
    ):
        raise ValueError("營養標示缺少有效的每份熱量或三大營養素")
    return {
        "status": "success",
        "image_type": "nutrition_label",
        "product_name": product_name,
        "brand": str(payload.get("brand") or "").strip(),
        "barcode": str(payload.get("barcode") or "").strip(),
        "package_amount": package_amount,
        "package_unit": package_unit,
        "servings_per_package": servings,
        "per_serving": per_serving,
        "per_100": per_100,
        "observed_at": str(payload.get("observed_at") or "").strip(),
        "observed_at_confidence": observed_at_confidence,
        "confidence": confidence,
        "notes": str(payload.get("notes") or "").strip(),
    }


def nutrition_consistency_warnings(label: Mapping[str, Any]) -> list[str]:
    unit = str(label.get("package_unit") or "").lower()
    package_amount = float(label.get("package_amount") or 0)
    servings = float(label.get("servings_per_package") or 0)
    per_serving = label.get("per_serving") or {}
    per_100 = label.get("per_100") or {}
    warnings = []

    calories = float(per_serving.get("calories_kcal") or 0)
    macro_calories = (
        float(per_serving.get("protein_g") or 0) * 4
        + float(per_serving.get("carbohydrate_g") or 0) * 4
        + float(per_serving.get("fat_g") or 0) * 9
    )
    if calories > 0 and macro_calories > 0:
        tolerance = max(50.0, calories * 0.25)
        if abs(calories - macro_calories) > tolerance:
            warnings.append("calories_kcal")

    if unit not in {"g", "ml", "kg", "l"} or package_amount <= 0 or servings <= 0:
        return warnings
    if not any(float(per_100.get(key) or 0) > 0 for key in NUTRIENT_KEYS):
        return warnings
    base_amount = package_amount * 1000 if unit in {"kg", "l"} else package_amount
    serving_amount = base_amount / servings
    for key in NUTRIENT_KEYS:
        serving_value = float(per_serving.get(key) or 0)
        actual_per_100 = float(per_100.get(key) or 0)
        expected_per_100 = serving_value * 100 / serving_amount
        if key == "calories_kcal":
            tolerance = max(1.0, expected_per_100 * 0.08)
        elif key.endswith("_mg"):
            tolerance = max(1.0, expected_per_100 * 0.12)
        else:
            tolerance = max(0.2, expected_per_100 * 0.12)
        if abs(actual_per_100 - expected_per_100) > tolerance and key not in warnings:
            warnings.append(key)
    return warnings


def normalize_product_identity_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("status") != "success" or payload.get("image_type") != "product_front":
        raise ValueError(str(payload.get("message") or "不是有效的商品正面資料"))
    product_name = str(payload.get("product_name") or "").strip()
    brand = str(payload.get("brand") or "").strip()
    barcode = str(payload.get("barcode") or "").strip()
    if not product_name:
        raise ValueError("product_name 不可空白")
    if len(product_name) > 120 or len(brand) > 120 or len(barcode) > 64:
        raise ValueError("商品識別文字過長")
    confidence = _number(payload.get("confidence", 0), "confidence", max_value=1.0)
    return {
        "status": "success",
        "image_type": "product_front",
        "product_name": product_name,
        "brand": brand,
        "barcode": barcode,
        "confidence": confidence,
    }


def normalize_garmin_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("status") != "success" or payload.get("image_type") != "garmin_workout":
        raise ValueError("不是有效的 Garmin 運動資料")
    workout_type = str(payload.get("workout_type") or "").strip()
    if workout_type not in {"跑步", "室內自行車", "游泳", "其他"}:
        raise ValueError("workout_type 不支援")
    required = ("duration_min", "aerobic_te", "anaerobic_te", "load_value")
    if any(key not in payload for key in required):
        raise ValueError("Garmin 資料缺少必要欄位")
    result = {
        "status": "success",
        "image_type": "garmin_workout",
        "workout_type": workout_type,
        "duration_min": _number(payload["duration_min"], "duration_min", allow_zero=False, max_value=1440),
        "avg_hr": _number(payload.get("avg_hr", 0), "avg_hr", max_value=260),
        "max_hr": _number(payload.get("max_hr", 0), "max_hr", max_value=260),
        "aerobic_te": _number(payload["aerobic_te"], "aerobic_te", max_value=5.0),
        "anaerobic_te": _number(payload["anaerobic_te"], "anaerobic_te", max_value=5.0),
        "primary_benefit": str(payload.get("primary_benefit") or "").strip(),
        "load_value": _number(payload["load_value"], "load_value", max_value=10000),
        "np_w": _number(payload.get("np_w", 0), "np_w", max_value=3000),
        "if_value": _number(payload.get("if_value", 0), "if_value", max_value=3),
        "tss": _number(payload.get("tss", 0), "tss", max_value=5000),
        "ftp_w": _number(payload.get("ftp_w", 0), "ftp_w", max_value=3000),
    }
    if result["avg_hr"] > 0 and result["max_hr"] > 0 and result["max_hr"] < result["avg_hr"]:
        raise ValueError("最大心率不可低於平均心率")
    return result


def scale_nutrition(per_serving: Mapping[str, Any], consumed_servings: float) -> dict[str, float]:
    servings = _number(consumed_servings, "consumed_servings")
    nutrients = _normalize_nutrients(per_serving)
    return {key: round(value * servings, 4) for key, value in nutrients.items()}


EXCHANGE_RULE_VERSION = "tw-exchange-v1"


def suggest_exchange_portions(
    *, product_name: str, nutrition: Mapping[str, Any]
) -> dict[str, Any]:
    """依正式營養規則產生可審核的份量建議；不等同營養師核准值。"""
    name = " ".join(str(product_name or "").strip().lower().split())
    nutrients = _normalize_nutrients(nutrition)
    protein = nutrients["protein_g"]
    fat = nutrients["fat_g"]
    carbohydrate = nutrients["carbohydrate_g"]

    milk_words = ("鮮乳", "牛奶", "乳飲", "優酪乳", "優格", "yogurt", "milk")
    protein_words = (
        "豆漿", "豆乳", "豆腐", "豆干", "雞", "豬", "牛", "羊", "魚", "蝦",
        "海鮮", "蛋", "肉", "protein", "雞胸",
    )
    starch_words = ("飯", "麵", "麥", "吐司", "麵包", "餅", "穀", "燕麥", "薯", "粥")
    drink_words = ("能量飲料", "汽水", "飲料", "energy drink")
    vegetable_words = ("蔬菜", "青菜", "沙拉", "花椰菜", "菠菜")
    fruit_words = ("水果", "果汁", "蘋果", "香蕉", "芭樂", "柳橙", "莓")

    categories: list[str] = []
    warnings: list[str] = []
    if any(word in name for word in milk_words) and "豆" not in name:
        categories.append("milk")
    else:
        if any(word in name for word in protein_words):
            categories.append("protein")
        carb_matches = []
        if any(word in name for word in starch_words):
            carb_matches.append("starch")
        if any(word in name for word in vegetable_words):
            carb_matches.append("vegetable")
        if any(word in name for word in fruit_words):
            carb_matches.append("fruit")
        if len(carb_matches) > 1:
            warnings.append("ambiguous_carbohydrate_category")
        # 同一份碳水只能歸到一個類別：明確主食優先，其次水果、蔬菜；
        # 泛稱飲料或無法辨識來源時才後備為主食建議。
        if "starch" in carb_matches:
            categories.append("starch")
        elif "fruit" in carb_matches:
            categories.append("fruit")
        elif "vegetable" in carb_matches:
            categories.append("vegetable")
        elif any(word in name for word in drink_words) or carbohydrate >= 2.0:
            categories.append("starch")
        if not categories and protein >= 3.5:
            categories.append("protein")

    order = ("milk", "protein", "starch", "vegetable", "fruit")
    categories = [category for category in order if category in categories]
    exchanges = {key: 0.0 for key in EXCHANGE_KEYS}

    if "milk" in categories:
        protein_ratio = protein / 8.0 if protein > 0 else 0.0
        carbohydrate_ratio = carbohydrate / 12.0 if carbohydrate > 0 else 0.0
        if not protein_ratio or not carbohydrate_ratio:
            warnings.append("milk_macro_incomplete")
        elif abs(protein_ratio - carbohydrate_ratio) / max(protein_ratio, carbohydrate_ratio) > 0.35:
            warnings.append("milk_macro_mismatch")
        else:
            exchanges["milk_exchange"] = round((protein_ratio + carbohydrate_ratio) / 2, 2)
    if "protein" in categories and protein > 0:
        protein_exchange = protein / 7.0
        fat_per_exchange = fat / protein_exchange if protein_exchange else 0.0
        fat_levels = {
            "protein_low_exchange": 3.0,
            "protein_medium_exchange": 5.0,
            "protein_high_exchange": 12.0,
        }
        level = min(fat_levels, key=lambda key: abs(fat_levels[key] - fat_per_exchange))
        exchanges[level] = round(protein_exchange, 2)
    if "starch" in categories:
        exchanges["starch_exchange"] = round(carbohydrate / 15.0, 2)
    if "vegetable" in categories:
        exchanges["vegetable_exchange"] = round(carbohydrate / 5.0, 2)
    if "fruit" in categories:
        exchanges["fruit_exchange"] = round(carbohydrate / 15.0, 2)

    # 一日樂食目前不計油脂份；脂肪克數與熱量仍留在nutrition snapshot。
    exchanges["fat_exchange"] = 0.0
    return {
        "categories": categories or ["unknown"],
        "warnings": warnings,
        "exchanges": exchanges,
        "review_status": "pending_review",
        "rule_version": EXCHANGE_RULE_VERSION,
    }


def food_fingerprint(
    product_name: str,
    brand: str,
    package_amount: float,
    package_unit: str,
    per_serving: Mapping[str, Any],
    *,
    barcode: str = "",
    servings_per_package: float = 1,
    per_100: Mapping[str, Any] | None = None,
) -> str:
    canonical = {
        "product_name": " ".join(str(product_name).strip().lower().split()),
        "brand": " ".join(str(brand).strip().lower().split()),
        "barcode": str(barcode).strip(),
        "package_amount": round(float(package_amount), 4),
        "package_unit": str(package_unit).strip().lower(),
        "servings_per_package": round(float(servings_per_package), 4),
        "per_serving": {key: round(float(per_serving.get(key, 0) or 0), 4) for key in sorted(NUTRIENT_KEYS)},
        "per_100": {key: round(float((per_100 or {}).get(key, 0) or 0), 4) for key in sorted(NUTRIENT_KEYS)},
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def exchange_approval_hash(
    food_fingerprint_value: str, rule_version: str, exchanges: Mapping[str, Any]
) -> str:
    canonical = {
        "food_fingerprint": str(food_fingerprint_value),
        "rule_version": str(rule_version),
        "exchanges": {
            key: round(float(exchanges.get(key, 0) or 0), 4) for key in EXCHANGE_KEYS
        },
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def remaining_targets(target: Mapping[str, Any], consumed: Mapping[str, Any]) -> dict[str, float]:
    keys = list(dict.fromkeys([*target.keys(), *consumed.keys()]))
    result = {}
    for key in keys:
        target_value = _number(target.get(key, 0), key)
        consumed_value = _number(consumed.get(key, 0), key)
        result[key] = round(max(0.0, target_value - consumed_value), 4)
    return result


def rank_menu_candidates(
    remaining: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """以份量代號優先、營養素後備，對安全且可供應餐點排序。"""
    weights = {
        "protein_low_exchange": 2.0,
        "protein_medium_exchange": 2.0,
        "protein_high_exchange": 2.0,
        "starch_exchange": 1.8,
        "vegetable_exchange": 1.8,
        "fruit_exchange": 1.0,
        "milk_exchange": 1.0,
        "fat_exchange": 1.3,
        "protein_g": 1.5,
        "carbohydrate_g": 1.1,
        "fat_g": 1.0,
        "calories_kcal": 0.8,
    }
    ranked: list[dict[str, Any]] = []
    for source in candidates:
        if not source.get("safe", True) or not source.get("available", True):
            continue
        candidate = dict(source)
        weighted_error = 0.0
        used_weight = 0.0
        for key, weight in weights.items():
            target = float(remaining.get(key, 0) or 0)
            if target <= 0 or key not in candidate or candidate.get(key) in (None, ""):
                continue
            actual = max(0.0, float(candidate.get(key, 0) or 0))
            weighted_error += min(abs(actual - target) / max(target, 1.0), 3.0) * weight
            used_weight += weight
        normalized_error = weighted_error / used_weight if used_weight else 9.0
        candidate["match_score"] = round(max(0.0, 100.0 * (1.0 - min(normalized_error, 1.0))), 1)
        ranked.append(candidate)
    ranked.sort(key=lambda row: (-row["match_score"], str(row.get("name", ""))))
    return ranked[: max(0, int(limit))]


def build_label_confirmation_bubble(
    label: Mapping[str, Any], *, token: str, consumed_servings: float = 1,
    consumed_at: str = "", consumed_time_source: str = "line_timestamp",
) -> dict[str, Any]:
    normalized = normalize_label_payload(label)
    nutrition = scale_nutrition(normalized["per_serving"], consumed_servings)
    amount = normalized["package_amount"] * float(consumed_servings) / normalized["servings_per_package"]
    brand_line = f"{normalized['brand']}｜" if normalized["brand"] else ""
    try:
        consumed_time_text = datetime.fromisoformat(consumed_at).strftime("%Y/%m/%d %H:%M")
    except (TypeError, ValueError):
        consumed_time_text = "以LINE收到時間為準"
    source_text = {
        "photo_timestamp": "照片時間",
        "manual": "手動設定",
    }.get(consumed_time_source, "LINE收到時間")
    exchange_suggestion = suggest_exchange_portions(
        product_name=normalized["product_name"], nutrition=nutrition
    )
    exchange_labels = {
        "milk_exchange": "奶類",
        "protein_low_exchange": "低脂蛋白",
        "protein_medium_exchange": "中脂蛋白",
        "protein_high_exchange": "高脂蛋白",
        "starch_exchange": "主食",
        "vegetable_exchange": "蔬菜",
        "fruit_exchange": "水果",
    }
    exchange_parts = [
        f"{label_text} {exchange_suggestion['exchanges'][key]:g}份"
        for key, label_text in exchange_labels.items()
        if exchange_suggestion["exchanges"][key] > 0
    ]
    exchange_text = "｜".join(exchange_parts) if exchange_parts else "目前無法安全推算，需人工審核"
    body = [
        {"type": "text", "text": normalized["product_name"], "size": "xl", "weight": "bold", "wrap": True},
        {"type": "text", "text": f"{brand_line}{amount:g}{normalized['package_unit']}｜{float(consumed_servings):g}份", "size": "sm", "color": "#666666", "margin": "sm", "wrap": True},
        {"type": "text", "text": f"進食時間：{consumed_time_text}（{source_text}）", "size": "xs", "color": "#666666", "margin": "xs", "wrap": True},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": f"熱量  {nutrition['calories_kcal']:g} kcal", "size": "md", "weight": "bold", "margin": "md"},
        {"type": "text", "text": f"蛋白質 {nutrition['protein_g']:g}g　脂肪 {nutrition['fat_g']:g}g", "size": "sm", "color": "#333333", "margin": "sm"},
        {"type": "text", "text": f"碳水 {nutrition['carbohydrate_g']:g}g　糖 {nutrition['sugar_g']:g}g", "size": "sm", "color": "#333333", "margin": "sm"},
        {"type": "text", "text": f"纖維 {nutrition['fiber_g']:g}g　鈉 {nutrition['sodium_mg']:g}mg", "size": "sm", "color": "#333333", "margin": "sm"},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": "推算營養份數（待審核）", "size": "sm", "weight": "bold", "color": "#0F766E", "margin": "md"},
        {"type": "text", "text": exchange_text, "size": "sm", "color": "#333333", "margin": "sm", "wrap": True},
        {"type": "text", "text": "油脂份不計；脂肪克數與熱量仍完整記錄。此為公式建議值，尚未扣入個人計畫；確認後會加入私人食品庫與今日飲食紀錄。", "size": "xs", "color": "#8A6D3B", "margin": "sm", "wrap": True},
    ]
    warning_fields = nutrition_consistency_warnings(normalized)
    if warning_fields:
        warning_text = "、".join(f"{NUTRIENT_LABELS[key]}換算不一致" for key in warning_fields)
        body.insert(-1, {
            "type": "text",
            "text": f"⚠️ {warning_text}，確認前請按『修正營養』。",
            "size": "sm",
            "weight": "bold",
            "color": "#B91C1C",
            "margin": "md",
            "wrap": True,
        })
    return {
        "type": "bubble",
        "size": "mega",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F766E", "paddingAll": "16px", "contents": [
            {"type": "text", "text": "📷 營養標示辨識完成", "color": "#FFFFFF", "weight": "bold", "size": "lg"},
            {"type": "text", "text": "請先確認，尚未正式記錄", "color": "#CCFBF1", "size": "sm", "margin": "xs"},
        ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "18px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px", "contents": [
            {"type": "button", "style": "primary", "color": "#06C755", "height": "sm", "action": {"type": "message", "label": "確認並記錄", "text": f"確認營養紀錄:{token}"}},
            {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "修改品名", "text": f"修改營養品名:{token}"}},
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "修正營養", "text": f"修改營養數字:{token}"}},
            ]},
            {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "修改份量", "text": f"修改營養份量:{token}"}},
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "修改時間", "text": f"修改營養時間:{token}"}},
            ]},
            {"type": "button", "style": "link", "height": "sm", "color": "#888888", "action": {"type": "message", "label": "取消，不記錄", "text": f"取消營養紀錄:{token}"}},
        ]},
    }


def nutrition_sheet_specs() -> dict[str, dict[str, list[list[Any]] | list[str]]]:
    return {
        "營養份量規則": {
            "headers": ["代號", "食物類別", "脂肪等級", "每份蛋白質g", "每份脂肪g", "每份碳水g", "每份熱量kcal", "預設份量", "單位", "換算說明", "狀態"],
            "seed_rows": [
                ["奶", "奶類", "全脂", 8, 8, 12, 150, 240, "ml", "一杯240ml", "active"],
                ["奶", "奶類", "低脂", 8, 4, 12, 120, 240, "ml", "一杯240ml", "active"],
                ["奶", "奶類", "脫脂", 8, 0, 12, 80, 240, "ml", "一杯240ml", "active"],
                ["蛋", "豆魚蛋肉類", "高脂", 7, 12, 0, 120, 1, "份", "需營養師確認食物歸類", "active"],
                ["蛋", "豆魚蛋肉類", "中脂", 7, 5, 0, 75, 1, "份", "需營養師確認食物歸類", "active"],
                ["蛋", "豆魚蛋肉類", "低脂", 7, 3, 0, 55, 1, "份", "約30g熟肉", "active"],
                ["主", "全穀雜糧類", "NA", 2, 0, 15, 70, 1, "份", "飯1/4碗", "active"],
                ["菜", "蔬菜類", "NA", 1, 0, 5, 25, 100, "g", "熟菜1碗約生重100g", "active"],
                ["果", "水果類", "NA", 0, 0, 15, 60, 1, "份", "依水果換算", "active"],
                ["油", "油脂堅果類", "NA", 0, 5, 0, 45, 1, "份", "油1茶匙或堅果約10g", "active"],
            ],
        },
        "食品資料庫": {
            "headers": [
                "food_id", "品名", "品牌", "條碼", "來源類型", "建立者User_ID", "可見範圍",
                "包裝容量", "單位", "每包裝份數", "每份熱量kcal", "每份蛋白質g", "每份脂肪g",
                "每份碳水g", "糖g", "膳食纖維g", "鈉mg", "奶份", "低脂蛋白份", "中脂蛋白份",
                "高脂蛋白份", "主食份", "蔬菜份", "水果份", "油脂份", "換算審核狀態",
                "原始圖片Ref", "辨識信心", "驗證狀態", "fingerprint", "建立時間", "更新時間",
            ],
            "seed_rows": [],
        },
        "客製化營養計畫": {
            "headers": [
                "plan_id", "User_ID", "計畫名稱", "版本", "生效日期", "結束日期", "星期",
                "日型態", "餐別", "預定時間", "熱量目標", "蛋白質目標g", "脂肪目標g",
                "碳水目標g", "奶份", "低脂蛋白份", "中脂蛋白份", "高脂蛋白份", "主食份",
                "蔬菜份", "水果份", "油脂份", "指定食品", "運動情境", "營養師", "狀態", "備註",
            ],
            "seed_rows": [],
        },
        "飲食紀錄": {
            "headers": [
                "log_id", "User_ID", "food_id", "品名", "攝取時間", "餐別", "攝取份數", "攝取量", "單位",
                "熱量kcal", "蛋白質g", "脂肪g", "碳水g", "糖g", "膳食纖維g", "鈉mg", "奶份",
                "低脂蛋白份", "中脂蛋白份", "高脂蛋白份", "主食份", "蔬菜份", "水果份", "油脂份",
                "來源圖片Ref", "plan_id", "確認狀態", "建立時間", "更新時間",
            ],
            "seed_rows": [],
        },
    }


def ensure_nutrition_schema(conn: sqlite3.Connection) -> None:
    """建立營養功能所需資料表；只新增，不刪除或覆寫既有表。"""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nutrition_schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS food_catalog (
            food_id TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            brand TEXT DEFAULT '',
            barcode TEXT DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'user_private_food',
            owner_user_id TEXT DEFAULT '',
            visibility TEXT NOT NULL DEFAULT 'private',
            package_amount REAL DEFAULT 0,
            package_unit TEXT DEFAULT '',
            servings_per_package REAL DEFAULT 1,
            per_serving_json TEXT NOT NULL DEFAULT '{}',
            per_100_json TEXT NOT NULL DEFAULT '{}',
            exchange_json TEXT NOT NULL DEFAULT '{}',
            exchange_review_status TEXT NOT NULL DEFAULT 'pending_review',
            fingerprint TEXT NOT NULL,
            original_image_ref TEXT DEFAULT '',
            recognition_confidence REAL DEFAULT 0,
            verification_status TEXT NOT NULL DEFAULT 'user_confirmed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS nutrition_plans (
            plan_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            effective_from TEXT NOT NULL,
            effective_to TEXT DEFAULT '',
            daily_targets_json TEXT NOT NULL DEFAULT '{}',
            dietitian_name TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS nutrition_plan_slots (
            slot_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            weekday INTEGER NOT NULL,
            day_type TEXT DEFAULT '',
            meal_slot TEXT NOT NULL,
            planned_time TEXT DEFAULT '',
            targets_json TEXT NOT NULL DEFAULT '{}',
            specified_foods_json TEXT NOT NULL DEFAULT '[]',
            workout_context TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            FOREIGN KEY(plan_id) REFERENCES nutrition_plans(plan_id)
        );

        CREATE TABLE IF NOT EXISTS food_logs (
            log_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            food_id TEXT NOT NULL,
            consumed_at TEXT NOT NULL,
            meal_slot TEXT DEFAULT '',
            consumed_servings REAL NOT NULL DEFAULT 1,
            consumed_amount REAL DEFAULT 0,
            consumed_unit TEXT DEFAULT '',
            nutrition_snapshot_json TEXT NOT NULL,
            exchange_snapshot_json TEXT NOT NULL DEFAULT '{}',
            approved_exchange_json TEXT NOT NULL DEFAULT '{}',
            exchange_approval_id TEXT DEFAULT '',
            source_image_ref TEXT DEFAULT '',
            plan_id TEXT DEFAULT '',
            plan_link_status TEXT NOT NULL DEFAULT 'pending',
            confirmation_status TEXT NOT NULL DEFAULT 'confirmed',
            legacy_applied_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(food_id) REFERENCES food_catalog(food_id)
        );

        CREATE TABLE IF NOT EXISTS food_exchange_approvals (
            approval_id TEXT PRIMARY KEY,
            food_id TEXT NOT NULL,
            food_fingerprint TEXT NOT NULL,
            suggestion_rule_version TEXT NOT NULL,
            approved_exchange_json TEXT NOT NULL,
            approved_exchange_hash TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            FOREIGN KEY(food_id) REFERENCES food_catalog(food_id)
        );

        CREATE TABLE IF NOT EXISTS pending_nutrition_logs (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            label_payload_json TEXT NOT NULL,
            source_image_ref TEXT DEFAULT '',
            source_message_id TEXT DEFAULT '',
            identity_message_id TEXT DEFAULT '',
            consumed_servings REAL NOT NULL DEFAULT 1,
            meal_slot TEXT DEFAULT '',
            consumed_at TEXT DEFAULT '',
            consumed_time_source TEXT NOT NULL DEFAULT 'line_timestamp',
            status TEXT NOT NULL DEFAULT 'pending',
            confirmed_log_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT DEFAULT '',
            retired_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS nutrition_input_states (
            user_id TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            input_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(token) REFERENCES pending_nutrition_logs(token)
        );

        CREATE TABLE IF NOT EXISTS nutrition_message_events (
            message_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(token) REFERENCES pending_nutrition_logs(token)
        );

        CREATE TABLE IF NOT EXISTS nutrition_sheet_outbox (
            outbox_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '',
            claimed_at TEXT DEFAULT '',
            lease_owner TEXT DEFAULT '',
            resync_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            synced_at TEXT DEFAULT '',
            UNIQUE(entity_type, entity_id)
        );

        CREATE TABLE IF NOT EXISTS combo_log_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            combo_name TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_food_catalog_owner_fingerprint
            ON food_catalog(owner_user_id, fingerprint, source_type);
        CREATE INDEX IF NOT EXISTS idx_food_catalog_owner ON food_catalog(owner_user_id);
        CREATE INDEX IF NOT EXISTS idx_food_logs_user_time ON food_logs(user_id, consumed_at);
        CREATE INDEX IF NOT EXISTS idx_plan_user_effective ON nutrition_plans(user_id, effective_from);
        CREATE INDEX IF NOT EXISTS idx_food_exchange_approvals_food
            ON food_exchange_approvals(food_id, approved_at);
        """
    )
    schema_component = "nutrition_system"
    schema_version = 4
    version_row = conn.execute(
        "SELECT version FROM nutrition_schema_versions WHERE component=?",
        (schema_component,),
    ).fetchone()
    if version_row and int(version_row[0]) >= schema_version:
        return
    conn.execute("BEGIN IMMEDIATE")
    version_row = conn.execute(
        "SELECT version FROM nutrition_schema_versions WHERE component=?",
        (schema_component,),
    ).fetchone()
    if version_row and int(version_row[0]) >= schema_version:
        conn.commit()
        return
    migrations = {
        "pending_nutrition_logs": {
            "source_message_id": "TEXT DEFAULT ''",
            "identity_message_id": "TEXT DEFAULT ''",
            "confirmed_log_id": "TEXT DEFAULT ''",
            "retired_at": "TEXT DEFAULT ''",
            "consumed_time_source": "TEXT NOT NULL DEFAULT 'line_timestamp'",
        },
        "food_logs": {
            "legacy_applied_at": "TEXT DEFAULT ''",
            "plan_link_status": "TEXT NOT NULL DEFAULT 'pending'",
            "approved_exchange_json": "TEXT NOT NULL DEFAULT '{}'",
            "exchange_approval_id": "TEXT DEFAULT ''",
        },
        "nutrition_message_events": {
            "result_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "nutrition_sheet_outbox": {
            "claimed_at": "TEXT DEFAULT ''",
            "lease_owner": "TEXT DEFAULT ''",
            "resync_required": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in migrations.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # 舊版 outbox 可能沒有複合唯一約束；先合併重複事件再建立索引。
    duplicate_outbox = conn.execute(
        """SELECT entity_type, entity_id FROM nutrition_sheet_outbox
           GROUP BY entity_type, entity_id HAVING COUNT(*) > 1"""
    ).fetchall()
    for entity_type, entity_id in duplicate_outbox:
        rows = conn.execute(
            """SELECT rowid, status, attempts, created_at, synced_at
               FROM nutrition_sheet_outbox WHERE entity_type=? AND entity_id=?
               ORDER BY created_at, rowid""",
            (entity_type, entity_id),
        ).fetchall()
        keeper = rows[0][0]
        all_synced = all(row[1] == "synced" for row in rows)
        conn.execute(
            """UPDATE nutrition_sheet_outbox
               SET status=?, attempts=?, last_error='', claimed_at='', lease_owner='',
                   resync_required=0, synced_at=? WHERE rowid=?""",
            (
                "synced" if all_synced else "pending",
                max(int(row[2] or 0) for row in rows),
                max((str(row[4] or "") for row in rows), default="") if all_synced else "",
                keeper,
            ),
        )
        conn.execute(
            "DELETE FROM nutrition_sheet_outbox WHERE entity_type=? AND entity_id=? AND rowid<>?",
            (entity_type, entity_id, keeper),
        )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_nutrition_outbox_entity
           ON nutrition_sheet_outbox(entity_type, entity_id)"""
    )
    # 舊版資料可能在unique index上線前已產生重複LINE message ID；保留最新草稿，
    # 清除較舊列的冪等鍵，再建立索引，避免部署啟動時migration失敗。
    duplicate_source_messages = conn.execute(
        """SELECT user_id,source_message_id FROM pending_nutrition_logs
           WHERE source_message_id<>'' GROUP BY user_id,source_message_id HAVING COUNT(*)>1"""
    ).fetchall()
    for user_id, message_id in duplicate_source_messages:
        rows = conn.execute(
            """SELECT rowid FROM pending_nutrition_logs
               WHERE user_id=? AND source_message_id=?
               ORDER BY created_at DESC,rowid DESC""",
            (user_id, message_id),
        ).fetchall()
        for (rowid,) in rows[1:]:
            conn.execute(
                "UPDATE pending_nutrition_logs SET source_message_id='' WHERE rowid=?",
                (rowid,),
            )
    duplicate_identity_messages = conn.execute(
        """SELECT identity_message_id FROM pending_nutrition_logs
           WHERE identity_message_id<>'' GROUP BY identity_message_id HAVING COUNT(*)>1"""
    ).fetchall()
    for (message_id,) in duplicate_identity_messages:
        rows = conn.execute(
            """SELECT rowid FROM pending_nutrition_logs
               WHERE identity_message_id=? ORDER BY created_at DESC,rowid DESC""",
            (message_id,),
        ).fetchall()
        for (rowid,) in rows[1:]:
            conn.execute(
                "UPDATE pending_nutrition_logs SET identity_message_id='' WHERE rowid=?",
                (rowid,),
            )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_source_message
           ON pending_nutrition_logs(user_id, source_message_id)
           WHERE source_message_id <> ''"""
    )
    duplicate_awaiting_users = conn.execute(
        """SELECT user_id FROM pending_nutrition_logs
           WHERE status='awaiting_identity' GROUP BY user_id HAVING COUNT(*) > 1"""
    ).fetchall()
    for (user_id,) in duplicate_awaiting_users:
        rows = conn.execute(
            """SELECT rowid FROM pending_nutrition_logs
               WHERE user_id=? AND status='awaiting_identity'
               ORDER BY created_at DESC, rowid DESC""",
            (user_id,),
        ).fetchall()
        for (rowid,) in rows[1:]:
            conn.execute(
                """UPDATE pending_nutrition_logs
                   SET status='expired',label_payload_json='{}',
                       retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
                   WHERE rowid=?""",
                (datetime.now().astimezone().isoformat(timespec="seconds"), rowid),
            )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_one_awaiting_identity_per_user
           ON pending_nutrition_logs(user_id) WHERE status='awaiting_identity'"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_identity_message
           ON pending_nutrition_logs(identity_message_id)
           WHERE identity_message_id <> ''"""
    )
    conn.execute(
        """INSERT INTO nutrition_schema_versions(component,version,applied_at)
           VALUES (?,?,?)
           ON CONFLICT(component) DO UPDATE SET
             version=excluded.version,applied_at=excluded.applied_at""",
        (
            schema_component,
            schema_version,
            datetime.now().astimezone().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def save_pending_label(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    payload: Mapping[str, Any],
    source_image_ref: str = "",
    source_message_id: str = "",
    consumed_servings: float = 1,
    meal_slot: str = "",
    consumed_at: str = "",
    consumed_time_source: str = "line_timestamp",
    allow_missing_identity: bool = False,
) -> str:
    normalized = normalize_label_payload(payload, require_product_name=not allow_missing_identity)
    if consumed_time_source not in {"photo_timestamp", "line_timestamp", "manual"}:
        raise ValueError("consumed_time_source 不支援")
    status = "pending" if normalized["product_name"] else "awaiting_identity"
    if source_message_id:
        existing = conn.execute(
            "SELECT token FROM pending_nutrition_logs WHERE user_id=? AND source_message_id=?",
            (user_id, source_message_id),
        ).fetchone()
        if existing:
            return existing[0]
    if status == "awaiting_identity":
        active = get_latest_awaiting_identity(conn, user_id=user_id)
        if active:
            raise ValueError("已有一筆營養標示等待商品正面，請先完成或取消後再上傳下一項")
    token = uuid.uuid4().hex[:12]
    now_dt = datetime.now().astimezone()
    now = now_dt.isoformat(timespec="seconds")
    expires_at = (now_dt + timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        conn.execute(
            """
            INSERT INTO pending_nutrition_logs
            (token, user_id, label_payload_json, source_image_ref, source_message_id,
             consumed_servings, meal_slot, consumed_at, consumed_time_source,
             status, confirmed_log_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                token,
                user_id,
                json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False),
                source_image_ref,
                source_message_id,
                _number(consumed_servings, "consumed_servings", allow_zero=False, max_value=100),
                meal_slot,
                consumed_at or now,
                consumed_time_source,
                status,
                now,
                expires_at,
            ),
        )
        conn.commit()
        return token
    except sqlite3.IntegrityError:
        if source_message_id:
            existing = conn.execute(
                "SELECT token FROM pending_nutrition_logs WHERE user_id=? AND source_message_id=?",
                (user_id, source_message_id),
            ).fetchone()
            if existing:
                return existing[0]
        if status == "awaiting_identity":
            raise ValueError("已有一筆營養標示等待商品正面，請先完成或取消後再上傳下一項")
        raise


def _pending_is_expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at)
        now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
        return expires < now
    except (TypeError, ValueError):
        return True


def set_nutrition_input_state(
    conn: sqlite3.Connection, *, user_id: str, token: str, input_type: str
) -> None:
    if input_type not in {"name", "nutrient"}:
        raise ValueError("不支援的營養輸入狀態")
    row = conn.execute(
        """SELECT status, expires_at FROM pending_nutrition_logs
           WHERE token=? AND user_id=?""",
        (token, user_id),
    ).fetchone()
    if not row or row[0] not in {"pending", "awaiting_identity"}:
        raise ValueError("找不到可修改的營養草稿")
    if _pending_is_expired(row[1]):
        conn.execute(
            """UPDATE pending_nutrition_logs
               SET status='expired',label_payload_json='{}',
                   retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
               WHERE token=? AND user_id=?""",
            (datetime.now().astimezone().isoformat(timespec="seconds"), token, user_id),
        )
        conn.commit()
        raise ValueError("這筆營養草稿已逾時")
    now_dt = datetime.now().astimezone()
    now = now_dt.isoformat(timespec="seconds")
    expires_at = (now_dt + timedelta(minutes=30)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO nutrition_input_states (user_id,token,input_type,created_at,expires_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET token=excluded.token,
             input_type=excluded.input_type, created_at=excluded.created_at,
             expires_at=excluded.expires_at""",
        (user_id, token, input_type, now, expires_at),
    )
    conn.commit()


def get_nutrition_input_state(
    conn: sqlite3.Connection, *, user_id: str
) -> dict[str, str] | None:
    row = conn.execute(
        "SELECT token,input_type,expires_at FROM nutrition_input_states WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    token, input_type, expires_at = row
    pending = conn.execute(
        "SELECT status,expires_at FROM pending_nutrition_logs WHERE token=? AND user_id=?",
        (token, user_id),
    ).fetchone()
    if (
        _pending_is_expired(expires_at)
        or not pending
        or pending[0] not in {"pending", "awaiting_identity"}
        or _pending_is_expired(pending[1])
    ):
        clear_nutrition_input_state(conn, user_id=user_id)
        return None
    return {"token": token, "input_type": input_type}


def clear_nutrition_input_state(conn: sqlite3.Connection, *, user_id: str) -> None:
    conn.execute("DELETE FROM nutrition_input_states WHERE user_id=?", (user_id,))
    conn.commit()


def get_latest_awaiting_identity(
    conn: sqlite3.Connection, *, user_id: str
) -> dict[str, Any] | None:
    rows = conn.execute(
        """SELECT token, label_payload_json, expires_at FROM pending_nutrition_logs
           WHERE user_id=? AND status='awaiting_identity'
           ORDER BY created_at DESC, rowid DESC""",
        (user_id,),
    ).fetchall()
    for token, payload_json, expires_at in rows:
        if _pending_is_expired(expires_at):
            conn.execute(
                """UPDATE pending_nutrition_logs
                   SET status='expired',label_payload_json='{}',
                       retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
                   WHERE token=? AND status='awaiting_identity'""",
                (datetime.now().astimezone().isoformat(timespec="seconds"), token),
            )
            continue
        conn.commit()
        return {
            "token": token,
            "label": normalize_label_payload(
                json.loads(payload_json), require_product_name=False
            ),
        }
    conn.commit()
    return None


def _editable_pending_row(
    conn: sqlite3.Connection, *, user_id: str, token: str
) -> tuple[dict[str, Any], str]:
    row = conn.execute(
        """SELECT label_payload_json, status, expires_at FROM pending_nutrition_logs
           WHERE token=? AND user_id=?""",
        (token, user_id),
    ).fetchone()
    if not row:
        raise ValueError("找不到待確認的營養紀錄")
    payload_json, status, expires_at = row
    if _pending_is_expired(expires_at):
        conn.execute(
            """UPDATE pending_nutrition_logs
               SET status='expired',label_payload_json='{}',
                   retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
               WHERE token=? AND user_id=?""",
            (datetime.now().astimezone().isoformat(timespec="seconds"), token, user_id),
        )
        conn.commit()
        raise ValueError("這筆營養草稿已逾時")
    if status not in {"pending", "awaiting_identity"}:
        raise ValueError("這筆營養紀錄已處理")
    return json.loads(payload_json), status


def attach_latest_pending_identity(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    identity: Mapping[str, Any],
    message_id: str,
) -> dict[str, Any]:
    normalized_identity = normalize_product_identity_payload(identity)
    if normalized_identity["confidence"] < 0.65:
        raise ValueError("商品正面辨識信心不足")
    message_id = str(message_id or "").strip()
    if not message_id or len(message_id) > 255:
        raise ValueError("商品正面訊息識別碼無效")
    conn.execute("BEGIN IMMEDIATE")
    try:
        event = conn.execute(
            "SELECT user_id,event_type,token FROM nutrition_message_events WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if event:
            if event[0] != user_id or event[1] != "product_front":
                raise ValueError("訊息識別碼已由其他流程使用")
            row = conn.execute(
                "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=? AND user_id=?",
                (event[2], user_id),
            ).fetchone()
            if not row:
                raise ValueError("找不到原商品正面配對結果")
            label = normalize_label_payload(json.loads(row[0]))
            conn.commit()
            return {"token": event[2], "label": label, "replayed": True}

        row = conn.execute(
            """SELECT token,label_payload_json,expires_at FROM pending_nutrition_logs
               WHERE user_id=? AND status='awaiting_identity'
               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if not row:
            raise ValueError("找不到等待商品正面的營養草稿")
        token, payload_json, expires_at = row
        if _pending_is_expired(expires_at):
            conn.execute(
                """UPDATE pending_nutrition_logs
                   SET status='expired',label_payload_json='{}',
                       retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
                   WHERE token=? AND user_id=? AND status='awaiting_identity'""",
                (datetime.now().astimezone().isoformat(timespec="seconds"), token, user_id),
            )
            conn.commit()
            raise ValueError("這筆營養草稿已逾時")
        payload = json.loads(payload_json)
        old_barcode = str(payload.get("barcode") or "").strip()
        new_barcode = normalized_identity["barcode"]
        if old_barcode and new_barcode and old_barcode != new_barcode:
            raise ValueError("商品正面與營養標示條碼不一致")
        payload.update(
            product_name=normalized_identity["product_name"],
            brand=normalized_identity["brand"],
            barcode=new_barcode or old_barcode,
        )
        label = normalize_label_payload(payload)
        changed = conn.execute(
            """UPDATE pending_nutrition_logs
               SET label_payload_json=?,status='pending',identity_message_id=?
               WHERE token=? AND user_id=? AND status='awaiting_identity'""",
            (
                json.dumps(label, ensure_ascii=False, sort_keys=True, allow_nan=False),
                message_id,
                token,
                user_id,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("商品照片配對狀態衝突")
        conn.execute(
            """INSERT INTO nutrition_message_events
               (message_id,user_id,event_type,token,created_at)
               VALUES (?,?, 'product_front', ?,?)""",
            (message_id, user_id, token, datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        conn.commit()
        return {"token": token, "label": label, "replayed": False}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def apply_nutrition_text_edit(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    message_id: str,
    product_name: str = "",
    field: str = "",
    value: Any = None,
    corrections: Sequence[tuple[str, Any]] | None = None,
) -> dict[str, Any]:
    message_id = str(message_id or "").strip()
    if not message_id or len(message_id) > 255:
        raise ValueError("文字訊息識別碼無效")
    conn.execute("BEGIN IMMEDIATE")
    try:
        event = conn.execute(
            "SELECT user_id,event_type,token,result_json FROM nutrition_message_events WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if event:
            if event[0] != user_id or event[1] != "text_edit":
                raise ValueError("訊息識別碼已由其他流程使用")
            row = conn.execute(
                "SELECT label_payload_json FROM pending_nutrition_logs WHERE token=? AND user_id=?",
                (event[2], user_id),
            ).fetchone()
            if not row:
                raise ValueError("找不到原營養修改結果")
            label = normalize_label_payload(json.loads(row[0]))
            stored_result = json.loads(event[3] or "{}")
            conn.commit()
            replayed = {"token": event[2], "label": label, "replayed": True}
            if isinstance(stored_result.get("changes"), list):
                replayed["changes"] = stored_result["changes"]
            return replayed

        state = conn.execute(
            """SELECT s.token,s.input_type,s.expires_at,p.label_payload_json,p.status,p.expires_at
               FROM nutrition_input_states s
               JOIN pending_nutrition_logs p ON p.token=s.token AND p.user_id=s.user_id
               WHERE s.user_id=?""",
            (user_id,),
        ).fetchone()
        if not state:
            raise ValueError("找不到等待輸入的營養修改")
        token, input_type, state_expires, payload_json, status, draft_expires = state
        if _pending_is_expired(state_expires) or _pending_is_expired(draft_expires):
            conn.execute("DELETE FROM nutrition_input_states WHERE user_id=?", (user_id,))
            if _pending_is_expired(draft_expires):
                conn.execute(
                    """UPDATE pending_nutrition_logs
                       SET status='expired',label_payload_json='{}',
                           retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
                       WHERE token=? AND user_id=? AND status IN ('pending','awaiting_identity')""",
                    (datetime.now().astimezone().isoformat(timespec="seconds"), token, user_id),
                )
            conn.commit()
            raise ValueError("營養修改已逾時")
        if status not in {"pending", "awaiting_identity"}:
            raise ValueError("這筆營養紀錄已處理")
        payload = json.loads(payload_json)
        changes: list[dict[str, Any]] = []
        if input_type == "name":
            name = str(product_name or "").strip()
            if not name or len(name) > 120:
                raise ValueError("商品名稱不可空白或超過120字")
            payload["product_name"] = name
        elif input_type == "nutrient":
            requested = list(corrections) if corrections is not None else [(field, value)]
            if status != "pending" or not requested:
                raise ValueError("不支援的營養欄位或尚未補上商品名稱")
            normalized_corrections: list[tuple[str, float]] = []
            seen: set[str] = set()
            for requested_field, requested_value in requested:
                requested_field = str(requested_field or "").strip()
                if requested_field not in NUTRIENT_KEYS:
                    raise ValueError("不支援的營養欄位或尚未補上商品名稱")
                if requested_field in seen:
                    raise ValueError("同一營養欄位不可重複修改")
                seen.add(requested_field)
                normalized_corrections.append((
                    requested_field,
                    _number(
                        requested_value,
                        requested_field,
                        max_value=NUTRIENT_LIMITS[requested_field],
                    ),
                ))
            package_amount = float(payload.get("package_amount") or 0)
            servings = float(payload.get("servings_per_package") or 0)
            unit = str(payload.get("package_unit") or "").lower()
            base_amount = package_amount * 1000 if unit in {"kg", "l"} else package_amount
            serving_amount = base_amount / servings if base_amount > 0 and servings > 0 else 0
            for corrected_field, corrected in normalized_corrections:
                old_value = float((payload.get("per_serving") or {}).get(corrected_field) or 0)
                payload.setdefault("per_serving", {})[corrected_field] = corrected
                if unit in {"g", "ml", "kg", "l"} and serving_amount > 0:
                    payload.setdefault("per_100", {})[corrected_field] = round(
                        corrected * 100 / serving_amount, 4
                    )
                changes.append({"field": corrected_field, "old": old_value, "new": corrected})
        else:
            raise ValueError("不支援的營養輸入狀態")
        label = normalize_label_payload(payload)
        changed = conn.execute(
            """UPDATE pending_nutrition_logs SET label_payload_json=?,status='pending'
               WHERE token=? AND user_id=? AND status IN ('pending','awaiting_identity')""",
            (
                json.dumps(label, ensure_ascii=False, sort_keys=True, allow_nan=False),
                token,
                user_id,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("營養修改狀態衝突")
        conn.execute("DELETE FROM nutrition_input_states WHERE user_id=?", (user_id,))
        event_result = {"changes": changes} if input_type == "nutrient" else {}
        conn.execute(
            """INSERT INTO nutrition_message_events
               (message_id,user_id,event_type,token,created_at,result_json)
               VALUES (?,?, 'text_edit', ?,?,?)""",
            (
                message_id, user_id, token,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                json.dumps(event_result, ensure_ascii=False, sort_keys=True, allow_nan=False),
            ),
        )
        conn.commit()
        result = {"token": token, "label": label, "replayed": False}
        if input_type == "nutrient":
            result["changes"] = changes
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def update_pending_consumption(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    token: str,
    consumed_servings: Any = None,
    meal_slot: str | None = None,
    consumed_at: str | None = None,
    consumed_time_source: str | None = None,
) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        payload, status = _editable_pending_row(conn, user_id=user_id, token=token)
        if status != "pending":
            raise ValueError("請先補上商品名稱")
        updates = []
        params: list[Any] = []
        if consumed_servings is not None:
            updates.append("consumed_servings=?")
            params.append(
                _number(consumed_servings, "consumed_servings", allow_zero=False, max_value=100)
            )
        if meal_slot is not None:
            if meal_slot not in {"早餐", "午餐", "晚餐", "點心"}:
                raise ValueError("meal_slot 不支援")
            updates.append("meal_slot=?")
            params.append(meal_slot)
        if consumed_at is not None:
            try:
                datetime.fromisoformat(consumed_at)
            except (TypeError, ValueError) as exc:
                raise ValueError("consumed_at 格式錯誤") from exc
            updates.append("consumed_at=?")
            params.append(consumed_at)
        if consumed_time_source is not None:
            if consumed_time_source not in {"photo_timestamp", "line_timestamp", "manual"}:
                raise ValueError("consumed_time_source 不支援")
            updates.append("consumed_time_source=?")
            params.append(consumed_time_source)
        if not updates:
            row = conn.execute(
                "SELECT consumed_servings,meal_slot,consumed_at,consumed_time_source FROM pending_nutrition_logs WHERE token=? AND user_id=?",
                (token, user_id),
            ).fetchone()
            conn.commit()
            return {
                "label": normalize_label_payload(payload),
                "consumed_servings": float(row[0]),
                "meal_slot": row[1],
                "consumed_at": row[2],
                "consumed_time_source": row[3],
            }
        params.extend([token, user_id])
        changed = conn.execute(
            f"UPDATE pending_nutrition_logs SET {', '.join(updates)} WHERE token=? AND user_id=? AND status='pending'",
            params,
        ).rowcount
        if changed != 1:
            raise RuntimeError("營養份量或時間修改狀態衝突")
        row = conn.execute(
            "SELECT consumed_servings,meal_slot,consumed_at,consumed_time_source FROM pending_nutrition_logs WHERE token=? AND user_id=?",
            (token, user_id),
        ).fetchone()
        conn.commit()
        return {
            "label": normalize_label_payload(payload),
            "consumed_servings": float(row[0]),
            "meal_slot": row[1],
            "consumed_at": row[2],
            "consumed_time_source": row[3],
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def cancel_pending_label(
    conn: sqlite3.Connection, *, user_id: str, token: str
) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute(
            """UPDATE pending_nutrition_logs
               SET status='cancelled',label_payload_json='{}',
                   retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
               WHERE token=? AND user_id=? AND status IN ('pending','awaiting_identity')""",
            (datetime.now().astimezone().isoformat(timespec="seconds"), token, user_id),
        ).rowcount
        if changed != 1:
            conn.commit()
            return {"cancelled": False, "source_image_ref": ""}
        row = conn.execute(
            "SELECT source_image_ref FROM pending_nutrition_logs WHERE token=? AND user_id=?",
            (token, user_id),
        ).fetchone()
        conn.execute("DELETE FROM nutrition_input_states WHERE user_id=? AND token=?", (user_id, token))
        conn.commit()
        return {"cancelled": True, "source_image_ref": (row[0] if row else "") or ""}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def attach_pending_identity(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    token: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_identity = normalize_product_identity_payload(identity)
    if normalized_identity["confidence"] < 0.65:
        raise ValueError("商品正面辨識信心不足")
    conn.execute("BEGIN IMMEDIATE")
    try:
        payload, status = _editable_pending_row(conn, user_id=user_id, token=token)
        if status != "awaiting_identity":
            raise ValueError("這筆草稿目前不需要補商品正面")
        old_barcode = str(payload.get("barcode") or "").strip()
        new_barcode = normalized_identity["barcode"]
        if old_barcode and new_barcode and old_barcode != new_barcode:
            raise ValueError("商品正面與營養標示條碼不一致")
        payload.update(
            product_name=normalized_identity["product_name"],
            brand=normalized_identity["brand"],
            barcode=new_barcode or old_barcode,
        )
        normalized = normalize_label_payload(payload)
        changed = conn.execute(
            """UPDATE pending_nutrition_logs SET label_payload_json=?, status='pending'
               WHERE token=? AND user_id=? AND status='awaiting_identity'""",
            (
                json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False),
                token,
                user_id,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("商品照片配對狀態衝突")
        conn.commit()
        return normalized
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def update_pending_label_name(
    conn: sqlite3.Connection, *, user_id: str, token: str, product_name: str
) -> dict[str, Any]:
    name = str(product_name or "").strip()
    if not name or len(name) > 120:
        raise ValueError("商品名稱不可空白或超過120字")
    conn.execute("BEGIN IMMEDIATE")
    try:
        payload, _ = _editable_pending_row(conn, user_id=user_id, token=token)
        payload["product_name"] = name
        normalized = normalize_label_payload(payload)
        conn.execute(
            """UPDATE pending_nutrition_logs SET label_payload_json=?, status='pending'
               WHERE token=? AND user_id=? AND status IN ('pending','awaiting_identity')""",
            (
                json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False),
                token,
                user_id,
            ),
        )
        conn.commit()
        return normalized
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def update_pending_label_nutrient(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    token: str,
    field: str,
    value: Any,
) -> dict[str, Any]:
    if field not in NUTRIENT_KEYS:
        raise ValueError("不支援的營養欄位")
    corrected = _number(value, field, max_value=NUTRIENT_LIMITS[field])
    conn.execute("BEGIN IMMEDIATE")
    try:
        payload, status = _editable_pending_row(conn, user_id=user_id, token=token)
        if status != "pending":
            raise ValueError("請先補上商品名稱")
        payload.setdefault("per_serving", {})[field] = corrected
        package_amount = float(payload.get("package_amount") or 0)
        servings = float(payload.get("servings_per_package") or 0)
        unit = str(payload.get("package_unit") or "").lower()
        base_amount = package_amount * 1000 if unit in {"kg", "l"} else package_amount
        if unit in {"g", "ml", "kg", "l"} and base_amount > 0 and servings > 0:
            serving_amount = base_amount / servings
            payload.setdefault("per_100", {})[field] = round(corrected * 100 / serving_amount, 4)
        normalized = normalize_label_payload(payload)
        conn.execute(
            """UPDATE pending_nutrition_logs SET label_payload_json=?
               WHERE token=? AND user_id=? AND status='pending'""",
            (
                json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False),
                token,
                user_id,
            ),
        )
        conn.commit()
        return normalized
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _confirmed_result(conn: sqlite3.Connection, log_id: str, *, already_confirmed: bool) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT f.food_id, f.product_name, f.brand, f.source_type,
               l.log_id, l.consumed_at, l.meal_slot, l.consumed_servings,
               l.consumed_amount, l.consumed_unit, l.nutrition_snapshot_json,
               l.exchange_snapshot_json, l.approved_exchange_json,
               l.exchange_approval_id, l.plan_id,
               a.food_fingerprint, a.suggestion_rule_version,
               a.approved_exchange_json, a.approved_exchange_hash, f.fingerprint
        FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
        LEFT JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
        WHERE l.log_id=?
        """,
        (log_id,),
    ).fetchone()
    if not row:
        raise ValueError("已確認紀錄遺失，請聯繫客服")
    suggested = json.loads(row[11] or "{}")
    approved = json.loads(row[12] or "{}")
    applied: dict[str, Any] = {}
    candidate_approval_id = row[13] or ""
    approval_id = ""
    if candidate_approval_id and approved and row[15] and row[15] == row[19]:
        approved_definition = json.loads(row[17] or "{}")
        expected_hash = exchange_approval_hash(row[15], row[16], approved_definition)
        expected_applied = {
            key: round(float(approved_definition.get(key, 0) or 0) * float(row[7] or 0), 4)
            for key in EXCHANGE_KEYS
        }
        if secrets.compare_digest(str(row[18] or ""), expected_hash) and all(
            abs(float(approved.get(key, 0) or 0) - value) <= 0.0001
            for key, value in expected_applied.items()
        ):
            applied = approved
            approval_id = candidate_approval_id
        else:
            approval_id = ""
    return {
        "already_confirmed": already_confirmed,
        "food": {"food_id": row[0], "product_name": row[1], "brand": row[2], "source_type": row[3]},
        "log": {
            "log_id": row[4], "consumed_at": row[5], "meal_slot": row[6],
            "consumed_servings": float(row[7]), "consumed_amount": float(row[8]),
            "consumed_unit": row[9], "nutrition": json.loads(row[10] or "{}"),
            "suggested_exchange": suggested,
            "approved_exchange": applied,
            "exchange": applied or suggested,
            "exchange_review_status": "approved" if applied else "pending_review",
            "exchange_approval_id": approval_id,
            "plan_id": row[14] or "",
        },
    }


def confirm_pending_label(
    conn: sqlite3.Connection,
    *,
    token: str,
    user_id: str,
    plan_id: str = "",
    plan_link_status: str = "pending",
) -> dict[str, Any]:
    if plan_link_status not in {"pending", "linked", "no_plan"}:
        raise ValueError("plan_link_status 不支援")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT label_payload_json, source_image_ref, consumed_servings, meal_slot,
                   consumed_at, status, confirmed_log_id, expires_at
            FROM pending_nutrition_logs WHERE token=? AND user_id=?
            """,
            (token, user_id),
        ).fetchone()
        if not row:
            raise ValueError("找不到待確認的營養紀錄")
        payload_json, source_image_ref, servings, meal_slot, consumed_at, status, confirmed_log_id, expires_at = row
        if status == "confirmed" and confirmed_log_id:
            result = _confirmed_result(conn, confirmed_log_id, already_confirmed=True)
            conn.commit()
            return result
        if status != "pending":
            raise ValueError("這筆營養紀錄已處理")
        now = utcish_now()
        if _pending_is_expired(expires_at):
            conn.execute(
                """UPDATE pending_nutrition_logs
                   SET status='expired',label_payload_json='{}',retired_at=?
                   WHERE token=? AND user_id=? AND status='pending'""",
                (now, token, user_id),
            )
            conn.commit()
            raise ValueError("這筆確認已逾時，請重新上傳營養標示")

        label = normalize_label_payload(json.loads(payload_json))
        warning_fields = nutrition_consistency_warnings(label)
        if warning_fields:
            names = "、".join(NUTRIENT_LABELS[key] for key in warning_fields)
            raise ValueError(f"{names}的每份與每100單位換算不一致，請先修正營養數字")
        per_serving_suggestion = suggest_exchange_portions(
            product_name=label["product_name"], nutrition=label["per_serving"]
        )
        suggested_food_exchange = per_serving_suggestion["exchanges"]
        suggested_food_payload = {
            **suggested_food_exchange,
            "_review_status": per_serving_suggestion["review_status"],
            "_rule_version": per_serving_suggestion["rule_version"],
            "_categories": per_serving_suggestion["categories"],
            "_warnings": per_serving_suggestion["warnings"],
        }
        suggested_food_exchange_json = json.dumps(
            suggested_food_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        fingerprint = food_fingerprint(
            label["product_name"], label["brand"], label["package_amount"],
            label["package_unit"], label["per_serving"], barcode=label["barcode"],
            servings_per_package=label["servings_per_package"], per_100=label["per_100"],
        )
        existing = conn.execute(
            """
            SELECT f.food_id,f.exchange_json,f.exchange_review_status,
                   a.approval_id,a.food_fingerprint,a.suggestion_rule_version,
                   a.approved_exchange_json,a.approved_exchange_hash
            FROM food_catalog f
            LEFT JOIN food_exchange_approvals a ON a.food_id=f.food_id
            WHERE f.fingerprint=? AND (f.owner_user_id=? OR f.visibility='public')
            ORDER BY CASE WHEN f.owner_user_id=? THEN 0 ELSE 1 END, a.approved_at DESC LIMIT 1
            """,
            (fingerprint, user_id, user_id),
        ).fetchone()
        approved_food_payload: dict[str, Any] = {}
        approval_id = ""
        approved_valid = False
        if existing:
            food_id = existing[0]
            if existing[2] == "approved" and existing[3] and existing[4] == fingerprint:
                candidate = json.loads(existing[6] or "{}")
                expected_hash = exchange_approval_hash(fingerprint, existing[5], candidate)
                if secrets.compare_digest(str(existing[7] or ""), expected_hash):
                    approved_food_payload = candidate
                    approval_id = existing[3]
                    approved_valid = True
            if not approved_valid:
                conn.execute(
                    """UPDATE food_catalog
                       SET exchange_json=?,exchange_review_status='pending_review',updated_at=?
                       WHERE food_id=?""",
                    (suggested_food_exchange_json, now, food_id),
                )
        else:
            food_id = new_id("food")
            conn.execute(
                """
                INSERT INTO food_catalog
                (food_id, product_name, brand, barcode, source_type, owner_user_id,
                 visibility, package_amount, package_unit, servings_per_package,
                 per_serving_json, per_100_json, exchange_json, exchange_review_status,
                 fingerprint, original_image_ref, recognition_confidence,
                 verification_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'user_private_food', ?, 'private', ?, ?, ?, ?, ?, ?,
                        'pending_review', ?, ?, ?, 'user_confirmed', ?, ?)
                """,
                (
                    food_id, label["product_name"], label["brand"], label["barcode"], user_id,
                    label["package_amount"], label["package_unit"], label["servings_per_package"],
                    json.dumps(label["per_serving"], ensure_ascii=False, sort_keys=True, allow_nan=False),
                    json.dumps(label["per_100"], ensure_ascii=False, sort_keys=True, allow_nan=False),
                    suggested_food_exchange_json, fingerprint, source_image_ref, label["confidence"], now, now,
                ),
            )

        nutrition = scale_nutrition(label["per_serving"], servings)
        log_suggestion = suggest_exchange_portions(
            product_name=label["product_name"], nutrition=nutrition
        )
        log_suggestion_payload = {
            **log_suggestion["exchanges"],
            "_review_status": "pending_review",
            "_rule_version": log_suggestion["rule_version"],
            "_categories": log_suggestion["categories"],
            "_warnings": log_suggestion["warnings"],
        }
        log_exchange_json = json.dumps(
            log_suggestion_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        applied_payload: dict[str, Any] = {}
        if approved_valid:
            applied_payload = {
                key: round(float(approved_food_payload.get(key, 0) or 0) * float(servings), 4)
                for key in EXCHANGE_KEYS
            }
            applied_payload.update(
                _review_status="approved",
                _rule_version=existing[5],
                _categories=approved_food_payload.get("_categories", []),
                _approval_id=approval_id,
            )
        applied_exchange_json = json.dumps(
            applied_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        consumed_amount = round(label["package_amount"] * float(servings) / label["servings_per_package"], 4)
        log_id = new_id("log")
        conn.execute(
            """
            INSERT INTO food_logs
            (log_id, user_id, food_id, consumed_at, meal_slot, consumed_servings,
             consumed_amount, consumed_unit, nutrition_snapshot_json,
             exchange_snapshot_json, approved_exchange_json, exchange_approval_id,
             source_image_ref, plan_id, plan_link_status,
             confirmation_status, legacy_applied_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', '', ?, ?)
            """,
            (
                log_id, user_id, food_id, consumed_at or now, meal_slot, float(servings),
                consumed_amount, label["package_unit"],
                json.dumps(nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False),
                log_exchange_json, applied_exchange_json, approval_id,
                source_image_ref, str(plan_id or "").strip(), plan_link_status, now, now,
            ),
        )
        changed = conn.execute(
            "UPDATE pending_nutrition_logs SET status='confirmed', confirmed_log_id=? WHERE token=? AND status='pending'",
            (log_id, token),
        ).rowcount
        if changed != 1:
            raise RuntimeError("確認狀態衝突")
        for entity_type, entity_id in (("food", food_id), ("food_log", log_id)):
            conn.execute(
                """INSERT OR IGNORE INTO nutrition_sheet_outbox
                   (outbox_id, entity_type, entity_id, status, attempts, last_error, created_at, synced_at)
                   VALUES (?, ?, ?, 'pending', 0, '', ?, '')""",
                (new_id("outbox"), entity_type, entity_id, now),
            )
        conn.commit()
        return _confirmed_result(conn, log_id, already_confirmed=False)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def insert_approved_meal_photo_log(
    conn: sqlite3.Connection,
    *,
    token: str,
    user_id: str,
    reviewer: str,
    consumed_at: str,
    meal_slot: str,
    source_image_ref: str,
    observed_payload: Mapping[str, Any],
    answers: Mapping[str, Any],
    exact_exchange: Mapping[str, Any],
) -> dict[str, Any]:
    """在呼叫端交易內建立照片餐點的正式核准、食品與飲食快照；不自行commit。"""
    token = str(token or "").strip()
    user_id = str(user_id or "").strip()
    reviewer = str(reviewer or "").strip()
    if len(token) != 12 or any(ch not in "0123456789abcdef" for ch in token):
        raise ValueError("餐點照片token無效")
    if not user_id or len(user_id) > 120 or not reviewer or len(reviewer) > 120:
        raise ValueError("餐點照片核准身分無效")
    approved_at = utcish_now()
    values = {
        key: round(_number(exact_exchange.get(key), key, max_value=100), 4)
        for key in EXCHANGE_KEYS
    }
    if values["fat_exchange"] != 0:
        raise ValueError("目前規則不計油脂交換份")
    estimated_nutrition = estimate_nutrition_from_exchanges(values)
    estimated_nutrition_json = json.dumps(
        estimated_nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    categories = [key for key in EXCHANGE_KEYS if values[key] > 0]
    rule_version = "meal-photo-admin-v1"
    approved_payload: dict[str, Any] = {
        **values,
        "_review_status": "approved",
        "_rule_version": rule_version,
        "_categories": categories,
        "_warnings": list(estimated_nutrition["_warnings"]),
        "_approved_by": reviewer,
        "_approved_at": approved_at,
        "_source_type": "meal_photo",
    }
    approved_json = json.dumps(
        approved_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    identity = json.dumps(
        {
            "source_type": "meal_photo", "token": token, "user_id": user_id,
            "observed_payload": dict(observed_payload or {}),
            "answers": dict(answers or {}),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    visible_names = [
        str(item.get("name") or "").strip()
        for item in list((observed_payload or {}).get("visible_items") or [])[:4]
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    ]
    product_name = "餐點照片" + ("：" + "、".join(visible_names) if visible_names else "")
    food_id = new_id("food")
    approval_id = new_id("approval")
    log_id = new_id("log")
    approval_hash = exchange_approval_hash(fingerprint, rule_version, approved_payload)
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
            exchange_json,exchange_review_status,fingerprint,original_image_ref,
            recognition_confidence,verification_status,created_at,updated_at)
           VALUES (?,?, '', '', 'user_meal_photo',?,'private',1,'meal',1,?,'{}',
                   ?,'approved',?,?,0,'admin_approved',?,?)""",
        (
            food_id, product_name[:160], user_id, estimated_nutrition_json,
            approved_json, fingerprint,
            str(source_image_ref or "")[:240], approved_at, approved_at,
        ),
    )
    conn.execute(
        """INSERT INTO food_exchange_approvals
           (approval_id,food_id,food_fingerprint,suggestion_rule_version,
            approved_exchange_json,approved_exchange_hash,reviewer,approved_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            approval_id, food_id, fingerprint, rule_version, approved_json,
            approval_hash, reviewer, approved_at,
        ),
    )
    applied_payload = {
        **approved_payload,
        "_approval_id": approval_id,
    }
    conn.execute(
        """INSERT INTO food_logs
           (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,
            consumed_amount,consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,
            approved_exchange_json,exchange_approval_id,source_image_ref,plan_id,
            plan_link_status,confirmation_status,legacy_applied_at,created_at,updated_at)
           VALUES (?,?,?,?,?,1,1,'meal',?,?,?,?,?, '', 'pending','confirmed',
                   'not_applicable',?,?)""",
        (
            log_id, user_id, food_id, str(consumed_at or approved_at)[:50],
            str(meal_slot or "")[:30], estimated_nutrition_json, approved_json,
            json.dumps(applied_payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
            approval_id, str(source_image_ref or "")[:240], approved_at, approved_at,
        ),
    )
    for entity_type, entity_id in (("food", food_id), ("food_log", log_id)):
        conn.execute(
            """INSERT OR IGNORE INTO nutrition_sheet_outbox
               (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
               VALUES (?,?,?,'pending',0,'',?,'')""",
            (new_id("outbox"), entity_type, entity_id, approved_at),
        )
    return {
        "food_id": food_id, "approval_id": approval_id, "log_id": log_id,
        "product_name": product_name[:160], "approved_exchange": approved_payload,
        "estimated_nutrition": estimated_nutrition,
    }


def quick_log_from_catalog(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    food_id: str,
    consumed_servings: float,
    meal_slot: str,
    consumed_at: str = "",
    manage_transaction: bool = True,
) -> dict[str, Any]:
    """從已有的food_catalog item快速建立一筆food_log，不經過pending流程。"""
    user_id = str(user_id or "").strip()
    food_id = str(food_id or "").strip()
    if not user_id or not food_id:
        raise ValueError("快速記錄缺少必要資訊")
    try:
        servings = float(consumed_servings)
    except (TypeError, ValueError) as exc:
        raise ValueError("份量數值無效") from exc
    if servings <= 0 or servings > 100:
        raise ValueError("份量需介於0.1~100")
    meal_slot = str(meal_slot or "").strip()
    if meal_slot not in {"早餐", "午餐", "晚餐", "點心", ""}:
        raise ValueError("餐別不支援")
    if manage_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        food = conn.execute(
            """SELECT product_name,brand,package_amount,package_unit,servings_per_package,
                      per_serving_json,exchange_json,exchange_review_status,
                      source_type,fingerprint
               FROM food_catalog
               WHERE food_id=? AND (owner_user_id=? OR visibility='public')""",
            (food_id, user_id),
        ).fetchone()
        if not food:
            raise ValueError("找不到這筆食品紀錄")
        product_name = food[0]
        package_amount = float(food[2] or 0)
        package_unit = food[3] or ""
        servings_per_package = float(food[4] or 1)
        per_serving = json.loads(food[5] or "{}")
        exchange_json_raw = json.loads(food[6] or "{}")
        source_type = food[8] or ""
        now = utcish_now()
        consumed_at = str(consumed_at or now)[:50]
        if per_serving and source_type != "user_meal_photo":
            nutrition = scale_nutrition(per_serving, servings)
            consumed_amount = round(package_amount * servings / servings_per_package, 4)
        else:
            nutrition = {}
            consumed_amount = 0
        if source_type == "user_meal_photo":
            applied = {
                key: round(float(exchange_json_raw.get(key, 0) or 0) * servings, 4)
                for key in EXCHANGE_KEYS
            }
            applied["_review_status"] = "approved"
            applied["_source_type"] = "meal_photo"
            applied["_warnings"] = ["calories_and_macros_na"]
        else:
            applied = {
                key: round(float(exchange_json_raw.get(key, 0) or 0) * servings, 4)
                for key in EXCHANGE_KEYS
            }
            applied["_review_status"] = exchange_json_raw.get("_review_status", "pending_review")
        log_id = new_id("log")
        conn.execute(
            """INSERT INTO food_logs
               (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,
                consumed_amount,consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,
                approved_exchange_json,exchange_approval_id,source_image_ref,plan_id,
                plan_link_status,confirmation_status,legacy_applied_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                log_id, user_id, food_id, consumed_at, meal_slot, servings,
                consumed_amount, package_unit,
                json.dumps(nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False),
                json.dumps(exchange_json_raw, ensure_ascii=False, sort_keys=True, allow_nan=False),
                json.dumps(applied, ensure_ascii=False, sort_keys=True, allow_nan=False),
                "", "", "", "pending", "confirmed", "not_applicable", now, now,
            ),
        )
        conn.execute(
            """INSERT OR IGNORE INTO nutrition_sheet_outbox
               (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
               VALUES (?,'food_log',?,'pending',0,'',?,'')""",
            (new_id("outbox"), log_id, now),
        )
        if manage_transaction:
            conn.commit()
        return {
            "log_id": log_id, "product_name": product_name,
            "consumed_servings": servings, "meal_slot": meal_slot,
            "consumed_at": consumed_at, "consumed_amount": consumed_amount,
            "consumed_unit": package_unit,
            "nutrition": nutrition, "applied_exchange": applied,
        }
    except Exception:
        if manage_transaction and conn.in_transaction:
            conn.rollback()
        raise


def search_food_catalog(
    conn: sqlite3.Connection, *, user_id: str, query: str = "", limit: int = 8
) -> list[dict[str, Any]]:
    """模糊搜尋用戶的food_catalog，包含包裝食品與餐點照片synthetic food。query為空時回傳全部。"""
    user_id = str(user_id or "").strip()
    query = str(query or "").strip()
    if not user_id:
        return []
    limit = max(1, min(int(limit or 8), 20))
    if query:
        pattern = f"%{query}%"
        rows = conn.execute(
            """SELECT food_id,product_name,brand,barcode,source_type,owner_user_id,
                      package_amount,package_unit,servings_per_package,
                      per_serving_json,exchange_json,exchange_review_status,
                      created_at,updated_at
               FROM food_catalog
               WHERE (owner_user_id=? OR visibility='public') AND product_name LIKE ?
               ORDER BY updated_at DESC
               LIMIT ?""",
            (user_id, pattern, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT food_id,product_name,brand,barcode,source_type,owner_user_id,
                      package_amount,package_unit,servings_per_package,
                      per_serving_json,exchange_json,exchange_review_status,
                      created_at,updated_at
               FROM food_catalog
               WHERE (owner_user_id=? OR visibility='public')
               ORDER BY updated_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [
        {
            "food_id": r[0], "product_name": r[1], "brand": r[2] or "", "barcode": r[3] or "",
            "source_type": r[4], "owner_user_id": r[5],
            "package_amount": float(r[6] or 0), "package_unit": r[7] or "",
            "servings_per_package": float(r[8] or 1),
            "per_serving": json.loads(r[9] or "{}"),
            "exchange": json.loads(r[10] or "{}"),
            "exchange_review_status": r[11] or "",
            "created_at": r[12] or "", "updated_at": r[13] or "",
            "last_consumed_at": None, "use_count": 0,
        }
        for r in rows
    ]


def search_food_history(
    conn: sqlite3.Connection, *, user_id: str, query: str, limit: int = 8
) -> list[dict[str, Any]]:
    """搜尋用戶過去的飲食紀錄，join food_catalog找出最常吃與最近吃的。"""
    user_id = str(user_id or "").strip()
    query = str(query or "").strip()
    if not user_id or not query:
        return []
    limit = max(1, min(int(limit or 8), 20))
    pattern = f"%{query}%"
    rows = conn.execute(
        """SELECT f.food_id,f.product_name,f.brand,f.barcode,f.source_type,f.owner_user_id,
                  f.package_amount,f.package_unit,f.servings_per_package,
                  f.per_serving_json,f.exchange_json,f.exchange_review_status,
                  MAX(l.consumed_at) AS last_consumed_at,
                  COUNT(l.log_id) AS use_count
           FROM food_logs l
           JOIN food_catalog f ON f.food_id=l.food_id
           WHERE l.user_id=? AND l.confirmation_status='confirmed'
             AND f.product_name LIKE ?
           GROUP BY f.food_id
           ORDER BY use_count DESC, last_consumed_at DESC
           LIMIT ?""",
        (user_id, pattern, limit),
    ).fetchall()
    return [
        {
            "food_id": r[0], "product_name": r[1], "brand": r[2] or "", "barcode": r[3] or "",
            "source_type": r[4], "owner_user_id": r[5],
            "package_amount": float(r[6] or 0), "package_unit": r[7] or "",
            "servings_per_package": float(r[8] or 1),
            "per_serving": json.loads(r[9] or "{}"),
            "exchange": json.loads(r[10] or "{}"),
            "exchange_review_status": r[11] or "",
            "last_consumed_at": r[12] or None,
            "use_count": int(r[13] or 0),
        }
        for r in rows
    ]


def search_food_page(
    conn: sqlite3.Connection, *, user_id: str, query: str,
    limit: int = 11, offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """以單一SQL分頁搜尋，歷史常吃優先；多取一筆判斷下一頁。"""
    user_id = str(user_id or "").strip()
    query = str(query or "").strip()
    if not user_id or not query:
        return [], False
    limit = max(1, min(int(limit or 11), 50))
    offset = max(0, min(int(offset or 0), 10000))
    rows = conn.execute(
        """SELECT f.food_id,f.product_name,f.brand,f.barcode,f.source_type,f.owner_user_id,
                  f.package_amount,f.package_unit,f.servings_per_package,
                  f.per_serving_json,f.exchange_json,f.exchange_review_status,
                  f.created_at,f.updated_at,
                  MAX(l.consumed_at) AS last_consumed_at,
                  COUNT(l.log_id) AS use_count
           FROM food_catalog f
           LEFT JOIN food_logs l
             ON l.food_id=f.food_id AND l.user_id=?
            AND l.confirmation_status='confirmed'
           WHERE (f.owner_user_id=? OR f.visibility='public')
             AND f.product_name LIKE ?
           GROUP BY f.food_id
           ORDER BY CASE WHEN COUNT(l.log_id)>0 THEN 0 ELSE 1 END,
                    use_count DESC,last_consumed_at DESC,f.updated_at DESC,f.food_id
           LIMIT ? OFFSET ?""",
        (user_id, user_id, f"%{query}%", limit + 1, offset),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return [
        {
            "food_id": r[0], "product_name": r[1], "brand": r[2] or "", "barcode": r[3] or "",
            "source_type": r[4], "owner_user_id": r[5],
            "package_amount": float(r[6] or 0), "package_unit": r[7] or "",
            "servings_per_package": float(r[8] or 1),
            "per_serving": json.loads(r[9] or "{}"),
            "exchange": json.loads(r[10] or "{}"),
            "exchange_review_status": r[11] or "",
            "created_at": r[12] or "", "updated_at": r[13] or "",
            "last_consumed_at": r[14] or None, "use_count": int(r[15] or 0),
        }
        for r in rows
    ], has_more


def approve_food_exchange_suggestion(
    conn: sqlite3.Connection, *, food_id: str, reviewer: str
) -> dict[str, Any]:
    food_id = str(food_id or "").strip()
    reviewer = str(reviewer or "").strip()
    if not food_id or len(food_id) > 80:
        raise ValueError("food_id 無效")
    if not reviewer or len(reviewer) > 120:
        raise ValueError("reviewer 無效")

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT product_name,exchange_json,exchange_review_status,fingerprint
               FROM food_catalog WHERE food_id=?""",
            (food_id,),
        ).fetchone()
        if not row:
            raise ValueError("找不到待審核食品")
        product_name, exchange_json, review_status, fingerprint = row
        current_payload = json.loads(exchange_json or "{}")
        if review_status == "approved":
            approval = conn.execute(
                """SELECT approval_id,suggestion_rule_version,approved_exchange_json,
                          approved_exchange_hash,food_fingerprint
                   FROM food_exchange_approvals WHERE food_id=?
                   ORDER BY approved_at DESC LIMIT 1""",
                (food_id,),
            ).fetchone()
            if not approval:
                raise ValueError("核准紀錄遺失，已停止套用")
            approved_payload = json.loads(approval[2] or "{}")
            expected_hash = exchange_approval_hash(approval[4], approval[1], approved_payload)
            if approval[4] != fingerprint or not secrets.compare_digest(approval[3], expected_hash):
                raise ValueError("核准紀錄驗證失敗，已停止套用")
            conn.commit()
            return {
                "food_id": food_id, "product_name": product_name,
                "exchange": approved_payload, "updated_logs": 0,
                "already_approved": True,
            }
        if review_status != "pending_review":
            raise ValueError("這筆食品目前不可核准")

        approved_at = utcish_now()
        rule_version = str(current_payload.get("_rule_version") or EXCHANGE_RULE_VERSION)
        approved_payload: dict[str, Any] = {
            key: round(_number(current_payload.get(key, 0), key, max_value=10000), 4)
            for key in EXCHANGE_KEYS
        }
        approved_payload.update(
            _review_status="approved",
            _rule_version=rule_version,
            _categories=current_payload.get("_categories", []),
            _warnings=current_payload.get("_warnings", []),
            _approved_by=reviewer,
            _approved_at=approved_at,
        )
        approved_json = json.dumps(
            approved_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        approval_id = new_id("approval")
        approval_hash = exchange_approval_hash(fingerprint, rule_version, approved_payload)
        conn.execute(
            """INSERT INTO food_exchange_approvals
               (approval_id,food_id,food_fingerprint,suggestion_rule_version,
                approved_exchange_json,approved_exchange_hash,reviewer,approved_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                approval_id, food_id, fingerprint, rule_version, approved_json,
                approval_hash, reviewer, approved_at,
            ),
        )
        changed = conn.execute(
            """UPDATE food_catalog
               SET exchange_review_status='approved',updated_at=?
               WHERE food_id=? AND exchange_review_status='pending_review'""",
            (approved_at, food_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("營養份數核准狀態衝突")

        updated_log_ids = []
        logs = conn.execute(
            "SELECT log_id,consumed_servings FROM food_logs WHERE food_id=?",
            (food_id,),
        ).fetchall()
        for log_id, consumed_servings in logs:
            applied: dict[str, Any] = {
                key: round(approved_payload[key] * float(consumed_servings or 0), 4)
                for key in EXCHANGE_KEYS
            }
            applied.update(
                _review_status="approved",
                _rule_version=rule_version,
                _categories=approved_payload.get("_categories", []),
                _approval_id=approval_id,
                _approved_by=reviewer,
                _approved_at=approved_at,
            )
            conn.execute(
                """UPDATE food_logs
                   SET approved_exchange_json=?,exchange_approval_id=?,updated_at=?
                   WHERE log_id=?""",
                (
                    json.dumps(applied, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    approval_id, approved_at, log_id,
                ),
            )
            updated_log_ids.append(log_id)

        entities = [("food", food_id), *[("food_log", value) for value in updated_log_ids]]
        for entity_type, entity_id in entities:
            outbox = conn.execute(
                "SELECT status FROM nutrition_sheet_outbox WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id),
            ).fetchone()
            if not outbox:
                conn.execute(
                    """INSERT INTO nutrition_sheet_outbox
                       (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
                       VALUES (?,?,?,'pending',0,'',?,'')""",
                    (new_id("outbox"), entity_type, entity_id, approved_at),
                )
            elif outbox[0] == "processing":
                conn.execute(
                    """UPDATE nutrition_sheet_outbox SET resync_required=1
                       WHERE entity_type=? AND entity_id=?""",
                    (entity_type, entity_id),
                )
            else:
                conn.execute(
                    """UPDATE nutrition_sheet_outbox
                       SET status='pending',last_error='',synced_at='',resync_required=0
                       WHERE entity_type=? AND entity_id=?""",
                    (entity_type, entity_id),
                )
        conn.commit()
        return {
            "food_id": food_id, "product_name": product_name,
            "exchange": approved_payload, "updated_logs": len(updated_log_ids),
            "already_approved": False,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise



def daily_consumed_totals(
    conn: sqlite3.Connection, *, user_id: str, date_iso: str, meal_slot: str = ""
) -> dict[str, float]:
    totals = {key: 0.0 for key in (*NUTRIENT_KEYS, *EXCHANGE_KEYS)}
    sql = """
        SELECT l.nutrition_snapshot_json,l.approved_exchange_json,l.exchange_approval_id,
               l.consumed_servings,
               a.food_fingerprint,a.suggestion_rule_version,a.approved_exchange_json,
               a.approved_exchange_hash,f.fingerprint,f.source_type
        FROM food_logs l
        JOIN food_catalog f ON f.food_id=l.food_id
        LEFT JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
        WHERE l.user_id=? AND date(l.consumed_at, '+8 hours')=? AND l.confirmation_status='confirmed'
    """
    params: list[Any] = [user_id, date_iso]
    if meal_slot:
        sql += " AND l.meal_slot=?"
        params.append(meal_slot)
    rows = conn.execute(sql, params).fetchall()
    for nutrition_json, applied_json, approval_id, consumed_servings, approval_fingerprint, rule_version, approved_json, approval_hash, food_fp, source_type in rows:
        nutrition_data = json.loads(nutrition_json or "{}")
        if source_type != "user_meal_photo":
            for key, value in nutrition_data.items():
                if key in totals:
                    totals[key] += float(value or 0)
        if not approval_id or not approval_fingerprint or approval_fingerprint != food_fp:
            continue
        approved_data = json.loads(approved_json or "{}")
        expected_hash = exchange_approval_hash(approval_fingerprint, rule_version, approved_data)
        if not secrets.compare_digest(str(approval_hash or ""), expected_hash):
            continue
        applied_data = json.loads(applied_json or "{}")
        expected_applied = {
            key: round(float(approved_data.get(key, 0) or 0) * float(consumed_servings or 0), 4)
            for key in EXCHANGE_KEYS
        }
        if any(
            abs(float(applied_data.get(key, 0) or 0) - expected_applied[key]) > 0.0001
            for key in EXCHANGE_KEYS
        ):
            continue
        for key, value in expected_applied.items():
            totals[key] += value
        if source_type == "user_meal_photo":
            for key, value in nutrition_data.items():
                if key in totals:
                    totals[key] += float(value or 0)
    return {key: round(value, 4) for key, value in totals.items()}


def daily_food_summary(
    conn: sqlite3.Connection, *, user_id: str, date_iso: str
) -> dict[str, Any]:
    """Return confirmed food details plus verified totals for one local consumption date."""
    rows = conn.execute(
        """
        SELECT l.consumed_at,f.product_name,l.nutrition_snapshot_json,
               l.exchange_approval_id,a.food_fingerprint,a.suggestion_rule_version,
               a.approved_exchange_json,a.approved_exchange_hash,f.fingerprint,
               l.approved_exchange_json,l.consumed_servings
        FROM food_logs l
        JOIN food_catalog f ON f.food_id=l.food_id
        LEFT JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
        WHERE l.user_id=? AND substr(l.consumed_at,1,10)=?
          AND l.confirmation_status='confirmed'
        ORDER BY l.consumed_at,l.log_id
        """,
        (user_id, date_iso),
    ).fetchall()
    foods: list[dict[str, Any]] = []
    pending_reviews = 0
    for (
        consumed_at, product_name, nutrition_json, approval_id, approval_fingerprint,
        rule_version, approved_json, approval_hash, food_fingerprint_value,
        applied_json, consumed_servings,
    ) in rows:
        nutrition = json.loads(nutrition_json or "{}")
        try:
            consumed_time = datetime.fromisoformat(str(consumed_at)).strftime("%H:%M")
        except ValueError:
            consumed_time = str(consumed_at)[11:16] if len(str(consumed_at)) >= 16 else "--:--"
        foods.append(
            {
                "time": consumed_time,
                "consumed_at": consumed_at,
                "name": product_name,
                "calories_kcal": float(nutrition.get("calories_kcal", 0) or 0),
                "protein_g": float(nutrition.get("protein_g", 0) or 0),
            }
        )
        valid_approval = False
        if approval_id and approval_fingerprint and approval_fingerprint == food_fingerprint_value:
            approved_data = json.loads(approved_json or "{}")
            expected_hash = exchange_approval_hash(
                approval_fingerprint, rule_version, approved_data
            )
            expected_applied = {
                key: round(
                    float(approved_data.get(key, 0) or 0) * float(consumed_servings or 0), 4
                )
                for key in EXCHANGE_KEYS
            }
            applied_data = json.loads(applied_json or "{}")
            valid_approval = secrets.compare_digest(
                str(approval_hash or ""), expected_hash
            ) and all(
                abs(float(applied_data.get(key, 0) or 0) - value) <= 0.0001
                for key, value in expected_applied.items()
            )
        if not valid_approval:
            pending_reviews += 1
    return {
        "foods": foods,
        "totals": daily_consumed_totals(conn, user_id=user_id, date_iso=date_iso),
        "pending_reviews": pending_reviews,
    }


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcish_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
