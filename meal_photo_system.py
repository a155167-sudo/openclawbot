"""無營養標示餐點照片的安全草稿、確認與估算呈現。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from nutrition_system import (
    _confirmed_result,
    ensure_nutrition_schema,
    insert_approved_meal_photo_log,
    insert_user_confirmed_meal_photo_log,
    meal_photo_estimate_snapshot_is_valid,
    user_confirmed_meal_photo_estimate_is_valid,
    user_confirmed_meal_photo_trust_projection,
)
from nutrition_estimate_checks import assess_nutrition


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
VISIBLE_CATEGORIES = {"vegetable", "protein", "starch", "fruit", "milk", "unknown"}
STARCH_VISIBILITY = {"visible", "not_visible", "unknown"}
OIL_SAUCE_STATUS = {"visible", "not_visible", "unknown"}
ANSWER_VALUES = {
    "scope": {"visible_only", "has_unseen", "unknown"},
    "protein_type": {"chicken", "pork", "fish", "egg", "tofu", "other", "none", "unknown"},
    "protein_portion": {"half_palm", "one_palm", "one_half_palm", "two_palm", "none", "unknown"},
    "protein_more": {"done", "add"},
    "protein_extra_type": {"chicken", "pork", "fish", "egg", "tofu", "other", "unknown"},
    "protein_extra_portion": {"half_palm", "one_palm", "one_half_palm", "two_palm", "unknown"},
    "starch_portion": {"none", "half_bowl", "one_bowl", "one_half_bowl", "two_bowl", "unseen_unknown", "unknown"},
    "vegetable_portion": {"none", "half_bowl", "one_bowl", "one_half_bowl", "two_bowl", "three_bowl", "unknown"},
    "cooking_oil": {"none", "light", "normal", "heavy", "unknown"},
    "sauce_level": {"none", "little", "half", "all", "unknown"},
}


def _confidence(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必須是數字") from exc
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError(f"{field} 必須介於0到1")
    return number


def _short_text(value: Any, field: str, *, maximum: int = 120, required: bool = True) -> str:
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ValueError(f"{field} 不可空白")
    if len(text) > maximum:
        raise ValueError(f"{field} 過長")
    return text


def _estimate_number(value: Any, field: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必須是數字")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise ValueError(f"{field} 超出合理範圍")
    return number


def _validate_provided_nutrition(calories_kcal: Any, protein_g: Any) -> None:
    assessment = assess_nutrition({
        "calories_kcal": calories_kcal,
        "protein_g": protein_g,
        "fat_g": None,
        "carbohydrate_g": None,
    })
    if assessment.get("requires_correction"):
        reason = str(assessment.get("reason") or "已提供的營養數值不合理")
        raise ValueError(f"AI營養估算合理性核對失敗：{reason}")


def _normalize_ai_estimate(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("AI餐點估算格式錯誤")
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 12:
        raise ValueError("AI餐點估算食材必須是1至12項")
    items = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise ValueError("AI餐點估算食材格式錯誤")
        item_id = str(item.get("item_id") or "").strip()
        if not re.fullmatch(r"item_[0-9a-f]{12}", item_id):
            seed = json.dumps(
                {"index": index, "name": item.get("name"), "portion": item.get("portion")},
                ensure_ascii=False, sort_keys=True,
            )
            item_id = "item_" + hashlib.sha256(seed.encode()).hexdigest()[:12]
        normalized_item = {
            "item_id": item_id,
            "name": _short_text(item.get("name"), "估算食材名稱", maximum=60),
            "portion": _short_text(item.get("portion"), "估算份量", maximum=40),
            "calories_kcal": _estimate_number(item.get("calories_kcal"), "食材熱量", 3000),
            "protein_g": _estimate_number(item.get("protein_g"), "食材蛋白質", 300),
        }
        for field, limit in (("calories_kcal", 3000), ("protein_g", 300)):
            range_value = item.get(f"{field}_range")
            if range_value is not None:
                if not isinstance(range_value, Mapping) or set(range_value) != {"min", "max"}:
                    raise ValueError(f"食材{field}區間格式錯誤")
                minimum = _estimate_number(range_value["min"], f"食材{field}下限", limit)
                maximum = _estimate_number(range_value["max"], f"食材{field}上限", limit)
                if not minimum <= normalized_item[field] <= maximum:
                    raise ValueError(f"食材{field}估算值不在區間內")
                normalized_item[f"{field}_range"] = {"min": minimum, "max": maximum}
        if item.get("nutrition_source") is not None:
            source = _short_text(item.get("nutrition_source"), "食材營養來源", maximum=40)
            if source not in {"ai_text_estimate", "owner_private_catalog", "user_provided"}:
                raise ValueError("食材營養來源不支援")
            normalized_item["nutrition_source"] = source
        items.append(normalized_item)
    if len({item["item_id"] for item in items}) != len(items):
        raise ValueError("AI餐點估算食材識別碼重複")
    totals = {}
    for field, limit in (("calories_kcal", 5000), ("protein_g", 500)):
        raw = value.get(field)
        if not isinstance(raw, Mapping) or set(raw) != {"estimate", "min", "max"}:
            raise ValueError(f"{field} 估算區間格式錯誤")
        estimate = _estimate_number(raw["estimate"], field, limit)
        minimum = _estimate_number(raw["min"], f"{field}下限", limit)
        maximum = _estimate_number(raw["max"], f"{field}上限", limit)
        if minimum > estimate or estimate > maximum or minimum == maximum:
            raise ValueError(f"{field} 估算值必須落在非零寬度區間內")
        totals[field] = {"estimate": estimate, "min": minimum, "max": maximum}
    _validate_provided_nutrition(
        totals["calories_kcal"]["estimate"], totals["protein_g"]["estimate"]
    )
    provenance = value.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "provider", "model", "method", "nutrition_basis"
    }:
        raise ValueError("AI餐點估算來源不完整")
    normalized_provenance = {
        key: _short_text(provenance.get(key), "AI估算來源", maximum=80)
        for key in ("provider", "model", "method", "nutrition_basis")
    }
    if normalized_provenance["method"] != "vision_model_estimate" or normalized_provenance["nutrition_basis"] != "unlabeled_meal_photo":
        raise ValueError("AI餐點估算來源不支援")
    normalized_estimate = {
        "items": items, **totals,
        "confidence": _confidence(value.get("confidence"), "AI餐點估算信心"),
        "provenance": normalized_provenance,
    }
    # This marker is only written by the local item/aggregate reconciliation
    # helper.  Older provider estimates intentionally remain unmarked so their
    # historical aggregate semantics are not reinterpreted application-wide.
    if value.get("reconciliation") == "items_sum_v1":
        normalized_estimate["reconciliation"] = "items_sum_v1"
    return normalized_estimate


def _ai_estimate_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    ai = _normalize_ai_estimate(value)
    return {
        "calories_kcal": ai["calories_kcal"]["estimate"],
        "protein_g": ai["protein_g"]["estimate"],
        "fat_g": None, "carbohydrate_g": None,
        "calories_kcal_range": {"min": ai["calories_kcal"]["min"], "max": ai["calories_kcal"]["max"], "basis": "ai_vision_estimate_range_v1"},
        "protein_g_range": {"min": ai["protein_g"]["min"], "max": ai["protein_g"]["max"], "basis": "ai_vision_estimate_range_v1"},
        "estimate_items": ai["items"], "estimate_confidence": ai["confidence"],
        "provenance": ai["provenance"],
        "protein_total_exchange": None, "starch_exchange": None, "vegetable_exchange": None,
        "cooking_oil_confirmation": "unknown", "sauce_confirmation": "unknown",
        "formal_status": "user_confirmed_ai_estimate_not_approved",
        "rule_version": "ai-vision-nutrition-estimate-v1",
    }


def validate_declared_item_sum_contract(payload: Mapping[str, Any]) -> None:
    """Reject a raw ``items_sum_v1`` claim before normalization can rewrite it."""
    ai = payload.get("ai_estimate") if isinstance(payload, Mapping) else None
    if not isinstance(ai, Mapping) or ai.get("reconciliation") != "items_sum_v1":
        return
    raw_items = ai.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("餐點照片明細與總營養不一致，請重新估算")
    for field in ("calories_kcal", "protein_g"):
        aggregate = ai.get(field)
        try:
            item_values = [item[field] for item in raw_items]
            if any(isinstance(value, bool) for value in item_values):
                raise TypeError
            item_sum = math.fsum(float(value) for value in item_values)
            aggregate_point = float(aggregate["estimate"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("餐點照片明細與總營養不一致，請重新估算") from None
        if not (
            math.isfinite(item_sum)
            and math.isfinite(aggregate_point)
            and math.isclose(item_sum, aggregate_point, rel_tol=1e-9, abs_tol=1e-9)
        ):
            raise ValueError("餐點照片明細與總營養不一致，請重新估算")


def _trusted_item_interval(item: Mapping[str, Any], field: str) -> tuple[float, float] | None:
    range_value = item.get(f"{field}_range")
    if isinstance(range_value, Mapping):
        return float(range_value["min"]), float(range_value["max"])
    # A user/catalog point is an explicit value, unlike a rounded vision detail.
    if item.get("nutrition_source") in {"user_provided", "owner_private_catalog"}:
        point = float(item[field])
        return point, point
    return None


def _reconcile_ai_estimate_items(
    payload: Mapping[str, Any], reconciled_items: list[Mapping[str, Any]],
    *, provider_aggregate_authoritative: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconcile edited details and aggregate intervals without inventing precision.

    Point totals normally become sums of all surviving item points.  A manual
    addition to an older provider estimate is the exception: provider aggregate
    and rounded item details were independent representations, so add the new
    item's point and validated interval directly to the prior aggregate.  Such
    an aggregate-authoritative result is intentionally not marked ``items_sum_v1``.

    If every survivor has a trustworthy item range, aggregate bounds are sums of
    those bounds.  Otherwise transfer the provider aggregate's *absolute additive
    uncertainty* around its original detail sum.  The lower bound is intersected
    with nutrition's non-negative domain.  Transfer is rejected when the original
    interval did not contain its own detail sum, or when truncation would produce
    no interval.
    """
    validate_declared_item_sum_contract(payload)
    normalized = normalize_meal_photo_payload(payload)
    ai = dict(normalized.get("ai_estimate") or {})
    original_items = [dict(item) for item in ai.get("items") or []]
    items = [dict(item) for item in reconciled_items]
    if not items:
        raise ValueError("至少要保留一項食材；空餐不能確認記錄")
    ai["items"] = items
    declared_item_sum = ai.get("reconciliation") == "items_sum_v1"

    original_by_id = {item["item_id"]: item for item in original_items}
    next_by_id = {item["item_id"]: item for item in items}
    for field in ("calories_kcal", "protein_g"):
        original_item_sum = math.fsum(float(item[field]) for item in original_items)
        next_item_sum = math.fsum(float(item[field]) for item in items)
        original = dict(ai[field])
        item_delta = next_item_sum - original_item_sum
        aggregate_delta_edit = provider_aggregate_authoritative
        estimate = (
            float(original["estimate"]) + item_delta
            if aggregate_delta_edit else next_item_sum
        )
        range_key = f"{field}_range"
        if not aggregate_delta_edit and all(range_key in item for item in items):
            minimum = sum(float(item[range_key]["min"]) for item in items)
            maximum = sum(float(item[range_key]["max"]) for item in items)
        elif aggregate_delta_edit:
            if math.isclose(item_delta, 0.0, rel_tol=1e-9, abs_tol=1e-9):
                # A representation-only update must not collapse an independent
                # provider aggregate to the rounded detail sum.
                estimate = float(original["estimate"])
                minimum = float(original["min"])
                maximum = float(original["max"])
            else:
                removed = []
                added = []
                for item_id in set(original_by_id) | set(next_by_id):
                    old_item = original_by_id.get(item_id)
                    new_item = next_by_id.get(item_id)
                    if old_item is not None and new_item is not None and (
                        float(old_item[field]) == float(new_item[field])
                        and old_item.get(range_key) == new_item.get(range_key)
                    ):
                        continue
                    if old_item is not None:
                        removed.append(old_item)
                    if new_item is not None:
                        added.append(new_item)
                removed_ranges = [_trusted_item_interval(item, field) for item in removed]
                added_ranges = [_trusted_item_interval(item, field) for item in added]
                if any(value is None for value in removed_ranges + added_ranges):
                    raise ValueError("食材更新後的估算區間無法確定，請重新估算")
                delta_min = (
                    sum(value[0] for value in added_ranges)
                    - sum(value[1] for value in removed_ranges)
                )
                delta_max = (
                    sum(value[1] for value in added_ranges)
                    - sum(value[0] for value in removed_ranges)
                )
                minimum = max(0.0, float(original["min"]) + delta_min)
                maximum = float(original["max"]) + delta_max
        else:
            if not float(original["min"]) <= original_item_sum <= float(original["max"]):
                raise ValueError("食材更新前的估算區間與明細不一致，請重新估算")
            minimum = max(0.0, estimate + float(original["min"]) - original_item_sum)
            maximum = estimate + float(original["max"]) - original_item_sum
        if not (minimum <= estimate <= maximum) or minimum == maximum:
            raise ValueError("食材更新後的估算區間無法確定，請重新估算")
        ai[field] = {"estimate": estimate, "min": minimum, "max": maximum}
    if declared_item_sum or not provider_aggregate_authoritative:
        ai["reconciliation"] = "items_sum_v1"
    else:
        ai.pop("reconciliation", None)
    normalized["ai_estimate"] = ai
    normalized = normalize_meal_photo_payload(normalized)
    return normalized, _ai_estimate_snapshot(normalized["ai_estimate"])


def _mutate_ai_estimate_items(
    payload: Mapping[str, Any], *, remove_item_id: str = "",
    add_item: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Edit items, then use the shared detail/aggregate reconciliation policy."""
    normalized = normalize_meal_photo_payload(payload)
    items = [dict(item) for item in normalized["ai_estimate"]["items"]]
    if remove_item_id:
        index = next((i for i, item in enumerate(items) if item["item_id"] == remove_item_id), None)
        if index is None:
            raise ValueError("找不到要移除的食材")
        if len(items) == 1:
            raise ValueError("至少要保留一項食材；空餐不能確認記錄")
        items.pop(index)
    elif add_item is not None:
        item = dict(add_item)
        if len(items) >= 12:
            raise ValueError("一筆餐點最多確認12項食材")
        if any(existing["item_id"] == item.get("item_id") for existing in items):
            raise ValueError("食材識別碼重複")
        items.append(item)
    else:
        raise ValueError("食材操作缺失")
    return _reconcile_ai_estimate_items(
        normalized, items, provider_aggregate_authoritative=True,
    )


def normalize_meal_photo_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """只保留照片可觀察資料；忽略模型自稱的營養數字與交換份。"""
    if not isinstance(payload, Mapping):
        raise ValueError("餐點照片資料格式錯誤")
    if payload.get("status") != "success" or payload.get("image_type") != "food_photo":
        raise ValueError(str(payload.get("message") or "不是有效的餐點照片資料"))

    raw_items = payload.get("visible_items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 12:
        raise ValueError("可見食物必須是1至12項清單")
    items = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise ValueError("可見食物格式錯誤")
        category = str(item.get("category") or "").strip()
        if category not in VISIBLE_CATEGORIES:
            raise ValueError("食物類別不支援")
        items.append(
            {
                "name": _short_text(item.get("name"), "食物名稱", maximum=60),
                "category": category,
                "confidence": _confidence(item.get("confidence"), "食物信心"),
            }
        )

    raw_uncertain = payload.get("uncertain_items", [])
    if not isinstance(raw_uncertain, list) or len(raw_uncertain) > 12:
        raise ValueError("不確定項目格式錯誤")
    uncertain = [
        _short_text(value, "不確定項目", maximum=100)
        for value in raw_uncertain
    ]

    starch_visibility = str(payload.get("starch_visibility") or "").strip()
    oil_sauce_status = str(payload.get("oil_sauce_status") or "").strip()
    if starch_visibility not in STARCH_VISIBILITY:
        raise ValueError("主食可見狀態不支援")
    if oil_sauce_status not in OIL_SAUCE_STATUS:
        raise ValueError("油醬狀態不支援")

    observed_confidence = payload.get("observed_at_confidence", 0)
    try:
        observed_confidence = _confidence(observed_confidence, "照片時間信心")
    except ValueError:
        observed_confidence = 0.0
    normalized = {
        "status": "success",
        "image_type": "food_photo",
        "visible_items": items,
        "uncertain_items": uncertain,
        "starch_visibility": starch_visibility,
        "oil_sauce_status": oil_sauce_status,
        "observed_at": _short_text(
            payload.get("observed_at"), "照片時間", maximum=40, required=False
        ),
        "observed_at_confidence": observed_confidence,
    }
    if "ai_estimate" in payload:
        normalized["ai_estimate"] = _normalize_ai_estimate(payload["ai_estimate"])
    return normalized


def _execute_sql_script_without_implicit_commit(
    conn: sqlite3.Connection, script: str
) -> None:
    """Execute complete statements without executescript's implicit COMMIT."""
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            conn.execute(statement)
            pending.clear()
    if "\n".join(pending).strip():
        raise sqlite3.OperationalError("incomplete meal-photo schema statement")


_CHOICE_TABLE = "meal_photo_ingredient_choices"
_CHOICE_OWNER_INDEX = "idx_meal_photo_ingredient_choices_owner"
_CHOICE_TABLE_SQL = """CREATE TABLE meal_photo_ingredient_choices (
    choice_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token TEXT NOT NULL,
    draft_version INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    parsed_items_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL DEFAULT '{}',
    collision_indexes_json TEXT NOT NULL,
    current_collision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, source_message_id),
    FOREIGN KEY(token) REFERENCES pending_meal_photo_drafts(token)
)"""
_CHOICE_OWNER_INDEX_SQL = """CREATE INDEX idx_meal_photo_ingredient_choices_owner
    ON meal_photo_ingredient_choices(user_id, token, status, created_at)"""
_CHOICE_COLUMNS = [
    (0, "choice_id", "TEXT", 0, None, 1, 0),
    (1, "user_id", "TEXT", 1, None, 0, 0),
    (2, "token", "TEXT", 1, None, 0, 0),
    (3, "draft_version", "INTEGER", 1, None, 0, 0),
    (4, "source_message_id", "TEXT", 1, None, 0, 0),
    (5, "parsed_items_json", "TEXT", 1, None, 0, 0),
    (6, "decisions_json", "TEXT", 1, "'{}'", 0, 0),
    (7, "collision_indexes_json", "TEXT", 1, None, 0, 0),
    (8, "current_collision", "INTEGER", 1, "0", 0, 0),
    (9, "status", "TEXT", 1, "'pending'", 0, 0),
    (10, "expires_at", "TEXT", 1, None, 0, 0),
    (11, "created_at", "TEXT", 1, None, 0, 0),
    (12, "updated_at", "TEXT", 1, None, 0, 0),
]
_CHOICE_FOREIGN_KEYS = [
    (0, 0, "pending_meal_photo_drafts", "token", "token", "NO ACTION", "NO ACTION", "NONE")
]
_CHOICE_INDEXES = {
    "sqlite_autoindex_meal_photo_ingredient_choices_1": (
        1, "pk", 0,
        [(0, 0, "choice_id", 0, "BINARY", 1), (1, -1, None, 0, "BINARY", 0)],
    ),
    "sqlite_autoindex_meal_photo_ingredient_choices_2": (
        1, "u", 0,
        [
            (0, 1, "user_id", 0, "BINARY", 1),
            (1, 4, "source_message_id", 0, "BINARY", 1),
            (2, -1, None, 0, "BINARY", 0),
        ],
    ),
    _CHOICE_OWNER_INDEX: (
        0, "c", 0,
        [
            (0, 1, "user_id", 0, "BINARY", 1),
            (1, 2, "token", 0, "BINARY", 1),
            (2, 9, "status", 0, "BINARY", 1),
            (3, 11, "created_at", 0, "BINARY", 1),
            (4, -1, None, 0, "BINARY", 0),
        ],
    ),
}


def _normalized_sql(sql: str | None) -> tuple[str, ...]:
    """Compare SQLite DDL by tokens, not formatting or keyword case."""
    if sql is None:
        return ()
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^]]*\]|"
        r"<=|>=|<>|!=|==|[-+*/%<>=(),.;]|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?",
        sql,
    )
    return tuple(token if token.startswith("'") else token.lower() for token in tokens if token != ";")


def _meal_photo_schema_version(conn: sqlite3.Connection) -> int | None:
    metadata_exists = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='meal_photo_schema_versions'"
    ).fetchone()
    if not metadata_exists:
        return None
    try:
        row = conn.execute(
            "SELECT version FROM meal_photo_schema_versions WHERE component='meal_photo_system'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("invalid meal-photo schema metadata") from exc
    if row is None:
        return None
    version = row[0]
    if not isinstance(version, int) or not 1 <= version <= 10:
        raise RuntimeError(f"unsupported meal-photo schema version: {version}")
    return version


def _choice_schema_mismatches(conn: sqlite3.Connection) -> list[str]:
    mismatches: list[str] = []
    table_row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (_CHOICE_TABLE,)
    ).fetchone()
    if table_row is None:
        return ["table missing"]
    if [tuple(row) for row in conn.execute(f'PRAGMA table_xinfo("{_CHOICE_TABLE}")')] != _CHOICE_COLUMNS:
        mismatches.append("columns")
    if [tuple(row) for row in conn.execute(f'PRAGMA foreign_key_list("{_CHOICE_TABLE}")')] != _CHOICE_FOREIGN_KEYS:
        mismatches.append("foreign keys")
    if _normalized_sql(table_row[0]) != _normalized_sql(_CHOICE_TABLE_SQL):
        mismatches.append("table SQL")

    actual_indexes: dict[str, tuple[int, str, int, list[tuple[Any, ...]]]] = {}
    for row in conn.execute(f'PRAGMA index_list("{_CHOICE_TABLE}")'):
        actual_indexes[row[1]] = (
            row[2], row[3], row[4],
            [tuple(index_row) for index_row in conn.execute(f'PRAGMA index_xinfo("{row[1]}")')],
        )
    if actual_indexes != _CHOICE_INDEXES:
        mismatches.append("indexes")
    owner_row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (_CHOICE_OWNER_INDEX,)
    ).fetchone()
    if owner_row is None or _normalized_sql(owner_row[0]) != _normalized_sql(_CHOICE_OWNER_INDEX_SQL):
        mismatches.append("owner index SQL")

    owned_objects = {
        tuple(row) for row in conn.execute(
            "SELECT type,name FROM sqlite_schema WHERE tbl_name=? AND type IN ('table','index','trigger')",
            (_CHOICE_TABLE,),
        )
    }
    expected_objects = {("table", _CHOICE_TABLE)} | {
        ("index", name) for name in _CHOICE_INDEXES
    }
    if owned_objects != expected_objects:
        mismatches.append("owned objects")
    return mismatches


def _preflight_meal_photo_choice_schema(conn: sqlite3.Connection) -> bool:
    """Validate the v9 predecessor or exact v10 choice schema before any mutation.

    Returns True for an already-current canonical v10 database.
    """
    version = _meal_photo_schema_version(conn)
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (_CHOICE_TABLE,)
    ).fetchone() is not None
    if not table_exists:
        if version is None or 1 <= version <= 9:
            return False
        raise RuntimeError(
            f"meal_photo_ingredient_choices schema mismatch: table missing at version {version}"
        )
    mismatches = _choice_schema_mismatches(conn)
    if version != 10:
        mismatches.insert(0, f"metadata version is {version!r}, expected 10")
    if mismatches:
        raise RuntimeError(
            "meal_photo_ingredient_choices schema mismatch: " + ", ".join(mismatches)
        )
    return True


def _ensure_meal_photo_schema(conn: sqlite3.Connection) -> None:
    if _preflight_meal_photo_choice_schema(conn):
        return
    _execute_sql_script_without_implicit_commit(
        conn,
        """
        CREATE TABLE IF NOT EXISTS meal_photo_schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_meal_photo_drafts (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL DEFAULT '',
            source_image_ref TEXT NOT NULL DEFAULT '',
            observed_payload_json TEXT NOT NULL,
            answers_json TEXT NOT NULL DEFAULT '{}',
            estimate_json TEXT NOT NULL DEFAULT '{}',
            meal_slot TEXT NOT NULL DEFAULT '',
            consumed_at TEXT NOT NULL DEFAULT '',
            consumed_time_source TEXT NOT NULL DEFAULT 'line_timestamp',
            status TEXT NOT NULL DEFAULT 'awaiting_confirmation',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            retired_at TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            review_json TEXT NOT NULL DEFAULT '{}',
            approved_log_id TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL DEFAULT '',
            approved_by TEXT NOT NULL DEFAULT '',
            workflow_version TEXT NOT NULL DEFAULT 'expert_review_v1',
            confirmed_log_id TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT NOT NULL DEFAULT '',
            confirmed_by TEXT NOT NULL DEFAULT '',
            original_confirmation_event_id TEXT NOT NULL DEFAULT ''
            ,removed_items_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meal_photo_source_event
            ON pending_meal_photo_drafts(user_id, source_message_id)
            WHERE source_message_id<>'';
        CREATE INDEX IF NOT EXISTS idx_meal_photo_user_status
            ON pending_meal_photo_drafts(user_id, status, created_at);
        CREATE TABLE IF NOT EXISTS meal_photo_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token TEXT NOT NULL,
            action TEXT NOT NULL,
            request_payload_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(token) REFERENCES pending_meal_photo_drafts(token)
        );
        CREATE INDEX IF NOT EXISTS idx_meal_photo_events_token
            ON meal_photo_events(token, created_at);
        CREATE TABLE IF NOT EXISTS meal_photo_ingredient_choices (
            choice_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token TEXT NOT NULL,
            draft_version INTEGER NOT NULL,
            source_message_id TEXT NOT NULL,
            parsed_items_json TEXT NOT NULL,
            decisions_json TEXT NOT NULL DEFAULT '{}',
            collision_indexes_json TEXT NOT NULL,
            current_collision INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, source_message_id),
            FOREIGN KEY(token) REFERENCES pending_meal_photo_drafts(token)
        );
        CREATE INDEX IF NOT EXISTS idx_meal_photo_ingredient_choices_owner
            ON meal_photo_ingredient_choices(user_id, token, status, created_at);
        CREATE TABLE IF NOT EXISTS meal_photo_image_events (
            user_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            status TEXT NOT NULL,
            claim_token TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, source_message_id)
        );
        CREATE TABLE IF NOT EXISTS meal_photo_notification_events (
            token TEXT NOT NULL,
            notification_kind TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY(token, notification_kind),
            FOREIGN KEY(token) REFERENCES pending_meal_photo_drafts(token)
        );
        CREATE TABLE IF NOT EXISTS meal_photo_notification_claims (
            token TEXT NOT NULL,
            notification_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            claim_token TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            delivered_at TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(token, notification_kind),
            FOREIGN KEY(token) REFERENCES pending_meal_photo_drafts(token)
        );
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_meal_photo_drafts)")
    }
    if "version" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )
    if "review_json" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN review_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "approved_log_id" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN approved_log_id TEXT NOT NULL DEFAULT ''"
        )
    if "approved_at" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN approved_at TEXT NOT NULL DEFAULT ''"
        )
    if "approved_by" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN approved_by TEXT NOT NULL DEFAULT ''"
        )
    if "workflow_version" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN workflow_version TEXT NOT NULL DEFAULT 'expert_review_v1'"
        )
    if "confirmed_log_id" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN confirmed_log_id TEXT NOT NULL DEFAULT ''"
        )
    if "confirmed_at" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN confirmed_at TEXT NOT NULL DEFAULT ''"
        )
    if "confirmed_by" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN confirmed_by TEXT NOT NULL DEFAULT ''"
        )
    if "original_confirmation_event_id" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN original_confirmation_event_id TEXT NOT NULL DEFAULT ''"
        )
    if "removed_items_json" not in columns:
        conn.execute(
            "ALTER TABLE pending_meal_photo_drafts ADD COLUMN removed_items_json TEXT NOT NULL DEFAULT '[]'"
        )
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO meal_photo_schema_versions(component,version,updated_at)
           VALUES('meal_photo_system',10,?)
           ON CONFLICT(component) DO UPDATE SET
             version=excluded.version,updated_at=excluded.updated_at
           WHERE meal_photo_schema_versions.version < excluded.version""",
        (now,),
    )


def ensure_meal_photo_schema(conn: sqlite3.Connection) -> None:
    """Install atomically; commit only when this standalone call owns the transaction."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
    savepoint = "meal_photo_schema_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _ensure_meal_photo_schema(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if owns_transaction:
            conn.rollback()
        raise
    if owns_transaction:
        try:
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def claim_meal_photo_image_event(
    conn: sqlite3.Connection, *, user_id: str, source_message_id: str,
    lease_seconds: int = 60, max_attempts: int = 3,
) -> dict[str, Any]:
    """Claim an original image event before inference; expired claims retry boundedly."""
    ensure_meal_photo_schema(conn)
    user_id = _short_text(user_id, "user_id", maximum=120)
    source_message_id = _short_text(source_message_id, "source_message_id", maximum=160)
    now_dt = datetime.now(TAIPEI_TZ)
    now = now_dt.isoformat(timespec="seconds")
    lease_until = (now_dt + timedelta(seconds=max(5, min(int(lease_seconds), 300)))).isoformat(timespec="seconds")
    claim_token = secrets.token_hex(16)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT status,lease_until,attempts,result_json FROM meal_photo_image_events
               WHERE user_id=? AND source_message_id=?""", (user_id, source_message_id),
        ).fetchone()
        # A model result is not recoverable until its draft commit exists.  Once it
        # does exist, that owner+source row is the durable outcome: repair the small
        # draft-commit/event-completion gap before considering a retry or live lease.
        # A crash before save_meal_photo_draft commits remains an unknown outcome and
        # is deliberately handled by the bounded inference retry below.
        draft = conn.execute(
            """SELECT token,status,version,expires_at FROM pending_meal_photo_drafts
               WHERE user_id=? AND source_message_id=?""",
            (user_id, source_message_id),
        ).fetchone()
        if row and draft:
            token, draft_status, draft_version, expires_at = draft
            expirable = {
                "awaiting_confirmation", "awaiting_item_name", "confirming",
                "awaiting_adjustment", "adjusting", "estimated", "reviewing", "review_ready",
            }
            if draft_status in expirable and str(expires_at or "") <= now:
                draft_status = "expired"
                conn.execute(
                    """UPDATE pending_meal_photo_drafts SET status='expired',
                       observed_payload_json='{}',answers_json='{}',estimate_json='{}',review_json='{}',
                       retired_at=? WHERE token=? AND user_id=?""",
                    (now, token, user_id),
                )
            result = {
                "token": token,
                "draft_status": draft_status,
                "draft_version": int(draft_version),
            }
            # Terminal rows are replayed as terminal text and must never be revived or
            # have retention-cleared images restored. Active rows are recoverable only
            # after the handler proves their persisted image exists.
            if draft_status not in expirable:
                conn.execute(
                    """UPDATE meal_photo_image_events
                       SET status='completed',claim_token='',lease_until='',result_json=?,updated_at=?
                       WHERE user_id=? AND source_message_id=?""",
                    (json.dumps(result, ensure_ascii=False, sort_keys=True), now,
                     user_id, source_message_id),
                )
                conn.commit()
                return {"state": "completed", "result": result}
            try:
                previous_result = json.loads(row[3] or "{}")
            except (TypeError, json.JSONDecodeError):
                previous_result = {}
            artifact_attempts = int(previous_result.get("artifact_attempts") or 0)
            if artifact_attempts >= max(1, int(max_attempts)):
                conn.commit()
                return {"state": "exhausted"}
            recovery_result = {**result, "artifact_attempts": artifact_attempts}
            conn.execute(
                """UPDATE meal_photo_image_events
                   SET status='processing',claim_token=?,lease_until=?,result_json=?,updated_at=?
                   WHERE user_id=? AND source_message_id=?""",
                (claim_token, lease_until,
                 json.dumps(recovery_result, ensure_ascii=False, sort_keys=True), now,
                 user_id, source_message_id),
            )
            conn.commit()
            return {
                "state": "recoverable", "claim_token": claim_token, "result": result,
            }
        if row and row[0] == "completed":
            conn.commit()
            return {"state": "completed", "result": json.loads(row[3] or "{}")}
        if row and row[0] == "processing" and str(row[1] or "") > now:
            conn.commit()
            return {"state": "busy"}
        attempts = int(row[2] or 0) if row else 0
        if attempts >= max(1, int(max_attempts)):
            conn.commit()
            return {"state": "exhausted"}
        conn.execute(
            """INSERT INTO meal_photo_image_events
               (user_id,source_message_id,status,claim_token,lease_until,attempts,result_json,updated_at)
               VALUES (?,?,'processing',?,?,1,'{}',?)
               ON CONFLICT(user_id,source_message_id) DO UPDATE SET
                 status='processing',claim_token=excluded.claim_token,lease_until=excluded.lease_until,
                 attempts=meal_photo_image_events.attempts+1,result_json='{}',updated_at=excluded.updated_at""",
            (user_id, source_message_id, claim_token, lease_until, now),
        )
        conn.commit()
        return {"state": "claimed", "claim_token": claim_token}
    except Exception:
        conn.rollback()
        raise


def finish_meal_photo_image_event(
    conn: sqlite3.Connection, *, user_id: str, source_message_id: str,
    claim_token: str, result: Mapping[str, Any],
) -> bool:
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    changed = conn.execute(
        """UPDATE meal_photo_image_events SET status='completed',claim_token='',lease_until='',
               result_json=?,updated_at=? WHERE user_id=? AND source_message_id=?
               AND status='processing' AND claim_token=?""",
        (json.dumps(dict(result), ensure_ascii=False, sort_keys=True, allow_nan=False),
         now, user_id, source_message_id, claim_token),
    ).rowcount
    conn.commit()
    return changed == 1


def release_meal_photo_image_event(
    conn: sqlite3.Connection, *, user_id: str, source_message_id: str,
    claim_token: str, abandon: bool = False, artifact_failure: bool = False,
) -> None:
    if abandon:
        conn.execute(
            """DELETE FROM meal_photo_image_events WHERE user_id=? AND source_message_id=?
               AND status='processing' AND claim_token=?""", (user_id, source_message_id, claim_token),
        )
    elif artifact_failure:
        row = conn.execute(
            """SELECT result_json FROM meal_photo_image_events
               WHERE user_id=? AND source_message_id=? AND status='processing' AND claim_token=?""",
            (user_id, source_message_id, claim_token),
        ).fetchone()
        if row:
            try:
                result = json.loads(row[0] or "{}")
            except (TypeError, json.JSONDecodeError):
                result = {}
            result["artifact_attempts"] = int(result.get("artifact_attempts") or 0) + 1
            conn.execute(
                """UPDATE meal_photo_image_events
                   SET status='failed',claim_token='',lease_until='',result_json=?
                   WHERE user_id=? AND source_message_id=?
                     AND status='processing' AND claim_token=?""",
                (json.dumps(result, ensure_ascii=False, sort_keys=True),
                 user_id, source_message_id, claim_token),
            )
    else:
        conn.execute(
            """UPDATE meal_photo_image_events SET status='failed',claim_token='',lease_until=''
               WHERE user_id=? AND source_message_id=? AND status='processing' AND claim_token=?""",
            (user_id, source_message_id, claim_token),
        )
    conn.commit()


def _blank_answers() -> dict[str, Any]:
    answers: dict[str, Any] = {field: None for field in ANSWER_VALUES}
    answers["protein_items"] = []
    return answers


def _expired(expires_at: str) -> bool:
    try:
        expiry = datetime.fromisoformat(expires_at)
        now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.now()
        return expiry < now
    except (TypeError, ValueError):
        return True


def save_meal_photo_draft(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    source_message_id: str,
    payload: Mapping[str, Any],
    source_image_ref: str = "",
    meal_slot: str = "",
    consumed_at: str = "",
    consumed_time_source: str = "line_timestamp",
    workflow_version: str = "user_confirmed_ai_estimate_v1",
) -> str:
    ensure_meal_photo_schema(conn)
    user_id = _short_text(user_id, "user_id", maximum=120)
    source_message_id = _short_text(
        source_message_id, "source_message_id", maximum=160, required=False
    )
    if consumed_time_source not in {"photo_timestamp", "line_timestamp", "manual"}:
        raise ValueError("consumed_time_source 不支援")
    if workflow_version not in {
        "expert_review_v1", "user_confirmed_ai_estimate_v1", "user_confirmed_ai_nutrition_v2",
        "confirmed_food_log_revision_v1",
    }:
        raise ValueError("workflow_version 不支援")
    normalized = normalize_meal_photo_payload(payload)
    direct_estimate = (
        _ai_estimate_snapshot(normalized["ai_estimate"])
        if workflow_version == "user_confirmed_ai_nutrition_v2" and "ai_estimate" in normalized
        else None
    )
    if workflow_version == "user_confirmed_ai_nutrition_v2" and direct_estimate is None:
        raise ValueError("AI營養估算缺失，不能建立可確認草稿")
    if source_message_id:
        row = conn.execute(
            """SELECT token FROM pending_meal_photo_drafts
               WHERE user_id=? AND source_message_id=?""",
            (user_id, source_message_id),
        ).fetchone()
        if row:
            return row[0]
    now_dt = datetime.now(TAIPEI_TZ)
    now = now_dt.isoformat(timespec="seconds")
    token = secrets.token_hex(6)
    try:
        conn.execute(
            """INSERT INTO pending_meal_photo_drafts
               (token,user_id,source_message_id,source_image_ref,observed_payload_json,
                answers_json,estimate_json,meal_slot,consumed_at,consumed_time_source,
                status,created_at,updated_at,expires_at,workflow_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                token,
                user_id,
                source_message_id,
                str(source_image_ref or "")[:240],
                json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False),
                json.dumps(_blank_answers(), ensure_ascii=False, sort_keys=True),
                json.dumps(direct_estimate or {}, ensure_ascii=False, sort_keys=True, allow_nan=False),
                str(meal_slot or "")[:30],
                str(consumed_at or now)[:50],
                consumed_time_source,
                "estimated" if direct_estimate else "awaiting_confirmation",
                now,
                now,
                (now_dt + timedelta(hours=24)).isoformat(timespec="seconds"),
                workflow_version,
            ),
        )
        conn.commit()
        return token
    except sqlite3.IntegrityError:
        if source_message_id:
            row = conn.execute(
                """SELECT token FROM pending_meal_photo_drafts
                   WHERE user_id=? AND source_message_id=?""",
                (user_id, source_message_id),
            ).fetchone()
            if row:
                return row[0]
        raise


def create_meal_photo_revision_draft(
    conn: sqlite3.Connection, *, user_id: str, log_id: str, from_version: int,
    request_text: str, estimate: Mapping[str, Any], source_message_id: str = "",
) -> dict[str, Any]:
    """Persist an isolated revision preview; the confirmed log remains untouched."""
    ensure_meal_photo_schema(conn)
    user_id = _short_text(user_id, "user_id", maximum=120)
    log_id = _short_text(log_id, "log_id", maximum=160)
    request_text = " ".join(str(request_text or "").strip().split())
    if not request_text or len(request_text) > 500:
        raise ValueError("修改內容不可空白或過長")
    if isinstance(from_version, bool) or not isinstance(from_version, int) or from_version < 1:
        raise ValueError("來源版本無效")
    snapshot = dict(estimate or {})
    if not meal_photo_estimate_snapshot_is_valid(snapshot) or snapshot.get("rule_version") != "ai-vision-nutrition-estimate-v1":
        raise ValueError("revision AI營養估算完整性驗證失敗")
    _validate_provided_nutrition(snapshot.get("calories_kcal"), snapshot.get("protein_g"))
    row = conn.execute(
        """SELECT l.version,l.source_image_ref,l.trust_hash,l.confirmation_status,
                  COALESCE(l.deleted_at,''),f.source_type,f.owner_user_id
           FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
           WHERE l.log_id=? AND l.user_id=?""",
        (log_id, user_id),
    ).fetchone()
    if not row or row[3] != "confirmed" or row[4] or row[5] != "user_meal_photo" or row[6] != user_id:
        raise ValueError("找不到可修改的餐點紀錄")
    if int(row[0]) != from_version:
        raise ValueError("這筆紀錄已更新，請重新開啟最新卡片")
    projection = user_confirmed_meal_photo_trust_projection(
        conn, log_id, "user_confirmed_ai_estimate"
    )
    if projection.get("integrity_status") != "verified":
        raise ValueError("原餐點可信鏈驗證失敗")
    provenance = snapshot.get("provenance") or {}
    review = {
        "schema_version": "confirmed-food-log-revision-preview-v1",
        "parent": {
            "user_id": user_id,
            "log_id": log_id,
            "from_version": from_version,
            "original_confirmation_hash": str(row[2] or ""),
            "previous_revision_hash": str(
                projection.get("effective_revision_hash") or row[2] or ""
            ),
        },
        "request_text_hash": hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
        "estimate_provenance": {
            "rule_version": "ai-vision-nutrition-estimate-v1",
            "provider": provenance.get("provider"),
            "model": provenance.get("model"),
            "source": "original_meal_photo_plus_user_revision",
        },
    }
    token = secrets.token_hex(12)
    source_message_id = str(source_message_id or f"revision:{token}").strip()
    if len(source_message_id) > 160:
        raise ValueError("revision來源訊息識別碼過長")
    now_dt = datetime.now(TAIPEI_TZ)
    now = now_dt.isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO pending_meal_photo_drafts
           (token,user_id,source_message_id,source_image_ref,observed_payload_json,
            answers_json,estimate_json,meal_slot,consumed_at,consumed_time_source,
            status,created_at,updated_at,expires_at,version,review_json,workflow_version)
           VALUES(?,?,?,?,'{}','{}',?,'','','line_timestamp','estimated',?,?,?,?,?,
                  'confirmed_food_log_revision_v1')""",
        (
            token, user_id, source_message_id, str(row[1] or ""),
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False),
            now, now, (now_dt + timedelta(hours=1)).isoformat(timespec="seconds"), 1,
            json.dumps(review, ensure_ascii=False, sort_keys=True, allow_nan=False),
        ),
    )
    conn.commit()
    return get_meal_photo_draft(conn, user_id=user_id, token=token)


def cancel_meal_photo_revision_draft(
    conn: sqlite3.Connection, *, user_id: str, token: str, expected_version: int,
) -> dict[str, Any]:
    """Cancel only a revision preview; no authoritative food log is changed."""
    ensure_meal_photo_schema(conn)
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    changed = conn.execute(
        """UPDATE pending_meal_photo_drafts
           SET status='cancelled',estimate_json='{}',review_json='{}',retired_at=?,
               updated_at=?,version=version+1
           WHERE token=? AND user_id=? AND version=? AND status='estimated'
             AND workflow_version='confirmed_food_log_revision_v1'""",
        (now, now, token, user_id, expected_version),
    ).rowcount
    if changed != 1:
        raise ValueError("找不到可取消的revision草稿，或草稿已更新")
    conn.commit()
    return get_meal_photo_draft(conn, user_id=user_id, token=token)


def get_meal_photo_draft(
    conn: sqlite3.Connection, *, user_id: str, token: str,
    allow_expired: bool = False,
) -> dict[str, Any]:
    ensure_meal_photo_schema(conn)
    row = conn.execute(
        """SELECT source_message_id,source_image_ref,observed_payload_json,answers_json,
                  estimate_json,meal_slot,consumed_at,consumed_time_source,status,
                  created_at,updated_at,expires_at,version,review_json,approved_log_id,
                  approved_at,approved_by,workflow_version,confirmed_log_id,confirmed_at,confirmed_by
           FROM pending_meal_photo_drafts WHERE token=? AND user_id=?""",
        (token, user_id),
    ).fetchone()
    if not row:
        raise ValueError("找不到這筆餐點照片草稿")
    if _expired(row[11]) and row[8] in {
        "awaiting_confirmation", "awaiting_item_name", "confirming",
        "awaiting_adjustment", "adjusting", "estimated", "reviewing", "review_ready"
    }:
        retired = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE pending_meal_photo_drafts SET status='expired',
               observed_payload_json='{}',answers_json='{}',estimate_json='{}',review_json='{}',retired_at=?
               WHERE token=? AND user_id=?""",
            (retired, token, user_id),
        )
        conn.commit()
        if not allow_expired:
            raise ValueError("這筆餐點照片草稿已逾時")
        return get_meal_photo_draft(conn, user_id=user_id, token=token)
    return {
        "token": token,
        "user_id": user_id,
        "source_message_id": row[0],
        "source_image_ref": row[1],
        "payload": json.loads(row[2] or "{}"),
        "answers": {**_blank_answers(), **json.loads(row[3] or "{}")},
        "estimate": json.loads(row[4] or "{}"),
        "meal_slot": row[5],
        "consumed_at": row[6],
        "consumed_time_source": row[7],
        "status": row[8],
        "created_at": row[9],
        "updated_at": row[10],
        "expires_at": row[11],
        "version": int(row[12]),
        "review": json.loads(row[13] or "{}"),
        "approved_log_id": row[14] or "",
        "approved_at": row[15] or "",
        "approved_by": row[16] or "",
        "workflow_version": row[17] or "expert_review_v1",
        "confirmed_log_id": row[18] or "",
        "confirmed_at": row[19] or "",
        "confirmed_by": row[20] or "",
    }


def get_meal_photo_draft_for_admin(
    conn: sqlite3.Connection,
    *,
    token: str,
    admin_user_id: str,
    required_admin_user_id: str,
) -> dict[str, Any]:
    """由設定的管理員以不可預測token安全取得任一使用者的待審草稿。"""
    admin_user_id = _short_text(admin_user_id, "admin_user_id", maximum=120)
    required_admin_user_id = str(required_admin_user_id or "").strip()
    if not required_admin_user_id or admin_user_id != required_admin_user_id:
        raise PermissionError("管理員限定")
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise ValueError("餐點草稿token無效")
    ensure_meal_photo_schema(conn)
    row = conn.execute(
        "SELECT user_id FROM pending_meal_photo_drafts WHERE token=?",
        (token,),
    ).fetchone()
    if not row:
        raise ValueError("找不到這筆餐點照片草稿")
    return get_meal_photo_draft(conn, user_id=row[0], token=token)


def list_pending_meal_photo_reviews(
    conn: sqlite3.Connection, *, limit: int = 10
) -> list[dict[str, Any]]:
    """列出仍有效且可由管理員繼續或完成的餐點審核草稿。"""
    ensure_meal_photo_schema(conn)
    limit = max(1, min(int(limit), 10))
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT token,user_id FROM pending_meal_photo_drafts
           WHERE workflow_version='expert_review_v1'
             AND status IN ('estimated','reviewing','review_ready') AND expires_at>?
           ORDER BY created_at ASC LIMIT ?""",
        (now, limit),
    ).fetchall()
    return [
        get_meal_photo_draft(conn, user_id=user_id, token=token)
        for token, user_id in rows
    ]


def claim_meal_photo_notification(
    conn: sqlite3.Connection, *, token: str, notification_kind: str,
    lease_seconds: int = 30,
) -> dict[str, str]:
    """原子取得通知租約；同一通知同時間只允許一個推播者。"""
    ensure_meal_photo_schema(conn)
    if notification_kind not in {"owner_approved", "owner_rejected"}:
        raise ValueError("餐點通知類型無效")
    now_dt = datetime.now(TAIPEI_TZ)
    now = now_dt.isoformat(timespec="seconds")
    lease_until = (now_dt + timedelta(seconds=max(5, min(int(lease_seconds), 300)))).isoformat(
        timespec="seconds"
    )
    claim_token = secrets.token_hex(16)
    conn.execute("BEGIN IMMEDIATE")
    try:
        delivered = conn.execute(
            """SELECT 1 FROM meal_photo_notification_events
               WHERE token=? AND notification_kind=?""",
            (token, notification_kind),
        ).fetchone()
        if delivered:
            conn.commit()
            return {"state": "delivered"}
        row = conn.execute(
            """SELECT status,lease_until FROM meal_photo_notification_claims
               WHERE token=? AND notification_kind=?""",
            (token, notification_kind),
        ).fetchone()
        if row and row[0] == "delivered":
            conn.commit()
            return {"state": "delivered"}
        if row and row[0] == "sending" and str(row[1] or "") > now:
            conn.commit()
            return {"state": "busy"}
        conn.execute(
            """INSERT INTO meal_photo_notification_claims
               (token,notification_kind,status,claim_token,lease_until,attempts,updated_at)
               VALUES(?,?,'sending',?,?,1,?)
               ON CONFLICT(token,notification_kind) DO UPDATE SET
                 status='sending',claim_token=excluded.claim_token,
                 lease_until=excluded.lease_until,attempts=attempts+1,
                 last_error='',updated_at=excluded.updated_at""",
            (token, notification_kind, claim_token, lease_until, now),
        )
        conn.commit()
        return {"state": "claimed", "claim_token": claim_token}
    except Exception:
        conn.rollback()
        raise


def complete_meal_photo_notification(
    conn: sqlite3.Connection, *, token: str, notification_kind: str,
    claim_token: str,
) -> bool:
    ensure_meal_photo_schema(conn)
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute(
            """UPDATE meal_photo_notification_claims
               SET status='delivered',claim_token='',lease_until='',delivered_at=?,updated_at=?
               WHERE token=? AND notification_kind=? AND status='sending' AND claim_token=?""",
            (now, now, token, notification_kind, claim_token),
        ).rowcount
        if changed:
            conn.execute(
                """INSERT OR IGNORE INTO meal_photo_notification_events
                   (token,notification_kind,delivered_at) VALUES(?,?,?)""",
                (token, notification_kind, now),
            )
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise


def release_meal_photo_notification(
    conn: sqlite3.Connection, *, token: str, notification_kind: str,
    claim_token: str, error: str,
) -> None:
    ensure_meal_photo_schema(conn)
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    conn.execute(
        """UPDATE meal_photo_notification_claims
           SET status='pending',claim_token='',lease_until='',last_error=?,updated_at=?
           WHERE token=? AND notification_kind=? AND status='sending' AND claim_token=?""",
        (str(error or "")[:300], now, token, notification_kind, claim_token),
    )
    conn.commit()


def meal_photo_notification_delivered(
    conn: sqlite3.Connection, *, token: str, notification_kind: str
) -> bool:
    ensure_meal_photo_schema(conn)
    if notification_kind not in {"owner_approved", "owner_rejected"}:
        raise ValueError("餐點通知類型無效")
    row = conn.execute(
        """SELECT 1 FROM meal_photo_notification_events
           WHERE token=? AND notification_kind=?""",
        (token, notification_kind),
    ).fetchone()
    return bool(row)


def mark_meal_photo_notification_delivered(
    conn: sqlite3.Connection, *, token: str, notification_kind: str
) -> None:
    ensure_meal_photo_schema(conn)
    if notification_kind not in {"owner_approved", "owner_rejected"}:
        raise ValueError("餐點通知類型無效")
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR IGNORE INTO meal_photo_notification_events
           (token,notification_kind,delivered_at) VALUES(?,?,?)""",
        (token, notification_kind, now),
    )
    conn.commit()


def clear_meal_photo_image_ref(
    conn: sqlite3.Connection, *, user_id: str, token: str, expected_ref: str
) -> bool:
    changed = conn.execute(
        """UPDATE pending_meal_photo_drafts SET source_image_ref=''
           WHERE token=? AND user_id=? AND source_image_ref=?
             AND status IN ('cancelled','expired')""",
        (token, user_id, expected_ref),
    ).rowcount
    conn.commit()
    return changed == 1


def daily_pending_meal_photo_count(
    conn: sqlite3.Connection, *, user_id: str, date_iso: str
) -> int:
    ensure_meal_photo_schema(conn)
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """SELECT token,consumed_at,expires_at FROM pending_meal_photo_drafts
               WHERE user_id=? AND status IN (
                 'awaiting_confirmation','awaiting_item_name','confirming',
                 'awaiting_adjustment','adjusting','estimated','reviewing','review_ready'
               )""",
            (user_id,),
        ).fetchall()
        active_consumed_at: list[str] = []
        for token, consumed_at, expires_at in rows:
            if _expired(str(expires_at or "")):
                conn.execute(
                    """UPDATE pending_meal_photo_drafts
                       SET status='expired',observed_payload_json='{}',answers_json='{}',
                           estimate_json='{}',review_json='{}',retired_at=?,updated_at=?,version=version+1
                       WHERE token=? AND user_id=?
                         AND status IN (
                           'awaiting_confirmation','awaiting_item_name','confirming',
                           'awaiting_adjustment','adjusting','estimated','reviewing','review_ready'
                         )""",
                    (now, now, token, user_id),
                )
            else:
                active_consumed_at.append(str(consumed_at or ""))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    count = 0
    for consumed_at in active_consumed_at:
        try:
            local_date = datetime.fromisoformat(consumed_at).astimezone(TAIPEI_TZ).date().isoformat()
        except (TypeError, ValueError):
            continue
        if local_date == date_iso:
            count += 1
    return count


def next_meal_photo_step(draft: Mapping[str, Any]) -> str:
    answers = {**_blank_answers(), **dict(draft.get("answers") or {})}
    for field in ("scope", "protein_type"):
        if answers.get(field) is None:
            return field
    if answers.get("protein_type") != "none":
        if answers.get("protein_portion") is None:
            return "protein_portion"
        if answers.get("protein_portion") != "none":
            if answers.get("protein_more") is None:
                return "protein_more"
            if answers.get("protein_more") == "add":
                if answers.get("protein_extra_type") is None:
                    return "protein_extra_type"
                if answers.get("protein_extra_portion") is None:
                    return "protein_extra_portion"
    for field in ("starch_portion", "vegetable_portion", "cooking_oil", "sauce_level"):
        if answers.get(field) is None:
            return field
    return "complete"


STEP_OPTIONS = {
    "scope": [("只有照片這些", "visible_only"), ("有未入鏡食物", "has_unseen"), ("不確定", "unknown")],
    "protein_type": [
        ("雞肉", "chicken"), ("豬肉", "pork"), ("魚類", "fish"),
        ("蛋", "egg"), ("豆製品", "tofu"), ("其他", "other"),
        ("沒有蛋白質食物", "none"), ("不確定", "unknown"),
    ],
    "protein_portion": [
        ("半個手掌", "half_palm"), ("1個手掌", "one_palm"),
        ("1.5個手掌", "one_half_palm"), ("2個手掌", "two_palm"),
        ("沒有", "none"), ("不確定", "unknown"),
    ],
    "protein_more": [("就這一種", "done"), ("＋還有其他蛋白質", "add")],
    "protein_extra_type": [
        ("雞肉", "chicken"), ("豬肉", "pork"), ("魚類", "fish"),
        ("蛋", "egg"), ("豆製品", "tofu"), ("其他", "other"),
        ("不確定", "unknown"),
    ],
    "protein_extra_portion": [
        ("半個手掌", "half_palm"), ("1個手掌", "one_palm"),
        ("1.5個手掌", "one_half_palm"), ("2個手掌", "two_palm"),
        ("不確定", "unknown"),
    ],
    "starch_portion": [
        ("沒有吃主食", "none"), ("半碗", "half_bowl"), ("1碗", "one_bowl"),
        ("1.5碗", "one_half_bowl"), ("2碗", "two_bowl"),
        ("未入鏡／不確定", "unseen_unknown"),
    ],
    "vegetable_portion": [
        ("沒有", "none"), ("半碗", "half_bowl"), ("1碗", "one_bowl"),
        ("1.5碗", "one_half_bowl"), ("2碗", "two_bowl"),
        ("3碗", "three_bowl"), ("不確定", "unknown"),
    ],
    "cooking_oil": [
        ("沒有／水煮蒸烤", "none"), ("少油", "light"),
        ("一般用油", "normal"), ("多油／油炸", "heavy"), ("不確定", "unknown"),
    ],
    "sauce_level": [
        ("沒有", "none"), ("少量", "little"), ("約一半", "half"),
        ("全部", "all"), ("不確定", "unknown"),
    ],
}


def meal_photo_step_options(token: str, step: str, *, version: int = 1) -> list[dict[str, str]]:
    if (
        not re.fullmatch(r"[0-9a-f]{12}", str(token or ""))
        or step not in STEP_OPTIONS or int(version) < 1
    ):
        raise ValueError("餐點確認步驟無效")
    return [
        {
            "label": label,
            "message": f"餐點選項:{token}:{step}:{value}",
            "data": f"mp:v1:{token}:{int(version)}:answer:{step}:{value}",
        }
        for label, value in STEP_OPTIONS[step]
    ]


RANGE_MAPS = {
    "protein_portion": {
        "half_palm": (1.0, 2.0), "one_palm": (2.0, 3.0),
        "one_half_palm": (3.0, 5.0), "two_palm": (4.0, 6.0), "none": (0.0, 0.0),
    },
    "starch_portion": {
        "half_bowl": (1.5, 2.5), "one_bowl": (3.0, 5.0),
        "one_half_bowl": (5.0, 7.0), "two_bowl": (6.0, 10.0), "none": (0.0, 0.0),
    },
    "vegetable_portion": {
        "half_bowl": (0.5, 1.0), "one_bowl": (1.0, 2.0),
        "one_half_bowl": (1.5, 3.0), "two_bowl": (2.0, 4.0),
        "three_bowl": (3.0, 6.0), "none": (0.0, 0.0),
    },
}


def _range_value(field: str, value: str | None, *, confirmed_none: bool = False):
    if confirmed_none:
        return {"min": 0.0, "max": 0.0, "basis": "user_confirmed_none"}
    pair = RANGE_MAPS[field].get(str(value or ""))
    if pair is None:
        return None
    return {
        "min": pair[0], "max": pair[1],
        "basis": "user_confirmed_none" if pair == (0.0, 0.0) else "hand_portion_range_v1",
    }


def _estimate_from_answers(answers: Mapping[str, Any]) -> dict[str, Any]:
    protein_none = answers.get("protein_type") == "none"
    protein_items = list(answers.get("protein_items") or [])
    if not protein_none and not protein_items:
        protein_items = [{
            "type": answers.get("protein_type"),
            "portion": answers.get("protein_portion"),
        }]
    estimated_items = []
    for item in protein_items:
        exchange = _range_value("protein_portion", item.get("portion"))
        estimated_items.append({
            "type": item.get("type"), "portion": item.get("portion"),
            "exchange": exchange,
        })
    if protein_none:
        protein_total = _range_value("protein_portion", None, confirmed_none=True)
    elif any(item["exchange"] is None for item in estimated_items):
        protein_total = None
    elif len(estimated_items) == 1:
        protein_total = estimated_items[0]["exchange"]
    else:
        protein_total = {
            "min": round(sum(item["exchange"]["min"] for item in estimated_items), 4),
            "max": round(sum(item["exchange"]["max"] for item in estimated_items), 4),
            "basis": "summed_hand_portion_ranges_v2",
        }
    estimate = {
        "calories_kcal": None,
        "protein_g": None,
        "fat_g": None,
        "carbohydrate_g": None,
        "protein_total_exchange": protein_total,
        "starch_exchange": _range_value("starch_portion", answers.get("starch_portion")),
        "vegetable_exchange": _range_value("vegetable_portion", answers.get("vegetable_portion")),
        "cooking_oil_confirmation": answers.get("cooking_oil"),
        "sauce_confirmation": answers.get("sauce_level"),
        "formal_status": "pending_review_not_counted",
        "rule_version": "hand-portion-range-v1",
    }
    if len(estimated_items) > 1:
        estimate["protein_items"] = estimated_items
        estimate["rule_version"] = "hand-portion-range-v2"
    return estimate


def apply_meal_photo_action(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    user_id: str,
    token: str,
    expected_version: int,
    action: str,
    field: str = "",
    value: str = "",
    quota_batch_key: str = "",
    quota_batch_owner: str = "",
) -> dict[str, Any]:
    """以durable event與版本鎖原子套用LINE Postback；重送回傳原result。"""
    ensure_meal_photo_schema(conn)
    ensure_nutrition_schema(conn)
    event_id = _short_text(event_id, "event_id", maximum=180)
    user_id = _short_text(user_id, "user_id", maximum=120)
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise ValueError("餐點草稿token無效")
    if action == "answer":
        if field not in ANSWER_VALUES:
            raise ValueError("餐點操作不支援")
        value = str(value or "").strip()
        if value not in ANSWER_VALUES[field]:
            raise ValueError("餐點回答選項不支援")
    elif action == "set_meal_slot":
        field = ""
        value = str(value or "").strip()
        if value not in {"早餐", "午餐", "晚餐", "點心"}:
            raise ValueError("餐別選項不支援")
    elif action in {"cancel", "confirm_estimate", "request_adjust"}:
        field = ""
        value = ""
    elif action == "remove_item":
        field = ""
        value = _short_text(value, "食材識別碼或名稱", maximum=60)
    elif action == "add_item":
        field = _short_text(field, "食材類別", maximum=20)
        if field not in VISIBLE_CATEGORIES:
            raise ValueError("食材類別不支援")
        value = _short_text(value, "食材資料", maximum=500)
    elif action == "batch_upsert_items":
        field = ""
        value = _short_text(value, "批次食材資料", maximum=4000)
    elif action in {"request_add", "cancel_add"}:
        field = ""
        value = ""
    else:
        raise ValueError("餐點操作不支援")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("餐點畫面版本無效") from exc
    if expected_version < 1:
        raise ValueError("餐點畫面版本無效")
    request = {
        "user_id": user_id, "token": token, "expected_version": expected_version,
        "action": action, "field": field, "value": value,
    }
    request_hash = hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if action == "batch_upsert_items":
            latest = conn.execute(
                """SELECT token FROM pending_meal_photo_drafts
                   WHERE user_id=?
                   ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if not latest or latest[0] != token:
                raise ValueError("這是較舊餐點，請使用最新餐點卡")
        existing = conn.execute(
            """SELECT user_id,token,action,request_payload_hash,result_json
               FROM meal_photo_events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if existing:
            if (
                existing[0] != user_id or existing[1] != token or existing[2] != action
                or existing[3] != request_hash
            ):
                raise ValueError("餐點事件識別碼衝突")
            result = json.loads(existing[4])
            draft = get_meal_photo_draft(
                conn, user_id=user_id, token=token, allow_expired=True,
            )
            status = str(draft.get("status") or "")
            if status == "user_confirmed":
                confirmed_log_id = str(draft.get("confirmed_log_id") or "")
                if not user_confirmed_meal_photo_estimate_is_valid(
                    conn, confirmed_log_id, expected_user_id=user_id,
                    expected_draft_token=token,
                    expected_draft_version=int(draft.get("version") or 0),
                ):
                    raise ValueError("已記錄餐點完整性驗證失敗")
                log_version = 1
                if draft.get("workflow_version") == "user_confirmed_ai_nutrition_v2":
                    log_version_row = conn.execute(
                        "SELECT version FROM food_logs WHERE log_id=? AND user_id=?",
                        (confirmed_log_id, user_id),
                    ).fetchone()
                    log_version = int(log_version_row[0])
                result = {
                    "kind": "recorded_updated" if log_version > 1 else "recorded",
                    "version": int(draft["version"]),
                    "log_id": confirmed_log_id,
                }
            elif (
                status == "cancelled" and result.get("kind") != "cancel"
            ) or status == "expired" or _expired(str(draft.get("expires_at") or "")):
                terminal_status = status if status in {"cancelled", "expired"} else "expired"
                result = {
                    "kind": "terminal", "status": terminal_status,
                    "version": int(draft.get("version") or 0),
                }
            conn.commit()
            return {
                "replayed": True,
                "result": result,
                "draft": draft,
            }
        row = conn.execute(
            """SELECT answers_json,status,expires_at,version,source_image_ref,
                      observed_payload_json,estimate_json,meal_slot,consumed_at,
                      source_message_id,workflow_version,confirmed_log_id,removed_items_json
               FROM pending_meal_photo_drafts WHERE token=? AND user_id=?""",
            (token, user_id),
        ).fetchone()
        if not row:
            raise ValueError("找不到這筆餐點照片草稿")
        answers = {**_blank_answers(), **json.loads(row[0] or "{}")}
        status, expires_at, current_version = row[1], row[2], int(row[3])
        if action == "confirm_estimate" and status == "user_confirmed":
            confirmed_log_id = str(row[11] or "")
            if (
                current_version - 1 != expected_version
                or not user_confirmed_meal_photo_estimate_is_valid(
                    conn, confirmed_log_id, expected_user_id=user_id,
                    expected_draft_token=token, expected_draft_version=current_version,
                )
            ):
                raise ValueError("已記錄餐點完整性驗證失敗")
            now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
            log_version = 1
            if row[10] == "user_confirmed_ai_nutrition_v2":
                log_version_row = conn.execute(
                    "SELECT version FROM food_logs WHERE log_id=? AND user_id=?",
                    (confirmed_log_id, user_id),
                ).fetchone()
                log_version = int(log_version_row[0])
            result = {
                "kind": "recorded_updated" if log_version > 1 else "recorded",
                "version": current_version,
                "log_id": confirmed_log_id,
            }
            conn.execute(
                """INSERT INTO meal_photo_events
                   (event_id,user_id,token,action,request_payload_hash,result_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, user_id, token, action, request_hash,
                 json.dumps(result, ensure_ascii=False, sort_keys=True), now),
            )
            conn.commit()
            return {"replayed": True, "result": result,
                    "draft": get_meal_photo_draft(conn, user_id=user_id, token=token)}
        if _expired(expires_at):
            raise ValueError("這筆餐點照片草稿已逾時")
        allowed_statuses = (
            {"awaiting_item_name"} if action in {"add_item", "batch_upsert_items", "cancel_add"}
            else {"estimated"} if action == "confirm_estimate"
            else {"awaiting_confirmation", "confirming", "estimated", "awaiting_adjustment", "adjusting"} if action == "cancel"
            else {"estimated", "awaiting_confirmation"} if action in {"remove_item", "request_add"}
            else {"estimated"} if action in {"request_adjust", "set_meal_slot"}
            else {"awaiting_confirmation", "confirming"}
        )
        if status not in allowed_statuses:
            raise ValueError("這筆餐點照片不能再修改")
        if current_version != expected_version:
            raise ValueError("餐點確認畫面已更新，請使用最新按鈕")
        next_version = current_version + 1
        now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
        if action == "set_meal_slot":
            if row[10] not in {"user_confirmed_ai_estimate_v1", "user_confirmed_ai_nutrition_v2"}:
                raise ValueError("這筆餐點不能由顧客選擇餐別")
            result = {"kind": "estimate", "version": next_version}
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET meal_slot=?,updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=? AND status='estimated'
                     AND workflow_version IN ('user_confirmed_ai_estimate_v1','user_confirmed_ai_nutrition_v2')""",
                (value, now, next_version, token, user_id, current_version),
            ).rowcount
        elif action == "request_adjust":
            if row[10] != "user_confirmed_ai_nutrition_v2":
                raise ValueError("只有AI先估餐點可使用文字調整")
            result = {"kind": "ask_adjustment", "version": next_version}
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET status='awaiting_adjustment',updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=? AND status='estimated'
                     AND workflow_version='user_confirmed_ai_nutrition_v2'""",
                (now, next_version, token, user_id, current_version),
            ).rowcount
        elif action == "confirm_estimate":
            if row[10] not in {"user_confirmed_ai_estimate_v1", "user_confirmed_ai_nutrition_v2"}:
                raise ValueError("舊版待審草稿不能由顧客直接記錄")
            observed = normalize_meal_photo_payload(json.loads(row[5] or "{}"))
            stored_estimate = json.loads(row[6] or "{}")
            expected_estimate = (
                _ai_estimate_snapshot(observed.get("ai_estimate") or {})
                if row[10] == "user_confirmed_ai_nutrition_v2"
                else _estimate_from_answers(answers)
            )
            if stored_estimate != expected_estimate or not meal_photo_estimate_snapshot_is_valid(stored_estimate):
                raise ValueError("餐點照片估算完整性驗證失敗")
            if row[10] == "user_confirmed_ai_nutrition_v2" and (
                observed.get("ai_estimate", {}).get("reconciliation") == "items_sum_v1"
            ):
                ai = observed["ai_estimate"]
                for nutrient in ("calories_kcal", "protein_g"):
                    item_sum = sum(float(item[nutrient]) for item in ai["items"])
                    aggregate = ai[nutrient]
                    if item_sum != float(aggregate["estimate"]) or not (
                        float(aggregate["min"]) <= item_sum <= float(aggregate["max"])
                    ):
                        raise ValueError("餐點照片明細與總營養不一致，請重新估算")
            formal_result = insert_user_confirmed_meal_photo_log(
                conn, token=token, user_id=user_id, source_message_id=str(row[9] or ""),
                confirmation_event_id=event_id, consumed_at=str(row[8] or ""),
                meal_slot=str(row[7] or ""), source_image_ref=str(row[4] or ""),
                observed_payload=observed, answers=answers, estimate=stored_estimate,
            )
            result = {"kind": "recorded", "version": next_version,
                      "log_id": formal_result["log_id"]}
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET status='user_confirmed',confirmed_log_id=?,confirmed_at=?,confirmed_by=?,
                       original_confirmation_event_id=?,updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=? AND status='estimated'
                     AND workflow_version IN ('user_confirmed_ai_estimate_v1','user_confirmed_ai_nutrition_v2')""",
                (formal_result["log_id"], now, user_id, event_id, now, next_version,
                 token, user_id, current_version),
            ).rowcount
        elif action == "cancel":
            result = {
                "kind": "cancel", "version": next_version, "source_image_ref": row[4],
            }
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET observed_payload_json='{}',answers_json='{}',estimate_json='{}',
                       status='cancelled',retired_at=?,updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=?
                     AND status IN ('awaiting_confirmation','confirming','estimated','awaiting_adjustment','adjusting')""",
                (now, now, next_version, token, user_id, current_version),
            ).rowcount
            if changed == 1 and status == "adjusting":
                for adjustment_event_id, adjustment_result in conn.execute(
                    """SELECT event_id,result_json FROM meal_photo_events
                       WHERE token=? AND user_id=? AND action='adjust_estimate'""", (token, user_id),
                ).fetchall():
                    try:
                        state = json.loads(adjustment_result or "{}")
                    except json.JSONDecodeError:
                        continue
                    if state.get("state") == "processing":
                        conn.execute(
                            "UPDATE meal_photo_events SET result_json=? WHERE event_id=? AND result_json=?",
                            (json.dumps({"state": "cancelled"}, sort_keys=True),
                             adjustment_event_id, adjustment_result),
                        )
        elif action == "remove_item":
            if status == "estimated" and row[10] == "user_confirmed_ai_nutrition_v2":
                if not re.fullmatch(r"item_[0-9a-f]{12}", value):
                    raise ValueError("食材識別碼無效")
                before = normalize_meal_photo_payload(json.loads(row[5] or "{}"))
                removed_item = next(
                    (dict(item) for item in before["ai_estimate"]["items"] if item["item_id"] == value),
                    None,
                )
                if removed_item is None:
                    raise ValueError("找不到要移除的食材")
                try:
                    removed_items = json.loads(row[12] or "[]")
                except (TypeError, json.JSONDecodeError):
                    removed_items = []
                if not isinstance(removed_items, list):
                    removed_items = []
                if not any(item.get("item_id") == value for item in removed_items if isinstance(item, dict)):
                    removed_items.append({
                        "item_id": value, "name": removed_item["name"],
                        "portion": removed_item["portion"],
                    })
                payload, estimate = _mutate_ai_estimate_items(
                    before, remove_item_id=value,
                )
                result = {"kind": "estimate", "version": next_version}
                changed = conn.execute(
                    """UPDATE pending_meal_photo_drafts
                       SET observed_payload_json=?,estimate_json=?,removed_items_json=?,updated_at=?,version=?
                       WHERE token=? AND user_id=? AND version=? AND status='estimated'
                         AND workflow_version='user_confirmed_ai_nutrition_v2'""",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                     json.dumps(estimate, ensure_ascii=False, sort_keys=True, allow_nan=False),
                     json.dumps(removed_items, ensure_ascii=False, sort_keys=True, allow_nan=False),
                     now, next_version, token, user_id, current_version),
                ).rowcount
            else:
                payload = normalize_meal_photo_payload(json.loads(row[5] or "{}"))
                original_items = list(payload["visible_items"])
                remove_index = next((i for i, item in enumerate(original_items) if item["name"] == value), None)
                if remove_index is None:
                    raise ValueError("找不到要移除的食材")
                payload["visible_items"] = [item for i, item in enumerate(original_items) if i != remove_index]
                if not payload["visible_items"]:
                    raise ValueError("至少要保留一項食材；若全部不符請取消後重拍")
                result = {"kind": "updated", "version": next_version}
                changed = conn.execute(
                    """UPDATE pending_meal_photo_drafts
                       SET observed_payload_json=?,updated_at=?,version=?
                       WHERE token=? AND user_id=? AND version=? AND status='awaiting_confirmation'""",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                     now, next_version, token, user_id, current_version),
                ).rowcount
        elif action == "request_add":
            source_status = "estimated" if row[10] == "user_confirmed_ai_nutrition_v2" else "awaiting_confirmation"
            if status != source_status:
                raise ValueError("這份估算不能新增食材")
            result = {"kind": "ask_item_name", "version": next_version}
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET status='awaiting_item_name',updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=? AND status=?""",
                (now, next_version, token, user_id, current_version, source_status),
            ).rowcount
        elif action == "cancel_add":
            target_status = "estimated" if row[10] == "user_confirmed_ai_nutrition_v2" else "awaiting_confirmation"
            result = {"kind": "estimate" if target_status == "estimated" else "updated", "version": next_version}
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET status=?,updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=?
                     AND status='awaiting_item_name'""",
                (target_status, now, next_version, token, user_id, current_version),
            ).rowcount
            if changed == 1:
                conn.execute(
                    """UPDATE meal_photo_ingredient_choices
                       SET status='cancelled',updated_at=?
                       WHERE token=? AND user_id=? AND status IN ('pending','resolving')""",
                    (now, token, user_id),
                )
        elif action == "batch_upsert_items":
            if row[10] != "user_confirmed_ai_nutrition_v2":
                raise ValueError("舊版餐點草稿不支援批次食材估算")
            try:
                supplied_items = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("批次食材資料格式錯誤") from exc
            if not isinstance(supplied_items, list) or not 1 <= len(supplied_items) <= 8:
                raise ValueError("一次可更新1至8項食材")
            payload = normalize_meal_photo_payload(json.loads(row[5] or "{}"))
            updated_names = []
            added_names = []
            seen_names = set()
            for supplied in supplied_items:
                if not isinstance(supplied, dict):
                    raise ValueError("批次食材資料格式錯誤")
                name = _short_text(supplied.get("name"), "食材名稱", maximum=60)
                name_key = re.sub(r"\s+", "", name).casefold()
                if name_key in seen_names:
                    raise ValueError("同一批次不可重複相同食材名稱")
                seen_names.add(name_key)
                portion = _short_text(supplied.get("portion"), "食材份量", maximum=40)
                calories = _estimate_number(supplied.get("calories_kcal"), "食材熱量", 3000)
                protein = _estimate_number(supplied.get("protein_g"), "食材蛋白質", 300)
                next_item = {
                    "name": name, "portion": portion,
                    "calories_kcal": calories, "protein_g": protein,
                }
                for nutrient, limit in (("calories_kcal", 3000), ("protein_g", 300)):
                    supplied_range = supplied.get(f"{nutrient}_range")
                    if supplied_range is not None:
                        if not isinstance(supplied_range, dict) or set(supplied_range) != {"min", "max"}:
                            raise ValueError("新增食材營養區間格式錯誤")
                        minimum = _estimate_number(supplied_range["min"], f"{nutrient}下限", limit)
                        maximum = _estimate_number(supplied_range["max"], f"{nutrient}上限", limit)
                        if not minimum <= next_item[nutrient] <= maximum:
                            raise ValueError("新增食材估算值不在區間內")
                        next_item[f"{nutrient}_range"] = {"min": minimum, "max": maximum}
                source = str(supplied.get("nutrition_source") or "user_provided")
                if source not in {"ai_text_estimate", "owner_private_catalog", "user_provided"}:
                    raise ValueError("新增食材營養來源不支援")
                next_item["nutrition_source"] = source
                intent = str(supplied.get("_choice_intent") or "auto")
                target_item_id = str(supplied.get("_target_item_id") or "")
                if intent not in {"auto", "modify", "extra", "new"}:
                    raise ValueError("同名食材處理方式無效")
                current_items = [dict(item) for item in payload["ai_estimate"]["items"]]
                matches = [
                    (index, item) for index, item in enumerate(current_items)
                    if re.sub(r"\s+", "", str(item.get("name") or "")).casefold() == name_key
                ]
                if len(matches) > 1 and intent != "extra":
                    raise ValueError(f"「{name}」有多個同名食材，請先選擇要修改哪一項")
                if intent == "modify":
                    targeted = [pair for pair in matches if pair[1].get("item_id") == target_item_id]
                    if len(targeted) != 1:
                        raise ValueError("原食材已變更，請重新新增")
                    index, old_item = targeted[0]
                    current_items[index] = {"item_id": old_item["item_id"], **next_item}
                    payload, _estimate = _reconcile_ai_estimate_items(
                        payload, current_items, provider_aggregate_authoritative=True,
                    )
                    updated_names.append(name)
                elif intent == "extra":
                    item_id = "item_" + hashlib.sha256(
                        f"{event_id}|extra|{name_key}|{portion}".encode()
                    ).hexdigest()[:12]
                    payload, _estimate = _mutate_ai_estimate_items(
                        payload, add_item={"item_id": item_id, **next_item},
                    )
                    added_names.append(name)
                elif intent == "new":
                    if matches:
                        raise ValueError("餐點食材已變更，請重新選擇同名處理方式")
                    item_id = "item_" + hashlib.sha256(
                        f"{event_id}|new|{name_key}|{portion}".encode()
                    ).hexdigest()[:12]
                    payload, _estimate = _mutate_ai_estimate_items(
                        payload, add_item={"item_id": item_id, **next_item},
                    )
                    added_names.append(name)
                elif matches:
                    index, old_item = matches[0]
                    current_items[index] = {"item_id": old_item["item_id"], **next_item}
                    payload, _estimate = _reconcile_ai_estimate_items(
                        payload, current_items, provider_aggregate_authoritative=True,
                    )
                    updated_names.append(name)
                else:
                    item_id = "item_" + hashlib.sha256(
                        f"{event_id}|{name_key}|{portion}".encode()
                    ).hexdigest()[:12]
                    payload, _estimate = _mutate_ai_estimate_items(
                        payload, add_item={"item_id": item_id, **next_item},
                    )
                    added_names.append(name)
            estimate = _ai_estimate_snapshot(payload["ai_estimate"])
            result = {
                "kind": "estimate", "version": next_version,
                "updated_names": updated_names, "added_names": added_names,
            }
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET observed_payload_json=?,estimate_json=?,status='estimated',updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=?
                     AND status='awaiting_item_name'
                     AND workflow_version='user_confirmed_ai_nutrition_v2'""",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                 json.dumps(estimate, ensure_ascii=False, sort_keys=True, allow_nan=False),
                 now, next_version, token, user_id, current_version),
            ).rowcount
        elif action == "add_item":
            if row[10] != "user_confirmed_ai_nutrition_v2":
                # 舊確認卡只傳食材名稱與分類；保留未估算語意，不製造0營養快照。
                name = _short_text(value, "食材名稱", maximum=60)
                payload = normalize_meal_photo_payload(json.loads(row[5] or "{}"))
                if len(payload["visible_items"]) >= 12:
                    raise ValueError("一筆餐點最多確認12項食材")
                payload["visible_items"].append({
                    "name": name, "category": field, "confidence": 1.0,
                })
                result = {"kind": "updated", "version": next_version}
                changed = conn.execute(
                    """UPDATE pending_meal_photo_drafts
                       SET observed_payload_json=?,status='awaiting_confirmation',updated_at=?,version=?
                       WHERE token=? AND user_id=? AND version=?
                         AND status='awaiting_item_name'
                         AND workflow_version<>'user_confirmed_ai_nutrition_v2'""",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                     now, next_version, token, user_id, current_version),
                ).rowcount
            else:
                try:
                    supplied = json.loads(value)
                    name = _short_text(supplied.get("name"), "食材名稱", maximum=60)
                    portion = _short_text(supplied.get("portion"), "食材份量", maximum=40)
                    calories = _estimate_number(supplied.get("calories_kcal"), "食材熱量", 3000)
                    protein = _estimate_number(supplied.get("protein_g"), "食材蛋白質", 300)
                    add_item = {
                        "name": name, "portion": portion,
                        "calories_kcal": calories, "protein_g": protein,
                    }
                    for nutrient, limit in (("calories_kcal", 3000), ("protein_g", 300)):
                        supplied_range = supplied.get(f"{nutrient}_range")
                        if supplied_range is not None:
                            if not isinstance(supplied_range, dict) or set(supplied_range) != {"min", "max"}:
                                raise ValueError("新增食材營養區間格式錯誤")
                            minimum = _estimate_number(supplied_range["min"], f"{nutrient}下限", limit)
                            maximum = _estimate_number(supplied_range["max"], f"{nutrient}上限", limit)
                            if not minimum <= add_item[nutrient] <= maximum:
                                raise ValueError("新增食材估算值不在區間內")
                            add_item[f"{nutrient}_range"] = {"min": minimum, "max": maximum}
                    source = str(supplied.get("nutrition_source") or "user_provided")
                    if source not in {"ai_text_estimate", "owner_private_catalog", "user_provided"}:
                        raise ValueError("新增食材營養來源不支援")
                    add_item["nutrition_source"] = source
                except (AttributeError, json.JSONDecodeError) as exc:
                    raise ValueError("新增食材資料格式錯誤") from exc
                item_id = "item_" + hashlib.sha256(
                    f"{event_id}|{name}|{portion}".encode()
                ).hexdigest()[:12]
                payload, estimate = _mutate_ai_estimate_items(
                    json.loads(row[5] or "{}"),
                    add_item={"item_id": item_id, **add_item},
                )
                result = {"kind": "estimate", "version": next_version}
                changed = conn.execute(
                    """UPDATE pending_meal_photo_drafts
                       SET observed_payload_json=?,estimate_json=?,status='estimated',updated_at=?,version=?
                       WHERE token=? AND user_id=? AND version=?
                         AND status='awaiting_item_name'
                         AND workflow_version='user_confirmed_ai_nutrition_v2'""",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                     json.dumps(estimate, ensure_ascii=False, sort_keys=True, allow_nan=False),
                     now, next_version, token, user_id, current_version),
                ).rowcount
        else:
            expected_step = next_meal_photo_step({"answers": answers})
            if field != expected_step:
                raise ValueError("餐點確認步驟不符，請使用最新按鈕")
            answers[field] = value
            if field == "protein_more" and value == "done":
                if not answers.get("protein_items"):
                    answers["protein_items"] = [{
                        "type": answers.get("protein_type"),
                        "portion": answers.get("protein_portion"),
                    }]
            elif field == "protein_extra_type":
                selected = {
                    str(item.get("type"))
                    for item in answers.get("protein_items") or []
                    if isinstance(item, Mapping)
                }
                selected.add(str(answers.get("protein_type")))
                if value in selected:
                    raise ValueError("這種蛋白質已經選過")
            elif field == "protein_extra_portion":
                items = list(answers.get("protein_items") or [])
                if not items:
                    items.append({
                        "type": answers.get("protein_type"),
                        "portion": answers.get("protein_portion"),
                    })
                if len(items) >= 4:
                    raise ValueError("一餐最多記錄4種蛋白質")
                items.append({
                    "type": answers.get("protein_extra_type"),
                    "portion": value,
                })
                answers["protein_items"] = items
                answers["protein_more"] = "done" if len(items) >= 4 else None
                answers["protein_extra_type"] = None
                answers["protein_extra_portion"] = None
            step = next_meal_photo_step({"answers": answers})
            if step == "complete":
                estimate = _estimate_from_answers(answers)
                status = "estimated"
                result = {
                    "kind": "estimate", "version": next_version, "estimate": estimate,
                }
            else:
                estimate = {}
                status = "confirming"
                result = {"kind": "question", "step": step, "version": next_version}
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET answers_json=?,estimate_json=?,status=?,updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=?
                     AND status IN ('awaiting_confirmation','confirming')""",
                (
                    json.dumps(answers, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    json.dumps(estimate, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    status, now, next_version, token, user_id, current_version,
                ),
            ).rowcount
        if changed != 1:
            raise ValueError("餐點草稿已被其他操作更新")
        if quota_batch_key or quota_batch_owner:
            committed = conn.execute(
                """UPDATE photo_ingredient_batch_quota_ops
                   SET status='committed',lease_expires_at='',updated_at=?
                   WHERE batch_key=? AND user_id=? AND parent_token=?
                     AND parent_version=? AND status='processing'
                     AND lease_owner=? AND charge_attempt_id=?""",
                (now, quota_batch_key, user_id, token, expected_version,
                 quota_batch_owner, quota_batch_owner),
            )
            if committed.rowcount != 1:
                raise ValueError("批次AI估算擁有權已變更")
        conn.execute(
            """INSERT INTO meal_photo_events
               (event_id,user_id,token,action,request_payload_hash,result_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                event_id, user_id, token, action, request_hash,
                json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False), now,
            ),
        )
        conn.commit()
        return {
            "replayed": False,
            "result": result,
            "draft": get_meal_photo_draft(conn, user_id=user_id, token=token),
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _correction_explicitly_adds_item(correction: str, item_name: str) -> bool:
    """Return whether correction explicitly authorizes adding this named item.

    The provider is not allowed to turn a generic correction into an unrelated
    addition after an item has been removed.  Require both an addition cue and
    a sufficiently specific name mention; a small qualifier allowlist supports
    natural wording such as ``另豆漿`` for provider name ``無糖豆漿``.
    """
    compact = re.sub(r"[\s，,。；;、：:]", "", str(correction or "")).lower()
    if not re.search(r"(?:另|新增|加回|加入|加上|再加|補上|還有|也有)", compact):
        return False
    name = re.sub(r"[\s，,。；;、：:]", "", str(item_name or "")).lower()
    candidates = {name}
    stripped = re.sub(r"^(?:無糖|有糖|低糖|微糖|低脂|全脂|原味)", "", name)
    if len(stripped) >= 2:
        candidates.add(stripped)
    return any(len(candidate) >= 2 and candidate in compact for candidate in candidates)


def apply_meal_photo_ai_adjustment(
    conn: sqlite3.Connection, *, event_id: str, user_id: str, token: str,
    expected_version: int, correction: str,
    estimate_provider: Callable[[str, Mapping[str, Any], str], Mapping[str, Any]],
    lease_seconds: int = 45,
) -> dict[str, Any]:
    """Leased/fenced AI revision; crash retry is bounded and late results cannot mutate."""
    ensure_meal_photo_schema(conn)
    event_id = _short_text(event_id, "event_id", maximum=180)
    user_id = _short_text(user_id, "user_id", maximum=120)
    correction = _short_text(correction, "修正內容", maximum=500)
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise ValueError("餐點草稿token無效")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("餐點畫面版本無效") from exc
    request_hash = hashlib.sha256(json.dumps({
        "user_id": user_id, "token": token, "expected_version": expected_version,
        "action": "adjust_estimate", "correction": correction,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    now_dt = datetime.now(TAIPEI_TZ)
    now = now_dt.isoformat(timespec="seconds")
    claim_token = secrets.token_hex(16)
    lease_until = (now_dt + timedelta(seconds=max(5, min(int(lease_seconds), 120)))).isoformat(timespec="seconds")

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT user_id,token,action,request_payload_hash,result_json FROM meal_photo_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        recovering = False
        attempt = 1
        if existing:
            if tuple(existing[:4]) != (user_id, token, "adjust_estimate", request_hash):
                raise ValueError("餐點事件識別碼衝突")
            previous = json.loads(existing[4] or "{}")
            if previous.get("state") == "cancelled":
                raise ValueError("這筆修正已取消")
            if previous.get("state") != "processing":
                conn.commit()
                return {"replayed": True, "result": previous,
                        "draft": get_meal_photo_draft(conn, user_id=user_id, token=token)}
            if str(previous.get("lease_until") or "") > now:
                raise ValueError("這筆修正正在處理，請稍候")
            attempt = int(previous.get("attempt") or 1) + 1
            if attempt > 3:
                raise ValueError("這筆修正重試次數已達上限，請取消後重新操作")
            recovering = True
        row = conn.execute(
            """SELECT source_image_ref,observed_payload_json,estimate_json,status,
                      expires_at,version,workflow_version,removed_items_json
               FROM pending_meal_photo_drafts WHERE token=? AND user_id=?""", (token, user_id),
        ).fetchone()
        if not row:
            raise ValueError("找不到這筆餐點照片草稿")
        if _expired(row[4]):
            raise ValueError("這筆餐點照片草稿已逾時")
        expected_status = "adjusting" if recovering else "awaiting_adjustment"
        if row[3] != expected_status or row[6] != "user_confirmed_ai_nutrition_v2":
            raise ValueError("這筆餐點照片目前不接受文字修正")
        if int(row[5]) != expected_version:
            raise ValueError("餐點確認畫面已更新，請使用最新操作")
        original_payload = normalize_meal_photo_payload(json.loads(row[1] or "{}"))
        try:
            removed_items = json.loads(row[7] or "[]")
        except (TypeError, json.JSONDecodeError):
            removed_items = []
        if not isinstance(removed_items, list):
            removed_items = []
        if not meal_photo_estimate_snapshot_is_valid(json.loads(row[2] or "{}")):
            raise ValueError("原餐點估算完整性驗證失敗")
        processing = {"state": "processing", "claim_token": claim_token,
                      "lease_until": lease_until, "attempt": attempt}
        processing_json = json.dumps(processing, ensure_ascii=False, sort_keys=True)
        if recovering:
            changed_event = conn.execute(
                """UPDATE meal_photo_events SET result_json=? WHERE event_id=? AND result_json=?""",
                (processing_json, event_id, existing[4]),
            ).rowcount
            if changed_event != 1:
                raise ValueError("餐點修正租約已被其他程序接手")
        else:
            conn.execute(
                """INSERT INTO meal_photo_events
                   (event_id,user_id,token,action,request_payload_hash,result_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, user_id, token, "adjust_estimate", request_hash, processing_json, now),
            )
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts SET status='adjusting',updated_at=?
                   WHERE token=? AND user_id=? AND version=? AND status='awaiting_adjustment'""",
                (now, token, user_id, expected_version),
            ).rowcount
            if changed != 1:
                raise ValueError("餐點草稿已被其他操作更新")
        source_image_ref = str(row[0] or "")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

    try:
        provider_payload = estimate_provider(source_image_ref, original_payload, correction)
        # A provider can finish after cleanup/cancel.  Check authoritative draft
        # state before parsing or reconciling that late payload so its content
        # cannot mask the stale-result error semantics.
        current_state = conn.execute(
            """SELECT status,expires_at,version,workflow_version
               FROM pending_meal_photo_drafts WHERE token=? AND user_id=?""",
            (token, user_id),
        ).fetchone()
        if (
            not current_state
            or current_state[0] != "adjusting"
            or _expired(current_state[1])
            or int(current_state[2]) != expected_version
            or current_state[3] != "user_confirmed_ai_nutrition_v2"
        ):
            raise ValueError("這筆修正已取消或餐點草稿已更新")
        revised_payload = normalize_meal_photo_payload(provider_payload)
        if "ai_estimate" not in revised_payload:
            raise ValueError("AI修正結果缺少營養估算")
        current_items = [dict(item) for item in original_payload["ai_estimate"]["items"]]
        current_ids = {item["item_id"] for item in current_items}
        removed_ids = {
            str(item.get("item_id") or "")
            for item in removed_items if isinstance(item, Mapping)
        }
        available_by_name: dict[str, list[str]] = {}
        for item in current_items:
            available_by_name.setdefault(item["name"], []).append(item["item_id"])
        used_current_ids: set[str] = set()
        reconciled_items = []
        for item_value in revised_payload["ai_estimate"]["items"]:
            item = dict(item_value)
            item_id = item["item_id"]
            if item_id in current_ids and item_id not in used_current_ids:
                used_current_ids.add(item_id)
                reconciled_items.append(item)
                continue
            # Provider-generated IDs include portion/index, so bind an exact
            # same-name correction back to one unused persistent identity.
            same_name_id = next(
                (candidate for candidate in available_by_name.get(item["name"], [])
                 if candidate not in used_current_ids),
                None,
            )
            if same_name_id:
                item["item_id"] = same_name_id
                used_current_ids.add(same_name_id)
                reconciled_items.append(item)
                continue
            explicitly_added = _correction_explicitly_adds_item(correction, item["name"])
            if item_id in removed_ids:
                if not explicitly_added:
                    continue
                # Explicit re-add gets a fresh identity while retaining the
                # historical tombstone for the removed item.
                item["item_id"] = "item_" + hashlib.sha256(
                    f"{event_id}|explicit-readd|{item['name']}|{item['portion']}".encode()
                ).hexdigest()[:12]
            elif removed_items and not explicitly_added:
                raise ValueError("AI修正回傳無法對應的新食材，請明確新增或重新確認")
            reconciled_items.append(item)
        provider_items = revised_payload["ai_estimate"]["items"]
        if len(reconciled_items) != len(provider_items):
            revised_payload, revised_estimate = _reconcile_ai_estimate_items(
                revised_payload, reconciled_items,
            )
        else:
            # No tombstoned detail was filtered: retain the provider's aggregate
            # semantics.  This B3 gate is deliberately scoped to reconciliation.
            revised_payload["ai_estimate"]["items"] = reconciled_items
            revised_payload = normalize_meal_photo_payload(revised_payload)
            revised_estimate = _ai_estimate_snapshot(revised_payload["ai_estimate"])
        allowed_names = {item["name"] for item in revised_payload["ai_estimate"]["items"]}
        revised_payload["visible_items"] = [
            item for item in revised_payload["visible_items"] if item["name"] in allowed_names
        ]
        if not meal_photo_estimate_snapshot_is_valid(revised_estimate):
            raise ValueError("AI修正估算完整性驗證失敗")
        next_version = expected_version + 1
        result = {"kind": "estimate", "version": next_version}
        now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
        conn.execute("BEGIN IMMEDIATE")
        event_changed = conn.execute(
            """UPDATE meal_photo_events SET result_json=? WHERE event_id=? AND user_id=?
               AND token=? AND action='adjust_estimate' AND request_payload_hash=? AND result_json=?""",
            (json.dumps(result, ensure_ascii=False, sort_keys=True), event_id, user_id, token,
             request_hash, processing_json),
        ).rowcount
        if event_changed != 1:
            raise ValueError("這筆修正已取消或由其他程序接手")
        changed = conn.execute(
            """UPDATE pending_meal_photo_drafts SET observed_payload_json=?,estimate_json=?,
                   status='estimated',updated_at=?,version=? WHERE token=? AND user_id=?
                   AND version=? AND status='adjusting' AND workflow_version='user_confirmed_ai_nutrition_v2'""",
            (json.dumps(revised_payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
             json.dumps(revised_estimate, ensure_ascii=False, sort_keys=True, allow_nan=False),
             now, next_version, token, user_id, expected_version),
        ).rowcount
        if changed != 1:
            raise ValueError("這筆修正已取消或餐點草稿已更新")
        conn.commit()
        return {"replayed": False, "result": result,
                "draft": get_meal_photo_draft(conn, user_id=user_id, token=token)}
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        draft_status = conn.execute(
            "SELECT status FROM pending_meal_photo_drafts WHERE token=? AND user_id=?",
            (token, user_id),
        ).fetchone()
        if draft_status and draft_status[0] == "cancelled":
            conn.execute(
                """UPDATE meal_photo_events SET result_json=? WHERE event_id=? AND result_json=?""",
                (json.dumps({"state": "cancelled"}, sort_keys=True), event_id, processing_json),
            )
            conn.commit()
            raise ValueError("這筆修正已取消") from exc
        restored = conn.execute(
            """UPDATE pending_meal_photo_drafts SET status='awaiting_adjustment'
               WHERE token=? AND user_id=? AND version=? AND status='adjusting'
                 AND workflow_version='user_confirmed_ai_nutrition_v2'
                 AND EXISTS (
                   SELECT 1 FROM meal_photo_events
                    WHERE event_id=? AND user_id=? AND token=?
                      AND action='adjust_estimate' AND request_payload_hash=? AND result_json=?
                 )""",
            (token, user_id, expected_version, event_id, user_id, token,
             request_hash, processing_json),
        ).rowcount
        if restored == 1:
            deleted = conn.execute(
                """DELETE FROM meal_photo_events WHERE event_id=? AND user_id=? AND token=?
                   AND action='adjust_estimate' AND request_payload_hash=? AND result_json=?""",
                (event_id, user_id, token, request_hash, processing_json),
            ).rowcount
            if deleted != 1:
                conn.rollback()
                raise RuntimeError("餐點修正失敗清理交易不一致") from exc
        conn.commit()
        raise


def _is_confirmed_zero_range(value: Mapping[str, Any] | None) -> bool:
    return bool(
        value
        and value.get("basis") == "user_confirmed_none"
        and float(value.get("min", -1)) == 0
        and float(value.get("max", -1)) == 0
    )


def _initial_meal_photo_review(estimate: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("protein_total_exchange", "starch_exchange", "vegetable_exchange"):
        if not isinstance(estimate.get(key), Mapping):
            raise ValueError("仍有NA份量，請先重新上傳或完成確認後再核准")
    review: dict[str, Any] = {
        "protein_class": None,
        "protein_exchange": None,
        "starch_exchange": None,
        "vegetable_exchange": None,
        "milk_exchange": None,
        "fruit_exchange": None,
    }
    if _is_confirmed_zero_range(estimate.get("protein_total_exchange")):
        review["protein_class"] = "none"
        review["protein_exchange"] = 0.0
    if _is_confirmed_zero_range(estimate.get("starch_exchange")):
        review["starch_exchange"] = 0.0
    if _is_confirmed_zero_range(estimate.get("vegetable_exchange")):
        review["vegetable_exchange"] = 0.0
    return review


def next_meal_photo_review_step(draft: Mapping[str, Any]) -> str:
    review = dict(draft.get("review") or {})
    for field in (
        "protein_class", "protein_exchange", "starch_exchange",
        "vegetable_exchange", "milk_exchange", "fruit_exchange",
    ):
        if review.get(field) is None:
            return field
    return "complete"


def _half_step_values(minimum: float, maximum: float) -> list[float]:
    start = math.ceil(minimum * 2 - 1e-9)
    end = math.floor(maximum * 2 + 1e-9)
    values = [value / 2 for value in range(start, end + 1)]
    if not values or len(values) > 12:
        raise ValueError("正式份量範圍無法產生安全選項")
    return values


def meal_photo_review_options(draft: Mapping[str, Any], field: str) -> list[dict[str, str]]:
    estimate = dict(draft.get("estimate") or {})
    if field == "protein_class":
        return [
            {"label": "低脂蛋白", "value": "low"},
            {"label": "中脂蛋白", "value": "medium"},
            {"label": "高脂蛋白", "value": "high"},
        ]
    range_key = {
        "protein_exchange": "protein_total_exchange",
        "starch_exchange": "starch_exchange",
        "vegetable_exchange": "vegetable_exchange",
    }.get(field)
    if range_key:
        value = estimate.get(range_key)
        if not isinstance(value, Mapping):
            raise ValueError("這項正式份量仍為NA")
        numbers = _half_step_values(float(value["min"]), float(value["max"]))
    elif field in {"milk_exchange", "fruit_exchange"}:
        numbers = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    else:
        raise ValueError("餐點審核欄位不支援")
    return [
        {"label": f"{number:g}份", "value": f"{number:g}"}
        for number in numbers
    ]


def _meal_photo_exact_exchange(review: Mapping[str, Any]) -> dict[str, float]:
    if any(review.get(field) is None for field in (
        "protein_class", "protein_exchange", "starch_exchange",
        "vegetable_exchange", "milk_exchange", "fruit_exchange",
    )):
        raise ValueError("正式份量尚未選完")
    protein_class = str(review.get("protein_class") or "")
    protein_value = float(review.get("protein_exchange") or 0)
    if protein_class not in {"none", "low", "medium", "high"}:
        raise ValueError("蛋白質分類不支援")
    if protein_class == "none" and protein_value != 0:
        raise ValueError("蛋白質分類與份量不一致")
    result = {
        "milk_exchange": float(review["milk_exchange"]),
        "protein_low_exchange": protein_value if protein_class == "low" else 0.0,
        "protein_medium_exchange": protein_value if protein_class == "medium" else 0.0,
        "protein_high_exchange": protein_value if protein_class == "high" else 0.0,
        "starch_exchange": float(review["starch_exchange"]),
        "vegetable_exchange": float(review["vegetable_exchange"]),
        "fruit_exchange": float(review["fruit_exchange"]),
        "fat_exchange": 0.0,
    }
    return {key: round(value, 4) for key, value in result.items()}


def apply_meal_photo_review_action(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    user_id: str,
    admin_user_id: str,
    required_admin_user_id: str,
    token: str,
    expected_version: int,
    action: str,
    field: str = "",
    value: str = "",
) -> dict[str, Any]:
    """管理員本人以durable event選定單值並原子寫入正式approval/log。"""
    ensure_meal_photo_schema(conn)
    ensure_nutrition_schema(conn)
    event_id = _short_text(event_id, "event_id", maximum=180)
    user_id = _short_text(user_id, "user_id", maximum=120)
    admin_user_id = _short_text(admin_user_id, "admin_user_id", maximum=120)
    required_admin_user_id = str(required_admin_user_id or "").strip()
    if (
        not required_admin_user_id
        or len(required_admin_user_id) > 120
        or admin_user_id != required_admin_user_id
    ):
        raise PermissionError("管理員限定")
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise ValueError("餐點草稿token無效")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("餐點審核畫面版本無效") from exc
    if expected_version < 1 or action not in {
        "start", "set", "cancel_review", "reject", "approve"
    }:
        raise ValueError("餐點審核操作不支援")
    if action != "set":
        field = ""
        value = ""
    request = {
        "user_id": user_id, "admin_user_id": admin_user_id, "token": token,
        "expected_version": expected_version, "action": action,
        "field": str(field or ""), "value": str(value or ""),
    }
    request_hash = hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            """SELECT user_id,token,action,request_payload_hash,result_json
               FROM meal_photo_events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if existing:
            if (
                existing[0] != user_id or existing[1] != token or existing[2] != action
                or existing[3] != request_hash
            ):
                raise ValueError("餐點審核事件識別碼衝突")
            result = json.loads(existing[4])
            if action == "approve":
                if not isinstance(result, dict):
                    raise ValueError("餐點核准重播紀錄驗證失敗")
                confirmed = _confirmed_result(
                    conn, str(result.get("log_id") or ""), already_confirmed=True
                )
                confirmed_log = confirmed.get("log") or {}
                if (
                    confirmed_log.get("exchange_review_status") != "approved"
                    or confirmed_log.get("exchange_approval_id") != result.get("approval_id")
                ):
                    raise ValueError("餐點核准重播紀錄驗證失敗")
            conn.commit()
            return {
                "replayed": True, "result": result,
                "draft": get_meal_photo_draft(conn, user_id=user_id, token=token),
            }
        row = conn.execute(
            """SELECT observed_payload_json,answers_json,estimate_json,review_json,status,
                      expires_at,version,source_image_ref,meal_slot,consumed_at
               FROM pending_meal_photo_drafts WHERE token=? AND user_id=?""",
            (token, user_id),
        ).fetchone()
        if not row:
            raise ValueError("找不到這筆餐點照片草稿")
        observed = json.loads(row[0] or "{}")
        answers = json.loads(row[1] or "{}")
        estimate = json.loads(row[2] or "{}")
        review = json.loads(row[3] or "{}")
        status, expires_at, current_version = row[4], row[5], int(row[6])
        if _expired(expires_at):
            raise ValueError("這筆餐點照片草稿已逾時")
        if current_version != expected_version:
            raise ValueError("餐點審核畫面已更新，請使用最新按鈕")
        next_version = current_version + 1
        now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
        formal_result: dict[str, Any] = {}
        if action == "start":
            if status != "estimated":
                raise ValueError("這筆餐點照片目前不能開始審核")
            review = _initial_meal_photo_review(estimate)
            step = next_meal_photo_review_step({"review": review})
            status = "review_ready" if step == "complete" else "reviewing"
            result = {
                "kind": "review_ready" if step == "complete" else "review_question",
                "version": next_version,
            }
            if step != "complete":
                result["step"] = step
        elif action == "set":
            if status != "reviewing":
                raise ValueError("這筆餐點照片目前不能修改審核值")
            step = next_meal_photo_review_step({"review": review})
            if field != step:
                raise ValueError("餐點審核步驟不符，請使用最新按鈕")
            allowed = {item["value"] for item in meal_photo_review_options(
                {"estimate": estimate, "review": review}, field
            )}
            value = str(value or "").strip()
            if value not in allowed:
                raise ValueError("正式份量選項不支援")
            review[field] = value if field == "protein_class" else float(value)
            step = next_meal_photo_review_step({"review": review})
            status = "review_ready" if step == "complete" else "reviewing"
            result = {
                "kind": "review_ready" if step == "complete" else "review_question",
                "version": next_version,
            }
            if step != "complete":
                result["step"] = step
        elif action == "cancel_review":
            if status not in {"reviewing", "review_ready"}:
                raise ValueError("這筆餐點照片目前不在審核中")
            review = {}
            status = "estimated"
            result = {"kind": "review_cancelled", "version": next_version}
        elif action == "reject":
            if status not in {"reviewing", "review_ready"}:
                raise ValueError("這筆餐點照片目前不能退回")
            review = {"rejected_by": admin_user_id, "rejected_at": now}
            status = "rejected"
            result = {
                "kind": "rejected", "version": next_version,
                "rejected_by": admin_user_id, "rejected_at": now,
            }
        else:
            if status != "review_ready" or next_meal_photo_review_step({"review": review}) != "complete":
                raise ValueError("正式份量尚未選完")
            exact = _meal_photo_exact_exchange(review)
            formal_result = insert_approved_meal_photo_log(
                conn, token=token, user_id=user_id, reviewer=admin_user_id,
                consumed_at=row[9], meal_slot=row[8], source_image_ref=row[7],
                observed_payload=observed, answers=answers, exact_exchange=exact,
                estimate=estimate,
            )
            status = "approved"
            result = {
                "kind": "approved", "version": next_version,
                "log_id": formal_result["log_id"],
                "approval_id": formal_result["approval_id"],
                "approved_exchange": exact,
                "estimated_nutrition": formal_result["estimated_nutrition"],
            }
        changed = conn.execute(
            """UPDATE pending_meal_photo_drafts
               SET review_json=?,status=?,approved_log_id=?,approved_at=?,approved_by=?,
                   updated_at=?,version=?
               WHERE token=? AND user_id=? AND version=?""",
            (
                json.dumps(review, ensure_ascii=False, sort_keys=True, allow_nan=False), status,
                formal_result.get("log_id", ""), now if action == "approve" else "",
                admin_user_id if action == "approve" else "", now, next_version,
                token, user_id, current_version,
            ),
        ).rowcount
        if changed != 1:
            raise ValueError("餐點審核已被其他操作更新")
        conn.execute(
            """INSERT INTO meal_photo_events
               (event_id,user_id,token,action,request_payload_hash,result_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                event_id, user_id, token, action, request_hash,
                json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False), now,
            ),
        )
        conn.commit()
        return {
            "replayed": False, "result": result,
            "draft": get_meal_photo_draft(conn, user_id=user_id, token=token),
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _format_exchange_range(value: Mapping[str, Any] | None, label: str) -> str:
    if value is None:
        return f"{label}：NA（待確認）"
    minimum = float(value["min"])
    maximum = float(value["max"])
    if value.get("basis") == "user_confirmed_none" and minimum == maximum == 0:
        return f"{label}：0份（使用者確認沒有）"
    return f"{label}：約{minimum:g}～{maximum:g}份（照片＋手掌份量估算）"


def build_meal_photo_estimate_bubble(
    draft: Mapping[str, Any], *, allow_admin_review: bool = False
) -> dict[str, Any]:
    estimate = dict(draft.get("estimate") or {})
    if draft.get("status") != "estimated" or not estimate:
        raise ValueError("餐點照片尚未完成估算")
    line = lambda text, color="#333333", size="sm": {
        "type": "text", "text": text, "wrap": True, "size": size, "color": color,
    }
    oil_text = {
        "none": "沒有／水煮蒸烤", "light": "少油", "normal": "一般用油",
        "heavy": "多油／油炸", "unknown": "NA（不確定）",
    }.get(str(estimate.get("cooking_oil_confirmation") or ""), "NA（待確認）")
    sauce_text = {
        "none": "沒有", "little": "少量", "half": "約一半",
        "all": "全部", "unknown": "NA（不確定）",
    }.get(str(estimate.get("sauce_confirmation") or ""), "NA（待確認）")
    protein_type_labels = {
        "chicken": "雞肉", "pork": "豬肉", "fish": "魚類", "egg": "蛋",
        "tofu": "豆製品", "other": "其他蛋白質", "unknown": "不確定蛋白質",
    }
    protein_items = estimate.get("protein_items")
    protein_lines = []
    protein_total_label = "蛋白質食物"
    if isinstance(protein_items, list) and len(protein_items) > 1:
        for item in protein_items:
            if not isinstance(item, Mapping):
                continue
            label = protein_type_labels.get(str(item.get("type") or ""), "蛋白質")
            protein_lines.append(line(_format_exchange_range(item.get("exchange"), label)))
        protein_total_label = "蛋白質食物合計"
    ai_nutrition = estimate.get("rule_version") == "ai-vision-nutrition-estimate-v1"
    if ai_nutrition:
        calories_range = estimate["calories_kcal_range"]
        protein_range = estimate["protein_g_range"]
        nutrition_lines = []
        for item in estimate.get("estimate_items", []):
            item_calories_range = item.get("calories_kcal_range")
            item_protein_range = item.get("protein_g_range")
            source = item.get("nutrition_source")
            if source == "ai_text_estimate" and item_calories_range and item_protein_range:
                item_text = (
                    f"{item['name']}：{item['portion']}（AI估算約{float(item['calories_kcal']):g} kcal／"
                    f"{float(item_calories_range['min']):g}～{float(item_calories_range['max']):g}；"
                    f"蛋白質{float(item['protein_g']):g} g／"
                    f"{float(item_protein_range['min']):g}～{float(item_protein_range['max']):g}）"
                )
            elif source == "owner_private_catalog":
                item_text = (
                    f"{item['name']}：{item['portion']}（私人食材資料約"
                    f"{float(item['calories_kcal']):g} kcal／蛋白質{float(item['protein_g']):g} g）"
                )
            else:
                item_text = f"{item['name']}：{item['portion']}（約{float(item['calories_kcal']):g} kcal／蛋白質{float(item['protein_g']):g} g）"
            nutrition_lines.append({
                "type": "box", "layout": "horizontal", "spacing": "sm",
                "contents": [
                    {
                        "type": "box", "layout": "vertical", "flex": 1,
                        "contents": [line(item_text)],
                    },
                    {
                        "type": "box", "layout": "vertical", "width": "64px", "flex": 0,
                        "height": "44px", "justifyContent": "center",
                        "contents": [{
                            "type": "button", "style": "secondary", "height": "sm",
                            "action": {"type": "postback", "label": "移除",
                                       "data": f"mp:v1:{draft.get('token')}:{int(draft.get('version') or 0)}:remove_item:{item['item_id']}",
                                       "displayText": f"移除{item['name']}"},
                        }],
                    },
                ],
            })
        nutrition_lines.extend([
            line(
                f"熱量：約{float(estimate['calories_kcal']):g} kcal（{float(calories_range['min']):g}～{float(calories_range['max']):g}）",
                "#7A4E00",
            ),
            line(
                f"蛋白質：約{float(estimate['protein_g']):g} g（{float(protein_range['min']):g}～{float(protein_range['max']):g}）",
                "#7A4E00",
            ),
        ])
    else:
        nutrition_lines = [
            line("熱量：NA（沒有營養標示，無法精確判定）", "#B00020"),
            line(_format_exchange_range(estimate.get("starch_exchange"), "主食")),
            *protein_lines,
            line(_format_exchange_range(
                estimate.get("protein_total_exchange"), protein_total_label
            )),
            line(_format_exchange_range(estimate.get("vegetable_exchange"), "蔬菜")),
            line(f"烹調用油：{oil_text}（使用者確認）"),
            line(f"湯汁／醬汁：{sauce_text}（使用者確認）"),
        ]
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFF3CD",
            "contents": [line("📊 照片估算｜尚未計入正式份量", "#7A4E00", "md")],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                *nutrition_lines,
                line("⚠️ 待營養師審核，尚未扣入個人營養計畫。", "#B26A00"),
                line("未知維持NA；只有你明確選『沒有』才顯示0份。", "#777777", "xs"),
            ],
        },
    }
    token = str(draft.get("token") or "")
    version = int(draft.get("version") or 0)
    if not re.fullmatch(r"[0-9a-f]{12}", token) or version < 1:
        raise ValueError("餐點確認資料無效")
    if draft.get("workflow_version") == "expert_review_v1":
        if allow_admin_review:
            bubble["footer"] = {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [{
                    "type": "button", "style": "primary", "color": "#0F766E",
                    "action": {
                        "type": "postback", "label": "審核並加入",
                        "data": f"mpr:v1:{token}:{version}:start",
                        "displayText": "審核這筆餐點照片份量",
                    },
                }],
            }
    else:
        bubble["header"]["contents"][0]["text"] = "📊 照片估算｜確認後正式記錄"
        bubble["body"]["contents"][-2]["text"] = (
            "確認後會記入飲食紀錄；AI照片估算，非營養師核准，不會冒充營養師核准值。"
        )
        meal_slot = str(draft.get("meal_slot") or "").strip()
        meal_slot_label = meal_slot if meal_slot in {"早餐", "午餐", "晚餐", "點心"} else "未指定"
        bubble["body"]["contents"].insert(0, line(f"餐別：{meal_slot_label}（確認前可更改）", "#0F766E"))
        bubble["footer"] = {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "box", "layout": "horizontal", "spacing": "xs",
                    "contents": [{
                        "type": "button", "style": "primary" if slot == meal_slot else "secondary",
                        "height": "sm", "flex": 1,
                        "action": {
                            "type": "postback", "label": slot,
                            "data": f"mp:v1:{token}:{version}:meal:{slot}",
                            "displayText": f"這餐是{slot}",
                        },
                    } for slot in ("早餐", "午餐", "晚餐", "點心")],
                },
                {
                    "type": "button", "style": "primary", "color": "#0F766E",
                    "action": {
                        "type": "postback", "label": "確認記錄",
                        "data": f"mp:v1:{token}:{version}:confirm_estimate",
                        "displayText": "確認照片並記錄這餐",
                    },
                },
                *([{
                    "type": "button", "style": "secondary",
                    "action": {
                        "type": "postback", "label": "調整一下",
                        "data": f"mp:v1:{token}:{version}:request_adjust",
                        "displayText": "調整這筆AI餐點估算",
                    },
                }] if ai_nutrition else []),
                *([{
                    "type": "button", "style": "secondary",
                    "action": {
                        "type": "postback", "label": "➕ 新增食材",
                        "data": f"mp:v1:{token}:{version}:request_add",
                        "displayText": "新增這餐的食材",
                    },
                }] if ai_nutrition else []),
                {
                    "type": "button", "style": "secondary",
                    "action": {
                        "type": "postback", "label": "取消",
                        "data": f"mp:v1:{token}:{version}:cancel",
                        "displayText": "取消這筆餐點照片記錄",
                    },
                },
            ],
        }
    return bubble


def build_confirmed_meal_photo_record_actions(
    *, log_id: str, log_version: int, allow_confirmed_revision: bool,
    revision_label: str = "修改這餐",
) -> list[dict[str, Any]]:
    """Build only live, version-fenced controls for a confirmed meal-photo log."""
    if not re.fullmatch(r"log_[a-f0-9]{16,32}", str(log_id or "")) or int(log_version) < 1:
        raise ValueError("已記錄餐點操作資料無效")
    actions = [{
        "type": "button", "style": "secondary", "height": "sm",
        "action": {"type": "message", "label": "重新查看", "text": "飲食紀錄"},
    }]
    if allow_confirmed_revision:
        actions.append({
            "type": "button", "style": "secondary", "height": "sm",
            "action": {
                "type": "postback", "label": revision_label,
                "data": f"mealrev:v1:{log_id}:{int(log_version)}:start",
                "displayText": revision_label,
            },
        })
    actions.append({
        "type": "button", "style": "secondary", "height": "sm",
        "action": {
            "type": "postback", "label": "撤銷紀錄",
            "data": f"foodlog:v1:{log_id}:{int(log_version)}:delete:ask",
            "displayText": "撤銷這筆飲食紀錄",
        },
    })
    return actions


def build_meal_photo_recorded_bubble(
    draft: Mapping[str, Any], *, allow_confirmed_revision: bool = False,
    log_id: str = "", log_version: int = 0,
) -> dict[str, Any]:
    """Render the persisted occurrence without implying expert approval."""
    source = dict(draft)
    source["status"] = "estimated"
    bubble = build_meal_photo_estimate_bubble(source)
    bubble["header"]["contents"][0]["text"] = "✅ 已記錄｜顧客確認・AI估算"
    meal_slot = str(draft.get("meal_slot") or "").strip()
    meal_slot_label = meal_slot if meal_slot in {"早餐", "午餐", "晚餐", "點心"} else "未指定"
    bubble["body"]["contents"][0]["text"] = f"餐別：{meal_slot_label}"
    bubble["body"]["contents"][-2]["text"] = (
        "這餐已記入飲食紀錄；估算區間未當成營養師核准值。"
    )
    bubble.pop("footer", None)
    if allow_confirmed_revision:
        bubble["footer"] = {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": build_confirmed_meal_photo_record_actions(
                log_id=log_id, log_version=log_version, allow_confirmed_revision=True,
            ),
        }
    return bubble


def build_meal_photo_confirmation_bubble(
    payload: Mapping[str, Any], *, token: str, consumed_at: str, version: int = 1
) -> dict[str, Any]:
    normalized = normalize_meal_photo_payload(payload)
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise ValueError("餐點草稿token無效")
    visible_items = normalized["visible_items"]
    protein_names = "、".join(
        item["name"] for item in visible_items if item["category"] == "protein"
    )
    uncertain = "、".join(normalized["uncertain_items"]) or "目前沒有"
    starch = {
        "visible": "畫面可見；種類／份量為NA（待確認）",
        "not_visible": "NA（待確認；畫面未見不代表沒有吃）",
        "unknown": "NA（待確認；無法判定）",
    }[normalized["starch_visibility"]]
    sauce = {
        "visible": "NA（待確認；畫面可見但用量未知）",
        "not_visible": "NA（待確認；畫面未見不代表沒有使用）",
        "unknown": "NA（待確認；無法判定）",
    }[normalized["oil_sauce_status"]]
    display_time = str(consumed_at or "").replace("T", " ")[:16] or "待確認"

    line = lambda text, color="#333333", size="sm": {
        "type": "text", "text": text, "wrap": True, "size": size, "color": color,
    }

    # 每項食材顯示 ❌ 按鈕
    item_rows = []
    for item in visible_items:
        cat_label = {"protein": "🥩", "starch": "🍚", "vegetable": "🥬", "other": "🍽️"}.get(item["category"], "🍽️")
        item_rows.append({
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"{cat_label} {item['name']}", "flex": 5, "size": "sm", "wrap": True},
                {"type": "button", "style": "secondary", "height": "sm", "flex": 2,
                 "action": {
                     "type": "postback", "label": "移除",
                     "data": f"mp:v1:{token}:{int(version)}:remove:{item['name']}",
                     "displayText": f"移除{item['name']}",
                 }},
            ],
        })

    # 如果沒有辨識到任何食材
    if not item_rows:
        item_rows.append(line("（未辨識到任何食材）", "#999999"))

    protein = (
        f"畫面可見 {protein_names}；種類／份量仍為NA（待確認）"
        if protein_names else "NA（待確認；畫面未見不代表沒有吃）"
    )

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFF3CD",
            "contents": [
                line("📷 餐點照片辨識｜待你確認", "#7A4E00", "md"),
                line("沒有營養標示，以下不是精確營養值", "#8A6D3B", "xs"),
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                line(f"時間：{display_time}"),
                line("辨識到的食材：", "#333333", "sm"),
                *item_rows,
                {"type": "separator", "margin": "sm"},
                line(f"不確定：{uncertain}", "#B26A00"),
                line(f"蛋白質食物：{protein}"),
                line(f"主食：{starch}"),
                line(f"烹調用油／醬汁：{sauce}"),
                line("熱量與交換份：NA（尚未估算）", "#B00020"),
                line("未知不會當成0；確認前不計入正式紀錄。", "#777777", "xs"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "secondary",
                    "action": {
                        "type": "postback", "label": "➕ 新增食材",
                        "data": f"mp:v1:{token}:{int(version)}:request_add",
                        "displayText": "新增食材",
                    },
                },
                {
                    "type": "button", "style": "primary", "color": "#E69500",
                    "action": {
                        "type": "postback", "label": "開始確認餐點",
                        "data": f"mp:v1:{token}:{int(version)}:start",
                        "displayText": "開始確認餐點",
                    },
                },
                {
                    "type": "button", "style": "secondary",
                    "action": {
                        "type": "postback", "label": "取消",
                        "data": f"mp:v1:{token}:{int(version)}:cancel",
                        "displayText": "取消餐點照片",
                    },
                },
            ],
        },
    }
