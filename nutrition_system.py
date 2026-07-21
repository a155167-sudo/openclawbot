"""一日樂食營養辨識、食品資料庫與配餐推薦的純資料層。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping


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


def _normalize_nutrients(values: Mapping[str, Any] | None) -> dict[str, float]:
    values = values or {}
    return {
        key: _number(values.get(key, 0), key, max_value=NUTRIENT_LIMITS[key])
        for key in NUTRIENT_KEYS
    }


def normalize_label_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """驗證並正規化 Vision 回傳的營養標示資料。"""
    if payload.get("status") not in (None, "success"):
        raise ValueError(str(payload.get("message") or "營養標示辨識失敗"))
    if payload.get("image_type") not in (None, "nutrition_label"):
        raise ValueError("圖片不是營養標示")

    product_name = str(payload.get("product_name") or "").strip()
    if not product_name:
        raise ValueError("product_name 不可空白")

    package_amount = _number(
        payload.get("package_amount"), "package_amount", allow_zero=False, max_value=100000
    )
    package_unit = str(payload.get("package_unit") or "").strip().lower()
    if package_unit not in {"g", "kg", "ml", "l", "份", "顆", "包", "瓶", "盒"}:
        raise ValueError("package_unit 不支援")

    servings = _number(
        payload.get("servings_per_package", 1), "servings_per_package", allow_zero=False, max_value=1000
    )
    confidence = _number(payload.get("confidence", 0), "confidence", max_value=1.0)
    per_serving = _normalize_nutrients(payload.get("per_serving"))
    per_100 = _normalize_nutrients(payload.get("per_100"))
    if per_serving["calories_kcal"] <= 0 or not any(
        per_serving[key] > 0 for key in ("protein_g", "fat_g", "carbohydrate_g")
    ):
        raise ValueError("營養標示缺少有效的每份熱量或三大營養素")
    if package_unit in {"g", "ml"} and per_100["calories_kcal"] > 0:
        serving_amount = package_amount / servings
        expected = per_100["calories_kcal"] * serving_amount / 100
        tolerance = max(5.0, per_serving["calories_kcal"] * 0.25)
        if abs(expected - per_serving["calories_kcal"]) > tolerance:
            raise ValueError("每份與每100單位的熱量資料不一致，請重新拍攝或人工確認")

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
        "confidence": confidence,
        "notes": str(payload.get("notes") or "").strip(),
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
    label: Mapping[str, Any], *, token: str, consumed_servings: float = 1
) -> dict[str, Any]:
    normalized = normalize_label_payload(label)
    nutrition = scale_nutrition(normalized["per_serving"], consumed_servings)
    amount = normalized["package_amount"] * float(consumed_servings) / normalized["servings_per_package"]
    brand_line = f"{normalized['brand']}｜" if normalized["brand"] else ""
    observed = normalized.get("observed_at") or "以LINE收到時間為準"
    body = [
        {"type": "text", "text": normalized["product_name"], "size": "xl", "weight": "bold", "wrap": True},
        {"type": "text", "text": f"{brand_line}{amount:g}{normalized['package_unit']}｜{float(consumed_servings):g}份", "size": "sm", "color": "#666666", "margin": "sm", "wrap": True},
        {"type": "text", "text": f"辨識時間：{observed}", "size": "xs", "color": "#999999", "margin": "xs", "wrap": True},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": f"熱量  {nutrition['calories_kcal']:g} kcal", "size": "md", "weight": "bold", "margin": "md"},
        {"type": "text", "text": f"蛋白質 {nutrition['protein_g']:g}g　脂肪 {nutrition['fat_g']:g}g", "size": "sm", "color": "#333333", "margin": "sm"},
        {"type": "text", "text": f"碳水 {nutrition['carbohydrate_g']:g}g　糖 {nutrition['sugar_g']:g}g", "size": "sm", "color": "#333333", "margin": "sm"},
        {"type": "text", "text": f"纖維 {nutrition['fiber_g']:g}g　鈉 {nutrition['sodium_mg']:g}mg", "size": "sm", "color": "#333333", "margin": "sm"},
        {"type": "text", "text": "確認後會加入你的私人食品庫與今日飲食紀錄；營養份量代號需營養師審核。", "size": "xs", "color": "#8A6D3B", "margin": "md", "wrap": True},
    ]
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
    conn.executescript(
        """
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
            source_image_ref TEXT DEFAULT '',
            plan_id TEXT DEFAULT '',
            plan_link_status TEXT NOT NULL DEFAULT 'pending',
            confirmation_status TEXT NOT NULL DEFAULT 'confirmed',
            legacy_applied_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(food_id) REFERENCES food_catalog(food_id)
        );

        CREATE TABLE IF NOT EXISTS pending_nutrition_logs (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            label_payload_json TEXT NOT NULL,
            source_image_ref TEXT DEFAULT '',
            source_message_id TEXT DEFAULT '',
            consumed_servings REAL NOT NULL DEFAULT 1,
            meal_slot TEXT DEFAULT '',
            consumed_at TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            confirmed_log_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT DEFAULT ''
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

        CREATE UNIQUE INDEX IF NOT EXISTS idx_food_catalog_owner_fingerprint
            ON food_catalog(owner_user_id, fingerprint, source_type);
        CREATE INDEX IF NOT EXISTS idx_food_catalog_owner ON food_catalog(owner_user_id);
        CREATE INDEX IF NOT EXISTS idx_food_logs_user_time ON food_logs(user_id, consumed_at);
        CREATE INDEX IF NOT EXISTS idx_plan_user_effective ON nutrition_plans(user_id, effective_from);
        """
    )
    migrations = {
        "pending_nutrition_logs": {
            "source_message_id": "TEXT DEFAULT ''",
            "confirmed_log_id": "TEXT DEFAULT ''",
        },
        "food_logs": {
            "legacy_applied_at": "TEXT DEFAULT ''",
            "plan_link_status": "TEXT NOT NULL DEFAULT 'pending'",
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
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_source_message
           ON pending_nutrition_logs(user_id, source_message_id)
           WHERE source_message_id <> ''"""
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
) -> str:
    normalized = normalize_label_payload(payload)
    if source_message_id:
        existing = conn.execute(
            "SELECT token FROM pending_nutrition_logs WHERE user_id=? AND source_message_id=?",
            (user_id, source_message_id),
        ).fetchone()
        if existing:
            return existing[0]
    token = uuid.uuid4().hex[:12]
    now_dt = datetime.now().astimezone()
    now = now_dt.isoformat(timespec="seconds")
    expires_at = (now_dt + timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        conn.execute(
            """
            INSERT INTO pending_nutrition_logs
            (token, user_id, label_payload_json, source_image_ref, source_message_id,
             consumed_servings, meal_slot, consumed_at, status, confirmed_log_id,
             created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)
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
        raise


def _confirmed_result(conn: sqlite3.Connection, log_id: str, *, already_confirmed: bool) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT f.food_id, f.product_name, f.brand, f.source_type,
               l.log_id, l.consumed_at, l.meal_slot, l.consumed_servings,
               l.consumed_amount, l.consumed_unit, l.nutrition_snapshot_json,
               l.exchange_snapshot_json, l.plan_id
        FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
        WHERE l.log_id=?
        """,
        (log_id,),
    ).fetchone()
    if not row:
        raise ValueError("已確認紀錄遺失，請聯繫客服")
    return {
        "already_confirmed": already_confirmed,
        "food": {"food_id": row[0], "product_name": row[1], "brand": row[2], "source_type": row[3]},
        "log": {
            "log_id": row[4], "consumed_at": row[5], "meal_slot": row[6],
            "consumed_servings": float(row[7]), "consumed_amount": float(row[8]),
            "consumed_unit": row[9], "nutrition": json.loads(row[10] or "{}"),
            "exchange": json.loads(row[11] or "{}"), "plan_id": row[12] or "",
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
        if expires_at and expires_at < now:
            conn.execute("UPDATE pending_nutrition_logs SET status='expired' WHERE token=?", (token,))
            conn.commit()
            raise ValueError("這筆確認已逾時，請重新上傳營養標示")

        label = normalize_label_payload(json.loads(payload_json))
        fingerprint = food_fingerprint(
            label["product_name"], label["brand"], label["package_amount"],
            label["package_unit"], label["per_serving"], barcode=label["barcode"],
            servings_per_package=label["servings_per_package"], per_100=label["per_100"],
        )
        existing = conn.execute(
            """
            SELECT food_id FROM food_catalog
            WHERE fingerprint=? AND (owner_user_id=? OR visibility='public')
            ORDER BY CASE WHEN owner_user_id=? THEN 0 ELSE 1 END LIMIT 1
            """,
            (fingerprint, user_id, user_id),
        ).fetchone()
        if existing:
            food_id = existing[0]
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
                VALUES (?, ?, ?, ?, 'user_private_food', ?, 'private', ?, ?, ?, ?, ?, '{}',
                        'pending_review', ?, ?, ?, 'user_confirmed', ?, ?)
                """,
                (
                    food_id, label["product_name"], label["brand"], label["barcode"], user_id,
                    label["package_amount"], label["package_unit"], label["servings_per_package"],
                    json.dumps(label["per_serving"], ensure_ascii=False, sort_keys=True, allow_nan=False),
                    json.dumps(label["per_100"], ensure_ascii=False, sort_keys=True, allow_nan=False),
                    fingerprint, source_image_ref, label["confidence"], now, now,
                ),
            )

        nutrition = scale_nutrition(label["per_serving"], servings)
        consumed_amount = round(label["package_amount"] * float(servings) / label["servings_per_package"], 4)
        log_id = new_id("log")
        conn.execute(
            """
            INSERT INTO food_logs
            (log_id, user_id, food_id, consumed_at, meal_slot, consumed_servings,
             consumed_amount, consumed_unit, nutrition_snapshot_json,
             exchange_snapshot_json, source_image_ref, plan_id, plan_link_status,
             confirmation_status, legacy_applied_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, 'confirmed', '', ?, ?)
            """,
            (
                log_id, user_id, food_id, consumed_at or now, meal_slot, float(servings),
                consumed_amount, label["package_unit"],
                json.dumps(nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False),
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


def daily_consumed_totals(
    conn: sqlite3.Connection, *, user_id: str, date_iso: str, meal_slot: str = ""
) -> dict[str, float]:
    totals = {key: 0.0 for key in (*NUTRIENT_KEYS, *EXCHANGE_KEYS)}
    sql = """
        SELECT nutrition_snapshot_json, exchange_snapshot_json
        FROM food_logs
        WHERE user_id=? AND substr(consumed_at, 1, 10)=? AND confirmation_status='confirmed'
    """
    params: list[Any] = [user_id, date_iso]
    if meal_slot:
        sql += " AND meal_slot=?"
        params.append(meal_slot)
    rows = conn.execute(sql, params).fetchall()
    for nutrition_json, exchange_json in rows:
        for key, value in json.loads(nutrition_json or "{}").items():
            if key in totals:
                totals[key] += float(value or 0)
        for key, value in json.loads(exchange_json or "{}").items():
            if key in totals:
                totals[key] += float(value or 0)
    return {key: round(value, 4) for key, value in totals.items()}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcish_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
