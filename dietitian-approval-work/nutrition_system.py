"""一日樂食營養辨識、食品資料庫與配餐推薦的純資料層。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence


_TAIPEI_LOCAL_CONSUMED_AT_SQL = """
CASE
  WHEN upper(substr(l.consumed_at, -1)) = 'Z'
    OR substr(l.consumed_at, 12) GLOB '*[-+][0-9][0-9]:[0-9][0-9]'
  THEN datetime(l.consumed_at, '+8 hours')
  ELSE datetime(l.consumed_at)
END
""".strip()


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


def _strict_json_number(
    value: Any, field: str, *, max_value: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必須是JSON數字")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} 必須是有限數字")
    if result < 0:
        raise ValueError(f"{field} 不可為負數")
    if max_value is not None and result > max_value:
        raise ValueError(f"{field} 超過合理範圍")
    return result


def _json_object_or_none(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


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
    def canonical_number(value: Any) -> float | str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "__invalid__"
        number = float(value)
        if not math.isfinite(number):
            return "__invalid__"
        return round(number, 4)

    def canonical_string(container: Mapping[str, Any], key: str) -> Any:
        if key not in container:
            return {"_absent": True}
        value = container[key]
        return value if isinstance(value, str) else {"_invalid": True}

    def canonical_legacy_number(value: Any) -> float | str:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, type(None))):
            return "__invalid__"
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return "__invalid__"
        if not math.isfinite(number):
            return "__invalid__"
        return round(number, 4)

    rule_version = str(rule_version)
    if not isinstance(exchanges, Mapping):
        canonical = {
            "food_fingerprint": str(food_fingerprint_value),
            "rule_version": rule_version,
            "exchanges": {"_invalid": True},
        }
        raw = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if rule_version == "meal-photo-admin-v2":
        canonical_exchanges = {
            key: canonical_number(exchanges[key]) if key in exchanges else "__missing__"
            for key in EXCHANGE_KEYS
        }
    else:
        canonical_exchanges = {
            key: canonical_legacy_number(exchanges.get(key, 0)) for key in EXCHANGE_KEYS
        }
    canonical = {
        "food_fingerprint": str(food_fingerprint_value),
        "rule_version": rule_version,
        "exchanges": canonical_exchanges,
    }
    if rule_version == "meal-photo-admin-v2":
        raw_items = exchanges.get("protein_items")
        if "protein_items" not in exchanges:
            canonical["protein_items"] = {"_absent": True}
        elif raw_items is None:
            canonical["protein_items"] = {"_invalid": True}
        elif not isinstance(raw_items, list):
            canonical["protein_items"] = {"_invalid": True}
        else:
            canonical_items = []
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    canonical_items.append({"_invalid": True})
                    continue
                if "exchange" not in raw_item:
                    canonical_exchange: Any = {"_absent": True}
                else:
                    raw_exchange = raw_item["exchange"]
                    if not isinstance(raw_exchange, Mapping):
                        canonical_exchange = {"_invalid": True}
                    else:
                        canonical_exchange = {
                            "min": (
                                canonical_number(raw_exchange["min"])
                                if "min" in raw_exchange else {"_absent": True}
                            ),
                            "max": (
                                canonical_number(raw_exchange["max"])
                                if "max" in raw_exchange else {"_absent": True}
                            ),
                            "basis": canonical_string(raw_exchange, "basis"),
                        }
                canonical_items.append({
                    "type": canonical_string(raw_item, "type"),
                    "portion": canonical_string(raw_item, "portion"),
                    "exchange": canonical_exchange,
                })
            canonical["protein_items"] = canonical_items
        raw_total = exchanges.get("protein_total_exchange")
        if "protein_total_exchange" not in exchanges:
            canonical["protein_total_exchange"] = {"_absent": True}
        elif raw_total is None:
            canonical["protein_total_exchange"] = {"_invalid": True}
        elif not isinstance(raw_total, Mapping):
            canonical["protein_total_exchange"] = {"_invalid": True}
        else:
            canonical["protein_total_exchange"] = {
                "min": (
                    canonical_number(raw_total["min"])
                    if "min" in raw_total else {"_absent": True}
                ),
                "max": (
                    canonical_number(raw_total["max"])
                    if "max" in raw_total else {"_absent": True}
                ),
                "basis": canonical_string(raw_total, "basis"),
            }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def exchange_approval_payload_is_valid(rule_version: str, exchanges: Any) -> bool:
    if not isinstance(exchanges, Mapping):
        return False
    if str(rule_version) != "meal-photo-admin-v2":
        try:
            for key in EXCHANGE_KEYS:
                value = exchanges.get(key, 0)
                if isinstance(value, bool) or not isinstance(
                    value, (int, float, str, type(None))
                ):
                    return False
                number = float(value or 0)
                if not math.isfinite(number) or number < 0 or number > 100:
                    return False
            return True
        except (TypeError, ValueError):
            return False
    try:
        for key in EXCHANGE_KEYS:
            if key not in exchanges:
                return False
            _strict_json_number(exchanges[key], key, max_value=100)
        has_items = "protein_items" in exchanges
        has_total = "protein_total_exchange" in exchanges
        if has_items != has_total:
            return False
        if not has_items:
            return True
        raw_items = exchanges["protein_items"]
        raw_total = exchanges["protein_total_exchange"]
        if (
            not isinstance(raw_items, list)
            or not 2 <= len(raw_items) <= 4
            or not isinstance(raw_total, Mapping)
        ):
            return False
        valid_types = {"chicken", "pork", "fish", "egg", "tofu", "other", "unknown"}
        portion_ranges = {
            "half_palm": (1.0, 2.0), "one_palm": (2.0, 3.0),
            "one_half_palm": (3.0, 5.0), "two_palm": (4.0, 6.0),
        }
        seen_types: set[str] = set()
        total_min = 0.0
        total_max = 0.0
        for item in raw_items:
            if not isinstance(item, Mapping):
                return False
            protein_type = item.get("type")
            portion = item.get("portion")
            item_exchange = item.get("exchange")
            if (
                not isinstance(protein_type, str)
                or protein_type not in valid_types
                or protein_type in seen_types
                or not isinstance(portion, str)
                or portion not in portion_ranges
                or not isinstance(item_exchange, Mapping)
                or item_exchange.get("basis") != "hand_portion_range_v1"
            ):
                return False
            item_min = _strict_json_number(
                item_exchange.get("min"), "protein_item.min", max_value=100
            )
            item_max = _strict_json_number(
                item_exchange.get("max"), "protein_item.max", max_value=100
            )
            if (item_min, item_max) != portion_ranges[portion]:
                return False
            seen_types.add(protein_type)
            total_min += item_min
            total_max += item_max
        return (
            raw_total.get("basis") == "summed_hand_portion_ranges_v2"
            and _strict_json_number(
                raw_total.get("min"), "protein_total.min", max_value=100
            ) == total_min
            and _strict_json_number(
                raw_total.get("max"), "protein_total.max", max_value=100
            ) == total_max
        )
    except (TypeError, ValueError):
        return False


def exchange_applied_payload_matches_approval_hash(
    food_fingerprint_value: str,
    rule_version: str,
    applied: Any,
    servings: Any,
    approval_hash: Any,
) -> bool:
    if not exchange_approval_payload_is_valid(rule_version, applied):
        return False
    try:
        if str(rule_version) != "meal-photo-admin-v2":
            return True
        serving_count = _strict_json_number(servings, "consumed_servings", max_value=100)
        if serving_count <= 0:
            return False
        normalized = dict(applied)
        for key in EXCHANGE_KEYS:
            normalized[key] = round(float(normalized[key]) / serving_count, 4)
        expected = exchange_approval_hash(
            food_fingerprint_value, rule_version, normalized
        )
        return secrets.compare_digest(str(approval_hash or ""), expected)
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def verified_exchange_approval_projection(
    *,
    log_user_id: Any,
    log_food_id: Any,
    catalog_source_type: Any,
    catalog_owner_user_id: Any,
    catalog_fingerprint: Any,
    consumed_servings: Any,
    applied_json: Any,
    approval_id: Any,
    approval_food_id: Any,
    approval_fingerprint: Any,
    rule_version: Any,
    approved_json: Any,
    approval_hash: Any,
) -> dict[str, Any]:
    """Project one approval through its complete source and hash contract.

    The approval/hash helpers remain the canonical payload validators.  This
    wrapper adds the relational and source checks consumers previously applied
    inconsistently, and preserves enough origin evidence to prevent a mutable
    ``source_type`` from downgrading an approved photo into an ordinary log.
    """
    approved = _json_object_or_none(approved_json)
    applied = _json_object_or_none(applied_json)
    rule_version_text = str(rule_version or "")
    is_meal_photo_origin = bool(
        str(catalog_source_type or "") == "user_meal_photo"
        or rule_version_text == "meal-photo-admin-v2"
        or (isinstance(approved, Mapping) and approved.get("_source_type") == "meal_photo")
        or (isinstance(applied, Mapping) and applied.get("_source_type") == "meal_photo")
    )
    result = {
        "is_valid": False,
        "is_meal_photo_origin": is_meal_photo_origin,
        "approved": approved or {},
        "applied": applied or {},
    }
    if (
        not approval_id
        or not approval_food_id
        or str(approval_food_id) != str(log_food_id or "")
        or not approval_fingerprint
        or str(approval_fingerprint) != str(catalog_fingerprint or "")
        or approved is None
        or applied is None
        or not exchange_approval_payload_is_valid(rule_version_text, approved)
        or not exchange_approval_payload_is_valid(rule_version_text, applied)
    ):
        return result
    if is_meal_photo_origin and (
        str(catalog_source_type or "") != "user_meal_photo"
        or not log_user_id
        or str(catalog_owner_user_id or "") != str(log_user_id)
    ):
        return result
    if rule_version_text == "meal-photo-admin-v2" and (
        approved.get("_source_type") != "meal_photo"
        or applied.get("_source_type") != "meal_photo"
    ):
        return result
    expected_hash = exchange_approval_hash(
        str(approval_fingerprint), rule_version_text, approved
    )
    if (
        not secrets.compare_digest(str(approval_hash or ""), expected_hash)
        or not exchange_applied_payload_matches_approval_hash(
            str(approval_fingerprint), rule_version_text, applied,
            consumed_servings, approval_hash,
        )
    ):
        return result
    try:
        expected_applied = {
            key: round(
                float(approved.get(key, 0) or 0) * float(consumed_servings or 0), 4
            )
            for key in EXCHANGE_KEYS
        }
        if any(
            abs(float(applied.get(key, 0) or 0) - expected_applied[key]) > 0.0001
            for key in EXCHANGE_KEYS
        ):
            return result
    except (TypeError, ValueError):
        return result
    result["is_valid"] = True
    return result


def verified_catalog_exchange_approval_projection(
    *,
    catalog_food_id: Any,
    catalog_source_type: Any,
    catalog_owner_user_id: Any,
    catalog_fingerprint: Any,
    approval_id: Any,
    approval_food_id: Any,
    approval_fingerprint: Any,
    rule_version: Any,
    approved_json: Any,
    approval_hash: Any,
    canonical_log_user_id: Any,
) -> dict[str, Any]:
    """Validate a catalog approval without requiring logs for ordinary foods.

    Meal-photo approvals are created with a canonical food log, whose immutable
    user binding supplies the ownership evidence that the catalog row itself
    cannot provide after mutation.  Ordinary catalog approvals retain their
    valid no-log lifecycle.
    """
    return verified_exchange_approval_projection(
        log_user_id=canonical_log_user_id,
        log_food_id=catalog_food_id,
        catalog_source_type=catalog_source_type,
        catalog_owner_user_id=catalog_owner_user_id,
        catalog_fingerprint=catalog_fingerprint,
        consumed_servings=1,
        applied_json=approved_json,
        approval_id=approval_id,
        approval_food_id=approval_food_id,
        approval_fingerprint=approval_fingerprint,
        rule_version=rule_version,
        approved_json=approved_json,
        approval_hash=approval_hash,
    )


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
                "信任類型", "估算Schema版本",
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
            menu_category TEXT NOT NULL DEFAULT '',
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
            trust_type TEXT NOT NULL DEFAULT '',
            trust_payload_json TEXT NOT NULL DEFAULT '{}',
            trust_hash TEXT NOT NULL DEFAULT '',
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
        CREATE INDEX IF NOT EXISTS idx_food_catalog_normalized_name
            ON food_catalog(
                lower(replace(replace(replace(replace(
                    product_name, ' ', ''), char(9), ''), char(10), ''), char(13), ''))
            );
        CREATE INDEX IF NOT EXISTS idx_food_logs_user_time ON food_logs(user_id, consumed_at);
        CREATE INDEX IF NOT EXISTS idx_plan_user_effective ON nutrition_plans(user_id, effective_from);
        CREATE INDEX IF NOT EXISTS idx_food_exchange_approvals_food
            ON food_exchange_approvals(food_id, approved_at);
        """
    )
    schema_component = "nutrition_system"
    schema_version = 6
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
            "trust_type": "TEXT NOT NULL DEFAULT ''",
            "trust_payload_json": "TEXT NOT NULL DEFAULT '{}'",
            "trust_hash": "TEXT NOT NULL DEFAULT ''",
        },
        "food_catalog": {
            "menu_category": "TEXT NOT NULL DEFAULT ''",
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
        """CREATE INDEX IF NOT EXISTS idx_food_catalog_menu_category
           ON food_catalog(menu_category)"""
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


def _canonical_json_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _valid_estimate_range(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"min", "max", "basis"}:
        return False
    minimum, maximum, basis = value["min"], value["max"], value["basis"]
    if (
        isinstance(minimum, bool) or isinstance(maximum, bool)
        or not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float))
        or not math.isfinite(minimum) or not math.isfinite(maximum)
        or minimum < 0 or maximum < minimum or maximum > 100
    ):
        return False
    return basis in {
        "user_confirmed_none", "hand_portion_range_v1", "bowl_range_v1",
        "summed_hand_portion_ranges_v2",
    }


def meal_photo_estimate_snapshot_is_valid(value: Any) -> bool:
    """Strictly validate the canonical, non-approved range snapshot."""
    if not isinstance(value, dict):
        return False
    if value.get("rule_version") == "ai-vision-nutrition-estimate-v1":
        required = {
            "calories_kcal", "protein_g", "fat_g", "carbohydrate_g",
            "calories_kcal_range", "protein_g_range", "estimate_items",
            "estimate_confidence", "provenance", "protein_total_exchange",
            "starch_exchange", "vegetable_exchange", "cooking_oil_confirmation",
            "sauce_confirmation", "formal_status", "rule_version",
        }
        if set(value) != required:
            return False
        def valid_number(raw: Any, maximum: float) -> bool:
            return (
                not isinstance(raw, bool) and isinstance(raw, (int, float))
                and math.isfinite(raw) and 0 <= raw <= maximum
            )
        if not valid_number(value["calories_kcal"], 5000) or not valid_number(value["protein_g"], 500):
            return False
        for key, maximum in (("calories_kcal_range", 5000), ("protein_g_range", 500)):
            raw = value[key]
            point = value["calories_kcal" if key.startswith("calories") else "protein_g"]
            if (
                not isinstance(raw, dict) or set(raw) != {"min", "max", "basis"}
                or raw.get("basis") != "ai_vision_estimate_range_v1"
                or not valid_number(raw.get("min"), maximum)
                or not valid_number(raw.get("max"), maximum)
                or not raw["min"] <= point <= raw["max"] or raw["min"] == raw["max"]
            ):
                return False
        items = value["estimate_items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 12:
            return False
        for item in items:
            if (
                not isinstance(item, dict) or set(item) != {"name", "portion", "calories_kcal", "protein_g"}
                or not isinstance(item["name"], str) or not item["name"] or len(item["name"]) > 60
                or not isinstance(item["portion"], str) or not item["portion"] or len(item["portion"]) > 40
                or not valid_number(item["calories_kcal"], 3000)
                or not valid_number(item["protein_g"], 300)
            ):
                return False
        provenance = value["provenance"]
        return (
            valid_number(value["estimate_confidence"], 1)
            and isinstance(provenance, dict)
            and set(provenance) == {"provider", "model", "method", "nutrition_basis"}
            and all(isinstance(provenance[key], str) and provenance[key] for key in provenance)
            and provenance["method"] == "vision_model_estimate"
            and provenance["nutrition_basis"] == "unlabeled_meal_photo"
            and value["fat_g"] is None and value["carbohydrate_g"] is None
            and value["protein_total_exchange"] is None and value["starch_exchange"] is None
            and value["vegetable_exchange"] is None
            and value["cooking_oil_confirmation"] == "unknown"
            and value["sauce_confirmation"] == "unknown"
            and value["formal_status"] == "user_confirmed_ai_estimate_not_approved"
        )
    required = {
        "calories_kcal", "protein_g", "fat_g", "carbohydrate_g",
        "protein_total_exchange", "starch_exchange", "vegetable_exchange",
        "cooking_oil_confirmation", "sauce_confirmation", "formal_status", "rule_version",
    }
    if not required.issubset(value) or set(value) - (required | {"protein_items"}):
        return False
    if any(value[key] is not None for key in (
        "calories_kcal", "protein_g", "fat_g", "carbohydrate_g",
    )):
        return False
    if not all(_valid_estimate_range(value[key]) for key in (
        "protein_total_exchange", "starch_exchange", "vegetable_exchange",
    )):
        return False
    if value["cooking_oil_confirmation"] not in {"none", "light", "normal", "heavy", "unknown"}:
        return False
    if value["sauce_confirmation"] not in {"none", "little", "half", "all", "unknown"}:
        return False
    if value["formal_status"] != "pending_review_not_counted":
        return False
    if value["rule_version"] not in {"hand-portion-range-v1", "hand-portion-range-v2"}:
        return False
    items = value.get("protein_items")
    if items is None:
        return value["rule_version"] == "hand-portion-range-v1"
    if value["rule_version"] != "hand-portion-range-v2" or not isinstance(items, list) or not 2 <= len(items) <= 4:
        return False
    seen = set()
    total = value["protein_total_exchange"]
    if not isinstance(total, dict) or total.get("basis") != "summed_hand_portion_ranges_v2":
        return False
    sum_min = sum_max = 0.0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"type", "portion", "exchange"}:
            return False
        if item["type"] not in {"chicken", "pork", "fish", "egg", "tofu", "other", "unknown"} or item["type"] in seen:
            return False
        seen.add(item["type"])
        if item["portion"] not in {"half_palm", "one_palm", "one_half_palm", "two_palm"}:
            return False
        if not _valid_estimate_range(item["exchange"]):
            return False
        sum_min += item["exchange"]["min"]
        sum_max += item["exchange"]["max"]
    return abs(sum_min - total["min"]) <= 0.0001 and abs(sum_max - total["max"]) <= 0.0001


def _legacy_user_confirmed_meal_photo_estimate_is_valid(
    conn: sqlite3.Connection, log_id: str, *, expected_user_id: str = "",
    expected_draft_token: str = "", expected_draft_version: int | None = None,
) -> bool:
    """Validate owner/version/event-bound user confirmation without treating it as approval."""
    try:
        log_columns = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
        version_sql = "l.version" if "version" in log_columns else "1"
        row = conn.execute(
            f"""SELECT l.log_id,l.user_id,l.food_id,l.consumed_at,l.meal_slot,l.consumed_servings,
                      l.exchange_snapshot_json,l.approved_exchange_json,l.exchange_approval_id,
                      l.nutrition_snapshot_json,l.confirmation_status,l.trust_type,
                      l.trust_payload_json,l.trust_hash,f.fingerprint,f.source_type,
                      f.owner_user_id,f.exchange_review_status,f.verification_status,{version_sql}
               FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id WHERE l.log_id=?""",
            (str(log_id or ""),),
        ).fetchone()
        if not row or row[10] != "confirmed" or row[11] != "user_confirmed_ai_estimate":
            return False
        snapshot = json.loads(row[6])
        nutrition_snapshot = json.loads(row[9])
        is_ai_nutrition = snapshot.get("rule_version") == "ai-vision-nutrition-estimate-v1"
        expected_nutrition = (
            {"calories_kcal": float(snapshot["calories_kcal"]), "protein_g": float(snapshot["protein_g"])}
            if is_ai_nutrition else {}
        )
        if row[7] != "{}" or str(row[8] or "") or nutrition_snapshot != expected_nutrition:
            return False
        if row[15:19] != ("user_meal_photo", row[1], "user_confirmed_ai_estimate", "user_confirmed_ai_estimate"):
            return False
        payload = json.loads(row[12])
        required = {
            "schema_version", "log_id", "food_id", "user_id", "draft_token",
            "source_message_id", "confirmation_event_id", "consumed_at", "meal_slot",
            "consumed_servings", "food_fingerprint", "exchange_snapshot",
            "estimate_rule_version", "confirmed_at",
        }
        if is_ai_nutrition:
            required.add("nutrition_snapshot")
        if not isinstance(payload, dict) or set(payload) != required:
            return False
        if expected_user_id and payload["user_id"] != expected_user_id:
            return False
        if expected_draft_token and payload["draft_token"] != expected_draft_token:
            return False
        expected_schema = "meal-photo-user-confirmation-v2" if is_ai_nutrition else "meal-photo-user-confirmation-v1"
        if payload["schema_version"] != expected_schema:
            return False
        if is_ai_nutrition and payload["nutrition_snapshot"] != nutrition_snapshot:
            return False
        if not all(isinstance(payload[key], str) and payload[key] for key in (
            "draft_token", "source_message_id", "confirmation_event_id", "confirmed_at",
        )):
            return False
        if isinstance(payload["consumed_servings"], bool) or payload["consumed_servings"] != 1:
            return False
        expected = (row[0], row[2], row[1], row[3], row[4], row[14])
        actual = tuple(payload[key] for key in (
            "log_id", "food_id", "user_id", "consumed_at", "meal_slot", "food_fingerprint",
        ))
        draft_columns = {
            str(item[1]) for item in conn.execute("PRAGMA table_info(pending_meal_photo_drafts)")
        }
        if "original_confirmation_event_id" not in draft_columns:
            return False
        draft = conn.execute(
            f"""SELECT user_id,source_message_id,status,version,workflow_version,
                       confirmed_log_id,confirmed_by,original_confirmation_event_id
                FROM pending_meal_photo_drafts WHERE token=?""",
            (payload["draft_token"],),
        ).fetchone()
        expected_workflow = "user_confirmed_ai_nutrition_v2" if is_ai_nutrition else "user_confirmed_ai_estimate_v1"
        if not draft or tuple(draft[:7]) != (
            row[1], payload["source_message_id"], "user_confirmed",
            int(draft[3]), expected_workflow, row[0], row[1],
        ):
            return False
        if not draft[7] or draft[7] != payload["confirmation_event_id"]:
            return False
        if int(draft[3]) < 2 or int(row[19]) != 1:
            return False
        if expected_draft_version is not None and int(draft[3]) != int(expected_draft_version):
            return False
        event = conn.execute(
            """SELECT user_id,token,action,request_payload_hash,result_json
               FROM meal_photo_events WHERE event_id=?""",
            (payload["confirmation_event_id"],),
        ).fetchone()
        if not event or event[:3] != (row[1], payload["draft_token"], "confirm_estimate"):
            return False
        original_request = {
            "user_id": row[1], "token": payload["draft_token"],
            "expected_version": int(draft[3]) - 1, "action": "confirm_estimate",
            "field": "", "value": "",
        }
        request_hash = hashlib.sha256(json.dumps(
            original_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        event_result = json.loads(event[4])
        if (
            event[3] != request_hash or not isinstance(event_result, dict)
            or event_result.get("kind") != "recorded"
            or event_result.get("log_id") != row[0]
            or event_result.get("version") != int(draft[3])
        ):
            return False
        return (
            actual == expected and payload["exchange_snapshot"] == snapshot
            and meal_photo_estimate_snapshot_is_valid(snapshot)
            and payload["estimate_rule_version"] == snapshot["rule_version"]
            and secrets.compare_digest(str(row[13] or ""), _canonical_json_hash(payload))
        )
    except (TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return False


def _meal_photo_state(
    *, food_id: str, consumed_at: str, meal_slot: str, consumed_servings: Any,
    estimate: Mapping[str, Any], nutrition: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "food_id": str(food_id),
        "consumed_at": str(consumed_at),
        "meal_slot": str(meal_slot or ""),
        "consumed_servings": float(consumed_servings),
        "exchange_snapshot": dict(estimate),
        "nutrition_snapshot": dict(nutrition),
    }


def _validated_current_meal_photo_state(
    conn: sqlite3.Connection, log_id: str, *, expected_user_id: str = "",
    expected_draft_token: str = "", expected_draft_version: int | None = None,
) -> dict[str, Any] | None:
    """Validate immutable confirmation genesis and every append-only current revision."""
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
        version_sql = "l.version" if "version" in columns else "1"
        deleted_sql = "COALESCE(l.deleted_at,'')" if "deleted_at" in columns else "''"
        row = conn.execute(
            f"""SELECT l.log_id,l.user_id,l.food_id,l.consumed_at,l.meal_slot,
                       l.consumed_servings,l.exchange_snapshot_json,l.nutrition_snapshot_json,
                       l.approved_exchange_json,l.exchange_approval_id,l.confirmation_status,
                       l.trust_type,l.trust_payload_json,l.trust_hash,f.fingerprint,
                       f.source_type,f.owner_user_id,f.exchange_review_status,
                       f.verification_status,{version_sql},{deleted_sql}
                FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
                WHERE l.log_id=?""",
            (str(log_id or ""),),
        ).fetchone()
        if (
            not row or row[10] != "confirmed" or row[20] or row[11] != "user_confirmed_ai_estimate"
            or row[15:19] != ("user_meal_photo", row[1], "user_confirmed_ai_estimate", "user_confirmed_ai_estimate")
            or row[8] != "{}" or str(row[9] or "")
            or (expected_user_id and row[1] != expected_user_id)
        ):
            return None
        payload = json.loads(row[12])
        base_required = {
            "schema_version", "log_id", "food_id", "user_id", "draft_token",
            "source_message_id", "confirmation_event_id", "consumed_at", "meal_slot",
            "consumed_servings", "food_fingerprint", "exchange_snapshot",
            "estimate_rule_version", "confirmed_at",
        }
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            "meal-photo-user-confirmation-v1", "meal-photo-user-confirmation-v2"
        }:
            return None
        is_v2 = payload["schema_version"] == "meal-photo-user-confirmation-v2"
        if set(payload) != base_required | ({"nutrition_snapshot"} if is_v2 else set()):
            return None
        if expected_draft_token and payload["draft_token"] != expected_draft_token:
            return None
        if not all(isinstance(payload.get(key), str) and payload[key] for key in (
            "draft_token", "source_message_id", "confirmation_event_id", "confirmed_at",
        )):
            return None
        if payload["consumed_servings"] != 1 or isinstance(payload["consumed_servings"], bool):
            return None
        if tuple(payload[key] for key in (
            "log_id", "food_id", "user_id", "consumed_at", "meal_slot", "food_fingerprint",
        )) != (row[0], row[2], row[1], row[3], row[4], row[14]):
            return None
        original_estimate = payload["exchange_snapshot"]
        if (
            not meal_photo_estimate_snapshot_is_valid(original_estimate)
            or payload["estimate_rule_version"] != original_estimate["rule_version"]
        ):
            return None
        original_nutrition = payload.get("nutrition_snapshot", {})
        if is_v2:
            if original_nutrition != {
                "calories_kcal": float(original_estimate["calories_kcal"]),
                "protein_g": float(original_estimate["protein_g"]),
            }:
                return None
            expected_workflow = "user_confirmed_ai_nutrition_v2"
        else:
            if original_nutrition:
                return None
            expected_workflow = "user_confirmed_ai_estimate_v1"
        if not secrets.compare_digest(str(row[13] or ""), _canonical_json_hash(payload)):
            return None
        draft = conn.execute(
            """SELECT user_id,source_message_id,status,version,workflow_version,
                      confirmed_log_id,confirmed_by,original_confirmation_event_id
               FROM pending_meal_photo_drafts WHERE token=?""",
            (payload["draft_token"],),
        ).fetchone()
        if not draft or tuple(draft[:7]) != (
            row[1], payload["source_message_id"], "user_confirmed", int(draft[3]),
            expected_workflow, row[0], row[1],
        ) or draft[7] != payload["confirmation_event_id"] or int(draft[3]) < 2:
            return None
        if expected_draft_version is not None and int(draft[3]) != int(expected_draft_version):
            return None
        event = conn.execute(
            """SELECT user_id,token,action,request_payload_hash,result_json
               FROM meal_photo_events WHERE event_id=?""",
            (payload["confirmation_event_id"],),
        ).fetchone()
        original_request = {
            "user_id": row[1], "token": payload["draft_token"],
            "expected_version": int(draft[3]) - 1, "action": "confirm_estimate",
            "field": "", "value": "",
        }
        request_hash = _canonical_json_hash(original_request)
        event_result = json.loads(event[4]) if event else None
        if (
            not event or event[:3] != (row[1], payload["draft_token"], "confirm_estimate")
            or event[3] != request_hash or not isinstance(event_result, dict)
            or event_result.get("kind") != "recorded" or event_result.get("log_id") != row[0]
            or event_result.get("version") != int(draft[3])
        ):
            return None
        state = _meal_photo_state(
            food_id=row[2], consumed_at=row[3], meal_slot=row[4], consumed_servings=1,
            estimate=original_estimate, nutrition=original_nutrition,
        )
        previous_hash = str(row[13])
        current_version = 1
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_food_log_events'"
        ).fetchone()
        revision_rows = conn.execute(
            """SELECT event_id,user_id,log_id,result_json FROM daily_food_log_events
               WHERE log_id=? AND action='confirm_ai_revision'""",
            (row[0],),
        ).fetchall() if table_exists else []
        required = {
            "schema_version", "event_id", "user_id", "log_id", "from_version",
            "to_version", "draft_token", "original_confirmation_hash",
            "previous_revision_hash", "request_text_hash", "before_state_hash",
            "after_state", "estimate_provenance", "created_at", "revision_hash",
        }
        revisions = []
        seen_from_versions = set()
        for revision_row in revision_rows:
            envelope = json.loads(revision_row[3])
            if not isinstance(envelope, dict) or set(envelope) != required:
                return None
            from_version = envelope["from_version"]
            to_version = envelope["to_version"]
            if (
                isinstance(from_version, bool) or not isinstance(from_version, int)
                or isinstance(to_version, bool) or not isinstance(to_version, int)
                or from_version < 1 or to_version != from_version + 1
                or from_version in seen_from_versions
            ):
                return None
            seen_from_versions.add(from_version)
            revisions.append((from_version, revision_row, envelope))
        revisions.sort(key=lambda item: item[0])
        for _, revision_row, envelope in revisions:
            if (
                envelope["schema_version"] != "confirmed-food-log-revision-v1"
                or (revision_row[0], revision_row[1], revision_row[2]) !=
                   (envelope["event_id"], envelope["user_id"], envelope["log_id"])
                or envelope["user_id"] != row[1] or envelope["log_id"] != row[0]
                or envelope["from_version"] != current_version
                or envelope["to_version"] != current_version + 1
                or envelope["original_confirmation_hash"] != row[13]
                or envelope["previous_revision_hash"] != previous_hash
                or envelope["before_state_hash"] != _canonical_json_hash(state)
                or not re.fullmatch(r"[0-9a-f]{64}", str(envelope["request_text_hash"] or ""))
                or envelope["revision_hash"] != _canonical_json_hash({
                    key: value for key, value in envelope.items() if key != "revision_hash"
                })
            ):
                return None
            after = envelope["after_state"]
            if not isinstance(after, dict) or set(after) != {
                "food_id", "consumed_at", "meal_slot", "consumed_servings",
                "exchange_snapshot", "nutrition_snapshot",
            }:
                return None
            estimate = after["exchange_snapshot"]
            nutrition = after["nutrition_snapshot"]
            provenance = envelope["estimate_provenance"]
            if (
                after["food_id"] != row[2] or after["consumed_at"] != row[3]
                or after["meal_slot"] != row[4] or after["consumed_servings"] != 1.0
                or not meal_photo_estimate_snapshot_is_valid(estimate)
                or estimate.get("rule_version") != "ai-vision-nutrition-estimate-v1"
                or nutrition != {"calories_kcal": float(estimate["calories_kcal"]),
                                 "protein_g": float(estimate["protein_g"])}
                or not isinstance(provenance, dict) or set(provenance) != {
                    "rule_version", "provider", "model", "source"
                }
                or provenance != {
                    "rule_version": "ai-vision-nutrition-estimate-v1",
                    "provider": estimate["provenance"]["provider"],
                    "model": estimate["provenance"]["model"],
                    "source": "original_meal_photo_plus_user_revision",
                }
            ):
                return None
            revision_draft = conn.execute(
                """SELECT user_id,status,workflow_version,review_json,estimate_json,
                          confirmed_log_id,version
                   FROM pending_meal_photo_drafts WHERE token=?""",
                (envelope["draft_token"],),
            ).fetchone()
            if not revision_draft or revision_draft[:3] != (
                row[1], "revision_confirmed", "confirmed_food_log_revision_v1"
            ) or revision_draft[5] or json.loads(revision_draft[4]) != estimate:
                return None
            review = json.loads(revision_draft[3])
            if (
                not isinstance(review, dict)
                or review.get("schema_version") != "confirmed-food-log-revision-preview-v1"
                or review.get("parent") != {
                    "user_id": row[1], "log_id": row[0],
                    "from_version": current_version,
                    "original_confirmation_hash": row[13],
                    "previous_revision_hash": previous_hash,
                }
                or review.get("request_text_hash") != envelope["request_text_hash"]
                or review.get("estimate_provenance") != provenance
            ):
                return None
            state = after
            previous_hash = envelope["revision_hash"]
            current_version += 1
        current_estimate = json.loads(row[6])
        current_nutrition = json.loads(row[7])
        if (
            int(row[19]) != current_version
            or current_estimate != state["exchange_snapshot"]
            or current_nutrition != state["nutrition_snapshot"]
            or float(row[5]) != state["consumed_servings"]
        ):
            return None
        return {
            "state": state, "log_version": current_version,
            "effective_revision_hash": previous_hash,
            "original_confirmation_hash": str(row[13]),
            "workflow_version": expected_workflow,
            "schema_version": payload["schema_version"],
        }
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error):
        return None


def user_confirmed_meal_photo_estimate_is_valid(
    conn: sqlite3.Connection, log_id: str, *, expected_user_id: str = "",
    expected_draft_token: str = "", expected_draft_version: int | None = None,
) -> bool:
    return _validated_current_meal_photo_state(
        conn, log_id, expected_user_id=expected_user_id,
        expected_draft_token=expected_draft_token,
        expected_draft_version=expected_draft_version,
    ) is not None


def user_confirmed_meal_photo_trust_projection(
    conn: sqlite3.Connection, log_id: str, trust_type: str,
) -> dict[str, Any]:
    """Project one explicit trust state without relabeling integrity failure as review."""
    marker = "user_confirmed_ai_estimate"
    try:
        evidence = conn.execute(
            """SELECT l.trust_type,l.trust_payload_json,l.trust_hash,
                      f.source_type,f.exchange_review_status,f.verification_status,
                      EXISTS(SELECT 1 FROM pending_meal_photo_drafts d
                             WHERE d.confirmed_log_id=l.log_id
                               AND d.workflow_version='user_confirmed_ai_estimate_v1')
               FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
               WHERE l.log_id=?""",
            (str(log_id or ""),),
        ).fetchone()
    except sqlite3.Error:
        evidence = None
    is_new_lane = trust_type == marker
    if evidence:
        is_new_lane = is_new_lane or bool(
            evidence[0] == marker
            or str(evidence[1] or "") not in {"", "{}"}
            or str(evidence[2] or "")
            or evidence[4] == marker
            or evidence[5] == marker
            or evidence[6]
        )
    if not is_new_lane:
        return {"trust_type": "", "schema_version": "", "integrity_status": ""}
    validated = _validated_current_meal_photo_state(conn, log_id)
    if validated is not None:
        state = validated["state"]
        return {
            "trust_type": "user_confirmed_ai_estimate",
            "schema_version": validated["schema_version"],
            "workflow_version": validated["workflow_version"],
            "integrity_status": "verified",
            "estimate": state["exchange_snapshot"],
            "nutrition": state["nutrition_snapshot"],
            "effective_revision_hash": validated["effective_revision_hash"],
            "original_confirmation_hash": validated["original_confirmation_hash"],
            "log_version": validated["log_version"],
        }
    return {
        "trust_type": "untrusted_user_confirmed_ai_estimate",
        "schema_version": "",
        "integrity_status": "integrity_verification_failed",
    }


def _ensure_food_log_revision_contract(conn: sqlite3.Connection) -> None:
    required_tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('daily_food_log_events','pending_meal_photo_drafts','nutrition_sheet_outbox')"
        )
    }
    if required_tables != {
        "daily_food_log_events", "pending_meal_photo_drafts", "nutrition_sheet_outbox"
    }:
        raise RuntimeError("revision資料層尚未完成初始化")
    log_columns = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
    if not {"version", "deleted_at"} <= log_columns:
        raise RuntimeError("food log revision欄位尚未完成初始化")
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS daily_food_revision_events_no_update
        BEFORE UPDATE ON daily_food_log_events
        WHEN OLD.action='confirm_ai_revision'
        BEGIN SELECT RAISE(ABORT, 'confirm_ai_revision events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS daily_food_revision_events_no_delete
        BEFORE DELETE ON daily_food_log_events
        WHEN OLD.action='confirm_ai_revision'
        BEGIN SELECT RAISE(ABORT, 'confirm_ai_revision events are append-only'); END;
    """)


def confirm_user_meal_photo_revision(
    conn: sqlite3.Connection, *, event_id: str, user_id: str, log_id: str,
    from_version: int, draft_token: str,
) -> dict[str, Any]:
    """Atomically append a trusted revision and CAS-update the same meal occurrence."""
    if conn.in_transaction:
        raise RuntimeError("revision確認需要由domain API持有top-level transaction")
    _ensure_food_log_revision_contract(conn)
    event_id = str(event_id or "").strip()
    user_id = str(user_id or "").strip()
    log_id = str(log_id or "").strip()
    draft_token = str(draft_token or "").strip()
    if not all((event_id, user_id, log_id, draft_token)):
        raise ValueError("revision確認來源不完整")
    if isinstance(from_version, bool) or not isinstance(from_version, int) or from_version < 1:
        raise ValueError("revision來源版本無效")
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = conn.execute(
            """SELECT user_id,log_id,action,result_json FROM daily_food_log_events
               WHERE event_id=?""", (event_id,),
        ).fetchone()
        if previous:
            if previous[:3] != (user_id, log_id, "confirm_ai_revision"):
                raise ValueError("revision事件識別碼衝突")
            result = json.loads(previous[3])
            if (
                not isinstance(result, dict) or result.get("event_id") != event_id
                or result.get("draft_token") != draft_token
                or result.get("from_version") != from_version
                or _validated_current_meal_photo_state(
                    conn, log_id, expected_user_id=user_id
                ) is None
            ):
                raise ValueError("revision重播資料驗證失敗")
            now = utcish_now()
            conn.execute(
                """INSERT INTO nutrition_sheet_outbox
                   (outbox_id,entity_type,entity_id,status,attempts,last_error,
                    claimed_at,lease_owner,resync_required,created_at,synced_at)
                   VALUES (?,'food_log',?,'pending',0,'','','',0,?,'')
                   ON CONFLICT(entity_type,entity_id) DO UPDATE SET
                     status=CASE WHEN status='processing' THEN status ELSE 'pending' END,
                     last_error=CASE WHEN status='processing' THEN last_error ELSE '' END,
                     claimed_at=CASE WHEN status='processing' THEN claimed_at ELSE '' END,
                     lease_owner=CASE WHEN status='processing' THEN lease_owner ELSE '' END,
                     synced_at=CASE WHEN status='processing' THEN synced_at ELSE '' END,
                     resync_required=CASE WHEN status='processing' THEN 1 ELSE 0 END""",
                (new_id("outbox"), log_id, now),
            )
            conn.commit()
            return {**result, "replayed": True}
        row = conn.execute(
            """SELECT l.food_id,l.consumed_at,l.meal_slot,l.consumed_servings,
                      l.version,l.trust_hash,l.confirmation_status,COALESCE(l.deleted_at,''),
                      f.source_type,f.owner_user_id
               FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
               WHERE l.log_id=? AND l.user_id=?""",
            (log_id, user_id),
        ).fetchone()
        if (
            not row or row[6] != "confirmed" or row[7] or row[8] != "user_meal_photo"
            or row[9] != user_id
        ):
            raise ValueError("找不到可修改的餐點紀錄")
        if int(row[4]) != from_version:
            raise ValueError("這筆紀錄已更新，請重新開啟最新卡片")
        validated = _validated_current_meal_photo_state(
            conn, log_id, expected_user_id=user_id
        )
        if validated is None or validated["log_version"] != from_version:
            raise ValueError("原餐點可信鏈驗證失敗")
        draft = conn.execute(
            """SELECT user_id,status,workflow_version,version,expires_at,review_json,
                      estimate_json,confirmed_log_id
               FROM pending_meal_photo_drafts WHERE token=?""",
            (draft_token,),
        ).fetchone()
        now = utcish_now()
        if (
            not draft or draft[0] != user_id or draft[1] != "estimated"
            or draft[2] != "confirmed_food_log_revision_v1" or draft[7]
        ):
            raise ValueError("找不到可確認的revision草稿")
        if str(draft[4] or "") <= now:
            raise ValueError("revision草稿已逾時")
        review = json.loads(draft[5])
        estimate = json.loads(draft[6])
        provenance = estimate.get("provenance") if isinstance(estimate, dict) else None
        expected_provenance = {
            "rule_version": "ai-vision-nutrition-estimate-v1",
            "provider": provenance.get("provider") if isinstance(provenance, dict) else None,
            "model": provenance.get("model") if isinstance(provenance, dict) else None,
            "source": "original_meal_photo_plus_user_revision",
        }
        expected_parent = {
            "user_id": user_id, "log_id": log_id, "from_version": from_version,
            "original_confirmation_hash": validated["original_confirmation_hash"],
            "previous_revision_hash": validated["effective_revision_hash"],
        }
        if (
            not isinstance(review, dict) or set(review) != {
                "schema_version", "parent", "request_text_hash", "estimate_provenance"
            }
            or review["schema_version"] != "confirmed-food-log-revision-preview-v1"
            or review["parent"] != expected_parent
            or review["estimate_provenance"] != expected_provenance
            or not re.fullmatch(r"[0-9a-f]{64}", str(review["request_text_hash"] or ""))
            or not meal_photo_estimate_snapshot_is_valid(estimate)
            or estimate.get("rule_version") != "ai-vision-nutrition-estimate-v1"
        ):
            raise ValueError("revision草稿可信來源驗證失敗")
        nutrition = {
            "calories_kcal": float(estimate["calories_kcal"]),
            "protein_g": float(estimate["protein_g"]),
        }
        before_state = validated["state"]
        after_state = _meal_photo_state(
            food_id=row[0], consumed_at=row[1], meal_slot=row[2],
            consumed_servings=row[3], estimate=estimate, nutrition=nutrition,
        )
        envelope = {
            "schema_version": "confirmed-food-log-revision-v1",
            "event_id": event_id, "user_id": user_id, "log_id": log_id,
            "from_version": from_version, "to_version": from_version + 1,
            "draft_token": draft_token,
            "original_confirmation_hash": validated["original_confirmation_hash"],
            "previous_revision_hash": validated["effective_revision_hash"],
            "request_text_hash": review["request_text_hash"],
            "before_state_hash": _canonical_json_hash(before_state),
            "after_state": after_state,
            "estimate_provenance": expected_provenance,
            "created_at": now,
        }
        envelope["revision_hash"] = _canonical_json_hash(envelope)
        envelope_json = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        conn.execute(
            """INSERT INTO daily_food_log_events
               (event_id,user_id,log_id,action,result_json,created_at)
               VALUES (?,?,?,'confirm_ai_revision',?,?)""",
            (event_id, user_id, log_id, envelope_json, now),
        )
        changed = conn.execute(
            """UPDATE food_logs SET exchange_snapshot_json=?,nutrition_snapshot_json=?,
                      consumed_servings=?,version=?,updated_at=?
               WHERE log_id=? AND user_id=? AND version=?
                 AND confirmation_status='confirmed' AND COALESCE(deleted_at,'')=''""",
            (
                json.dumps(estimate, ensure_ascii=False, sort_keys=True, allow_nan=False),
                json.dumps(nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False),
                after_state["consumed_servings"], from_version + 1, now,
                log_id, user_id, from_version,
            ),
        ).rowcount
        if changed != 1:
            raise ValueError("這筆紀錄已更新，請重新開啟最新卡片")
        changed = conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET status='revision_confirmed',version=version+1,updated_at=?,
                   confirmed_at=?,confirmed_by=?
               WHERE token=? AND user_id=? AND version=? AND status='estimated'
                 AND workflow_version='confirmed_food_log_revision_v1'""",
            (now, now, user_id, draft_token, user_id, int(draft[3])),
        ).rowcount
        if changed != 1:
            raise ValueError("revision草稿已更新")
        conn.execute(
            """INSERT INTO nutrition_sheet_outbox
               (outbox_id,entity_type,entity_id,status,attempts,last_error,
                claimed_at,lease_owner,resync_required,created_at,synced_at)
               VALUES (?,'food_log',?,'pending',0,'','','',0,?,'')
               ON CONFLICT(entity_type,entity_id) DO UPDATE SET
                 status=CASE WHEN status='processing' THEN status ELSE 'pending' END,
                 last_error=CASE WHEN status='processing' THEN last_error ELSE '' END,
                 claimed_at=CASE WHEN status='processing' THEN claimed_at ELSE '' END,
                 lease_owner=CASE WHEN status='processing' THEN lease_owner ELSE '' END,
                 synced_at=CASE WHEN status='processing' THEN synced_at ELSE '' END,
                 resync_required=CASE WHEN status='processing' THEN 1 ELSE 0 END""",
            (new_id("outbox"), log_id, now),
        )
        if _validated_current_meal_photo_state(
            conn, log_id, expected_user_id=user_id
        ) is None:
            raise ValueError("revision寫入後可信鏈驗證失敗")
        conn.commit()
        return {**envelope, "replayed": False}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def user_confirmed_meal_photo_food_trust_projection(
    conn: sqlite3.Connection, food_id: str,
) -> dict[str, str]:
    """Project catalog trust from catalog plus same-owner canonical-log evidence."""
    marker = "user_confirmed_ai_estimate"
    empty = {"trust_type": "", "schema_version": "", "integrity_status": ""}
    try:
        food = conn.execute(
            """SELECT owner_user_id,exchange_review_status,verification_status
               FROM food_catalog WHERE food_id=?""",
            (str(food_id or ""),),
        ).fetchone()
        if not food:
            return empty
        logs = conn.execute(
            """SELECT l.log_id,l.trust_type,l.trust_payload_json,l.trust_hash
               FROM food_logs l WHERE l.food_id=? AND l.user_id=?""",
            (str(food_id or ""), str(food[0] or "")),
        ).fetchall()
    except sqlite3.Error:
        return empty
    is_new_lane = food[1] == marker or food[2] == marker or any(
        log[1] == marker
        or str(log[2] or "") not in {"", "{}"}
        or bool(str(log[3] or ""))
        for log in logs
    )
    if not is_new_lane:
        return empty
    for log in logs:
        projected = user_confirmed_meal_photo_trust_projection(conn, log[0], log[1])
        if projected["integrity_status"] == "verified":
            return projected
    return {
        "trust_type": "untrusted_user_confirmed_ai_estimate",
        "schema_version": "",
        "integrity_status": "integrity_verification_failed",
    }


def insert_user_confirmed_meal_photo_log(
    conn: sqlite3.Connection, *, token: str, user_id: str, source_message_id: str,
    confirmation_event_id: str, consumed_at: str, meal_slot: str, source_image_ref: str,
    observed_payload: Mapping[str, Any], answers: Mapping[str, Any], estimate: Mapping[str, Any],
) -> dict[str, Any]:
    """Create one canonical meal occurrence in the caller-owned confirmation transaction."""
    if not meal_photo_estimate_snapshot_is_valid(dict(estimate or {})):
        raise ValueError("餐點照片估算完整性驗證失敗")
    if not token or not user_id or not source_message_id or not confirmation_event_id:
        raise ValueError("餐點照片確認來源不完整")
    now = utcish_now()
    identity = json.dumps({
        "source_type": "meal_photo", "token": token, "user_id": user_id,
        "observed_payload": dict(observed_payload or {}), "answers": dict(answers or {}),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    names = [str(item.get("name") or "").strip() for item in
             list((observed_payload or {}).get("visible_items") or [])[:4]
             if isinstance(item, Mapping) and str(item.get("name") or "").strip()]
    product_name = ("餐點照片" + ("：" + "、".join(names) if names else ""))[:160]
    food_id, log_id = new_id("food"), new_id("log")
    snapshot = dict(estimate)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
    is_ai_nutrition = snapshot.get("rule_version") == "ai-vision-nutrition-estimate-v1"
    nutrition_snapshot = (
        {"calories_kcal": float(snapshot["calories_kcal"]), "protein_g": float(snapshot["protein_g"])}
        if is_ai_nutrition else {}
    )
    nutrition_snapshot_json = json.dumps(
        nutrition_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
            exchange_json,exchange_review_status,fingerprint,original_image_ref,
            recognition_confidence,verification_status,created_at,updated_at)
           VALUES (?,?, '', '', 'user_meal_photo',?,'private',1,'meal',1,'{}','{}',
                   ?,'user_confirmed_ai_estimate',?,?,0,'user_confirmed_ai_estimate',?,?)""",
        (food_id, product_name, user_id, snapshot_json, fingerprint,
         str(source_image_ref or "")[:240], now, now),
    )
    trust_payload = {
        "schema_version": (
            "meal-photo-user-confirmation-v2" if is_ai_nutrition
            else "meal-photo-user-confirmation-v1"
        ), "log_id": log_id,
        "food_id": food_id, "user_id": user_id, "draft_token": token,
        "source_message_id": source_message_id, "confirmation_event_id": confirmation_event_id,
        "consumed_at": str(consumed_at or now)[:50], "meal_slot": str(meal_slot or "")[:30],
        "consumed_servings": 1, "food_fingerprint": fingerprint,
        "exchange_snapshot": snapshot, "estimate_rule_version": snapshot["rule_version"],
        "confirmed_at": now,
    }
    if is_ai_nutrition:
        trust_payload["nutrition_snapshot"] = nutrition_snapshot
    conn.execute(
        """INSERT INTO food_logs
           (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,consumed_amount,
            consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,approved_exchange_json,
            exchange_approval_id,source_image_ref,plan_id,plan_link_status,confirmation_status,
            legacy_applied_at,created_at,updated_at,trust_type,trust_payload_json,trust_hash)
           VALUES (?,?,?,?,?,1,1,'meal',?,?,'{}','',?,'','pending','confirmed',
                   'not_applicable',?,?,'user_confirmed_ai_estimate',?,?)""",
        (log_id, user_id, food_id, trust_payload["consumed_at"], trust_payload["meal_slot"],
         nutrition_snapshot_json, snapshot_json, str(source_image_ref or "")[:240], now, now,
         json.dumps(trust_payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
         _canonical_json_hash(trust_payload)),
    )
    for entity_type, entity_id in (("food", food_id), ("food_log", log_id)):
        conn.execute(
            """INSERT OR IGNORE INTO nutrition_sheet_outbox
               (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
               VALUES (?,?,?,'pending',0,'',?,'')""",
            (new_id("outbox"), entity_type, entity_id, now),
        )
    return {"food_id": food_id, "log_id": log_id, "product_name": product_name,
            "estimate": snapshot, "trust_type": "user_confirmed_ai_estimate"}


def _confirmed_result(conn: sqlite3.Connection, log_id: str, *, already_confirmed: bool) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT f.food_id, f.product_name, f.brand, f.source_type,
               l.log_id, l.consumed_at, l.meal_slot, l.consumed_servings,
               l.consumed_amount, l.consumed_unit, l.nutrition_snapshot_json,
               l.exchange_snapshot_json, l.approved_exchange_json,
               l.exchange_approval_id, l.plan_id,
               a.food_fingerprint, a.suggestion_rule_version,
               a.approved_exchange_json, a.approved_exchange_hash, f.fingerprint,
               l.trust_type,l.trust_hash,f.owner_user_id,a.food_id,l.user_id,l.food_id
        FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
        LEFT JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
        WHERE l.log_id=?
        """,
        (log_id,),
    ).fetchone()
    if not row:
        raise ValueError("已確認紀錄遺失，請聯繫客服")
    suggested = _json_object_or_none(row[11]) or {}
    candidate_approval_id = row[13] or ""
    approval = verified_exchange_approval_projection(
        log_user_id=row[24], log_food_id=row[25],
        catalog_source_type=row[3], catalog_owner_user_id=row[22],
        catalog_fingerprint=row[19], consumed_servings=row[7],
        applied_json=row[12], approval_id=candidate_approval_id,
        approval_food_id=row[23], approval_fingerprint=row[15],
        rule_version=row[16], approved_json=row[17], approval_hash=row[18],
    )
    applied = approval["applied"] if approval["is_valid"] else {}
    approval_id = candidate_approval_id if approval["is_valid"] else ""
    nutrition = _json_object_or_none(row[10]) or {}
    is_meal_photo_origin = bool(
        row[3] == "user_meal_photo" or approval["is_meal_photo_origin"]
    )
    if is_meal_photo_origin:
        nutrition = estimate_nutrition_from_exchanges(applied) if applied else {}
    trust = user_confirmed_meal_photo_trust_projection(
        conn, str(row[4] or ""), str(row[20] or "")
    )
    if (
        trust["integrity_status"] == "verified"
        and suggested.get("rule_version") == "ai-vision-nutrition-estimate-v1"
    ):
        nutrition = _json_object_or_none(row[10]) or {}
    if trust["integrity_status"] == "integrity_verification_failed":
        suggested = {}
    approval_integrity_failed = bool(
        is_meal_photo_origin and candidate_approval_id and not approval["is_valid"]
    )
    if approval_integrity_failed:
        suggested = {}
    integrity_status = (
        "integrity_verification_failed"
        if approval_integrity_failed else trust["integrity_status"]
    )
    return {
        "already_confirmed": already_confirmed,
        "food": {"food_id": row[0], "product_name": row[1], "brand": row[2], "source_type": row[3]},
        "log": {
            "log_id": row[4], "consumed_at": row[5], "meal_slot": row[6],
            "consumed_servings": float(row[7]), "consumed_amount": float(row[8]),
            "consumed_unit": row[9], "nutrition": nutrition,
            "suggested_exchange": suggested,
            "approved_exchange": applied,
            "exchange": applied or suggested,
            "exchange_review_status": (
                "approved" if applied else
                "integrity_verification_failed" if approval_integrity_failed else
                trust["trust_type"] if trust["trust_type"] else "pending_review"
            ),
            "exchange_approval_id": approval_id,
            "plan_id": row[14] or "",
            "trust_type": trust["trust_type"],
            "trust_schema_version": trust["schema_version"],
            "trust_integrity_status": integrity_status,
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
                candidate = _json_object_or_none(existing[6])
                expected_hash = ""
                if candidate is not None:
                    expected_hash = exchange_approval_hash(fingerprint, existing[5], candidate)
                if (
                    candidate is not None
                    and exchange_approval_payload_is_valid(existing[5], candidate)
                    and secrets.compare_digest(str(existing[7] or ""), expected_hash)
                ):
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
    estimate: Mapping[str, Any] | None = None,
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
        key: round(_strict_json_number(exact_exchange.get(key), key, max_value=100), 4)
        for key in EXCHANGE_KEYS
    }
    if values["fat_exchange"] != 0:
        raise ValueError("目前規則不計油脂交換份")
    protein_items: list[dict[str, Any]] = []
    protein_total_exchange: dict[str, Any] | None = None
    raw_protein_items = (estimate or {}).get("protein_items")
    raw_protein_total = (estimate or {}).get("protein_total_exchange")
    raw_answer_items = (answers or {}).get("protein_items")
    if raw_answer_items is None:
        raw_answer_items = []
    if not isinstance(raw_answer_items, list):
        raise ValueError("複數蛋白質明細無效")
    valid_types = {"chicken", "pork", "fish", "egg", "tofu", "other", "unknown"}
    portion_ranges = {
        "half_palm": (1.0, 2.0), "one_palm": (2.0, 3.0),
        "one_half_palm": (3.0, 5.0), "two_palm": (4.0, 6.0),
    }
    top_protein_type = str((answers or {}).get("protein_type") or "")
    top_protein_portion = str((answers or {}).get("protein_portion") or "")
    has_top_protein_answer = bool(
        "protein_type" in (answers or {}) or "protein_portion" in (answers or {})
    )
    if has_top_protein_answer or raw_answer_items:
        if top_protein_type == "none":
            if top_protein_portion or raw_answer_items:
                raise ValueError("蛋白質明細與主要回答不符")
        elif (
            top_protein_type not in valid_types
            or top_protein_portion not in {*portion_ranges, "none", "unknown"}
        ):
            raise ValueError("蛋白質主要回答無效")
        elif raw_answer_items:
            first_answer_item = raw_answer_items[0]
            if not isinstance(first_answer_item, Mapping) or (
                str(first_answer_item.get("type") or "") != top_protein_type
                or str(first_answer_item.get("portion") or "") != top_protein_portion
            ):
                raise ValueError("蛋白質明細與主要回答不符")
    estimate_rule_version = str((estimate or {}).get("rule_version") or "")
    total_is_v2 = bool(
        isinstance(raw_protein_total, Mapping)
        and raw_protein_total.get("basis") == "summed_hand_portion_ranges_v2"
    )
    v2_declared = bool(
        len(raw_answer_items) >= 2
        or raw_protein_items is not None
        or estimate_rule_version == "hand-portion-range-v2"
        or total_is_v2
    )
    if v2_declared and not (
        2 <= len(raw_answer_items) <= 4
        and isinstance(raw_protein_items, list)
        and 2 <= len(raw_protein_items) <= 4
        and estimate_rule_version == "hand-portion-range-v2"
        and total_is_v2
    ):
        raise ValueError("複數蛋白質明細無效")
    if has_top_protein_answer and not v2_declared:
        if top_protein_type == "none" or top_protein_portion == "none":
            expected_total = (0.0, 0.0, "user_confirmed_none")
        elif top_protein_portion == "unknown":
            raise ValueError("蛋白質份量仍為NA")
        else:
            expected_range = portion_ranges[top_protein_portion]
            expected_total = (*expected_range, "hand_portion_range_v1")
        if not isinstance(raw_protein_total, Mapping):
            raise ValueError("蛋白質合計無效")
        actual_total = (
            _strict_json_number(raw_protein_total.get("min"), "protein_total.min", max_value=100),
            _strict_json_number(raw_protein_total.get("max"), "protein_total.max", max_value=100),
            raw_protein_total.get("basis"),
        )
        if actual_total != expected_total:
            raise ValueError("蛋白質合計與主要回答不符")
    if raw_protein_items is not None:
        if not isinstance(raw_protein_items, list) or not 2 <= len(raw_protein_items) <= 4:
            raise ValueError("複數蛋白質明細無效")
        seen_types: set[str] = set()
        for raw_item in raw_protein_items:
            if not isinstance(raw_item, Mapping):
                raise ValueError("複數蛋白質明細無效")
            protein_type = str(raw_item.get("type") or "")
            portion = str(raw_item.get("portion") or "")
            exchange = raw_item.get("exchange")
            expected_range = portion_ranges.get(portion)
            if protein_type not in valid_types or expected_range is None or not isinstance(exchange, Mapping):
                raise ValueError("複數蛋白質明細無效")
            if protein_type in seen_types:
                raise ValueError("複數蛋白質種類重複")
            seen_types.add(protein_type)
            item_min = _strict_json_number(exchange.get("min"), "protein_item.min", max_value=100)
            item_max = _strict_json_number(exchange.get("max"), "protein_item.max", max_value=100)
            if (
                (item_min, item_max) != expected_range
                or exchange.get("basis") != "hand_portion_range_v1"
            ):
                raise ValueError("複數蛋白質明細與份量不符")
            protein_items.append({
                "type": protein_type, "portion": portion,
                "exchange": {
                    "min": item_min, "max": item_max,
                    "basis": "hand_portion_range_v1",
                },
            })
        answer_items = [
            {"type": str(item.get("type") or ""), "portion": str(item.get("portion") or "")}
            for item in raw_answer_items
            if isinstance(item, Mapping)
        ]
        if answer_items != [
            {"type": item["type"], "portion": item["portion"]} for item in protein_items
        ]:
            raise ValueError("複數蛋白質明細與確認答案不符")
        raw_total = (estimate or {}).get("protein_total_exchange")
        if not isinstance(raw_total, Mapping):
            raise ValueError("複數蛋白質合計無效")
        total_min = _strict_json_number(raw_total.get("min"), "protein_total.min", max_value=100)
        total_max = _strict_json_number(raw_total.get("max"), "protein_total.max", max_value=100)
        if (
            raw_total.get("basis") != "summed_hand_portion_ranges_v2"
            or total_min != sum(item["exchange"]["min"] for item in protein_items)
            or total_max != sum(item["exchange"]["max"] for item in protein_items)
        ):
            raise ValueError("複數蛋白質合計與明細不符")
        protein_total_exchange = {
            "min": total_min, "max": total_max,
            "basis": "summed_hand_portion_ranges_v2",
        }
    estimated_nutrition = estimate_nutrition_from_exchanges(values)
    estimated_nutrition_json = json.dumps(
        estimated_nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    categories = [key for key in EXCHANGE_KEYS if values[key] > 0]
    rule_version = "meal-photo-admin-v2"
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
    if protein_items:
        approved_payload["protein_items"] = protein_items
        approved_payload["protein_total_exchange"] = protein_total_exchange
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
    if servings < 0.1 or servings > 100:
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
        escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_query}%"
        rows = conn.execute(
            """SELECT food_id,product_name,brand,barcode,source_type,owner_user_id,
                      package_amount,package_unit,servings_per_package,
                      per_serving_json,exchange_json,exchange_review_status,
                      created_at,updated_at
               FROM food_catalog
               WHERE (owner_user_id=? OR visibility='public')
                 AND product_name LIKE ? ESCAPE '\\'
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
    menu_category: str = "", limit: int = 11, offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """以單一SQL分頁搜尋，歷史常吃優先；可按菜單分類篩選。"""
    user_id = str(user_id or "").strip()
    query = str(query or "").strip()
    menu_category = str(menu_category or "").strip()
    if menu_category not in {"", "main", "side", "drink"}:
        return [], False
    if not user_id or (not query and not menu_category):
        return [], False
    limit = max(1, min(int(limit or 11), 50))
    offset = max(0, min(int(offset or 0), 10000))
    category_clause = "f.menu_category=?" if menu_category else "f.product_name LIKE ?"
    category_value = menu_category if menu_category else f"%{query}%"
    rows = conn.execute(
        f"""SELECT f.food_id,f.product_name,f.brand,f.barcode,f.source_type,f.owner_user_id,
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
             AND {category_clause}
           GROUP BY f.food_id
           ORDER BY CASE WHEN COUNT(l.log_id)>0 THEN 0 ELSE 1 END,
                    use_count DESC,last_consumed_at DESC,f.updated_at DESC,f.food_id
           LIMIT ? OFFSET ?""",
        (user_id, user_id, category_value, limit + 1, offset),
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
            parsed_approved_payload = _json_object_or_none(approval[2])
            if parsed_approved_payload is None:
                raise ValueError("核准紀錄驗證失敗，已停止套用")
            approved_payload = parsed_approved_payload
            expected_hash = exchange_approval_hash(approval[4], approval[1], approved_payload)
            if (
                approval[4] != fingerprint
                or not exchange_approval_payload_is_valid(approval[1], approved_payload)
                or not secrets.compare_digest(approval[3], expected_hash)
            ):
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
    sql = f"""
        SELECT l.nutrition_snapshot_json,l.approved_exchange_json,l.exchange_approval_id,
               l.consumed_servings,
               a.food_fingerprint,a.suggestion_rule_version,a.approved_exchange_json,
               a.approved_exchange_hash,f.fingerprint,f.source_type,l.log_id,l.trust_type,
               l.user_id,l.food_id,f.owner_user_id,a.food_id
        FROM food_logs l
        JOIN food_catalog f ON f.food_id=l.food_id
        LEFT JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
        WHERE l.user_id=? AND date({_TAIPEI_LOCAL_CONSUMED_AT_SQL})=?
          AND l.confirmation_status='confirmed'
    """
    params: list[Any] = [user_id, date_iso]
    if meal_slot:
        sql += " AND l.meal_slot=?"
        params.append(meal_slot)
    rows = conn.execute(sql, params).fetchall()
    for (
        nutrition_json, applied_json, approval_id, consumed_servings,
        approval_fingerprint, rule_version, approved_json, approval_hash,
        food_fp, source_type, log_id, trust_type, log_user_id, log_food_id,
        catalog_owner_user_id, approval_food_id,
    ) in rows:
        trust = user_confirmed_meal_photo_trust_projection(conn, log_id, trust_type)
        approval = verified_exchange_approval_projection(
            log_user_id=log_user_id,
            log_food_id=log_food_id,
            catalog_source_type=source_type,
            catalog_owner_user_id=catalog_owner_user_id,
            catalog_fingerprint=food_fp,
            consumed_servings=consumed_servings,
            applied_json=applied_json,
            approval_id=approval_id,
            approval_food_id=approval_food_id,
            approval_fingerprint=approval_fingerprint,
            rule_version=rule_version,
            approved_json=approved_json,
            approval_hash=approval_hash,
        )
        is_meal_photo_origin = bool(
            approval["is_meal_photo_origin"]
            or trust.get("integrity_status") in {"verified", "integrity_verification_failed"}
            or trust.get("trust_type")
        )
        if not is_meal_photo_origin:
            nutrition_data = _json_object_or_none(nutrition_json) or {}
            for key, value in nutrition_data.items():
                if key in totals:
                    totals[key] += float(value or 0)
        if not approval["is_valid"]:
            continue
        for key in EXCHANGE_KEYS:
            totals[key] += float(approval["applied"].get(key, 0) or 0)
        if is_meal_photo_origin:
            estimated = estimate_nutrition_from_exchanges(approval["applied"])
            for key, value in estimated.items():
                if key in totals:
                    totals[key] += float(value or 0)
    return {key: round(value, 4) for key, value in totals.items()}


def daily_food_summary(
    conn: sqlite3.Connection, *, user_id: str, date_iso: str
) -> dict[str, Any]:
    """Return confirmed food details plus verified totals for one local consumption date."""
    rows = conn.execute(
        f"""
        SELECT l.consumed_at,f.product_name,l.nutrition_snapshot_json,
               l.exchange_approval_id,a.food_fingerprint,a.suggestion_rule_version,
               a.approved_exchange_json,a.approved_exchange_hash,f.fingerprint,
               l.approved_exchange_json,l.consumed_servings,f.source_type,
               l.log_id,l.trust_type,l.exchange_snapshot_json,
               l.user_id,l.food_id,f.owner_user_id,a.food_id
        FROM food_logs l
        JOIN food_catalog f ON f.food_id=l.food_id
        LEFT JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
        WHERE l.user_id=? AND date({_TAIPEI_LOCAL_CONSUMED_AT_SQL})=?
          AND l.confirmation_status='confirmed'
        ORDER BY {_TAIPEI_LOCAL_CONSUMED_AT_SQL},l.log_id
        """,
        (user_id, date_iso),
    ).fetchall()
    foods: list[dict[str, Any]] = []
    pending_reviews = 0
    estimated_totals = {"calories_kcal": 0.0, "protein_g": 0.0}
    for (
        consumed_at, product_name, nutrition_json, approval_id, approval_fingerprint,
        rule_version, approved_json, approval_hash, food_fingerprint_value,
        applied_json, consumed_servings, source_type, log_id, trust_type, estimate_json,
        log_user_id, log_food_id, catalog_owner_user_id, approval_food_id,
    ) in rows:
        nutrition = _json_object_or_none(nutrition_json) or {}
        try:
            consumed_dt = datetime.fromisoformat(str(consumed_at))
            if consumed_dt.tzinfo is None:
                consumed_dt = consumed_dt.replace(tzinfo=timezone(timedelta(hours=8)))
            else:
                consumed_dt = consumed_dt.astimezone(timezone(timedelta(hours=8)))
            consumed_time = consumed_dt.strftime("%H:%M")
        except ValueError:
            consumed_time = str(consumed_at)[11:16] if len(str(consumed_at)) >= 16 else "--:--"
        approval = verified_exchange_approval_projection(
            log_user_id=log_user_id,
            log_food_id=log_food_id,
            catalog_source_type=source_type,
            catalog_owner_user_id=catalog_owner_user_id,
            catalog_fingerprint=food_fingerprint_value,
            consumed_servings=consumed_servings,
            applied_json=applied_json,
            approval_id=approval_id,
            approval_food_id=approval_food_id,
            approval_fingerprint=approval_fingerprint,
            rule_version=rule_version,
            approved_json=approved_json,
            approval_hash=approval_hash,
        )
        valid_approval = bool(approval["is_valid"])
        trust = user_confirmed_meal_photo_trust_projection(conn, log_id, trust_type)
        valid_user_estimate = trust["integrity_status"] == "verified"
        is_meal_photo_origin = bool(
            approval["is_meal_photo_origin"]
            or source_type == "user_meal_photo"
            or trust.get("integrity_status") in {"verified", "integrity_verification_failed"}
            or trust.get("trust_type")
        )
        approval_integrity_failed = bool(
            is_meal_photo_origin and approval_id and not valid_approval
        )
        if (
            not valid_approval and not valid_user_estimate and not trust["trust_type"]
            and not approval_integrity_failed
        ):
            pending_reviews += 1
        if is_meal_photo_origin:
            if valid_approval:
                nutrition = estimate_nutrition_from_exchanges(approval["applied"])
            elif valid_user_estimate:
                nutrition = dict(trust.get("nutrition") or {})
            else:
                nutrition = {}
        valid_ai_nutrition = (
            valid_user_estimate
            and is_meal_photo_origin
            and nutrition.get("calories_kcal") is not None
            and nutrition.get("protein_g") is not None
        )
        integrity_status = (
            "integrity_verification_failed"
            if approval_integrity_failed else trust["integrity_status"]
        )
        if valid_ai_nutrition:
            estimated_totals["calories_kcal"] += float(nutrition["calories_kcal"])
            estimated_totals["protein_g"] += float(nutrition["protein_g"])
        foods.append(
            {
                "time": consumed_time,
                "consumed_at": consumed_at,
                "name": product_name,
                "calories_kcal": (
                    float(nutrition["calories_kcal"])
                    if (valid_ai_nutrition or valid_approval)
                    and nutrition.get("calories_kcal") is not None else
                    (None if is_meal_photo_origin else float(nutrition.get("calories_kcal", 0) or 0))
                ),
                "protein_g": (
                    float(nutrition["protein_g"])
                    if (valid_ai_nutrition or valid_approval)
                    and nutrition.get("protein_g") is not None else
                    (None if is_meal_photo_origin else float(nutrition.get("protein_g", 0) or 0))
                ),
                "trust_type": trust["trust_type"],
                "trust_integrity_status": integrity_status,
                "estimate": (
                    _json_object_or_none(estimate_json) if valid_user_estimate else None
                ),
            }
        )
    return {
        "foods": foods,
        "totals": daily_consumed_totals(conn, user_id=user_id, date_iso=date_iso),
        "estimated_totals": {key: round(value, 4) for key, value in estimated_totals.items()},
        "pending_reviews": pending_reviews,
    }


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcish_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
