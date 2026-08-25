import hashlib
import hmac
import os
import json
import sqlite3
from datetime import datetime, timedelta
import secrets
import string
import base64
import copy
import csv
import fcntl
import random
import re
import requests
import threading
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

# Google & Web 相關套件
import gspread
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextSendMessage, TextMessage, ImageMessage, QuickReply,
    QuickReplyButton, MessageAction, PostbackAction, PostbackEvent,
    CameraAction, CameraRollAction, ImageSendMessage,
)
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager, closing
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from nutrition_system import (
    apply_nutrition_text_edit,
    approve_food_exchange_suggestion,
    attach_latest_pending_identity,
    build_label_confirmation_bubble,
    cancel_pending_label,
    confirm_pending_label,
    daily_consumed_totals,
    daily_food_summary,
    ensure_nutrition_schema,
    estimate_nutrition_from_exchanges,
    exchange_approval_hash,
    get_nutrition_input_state,
    normalize_garmin_payload,
    normalize_label_payload,
    nutrition_sheet_specs,
    quick_log_from_catalog,
    rank_menu_candidates,
    remaining_targets,
    save_pending_label,
    scale_nutrition,
    search_food_catalog,
    search_food_history,
    search_food_page,
    set_nutrition_input_state,
    clear_nutrition_input_state,
    update_pending_consumption,
)
from daily_health_report import (
    claim_daily_delivery,
    ensure_daily_health_schema,
    finish_daily_delivery,
    format_daily_health_report,
    get_daily_health_checkin,
    parse_health_checkin,
    save_daily_health_checkin,
    summarize_intervals_activities,
)
from meal_photo_system import (
    apply_meal_photo_action,
    build_meal_photo_confirmation_bubble,
    build_meal_photo_estimate_bubble,
    clear_meal_photo_image_ref,
    daily_pending_meal_photo_count,
    ensure_meal_photo_schema,
    get_meal_photo_draft,
    get_meal_photo_draft_for_admin,
    claim_meal_photo_notification,
    complete_meal_photo_notification,
    list_pending_meal_photo_reviews,
    meal_photo_step_options,
    release_meal_photo_notification,
    next_meal_photo_step,
    normalize_meal_photo_payload,
    save_meal_photo_draft,
)

# --- 1. 時區與基本工具設定 ---
TW_TZ = ZoneInfo("Asia/Taipei")

def tw_today():
    return datetime.now(TW_TZ).date()

def tw_now():
    return datetime.now(TW_TZ)


DAILY_FOOD_NUTRIENT_FIELDS = (
    "calories_kcal", "protein_g", "fat_g", "carbohydrate_g",
)


def ensure_daily_food_ledger_schema(conn: sqlite3.Connection) -> None:
    """為既有 food_logs 加入每日帳本編輯、版本與稽核欄位。"""
    ensure_nutrition_schema(conn)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
    additions = {
        "version": "INTEGER NOT NULL DEFAULT 1",
        "deleted_at": "TEXT NOT NULL DEFAULT ''",
        "nutrient_sources_json": "TEXT NOT NULL DEFAULT '{}'",
        "original_nutrition_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        "operation_key": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE food_logs ADD COLUMN {column} {definition}")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recent_meal_logs'"
    ).fetchone():
        recent_columns = {row[1] for row in conn.execute("PRAGMA table_info(recent_meal_logs)")}
        if "food_log_id" not in recent_columns:
            conn.execute("ALTER TABLE recent_meal_logs ADD COLUMN food_log_id TEXT DEFAULT ''")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_food_log_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            log_id TEXT NOT NULL,
            action TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_food_log_replay_snapshots (
            operation_key TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            log_id TEXT NOT NULL,
            flex_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_food_edit_states (
            user_id TEXT PRIMARY KEY,
            log_id TEXT NOT NULL,
            expected_version INTEGER NOT NULL,
            input_type TEXT NOT NULL,
            field TEXT NOT NULL DEFAULT '',
            pending_value REAL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_food_ledger_migrations (
            user_id TEXT NOT NULL,
            ledger_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, ledger_date)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_food_operation_key
            ON food_logs(operation_key) WHERE operation_key<>'';
        CREATE INDEX IF NOT EXISTS idx_daily_food_logs_user_date
            ON food_logs(user_id, consumed_at, confirmation_status, deleted_at);
        """
    )


def _ledger_number(value, *, allow_none=True):
    if value is None and allow_none:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("營養數值格式不正確") from exc
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        raise ValueError("營養數值必須是非負有限數字")
    return round(number, 4)


def _normalize_ledger_nutrition(nutrition: dict) -> dict:
    raw = nutrition or {}
    result = {}
    for field in DAILY_FOOD_NUTRIENT_FIELDS:
        if field in raw:
            result[field] = _ledger_number(raw.get(field))
    return result


def create_daily_food_log(
    conn: sqlite3.Connection, *, user_id: str, product_name: str,
    meal_slot: str, consumed_at: str, servings: float,
    nutrition: dict, source_type: str, operation_key: str = "",
) -> dict:
    """建立可編輯逐筆飲食紀錄；營養快照保留未知 None。"""
    if not conn.in_transaction:
        ensure_daily_food_ledger_schema(conn)
    operation_key = str(operation_key or "").strip()[:180]
    if operation_key:
        existing_log = conn.execute(
            """SELECT fl.log_id,fl.food_id,fc.product_name,fl.meal_slot,fl.consumed_at,
                      fl.consumed_servings,fl.nutrition_snapshot_json,fl.version
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.operation_key=? AND fl.user_id=?""", (operation_key, str(user_id or "").strip()),
        ).fetchone()
        if existing_log:
            return {
                "log_id": existing_log[0], "food_id": existing_log[1],
                "product_name": existing_log[2], "meal_slot": existing_log[3] or "",
                "consumed_at": existing_log[4], "servings": float(existing_log[5] or 0),
                "nutrition": json.loads(existing_log[6] or "{}"),
                "version": int(existing_log[7] or 1), "replayed": True,
            }
    user_id = str(user_id or "").strip()
    product_name = str(product_name or "").strip()[:120]
    source_type = str(source_type or "user_private_food").strip()[:60]
    meal_slot = str(meal_slot or "").strip()
    consumed_at = str(consumed_at or tw_now().isoformat(timespec="seconds"))[:50]
    if not user_id or not product_name:
        raise ValueError("飲食紀錄缺少使用者或品名")
    if meal_slot not in {"", "早餐", "午餐", "晚餐", "點心"}:
        raise ValueError("餐別不支援")
    servings = _ledger_number(servings, allow_none=False)
    if servings < 0.1 or servings > 100:
        raise ValueError("份量需介於 0.1～100 份")
    normalized = _normalize_ledger_nutrition(nutrition)
    if not normalized or all(value is None for value in normalized.values()):
        raise ValueError("至少需要一項可記錄的營養資料")

    fingerprint = hashlib.sha256(
        f"ledger|{user_id}|{source_type}|{product_name}".encode("utf-8")
    ).hexdigest()
    food_id = "ledger_" + fingerprint[:24]
    now = tw_now().isoformat(timespec="seconds")
    per_serving = {
        key: (None if value is None else round(value / servings, 4))
        for key, value in normalized.items()
    }
    conn.execute(
        """INSERT INTO food_catalog
           (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
            package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
            exchange_json,exchange_review_status,fingerprint,original_image_ref,
            recognition_confidence,verification_status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(food_id) DO UPDATE SET
             product_name=excluded.product_name,per_serving_json=excluded.per_serving_json,
             updated_at=excluded.updated_at""",
        (
            food_id, product_name, "", "", source_type, user_id, "private",
            1, "份", 1, json.dumps(per_serving, ensure_ascii=False, allow_nan=False),
            "{}", "{}", "pending_review", fingerprint, "", 0,
            "user_confirmed" if source_type != "ai_text_estimate" else "ai_estimated",
            now, now,
        ),
    )
    log_id = "log_" + uuid.uuid4().hex[:20]
    sources = {
        key: ("ai_estimate" if source_type == "ai_text_estimate" else source_type)
        for key in normalized
    }
    nutrition_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False)
    inserted = conn.execute(
        """INSERT INTO food_logs
           (log_id,user_id,food_id,consumed_at,meal_slot,consumed_servings,
            consumed_amount,consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,
            approved_exchange_json,exchange_approval_id,source_image_ref,plan_id,
            plan_link_status,confirmation_status,legacy_applied_at,created_at,updated_at,
            version,deleted_at,nutrient_sources_json,original_nutrition_snapshot_json,
            operation_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(operation_key) WHERE operation_key<>'' DO NOTHING""",
        (
            log_id, user_id, food_id, consumed_at, meal_slot, servings,
            servings, "份", nutrition_json, "{}", "{}", "", "", "",
            "no_plan", "confirmed", "not_applicable", now, now, 1, "",
            json.dumps(sources, ensure_ascii=False, sort_keys=True), nutrition_json,
            operation_key,
        ),
    )
    if inserted.rowcount == 0 and operation_key:
        existing_log = conn.execute(
            """SELECT fl.log_id,fl.food_id,fc.product_name,fl.meal_slot,fl.consumed_at,
                      fl.consumed_servings,fl.nutrition_snapshot_json,fl.version
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.operation_key=? AND fl.user_id=?""", (operation_key, str(user_id or "").strip()),
        ).fetchone()
        if existing_log:
            return {
                "log_id": existing_log[0], "food_id": existing_log[1],
                "product_name": existing_log[2], "meal_slot": existing_log[3] or "",
                "consumed_at": existing_log[4], "servings": float(existing_log[5] or 0),
                "nutrition": json.loads(existing_log[6] or "{}"),
                "version": int(existing_log[7] or 1), "replayed": True,
            }
        raise RuntimeError("飲食入帳事件識別碼衝突")
    try:
        conn.execute(
            """INSERT OR IGNORE INTO nutrition_sheet_outbox
               (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
               VALUES (?,'food_log',?,'pending',0,'',?,'')""",
            ("outbox_" + uuid.uuid4().hex[:20], log_id, now),
        )
    except sqlite3.OperationalError:
        pass
    return {
        "log_id": log_id, "food_id": food_id, "product_name": product_name,
        "meal_slot": meal_slot, "consumed_at": consumed_at,
        "servings": servings, "nutrition": normalized, "version": 1,
    }


def migrate_current_day_legacy_totals_to_ledger(conn: sqlite3.Connection) -> int:
    """首次部署時，把無法還原逐筆的今日舊總額保存成一筆明確的合計紀錄。"""
    ensure_daily_food_ledger_schema(conn)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_profile'"
    ).fetchone():
        return 0
    today = tw_today().isoformat()
    migrated = 0
    profiles = conn.execute(
        """SELECT user_id,today_extra_cal,today_extra_pro
           FROM health_profile WHERE today_date=?""",
        (today,),
    ).fetchall()
    for user_id, legacy_cal, legacy_pro in profiles:
        if conn.execute(
            "SELECT 1 FROM daily_food_ledger_migrations WHERE user_id=? AND ledger_date=?",
            (user_id, today),
        ).fetchone():
            continue
        rows = _daily_food_rows(conn, user_id, today)
        ledger_cal = ledger_pro = 0.0
        for row in rows:
            nutrition = _ledger_item_from_row(row)["nutrition"]
            if nutrition.get("calories_kcal") is not None:
                ledger_cal += float(nutrition["calories_kcal"])
            if nutrition.get("protein_g") is not None:
                ledger_pro += float(nutrition["protein_g"])
        cal_gap = round(float(legacy_cal or 0) - ledger_cal, 4)
        pro_gap = round(float(legacy_pro or 0) - ledger_pro, 4)
        carryover = {}
        if cal_gap > 0.01:
            carryover["calories_kcal"] = cal_gap
        if pro_gap > 0.01:
            carryover["protein_g"] = pro_gap
        if carryover:
            create_daily_food_log(
                conn, user_id=user_id,
                product_name="部署前合計（無逐筆明細）", meal_slot="",
                consumed_at=f"{today}T00:00:00+08:00", servings=1,
                nutrition=carryover, source_type="legacy_daily_carryover",
            )
        conn.execute(
            """INSERT OR IGNORE INTO daily_food_ledger_migrations
               (user_id,ledger_date,created_at) VALUES (?,?,?)""",
            (user_id, today, tw_now().isoformat(timespec="seconds")),
        )
        migrated += 1
    conn.commit()
    return migrated


def _daily_food_rows(conn: sqlite3.Connection, user_id: str, date_text: str) -> list[tuple]:
    # Caller must ensure schema before opening any write transaction. Never run
    # executescript here: sqlite3 would implicitly commit an active transaction.
    return conn.execute(
        """SELECT fl.log_id,fl.food_id,fc.product_name,fc.source_type,fl.consumed_at,
                  fl.meal_slot,fl.consumed_servings,fl.consumed_amount,fl.consumed_unit,
                  fl.nutrition_snapshot_json,fl.nutrient_sources_json,fl.version,
                  fl.approved_exchange_json,fc.fingerprint,a.food_fingerprint,
                  a.suggestion_rule_version,a.approved_exchange_hash
           FROM food_logs fl
           JOIN food_catalog fc ON fc.food_id=fl.food_id
           LEFT JOIN food_exchange_approvals a ON a.approval_id=fl.exchange_approval_id
           WHERE fl.user_id=? AND date(fl.consumed_at, '+8 hours')=?
             AND fl.confirmation_status='confirmed' AND COALESCE(fl.deleted_at,'')=''
           ORDER BY fl.consumed_at,fl.created_at,fl.log_id""",
        (user_id, date_text),
    ).fetchall()


def _ledger_item_from_row(row) -> dict:
    nutrition = json.loads(row[9] or "{}")
    # 已核准餐點照片以驗證過的交換份估算；未核准/無值維持 NA。
    if not nutrition and row[12] and row[13] and row[14] == row[13]:
        try:
            approved = json.loads(row[12] or "{}")
            expected = exchange_approval_hash(row[14], row[15], approved)
            if secrets.compare_digest(str(row[16] or ""), expected):
                nutrition = estimate_nutrition_from_exchanges(approved)
        except (TypeError, ValueError, json.JSONDecodeError):
            nutrition = {}
    return {
        "log_id": row[0], "food_id": row[1], "product_name": row[2],
        "source_type": row[3], "consumed_at": row[4], "meal_slot": row[5] or "",
        "servings": float(row[6] or 0), "consumed_amount": float(row[7] or 0),
        "consumed_unit": row[8] or "", "nutrition": nutrition,
        "nutrient_sources": json.loads(row[10] or "{}"), "version": int(row[11] or 1),
    }


def get_daily_food_ledger(user_id: str, date_text: str) -> dict:
    date_text = str(date_text or "").strip()
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期格式必須是 YYYY-MM-DD") from exc
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        rows = _daily_food_rows(conn, user_id, date_text)
        items = [_ledger_item_from_row(row) for row in rows]
        hp = conn.execute(
            "SELECT tdee,protein FROM health_profile WHERE user_id=?", (user_id,)
        ).fetchone()
    known_totals = {field: 0.0 for field in DAILY_FOOD_NUTRIENT_FIELDS}
    unknown_fields = set()
    known_counts = {field: 0 for field in DAILY_FOOD_NUTRIENT_FIELDS}
    for item in items:
        nutrition = item["nutrition"]
        for field in DAILY_FOOD_NUTRIENT_FIELDS:
            value = nutrition.get(field)
            if value is None:
                unknown_fields.add(field)
            else:
                known_totals[field] += float(value)
                known_counts[field] += 1
    totals = {
        field: (None if field in unknown_fields or known_counts[field] == 0 else round(value, 4))
        for field, value in known_totals.items()
    }
    return {
        "date": date_text, "items": items, "count": len(items),
        "totals": totals,
        "known_totals": {field: round(value, 4) for field, value in known_totals.items()},
        "known_counts": known_counts, "unknown_fields": unknown_fields,
        "tdee": float(hp[0] or 2000) if hp else 2000.0,
        "protein_goal": float(hp[1] or 100) if hp else 100.0,
    }


def _sync_health_profile_from_ledger_conn(
    conn: sqlite3.Connection, user_id: str, date_text: str,
    *, current_date: str = "",
) -> None:
    if date_text != (current_date or tw_today().isoformat()):
        return
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_profile'"
    ).fetchone():
        return
    if not conn.execute("SELECT 1 FROM health_profile WHERE user_id=?", (user_id,)).fetchone():
        return
    items = [_ledger_item_from_row(row) for row in _daily_food_rows(conn, user_id, date_text)]
    cal = pro = 0.0
    names = []
    for item in items:
        names.append(item["product_name"])
        nutrition = item["nutrition"]
        if nutrition.get("calories_kcal") is not None:
            cal += float(nutrition["calories_kcal"])
        if nutrition.get("protein_g") is not None:
            pro += float(nutrition["protein_g"])
    conn.execute(
        """UPDATE health_profile
           SET today_extra_cal=?,today_extra_pro=?,today_food_items=?,today_date=?
           WHERE user_id=?""",
        (round(cal, 4), round(pro, 4), "、".join(names), date_text, user_id),
    )


def apply_daily_food_log_edit(
    *, user_id: str, log_id: str, expected_version: int, event_id: str,
    action: str, field: str = "", value=None,
) -> dict:
    """以 log_id+version 原子修改單筆紀錄；同 event_id 可安全重播。"""
    event_id = str(event_id or "").strip()
    if not event_id:
        raise ValueError("缺少操作事件識別碼")
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT result_json FROM daily_food_log_events WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        ).fetchone()
        if previous:
            result = json.loads(previous[0])
            result["replayed"] = True
            conn.commit()
            return result
        row = conn.execute(
            """SELECT fl.version,fl.consumed_servings,fl.nutrition_snapshot_json,
                      fl.nutrient_sources_json,fl.consumed_at,fc.product_name,
                      fl.food_id,fc.source_type
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.log_id=? AND fl.user_id=? AND fl.confirmation_status='confirmed'
                 AND COALESCE(fl.deleted_at,'')=''""",
            (log_id, user_id),
        ).fetchone()
        if not row:
            raise ValueError("找不到這筆飲食紀錄")
        version, old_servings = int(row[0]), float(row[1] or 0)
        if version != int(expected_version):
            raise ValueError("這筆紀錄已更新，請重新開啟最新卡片")
        nutrition = json.loads(row[2] or "{}")
        sources = json.loads(row[3] or "{}")
        new_servings = old_servings
        new_name = ""
        if action == "correct_nutrition":
            if field not in DAILY_FOOD_NUTRIENT_FIELDS:
                raise ValueError("不支援的營養欄位")
            nutrition[field] = _ledger_number(value, allow_none=False)
            sources[field] = "user_nutrition_label"
        elif action == "patch_nutrition":
            if not isinstance(value, dict) or not value:
                raise ValueError("缺少營養修正資料")
            for nutrient_field, nutrient_value in value.items():
                if nutrient_field not in DAILY_FOOD_NUTRIENT_FIELDS:
                    raise ValueError("不支援的營養欄位")
                nutrition[nutrient_field] = _ledger_number(nutrient_value)
                sources[nutrient_field] = "user_portion_adjustment"
        elif action == "set_servings":
            new_servings = _ledger_number(value, allow_none=False)
            if new_servings < 0.1 or new_servings > 100 or old_servings <= 0:
                raise ValueError("份量需介於 0.1～100 份")
            ratio = new_servings / old_servings
            nutrition = {
                key: (None if nutrient is None else round(float(nutrient) * ratio, 4))
                for key, nutrient in nutrition.items()
            }
        elif action == "set_meal_slot":
            if str(value) not in {"早餐", "午餐", "晚餐", "點心"}:
                raise ValueError("餐別不支援")
        elif action in {"rename", "replace_item"}:
            replacement = value if isinstance(value, dict) else {"name": value}
            new_name = " ".join(str(replacement.get("name") or "").split())[:80]
            if not new_name:
                raise ValueError("品項名稱不能空白")
            if action == "replace_item":
                replacement_nutrition = _normalize_ledger_nutrition(replacement.get("nutrition") or {})
                if not replacement_nutrition or all(v is None for v in replacement_nutrition.values()):
                    raise ValueError("新品項缺少營養資料")
                nutrition = {field_name: replacement_nutrition.get(field_name) for field_name in DAILY_FOOD_NUTRIENT_FIELDS}
                sources = {
                    field_name: "user_item_replacement"
                    for field_name, nutrient_value in nutrition.items() if nutrient_value is not None
                }
        elif action == "delete":
            pass
        else:
            raise ValueError("不支援的飲食紀錄操作")
        new_version = version + 1
        now = tw_now().isoformat(timespec="seconds")
        result_name = row[5]
        if action == "delete":
            conn.execute(
                """UPDATE food_logs SET deleted_at=?,confirmation_status='deleted',
                   version=?,updated_at=? WHERE log_id=?""",
                (now, new_version, now, log_id),
            )
        elif action == "set_meal_slot":
            conn.execute(
                "UPDATE food_logs SET meal_slot=?,version=?,updated_at=? WHERE log_id=?",
                (str(value), new_version, now, log_id),
            )
        elif action in {"rename", "replace_item"}:
            fingerprint = hashlib.sha256(
                f"renamed|{user_id}|{new_name}".encode("utf-8")
            ).hexdigest()
            renamed_food_id = "food_" + fingerprint[:24]
            per_serving = {
                key: (None if nutrient is None else round(float(nutrient) / old_servings, 4))
                for key, nutrient in nutrition.items()
            }
            conn.execute(
                """INSERT INTO food_catalog
                   (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                    package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                    exchange_json,exchange_review_status,fingerprint,original_image_ref,
                    recognition_confidence,verification_status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(food_id) DO UPDATE SET updated_at=excluded.updated_at""",
                (
                    renamed_food_id, new_name, "", "", "user_private_food", user_id, "private",
                    1, "份", 1, json.dumps(per_serving, ensure_ascii=False, allow_nan=False),
                    "{}", "{}", "pending_review", fingerprint, "", 1,
                    "user_confirmed", now, now,
                ),
            )
            conn.execute(
                """UPDATE food_logs SET food_id=?,nutrition_snapshot_json=?,
                   nutrient_sources_json=?,version=?,updated_at=? WHERE log_id=?""",
                (
                    renamed_food_id,
                    json.dumps(nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    json.dumps(sources, ensure_ascii=False, sort_keys=True),
                    new_version, now, log_id,
                ),
            )
            result_name = new_name
        else:
            conn.execute(
                """UPDATE food_logs SET consumed_servings=?,consumed_amount=?,
                   nutrition_snapshot_json=?,nutrient_sources_json=?,version=?,updated_at=?
                   WHERE log_id=?""",
                (
                    new_servings, new_servings,
                    json.dumps(nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    json.dumps(sources, ensure_ascii=False, sort_keys=True),
                    new_version, now, log_id,
                ),
            )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recent_meal_logs'"
        ).fetchone():
            if action == "patch_nutrition":
                conn.execute(
                    """UPDATE recent_meal_logs SET current_cal=?,current_pro=?,updated_at=?
                       WHERE user_id=? AND food_log_id=?""",
                    (nutrition.get("calories_kcal"), nutrition.get("protein_g"), now, user_id, log_id),
                )
            elif action == "replace_item":
                conn.execute(
                    """UPDATE recent_meal_logs SET meal_name=?,base_cal=?,base_pro=?,
                       current_cal=?,current_pro=?,source_text='改品項',updated_at=?
                       WHERE user_id=? AND food_log_id=?""",
                    (
                        new_name, nutrition.get("calories_kcal"), nutrition.get("protein_g"),
                        nutrition.get("calories_kcal"), nutrition.get("protein_g"), now,
                        user_id, log_id,
                    ),
                )
        date_text = conn.execute(
            "SELECT date(?, '+8 hours')", (str(row[4] or ""),)
        ).fetchone()[0]
        _sync_health_profile_from_ledger_conn(conn, user_id, date_text)
        result = {
            "log_id": log_id, "product_name": result_name, "version": new_version,
            "servings": new_servings, "nutrition": nutrition,
            "action": action, "date": date_text, "replayed": False,
        }
        conn.execute(
            """INSERT INTO daily_food_log_events
               (event_id,user_id,log_id,action,result_json,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                event_id, user_id, log_id, action,
                json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False), now,
            ),
        )
        try:
            conn.execute(
                """INSERT INTO nutrition_sheet_outbox
                   (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
                   VALUES (?,'food_log',?,'pending',0,'',?,'')
                   ON CONFLICT(entity_type,entity_id) DO UPDATE SET status='pending',synced_at=''""",
                ("outbox_" + uuid.uuid4().hex[:20], log_id, now),
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
        return result


def clear_daily_food_ledger(user_id: str, *, event_id: str) -> dict:
    """原子清除台灣時區今天的逐筆帳本，並保留 soft-delete 稽核與 Sheet 同步。"""
    user_id = str(user_id or "").strip()
    event_id = str(event_id or "").strip()
    if not user_id or not event_id:
        raise ValueError("缺少清空事件識別碼")
    today = tw_today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT result_json FROM daily_food_log_events WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        ).fetchone()
        if previous:
            result = json.loads(previous[0])
            result["replayed"] = True
            conn.commit()
            return result
        log_ids = [row[0] for row in conn.execute(
            """SELECT log_id FROM food_logs
               WHERE user_id=? AND date(consumed_at, '+8 hours')=?
                 AND confirmation_status='confirmed' AND COALESCE(deleted_at,'')=''""",
            (user_id, today),
        ).fetchall()]
        now = tw_now().isoformat(timespec="seconds")
        for log_id in log_ids:
            conn.execute(
                """UPDATE food_logs SET deleted_at=?,confirmation_status='deleted',
                   version=COALESCE(version,1)+1,updated_at=? WHERE log_id=? AND user_id=?""",
                (now, now, log_id, user_id),
            )
            conn.execute(
                """INSERT INTO nutrition_sheet_outbox
                   (outbox_id,entity_type,entity_id,status,attempts,last_error,created_at,synced_at)
                   VALUES (?,'food_log',?,'pending',0,'',?,'')
                   ON CONFLICT(entity_type,entity_id) DO UPDATE SET
                     status='pending',synced_at='',resync_required=1""",
                ("outbox_" + uuid.uuid4().hex[:20], log_id, now),
            )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recent_meal_logs'"
        ).fetchone():
            conn.execute(
                "DELETE FROM recent_meal_logs WHERE user_id=? AND meal_date=?",
                (user_id, today),
            )
        _sync_health_profile_from_ledger_conn(conn, user_id, today)
        result = {
            "date": today, "deleted_count": len(log_ids),
            "replayed": False,
        }
        conn.execute(
            """INSERT INTO daily_food_log_events
               (event_id,user_id,log_id,action,result_json,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                event_id, user_id, f"day:{today}", "clear_day",
                json.dumps(result, ensure_ascii=False, sort_keys=True), now,
            ),
        )
        conn.commit()
        return result


def _ledger_value_text(ledger: dict, field: str, unit: str) -> str:
    known = ledger["known_totals"].get(field, 0)
    count = ledger["known_counts"].get(field, 0)
    if count == 0:
        return "NA"
    text = f"{known:g} {unit}"
    if field in ledger["unknown_fields"]:
        text += "＋部分未知"
    return text


def _daily_food_summary_bubble(ledger: dict, day_ref: str) -> dict:
    is_today = ledger["date"] == tw_today().isoformat()
    title = "今日飲食總結" if is_today else "昨日飲食總結"
    cal_text = _ledger_value_text(ledger, "calories_kcal", "kcal")
    pro_text = _ledger_value_text(ledger, "protein_g", "g")
    carb_text = _ledger_value_text(ledger, "carbohydrate_g", "g")
    fat_text = _ledger_value_text(ledger, "fat_g", "g")
    known_cal = ledger["known_totals"]["calories_kcal"]
    remaining = max(0, ledger["tdee"] - known_cal)
    if "calories_kcal" in ledger["unknown_fields"]:
        target_text = f"🎯 目標：{ledger['tdee']:g} kcal｜剩餘 NA（部分熱量未知）"
    else:
        target_text = f"🎯 目標：{ledger['tdee']:g} kcal｜剩餘 {remaining:g} kcal"
    return {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F766E", "contents": [
            {"type": "text", "text": f"📅 {title}", "color": "#FFFFFF", "weight": "bold", "size": "lg"},
            {"type": "text", "text": ledger["date"], "color": "#CCFBF1", "size": "xs", "margin": "xs"},
        ]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "text", "text": f"共記錄 {ledger['count']} 項", "size": "sm", "weight": "bold"},
            {"type": "text", "text": f"🔥 熱量：{cal_text}", "size": "sm", "wrap": True},
            {"type": "text", "text": target_text, "size": "xs", "color": "#555555", "wrap": True},
            {"type": "text", "text": f"🥩 蛋白質：{pro_text}", "size": "sm", "wrap": True},
            {"type": "text", "text": f"🍚 碳水：{carb_text}", "size": "sm", "wrap": True},
            {"type": "text", "text": f"🥑 脂肪：{fat_text}", "size": "sm", "wrap": True},
            {"type": "text", "text": "NA 代表紀錄未提供，未當成 0。", "size": "xs", "color": "#B45309", "wrap": True},
        ]},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "secondary", "height": "sm", "action": {
                "type": "postback", "label": "切換今天／昨天",
                "data": f"foodlog:v1:day:{'yesterday' if day_ref == 'today' else 'today'}:page:0",
                "displayText": "查看另一日飲食紀錄",
            }}
        ]},
    }


def _daily_food_item_bubble(item: dict) -> dict:
    nutrition = item["nutrition"]
    def val(field, unit):
        raw = nutrition.get(field)
        return "NA" if raw is None else f"{float(raw):g} {unit}"
    source_labels = {
        "ai_text_estimate": "AI 預估", "user_private_food": "我的食品",
        "planned_meal": "排餐確認", "frequent_food": "常吃食品",
        "user_correction": "使用者修正", "user_meal_photo": "餐點照片",
        "menu_csv": "一日樂食菜單", "legacy_daily_carryover": "部署前合計",
    }
    consumed_at = str(item["consumed_at"] or "")
    try:
        parsed_time = datetime.fromisoformat(consumed_at.replace("Z", "+00:00"))
        time_text = (
            parsed_time.astimezone(TW_TZ).strftime("%H:%M")
            if parsed_time.tzinfo else parsed_time.strftime("%H:%M")
        )
    except ValueError:
        time_text = consumed_at[11:16] if len(consumed_at) >= 16 else "時間未記錄"
    log_id, version = item["log_id"], item["version"]
    if item["source_type"] == "legacy_daily_carryover":
        footer_contents = [{
            "type": "button", "style": "secondary", "height": "sm",
            "action": {"type": "message", "label": "重新查看", "text": "飲食紀錄"},
        }]
    else:
        footer_contents = [
            {"type": "button", "style": "primary", "color": "#0F766E", "height": "sm", "action": {
                "type": "postback", "label": "調整份量",
                "data": f"foodlog:v1:{log_id}:{version}:portion:start", "displayText": "調整這筆飲食份量",
            }},
            {"type": "button", "style": "secondary", "height": "sm", "action": {
                "type": "postback", "label": "修正營養",
                "data": f"foodlog:v1:{log_id}:{version}:nutrition:start", "displayText": "修正這筆飲食營養",
            }},
            {"type": "button", "style": "secondary", "height": "sm", "action": {
                "type": "postback", "label": "更多操作",
                "data": f"foodlog:v1:{log_id}:{version}:more", "displayText": "更多飲食紀錄操作",
            }},
        ]
    return {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#ECFDF5", "contents": [
            {"type": "text", "text": item["product_name"][:60], "weight": "bold", "size": "md", "wrap": True, "color": "#065F46"},
            {"type": "text", "text": f"{time_text}｜{item['meal_slot'] or '未分類'}", "size": "xs", "color": "#555555", "margin": "xs"},
        ]},
        "body": {"type": "box", "layout": "vertical", "spacing": "xs", "contents": [
            {"type": "text", "text": f"份量：{item['servings']:g} 份", "size": "sm"},
            {"type": "text", "text": f"🔥 {val('calories_kcal', 'kcal')}", "size": "sm"},
            {"type": "text", "text": f"🥩 {val('protein_g', 'g')}", "size": "sm"},
            {"type": "text", "text": f"🍚 {val('carbohydrate_g', 'g')}｜🥑 {val('fat_g', 'g')}", "size": "xs", "wrap": True},
            {"type": "text", "text": f"來源：{source_labels.get(item['source_type'], item['source_type'] or '未標示')}", "size": "xs", "color": "#777777", "wrap": True},
        ]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer_contents},
    }


def build_daily_food_ledger_flex(user_id: str, day_ref: str, page: int = 0):
    from linebot.models import FlexSendMessage
    day_ref = str(day_ref or "today")
    if day_ref == "today":
        target = tw_today()
    elif day_ref == "yesterday":
        target = tw_today() - timedelta(days=1)
    else:
        raise ValueError("只支援今天或昨天")
    page = max(0, min(int(page or 0), 1000))
    ledger = get_daily_food_ledger(user_id, target.isoformat())
    start = page * 10
    page_items = ledger["items"][start:start + 10]
    bubbles = [_daily_food_summary_bubble(ledger, day_ref)]
    bubbles.extend(_daily_food_item_bubble(item) for item in page_items)
    if start + 10 < len(ledger["items"]):
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "body": {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "還有更多飲食紀錄", "weight": "bold", "wrap": True},
            ]},
            "footer": {"type": "box", "layout": "vertical", "contents": [
                {"type": "button", "style": "primary", "color": "#0F766E", "action": {
                    "type": "postback", "label": "下一頁",
                    "data": f"foodlog:v1:day:{day_ref}:page:{page + 1}",
                    "displayText": "查看下一頁飲食紀錄",
                }}
            ]},
        })
    elif page > 0:
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "body": {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "已到最後一頁", "weight": "bold"},
            ]},
            "footer": {"type": "box", "layout": "vertical", "contents": [
                {"type": "button", "style": "secondary", "action": {
                    "type": "postback", "label": "上一頁",
                    "data": f"foodlog:v1:day:{day_ref}:page:{page - 1}",
                    "displayText": "查看上一頁飲食紀錄",
                }}
            ]},
        })
    return FlexSendMessage(
        alt_text=f"{target.strftime('%m/%d')} 飲食紀錄",
        contents={"type": "carousel", "contents": bubbles},
    )


def build_daily_food_date_picker_flex():
    from linebot.models import FlexSendMessage
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F766E", "contents": [
            {"type": "text", "text": "🍽️ 我的飲食紀錄", "color": "#FFFFFF", "weight": "bold", "size": "lg"},
        ]},
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "想查看哪一天？", "size": "sm", "wrap": True},
        ]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "button", "style": "primary", "color": "#0F766E", "action": {
                "type": "postback", "label": "今天的紀錄", "data": "foodlog:v1:day:today:page:0", "displayText": "查看今天飲食紀錄",
            }},
            {"type": "button", "style": "secondary", "action": {
                "type": "postback", "label": "昨天的紀錄", "data": "foodlog:v1:day:yesterday:page:0", "displayText": "查看昨天飲食紀錄",
            }},
        ]},
    }
    return FlexSendMessage(alt_text="選擇今天或昨天的飲食紀錄", contents=bubble)


def set_daily_food_edit_state(
    user_id: str, log_id: str, expected_version: int, input_type: str,
    *, field: str = "", pending_value=None, payload: dict | None = None,
) -> None:
    now = tw_now()
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        conn.execute(
            """INSERT INTO daily_food_edit_states
               (user_id,log_id,expected_version,input_type,field,pending_value,
                payload_json,created_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 log_id=excluded.log_id,expected_version=excluded.expected_version,
                 input_type=excluded.input_type,field=excluded.field,
                 pending_value=excluded.pending_value,payload_json=excluded.payload_json,
                 created_at=excluded.created_at,expires_at=excluded.expires_at""",
            (
                user_id, log_id, int(expected_version), input_type, field,
                pending_value, json.dumps(payload or {}, ensure_ascii=False),
                now.isoformat(timespec="seconds"),
                (now + timedelta(minutes=10)).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()


def get_daily_food_edit_state(user_id: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        row = conn.execute(
            """SELECT log_id,expected_version,input_type,field,pending_value,
                      payload_json,expires_at
               FROM daily_food_edit_states WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        if str(row[6] or "") < tw_now().isoformat(timespec="seconds"):
            conn.execute("DELETE FROM daily_food_edit_states WHERE user_id=?", (user_id,))
            conn.commit()
            return None
    return {
        "log_id": row[0], "expected_version": int(row[1]), "input_type": row[2],
        "field": row[3] or "", "pending_value": row[4],
        "payload": json.loads(row[5] or "{}"),
    }


def clear_daily_food_edit_state(user_id: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        conn.execute("DELETE FROM daily_food_edit_states WHERE user_id=?", (user_id,))
        conn.commit()


def _daily_food_log_brief(user_id: str, log_id: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        row = conn.execute(
            """SELECT fc.product_name,fl.nutrition_snapshot_json,fl.consumed_servings,
                      fl.version,fl.consumed_at
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.user_id=? AND fl.log_id=? AND fl.confirmation_status='confirmed'
                 AND COALESCE(fl.deleted_at,'')=''""",
            (user_id, log_id),
        ).fetchone()
    if not row:
        raise ValueError("找不到這筆飲食紀錄")
    return {
        "product_name": row[0], "nutrition": json.loads(row[1] or "{}"),
        "servings": float(row[2] or 0), "version": int(row[3] or 1),
        "date": str(row[4] or "")[:10],
    }


def build_daily_food_nutrition_confirmation_flex(
    *, user_id: str, log_id: str, expected_version: int, field: str, value: float,
):
    from linebot.models import FlexSendMessage
    brief = _daily_food_log_brief(user_id, log_id)
    if brief["version"] != int(expected_version):
        raise ValueError("這筆紀錄已更新，請重新開啟最新卡片")
    labels = {
        "calories_kcal": ("熱量", "kcal"), "protein_g": ("蛋白質", "g"),
        "fat_g": ("脂肪", "g"), "carbohydrate_g": ("碳水", "g"),
    }
    label, unit = labels[field]
    old = brief["nutrition"].get(field)
    old_text = "NA" if old is None else f"{float(old):g} {unit}"
    value_text = f"{float(value):g} {unit}"
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#FF6B35", "contents": [
            {"type": "text", "text": "📝 確認修正營養", "color": "#FFFFFF", "weight": "bold", "size": "lg"},
        ]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "text", "text": brief["product_name"], "weight": "bold", "wrap": True},
            {"type": "text", "text": f"{label}：{old_text} → {value_text}", "size": "sm", "wrap": True},
            {"type": "text", "text": "只修改這個欄位，其他營養數值不會跟著改。", "size": "xs", "color": "#555555", "wrap": True},
        ]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "button", "style": "primary", "color": "#0F766E", "action": {
                "type": "postback", "label": "只修正這次",
                "data": f"foodlog:v1:{log_id}:{expected_version}:nutrition:apply:{field}:{float(value):g}:once",
                "displayText": f"確認把{label}改成 {value_text}",
            }},
            {"type": "button", "style": "secondary", "action": {
                "type": "postback", "label": "修正並存成我的食品",
                "data": f"foodlog:v1:{log_id}:{expected_version}:nutrition:apply:{field}:{float(value):g}:save",
                "displayText": "修正並儲存成我的食品",
            }},
        ]},
    }
    return FlexSendMessage(alt_text=f"確認修正 {brief['product_name']} {label}", contents=bubble)


def save_daily_log_as_private_food(user_id: str, log_id: str, private_name: str) -> dict:
    private_name = " ".join(str(private_name or "").split())[:80]
    if not private_name:
        raise ValueError("我的食品名稱不能空白")
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        row = conn.execute(
            """SELECT fl.nutrition_snapshot_json,fl.consumed_servings
               FROM food_logs fl WHERE fl.user_id=? AND fl.log_id=?
                 AND fl.confirmation_status='confirmed' AND COALESCE(fl.deleted_at,'')=''""",
            (user_id, log_id),
        ).fetchone()
        if not row:
            raise ValueError("找不到這筆飲食紀錄")
        nutrition = json.loads(row[0] or "{}")
        servings = float(row[1] or 1)
        per_serving = {
            key: (None if value is None else round(float(value) / servings, 4))
            for key, value in nutrition.items()
        }
        fingerprint = hashlib.sha256(
            f"private|{user_id}|{private_name}".encode("utf-8")
        ).hexdigest()
        food_id = "food_" + fingerprint[:24]
        now = tw_now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO food_catalog
               (food_id,product_name,brand,barcode,source_type,owner_user_id,visibility,
                package_amount,package_unit,servings_per_package,per_serving_json,per_100_json,
                exchange_json,exchange_review_status,fingerprint,original_image_ref,
                recognition_confidence,verification_status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(food_id) DO UPDATE SET
                 per_serving_json=excluded.per_serving_json,updated_at=excluded.updated_at""",
            (
                food_id, private_name, "", "", "user_private_food", user_id, "private",
                1, "份", 1, json.dumps(per_serving, ensure_ascii=False, allow_nan=False),
                "{}", "{}", "pending_review", fingerprint, "", 1,
                "user_confirmed", now, now,
            ),
        )
        conn.commit()
    return {"food_id": food_id, "product_name": private_name, "per_serving": per_serving}


def build_daily_food_edit_success_flex(result: dict, *, saved_name: str = ""):
    from linebot.models import FlexSendMessage
    nutrition = result.get("nutrition") or {}
    date_ref = "today" if result.get("date") == tw_today().isoformat() else "yesterday"
    body = [
        {"type": "text", "text": result.get("product_name") or "飲食紀錄", "weight": "bold", "wrap": True},
        {"type": "text", "text": f"份量：{float(result.get('servings') or 0):g} 份", "size": "sm"},
        {"type": "text", "text": f"🔥 {nutrition.get('calories_kcal', 'NA')} kcal｜🥩 {nutrition.get('protein_g', 'NA')} g", "size": "sm", "wrap": True},
    ]
    if saved_name:
        body.append({"type": "text", "text": f"已儲存為我的食品：{saved_name}", "size": "xs", "color": "#0F766E", "wrap": True})
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#DCFCE7", "contents": [
            {"type": "text", "text": "✅ 飲食紀錄已更新", "color": "#166534", "weight": "bold", "size": "lg"},
        ]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary", "color": "#0F766E", "action": {
                "type": "postback", "label": "查看最新紀錄",
                "data": f"foodlog:v1:day:{date_ref}:page:0", "displayText": "查看最新飲食紀錄",
            }}
        ]},
    }
    return FlexSendMessage(alt_text="飲食紀錄已更新", contents=bubble)


def normalize_date_str(date_str):
    """將各式日期格式 (2026-3-5, 2026/03/05 等) 統一轉為 2026/03/05"""
    if not date_str: return ""
    # 確保是字串並去除空白
    s = str(date_str).strip()
    try:
        # 先把所有的橫線換成斜線
        s = s.replace('-', '/')
        # 嘗試解析並重新格式化為 YYYY/MM/DD (補零)
        parts = s.split('/')
        if len(parts) == 3:
            y = parts[0]
            m = parts[1].zfill(2)
            d = parts[2].zfill(2)
            return f"{y}/{m}/{d}"
        return s
    except:
        return s
# --- 2. Google Sheet 授權與連線 ---
SCOPE = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

try:
    # 1. 優先從環境變數讀取
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    
    if creds_json:
        # 從環境變數
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPE)
        print("💡 使用環境變數 GOOGLE_CREDENTIALS")
    else:
        # 2. 從檔案
        creds = Credentials.from_service_account_file("google_key.json", scopes=SCOPE)
        print("💡 使用檔案 google_key.json")
    
    gc = gspread.authorize(creds)
    sh = gc.open_by_key("1webSlOkY0OwpY-9_HxxNKowLMoChGaWNlIpUVyJluiQ")
    sheet_main = sh.worksheet("Master_API_View")
    sheet_log = sh.worksheet("raw_logs")

    # ─────────────────────────────────────────────────────────────────────────
    # 🌟 Phase 2：初始化測試分頁（教練運動日誌）
    # ─────────────────────────────────────────────────────────────────────────
    def setup_garmin_test_sheet():
        """建立 Test_Garmin_Log 分頁，確認不存在才建立（不影響 production 資料）"""
        try:
            existing = [w.title for w in sh.worksheets()]
            if "Test_Garmin_Log" not in existing:
                sh.add_worksheet(title="Test_Garmin_Log", rows=500, cols=20)
                ws = sh.worksheet("Test_Garmin_Log")
                ws.update(
                    "A1:T1",
                    [["日期", "User_ID", "姓名", "運動類型", "時長(分)",
                      "平均心率", "最大心率", "有氧TE", "無氧TE",
                      "主要益處", "運動負荷", "NP(W)", "IF", "TSS", "FTP(W)",
                      "疲勞指數", "教練備註", "created_at"]],
                    1
                )
                print("✅ Test_Garmin_Log 分頁建立成功")
            else:
                print("ℹ️ Test_Garmin_Log 分頁已存在")
        except Exception as e:
            print(f"⚠️ 建立測試分頁失敗（不影響主功能）：{e}")

    setup_garmin_test_sheet()

    # ─────────────────────────────────────────────────────────────────────────
    # 🌟 Phase 2：寫入 Garmin 資料到測試分頁
    # ─────────────────────────────────────────────────────────────────────────
    def write_workout_to_sheet(uid, workout_data):
        """
        將解析後的運動資料寫入 Test_Garmin_Log 分頁。
        同一 user_id + 日期已有資料時，合併（不覆蓋）。
        """
        try:
            ws = sh.worksheet("Test_Garmin_Log")
            all_rows = ws.get_all_records()

            # 找現有列（同一 user_id + 日期）
            existing_row_idx = None
            for idx, row in enumerate(all_rows, start=2):
                if str(row.get("User_ID", "")) == str(uid) and str(row.get("日期", "")) == workout_data.get("workout_date", ""):
                    existing_row_idx = idx
                    break

            # 計算疲勞指數
            fatigue = round(workout_data.get("aerobic_te", 0) * workout_data.get("duration_min", 0) / 30, 1)

            new_row = [
                workout_data.get("workout_date", ""),
                uid,
                workout_data.get("name", ""),
                workout_data.get("workout_type", ""),
                workout_data.get("duration_min", ""),
                workout_data.get("avg_hr", ""),
                workout_data.get("max_hr", ""),
                workout_data.get("aerobic_te", ""),
                workout_data.get("anaerobic_te", ""),
                workout_data.get("primary_benefit", ""),
                workout_data.get("load_value", ""),
                workout_data.get("np_w", ""),
                workout_data.get("if_value", ""),
                workout_data.get("tss", ""),
                workout_data.get("ftp_w", ""),
                fatigue,
                "",  # 教練備註（空）
                workout_data.get("created_at", "")
            ]

            if existing_row_idx:
                # 合併：同一日期有資料就 append
                existing = all_rows[existing_row_idx - 2]
                for i, val in enumerate(new_row):
                    if str(existing.get(sh.cell(existing_row_idx, i+1).value, "")).strip() == "" and str(val).strip() != "":
                        pass  # 只補充空白欄位

            ws.append_row(new_row, insert_data_option="INSERT_ROWS")
            print(f"✅ 運動資料寫入 Test_Garmin_Log：{uid} {workout_data.get('workout_type')} {workout_data.get('workout_date')}")

        except Exception as e:
            print(f"⚠️ 寫入 Sheet 失敗（不影響 SQLite）：{e}")

    print("✅ Google Sheet 連線成功！")
    
except Exception as e:
    print(f"❌ Google Sheet 連線出錯：{e}")
    gc = None
    sh = None
    sheet_main = None
    sheet_log = None

# --- 下方接著寫你的 LINE Bot API 和 路由邏輯 ---

# --- 保險箱初始化設定 ---
# 我們把建立資料夾的邏輯移到 init_db 裡面會更安全，
# 這裡可以先註解掉或保持原樣，但 init_db 一定要改用「絕對路徑」版本。
# -----------------------
# ==========================================
# 1. 設定區 (金鑰與網址)
# ==========================================
# 🍱 可排入菜單的主餐關鍵字（其他單品不可排）
MEAL_PLAN_KEYWORDS = ["便當", "食蔬", "低碳", "沙拉", "番茄麵", "青蔬麵"]

STORE_ADDRESS = "台北市松山區南京東路四段133巷4弄5號"
HUBS = [
    {"name": "Anytime Fitness 信義店", "address": "台北市信義區松仁路89號"},
    {"name": "健身工廠 中山廠", "address": "台北市中山區南京東路二段8號"}
]

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("MAPS_API_KEY")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "#GEN_CODES")
_railway_public_domain = str(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
PUBLIC_BASE_URL = str(
    os.environ.get("PUBLIC_BASE_URL")
    or (f"https://{_railway_public_domain}" if _railway_public_domain else "")
    or "https://openclawbot-production-36ed.up.railway.app"
).rstrip("/")
MEAL_PHOTO_IMAGE_SECRET = str(os.environ.get("MEAL_PHOTO_IMAGE_SECRET") or "")
DB_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
DB_PATH = os.path.join(DB_DIR, "user_quota.db")
os.makedirs(DB_DIR, mode=0o700, exist_ok=True)
try:
    os.chmod(DB_DIR, 0o700)
except OSError:
    pass

# ==========================================
# 🌟 自動升級資料庫：確保 health_profile 有 status 欄位
# ==========================================
import sqlite3
with sqlite3.connect(DB_PATH) as conn:
    try:
        conn.execute("ALTER TABLE health_profile ADD COLUMN status TEXT")
        print("✅ 成功為 health_profile 新增 status (分班) 欄位！")
    except sqlite3.OperationalError:
        # 如果欄位已經存在，會跳出 OperationalError，這很正常，我們直接忽略
        pass
# ==========================================    
COACH_UIDS = ["Uefd72ca53a9a6ac39781fe673c398530","U9540c22cea2d6e0b1df8edbd9e3ebc41"]
pending_image_date = {}
pending_subscription_state = {}

ADMIN_ONLY_EXACT_COMMANDS = {
    "#綁定老闆", "#點數庫存", "#更新菜單", "#今日出餐完成", "#發送明日提醒",
    "#測試週報", "#測試晚報", "#生24", "#生48", "#延餐清單", "#待核訂單",
    "#清空熱量", "#刪除檔案", "#重置", "重置本週", "檢查數據", "#待審營養份量",
    "#待審餐點"
}
ADMIN_ONLY_PREFIXES = (
    "@靜音 ", "@解除靜音 ", "#喚醒AI ", "#上傳點數\n", "#核准延餐 ", "#拒絕延餐 ",
    "#核准訂單 ", "#拒絕訂單 ", "#開通訂單 ", "#核准營養份量 "
)

def is_admin_only_command(msg: str) -> bool:
    return msg in ADMIN_ONLY_EXACT_COMMANDS or any(msg.startswith(prefix) for prefix in ADMIN_ONLY_PREFIXES)

# ── 顧客清單同步 helper ──────────────────────────────────────────────────────
def sync_customer_sheet(uid, name, status, remaining_meals, expiry_date, tdee):
    """將用戶資料同步寫入 Google Sheet「顧客清單」（upsert）"""
    if not gc:
        return
    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet("顧客清單")
        except Exception:
            ws = sh.add_worksheet(title="顧客清單", rows="500", cols="8")
            ws.update(values=[["User_ID","姓名","狀態","剩餘餐數","到期日","TDEE","建立時間","備註"]], range_name="A1:H1")

        # 找是否已有此 uid
        all_ids = ws.col_values(1)  # A 欄全部 User_ID
        from datetime import datetime as _dt
        now_str = _dt.now().strftime("%Y/%m/%d %H:%M")
        row_data = [uid, name or "", status or "", remaining_meals or 0,
                    expiry_date or "", tdee or 0, now_str, ""]
        if uid in all_ids:
            row_idx = all_ids.index(uid) + 1  # 1-indexed
            ws.update(values=[row_data], range_name=f"A{row_idx}:H{row_idx}")
        else:
            ws.append_row(row_data)
    except Exception as _e:
        print(f"⚠️ sync_customer_sheet 失敗: {_e}")

# 🔥 Google 試算表設定 🔥
SPREADSHEET_ID = "1webSlOkY0OwpY-9_HxxNKowLMoChGaWNlIpUVyJluiQ"

# Google 試算表設定 (網址公開安全，靠 service_account 保護)
SPREADSHEET_ID = "1webSlOkY0OwpY-9_HxxNKowLMoChGaWNlIpUVyJluiQ"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"

# ==========================================
# 🔑 功能一：老闆 LINE UID（靜音指令專用）
# ==========================================
ADMIN_UID = "Uefd72ca53a9a6ac39781fe673c398530"

def get_admin_notify_uid():
    """回傳管理通知目的地；僅通知用途可在讀取失敗時使用預設值。"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
            row = c.fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception as e:
        print(f"⚠️ 讀取管理員通知 UID 失敗，改用預設 ADMIN_UID: {e}")
    return ADMIN_UID


def get_bound_admin_uid_for_authorization() -> str:
    """嚴格讀取目前綁定管理員；授權用途遇到缺值或DB錯誤一律拒絕。"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT value FROM admin_settings WHERE key='admin_id'"
            ).fetchone()
    except Exception as exc:
        print(f"⛔ 讀取管理員授權綁定失敗，已拒絕跨使用者操作：{exc}")
        raise PermissionError("目前無法驗證管理員身分，請稍後再試") from exc
    admin_uid = str(row[0] if row else "").strip()
    if not re.fullmatch(r"U[0-9a-fA-F]{32}", admin_uid):
        raise PermissionError("尚未設定有效的管理員綁定")
    return admin_uid


HEALTH_CHECKIN_TEMPLATE = (
    "健康回報｜體重70.2｜飲水2500｜排便有｜用藥無｜"
    "睡眠00:30-07:15｜品質良好"
)


def get_jason_health_report_uid() -> str:
    """Return the immutable Jason principal; never follow mutable notification bindings."""
    return str(ADMIN_UID or "").strip()


def save_jason_health_checkin(user_id: str, text: str, *, now=None) -> str:
    """Validate and upsert Jason's daily manual health fields."""
    if not get_jason_health_report_uid() or user_id != get_jason_health_report_uid():
        raise PermissionError("目前只有Jason啟用每日健康日報")
    current = (now or tw_now()).astimezone(TW_TZ)
    values = parse_health_checkin(text)
    report_date = current.date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_health_schema(conn)
        save_daily_health_checkin(
            conn,
            user_id=user_id,
            report_date=report_date,
            values=values,
            updated_at=current.isoformat(timespec="seconds"),
        )
    return (
        f"✅ 已更新 {current.strftime('%Y/%m/%d')} 健康回報\n"
        f"體重 {values['weight_kg']:g}kg｜飲水 {values['water_ml']}ml｜"
        f"排便 {values['bowel_status']}｜用藥 {values['medication']}\n"
        f"睡眠 {values['sleep_start']}–{values['sleep_end']}（{values['sleep_quality']}）\n\n"
        "23:30會自動整合飲食、營養計畫與Intervals.icu運動紀錄。"
    )


def _jason_intervals_credentials(user_id: str, report_date: str):
    """Read Jason's Intervals credentials without logging or returning them to LINE."""
    if not get_jason_health_report_uid() or user_id != get_jason_health_report_uid():
        return None
    try:
        records = sheet_main.get_all_records() if sheet_main else []
        candidates = []
        for row in records:
            if str(row.get("User_ID", "")).strip() != user_id:
                continue
            athlete_id = str(row.get("Intervals_ID", "") or "").strip()
            api_key = str(row.get("Intervals_API_Key", "") or "").strip()
            if athlete_id and api_key:
                row_date = normalize_date_str(row.get("Date", "")).replace("/", "-")
                candidates.append((row_date == report_date, athlete_id, api_key))
        if candidates:
            _exact, athlete_id, api_key = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
            return athlete_id, api_key
    except Exception as exc:
        print(f"⚠️ 讀取Intervals.icu設定失敗：{type(exc).__name__}")
    athlete_id = os.environ.get("INTERVALS_ATHLETE_ID", "").strip()
    api_key = os.environ.get("INTERVALS_API_KEY", "").strip()
    return (athlete_id, api_key) if athlete_id and api_key else None


def fetch_daily_intervals_summary(user_id: str, report_date: str):
    credentials = _jason_intervals_credentials(user_id, report_date)
    if not credentials:
        return None
    athlete_id, api_key = credentials
    try:
        response = requests.get(
            f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities",
            params={"oldest": report_date, "newest": report_date},
            auth=("API_KEY", api_key),
            timeout=12,
        )
        if response.status_code != 200:
            print(f"⚠️ Intervals.icu日報HTTP {response.status_code}")
            return None
        activities = response.json()
        if not isinstance(activities, list):
            return None
        return summarize_intervals_activities(activities)
    except Exception as exc:
        print(f"⚠️ Intervals.icu日報讀取失敗：{type(exc).__name__}")
        return None


def build_jason_daily_health_report(user_id: str, report_date: str) -> str:
    if not get_jason_health_report_uid() or user_id != get_jason_health_report_uid():
        raise PermissionError("目前只有Jason啟用每日健康日報")
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        ensure_nutrition_schema(conn)
        ensure_daily_health_schema(conn)
        ensure_meal_photo_schema(conn)
        checkin = get_daily_health_checkin(
            conn, user_id=user_id, report_date=report_date
        )
        food_summary = daily_food_summary(
            conn, user_id=user_id, date_iso=report_date
        )
        pending_meal_photos = daily_pending_meal_photo_count(
            conn, user_id=user_id, date_iso=report_date
        )
    try:
        target = get_daily_nutrition_target(user_id, report_date)
    except Exception as exc:
        print(f"⚠️ 每日健康日報讀取營養計畫失敗：{type(exc).__name__}")
        target = None
    exercise = fetch_daily_intervals_summary(user_id, report_date)
    return format_daily_health_report(
        report_date=report_date,
        checkin=checkin,
        foods=food_summary["foods"],
        totals=food_summary["totals"],
        target=target,
        exercise=exercise,
        pending_reviews=food_summary["pending_reviews"] + pending_meal_photos,
    )


def _send_jason_daily_message(kind: str, text_factory, *, now=None) -> bool:
    current = (now or tw_now()).astimezone(TW_TZ)
    report_date = current.date().isoformat()
    user_id = get_jason_health_report_uid()
    if not user_id:
        return False
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        ensure_daily_health_schema(conn)
        claim_token = claim_daily_delivery(
            conn,
            user_id=user_id,
            report_date=report_date,
            kind=kind,
            claimed_at=current.isoformat(timespec="seconds"),
        )
    if not claim_token:
        return False
    try:
        text = text_factory(user_id, report_date)
        line_bot_api.push_message(
            user_id, TextSendMessage(text=text), timeout=12
        )
    except Exception as exc:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            ensure_daily_health_schema(conn)
            finish_daily_delivery(
                conn,
                user_id=user_id,
                report_date=report_date,
                kind=kind,
                claim_token=claim_token,
                sent=False,
                finished_at=tw_now().isoformat(timespec="seconds"),
                error=f"{type(exc).__name__}: {exc}",
            )
        print(f"⚠️ Jason每日健康{kind}推送失敗：{type(exc).__name__}")
        return False
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        ensure_daily_health_schema(conn)
        finish_daily_delivery(
            conn,
            user_id=user_id,
            report_date=report_date,
            kind=kind,
            claim_token=claim_token,
            sent=True,
            finished_at=tw_now().isoformat(timespec="seconds"),
        )
    return True


def send_jason_health_checkin_prompt(*, now=None) -> bool:
    def prompt(_user_id, report_date):
        return (
            f"🩺 {report_date.replace('-', '/')} 晚間健康回報\n\n"
            "請複製下列一行、修改數字後直接傳回：\n"
            f"{HEALTH_CHECKIN_TEMPLATE}\n\n"
            "排便可填：有／無／NA；品質可填：良好／普通／不佳／NA。"
        )

    return _send_jason_daily_message("prompt", prompt, now=now)


def send_jason_daily_health_report(*, now=None) -> bool:
    return _send_jason_daily_message(
        "report", build_jason_daily_health_report, now=now
    )

# 🔥 設定 FastAPI 的生命週期與隱形店長排程
def register_daily_health_jobs(scheduler):
    scheduler.add_job(
        send_jason_health_checkin_prompt,
        "cron",
        hour=22,
        minute="30,40,50",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_jason_daily_health_report,
        "cron",
        hour=23,
        minute="30,40,50",
        max_instances=1,
        coalesce=True,
    )


def register_nutrition_cleanup_job(scheduler):
    scheduler.add_job(
        cleanup_nutrition_images,
        "interval",
        hours=1,
        max_instances=1,
        coalesce=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 伺服器啟動時，喚醒隱形店長
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    
    # ⏸️ 每天 22:00（週一～週六）自動扣餐 + 個人化晚報：Jason 2026-06-12 要求暫時關閉
    # scheduler.add_job(auto_daily_evening_report, 'cron', day_of_week='mon-sat', hour=22, minute=0)

    # ⏰ 每週日 22:05 自動發送週報（含本週回顧 + 下週預覽）
    scheduler.add_job(auto_expiry_reminder, 'cron', hour=10, minute=0)
    scheduler.add_job(send_subscription_expiry_reminders, 'cron', hour=11, minute=0)
    scheduler.add_job(auto_weekly_coach_batch, 'cron', day_of_week='sun', hour=22, minute=5)
    register_daily_health_jobs(scheduler)
    scheduler.add_job(retry_pending_nutrition_plan_links, 'interval', minutes=10, max_instances=1, coalesce=True)
    scheduler.add_job(flush_nutrition_sheet_outbox, 'interval', minutes=10, max_instances=1, coalesce=True)
    register_nutrition_cleanup_job(scheduler)
    try:
        retry_pending_nutrition_plan_links()
        flush_nutrition_sheet_outbox()
        cleanup_nutrition_images()
    except Exception as exc:
        print(f"⚠️ 啟動時營養資料維護失敗：{exc}")

    scheduler.start()
    print("✅ 全自動定時器已啟動！系統進入無人駕駛模式 ON！")
    
    yield
    
    # 伺服器關閉時，讓店長下班
    scheduler.shutdown()

# 正式建立啟用了定時器的 FastAPI 應用程式
app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health_check():
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as conn:
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {
                "usage", "health_profile", "food_catalog", "food_logs",
                "nutrition_sheet_outbox", "pending_meal_photo_drafts", "meal_photo_events",
                "meal_photo_schema_versions",
            }
            missing = required - existing
            if missing:
                raise sqlite3.OperationalError("required schema missing")
            meal_version = conn.execute(
                "SELECT version FROM meal_photo_schema_versions WHERE component='meal_photo_system'"
            ).fetchone()
            draft_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(pending_meal_photo_drafts)")
            }
            if not meal_version or int(meal_version[0]) < 2 or "version" not in draft_columns:
                raise sqlite3.OperationalError("meal photo schema incomplete")
    except sqlite3.Error as exc:
        print(f"⚠️ 健康檢查資料庫失敗：{exc}")
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "service": "openclawbot"}


client = OpenAI(api_key=OPENAI_API_KEY)
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
user_memory = {}
processed_messages = set()

# ==========================================
# 🌟 LIFF 教練專屬網頁後台 & API
# ==========================================
# 1. 提供資料給網頁的 API
@app.get("/api/coach-data")
async def get_coach_data(admin_uid: str):
    if admin_uid not in COACH_UIDS:
        return {"error": "Unauthorized"}

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT user_id, workout_date, workout_type, duration_min, aerobic_te, load_value, avg_hr 
            FROM workout_records WHERE workout_date >= date('now', '-7 days')
            ORDER BY workout_date DESC, created_at DESC
        """)
        records = c.fetchall()
        
        # 🌟 修改：status 作為大班別，training_group 作為 1~6 區
        c.execute("SELECT user_id, name, status, COALESCE(training_group, '') FROM health_profile")
        profiles = {}
        for row in c.fetchall():
            uid_str = row[0]
            name_str = row[1]
            # 如果資料庫裡的 status 是空的，就歸類到 '未分類'
            group_str = row[2] if row[2] else '未分類'
            training_group_str = str(row[3] or '').strip()
            profiles[uid_str] = {"name": name_str, "group": group_str, "training_group": training_group_str}

    result = {}
    for r in records:
        uid_r, date, wtype, dur, te, load, hr = r
        
        # 🌟 修改：取得包含 name 和 group 的字典
        user_info = profiles.get(uid_r, {"name": uid_r[:8], "group": "未分類"})
        
        if uid_r not in result:
            result[uid_r] = {
                "uid": uid_r, 
                "name": user_info["name"], 
                "group": user_info["group"], # 🌟 新增群組資料給網頁用
                "training_group": user_info.get("training_group", ""), # 🌟 1~6 區
                "total_dur": 0, "total_load": 0, 
                "logs": [], 
                "chart_data": {"dates": [], "loads": [], "hrs": []} 
            }
            
        result[uid_r]["total_dur"] += dur
        result[uid_r]["total_load"] += load
        short_date = date[5:].replace("-", "/")
        emoji = "🚴" if "自行車" in wtype else "🏃"
        result[uid_r]["logs"].append(f"{short_date} {emoji}{wtype} {dur}分 ⚡{load} ❤️{hr}")
        
        # 把乾淨的數字存入圖表陣列
        result[uid_r]["chart_data"]["dates"].append(short_date)
        result[uid_r]["chart_data"]["loads"].append(load)
        result[uid_r]["chart_data"]["hrs"].append(hr)
        
    # 🌟 補上沒有近期運動紀錄的學員，讓教練也能分配 1~6 區與指派課表
    for uid_p, user_info in profiles.items():
        if uid_p not in result:
            result[uid_p] = {
                "uid": uid_p,
                "name": user_info["name"],
                "group": user_info["group"],
                "training_group": user_info.get("training_group", ""),
                "total_dur": 0, "total_load": 0,
                "logs": ["尚無近期運動紀錄"],
                "chart_data": {"dates": [], "loads": [], "hrs": []}
            }

    # 🌟 為了讓圖表從左到右顯示 (舊到新)，我們把陣列反轉
    final_list = list(result.values())
    for item in final_list:
        item["chart_data"]["dates"].reverse()
        item["chart_data"]["loads"].reverse()
        item["chart_data"]["hrs"].reverse()
        
    return final_list

# 🌟 新增：接收網頁傳來的關心訊息，並用 LINE 機器人推播給學員
class CoachCarePayload(BaseModel):
    admin_uid: str
    target_uid: str
    message: str

@app.post("/api/send-care")
async def send_coach_care(payload: CoachCarePayload):
    # 第一道防線：確認發送者是教練
    if payload.admin_uid not in COACH_UIDS:
        return {"success": False, "error": "您沒有權限發送訊息"}
    
    try:
        # 呼叫 LINE Bot API，將訊息推播給指定的學員
        line_bot_api.push_message(
            payload.target_uid,
            TextSendMessage(text=f"👨‍🏫 教練傳來關心：\n\n{payload.message}")
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
# 🌟 新增：接收網頁傳來的分班指令，更新資料庫
class UpdateGroupPayload(BaseModel):
    admin_uid: str
    target_uid: str
    new_group: str

@app.post("/api/update-group")
async def update_student_group(payload: UpdateGroupPayload):
    # 安全檢查
    if payload.admin_uid not in COACH_UIDS:
        return {"success": False, "error": "Unauthorized"}
    
    student_name = "未知學員"
    
    try:
        # --- 動作 1：更新本機資料庫 ---
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # 順便把學員名字撈出來，等等寫入表格會用到
            c.execute("SELECT name FROM health_profile WHERE user_id = ?", (payload.target_uid,))
            row = c.fetchone()
            if row:
                student_name = row[0]
                
            # 更新 health_profile 裡面的 status 欄位
            c.execute("UPDATE health_profile SET status = ? WHERE user_id = ?", (payload.new_group, payload.target_uid))
            conn.commit()

        # --- 動作 2：自動連動 Google Sheets ---
        # ⚠️ 注意：這裡假設你一開始連線 Google Sheets 的變數名稱叫做 'sh'
        # 如果你的變數名稱是 spreadsheet 或是其他名字，請把下面的 sh 換掉
        try:
            import datetime
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            
            # 嘗試尋找是不是已經有這個班級的分頁了？
            try:
                wks = sh.worksheet(payload.new_group)
            except:
                # 找不到！代表是新班級，自動建立一個新分頁！
                wks = sh.add_worksheet(title=payload.new_group, rows="1000", cols="10")
                # 在第一行自動補上漂亮的標題列
                wks.append_row(["更新時間", "學員 UID", "學員姓名", "目前狀態"])
                print(f"✨ 雲端自動化：已建立全新分頁【{payload.new_group}】")

            # 將這名學員的資料寫入該分頁的最後一行
            wks.append_row([now, payload.target_uid, student_name, "已加入本班級"])
            print(f"📝 雲端自動化：已將 {student_name} 同步至【{payload.new_group}】分頁")

        except Exception as sheet_err:
            print(f"⚠️ 本機資料庫已更新，但 Google Sheets 同步失敗: {sheet_err}")

        return {"success": True}
    except Exception as e:

        print(f"❌ 發生錯誤: {str(e)}")
        return {"success": False, "error": str(e)}

# ─────────────────────────────────────────────────────────────────────────
# 🌟 教練分區課表：班級(status) + 1~6 區(training_group) + 課表指派
# ─────────────────────────────────────────────────────────────────────────
TRAINING_ASSIGNMENTS_SHEET = "training_assignments"
TRAINING_ASSIGNMENTS_HEADERS = [
    "assignment_id", "created_at", "plan_title", "class_name", "training_group",
    "group_coach", "member_uid", "member_name", "session_label", "session_order",
    "target_date", "workout_type", "content", "note", "assignment_type",
    "assigned_by", "status"
]

class UpdateTrainingGroupPayload(BaseModel):
    admin_uid: str
    target_uid: str
    training_group: str

class AddGroupTrainingPlanPayload(BaseModel):
    admin_uid: str
    class_name: str
    target_date: str = ""
    plan_title: str = ""
    session_label: str = "訓練一"
    session_order: int = 1
    workout_type: str = "跑步"
    raw_plan_text: str

class AddIndividualTrainingPlanPayload(BaseModel):
    admin_uid: str
    target_uid: str
    member_name: str = ""
    class_name: str = ""
    training_group: str = ""
    target_date: str = ""
    plan_title: str = ""
    session_label: str = "個人微調"
    session_order: int = 1
    workout_type: str = "跑步"
    content: str
    note: str = ""

class AddWeeklyGroupTrainingPlanPayload(BaseModel):
    admin_uid: str
    class_name: str
    plan_title: str = "週課表"
    workout_type: str = "跑步"
    target_dates: str = ""
    raw_weekly_text: str


def is_coach_user(user_id: str) -> bool:
    return user_id in COACH_UIDS


def normalize_training_group(raw) -> str:
    s = str(raw or "").strip()
    circled = {"❶":"1", "❷":"2", "❸":"3", "❹":"4", "❺":"5", "❻":"6", "①":"1", "②":"2", "③":"3", "④":"4", "⑤":"5", "⑥":"6"}
    for k, v in circled.items():
        s = s.replace(k, v)
    m = re.search(r"[1-6]", s)
    return m.group(0) if m else ""


def get_or_create_training_assignments_sheet():
    if not gc:
        raise RuntimeError("Google Sheet 尚未連線")
    ss = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = ss.worksheet(TRAINING_ASSIGNMENTS_SHEET)
    except Exception:
        ws = ss.add_worksheet(title=TRAINING_ASSIGNMENTS_SHEET, rows=2000, cols=len(TRAINING_ASSIGNMENTS_HEADERS) + 2)
        ws.append_row(TRAINING_ASSIGNMENTS_HEADERS)
    return ws


def parse_group_training_plan(raw_text: str):
    """解析 ❶~❻ 分區課表。Coach 行只用來切段，不寫入正式課表內容。"""
    marker_map = {"❶":"1", "❷":"2", "❸":"3", "❹":"4", "❺":"5", "❻":"6", "①":"1", "②":"2", "③":"3", "④":"4", "⑤":"5", "⑥":"6"}
    lines = [ln.rstrip() for ln in str(raw_text or "").splitlines()]
    groups = []
    current = None

    def flush():
        nonlocal current
        if not current:
            return
        content = "\n".join([ln for ln in current["lines"] if ln.strip()]).strip()
        if content:
            groups.append({"training_group": current["group"], "content": content})
        current = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current and current["lines"]:
                current["lines"].append("")
            continue
        group = ""
        rest = stripped
        for mark, num in marker_map.items():
            if stripped.startswith(mark):
                group = num
                rest = stripped[len(mark):].strip()
                break
        if not group:
            m = re.match(r"^([1-6])[\.、\)]?\s*(.*)$", stripped)
            if m and "coach" in m.group(2).lower():
                group = m.group(1)
                rest = m.group(2).strip()
        if group:
            flush()
            current = {"group": group, "lines": []}
            # Coach 可能會變動：不寫入正式課表內容。
            if rest and not rest.lower().startswith("coach"):
                current["lines"].append(rest)
            continue
        if current:
            current["lines"].append(stripped)
    flush()
    return groups


def parse_weekly_group_training_plan(raw_text: str):
    """解析一整週多段課表。段落標題支援：訓練一/訓練二/訓練3/Day 1。"""
    lines = str(raw_text or "").splitlines()
    sessions = []
    current_label = "訓練一"
    current_lines = []
    order = 1
    label_pattern = re.compile(r"^(訓練[一二三四五六七八九十0-9]+|第[一二三四五六七八九十0-9]+練|Day\s*\d+|D\d+)\s*[:：-]?\s*$", re.I)

    def flush():
        nonlocal order, current_label, current_lines
        raw = "\n".join(current_lines).strip()
        if raw:
            groups = parse_group_training_plan(raw)
            if groups:
                sessions.append({"session_label": current_label, "session_order": order, "groups": groups})
                order += 1
        current_lines = []

    for line in lines:
        stripped = line.strip()
        if label_pattern.match(stripped):
            flush()
            current_label = stripped.rstrip(':：-').strip()
            continue
        current_lines.append(line)
    flush()
    return sessions


def parse_target_dates(raw_dates: str):
    tokens = [x.strip() for x in re.split(r"[,，、\n\s]+", str(raw_dates or "")) if x.strip()]
    dates = []
    for token in tokens:
        normalized = normalize_date_str(token)
        # 支援教練只輸入 MM/DD；自動補今年，避免寫成 05-31 造成會員端查不到。
        if re.match(r"^\d{1,2}/\d{1,2}$", normalized):
            normalized = f"{tw_today().year}/{normalized}"
        dates.append(normalized.replace("/", "-"))
    return dates


def append_group_training_plan(payload: AddGroupTrainingPlanPayload):
    groups = parse_group_training_plan(payload.raw_plan_text)
    if not groups:
        return False, "解析不到 ❶~❻ 分區課表，請確認格式。", 0
    ws = get_or_create_training_assignments_sheet()
    created_at = tw_now().strftime("%Y-%m-%d %H:%M:%S")
    plan_title = (payload.plan_title or payload.target_date or "分區課表").strip()
    class_name = payload.class_name.strip()
    target_date = normalize_date_str(payload.target_date).replace("/", "-") if payload.target_date else ""
    rows = []
    for g in groups:
        assignment_id = f"assign_{tw_now().strftime('%Y%m%d%H%M%S')}_{class_name}_{payload.session_order}_{g['training_group']}"
        rows.append([
            assignment_id, created_at, plan_title, class_name, g["training_group"],
            "", "", "", payload.session_label.strip() or "訓練一", payload.session_order,
            target_date, payload.workout_type.strip() or "跑步", g["content"], "", "group",
            payload.admin_uid, "assigned"
        ])
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    return True, f"✅ 已新增 {class_name} {payload.session_label} 的 {len(rows)} 個分區課表", len(rows)


def append_individual_training_plan(payload: AddIndividualTrainingPlanPayload):
    content = str(payload.content or "").strip()
    if not content:
        return False, "個人課表內容不可空白", 0
    ws = get_or_create_training_assignments_sheet()
    created_at = tw_now().strftime("%Y-%m-%d %H:%M:%S")
    target_date = normalize_date_str(payload.target_date).replace("/", "-") if payload.target_date else ""
    assignment_id = f"assign_{tw_now().strftime('%Y%m%d%H%M%S')}_individual_{payload.target_uid}_{payload.session_order}"
    row = [
        assignment_id, created_at, (payload.plan_title or payload.target_date or "個人微調課表").strip(),
        payload.class_name.strip(), normalize_training_group(payload.training_group),
        "", payload.target_uid, payload.member_name.strip(), payload.session_label.strip() or "個人微調", payload.session_order,
        target_date, payload.workout_type.strip() or "跑步", content, payload.note.strip(), "individual",
        payload.admin_uid, "assigned"
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return True, f"✅ 已新增 {payload.member_name or '學員'} 的個人微調課表", 1


def append_weekly_group_training_plan(payload: AddWeeklyGroupTrainingPlanPayload):
    sessions = parse_weekly_group_training_plan(payload.raw_weekly_text)
    if not sessions:
        return False, "解析不到週課表段落，請確認包含 訓練一/訓練二 與 ❶~❻ 內容。", 0
    dates = parse_target_dates(payload.target_dates)
    ws = get_or_create_training_assignments_sheet()
    created_at = tw_now().strftime("%Y-%m-%d %H:%M:%S")
    class_name = payload.class_name.strip()
    rows = []
    for idx, session in enumerate(sessions):
        target_date = dates[idx] if idx < len(dates) else ""
        for g in session["groups"]:
            assignment_id = f"assign_{tw_now().strftime('%Y%m%d%H%M%S')}_{class_name}_{session['session_order']}_{g['training_group']}"
            rows.append([
                assignment_id, created_at, payload.plan_title.strip() or "週課表", class_name, g["training_group"],
                "", "", "", session["session_label"], session["session_order"],
                target_date, payload.workout_type.strip() or "跑步", g["content"], "", "group",
                payload.admin_uid, "assigned"
            ])
    if not rows:
        return False, "沒有可寫入的分區課表", 0
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    return True, f"✅ 已新增 {class_name} 週課表：{len(sessions)} 個訓練日、{len(rows)} 筆分區課表", len(rows)


@app.post("/api/update-training-group")
async def update_training_group(payload: UpdateTrainingGroupPayload):
    if not is_coach_user(payload.admin_uid):
        return {"success": False, "error": "Unauthorized"}
    training_group = normalize_training_group(payload.training_group)
    if not training_group:
        return {"success": False, "error": "請輸入 1~6 的區別"}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("UPDATE health_profile SET training_group=? WHERE user_id=?", (training_group, payload.target_uid))
            conn.commit()
        return {"success": True, "training_group": training_group}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/add-group-training-plan")
async def add_group_training_plan(payload: AddGroupTrainingPlanPayload):
    if not is_coach_user(payload.admin_uid):
        return {"success": False, "error": "Unauthorized"}
    try:
        ok, msg, count = append_group_training_plan(payload)
        return {"success": ok, "message": msg, "count": count}
    except Exception as e:
        print(f"❌ 新增分區課表失敗: {e}")
        return {"success": False, "error": str(e)}

@app.post("/api/add-individual-training-plan")
async def add_individual_training_plan(payload: AddIndividualTrainingPlanPayload):
    if not is_coach_user(payload.admin_uid):
        return {"success": False, "error": "Unauthorized"}
    try:
        ok, msg, count = append_individual_training_plan(payload)
        return {"success": ok, "message": msg, "count": count}
    except Exception as e:
        print(f"❌ 新增個人微調課表失敗: {e}")
        return {"success": False, "error": str(e)}

@app.post("/api/add-weekly-group-training-plan")
async def add_weekly_group_training_plan(payload: AddWeeklyGroupTrainingPlanPayload):
    if not is_coach_user(payload.admin_uid):
        return {"success": False, "error": "Unauthorized"}
    try:
        ok, msg, count = append_weekly_group_training_plan(payload)
        return {"success": ok, "message": msg, "count": count}
    except Exception as e:
        print(f"❌ 新增週分區課表失敗: {e}")
        return {"success": False, "error": str(e)}


def get_user_assignment_context(user_id: str):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT name, COALESCE(status, ''), COALESCE(training_group, '') FROM health_profile WHERE user_id=?", (user_id,))
            row = c.fetchone()
            if row:
                return {"member_name": row[0] or "", "class_name": row[1] or "", "training_group": normalize_training_group(row[2])}
    except Exception as e:
        print(f"⚠️ 讀取學員分區失敗: {e}")
    return {"member_name": "", "class_name": "", "training_group": ""}


def _assignment_date_key(raw):
    s = str(raw or "").strip()
    if not s:
        return ""
    return normalize_date_str(s).replace("/", "-")


def format_assignment_workout(row):
    label = str(row.get("session_label", "") or "").strip()
    content = str(row.get("content", "") or "").strip()
    title = str(row.get("plan_title", "") or "").strip()
    parts = []
    if title:
        parts.append(title)
    if label:
        parts.append(label)
    if content:
        parts.append(content)
    return "\n".join(parts).strip() or "無"


def find_training_assignment_for_date(user_id: str, target_date) -> dict:
    if not gc:
        return {}
    ctx = get_user_assignment_context(user_id)
    date_key = target_date.isoformat() if hasattr(target_date, "isoformat") else _assignment_date_key(target_date)
    try:
        ws = get_or_create_training_assignments_sheet()
        records = ws.get_all_records()
    except Exception as e:
        print(f"⚠️ 讀取 training_assignments 失敗: {e}")
        return {}

    best = None
    best_priority = -1
    for row in records:
        if str(row.get("status", "assigned") or "assigned").strip() not in ["assigned", ""]:
            continue
        if _assignment_date_key(row.get("target_date")) != date_key:
            continue
        assignment_type = str(row.get("assignment_type", "group") or "group").strip()
        class_name = str(row.get("class_name", "") or "").strip()
        training_group = normalize_training_group(row.get("training_group"))
        member_uid = str(row.get("member_uid", "") or "").strip()
        priority = -1
        if assignment_type == "individual" and member_uid == user_id:
            priority = 3
        elif assignment_type == "group" and class_name == ctx["class_name"] and training_group and training_group == ctx["training_group"]:
            priority = 2
        elif assignment_type == "class" and class_name == ctx["class_name"]:
            priority = 1
        if priority >= best_priority and priority > 0:
            best = row
            best_priority = priority
    return best or {}

# 2. 戰情室網頁畫面 (HTML)
@app.get("/coach-dashboard", response_class=HTMLResponse)
async def coach_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>一日樂食-教練戰情室</title>
        <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body class="bg-slate-50 min-h-screen pb-10 relative">
        <div class="max-w-md mx-auto p-4">
            <h1 class="text-2xl font-bold mb-4 text-blue-600 flex items-center gap-2">
                <span class="text-3xl">📊</span> 教練戰情室
            </h1>
    <style>.hide-scrollbar::-webkit-scrollbar { display: none; }</style>        
            <div id="group-tabs" class="flex gap-2 overflow-x-auto mb-4 pb-2 hide-scrollbar">
                </div>

            <div class="grid grid-cols-2 gap-2 mb-4">
                <button onclick="addGroupTrainingPlan()" class="bg-emerald-600 text-white rounded-xl py-3 font-bold shadow-sm hover:bg-emerald-700 text-sm">➕ 單日分區</button>
                <button onclick="addWeeklyGroupTrainingPlan()" class="bg-indigo-600 text-white rounded-xl py-3 font-bold shadow-sm hover:bg-indigo-700 text-sm">🗓️ 週課表</button>
            </div>

            <div id="loader" class="text-center py-10 text-gray-500 animate-pulse">資料讀取中...</div>
            <div id="student-list" class="space-y-5 hidden"></div>
        </div>

        <div id="chartModal" class="fixed inset-0 bg-slate-900 bg-opacity-40 z-50 hidden flex-col justify-end transition-opacity">
            <div id="chartModalContent" class="bg-white w-full rounded-t-3xl p-5 pb-10 transform transition-transform translate-y-full duration-300">
                <div class="flex justify-between items-center mb-4">
                    <h2 id="chartTitle" class="text-xl font-bold text-slate-800">歷史趨勢</h2>
                    <button onclick="closeChart()" class="bg-slate-100 text-slate-600 rounded-full w-8 h-8 flex items-center justify-center font-bold hover:bg-slate-200">X</button>
                </div>
                <div class="relative w-full" style="height: 250px;">
                    <canvas id="myChart"></canvas>
                </div>
            </div>
        </div>

        <script>
            let globalData = []; // 儲存所有學員資料
            let myChartInstance = null; // 儲存圖表實例
            let currentGroup = '全部'; // 目前選擇的群組

            // --- 1. 傳送關心 ---
            window.sendCare = async function(studentUid, studentName, load) {
                let draft = `這週訓練負荷達到 ${load}，辛苦了！記得多補充蛋白質並好好恢復喔！`;
                if (load > 400) draft = `這週訓練負荷偏高 (${load})，要注意身體有沒有異常痠痛，必要時多休息一天！`;
                if (load < 100) draft = `這週訓練量比較少喔，這週末有安排什麼運動計畫嗎？`;

                const msg = prompt(`要傳送給 ${studentName} 什麼關心訊息？`, draft);
                if (!msg) return;

                try {
                    const profile = await liff.getProfile();
                    const res = await fetch('/api/send-care', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ admin_uid: profile.userId, target_uid: studentUid, message: msg })
                    });
                    const data = await res.json();
                    if (data.success) alert(`✅ 已成功傳送給 ${studentName}！`);
                    else alert(`❌ 發送失敗：${data.error}`);
                } catch(e) { alert('系統錯誤。'); }
            };

            // --- 2. 顯示圖表 ---
            window.showChart = function(uid) {
                const student = globalData.find(s => s.uid === uid);
                if(!student) return;

                document.getElementById('chartTitle').innerText = student.name + ' 的訓練趨勢';
                const modal = document.getElementById('chartModal');
                const modalContent = document.getElementById('chartModalContent');
                modal.classList.remove('hidden');
                setTimeout(() => modalContent.classList.remove('translate-y-full'), 10);

                const ctx = document.getElementById('myChart').getContext('2d');
                if(myChartInstance) myChartInstance.destroy();

                const barColors = student.chart_data.loads.map(load => load > 200 ? 'rgba(249, 115, 22, 0.8)' : 'rgba(59, 130, 246, 0.8)');

                myChartInstance = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: student.chart_data.dates,
                        datasets: [
                            { type: 'line', label: '平均心率', data: student.chart_data.hrs, borderColor: '#ef4444', backgroundColor: '#ef4444', yAxisID: 'y-hr', tension: 0.3, borderWidth: 2 },
                            { type: 'bar', label: '訓練負荷', data: student.chart_data.loads, backgroundColor: barColors, borderRadius: 4, yAxisID: 'y-load' }
                        ]
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
                        scales: {
                            'y-load': { type: 'linear', display: true, position: 'left', beginAtZero: true, title: {display: true, text: '負荷 (柱狀)'} },
                            'y-hr': { type: 'linear', display: true, position: 'right', grid: {drawOnChartArea: false}, title: {display: true, text: '心率 (折線)'} }
                        }
                    }
                });
            };

            // --- 3. 關閉圖表 ---
            window.closeChart = function() {
                const modalContent = document.getElementById('chartModalContent');
                modalContent.classList.add('translate-y-full');
                setTimeout(() => {
                    document.getElementById('chartModal').classList.add('hidden');
                    if(myChartInstance) myChartInstance.destroy();
                }, 300);
            };

            // --- 4. 🌟 新增：更新分班 ---
            window.changeGroup = async function(studentUid, studentName, currentGroup) {
                const newGroup = prompt(`請輸入 ${studentName} 的新班級名稱：\n(例如：新手班、進階班、減脂營)`, currentGroup);
                if (!newGroup || newGroup === currentGroup) return;

                try {
                    const profile = await liff.getProfile();
                    const res = await fetch('/api/update-group', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ admin_uid: profile.userId, target_uid: studentUid, new_group: newGroup })
                    });
                    const data = await res.json();
                    if (data.success) {
                        alert(`✅ 已將 ${studentName} 移至 ${newGroup}！`);
                        window.location.reload(); 
                    } else {
                        alert(`❌ 更新失敗：${data.error}`);
                    }
                } catch(e) { alert('系統錯誤。'); }
            };

            // --- 4B. 更新 1~6 區 ---
            window.changeTrainingGroup = async function(studentUid, studentName, currentTrainingGroup) {
                const newGroup = prompt(`請輸入 ${studentName} 的區別（1~6）：`, currentTrainingGroup || '');
                if (!newGroup || newGroup === currentTrainingGroup) return;
                try {
                    const profile = await liff.getProfile();
                    const res = await fetch('/api/update-training-group', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ admin_uid: profile.userId, target_uid: studentUid, training_group: newGroup })
                    });
                    const data = await res.json();
                    if (data.success) {
                        alert(`✅ 已將 ${studentName} 設為 ${data.training_group} 區！`);
                        window.location.reload();
                    } else {
                        alert(`❌ 更新失敗：${data.error}`);
                    }
                } catch(e) { alert('系統錯誤。'); }
            };

            // --- 共用：多行貼上課表輸入框 ---
            window.openLargeTextModal = function(title, placeholder = '', defaultText = '') {
                return new Promise((resolve) => {
                    const overlay = document.createElement('div');
                    overlay.className = 'fixed inset-0 bg-slate-900 bg-opacity-50 z-[9999] flex items-end justify-center p-0 sm:p-4';
                    overlay.innerHTML = `
                        <div class="bg-white w-full max-w-lg rounded-t-3xl sm:rounded-3xl p-5 shadow-xl">
                            <div class="flex items-center justify-between mb-3">
                                <h2 class="text-lg font-bold text-slate-800">${title}</h2>
                                <button type="button" data-action="cancel" class="text-slate-500 bg-slate-100 rounded-full w-8 h-8 font-bold">×</button>
                            </div>
                            <p class="text-xs text-slate-500 mb-2">可直接貼上完整多行課表；按「儲存」送出。</p>
                            <textarea data-role="textarea" class="w-full h-80 border border-slate-300 rounded-xl p-3 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500" placeholder="${placeholder}"></textarea>
                            <div class="grid grid-cols-2 gap-2 mt-4">
                                <button type="button" data-action="cancel" class="bg-slate-100 text-slate-700 rounded-xl py-3 font-bold">取消</button>
                                <button type="button" data-action="submit" class="bg-blue-600 text-white rounded-xl py-3 font-bold">儲存</button>
                            </div>
                        </div>`;
                    document.body.appendChild(overlay);
                    const textarea = overlay.querySelector('[data-role="textarea"]');
                    textarea.value = defaultText || '';
                    const close = (value) => {
                        overlay.remove();
                        resolve(value);
                    };
                    overlay.querySelectorAll('[data-action="cancel"]').forEach(btn => btn.onclick = () => close(null));
                    overlay.querySelector('[data-action="submit"]').onclick = () => close(textarea.value);
                    textarea.addEventListener('keydown', (e) => {
                        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') close(textarea.value);
                    });
                    setTimeout(() => textarea.focus(), 50);
                });
            };

            // --- 4C. 貼上 ❶~❻ 分區課表 ---
            window.addGroupTrainingPlan = async function() {
                const className = prompt('班級 / 大分組（例如：週三班）：', currentGroup !== '全部' ? currentGroup : '週三班');
                if (!className) return;
                const targetDate = prompt('課表日期（例如：2026-05-27；可留空）：', '');
                const planTitle = prompt('課表名稱（例如：05/27訓練課表）：', targetDate ? `${targetDate}訓練課表` : '分區訓練課表');
                const sessionLabel = prompt('訓練標籤（例如：訓練一 / 訓練二）：', '訓練一');
                const rawPlanText = await openLargeTextModal('貼上單日 ❶~❻ 分區課表', '❶Coach：\\n課表內容...\\n\\n❷Coach：\\n課表內容...');
                if (!rawPlanText) return;
                try {
                    const profile = await liff.getProfile();
                    const res = await fetch('/api/add-group-training-plan', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            admin_uid: profile.userId,
                            class_name: className,
                            target_date: targetDate || '',
                            plan_title: planTitle || '',
                            session_label: sessionLabel || '訓練一',
                            session_order: 1,
                            workout_type: '跑步',
                            raw_plan_text: rawPlanText
                        })
                    });
                    const data = await res.json();
                    if (data.success) alert(data.message || '✅ 已新增分區課表');
                    else alert(`❌ 新增失敗：${data.error || data.message}`);
                } catch(e) { alert('系統錯誤。'); }
            };

            // --- 4D. 一次新增整週多日分區課表 ---
            window.addWeeklyGroupTrainingPlan = async function() {
                const className = prompt('班級 / 大分組（例如：週三班）：', currentGroup !== '全部' ? currentGroup : '週三班');
                if (!className) return;
                const targetDates = prompt('各訓練日日期，依序用逗號分隔（例如：2026-05-27,2026-05-29,2026-05-31；可留空）：', '');
                const planTitle = prompt('週課表名稱（例如：05/27這週課表）：', '週課表');
                const rawWeeklyText = await openLargeTextModal('貼上整週多日分區課表', '格式：先寫「訓練一」再貼 ❶~❻，接著「訓練二」再貼 ❶~❻', `訓練一
❶Coach：

❷Coach：

訓練二
❶Coach：

❷Coach：`);
                if (!rawWeeklyText) return;
                try {
                    const profile = await liff.getProfile();
                    const res = await fetch('/api/add-weekly-group-training-plan', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            admin_uid: profile.userId,
                            class_name: className,
                            plan_title: planTitle || '週課表',
                            workout_type: '跑步',
                            target_dates: targetDates || '',
                            raw_weekly_text: rawWeeklyText
                        })
                    });
                    const data = await res.json();
                    if (data.success) alert(data.message || '✅ 已新增週課表');
                    else alert(`❌ 新增失敗：${data.error || data.message}`);
                } catch(e) { alert('系統錯誤。'); }
            };

            // --- 4E. 個人微調課表：優先覆蓋分區課表 ---
            window.addIndividualTrainingPlan = async function(studentUid, studentName, className, trainingGroup) {
                const targetDate = prompt(`要替 ${studentName} 微調哪一天？（例如：2026-05-27）`, '');
                if (!targetDate) return;
                const sessionLabel = prompt('訓練標籤（例如：訓練一 / 個人微調）：', '個人微調');
                const content = prompt('請輸入個人微調課表內容：', '');
                if (!content) return;
                const note = prompt('備註（可留空）：', '');
                try {
                    const profile = await liff.getProfile();
                    const res = await fetch('/api/add-individual-training-plan', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            admin_uid: profile.userId,
                            target_uid: studentUid,
                            member_name: studentName,
                            class_name: className || '',
                            training_group: trainingGroup || '',
                            target_date: targetDate,
                            plan_title: `${targetDate} 個人微調`,
                            session_label: sessionLabel || '個人微調',
                            session_order: 1,
                            workout_type: '跑步',
                            content: content,
                            note: note || ''
                        })
                    });
                    const data = await res.json();
                    if (data.success) alert(data.message || '✅ 已新增個人微調');
                    else alert(`❌ 新增失敗：${data.error || data.message}`);
                } catch(e) { alert('系統錯誤。'); }
            };

            // --- 5. 畫出上方標籤按鈕 ---
            function renderTabs() {
                const tabsContainer = document.getElementById('group-tabs');
                if (!tabsContainer) return; 
                
                tabsContainer.innerHTML = '';
                const groups = ['全部', ...new Set(globalData.map(s => s.group))];
                
                groups.forEach(grp => {
                    const btn = document.createElement('button');
                    const isActive = (grp === currentGroup);
                    btn.className = `whitespace-nowrap px-4 py-1.5 rounded-full text-sm font-bold transition-colors ${isActive ? 'bg-blue-600 text-white shadow-md' : 'bg-slate-200 text-slate-600 hover:bg-slate-300'}`;
                    btn.innerText = grp;
                    btn.onclick = () => {
                        currentGroup = grp;
                        renderTabs(); 
                        renderList(); 
                    };
                    tabsContainer.appendChild(btn);
                });
            }

            // --- 6. 🌟 畫出名單 (包含可點擊的分類標籤) ---
            function renderList() {
                const list = document.getElementById('student-list');
                list.innerHTML = ''; 
                
                const filteredData = globalData.filter(std => currentGroup === '全部' || std.group === currentGroup);
                
                if (filteredData.length === 0) {
                    list.innerHTML = `<div class="text-center text-gray-400 py-8">此群組尚無紀錄</div>`;
                    return;
                }
                
                filteredData.forEach(std => {
                    let loadColor = "bg-green-50 text-green-600 border-green-200";
                    let loadIcon = "✅";
                    if (std.total_load > 400) { loadColor = "bg-red-50 text-red-600 border-red-200"; loadIcon = "⚠️"; }
                    else if (std.total_load < 100) { loadColor = "bg-yellow-50 text-yellow-600 border-yellow-200"; loadIcon = "💤"; }

                    const card = document.createElement('div');
                    card.className = "bg-white p-5 rounded-2xl shadow-sm border border-slate-100 relative overflow-hidden";
                    
                    const logsHtml = std.logs.slice(0, 7).map(log => 
                        `<div class="flex justify-between items-center text-sm text-slate-600 py-2 border-b border-slate-50 last:border-0">
                            <span class="font-medium text-slate-500">${log.split(' ')[0]}</span>
                            <span>${log.substring(log.indexOf(' ') + 1)}</span>
                        </div>`
                    ).join('');

                    card.innerHTML = `
                        <div class="absolute left-0 top-0 bottom-0 w-1 ${loadColor.split(' ')[0].replace('50', '400')}"></div>
                        <div class="flex justify-between items-start mb-4 pl-2">
                            <div>
                                <h2 class="font-bold text-xl text-slate-800">${std.name}</h2>
                                <button onclick="changeGroup('${std.uid}', '${std.name}', '${std.group}')" class="inline-flex items-center gap-1 mt-1 bg-slate-100 text-slate-500 hover:bg-slate-200 text-[10px] px-2 py-1 rounded transition-colors cursor-pointer">
                                    <span>🏷️ ${std.group}</span>
                                    <span class="text-[8px]">▼</span>
                                </button>
                                <button onclick="changeTrainingGroup('${std.uid}', '${std.name}', '${std.training_group || ''}')" class="inline-flex items-center gap-1 mt-1 ml-1 bg-emerald-50 text-emerald-600 hover:bg-emerald-100 text-[10px] px-2 py-1 rounded transition-colors cursor-pointer">
                                    <span>📍 ${std.training_group ? std.training_group + '區' : '未分區'}</span>
                                    <span class="text-[8px]">▼</span>
                                </button>
                            </div>
                            <div class="text-right">
                                <div class="${loadColor} border px-3 py-1.5 rounded-lg text-sm font-bold inline-flex items-center gap-1">
                                    <span>${loadIcon}</span> 負荷 ${std.total_load}
                                </div>
                                <div class="text-xs text-slate-500 mt-1.5 font-medium">⏱️ 總計 ${std.total_dur} 分鐘</div>
                            </div>
                        </div>
                        <div class="bg-slate-50 rounded-xl p-3 pl-4 border border-slate-100 mb-4">${logsHtml}</div>
                        <div class="grid grid-cols-3 gap-2 pl-2">
                            <button onclick="sendCare('${std.uid}', '${std.name}', ${std.total_load})" class="bg-blue-50 text-blue-600 text-xs py-2.5 rounded-xl font-bold hover:bg-blue-100 transition-all">💬 關心</button>
                            <button onclick="addIndividualTrainingPlan('${std.uid}', '${std.name}', '${std.group}', '${std.training_group || ''}')" class="bg-amber-50 text-amber-600 text-xs py-2.5 rounded-xl font-bold hover:bg-amber-100 transition-all">✏️ 微調</button>
                            <button onclick="showChart('${std.uid}')" class="bg-slate-50 text-slate-600 text-xs py-2.5 rounded-xl font-bold hover:bg-slate-100 transition-all">📊 圖表</button>
                        </div>
                    `;
                    list.appendChild(card);
                });
            }

            // --- 7. 乾淨的啟動函數 ---
            async function init() {
                try {
                    await liff.init({ liffId: "2009824277-W3lYtSjF" });
                    if (!liff.isLoggedIn()) { liff.login(); return; }
                    
                    const profile = await liff.getProfile();
                    const res = await fetch(`/api/coach-data?admin_uid=${profile.userId}`);
                    const data = await res.json();
                    
                    globalData = data; 
                    
                    document.getElementById('loader').classList.add('hidden');
                    document.getElementById('student-list').classList.remove('hidden');
                    
                    renderTabs();
                    renderList();

                } catch (err) {
                    document.getElementById('loader').innerText = "❌ 讀取失敗：" + err.message;
                }
            }
            
            init();
        </script>
    </body>
    </html>
    """
# 喚醒 Google 虛擬助理 (🔥 卸下裝甲，回歸純淨版)
try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    # 1. 直接從保險箱拿出完美的字串
    creds_str = os.environ.get("GOOGLE_CREDENTIALS")
    
    # 2. 原汁原味轉成字典 (什麼 replace 都不用加，因為您貼得太完美了！)
    creds_dict = json.loads(creds_str)
    
    # 3. 直接拿鑰匙開門
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    print("✅ Google 雲端大門正式開啟！寫入權限 100% 取得！")
    
except Exception as e:
    print(f"⚠️ Google 助理連線失敗: {e}")
    if not gc:  # 保留第一次成功的連線，不要蓋掉
        gc = None

# ==========================================
# 2. 菜單資料載入 (🔥 新增：主餐/單品精準分類與熱更新)
# ==========================================
MAIN_DISHES = []
def load_menu():
    global MAIN_DISHES
    MAIN_DISHES.clear()
    try:
        with open("menu.csv", mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_clean = {k.strip() if isinstance(k, str) else k: v for k, v in row.items()}
                name = row_clean.get("品項", "").strip()
                if not name: continue
                try:
                    cal = float(row_clean.get("熱量(kcal)", "0").strip() or 0.0)
                    pro = float(row_clean.get("蛋白質(g)", "0").strip() or 0.0)
                    fat = float(row_clean.get("脂肪(g)", "0").strip() or 0.0)
                    carbs = float(row_clean.get("碳水化合物(g)", row_clean.get("碳水(g)", "0")).strip() or 0.0)
                    price = int(row_clean.get("價錢", row_clean.get("價格", "150")).strip() or 150)
                    ingredients = row_clean.get("內容物", "新鮮食材製作").strip()
                    main_keywords = ["便當", "麵", "食蔬", "低碳", "沙拉", "原型"]
                    drink_keywords = ["豆漿", "茶"]
                    if any(kw in name for kw in drink_keywords):
                        category = "drink"
                    elif any(kw in name for kw in main_keywords):
                        category = "main"
                    else:
                        category = "side"
                    # 🔥 碳循環分類：高碳 / 低碳
                    if any(kw in name for kw in ["便當", "食蔬", "麵"]):
                        carb_type = "高碳"
                    else:
                        carb_type = "低碳"  # 沙拉、低碳、其他
                    dish = {
                        "name": name, "cal": cal, "pro": pro, "fat": fat, "carbs": carbs,
                        "calories_kcal": cal, "protein_g": pro, "fat_g": fat, "carbohydrate_g": carbs,
                        "price": price, "category": category, "ingredients": ingredients,
                        "carb_type": carb_type, "safe": True, "available": True,
                    }
                    exchange_columns = {
                        "milk_exchange": "奶份", "protein_low_exchange": "低脂蛋白份",
                        "protein_medium_exchange": "中脂蛋白份", "protein_high_exchange": "高脂蛋白份",
                        "starch_exchange": "主食份", "vegetable_exchange": "蔬菜份",
                        "fruit_exchange": "水果份", "fat_exchange": "油脂份",
                    }
                    for exchange_key, column_name in exchange_columns.items():
                        raw_exchange = row_clean.get(column_name)
                        if raw_exchange is not None and str(raw_exchange).strip() != "":
                            dish[exchange_key] = float(str(raw_exchange).strip())
                    MAIN_DISHES.append(dish)
                except Exception as e:
                    # 🔥 抓蟲程式碼必須放在這裡，對齊內部的 try！
                    print(f"⚠️ 跳過餐點【{name}】: 數字格式有誤，原因：{e}")
                    
        print(f"✅ 成功載入 {len(MAIN_DISHES)} 項餐點！")
        return f"✅ 菜單更新成功！共載入 {len(MAIN_DISHES)} 項餐點。"
    except Exception as e: 
        print(f"⚠️ 讀取 menu.csv 失敗: {e}")
        return "❌ 菜單更新失敗，請檢查檔案。"


def sync_menu_to_food_catalog():
    """同步 menu.csv 的餐點到 food_catalog，讓 LINE 搜尋與常吃清單可用。"""
    if not MAIN_DISHES:
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            ensure_nutrition_schema(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            inserted = 0
            for dish in MAIN_DISHES:
                name = dish["name"]
                menu_category = str(dish.get("category") or "").strip()
                if menu_category not in {"main", "side", "drink"}:
                    menu_category = "side"
                existing = conn.execute(
                    """SELECT food_id FROM food_catalog
                       WHERE product_name=? AND source_type='label'
                         AND owner_user_id='system'""",
                    (name,),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE food_catalog
                           SET menu_category=?, visibility='public'
                           WHERE food_id=?""",
                        (menu_category, existing[0]),
                    )
                    continue
                per_serving = {
                    "calories_kcal": dish.get("calories_kcal", 0),
                    "protein_g": dish.get("protein_g", 0),
                    "fat_g": dish.get("fat_g", 0),
                    "carbohydrate_g": dish.get("carbohydrate_g", 0),
                }
                fid = f"menu_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    """INSERT INTO food_catalog
                       (food_id, product_name, brand, barcode, source_type, owner_user_id, visibility,
                        menu_category,
                        package_amount, package_unit, servings_per_package, per_serving_json, per_100_json,
                        exchange_json, exchange_review_status, fingerprint, original_image_ref,
                        recognition_confidence, verification_status, created_at, updated_at)
                       VALUES (?,?,'','','label','system','public',?,
                               1,'份',1,?,'{}',
                               '{}','approved',?,'',1.0,'auto',?,?)""",
                    (fid, name, menu_category, json.dumps(per_serving), fid, now, now),
                )
                inserted += 1
            conn.commit()
            if inserted:
                print(f"✅ 已同步 {inserted} 道菜單到 food_catalog")
    except Exception as e:
        print(f"⚠️ 同步菜單到 food_catalog 失敗: {e}")
# ==========================================
# 3. 資料庫初始化 (🔥 升級版：支援點數網址與發放紀錄)
# ==========================================
def init_db():
    # 單一資料路徑來源：必須遵守 DATA_DIR／DB_PATH，才能安全掛載 Railway Volume。
    os.makedirs(DB_DIR, mode=0o700, exist_ok=True)

    try:
        # 🔗 3. 安全連線
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # --- 以下是您的原本表格定義 (保持不變) ---
        c.execute('''CREATE TABLE IF NOT EXISTS usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS health_profile (user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL, goal TEXT, restrictions TEXT, summary_text TEXT, active_days TEXT)''')
        # 升級：確保後來新增的欄位存在（防止新環境重建資料庫缺欄位）
        try:
            c.execute("ALTER TABLE health_profile ADD COLUMN sheet_name TEXT")
            c.execute("ALTER TABLE health_profile ADD COLUMN today_extra_pro INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # 欄位已存在
        c.execute('''CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT)''')
        
        # 🔥 行銷問卷專用的資料表
        c.execute('''CREATE TABLE IF NOT EXISTS reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS recent_meal_logs (
            user_id TEXT PRIMARY KEY,
            meal_name TEXT,
            base_cal INTEGER,
            base_pro INTEGER,
            current_cal INTEGER,
            current_pro INTEGER,
            meal_date TEXT,
            source_text TEXT,
            updated_at TEXT,
            food_log_id TEXT DEFAULT ''
        )''')
        try:
            c.execute("ALTER TABLE recent_meal_logs ADD COLUMN food_log_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS deferred_meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            customer_name TEXT DEFAULT '',
            original_date TEXT NOT NULL,
            original_meal_type TEXT NOT NULL,
            target_date TEXT NOT NULL,
            target_meal_type TEXT NOT NULL,
            is_cross_period INTEGER DEFAULT 0,
            has_conflict INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            approved_at TEXT DEFAULT '',
            approved_by TEXT DEFAULT ''
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS planned_meal_checks (
            user_id TEXT,
            meal_date TEXT,
            meal_slot TEXT,
            meal_name TEXT,
            cal INTEGER,
            pro INTEGER,
            checked_at TEXT,
            PRIMARY KEY (user_id, meal_date, meal_slot)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS subscription_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            customer_name TEXT DEFAULT '',
            meal_count INTEGER DEFAULT 0,
            address TEXT DEFAULT '',
            distance_text TEXT DEFAULT '',
            delivery_fee INTEGER DEFAULT 0,
            delivery_count INTEGER DEFAULT 0,
            meal_low_total INTEGER DEFAULT 0,
            meal_high_total INTEGER DEFAULT 0,
            delivery_total INTEGER DEFAULT 0,
            quote_low_total INTEGER DEFAULT 0,
            quote_high_total INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            admin_note TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            approved_at TEXT DEFAULT '',
            approved_by TEXT DEFAULT '',
            activated_at TEXT DEFAULT '',
            vip_code TEXT DEFAULT ''
        )''')
        for _col, _ddl in [
            ("form_payload_json", "TEXT DEFAULT ''"),
            ("formalized_at", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE subscription_orders ADD COLUMN {_col} {_ddl}")
            except sqlite3.OperationalError:
                pass
        c.execute('''CREATE TABLE IF NOT EXISTS workout_checks (
            user_id TEXT,
            workout_date TEXT,
            workout_name TEXT,
            checked_at TEXT,
            PRIMARY KEY (user_id, workout_date)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS frequent_foods (
            user_id TEXT,
            meal_name TEXT,
            last_cal INTEGER,
            last_pro INTEGER,
            use_count INTEGER DEFAULT 1,
            last_used_at TEXT,
            PRIMARY KEY (user_id, meal_name)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_achievements (
            user_id TEXT PRIMARY KEY,
            xp_total INTEGER DEFAULT 0,
            streak_days INTEGER DEFAULT 0,
            last_valid_plan_date TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS achievement_daily_log (
            user_id TEXT,
            log_date TEXT,
            has_today_plan INTEGER DEFAULT 0,
            meals_logged_count INTEGER DEFAULT 0,
            protein_80_reached INTEGER DEFAULT 0,
            workout_done INTEGER DEFAULT 0,
            task_done INTEGER DEFAULT 0,
            valid_completed_day INTEGER DEFAULT 0,
            today_xp_earned INTEGER DEFAULT 0,
            badge_unlocked_today INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, log_date)
        )''')
        
        # 👇 這裡就是正確加入的 sRPE 升級欄位 👇
        try:
            c.execute("ALTER TABLE user_achievements ADD COLUMN weekly_srpe INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # 👆 確保有 except 承接錯誤 👆

        # ── 教練運動日誌 workout_records ──────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS workout_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            workout_date TEXT,
            workout_type TEXT,
            duration_min INTEGER,
            avg_hr INTEGER,
            max_hr INTEGER,
            aerobic_te REAL,
            anaerobic_te REAL,
            primary_benefit TEXT,
            load_value INTEGER,
            np_w INTEGER,
            if_value REAL,
            tss REAL,
            ftp_w INTEGER,
            created_at TEXT,
            UNIQUE(user_id, workout_date, workout_type)
        )''')

        for col, dtype in [("today_extra_cal", "INTEGER DEFAULT 0"), 
                           ("today_date", "TEXT DEFAULT ''"), 
                           ("sheet_name", "TEXT DEFAULT ''"), 
                           ("today_extra_pro", "INTEGER DEFAULT 0"), 
                           ("today_food_items", "TEXT DEFAULT ''"),
                           ("is_coaching_enabled", "INTEGER DEFAULT 1"), # 🔥 教練權限
                           ("is_carb_cycling_enabled", "INTEGER DEFAULT 1"), # 🔥 碳循環開關
                           ("ai_silenced_until", "TEXT DEFAULT ''"),     # 🔥 客服靜音倒數
                           ("ai_mute", "INTEGER DEFAULT 0"),             # 🔑 功能一：老闆靜音旗標
                           ("user_level", "INTEGER DEFAULT 2"),          # 🔥 碳循環等級
                           ("race_date", "TEXT DEFAULT ''"),            # 🔥 目標賽事日期
                           ("address", "TEXT DEFAULT ''"),
                           ("distance_text", "TEXT DEFAULT ''"),
                           ("distance_meters", "INTEGER DEFAULT 0"),
                           ("delivery_fee", "INTEGER DEFAULT 0"),
                           ("delivery_zone", "TEXT DEFAULT ''"),
                           ("route_group", "TEXT DEFAULT ''"),
                           ("delivery_note", "TEXT DEFAULT ''"),
                           ("training_group", "TEXT DEFAULT ''")]:
            try: 
                c.execute(f"ALTER TABLE health_profile ADD COLUMN {col} {dtype}")
            except sqlite3.OperationalError: 
                pass
        # 營養辨識、客製化計畫與完整飲食紀錄資料表（只新增，不覆寫既有資料）
        ensure_nutrition_schema(conn)
        ensure_daily_food_ledger_schema(conn)
        migrate_current_day_legacy_totals_to_ledger(conn)
        # Jason 每日健康回報與23:30日報冪等推送狀態。
        ensure_daily_health_schema(conn)
        # 無營養標示餐點照片的持久草稿與按鈕確認狀態。
        ensure_meal_photo_schema(conn)

        # --- 以上結束 ---

        conn.commit()
        conn.close()
        print(f"✅ 保險箱資料庫連線成功！路徑: {DB_PATH}")

    except Exception as e:
        print(f"❌ 啟動保險箱失敗，錯誤原因: {e}")
init_db()
load_menu()  # 🔥 伺服器啟動時自動載入菜單
sync_menu_to_food_catalog()  # 同步菜單到 food_catalog

# ==========================================
# 4. 接收表單與配餐 (過敏原雷達 + 完美排序)
# ==========================================
@app.post("/form-data")
async def receive_form_data(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        print(f"📦 [表單測試] 收到 Google 傳來的大禮包：{data}")
        
        def get_val(keyword):
            for k, v in data.items():
                if keyword in k and v: 
                    return ",".join([str(i) for i in v]) if isinstance(v, list) else str(v)
            return ""
        
        user_id = get_val("UID")
        print(f"🔍 [表單測試] 抓到的 UID 是：'{user_id}'")
        print(f"🔑 [DEBUG] 表單所有欄位 keys：{list(data.keys())}")
        print(f"📝 [DEBUG] 稱呼欄位比對結果：{ {k: v for k, v in data.items() if '稱呼' in str(k)} }")
        
        if not user_id or user_id == "UID_REPLACE_ME": 
            print("❌ [表單拒絕] 找不到有效的 UID，這張表單我直接丟掉！")
            return {"status": "ignored"}
        if user_id in user_memory: del user_memory[user_id]

        name, goal, restrictions = get_val("稱呼"), get_val("目標"), get_val("禁忌")
        pickup_method = get_val("本期取餐方式") or get_val("取餐方式") or get_val("配送方式") or ""
        address = get_val("本期外送地址") or get_val("地址") or get_val("外送地址") or get_val("收件地址") or ""
        is_delivery = ("外送" in pickup_method) or (not pickup_method and bool(address))
        delivery_info = calculate_delivery_quote(address) if (is_delivery and address) else {
            "success": False,
            "address": address,
            "distance_text": "",
            "distance_meters": 0,
            "duration_text": "",
            "delivery_fee": 0,
            "delivery_fee_text": "自取或未提供地址",
            "hub_name": "",
            "route_group": "OTHER",
            "delivery_zone": "SELF_PICKUP" if not is_delivery else "未分類",
            "carpool_hint": "",
        }
        weight, height, age, gender = float(get_val("體重") or 70), float(get_val("身高") or 170), float(get_val("年齡") or 30), get_val("性別")
        # 🔥 身高防呆：如果客人填 1.76 公尺，自動轉成 176 公分
        if height < 3.0:
            height *= 100
        activity = get_val("活動量")  
        # 雙開關骨架：安排課表 / 啟用碳循環
        coaching_raw = get_val("規律運動") or get_val("安排課表") or ""
        sport_type = get_val("運動訓練菜單") or "未設定"
        is_coaching_enabled = 1
        if coaching_raw:
            if "沒有" in coaching_raw or "飲食控制" in coaching_raw or "不需要" in coaching_raw:
                is_coaching_enabled = 0
            elif "安排課表" in coaching_raw or "有" in coaching_raw:
                is_coaching_enabled = 1

        carb_switch_raw = get_val("啟用碳循環") or ""
        if carb_switch_raw:
            is_carb_cycling_enabled = 1 if ("是" in carb_switch_raw or "啟用" in carb_switch_raw) else 0
        else:
            # 舊表單尚未放開關時，先沿用舊行為：有教練流程預設開啟，否則關閉
            is_carb_cycling_enabled = 1 if is_coaching_enabled else 0

        # 🔥 碳循環：等級與賽事日期
        _level_raw = get_val("Level") or get_val("等級") or ""
        if "初" in _level_raw or "1" in _level_raw:
            user_level = 1
        elif "高" in _level_raw or "3" in _level_raw:
            user_level = 3
        elif "進" in _level_raw or "2" in _level_raw:
            user_level = 2
        else:
            try:
                import re as _re
                _m = _re.search(r'\d+', _level_raw)
                user_level = int(_m.group()) if _m else 2
            except:
                user_level = 2
        race_date_raw = get_val("Race Date") or get_val("賽事日期") or ""
        # 統一轉為 YYYY/MM/DD 格式
        race_date = ""
        if race_date_raw:
            for fmt in ["%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"]:
                try:
                    from datetime import datetime as _dt
                    race_date = _dt.strptime(race_date_raw.strip(), fmt).strftime("%Y/%m/%d")
                    break
                except Exception:
                    continue
            if not race_date:
                race_date = race_date_raw  # fallback 原始字串
        training_freq = get_val("確認訓練頻率") or "未設定"
        normal_train_time = get_val("一般訓練日") or "未設定"
        long_train_day = get_val("長距離") or "未設定"
        run_pace = get_val("5K") or "未提供"
        bike_ftp = get_val("FTP") or "未提供"
        swim_pace = get_val("CSS") or "未提供"
        bmr = (10 * weight + 6.25 * height - 5 * age - 161) if "女" in gender else (10 * weight + 6.25 * height - 5 * age + 5)
        act_mult = 1.2
        if "輕" in activity: act_mult = 1.375
        elif "中" in activity: act_mult = 1.55
        elif "高" in activity: act_mult = 1.725
        elif "極" in activity: act_mult = 1.9
        tdee_base = bmr * act_mult
        
        protein = weight * 1.6
        if "減脂" in goal: 
            tdee = tdee_base - 300
            protein = weight * 2.0
        elif "增肌" in goal: 
            tdee = tdee_base + 300
            protein = weight * 2.0
        else: tdee = tdee_base
        
        base_lunch_pool = [d for d in MAIN_DISHES if d.get('category') == 'main']
        base_dinner_pool = [d for d in MAIN_DISHES if d.get('category') == 'main']
        
        if restrictions:
            noise_words = ['跟', '和', '與', '、', '，', ' ', '不吃', '不要', '不能', '不能吃', '過敏', '類', '我對', '另外']
            clean_res = restrictions
            for noise in noise_words:
                clean_res = clean_res.replace(noise, ',')
                
            bad_words = [w.strip() for w in clean_res.split(',')]
            bad_words = [w for w in bad_words if w]
            
            major_allergens = ['牛', '豬', '雞', '羊', '海鮮', '魚', '蝦', '蟹', '堅果', '花生', '起司', '豆']
            for ma in major_allergens:
                if ma in restrictions and ma not in bad_words:
                    bad_words.append(ma)
            
            safe_lunch_pool = [d for d in base_lunch_pool if not any(bw in d['name'] or bw in d.get('ingredients', '') for bw in bad_words)]
            safe_dinner_pool = [d for d in base_dinner_pool if not any(bw in d['name'] or bw in d.get('ingredients', '') for bw in bad_words)]
            
            lunch_pool = safe_lunch_pool if safe_lunch_pool else base_lunch_pool
            dinner_pool = safe_dinner_pool if safe_dinner_pool else base_dinner_pool
        else:
            lunch_pool = base_lunch_pool
            dinner_pool = base_dinner_pool
        
        schedule_lines, total_price, active_days = [], 0, set()
        schedule_sheet_rows = [["週期與星期", "午餐安排", "晚餐安排", "熱量剩餘 / 蛋白質需補", "列印狀態"]]
        
        plan_requests = []
        week_dict = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}
        
        # 1. 抓取表單中的關鍵資訊
        # 表單有四個分週欄位，全部合併成一串（week_tracker 自動分週）
        _w1 = get_val("第一週") or ""
        _w2 = get_val("第二週") or ""
        _w3 = get_val("第三週") or ""
        _w4 = get_val("第四週") or ""
        _weeks_combined = [d.strip() for w in [_w1, _w2, _w3, _w4] for d in w.split(',') if d.strip()]
        date_str = ','.join(_weeks_combined) if _weeks_combined else ""
        print(f"📅 [DEBUG] 四週取餐日期合併：{date_str}")
        # Fallback: 單週表單
        if not date_str:
            date_str = get_val("取餐") or get_val("勾選")
            if not date_str:
                _raw_ds = get_val("日期")
                if _raw_ds and any(c in _raw_ds for c in ["週", "星期"]):
                    date_str = _raw_ds
        user_restrictions = restrictions.lower() # 顧客禁忌 (小寫化方便比對)
        
        # 2. 抓取顧客喜好標籤
        pref_staple = get_val("您的主食偏好是？(可複選)") or get_val("主食偏好") or ""
        pref_protein = get_val("您最喜歡的蛋白質是") or get_val("蛋白質") or ""
        
        # 🔥 定義真正喜歡的關鍵字 (解決「沒有飯」卻抓到「飯」的 Bug)
        liked_staples = []
        if "飯食" in pref_staple: liked_staples.append("飯")   # 修正：原本錯寫成「飯食派」
        if "原型" in pref_staple: liked_staples.extend(["地瓜", "南瓜", "馬鈴薯", "原型"])  # 加入「原型」本身
        if "低碳" in pref_staple: liked_staples.extend(["低碳", "菜"])
        if "麵" in pref_staple: liked_staples.append("麵")
        if "沙拉" in pref_staple: liked_staples.append("沙拉")

        liked_proteins = []
        if any(kw in pref_protein for kw in ["素食", "豆腐", "鷹嘴豆"]):
            liked_proteins.extend(["素", "豆腐", "鷹嘴豆", "鮮蔬"])
        if "雞" in pref_protein: liked_proteins.append("雞")
        if "豬" in pref_protein: liked_proteins.append("豬")
        if "牛" in pref_protein: liked_proteins.append("牛")
        if any(kw in pref_protein for kw in ["海鮮", "魚", "鱸魚", "鮭魚"]):
            liked_proteins.extend(["海鮮", "魚", "鱸魚", "鮭魚"])
        
        # 3. 建立「絕對安全菜單池」 (先過濾掉禁忌，且只挑主餐)
        safe_menu = []
        # 🔥 修正：說「不要海鮮」時，擴展過濾所有魚蝦蟹相關關鍵字
        seafood_sub_words = ["魚", "蝦", "蟹", "花枝", "透抽", "章魚", "牡蠣", "鮭", "鱸", "鮪", "鯖"]
        for dish in MAIN_DISHES:
            if dish.get('category') != 'main':
                continue
            # 只允許可配餐的六大主餐類（便當/食蔬/低碳/沙拉/番茄麵/青蔬麵）
            if not any(kw in dish['name'] for kw in MEAL_PLAN_KEYWORDS):
                continue
            dish_name = dish['name'].lower()
            is_safe = True
            forbidden_keywords = ["牛", "豬", "雞", "魚", "海鮮", "蝦", "蟹"]
            for word in forbidden_keywords:
                if word in user_restrictions and word in dish_name:
                    is_safe = False
                    break
            # 特殊處理：用戶寫「海鮮」禁忌時，同步過濾菜名含魚蝦蟹字樣的餐點
            if is_safe and "海鮮" in user_restrictions:
                if any(sw in dish_name for sw in seafood_sub_words):
                    is_safe = False
            if is_safe:
                safe_menu.append(dish)

        # 🔥 Phase 3: 計算起始日、訓練週期、4 週課表（移至此處供配餐使用）
        today_start = tw_today()
        days_ahead_start = 0 - today_start.weekday()
        if days_ahead_start <= 0:
            days_ahead_start += 7
        start_date = today_start + timedelta(days=days_ahead_start)

        phase_name, weeks_to_race_val = calculate_training_phase(race_date)
        # ⚠️ generate_4week_plan 改為輕量呼叫：若 API 超時或失敗，使用 fallback 碳水邏輯
        # 完整的 4 週課表寫入由後續排程或 run_weekly_coach 負責
        four_week_plan = {}

        # 4. 解析取餐日期並進行「超級紅娘配對」 (🔥 融合終極穩定版 + 主食黑名單)
        plan_requests = []
        total_price = 0
        active_days_list = []  # ⚠️ 安全預設值，避免 date_str 為空時 NameError

        if date_str:
            week_dict = {
                "星期一": 1, "週一": 1, "星期二": 2, "週二": 2, 
                "星期三": 3, "週三": 3, "星期四": 4, "週四": 4, 
                "星期五": 5, "週五": 5, "星期六": 6, "週六": 6, "星期日": 7, "週日": 7
            }
            days = [d.strip() for d in date_str.split(',')]
            active_days_list = [] 
            week_tracker = {1:0, 2:0, 3:0, 4:0, 5:0, 6:0, 7:0}
            
            for d in days:
                d_num = next((num for zh, num in week_dict.items() if zh in d), 99)
                if d_num != 99:
                    active_days_list.append(d)
                    week_tracker[d_num] += 1
                    w_num = week_tracker[d_num]
                    
                    # 🔥 終極主食地雷過濾系統 (漏掉的就是這裡！)
                    unliked_staples = []
                    if "都不挑食" not in pref_staple:
                        if "沙拉" not in pref_staple: unliked_staples.append("沙拉")
                        if "麵" not in pref_staple: unliked_staples.extend(["麵", "義大利麵", "烏龍", "筆管"])
                        if "飯" not in pref_staple: unliked_staples.extend(["飯", "燉飯", "紫米", "糙米"])
                    
                    # 🔥 Phase 3: 依 4 週課表強度決定碳水 pool（含 fallback）
                    target_date_check = start_date + timedelta(days=(w_num-1)*7 + (d_num-1))
                    actual_date_check = target_date_check.strftime("%Y/%m/%d")

                    if four_week_plan and actual_date_check in four_week_plan:
                        # 優先用 AI 生成的 4 週課表強度
                        day_intensity = four_week_plan[actual_date_check].get("intensity", "LOW")
                        is_high_carb = day_intensity in ("HIGH", "MED")
                    else:
                        # Fallback: 根據 training_freq 與賽事倒數判斷
                        _train_days = [t.strip() for t in training_freq.split(',') if t.strip()]
                        _long_days  = [t.strip() for t in long_train_day.split(',') if t.strip()]
                        _race_override = False
                        if race_date:
                            try:
                                _race_dt = datetime.strptime(race_date, "%Y/%m/%d").replace(tzinfo=TW_TZ)
                                _t_aware = datetime(target_date_check.year, target_date_check.month, target_date_check.day, tzinfo=TW_TZ)
                                if 0 <= (_race_dt - _t_aware).days <= 7:
                                    _race_override = True
                            except Exception:
                                pass
                        is_high_carb = _race_override or any(lt in d for lt in _long_days) or any(td in d for td in _train_days)

                    if is_high_carb:
                        carb_pool = [dish for dish in safe_menu if dish.get('carb_type') == '高碳']
                        if not carb_pool:
                            carb_pool = safe_menu
                    else:
                        carb_pool = [dish for dish in safe_menu if dish.get('carb_type') == '低碳']
                        if not carb_pool:
                            carb_pool = safe_menu

                    matches = []
                    staple_only_matches = []
                    protein_only_matches = []
                    for dish in carb_pool:
                        d_text = (dish['name'] + dish.get('ingredients', '')).lower()
                        
                        # 1. 踩到主食地雷？直接淘汰！
                        if any(us in d_text for us in unliked_staples):
                            continue

                        staple_ok = ("都不挑食" in pref_staple or not liked_staples or any(ls in d_text for ls in liked_staples))
                        protein_ok = (not liked_proteins or any(lp in d_text for lp in liked_proteins))

                        if staple_ok:
                            staple_only_matches.append(dish)
                        if protein_ok:
                            protein_only_matches.append(dish)
                        if staple_ok and protein_ok:
                            matches.append(dish)
                            
                    # 優先：主食＋蛋白質都命中 → 只命中主食 → 只命中蛋白 → 合法碳水池 → safe_menu
                    if len(matches) >= 2:
                        pool = matches
                    elif len(staple_only_matches) >= 2:
                        pool = staple_only_matches
                    elif len(protein_only_matches) >= 2:
                        pool = protein_only_matches
                    else:
                        pool = [dish for dish in carb_pool if not any(us in (dish['name'] + dish.get('ingredients', '')).lower() for us in unliked_staples)]
                        if len(pool) < 2:
                            pool = safe_menu 
                    
                    # 隨機抽 2 道菜
                    if len(pool) >= 2:
                        daily_pick = random.sample(pool, 2)
                    elif len(pool) == 1:
                        daily_pick = [pool[0], pool[0]]
                    else:
                        continue 
                    
                    plan_requests.append((w_num, d_num, f"第{w_num}週", d, daily_pick[0], daily_pick[1]))
                    # 💡 累加餐點總價
                    total_price += (daily_pick[0]['price'] + daily_pick[1]['price'])

        # 排序確保顯示順序正確
        plan_requests.sort(key=lambda x: (x[0], x[1]))

        # ==========================================
        # 5. 生成預覽文字與試算表資料 (🔥 升級版：自動推算日期與雙重表單)
        # ==========================================
        schedule_text = ""
        schedule_sheet_rows = [["實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排", "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白", "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態"]]
        master_api_rows = []
        
        # 💡 起始日已在 Phase 3 區塊計算（start_date 已定義）

        for w_num, d_num, w_label, day_name, lunch, dinner in plan_requests:
            day_tdee_left = int(tdee) - lunch['cal'] - dinner['cal']
            day_p_need = int(protein) - lunch['pro'] - dinner['pro']
            daily_price = lunch['price'] + dinner['price']
            
            # 🎯 算出這餐的實際日期
            target_date = start_date + timedelta(days=(w_num-1)*7 + (d_num-1))
            actual_date_str = target_date.strftime("%Y/%m/%d")

            schedule_text += f"\n【{w_label}-{day_name}】\n☀️午：{lunch['name']} ({lunch['cal']}kcal / ${lunch['price']})\n🌙晚：{dinner['name']} ({dinner['cal']}kcal / ${dinner['price']})\n👉 當日熱量剩餘: {day_tdee_left}kcal\n👉 蛋白質需補: {day_p_need}g\n"
            
            lunch_str = f"{lunch['name']} (${lunch['price']})"
            dinner_str = f"{dinner['name']} (${dinner['price']})"
            planned_cal_total = lunch['cal'] + dinner['cal']
            planned_pro_total = lunch['pro'] + dinner['pro']
            schedule_sheet_rows.append([
                actual_date_str,
                f"{w_label}-{day_name}",
                lunch_str,
                lunch['cal'],
                lunch['pro'],
                dinner_str,
                dinner['cal'],
                dinner['pro'],
                planned_cal_total,
                planned_pro_total,
                f"剩 {day_tdee_left}kcal / 補 {day_p_need}g",
                f"${daily_price}",
                "",  
                "待列印"      
            ])

            # 🤖 寫給機器人看的總表 (1 代表有教練權限)
            workout_day = four_week_plan.get(actual_date_str, {}).get("workout", "")
            master_api_rows.append([
                actual_date_str, 
                user_id, 
                int(tdee), 
                lunch['name'], 
                dinner['name'], 
                "", 
                is_coaching_enabled, 
                goal,          
                sport_type,    
                workout_day,         # 🔥 當日課表 (對應第10欄 Plan_Week)
                "",            
                "",            
                training_freq,       
                normal_train_time,   
                long_train_day,      
                run_pace,            # 🌟 必須確保這裡有放進去！(對應第16欄)
                bike_ftp,            # 🌟 必須確保這裡有放進去！(對應第17欄)
                swim_pace,           # 🌟 必須確保這裡有放進去！(對應第18欄)
                user_level,          # 🔥 碳循環等級 (對應第19欄)
                race_date,           # 🔥 目標賽事日期 (對應第20欄)
                is_carb_cycling_enabled
            ])

        # 付款 gate：表單送出後先只建立 pending 訂單，不寫正式 health_profile / Master_API_View / 個人分頁。
        today_str_for_sheet = tw_now().strftime("%Y%m%d")
        safe_name = f"{name}_{user_id[-4:]}_{today_str_for_sheet}"
        delivery_fee_per_trip = int(delivery_info.get("delivery_fee", 0) or 0)
        delivery_days_count = len(active_days_list)
        delivery_total_fee = delivery_fee_per_trip * delivery_days_count if is_delivery else 0
        total_with_delivery = int(total_price) + delivery_total_fee
        line_display_name = get_line_display_name_safe(user_id)
        form_snapshot = {
            "user_id": user_id, "line_display_name": line_display_name,
            "name": name, "goal": goal, "restrictions": restrictions,
            "pickup_method": pickup_method, "address": address, "is_delivery": is_delivery,
            "delivery_info": delivery_info, "delivery_fee_per_trip": delivery_fee_per_trip,
            "delivery_days_count": delivery_days_count, "delivery_total_fee": delivery_total_fee,
            "total_with_delivery": total_with_delivery, "weight": weight, "height": height,
            "age": age, "gender": gender, "activity": activity, "tdee": int(tdee),
            "protein": float(protein), "pref_staple": pref_staple, "pref_protein": pref_protein,
            "schedule_text": schedule_text, "schedule_sheet_rows": schedule_sheet_rows,
            "master_api_rows": master_api_rows, "active_days_list": active_days_list,
            "total_price": int(total_price), "safe_name": safe_name,
            "is_coaching_enabled": is_coaching_enabled, "is_carb_cycling_enabled": is_carb_cycling_enabled,
            "user_level": user_level, "race_date": race_date, "sport_type": sport_type,
            "training_freq": training_freq, "normal_train_time": normal_train_time,
            "long_train_day": long_train_day, "run_pace": run_pace, "bike_ftp": bike_ftp,
            "swim_pace": swim_pace, "start_date": start_date.isoformat(),
            "phase_name": phase_name, "weeks_to_race": weeks_to_race_val,
            "created_at": tw_now().strftime("%Y-%m-%d %H:%M:%S"),
            "raw_form_data": data,
        }
        order_id = create_pending_subscription_form_order(form_snapshot)

        pickup_line = f"\n📦 取餐方式：{pickup_method}" if pickup_method else ""
        address_line = f"\n📍 外送地址：{address}" if (is_delivery and address) else ""
        delivery_line = f"\n🛵 單次外送運費：${delivery_fee_per_trip}（{delivery_info.get('distance_text', '未計算')}）" if (is_delivery and address) else ""
        delivery_days_line = f"\n📦 本期配送天數：{delivery_days_count} 天" if is_delivery else ""
        delivery_total_line = f"\n🚚 四週外送費：${delivery_total_fee}" if is_delivery else ""
        self_pickup_line = "\n🛍️ 本期為自取，不產生外送費。" if (pickup_method and not is_delivery) else ""
        push_msg = (
            f"🎉 {name}，包月資料已收到！\n\n"
            f"訂單編號：#{order_id}\n"
            "AI 營養師已先為您完成初步精算，但尚未正式開通：\n"
            f"🔥 TDEE: {int(tdee)} kcal\n"
            f"🥩 蛋白質目標: {int(protein)} g\n"
            f"💰 排餐金額: ${total_price}{pickup_line}{address_line}{delivery_line}{delivery_days_line}{delivery_total_line}{self_pickup_line}\n"
            f"🧾 本期預估總金額：${total_with_delivery}\n\n"
            "📌 下一步流程\n"
            "1️⃣ 客服確認餐數、取餐日期、外送費與最終金額\n"
            "2️⃣ 確認無誤後提供付款資訊\n"
            "3️⃣ 付款完成後，客服會正式開通，不需要您再填一次表單\n"
            "4️⃣ 開通後即可使用專屬菜單與 AI 營養管理\n\n"
            "🍽️ 想先檢查排餐內容的話，可以直接輸入「查看菜單」，確認有沒有想更換或需要調整的餐點。"
        )
        line_bot_api.push_message(
            user_id,
            TextSendMessage(
                text=push_msg,
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="查看菜單", text="查看菜單")),
                    QuickReplyButton(action=MessageAction(label="找客服調整", text="找客服")),
                ])
            )
        )
        notify_admin_pending_subscription_form(order_id, form_snapshot)
        return {"status": "pending", "order_id": order_id}

        # 更新 SQLite
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO health_profile (
                user_id, name, tdee, protein, goal, restrictions, summary_text, active_days,
                today_extra_cal, today_date, sheet_name, is_coaching_enabled, is_carb_cycling_enabled,
                ai_silenced_until, user_level, race_date, address, distance_text, distance_meters,
                delivery_fee, delivery_zone, route_group, delivery_note
            ) VALUES (?,?,?,?,?,?,?,?,0,'',?,?,?,?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, name, int(tdee), protein, goal, restrictions, schedule_text, ",".join(active_days_list),
            safe_name, is_coaching_enabled, is_carb_cycling_enabled, '', user_level, race_date,
            address, delivery_info.get("distance_text", ""), delivery_info.get("distance_meters", 0),
            delivery_info.get("delivery_fee", 0), delivery_info.get("delivery_zone", ""),
            delivery_info.get("route_group", ""), delivery_info.get("carpool_hint", "")
        ))
        conn.commit()
        # 同步顧客清單：從 usage 讀剩餘餐數與狀態
        _u = conn.execute("SELECT status, remaining_meals, expiry_date FROM usage WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        if _u:
            sync_customer_sheet(user_id, name, _u[0], _u[1], _u[2], int(tdee))

        # ==========================================
        # 6. 寫入 Google 試算表 (包含個人分頁與機器人總表)
        # ==========================================
        if gc:
            try:
                print(f"📊 [DEBUG] 開始寫入 Google Sheet，共 {len(master_api_rows)} 筆資料")
                sheet = gc.open_by_url(SHEET_URL)
                
                # (1) 寫入歷史總表
                main_sheet = sheet.sheet1
                now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
                main_sheet.append_row([now_str, name, goal, int(tdee), int(protein), restrictions, total_price, ",".join(active_days_list), schedule_text])
                
                # (2) 為客戶建立專屬分頁
                try:
                    try: user_sheet = sheet.add_worksheet(title=safe_name, rows="1000", cols="20")
                    except:
                        user_sheet = sheet.worksheet(safe_name)
                        user_sheet.clear()
                        
                    profile_data = [["【VIP 客戶檔案】", f"姓名: {name}", f"目前體重: {weight} kg", f"目標: {goal}", f"TDEE: {int(tdee)} kcal", f"蛋白質: {int(protein)} g", f"禁忌: {restrictions}", f"喜好: {pref_staple} + {pref_protein}", f"💰 排餐總額: ${total_price}"], [""]]
                    menu_title = [["【專屬排餐計畫 (第1週~第4週)】"]]
                    tracking_headers = [[""], ["================================================================="], ["【日常飲食與動態追蹤】"], ["紀錄時間", "紀錄類型", "客人傳送內容", "數值變化(kcal)"]]
                    
                    user_sheet.append_rows(profile_data + menu_title + schedule_sheet_rows + tracking_headers)
                except Exception: pass

                # 🔥 (3) 同步將資料塞進 Master_API_View (Phase 3: 先刪舊行，再寫 28 天)
                try:
                    try:
                        api_sheet = sheet.worksheet("Master_API_View")
                    except gspread.exceptions.WorksheetNotFound:
                        api_sheet = sheet.add_worksheet(title="Master_API_View", rows="2000", cols="20")
                        api_sheet.append_row(["Date", "User_ID", "TDEE", "Lunch_Item", "Dinner_Item", "Tomorrow_Training", "Is_Coaching_Enabled", "Plan_Type", "Sport_Type", "Plan_Week", "Intervals_ID", "Intervals_API_Key", "Training_Freq", "Normal_Train_Time", "Long_Train_Day", "Run_Pace", "Bike_FTP", "Swim_Pace", "User_Level", "Race_Date", "Is_Carb_Cycling_Enabled"])

                    # 🗑️ 刪掉此用戶的舊行（由下往上，避免 index 偏移）
                    try:
                        all_vals = api_sheet.get_all_values()
                        rows_to_del = [
                            i + 1  # 1-indexed sheet row (i=0 是 header)
                            for i, row in enumerate(all_vals)
                            if i > 0 and len(row) > 1 and row[1] == user_id
                        ]
                        for rn in sorted(rows_to_del, reverse=True):
                            api_sheet.delete_rows(rn)
                        if rows_to_del:
                            print(f"✅ 已刪除 {len(rows_to_del)} 行舊資料")
                    except Exception as _del_e:
                        print(f"⚠️ 刪除舊行失敗: {_del_e}")

                    # 📅 補上非取餐日的訓練行（來自 4 週課表）
                    meal_dates = {row[0] for row in master_api_rows}
                    extra_training_rows = []
                    for _dstr in sorted(four_week_plan.keys()):
                        if _dstr not in meal_dates:
                            _pi = four_week_plan[_dstr]
                            extra_training_rows.append([
                                _dstr, user_id, int(tdee), "無", "無", "", is_coaching_enabled,
                                goal, sport_type, _pi.get("workout", ""), "", "",
                                training_freq, normal_train_time, long_train_day,
                                run_pace, bike_ftp, swim_pace, user_level, race_date, is_carb_cycling_enabled
                            ])

                    # 合併排序後一次寫入
                    all_new_rows = sorted(master_api_rows + extra_training_rows, key=lambda r: str(r[0]))
                    if all_new_rows:
                        api_sheet.append_rows(all_new_rows)
                    print(f"✅ 成功將 {len(all_new_rows)} 行寫入 Master_API_View！")

                    # 🔥 背景生成 4 週訓練課表 + 碳循環菜單重新分配（方案 C）
                    if is_coaching_enabled:
                        background_tasks.add_task(update_4week_plan_background, user_id, start_date, {
                            "sport_type": sport_type,
                            "training_freq": training_freq,
                            "long_train_day": long_train_day,
                            "run_pace": run_pace,
                            "bike_ftp": bike_ftp,
                            "swim_pace": swim_pace,
                            "phase_name": phase_name,
                            "weeks_to_race": weeks_to_race_val,
                            "restrictions": restrictions or "",
                            "is_carb_cycling_enabled": is_carb_cycling_enabled,
                        })
                except Exception as e:
                    print(f"⚠️ 寫入 Master_API_View 失敗: {e}")
                    
            except Exception: pass

        # 最後推播訊息給客人
        # 👉【修改】回覆客人的訊息中，補上本次排餐總額！
        delivery_fee_per_trip = int(delivery_info.get("delivery_fee", 0) or 0)
        delivery_days_count = len(active_days_list)
        delivery_total_fee = delivery_fee_per_trip * delivery_days_count if is_delivery else 0
        total_with_delivery = int(total_price) + delivery_total_fee
        pickup_line = f"\n📦 取餐方式：{pickup_method}" if pickup_method else ""
        address_line = f"\n📍 外送地址：{address}" if (is_delivery and address) else ""
        delivery_line = f"\n🛵 單次外送運費：${delivery_fee_per_trip}（{delivery_info.get('distance_text', '未計算')}）" if (is_delivery and address) else ""
        delivery_days_line = f"\n📦 本期配送天數：{delivery_days_count} 天" if is_delivery else ""
        delivery_total_line = f"\n🚚 四週外送費：${delivery_total_fee}" if is_delivery else ""
        self_pickup_line = "\n🛍️ 本期為自取，不產生外送費。" if (pickup_method and not is_delivery) else ""
        push_msg = (
            f"🎉 {name}，包月資料已收到！\n\n"
            "AI 營養師已先為您完成初步精算：\n"
            f"🔥 TDEE: {int(tdee)} kcal\n"
            f"🥩 蛋白質目標: {int(protein)} g\n"
            f"💰 排餐金額: ${total_price}{pickup_line}{address_line}{delivery_line}{delivery_days_line}{delivery_total_line}{self_pickup_line}\n"
            f"🧾 本期預估總金額：${total_with_delivery}\n\n"
            "📌 下一步流程\n"
            "1️⃣ 客服確認餐數、取餐日期、外送費與最終金額\n"
            "2️⃣ 確認無誤後提供付款資訊\n"
            "3️⃣ 付款完成後，客服會傳送 VIP 開通碼給您\n"
            "4️⃣ 開通後即可使用專屬菜單與 AI 營養管理\n\n"
            "您也可以先點選選單的『查看菜單』查看初步排餐。"
        )
        line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
        admin_form_msg = (
            f"📝【包月表單已完成】\n"
            f"顧客：{name or user_id[:8]}\n"
            f"UID：{user_id}\n"
            f"取餐方式：{pickup_method or '未填'}\n"
            f"外送地址：{address or '未提供'}\n"
            f"單次外送費：${delivery_fee_per_trip}\n"
            f"配送天數：{delivery_days_count if is_delivery else '自取'}\n"
            f"排餐金額：${total_price}\n"
            f"本期預估總金額：${total_with_delivery}\n\n"
            "下一步：請客服確認金額與付款資訊；付款後再開通 VIP。"
        )
        try:
            admin_notify_uid = get_admin_notify_uid()
            line_bot_api.push_message(admin_notify_uid, TextSendMessage(text=admin_form_msg))
            print(f"✅ 已推播包月表單完成通知給管理員：{admin_notify_uid}")
        except Exception as _admin_push_e:
            print(f"⚠️ 推播包月表單完成通知給管理員失敗: {_admin_push_e}")
        return {"status": "success"}

    except Exception as e: 
        print(f"💥 [表單崩潰致命錯誤]: {str(e)}")
        return {"status": "error", "msg": str(e)}
# ==========================================
# 🔥 滿意度問卷接收器 (自動發放不重複點數)
# ==========================================
@app.post("/survey-data")
async def receive_survey_data(request: Request):
    try:
        data = await request.json()
        print(f"📝 [問卷測試] 收到問卷資料：{data}")
        
        # 抓取表單裡的 UID
        user_id = ""
        for k, v in data.items():
            if "UID" in k.upper():
                user_id = str(v).strip()
                break
                
        if not user_id or user_id == "UID_REPLACE_ME":
            return {"status": "ignored", "msg": "無效的 UID"}

        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        
        # 1. 檢查這個人是不是已經領過點數了？(防貪小便宜)
        c.execute("SELECT claim_date FROM survey_records WHERE user_id=?", (user_id,))
        if c.fetchone():
            conn.close()
            # 已經領過，不再發網址，但可以回個感謝訊息
            try: line_bot_api.push_message(user_id, TextSendMessage(text="❤️ 感謝您再次填寫問卷！您之前已經領取過集點卡點數囉，一日樂食祝您有美好的一天！"))
            except: pass
            return {"status": "already_claimed"}

        # 2. 從保險箱抽出一張「還沒被使用」的點數網址
        c.execute("SELECT link FROM reward_links WHERE is_used=0 LIMIT 1")
        row = c.fetchone()
        
        if row:
            reward_link = row[0]
            # 標記為已使用，並記錄這個人已經領過
            c.execute("UPDATE reward_links SET is_used=1 WHERE link=?", (reward_link,))
            c.execute("INSERT INTO survey_records (user_id, claim_date) VALUES (?, ?)", (user_id, tw_today().isoformat()))
            conn.commit()
            
            # 3. 把專屬點數網址私訊給客人
            push_msg = f"🎉 感謝您的寶貴回饋！\n\n這是答應您的專屬獎勵，請點擊下方連結領取【一日樂食集點卡 1 點】👇\n\n{reward_link}\n\n(⚠️ 注意：此連結為專屬一次性連結，點擊後即失效，請勿轉發給他人喔！)"
            line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
        else:
            # 點數發光了，通知老闆！
            c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
            admin_row = c.fetchone()
            if admin_row:
                line_bot_api.push_message(admin_row[0], TextSendMessage(text="🚨 老闆緊急通知：填問卷送點數的「點數網址」已經被抽光啦！請盡快上後台產生新的網址並用 #上傳點數 補貨！"))
        
        conn.close()
        return {"status": "success"}
    except Exception as e:
        print(f"⚠️ 問卷處理錯誤: {e}")
        return {"status": "error"}
def find_meal_slot_in_user_sheet(sheet, target_date: str, meal_type: str):
    records = sheet.get_all_values()
    meal_col = 2 if meal_type == "午餐" else 5
    for i, row in enumerate(records):
        if len(row) > 1 and target_date in row[1] and "週" in row[1]:
            current_value = row[meal_col] if len(row) > meal_col else ""
            printed = (len(row) > 13 and row[13] == "已列印")
            return {
                "row_idx": i,
                "meal_col": meal_col,
                "value": current_value,
                "printed": printed,
                "records": records,
            }
    return None


def parse_defer_command(msg: str):
    m = re.match(r'^#延餐\s*(\d{1,2}/\d{1,2})\s*(午餐|晚餐)\s*->\s*(\d{1,2}/\d{1,2})\s*(午餐|晚餐)\s*$', str(msg).strip())
    if not m:
        return None
    return {
        "original_date": m.group(1),
        "original_meal_type": m.group(2),
        "target_date": m.group(3),
        "target_meal_type": m.group(4),
    }


def create_deferred_meal_request(user_id, customer_name, original_date, original_meal_type, target_date, target_meal_type, is_cross_period, has_conflict, note=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    created_at = tw_now().isoformat()
    c.execute("""
        INSERT INTO deferred_meals (
            user_id, customer_name, original_date, original_meal_type,
            target_date, target_meal_type, is_cross_period, has_conflict,
            status, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (
        user_id, customer_name, original_date, original_meal_type,
        target_date, target_meal_type, int(is_cross_period), int(has_conflict),
        note, created_at
    ))
    request_id = c.lastrowid
    conn.commit()
    conn.close()
    return request_id


def list_pending_deferred_meals(limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, customer_name, original_date, original_meal_type, target_date, target_meal_type, is_cross_period, has_conflict, note
        FROM deferred_meals
        WHERE status='pending'
        ORDER BY id ASC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


def execute_deferred_meal_move(user_id, original_date, original_meal_type, target_date, target_meal_type):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT sheet_name, summary_text FROM health_profile WHERE user_id=?", (user_id,))
    res = c.fetchone()
    if not res or not res[0]:
        conn.close()
        return False, "❌ 找不到您的專屬菜單檔案。"

    sheet_name, old_summary = res[0], res[1] or ""
    try:
        sheet = gc.open_by_url(SHEET_URL).worksheet(sheet_name)
        src = find_meal_slot_in_user_sheet(sheet, original_date, original_meal_type)
        dst = find_meal_slot_in_user_sheet(sheet, target_date, target_meal_type)

        if not src:
            conn.close()
            return False, f"❌ 找不到原餐：{original_date} {original_meal_type}"
        if not dst:
            conn.close()
            return False, f"❌ 找不到目標日期：{target_date} {target_meal_type}"
        if src["printed"] or dst["printed"]:
            conn.close()
            return False, "⚠️ 餐點已出單列印，無法延餐。"
        if not src["value"] or src["value"] in ["無", "尚未安排"]:
            conn.close()
            return False, f"❌ 原餐 {original_date} {original_meal_type} 沒有可延的餐點。"
        if dst["value"] not in ["", "無", "尚未安排"]:
            conn.close()
            return False, f"⚠️ 目標日 {target_date} {target_meal_type} 已有餐點，請人工處理。"

        meal_name = src["value"]
        sheet.update_cell(dst["row_idx"] + 1, dst["meal_col"] + 1, meal_name)
        sheet.update_cell(src["row_idx"] + 1, src["meal_col"] + 1, "無")

        timestamp = tw_now().strftime("%m/%d %H:%M")
        new_summary = old_summary + f"\n⏸ 系統紀錄：{timestamp} 將 {original_date}{original_meal_type} 延至 {target_date}{target_meal_type}。"
        c.execute("UPDATE health_profile SET summary_text=? WHERE user_id=?", (new_summary, user_id))
        conn.commit()
        conn.close()
        return True, f"✅ 已將 {original_date}{original_meal_type} 延至 {target_date}{target_meal_type}"
    except Exception as e:
        conn.close()
        print(f"⚠️ 延餐錯誤: {e}")
        return False, "⚠️ 延餐失敗，請聯絡客服。"


def approve_deferred_meal_request(request_id, admin_uid=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, customer_name, original_date, original_meal_type, target_date, target_meal_type, status
        FROM deferred_meals WHERE id=?
    """, (request_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "❌ 找不到這筆延餐申請。"

    user_id, customer_name, original_date, original_meal_type, target_date, target_meal_type, status = row
    if status != "pending":
        conn.close()
        return "⚠️ 這筆延餐申請已不是待審核狀態。"

    ok, result_msg = execute_deferred_meal_move(user_id, original_date, original_meal_type, target_date, target_meal_type)
    if ok:
        c.execute("""
            UPDATE deferred_meals
            SET status='completed', approved_at=?, approved_by=?
            WHERE id=?
        """, (tw_now().isoformat(), admin_uid, request_id))
        conn.commit()
        conn.close()
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 您的延餐申請已核准\n{original_date} {original_meal_type} 已改至 {target_date} {target_meal_type}"))
        except Exception:
            pass
        return f"✅ 已核准延餐申請 #{request_id}\n{result_msg}"

    c.execute("UPDATE deferred_meals SET note=? WHERE id=?", (result_msg, request_id))
    conn.commit()
    conn.close()
    return f"⚠️ 延餐申請 #{request_id} 核准失敗\n{result_msg}"


def reject_deferred_meal_request(request_id, reason="", admin_uid=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, status FROM deferred_meals WHERE id=?", (request_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "❌ 找不到這筆延餐申請。"
    user_id, status = row
    if status != "pending":
        conn.close()
        return "⚠️ 這筆延餐申請已不是待審核狀態。"

    c.execute("""
        UPDATE deferred_meals
        SET status='rejected', note=?, approved_at=?, approved_by=?
        WHERE id=?
    """, (reason, tw_now().isoformat(), admin_uid, request_id))
    conn.commit()
    conn.close()
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=f"⚠️ 您的延餐申請未通過\n原因：{reason or '請聯絡客服確認'}"))
    except Exception:
        pass
    return f"✅ 已拒絕延餐申請 #{request_id}"


def execute_meal_swap(user_id, d1, m1, d2, m2):
    """處理顧客餐點互換邏輯 - 精準定位版 [cite: 2]"""
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT sheet_name, summary_text FROM health_profile WHERE user_id=?", (user_id,))
    res = c.fetchone()
    if not res or not res[0]: 
        conn.close(); return "❌ 找不到您的專屬菜單檔案。"
    sheet_name, old_summary = res[0], res[1]

    # 1. 時間鎖定邏輯 (維持不變)
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    today_str = weekdays[tw_today().weekday()]
    current_hour = tw_now().hour

    def check_lock(target_day, target_meal):
        if target_day == today_str:
            if target_meal == "午餐" and current_hour >= 8: return False
            if target_meal == "晚餐" and current_hour >= 14: return False
        return True

    if not check_lock(d1, m1) or not check_lock(d2, m2):
        conn.close(); return "⚠️ 已超過修改期限，內場已開始備餐。"

    # 2. Google Sheet 資料互換
    try:
        sheet = gc.open_by_url(SHEET_URL).worksheet(sheet_name)
        src = find_meal_slot_in_user_sheet(sheet, d1, m1)
        dst = find_meal_slot_in_user_sheet(sheet, d2, m2)

        if not src or not dst:
            conn.close(); return "❌ 找不到指定的日期。"
        if src["printed"] or dst["printed"]:
            conn.close(); return "⚠️ 您的餐點已經出單列印，無法更換！"

        meal1_name = src["value"]
        meal2_name = dst["value"]

        sheet.update_cell(src["row_idx"] + 1, src["meal_col"] + 1, meal2_name)
        sheet.update_cell(dst["row_idx"] + 1, dst["meal_col"] + 1, meal1_name)

        timestamp = tw_now().strftime("%m/%d %H:%M")
        new_summary = old_summary + f"\n🔄 系統紀錄：{timestamp} 將 {d1}{m1} 與 {d2}{m2} 互換。"
        c.execute("UPDATE health_profile SET summary_text=? WHERE user_id=?", (new_summary, user_id))
        conn.commit(); conn.close()
        return f"✅ 成功將【{d1}{m1}】與【{d2}{m2}】互換囉！"
    except Exception as e:
        conn.close(); print(f"⚠️ 換餐錯誤: {e}"); return "⚠️ 換餐失敗，請聯絡客服。"     
# ==========================================
# 5. AI 對話引擎 (🔥 終極防偷懶 + 食物記憶版)
# ==========================================

def calculate_carb_cycle(user_level, race_date_str):
    """
    根據用戶等級與賽事距離，決定今天的碳水模式
    """
    import datetime as dt  
    
    # 1. 取得現在時間 (含時區)
    today = datetime.now(ZoneInfo("Asia/Taipei"))
    carb_mode = "中碳水" # 預設值
    
    # 2. 處理賽事倒數
    days_to_race = None
    # 🌟 修改：更嚴謹地排除空字串、空白字元與 "無"
    if race_date_str and str(race_date_str).strip() not in ["", "無", "None"]:
        try:
            # 將字串轉為日期物件，並賦予時區
            race_date = datetime.strptime(race_date_str, "%Y/%m/%d").replace(tzinfo=ZoneInfo("Asia/Taipei"))
            
            # 計算時間差
            delta = race_date - today
            
            # 取得整數天數
            days_to_race = delta.days + 1  # 加 1 是為了包含今天
            
        except Exception as e:
            print(f"❌ 週期化邏輯日期轉換出錯: {e}")
            pass

    # 3. 根據 Level 判定邏輯
    level = str(user_level)
    
    if level == "1":
        carb_mode = "低碳水 (穩定燃脂模式)"
    elif level == "2":
        carb_mode = "中碳水 (規律運動模式)"
    elif level == "3":
        if days_to_race is not None:
            if 0 <= days_to_race <= 7:
                carb_mode = "高碳水 (超量補償期：備戰衝刺)"
            elif 8 <= days_to_race <= 14:
                carb_mode = "中高碳 (能量儲備期)"
            else:
                carb_mode = "中碳水 (基礎訓練期)"
        else:
            carb_mode = "中碳水 (專屬選手模式)"
            
    return carb_mode, days_to_race

# ==========================================
# 🔥 Phase 3: 訓練週期判斷 & 4 週課表生成
# ==========================================
def calculate_training_phase(race_date_str):
    """
    根據距賽週數判斷訓練週期。
    Returns: (phase_name, weeks_to_race) 或 (None, None) 若無賽事日期。
    """
    if not race_date_str or race_date_str in ("無", ""):
        return None, None
    try:
        race_dt = datetime.strptime(race_date_str, "%Y/%m/%d").replace(tzinfo=TW_TZ)
        today = tw_now()
        weeks_to_race = (race_dt - today).days / 7
        if weeks_to_race < 0:
            phase = "恢復期"
        elif weeks_to_race <= 2:
            phase = "減量期"
        elif weeks_to_race <= 6:
            phase = "巔峰期"
        elif weeks_to_race <= 12:
            phase = "進展期"
        else:
            phase = "基礎期"
        return phase, round(weeks_to_race, 1)
    except Exception as e:
        print(f"⚠️ calculate_training_phase 失敗: {e}")
        return None, None

def generate_4week_plan(start_date, user_data):
    """
    呼叫 AI 生成 4 週訓練課表（28 天）。
    Returns: dict {date_str: {"workout": str, "intensity": "HIGH"/"MED"/"LOW"}}
             失敗時回傳 {}。
    """
    dates_28 = [(start_date + timedelta(days=i)).strftime("%Y/%m/%d") for i in range(28)]
    phase_name = user_data.get("phase_name")
    weeks_to_race = user_data.get("weeks_to_race")

    if phase_name:
        phase_context = f"目前訓練週期：【{phase_name}】，距離目標賽事約 {weeks_to_race} 週。"
    else:
        phase_context = "目前無目標賽事，以一般健康維持與規律運動為主。"

    prompt = f"""你是頂尖運動教練。請根據以下顧客資料，生成未來 4 週（28 天）的完整訓練計畫。

【顧客資料】
- 運動類型：{user_data.get('sport_type', '未設定')}
- 訓練頻率（可訓練的星期）：{user_data.get('training_freq', '未設定')}
- 長訓日：{user_data.get('long_train_day', '未設定')}
- 5K 配速：{user_data.get('run_pace', '未提供')}
- 自行車 FTP：{user_data.get('bike_ftp', '未提供')}
- 游泳 CSS 配速：{user_data.get('swim_pace', '未提供')}
- {phase_context}

【28 天日期（從 {dates_28[0]} 到 {dates_28[-1]}）】
{', '.join(dates_28)}

【課表規則】
1. 只在 training_freq 指定的星期排主訓練，其餘一律寫「休息」或「主動恢復（散步/伸展）」
2. 長訓日（long_train_day）安排當週時間最長、強度最高的課表
3. intensity 只能填 HIGH、MED、LOW 三種（HIGH=間歇/閾值, MED=中強度有氧, LOW=Z2輕鬆/休息/恢復）
4. workout 要具體（含時間/距離/配速/瓦數），非訓練日寫「休息」或「主動恢復（散步/伸展）」

【輸出格式】嚴格輸出合法 JSON，不加任何說明文字或 ``` 包裝，共 28 筆：
{{
  "YYYY/MM/DD": {{"workout": "課表內容", "intensity": "HIGH"}},
  "YYYY/MM/DD": {{"workout": "休息", "intensity": "LOW"}}
}}"""

    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=2500
        )
        raw = res.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 1)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
        print(f"✅ generate_4week_plan 成功：共 {len(plan)} 天")
        return plan
    except Exception as e:
        print(f"⚠️ generate_4week_plan 失敗: {e}")
        return {}

def infer_intensity_from_workout_text(workout_text: str) -> str:
    text = (workout_text or "").strip().lower()
    if not text:
        return "LOW"

    low_keywords = ["休息", "恢復", "伸展", "散步", "z1", "recovery", "rest"]
    high_keywords = ["間歇", "衝刺", "節奏", "閾值", "tempo", "threshold", "interval", "vo2", "ftp", "測驗", "test", "race", "比賽"]
    med_keywords = ["長跑", "長騎", "耐力", "z2", "有氧", "steady", "endurance", "easy"]

    if any(k in text for k in low_keywords):
        return "LOW"
    if any(k in text for k in high_keywords):
        return "HIGH"
    if any(k in text for k in med_keywords):
        return "MED"
    return "MED"


def build_safe_main_dishes(restrictions_text: str = ""):
    restrictions_bg = (restrictions_text or "").lower()
    safe_menu_bg = []
    for dish in MAIN_DISHES:
        if dish.get("category") != "main":
            continue
        if not any(kw in dish["name"] for kw in MEAL_PLAN_KEYWORDS):
            continue
        dish_name_bg = dish['name'].lower()
        is_safe_bg = True
        for allergen in ["牛", "豬", "雞", "蛋", "奶", "海鮮", "花生"]:
            if allergen in restrictions_bg and allergen in dish_name_bg:
                is_safe_bg = False
                break
        if is_safe_bg:
            safe_menu_bg.append(dish)

    if not safe_menu_bg:
        safe_menu_bg = [d for d in MAIN_DISHES if d.get("category") == "main"]
    return safe_menu_bg


def repack_meal_plan_for_user(user_id: str):
    """
    依目前 Master_API_View 的既有課表內容，重新分配午晚餐。
    - 若碳循環開啟：依課表文字推估強度後重排高/低碳菜單
    - 若碳循環關閉：重新抽主餐，但不做高低碳切換
    """
    if not gc:
        return False, "⚠️ Google Sheet 尚未連線，暫時無法重新排餐。"

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT name, restrictions, is_carb_cycling_enabled FROM health_profile WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if not row:
            return False, "⚠️ 找不到你的健康檔案，請先重新填寫表單。"

        user_name, restrictions, is_carb_cycling_enabled = row
        safe_menu_bg = build_safe_main_dishes(restrictions)
        if not safe_menu_bg:
            return False, "⚠️ 目前沒有可用的主餐資料，無法重新排餐。"

        api_sheet = gc.open_by_url(SHEET_URL).worksheet("Master_API_View")
        all_vals = api_sheet.get_all_values()
        if len(all_vals) < 2:
            return False, "⚠️ Master_API_View 目前沒有可重排的資料。"

        header = all_vals[0]
        def _ci(name, default):
            return header.index(name) if name in header else default

        uid_col = _ci("User_ID", 1)
        date_col = _ci("Date", 0)
        lunch_col = _ci("Lunch_Item", 3)
        dinner_col = _ci("Dinner_Item", 4)
        plan_col = _ci("Plan_Week", 9)

        def col_letter(idx):
            result = ""
            idx += 1
            while idx > 0:
                idx, rem = divmod(idx - 1, 26)
                result = chr(65 + rem) + result
            return result

        updates = []
        preview_lines = []
        updated_days = 0

        high_pool = [d for d in safe_menu_bg if d.get("carb_type") == "高碳"] or safe_menu_bg
        low_pool = [d for d in safe_menu_bg if d.get("carb_type") == "低碳"] or safe_menu_bg

        for i, row_vals in enumerate(all_vals[1:], 2):
            if not (len(row_vals) > uid_col and row_vals[uid_col] == user_id):
                continue

            date_val = row_vals[date_col] if len(row_vals) > date_col else ""
            workout = row_vals[plan_col] if len(row_vals) > plan_col else ""
            intensity = infer_intensity_from_workout_text(workout)

            if is_carb_cycling_enabled:
                carb_pool_bg = high_pool if intensity in ("HIGH", "MED") else low_pool
                carb_tag = "高碳" if intensity in ("HIGH", "MED") else "低碳"
            else:
                carb_pool_bg = safe_menu_bg
                carb_tag = "固定排餐"

            if len(carb_pool_bg) >= 2:
                picks = random.sample(carb_pool_bg, 2)
            elif carb_pool_bg:
                picks = [carb_pool_bg[0], carb_pool_bg[0]]
            else:
                continue

            updates += [
                {"range": f"{col_letter(lunch_col)}{i}", "values": [[picks[0]['name']]]},
                {"range": f"{col_letter(dinner_col)}{i}", "values": [[picks[1]['name']]]},
            ]
            updated_days += 1

            if len(preview_lines) < 7:
                preview_lines.append(
                    f"{date_val}｜{carb_tag}\n午：{picks[0]['name']}\n晚：{picks[1]['name']}"
                )

        if not updates:
            return False, "⚠️ 找不到可重排的排餐資料。"

        api_sheet.batch_update(updates)
        sync_user_sheet_from_master(user_id)

        summary = (
            f"✅ 已重新排餐，共更新 {updated_days} 天\n"
            + "══════════════════════\n"
            + "\n──────────────────\n".join(preview_lines)
        )
        if updated_days > len(preview_lines):
            summary += f"\n... 其餘 {updated_days - len(preview_lines)} 天也已同步更新"
        summary += "\n══════════════════════"
        if is_carb_cycling_enabled:
            summary += "\n🍚 已依現有課表強度重新套用碳循環。"
        else:
            summary += "\n🍱 碳循環目前關閉，本次只重新抽換主餐。"

        return True, summary
    except Exception as e:
        print(f"⚠️ repack_meal_plan_for_user 失敗: {e}")
        return False, f"⚠️ 重新排餐失敗：{e}"
    finally:
        if conn:
            conn.close()


def extract_user_sheet_suffix(user_id: str) -> str:
    user_id = str(user_id or "").strip()
    return user_id[-4:] if len(user_id) >= 4 else user_id


def parse_sheet_title_date(sheet_title: str):
    if not sheet_title:
        return None
    m = re.search(r'(20\d{6})$', str(sheet_title).strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d")
    except Exception:
        return None


def pick_latest_sheet_title(sheet_titles):
    def _sort_key(title):
        parsed = parse_sheet_title_date(title)
        return (parsed or datetime.min, str(title))
    return sorted(sheet_titles, key=_sort_key, reverse=True)[0] if sheet_titles else None


def find_candidate_user_sheets(book, user_id: str, user_name: str = ""):
    suffix = extract_user_sheet_suffix(user_id)
    normalized_name = str(user_name or "").strip().lower()
    candidates = []

    for ws in book.worksheets():
        title = ws.title.strip()
        title_lower = title.lower()

        suffix_match = bool(suffix and f"_{suffix}_" in title)
        name_match = bool(normalized_name and title_lower.startswith(f"{normalized_name}_"))

        if suffix_match or name_match:
            candidates.append(title)

    exact_candidates = [t for t in candidates if suffix and f"_{suffix}_" in t]
    return exact_candidates if exact_candidates else candidates


def resolve_user_sheet_name(book, user_id: str, user_name: str = "", current_sheet_name: str = ""):
    worksheet_titles = {ws.title for ws in book.worksheets()}
    current_sheet_name = str(current_sheet_name or "").strip()

    if current_sheet_name and current_sheet_name in worksheet_titles:
        return current_sheet_name, "current"

    candidates = find_candidate_user_sheets(book, user_id, user_name)
    if candidates:
        return pick_latest_sheet_title(candidates), "candidate"

    return None, "missing"


def update_health_profile_sheet_name(user_id: str, sheet_name: str):
    if not sheet_name:
        return
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE health_profile SET sheet_name=? WHERE user_id=?", (sheet_name, user_id))
        conn.commit()
    except Exception as e:
        print(f"⚠️ 更新 sheet_name 失敗: {e}")
    finally:
        if conn:
            conn.close()


def get_or_create_user_sheet(book, user_id: str, user_name: str, current_sheet_name: str = ""):
    resolved_sheet_name, source = resolve_user_sheet_name(book, user_id, user_name, current_sheet_name)

    if resolved_sheet_name:
        if resolved_sheet_name != (current_sheet_name or ""):
            update_health_profile_sheet_name(user_id, resolved_sheet_name)
        return book.worksheet(resolved_sheet_name), resolved_sheet_name, source

    sync_user_sheet_from_master(user_id)
    resolved_sheet_name, source = resolve_user_sheet_name(book, user_id, user_name, current_sheet_name)
    if resolved_sheet_name:
        if resolved_sheet_name != (current_sheet_name or ""):
            update_health_profile_sheet_name(user_id, resolved_sheet_name)
        return book.worksheet(resolved_sheet_name), resolved_sheet_name, f"rebuilt_{source}"

    return None, None, "missing"


def sync_user_sheet_from_master(user_id: str):
    """
    以 Master_API_View 作為單一真實來源，重建個人分頁中的排餐區塊，
    避免第一階段初始排餐與第二階段碳循環重排後資料不一致。
    同時把 health_profile.sheet_name 視為可校正欄位，而不是唯一真相。
    """
    if not gc:
        return
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT sheet_name, name, tdee, protein, goal, restrictions FROM health_profile WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if not row:
            return
        current_sheet_name, name, tdee, protein, goal, restrictions = row

        sheet = gc.open_by_url(SHEET_URL)
        api_sheet = sheet.worksheet("Master_API_View")
        all_vals = api_sheet.get_all_values()
        if len(all_vals) < 2:
            return

        header = all_vals[0]
        def _ci(name, default):
            return header.index(name) if name in header else default

        date_col = _ci("Date", 0)
        uid_col = _ci("User_ID", 1)
        lunch_col = _ci("Lunch_Item", 3)
        dinner_col = _ci("Dinner_Item", 4)
        plan_col = _ci("Plan_Week", 9)

        rows = []
        for r in all_vals[1:]:
            if len(r) > uid_col and r[uid_col] == user_id:
                date_val = r[date_col] if len(r) > date_col else ""
                lunch = r[lunch_col] if len(r) > lunch_col else ""
                dinner = r[dinner_col] if len(r) > dinner_col else ""
                workout = r[plan_col] if len(r) > plan_col else ""
                if date_val and lunch and dinner and lunch != "無" and dinner != "無":
                    rows.append((date_val, lunch, dinner, workout))

        if not rows:
            return

        rows.sort(key=lambda x: x[0])
        weekday_map = {0:"週一", 1:"週二", 2:"週三", 3:"週四", 4:"週五", 5:"週六", 6:"週日"}
        schedule_sheet_rows = [["實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排", "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白", "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態"]]
        total_price = 0
        week_counts = {0:0,1:0,2:0,3:0,4:0,5:0,6:0}

        for date_val, lunch_name, dinner_name, workout in rows:
            try:
                d_obj = datetime.strptime(date_val, "%Y/%m/%d")
            except Exception:
                continue
            weekday = d_obj.weekday()
            week_counts[weekday] += 1
            w_num = week_counts[weekday]
            wday = weekday_map[weekday]
            lunch = next((d for d in MAIN_DISHES if d['name'] == lunch_name), None)
            dinner = next((d for d in MAIN_DISHES if d['name'] == dinner_name), None)
            if not lunch or not dinner:
                continue
            day_tdee_left = int(tdee) - lunch['cal'] - dinner['cal']
            day_p_need = int(protein) - lunch['pro'] - dinner['pro']
            daily_price = lunch['price'] + dinner['price']
            total_price += daily_price
            schedule_sheet_rows.append([
                date_val,
                f"第{w_num}週-{wday}",
                f"{lunch['name']} (${lunch['price']})",
                lunch['cal'],
                lunch['pro'],
                f"{dinner['name']} (${dinner['price']})",
                dinner['cal'],
                dinner['pro'],
                lunch['cal'] + dinner['cal'],
                lunch['pro'] + dinner['pro'],
                f"剩 {day_tdee_left}kcal / 補 {day_p_need}g",
                f"${daily_price}",
                workout or "",
                "待列印"
            ])

        resolved_sheet_name, source = resolve_user_sheet_name(sheet, user_id, name, current_sheet_name)
        final_sheet_name = resolved_sheet_name or current_sheet_name
        if not final_sheet_name:
            final_sheet_name = f"{name}_{extract_user_sheet_suffix(user_id)}_{tw_now().strftime('%Y%m%d')}"

        if final_sheet_name != (current_sheet_name or ""):
            update_health_profile_sheet_name(user_id, final_sheet_name)

        try:
            user_sheet = sheet.worksheet(final_sheet_name)
        except Exception:
            user_sheet = sheet.add_worksheet(title=final_sheet_name, rows="1000", cols="14")

        user_sheet.clear()
        profile_data = [["【VIP 客戶檔案】", f"姓名: {name}", f"User_ID: {user_id}", f"目標: {goal}", f"TDEE: {int(tdee)} kcal", f"蛋白質: {int(protein)} g", f"禁忌: {restrictions}", "", f"來源: {source}", f"💰 排餐總額: ${total_price}"], [""]]
        menu_title = [["【專屬排餐計畫 (第1週~第4週)｜已套用碳循環】"]]
        tracking_headers = [[""], ["================================================================="], ["【日常飲食與動態追蹤】"], ["紀錄時間", "紀錄類型", "客人傳送內容", "數值變化(kcal)"]]
        user_sheet.append_rows(profile_data + menu_title + schedule_sheet_rows + tracking_headers)
        print(f"✅ 已同步更新個人分頁：{final_sheet_name}")
    except Exception as e:
        print(f"⚠️ sync_user_sheet_from_master 失敗: {e}")
    finally:
        if conn:
            conn.close()


def update_4week_plan_background(user_id: str, start_date, user_data: dict):
    """
    背景任務（方案 C）：
    1. AI 生成 4 週訓練課表
    2. 依強度（HIGH/MED=高碳, LOW=低碳）重新分配午晚餐
    3. 批次更新 Master_API_View（Lunch/Dinner/Plan_Week）
    4. 推播 LINE 通知「課表＋碳循環已調整」
    """
    try:
        print(f"🔄 [背景] 開始為 {user_id} 生成 4 週訓練課表...")
        plan = generate_4week_plan(start_date, user_data)
        if not plan or not gc:
            print("⚠️ [背景] plan 為空或 gc 未連線，跳過")
            return

        # 重建安全菜單（只取主餐類 + 過濾禁忌）
        safe_menu_bg = build_safe_main_dishes(user_data.get("restrictions", ""))

        # 開啟 Master_API_View
        api_sheet = gc.open_by_url(SHEET_URL).worksheet("Master_API_View")
        all_vals = api_sheet.get_all_values()
        if len(all_vals) < 2:
            return

        header = all_vals[0]
        def _ci(name, default): return header.index(name) if name in header else default
        uid_col    = _ci("User_ID", 1)
        date_col   = _ci("Date", 0)
        lunch_col  = _ci("Lunch_Item", 3)
        dinner_col = _ci("Dinner_Item", 4)
        plan_col   = _ci("Plan_Week", 9)

        weekday_map = {0:"週一", 1:"週二", 2:"週三", 3:"週四", 4:"週五", 5:"週六", 6:"週日"}
        updates = []
        summary_lines = []
        is_carb_cycling_enabled = bool(user_data.get("is_carb_cycling_enabled", True))

        for i, row in enumerate(all_vals[1:], 2):
            if not (len(row) > uid_col and row[uid_col] == user_id):
                continue
            date_val = row[date_col] if len(row) > date_col else ""
            if date_val not in plan:
                continue

            intensity = plan[date_val].get("intensity", "LOW")
            workout   = plan[date_val].get("workout", "")

            # 依強度選碳水 pool
            is_high = intensity in ("HIGH", "MED")
            carb_tag = "高碳" if is_high else "低碳"

            # 批次更新欄位
            def col_letter(idx): return chr(ord('A') + idx)
            if is_carb_cycling_enabled:
                carb_pool_bg = [d for d in safe_menu_bg if d.get("carb_type") == carb_tag]
                if not carb_pool_bg:
                    carb_pool_bg = safe_menu_bg

                if len(carb_pool_bg) >= 2:
                    picks = random.sample(carb_pool_bg, 2)
                elif carb_pool_bg:
                    picks = [carb_pool_bg[0], carb_pool_bg[0]]
                else:
                    continue

                updates += [
                    {"range": f"{col_letter(lunch_col)}{i}",  "values": [[picks[0]['name']]]},
                    {"range": f"{col_letter(dinner_col)}{i}", "values": [[picks[1]['name']]]},
                    {"range": f"{col_letter(plan_col)}{i}",   "values": [[workout]]},
                ]
            else:
                lunch_now = row[lunch_col] if len(row) > lunch_col else ""
                dinner_now = row[dinner_col] if len(row) > dinner_col else ""
                picks = [{"name": lunch_now}, {"name": dinner_now}]
                updates += [
                    {"range": f"{col_letter(plan_col)}{i}",   "values": [[workout]]},
                ]

            # 摘要（只取前 7 天顯示）
            if len(summary_lines) < 7:
                try:
                    d_obj = datetime.strptime(date_val, "%Y/%m/%d")
                    wday = weekday_map[d_obj.weekday()]
                except:
                    wday = ""
                emoji = "🔥" if is_high else "🥗"
                summary_lines.append(
                    f"{date_val}（{wday}）{emoji}{carb_tag}\n"
                    f"  午：{picks[0]['name']}\n  晚：{picks[1]['name']}\n"
                    f"  課：{workout[:25]}"
                )

        if updates:
            api_sheet.batch_update(updates)
            print(f"✅ [背景] 已更新 {len(updates)//3} 天碳循環菜單＋課表")
            # 同步刷新客戶個人分頁，避免與 Master_API_View 不一致
            sync_user_sheet_from_master(user_id)

        # 推播 LINE 更新通知
        if summary_lines:
            updated_days = len([u for u in updates if str(u.get('range','')).startswith('J')])
            if is_carb_cycling_enabled:
                notice = (
                    "✅ 4 週訓練課表已生成！碳循環菜單已依強度調整 🎯\n"
                    "══════════════════════\n"
                    + "\n──────────────────\n".join(summary_lines)
                    + (f"\n... 共 {updated_days} 天已更新" if updated_days > 7 else "")
                    + "\n══════════════════════\n"
                    "🔥 高強度日 = 高碳補糖，🥗 低強度/休息日 = 低碳燃脂"
                )
            else:
                notice = (
                    "✅ 4 週訓練課表已生成！已依最新訓練內容更新課表 🎯\n"
                    "══════════════════════\n"
                    + "\n──────────────────\n".join(summary_lines)
                    + (f"\n... 共 {updated_days} 天已更新" if updated_days > 7 else "")
                    + "\n══════════════════════\n"
                    "🍱 本次保留原排餐，只同步更新訓練課表。"
                )
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=notice))
                print(f"✅ [背景] LINE 通知已推播")
            except Exception as _le:
                print(f"⚠️ [背景] LINE 推播失敗: {_le}")

    except Exception as e:
        import traceback
        print(f"⚠️ [背景] 4 週課表更新失敗: {e}")
        traceback.print_exc()

# ==========================================
# 📊 儀表板資料層 (Phase 1 + Phase 2 共用)
# ==========================================
def get_user_sheet_rows(sheet):
    """讀個人分頁資料，繞過 get_all_records() 的重複標題問題"""
    all_values = sheet.get_all_values()
    if not all_values:
        return []
    # 找標題行：第一個第一欄看起來像日期格式（含 /）的行的前一行，
    # 或直接找含「日期」或「實際日期」的行
    header_idx = None
    for i, row in enumerate(all_values):
        if row and any(h in str(row[0]) for h in ["日期", "實際日期", "Date"]):
            header_idx = i
            break
    if header_idx is None:
        return []
    headers = all_values[header_idx]
    result = []
    for row in all_values[header_idx + 1:]:
        if not any(cell.strip() for cell in row):
            continue  # 跳空行
        row_dict = {}
        for j, h in enumerate(headers):
            row_dict[h] = row[j] if j < len(row) else ""
        result.append(row_dict)
    return result

def _pick_first(row: dict, keys, default=""):
    for key in keys:
        val = row.get(key, "")
        if str(val).strip():
            return str(val).strip()
    return default


def upsert_frequent_food(user_id: str, meal_name: str, cal=None, pro=None):
    if not meal_name:
        return
    try:
        # ✅ 安全連線
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO frequent_foods (user_id, meal_name, last_cal, last_pro, use_count, last_used_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(user_id, meal_name) DO UPDATE SET
                    last_cal=excluded.last_cal,
                    last_pro=excluded.last_pro,
                    use_count=frequent_foods.use_count + 1,
                    last_used_at=excluded.last_used_at
            """, (user_id, meal_name, cal, pro, tw_now().isoformat()))
            conn.commit()
    except Exception:
        pass


def add_frequent_food_to_today(user_id: str, meal_name: str):
    # ✅ 安全連線
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT last_cal, last_pro FROM frequent_foods WHERE user_id=? AND meal_name=?", (user_id, meal_name))
        row = c.fetchone()
        if not row:
            return None, "找不到這筆常吃食物。"
        cal, pro = row

        c.execute("SELECT today_extra_cal, today_extra_pro, today_food_items, today_date, tdee, protein FROM health_profile WHERE user_id=?", (user_id,))
        hp = c.fetchone()
        if not hp:
            return None, "找不到你的健康資料。"

        today_extra_cal, today_extra_pro, today_food_items, today_date, tdee, protein_goal = hp
        today = tw_today().isoformat()
        if cal is None and pro is None:
            return None, "這筆常吃食物沒有營養資料，請先補充後再加入。"
        meal_hour = tw_now().hour
        meal_slot = "早餐" if meal_hour < 11 else ("午餐" if meal_hour < 15 else ("晚餐" if meal_hour < 21 else "點心"))
        daily_log = create_daily_food_log(
            conn, user_id=user_id, product_name=meal_name, meal_slot=meal_slot,
            consumed_at=tw_now().isoformat(timespec="seconds"), servings=1,
            nutrition={"calories_kcal": cal, "protein_g": pro},
            source_type="frequent_food",
        )
        _sync_health_profile_from_ledger_conn(conn, user_id, today)
        c.execute(
            "SELECT today_extra_cal,today_extra_pro,today_food_items FROM health_profile WHERE user_id=?",
            (user_id,),
        )
        projected = c.fetchone() or (0, 0, "")
        new_extra_cal, new_extra_pro, new_food_items = projected[0] or 0, projected[1] or 0, projected[2] or ""

        c.execute("""
            INSERT INTO recent_meal_logs (user_id, meal_name, base_cal, base_pro, current_cal, current_pro, meal_date, source_text, updated_at, food_log_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                meal_name=excluded.meal_name,
                base_cal=excluded.base_cal,
                base_pro=excluded.base_pro,
                current_cal=excluded.current_cal,
                current_pro=excluded.current_pro,
                meal_date=excluded.meal_date,
                source_text=excluded.source_text,
                updated_at=excluded.updated_at,
                food_log_id=excluded.food_log_id
        """, (user_id, meal_name, cal, pro, cal, pro, today, "常吃直接加入", tw_now().isoformat(), daily_log["log_id"]))
        conn.commit()

    # 💡 離開 with 區塊後再呼叫其他函數，避免資料庫鎖定
    upsert_frequent_food(user_id, meal_name, cal, pro)
    flex = build_meal_log_flex(meal_name, cal, pro, new_extra_cal, tdee or 2000, new_extra_pro, protein_goal or 100)
    return flex, f"已加入常吃：{meal_name}"


def build_frequent_food_picker_flex(user_id: str, mode: str = "add"):
    from linebot.models import FlexSendMessage
    d = get_dashboard_data(user_id)
    items = d.get("frequent_foods", []) if d else []
    contents = [{"type":"text","text":("🍱 重選常吃" if mode == "replace" else "🍱 常吃清單"),"size":"sm","weight":"bold","color":"#333333"}]
    if not items:
        contents.append({"type":"text","text":"還沒有常吃資料，先記錄幾餐就會出現。","size":"sm","color":"#888888","margin":"sm","wrap":True})
    else:
        for item in items:
            btn_text = f"改品項為：{item['name']}" if mode == "replace" else f"加入常吃：{item['name']}"
            btn_label = "選這個" if mode == "replace" else "直接加入"
            contents.append({"type":"box","layout":"vertical","margin":"md","spacing":"sm","contents":[
                {"type":"text","text":item['name'],"size":"sm","color":"#333333","wrap":True},
                {"type":"text","text":f"{item['cal']} kcal / {item['pro']}g","size":"xs","color":"#888888"},
                {"type":"button","action":{"type":"message","label":btn_label,"text":btn_text},"style":"secondary","height":"sm"}
            ]})
    bubble = {"type":"bubble","size":"mega","body":{"type":"box","layout":"vertical","paddingAll":"16px","contents":contents}}
    return FlexSendMessage(alt_text="常吃清單", contents=bubble)


def build_add_workout_entry_flex():
    from linebot.models import FlexSendMessage
    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": "#3B82F6",
            "contents": [
                {"type": "text", "text": "🏃 新增運動", "color": "#ffffff", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "直接貼你的課表格式就好", "color": "#dbeafe", "size": "sm", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "spacing": "md",
            "contents": [
                {"type": "text", "text": "你可以直接貼自由格式，我會照原本流程幫你寫入。", "size": "sm", "color": "#333333", "wrap": True},
                {"type": "text", "text": "範例：", "size": "sm", "weight": "bold", "color": "#333333"},
                {"type": "text", "text": "新增課表\n日期：2026/03/25\n運動：(1.2K x 4)+2K@05:00/km R:1'30''/2'30''\n時間：60分鐘\n強度：中", "size": "sm", "color": "#555555", "wrap": True},
                {"type": "text", "text": "或直接貼：\n2026/3/28\n10K~12K@05:40~05:30/km", "size": "sm", "color": "#555555", "wrap": True}
            ]
        }
    }
    return FlexSendMessage(alt_text="新增運動", contents=bubble)


def replace_recent_meal_with_name(user_id: str, meal_name: str):
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        rec = conn.execute(
            """SELECT r.meal_name,r.meal_date,r.food_log_id,fl.version
               FROM recent_meal_logs r LEFT JOIN food_logs fl
                 ON fl.log_id=r.food_log_id AND fl.user_id=r.user_id
               WHERE r.user_id=?""", (user_id,),
        ).fetchone()
        if not rec:
            return None, "目前沒有最近一筆可修改的記錄。"
        if rec[1] != tw_today().isoformat():
            return None, "最近一筆不是今天的，先重新記錄一餐再修正。"
        if not rec[2] or rec[3] is None:
            return None, "這是舊版未綁定逐筆帳本的紀錄，請先重新記錄一餐再修改。"
        ff = conn.execute(
            "SELECT last_cal,last_pro FROM frequent_foods WHERE user_id=? AND meal_name=?",
            (user_id, meal_name),
        ).fetchone()
        new_cal, new_pro = ff if ff else (None, None)
        if not ff:
            dish = next((d for d in MAIN_DISHES if d['name'] == meal_name), None)
            if dish:
                new_cal, new_pro = dish.get('cal'), dish.get('pro')
        if new_cal is None and new_pro is None:
            return None, "找不到這個品項的營養資料，請從常吃清單挑選或重新記錄。"
        log_id, version = rec[2], int(rec[3])
    result = apply_daily_food_log_edit(
        user_id=user_id, log_id=log_id, expected_version=version,
        event_id=f"legacy-replace:{user_id}:{log_id}:{version}:{meal_name}",
        action="replace_item", value={
            "name": meal_name,
            "nutrition": {"calories_kcal": new_cal, "protein_g": new_pro},
        },
    )
    with sqlite3.connect(DB_PATH) as conn:
        hp = conn.execute(
            "SELECT today_extra_cal,today_extra_pro,tdee,protein FROM health_profile WHERE user_id=?",
            (user_id,),
        ).fetchone() or (0, 0, 2000, 100)
        conn.commit()
    upsert_frequent_food(user_id, meal_name, new_cal, new_pro)
    flex = build_meal_log_flex(meal_name, new_cal, new_pro, hp[0], hp[2] or 2000, hp[1], hp[3] or 100)
    return flex, f"已把最近一筆改成：{result['product_name']}"


def mark_planned_meal_as_eaten(user_id: str, meal_slot: str):
    meal_slot = meal_slot.strip()
    if meal_slot not in ["午餐", "晚餐"]:
        return None, "不支援的餐別。"

    d = get_dashboard_data(user_id)
    if not d:
        return None, "找不到你的資料。"

    if meal_slot == "午餐":
        meal_name = d.get("today_lunch", "")
        cal = d.get("lunch_cal", 0)
        pro = d.get("lunch_pro", 0)
    else:
        meal_name = d.get("today_dinner", "")
        cal = d.get("dinner_cal", 0)
        pro = d.get("dinner_pro", 0)

    if not meal_name or meal_name == "尚未安排":
        return None, f"今天沒有可確認的{meal_slot}。"
    if not cal and not pro:
        return None, f"{meal_slot}還沒有營養資料，暫時無法直接確認已吃。"

    today = tw_today().isoformat()
    try:
        # ✅ 安全連線
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM planned_meal_checks WHERE user_id=? AND meal_date=? AND meal_slot=?", (user_id, today, meal_slot))
            if c.fetchone():
                return None, f"今天的{meal_slot}已經確認過了。"

            c.execute("SELECT today_extra_cal, today_extra_pro, today_food_items, today_date, tdee, protein FROM health_profile WHERE user_id=?", (user_id,))
            hp = c.fetchone()
            if not hp:
                return None, "找不到你的健康資料。"

            today_extra_cal, today_extra_pro, today_food_items, today_date, tdee, protein_goal = hp
            daily_log = create_daily_food_log(
                conn, user_id=user_id, product_name=meal_name, meal_slot=meal_slot,
                consumed_at=tw_now().isoformat(timespec="seconds"), servings=1,
                nutrition={"calories_kcal": cal, "protein_g": pro},
                source_type="planned_meal",
            )
            _sync_health_profile_from_ledger_conn(conn, user_id, today)
            c.execute(
                "SELECT today_extra_cal,today_extra_pro,today_food_items FROM health_profile WHERE user_id=?",
                (user_id,),
            )
            projected = c.fetchone() or (0, 0, "")
            new_extra_cal, new_extra_pro, new_food_items = projected[0] or 0, projected[1] or 0, projected[2] or ""
            c.execute("INSERT OR REPLACE INTO planned_meal_checks (user_id, meal_date, meal_slot, meal_name, cal, pro, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (user_id, today, meal_slot, meal_name, cal, pro, tw_now().isoformat()))
            c.execute("""
                INSERT INTO recent_meal_logs (user_id, meal_name, base_cal, base_pro, current_cal, current_pro, meal_date, source_text, updated_at, food_log_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    meal_name=excluded.meal_name,
                    base_cal=excluded.base_cal,
                    base_pro=excluded.base_pro,
                    current_cal=excluded.current_cal,
                    current_pro=excluded.current_pro,
                    meal_date=excluded.meal_date,
                    source_text=excluded.source_text,
                    updated_at=excluded.updated_at,
                    food_log_id=excluded.food_log_id
            """, (user_id, meal_name, cal, pro, cal, pro, today, f"{meal_slot}已吃", tw_now().isoformat(), daily_log["log_id"]))
            conn.commit()
    except Exception as e:
        return None, f"發生錯誤：{str(e)}"

    flex = build_meal_log_flex(meal_name, cal, pro, new_extra_cal, tdee or 2000, new_extra_pro, protein_goal or 100)
    return flex, f"已幫你確認{meal_slot}：{meal_name}。"


def mark_today_workout_done(user_id: str):
    d = get_dashboard_data(user_id)
    if not d:
        return "找不到你的資料。"
    workout_name = d.get("today_workout", "無")
    if not workout_name or workout_name == "無":
        return "今天沒有可確認的運動安排。"

    today = tw_today().isoformat()
    try:
        # ✅ 安全連線
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM workout_checks WHERE user_id=? AND workout_date=?", (user_id, today))
            if c.fetchone():
                return f"今天的運動已經確認完成：{workout_name}。"

            c.execute("INSERT OR REPLACE INTO workout_checks (user_id, workout_date, workout_name, checked_at) VALUES (?, ?, ?, ?)",
                      (user_id, today, workout_name, tw_now().isoformat()))
            conn.commit()
    except Exception as e:
        return f"發生錯誤：{str(e)}"
        
    return f"🏃 已幫你標記今日運動完成：{workout_name}"
# ==========================================
# 🌟 專門負責在背景寫入 Google Sheet 的函數 (避免卡頓)
# ==========================================
def background_log_workout_to_sheet(user_id: str, day_str: str, rpe_score: int, srpe_score: int, duration_mins: int):
    if not gc:
        return
        
    try:
        # 從資料庫重新讀取必要資訊
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT sheet_name FROM health_profile WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()

        if row and row[0]:
            sheet_name = row[0]
            sheet = gc.open_by_url(SHEET_URL)
            now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
            sheet.worksheet(sheet_name).append_row([
                now_str, 
                f"🏃 {day_str}運動狀態回報", 
                f"狀態：【完美達標】(RPE: {rpe_score})", 
                f"產生 sRPE 訓練負荷：{srpe_score} (預估時長 {duration_mins}m)"
            ])
            print(f"✅ [背景任務] 成功將 {user_id} 的運動紀錄寫入 Google Sheet")
    except Exception as e:
        print(f"⚠️ [背景任務] 寫入 Google Sheet 運動狀態失敗: {e}")

# ==========================================
# 🌟 處理運動打卡的主函數
# ==========================================
def mark_workout_done_with_srpe(user_id: str, rpe_score: int, day_str: str = "今日"):
    """處理運動打卡，並根據 RPE 與課表時間計算 sRPE 訓練負荷"""
    import re
    from datetime import timedelta
    d = get_dashboard_data(user_id)
    if not d: return "找不到你的資料。"
    
    # 決定是抓今天還是昨天的課表
    if day_str == "今日":
        workout_name = d.get("today_workout", "無")
        target_date = tw_today().isoformat()
    else:
        # 昨日
        workout_name = "無"
        target_date = (tw_today() - timedelta(days=1)).isoformat()
        
    if workout_name == "無" and day_str == "今日":
        return "今天沒有可確認的運動安排。"

    # 🤖 智能時間萃取器
    duration_mins = 60 # 強制保底預設值 60 分鐘
    if day_str == "今日":
        try:
            time_match = re.search(r'(\d+(?:\.\d+)?)\s*(m|min|分鐘|h|hr|小時)', workout_name.lower())
            if time_match:
                val = float(time_match.group(1))
                unit = time_match.group(2)
                if unit in ['h', 'hr', '小時']:
                    duration_mins = int(val * 60)
                else:
                    duration_mins = int(val)
        except Exception as e:
            print(f"⚠️ 時間解析失敗，使用預設 60 分鐘: {e}")
            duration_mins = 60
            
    if duration_mins <= 0:
        duration_mins = 60

    # 🧮 計算 sRPE
    srpe_score = duration_mins * rpe_score
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 寫入 SQLite
    c.execute("INSERT OR REPLACE INTO workout_checks (user_id, workout_date, workout_name, checked_at) VALUES (?, ?, ?, ?)",
              (user_id, target_date, workout_name, tw_now().isoformat()))
              
    c.execute("INSERT OR IGNORE INTO user_achievements (user_id, xp_total, streak_days, weekly_srpe) VALUES (?, 0, 0, 0)", (user_id,))
    
    c.execute("""
        UPDATE user_achievements 
        SET weekly_srpe = COALESCE(weekly_srpe, 0) + ?, 
            xp_total = COALESCE(xp_total, 0) + 20 
        WHERE user_id = ?
    """, (srpe_score, user_id))
    
    conn.commit()
    conn.close()
    
    # 🌟 魔法在這裡！利用 Python 內建的 threading 把耗時工作丟到背景
    threading.Thread(
        target=background_log_workout_to_sheet, 
        args=(user_id, day_str, rpe_score, srpe_score, duration_mins)
    ).start()
            
    return f"🔥 太棒了！已紀錄{day_str}訓練完成。\n\n📊 本次訓練累積了 【{srpe_score} sRPE】 的負荷積分！\n⭐ 達成任務，獲得 +20 XP！"
def build_edit_content_flex():
    from linebot.models import FlexSendMessage
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": "#FF6B35",
            "contents": [
                {"type": "text", "text": "🍚 修正內容", "color": "#ffffff", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "套用到最近一筆記錄", "color": "#ffe3d9", "size": "sm", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type":"button","action":{"type":"message","label":"⚖️ 改份量","text":"改份量"},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"message","label":"🍱 改品項","text":"改品項"},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"message","label":"🍱 重選常吃","text":"重選常吃"},"style":"secondary","height":"sm"}
            ]
        }
    }
    return FlexSendMessage(alt_text="修正內容", contents=bubble)


def build_portion_adjust_flex():
    from linebot.models import FlexSendMessage
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": "#FF6B35",
            "contents": [
                {"type": "text", "text": "⚖️ 改份量", "color": "#ffffff", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "套用到最近一筆記錄", "color": "#ffe3d9", "size": "sm", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type":"button","action":{"type":"message","label":"🥣 少量","text":"少量"},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"message","label":"🍽️ 正常","text":"正常"},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"message","label":"🍱 大份","text":"大份"},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"message","label":"🍚 少飯","text":"少飯"},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"message","label":"🍚 加飯","text":"加飯"},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"message","label":"🥫 去醬","text":"去醬"},"style":"secondary","height":"sm"}
            ]
        }
    }
    return FlexSendMessage(alt_text="改份量", contents=bubble)


def apply_portion_adjustment(user_id: str, action: str):
    rules = {
        "少量": (0.7, 0.7), "正常": (1.0, 1.0), "大份": (1.3, 1.3),
        "少飯": (0.85, 0.95), "加飯": (1.15, 1.05), "去醬": (0.9, 1.0),
    }
    if action not in rules:
        return None, "不支援的修正選項"
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        rec = conn.execute(
            """SELECT r.meal_name,r.base_cal,r.base_pro,r.meal_date,r.food_log_id,
                      fl.version,h.tdee,h.protein
               FROM recent_meal_logs r
               LEFT JOIN food_logs fl ON fl.log_id=r.food_log_id AND fl.user_id=r.user_id
               LEFT JOIN health_profile h ON h.user_id=r.user_id
               WHERE r.user_id=?""", (user_id,),
        ).fetchone()
        if not rec:
            return None, "目前沒有最近一筆可修正的記錄。"
        meal_name, base_cal, base_pro, meal_date, log_id, version, tdee, protein_goal = rec
        if meal_date != tw_today().isoformat():
            return None, "最近一筆記錄不是今天的，先重新記錄一餐再修正。"
        if not log_id or version is None:
            return None, "這是舊版未綁定逐筆帳本的紀錄，請先重新記錄一餐再修改。"
    cal_mul, pro_mul = rules[action]
    new_cal = int(round((base_cal or 0) * cal_mul)) if base_cal is not None else None
    new_pro = int(round((base_pro or 0) * pro_mul)) if base_pro is not None else None
    patch_values = {}
    if new_cal is not None:
        patch_values["calories_kcal"] = new_cal
    if new_pro is not None:
        patch_values["protein_g"] = new_pro
    result = apply_daily_food_log_edit(
        user_id=user_id, log_id=log_id, expected_version=int(version),
        event_id=f"legacy-portion:{user_id}:{log_id}:{version}:{action}",
        action="patch_nutrition", value=patch_values,
    )
    with sqlite3.connect(DB_PATH) as conn:
        hp = conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id=?", (user_id,)
        ).fetchone() or (0, 0)
        conn.commit()
    flex = build_meal_log_flex(meal_name, new_cal, new_pro, hp[0], tdee or 2000, hp[1], protein_goal or 100)
    return flex, f"已將「{result['product_name']}」調整為{action}。"


BADGE_LEVELS = [
    (1, "小種子", 0),
    (2, "初萌芽", 400),
    (3, "青小苗", 800),
    (4, "茁壯葉", 1200),
    (5, "初花苞", 1600),
    (6, "盛開花", 2000),
    (7, "豐收果", 2400),
]


def get_badge_level_from_xp(xp_total: int):
    xp_total = max(0, int(xp_total or 0))
    current = BADGE_LEVELS[0]
    next_level = None
    for idx, level in enumerate(BADGE_LEVELS):
        if xp_total >= level[2]:
            current = level
            next_level = BADGE_LEVELS[idx + 1] if idx + 1 < len(BADGE_LEVELS) else None
    return current, next_level


def get_streak_title(streak_days: int) -> str:
    streak_days = int(streak_days or 0)
    if streak_days >= 31:
        return "鐵粉"
    if streak_days >= 15:
        return "老面孔"
    if streak_days >= 8:
        return "穩定玩家"
    if streak_days >= 4:
        return "漸入佳境"
    return "初訪者"


def compute_achievement_snapshot(user_id: str, dashboard: dict = None) -> dict:
    today = tw_today().isoformat()
    d = dashboard or get_dashboard_data(user_id)
    if not d: return {}

    has_today_plan = any([
        (d.get("today_lunch") or "") not in ["", "尚未安排"],
        (d.get("today_dinner") or "") not in ["", "尚未安排"],
        (d.get("today_workout") or "") not in ["", "無", "尚未安排"],
    ])
    meals_logged_count = int(d.get("recorded_count") or 0)
    protein_80_reached = bool(d.get("task_protein_80"))
    workout_done = bool(d.get("workout_done"))
    valid_completed_day = bool(has_today_plan and meals_logged_count >= 1)

    task_log_once_done = meals_logged_count >= 1
    task_log_two_meals_done = meals_logged_count >= 2
    task_protein_80_done = protein_80_reached
    task_workout_done = workout_done
    today_task_done = sum([task_log_once_done, task_log_two_meals_done, task_protein_80_done, task_workout_done]) >= 3

    # ✅ 安全連線
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        try:
            c.execute("SELECT xp_total, streak_days, last_valid_plan_date, weekly_srpe FROM user_achievements WHERE user_id=?", (user_id,))
            row = c.fetchone()
            if row:
                xp_total, streak_days, last_valid_plan_date, weekly_srpe = row
            else:
                xp_total, streak_days, last_valid_plan_date, weekly_srpe = 0, 0, "", 0
        except Exception:
            xp_total, streak_days, last_valid_plan_date, weekly_srpe = 0, 0, "", 0

        c.execute("SELECT has_today_plan, meals_logged_count, protein_80_reached, workout_done, task_done, valid_completed_day, today_xp_earned, badge_unlocked_today FROM achievement_daily_log WHERE user_id=? AND log_date=?", (user_id, today))
        existing = c.fetchone()

        today_xp_earned = 0
        badge_unlocked_today = 0

        if existing:
            prev_has_plan, prev_meals, prev_protein80, prev_workout_done, prev_task_done, prev_valid_day, prev_today_xp, prev_badge_unlocked = existing
            prev_meals = int(prev_meals or 0)
            prev_today_xp = int(prev_today_xp or 0)
            today_xp_earned = prev_today_xp
            badge_unlocked_today = int(prev_badge_unlocked or 0)

            if not prev_valid_day and valid_completed_day: today_xp_earned += 20
            if meals_logged_count >= 2 and prev_meals < 2: today_xp_earned += 5
            if protein_80_reached and not prev_protein80: today_xp_earned += 5
            if today_task_done and not prev_task_done: today_xp_earned += 5

            delta_xp = today_xp_earned - prev_today_xp
            if delta_xp: xp_total += delta_xp

            c.execute("UPDATE achievement_daily_log SET has_today_plan=?, meals_logged_count=?, protein_80_reached=?, workout_done=?, task_done=?, valid_completed_day=?, today_xp_earned=?, badge_unlocked_today=? WHERE user_id=? AND log_date=?",
                      (1 if has_today_plan else 0, meals_logged_count, 1 if protein_80_reached else 0, 1 if workout_done else 0, 1 if today_task_done else 0, 1 if valid_completed_day else 0, today_xp_earned, badge_unlocked_today, user_id, today))
        else:
            if valid_completed_day: today_xp_earned += 20
            if meals_logged_count >= 2: today_xp_earned += 5
            if protein_80_reached: today_xp_earned += 5
            if today_task_done: today_xp_earned += 5
            xp_total += today_xp_earned

            c.execute("INSERT OR REPLACE INTO achievement_daily_log (user_id, log_date, has_today_plan, meals_logged_count, protein_80_reached, workout_done, task_done, valid_completed_day, today_xp_earned, badge_unlocked_today) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (user_id, today, 1 if has_today_plan else 0, meals_logged_count, 1 if protein_80_reached else 0, 1 if workout_done else 0, 1 if today_task_done else 0, 1 if valid_completed_day else 0, today_xp_earned, 0))

        yesterday = (tw_today() - timedelta(days=1)).isoformat()
        new_streak = int(streak_days or 0)
        if valid_completed_day:
            if last_valid_plan_date == today: pass
            elif last_valid_plan_date == yesterday: new_streak += 1
            else: new_streak = 1
        else:
            if has_today_plan and last_valid_plan_date not in [today, yesterday]: new_streak = 0

        streak_bonus = 0
        if valid_completed_day and last_valid_plan_date != today and new_streak in [4, 8, 15, 31]:
            streak_bonus = 20

        current_before, _ = get_badge_level_from_xp(xp_total)
        if streak_bonus:
            xp_total += streak_bonus
            today_xp_earned += streak_bonus
        current_after, next_level = get_badge_level_from_xp(xp_total)
        if current_after[0] > current_before[0]:
            xp_total += 15
            today_xp_earned += 15
            badge_unlocked_today = 1
            current_after, next_level = get_badge_level_from_xp(xp_total)

        new_last_valid_plan_date = today if valid_completed_day else last_valid_plan_date
        c.execute("INSERT OR REPLACE INTO user_achievements (user_id, xp_total, streak_days, last_valid_plan_date, weekly_srpe, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, xp_total, new_streak, new_last_valid_plan_date or "", weekly_srpe, tw_now().isoformat()))
        c.execute("UPDATE achievement_daily_log SET today_xp_earned=?, badge_unlocked_today=? WHERE user_id=? AND log_date=?",
                  (today_xp_earned, badge_unlocked_today, user_id, today))
        conn.commit()

    current_level_num, current_badge_name, current_threshold = current_after
    next_badge_name = next_level[1] if next_level else "已達最高等級"
    next_threshold = next_level[2] if next_level else current_threshold
    xp_to_next_level = max(0, next_threshold - xp_total) if next_level else 0
    span = max(1, (next_threshold - current_threshold)) if next_level else 1
    xp_progress_percent = 100 if not next_level else max(0, min(100, round((xp_total - current_threshold) / span * 100)))

    return {
        "has_today_plan": has_today_plan, "valid_completed_day": valid_completed_day, "today_task_done": today_task_done,
        "today_xp_earned": int(today_xp_earned or 0), "xp_total": int(xp_total or 0), "current_badge_level": current_level_num,
        "current_badge_name": current_badge_name, "next_badge_name": next_badge_name, "xp_to_next_level": xp_to_next_level,
        "xp_progress_percent": xp_progress_percent, "streak_days": new_streak, "streak_title": get_streak_title(new_streak),
        "task_log_once_done": task_log_once_done, "task_log_two_meals_done": task_log_two_meals_done,
        "task_protein_80_done": task_protein_80_done, "task_workout_done": task_workout_done,
    }


def get_dashboard_data(user_id: str) -> dict:
    """取得儀表板所需資料，Phase 1 Flex Message 和 Phase 2 LIFF API 都用這個"""
    hp = None
    checked_slots = set()
    workout_done = False
    frequent_foods = []
    approved_photo_foods = []
    approved_photo_cal = 0.0
    approved_photo_pro = 0.0
    today_str = tw_today().isoformat()

    # 🌟 優化：將原本散落的 3 次資料庫連線，合併成 1 次安全連線！
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        
        # 1. 抓健康檔案
        c.execute("""SELECT name, tdee, protein, today_extra_cal, today_extra_pro,
                            today_food_items, today_date, sheet_name
                     FROM health_profile WHERE user_id=?""", (user_id,))
        hp = c.fetchone()
        
        if hp:
            # 2. 抓今日打卡紀錄
            try:
                c.execute("SELECT meal_slot FROM planned_meal_checks WHERE user_id=? AND meal_date=?", (user_id, today_str))
                checked_slots = {r[0] for r in c.fetchall()}
                c.execute("SELECT 1 FROM workout_checks WHERE user_id=? AND workout_date=?", (user_id, today_str))
                workout_done = bool(c.fetchone())
            except Exception:
                pass
                
            # 3. 抓常吃清單
            try:
                c.execute("SELECT meal_name, last_cal, last_pro, use_count FROM frequent_foods WHERE user_id=? ORDER BY use_count DESC, last_used_at DESC LIMIT 4", (user_id,))
                frequent_foods = [
                    {"name": r[0], "cal": r[1] or 0, "pro": r[2] or 0, "count": r[3] or 0}
                    for r in c.fetchall()
                ]
            except Exception:
                pass

            # 4. 照片餐點只有在管理員完成精確份量核准後，才從正式 food_logs 顯示。
            # 營養值只從通過 approval hash 的核准交換份重算，舊NA快照亦可安全納入。
            try:
                c.execute(
                    """SELECT fc.product_name,fc.fingerprint,a.food_fingerprint,
                              a.suggestion_rule_version,a.approved_exchange_json,
                              a.approved_exchange_hash
                       FROM food_logs fl
                       JOIN food_catalog fc ON fc.food_id=fl.food_id
                       JOIN food_exchange_approvals a
                         ON a.approval_id=fl.exchange_approval_id AND a.food_id=fl.food_id
                       WHERE fl.user_id=? AND date(fl.consumed_at, '+8 hours')=?
                         AND fl.confirmation_status='confirmed'
                         AND fc.source_type='user_meal_photo'
                       ORDER BY fl.consumed_at, fl.created_at""",
                    (user_id, today_str),
                )
                for row in c.fetchall():
                    try:
                        if str(row[2] or "") != str(row[1] or ""):
                            continue
                        approved_exchange = json.loads(row[4] or "{}")
                        expected_hash = exchange_approval_hash(row[2], row[3], approved_exchange)
                        if not secrets.compare_digest(str(row[5] or ""), expected_hash):
                            continue
                        estimated = estimate_nutrition_from_exchanges(approved_exchange)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    product_name = str(row[0] or "").strip()
                    if product_name:
                        approved_photo_foods.append(product_name)
                    approved_photo_cal += float(estimated["calories_kcal"])
                    approved_photo_pro += float(estimated["protein_g"])
            except Exception:
                pass

    if not hp:
        return None

    name, tdee, protein_goal, extra_cal, extra_pro, food_items, today_date, sheet_name = hp
    tdee = tdee or 2000
    protein_goal = protein_goal or 100
    extra_cal = extra_cal or 0
    extra_pro = extra_pro or 0

    if today_date != today_str:
        extra_cal, extra_pro, food_items = 0, 0, ""

    extra_cal = round(float(extra_cal) + approved_photo_cal, 4)
    extra_pro = round(float(extra_pro) + approved_photo_pro, 4)

    cal_remaining = max(0, tdee - extra_cal)
    pro_remaining = max(0, protein_goal - extra_pro)
    cal_pct = min(100, round(extra_cal / tdee * 100)) if tdee > 0 else 0
    pro_pct = min(100, round(extra_pro / protein_goal * 100)) if protein_goal > 0 else 0

    def _to_int(raw, default=0):
        m = re.search(r'(\d+)', str(raw or ''))
        return int(m.group(1)) if m else default

    today_lunch, today_dinner, today_workout = "尚未安排", "尚未安排", "無"
    lunch_cal = lunch_pro = dinner_cal = dinner_pro = 0
    planned_cal = planned_pro = 0
    today_date_str = tw_today().strftime("%Y/%m/%d")
    
    if gc and sheet_name:
        try:
            book = gc.open_by_key(SPREADSHEET_ID)
            user_sheet, resolved_sheet_name, resolved_source = get_or_create_user_sheet(book, user_id, name, sheet_name)
            if user_sheet:
                if resolved_sheet_name != sheet_name:
                    sheet_name = resolved_sheet_name
                    print(f"ℹ️ Dashboard 已校正 sheet_name：{user_id} -> {resolved_sheet_name} ({resolved_source})")

                for row in get_user_sheet_rows(user_sheet):
                    row_date = _pick_first(row, ["日期", "取餐日期", "實際日期", "Date"], "")
                    if normalize_date_str(row_date) == normalize_date_str(today_date_str):
                        today_lunch  = _pick_first(row, ["午餐", "午餐安排", "Lunch_Item"], "尚未安排")
                        today_dinner = _pick_first(row, ["晚餐", "晚餐安排", "Dinner_Item"], "尚未安排")
                        today_workout = _pick_first(row, ["Sport_Type", "運動強度", "運動", "Workout"], "無")
                        lunch_cal = _to_int(_pick_first(row, ["午餐熱量"], 0), 0)
                        lunch_pro = _to_int(_pick_first(row, ["午餐蛋白"], 0), 0)
                        dinner_cal = _to_int(_pick_first(row, ["晚餐熱量"], 0), 0)
                        dinner_pro = _to_int(_pick_first(row, ["晚餐蛋白"], 0), 0)
                        planned_cal = _to_int(_pick_first(row, ["今日排餐總熱量"], 0), lunch_cal + dinner_cal)
                        planned_pro = _to_int(_pick_first(row, ["今日排餐總蛋白"], 0), lunch_pro + dinner_pro)
                        break

            if today_workout in ["", "無", "尚未安排"]:
                try:
                    api_sheet = book.worksheet("Master_API_View")
                    records = api_sheet.get_all_records()
                    for r in records:
                        sheet_date = str(r.get("Date", "")).replace("-", "/").strip()
                        if str(r.get("User_ID", "")).strip() == user_id and sheet_date == today_date_str:
                            today_workout = str(r.get("Tomorrow_Training", "")).strip() or str(r.get("Plan_Week", "")).strip() or str(r.get("Sport_Type", "")).strip() or "無"
                            break
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ Dashboard 讀取個人分頁失敗: {e}")

    cal_recorded_segment = min(extra_cal, tdee)
    cal_planned_segment = max(min(planned_cal, tdee) - cal_recorded_segment, 0)
    cal_remaining_segment = max(tdee - cal_recorded_segment - cal_planned_segment, 0)

    pro_recorded_segment = min(extra_pro, protein_goal)
    pro_planned_segment = max(min(planned_pro, protein_goal) - pro_recorded_segment, 0)
    pro_remaining_segment = max(protein_goal - pro_recorded_segment - pro_planned_segment, 0)

    lunch_checked = "午餐" in checked_slots
    dinner_checked = "晚餐" in checked_slots

    food_list = [f.strip() for f in food_items.split("、") if f.strip()] if food_items else []
    food_list.extend(approved_photo_foods)
    recorded_count = len(food_list)
    task_logged_once = (extra_cal > 0 or extra_pro > 0 or recorded_count >= 1)
    task_two_meals = recorded_count >= 2
    task_protein_80 = extra_pro >= (protein_goal * 0.8)

    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    today_label = f"{tw_today().strftime('%m/%d')}（{weekdays[tw_today().weekday()]}）"

    future_days = []
    if gc and sheet_name:
        try:
            book = gc.open_by_key(SPREADSHEET_ID)
            user_sheet = book.worksheet(sheet_name)
            user_rows = get_user_sheet_rows(user_sheet)
            user_map = {}
            for row in user_rows:
                row_date = _pick_first(row, ["日期", "取餐日期", "實際日期", "Date"], "")
                if not row_date: continue

            api_map = {}
            try:
                api_sheet = book.worksheet("Master_API_View")
                for r in api_sheet.get_all_records():
                    sheet_date = str(r.get("Date", "")).replace("-", "/").strip()
                    if str(r.get("User_ID", "")).strip() == user_id and sheet_date:
                        api_map[sheet_date] = r
            except Exception:
                pass

            today_dt = tw_today()
            for delta_days in range(1, 5):
                row_dt = today_dt + timedelta(days=delta_days)
                key = row_dt.strftime("%Y/%m/%d")
                row = user_map.get(key, {})
                api_row = api_map.get(key, {})
                day_label = f"{row_dt.strftime('%m/%d')}（{weekdays[row_dt.weekday()]}）"
                row_lunch = _pick_first(row, ["午餐", "午餐安排", "Lunch_Item"], "尚未安排") if row else (str(api_row.get("Lunch_Item", "")).strip() or "尚未安排")
                row_dinner = _pick_first(row, ["晚餐", "晚餐安排", "Dinner_Item"], "尚未安排") if row else (str(api_row.get("Dinner_Item", "")).strip() or "尚未安排")
                row_workout = _pick_first(row, ["Sport_Type", "運動強度", "運動", "Workout"], "無") if row else "無"
                if row_workout in ["", "無", "尚未安排"]:
                    row_workout = str(api_row.get("Tomorrow_Training", "")).strip() or str(api_row.get("Plan_Week", "")).strip() or str(api_row.get("Sport_Type", "")).strip() or "無"
                future_days.append({
                    "label": day_label, "workout": row_workout or "無", "lunch": row_lunch, "dinner": row_dinner,
                })
        except Exception:
            pass

    # 🌟 教練後台分區課表優先覆蓋：個人 > 區別 > 班級
    try:
        today_assignment = find_training_assignment_for_date(user_id, tw_today())
        if today_assignment:
            today_workout = format_assignment_workout(today_assignment)
        for item in future_days:
            try:
                label = item.get("label", "")
                m = re.search(r"(\d{2})/(\d{2})", label)
                if not m:
                    continue
                row_date = tw_today().replace(month=int(m.group(1)), day=int(m.group(2)))
                assignment = find_training_assignment_for_date(user_id, row_date)
                if assignment:
                    item["workout"] = format_assignment_workout(assignment)
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ 分區課表整合失敗: {e}")

    result = {
        "name": name or "你", "today_label": today_label, "tdee": tdee, "protein_goal": protein_goal,
        "extra_cal": extra_cal, "extra_pro": extra_pro, "planned_cal": planned_cal, "planned_pro": planned_pro,
        "cal_remaining": cal_remaining, "pro_remaining": pro_remaining, "cal_pct": cal_pct, "pro_pct": pro_pct,
        "cal_recorded_segment": cal_recorded_segment, "cal_planned_segment": cal_planned_segment, "cal_remaining_segment": cal_remaining_segment,
        "pro_recorded_segment": pro_recorded_segment, "pro_planned_segment": pro_planned_segment, "pro_remaining_segment": pro_remaining_segment,
        "today_lunch": today_lunch, "today_dinner": today_dinner, "today_workout": today_workout,
        "lunch_cal": lunch_cal, "lunch_pro": lunch_pro, "dinner_cal": dinner_cal, "dinner_pro": dinner_pro,
        "food_list": food_list, "recorded_count": recorded_count, "lunch_checked": lunch_checked,
        "dinner_checked": dinner_checked, "workout_done": workout_done, "task_logged_once": task_logged_once,
        "task_two_meals": task_two_meals, "task_protein_80": task_protein_80, "frequent_foods": frequent_foods,
        "future_days": future_days,
    }
    try:
        result.update(compute_achievement_snapshot(user_id, result))
    except Exception as e:
        print(f"⚠️ 成就中心計算失敗: {e}")
    return result



def build_dashboard_flex(user_id: str):
    """組 LINE Flex Message：戰情中心 (Layout B) + 成就系統 (Image 2) + 完整補回日曆與昨日/明日區塊"""
    from linebot.models import FlexSendMessage
    from datetime import datetime
    from datetime import timedelta as _td
    import sqlite3

    d = get_dashboard_data(user_id)
    if not d: return None

    # --- 1. 小工具函數區 ---
    def dual_progress_bar(recorded: int, planned_only: int, remaining: int, recorded_color: str, planned_color: str) -> dict:
        total = max(recorded + planned_only + remaining, 1)
        seg_recorded = round(recorded / total * 100)
        seg_planned = round(planned_only / total * 100)
        seg_remaining = max(0, 100 - seg_recorded - seg_planned)
        parts = []
        if seg_recorded > 0: parts.append({"type": "box", "layout": "vertical", "contents": [], "flex": seg_recorded, "backgroundColor": recorded_color, "height": "8px", "cornerRadius": "4px"})
        if seg_planned > 0: parts.append({"type": "box", "layout": "vertical", "contents": [], "flex": seg_planned, "backgroundColor": planned_color, "height": "8px", "cornerRadius": "4px"})
        if seg_remaining > 0: parts.append({"type": "box", "layout": "vertical", "contents": [], "flex": seg_remaining, "backgroundColor": "#EDEDED", "height": "8px", "cornerRadius": "4px"})
        return {"type": "box", "layout": "horizontal", "contents": parts, "spacing": "none"}

    def single_progress_bar(pct, color):
        pct = max(1, min(100, pct))
        return {
            "type": "box", "layout": "horizontal", "spacing": "none", "contents": [
                {"type": "box", "layout": "vertical", "flex": pct, "backgroundColor": color, "contents": [], "height": "8px", "cornerRadius": "4px"},
                {"type": "box", "layout": "vertical", "flex": 100-pct, "backgroundColor": "#E5E7EB", "contents": [], "height": "8px", "cornerRadius": "4px"}
            ]
        }

    def badge_tag(text, bg_color, text_color="#ffffff"):
        return {
            "type": "box", "layout": "vertical", "backgroundColor": bg_color, "cornerRadius": "20px",
            "paddingAll": "4px", "paddingStart": "10px", "paddingEnd": "10px", "contents": [
                {"type": "text", "text": text, "color": text_color, "size": "xxs", "weight": "bold"}
            ]
        }

    # --- 2. 準備左卡資料 (飲食) ---
    hour = tw_now().hour
    greeting = "早安" if hour < 12 else ("午安" if hour < 17 else "晚安")
    lunch_line = f"☀️ 午餐：{d['today_lunch']}"
    if d["lunch_cal"] or d["lunch_pro"]: lunch_line += f"（{d['lunch_cal']} kcal / {d['lunch_pro']}g）"
    dinner_line = f"🌙 晚餐：{d['today_dinner']}"
    if d["dinner_cal"] or d["dinner_pro"]: dinner_line += f"（{d['dinner_cal']} kcal / {d['dinner_pro']}g）"

    frequent_contents = [{"type": "text", "text": "🍱 常吃清單", "size": "xs", "color": "#888888"}]
    if d.get("frequent_foods"):
        for item in d["frequent_foods"]:
            frequent_contents.append({
                "type": "box", "layout": "vertical", "margin": "sm", "spacing": "sm", "contents": [
                    {"type": "text", "text": item["name"], "size": "sm", "color": "#333333", "wrap": True},
                    {"type": "button", "action": {"type": "message", "label": "直接加入", "text": f"加入常吃：{item['name']}"}, "style": "secondary", "height": "sm"}
                ]
            })

    # --- 3. 計算賽事倒數與週期 ---
    try:
        from server import calculate_training_phase
    except ImportError:
        def calculate_training_phase(date_str): return "健康維持", 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 分開撈取，確保不漏資料！
    # 撈賽事日期
    c.execute("SELECT race_date FROM health_profile WHERE user_id=?", (user_id,))
    race_row = c.fetchone()
    race_date_str = race_row[0] if race_row else ""
    
    # 🌟 獨立精準撈取 XP 與 sRPE！
    c.execute("SELECT xp_total, weekly_srpe FROM user_achievements WHERE user_id=?", (user_id,))
    ach_row = c.fetchone()
    # (原本的 conn.close() 已刪除)
    
    xp_total = ach_row[0] if ach_row else 0
    weekly_srpe = ach_row[1] if ach_row else 0
    
    phase_name, weeks_left = calculate_training_phase(race_date_str)
    countdown_text = "無賽事安排"
    if race_date_str:
        try:
            rd = datetime.strptime(race_date_str, "%Y/%m/%d").replace(tzinfo=TW_TZ)
            delta_days = (rd - tw_now()).days + 1
            countdown_text = f"賽事倒數 {delta_days} 天" if delta_days >= 0 else "賽事已結束"
        except: pass

    # --- 4. 準備成就、任務狀態與日曆 ---
    xp_pct = d.get("xp_progress_percent", 0)
    current_tss_target = 1000 # 未來可從 AI 預算抓取
    srpe_pct = min(100, round((weekly_srpe / max(1, current_tss_target)) * 100))
    
    today_date = tw_today()
    yesterday = today_date - _td(days=1)
    tomorrow = today_date + _td(days=1)
    
    today_workout = d.get("today_workout", "待安排")
    today_workout_done = d.get("workout_done", False)
    tomorrow_workout = d["future_days"][0].get("workout", "休息日") if d.get("future_days") else "休息日"
    
    # 任務打勾判定
    task_1 = '✅' if d.get('task_log_once_done') else '⬜'
    task_2 = '✅' if d.get('task_log_two_meals_done') else '⬜'
    task_3 = '✅' if d.get('task_protein_80_done') else '⬜'
    task_4 = '✅' if d.get('task_workout_done') else '⬜'

    # 🌟 查詢本週 7 天打卡狀態
    monday_date = today_date - _td(days=today_date.weekday())
    week_dates_str = [(monday_date + _td(days=i)).isoformat() for i in range(7)]
    
    c.execute("SELECT workout_date FROM workout_checks WHERE user_id=? AND workout_date >= ? AND workout_date <= ?", 
              (user_id, week_dates_str[0], week_dates_str[-1]))
    done_dates = {row[0] for row in c.fetchall()}
    conn.close()

    weekdays_zh = ["一", "二", "三", "四", "五", "六", "日"]
    week_checks_ui = []
    days_completed_this_week = 0
    
    for i in range(7):
        is_done = week_dates_str[i] in done_dates
        if is_done: days_completed_this_week += 1
        icon = "✅" if is_done else "⬜"
        color = "#06C755" if is_done else "#AAAAAA"
        weight = "bold" if is_done else "regular"
        
        if week_dates_str[i] == today_date.isoformat():
            text_label = f"[{weekdays_zh[i]}]\n{icon}"
        else:
            text_label = f"{weekdays_zh[i]}\n{icon}"
            
        week_checks_ui.append({
            "type": "text", "text": text_label, "size": "xxs", "color": color, 
            "align": "center", "weight": weight, "flex": 1, "wrap": True
        })

    # ==========================================
    # 👈 左卡：純飲食儀表板 Bubble
    # ==========================================
    diet_bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": "#06C755",
            "contents": [
                {"type": "text", "text": f"👋 {d['name']}，{greeting}", "color": "#ffffff", "size": "sm", "weight": "bold"},
                {"type": "text", "text": "今日飲食儀表板", "color": "#ffffff", "size": "xl", "weight": "bold", "margin": "sm"},
                {"type": "text", "text": d['today_label'], "color": "#ccffcc", "size": "xs", "margin": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "🔥 熱量進度", "size": "xs", "color": "#888888"},
                {"type": "text", "text": f"今日已記錄：{d['extra_cal']} / {d['tdee']} kcal", "size": "md", "weight": "bold", "color": "#222222", "margin": "sm"},
                dual_progress_bar(d["cal_recorded_segment"], d["cal_planned_segment"], d["cal_remaining_segment"], "#06C755", "#B7E8C8"),
                {"type": "separator", "margin": "md"},
                
                {"type": "text", "text": "🥩 蛋白進度", "size": "xs", "color": "#888888", "margin": "md"},
                {"type": "text", "text": f"今日已記錄：{d['extra_pro']} / {d['protein_goal']} g", "size": "md", "weight": "bold", "color": "#222222", "margin": "sm"},
                dual_progress_bar(d["pro_recorded_segment"], d["pro_planned_segment"], d["pro_remaining_segment"], "#FF6B35", "#FFD2C2"),
                {"type": "separator", "margin": "lg"},

                {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#f8f8f8", "cornerRadius": "8px", "paddingAll": "12px", "contents": [
                    {"type": "text", "text": "📋 今日安排", "size": "xs", "color": "#888888"},
                    {"type": "text", "text": lunch_line, "size": "sm", "color": "#333333", "margin": "sm", "wrap": True},
                    {"type": "button", "action": {"type": "message", "label": ("✅ 午餐已吃" if d.get("lunch_checked") else "☀️ 午餐已吃"), "text": "午餐已吃"}, "style": "secondary", "height": "sm", "margin": "sm"},
                    {"type": "text", "text": dinner_line, "size": "sm", "color": "#333333", "margin": "md", "wrap": True},
                    {"type": "button", "action": {"type": "message", "label": ("✅ 晚餐已吃" if d.get("dinner_checked") else "🌙 晚餐已吃"), "text": "晚餐已吃"}, "style": "secondary", "height": "sm", "margin": "sm"}
                ]},

                {"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "contents": [
                    {"type": "button", "action": {"type": "message", "label": "✏️ 記錄一餐", "text": "記錄一餐"}, "style": "primary", "color": "#06C755", "flex": 1, "height": "sm"},
                    {"type": "button", "action": {"type": "message", "label": "🍚 修正內容", "text": "我要修改飲食紀錄"}, "style": "secondary", "flex": 1, "height": "sm"}
                ]},

                {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#FFFDF5", "cornerRadius": "8px", "paddingAll": "10px", "contents": frequent_contents}
            ]
        }
    }

    # ==========================================
    # 👉 右卡：運動與成就專區 Bubble (完整包含日曆與昨/明區塊)
    # ==========================================
    sport_bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": "#3B82F6",
            "contents": [
                {"type": "text", "text": "🏆 運動戰情指揮中心", "color": "#ffffff", "size": "lg", "weight": "bold"},
                {"type": "text", "text": d['today_label'], "color": "#dbeafe", "size": "xs", "margin": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "contents": [
                # ─── 1. 戰情區 (Tags) ───
                {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                    badge_tag(f"🔥 {phase_name or '健康維持'}", "#EF4444"),
                    badge_tag(f"⏳ {countdown_text}", "#10B981")
                ]},
                
                # ─── 2. 負荷進度條 (sRPE) ───
                {"type": "box", "layout": "vertical", "margin": "xl", "contents": [
                    {"type": "text", "text": "📊 本週訓練負荷 (sRPE)", "size": "xs", "color": "#888888", "weight": "bold"},
                    {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                        {"type": "text", "text": str(weekly_srpe), "size": "3xl", "weight": "bold", "color": "#3B82F6"},
                        {"type": "text", "text": f" / {current_tss_target}", "size": "sm", "color": "#AAAAAA", "weight": "bold", "margin": "sm"}
                    ]},
                    single_progress_bar(srpe_pct, "#3B82F6"),
                    {"type": "text", "text": "💬 穩定累積，避免單次過度負荷！", "size": "xxs", "color": "#64748B", "margin": "sm"}
                ]},
                {"type": "separator", "margin": "lg"},

                # ─── 🌟 3. 動態 7 天打勾日曆 (加回來了！) ───
                {"type": "text", "text": f"📅 本週執行狀態 (完成 {days_completed_this_week} 天)", "size": "xs", "weight": "bold", "color": "#888888", "margin": "md"},
                {"type": "box", "layout": "horizontal", "margin": "md", "contents": week_checks_ui},
                {"type": "separator", "margin": "lg"},

                # ─── 4. 成就中心 (XP & 任務) ───
                {"type": "box", "layout": "vertical", "margin": "lg", "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "🏅 成就中心", "size": "sm", "color": "#EAB308", "weight": "bold"},
                        {"type": "text", "text": f"{xp_total} XP", "size": "xs", "color": "#888888", "align": "end"}
                    ]},
                    {"type": "text", "text": d.get('current_badge_name', '小種子'), "size": "xl", "weight": "bold", "color": "#222222", "margin": "sm"},
                    {"type": "text", "text": f"還差 {d.get('xp_to_next_level', 0)} XP 升級為【{d.get('next_badge_name', '下一階')}】", "size": "xs", "color": "#555555", "margin": "xs"},
                    {"type": "box", "layout": "vertical", "margin": "sm", "contents": [
                        {"type": "text", "text": f"進度條 {xp_pct}%", "size": "xxs", "color": "#888888", "margin": "xs"},
                        single_progress_bar(xp_pct, "#10B981")
                    ]},
                    {"type": "text", "text": f"🔥 {d.get('streak_days', 0)} 天連續紀錄", "size": "sm", "weight": "bold", "color": "#222222", "margin": "md"},
                    
                    # 今日任務清單
                    {"type": "text", "text": "✅ 今日任務", "size": "sm", "weight": "bold", "color": "#10B981", "margin": "md"},
                    {"type": "box", "layout": "horizontal", "margin": "xs", "contents": [
                        {"type": "box", "layout": "vertical", "flex": 1, "contents": [
                            {"type": "text", "text": f"{task_1} 完成紀錄", "size": "xs", "color": "#333333"}, 
                            {"type": "text", "text": f"{task_2} 記錄 2 餐", "size": "xs", "color": "#333333", "margin": "xs"}
                        ]},
                        {"type": "box", "layout": "vertical", "flex": 1, "contents": [
                            {"type": "text", "text": f"{task_3} 蛋白 80%", "size": "xs", "color": "#333333"}, 
                            {"type": "text", "text": f"{task_4} 今日運動", "size": "xs", "color": "#333333", "margin": "xs"}
                        ]}
                    ]}
                ]},
                {"type": "separator", "margin": "lg"},

                # ─── 5. 今日課表安排 (版面修正版) ───
                {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8FAFC", "paddingAll": "12px", "cornerRadius": "8px", "contents": [
                    {"type": "text", "text": f"🌟 今日課表 {today_date.strftime('%m/%d')}", "size": "sm", "weight": "bold", "color": "#EAB308"},
                    {"type": "text", "text": f"🚴 {today_workout}", "size": "md", "weight": "bold", "color": "#222222", "margin": "md", "wrap": True},
                    # 💡 修正 1：移除 flex 屬性，讓系統自動均分寬度
                    # 💡 修正 2：如果文字真的太長，將 layout 改成 vertical 讓按鈕上下排列（我們這裡先保留 horizontal，靠拿掉 flex 解決）
                    {"type": "box", "layout": "horizontal", "margin": "md", "spacing": "sm", "contents": [
                        {"type": "button", "action": {"type": "message", "label": "✅ 完美達標", "text": "✅ 今日完美達標"}, "style": "primary", "color": "#10B981", "height": "sm"},
                        {"type": "button", "action": {"type": "message", "label": "⚠️ 調整", "text": "⚠️ 今日調整"}, "style": "secondary", "height": "sm"}
                    ]}
                ]},

                # ─── 🌟 6. 昨日 / 明日 緊湊區 (加回來了！) ───
                {"type": "box", "layout": "horizontal", "margin": "lg", "spacing": "md", "contents": [
                    {"type": "box", "layout": "vertical", "flex": 1, "backgroundColor": "#FFF5F5", "paddingAll": "12px", "cornerRadius": "8px", "contents": [
                        {"type": "text", "text": f"📆 昨日 {yesterday.strftime('%m/%d')}", "size": "xs", "color": "#888888"},
                        {"type": "text", "text": "⚠️ 尚未回報", "size": "sm", "color": "#EF4444", "margin": "sm", "weight": "bold"},
                        {"type": "button", "action": {"type": "message", "label": "補登", "text": "✅ 昨日補登達標"}, "style": "primary", "color": "#F97316", "height": "sm", "margin": "md"}
                    ]},
                    {"type": "box", "layout": "vertical", "flex": 1, "backgroundColor": "#F1F5F9", "paddingAll": "12px", "cornerRadius": "8px", "contents": [
                        {"type": "text", "text": f"📆 明日 {tomorrow.strftime('%m/%d')}", "size": "xs", "color": "#888888"},
                        {"type": "text", "text": f"{tomorrow_workout[:10]}...", "size": "sm", "color": "#333333", "margin": "sm", "weight": "bold", "wrap": True},
                        {"type": "button", "action": {"type": "message", "label": "新增", "text": "新增運動"}, "style": "primary", "color": "#64748B", "height": "sm", "margin": "md"}
                    ]}
                ]}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [
                {"type": "button", "action": {"type": "message", "label": "📅 查看本週完整課表", "text": "本週完整課表"}, "style": "primary", "color": "#3B82F6", "height": "sm"}
            ]
        }
    }

    carousel = {"type": "carousel", "contents": [diet_bubble, sport_bubble]}
    return FlexSendMessage(alt_text="今日儀表板", contents=carousel)


def build_sport_carousel_flex(user_id: str):
    """訊息 2：三天輪播 Carousel（昨日·今日·明日），獨立訊息"""
    from linebot.models import FlexSendMessage
    from datetime import timedelta as _td
    d = get_dashboard_data(user_id)
    if not d: return None

    today_date = tw_today()
    yesterday = today_date - _td(days=1)
    tomorrow = today_date + _td(days=1)
    today_workout = d.get("today_workout", "待安排")

    def day_label(dt): return dt.strftime("%m/%d")

    yesterday_bubble = {
        "type": "bubble", "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"📆 昨日 {day_label(yesterday)}", "color": "#888888", "size": "sm"},
                {"type": "text", "text": "🏃 待查詢", "size": "md", "color": "#333333", "weight": "bold", "margin": "md", "wrap": True},
                {"type": "text", "text": "⚠️ 尚未回報", "size": "sm", "color": "#FF3B30", "margin": "sm"}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [
                {"type": "button", "action": {"type": "message", "label": "補登昨日狀態", "text": "✅ 昨日補登達標"}, "style": "secondary", "height": "sm"}
            ]
        }
    }

    today_bubble = {
        "type": "bubble", "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": "#F8FFF9",
            "contents": [
                {"type": "text", "text": f"🌟 今日 {day_label(today_date)}", "color": "#EAB308", "size": "sm", "weight": "bold"},
                {"type": "text", "text": f"🚴 {today_workout}", "size": "lg", "weight": "bold", "color": "#222222", "margin": "md", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [
                {"type": "button", "action": {"type": "message", "label": "✅ 完美達標 (+90 TSS)", "text": "✅ 今日完美達標"}, "style": "primary", "color": "#06C755", "height": "sm"},
                {"type": "button", "action": {"type": "message", "label": "⚠️ 調整狀況", "text": "⚠️ 今日調整"}, "style": "secondary", "height": "sm", "margin": "sm"}
            ]
        }
    }

    tomorrow_workout = "休息日"
    if d.get("future_days") and len(d["future_days"]) > 0:
        tomorrow_workout = d["future_days"][0].get("workout", "休息日")

    tomorrow_bubble = {
        "type": "bubble", "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"📆 明日 {day_label(tomorrow)}", "color": "#888888", "size": "sm"},
                {"type": "text", "text": f"😴 {tomorrow_workout}", "size": "md", "color": "#AAAAAA", "margin": "md", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "contents": [
                {"type": "button", "action": {"type": "message", "label": "✏️ 新增/修改運動", "text": "新增運動"}, "style": "link", "color": "#888888", "height": "sm"}
            ]
        }
    }

    carousel = {"type": "carousel", "contents": [yesterday_bubble, today_bubble, tomorrow_bubble]}
    return FlexSendMessage(alt_text="📅 運動專區", contents=carousel)
def build_meal_log_flex(
    logged_name, logged_cal, logged_pro, new_cal, tdee, new_pro, protein_goal,
    exchange_text="", exchange_review_status="pending_review", *, log_id="", version=0,
):
    """Task 2：記錄成功回饋卡"""
    cal_pct  = min(100, round(new_cal / tdee * 100)) if tdee > 0 else 0
    pro_pct  = min(100, round(new_pro / protein_goal * 100)) if protein_goal > 0 else 0
    cal_remaining = max(0, tdee - new_cal)
    pro_remaining = max(0, protein_goal - new_pro)

    def bar(pct, color):
        filled = max(1, min(99, pct))
        return {"type":"box","layout":"horizontal","contents":[
            {"type":"box","layout":"vertical","contents":[],"flex":filled,"backgroundColor":color,"height":"8px","cornerRadius":"4px"},
            {"type":"box","layout":"vertical","contents":[],"flex":100-filled,"backgroundColor":"#f0f0f0","height":"8px","cornerRadius":"4px"},
        ],"spacing":"none"}

    cal_text  = f"{logged_cal} kcal" if logged_cal is not None else "未知"
    pro_text  = f"{logged_pro} g"    if logged_pro is not None else "未知"
    if log_id and int(version or 0) > 0:
        edit_action = {
            "type": "postback", "label": "🍚 修正內容",
            "data": f"foodlog:v1:{log_id}:{int(version)}:more",
            "displayText": "修正這筆飲食紀錄",
        }
    else:
        edit_action = {"type": "message", "label": "🍚 修正內容", "text": "修正內容"}

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "16px",
            "backgroundColor": "#06C755",
            "contents": [
                {"type":"text","text":"✅ 記錄成功","color":"#ffffff","size":"lg","weight":"bold"},
                {"type":"text","text":logged_name,"color":"#ccffcc","size":"sm","margin":"xs"},
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "contents": [
                # 本次
                {"type":"box","layout":"horizontal","contents":[
                    {"type":"box","layout":"vertical","flex":1,"contents":[
                        {"type":"text","text":"本次熱量","size":"xs","color":"#888888"},
                        {"type":"text","text":cal_text,"size":"lg","weight":"bold","color":"#222222"},
                    ]},
                    {"type":"box","layout":"vertical","flex":1,"contents":[
                        {"type":"text","text":"本次蛋白","size":"xs","color":"#888888"},
                        {"type":"text","text":pro_text,"size":"lg","weight":"bold","color":"#222222"},
                    ]},
                ]},
                {"type":"separator","margin":"md"},
                # 今日累積
                {"type":"text","text":"🔥 今日熱量累積","size":"xs","color":"#888888","margin":"md"},
                {"type":"box","layout":"horizontal","margin":"sm","contents":[
                    {"type":"text","text":str(new_cal),"size":"xl","weight":"bold","color":"#222222"},
                    {"type":"text","text":f"/ {tdee} kcal","size":"xs","color":"#888888","align":"end","gravity":"bottom"},
                ]},
                bar(cal_pct, "#06C755"),
                {"type":"text","text":f"還剩 {cal_remaining} kcal","size":"xs","color":"#06C755","margin":"sm"},
                {"type":"separator","margin":"md"},
                {"type":"text","text":"🥩 今日蛋白累積","size":"xs","color":"#888888","margin":"md"},
                {"type":"box","layout":"horizontal","margin":"sm","contents":[
                    {"type":"text","text":str(new_pro),"size":"xl","weight":"bold","color":"#222222"},
                    {"type":"text","text":f"/ {protein_goal} g","size":"xs","color":"#888888","align":"end","gravity":"bottom"},
                ]},
                bar(pro_pct, "#FF6B35"),
                {"type":"text","text":f"還差 {pro_remaining} g","size":"xs","color":"#FF6B35","margin":"sm"},
            ]
        },
        "footer": {
            "type":"box","layout":"vertical","spacing":"sm","paddingAll":"16px",
            "contents": [
                {"type":"box","layout":"horizontal","spacing":"sm","contents":[
                    {"type":"button","action":{"type":"message","label":"✏️ 再記一餐","text":"記錄一餐"},"style":"primary","color":"#06C755","flex":1,"height":"sm"},
                    {"type":"button","action":{"type":"message","label":"🍽️ 推薦晚餐","text":"推薦晚餐"},"style":"secondary","flex":1,"height":"sm"},
                ]},
                {"type":"box","layout":"horizontal","spacing":"sm","contents":[
                    {"type":"button","action":edit_action,"style":"secondary","flex":1,"height":"sm"},
                    {"type":"button","action":{"type":"message","label":"📊 看今日進度","text":"首頁"},"style":"secondary","flex":1,"height":"sm"},
                ]},
            ]
        }
    }
    if exchange_text:
        exchange_note = (
            f"正式營養份數：{exchange_text}\n已納入個人計畫"
            if exchange_review_status == "approved"
            else f"推算營養份數：{exchange_text}\n待營養師審核，尚未扣入個人計畫"
        )
        bubble["body"]["contents"].append({
            "type": "text",
            "text": exchange_note,
            "size": "xs",
            "color": "#8A6D3B",
            "margin": "md",
            "wrap": True,
        })
    from linebot.models import FlexSendMessage
    return FlexSendMessage(alt_text=f"✅ 已記錄：{logged_name}", contents=bubble)



def parse_log_nutrition_tag(ans: str):
    patterns = [
        r'\[LOG_NUTRITION:\s*CAL\s*=\s*([^,\]]+)\s*,\s*PRO\s*=\s*([^,\]]+)\s*,\s*NAME\s*=\s*(.+?)\]',
        r'\[LOG_NUTRITION:\s*([^,\]]+)\s*,\s*([^,\]]+)\s*,\s*(.+?)\]',
    ]

    def _parse_numeric_or_unknown(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        if raw.upper() in ["UNKNOWN", "未知", "UNK", "NONE", "N/A"]:
            return None
        cleaned = re.sub(r'[^\d-]', '', raw)
        return int(cleaned) if cleaned not in ["", "-"] else None

    for pattern in patterns:
        match = re.search(pattern, ans, re.IGNORECASE)
        if match:
            cal_raw = match.group(1).strip()
            pro_raw = match.group(2).strip()
            name_raw = match.group(3).strip()
            return {
                "source": "tag",
                "match": match,
                "cal": _parse_numeric_or_unknown(cal_raw),
                "pro": _parse_numeric_or_unknown(pro_raw),
                "name": name_raw.replace("大卡", "").replace("克", "").strip(),
            }
    return None


def parse_log_nutrition_fallback(ans: str, user_msg: str = ""):
    """當 AI 沒吐 LOG_NUTRITION tag 時，從一般文字回覆中盡量抓出記錄資訊。"""
    def _parse_num(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        if any(x in raw for x in ["未知", "UNKNOWN", "N/A"]):
            return None
        m = re.search(r'(\d+)', raw.replace(',', ''))
        return int(m.group(1)) if m else None

    name = None
    cal = None
    pro = None

    patterns_name = [
        r'📝\s*品項[:：]\s*(.+)',
        r'品項[:：]\s*(.+)',
        r'已記錄[:：]\s*(.+)',
        r'記錄[:：]\s*(.+)',
    ]
    for pattern in patterns_name:
        m = re.search(pattern, ans, re.IGNORECASE)
        if m:
            name = m.group(1).strip().splitlines()[0].strip()
            break

    m_cal = re.search(r'本次熱量[:：]\s*([^\r\n]+)', ans, re.IGNORECASE)
    if m_cal:
        cal = _parse_num(m_cal.group(1))

    m_pro = re.search(r'本次蛋白(?:質)?[:：]\s*([^\r\n]+)', ans, re.IGNORECASE)
    if m_pro:
        pro = _parse_num(m_pro.group(1))

    if not name:
        guess = user_msg.strip()
        guess = re.sub(r'^(我(今天)?(吃了|喝了)|今天(吃了|喝了)|吃了|喝了|早餐|午餐|晚餐)[：: ]*', '', guess)
        guess = guess.strip('。！？,.， ')
        if guess:
            name = guess

    if name and (cal is not None or pro is not None):
        return {
            "source": "fallback",
            "match": None,
            "cal": cal,
            "pro": pro,
            "name": name,
        }
    return None


def parse_ai_estimate_range_fallback(ans: str, user_msg: str):
    """Turn an explicitly confirmed AI range into one deterministic midpoint record."""
    if not should_ai_create_food_log(user_msg):
        return None

    def _midpoint(pattern):
        match = re.search(pattern, str(ans or ""), re.IGNORECASE)
        if not match:
            return None
        low = float(match.group(1))
        high = float(match.group(2) or low)
        if low <= 0 or high < low:
            return None
        return int((low + high) / 2 + 0.5)

    range_separator = r"(?:[~～\-–—]|到|至)"
    cal = _midpoint(
        rf"(?:本次)?熱量\s*(?:[:：]\s*)?(?:約\s*)?"
        rf"(\d+(?:\.\d+)?)\s*{range_separator}?\s*"
        rf"(\d+(?:\.\d+)?)?\s*(?:大卡|卡|kcal)"
    )
    pro = _midpoint(
        rf"(?:本次)?蛋白(?:質)?\s*(?:[:：]\s*)?(?:約\s*)?"
        rf"(\d+(?:\.\d+)?)\s*{range_separator}?\s*"
        rf"(\d+(?:\.\d+)?)?\s*(?:克|g)"
    )
    if cal is None and pro is None:
        return None
    name = re.sub(
        r"^請用一般估算記錄\s+(?:(?:早餐|午餐|晚餐|點心)\s+)?",
        "", str(user_msg or "").strip(),
    ).strip(" ：:，,。.!！")
    if not name:
        return None
    return {
        "source": "range_fallback", "match": None,
        "cal": cal, "pro": pro, "name": name,
    }


def should_ai_create_food_log(user_msg):
    """Only the explicit fallback confirmation may let the legacy AI write a food log."""
    return bool(re.match(
        r"^請用一般估算記錄\s+\S+", str(user_msg or "").strip()
    ))


def ai_estimate_meal_slot(user_msg):
    match = re.match(
        r"^請用一般估算記錄\s+(早餐|午餐|晚餐|點心)\s+",
        str(user_msg or "").strip(),
    )
    return match.group(1) if match else ""


def load_ai_estimate_replay(user_id, operation_key):
    operation_key = str(operation_key or "").strip()[:180]
    user_id = str(user_id or "").strip()
    if not operation_key or not user_id:
        return None
    def _flex_from_payload(payload):
        if not isinstance(payload, dict) or payload.get("type") != "flex":
            raise ValueError("invalid replay flex")
        from linebot.models import FlexSendMessage
        return FlexSendMessage(
            alt_text=payload["altText"], contents=payload["contents"]
        )
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        row = conn.execute(
            """SELECT result_json FROM daily_food_log_events
               WHERE event_id=? AND user_id=? AND action='ai_estimate_log'""",
            (operation_key, user_id),
        ).fetchone()
        if row:
            try:
                state = json.loads(row[0] or "{}")
                payload = state["flex"]
                return _flex_from_payload(payload)
            except (KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError):
                pass
        snapshot = conn.execute(
            """SELECT log_id,flex_json FROM ai_food_log_replay_snapshots
               WHERE operation_key=? AND user_id=?""",
            (operation_key, user_id),
        ).fetchone()
        if snapshot:
            try:
                payload = json.loads(snapshot[1] or "{}")
                replay = _flex_from_payload(payload)
                state_json = json.dumps(
                    {"kind": "ai_estimate_log", "flex": payload},
                    ensure_ascii=False, separators=(",", ":"),
                )
                conn.execute(
                    """INSERT INTO daily_food_log_events
                       (event_id,user_id,log_id,action,result_json,created_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET
                         user_id=excluded.user_id,log_id=excluded.log_id,
                         action=excluded.action,result_json=excluded.result_json,
                         created_at=excluded.created_at""",
                    (operation_key, user_id, snapshot[0], "ai_estimate_log", state_json,
                     tw_now().isoformat(timespec="seconds")),
                )
                conn.commit()
                return replay
            except (KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError):
                pass
        log_row = conn.execute(
            """SELECT fl.log_id,fl.version,fl.nutrition_snapshot_json,fc.product_name
               FROM food_logs fl JOIN food_catalog fc ON fc.food_id=fl.food_id
               WHERE fl.operation_key=? AND fl.user_id=?
                 AND fl.confirmation_status='confirmed' AND COALESCE(fl.deleted_at,'')=''""",
            (operation_key, user_id),
        ).fetchone()
        if not log_row:
            return None
        nutrition = json.loads(log_row[2] or "{}")
        hp = conn.execute(
            """SELECT today_extra_cal,tdee,today_extra_pro,protein
               FROM health_profile WHERE user_id=?""", (user_id,)
        ).fetchone() or (0, 2000, 0, 100)
        replay = build_meal_log_flex(
            log_row[3], nutrition.get("calories_kcal"), nutrition.get("protein_g"),
            hp[0] or 0, hp[1] or 2000, hp[2] or 0, hp[3] or 100,
            log_id=log_row[0], version=int(log_row[1] or 1),
        )
        replay_state = json.dumps(
            {"kind": "ai_estimate_log", "flex": json.loads(replay.as_json_string())},
            ensure_ascii=False, separators=(",", ":"),
        )
        conn.execute(
            """INSERT INTO daily_food_log_events
               (event_id,user_id,log_id,action,result_json,created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 user_id=excluded.user_id,log_id=excluded.log_id,
                 action=excluded.action,result_json=excluded.result_json,
                 created_at=excluded.created_at""",
            (operation_key, user_id, log_row[0], "ai_estimate_log", replay_state,
             tw_now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return replay


def claim_ai_estimate_request(user_id, operation_key, lease_seconds=120):
    operation_key = str(operation_key or "").strip()[:180]
    user_id = str(user_id or "").strip()
    if not operation_key or not user_id:
        return "invalid"
    now = tw_now()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        ensure_daily_food_ledger_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id,action,created_at FROM daily_food_log_events WHERE event_id=?",
            (operation_key,),
        ).fetchone()
        if row:
            if row[0] != user_id:
                conn.rollback()
                return "blocked"
            if row[1] == "ai_estimate_log":
                conn.commit()
                return "complete"
            if row[1] == "ai_estimate_pending":
                conn.commit()
                return "pending"
        conn.execute(
            """INSERT INTO daily_food_log_events
               (event_id,user_id,log_id,action,result_json,created_at)
               VALUES (?,?,'','ai_estimate_pending','{}',?)
               ON CONFLICT(event_id) DO UPDATE SET
                 user_id=excluded.user_id,log_id='',action='ai_estimate_pending',
                 result_json='{}',created_at=excluded.created_at""",
            (operation_key, user_id, now.isoformat(timespec="seconds")),
        )
        conn.commit()
        return "claimed"


def fail_ai_estimate_request(user_id, operation_key, *, refund_quota):
    user_id = str(user_id or "").strip()
    operation_key = str(operation_key or "")[:180]
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """DELETE FROM daily_food_log_events
               WHERE event_id=? AND user_id=? AND action='ai_estimate_pending'""",
            (operation_key, user_id),
        )
        if refund_quota:
            conn.execute(
                """UPDATE usage
                   SET remaining_chat_quota=MIN(
                       COALESCE(daily_chat_limit,remaining_chat_quota+1),
                       remaining_chat_quota+1
                   )
                   WHERE user_id=?""",
                (user_id,),
            )
        conn.commit()


def release_ai_estimate_claim(user_id, operation_key):
    fail_ai_estimate_request(user_id, operation_key, refund_quota=False)


def get_ai_response_with_memory(user_id, user_msg, operation_key=""):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    ensure_daily_food_ledger_schema(conn)
    
    # 抓取客人資料
    c.execute("SELECT summary_text, tdee, active_days, protein, user_level, race_date FROM health_profile WHERE user_id=?", (user_id,))
    hp = c.fetchone()
    
    today_str = tw_today().isoformat()
    # 抓取今日外食紀錄 (多抓 today_food_items)
    c.execute("SELECT today_extra_cal, today_date, sheet_name, name, today_extra_pro, today_food_items FROM health_profile WHERE user_id=?", (user_id,))
    daily_rec = c.fetchone()
    
    # 判斷是不是新的一天，如果是就歸零 (包含食物清單)
    if daily_rec and daily_rec[1] != today_str:
        c.execute("UPDATE health_profile SET today_extra_cal=0, today_extra_pro=0, today_food_items='', today_date=? WHERE user_id=?", (today_str, user_id))
        conn.commit() 
        extra_cal, extra_pro, food_items = 0, 0, ""
    else:
        extra_cal = daily_rec[0] if daily_rec else 0
        extra_pro = daily_rec[4] if (daily_rec and len(daily_rec) > 4 and daily_rec[4] is not None) else 0
        food_items = daily_rec[5] if (daily_rec and len(daily_rec) > 5 and daily_rec[5] is not None) else ""

    report = f"\n【絕對參考報告內容】:\n{hp[0]}" if hp else "\n檔案未填，請引導客人填表。"
    tdee_val = hp[1] if hp else 2000
    active_days = hp[2] if hp else ""
    protein_val = hp[3] if hp else 100
    history = user_memory.get(user_id, [])[-6:]
    ingredients_memo = "\n".join([f"- {d['name']}|{d.get('cal',0)}kcal|蛋白{d.get('pro',0)}g|{d.get('ingredients','無資料')}" for d in MAIN_DISHES])
    
    food_items_text = food_items if food_items else "無"
    
    # === 新增：從 Google Sheets 讀取當天排餐 ===
    today_date_str = tw_today().strftime("%Y/%m/%d")
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    today_str_zh = weekdays[tw_today().weekday()]

    # 🌟 1. 預設空值 (加入運動變數)
    today_lunch, today_dinner = "無", "無"
    today_workout, tomorrow_workout = "無", "無" # 新增用來存運動的變數

    # 🌟 2. 取得今日與明日的日期字串 (嚴格比對用)
    today_date_str = tw_today().strftime("%Y/%m/%d")
    tomorrow_date_str = (tw_today() + timedelta(days=1)).strftime("%Y/%m/%d")

    user_sheet_name = daily_rec[2] if daily_rec and len(daily_rec) > 2 else ""

    if gc and user_sheet_name:
        try:
            # 去個人的專屬分頁找排餐與運動
            book = gc.open_by_key(SPREADSHEET_ID)
            user_sheet, resolved_sheet_name, resolved_source = get_or_create_user_sheet(book, user_id, daily_rec[3] if daily_rec else "", user_sheet_name)
            if user_sheet:
                if resolved_sheet_name != user_sheet_name:
                    user_sheet_name = resolved_sheet_name
                    print(f"ℹ️ 對話引擎已校正 sheet_name：{user_id} -> {resolved_sheet_name} ({resolved_source})")

                all_rows = get_user_sheet_rows(user_sheet)
                
                for row in all_rows:
                    row_date = _pick_first(row, ["日期", "實際日期", "取餐日期", "Date"], "")
                    
                    # 🎯 比對今天：抓午晚餐 + 今日的 Sport_Type
                    if normalize_date_str(row_date) == normalize_date_str(today_date_str):
                        today_lunch = _pick_first(row, ["午餐", "午餐安排", "Lunch_Item"], "無")
                        today_dinner = _pick_first(row, ["晚餐", "晚餐安排", "Dinner_Item"], "無")
                        today_workout = _pick_first(row, ["Sport_Type", "運動強度", "運動", "Workout"], "無")
                        
                    # 🎯 比對明天：抓明日的 Sport_Type
                    elif normalize_date_str(row_date) == normalize_date_str(tomorrow_date_str):
                        tomorrow_workout = _pick_first(row, ["Sport_Type", "運動強度", "運動", "Workout"], "無")
                        
        except Exception as e:
            print(f"⚠️ 讀取當天排餐/運動失敗: {e}")

    # 週期化飲食：從 DB 讀取用戶等級與賽事日期
    user_level = hp[4] if (hp and hp[4] is not None) else 2
    race_date_str = hp[5] if (hp and hp[5]) else "無"
    mode, countdown = calculate_carb_cycle(user_level, race_date_str)

    # 判斷是否有排餐
    has_meal_today = (today_lunch != "無" or today_dinner != "無")

    if has_meal_today:
        today_status = f"✅ 今天 ({today_str_zh}) 是您的【取餐日】\n🍱 午餐：{today_lunch}\n🌙 晚餐：{today_dinner}"
        base_cal_text = "【報告上的『當日熱量剩餘』】"
        base_pro_text = "【報告上的『蛋白質需補』】"
    else:
        # 把原本的 ❌ 改成 🎉，讓無排餐日的氛圍更歡樂
        today_status = f"🎉 今天 ({today_str_zh}) 是您的【無排餐日】，擁有今日完整額度喔！"
        base_cal_text = str(int(tdee_val))
        base_pro_text = str(int(protein_val))

    system_prompt = f"""你是「一日樂食（Deli Express）」的專屬飲食紀錄助理。

【回覆風格最高指導原則】
🚨 請用精簡、口語化、有活力的台灣中文回覆。
🚨 廢話少說！給客人的文字務必控制在 100 字以內！多用條列式或 Emoji，絕對不要過度客氣、不要囉嗦、不要講冗長的寒暄廢話！

【目前系統狀態】
- 今天狀態：{today_status}
- 初始可用熱量：{base_cal_text} 大卡
- 初始可用蛋白：{base_pro_text} 克
- 今日運動紀錄：{today_workout}
- 明日運動預定：{tomorrow_workout}
- 稍早已吃外食熱量：{extra_cal} 大卡（今日已吃清單：{food_items_text}）
- 稍早已吃外食蛋白：{extra_pro} 克
- 客人等級：Level {user_level}
- 賽事倒數：{"距離目標賽事還有 " + str(countdown) + " 天" if countdown is not None else "目前無特定賽事"}
- 今日飲食方針：{mode}

【核心守則（嚴格遵守）】
1. 若顧客輸入的食物存在於本店資料庫 / 菜單，必須 100% 使用資料庫數值，不可改寫、補值或猜測。
2. 一般食物問句只回答，不可寫入正式紀錄；只有訊息以「請用一般估算記錄」開頭時，才估算並記錄。
3. 若顧客明確表示是外部食物，或該食物不存在於本店資料庫，可用常識估算，但：
   - 不可給假精準到個位數的數字
   - 應優先給範圍
   - 若最後要寫入正式紀錄，只有在顧客給了明確數字，或你能合理判斷為單一常見份量時，才可寫入熱量；蛋白質若不確定可標記 UNKNOWN
4. 若顧客只提供單一營養素，不可腦補另一個。
   - 例：『早餐 500 卡』→ 可以記熱量 500，但蛋白質必須 UNKNOWN
   - 例：『乳清蛋白 30g』→ 可以記蛋白質 30，但熱量必須 UNKNOWN
5. 若資訊不足，不要硬結算；先問清楚。
6. 若顧客描述份量（半份、兩顆、一半飯），應以單份數值做數學換算。
7. 只有顧客已明確點選「使用一般估算」，訊息以「請用一般估算記錄」開頭時，才可加 LOG_NUTRITION 並正式記錄。
8. 一般詢問若出現範圍值（例如 300~400 卡、約 25-30g 蛋白），不要寫入正式紀錄。但若訊息以「請用一般估算記錄」開頭，代表顧客已按下確認按鈕：必須取範圍中間值、四捨五入成單一合理估算值，立即加入 LOG_NUTRITION 並正式記錄，不可再反問要記哪個值。
9. 若顧客訊息同時包含多個食物，但每個食物資訊不足，先拆開詢問最關鍵缺口，不要把多樣食物合併成單一筆紀錄。

【回覆格式】
- 先自然回應一句 (不要超過 15 個字)
- 若可結算，再用條列：
  - 📝 品項：...
  - 🔥 本次熱量：...
  - 🥩 本次蛋白：...
  - 🔥 今日熱量結算：...
  - 🥩 今日蛋白結算：...
- 若有不確定值，清楚標示「未知」或「預估」
- 若是外部食物估算，最後加上：
  *(此為一般預估，實際熱量依店家有異)*

【隱藏 tag 規則 - 非常重要，必須嚴格遵守】
- 只有當用戶訊息以「請用一般估算記錄」開頭，才可在回覆最末尾加：
  [LOG_NUTRITION: CAL=數字或UNKNOWN, PRO=數字或UNKNOWN, NAME=品項名稱]
- 一般食物問句、營養查詢或確認問題禁止加 LOG_NUTRITION，也禁止當成正式飲食紀錄。
  【🚨 自助改單最高指令 🚨】
    如果顧客明確要求「把A天的某餐，換成B天的某餐」（例如：把週一午餐換成週三晚餐）：
    請在回覆的最結尾加上隱藏標籤：[SWAP_MEAL: 週一_午餐, 週三_晚餐]
    (注意：日期只能填簡稱如「週一」，時段只能填「午餐」或「晚餐」)


{report}

【本店餐點內容物 - 內部參考】
{ingredients_memo}
"""
    
    try:
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_msg}]
        res = client.chat.completions.create(model="gpt-4o-mini", messages=messages, max_tokens=2000, temperature=0.3)
        ans = res.choices[0].message.content
    except Exception as e:
        print(f"⚠️ AI 大腦呼叫失敗：{e}")
        return ("⚠️ AI 助理暫時忙碌中，請稍後再試；若持續發生請聯繫客服。", None)
        
    meal_log_flex = None
    parsed_tag = (
        parse_log_nutrition_tag(ans)
        or parse_log_nutrition_fallback(ans, user_msg)
        or parse_ai_estimate_range_fallback(ans, user_msg)
    )
    
    if parsed_tag and should_ai_create_food_log(user_msg):
        try:
            logged_cal = parsed_tag["cal"]
            logged_pro = parsed_tag["pro"]
            logged_name = parsed_tag["name"]

            # 至少要有品項，且熱量/蛋白至少有一個抓到，才視為有效記錄
            if logged_name and (logged_cal is not None or logged_pro is not None):
                meal_slot = ai_estimate_meal_slot(user_msg)
                if not meal_slot:
                    meal_hour = tw_now().hour
                    meal_slot = "早餐" if meal_hour < 11 else ("午餐" if meal_hour < 15 else ("晚餐" if meal_hour < 21 else "點心"))
                daily_log = create_daily_food_log(
                    conn, user_id=user_id, product_name=logged_name,
                    meal_slot=meal_slot, consumed_at=tw_now().isoformat(timespec="seconds"),
                    servings=1,
                    nutrition={"calories_kcal": logged_cal, "protein_g": logged_pro},
                    source_type="ai_text_estimate", operation_key=operation_key,
                )
                if daily_log.get("replayed"):
                    logged_name = daily_log["product_name"]
                    logged_cal = daily_log["nutrition"].get("calories_kcal")
                    logged_pro = daily_log["nutrition"].get("protein_g")
                logged_cal = float(logged_cal) if logged_cal is not None else None
                logged_pro = float(logged_pro) if logged_pro is not None else None
                _sync_health_profile_from_ledger_conn(conn, user_id, tw_today().isoformat())
                c.execute(
                    "SELECT today_extra_cal,today_extra_pro,today_food_items FROM health_profile WHERE user_id=?",
                    (user_id,),
                )
                projected = c.fetchone() or (0, 0, "")
                new_extra_cal, new_extra_pro, new_food_items = (
                    projected[0] or 0, projected[1] or 0, projected[2] or "",
                )
                
                # 隱藏 tag 會在此區塊後統一移除。

                # 存最近一筆，供 Task 3 份量修正使用
                try:
                    c.execute("""
                        INSERT INTO recent_meal_logs (user_id, meal_name, base_cal, base_pro, current_cal, current_pro, meal_date, source_text, updated_at, food_log_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            meal_name=excluded.meal_name,
                            base_cal=excluded.base_cal,
                            base_pro=excluded.base_pro,
                            current_cal=excluded.current_cal,
                            current_pro=excluded.current_pro,
                            meal_date=excluded.meal_date,
                            source_text=excluded.source_text,
                            updated_at=excluded.updated_at,
                            food_log_id=excluded.food_log_id
                    """, (user_id, logged_name, logged_cal, logged_pro, logged_cal, logged_pro, tw_today().isoformat(), user_msg, tw_now().isoformat(), daily_log["log_id"]))
                except Exception as _se:
                    print(f'⚠️ 最近一筆記錄保存失敗: {_se}')
                    raise

                # Task 2: 組記錄成功回饋卡
                try:
                    version_row = c.execute(
                        "SELECT version FROM food_logs WHERE log_id=? AND user_id=?",
                        (daily_log["log_id"], user_id),
                    ).fetchone()
                    log_version = int(version_row[0] or 1) if version_row else 1
                    meal_log_flex = build_meal_log_flex(
                        logged_name, logged_cal, logged_pro,
                        new_extra_cal, tdee_val, new_extra_pro, protein_val,
                        log_id=daily_log["log_id"], version=log_version,
                    )
                except Exception as _fe:
                    print(f'⚠️ 回饋卡組建失敗: {_fe}')
                    from linebot.models import FlexSendMessage
                    meal_log_flex = FlexSendMessage(
                        alt_text=f"✅ 已記錄：{logged_name}",
                        contents={
                            "type": "bubble", "size": "kilo",
                            "body": {"type": "box", "layout": "vertical", "contents": [
                                {"type": "text", "text": "✅ 記錄成功", "weight": "bold", "size": "xl"},
                                {"type": "text", "text": logged_name, "wrap": True, "margin": "md"},
                                {"type": "text", "text": f"熱量 {logged_cal if logged_cal is not None else '未知'} kcal／蛋白質 {logged_pro if logged_pro is not None else '未知'} g", "wrap": True, "size": "sm", "margin": "sm"},
                            ]},
                            "footer": {"type": "box", "layout": "vertical", "contents": [
                                {"type": "button", "style": "primary", "action": {
                                    "type": "postback", "label": "修正內容",
                                    "data": f"foodlog:v1:{daily_log['log_id']}:{int(daily_log.get('version') or 1)}:more",
                                    "displayText": "修正這筆飲食紀錄",
                                }}
                            ]},
                        },
                    )

                if meal_log_flex is not None and operation_key:
                    flex_payload = json.loads(meal_log_flex.as_json_string())
                    replay_state = json.dumps(
                        {
                            "kind": "ai_estimate_log",
                            "flex": flex_payload,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    created_at = tw_now().isoformat(timespec="seconds")
                    c.execute(
                        """INSERT OR IGNORE INTO ai_food_log_replay_snapshots
                           (operation_key,user_id,log_id,flex_json,created_at)
                           VALUES (?,?,?,?,?)""",
                        (
                            str(operation_key)[:180], user_id, daily_log["log_id"],
                            json.dumps(flex_payload, ensure_ascii=False, separators=(",", ":")),
                            created_at,
                        ),
                    )
                    c.execute(
                        """INSERT INTO daily_food_log_events
                           (event_id,user_id,log_id,action,result_json,created_at)
                           VALUES (?,?,?,?,?,?)
                           ON CONFLICT(event_id) DO UPDATE SET
                             user_id=excluded.user_id,log_id=excluded.log_id,
                             action=excluded.action,result_json=excluded.result_json,
                             created_at=excluded.created_at""",
                        (
                            str(operation_key)[:180], user_id, daily_log["log_id"],
                            "ai_estimate_log", replay_state, created_at,
                        ),
                    )
                conn.commit()

                if parsed_tag.get("source") == "range_fallback":
                    ans = (
                        f"✅ 已用一般估算記錄：{logged_name}\n"
                        f"🔥 熱量 {logged_cal if logged_cal is not None else '未知'} 大卡｜"
                        f"🥩 蛋白質 {logged_pro if logged_pro is not None else '未知'} 克"
                    )

                # 核心 ledger 與 replay state 已提交後才執行非核心 mirror。
                if not daily_log.get("replayed") and len(logged_name) <= 15:
                    try:
                        upsert_frequent_food(user_id, logged_name, logged_cal, logged_pro)
                    except Exception as _ff:
                        print(f"⚠️ 常吃清單同步失敗: {_ff}")
                
                # 寫入 Google Sheet
                if not daily_log.get("replayed") and daily_rec and daily_rec[2] and gc:
                    try:
                        sheet = gc.open_by_url(SHEET_URL)
                        now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
                        cal_text = f"+{logged_cal} kcal" if logged_cal is not None else "+熱量未知"
                        pro_text = f"+{logged_pro} g" if logged_pro is not None else "+蛋白未知"
                        note = f"{cal_text} / {pro_text} ({logged_name})"
                        if parsed_tag.get("source") == "fallback":
                            note += " [fallback]"
                        sheet.worksheet(daily_rec[2]).append_row([now_str, "外食熱量與蛋白打卡", user_msg, note])
                    except Exception:
                        pass
        except Exception as e:
            conn.rollback()
            meal_log_flex = None
            print(f"❌ 標籤解析存入失敗: {e}")

    # 防止模型未遵守提示詞時，把隱藏記錄標籤顯示給一般問句使用者。
    ans = re.sub(
        r'\[LOG_NUTRITION:.*?\]', '', str(ans or ''), flags=re.IGNORECASE
    ).strip()

    match_swap = re.search(r'\[SWAP_MEAL:\s*(.+?)_(.+?),\s*(.+?)_(.+?)\]', ans)
    if match_swap:
        d1, m1, d2, m2 = match_swap.groups()
        
        # 1. 呼叫剛才步驟 2 寫好的換餐函數 (直接操作 Google Sheet)
        swap_result_msg = execute_meal_swap(user_id, d1.strip(), m1.strip(), d2.strip(), m2.strip())
        
        # 2. 把 AI 生成的隱藏標籤清掉，並補上實際的結果回傳給客人
        ans = re.sub(r'\[SWAP_MEAL:.*?\]', '', ans).strip()
        ans += f"\n\n🤖 系統操作結果：\n{swap_result_msg}"
        
        # 3. (老闆監控通知) 如果換餐成功，還是傳個 LINE 讓老闆知道一下
        if "✅ 成功" in swap_result_msg:
            c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
            admin_row = c.fetchone()
            if admin_row:
                customer_name = daily_rec[3] if daily_rec else "顧客"
                boss_msg = f"🤖【系統自動換餐通知】\n顧客 {customer_name} 透過 AI 成功將 {d1}{m1} 與 {d2}{m2} 互換囉！\n(系統已自動更新表單，等待定時出單機列印)"
                try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=boss_msg))
                except Exception: pass


# 🔥 偵測呼叫老闆訊號 [CALL_BOSS]
    match_call_boss = re.search(r'\[CALL_BOSS\]', ans)
    if match_call_boss:
        ans = ans.replace('[CALL_BOSS]', '').strip()
        ans += "\n\n(系統提示：已為您暫停 AI 助理，並通知真人客服，請稍候我們會盡快回覆您！)"
        
        # 設定靜音 24 小時
        silence_time = (tw_now() + timedelta(hours=24)).isoformat()
        c.execute("UPDATE health_profile SET ai_silenced_until=? WHERE user_id=?", (silence_time, user_id))
        conn.commit()

        # 發送求救訊號給老闆
        c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
        admin_row = c.fetchone()
        if admin_row:
            customer_name = daily_rec[3] if daily_rec else "顧客"
            boss_msg = f"🚨【客服呼叫】顧客 {customer_name} ({user_id[-4:]}) 需要真人協助！\nAI 已自動暫停 24 小時，請至 LINE 官方帳號處理。"
            try: 
                line_bot_api.push_message(admin_row[0], TextSendMessage(text=boss_msg))
            except Exception: 
                pass

    conn.close()
    user_memory[user_id] = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": ans}]
    return (ans, meal_log_flex)


# ==========================================
# 6. 其他輔助函數與 Webhook (🔥 融合版：完整保留測距、VIP功能)
# ==========================================
def check_permission_and_quota(user_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        today = tw_today().isoformat()
        c.execute("SELECT remaining_chat_quota, remaining_meals, last_date, status, expiry_date, daily_chat_limit FROM usage WHERE user_id=?", (user_id,))
        record = c.fetchone()
        if record is None:
            return False, ""
        q, m, ld, s, ed, dcl = record
        if ed and today > ed:
            return False, ""
        if m is not None and int(m) <= 0:
            return False, ""
        if ld != today:
            q = dcl
        if q > 0:
            c.execute("UPDATE usage SET remaining_chat_quota=?, last_date=? WHERE user_id=?", (q-1, today, user_id))
            conn.commit()
            return True, f"(剩{m}餐 | 諮詢:{q-1})"
        return False, f"(剩{m}餐 | 諮詢:0)"

def send_tomorrow_reminders():
    tomorrow = tw_today() + timedelta(days=1)
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    tomorrow_str = weekdays[tomorrow.weekday()]
    
    users = []
    try:
        # ✅ 安全連線
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{tomorrow_str}%",))
            users = c.fetchall()
    except Exception as e:
        return f"⚠️ 讀取資料庫失敗：{e}"

    count = 0
    for uid, name in users:
        msg = f"🌙 {name} 晚安！\n明天 ({tomorrow_str}) 是您的專屬取餐日喔！\n\n💪 營養師溫馨提醒：\n為確保您的營養達標，明天需要幫您額外準備【舒肥雞胸肉】或【無糖豆漿】來補足蛋白質缺口嗎？\n(直接回覆需要的品項，店長明天就會幫您準備好！)"
        try: 
            line_bot_api.push_message(uid, TextSendMessage(text=msg))
            count += 1
        except Exception: 
            pass
            
    return f"✅ 成功發送了 {count} 封明日取餐提醒推播！"

def get_distance(origin_address, target_address, mode="driving"):
    if not GOOGLE_MAPS_API_KEY:
        print("⚠️ Google Maps 測距失敗：環境變數 GOOGLE_MAPS_API_KEY 未設定。")
        return False, "", 0, ""
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {"origins": origin_address, "destinations": target_address, "mode": mode, "language": "zh-TW", "key": GOOGLE_MAPS_API_KEY}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("status") == "OK":
            element = data["rows"][0]["elements"][0]
            if element.get("status") == "OK":
                return True, element["distance"]["text"], element["distance"]["value"], element["duration"]["text"]
            print(f"⚠️ Google Maps element status: {element.get('status')}｜origin={origin_address}｜target={target_address}")
        else:
            print(f"⚠️ Google Maps API status: {data.get('status')}｜error={data.get('error_message', '')}")
        return False, "", 0, ""
    except Exception as e:
        print(f"⚠️ Google Maps 測距例外：{e}")
        return False, "", 0, ""


def infer_route_group(address: str) -> str:
    address = str(address or "").strip()
    district_map = {
        "WEST": ["萬華", "中正", "大同"],
        "EAST": ["信義", "南港", "內湖"],
        "SOUTH": ["大安", "文山", "新店", "永和", "中和"],
        "NORTH": ["士林", "北投", "中山", "松山"],
    }
    for group, keywords in district_map.items():
        if any(keyword in address for keyword in keywords):
            return group
    return "OTHER"


def calculate_delivery_quote(target_address: str):
    target_address = str(target_address or "").strip()
    if not target_address:
        return {
            "success": False,
            "delivery_available": None,
            "address": "",
            "distance_text": "",
            "distance_meters": 0,
            "duration_text": "",
            "delivery_fee": 0,
            "delivery_fee_text": "未提供地址",
            "hub_name": "",
            "route_group": "OTHER",
            "delivery_zone": "未分類",
            "carpool_hint": "",
        }

    success, dist_text, dist_meters, duration_text = get_distance(STORE_ADDRESS, target_address)
    if not success:
        return {
            "success": False,
            "delivery_available": None,
            "address": target_address,
            "distance_text": "",
            "distance_meters": 0,
            "duration_text": "",
            "delivery_fee": 0,
            "delivery_fee_text": "地址查詢失敗",
            "hub_name": "",
            "route_group": infer_route_group(target_address),
            "delivery_zone": "未分類",
            "carpool_hint": "",
        }

    hub_match = None
    if dist_meters <= 1000:
        delivery_fee = 20
        delivery_zone = "KM_0_1"
        delivery_available = True
    elif dist_meters <= 1500:
        delivery_fee = 25
        delivery_zone = "KM_1_1_5"
        delivery_available = True
    elif dist_meters <= 2000:
        delivery_fee = 30
        delivery_zone = "KM_1_5_2"
        delivery_available = True
    elif dist_meters <= 2500:
        delivery_fee = 40
        delivery_zone = "KM_2_2_5"
        delivery_available = True
    elif dist_meters <= 3000:
        delivery_fee = 50
        delivery_zone = "KM_2_5_3"
        delivery_available = True
    else:
        delivery_fee = 0
        delivery_zone = "UNAVAILABLE"
        delivery_available = False

    delivery_fee_text = (
        f"{delivery_fee} 元"
        if delivery_available
        else "超過 3 公里，暫不提供外送"
    )

    route_group = infer_route_group(target_address)
    carpool_hint = f"若同配送日有 {route_group} 同路客戶，可再套用順風車折扣。" if route_group != "OTHER" else "若有同區域併單，可再人工調整順風車折扣。"

    return {
        "success": True,
        "delivery_available": delivery_available,
        "address": target_address,
        "distance_text": dist_text,
        "distance_meters": dist_meters,
        "duration_text": duration_text,
        "delivery_fee": delivery_fee,
        "delivery_fee_text": delivery_fee_text,
        "hub_name": hub_match or "",
        "route_group": route_group,
        "delivery_zone": delivery_zone,
        "carpool_hint": carpool_hint,
    }


def get_subscription_form_link(uid: str) -> str:
    return f"https://docs.google.com/forms/d/e/1FAIpQLSfblmRmSc669n_C7JU1wja0g4KrEGs1oRQwdq6cfNCC8b1DFA/viewform?usp=pp_url&entry.1461831832={uid}"


def get_customer_profile_for_order(uid: str):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT name, address, distance_text, delivery_fee FROM health_profile WHERE user_id=?", (uid,))
            return c.fetchone()
    except Exception:
        return None


def parse_subscription_order_message(msg: str):
    cleaned = msg.strip()
    cleaned = cleaned.replace("我要估價", "估價", 1).replace("我要訂購", "訂購", 1)
    m = re.match(r"^(估價|訂購)\s*(\d{1,3})\s*餐?\s*(.*)$", cleaned)
    if not m:
        return None
    action, meal_count, address = m.group(1), int(m.group(2)), m.group(3).strip()
    if meal_count <= 0 or meal_count > 120:
        return {"error": "餐數請輸入 1～120 之間，例如：訂購 24餐 台北市松山區..."}
    return {"action": action, "meal_count": meal_count, "address": address}


SUBSCRIPTION_PERIOD_WEEKS = 4
SUBSCRIPTION_MEAL_LOW = 170
SUBSCRIPTION_MEAL_HIGH = 220
PAYMENT_BANK_NAME = "中國信託銀行"
PAYMENT_BANK_BRANCH = "東門分行"
PAYMENT_ACCOUNT_NAME = "一日樂食店"
PAYMENT_ACCOUNT_NUMBER = "215540069587"


def format_payment_info() -> str:
    return (
        "匯款資訊：\n"
        f"銀行：{PAYMENT_BANK_NAME}\n"
        f"分行：{PAYMENT_BANK_BRANCH}\n"
        f"戶名：{PAYMENT_ACCOUNT_NAME}\n"
        f"帳號：{PAYMENT_ACCOUNT_NUMBER}"
    )


def get_line_display_name_safe(uid: str) -> str:
    """盡量用 LINE userId 抓顧客顯示名稱；失敗時回空字串，避免表單 webhook 中斷。"""
    if not uid:
        return ""
    try:
        profile = line_bot_api.get_profile(uid)
        return getattr(profile, "display_name", "") or ""
    except Exception as e:
        print(f"⚠️ 取得 LINE 顯示名稱失敗 uid={str(uid)[:8]}...: {e}")
        return ""


def build_subscription_intro_text(uid: str) -> str:
    return (
        "🍱 一日樂食 AI 包月計畫\n\n"
        "如果你常常覺得：\n\n"
        "「想吃健康一點，但不知道怎麼安排」\n"
        "「想減脂，但每天都要想吃什麼很累」\n"
        "「知道要控制熱量，但懶得自己算」\n\n"
        "一日樂食可以幫你把飲食變簡單。\n\n"
        "我們會依照你的目標、用餐天數與取餐方式，\n"
        "協助安排適合你的健康餐組合。\n\n"
        "你不需要自己算 TDEE，\n"
        "也不用每天研究熱量和蛋白質。\n\n"
        "✅ 一週 2～5 天可選\n"
        "✅ 外送一天固定 2 餐，兩餐同一個時段配送，一天只送一次\n"
        "✅ 自取餐數更彈性\n"
        "✅ 週六可自取\n"
        "✅ LINE 協助追蹤與調整\n\n"
        "點選下方按鈕，先快速估算本期費用。"
    )


def build_subscription_intro_flex(uid: str):
    from linebot.models import FlexSendMessage
    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "20px",
            "backgroundColor": "#FFF8ED",
            "contents": [
                {"type": "text", "text": "🍱 一日樂食 AI 包月計畫", "weight": "bold", "size": "xl", "color": "#7A3E16", "wrap": True},
                {"type": "text", "text": "不用每天想吃什麼，也不用自己算熱量。", "size": "sm", "color": "#8A6A4F", "wrap": True, "margin": "sm"},
                {"type": "separator", "margin": "lg", "color": "#E8D3B8"},
                {"type": "text", "text": "如果你想吃健康一點、想減脂，或只是想把每天吃什麼變簡單，一日樂食會依照你的目標、用餐天數與取餐方式，協助安排適合的健康餐組合。", "size": "sm", "color": "#3F2F24", "wrap": True, "margin": "lg"},
                {"type": "box", "layout": "vertical", "spacing": "xs", "margin": "lg", "paddingAll": "12px", "backgroundColor": "#FFFFFF", "cornerRadius": "12px", "contents": [
                    {"type": "text", "text": "包月特色", "weight": "bold", "size": "sm", "color": "#C45A18"},
                    {"type": "text", "text": "✅ 一週 2～5 天可選", "size": "xs", "color": "#5A4638"},
                    {"type": "text", "text": "✅ 外送一天固定 2 餐，兩餐同一個時段配送，一天只送一次", "size": "xs", "color": "#5A4638", "wrap": True},
                    {"type": "text", "text": "✅ 自取餐數更彈性，週六可自取", "size": "xs", "color": "#5A4638", "wrap": True},
                    {"type": "text", "text": "✅ LINE 協助追蹤與調整", "size": "xs", "color": "#5A4638"}
                ]}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#D96B2B", "action": {"type": "message", "label": "開始估價", "text": "開始包月估價"}},
                {"type": "button", "style": "link", "action": {"type": "message", "label": "找客服", "text": "找客服"}}
            ]
        }
    }
    return FlexSendMessage(alt_text="一日樂食包月 2.0", contents=bubble)


def get_subscription_delivery_discount(days_per_week: int) -> int:
    try:
        days = int(days_per_week or 0)
    except Exception:
        days = 0
    if days >= 5:
        return 300
    if days == 4:
        return 200
    if days == 3:
        return 100
    return 0


def get_subscription_self_pickup_bonus(days_per_week: int) -> int:
    try:
        days = int(days_per_week or 0)
    except Exception:
        days = 0
    if days >= 5:
        return 2
    if days >= 3:
        return 1
    return 0


def format_subscription_day_bonus(days_per_week: int) -> str:
    discount = get_subscription_delivery_discount(days_per_week)
    bonus = get_subscription_self_pickup_bonus(days_per_week)
    if not discount and not bonus:
        return (
            "小提醒：一週 3 天以上可享本期包月優惠。\n"
            "外送可折抵運費，自取可加贈蛋白補充。"
        )
    return (
        "本期優惠：\n"
        f"🚚 外送四週運費折 ${discount}\n"
        f"🛍 自取送 {bonus} 次蛋白補充"
    )


def subscription_days_quick_reply():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="2 天", text="包月天數 2")),
        QuickReplyButton(action=MessageAction(label="3 天", text="包月天數 3")),
        QuickReplyButton(action=MessageAction(label="4 天", text="包月天數 4")),
        QuickReplyButton(action=MessageAction(label="5 天", text="包月天數 5")),
    ])


def subscription_pickup_quick_reply():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="我要外送", text="包月取餐 外送")),
        QuickReplyButton(action=MessageAction(label="我要自取", text="包月取餐 自取")),
    ])


def subscription_self_pickup_meals_quick_reply():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="每天 1 餐", text="自取餐數 1")),
        QuickReplyButton(action=MessageAction(label="每天 2 餐", text="自取餐數 2")),
        QuickReplyButton(action=MessageAction(label="每天 3 餐", text="自取餐數 3")),
        QuickReplyButton(action=MessageAction(label="跟客服討論", text="自取餐數 客服討論")),
    ])


def calculate_subscription_estimate(uid: str, meal_count: int, address: str = "", delivery_count: int = None, pickup_method: str = "外送", days_per_week: int = None, meals_per_day: int = None):
    profile = get_customer_profile_for_order(uid)
    name = profile[0] if profile else ""
    saved_address = profile[1] if profile and len(profile) > 1 else ""
    if not address:
        address = saved_address or ""

    quote = calculate_delivery_quote(address) if address and pickup_method == "外送" else {"success": False, "delivery_available": True if pickup_method == "自取" else None, "delivery_fee": 0, "distance_text": "", "duration_text": "", "delivery_fee_text": "自取無外送費" if pickup_method == "自取" else "未提供地址", "route_group": "OTHER", "delivery_zone": "未分類", "carpool_hint": ""}
    if pickup_method == "外送" and address and not quote.get("success"):
        # Google Maps 偶爾會因地址格式/API 狀態查不到；估價流程不要卡死，先讓用戶看到餐費，外送費交由客服確認。
        quote["delivery_fee_text"] = "已收到地址，但地圖暫時無法判讀，外送費需客服確認"
        quote["distance_text"] = quote.get("distance_text") or "已填地址，距離需客服確認"
        quote["duration_text"] = quote.get("duration_text") or ""
        quote["route_group"] = quote.get("route_group") or infer_route_group(address)
    delivery_available = quote.get("delivery_available") if pickup_method == "外送" else True
    delivery_fee = int(quote.get("delivery_fee") or 0) if quote.get("success") and delivery_available is not False and pickup_method == "外送" else 0
    if delivery_count is None:
        delivery_count = meal_count if pickup_method == "外送" else 0
    meal_low_total = meal_count * SUBSCRIPTION_MEAL_LOW
    meal_high_total = meal_count * SUBSCRIPTION_MEAL_HIGH
    delivery_total = delivery_fee * delivery_count
    delivery_discount_available = get_subscription_delivery_discount(days_per_week) if pickup_method == "外送" else 0
    delivery_discount_applied = min(delivery_total, delivery_discount_available) if delivery_total > 0 else 0
    self_pickup_bonus_count = get_subscription_self_pickup_bonus(days_per_week) if pickup_method == "自取" else 0
    quote_low_total = meal_low_total + delivery_total - delivery_discount_applied
    quote_high_total = meal_high_total + delivery_total - delivery_discount_applied
    return {
        "name": name,
        "address": address,
        "quote": quote,
        "meal_count": meal_count,
        "delivery_count": delivery_count,
        "delivery_fee": delivery_fee,
        "delivery_available": delivery_available,
        "meal_low_total": meal_low_total,
        "meal_high_total": meal_high_total,
        "delivery_total": delivery_total,
        "delivery_discount_available": delivery_discount_available,
        "delivery_discount_applied": delivery_discount_applied,
        "self_pickup_bonus_count": self_pickup_bonus_count,
        "quote_low_total": quote_low_total,
        "quote_high_total": quote_high_total,
        "pickup_method": pickup_method,
        "days_per_week": days_per_week,
        "meals_per_day": meals_per_day,
        "period_weeks": SUBSCRIPTION_PERIOD_WEEKS,
    }


def format_delivery_manual_review_note(q: dict) -> str:
    """外送距離較遠時，提示顧客與管理員需人工確認，避免長距離運費被誤解為一定可承接。"""
    distance_meters = int((q or {}).get("distance_meters") or 0)
    delivery_zone = (q or {}).get("delivery_zone") or ""
    if delivery_zone == "OUTSIDE" or distance_meters > 6000:
        return "⚠️ 此地址距離較遠，超出自家車隊主要配送範圍，需客服人工確認；也可能建議改自取或不承接外送。"
    if delivery_zone == "FAR" or distance_meters > 4000:
        return "⚠️ 此地址已超過 4 公里，外送費與是否可併單需客服人工確認。"
    return ""


def format_subscription_estimate(est: dict, include_order_hint: bool = True) -> str:
    q = est.get("quote", {})
    address = est.get("address") or "尚未提供"
    pickup_method = est.get("pickup_method") or "外送"
    days = est.get("days_per_week")
    meals_per_day = est.get("meals_per_day")
    if pickup_method == "外送" and est.get("delivery_available") is False:
        distance_text = q.get("distance_text") or "超過 3 公里"
        duration_text = f" / {q.get('duration_text')}" if q.get("duration_text") else ""
        return (
            "🚫 此地址暫不提供外送\n\n"
            f"📍 地址：{address}\n"
            f"📏 距離：{distance_text}{duration_text}\n\n"
            "目前外送範圍為門市 3 公里內。\n"
            "你可以重新選擇自取，或找客服確認其他方式。"
        )
    distance_line = f"📏 距離：{q.get('distance_text')} / {q.get('duration_text')}" if q.get("success") else ("📏 距離：自取不需測距" if pickup_method == "自取" else f"📏 距離：{q.get('distance_text') or '已填地址，距離需客服確認'}")
    delivery_text = q.get("delivery_fee_text") or ("自取無外送費" if pickup_method == "自取" else "尚未提供地址，外送費需人工確認")
    manual_review_note = format_delivery_manual_review_note(q) if pickup_method == "外送" else ""
    manual_review_line = f"\n{manual_review_note}" if manual_review_note else ""
    plan_line = f"📅 每週：{days} 天\n" if days else ""
    meals_line = f"🍽️ 餐數：每週 {days} 天 × 每天 {meals_per_day} 餐，本期 {est.get('period_weeks', 4)} 週共 {est['meal_count']} 餐\n" if days and meals_per_day else f"🍱 餐數：{est['meal_count']} 餐\n"
    discount_available = int(est.get("delivery_discount_available") or 0)
    discount_applied = int(est.get("delivery_discount_applied") or 0)
    pickup_bonus = int(est.get("self_pickup_bonus_count") or 0)
    if pickup_method == "外送" and discount_available:
        discount_line = f"\n🎁 本期包月優惠：四週外送費折 ${discount_available}"
        if discount_applied:
            discount_line += f"（已折抵 ${discount_applied}）"
        else:
            discount_line += "（外送費需客服確認時，折抵會一起核算）"
    elif pickup_method == "自取" and pickup_bonus:
        discount_line = f"\n🎁 自取優惠：送 {pickup_bonus} 次蛋白補充"
    else:
        discount_line = ""
    hint = ""
    if include_order_hint:
        hint = "\n\n價格可接受的話，請點「可以，建立包月資料」或回覆「我要填寫包月資料」。"
    return (
        "🧾 一日樂食包月粗估\n"
        f"{plan_line}"
        f"🚚 取餐方式：{pickup_method}\n"
        f"{meals_line}"
        f"📍 地址：{address if pickup_method == '外送' else '自取'}\n"
        f"{distance_line}\n"
        f"🛵 單次外送費：{delivery_text}\n"
        f"🚚 估計配送次數：{est['delivery_count']} 次\n\n"
        f"餐費粗估：${est['meal_low_total']:,}～${est['meal_high_total']:,}\n"
        f"外送費粗估：${est['delivery_total']:,}\n"
        f"{discount_line}\n"
        f"本期粗估合計：${est['quote_low_total']:,}～${est['quote_high_total']:,}\n\n"
        "備註：這是下單前粗估，實際金額會由客服依菜單、餐數、外送距離、優惠與付款方式最後確認。"
        f"{manual_review_line}"
        f"{hint}"
    )


def build_subscription_estimate_flex(uid: str, est: dict):
    from linebot.models import FlexSendMessage
    pickup_method = est.get("pickup_method") or "外送"
    days = est.get("days_per_week")
    meals_per_day = est.get("meals_per_day")
    address = est.get("address") or "尚未提供"
    q = est.get("quote", {})
    if pickup_method == "外送" and est.get("delivery_available") is False:
        bubble = {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "20px",
                "backgroundColor": "#FFF5F2",
                "contents": [
                    {"type": "text", "text": "🚫 此地址暫不提供外送", "weight": "bold", "size": "xl", "color": "#9F2D20", "wrap": True},
                    {"type": "text", "text": f"地址：{address}", "size": "sm", "color": "#4B352F", "wrap": True, "margin": "lg"},
                    {"type": "text", "text": f"距離：{q.get('distance_text') or '超過 3 公里'} / {q.get('duration_text') or '時間未提供'}", "size": "sm", "color": "#4B352F", "wrap": True, "margin": "sm"},
                    {"type": "separator", "margin": "lg", "color": "#E8C9C3"},
                    {"type": "text", "text": "目前外送範圍為門市 3 公里內。你可以重新選擇自取，或找客服確認其他方式。", "size": "sm", "color": "#6B433A", "wrap": True, "margin": "lg"},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "button", "style": "primary", "color": "#2E8B57", "action": {"type": "message", "label": "重新選擇取餐方式", "text": "開始包月估價"}},
                    {"type": "button", "style": "link", "action": {"type": "message", "label": "找客服", "text": "找客服"}},
                ],
            },
        }
        return FlexSendMessage(alt_text="此地址暫不提供外送", contents=bubble)
    summary = f"每週 {days} 天 × 每天 {meals_per_day} 餐｜本期 {est.get('period_weeks', 4)} 週共 {est['meal_count']} 餐" if days and meals_per_day else f"共 {est['meal_count']} 餐"
    distance_text = f"{q.get('distance_text')} / {q.get('duration_text')}" if q.get("success") else ("自取不需測距" if pickup_method == "自取" else (q.get("distance_text") or "已填地址，距離需客服確認"))
    manual_review_note = format_delivery_manual_review_note(q) if pickup_method == "外送" else ""
    discount_available = int(est.get("delivery_discount_available") or 0)
    discount_applied = int(est.get("delivery_discount_applied") or 0)
    pickup_bonus = int(est.get("self_pickup_bonus_count") or 0)
    if pickup_method == "外送" and discount_available:
        promo_text = f"本期優惠：四週外送費折 ${discount_available}"
        if discount_applied:
            promo_text += f"｜已折 ${discount_applied}"
        else:
            promo_text += "｜客服確認外送費時一起核算"
    elif pickup_method == "自取" and pickup_bonus:
        promo_text = f"自取優惠：送 {pickup_bonus} 次蛋白補充"
    else:
        promo_text = ""
    promo_contents = [{"type": "text", "text": f"🎁 {promo_text}", "size": "sm", "color": "#D85A1B", "weight": "bold", "wrap": True, "margin": "xs"}] if promo_text else []
    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "20px",
            "backgroundColor": "#F7FBF4",
            "contents": [
                {"type": "text", "text": "💰 包月粗估結果", "weight": "bold", "size": "xl", "color": "#24513A"},
                {"type": "text", "text": summary, "size": "sm", "color": "#466152", "wrap": True, "margin": "sm"},
                {"type": "separator", "margin": "lg", "color": "#CFE4D5"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "lg", "contents": [
                    {"type": "text", "text": f"取餐方式：{pickup_method}", "size": "sm", "color": "#23352A", "wrap": True},
                    {"type": "text", "text": f"地址：{address if pickup_method == '外送' else '自取'}", "size": "sm", "color": "#23352A", "wrap": True},
                    {"type": "text", "text": f"距離：{distance_text}", "size": "sm", "color": "#23352A", "wrap": True},
                    {"type": "text", "text": f"配送次數：{est['delivery_count']} 次", "size": "sm", "color": "#23352A", "wrap": True}
                ]},
                {"type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "14px", "backgroundColor": "#FFFFFF", "cornerRadius": "12px", "contents": [
                    {"type": "text", "text": f"餐費：${est['meal_low_total']:,}～${est['meal_high_total']:,}", "size": "sm", "color": "#24513A", "weight": "bold"},
                    {"type": "text", "text": f"外送費：${est['delivery_total']:,}", "size": "sm", "color": "#24513A", "weight": "bold", "margin": "xs"},
                    *promo_contents,
                    {"type": "text", "text": f"本期合計：約 ${est['quote_low_total']:,}～${est['quote_high_total']:,}", "size": "lg", "color": "#D85A1B", "weight": "bold", "wrap": True, "margin": "md"}
                ]},
                {"type": "text", "text": "實際金額會依菜單、餐數、外送距離、優惠與客服確認為準。", "size": "xs", "color": "#6F7B72", "wrap": True, "margin": "md"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#2E8B57", "action": {"type": "uri", "label": "可以，建立包月資料", "uri": get_subscription_form_link(uid)}},
                {"type": "button", "style": "secondary", "action": {"type": "message", "label": "重新選天數", "text": "開始包月估價"}},
                {"type": "button", "style": "link", "action": {"type": "message", "label": "找客服確認", "text": "找客服"}}
            ]
        }
    }
    if manual_review_note:
        bubble["body"]["contents"].insert(-1, {"type": "text", "text": manual_review_note, "size": "xs", "color": "#D85A1B", "wrap": True, "margin": "md"})
    return FlexSendMessage(alt_text="包月粗估結果", contents=bubble)


def create_subscription_order(uid: str, est: dict):
    created_at = tw_now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO subscription_orders (
                user_id, customer_name, meal_count, address, distance_text, delivery_fee, delivery_count,
                meal_low_total, meal_high_total, delivery_total, quote_low_total, quote_high_total,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            uid, est.get("name") or "", est["meal_count"], est.get("address") or "",
            est.get("quote", {}).get("distance_text") or "", est["delivery_fee"], est["delivery_count"],
            est["meal_low_total"], est["meal_high_total"], est["delivery_total"],
            est["quote_low_total"], est["quote_high_total"], created_at
        ))
        order_id = c.lastrowid
        conn.commit()
    return order_id


def notify_admin_new_subscription_order(order_id: int, uid: str, est: dict):
    admin_msg = (
        f"🧾【包月待確認訂單 #{order_id}】\n"
        f"顧客：{est.get('name') or uid[:8]}\n"
        f"UID：{uid}\n"
        f"餐數：{est['meal_count']} 餐\n"
        f"地址：{est.get('address') or '未提供'}\n"
        f"距離：{est.get('quote', {}).get('distance_text') or 'NA'}\n"
        f"單次外送費：{est['delivery_fee']}\n"
        f"粗估合計：${est['quote_low_total']:,}～${est['quote_high_total']:,}\n\n"
        f"核准：#核准訂單 {order_id}\n"
        f"拒絕：#拒絕訂單 {order_id} 原因\n"
        f"付款後開通：#開通訂單 {order_id}"
    )
    try:
        admin_id = get_admin_notify_uid()
        line_bot_api.push_message(admin_id, TextSendMessage(text=admin_msg))
    except Exception as e:
        print(f"⚠️ 推播包月訂單給管理員失敗: {e}")


def create_pending_subscription_form_order(snapshot: dict) -> int:
    """把已填表單先存成 pending 訂單；付款前不寫正式排餐表。"""
    created_at = snapshot.get("created_at") or tw_now().strftime("%Y-%m-%d %H:%M:%S")
    meal_count = len(snapshot.get("active_days_list") or []) * 2
    quote_total = int(snapshot.get("total_with_delivery") or snapshot.get("total_price") or 0)
    delivery_info = snapshot.get("delivery_info") or {}
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO subscription_orders (
                user_id, customer_name, meal_count, address, distance_text, delivery_fee, delivery_count,
                meal_low_total, meal_high_total, delivery_total, quote_low_total, quote_high_total,
                status, created_at, form_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            snapshot.get("user_id") or "",
            snapshot.get("name") or "",
            meal_count,
            snapshot.get("address") or "",
            delivery_info.get("distance_text") or "",
            int(snapshot.get("delivery_fee_per_trip") or 0),
            int(snapshot.get("delivery_days_count") or 0),
            int(snapshot.get("total_price") or 0),
            int(snapshot.get("total_price") or 0),
            int(snapshot.get("delivery_total_fee") or 0),
            quote_total,
            quote_total,
            created_at,
            json.dumps(snapshot, ensure_ascii=False)
        ))
        order_id = c.lastrowid
        conn.commit()
    return order_id


def notify_admin_pending_subscription_form(order_id: int, snapshot: dict):
    uid = snapshot.get('user_id') or ''
    line_display_name = snapshot.get('line_display_name') or get_line_display_name_safe(uid)
    uid_tail = uid[-8:] if uid else 'NA'
    delivery_review_note = format_delivery_manual_review_note(snapshot.get('delivery_info') or {})
    delivery_review_line = f"\n{delivery_review_note}" if delivery_review_note else ""
    admin_form_msg = (
        f"📝【包月表單待付款 #{order_id}】\n"
        f"表單姓名：{snapshot.get('name') or '未填'}\n"
        f"LINE名稱：{line_display_name or '抓取失敗/未提供'}\n"
        f"UID末8碼：{uid_tail}\n"
        f"UID：{uid or '未提供'}\n"
        f"取餐方式：{snapshot.get('pickup_method') or '未填'}\n"
        f"外送地址：{snapshot.get('address') or '未提供'}\n"
        f"單次外送費：${int(snapshot.get('delivery_fee_per_trip') or 0)}\n"
        f"配送天數：{snapshot.get('delivery_days_count') if snapshot.get('is_delivery') else '自取'}\n"
        f"排餐金額：${int(snapshot.get('total_price') or 0)}\n"
        f"本期預估總金額：${int(snapshot.get('total_with_delivery') or 0)}\n"
        f"{delivery_review_line}\n\n"
        f"核准並發送匯款資訊：#核准訂單 {order_id}\n"
        f"付款完成後正式開通：#開通訂單 {order_id}\n\n"
        "提醒：#核准訂單 會直接把中國信託匯款資訊推送給這個 LINE 用戶。\n"
        "此單目前只存在 pending，尚未寫入正式排餐試算表。"
    )
    try:
        admin_notify_uid = get_admin_notify_uid()
        line_bot_api.push_message(admin_notify_uid, TextSendMessage(text=admin_form_msg))
        print(f"✅ 已推播包月 pending 表單通知給管理員：{admin_notify_uid}")
    except Exception as e:
        print(f"⚠️ 推播包月 pending 表單通知給管理員失敗: {e}")


def formalize_subscription_snapshot(order_id: int, snapshot: dict):
    """付款開通後，將 pending 表單資料正式寫入 health_profile、個人分頁與 Master_API_View。"""
    user_id = snapshot.get("user_id") or ""
    if not user_id:
        return False, "pending 表單資料缺少 UID"

    name = snapshot.get("name") or ""
    goal = snapshot.get("goal") or ""
    restrictions = snapshot.get("restrictions") or ""
    schedule_text = snapshot.get("schedule_text") or ""
    active_days_list = snapshot.get("active_days_list") or []
    safe_name = snapshot.get("safe_name") or f"{name}_{user_id[-4:]}_{tw_now().strftime('%Y%m%d')}"
    delivery_info = snapshot.get("delivery_info") or {}
    tdee = int(snapshot.get("tdee") or 0)
    protein = float(snapshot.get("protein") or 0)
    is_coaching_enabled = int(snapshot.get("is_coaching_enabled") or 0)
    is_carb_cycling_enabled = int(snapshot.get("is_carb_cycling_enabled") or 0)
    user_level = int(snapshot.get("user_level") or 2)
    race_date = snapshot.get("race_date") or ""
    address = snapshot.get("address") or ""
    total_price = int(snapshot.get("total_price") or 0)
    schedule_sheet_rows = snapshot.get("schedule_sheet_rows") or []
    master_api_rows = snapshot.get("master_api_rows") or []
    pref_staple = snapshot.get("pref_staple") or ""
    pref_protein = snapshot.get("pref_protein") or ""
    weight = snapshot.get("weight") or ""

    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO health_profile (
                user_id, name, tdee, protein, goal, restrictions, summary_text, active_days,
                today_extra_cal, today_date, sheet_name, is_coaching_enabled, is_carb_cycling_enabled,
                ai_silenced_until, user_level, race_date, address, distance_text, distance_meters,
                delivery_fee, delivery_zone, route_group, delivery_note
            ) VALUES (?,?,?,?,?,?,?,?,0,'',?,?,?,?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, name, tdee, protein, goal, restrictions, schedule_text, ",".join(active_days_list),
            safe_name, is_coaching_enabled, is_carb_cycling_enabled, '', user_level, race_date,
            address, delivery_info.get("distance_text", ""), delivery_info.get("distance_meters", 0),
            delivery_info.get("delivery_fee", 0), delivery_info.get("delivery_zone", ""),
            delivery_info.get("route_group", ""), delivery_info.get("carpool_hint", "")
        ))
        c.execute("UPDATE subscription_orders SET formalized_at=? WHERE id=?", (tw_now().strftime("%Y-%m-%d %H:%M:%S"), order_id))
        _u = c.execute("SELECT status, remaining_meals, expiry_date FROM usage WHERE user_id=?", (user_id,)).fetchone()
        conn.commit()

    if _u:
        sync_customer_sheet(user_id, name, _u[0], _u[1], _u[2], tdee)

    if gc:
        try:
            print(f"📊 [PAYMENT_GATE] 正式寫入 Google Sheet，訂單 #{order_id}，共 {len(master_api_rows)} 筆資料")
            sheet = gc.open_by_url(SHEET_URL)
            main_sheet = sheet.sheet1
            now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
            main_sheet.append_row([now_str, name, goal, tdee, int(protein), restrictions, total_price, ",".join(active_days_list), schedule_text])

            try:
                try:
                    user_sheet = sheet.add_worksheet(title=safe_name, rows="1000", cols="20")
                except Exception:
                    user_sheet = sheet.worksheet(safe_name)
                    user_sheet.clear()
                profile_data = [["【VIP 客戶檔案】", f"姓名: {name}", f"目前體重: {weight} kg", f"目標: {goal}", f"TDEE: {tdee} kcal", f"蛋白質: {int(protein)} g", f"禁忌: {restrictions}", f"喜好: {pref_staple} + {pref_protein}", f"💰 排餐總額: ${total_price}"], [""]]
                menu_title = [["【專屬排餐計畫 (第1週~第4週)】"]]
                tracking_headers = [[""], ["================================================================="], ["【日常飲食與動態追蹤】"], ["紀錄時間", "紀錄類型", "客人傳送內容", "數值變化(kcal)"]]
                user_sheet.append_rows(profile_data + menu_title + schedule_sheet_rows + tracking_headers)
            except Exception as e:
                print(f"⚠️ 建立/更新個人分頁失敗: {e}")

            try:
                try:
                    api_sheet = sheet.worksheet("Master_API_View")
                except gspread.exceptions.WorksheetNotFound:
                    api_sheet = sheet.add_worksheet(title="Master_API_View", rows="2000", cols="20")
                    api_sheet.append_row(["Date", "User_ID", "TDEE", "Lunch_Item", "Dinner_Item", "Tomorrow_Training", "Is_Coaching_Enabled", "Plan_Type", "Sport_Type", "Plan_Week", "Intervals_ID", "Intervals_API_Key", "Training_Freq", "Normal_Train_Time", "Long_Train_Day", "Run_Pace", "Bike_FTP", "Swim_Pace", "User_Level", "Race_Date", "Is_Carb_Cycling_Enabled"])
                try:
                    all_vals = api_sheet.get_all_values()
                    rows_to_del = [i + 1 for i, row in enumerate(all_vals) if i > 0 and len(row) > 1 and row[1] == user_id]
                    for rn in sorted(rows_to_del, reverse=True):
                        api_sheet.delete_rows(rn)
                except Exception as e:
                    print(f"⚠️ 刪除 Master_API_View 舊行失敗: {e}")
                if master_api_rows:
                    api_sheet.append_rows(master_api_rows)
                print(f"✅ [PAYMENT_GATE] 訂單 #{order_id} 已正式寫入 Master_API_View：{len(master_api_rows)} 行")
            except Exception as e:
                print(f"⚠️ 寫入 Master_API_View 失敗: {e}")
        except Exception as e:
            print(f"⚠️ 正式寫入 Google Sheet 失敗: {e}")
            return False, f"Google Sheet 寫入失敗：{e}"

    if is_coaching_enabled:
        try:
            start_date_raw = snapshot.get("start_date") or ""
            start_date = datetime.fromisoformat(start_date_raw).date() if start_date_raw else tw_today()
            threading.Thread(target=update_4week_plan_background, args=(user_id, start_date, {
                "sport_type": snapshot.get("sport_type") or "未設定",
                "training_freq": snapshot.get("training_freq") or "未設定",
                "long_train_day": snapshot.get("long_train_day") or "未設定",
                "run_pace": snapshot.get("run_pace") or "未提供",
                "bike_ftp": snapshot.get("bike_ftp") or "未提供",
                "swim_pace": snapshot.get("swim_pace") or "未提供",
                "phase_name": snapshot.get("phase_name") or "",
                "weeks_to_race": snapshot.get("weeks_to_race"),
                "restrictions": restrictions or "",
                "is_carb_cycling_enabled": is_carb_cycling_enabled,
            }), daemon=True).start()
        except Exception as e:
            print(f"⚠️ 啟動背景課表/碳循環更新失敗: {e}")

    return True, "已將 pending 表單正式寫入排餐試算表"


def list_pending_subscription_orders(limit=20):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, customer_name, meal_count, address, quote_low_total, quote_high_total, created_at, user_id, form_payload_json
            FROM subscription_orders
            WHERE status='pending'
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        return c.fetchall()


def update_subscription_order_status(order_id: int, status: str, admin_uid: str, note: str = ""):
    now = tw_now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT id, user_id, customer_name, meal_count, quote_low_total, quote_high_total, status, form_payload_json FROM subscription_orders WHERE id=?", (order_id,))
        row = c.fetchone()
        if not row:
            return False, "❌ 找不到這筆包月訂單。"
        oid, user_id, customer_name, meal_count, low_total, high_total, old_status, form_payload_json = row
        if status == "approved":
            c.execute("UPDATE subscription_orders SET status='approved', approved_at=?, approved_by=?, admin_note=? WHERE id=?", (now, admin_uid, note, order_id))
            amount_text = f"${low_total:,}" if int(low_total or 0) == int(high_total or 0) else f"${low_total:,}～${high_total:,}"
            customer_msg = (
                f"✅ 您的一日樂食包月訂單 #{order_id} 已確認！\n"
                f"姓名：{customer_name or '未填'}\n"
                f"餐數：{meal_count} 餐\n"
                f"本期應匯款金額：{amount_text}\n\n"
                f"{format_payment_info()}\n\n"
                "匯款完成後，請直接回傳匯款帳號末五碼，或傳送匯款截圖給客服確認。\n"
                "付款確認後，我們會正式開通包月與 AI 營養管理。"
            )
        elif status == "rejected":
            c.execute("UPDATE subscription_orders SET status='rejected', admin_note=? WHERE id=?", (note or "需人工重新確認", order_id))
            customer_msg = f"⚠️ 您的一日樂食包月訂單 #{order_id} 需要重新確認：{note or '請聯絡客服確認'}"
        elif status == "activated":
            duration_days = 31
            chat_limit = 30 if meal_count >= 48 else 20
            vip_code = "#VIPORDER-" + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            c.execute("INSERT INTO vips (code, meals, duration_days, chat_limit, is_used) VALUES (?, ?, ?, ?, 0)", (vip_code, meal_count, duration_days, chat_limit))
            c.execute("UPDATE subscription_orders SET status='activated', activated_at=?, vip_code=? WHERE id=?", (now, vip_code, order_id))
            customer_msg = (
                f"🎉 付款已確認，包月已正式開通！\n"
                f"請直接複製並傳送這組開通碼完成會員權限啟用：\n{vip_code}\n\n"
                "您先前填寫的包月資料已轉正式，不需要再填一次表單。"
            )
        else:
            return False, "❌ 不支援的訂單狀態。"
        conn.commit()
    if status == "activated" and form_payload_json:
        try:
            snapshot = json.loads(form_payload_json)
            ok, formalize_msg = formalize_subscription_snapshot(order_id, snapshot)
            if not ok:
                customer_msg += f"\n\n⚠️ 系統提醒：正式排餐表寫入需客服確認：{formalize_msg}"
            else:
                customer_msg += "\n\n✅ 專屬排餐與正式試算表已同步建立。"
        except Exception as e:
            print(f"⚠️ formalize pending 訂單失敗: {e}")
            customer_msg += "\n\n⚠️ 系統提醒：正式排餐表寫入失敗，客服會協助確認。"
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=customer_msg))
    except Exception as e:
        print(f"⚠️ 推播訂單狀態給顧客失敗: {e}")
        return False, f"⚠️ 訂單 #{order_id} 已更新為 {status}，但推播給顧客失敗：{e}"
    recipient_hint = f"{customer_name or '顧客'}（UID末8碼：{str(user_id)[-8:]}）" if user_id else (customer_name or '顧客')
    return True, f"✅ 訂單 #{order_id} 已更新為 {status}，並已推播給 {recipient_hint}。"


def send_subscription_expiry_reminders(days_before: int = 3):
    target_date = (tw_today() + timedelta(days=days_before)).isoformat()
    notified = 0
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT u.user_id, h.name, u.remaining_meals, u.expiry_date
                FROM usage u
                LEFT JOIN health_profile h ON h.user_id = u.user_id
                WHERE u.status='vip' AND u.expiry_date=?
            """, (target_date,))
            rows = c.fetchall()

        for user_id, name, remaining_meals, expiry_date in rows:
            msg = (
                f"⏰ {name or '會員'} 您好！\n"
                f"您的方案將於 {expiry_date} 到期，目前剩餘 {remaining_meals or 0} 餐。\n\n"
                "若想不中斷配餐與 AI 營養師服務，建議這幾天就先續訂下一期包月喔！"
            )
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                notified += 1
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ send_subscription_expiry_reminders 失敗: {e}")
    return notified

def generate_package_codes(t, n):
    codes = []
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        m, d, l, p = (24,31,20,"#VIP24-") if t=="24m" else (48,31,30,"#VIP48-")
        for _ in range(n):
            c_str = p + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            c.execute("INSERT INTO vips VALUES (?,?,?,?,0)", (c_str, m, d, l))
            codes.append(c_str)
        conn.commit()
    return codes

def redeem_code(uid, code):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT meals, duration_days, chat_limit FROM vips WHERE code=? AND is_used=0", (code,))
    r = c.fetchone()
    if not r: conn.close(); return None, "❌ 無效"
    c.execute("SELECT id, formalized_at FROM subscription_orders WHERE vip_code=? AND user_id=?", (code, uid))
    linked_order = c.fetchone()
    m, d, l = r; today = tw_today()
    c.execute("UPDATE vips SET is_used=1 WHERE code=?", (code,))
    c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (uid,))
    u = c.fetchone(); curr_m = u[0] if u else 0
    exp = (today + timedelta(days=d)).isoformat()
    c.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?)", (uid, l, curr_m+m, today.isoformat(), 'vip', exp, l))
    conn.commit(); conn.close()
    if linked_order and linked_order[1]:
        return exp, (
            "🎉 兌換成功，包月會員權限已啟用！\n"
            "您先前填寫的包月資料已由客服付款確認後轉正式，\n"
            "不需要再填一次表單，接下來可直接使用專屬菜單與 AI 營養管理。"
        )
    link = f"https://docs.google.com/forms/d/e/1FAIpQLSfblmRmSc669n_C7JU1wja0g4KrEGs1oRQwdq6cfNCC8b1DFA/viewform?usp=pp_url&entry.1461831832={uid}"
    return exp, f"🎉 兌換成功！\n您的專屬排餐表單：\n{link}"

# ==========================================
# 📅 功能四：每週教練排課核心函數
# ==========================================
def run_weekly_coach(uid, reply_token=None):
    """執行每週教練排課完整流程：抓資料 → AI生成 → 寫Sheet → 推播LINE"""

    # 1. 從 SQLite 取得用戶個人設定
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 💡 順手幫你修復了一個隱藏 Bug：原本漏抓了 race_date，這裡補上了！
    c.execute("SELECT name, goal, restrictions, active_days, tdee, protein, race_date FROM health_profile WHERE user_id=?", (uid,))
    hp = c.fetchone()
    conn.close()

    if not hp:
        msg = "找不到您的個人檔案，請先填寫體質評估表單喔！📝"
        try:
            if reply_token: line_bot_api.reply_message(reply_token, TextSendMessage(text=msg))
            else: line_bot_api.push_message(uid, TextSendMessage(text=msg))
        except: pass
        return False, msg
    
    # 解析健康檔案
    name, goal, restrictions, active_days, tdee, protein, race_date = hp

    # 2. 計算下週日期範圍（下週一到週日）
    today = tw_today()
    days_to_monday = (7 - today.weekday()) % 7 or 7
    next_monday = today + timedelta(days=days_to_monday)
    next_week_dates = [(next_monday + timedelta(days=i)).strftime("%Y/%m/%d") for i in range(7)]
    weekday_names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    week_range = f"{next_week_dates[0]} – {next_week_dates[6]}"

    # 3. 從 Google Sheet 抓下週排餐 & 運動設定
    next_week_meals, row_date_map = [], {}
    intervals_id, intervals_key = "", ""
    sport_type = training_freq = normal_train_time = long_train_day = "未設定"
    run_pace = bike_ftp = swim_pace = "未提供"
    
    if gc:
        try:
            api_sheet = gc.open_by_url(SHEET_URL).worksheet("Master_API_View")
            all_records = api_sheet.get_all_records()
            for i, row in enumerate(all_records):
                if str(row.get("User_ID")) == uid and str(row.get("Date")) in next_week_dates:
                    if sport_type == "未設定":
                        sport_type = str(row.get("Sport_Type", "未設定"))
                        training_freq = str(row.get("Training_Freq", "未設定"))
                        normal_train_time = str(row.get("Normal_Train_Time", "未設定"))
                        long_train_day = str(row.get("Long_Train_Day", "未設定"))
                        run_pace = str(row.get("Run_Pace", "未提供"))
                        bike_ftp = str(row.get("Bike_FTP", "未提供"))
                        swim_pace = str(row.get("Swim_Pace", "未提供"))

                    day_idx = next_week_dates.index(str(row.get("Date")))
                    next_week_meals.append({
                        "date": row.get("Date"),
                        "weekday": weekday_names[day_idx],
                        "lunch": row.get("Lunch_Item", ""),
                        "dinner": row.get("Dinner_Item", "")
                    })
                    row_date_map[str(row.get("Date"))] = i + 2
                    if not intervals_id and row.get("Intervals_ID"):
                        intervals_id = str(row.get("Intervals_ID"))
                        intervals_key = str(row.get("Intervals_API_Key", ""))
        except Exception as e:
            print(f"⚠️ 取得下週排餐與設定失敗: {e}")

    # 4. 抓 Intervals.icu 本週體能數據與活動
    icu_data = get_intervals_data(intervals_id, intervals_key) if (intervals_id and intervals_key) else None
    this_week_activities = []
    if intervals_id and intervals_key:
        try:
            week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
            resp = requests.get(
                f"https://intervals.icu/api/v1/athlete/{intervals_id}/activities?oldest={week_start}&newest={today.strftime('%Y-%m-%d')}&limit=10",
                auth=('API_KEY', intervals_key), timeout=10
            )
            if resp.status_code == 200:
                for a in resp.json():
                    if isinstance(a, dict):
                        this_week_activities.append({
                            "date": str(a.get("start_date_local", ""))[:10],
                            "type": a.get("type", ""),
                            "distance_km": round(a.get("distance", 0) / 1000, 1),
                            "duration_min": a.get("moving_time", 0) // 60,
                            "tss": a.get("icu_training_load") or 0
                        })
        except Exception as e:
            print(f"⚠️ 抓本週活動失敗: {e}")

    # 5. Load training library
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _training_library = ""
    try:
        with open(os.path.join(_this_dir, "openclawbot", "training_library.md"), encoding="utf-8-sig") as _f:
            _training_library = _f.read()
    except:
        pass

    # 6. 計算運動區間 (Zones) 與 eTSS 預算
    run_zones = {}
    if run_pace and run_pace != "未提供":
        try:
            pace_val = run_pace.split("/")[0]
            m, s = map(int, pace_val.split(":"))
            pace_5k_sec = (m * 60 + s) / 5
            threshold_sec = pace_5k_sec + 20
            marathon_sec = pace_5k_sec + 37
            easy_low_sec = threshold_sec + 58
            easy_high_sec = threshold_sec + 74
            interval_sec = pace_5k_sec - 8
            rep_sec = pace_5k_sec - 23
            def _sp(s): return f"{int(s//60)}:{int(s%60):02d}/km"
            run_zones = {
                "threshold_pace": _sp(threshold_sec),
                "marathon_pace": _sp(marathon_sec),
                "easy_pace_range": f"{_sp(easy_low_sec)}~{_sp(easy_high_sec)}",
                "interval_pace": _sp(interval_sec),
                "repetition_pace": _sp(rep_sec),
            }
        except: pass

    bike_zones = {}
    if bike_ftp and bike_ftp != "未提供":
        try:
            ftp = float(bike_ftp)
            bike_zones = {
                "z1_watts": f"<{int(ftp*0.55)}W", "z2_watts": f"{int(ftp*0.69)}W",
                "z3_watts": f"{int(ftp*0.83)}~{int(ftp*0.90)}W", "threshold_watts": f"{int(ftp*0.95)}W",
                "vo2max_watts": f"{int(ftp*1.13)}~{int(ftp*1.20)}W",
            }
        except: pass

    swim_zones = {}
    if swim_pace and swim_pace != "未提供":
        try:
            pace_val = swim_pace.split("/")[0]
            m, s = map(int, pace_val.split(":"))
            css_sec = m * 60 + s
            easy_swim = css_sec + 10
            swim_zones = {
                "css_pace": f"{m}:{s:02d}/100m", "easy_swim_pace": f"{int(easy_swim//60)}:{int(easy_swim%60):02d}/100m",
            }
        except: pass

    # 賽事倒數計算
    _race_weeks_left = 0
    if race_date and race_date != "無":
        try:
            _rd = datetime.strptime(str(race_date), "%Y/%m/%d").date()
            _race_weeks_left = max(0, (_rd - today.date()).days // 7)
        except: pass

    if _race_weeks_left == 0 or not race_date or race_date == "無":
        _period, _weekly_etss = "健康維持", 200
    elif _race_weeks_left >= 12: _period, _weekly_etss = "基礎期", 220
    elif _race_weeks_left >= 6: _period, _weekly_etss = "建設期", 280
    elif _race_weeks_left >= 3: _period, _weekly_etss = "高峰期", 320
    elif _race_weeks_left >= 1: _period, _weekly_etss = "減量週", 150
    else: _period, _weekly_etss = "比賽週", 100

    _etss_data = {"period": _period, "weeks_left": _race_weeks_left, "budget": _weekly_etss}

    # 7. 建立完美的、沒有遺漏的 Prompt 輸入資料
    input_data = {
        "athlete": name, "goal": goal, "active_days": active_days, "restrictions": restrictions or "無",
        "tdee": tdee, "protein_target_g": int(protein) if protein else 0, "week_range": week_range,
        "sport_type": sport_type, "training_freq": training_freq, "normal_train_time": normal_train_time, "long_train_day": long_train_day,
        "run_pace": run_pace, "bike_ftp": bike_ftp, "swim_pace": swim_pace,
        "next_week_meals": next_week_meals, "run_zones": run_zones, "bike_zones": bike_zones, "swim_zones": swim_zones,
        "_etss_data": _etss_data, "_training_library": _training_library,
        "this_week_activities": this_week_activities or "無紀錄", "intervals_fitness": icu_data
    }

    weekly_system_prompt = """# Role & Objective
你是一位保守、科學化、以可持續執行為優先的運動教練與營養專家，任職於「一日樂食」。
每週任務：根據顧客的目標、訓練頻率與長訓日，安排下週 7 天完整課表。
首要原則：不是把課表寫得很猛，而是寫得安全、可恢復、可長期持續。

教練請完整參考上方的「鐵人三項訓練庫」內容，選擇適合當週週期的訓練方式。

═══════════════════════════════════════════════════════════════
第一部分：核心原則
═══════════════════════════════════════════════════════════════

## 1. 四大訓練原則
- 【漸進超負荷】每週訓練負荷增加不超過 10%
- 【針對性原則】訓練內容越接近比賽越好
- 【可逆性原則】訓練減少時體能會下降，減量是故意為之
- 【個性化原則】根據運動員獨特情況調整

## 2. 備戰時間與週期配置

根據「備戰週數（距離目標賽事的週數）」決定訓練結構：

| 備戰週數 | 訓練分期 | 重點 |
|---------|---------|------|
| 12週以上 | 基礎期→建設期→巔峰期→競賽期 | 完整結構 |
| 6-12週 | 基礎期（壓縮）→建設期→巔峰期→競賽期 | 理想結構 |
| 4-6週 | 濃縮基礎期（3-4週）→建設期→巔峰期 | 需要取捨 |
| 3-4週 | 建設期為主（2-3週）→巔峰期 | 極限壓縮 |
| 2-3週以內 | 純建設期 → 賽前一週減量 | 風險最高，強度要更保守 |
| 無賽事 | 一般健康維持/規律運動 | 以有氧基礎+防受傷為主 |

- 基礎期：騎車Z2+跑步Z2 有氧建立，不安排高強度 Brick
- 建設期：Threshold跑+Brick 為核心，專項訓練
- 巔峰期：賽前1-2週，訓練量降至 50-70%，維持強度
- 競賽期：完全減量，以輕鬆動覺為主

## 3. 80/20 分配與波動週期化

【預設模式A：固定分配】
- 每項運動（游泳/騎車/跑步）各自遵守 80/20
- 每項每週都有輕重分配

【模式B：波動分配】（適合訓練時間有限的業餘運動員）
- 每週輪流將兩項設為重點（高強度），其一維持輕鬆
- 游泳維持穩定，騎車+跑步為波動主力
- 訓練頻率 ≥ 4天/週 → 可用模式A
- 訓練頻率 ≤ 3天/週 → 建議用模式B

⚠️ Zone X 陷阱：「有點累但不太累」通常是錯誤的 Zone，應避免

## 4. Brick Run 的時機

Brick = 騎完車立刻跑步，模擬真實比賽轉換

| 時期 | 是否安排 Brick |
|------|--------------|
| 基礎期 | ❌ 不需要，重心在有氧基礎 |
| 建設期 | ✅ 每週 1-2 次（核心訓練） |
| 巔峰期 | ✅ 維持，賽前 1 週停止 |
| 競賽期 | ⚠️ 停止，改為輕鬆轉換或休息 |

⚠️ Brick 的跑步配速應比 Threshold 慢（因為肌肉已疲勞），不要安排 M 或 T 以上配速

## 5. 跑步 Zone 系統（統一用 E/LP/M/T/I/R）

【重要：統一命名】
- 跑步 Zone 用 E/LP/M/T/I/R，不用 Z1/Z2/Z3/Z4/Z5
- 教練說明與課表輸出時，跑步一律寫 E/LP/M/T/I/R

【跑步 Zone 定義（所有配速以 input_data 為準）】
| Zone | 名稱 | 配速 | eTSS/hr |
|------|------|------|---------|
| E | Easy（基礎有氧）| 見 input_data["run_zones"]["easy_pace_range"] | 60 |
| LP | Long Progression | 前 E pace，後漸速至 M pace | 75 |
| M | Marathon Pace | 見 input_data["run_zones"]["marathon_pace"] | 75 |
| T | Threshold | 見 input_data["run_zones"]["threshold_pace"] | 90 |
| I | Interval（VO2max）| 見 input_data["run_zones"]["interval_pace"] | 110 |
| R | Repetition | 見 input_data["run_zones"]["repetition_pace"] | 110 |

【LP（Long Progression）明確定義】
- 跑步 LP：前段 E pace，後段漸速至 M pace（依 input_data marathon_pace）

【Brick 跑步配速】
- Brick 跑步 = E zone（肌肉已疲憊，不要用 M 或 T）


## 6. 自行車 Zone 系統（FTP 百分比）

| Zone | 名稱 | 功率（FTP %）| 單一數值（來自 input_data["bike_zones"]）|
|------|------|------------|-------------------|
| Z1 | 恢復 | < 55% FTP | < 132W |
| Z2 | 基礎有氧 | 55-75% FTP | 155W |
| Z3 | 踏耐力 | 75-90% FTP | 176-203W |
| T/Bike | Threshold | 90-105% FTP | 213W |
| I/Bike | VO2max | 105-120% FTP | 236-270W |

【重要：教練說明裡 Z2 統一用 155W，Threshold Bike 統一用 213W，不要寫範圍】
【LP（Long Progression Bike）：前段 Z2 瓦數，後段漸速至 Threshold 以下】
⚠️ 沒有功率計的顧客，以 RPE 替代

## 7. 數據缺失時的處理紀律

【當有 Race_Date 時】
- 自動計算「備戰週數」，套用「第2點」的週期配置框架
- 賽事倒數 2 週以內：全力減量，強度降到 50%

【當沒有 Race_Date 時】
- 預設：「一般健康維持+規律運動」，以有氧基礎建立為主
- 不要安排太密集的高強度課
- 以「每週建立一個訓練習慣」為目標

【當沒有 Swim_Pace（CSS）時】
- 游泳：輕鬆游泳 30~45 分鐘，不給配速目標，只給容積目標

【當沒有 Bike_FTP 時】
- 以 RPE 替代（Z2 = RPE 4-5，Z3 = RPE 6-7，Z4 = RPE 8）

【當只有 5K 沒有 10K 時】
- 預設 Threshold = 5K 配速 + 40s/km（比正常值保守）
- 在課表註明：「此 Threshold 配速為根據 5K 估算，若有 10K 或半馬成績請回報教練更正」

【當 FTP 超過 3 個月未更新時】
- 預設降低 5% 計算
- 在課表註明：「此課表假設 FTP 為近期測試值，若有更新請告知」

═══════════════════════════════════════════════════════════════
第二部分：專屬排課鐵律
═══════════════════════════════════════════════════════════════

## 8. 運動類型連動

- 若為「鐵人三項」：每週【必須】包含至少 1 次游泳、1 次騎車、1 次跑步
- 若為「耐力運動」：必須利用顧客提供的配速數據計算出具體目標
- 若為「肌力/重訓/健美」：進入力量模組（見第三部分）

## 9. 訓練頻率紀律（最高優先，絕對不可違反）

【強制訓練日程對照】
- 從 input_data 的 "week_range" 解析出本週 7 天正確星期
- 例如：week_range = "2026/04/06 ~ 2026/04/12" → 4/6=週一, 4/7=週二, 4/8=週三, 4/9=週四, 4/10=週五, 4/11=週六, 4/12=週日

【強制訓練日規則】
- training_freq 指定的日子 = 必須安排訓練，絕對不可安排「休息」
- 非 training_freq 指定的日期 = 一律「休息」或「主動恢復」，不可安排主訓練
- 每天 daily_plan 的 key 必須是 YYYY/MM/DD 完整日期，與 week_range 的 7 天完全對應

【長訓日（long_train_day）安排鐵律】
- long_train_day 當天 = 該週時長最長的訓練，必須明確標示「長訓日」
- ⚠️【最高紀律】長訓日不可以是高強度！
  - 騎車長訓 → 只能是 Z2（FTP 55-75%），例如「騎車 90-120min @ Z2 瓦數」
  - 跑步長訓 → 只能是 Z2（配速如 5:40/km），例如「跑步 45-60min @ E zone 配速」
  - 游泳長訓 → 只能是低強度長泳，例如「游泳 60min 輕鬆游」
- 絕對禁止：長訓日安排 Threshold、間歇、VO2max、衝刺
- 高強度訓練（Threshold/間歇）只能放在 non-long_train_day 的日子

## 10. 強度分配紀律

- 耐力/鐵人課表中，大多數課表必須是低強度
- 每週高強度主課：訓練頻率 ≥ 4天 → 最多 2 次；≤ 3天 → 最多 1 次
- 長訓日以耐力/Z2 為主，不可把長訓同時寫成高強度主體
- 不得連續兩天安排高強度主課
- 若本週已有疲勞跡象，寧可少排，也不要硬加量加強度

## 11. 四週微週期

- 第 1 週：建立（Foundation）
- 第 2 週：小幅漸進（Progressive Overload）
- 第 3 週：本循環最高週（Peak），仍以保守為原則
- 第 4 週：減量週（Recovery），總量下降 20~30%，強度下降

## 12. 數據精準化紀律

- 禁寫模糊字眼：絕對不可只寫「Z2 跑步」或「騎車 60m」
- 必須根據 run_pace、bike_ftp、swim_pace 算出當天的目標配速/瓦數
- 絕對不可把 5K 配速、FTP、CSS 本身直接當作耐力課的目標；所有配速/瓦數必須依據 input_data 中計算好的 zone 數值

═══════════════════════════════════════════════════════════════
第三部分：力量型訓練原則（sport_type 為肌力/重訓/健美時啟用）
═══════════════════════════════════════════════════════════════

1. 依 training_freq 固定分化模板：
   - 2 天：上肢/下肢 或 全身/全身
   - 3 天：推/拉/腿 或 全身三變化
   - 4 天：上/下/上/下
   - 5 天以上：才可做更細部分化
2. 每次訓練 4~6 個動作：主動作 1~2 個 + 輔助動作 2~4 個
3. 一般客戶以 6~12 次為主，RPE 6~8，保留 1~3 reps
4. 腿日後隔天不可再排高疲勞下肢主課
5. 課表格式：「部位 + 主要動作 + 組數×次數」

═══════════════════════════════════════════════════════════════
第四部分：加餐戰略
═══════════════════════════════════════════════════════════════

⚠️ 只能從以下「一日樂食」真實品項推薦，不可自行捏造：
- 肉類蛋白質：雞胸肉、嫩肩里肌牛、豬里肌、香煎鮭魚、金目鱸魚
- 植物蛋白質：日式煮豆腐、糖心蛋
- 優質碳水：烤地瓜、馬鈴薯泥、五穀飯
- 飲品：燕麥豆漿、南瓜豆漿、紅豆紫米豆漿

根據當天是高強度/耐力訓練、低強度恢復日或休息日來推薦適合的組合。

═══════════════════════════════════════════════════════════════
教練鐵律（輸出前必檢查）
═══════════════════════════════════════════════════════════════

【紀律一：跑步 Zone 命名】
跑步 Zone 只准用：E / LP / M / T / I / R
絕對不准寫：Z1 / Z2 / Z3 / Z4 / Z5
寫錯的輸出視為不合格，需重新生成
例如：「60min @ 6:08/km Z2」→ 正確應為「60min @ 6:08/km E」

【紀律二：TSS 預算結算（Chain-of-Thought）】
在開始寫 daily_plan 之前，請先在頭腦內部完成以下步驟：
Step 1：根據 input_data["_etss_data"]["budget"] 得出本週 TSS 上限
Step 2：根據訓練頻率，估算每天平均 TSS 配置（哪些天高強度、哪些天輕鬆）
Step 3：估算每天 TSS，確保 7 天總和不超過 budget
Step 4：若某天 TSS 太高，調整該天強度（降低 Zone 或縮短時間）
Step 5：確認總和達標後，才正式產出 daily_plan
【重要】千萬不要先寫課表最後才算 TSS，LLM 無法回頭修改；必須先確認預算夠用，再開始列課表
line_message 末尾必須標明：「本週總 eTSS：XXX（TSS/預算{budget}）」

【紀律三：減量週強度上限】
若 {period} 為「減量週」或「競賽期」：
- 強度上限：M zone（T threshold）
- 禁止安排：T（Threshold）以上的專項訓練
- 總量降至 50-70%
- 若 budget < 150 TSS，訓練應更保守，以 E/M 為主

═══════════════════════════════════════════════════════════════
第五部分：輸出格式（強制 JSON）
═══════════════════════════════════════════════════════════════

回傳一個合法 JSON：
{
  "line_message": "（LINE 推播長文，含 Emoji、狀態總評、加餐建議）",
  "daily_plan": {
    "YYYY/MM/DD": "運動種類 + 強度/動作 + 時間/組數 + 明確配速/瓦數",
    "YYYY/MM/DD": "..."
  }
}

規則：
- daily_plan 的 key 必須是 YYYY/MM/DD 格式，與下週 7 天完全對應
- daily_plan 的 value 只寫當天課表（簡潔一行），不含日期或星期
- 非主訓練日必須寫「休息」或「主動恢復」（但 training_freq 指定的四天絕對不可寫休息）
- 高強度日必須標示運動種類，並寫出工作段與恢復段
- 固定瓦數結尾加 W，不可寫成「204~204」區間格式
- 重訓課表必須寫出分化部位與動作內容
- 請優先給出保守、可持續執行的課表
- line_message 末尾必須加上「本週總 eTSS：XXX（TSS/預算XXX）」說明是否在預算內

## 7. eTSS（預估訓練壓力分數）

教練必須為每項訓練計算 eTSS，並確保當週總 eTSS 不超過 input_data["_etss_data"]["budget"]。

【每小時 eTSS 基準】
- Z1：40 TSS/hr
- Z2：60 TSS/hr
- Z3：75 TSS/hr
- Z4：90 TSS/hr
- Z5：110 TSS/hr

【單次計算公式】
單次 eTSS = 每小時基準 × (實際分鐘 / 60)

【當週週期與 eTSS 預算】（從 input_data["_etss_data"] 讀取）
- 當週週期：{period}
- 當週 eTSS 預算：{budget} TSS

【當週總 eTSS 不得超過預算 + 10%】
超出時必須將訓練調整為更輕鬆 Zone 或減少時間

【Strides（Z5 微刺激）不列入主要訓練計算】
在 E/LP 尾聲加入的 Strides（4-6×15s），eTSS 約 5-10，不列入計算

- 直接輸出純 JSON 文字，不要用 ```json 包裝，不要用任何 markdown 包裝"""
# 把 {period} 和 {budget} 置換為實際數值
    _period = _etss_data["period"]
    _budget = _etss_data["budget"]
    weekly_system_prompt = weekly_system_prompt.replace("{period}", _period)
    weekly_system_prompt = weekly_system_prompt.replace("{budget}", str(_budget))
    # 7. 呼叫 LLM 生成課表
    try:
        res = client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[
                {"role": "system", "content": weekly_system_prompt},
                {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)}
            ],
            temperature=0.6, max_completion_tokens=2000
        )
        raw_content = res.choices[0].message.content
        # ── JSON 清洗防線（去除 ```json 包裝）────────────
        raw_content = raw_content.strip()
        if raw_content.startswith("\"\"\""):
            raw_content = raw_content[3:]
        if raw_content.startswith("```json"):
            raw_content = raw_content[7:]
        elif raw_content.startswith("\"\"\""):
            raw_content = raw_content[3:]
        if raw_content.rstrip().endswith("\"\"\""):
            raw_content = raw_content.rstrip()[:-3]
        if raw_content.rstrip().endswith("```"):
            raw_content = raw_content.rstrip()[:-3]
        raw_content = raw_content.strip()
    except Exception as e:
        error_msg = f"⚠️ 教練排課失敗，請稍後再試。（{str(e)[:50]}）"
        try:
            if reply_token: line_bot_api.reply_message(reply_token, TextSendMessage(text=error_msg))
            else: line_bot_api.push_message(uid, TextSendMessage(text=error_msg))
        except: pass
        return False, error_msg

    # 7b. 解析 LLM 回傳的 JSON
    line_message = raw_content  # fallback
    daily_plan = {}
    try:
        # 去除 ```json ... ``` 包裝（防禦）
        clean = raw_content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rsplit("```", 1)[0].strip()
        parsed = json.loads(clean)
        line_message = parsed.get("line_message", raw_content)
        daily_plan = parsed.get("daily_plan", {})
        print(f"✅ JSON 解析成功，daily_plan 包含 {len(daily_plan)} 天")
    except Exception as e:
        print(f"⚠️ JSON 解析失敗，fallback 為純文字: {e}")
        line_message = raw_content
        daily_plan = {}

    # ==========================================
    # 🌟 8. 準備將課表寫回 Google Sheet (寫入 Plan_Week 第 10 欄)
    # ==========================================
    if gc and daily_plan:
        try:
            new_rows_to_add = [] 
            for date_str, plan_text in daily_plan.items():
                if date_str in row_date_map:
                    # 狀況 A：原本就有的格子，直接更新 (寫入第 10 欄 Plan_Week)
                    row_idx = row_date_map[date_str]
                    api_sheet.update_cell(row_idx, 10, plan_text)
                else:
                    # 狀況 B：沒格子的週末，準備自動補齊 18 個欄位
                    # ⚠️ 注意：這裡把 plan_text 放在第 10 個位子 (Plan_Week)
                    new_rows_to_add.append([
                        date_str, uid, 0, "無", "無", "", 1, 
                        goal, sport_type, plan_text, intervals_id, intervals_key, 
                        training_freq, normal_train_time, long_train_day, 
                        run_pace, bike_ftp, swim_pace
                    ])
            
            # 如果有沒訂餐的週末課表，一口氣加進 Sheet 裡
            if new_rows_to_add:
                print(f"📡 DEBUG: 準備寫入，長度為 {len(new_rows_to_add[0])} 欄")
                api_sheet.append_rows(new_rows_to_add)
                print(f"✅ [關鍵成功] Google Sheet 自動補齊了 {len(new_rows_to_add)} 天課表！")

        except Exception as e:
            print(f"❌ [寫入失敗] Google Sheet 發生錯誤: {e}")

    # ==========================================
    # 🌟 9. 最後才把整理好的 LINE 訊息推播給客人
    # ==========================================
    try:
        # 注意這裡用的是 uid，並推播解析出來的 line_message
        line_bot_api.push_message(uid, TextSendMessage(text=line_message))
        print(f"✅ LINE 訊息已順利推播給 UID: {uid}")
    except Exception as e:
        print(f"❌ LINE 推播失敗: {e}")

    # 必須要有這行 return，把結果丟回給前面的 callback
    return line_message, daily_plan


# 🌟 修改 callback 路由，讓它可以接收 background_tasks
@app.post("/callback")
async def callback(request: Request):
    sig = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        handler.handle(body.decode("utf-8"), sig)
    except InvalidSignatureError: 
        print("⚠️ LINE 簽章錯誤！請檢查 Railway 的 LINE_CHANNEL_SECRET 是否填錯或有空格！")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        print(f"⚠️ LINE 訊息處理發生嚴重錯誤: {e}")
        raise HTTPException(status_code=500, detail="Webhook processing failed") from e

    return "OK"


def _nutrition_ws(title):
    if not sh:
        raise RuntimeError("Google Sheet 尚未連線")
    specs = nutrition_sheet_specs()
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        spec = specs[title]
        ws = sh.add_worksheet(title=title, rows=500, cols=len(spec["headers"]) + 2)
        ws.append_row(spec["headers"])
        for row in spec.get("seed_rows", []):
            ws.append_row(row)
        ws.freeze(rows=1)
    return ws


nutrition_sheet_sync_lock = threading.Lock()


def _upsert_raw_sheet_row(ws, entity_id, row_values):
    # Current gspread returns None when find() has no match. Do not catch a
    # removed CellNotFound class, and let real API/network errors propagate.
    cell = ws.find(entity_id, in_column=1)
    if cell:
        end = gspread.utils.rowcol_to_a1(cell.row, len(row_values))
        ws.update(values=[row_values], range_name=f"A{cell.row}:{end}", value_input_option="RAW")
    else:
        ws.append_row(row_values, value_input_option="RAW")


def _sync_food_outbox(entity_id):
    with sqlite3.connect(DB_PATH) as conn:
        food = conn.execute("""
            SELECT f.food_id, product_name, brand, barcode, source_type, owner_user_id,
                   visibility, package_amount, package_unit, servings_per_package,
                   per_serving_json, exchange_json, exchange_review_status,
                   original_image_ref, recognition_confidence, verification_status,
                   fingerprint, created_at, updated_at,
                   a.approved_exchange_json, a.food_fingerprint,
                   a.suggestion_rule_version, a.approved_exchange_hash
            FROM food_catalog f
            LEFT JOIN food_exchange_approvals a ON a.approval_id=(
                SELECT approval_id FROM food_exchange_approvals
                WHERE food_id=f.food_id ORDER BY approved_at DESC LIMIT 1
            )
            WHERE f.food_id=?
        """, (entity_id,)).fetchone()
    if not food:
        raise RuntimeError("food outbox entity missing")
    per = json.loads(food[10] or "{}")
    exch = {}
    if food[12] == "approved" and food[20] and food[20] == food[16]:
        candidate = json.loads(food[19] or "{}")
        expected_hash = exchange_approval_hash(food[20], food[21], candidate)
        if secrets.compare_digest(str(food[22] or ""), expected_hash):
            exch = candidate
    ws = _nutrition_ws("食品資料庫")
    row_values = [
        food[0], food[1], food[2], food[3], food[4], food[5], food[6],
        food[7], food[8], food[9], per.get("calories_kcal", 0), per.get("protein_g", 0),
        per.get("fat_g", 0), per.get("carbohydrate_g", 0), per.get("sugar_g", 0),
        per.get("fiber_g", 0), per.get("sodium_mg", 0), exch.get("milk_exchange", 0),
        exch.get("protein_low_exchange", 0), exch.get("protein_medium_exchange", 0),
        exch.get("protein_high_exchange", 0), exch.get("starch_exchange", 0),
        exch.get("vegetable_exchange", 0), exch.get("fruit_exchange", 0),
        exch.get("fat_exchange", 0), food[12], food[13], food[14], food[15],
        food[16], food[17], food[18]
    ]
    _upsert_raw_sheet_row(ws, entity_id, row_values)


def _sync_food_log_outbox(entity_id):
    with sqlite3.connect(DB_PATH) as conn:
        log = conn.execute("""
            SELECT l.log_id, l.user_id, l.food_id, f.product_name, l.consumed_at,
                   l.meal_slot, l.consumed_servings, l.consumed_amount, l.consumed_unit,
                   l.nutrition_snapshot_json, l.approved_exchange_json, l.source_image_ref,
                   l.plan_id, l.confirmation_status, l.created_at, l.updated_at,
                   l.exchange_approval_id, a.food_fingerprint, a.suggestion_rule_version,
                   a.approved_exchange_json, a.approved_exchange_hash, f.fingerprint
            FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
            LEFT JOIN food_exchange_approvals a ON a.approval_id=l.exchange_approval_id
            WHERE l.log_id=?
        """, (entity_id,)).fetchone()
    if not log:
        raise RuntimeError("food log outbox entity missing")
    nutrition = json.loads(log[9] or "{}")
    exch = json.loads(log[10] or "{}")
    approval_values = json.loads(log[19] or "{}")
    approval_valid = bool(log[16] and log[17] and log[17] == log[21])
    if approval_valid:
        expected_hash = exchange_approval_hash(log[17], log[18], approval_values)
        approval_valid = secrets.compare_digest(str(log[20] or ""), expected_hash)
    if approval_valid:
        expected_applied = {
            key: round(float(approval_values.get(key, 0) or 0) * float(log[6] or 0), 4)
            for key in (
                "milk_exchange", "protein_low_exchange", "protein_medium_exchange",
                "protein_high_exchange", "starch_exchange", "vegetable_exchange",
                "fruit_exchange", "fat_exchange",
            )
        }
        approval_valid = all(
            abs(float(exch.get(key, 0) or 0) - value) <= 0.0001
            for key, value in expected_applied.items()
        )
    if not approval_valid:
        exch = {}
    ws = _nutrition_ws("飲食紀錄")
    row_values = [
        log[0], log[1], log[2], log[3], log[4], log[5], log[6], log[7], log[8],
        nutrition.get("calories_kcal", 0), nutrition.get("protein_g", 0), nutrition.get("fat_g", 0),
        nutrition.get("carbohydrate_g", 0), nutrition.get("sugar_g", 0), nutrition.get("fiber_g", 0),
        nutrition.get("sodium_mg", 0), exch.get("milk_exchange", 0),
        exch.get("protein_low_exchange", 0), exch.get("protein_medium_exchange", 0),
        exch.get("protein_high_exchange", 0), exch.get("starch_exchange", 0),
        exch.get("vegetable_exchange", 0), exch.get("fruit_exchange", 0),
        exch.get("fat_exchange", 0), log[11], log[12], log[13], log[14], log[15]
    ]
    _upsert_raw_sheet_row(ws, entity_id, row_values)


def flush_nutrition_sheet_outbox(limit=50):
    """跨程序鎖定整段外部寫入，避免逾時lease worker以舊資料覆蓋Sheet。"""
    if not sh:
        return 0
    lock_path = f"{DB_PATH}.nutrition-sheet.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return _flush_nutrition_sheet_outbox_locked(limit)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _flush_nutrition_sheet_outbox_locked(limit=50):
    """用SQLite lease跨執行緒／worker認領事件，失敗或逾時後可安全重試。"""
    if not sh:
        return 0
    synced = 0
    lease_owner = f"lease_{secrets.token_hex(12)}"
    claimed_at = tw_now().isoformat(timespec="seconds")
    lease_cutoff = (tw_now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    with nutrition_sheet_sync_lock:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            ensure_nutrition_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE nutrition_sheet_outbox
                   SET status='pending', claimed_at='', lease_owner=''
                   WHERE status='processing' AND claimed_at<?""",
                (lease_cutoff,),
            )
            candidates = conn.execute(
                """SELECT outbox_id FROM nutrition_sheet_outbox
                   WHERE status='pending' ORDER BY created_at LIMIT ?""",
                (int(limit),),
            ).fetchall()
            ids = [row[0] for row in candidates]
            for outbox_id in ids:
                conn.execute(
                    """UPDATE nutrition_sheet_outbox SET status='processing', claimed_at=?, lease_owner=?
                       WHERE outbox_id=? AND status='pending'""",
                    (claimed_at, lease_owner, outbox_id),
                )
            conn.commit()
            rows = conn.execute(
                """SELECT outbox_id, entity_type, entity_id FROM nutrition_sheet_outbox
                   WHERE status='processing' AND lease_owner=? ORDER BY created_at""",
                (lease_owner,),
            ).fetchall()
        for outbox_id, entity_type, entity_id in rows:
            try:
                if entity_type == "food":
                    _sync_food_outbox(entity_id)
                elif entity_type == "food_log":
                    _sync_food_log_outbox(entity_id)
                else:
                    raise ValueError("unknown outbox entity type")
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        """UPDATE nutrition_sheet_outbox
                           SET status=CASE WHEN resync_required=1 THEN 'pending' ELSE 'synced' END,
                               synced_at=CASE WHEN resync_required=1 THEN '' ELSE ? END,
                               resync_required=0, last_error='', claimed_at='', lease_owner=''
                           WHERE outbox_id=? AND status='processing' AND lease_owner=?""",
                        (tw_now().isoformat(timespec="seconds"), outbox_id, lease_owner),
                    )
                    conn.commit()
                synced += 1
            except Exception as exc:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        """UPDATE nutrition_sheet_outbox
                           SET status='pending', attempts=attempts+1, last_error=?,
                               claimed_at='', lease_owner='', resync_required=0
                           WHERE outbox_id=? AND lease_owner=?""",
                        (str(exc)[:500], outbox_id, lease_owner),
                    )
                    conn.commit()
                print(f"⚠️ 營養 outbox 同步失敗 {entity_type}/{entity_id}: {exc}")
    return synced


def sync_confirmed_nutrition_to_sheet(result):
    return flush_nutrition_sheet_outbox()


def format_exchange_summary(exchange):
    labels = (
        ("milk_exchange", "奶類"),
        ("protein_low_exchange", "低脂蛋白"),
        ("protein_medium_exchange", "中脂蛋白"),
        ("protein_high_exchange", "高脂蛋白"),
        ("starch_exchange", "主食"),
        ("vegetable_exchange", "蔬菜"),
        ("fruit_exchange", "水果"),
    )
    parts = [
        f"{label_text} {float((exchange or {}).get(key, 0) or 0):g}份"
        for key, label_text in labels
        if float((exchange or {}).get(key, 0) or 0) > 0
    ]
    return "｜".join(parts) if parts else "目前無法安全推算"



def handle_exchange_review_admin_command(msg, uid):
    if uid != ADMIN_UID:
        raise PermissionError("管理員限定")
    msg = str(msg or "").strip()
    if msg == "#待審營養份量":
        with sqlite3.connect(DB_PATH) as conn:
            ensure_nutrition_schema(conn)
            rows = conn.execute(
                """SELECT food_id,product_name,exchange_json
                   FROM food_catalog WHERE exchange_review_status='pending_review'
                   ORDER BY updated_at DESC,rowid DESC LIMIT 10"""
            ).fetchall()
        if not rows:
            return "✅ 目前沒有待審核的營養份量。"
        lines = [f"🧾 待審營養份量（{len(rows)}筆，最多顯示10筆）"]
        for index, (food_id, product_name, exchange_json) in enumerate(rows, 1):
            exchange = json.loads(exchange_json or "{}")
            lines.extend([
                f"\n{index}. {product_name}",
                f"推算：{format_exchange_summary(exchange)}",
                f"核准：#核准營養份量 {food_id}",
            ])
        return "\n".join(lines)

    match = re.fullmatch(r"#核准營養份量\s+(\S{1,80})", msg)
    if not match:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        result = approve_food_exchange_suggestion(
            conn, food_id=match.group(1), reviewer=uid
        )
    if result["already_approved"]:
        return (
            f"ℹ️ {result['product_name']} 的營養份量已經核准。\n"
            f"正式份數：{format_exchange_summary(result['exchange'])}"
        )
    try:
        flush_nutrition_sheet_outbox()
    except Exception as exc:
        print(f"⚠️ 營養份量核准後Sheet同步暫時失敗，已保留outbox：{exc}")
    return (
        f"✅ 核准完成：{result['product_name']}\n"
        f"正式份數：{format_exchange_summary(result['exchange'])}\n"
        f"更新 {result['updated_logs']} 筆飲食紀錄，已開始納入個人計畫統計。"
    )



def apply_confirmed_nutrition_to_legacy_dashboard(user_id, result):
    """兼容既有儀表板，並用 food_logs.legacy_applied_at 保證只累加一次。"""
    consumed_at = str(result["log"].get("consumed_at") or "")
    log_id = result["log"]["log_id"]
    if consumed_at[:10] != tw_today().isoformat():
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE food_logs SET legacy_applied_at='not_applicable' WHERE log_id=? AND legacy_applied_at=''", (log_id,))
            conn.commit()
        return None
    nutrition = result["log"]["nutrition"]
    cal = float(nutrition.get("calories_kcal", 0) or 0)
    pro = float(nutrition.get("protein_g", 0) or 0)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_daily_food_ledger_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        applied = conn.execute("SELECT legacy_applied_at FROM food_logs WHERE log_id=? AND user_id=?", (log_id, user_id)).fetchone()
        if not applied or applied[0]:
            conn.commit()
            return None
        row = conn.execute("SELECT tdee,protein FROM health_profile WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("UPDATE food_logs SET legacy_applied_at='no_profile' WHERE log_id=?", (log_id,))
            conn.commit()
            return None
        tdee, protein_goal = row
        applied_at = tw_now().isoformat(timespec="seconds")
        nutrition_json = json.dumps(nutrition, ensure_ascii=False, sort_keys=True, allow_nan=False)
        conn.execute(
            """UPDATE food_logs
               SET legacy_applied_at=?,nutrition_snapshot_json=CASE
                   WHEN nutrition_snapshot_json IN ('','{}') THEN ? ELSE nutrition_snapshot_json END
               WHERE log_id=?""",
            (applied_at, nutrition_json, log_id),
        )
        _sync_health_profile_from_ledger_conn(conn, user_id, tw_today().isoformat())
        projected = conn.execute(
            "SELECT today_extra_cal,today_extra_pro FROM health_profile WHERE user_id=?", (user_id,)
        ).fetchone() or (0, 0)
        new_cal, new_pro = projected[0] or 0, projected[1] or 0
        conn.commit()
    upsert_frequent_food(user_id, result["food"]["product_name"], round(cal), round(pro))
    exchange_text = format_exchange_summary(result["log"].get("exchange") or {})
    return build_meal_log_flex(
        result["food"]["product_name"], round(cal, 1), round(pro, 1),
        new_cal, tdee or 2000, new_pro, protein_goal or 100,
        exchange_text=exchange_text,
        exchange_review_status=result["log"].get("exchange_review_status", "pending_review"),
    )


def get_pending_nutrition_label(user_id, token):
    with sqlite3.connect(DB_PATH) as conn:
        result = update_pending_consumption(conn, user_id=user_id, token=token)
    return result["label"], result["consumed_servings"]


def _sheet_number(row, key):
    try:
        return float(str(row.get(key, 0) or 0).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def current_meal_slot(now=None):
    hour = (now or tw_now()).hour
    if hour < 10:
        return "早餐"
    if hour < 15:
        return "午餐"
    if hour < 21:
        return "晚餐"
    return "點心"


def parse_natural_food_log_intent(text):
    """Parse an explicit single-food logging request without asking an LLM."""
    message = " ".join(str(text or "").strip().split())
    if not message:
        return None
    if re.search(r"[?？]", message):
        return None
    question_probe = re.sub(r"[。.!！\s]+$", "", message)
    if re.search(
        r"(?:會胖|多少|幾卡|幾大卡|嗎)$", question_probe
    ):
        return None
    meal_slot = ""
    body = ""
    meal_match = re.match(
        r"^(早餐|午餐|晚餐|點心)\s*(?:吃(?:了)?|喝(?:了)?)\s*(.+)$", message
    )
    if meal_match:
        meal_slot, body = meal_match.group(1), meal_match.group(2)
    else:
        suffix_intent = re.match(
            r"^(.+?)(?:吃了|喝了)[：:\s]*"
            r"(\d+(?:\.\d+)?|一|二|兩|半)\s*"
            r"(c\.?c\.?|ml|毫升|g|克|公克|份|瓶|包)$",
            message,
            re.IGNORECASE,
        )
        if suffix_intent:
            body = (
                f"{suffix_intent.group(1).strip()} "
                f"{suffix_intent.group(2)}{suffix_intent.group(3)}"
            )
        intent_patterns = (
            r"^(?:我要)?(?:紀錄|記錄)(?:一下)?(?:飲食[：:\s]*|[：:\s]+)(.+)$",
            r"^幫我(?:紀錄|記錄|記)(?:一下)?我(?:今天)?(?:吃了|喝了)[：:\s]*(.+)$",
            r"^幫我(?:紀錄|記錄|記)(?:一下)?(?:飲食[：:\s]*|[：:\s]*)(.+)$",
            r"^我(?:今天)?(?:吃了|喝了)[：:\s]*(.+)$",
        )
        if not body:
            for pattern in intent_patterns:
                matched = re.match(pattern, message)
                if matched:
                    body = matched.group(1)
                    break
    body = body.strip(" ：:，,。.!！")
    if not body:
        return None
    if re.fullmatch(r"(?:嗎|嗎[?？]|什麼|什麼[?？]|要記什麼[?？]?)", body):
        return None
    slot_match = re.match(r"^(早餐|午餐|晚餐|點心)[：:\s]*(.+)$", body)
    if slot_match:
        meal_slot, body = slot_match.group(1), slot_match.group(2).strip()

    amount = None
    unit = ""
    amount_match = re.match(
        r"^(.+?)\s*(\d+(?:\.\d+)?|一|二|兩|半)\s*"
        r"(c\.?c\.?|ml|毫升|g|克|公克|份|瓶|包)$",
        body,
        re.IGNORECASE,
    )
    if amount_match:
        body = amount_match.group(1).strip(" ：:，,")
        raw_amount = amount_match.group(2)
        amount = {"一": 1.0, "二": 2.0, "兩": 2.0, "半": 0.5}.get(
            raw_amount, float(raw_amount) if re.fullmatch(r"\d+(?:\.\d+)?", raw_amount) else None
        )
        raw_unit = amount_match.group(3).lower().replace(".", "")
        unit = {
            "cc": "ml", "ml": "ml", "毫升": "ml",
            "g": "g", "克": "g", "公克": "g",
            "份": "serving", "瓶": "package", "包": "package",
        }[raw_unit]
    else:
        amount_match = re.match(
            r"^(\d+(?:\.\d+)?|一|二|兩|半)\s*"
            r"(c\.?c\.?|ml|毫升|g|克|公克|份|瓶|包)\s*(.+)$",
            body,
            re.IGNORECASE,
        )
        if amount_match:
            raw_amount = amount_match.group(1)
            amount = {"一": 1.0, "二": 2.0, "兩": 2.0, "半": 0.5}.get(
                raw_amount,
                float(raw_amount)
                if re.fullmatch(r"\d+(?:\.\d+)?", raw_amount)
                else None,
            )
            raw_unit = amount_match.group(2).lower().replace(".", "")
            unit = {
                "cc": "ml", "ml": "ml", "毫升": "ml",
                "g": "g", "克": "g", "公克": "g",
                "份": "serving", "瓶": "package", "包": "package",
            }[raw_unit]
            body = amount_match.group(3).strip(" ：:，,")
    food_name = " ".join(body.split()).strip()
    if not food_name or len(food_name) > 160:
        return None
    return {
        "food_name": food_name, "amount": amount,
        "unit": unit, "meal_slot": meal_slot,
    }


def _natural_food_candidates(conn, user_id, food_name):
    query_key = re.sub(r"[ \t\r\n]+", "", str(food_name or ""))
    exact_rows = conn.execute(
        """SELECT food_id,product_name,brand,barcode,source_type,owner_user_id,
                  package_amount,package_unit,servings_per_package,
                  per_serving_json,exchange_json,exchange_review_status,
                  created_at,updated_at
           FROM food_catalog
           WHERE (owner_user_id=? OR visibility='public')
             AND lower(replace(replace(replace(replace(
                   product_name, ' ', ''), char(9), ''), char(10), ''), char(13), ''))=lower(?)
           ORDER BY CASE WHEN owner_user_id=? THEN 0 ELSE 1 END, updated_at DESC""",
        (user_id, query_key, user_id),
    ).fetchall()
    exact = []
    for row in exact_rows:
        exact.append({
            "food_id": row[0], "product_name": row[1],
            "brand": row[2] or "", "barcode": row[3] or "",
            "source_type": row[4], "owner_user_id": row[5],
            "package_amount": float(row[6] or 0),
            "package_unit": row[7] or "",
            "servings_per_package": float(row[8] or 1),
            "per_serving": json.loads(row[9] or "{}"),
            "exchange": json.loads(row[10] or "{}"),
            "exchange_review_status": row[11] or "",
            "created_at": row[12] or "", "updated_at": row[13] or "",
            "last_consumed_at": None, "use_count": 0,
        })
    if exact:
        return exact, "exact"
    candidates = search_food_catalog(
        conn, user_id=user_id, query=food_name, limit=20
    )
    candidates.sort(key=lambda item: item["owner_user_id"] != user_id)
    return candidates, "fuzzy"


def _validate_natural_food_amount(amount, unit):
    amount = float(amount)
    maximum = 100.0 if unit in {"serving", "package"} else 100000.0
    if not 0.1 <= amount <= maximum:
        unit_label = _natural_unit_label(unit) or "份量"
        raise ValueError(f"{unit_label}需介於 0.1～{maximum:g}")
    return amount


def _natural_food_servings(item, amount, unit):
    if amount is None:
        return None
    amount = _validate_natural_food_amount(amount, unit)
    if unit == "serving":
        return amount
    servings_per_package = float(item.get("servings_per_package") or 1)
    if unit == "package":
        return amount * servings_per_package
    package_amount = float(item.get("package_amount") or 0)
    package_unit = str(item.get("package_unit") or "").strip().lower().replace(".", "")
    package_unit = {
        "cc": "ml", "毫升": "ml", "公克": "g", "克": "g",
    }.get(package_unit, package_unit)
    if unit not in {"ml", "g"} or unit != package_unit or package_amount <= 0:
        raise ValueError(
            f"「{item['product_name']}」以{item.get('package_unit') or '份'}記錄，"
            f"無法直接換算這個單位"
        )
    return amount / package_amount * servings_per_package


def resolve_nutrition_consumed_at(parsed, *, received_at):
    """Only promote a high-confidence, plausible photo watermark to event time."""
    fallback = received_at
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=TW_TZ)
    else:
        fallback = fallback.astimezone(TW_TZ)
    try:
        confidence = float(parsed.get("observed_at_confidence", 0) or 0)
        if not 0.85 <= confidence <= 1:
            return fallback, "line_timestamp"
        raw = str(parsed.get("observed_at") or "").strip()
        candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if candidate.tzinfo is None:
            return fallback, "line_timestamp"
        candidate = candidate.astimezone(TW_TZ).replace(microsecond=0)
    except (TypeError, ValueError, OverflowError):
        return fallback, "line_timestamp"
    if candidate < fallback - timedelta(days=30):
        return fallback, "line_timestamp"
    if candidate > fallback + timedelta(minutes=10):
        return fallback, "line_timestamp"
    return candidate, "photo_timestamp"


def build_nutrition_vision_prompt():
    return (
        "你是一位專業的圖片資料辨識助理。先判斷圖片類型，再只回傳單一 JSON 物件，不要 markdown。\n\n"
        "image_type 只能是 garmin_workout、nutrition_label、product_front、food_photo、unknown。\n"
        "若是 Garmin 運動截圖，回傳：{\"status\":\"success\",\"image_type\":\"garmin_workout\","
        "\"workout_type\":\"跑步/室內自行車/游泳/其他\",\"duration_min\":0,\"avg_hr\":0,"
        "\"max_hr\":0,\"aerobic_te\":0,\"anaerobic_te\":0,\"primary_benefit\":\"\","
        "\"load_value\":0,\"np_w\":0,\"if_value\":0,\"tss\":0,\"ftp_w\":0}。\n"
        "若是包裝食品營養標示，請逐字辨識標示，不可猜測看不到的數值，回傳："
        "{\"status\":\"success\",\"image_type\":\"nutrition_label\",\"product_name\":\"\","
        "\"brand\":\"\",\"barcode\":\"\",\"package_amount\":0,\"package_unit\":\"g或ml等\","
        "\"servings_per_package\":1,\"per_serving\":{\"calories_kcal\":0,\"protein_g\":0,"
        "\"fat_g\":0,\"saturated_fat_g\":0,\"trans_fat_g\":0,\"cholesterol_mg\":0,"
        "\"carbohydrate_g\":0,\"sugar_g\":0,\"fiber_g\":0,\"sodium_mg\":0},"
        "\"per_100\":{\"calories_kcal\":0,\"protein_g\":0,\"fat_g\":0,"
        "\"saturated_fat_g\":0,\"trans_fat_g\":0,\"cholesterol_mg\":0,"
        "\"carbohydrate_g\":0,\"sugar_g\":0,\"fiber_g\":0,\"sodium_mg\":0},"
        "\"observed_at\":\"圖片浮水印明確顯示的Asia/Taipei ISO 8601時間，沒有則空字串\","
        "\"observed_at_confidence\":0到1,\"confidence\":0到1,\"notes\":\"\"}。\n"
        "若圖片角落有日期時間浮水印（例如2026年7月21日、晚上8:42），轉為Asia/Taipei的"
        "2026-07-21T20:42:00+08:00；只有年月日、時、分均清楚時才填observed_at，"
        "否則留空且observed_at_confidence=0，不可猜測。"
        "package_amount 必須是整個包裝容量；若只看到每份量，且本包裝含1份，可用每份量。"
        "缺少品名仍回傳 status=success，product_name與brand可留空；不可因缺少品名丟棄已讀到的營養資料。"
        "只有營養數值或容量模糊到無法安全讀取時，才回傳 status=error 並說明需補拍位置。\n"
        "若是商品正面、可看到品名但沒有完整營養表，回傳："
        "{\"status\":\"success\",\"image_type\":\"product_front\",\"product_name\":\"完整品名與口味\","
        "\"brand\":\"品牌\",\"barcode\":\"看得到才填\",\"confidence\":0到1}。不可猜測看不到的口味。\n"
        "若只有餐盤照片，回傳："
        "{\"status\":\"success\",\"image_type\":\"food_photo\","
        "\"visible_items\":[{\"name\":\"只寫畫面可見食物\",\"category\":\"vegetable/protein/starch/fruit/milk/unknown\",\"confidence\":0到1}],"
        "\"uncertain_items\":[\"看得到但無法確定種類的項目\"],"
        "\"starch_visibility\":\"visible/not_visible/unknown\","
        "\"oil_sauce_status\":\"visible/not_visible/unknown\","
        "\"observed_at\":\"照片浮水印ISO時間或空字串\",\"observed_at_confidence\":0到1}。"
        "只描述可觀察內容，不可猜肉類品種、食材重量、未入鏡食物、烹調油量；"
        "不可估算熱量、營養素或交換份，也不可用0代表看不到或不知道。\n"
        "其他圖片回傳 status=error、image_type=unknown 和繁體中文 message。"
    )


def stage_nutrition_label(
    conn, *, user_id, parsed, source_message_id, meal_slot, consumed_at,
    consumed_time_source="line_timestamp",
):
    label = normalize_label_payload(parsed, require_product_name=False)
    if label["confidence"] < 0.65:
        raise ValueError("營養標示辨識信心不足，請重新拍攝清楚完整的標示")
    token = save_pending_label(
        conn,
        user_id=user_id,
        payload=label,
        source_message_id=str(source_message_id or ""),
        consumed_servings=1,
        meal_slot=meal_slot,
        consumed_at=consumed_at,
        consumed_time_source=consumed_time_source,
        allow_missing_identity=True,
    )
    row = conn.execute(
        """SELECT label_payload_json,status,consumed_at,consumed_time_source,
                  consumed_servings FROM pending_nutrition_logs WHERE token=? AND user_id=?""",
        (token, user_id),
    ).fetchone()
    if not row:
        raise RuntimeError("營養草稿建立失敗")
    stored_label = normalize_label_payload(
        json.loads(row[0]), require_product_name=row[1] == "pending"
    )
    return {
        "token": token,
        "label": stored_label,
        "needs_identity": row[1] == "awaiting_identity",
        "consumed_at": row[2],
        "consumed_time_source": row[3],
        "consumed_servings": float(row[4]),
    }


def pair_product_front(conn, *, user_id, parsed, source_message_id):
    result = attach_latest_pending_identity(
        conn,
        user_id=user_id,
        identity=parsed,
        message_id=str(source_message_id or ""),
    )
    row = conn.execute(
        """SELECT consumed_at,consumed_time_source,consumed_servings
           FROM pending_nutrition_logs WHERE token=? AND user_id=?""",
        (result["token"], user_id),
    ).fetchone()
    if not row:
        raise RuntimeError("營養草稿時間遺失")
    result["consumed_at"] = row[0]
    result["consumed_time_source"] = row[1]
    result["consumed_servings"] = float(row[2])
    return result


_NUTRITION_CORRECTION_FIELDS = {
    "熱量": "calories_kcal",
    "蛋白質": "protein_g",
    "脂肪": "fat_g",
    "飽和脂肪": "saturated_fat_g",
    "反式脂肪": "trans_fat_g",
    "膽固醇": "cholesterol_mg",
    "碳水": "carbohydrate_g",
    "碳水化合物": "carbohydrate_g",
    "糖": "sugar_g",
    "纖維": "fiber_g",
    "膳食纖維": "fiber_g",
    "鈉": "sodium_mg",
}


def parse_nutrition_corrections(text):
    """解析一則訊息中的多個營養修正；回傳(有效修正, 無法解析片段)。"""
    raw = re.sub(r"^修正營養\s*", "", str(text or "").strip()).strip()
    if not raw:
        return [], []
    labels = sorted(_NUTRITION_CORRECTION_FIELDS, key=len, reverse=True)
    pattern = re.compile(
        rf"({'|'.join(map(re.escape, labels))})\s*(?:[:：=]\s*)?"
        r"([0-9]+(?:\.[0-9]+)?)\s*(kcal|mg|g|大卡|毫克|公克)?",
        re.IGNORECASE,
    )
    expected_units = {
        "calories_kcal": ({"kcal", "大卡"}, "kcal"),
        "sodium_mg": ({"mg", "毫克"}, "mg"),
        "cholesterol_mg": ({"mg", "毫克"}, "mg"),
    }
    corrections = []
    consumed = []
    seen = set()
    errors = []
    for match in pattern.finditer(raw):
        label = match.group(1)
        field = _NUTRITION_CORRECTION_FIELDS[label]
        consumed.append((match.start(), match.end()))
        unit = str(match.group(3) or "").lower()
        allowed_units, display_unit = expected_units.get(
            field, ({"g", "公克"}, "g")
        )
        if unit and unit not in allowed_units:
            errors.append(f"{label}單位應為 {display_unit}")
            continue
        if field in seen:
            errors.append(f"{label}重複輸入")
            continue
        seen.add(field)
        corrections.append((field, float(match.group(2))))
    residual_chars = list(raw)
    for start, end in consumed:
        residual_chars[start:end] = " " * (end - start)
    residual = "".join(residual_chars)
    for fragment in re.split(r"[、，,；;。\n]+", residual):
        fragment = fragment.strip(" \t:：=")
        if fragment:
            errors.append(fragment)
    return corrections, errors


def parse_nutrition_correction_command(text):
    corrections, errors = parse_nutrition_corrections(text)
    if errors or len(corrections) != 1:
        return None
    return corrections[0]


def _nutrition_targets_from_plan_row(row):
    targets = {
        "calories_kcal": _sheet_number(row, "熱量目標"),
        "protein_g": _sheet_number(row, "蛋白質目標g"),
        "fat_g": _sheet_number(row, "脂肪目標g"),
        "carbohydrate_g": _sheet_number(row, "碳水目標g"),
        "milk_exchange": _sheet_number(row, "奶份"),
        "protein_low_exchange": _sheet_number(row, "低脂蛋白份"),
        "protein_medium_exchange": _sheet_number(row, "中脂蛋白份"),
        "protein_high_exchange": _sheet_number(row, "高脂蛋白份"),
        "starch_exchange": _sheet_number(row, "主食份"),
        "vegetable_exchange": _sheet_number(row, "蔬菜份"),
        "fruit_exchange": _sheet_number(row, "水果份"),
        "fat_exchange": _sheet_number(row, "油脂份"),
    }
    if str(row.get("蛋白質總份", "")).strip() != "":
        targets["protein_total_exchange"] = _sheet_number(row, "蛋白質總份")
    return targets


def get_daily_nutrition_target(user_id, target_date):
    """Prefer an all-day plan; otherwise sum the newest valid row per meal slot."""
    try:
        plan_date = datetime.fromisoformat(str(target_date)[:10]).date()
    except ValueError:
        return None
    weekday_names = {0: "週一", 1: "週二", 2: "週三", 3: "週四", 4: "週五", 5: "週六", 6: "週日"}
    weekday = weekday_names[plan_date.weekday()]
    try:
        records = _nutrition_ws("客製化營養計畫").get_all_records()
    except Exception as exc:
        print(f"⚠️ 讀取全日客製化營養計畫失敗：{type(exc).__name__}")
        raise RuntimeError("客製化營養計畫暫時無法讀取") from exc

    matching = []
    for row in records:
        if not str(row.get("plan_id", "")).strip():
            continue
        if str(row.get("User_ID", "")).strip() != user_id:
            continue
        if str(row.get("狀態", "active")).strip().lower() not in ("active", "啟用", "有效", ""):
            continue
        row_weekday = str(row.get("星期", "")).strip()
        exact_weekday = row_weekday in (weekday, str(plan_date.weekday() + 1))
        if row_weekday and not exact_weekday and row_weekday not in ("每日", "全部"):
            continue
        start = normalize_date_str(row.get("生效日期", "")).replace("/", "-")
        end = normalize_date_str(row.get("結束日期", "")).replace("/", "-")
        if start and start > plan_date.isoformat():
            continue
        if end and end < plan_date.isoformat():
            continue
        matching.append((exact_weekday, row))
    if not matching:
        return None

    def rank(item):
        exact_weekday, row = item
        return (
            1 if exact_weekday else 0,
            _sheet_number(row, "版本"),
            normalize_date_str(row.get("生效日期", "")),
        )

    all_day = [item for item in matching if str(item[1].get("餐別", "")).strip() in ("全日", "每日")]
    if all_day:
        return _nutrition_targets_from_plan_row(max(all_day, key=rank)[1])

    best_by_meal = {}
    for item in matching:
        meal = str(item[1].get("餐別", "")).strip()
        if not meal:
            continue
        previous = best_by_meal.get(meal)
        if previous is None or rank(item) > rank(previous):
            best_by_meal[meal] = item
    if not best_by_meal:
        return None

    summed = {}
    protein_total = 0.0
    has_explicit_protein_total = False
    for _exact, row in best_by_meal.values():
        row_targets = _nutrition_targets_from_plan_row(row)
        for key, value in row_targets.items():
            if key != "protein_total_exchange":
                summed[key] = round(float(summed.get(key, 0)) + float(value or 0), 4)
        if "protein_total_exchange" in row_targets:
            has_explicit_protein_total = True
            protein_total += float(row_targets["protein_total_exchange"] or 0)
        else:
            protein_total += sum(
                float(row_targets.get(key, 0) or 0)
                for key in (
                    "protein_low_exchange", "protein_medium_exchange", "protein_high_exchange"
                )
            )
    if has_explicit_protein_total:
        summed["protein_total_exchange"] = round(protein_total, 4)
    return summed


def get_active_nutrition_target(user_id, meal_slot="", target_date=None):
    """依指定攝取日期取得有效版本；精確星期／餐別優先於萬用列。"""
    meal_slot = meal_slot or current_meal_slot()
    if target_date is None:
        plan_date = tw_today()
    elif hasattr(target_date, "year") and not isinstance(target_date, str):
        plan_date = target_date
    else:
        try:
            plan_date = datetime.fromisoformat(str(target_date)[:10]).date()
        except ValueError:
            return None
    weekday_names = {0: "週一", 1: "週二", 2: "週三", 3: "週四", 4: "週五", 5: "週六", 6: "週日"}
    weekday = weekday_names[plan_date.weekday()]
    try:
        records = _nutrition_ws("客製化營養計畫").get_all_records()
    except Exception as exc:
        print(f"⚠️ 讀取客製化營養計畫失敗：{exc}")
        raise RuntimeError("客製化營養計畫暫時無法讀取") from exc

    matching = []
    for row in records:
        plan_id = str(row.get("plan_id", "")).strip()
        if not plan_id or str(row.get("User_ID", "")).strip() != user_id:
            continue
        if str(row.get("狀態", "active")).strip().lower() not in ("active", "啟用", "有效", ""):
            continue
        row_weekday = str(row.get("星期", "")).strip()
        exact_weekday = row_weekday in (weekday, str(plan_date.weekday() + 1))
        if row_weekday and not exact_weekday and row_weekday not in ("每日", "全部"):
            continue
        row_meal = str(row.get("餐別", "")).strip()
        exact_meal = row_meal == meal_slot
        daily_scope = row_meal in ("全日", "每日")
        if row_meal and not exact_meal and not daily_scope:
            continue
        start = normalize_date_str(row.get("生效日期", "")).replace("/", "-")
        end = normalize_date_str(row.get("結束日期", "")).replace("/", "-")
        if start and start > plan_date.isoformat():
            continue
        if end and end < plan_date.isoformat():
            continue
        specificity = (1 if exact_meal else 0, 1 if exact_weekday else 0)
        matching.append((specificity, row))
    if not matching:
        return None
    matching.sort(
        key=lambda item: (
            item[0][0], item[0][1], _sheet_number(item[1], "版本"),
            str(item[1].get("生效日期", "")),
        ),
        reverse=True,
    )
    row = matching[0][1]
    row_meal = str(row.get("餐別", "")).strip()
    daily_scope = row_meal in ("全日", "每日")
    targets = _nutrition_targets_from_plan_row(row)
    return {
        "plan_id": str(row.get("plan_id", "")).strip(),
        "meal_slot": "全日" if daily_scope else meal_slot,
        "consumption_meal_slot": "" if daily_scope else meal_slot,
        "target_date": plan_date.isoformat(),
        "targets": targets,
        "row": row,
    }


def retry_pending_nutrition_plan_links(limit=50):
    """重新連結因Google Sheet暫時失敗而未能判定的歷史計畫。"""
    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        rows = conn.execute(
            """SELECT log_id, user_id, meal_slot, consumed_at FROM food_logs
               WHERE plan_link_status='pending' ORDER BY created_at LIMIT ?""",
            (int(limit),),
        ).fetchall()
    updated = 0
    for log_id, user_id, meal_slot, consumed_at in rows:
        try:
            plan = get_active_nutrition_target(user_id, meal_slot or "", consumed_at or "")
        except Exception as exc:
            print(f"⚠️ 計畫連結重試仍失敗 {log_id}: {exc}")
            continue
        plan_id = plan["plan_id"] if plan else ""
        status = "linked" if plan else "no_plan"
        with sqlite3.connect(DB_PATH) as conn:
            changed = conn.execute(
                """UPDATE food_logs SET plan_id=?, plan_link_status=?, updated_at=?
                   WHERE log_id=? AND plan_link_status='pending'""",
                (plan_id, status, tw_now().isoformat(timespec="seconds"), log_id),
            ).rowcount
            if changed:
                _queue_nutrition_outbox(conn, "food_log", log_id)
                updated += 1
            conn.commit()
    return updated


def nutrition_menu_recommendations(user_id, meal_slot=""):
    plan = get_active_nutrition_target(user_id, meal_slot, tw_today())
    if not plan:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        consumed = daily_consumed_totals(
            conn, user_id=user_id, date_iso=plan["target_date"],
            meal_slot=plan["consumption_meal_slot"],
        )
    remaining = remaining_targets(plan["targets"], consumed)
    with sqlite3.connect(DB_PATH) as conn:
        restriction_row = conn.execute("SELECT restrictions FROM health_profile WHERE user_id=?", (user_id,)).fetchone()
    restrictions = str(restriction_row[0] if restriction_row else "").strip()
    restriction_terms = [x.strip() for x in re.split(r"[,，、/]|不吃|過敏", restrictions) if len(x.strip()) >= 2]
    candidates = []
    for dish in MAIN_DISHES:
        if dish.get("category") != "main":
            continue
        haystack = f"{dish.get('name', '')} {dish.get('ingredients', '')}"
        candidate = dict(dish)
        candidate["safe"] = not any(term in haystack for term in restriction_terms)
        candidates.append(candidate)
    ranked = rank_menu_candidates(remaining, candidates, limit=3)
    return {"plan": plan, "consumed": consumed, "remaining": remaining, "ranked": ranked}


def build_nutrition_recommendation_flex(recommendation):
    meal_slot = recommendation["plan"]["meal_slot"]
    remaining = recommendation["remaining"]
    ranked = recommendation["ranked"]
    target_lines = []
    exchange_labels = [
        ("starch_exchange", "主"), ("protein_low_exchange", "低脂蛋"),
        ("protein_medium_exchange", "中脂蛋"), ("protein_high_exchange", "高脂蛋"),
        ("vegetable_exchange", "菜"), ("fruit_exchange", "果"),
        ("milk_exchange", "奶"), ("fat_exchange", "油"),
    ]
    for key, label in exchange_labels:
        if remaining.get(key, 0) > 0:
            target_lines.append(f"{label}{remaining[key]:g}")
    if not target_lines:
        target_lines = [
            f"{remaining.get('calories_kcal', 0):g} kcal",
            f"蛋白質 {remaining.get('protein_g', 0):g}g",
        ]
    contents = [
        {"type": "text", "text": f"{meal_slot}剩餘需求", "size": "sm", "color": "#666666"},
        {"type": "text", "text": "・".join(target_lines), "size": "lg", "weight": "bold", "wrap": True, "margin": "sm"},
        {"type": "separator", "margin": "md"},
    ]
    if not ranked:
        contents.append({"type": "text", "text": "目前沒有符合禁忌與供應條件的餐點。", "wrap": True, "margin": "md"})
    for index, dish in enumerate(ranked, 1):
        contents.extend([
            {"type": "text", "text": f"{index}. {dish['name']}", "weight": "bold", "size": "md", "margin": "md", "wrap": True},
            {"type": "text", "text": f"符合度 {dish['match_score']:g}%｜{dish.get('calories_kcal', 0):g} kcal｜蛋白質 {dish.get('protein_g', 0):g}g｜${dish.get('price', 0)}", "size": "xs", "color": "#555555", "wrap": True, "margin": "xs"},
        ])
    contents.append({"type": "text", "text": "份量代號未完整建檔的菜單會以熱量與三大營養素後備比對；營養師補齊份量後排序會更準。", "size": "xs", "color": "#8A6D3B", "wrap": True, "margin": "lg"})
    return {
        "type": "bubble", "size": "mega",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#D97706", "paddingAll": "16px", "contents": [
            {"type": "text", "text": "🍱 一日樂食個人推薦", "color": "#FFFFFF", "weight": "bold", "size": "lg"},
            {"type": "text", "text": "已扣除今天確認過的飲食紀錄", "color": "#FEF3C7", "size": "sm", "margin": "xs"},
        ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "18px", "contents": contents},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "14px", "contents": [
            {"type": "button", "style": "primary", "color": "#D97706", "action": {"type": "message", "label": "重新計算", "text": "推薦一日樂食"}},
        ]},
    }


def _validate_image_bytes(image_bytes):
    if not isinstance(image_bytes, (bytes, bytearray)):
        raise ValueError("圖片格式無效")
    if not 100 <= len(image_bytes) <= 10 * 1024 * 1024:
        raise ValueError("圖片大小必須介於100 bytes與10MB")
    data = bytes(image_bytes)
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("只支援 JPEG、PNG 或 WebP 圖片")


def _nutrition_image_path(image_ref):
    prefix = "nutrition-image:"
    if not str(image_ref).startswith(prefix):
        return None
    filename = str(image_ref)[len(prefix):]
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp)", filename):
        return None
    root = os.path.abspath(os.path.join(DB_DIR, "nutrition_images"))
    path = os.path.abspath(os.path.join(root, filename))
    return path if os.path.dirname(path) == root else None


MEAL_PHOTO_IMAGE_URL_TTL_SECONDS = 10 * 60
MEAL_PHOTO_MAX_PREVIEW_PIXELS = 25_000_000


def _meal_photo_image_signature(token, extension, expires, preview):
    secret = MEAL_PHOTO_IMAGE_SECRET.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("餐點照片網址簽章密鑰未設定")
    payload = f"v1\n{token}\n{extension}\n{int(expires)}\n{1 if preview else 0}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def build_meal_photo_image_url(draft, *, preview=False, now=None):
    """建立只在短時間內有效、且綁定原圖/預覽版本的餐點照片網址。"""
    token = str(draft.get("token") or "")
    image_ref = str(draft.get("source_image_ref") or "")
    if not re.fullmatch(r"[0-9a-f]{12}", token):
        raise ValueError("餐點照片 token 無效")
    match = re.fullmatch(r"nutrition-image:[0-9a-f]{32}\.(jpg|png|webp)", image_ref)
    if not match:
        raise ValueError("餐點照片參照無效")
    base = urlsplit(PUBLIC_BASE_URL)
    if (
        base.scheme != "https" or not base.hostname or base.username or base.password
        or base.path not in {"", "/"} or base.query or base.fragment
    ):
        raise ValueError("餐點照片公開網址必須是純 HTTPS 網域")
    source_extension = match.group(1)
    output_extension = "jpg" if preview or source_extension == "webp" else source_extension
    issued_at = int(datetime.now(TW_TZ).timestamp() if now is None else now)
    try:
        draft_expires = int(datetime.fromisoformat(str(draft.get("expires_at") or "")).timestamp())
    except (TypeError, ValueError) as exc:
        raise ValueError("餐點照片草稿期限無效") from exc
    expires = min(issued_at + MEAL_PHOTO_IMAGE_URL_TTL_SECONDS, draft_expires)
    if expires <= issued_at:
        raise ValueError("餐點照片草稿已過期")
    signature = _meal_photo_image_signature(token, output_extension, expires, preview)
    return (
        f"{PUBLIC_BASE_URL.rstrip('/')}/meal-photo-image/{token}.{output_extension}"
        f"?expires={expires}&sig={signature}&preview={1 if preview else 0}"
    )


def _authorize_meal_photo_image_request(
    *, token, extension, expires, signature, preview=False, now=None
):
    current = int(datetime.now(TW_TZ).timestamp() if now is None else now)
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise HTTPException(status_code=404, detail="image not found")
    if extension not in {"jpg", "png"}:
        raise HTTPException(status_code=404, detail="image not found")
    try:
        expires = int(expires)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="invalid image signature") from exc
    if expires < current:
        raise HTTPException(status_code=410, detail="image URL expired")
    if expires > current + MEAL_PHOTO_IMAGE_URL_TTL_SECONDS + 60:
        raise HTTPException(status_code=403, detail="invalid image expiry")
    expected = _meal_photo_image_signature(token, extension, expires, bool(preview))
    if not hmac.compare_digest(expected, str(signature or "")):
        raise HTTPException(status_code=403, detail="invalid image signature")

    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        row = conn.execute(
            """SELECT source_image_ref,status,expires_at FROM pending_meal_photo_drafts
               WHERE token=?""",
            (token,),
        ).fetchone()
    allowed_statuses = {"estimated", "reviewing", "review_ready"}
    if not row or row[1] not in allowed_statuses:
        raise HTTPException(status_code=410 if row else 404, detail="image unavailable")
    try:
        draft_expires = datetime.fromisoformat(str(row[2] or ""))
        draft_expires_timestamp = int(draft_expires.timestamp())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=410, detail="image unavailable") from exc
    if draft_expires_timestamp < current or expires > draft_expires_timestamp:
        raise HTTPException(status_code=410, detail="image URL expired")
    image_ref = str(row[0] or "")
    match = re.fullmatch(r"nutrition-image:[0-9a-f]{32}\.(jpg|png|webp)", image_ref)
    if not match:
        raise HTTPException(status_code=404, detail="image not found")
    source_extension = match.group(1)
    expected_extension = "jpg" if preview or source_extension == "webp" else source_extension
    if extension != expected_extension:
        raise HTTPException(status_code=404, detail="image not found")
    path = _nutrition_image_path(image_ref)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="image not found")
    return path


def _meal_photo_preview_response(path):
    """產生符合 LINE preview 1MB 限制的 JPEG，不另留永久衍生檔。"""
    from io import BytesIO
    from PIL import Image

    try:
        with Image.open(path) as image:
            if image.width * image.height > MEAL_PHOTO_MAX_PREVIEW_PIXELS:
                raise ValueError("餐點照片像素超過預覽上限")
            image.thumbnail((1200, 1200))
            if image.mode != "RGB":
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            for quality in (85, 75, 65, 55, 45):
                output = BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                data = output.getvalue()
                if len(data) <= 950 * 1024:
                    return Response(
                        content=data,
                        media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
                    )
    except Exception as exc:
        print(f"⚠️ 產生餐點照片預覽失敗：{exc}")
        raise HTTPException(status_code=404, detail="image unavailable") from exc
    raise HTTPException(status_code=413, detail="image preview too large")


@app.get("/meal-photo-image/{token}.{extension}")
def get_meal_photo_image(token: str, extension: str, expires: int, sig: str, preview: bool = False):
    path = _authorize_meal_photo_image_request(
        token=token,
        extension=extension,
        expires=expires,
        signature=sig,
        preview=preview,
    )
    if preview or path.lower().endswith(".webp"):
        return _meal_photo_preview_response(path)
    media_type = "image/jpeg" if extension == "jpg" else "image/png"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


def _store_nutrition_image(image_bytes, extension, image_ref=""):
    root = os.path.join(DB_DIR, "nutrition_images")
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    ref = image_ref or f"nutrition-image:{secrets.token_hex(16)}{extension}"
    if not ref.endswith(extension):
        raise ValueError("圖片參照與格式不一致")
    path = _nutrition_image_path(ref)
    if not path:
        raise RuntimeError("無法建立安全的圖片路徑")
    if os.path.exists(path):
        try:
            with open(path, "rb") as existing_file:
                existing_bytes = existing_file.read(10 * 1024 * 1024 + 1)
            existing_extension, _ = _validate_image_bytes(existing_bytes)
            if existing_extension == extension:
                return ref
        except (OSError, ValueError):
            pass
    temp_path = f"{path}.{secrets.token_hex(8)}.tmp"
    fd = None
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as image_file:
            fd = None
            image_file.write(image_bytes)
            image_file.flush()
            os.fsync(image_file.fileno())
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return ref
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _delete_nutrition_image(image_ref):
    path = _nutrition_image_path(image_ref)
    if not path or not os.path.exists(path):
        return True
    try:
        os.remove(path)
        return True
    except OSError as exc:
        print(f"⚠️ 刪除營養圖片失敗，保留參照供下次重試：{exc}")
        return False


def _queue_nutrition_outbox(conn, entity_type, entity_id):
    now = tw_now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO nutrition_sheet_outbox
           (outbox_id, entity_type, entity_id, status, attempts, last_error,
            claimed_at, lease_owner, resync_required, created_at, synced_at)
           VALUES (?, ?, ?, 'pending', 0, '', '', '', 0, ?, '')
           ON CONFLICT(entity_type, entity_id) DO UPDATE SET
             status=CASE WHEN status='processing' THEN status ELSE 'pending' END,
             resync_required=CASE WHEN status='processing' THEN 1 ELSE resync_required END,
             claimed_at=CASE WHEN status='processing' THEN claimed_at ELSE '' END,
             lease_owner=CASE WHEN status='processing' THEN lease_owner ELSE '' END,
             synced_at=CASE WHEN status='processing' THEN synced_at ELSE '' END""",
        (f"outbox_{secrets.token_hex(8)}", entity_type, entity_id, now),
    )


def cleanup_nutrition_images():
    """刪除成功後才清除參照；失敗的檔案會在下一輪再次嘗試。"""
    image_root = os.path.join(DB_DIR, "nutrition_images")
    if os.path.isdir(image_root):
        for filename in os.listdir(image_root):
            temp_path = os.path.join(image_root, filename)
            if not filename.endswith(".tmp") or not os.path.isfile(temp_path):
                continue
            try:
                if tw_now().timestamp() - os.path.getmtime(temp_path) > 3600:
                    os.remove(temp_path)
            except OSError:
                pass
    now_dt = tw_now()
    cutoff = (now_dt - timedelta(days=90)).isoformat(timespec="seconds")

    def is_before(value, reference):
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value)
            comparable = reference.astimezone(parsed.tzinfo) if parsed.tzinfo else reference.replace(tzinfo=None)
            return parsed < comparable
        except (TypeError, ValueError):
            return True

    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        candidates = conn.execute(
            """SELECT token,source_image_ref,status,expires_at,retired_at
               FROM pending_nutrition_logs
               WHERE status IN ('pending','awaiting_identity','expired','cancelled')"""
        ).fetchall()
        pending_rows = []
        now_text = now_dt.isoformat(timespec="seconds")
        for token, source_image_ref, status, expires_at, retired_at in candidates:
            should_retire = status in {"expired", "cancelled"}
            if status in {"pending", "awaiting_identity"} and is_before(expires_at, now_dt):
                status = "expired"
                should_retire = True
            if not should_retire:
                continue
            conn.execute(
                """UPDATE pending_nutrition_logs
                   SET status=?,label_payload_json='{}',
                       retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END
                   WHERE token=?""",
                (status, now_text, token),
            )
            conn.execute("DELETE FROM nutrition_input_states WHERE token=?", (token,))
            if source_image_ref:
                pending_rows.append((token, source_image_ref))
        input_states = conn.execute(
            "SELECT user_id,expires_at FROM nutrition_input_states"
        ).fetchall()
        for state_user_id, state_expires_at in input_states:
            if is_before(state_expires_at, now_dt):
                conn.execute(
                    "DELETE FROM nutrition_input_states WHERE user_id=?", (state_user_id,)
                )
        conn.execute(
            """DELETE FROM nutrition_input_states
               WHERE token IN (
                 SELECT token FROM pending_nutrition_logs
                 WHERE status NOT IN ('pending','awaiting_identity')
               )"""
        )
        conn.commit()
    for token, ref in pending_rows:
        if _delete_nutrition_image(ref):
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE pending_nutrition_logs SET source_image_ref='' WHERE token=? AND source_image_ref=?",
                    (token, ref),
                )
                conn.commit()

    # 無標示餐點草稿同樣遵守24小時到期與delete-first/reference-clear-second。
    meal_photo_rows = []
    with sqlite3.connect(DB_PATH) as conn:
        ensure_meal_photo_schema(conn)
        now_text = now_dt.isoformat(timespec="seconds")
        candidates = conn.execute(
            """SELECT token,user_id,source_image_ref,status,expires_at
               FROM pending_meal_photo_drafts
               WHERE status IN ('awaiting_confirmation','confirming','estimated','expired','cancelled')"""
        ).fetchall()
        for token, user_id, ref, status, expires_at in candidates:
            should_retire = status in {"expired", "cancelled"}
            if status in {"awaiting_confirmation", "confirming", "estimated"} and is_before(expires_at, now_dt):
                status = "expired"
                should_retire = True
            if not should_retire:
                continue
            conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET status=?,observed_payload_json='{}',answers_json='{}',estimate_json='{}',
                       retired_at=CASE WHEN retired_at='' THEN ? ELSE retired_at END,
                       updated_at=? WHERE token=? AND user_id=?""",
                (status, now_text, now_text, token, user_id),
            )
            if ref:
                meal_photo_rows.append((token, user_id, ref))
        conn.commit()
    for token, user_id, ref in meal_photo_rows:
        if _delete_nutrition_image(ref):
            with sqlite3.connect(DB_PATH) as conn:
                clear_meal_photo_image_ref(
                    conn, user_id=user_id, token=token, expected_ref=ref
                )

    meal_tombstone_cutoff = now_dt - timedelta(days=30)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_meal_photo_schema(conn)
        old_events = conn.execute(
            """SELECT event_id,created_at FROM meal_photo_events
               WHERE token IN (
                 SELECT token FROM pending_meal_photo_drafts
                 WHERE status IN ('expired','cancelled')
               )"""
        ).fetchall()
        for event_id, created_at in old_events:
            if is_before(created_at, meal_tombstone_cutoff):
                conn.execute("DELETE FROM meal_photo_events WHERE event_id=?", (event_id,))
        tombstones = conn.execute(
            """SELECT token,retired_at,source_image_ref
               FROM pending_meal_photo_drafts WHERE status IN ('expired','cancelled')"""
        ).fetchall()
        for token, retired_at, source_image_ref in tombstones:
            has_events = conn.execute(
                "SELECT 1 FROM meal_photo_events WHERE token=? LIMIT 1", (token,)
            ).fetchone()
            if (
                not source_image_ref and not has_events
                and is_before(retired_at, meal_tombstone_cutoff)
            ):
                conn.execute("DELETE FROM pending_meal_photo_drafts WHERE token=?", (token,))
        conn.commit()

    event_cutoff = now_dt - timedelta(days=30)
    # event以token外鍵連到草稿；最小tombstone須與event同保留30天，
    # 不能先刪父列而連帶提早移除尚未到期的冪等證據。
    tombstone_cutoff = event_cutoff
    with sqlite3.connect(DB_PATH) as conn:
        for message_id, created_at in conn.execute(
            "SELECT message_id,created_at FROM nutrition_message_events"
        ).fetchall():
            if is_before(created_at, event_cutoff):
                conn.execute(
                    "DELETE FROM nutrition_message_events WHERE message_id=?", (message_id,)
                )
        tombstones = conn.execute(
            """SELECT token,retired_at,source_image_ref FROM pending_nutrition_logs
               WHERE status IN ('expired','cancelled')"""
        ).fetchall()
        for token, retired_at, source_image_ref in tombstones:
            has_retained_events = conn.execute(
                "SELECT 1 FROM nutrition_message_events WHERE token=? LIMIT 1", (token,)
            ).fetchone()
            if (
                not source_image_ref
                and not has_retained_events
                and is_before(retired_at, tombstone_cutoff)
            ):
                conn.execute("DELETE FROM nutrition_input_states WHERE token=?", (token,))
                conn.execute("DELETE FROM pending_nutrition_logs WHERE token=?", (token,))
        conn.commit()

    with sqlite3.connect(DB_PATH) as conn:
        old_logs = conn.execute(
            """SELECT log_id, food_id, source_image_ref FROM food_logs
               WHERE created_at<? AND source_image_ref<>''""",
            (cutoff,),
        ).fetchall()
    for log_id, food_id, ref in old_logs:
        if not _delete_nutrition_image(ref):
            continue
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE food_logs SET source_image_ref='' WHERE log_id=? AND source_image_ref=?",
                (log_id, ref),
            )
            conn.execute(
                "UPDATE food_catalog SET original_image_ref='' WHERE food_id=? AND original_image_ref=?",
                (food_id, ref),
            )
            _queue_nutrition_outbox(conn, "food", food_id)
            _queue_nutrition_outbox(conn, "food_log", log_id)
            conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 🌟 Phase 1：教練運動日誌 — LINE 圖片處理
# ─────────────────────────────────────────────────────────────────────────────
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """收到 Garmin 截圖 → GPT-4o Vision 解析 → 寫入 SQLite → 回覆用戶"""
    uid = event.source.user_id
    message_id = event.message.id
    if message_id in processed_messages:
        return
    if len(processed_messages) >= 1000:
        processed_messages.clear()
    processed_messages.add(message_id)

    try:
        cleanup_nutrition_images()
        # Step 1：下載 LINE 圖片原始 binary
        message_content = line_bot_api.get_message_content(message_id)
        image_bytes = message_content.content
        extension = _validate_image_bytes(image_bytes)

        # Step 2：轉成 base64（不另建暫存檔）
        import base64
        b64_str = base64.b64encode(image_bytes).decode("utf-8")
        mime_type = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[extension]
        data_url = f"data:{mime_type};base64,{b64_str}"

        # Step 3：GPT-4o Vision 解析
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"}
                        },
                        {
                            "type": "text",
                            "text": build_nutrition_vision_prompt()
                        }
                    ]
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=1200
        )

        raw = response.choices[0].message.content.strip()
        # 清理可能的 markdown code block
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()

        parsed = json.loads(raw)
        del b64_str, data_url  # 立刻釋放記憶體

        # Step 4：依圖片類型分流
        image_type = parsed.get("image_type", "unknown")
        if parsed.get("status") == "error":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=parsed.get("message", "無法辨識這張圖片，請確認餐點照片、營養標示或 Garmin 數據是否清楚。"))
            )
            return

        if image_type == "nutrition_label":
            try:
                received_at = datetime.fromtimestamp(float(event.timestamp) / 1000, TW_TZ)
            except (AttributeError, TypeError, ValueError, OverflowError):
                received_at = tw_now()
            consumed_time, consumed_time_source = resolve_nutrition_consumed_at(
                parsed, received_at=received_at
            )
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                staged = stage_nutrition_label(
                    conn,
                    user_id=uid,
                    parsed=parsed,
                    source_message_id=str(message_id),
                    meal_slot=current_meal_slot(consumed_time),
                    consumed_at=consumed_time.isoformat(timespec="seconds"),
                    consumed_time_source=consumed_time_source,
                )
                token = staged["token"]
                label = staged["label"]
                existing_ref = conn.execute(
                    "SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?",
                    (token,),
                ).fetchone()
                source_image_ref = existing_ref[0] if existing_ref else ""
                if not source_image_ref:
                    proposed_ref = f"nutrition-image:{secrets.token_hex(16)}{extension}"
                    changed = conn.execute(
                        """UPDATE pending_nutrition_logs SET source_image_ref=?
                           WHERE token=? AND source_image_ref=''""",
                        (proposed_ref, token),
                    ).rowcount
                    conn.commit()
                    if changed == 1:
                        source_image_ref = proposed_ref
                    else:
                        refreshed = conn.execute(
                            "SELECT source_image_ref FROM pending_nutrition_logs WHERE token=?",
                            (token,),
                        ).fetchone()
                        source_image_ref = refreshed[0] if refreshed else ""
                if not source_image_ref:
                    raise RuntimeError("無法保留營養圖片參照")
                image_path = _nutrition_image_path(source_image_ref)
                if not image_path or not os.path.exists(image_path):
                    _store_nutrition_image(image_bytes, extension, source_image_ref)
            if staged["needs_identity"]:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=(
                            "✅ 已讀到營養標示並暫存。\n"
                            "請再拍商品正面（要看得到完整品名與口味），我會自動合併成同一筆。\n"
                            "也可以點「輸入品名」後直接輸入商品名稱。"
                        ),
                        quick_reply=QuickReply(items=[
                            QuickReplyButton(action=MessageAction(label="輸入品名", text=f"修改營養品名:{token}")),
                            QuickReplyButton(action=MessageAction(label="取消", text=f"取消營養紀錄:{token}")),
                        ]),
                    ),
                )
                return
            from linebot.models import FlexSendMessage
            line_bot_api.reply_message(
                event.reply_token,
                FlexSendMessage(
                    alt_text=f"請確認營養標示：{label['product_name']}",
                    contents=build_label_confirmation_bubble(
                        label,
                        token=token,
                        consumed_servings=staged["consumed_servings"],
                        consumed_at=staged["consumed_at"],
                        consumed_time_source=staged["consumed_time_source"],
                    ),
                ),
            )
            return

        if image_type == "product_front":
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    ensure_nutrition_schema(conn)
                    paired = pair_product_front(
                        conn,
                        user_id=uid,
                        parsed=parsed,
                        source_message_id=str(message_id),
                    )
                from linebot.models import FlexSendMessage
                line_bot_api.reply_message(
                    event.reply_token,
                    FlexSendMessage(
                        alt_text=f"請確認營養標示：{paired['label']['product_name']}",
                        contents=build_label_confirmation_bubble(
                            paired["label"],
                            token=paired["token"],
                            consumed_servings=paired["consumed_servings"],
                            consumed_at=paired["consumed_at"],
                            consumed_time_source=paired["consumed_time_source"],
                        ),
                    ),
                )
            except ValueError as exc:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"⚠️ {exc}。請先上傳商品背面的完整營養標示。"),
                )
            return

        if image_type == "food_photo":
            observed = normalize_meal_photo_payload(parsed)
            try:
                received_at = datetime.fromtimestamp(float(event.timestamp) / 1000, TW_TZ)
            except (AttributeError, TypeError, ValueError, OverflowError):
                received_at = tw_now()
            consumed_time, consumed_time_source = resolve_nutrition_consumed_at(
                observed, received_at=received_at
            )
            proposed_ref = f"nutrition-image:{secrets.token_hex(16)}{extension}"
            with sqlite3.connect(DB_PATH) as conn:
                ensure_meal_photo_schema(conn)
                token = save_meal_photo_draft(
                    conn,
                    user_id=uid,
                    source_message_id=str(message_id),
                    payload=observed,
                    source_image_ref=proposed_ref,
                    meal_slot=current_meal_slot(consumed_time),
                    consumed_at=consumed_time.isoformat(timespec="seconds"),
                    consumed_time_source=consumed_time_source,
                )
                draft = get_meal_photo_draft(conn, user_id=uid, token=token)
            source_image_ref = draft["source_image_ref"]
            image_path = _nutrition_image_path(source_image_ref)
            if not image_path:
                raise RuntimeError("餐點圖片參照無效")
            if not os.path.exists(image_path):
                _store_nutrition_image(image_bytes, extension, source_image_ref)
            from linebot.models import FlexSendMessage
            line_bot_api.reply_message(
                event.reply_token,
                FlexSendMessage(
                    alt_text="餐點照片已辨識，請確認內容",
                    contents=build_meal_photo_confirmation_bubble(
                        draft["payload"], token=token, consumed_at=draft["consumed_at"],
                        version=draft["version"],
                    ),
                ),
            )
            return

        if image_type != "garmin_workout":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="目前可辨識餐點照片、Garmin 運動截圖或包裝食品營養標示，請重新拍攝清楚完整的圖片。"),
            )
            return
        parsed = normalize_garmin_payload(parsed)

        # Step 5：寫入 SQLite（Garmin）
        # 🌟 檢查這個人是不是剛剛選了補登日期？
        if uid in pending_image_date:
            workout_date = pending_image_date[uid] # 取出補登日期 (例如昨天的日期)
            del pending_image_date[uid] # 用完就刪除狀態，以免影響他下次傳圖
            is_makeup = True # 標記這是補登
        else:
            workout_date = tw_today().isoformat() # 如果沒有補登狀態，預設就是今天
            is_makeup = False
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO workout_records
                (user_id, workout_date, workout_type, duration_min,
                 avg_hr, max_hr, aerobic_te, anaerobic_te,
                 primary_benefit, load_value, np_w, if_value, tss, ftp_w, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                uid,
                workout_date,
                parsed.get("workout_type", "未知"),
                parsed.get("duration_min", 0),
                parsed.get("avg_hr", 0),
                parsed.get("max_hr", 0),
                parsed.get("aerobic_te", 0),
                parsed.get("anaerobic_te", 0),
                parsed.get("primary_benefit", ""),
                parsed.get("load_value", 0),
                parsed.get("np_w", 0),
                parsed.get("if_value", 0),
                parsed.get("tss", 0),
                parsed.get("ftp_w", 0),
                datetime.now(TW_TZ).isoformat()
            ))
            conn.commit()

            # 順便查姓名（給 Sheet 寫入用）
            c.execute("SELECT name FROM health_profile WHERE user_id=?", (uid,))
            name_row = c.fetchone()
            user_name = name_row[0] if name_row else ""

        # Step 6：寫入 Google Sheet 測試分頁（Phase 2）
        workout_data = {
            "workout_date": workout_date,
            "name": user_name,
            "workout_type": parsed.get("workout_type", ""),
            "duration_min": parsed.get("duration_min", 0),
            "avg_hr": parsed.get("avg_hr", 0),
            "max_hr": parsed.get("max_hr", 0),
            "aerobic_te": parsed.get("aerobic_te", 0),
            "anaerobic_te": parsed.get("anaerobic_te", 0),
            "primary_benefit": parsed.get("primary_benefit", ""),
            "load_value": parsed.get("load_value", 0),
            "np_w": parsed.get("np_w", 0),
            "if_value": parsed.get("if_value", 0),
            "tss": parsed.get("tss", 0),
            "ftp_w": parsed.get("ftp_w", 0),
            "created_at": datetime.now(TW_TZ).isoformat()
        }
        write_workout_to_sheet(uid, workout_data)

        # Step 7：回覆用戶摘要
        wt = parsed.get("workout_type", "運動")
        dur = parsed.get("duration_min", 0)
        avg = parsed.get("avg_hr", 0)
        load = parsed.get("load_value", 0)
        te = parsed.get("aerobic_te", 0)
        benefit = parsed.get("primary_benefit", "")

        reply_lines = [
            f"✅ 教練已收到並記錄！(日期：{workout_date})\n" if not is_makeup else f"✅ 補登成功！已將此紀錄歸檔至 {workout_date} 喔！\n",
            f"🚴 {wt}",
            f"⏱️ 時長：{dur} 分鐘",
            f"❤️ 平均心率：{avg} bpm",
            f"📊 訓練效果：{te}（{benefit}）",
            f"⚡ 運動負荷：{load}",
        ]
        # 自行車附加欄位
        if parsed.get("np_w", 0) > 0:
            reply_lines.append(f"🔋 NP：{parsed['np_w']}W｜IF：{parsed['if_value']}｜TSS：{parsed['tss']}")
        reply_lines.append(f"\n完整數據已寫入你的運動日誌，教練隨時可以調閱 💪")

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(reply_lines)))

    except (json.JSONDecodeError, ValueError) as exc:
        print(f"⚠️ 圖片內容驗證失敗：{exc}")
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="😵 圖片資料無法安全辨識，請重新拍攝清楚完整的餐點照片、營養標示或 Garmin 截圖。")
            )
        except Exception:
            processed_messages.discard(message_id)
            raise
    except Exception as exc:
        processed_messages.discard(message_id)
        print(f"⚠️ 圖片處理暫時性錯誤，允許 LINE 重送：{exc}")
        raise


MEAL_PHOTO_STEP_QUESTIONS = {
    "scope": "這餐是否還有照片外、未入鏡的食物或飲料？",
    "protein_type": "主要蛋白質食物是哪一類？若看不出來請選『不確定』。",
    "protein_portion": "蛋白質食物大約有幾個手掌大？",
    "starch_portion": "這餐主食大約多少？照片沒拍到但有吃，也請照實選擇。",
    "vegetable_portion": "蔬菜大約有幾碗？",
    "cooking_oil": "這餐的烹調用油大約如何？照片看不出來請選『不確定』。",
    "sauce_level": "湯汁／醬汁大約吃了多少？",
}

MEAL_PHOTO_REVIEW_QUESTIONS = {
    "protein_class": "請選擇蛋白質分類：",
    "protein_exchange": "請選擇蛋白質食物正式份量：",
    "starch_exchange": "請選擇主食正式份量：",
    "vegetable_exchange": "請選擇蔬菜正式份量：",
    "milk_exchange": "請選擇奶類正式份量：",
    "fruit_exchange": "請選擇水果正式份量：",
}

# ── 快速早餐組合 ──
# key = 用戶在LINE輸入的文字；items = [(品名搜尋關鍵字, 份量), ...]
BREAKFAST_COMBOS = {
    "早餐1": [
        ("穀麥高粱", 2.0),
        ("草莓穀物脆片", 0.5),
        ("無糖優格", 2.0),
    ],
    "早餐2": [
        ("穀麥高粱", 2.0),
        ("草莓穀物脆片", 1.0),
        ("無糖優格", 1.0),
    ],
}


def build_meal_photo_step_message(token, step, version=1):
    options = meal_photo_step_options(token, step, version=version)
    items = [
        QuickReplyButton(
            action=PostbackAction(
                label=item["label"], data=item["data"], display_text=item["label"]
            )
        )
        for item in options
    ]
    items.append(
        QuickReplyButton(
            action=PostbackAction(
                label="取消", data=f"mp:v1:{token}:{int(version)}:cancel",
                display_text="取消餐點照片",
            )
        )
    )
    return TextSendMessage(
        text=MEAL_PHOTO_STEP_QUESTIONS[step], quick_reply=QuickReply(items=items)
    )


def build_food_search_result_bubble(item, *, action_data="", action_label="快速記錄"):
    """食物搜尋結果的單張Flex bubble。"""
    name = item["product_name"]
    brand = item.get("brand") or ""
    source_type = item.get("source_type") or ""
    exchange = item.get("exchange") or {}
    per_serving = item.get("per_serving") or {}
    use_count = item.get("use_count") or 0
    last_at = str(item.get("last_consumed_at") or "")
    if last_at and "T" in last_at:
        last_at = last_at.split("T")[0]
    food_id = item["food_id"]

    if source_type == "user_meal_photo":
        header_text = "📷 餐點照片"
        header_color = "#FFF3CD"
        text_color = "#7A4E00"
    else:
        header_text = "🏷️ 包裝食品"
        header_color = "#DBEAFE"
        text_color = "#1E3A5F"

    def _kv(label, value):
        return {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": label, "size": "xs", "color": "#555555", "flex": 3},
            {"type": "text", "text": str(value), "size": "xs", "color": "#111111", "align": "end", "flex": 2},
        ]}

    body = []
    if brand:
        body.append(_kv("品牌", brand))
    if per_serving.get("calories_kcal"):
        body.append(_kv("每份熱量", f"{float(per_serving['calories_kcal']):.0f} kcal"))
        body.append(_kv("每份蛋白質", f"{float(per_serving.get('protein_g', 0)):.1f}g"))
    for key, label in (
        ("starch_exchange", "主食"), ("protein_medium_exchange", "中脂蛋白"),
        ("protein_low_exchange", "低脂蛋白"), ("protein_high_exchange", "高脂蛋白"),
        ("vegetable_exchange", "蔬菜"), ("fruit_exchange", "水果"), ("milk_exchange", "奶類"),
    ):
        val = float(exchange.get(key, 0) or 0)
        if val > 0:
            body.append(_kv(label, f"{val:g}份"))
    if use_count > 0:
        body.append({"type": "text", "text": f"📊 累計吃過 {use_count} 次", "size": "xs", "color": "#777777"})
    if last_at:
        body.append({"type": "text", "text": f"📅 最近：{last_at}", "size": "xs", "color": "#777777"})

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": header_color,
            "contents": [
                {"type": "text", "text": header_text, "size": "xs", "color": text_color},
                {"type": "text", "text": name[:40], "weight": "bold", "size": "sm", "color": "#111111", "wrap": True},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "xs", "contents": body},
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#0F766E", "height": "sm",
                 "action": {"type": "postback", "label": action_label,
                            "data": action_data or f"relog:v1:{food_id}:start",
                            "displayText": f"記錄：{name[:20]}"}},
            ],
        },
    }


def build_food_servings_picker(food_id, product_name="", meal_slot=""):
    def _serving_action_data(serving):
        if meal_slot in {"早餐", "午餐", "晚餐", "點心"}:
            return f"nlfood:v1:{food_id}:servings:{serving}:meal:{meal_slot}"
        return f"relog:v1:{food_id}:servings:{serving}"

    items = [
        QuickReplyButton(action=PostbackAction(
            label=f"{sv}份", data=_serving_action_data(sv),
            display_text=f"{sv}份",
        ))
        for sv in ("0.5", "1", "1.5", "2", "2.5", "3")
    ]
    items.append(QuickReplyButton(action=PostbackAction(
        label="取消", data=f"relog:v1:{food_id}:servings:cancel",
        display_text="取消快速記錄",
    )))
    prefix = f"已找到「{product_name}」。" if product_name else ""
    return TextSendMessage(
        text=f"{prefix}請選擇份量：", quick_reply=QuickReply(items=items)
    )


def _quick_log_catalog_card_once(
    *, user_id, food_id, meal_slot, event_ref, servings=None,
    amount=None, amount_unit="", display_quantity="",
):
    """Atomically quick-log one catalog food and replay the same success card."""
    durable_event_id = "quick-relog:" + hashlib.sha256(
        str(event_ref or "").encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        ensure_daily_food_ledger_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            """SELECT result_json FROM daily_food_log_events
               WHERE event_id=? AND user_id=? AND action='quick_relog'""",
            (durable_event_id, user_id),
        ).fetchone()
        if previous:
            card_state = json.loads(previous[0])
        else:
            logged_at = tw_now()
            resolved_servings = servings
            if amount is not None:
                conn.row_factory = sqlite3.Row
                catalog_row = conn.execute(
                    """SELECT food_id,product_name,owner_user_id,visibility,
                              package_amount,package_unit,servings_per_package
                       FROM food_catalog
                       WHERE food_id=? AND (owner_user_id=? OR visibility='public')""",
                    (food_id, user_id),
                ).fetchone()
                if catalog_row is None:
                    raise PermissionError("找不到可使用的食品資料")
                resolved_servings = _natural_food_servings(
                    dict(catalog_row), amount, amount_unit
                )
            if resolved_servings is None:
                raise ValueError("缺少可記錄的食品份量")
            result = quick_log_from_catalog(
                conn, user_id=user_id, food_id=food_id,
                consumed_servings=float(resolved_servings), meal_slot=meal_slot,
                consumed_at=logged_at.isoformat(timespec="seconds"),
                manage_transaction=False,
            )
            today = logged_at.date().isoformat()
            _sync_health_profile_from_ledger_conn(
                conn, user_id, today, current_date=today
            )
            version_row = conn.execute(
                "SELECT version FROM food_logs WHERE log_id=? AND user_id=?",
                (result["log_id"], user_id),
            ).fetchone()
            log_version = int(version_row[0] or 1) if version_row else 1
            try:
                hp = conn.execute(
                    """SELECT today_extra_cal,today_extra_pro,tdee,protein
                       FROM health_profile WHERE user_id=?""",
                    (user_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                hp = None
            if hp:
                daily_cal, daily_pro, tdee, protein_goal = hp
            else:
                rows = _daily_food_rows(conn, user_id, today)
                daily_cal = daily_pro = 0.0
                for row in rows:
                    snapshot = _ledger_item_from_row(row)["nutrition"]
                    daily_cal += float(snapshot.get("calories_kcal") or 0)
                    daily_pro += float(snapshot.get("protein_g") or 0)
                tdee, protein_goal = 2000, 100
            nutrition = result.get("nutrition") or {}
            quantity = str(display_quantity or "").strip()
            if not quantity:
                quantity = f"{result['consumed_servings']:g}份"
            card_state = {
                "logged_name": f"{result['product_name']} {quantity}（{result['meal_slot']}）",
                "logged_cal": round(float(nutrition["calories_kcal"]), 1)
                if nutrition.get("calories_kcal") is not None else None,
                "logged_pro": round(float(nutrition["protein_g"]), 1)
                if nutrition.get("protein_g") is not None else None,
                "daily_cal": round(float(daily_cal or 0), 1),
                "daily_pro": round(float(daily_pro or 0), 1),
                "tdee": float(tdee or 2000),
                "protein_goal": float(protein_goal or 100),
                "log_id": result["log_id"], "version": log_version,
            }
            conn.execute(
                """INSERT INTO daily_food_log_events
                   (event_id,user_id,log_id,action,result_json,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    durable_event_id, user_id, result["log_id"], "quick_relog",
                    json.dumps(card_state, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    logged_at.isoformat(timespec="seconds"),
                ),
            )
        conn.commit()
    return build_meal_log_flex(
        card_state["logged_name"], card_state.get("logged_cal"),
        card_state.get("logged_pro"), card_state["daily_cal"],
        card_state["tdee"], card_state["daily_pro"],
        card_state["protein_goal"], log_id=card_state["log_id"],
        version=card_state["version"],
    )


def _natural_unit_label(unit):
    return {
        "ml": "ml", "g": "g", "serving": "份", "package": "個包裝",
    }.get(unit, "")


def build_natural_food_log_reply(*, user_id, message_id, event, request):
    """Resolve one explicit food-log request without invoking the LLM."""
    try:
        if request["amount"] is not None:
            _validate_natural_food_amount(request["amount"], request["unit"])
        with sqlite3.connect(DB_PATH) as conn:
            ensure_nutrition_schema(conn)
            candidates, match_kind = _natural_food_candidates(
                conn, user_id, request["food_name"]
            )
        unit_mismatch = False
        if request["amount"] is not None and candidates:
            compatible = []
            for item in candidates:
                try:
                    _natural_food_servings(item, request["amount"], request["unit"])
                except ValueError:
                    continue
                compatible.append(item)
            if compatible:
                candidates = compatible
            else:
                unit_mismatch = True
                candidates = []
        if match_kind == "exact" and candidates:
            private_candidates = [
                item for item in candidates if item["owner_user_id"] == user_id
            ]
            if private_candidates:
                candidates = private_candidates
        if len(candidates) == 1 and match_kind == "exact":
            item = candidates[0]
            slot = request["meal_slot"] or current_meal_slot()
            if request["amount"] is None:
                return build_food_servings_picker(
                    item["food_id"], item["product_name"], meal_slot=slot
                )
            quantity = f"{request['amount']:g}{_natural_unit_label(request['unit'])}"
            event_ref = str(getattr(event, "webhook_event_id", "") or "").strip()
            if not event_ref:
                event_ref = f"natural-food:{user_id}:{message_id}"
            return _quick_log_catalog_card_once(
                user_id=user_id, food_id=item["food_id"],
                amount=request["amount"], amount_unit=request["unit"],
                meal_slot=slot, event_ref=event_ref, display_quantity=quantity,
            )
        if candidates:
            from linebot.models import FlexSendMessage
            slot = request["meal_slot"] or current_meal_slot()
            action_amount = request["amount"]
            bubbles = []
            for item in candidates[:11]:
                action_data = (
                    f"nlfood:v1:{item['food_id']}:servings:start:meal:{slot}"
                )
                action_label = "選擇份量"
                if action_amount is not None:
                    action_data = (
                        f"nlfood:v1:{item['food_id']}:amount:{action_amount:g}:"
                        f"{request['unit']}:meal:{slot}"
                    )
                    action_label = (
                        f"記錄 {action_amount:g}{_natural_unit_label(request['unit'])}"
                    )
                bubbles.append(build_food_search_result_bubble(
                    item, action_data=action_data, action_label=action_label
                ))
            return FlexSendMessage(
                alt_text=f"請選擇要記錄的{request['food_name']}",
                contents={"type": "carousel", "contents": bubbles},
            )
        query = request["food_name"]
        fallback_slot = request["meal_slot"] or current_meal_slot()
        amount_text = ""
        if request["amount"] is not None:
            amount_text = (
                f" {request['amount']:g}{_natural_unit_label(request['unit'])}"
            )
        if unit_mismatch:
            response_text = (
                f"⚠️ 尚未記錄：「{query}」的食品庫單位無法直接換算"
                f"{amount_text.strip()}。\n請改選一般估算，或建立相同單位的食品資料："
            )
        else:
            response_text = (
                f"🔍 食品庫找不到「{query}」。\n"
                "我不會用 UNKNOWN 假裝已記錄，請選擇下一步："
            )
        return TextSendMessage(
            text=response_text,
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(
                    label="瀏覽我的食物", text="搜尋我的食物"
                )),
                QuickReplyButton(action=CameraAction(label="拍營養標示")),
                QuickReplyButton(action=CameraRollAction(label="從相簿選擇")),
                QuickReplyButton(action=MessageAction(
                    label="使用一般估算",
                    text=f"請用一般估算記錄 {fallback_slot} {query}{amount_text}"
                )),
            ]),
        )
    except (ValueError, PermissionError) as exc:
        return TextSendMessage(text=f"⚠️ 尚未記錄：{exc}")


def build_meal_photo_review_step_message(draft, step, version=1):
    from meal_photo_system import meal_photo_review_options
    token = draft["token"]
    options = meal_photo_review_options(draft, step)
    items = [
        QuickReplyButton(
            action=PostbackAction(
                label=item["label"],
                data=f"mpr:v1:{token}:{int(version)}:set:{step}:{item['value']}",
                display_text=item["label"],
            )
        )
        for item in options
    ]
    items.append(
        QuickReplyButton(
            action=PostbackAction(
                label="退回客戶",
                data=f"mpr:v1:{token}:{int(version)}:reject",
                display_text="退回這筆餐點，請客戶重新送審",
            )
        )
    )
    items.append(
        QuickReplyButton(
            action=PostbackAction(
                label="取消審核",
                data=f"mpr:v1:{token}:{int(version)}:cancel_review",
                display_text="取消這筆審核",
            )
        )
    )
    return TextSendMessage(
        text=MEAL_PHOTO_REVIEW_QUESTIONS.get(step, "請選擇："),
        quick_reply=QuickReply(items=items),
    )


def build_meal_photo_review_ready_bubble(draft):
    from meal_photo_system import _meal_photo_exact_exchange
    token, version = draft["token"], draft["version"]
    review = draft.get("review") or {}
    exact = _meal_photo_exact_exchange(review)
    estimated = estimate_nutrition_from_exchanges(exact)
    consumed_at = str(draft.get("consumed_at") or "").replace("T", " ")[:16] or "待確認"

    def _kv(label, value):
        return {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#555555", "flex": 3, "wrap": True},
            {"type": "text", "text": str(value), "size": "sm", "color": "#111111", "align": "end", "flex": 2, "wrap": True},
        ]}

    def _exchange_label(count, label):
        return f"{count:g}份" if count else "0份"

    body_contents = [
        _kv("時間", consumed_at),
        _kv("主食", _exchange_label(exact["starch_exchange"], "主食")),
    ]
    for key, label in (
        ("protein_low_exchange", "低脂蛋白"),
        ("protein_medium_exchange", "中脂蛋白"),
        ("protein_high_exchange", "高脂蛋白"),
    ):
        if exact[key] > 0:
            body_contents.append(_kv(label, _exchange_label(exact[key], label)))
    body_contents.append(_kv("蔬菜", _exchange_label(exact["vegetable_exchange"], "蔬菜")))
    body_contents.append(_kv("水果", _exchange_label(exact["fruit_exchange"], "水果")))
    body_contents.append(_kv("奶類", _exchange_label(exact["milk_exchange"], "奶類")))
    body_contents.append({
        "type": "text",
        "text": f"📊 代換估算：{estimated['calories_kcal']:g} kcal｜蛋白質 {estimated['protein_g']:g}g",
        "size": "xs", "color": "#0F766E", "weight": "bold", "wrap": True,
    })
    body_contents.append({
        "type": "text",
        "text": f"脂肪 {estimated['fat_g']:g}g｜碳水 {estimated['carbohydrate_g']:g}g",
        "size": "xs", "color": "#555555", "wrap": True,
    })
    body_contents.append({"type": "text", "text": "⚠️ 未含未量化的烹調油與醬料", "size": "xs", "color": "#B45309", "wrap": True})
    if "milk_assumed_low_fat" in estimated.get("_warnings", []):
        body_contents.append({"type": "text", "text": "ℹ️ 奶類以低脂奶估算", "size": "xs", "color": "#1D4ED8", "wrap": True})
    body_contents.append({"type": "text", "text": "油脂份：0份（目前不計入）", "size": "xs", "color": "#777777"})
    body_contents.append({"type": "text", "text": "按『確認加入』後才會計入每日總量。", "size": "xs", "color": "#777777", "wrap": True})

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#DCFCE7",
            "contents": [{"type": "text", "text": "✅ 最終核准份量", "weight": "bold", "size": "md", "color": "#166534"}],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents},
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#0F766E",
                 "action": {"type": "postback", "label": "確認加入正式份量",
                            "data": f"mpr:v1:{token}:{version}:approve",
                            "displayText": "確認加入正式份量"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "postback", "label": "退回客戶",
                            "data": f"mpr:v1:{token}:{version}:reject",
                            "displayText": "退回這筆餐點，請客戶重新送審"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "postback", "label": "取消審核",
                            "data": f"mpr:v1:{token}:{version}:cancel_review",
                            "displayText": "取消這筆審核"}},
            ],
        },
    }


def build_meal_photo_approved_bubble(draft, result):
    token = draft["token"]
    consumed_at = str(draft.get("consumed_at") or "").replace("T", " ")[:16] or "待確認"
    exact = result.get("approved_exchange") or {}
    estimated = result.get("estimated_nutrition") or estimate_nutrition_from_exchanges(exact)

    def _kv(label, value):
        return {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#555555", "flex": 3, "wrap": True},
            {"type": "text", "text": str(value), "size": "sm", "color": "#111111", "align": "end", "flex": 2, "wrap": True},
        ]}

    body_contents = [_kv("時間", consumed_at), _kv("主食", f"{exact.get('starch_exchange', 0):g}份")]
    for key, label in (
        ("protein_low_exchange", "低脂蛋白"), ("protein_medium_exchange", "中脂蛋白"),
        ("protein_high_exchange", "高脂蛋白"),
    ):
        if exact.get(key, 0) > 0:
            body_contents.append(_kv(label, f"{exact[key]:g}份"))
    body_contents.append(_kv("蔬菜", f"{exact.get('vegetable_exchange', 0):g}份"))
    body_contents.append(_kv("水果", f"{exact.get('fruit_exchange', 0):g}份"))
    body_contents.append(_kv("奶類", f"{exact.get('milk_exchange', 0):g}份"))
    body_contents.append({
        "type": "text",
        "text": f"📊 代換估算：{estimated['calories_kcal']:g} kcal｜蛋白質 {estimated['protein_g']:g}g",
        "size": "xs", "color": "#0F766E", "weight": "bold", "wrap": True,
    })
    body_contents.append({
        "type": "text",
        "text": f"脂肪 {estimated['fat_g']:g}g｜碳水 {estimated['carbohydrate_g']:g}g",
        "size": "xs", "color": "#555555", "wrap": True,
    })
    body_contents.append({"type": "text", "text": "⚠️ 未含未量化的烹調油與醬料", "size": "xs", "color": "#B45309", "wrap": True})
    if "milk_assumed_low_fat" in estimated.get("_warnings", []):
        body_contents.append({"type": "text", "text": "ℹ️ 奶類以低脂奶估算", "size": "xs", "color": "#1D4ED8", "wrap": True})
    body_contents.append({"type": "text", "text": "✅ 已計入正式份量與每日總量", "size": "sm", "color": "#166534", "weight": "bold", "wrap": True})
    body_contents.append({"type": "text", "text": f"核准時間：{datetime.now(TW_TZ).strftime('%Y/%m/%d %H:%M')}", "size": "xs", "color": "#777777"})

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#D1FAE5",
            "contents": [{"type": "text", "text": "✅ 營養師已核准｜已計入正式份量", "weight": "bold", "size": "md", "color": "#065F46"}],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents},
    }


def build_meal_photo_admin_review_messages(draft):
    """建立跨使用者送審通知；token只放於管理員限定postback。"""
    from linebot.models import FlexSendMessage
    payload = draft.get("payload") or {}
    names = [
        str(item.get("name") or "").strip()
        for item in payload.get("visible_items", [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    foods = "、".join(names[:8]) or "未辨識餐點內容"
    owner_suffix = str(draft.get("user_id") or "")[-8:] or "NA"
    consumed_at = str(draft.get("consumed_at") or "").replace("T", " ")[:16] or "待確認"
    header = TextSendMessage(text=(
        "📷 新的餐點審核需求\n"
        f"用戶 UID 末8碼：{owner_suffix}\n"
        f"餐別：{draft.get('meal_slot') or '未指定'}\n"
        f"時間：{consumed_at}\n"
        f"辨識餐點：{foods}\n\n"
        "請按下方『審核並加入』選定正式營養份量。"
    ))
    review_card = FlexSendMessage(
        alt_text="新的餐點審核需求",
        contents=build_meal_photo_estimate_bubble(draft, allow_admin_review=True),
    )
    messages = []
    if draft.get("source_image_ref"):
        try:
            messages.append(ImageSendMessage(
                original_content_url=build_meal_photo_image_url(draft, preview=False),
                preview_image_url=build_meal_photo_image_url(draft, preview=True),
            ))
        except (RuntimeError, ValueError) as exc:
            print(f"⚠️ 餐點審核通知無法附上原始照片：{exc}")
    return [*messages, header, review_card]


def build_pending_meal_photo_review_message(uid):
    configured_admin_uid = get_bound_admin_uid_for_authorization()
    if uid != configured_admin_uid:
        raise PermissionError("管理員限定")
    with sqlite3.connect(DB_PATH) as conn:
        drafts = list_pending_meal_photo_reviews(conn, limit=10)
    if not drafts:
        return TextSendMessage(text="✅ 目前沒有待審核的餐點照片。")
    lines = [f"📷 待審餐點（{len(drafts)}筆，最多顯示10筆）"]
    buttons = []
    for index, draft in enumerate(drafts, 1):
        owner_suffix = str(draft.get("user_id") or "")[-8:] or "NA"
        consumed_at = str(draft.get("consumed_at") or "").replace("T", " ")[:16] or "待確認"
        names = [
            str(item.get("name") or "").strip()
            for item in (draft.get("payload") or {}).get("visible_items", [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        foods = "、".join(names[:4]) or "未辨識餐點"
        status = str(draft.get("status") or "")
        status_label = {
            "estimated": "待開始", "reviewing": "審核中", "review_ready": "待核准",
        }.get(status, "待處理")
        lines.append(
            f"{index}. UID末8碼 {owner_suffix}｜{draft.get('meal_slot') or '未指定'}｜{consumed_at}｜{status_label}\n   {foods}"
        )
        action = "start" if status == "estimated" else "resume"
        button_label = "審核" if status == "estimated" else "繼續"
        buttons.append(QuickReplyButton(action=PostbackAction(
            label=f"{button_label} {owner_suffix}",
            data=f"mpr:v1:{draft['token']}:{draft['version']}:{action}",
            display_text=f"{button_label} UID末8碼 {owner_suffix} 的餐點",
        )))
    return TextSendMessage(
        text="\n".join(lines),
        quick_reply=QuickReply(items=buttons),
    )


def push_meal_photo_review_request(draft):
    admin_uid = str(get_admin_notify_uid() or "").strip()
    owner_uid = str(draft.get("user_id") or "").strip()
    if not admin_uid or admin_uid == owner_uid:
        return False
    try:
        messages = build_meal_photo_admin_review_messages(draft)
    except Exception as exc:
        print(f"⚠️ 建立餐點審核通知失敗：{exc}")
        return False

    main_messages = messages
    if messages and isinstance(messages[0], ImageSendMessage):
        main_messages = messages[1:]
        try:
            # 圖片是 best effort；timeout 時不重送，避免 LINE 已接受卻造成重複照片。
            line_bot_api.push_message(admin_uid, messages[0], timeout=12)
        except Exception as exc:
            print(f"⚠️ 原始餐點照片推播失敗，繼續送文字審核通知：{exc}")
    try:
        line_bot_api.push_message(admin_uid, main_messages, timeout=12)
        return True
    except Exception as exc:
        print(f"⚠️ 推播餐點文字審核通知失敗：{exc}")
        return False


def _push_meal_photo_owner_notification_once(draft, notification_kind, text):
    owner_uid = str(draft.get("user_id") or "").strip()
    admin_uid = str(get_admin_notify_uid() or "").strip()
    token = str(draft.get("token") or "").strip()
    if not owner_uid:
        return False
    if owner_uid == admin_uid:
        return True
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            claim = claim_meal_photo_notification(
                conn, token=token, notification_kind=notification_kind
            )
    except Exception as exc:
        print(f"⚠️ 取得餐點通知租約失敗（{notification_kind}）：{exc}")
        return False
    if claim["state"] == "delivered":
        return True
    if claim["state"] == "busy":
        return False
    claim_token = claim["claim_token"]
    retry_key = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"meal-photo:{token}:{notification_kind}")
    )
    line_accepted = False
    try:
        retry_client = copy.copy(line_bot_api)
        if hasattr(retry_client, "headers"):
            retry_client.headers = dict(getattr(line_bot_api, "headers", {}) or {})
        retry_client.push_message(
            owner_uid, TextSendMessage(text=text), retry_key=retry_key, timeout=12
        )
        line_accepted = True
    except Exception as exc:
        line_accepted = (
            getattr(exc, "status_code", None) == 409
            and bool(str(getattr(exc, "accepted_request_id", "") or "").strip())
        )
        if not line_accepted:
            try:
                with sqlite3.connect(DB_PATH, timeout=10) as conn:
                    release_meal_photo_notification(
                        conn, token=token, notification_kind=notification_kind,
                        claim_token=claim_token, error=str(exc),
                    )
            except Exception as release_exc:
                print(f"⚠️ 釋放餐點通知租約失敗：{release_exc}")
            print(f"⚠️ 推播餐點結果給使用者失敗（{notification_kind}）：{exc}")
            return False
        print(
            f"ℹ️ LINE 已接受相同 retry key 的餐點通知（{notification_kind}）："
            f"{getattr(exc, 'accepted_request_id', '')}"
        )
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            completed = complete_meal_photo_notification(
                conn, token=token, notification_kind=notification_kind,
                claim_token=claim_token,
            )
        if not completed:
            print(f"⚠️ LINE 已接受餐點通知，但 delivered marker 未更新（{notification_kind}）")
    except Exception as exc:
        # LINE API 已回傳成功：保留 sending 租約且不立即重送；固定 retry_key 也由 LINE 端去重。
        print(f"⚠️ LINE 已接受餐點通知，但寫入 delivered marker 失敗（{notification_kind}）：{exc}")
    return True


def push_meal_photo_return_to_owner(draft):
    text = (
        "↩️ 你的餐點照片已由營養師退回，尚未計入正式營養紀錄。\n"
        "請重新拍攝清楚的完整餐點，或補充食材與份量後再次送審。"
    )
    return _push_meal_photo_owner_notification_once(
        draft, "owner_rejected", text
    )


def push_meal_photo_approval_to_owner(draft, result):
    exact = result.get("approved_exchange") or {}
    estimated = result.get("estimated_nutrition") or {}
    text = (
        "✅ 你的餐點已由營養師核准並計入正式紀錄。\n"
        f"主食 {float(exact.get('starch_exchange', 0) or 0):g}份｜"
        f"蔬菜 {float(exact.get('vegetable_exchange', 0) or 0):g}份\n"
        f"估算熱量 {float(estimated.get('calories_kcal', 0) or 0):g} kcal｜"
        f"蛋白質 {float(estimated.get('protein_g', 0) or 0):g}g"
    )
    return _push_meal_photo_owner_notification_once(
        draft, "owner_approved", text
    )


def build_meal_photo_notification_retry_message(draft, kind):
    label = "重試核准通知" if kind == "approved" else "重試退回通知"
    return TextSendMessage(
        text="⚠️ 餐點狀態已安全寫入，但客戶通知尚未送達。請按下方按鈕重試。",
        quick_reply=QuickReply(items=[QuickReplyButton(action=PostbackAction(
            label=label,
            data=f"mprn:v1:{draft['token']}:{kind}",
            display_text=label,
        ))]),
    )


@handler.add(PostbackEvent)
def handle_meal_photo_postback(event):
    data = str(getattr(event.postback, "data", "") or "")
    uid = event.source.user_id

    # ── nlfood:v1 自然語句候選食品選擇 ──
    natural_serving_choice = re.fullmatch(
        r"nlfood:v1:([a-z0-9_]{8,40}):servings:"
        r"(start|[0-9]+(?:\.[0-9]+)?):meal:(早餐|午餐|晚餐|點心)",
        data,
    )
    if natural_serving_choice:
        food_id = natural_serving_choice.group(1)
        serving_choice = natural_serving_choice.group(2)
        slot = natural_serving_choice.group(3)
        try:
            if serving_choice == "start":
                with sqlite3.connect(DB_PATH) as conn:
                    row = conn.execute(
                        """SELECT product_name FROM food_catalog
                           WHERE food_id=? AND (owner_user_id=? OR visibility='public')""",
                        (food_id, uid),
                    ).fetchone()
                if row is None:
                    raise PermissionError("找不到可使用的食品資料")
                reply = build_food_servings_picker(
                    food_id, row[0], meal_slot=slot
                )
            else:
                servings = _validate_natural_food_amount(
                    float(serving_choice), "serving"
                )
                event_ref = str(
                    getattr(event, "webhook_event_id", "") or ""
                ).strip()
                if not event_ref:
                    event_ref = f"{uid}|{getattr(event, 'timestamp', '')}|{data}"
                reply = _quick_log_catalog_card_once(
                    user_id=uid, food_id=food_id, servings=servings,
                    meal_slot=slot, event_ref=event_ref,
                    display_quantity=f"{servings:g}份",
                )
        except (ValueError, PermissionError) as exc:
            reply = TextSendMessage(text=f"⚠️ 尚未記錄：{exc}")
        line_bot_api.reply_message(event.reply_token, reply)
        return

    natural_choice = re.fullmatch(
        r"nlfood:v1:([a-z0-9_]{8,40}):amount:([0-9]+(?:\.[0-9]+)?):"
        r"(ml|g|serving|package):meal:(早餐|午餐|晚餐|點心)",
        data,
    )
    if natural_choice:
        food_id = natural_choice.group(1)
        amount = float(natural_choice.group(2))
        unit = natural_choice.group(3)
        slot = natural_choice.group(4)
        try:
            event_ref = str(getattr(event, "webhook_event_id", "") or "").strip()
            if not event_ref:
                event_ref = f"{uid}|{getattr(event, 'timestamp', '')}|{data}"
            reply = _quick_log_catalog_card_once(
                user_id=uid, food_id=food_id, amount=amount, amount_unit=unit,
                meal_slot=slot, event_ref=event_ref,
                display_quantity=f"{amount:g}{_natural_unit_label(unit)}",
            )
        except (ValueError, PermissionError) as exc:
            reply = TextSendMessage(text=f"⚠️ 尚未記錄：{exc}")
        line_bot_api.reply_message(event.reply_token, reply)
        return

    # ── foodlog:v1 每日飲食帳本 ──
    ledger_day = re.fullmatch(r"foodlog:v1:day:(today|yesterday):page:(\d{1,3})", data)
    ledger_item = re.fullmatch(
        r"foodlog:v1:(log_[a-f0-9]{16,32}):(\d+):(portion:start|portion:custom|nutrition:start|more|rename:start|delete:ask|delete:confirm)",
        data,
    )
    ledger_portion = re.fullmatch(
        r"foodlog:v1:(log_[a-f0-9]{16,32}):(\d+):portion:set:([0-9.]+)", data
    )
    ledger_nutrition_field = re.fullmatch(
        r"foodlog:v1:(log_[a-f0-9]{16,32}):(\d+):nutrition:field:(calories_kcal|protein_g|fat_g|carbohydrate_g)",
        data,
    )
    ledger_nutrition_apply = re.fullmatch(
        r"foodlog:v1:(log_[a-f0-9]{16,32}):(\d+):nutrition:apply:"
        r"(calories_kcal|protein_g|fat_g|carbohydrate_g):([0-9.]+):(once|save)",
        data,
    )
    ledger_slot = re.fullmatch(
        r"foodlog:v1:(log_[a-f0-9]{16,32}):(\d+):slot:(早餐|午餐|晚餐|點心)", data
    )
    if ledger_day or ledger_item or ledger_portion or ledger_nutrition_field or ledger_nutrition_apply or ledger_slot:
        try:
            if ledger_day:
                reply = build_daily_food_ledger_flex(
                    uid, ledger_day.group(1), page=int(ledger_day.group(2))
                )
            else:
                matched = ledger_item or ledger_portion or ledger_nutrition_field or ledger_nutrition_apply or ledger_slot
                assert matched is not None
                log_id, version = matched.group(1), int(matched.group(2))
                event_key = str(getattr(event, "webhook_event_id", "") or "").strip()
                if not event_key:
                    fallback = f"{uid}|{getattr(event, 'timestamp', '')}|{data}"
                    event_key = "foodlog:" + hashlib.sha256(fallback.encode()).hexdigest()
                if ledger_item:
                    action = ledger_item.group(3)
                    brief = _daily_food_log_brief(uid, log_id)
                    if brief["version"] != version:
                        raise ValueError("這筆紀錄已更新，請重新開啟最新卡片")
                    if action == "portion:start":
                        items = [
                            QuickReplyButton(action=PostbackAction(
                                label=f"{sv}份", data=f"foodlog:v1:{log_id}:{version}:portion:set:{sv}",
                                display_text=f"調整為 {sv} 份",
                            ))
                            for sv in ("0.5", "0.75", "1", "1.5", "2")
                        ]
                        items.append(QuickReplyButton(action=PostbackAction(
                            label="自訂", data=f"foodlog:v1:{log_id}:{version}:portion:custom",
                            display_text="自訂飲食份量",
                        )))
                        reply = TextSendMessage(
                            text=f"目前「{brief['product_name']}」是 {brief['servings']:g} 份，請選擇新份量：",
                            quick_reply=QuickReply(items=items),
                        )
                    elif action == "portion:custom":
                        set_daily_food_edit_state(uid, log_id, version, "portion_value")
                        reply = TextSendMessage(text="請輸入新的份數，例如：1.25")
                    elif action == "nutrition:start":
                        fields = [
                            ("熱量", "calories_kcal"), ("蛋白質", "protein_g"),
                            ("脂肪", "fat_g"), ("碳水", "carbohydrate_g"),
                        ]
                        reply = TextSendMessage(
                            text=f"要修正「{brief['product_name']}」哪一項？",
                            quick_reply=QuickReply(items=[
                                QuickReplyButton(action=PostbackAction(
                                    label=label,
                                    data=f"foodlog:v1:{log_id}:{version}:nutrition:field:{field}",
                                    display_text=f"修正{label}",
                                )) for label, field in fields
                            ]),
                        )
                    elif action == "more":
                        options = [
                            QuickReplyButton(action=PostbackAction(
                                label=slot, data=f"foodlog:v1:{log_id}:{version}:slot:{slot}",
                                display_text=f"改成{slot}",
                            )) for slot in ("早餐", "午餐", "晚餐", "點心")
                        ] + [
                            QuickReplyButton(action=PostbackAction(
                                label="修改品項", data=f"foodlog:v1:{log_id}:{version}:rename:start",
                                display_text="修改這筆品項名稱",
                            )),
                            QuickReplyButton(action=PostbackAction(
                                label="刪除紀錄", data=f"foodlog:v1:{log_id}:{version}:delete:ask",
                                display_text="刪除這筆飲食紀錄",
                            )),
                        ]
                        reply = TextSendMessage(
                            text=f"「{brief['product_name']}」更多操作：",
                            quick_reply=QuickReply(items=options),
                        )
                    elif action == "rename:start":
                        set_daily_food_edit_state(uid, log_id, version, "rename")
                        reply = TextSendMessage(text="請輸入新的品項名稱；這次只修改這一筆紀錄。")
                    elif action == "delete:ask":
                        reply = TextSendMessage(
                            text=f"確定刪除「{brief['product_name']}」嗎？刪除後會重新計算當日總額。",
                            quick_reply=QuickReply(items=[
                                QuickReplyButton(action=PostbackAction(
                                    label="確認刪除", data=f"foodlog:v1:{log_id}:{version}:delete:confirm",
                                    display_text="確認刪除這筆飲食紀錄",
                                )),
                            ]),
                        )
                    else:
                        result = apply_daily_food_log_edit(
                            user_id=uid, log_id=log_id, expected_version=version,
                            event_id=event_key, action="delete",
                        )
                        reply = build_daily_food_edit_success_flex(result)
                elif ledger_portion:
                    result = apply_daily_food_log_edit(
                        user_id=uid, log_id=log_id, expected_version=version,
                        event_id=event_key, action="set_servings", value=float(ledger_portion.group(3)),
                    )
                    reply = build_daily_food_edit_success_flex(result)
                elif ledger_nutrition_field:
                    field = ledger_nutrition_field.group(3)
                    set_daily_food_edit_state(uid, log_id, version, "nutrition_value", field=field)
                    label = {"calories_kcal": "熱量 kcal", "protein_g": "蛋白質 g", "fat_g": "脂肪 g", "carbohydrate_g": "碳水 g"}[field]
                    reply = TextSendMessage(text=f"請輸入正確的{label}數值，例如：230")
                elif ledger_nutrition_apply:
                    field, new_value, mode = (
                        ledger_nutrition_apply.group(3), float(ledger_nutrition_apply.group(4)),
                        ledger_nutrition_apply.group(5),
                    )
                    result = apply_daily_food_log_edit(
                        user_id=uid, log_id=log_id, expected_version=version,
                        event_id=event_key, action="correct_nutrition", field=field, value=new_value,
                    )
                    if mode == "save":
                        set_daily_food_edit_state(
                            uid, log_id, result["version"], "save_private_name",
                            payload={"result": result},
                        )
                        reply = TextSendMessage(
                            text="營養已修正。請輸入容易辨識的私人食品名稱，例如：早餐店A火腿蛋吐司"
                        )
                    else:
                        clear_daily_food_edit_state(uid)
                        reply = build_daily_food_edit_success_flex(result)
                else:
                    assert ledger_slot is not None
                    result = apply_daily_food_log_edit(
                        user_id=uid, log_id=log_id, expected_version=version,
                        event_id=event_key, action="set_meal_slot", value=ledger_slot.group(3),
                    )
                    reply = build_daily_food_edit_success_flex(result)
            line_bot_api.reply_message(event.reply_token, reply)
        except ValueError as exc:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠️ {exc}\n請重新開啟最新的飲食紀錄卡。"),
            )
        return

    notification_retry = re.fullmatch(
        r"mprn:v1:([0-9a-f]{12}):(approved|rejected)", data
    )
    if notification_retry:
        token, kind = notification_retry.group(1), notification_retry.group(2)
        try:
            configured_admin_uid = get_bound_admin_uid_for_authorization()
            if uid != configured_admin_uid:
                raise PermissionError("管理員限定")
            with sqlite3.connect(DB_PATH) as conn:
                draft = get_meal_photo_draft_for_admin(
                    conn, token=token, admin_user_id=uid,
                    required_admin_user_id=configured_admin_uid,
                )
            if draft["status"] != kind:
                raise ValueError("餐點狀態與通知類型不符")
            if kind == "approved":
                from meal_photo_system import _meal_photo_exact_exchange
                exact = _meal_photo_exact_exchange(draft.get("review") or {})
                delivered = push_meal_photo_approval_to_owner(
                    draft,
                    {
                        "approved_exchange": exact,
                        "estimated_nutrition": estimate_nutrition_from_exchanges(exact),
                    },
                )
            else:
                delivered = push_meal_photo_return_to_owner(draft)
            reply = (
                TextSendMessage(text="✅ 客戶通知已送達。")
                if delivered else build_meal_photo_notification_retry_message(draft, kind)
            )
        except PermissionError:
            reply = TextSendMessage(text="⚠️ 管理員限定，無法重試餐點通知。")
        except ValueError as exc:
            reply = TextSendMessage(text=f"⚠️ {exc}")
        line_bot_api.reply_message(event.reply_token, reply)
        return

    # ── relog:v1 快速重新記錄 ──
    relog_start = re.fullmatch(r"relog:v1:([a-z0-9_]{8,40}):start", data)
    relog_servings = re.fullmatch(r"relog:v1:([a-z0-9_]{8,40}):servings:([0-9.]+)", data)
    relog_cancel = re.fullmatch(r"relog:v1:([a-z0-9_]{8,40}):servings:cancel", data)
    relog_meal = re.fullmatch(r"relog:v1:([a-z0-9_]{8,40}):sv:([0-9.]+):meal:(早餐|午餐|晚餐|點心)", data)
    if relog_start or relog_servings or relog_cancel or relog_meal:
        matched = relog_start or relog_servings or relog_cancel or relog_meal
        assert matched is not None
        food_id = matched.group(1)
        try:
            if relog_start:
                reply = build_food_servings_picker(food_id)
            elif relog_cancel:
                reply = TextSendMessage(text="✅ 已取消快速記錄。")
            elif relog_servings:
                sv = matched.group(2)
                items = []
                for slot in ("早餐", "午餐", "晚餐", "點心"):
                    items.append(QuickReplyButton(action=PostbackAction(
                        label=slot, data=f"relog:v1:{food_id}:sv:{sv}:meal:{slot}",
                        display_text=f"{sv}份・{slot}",
                    )))
                reply = TextSendMessage(
                    text=f"已選 {sv} 份，請選擇餐別：",
                    quick_reply=QuickReply(items=items),
                )
            else:
                assert relog_meal is not None
                sv = float(matched.group(2))
                slot = matched.group(3)
                event_ref = str(getattr(event, "webhook_event_id", "") or "").strip()
                if not event_ref:
                    event_ref = f"{uid}|{getattr(event, 'timestamp', '')}|{data}"
                reply = _quick_log_catalog_card_once(
                    user_id=uid, food_id=food_id, servings=sv, meal_slot=slot,
                    event_ref=event_ref,
                )
            line_bot_api.reply_message(event.reply_token, reply)
        except (ValueError, PermissionError) as exc:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ {exc}"))
        return

    start = re.fullmatch(r"mp:v1:([0-9a-f]{12}):(\d+):start", data)
    cancel = re.fullmatch(r"mp:v1:([0-9a-f]{12}):(\d+):cancel", data)
    answer = re.fullmatch(
        r"mp:v1:([0-9a-f]{12}):(\d+):answer:([a-z_]+):([a-z_]+)", data
    )
    remove_item = re.fullmatch(r"mp:v1:([0-9a-f]{12}):(\d+):remove:(.+)", data)
    request_add = re.fullmatch(
        r"mp:v1:([0-9a-f]{12}):(\d+):(?:request_add|add)", data
    )
    cancel_add = re.fullmatch(
        r"mp:v1:([0-9a-f]{12}):(\d+):cancel_add", data
    )
    add_category = re.fullmatch(
        r"mp:v1:([0-9a-f]{12}):(\d+):add_item:"
        r"(vegetable|protein|starch|fruit|milk|unknown):([A-Za-z0-9_-]+)",
        data,
    )
    review_start = re.fullmatch(r"mpr:v1:([0-9a-f]{12}):(\d+):start", data)
    review_resume = re.fullmatch(r"mpr:v1:([0-9a-f]{12}):(\d+):resume", data)
    review_set = re.fullmatch(
        r"mpr:v1:([0-9a-f]{12}):(\d+):set:([a-z_]+):([a-z0-9_.]+)", data
    )
    review_cancel = re.fullmatch(r"mpr:v1:([0-9a-f]{12}):(\d+):cancel_review", data)
    review_reject = re.fullmatch(r"mpr:v1:([0-9a-f]{12}):(\d+):reject", data)
    review_approve = re.fullmatch(r"mpr:v1:([0-9a-f]{12}):(\d+):approve", data)
    if not (start or cancel or answer or remove_item or request_add or cancel_add or add_category or review_start or review_resume or review_set or review_cancel or review_reject or review_approve):
        return
    uid = event.source.user_id
    if not (review_start or review_resume or review_set or review_cancel or review_reject or review_approve):
        matched = start or cancel or answer or remove_item or request_add or cancel_add or add_category
        assert matched is not None
        token, version = matched.group(1), int(matched.group(2))
    event_id = str(getattr(event, "webhook_event_id", "") or "").strip()
    if not event_id:
        fallback = f"{uid}|{getattr(event, 'timestamp', '')}|{data}"
        event_id = "postback:" + hashlib.sha256(fallback.encode()).hexdigest()
    applied = None
    try:
        if review_start or review_resume or review_set or review_cancel or review_reject or review_approve:
            configured_admin_uid = get_bound_admin_uid_for_authorization()
            if uid != configured_admin_uid:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="⚠️ 管理員限定，無法執行餐點審核。"),
                )
                return
            matched = review_start or review_resume or review_set or review_cancel or review_reject or review_approve
            assert matched is not None
            token, version = matched.group(1), int(matched.group(2))
            from meal_photo_system import apply_meal_photo_review_action, next_meal_photo_review_step
            if review_resume:
                with sqlite3.connect(DB_PATH) as conn:
                    draft = get_meal_photo_draft_for_admin(
                        conn, token=token, admin_user_id=uid,
                        required_admin_user_id=configured_admin_uid,
                    )
                if draft["version"] != version:
                    raise ValueError("餐點審核畫面已更新，請重新開啟待審清單")
                if draft["status"] == "reviewing":
                    step = next_meal_photo_review_step(draft)
                    reply = build_meal_photo_review_step_message(draft, step, version)
                elif draft["status"] == "review_ready":
                    from linebot.models import FlexSendMessage
                    reply = FlexSendMessage(
                        alt_text="最終核准份量，請確認加入",
                        contents=build_meal_photo_review_ready_bubble(draft),
                    )
                elif draft["status"] == "estimated":
                    from linebot.models import FlexSendMessage
                    reply = FlexSendMessage(
                        alt_text="餐點照片估算完成，待營養師審核",
                        contents=build_meal_photo_estimate_bubble(
                            draft, allow_admin_review=True
                        ),
                    )
                else:
                    raise ValueError("這筆餐點目前無法繼續審核")
                line_bot_api.reply_message(event.reply_token, reply)
                return
            if review_start:
                action, field, value = "start", "", ""
            elif review_set:
                action, field, value = "set", matched.group(3), matched.group(4)
            elif review_cancel:
                action, field, value = "cancel_review", "", ""
            elif review_reject:
                action, field, value = "reject", "", ""
            else:
                action, field, value = "approve", "", ""
            with sqlite3.connect(DB_PATH) as conn:
                admin_draft = get_meal_photo_draft_for_admin(
                    conn, token=token, admin_user_id=uid,
                    required_admin_user_id=configured_admin_uid,
                )
                owner_uid = admin_draft["user_id"]
                applied = apply_meal_photo_review_action(
                    conn, event_id=event_id, user_id=owner_uid, admin_user_id=uid,
                    required_admin_user_id=configured_admin_uid,
                    token=token, expected_version=version, action=action,
                    field=field, value=value,
                )
                draft, result = applied["draft"], applied["result"]
            kind = result["kind"]
            if kind == "review_question":
                reply = build_meal_photo_review_step_message(
                    draft, result["step"], result["version"]
                )
            elif kind in {"review_ready"}:
                from linebot.models import FlexSendMessage
                reply = FlexSendMessage(
                    alt_text="最終核准份量，請確認加入",
                    contents=build_meal_photo_review_ready_bubble(draft),
                )
            elif kind == "approved":
                from linebot.models import FlexSendMessage
                reply = FlexSendMessage(
                    alt_text="✅ 已核准｜已計入正式份量",
                    contents=build_meal_photo_approved_bubble(draft, result),
                )
            elif kind == "review_cancelled":
                from linebot.models import FlexSendMessage
                reply = FlexSendMessage(
                    alt_text="餐點照片估算完成，待營養師審核",
                    contents=build_meal_photo_estimate_bubble(
                        draft, allow_admin_review=True
                    ),
                )
            elif kind == "rejected":
                reply = TextSendMessage(
                    text="↩️ 已退回客戶；這筆餐點未計入正式營養紀錄。"
                )
            else:
                raise ValueError("餐點審核結果無效")
            if kind == "approved":
                owner_notified = push_meal_photo_approval_to_owner(draft, result)
                if not owner_notified:
                    reply = [
                        build_meal_photo_notification_retry_message(draft, "approved"),
                        reply,
                    ]
            elif kind == "rejected":
                owner_notified = push_meal_photo_return_to_owner(draft)
                if not owner_notified:
                    reply = build_meal_photo_notification_retry_message(draft, "rejected")
            line_bot_api.reply_message(event.reply_token, reply)
            return
        with sqlite3.connect(DB_PATH) as conn:
            ensure_meal_photo_schema(conn)
            if start:
                draft = get_meal_photo_draft(conn, user_id=uid, token=token)
                if draft["status"] == "estimated":
                    result = {"kind": "estimate", "version": draft["version"]}
                elif draft["version"] != version:
                    raise ValueError("餐點確認畫面已更新，請使用最新按鈕")
                else:
                    result = {
                        "kind": "question", "step": next_meal_photo_step(draft),
                        "version": draft["version"],
                    }
            elif cancel:
                applied = apply_meal_photo_action(
                    conn, event_id=event_id, user_id=uid, token=token,
                    expected_version=version, action="cancel",
                )
                draft, result = applied["draft"], applied["result"]
            elif remove_item:
                applied = apply_meal_photo_action(
                    conn, event_id=event_id, user_id=uid, token=token,
                    expected_version=version, action="remove_item",
                    value=remove_item.group(3),
                )
                draft, result = applied["draft"], applied["result"]
            elif request_add:
                applied = apply_meal_photo_action(
                    conn, event_id=event_id, user_id=uid, token=token,
                    expected_version=version, action="request_add",
                )
                draft, result = applied["draft"], applied["result"]
            elif cancel_add:
                applied = apply_meal_photo_action(
                    conn, event_id=event_id, user_id=uid,
                    token=cancel_add.group(1),
                    expected_version=int(cancel_add.group(2)), action="cancel_add",
                )
                draft, result = applied["draft"], applied["result"]
            elif add_category:
                encoded_name = add_category.group(4)
                try:
                    padding = "=" * (-len(encoded_name) % 4)
                    item_name = base64.urlsafe_b64decode(
                        encoded_name + padding
                    ).decode("utf-8")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValueError("食材名稱格式錯誤，請重新新增") from exc
                applied = apply_meal_photo_action(
                    conn, event_id=event_id, user_id=uid,
                    token=add_category.group(1),
                    expected_version=int(add_category.group(2)), action="add_item",
                    field=add_category.group(3), value=item_name,
                )
                draft, result = applied["draft"], applied["result"]
            else:
                assert answer is not None
                applied = apply_meal_photo_action(
                    conn, event_id=event_id, user_id=uid, token=token,
                    expected_version=version, action="answer",
                    field=answer.group(3), value=answer.group(4),
                )
                draft, result = applied["draft"], applied["result"]
        kind = result["kind"]
        if kind == "question":
            reply = build_meal_photo_step_message(
                token, result["step"], result["version"]
            )
        elif kind == "estimate":
            from linebot.models import FlexSendMessage
            configured_admin_uid = str(get_admin_notify_uid() or "").strip()
            is_admin_owner = bool(configured_admin_uid and uid == configured_admin_uid)
            if not is_admin_owner and applied is not None and not applied.get("replayed"):
                push_meal_photo_review_request(draft)
            reply = FlexSendMessage(
                alt_text="餐點照片估算完成，待營養師審核",
                contents=build_meal_photo_estimate_bubble(
                    draft, allow_admin_review=is_admin_owner
                ),
            )
        elif kind == "cancel":
            image_ref = str(result.get("source_image_ref") or "")
            if image_ref and _delete_nutrition_image(image_ref):
                with sqlite3.connect(DB_PATH) as conn:
                    clear_meal_photo_image_ref(
                        conn, user_id=uid, token=token, expected_ref=image_ref
                    )
            reply = TextSendMessage(text="✅ 已取消餐點照片紀錄，辨識內容已清除。")
        elif kind == "ask_item_name":
            reply = TextSendMessage(
                text=(
                    "➕ 請直接輸入要新增的食材名稱。\n"
                    "例如：玉米筍\n\n"
                    "一次輸入一項，送出後請選擇食材分類。"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=PostbackAction(
                        label="取消新增",
                        data=f"mp:v1:{draft['token']}:{result['version']}:cancel_add",
                        display_text="取消新增食材",
                    ))
                ]),
            )
        elif kind == "updated":
            from linebot.models import FlexSendMessage
            reply = FlexSendMessage(
                alt_text="餐點照片食材已更新",
                contents=build_meal_photo_confirmation_bubble(
                    draft["payload"], token=token, consumed_at=draft.get("consumed_at", ""),
                    version=result["version"],
                ),
            )
        else:
            raise ValueError("餐點操作結果無效")
        line_bot_api.reply_message(event.reply_token, reply)
    except PermissionError as exc:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"⚠️ {exc}。已拒絕這次管理操作。"),
        )
    except ValueError as exc:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"⚠️ {exc}。請回到最新餐點確認卡。"),
        )


def _build_breakfast_combo_reply_once(
    user_id: str, combo_name: str, message_id: str,
):
    """以單一交易記錄早餐組合；同一LINE message重送只重建回覆，不重複入帳。"""
    from datetime import datetime as _dt
    from linebot.models import FlexSendMessage

    combo = BREAKFAST_COMBOS[combo_name]
    now_tw = _dt.now(TW_TZ).isoformat(timespec="seconds")
    event_id = "combo:" + hashlib.sha256(
        f"{user_id}|{message_id}|{combo_name}".encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = conn.execute(
                """SELECT result_json FROM combo_log_events
                   WHERE event_id=? AND user_id=? AND combo_name=?""",
                (event_id, user_id, combo_name),
            ).fetchone()
            if stored:
                payload = json.loads(stored[0])
            else:
                resolved = []
                for keyword, servings in combo:
                    matches = search_food_catalog(
                        conn, user_id=user_id, query=keyword, limit=1
                    )
                    if not matches:
                        raise ValueError(
                            f"找不到「{keyword}」的食物卡片，請先傳營養標示照片建立"
                        )
                    resolved.append((matches[0], servings))

                items = []
                total_cal = total_pro = total_carb = 0.0
                for food, servings in resolved:
                    result = quick_log_from_catalog(
                        conn, user_id=user_id, food_id=food["food_id"],
                        consumed_servings=servings, meal_slot="早餐",
                        consumed_at=now_tw, manage_transaction=False,
                    )
                    nutrition = result.get("nutrition") or {}
                    cal = float(nutrition.get("calories_kcal") or 0)
                    pro = float(nutrition.get("protein_g") or 0)
                    carb = float(nutrition.get("carbohydrate_g") or 0)
                    total_cal += cal
                    total_pro += pro
                    total_carb += carb
                    items.append({
                        "product_name": food["product_name"],
                        "servings": float(servings),
                        "calories_kcal": cal,
                        "protein_g": pro,
                        "carbohydrate_g": carb,
                    })

                table_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_profile'"
                ).fetchone()
                if table_exists:
                    today_str = tw_today().isoformat()
                    row = conn.execute(
                        """SELECT today_extra_cal,today_extra_pro,today_food_items,today_date
                           FROM health_profile WHERE user_id=?""",
                        (user_id,),
                    ).fetchone()
                    if row:
                        old_cal, old_pro, old_items, old_date = row
                        if old_date != today_str:
                            old_cal, old_pro, old_items = 0, 0, ""
                        new_items = (
                            f"{old_items}、{combo_name}".strip("、")
                            if old_items else combo_name
                        )
                        conn.execute(
                            """UPDATE health_profile
                               SET today_extra_cal=?,today_extra_pro=?,today_food_items=?,today_date=?
                               WHERE user_id=?""",
                            (
                                round((old_cal or 0) + total_cal),
                                round((old_pro or 0) + total_pro, 1),
                                new_items, today_str, user_id,
                            ),
                        )

                conn.execute("""CREATE TABLE IF NOT EXISTS frequent_foods (
                    user_id TEXT, meal_name TEXT, last_cal INTEGER, last_pro INTEGER,
                    use_count INTEGER DEFAULT 1, last_used_at TEXT,
                    PRIMARY KEY (user_id, meal_name))""")
                frequent_rows = [
                    (combo_name, round(total_cal), round(total_pro)),
                    *[
                        (
                            item["product_name"], round(item["calories_kcal"]),
                            round(item["protein_g"]),
                        )
                        for item in items
                    ],
                ]
                for meal_name, cal, pro in frequent_rows:
                    conn.execute(
                        """INSERT INTO frequent_foods
                           (user_id,meal_name,last_cal,last_pro,use_count,last_used_at)
                           VALUES (?,?,?,?,1,?)
                           ON CONFLICT(user_id,meal_name) DO UPDATE SET
                               last_cal=excluded.last_cal,last_pro=excluded.last_pro,
                               use_count=frequent_foods.use_count+1,
                               last_used_at=excluded.last_used_at""",
                        (user_id, meal_name, cal, pro, now_tw),
                    )
                payload = {
                    "items": items, "total_cal": total_cal,
                    "total_pro": total_pro, "total_carb": total_carb,
                }
                conn.execute(
                    """INSERT INTO combo_log_events
                       (event_id,user_id,combo_name,result_json,created_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        event_id, user_id, combo_name,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True), now_tw,
                    ),
                )
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    food_rows = []
    for item in payload["items"]:
        servings_text = f"{float(item['servings']):g}"
        food_rows.append({
            "type": "box", "layout": "horizontal", "margin": "md",
            "contents": [
                {"type": "text", "text": f"{item['product_name']} {servings_text}份",
                 "size": "sm", "color": "#333333", "flex": 5, "wrap": True},
                {"type": "text", "text": f"{round(float(item['calories_kcal']))} kcal",
                 "size": "sm", "color": "#666666", "align": "end", "flex": 2},
            ],
        })
    total_cal = float(payload.get("total_cal") or 0)
    total_pro = float(payload.get("total_pro") or 0)
    total_carb = float(payload.get("total_carb") or 0)
    cal_text = f"{total_cal:.0f}" if total_cal else "NA"
    pro_text = f"{total_pro:.1f}" if total_pro else "NA"
    carb_text = f"{total_carb:.1f}" if total_carb else "NA"
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#2E7D32",
            "contents": [{
                "type": "text", "text": f"✅ 已記錄 {combo_name}",
                "color": "#FFFFFF", "size": "lg", "weight": "bold",
            }],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                *food_rows,
                {"type": "separator", "margin": "lg"},
                {
                    "type": "box", "layout": "horizontal", "margin": "lg",
                    "contents": [
                        {"type": "text", "text": f"🔥 {cal_text} kcal", "size": "sm", "color": "#E65100", "flex": 3},
                        {"type": "text", "text": f"蛋白質 {pro_text}g", "size": "sm", "color": "#1565C0", "flex": 3},
                        {"type": "text", "text": f"碳水 {carb_text}g", "size": "sm", "color": "#6A1B9A", "flex": 3},
                    ],
                },
            ],
        },
    }
    return FlexSendMessage(
        alt_text=f"已記錄 {combo_name} {cal_text}kcal", contents=bubble
    )


def _handle_message_impl(event):
    msg_id = event.message.id
    if msg_id in processed_messages:
        return
    if len(processed_messages) >= 1000:
        processed_messages.clear()
    processed_messages.add(msg_id)

    msg, uid = event.message.text.strip(), event.source.user_id

    # 飲食帳本編輯的文字輸入（營養數值、修改品項、私人食品名稱）優先於 AI 對話。
    ledger_state = get_daily_food_edit_state(uid)
    if ledger_state:
        clear_after_reply = False
        try:
            if msg in {"取消", "取消修改", "取消修正"}:
                reply = TextSendMessage(text="已取消修改飲食紀錄。")
                clear_after_reply = True
            elif ledger_state["input_type"] == "nutrition_value":
                clean_value = re.sub(r"(?:kcal|大卡|卡|公克|克|g)$", "", msg.strip(), flags=re.I).strip()
                value = _ledger_number(clean_value, allow_none=False)
                reply = build_daily_food_nutrition_confirmation_flex(
                    user_id=uid, log_id=ledger_state["log_id"],
                    expected_version=ledger_state["expected_version"],
                    field=ledger_state["field"], value=float(value),
                )
            elif ledger_state["input_type"] == "portion_value":
                clean_value = re.sub(r"(?:份)$", "", msg.strip()).strip()
                value = _ledger_number(clean_value, allow_none=False)
                result = apply_daily_food_log_edit(
                    user_id=uid, log_id=ledger_state["log_id"],
                    expected_version=ledger_state["expected_version"],
                    event_id=f"foodlog:text:{msg_id}", action="set_servings", value=value,
                )
                set_daily_food_edit_state(
                    uid, ledger_state["log_id"], result["version"], "completed",
                    payload={"result": result},
                )
                clear_after_reply = True
                reply = build_daily_food_edit_success_flex(result)
            elif ledger_state["input_type"] == "rename":
                result = apply_daily_food_log_edit(
                    user_id=uid, log_id=ledger_state["log_id"],
                    expected_version=ledger_state["expected_version"],
                    event_id=f"foodlog:text:{msg_id}", action="rename", value=msg,
                )
                set_daily_food_edit_state(
                    uid, ledger_state["log_id"], result["version"], "completed",
                    payload={"result": result},
                )
                clear_after_reply = True
                reply = build_daily_food_edit_success_flex(result)
            elif ledger_state["input_type"] == "save_private_name":
                saved = save_daily_log_as_private_food(uid, ledger_state["log_id"], msg)
                result = ledger_state["payload"].get("result") or _daily_food_log_brief(uid, ledger_state["log_id"])
                set_daily_food_edit_state(
                    uid, ledger_state["log_id"], int(result.get("version") or ledger_state["expected_version"]),
                    "completed", payload={"result": result, "saved_name": saved["product_name"]},
                )
                clear_after_reply = True
                reply = build_daily_food_edit_success_flex(result, saved_name=saved["product_name"])
            elif ledger_state["input_type"] == "completed":
                result = ledger_state["payload"].get("result") or _daily_food_log_brief(uid, ledger_state["log_id"])
                clear_after_reply = True
                reply = build_daily_food_edit_success_flex(
                    result, saved_name=ledger_state["payload"].get("saved_name", "")
                )
            else:
                reply = TextSendMessage(text="這次修改已失效，請重新開啟飲食紀錄卡。")
                clear_after_reply = True
        except ValueError as exc:
            reply = TextSendMessage(text=f"⚠️ {exc}\n可重新輸入，或輸入「取消修改」。")
        line_bot_api.reply_message(event.reply_token, reply)
        if clear_after_reply:
            clear_daily_food_edit_state(uid)
        return

    # 處理「新增食材」等待狀態；使用既有草稿表與版本鎖，不直接拼SQL修改payload。
    with sqlite3.connect(DB_PATH) as conn:
        ensure_meal_photo_schema(conn)
        draft_row = conn.execute(
            """SELECT token,version FROM pending_meal_photo_drafts
               WHERE user_id=? AND status='awaiting_item_name'
               ORDER BY updated_at DESC LIMIT 1""",
            (uid,),
        ).fetchone()
        if draft_row:
            token_d, version_d = draft_row
            item_name = " ".join(msg.split())
            cancel_add_data = f"mp:v1:{token_d}:{int(version_d)}:cancel_add"
            if item_name in {"取消", "取消新增", "取消新增食材"}:
                reply = TextSendMessage(
                    text="這段文字不會加入食材；請按下方按鈕返回原確認卡。",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=PostbackAction(
                            label="確認取消新增",
                            data=cancel_add_data,
                            display_text="確認取消新增食材",
                        ))
                    ]),
                )
            elif not item_name or len(item_name) > 60:
                reply = TextSendMessage(
                    text="⚠️ 食材名稱需為1～60個字。\n\n請重新輸入，例如：玉米筍"
                )
            else:
                encoded_name = base64.urlsafe_b64encode(
                    item_name.encode("utf-8")
                ).decode("ascii").rstrip("=")
                categories = [
                    ("🥩 蛋白質", "protein"),
                    ("🍚 主食", "starch"),
                    ("🥬 蔬菜", "vegetable"),
                    ("🍎 水果", "fruit"),
                    ("🥛 奶類", "milk"),
                    ("❓ 其他／不確定", "unknown"),
                ]
                category_actions = [
                    (
                        label,
                        category,
                        f"mp:v1:{token_d}:{int(version_d)}:"
                        f"add_item:{category}:{encoded_name}",
                    )
                    for label, category in categories
                ]
                if any(len(data.encode("utf-8")) > 300 for _, _, data in category_actions):
                    reply = TextSendMessage(
                        text=(
                            "⚠️ 食材名稱過長，無法建立分類按鈕。\n\n"
                            "請縮短名稱後重新輸入，例如：鮭魚壽司"
                        )
                    )
                else:
                    reply = TextSendMessage(
                        text=f"「{item_name}」請選擇食材分類：",
                        quick_reply=QuickReply(items=[
                            QuickReplyButton(action=PostbackAction(
                                label=label,
                                data=data,
                                display_text=f"{label}：{item_name}",
                            ))
                            for label, _, data in category_actions
                        ] + [
                            QuickReplyButton(action=PostbackAction(
                                label="取消新增",
                                data=cancel_add_data,
                                display_text="取消新增食材",
                            ))
                        ]),
                    )
            line_bot_api.reply_message(event.reply_token, reply)
            return

    if msg == "#待審餐點":
        try:
            admin_command_uid = get_bound_admin_uid_for_authorization()
        except PermissionError:
            admin_command_uid = ""
    else:
        admin_command_uid = ADMIN_UID
    if uid != admin_command_uid and is_admin_only_command(msg):
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⛔ 這是管理員專用指令，若需要協助請直接留言給客服喔。"))
        except Exception:
            processed_messages.discard(msg_id)
            raise
        return

    if msg == "#待審餐點":
        try:
            reply = build_pending_meal_photo_review_message(uid)
            line_bot_api.reply_message(event.reply_token, reply)
        except (PermissionError, ValueError) as exc:
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=f"⚠️ {exc}")
            )
        except Exception:
            processed_messages.discard(msg_id)
            raise
        return

    if msg.startswith("健康回報"):
        try:
            reply_text = save_jason_health_checkin(uid, msg)
        except (PermissionError, ValueError) as exc:
            reply_text = f"⚠️ {exc}\n\n請使用範本：\n{HEALTH_CHECKIN_TEMPLATE}"
        try:
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=reply_text)
            )
        except Exception:
            processed_messages.discard(msg_id)
            raise
        return

    if msg in {"今日健康日報", "重新整理今日報告"}:
        try:
            if uid != get_jason_health_report_uid():
                raise PermissionError("目前只有Jason啟用每日健康日報")
            reply_text = build_jason_daily_health_report(uid, tw_today().isoformat())
        except PermissionError as exc:
            reply_text = f"⚠️ {exc}"
        except Exception as exc:
            print(f"⚠️ 即時健康日報建立失敗：{type(exc).__name__}")
            reply_text = "⚠️ 今日健康日報暫時無法建立，請稍後再試。"
        try:
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=reply_text)
            )
        except Exception:
            processed_messages.discard(msg_id)
            raise
        return

    # ── 快速早餐組合 ──
    if msg in BREAKFAST_COMBOS:
        try:
            reply = _build_breakfast_combo_reply_once(uid, msg, msg_id)
        except (ValueError, PermissionError) as exc:
            reply = TextSendMessage(text=f"⚠️ {exc}")
        try:
            line_bot_api.reply_message(event.reply_token, reply)
        except Exception:
            # 資料已由持久化 combo_log_events 冪等保護；允許 LINE 安全重送回覆。
            processed_messages.discard(msg_id)
            raise
        return

    natural_log = parse_natural_food_log_intent(msg)
    if natural_log:
        try:
            reply = build_natural_food_log_reply(
                user_id=uid, message_id=msg_id, event=event, request=natural_log
            )
            line_bot_api.reply_message(event.reply_token, reply)
        except Exception:
            processed_messages.discard(msg_id)
            raise
        return

    if msg.startswith("搜尋") or msg.startswith("查食物"):
        query = msg.replace("搜尋", "", 1).replace("查食物", "", 1).strip()
        if query in {"我的食物", "我的食品", "我的食物庫"}:
            query = "_my"
        # 分頁支援：搜尋下一頁 2 _my／搜尋下一頁 2 雞胸
        page = 1
        next_page_match = re.fullmatch(r"下一頁\s+(\d+)(?:\s+(.+))?", query)
        if next_page_match:
            raw_page = next_page_match.group(1)
            if len(raw_page) > 3 or int(raw_page) > 100:
                try:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text="⚠️ 搜尋頁碼無效，請重新點選「搜尋我的食物」。"),
                    )
                except Exception:
                    processed_messages.discard(msg_id)
                    raise
                return
            page = max(1, int(raw_page))
            # 舊卡未攜帶條件時回到我的食物；新卡會保留 _my 或原關鍵字。
            query = str(next_page_match.group(2) or "_my").strip()
        menu_category = {"單品": "side", "飲品": "drink"}.get(query, "")
        try:
            from linebot.models import FlexSendMessage
            page_limit = 11  # LINE carousel 最多12個bubble，保留1格給下一頁
            offset = (page - 1) * page_limit
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                # "我的食物"：只搜用戶自己的卡片
                if query == "_my":
                    total = conn.execute(
                        "SELECT COUNT(*) FROM food_catalog WHERE owner_user_id=?", (uid,)
                    ).fetchone()[0]
                    catalog = conn.execute(
                        """SELECT food_id,product_name,brand,barcode,source_type,owner_user_id,
                                  package_amount,package_unit,servings_per_package,
                                  per_serving_json,exchange_json,exchange_review_status,
                                  created_at,updated_at
                           FROM food_catalog WHERE owner_user_id=?
                           ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                        (uid, page_limit, offset),
                    ).fetchall()
                    catalog = [
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
                        for r in catalog
                    ]
                    history = []
                    has_more = (offset + page_limit) < total
                elif query:
                    catalog, has_more = search_food_page(
                        conn, user_id=uid,
                        query="" if menu_category else query,
                        menu_category=menu_category,
                        limit=page_limit, offset=offset,
                    )
                    history = []
                else:
                    catalog = []
                    history = []
                    has_more = False
            seen = set()
            merged = []
            for item in history + catalog:
                if item["food_id"] not in seen:
                    seen.add(item["food_id"])
                    merged.append(item)

            # 無關鍵字時：顯示分類選單
            if not query:
                categories = {
                    "我的食物": "_my", "便當": "便當", "食蔬": "食蔬", "低碳": "低碳",
                    "沙拉": "沙拉", "番茄麵": "番茄麵", "青蔬麵": "青蔬麵",
                    "單品": "單品", "飲品": "飲品",
                }
                cat_buttons = []
                for label, keyword in categories.items():
                    cat_buttons.append({
                        "type": "box", "layout": "vertical", "margin": "sm",
                        "contents": [{
                            "type": "button",
                            "action": {"type": "message", "label": f"🔍 {label}", "text": f"搜尋 {keyword}"},
                            "style": "secondary", "height": "sm",
                        }],
                    })
                bubble = {
                    "type": "bubble",
                    "body": {
                        "type": "box", "layout": "vertical",
                        "contents": [
                            {"type": "text", "text": "📋 一日樂食菜單", "weight": "bold", "size": "lg"},
                            {"type": "text", "text": "請選擇分類查看餐點", "size": "sm", "color": "#999999", "margin": "sm"},
                            {"type": "separator", "margin": "md"},
                            *cat_buttons,
                        ],
                    },
                }
                line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="菜單分類", contents=bubble))
                return
            if not merged:
                if query:
                    reply = TextSendMessage(text=f"🔍 找不到「{query}」相關的食物紀錄。\n\n你可以：\n• 換個關鍵字再試\n• 傳營養標示照片建立新卡片\n• 傳餐點照片由營養師估算")
                else:
                    reply = TextSendMessage(text="📭 你還沒有任何食物卡片。\n\n傳營養標示照片或餐點照片就可以建立第一張卡片！")
            else:
                bubbles = [build_food_search_result_bubble(item) for item in merged[:page_limit]]
                if has_more:
                    next_page = page + 1
                    next_label = f"搜尋下一頁 {next_page} {query}"
                    bubbles.append({
                        "type": "bubble", "size": "kilo",
                        "body": {
                            "type": "box", "layout": "vertical", "justifyContent": "center", "alignItems": "center",
                            "contents": [
                                {"type": "button",
                                 "action": {"type": "message", "label": f"下一頁 ({next_page})", "text": next_label},
                                 "style": "primary", "color": "#4CAF50"},
                            ],
                        },
                    })
                alt = f"找到 {len(merged)} 張卡片" if not query else f"搜尋「{query}」找到 {len(merged)} 筆"
                reply = FlexSendMessage(
                    alt_text=alt,
                    contents={"type": "carousel", "contents": bubbles},
                )
        except Exception as exc:
            print(f"⚠️ 食物搜尋失敗：{type(exc).__name__}: {exc}")
            reply = TextSendMessage(text="⚠️ 搜尋暫時無法使用，請稍後再試。")
        try:
            line_bot_api.reply_message(event.reply_token, reply)
        except Exception:
            processed_messages.discard(msg_id)
            raise
        return

    if msg == "#待審營養份量" or msg.startswith("#核准營養份量 "):
        try:
            reply_text = handle_exchange_review_admin_command(msg, uid)
            if reply_text is None:
                raise ValueError("營養份量管理指令格式錯誤")
        except (PermissionError, ValueError) as exc:
            try:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"⚠️ {exc}"),
                )
            except Exception:
                processed_messages.discard(msg_id)
                raise
            return
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text),
            )
        except Exception:
            processed_messages.discard(msg_id)
            raise
        return

    # 營養標示確認流程必須先於 VIP／靜音檢查，確保剛上傳圖片的用戶可完成確認。
    name_edit_match = re.fullmatch(r"修改營養品名:([0-9a-f]{12})", msg)
    if name_edit_match:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                set_nutrition_input_state(
                    conn, user_id=uid, token=name_edit_match.group(1), input_type="name"
                )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請輸入『商品名稱 完整名稱』，例如：商品名稱 無加糖高蛋白豆漿。若不修改請輸入『取消修改』。"),
            )
        except Exception as exc:
            print(f"⚠️ 開啟營養品名修改失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 找不到可修改的營養草稿，請重新上傳營養標示。"))
        return

    nutrient_edit_match = re.fullmatch(r"修改營養數字:([0-9a-f]{12})", msg)
    if nutrient_edit_match:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                set_nutrition_input_state(
                    conn, user_id=uid, token=nutrient_edit_match.group(1), input_type="nutrient"
                )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "請輸入要修正的『每份』營養，例如：\n"
                        "鈉 48\n熱量 228\n蛋白質 21.2\n\n"
                        "可修改：熱量、蛋白質、脂肪、飽和脂肪、反式脂肪、膽固醇、碳水、糖、膳食纖維、鈉。"
                    )
                ),
            )
        except Exception as exc:
            print(f"⚠️ 開啟營養數字修改失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 找不到可修改的營養草稿，請重新上傳營養標示。"))
        return

    if msg == "取消修改":
        with sqlite3.connect(DB_PATH) as conn:
            ensure_nutrition_schema(conn)
            clear_nutrition_input_state(conn, user_id=uid)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="已取消這次修改；原營養草稿仍保留，可回確認卡繼續操作。"),
        )
        return

    confirm_match = re.fullmatch(r"確認營養紀錄:([0-9a-f]{12})", msg)
    if confirm_match:
        confirmation_committed = False
        try:
            token = confirm_match.group(1)
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                pending_context = conn.execute(
                    "SELECT meal_slot, consumed_at FROM pending_nutrition_logs WHERE token=? AND user_id=?",
                    (token, uid),
                ).fetchone()
            plan_id = ""
            plan_link_status = "pending"
            if pending_context:
                try:
                    active_plan = get_active_nutrition_target(uid, pending_context[0] or "", pending_context[1] or "")
                    if active_plan:
                        plan_id = active_plan["plan_id"]
                        plan_link_status = "linked"
                    else:
                        plan_link_status = "no_plan"
                except Exception as plan_exc:
                    print(f"⚠️ 飲食紀錄連結營養計畫失敗，將由排程重試：{plan_exc}")
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                result = confirm_pending_label(
                    conn, token=token, user_id=uid, plan_id=plan_id,
                    plan_link_status=plan_link_status,
                )
                confirmation_committed = True
                clear_nutrition_input_state(conn, user_id=uid)
            sync_confirmed_nutrition_to_sheet(result)
            dashboard_flex = apply_confirmed_nutrition_to_legacy_dashboard(uid, result)
            if dashboard_flex:
                line_bot_api.reply_message(event.reply_token, dashboard_flex)
            else:
                n = result["log"]["nutrition"]
                exchange_text = format_exchange_summary(result["log"].get("exchange") or {})
                exchange_status = result["log"].get("exchange_review_status", "pending_review")
                exchange_note = (
                    f"正式營養份數：{exchange_text}\n油脂份不計；已納入個人計畫。"
                    if exchange_status == "approved"
                    else f"推算營養份數：{exchange_text}\n油脂份不計；建議值待營養師審核，尚未扣入個人計畫。"
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=(f"✅ 已記錄並加入你的私人食品庫：{result['food']['product_name']}\n"
                          f"熱量 {n.get('calories_kcal', 0):g} kcal｜蛋白質 {n.get('protein_g', 0):g}g\n"
                          f"{exchange_note}")
                ))
        except ValueError as exc:
            if confirmation_committed:
                processed_messages.discard(str(msg_id))
                raise
            print(f"⚠️ 確認營養紀錄被資料驗證阻擋：{exc}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠️ 尚未記錄：{exc}。請回到確認卡按『修正營養』。"),
            )
        except Exception as exc:
            if confirmation_committed:
                processed_messages.discard(str(msg_id))
                raise
            print(f"⚠️ 確認營養紀錄失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前無法確認這筆營養紀錄，請稍後再試或聯繫客服。"))
        return

    cancel_match = re.fullmatch(r"取消營養紀錄:([0-9a-f]{12})", msg)
    if cancel_match:
        image_ref = ""
        image_deleted = True
        with sqlite3.connect(DB_PATH) as conn:
            ensure_nutrition_schema(conn)
            cancelled = cancel_pending_label(
                conn, user_id=uid, token=cancel_match.group(1)
            )
        if cancelled["cancelled"]:
            image_ref = cancelled["source_image_ref"]
            image_deleted = _delete_nutrition_image(image_ref) if image_ref else True
            if image_ref and image_deleted:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        """UPDATE pending_nutrition_logs SET source_image_ref=''
                           WHERE token=? AND user_id=? AND status='cancelled' AND source_image_ref=?""",
                        (cancel_match.group(1), uid, image_ref),
                    )
                    conn.commit()
            text = "已取消，這筆資料沒有寫入食品庫或飲食紀錄；原圖已排入安全刪除。"
        else:
            text = "找不到待取消的紀錄，或這筆已經處理。"
        if image_ref and not image_deleted:
            print(f"⚠️ 取消紀錄的圖片將於清理排程重試：{image_ref}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
        return

    modify_match = re.fullmatch(r"修改營養份量:([0-9a-f]{12})", msg)
    if modify_match:
        token = modify_match.group(1)
        try:
            get_pending_nutrition_label(uid, token)
            quick = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="半份", text=f"設定營養份量:{token}:0.5")),
                QuickReplyButton(action=MessageAction(label="1份", text=f"設定營養份量:{token}:1")),
                QuickReplyButton(action=MessageAction(label="1.5份", text=f"設定營養份量:{token}:1.5")),
                QuickReplyButton(action=MessageAction(label="2份", text=f"設定營養份量:{token}:2")),
            ])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇實際吃了幾份：", quick_reply=quick))
        except Exception as exc:
            print(f"⚠️ 讀取待修改營養份量失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 找不到可修改的待確認紀錄，請重新上傳營養標示。"))
        return

    time_modify_match = re.fullmatch(r"修改營養時間:([0-9a-f]{12})", msg)
    if time_modify_match:
        token = time_modify_match.group(1)
        try:
            get_pending_nutrition_label(uid, token)
            quick = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="現在", text=f"設定營養時間:{token}:now")),
                QuickReplyButton(action=MessageAction(label="今天早餐", text=f"設定營養時間:{token}:breakfast")),
                QuickReplyButton(action=MessageAction(label="今天午餐", text=f"設定營養時間:{token}:lunch")),
                QuickReplyButton(action=MessageAction(label="今天晚餐", text=f"設定營養時間:{token}:dinner")),
            ])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇實際食用時間：", quick_reply=quick))
        except Exception as exc:
            print(f"⚠️ 讀取待修改營養時間失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 找不到可修改的待確認紀錄，請重新上傳營養標示。"))
        return

    time_set_match = re.fullmatch(r"設定營養時間:([0-9a-f]{12}):(now|breakfast|lunch|dinner)", msg)
    if time_set_match:
        token, choice = time_set_match.groups()
        try:
            now = tw_now()
            choices = {
                "now": (now, current_meal_slot(now)),
                "breakfast": (now.replace(hour=8, minute=0, second=0, microsecond=0), "早餐"),
                "lunch": (now.replace(hour=12, minute=0, second=0, microsecond=0), "午餐"),
                "dinner": (now.replace(hour=18, minute=0, second=0, microsecond=0), "晚餐"),
            }
            consumed_time, meal_slot = choices[choice]
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                updated = update_pending_consumption(
                    conn,
                    user_id=uid,
                    token=token,
                    consumed_at=consumed_time.isoformat(timespec="seconds"),
                    meal_slot=meal_slot,
                    consumed_time_source="manual",
                )
            label = updated["label"]
            servings = updated["consumed_servings"]
            from linebot.models import FlexSendMessage
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(
                alt_text=f"請確認營養標示：{label['product_name']}",
                contents=build_label_confirmation_bubble(
                    label,
                    token=token,
                    consumed_servings=servings,
                    consumed_at=updated["consumed_at"],
                    consumed_time_source="manual",
                ),
            ))
        except Exception as exc:
            print(f"⚠️ 修改營養時間失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 無法修改時間，請重新開啟確認卡或重新上傳營養標示。"))
        return

    serving_match = re.fullmatch(r"設定營養份量:([0-9a-f]{12}):(0\.5|1|1\.5|2)", msg)
    if serving_match:
        token, serving_text = serving_match.groups()
        try:
            servings = float(serving_text)
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                updated = update_pending_consumption(
                    conn, user_id=uid, token=token, consumed_servings=servings
                )
            label = updated["label"]
            from linebot.models import FlexSendMessage
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(
                alt_text=f"請確認營養標示：{label['product_name']}",
                contents=build_label_confirmation_bubble(
                    label,
                    token=token,
                    consumed_servings=servings,
                    consumed_at=updated["consumed_at"],
                    consumed_time_source=updated["consumed_time_source"],
                ),
            ))
        except Exception as exc:
            print(f"⚠️ 修改營養份量失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 無法修改份量，請重新開啟確認卡或重新上傳營養標示。"))
        return

    # 已完成的營養文字修改若因 LINE 回覆失敗而重送，只重建原確認卡。
    nutrition_edit_replay = None
    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        replay_row = conn.execute(
            """SELECT e.token,p.label_payload_json,p.consumed_servings,p.consumed_at,
                      p.consumed_time_source,e.result_json
               FROM nutrition_message_events e
               JOIN pending_nutrition_logs p ON p.token=e.token AND p.user_id=e.user_id
               WHERE e.message_id=? AND e.user_id=? AND e.event_type='text_edit'""",
            (str(msg_id), uid),
        ).fetchone()
        if replay_row:
            nutrition_edit_replay = (
                replay_row[0],
                normalize_label_payload(json.loads(replay_row[1])),
                float(replay_row[2]),
                replay_row[3],
                replay_row[4],
                json.loads(replay_row[5] or "{}"),
            )
    if nutrition_edit_replay:
        token, label, servings, consumed_at, consumed_time_source, replay_result = nutrition_edit_replay
        from linebot.models import FlexSendMessage
        confirmation_message = FlexSendMessage(
            alt_text=f"請確認營養標示：{label['product_name']}",
            contents=build_label_confirmation_bubble(
                label, token=token, consumed_servings=servings,
                consumed_at=consumed_at,
                consumed_time_source=consumed_time_source,
            ),
        )
        replay_changes = replay_result.get("changes", []) if isinstance(replay_result, dict) else []
        if replay_changes:
            field_labels = {
                value: key for key, value in _NUTRITION_CORRECTION_FIELDS.items()
                if key not in {"碳水", "纖維"}
            }
            field_units = {
                "calories_kcal": "kcal", "sodium_mg": "mg",
                "cholesterol_mg": "mg",
            }
            change_lines = [
                f"{field_labels.get(change['field'], change['field'])}："
                f"{change['old']:g} → {change['new']:g} {field_units.get(change['field'], 'g')}"
                for change in replay_changes
            ]
            replay_summary = "\n".join([
                f"✅ 已更新 {len(change_lines)} 個項目（重送確認）",
                *change_lines,
                "營養數值不會重複更新。",
            ])
        else:
            replay_summary = "✅ 修改已完成，這是重送的確認卡；營養內容不會重複更新。"
        line_bot_api.reply_message(
            event.reply_token,
            [
                TextSendMessage(text=replay_summary),
                confirmation_message,
            ],
        )
        return

    # 「修改品名／修正營養」按鈕後的下一則文字，由 SQLite 狀態綁定到同一用戶與 token。
    with sqlite3.connect(DB_PATH) as conn:
        ensure_nutrition_schema(conn)
        nutrition_input_state = get_nutrition_input_state(conn, user_id=uid)
    if nutrition_input_state:
        input_type = nutrition_input_state["input_type"]
        edit_committed = False
        try:
            with sqlite3.connect(DB_PATH) as conn:
                ensure_nutrition_schema(conn)
                if input_type == "name":
                    name_match = re.fullmatch(r"商品名稱\s+(.+)", msg)
                    if not name_match:
                        line_bot_api.reply_message(
                            event.reply_token,
                            TextSendMessage(text="請輸入『商品名稱 完整名稱』，例如：商品名稱 無加糖高蛋白豆漿；若不修改請輸入『取消修改』。"),
                        )
                        return
                    edited = apply_nutrition_text_edit(
                        conn,
                        user_id=uid,
                        message_id=str(msg_id),
                        product_name=name_match.group(1),
                    )
                else:
                    corrections, parse_errors = parse_nutrition_corrections(msg)
                    if parse_errors or not corrections:
                        problem = "、".join(parse_errors) if parse_errors else "沒有讀到營養欄位與數字"
                        line_bot_api.reply_message(
                            event.reply_token,
                            TextSendMessage(
                                text=(
                                    f"⚠️ 尚未修改：無法辨識「{problem}」。\n\n"
                                    "可一次輸入多項，例如：\n"
                                    "熱量 204、蛋白質 16.4、鈉 48\n\n"
                                    "也可分行輸入，支援 kcal、g、mg；若不修改請輸入『取消修改』。"
                                )
                            ),
                        )
                        return
                    edited = apply_nutrition_text_edit(
                        conn,
                        user_id=uid,
                        message_id=str(msg_id),
                        corrections=corrections,
                    )
                edit_committed = True
                token = edited["token"]
                label = edited["label"]
                servings_row = conn.execute(
                    """SELECT consumed_servings,consumed_at,consumed_time_source
                       FROM pending_nutrition_logs WHERE token=? AND user_id=?""",
                    (token, uid),
                ).fetchone()
                servings = float(servings_row[0]) if servings_row else 1.0
                consumed_at = servings_row[1] if servings_row else ""
                consumed_time_source = servings_row[2] if servings_row else "line_timestamp"
            from linebot.models import FlexSendMessage
            confirmation_message = FlexSendMessage(
                alt_text=f"請確認營養標示：{label['product_name']}",
                contents=build_label_confirmation_bubble(
                    label, token=token, consumed_servings=servings,
                    consumed_at=consumed_at,
                    consumed_time_source=consumed_time_source,
                ),
            )
            if input_type == "nutrient":
                field_labels = {
                    value: key for key, value in _NUTRITION_CORRECTION_FIELDS.items()
                    if key not in {"碳水", "纖維"}
                }
                field_units = {
                    "calories_kcal": "kcal", "sodium_mg": "mg",
                    "cholesterol_mg": "mg",
                }
                change_lines = []
                for change in edited.get("changes", []):
                    field_name = field_labels.get(change["field"], change["field"])
                    unit = field_units.get(change["field"], "g")
                    change_lines.append(
                        f"{field_name}：{change['old']:g} → {change['new']:g} {unit}"
                    )
                if edited.get("replayed"):
                    summary = "✅ 修改已完成，這是重送的確認卡；營養數值不會重複更新。"
                else:
                    summary = "\n".join([
                        f"✅ 已更新 {len(change_lines)} 個項目",
                        *change_lines,
                        "其他營養數值維持不變。",
                    ])
                reply_payload = [TextSendMessage(text=summary), confirmation_message]
            else:
                reply_payload = confirmation_message
            line_bot_api.reply_message(event.reply_token, reply_payload)
        except ValueError as exc:
            if edit_committed:
                processed_messages.discard(str(msg_id))
                raise
            print(f"⚠️ 修改營養草稿被資料驗證阻擋：{exc}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠️ 無法套用修改：{exc}"),
            )
        except Exception as exc:
            if edit_committed:
                processed_messages.discard(str(msg_id))
                raise
            print(f"⚠️ 修改營養草稿失敗：{exc}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 目前無法修改這筆營養草稿，請稍後再試或重新上傳。"),
            )
        return

    if msg in ("推薦一日樂食", "營養推薦", "我的營養計畫", "推薦早餐", "推薦午餐", "推薦晚餐"):
        explicit_meal = {"推薦早餐": "早餐", "推薦午餐": "午餐", "推薦晚餐": "晚餐"}.get(msg, "")
        try:
            recommendation = nutrition_menu_recommendations(uid, explicit_meal)
            if not recommendation:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=("目前還沒有找到今天有效的客製化營養計畫。\n"
                          "請由營養師在「客製化營養計畫」分頁填入你的 User_ID、星期、餐別與份量目標後，再按一次「推薦一日樂食」。")
                ))
            else:
                from linebot.models import FlexSendMessage
                line_bot_api.reply_message(event.reply_token, FlexSendMessage(
                    alt_text="一日樂食個人餐點推薦",
                    contents=build_nutrition_recommendation_flex(recommendation),
                ))
        except Exception as exc:
            print(f"⚠️ 營養推薦失敗：{exc}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前無法計算個人餐點推薦，請稍後再試或聯繫營養師。"))
        return

    # ======================================================
    # 🌱 公開行銷入口：未開通顧客也可以使用
    # 放在靜音 / VIP 權限檢查之前，確保一定會觸發
    # ======================================================
    if msg in ["了解包月方案", "包月方案", "我要包月", "如何訂購"]:
        pending_subscription_state.pop(uid, None)
        line_bot_api.reply_message(event.reply_token, build_subscription_intro_flex(uid))
        return

    if msg in ["我的方案", "包月狀態", "查看狀態"]:
        pending_subscription_state.pop(uid, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=(
                "你目前尚未啟用包月方案。\n"
                "包月會員可以查看剩餘餐數、配送日、付款狀態與包月菜單。\n\n"
                "要先了解包月內容嗎？"
            ),
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="查看包月方案", text="包月方案")),
                QuickReplyButton(action=MessageAction(label="查看包月菜單", text="查看包月菜單")),
                QuickReplyButton(action=MessageAction(label="先不用", text="先不用")),
            ])
        ))
        return

    if msg == "先不用":
        pending_subscription_state.pop(uid, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="沒問題～有需要時可以再從圖文選單查看「包月方案」。"))
        return

    if msg in ["碳循環", "碳循環排餐", "啟用碳循環", "我要碳循環", "碳循環菜單"]:
        pending_subscription_state.pop(uid, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=(
            "碳循環排餐目前正在調整中，暫時先不開放自動排餐。\n"
            "如果你有運動日、減脂或訓練需求，可以先找客服協助安排。"
        ), quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="開始估價", text="開始包月估價")),
            QuickReplyButton(action=MessageAction(label="找客服", text="找客服")),
        ])))
        return

    if msg == "包月說明":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=build_subscription_intro_text(uid),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="開始估價", text="開始包月估價")),
                    QuickReplyButton(action=MessageAction(label="找客服", text="找客服")),
                ])
            )
        )
        return

    if msg == "開始包月估價":
        pending_subscription_state[uid] = {"step": "days"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "🍱 你一週想吃幾天呢？\n\n"
                    "一週 3 天以上可享本期包月優惠：\n\n"
                    "🚚 外送四週運費折抵\n"
                    "・每週 3 天：折 $100\n"
                    "・每週 4 天：折 $200\n"
                    "・每週 5 天以上：折 $300\n\n"
                    "🛍 自取加贈蛋白補充\n"
                    "・每週 3 天以上：送 1 次\n"
                    "・每週 5 天以上：送 2 次\n\n"
                    "請先選擇一週想吃幾天。"
                ),
                quick_reply=subscription_days_quick_reply()
            )
        )
        return

    if msg.startswith("包月天數 "):
        raw_days = msg.replace("包月天數 ", "", 1).strip()
        if raw_days == "不確定":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "如果剛開始控制飲食，建議先從每週 2～3 天開始。\n"
                        "如果想更穩定控熱量或增肌減脂，可以選每週 4～5 天。\n\n"
                        "你想先用幾天估算？"
                    ),
                    quick_reply=subscription_days_quick_reply()
                )
            )
            return
        if raw_days not in ["2", "3", "4", "5"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇每週 2～5 天喔。", quick_reply=subscription_days_quick_reply()))
            return
        days_int = int(raw_days)
        pending_subscription_state[uid] = {"step": "pickup", "days_per_week": days_int}
        bonus_text = format_subscription_day_bonus(days_int)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    f"✅ 好的，先用每週 {raw_days} 天估算。\n\n"
                    f"{bonus_text}\n\n"
                    "接著請選擇取餐方式：\n"
                    "🛵 外送：一天固定 2 餐，兩餐同一個時段配送、一天只送一次，會依地址估算外送費。\n"
                    "🛍 自取：餐數較彈性，也可以週六自取。"
                ),
                quick_reply=subscription_pickup_quick_reply()
            )
        )
        return

    if msg.startswith("包月取餐 "):
        pickup_method = msg.replace("包月取餐 ", "", 1).strip()
        state = pending_subscription_state.get(uid) or {}
        days = state.get("days_per_week")
        if not days:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="我先幫你從天數開始估算。", quick_reply=subscription_days_quick_reply()))
            pending_subscription_state[uid] = {"step": "days"}
            return
        if pickup_method == "外送":
            pending_subscription_state[uid] = {"step": "delivery_address", "days_per_week": days, "pickup_method": "外送"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=(
                "✅ 好的，外送會以一天 2 餐估算。\n"
                "🛵 兩餐同一個時段配送、一天只送一次。\n\n"
                "📍 請直接輸入你的外送地址，我幫你確認距離與預估外送費。\n\n"
                "⚠️ 提醒：週六目前不提供外送。"
            )))
            return
        if pickup_method == "自取":
            pending_subscription_state[uid] = {"step": "self_pickup_meals", "days_per_week": days, "pickup_method": "自取"}
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="自取可以彈性選擇餐數。\n你一個取餐日大約想拿幾餐？", quick_reply=subscription_self_pickup_meals_quick_reply())
            )
            return

    if msg.startswith("自取餐數 "):
        raw_meals = msg.replace("自取餐數 ", "", 1).strip()
        state = pending_subscription_state.get(uid) or {}
        days = state.get("days_per_week")
        if not days:
            pending_subscription_state[uid] = {"step": "days"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="我先幫你從天數開始估算。", quick_reply=subscription_days_quick_reply()))
            return
        if raw_meals == "客服討論":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="沒問題，自取餐數可由客服協助安排。你也可以先選每天 1～3 餐看粗估價格。", quick_reply=subscription_self_pickup_meals_quick_reply()))
            return
        if raw_meals not in ["1", "2", "3"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇每天 1～3 餐喔。", quick_reply=subscription_self_pickup_meals_quick_reply()))
            return
        meals_per_day = int(raw_meals)
        meal_count = int(days) * meals_per_day * SUBSCRIPTION_PERIOD_WEEKS
        est = calculate_subscription_estimate(uid, meal_count, "", delivery_count=0, pickup_method="自取", days_per_week=int(days), meals_per_day=meals_per_day)
        pending_subscription_state[uid] = {"step": "estimated", "estimate": est}
        line_bot_api.reply_message(event.reply_token, build_subscription_estimate_flex(uid, est))
        return

    if msg in ["找客服", "客服", "聯絡客服"]:
        pending_subscription_state.pop(uid, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="可以，請直接在這裡留言你的需求，客服會協助確認餐數、外送/自取、金額與付款方式。"))
        return

    state = pending_subscription_state.get(uid) or {}
    if state.get("step") == "delivery_address" and msg and not msg.startswith("#"):
        days = int(state.get("days_per_week") or 0)
        if not days:
            pending_subscription_state[uid] = {"step": "days"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="我先幫你從天數開始估算。", quick_reply=subscription_days_quick_reply()))
            return
        meals_per_day = 2
        meal_count = days * meals_per_day * SUBSCRIPTION_PERIOD_WEEKS
        delivery_count = days * SUBSCRIPTION_PERIOD_WEEKS
        address_text = msg.replace("#測距 ", "").replace("測距 ", "").strip()
        est = calculate_subscription_estimate(uid, meal_count, address_text, delivery_count=delivery_count, pickup_method="外送", days_per_week=days, meals_per_day=meals_per_day)
        pending_subscription_state[uid] = {"step": "estimated", "estimate": est}
        line_bot_api.reply_message(event.reply_token, build_subscription_estimate_flex(uid, est))
        return

    if msg.startswith("測距 ") or msg.startswith("#測距 "):
        target_address = msg.replace("#測距 ", "").replace("測距 ", "").strip()

        if not target_address:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請輸入完整地址喔！\n範例：測距 台北市松山區南京東路四段133巷4弄5號")
            )
            return

        quote = calculate_delivery_quote(target_address)

        if quote.get("success") and quote.get("delivery_available") is False:
            reply_text = (
                "🚫 此地址暫不提供外送\n\n"
                f"📍 目的地：{target_address}\n"
                f"📏 距本店距離：{quote.get('distance_text')}\n\n"
                "目前外送範圍為門市 3 公里內。\n"
                "你可以改選自取，或找客服確認其他方式。"
            )
        elif quote.get("success"):
            reply_text = (
                f"🛵 一日樂食 外送試算結果\n"
                f"📍 目的地：{target_address}\n"
                f"📏 距本店距離：{quote.get('distance_text')}\n"
                f"⏱️ 騎車時間：{quote.get('duration_text')}\n"
                f"💰 運費評估：{quote.get('delivery_fee_text')}\n"
                f"🧭 建議分線：{quote.get('route_group')} / {quote.get('delivery_zone')}\n"
                f"💡 {quote.get('carpool_hint')}\n\n"
                f"想了解包月方案，請回覆：了解包月方案"
            )
        else:
            reply_text = (
                "地圖系統暫時找不到這個地址，可能是地址格式或 Google Maps 暫時查詢失敗。\n\n"
                "你仍可先走包月估價流程，系統會先算餐費，外送費由客服最後確認。\n"
                "請回覆：開始包月估價"
            )

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    if msg == "我要填寫包月資料":
        reply_text = (
            "太好了！接下來請填寫包月資料表。\n\n"
            "這份資料會用來計算 TDEE、蛋白質目標、飲食禁忌與配餐方向。\n"
            "填完後客服會協助確認餐數、取餐方式、外送費、本期金額與付款資訊。\n\n"
            f"表單連結：\n{get_subscription_form_link(uid)}\n\n"
            "如果還沒估價，也可以回覆「開始包月估價」先看粗估金額。"
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=reply_text,
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="開始估價", text="開始包月估價")),
                    QuickReplyButton(action=MessageAction(label="找客服", text="找客服")),
                ])
            )
        )
        return

    parsed_order = parse_subscription_order_message(msg)
    if parsed_order:
        if parsed_order.get("error"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=parsed_order["error"]))
            return
        est = calculate_subscription_estimate(uid, parsed_order["meal_count"], parsed_order.get("address", ""))
        if not est.get("address"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=(
                "請提供完整外送地址，才能估運費與送出訂單喔！\n\n"
                "範例：\n估價 24餐 台北市松山區南京東路四段133巷4弄5號\n"
                "或：\n訂購 24餐 台北市松山區南京東路四段133巷4弄5號"
            )))
            return
        if est.get("delivery_available") is False:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=format_subscription_estimate(est, include_order_hint=False)),
            )
            return
        if parsed_order["action"] == "估價":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_subscription_estimate(est, include_order_hint=True)))
            return
        order_id = create_subscription_order(uid, est)
        notify_admin_new_subscription_order(order_id, uid, est)
        reply_text = (
            f"✅ 已收到您的包月訂單 #{order_id}，客服會確認餐數、外送與付款資訊。\n\n"
            f"{format_subscription_estimate(est, include_order_hint=False)}\n\n"
            "客服確認後會用 LINE 通知您下一步。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    if msg in ["我要估價", "估價"]:
        pending_subscription_state[uid] = {"step": "days"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="可以，我先幫你用包月 2.0 流程估價。\n請先選擇一週想吃幾天：", quick_reply=subscription_days_quick_reply())
        )
        return

    if msg in ["我要訂購", "訂購"]:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="包月 2.0 會先完成估價，再由客服確認正式金額與付款。\n你可以先開始估價，或直接找客服協助。",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="開始估價", text="開始包月估價")),
                    QuickReplyButton(action=MessageAction(label="找客服", text="找客服")),
                ])
            )
        )
        return

    if msg == "付款方式":
        reply_text = (
            "包月付款流程：\n\n"
            "1️⃣ 先填寫包月資料\n"
            "2️⃣ 系統產生 TDEE 與四週菜單\n"
            "3️⃣ 我們確認餐數、外送費與總金額\n"
            "4️⃣ 提供付款資訊\n"
            "5️⃣ 完成付款後正式開通包月\n\n"
            "目前可使用：轉帳、現場付款。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
        
    # 🕵️‍♂️ 工程師專用除錯指令
    if msg == "檢查數據":
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("SELECT xp_total, weekly_srpe, streak_days FROM user_achievements WHERE user_id=?", (uid,))
                row = c.fetchone()

            if row:
                xp, srpe, streak = row
                current_badge, _ = get_badge_level_from_xp(xp)
                badge_name = current_badge[1]
                debug_msg = f"🛠️ 【系統底層數據檢查】\nUID: {uid[:8]}...\n🌟 總經驗值 (XP): {xp}\n🔥 本週疲勞度 (sRPE): {srpe}\n🎖️ 目前等級: {badge_name}\n📅 連續打卡天數: {streak} 天"
            else:
                debug_msg = "🛠️ 系統回報：資料庫內尚未建立您的成就檔案（XP 為 0）。"

            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=debug_msg))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🛠️ 讀取資料庫失敗：{str(e)}"))
        return

    # 🧨 工程師專用：重置本週資料
    if msg == "重置本週":
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("UPDATE user_achievements SET weekly_srpe = 0 WHERE user_id = ?", (uid,))
            c.execute("DELETE FROM workout_checks WHERE user_id = ?", (uid,))
            conn.commit()
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 已重置本週 sRPE 與打卡紀錄！請重新測試「完美達標」。"))
        return

    # ─────────────────────────────────────────────────────────────────────────
    # 🌟 Phase 3：教練彙總介面 — #教練 指令（LIFF 升級版）
    # ─────────────────────────────────────────────────────────────────────────
    COACH_UIDS = ["Uefd72ca53a9a6ac39781fe673c398530","U9540c22cea2d6e0b1df8edbd9e3ebc41"]

    if msg == "#教練" and uid in COACH_UIDS:
        # 這裡放入你剛剛拿到的 LIFF URL
        liff_url = "https://liff.line.me/2009824277-W3lYtSjF"
        
        from linebot.models import FlexSendMessage
        bubble = {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
                    {"type": "text", "text": "👨‍🏫 教練您好！", "weight": "bold", "size": "xl", "color": "#1e293b"},
                    {"type": "text", "text": "請點擊下方按鈕開啟專屬戰情室，查看所有學員的最新運動數據。", "wrap": True, "margin": "md", "color": "#64748b", "size": "sm"}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                    {"type": "button", "action": {"type": "uri", "label": "🚀 開啟教練後台", "uri": liff_url}, "style": "primary", "color": "#3B82F6"}
                ]
            }
        }
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="開啟教練後台", contents=bubble))
        return   
    
    # ==========================================
    # 🔑 功能一：老闆靜音指令攔截（最優先）
    # ==========================================
    if uid == ADMIN_UID:
        if msg.startswith("@靜音 ") or msg.startswith("@解除靜音 "):
            is_mute = msg.startswith("@靜音 ")
            target_name = msg.replace("@靜音 ", "").replace("@解除靜音 ", "").strip()
            
            # ✅ 安全連線
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("UPDATE health_profile SET ai_mute=? WHERE name=?", (1 if is_mute else 0, target_name))
                affected = c.rowcount
                conn.commit()
                
            user_id = event.source.user_id 
            if affected > 0:
                action_str = "已靜音" if is_mute else "已解除靜音"
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ {action_str} {target_name}"))
                except Exception as e:
                    print(f"❌ Push 失敗: {e}")
            else:
                line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 找不到客人：{target_name}（請確認姓名完全相符）"))
            return

    # ==========================================
    # 🛑 功能一：靜音擋箭牌（一般客人才檢查）
    # ==========================================
    if uid != ADMIN_UID:
        try:
            # ✅ 安全連線
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("SELECT ai_mute FROM health_profile WHERE user_id=?", (uid,))
                mute_row = c.fetchone()
                if mute_row and mute_row[0] == 1:
                    return  # 🛑 已靜音，直接結束，不呼叫 AI
        except sqlite3.OperationalError:
            pass

    # 🔥 檢查是否處於「客服靜音期」
    try:
        # ✅ 安全連線
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT ai_silenced_until FROM health_profile WHERE user_id=?", (uid,))
            row = c.fetchone()
            if row and row[0]:
                silenced_until = row[0]
                if tw_now().isoformat() < silenced_until:
                    return # 還在靜音期，直接略過不理他，讓老闆回覆
                else:
                    c.execute("UPDATE health_profile SET ai_silenced_until='' WHERE user_id=?", (uid,))
                    conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

   # ==========================================
    # 🏃 功能二：客人手動新增單日課表（對應 LINE 選單）
    # ==========================================
    if msg.startswith("新增課表"):
        try:
            # 1. 解析各欄位（相容換行或空白分隔）
            parts = re.split(r'[\n\r]+', msg.strip())
            user_input = {}
            for part in parts:
                part = part.strip()
                if "：" in part:  # 處理全形冒號
                    key, val = part.split("：", 1)
                    user_input[key.strip()] = val.strip()
                elif ":" in part: # 處理半形冒號
                    key, val = part.split(":", 1)
                    user_input[key.strip()] = val.strip()

            target_date = user_input.get("日期", "")
            workout_name = user_input.get("運動", "")
            workout_time = user_input.get("時間", "")
            workout_intensity = user_input.get("強度", "")

            # 🌟 情境 A：客人只點了選單，內容是空的
            if not target_date or not workout_name:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="哇！看來你準備要開始新的運動計畫了呢！🎉💪\n\n請複製並填寫以下資訊回傳給我：\n\n新增課表\n日期：2026/03/20\n運動：自行車\n時間：1小時\n強度：中\n\n我會幫你記錄下來，讓我們一起朝著健康的目標邁進吧！💡✨"
                ))
                return

            # 🌟 情境 B：客人有填寫資料，開始處理日期格式 (將 3/20 轉成 2026/03/20)
            if "/" in target_date:
                d_parts = target_date.split("/")
                if len(d_parts) == 2: # 如果客人只打 3/20
                    target_date = f"{tw_today().year}/{int(d_parts[0]):02d}/{int(d_parts[1]):02d}"
                elif len(d_parts) == 3: # 如果客人打 2026/3/20
                    target_date = f"{d_parts[0]}/{int(d_parts[1]):02d}/{int(d_parts[2]):02d}"

            # 組合成單一字串準備寫入 Tomorrow_Training
            workout_content = f"{workout_intensity} {workout_name} {workout_time}".strip()

            # 2. 寫入 Google Sheet (Master_API_View 的 Tomorrow_Training 欄位)
            if gc:
                # ⚠️ 這裡換回你原本不會報錯的連線方式
                api_sheet = gc.open_by_url(SHEET_URL).worksheet("Master_API_View")
                records = api_sheet.get_all_records()
                
                # 尋找客人的日期格子 (相容 / 與 - 格式)
                target_idx = None
                for i, r in enumerate(records):
                    sheet_date = str(r.get("Date", "")).replace("-", "/") # 把試算表的橫線也轉成斜線比對
                    if str(r.get("User_ID")) == uid and sheet_date == target_date.replace("-", "/"):
                        target_idx = i + 2
                        break

                if target_idx:
                    # 找到格子，寫入第 6 欄 (Tomorrow_Training)
                    api_sheet.update_cell(target_idx, 6, workout_content)
                    
                    # 🌟 寫入成功後，發送熱情的確認對話！
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"太棒了！🚴‍♂️✨你已經成功新增了一個運動計畫！以下是你的課表資訊：\n\n- 📅 日期：{target_date}\n- 🏃 運動：{workout_name}\n- ⏰ 時間：{workout_time}\n- ⚡ 強度：{workout_intensity}\n\n這樣的運動安排一定會讓你感覺神清氣爽！記得保持水分補充喔！💧💪"
                    ))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"❌ 找不到 {target_date} 的專屬紀錄，請確認當天是否有為您排餐喔！"
                    ))

        except Exception as e:
            print(f"⚠️ 手動新增課表失敗: {e}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="⚠️ 系統發生錯誤，請稍後再試或檢查格式是否正確。"
            ))
        
        return

    # 👇 第一步加在這裡！老闆專屬的記憶檢查按鈕 👇
    if msg == "新增運動":
        line_bot_api.reply_message(event.reply_token, build_add_workout_entry_flex())
        return

    if msg == "換菜色":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="我先把換菜色入口隱藏了，避免主流程太分散。等我們把新增運動與主線體驗收斂好，再決定要不要重新開放。"))
        return

    # 🏃 運動專區觸發：一次發送兩則訊息 (Hero Card + 輪播卡)
    if msg == "運動":
        hero_msg = build_sport_hero_flex(uid)
        carousel_msg = build_sport_carousel_flex(uid)
        
        reply_msgs = []
        if hero_msg: reply_msgs.append(hero_msg)
        if carousel_msg: reply_msgs.append(carousel_msg)
            
        if reply_msgs:
            line_bot_api.reply_message(event.reply_token, reply_msgs)
        return
    # ==========================================
    # 📅 查看下週/本週課表功能開通 (精美小卡版 + 智能表情)
    # ==========================================
    if msg == "本週完整課表":
        d = get_dashboard_data(uid)
        if not d or not d.get("future_days"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📅 目前尚未為您排定後續課表喔！"))
            return
            
        # 🤖 智能表情符號判斷器
        def get_workout_emoji(text):
            t = text.lower()
            if "休息" in t: return "😴"
            if "游" in t or "swim" in t: return "🏊"
            if "騎" in t or "自行車" in t or "瓦" in t: return "🚴"
            if "跑" in t or "配速" in t or "run" in t: return "🏃"
            if "重訓" in t or "肌力" in t: return "🏋️"
            if "伸展" in t or "恢復" in t or "瑜珈" in t: return "🧘"
            return "💪" # 如果都不符合，預設給個肌肉符號

        # 準備組合卡片內的每一天
        days_contents = []
        for day in d["future_days"]:
            workout_text = day['workout']
            emoji = get_workout_emoji(workout_text) # 取得對應的符號
            
            days_contents.append({
                "type": "box", "layout": "vertical", "margin": "md",
                "contents": [
                    {"type": "text", "text": f"🔹 {day['label']}", "size": "sm", "color": "#3B82F6", "weight": "bold"},
                    # 這裡把寫死的 🏃 換成了動態的 emoji
                    {"type": "text", "text": f"{emoji} {workout_text}", "size": "sm", "color": "#333333", "wrap": True, "margin": "xs"}
                ]
            })
            days_contents.append({"type": "separator", "margin": "md"})
            
        # 移除最後一個多餘的分隔線讓排版更好看
        if days_contents and days_contents[-1]["type"] == "separator":
            days_contents.pop()

        # 組合完整的 Flex Message
        bubble = {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#3B82F6", "paddingAll": "16px",
                "contents": [
                    {"type": "text", "text": "🗓️ 本週後續訓練安排", "color": "#ffffff", "size": "lg", "weight": "bold"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px",
                "contents": days_contents
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": "#F8FAFC",
                "contents": [
                    {"type": "text", "text": "💪 課表是活的，若有調整需求隨時告訴教練！", "size": "xs", "color": "#888888", "wrap": True}
                ]
            }
        }
        
        from linebot.models import FlexSendMessage
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="本週後續訓練安排", contents=bubble))
        return
            
        summary = "🗓️ 本週後續訓練安排：\n"
        for day in d["future_days"]:
            summary += f"\n🔹 {day['label']}\n🏃 {day['workout']}\n"
            
        summary += "\n💪 課表是活的，若有調整需求隨時告訴教練！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=summary))
        return
    # 🍽️ 飲食紀錄先選今天／昨天，再顯示每日總結與逐筆輪播。
    if msg == "飲食紀錄":
        line_bot_api.reply_message(event.reply_token, build_daily_food_date_picker_flex())
        return

    # 📊 首頁儀表板觸發
    if msg in ["首頁", "儀表板", "我的狀態", "今日進度", "dashboard", "Dashboard"]:
        flex_msg = build_dashboard_flex(uid)
        if flex_msg:
            line_bot_api.reply_message(event.reply_token, flex_msg)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=(
                    "⚠️ 目前還沒有你的飲食紀錄資料\n"
                    "請先填寫資料建立檔案，就可以開始查看每日飲食紀錄。\n\n"
                    "也可以點選「包月方案」了解固定健康餐服務。"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="填寫資料", text="填寫體質表單")),
                    QuickReplyButton(action=MessageAction(label="包月方案", text="包月方案")),
                    QuickReplyButton(action=MessageAction(label="先不用", text="先不用")),
                ])
            ))
        return

    if msg.startswith("加入常吃："):
        meal_name = msg.replace("加入常吃：", "", 1).strip()
        # 組合餐：直接執行 combo 邏輯
        if meal_name in BREAKFAST_COMBOS:
            # 同一個 LINE 事件需重新進入組合餐 handler；先解除外層的處理中標記，
            # 內層會立即重新加入，避免被誤判為重複事件而直接 return。
            processed_messages.discard(msg_id)
            event.message.text = meal_name
            return _handle_message_impl(event)
        meal_flex, meal_text = add_frequent_food_to_today(uid, meal_name)
        if meal_flex:
            line_bot_api.reply_message(event.reply_token, meal_flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=meal_text))
        return

    if msg.startswith("改品項為："):
        meal_name = msg.replace("改品項為：", "", 1).strip()
        meal_flex, meal_text = replace_recent_meal_with_name(uid, meal_name)
        if meal_flex:
            line_bot_api.reply_message(event.reply_token, meal_flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=meal_text))
        return

    # ==========================================
    # 🏃 零摩擦紀錄系統：處理卡片按鈕與 sRPE 疲勞評估
    # ==========================================
    if msg in ["⚠️ 今日調整", "⚠️ 昨日調整"]:
        day_str = "今日" if "今日" in msg else "昨日"
        quick_reply_obj = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="⏱️ 時間不夠，練一半", text=f"{day_str}狀態：時間不夠")),
            QuickReplyButton(action=MessageAction(label="🥵 太累了，降強度", text=f"{day_str}狀態：降強度")),
            QuickReplyButton(action=MessageAction(label="🤕 身體不適 / 請假", text=f"{day_str}狀態：請假")),
            QuickReplyButton(action=MessageAction(label="🔄 換練別的項目了", text=f"{day_str}狀態：換項目"))
        ])
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"辛苦了！課表是活的，請告訴教練 {day_str} 的實際情況：", quick_reply=quick_reply_obj)
        )
        return

    # 🌟 攔截打卡動作，引導評估 RPE
    if msg in ["✅ 今日完美達標", "✅ 昨日補登達標"]:
        day_label = "今日" if "今日" in msg else "昨日"
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label=f"RPE {i}", text=f"{day_label}RPE：{i}")) for i in range(1, 11)
        ])
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"請選擇你【{day_label}】訓練的疲勞度 (RPE 1-10)：\n1=非常輕鬆, 10=極限力竭", quick_reply=quick_reply)
        )
        return
        
        # 建立 1~10 分的快速回覆選單
        rpe_items = []
        rpe_labels = ["1 超輕鬆", "2 很輕鬆", "3 輕鬆", "4 有點喘", "5 喘但可說話", 
                      "6 稍吃力", "7 吃力", "8 很吃力", "9 極度吃力", "10 筋疲力盡"]
        for i, label in enumerate(rpe_labels, 1):
            rpe_items.append(QuickReplyButton(action=MessageAction(label=label, text=f"{day_str}RPE：{i}")))
            
        quick_reply_obj = QuickReply(items=rpe_items)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"太棒了！恭喜完成{day_str}訓練 🎉\n\n為了精準計算你的訓練負荷，請誠實評估剛剛這頓課表的「疲勞指數 (RPE 1~10)」：", quick_reply=quick_reply_obj)
        )
        return

    # 🌟 接收 RPE 評分並計算 sRPE
    # 🌟 接收 RPE 評分並計算 sRPE
    if msg.startswith("今日RPE：") or msg.startswith("昨日RPE："):
        day_str = "今日" if "今日" in msg else "昨日"
        try:
            rpe_score = int(msg.split("：")[1])
        except:
            rpe_score = 5 # 防呆預設值
            
        # 照常寫入 RPE 資料庫
        reply_text = mark_workout_done_with_srpe(uid, rpe_score, day_str)
        
        # 🌟 決定「目標日期」
        import datetime
        if day_str == "今日":
            target_date = datetime.date.today().strftime("%Y-%m-%d")
        else:
            target_date = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            
        # 🌟 讓系統記住這個人正在等待補登圖片！
        pending_image_date[uid] = target_date
        
        # 🌟 在原本的回覆文字後面，加上「討截圖」的引導語
        final_reply = f"{reply_text}\n\n📸 如果你有【{day_str}】的 Garmin 訓練截圖，請現在直接傳送給我，我會自動幫你歸檔到 {target_date}！\n(如果沒有截圖，請直接忽略此訊息~)"
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=final_reply))
        return

    # 處理非完美達標的調整狀態 (請假、時間不夠等)
    if msg.startswith("今日狀態：") or msg.startswith("昨日狀態："):
        day_str = "今日" if "今日" in msg else "昨日"
        status_label = msg.split("：")[-1]
        
        # 呼叫舊函數純打卡 (給予基礎 XP 即可)
        reply_text = mark_today_workout_done(uid)
        
        # 寫入 Google Sheet 教練日誌
        if gc:
            try:
                # ✅ 安全連線
                with closing(sqlite3.connect(DB_PATH)) as conn:
                    c = conn.cursor()
                    c.execute("SELECT sheet_name FROM health_profile WHERE user_id=?", (uid,))
                    row = c.fetchone()

                if row and row[0]:
                    sheet_name = row[0]
                    d = get_dashboard_data(uid)
                    workout_name = d.get("today_workout", "無") if d else "無"

                    sheet = gc.open_by_url(SHEET_URL)
                    now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
                    sheet.worksheet(sheet_name).append_row([
                        now_str, f"🏃 {day_str}運動狀態回報", f"狀態：【{status_label}】", f"原訂課表: {workout_name}"
                    ])
            except Exception as e:
                print(f"⚠️ 寫入 Google Sheet 運動狀態失敗: {e}")

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"收到！已為您紀錄{day_str}狀態為「{status_label}」。\n(狀態已同步寫入您的教練日誌，週末排課時會作為重要參考！💪)")
        )
        return

    if msg == "今日運動完成":
        reply_text = mark_today_workout_done(uid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    if msg in ["午餐已吃", "晚餐已吃"]:
        slot = "午餐" if msg.startswith("午餐") else "晚餐"
        meal_flex, meal_text = mark_planned_meal_as_eaten(uid, slot)
        if meal_flex:
            line_bot_api.reply_message(event.reply_token, meal_flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=meal_text))
        return

    if msg == "修正內容":
        line_bot_api.reply_message(event.reply_token, build_edit_content_flex())
        return

    if msg == "改份量":
        line_bot_api.reply_message(event.reply_token, build_portion_adjust_flex())
        return

    if msg == "改品項":
        line_bot_api.reply_message(event.reply_token, build_frequent_food_picker_flex(uid, mode="replace"))
        return

    if msg == "重選常吃":
        line_bot_api.reply_message(event.reply_token, build_frequent_food_picker_flex(uid, mode="add"))
        return

    if msg in ["少量", "正常", "大份", "少飯", "加飯", "去醬"]:
        adjust_flex, adjust_text = apply_portion_adjustment(uid, msg)
        if adjust_flex:
            line_bot_api.reply_message(event.reply_token, adjust_flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=adjust_text))
        return

    if msg == "#查狀態":
        # ✅ 安全連線
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT today_extra_cal, today_extra_pro, today_food_items, today_date FROM health_profile WHERE user_id=?", (uid,))
            row = c.fetchone()
            
        if row:
            status_msg = f"🔍 目前系統記憶狀況：\n📅 日期：{row[3]}\n🔥 累計熱量：{row[0]} kcal\n🥩 累計蛋白：{row[1]} g\n🍱 品項清單：{row[2] if row[2] else '空'}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=status_msg))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 系統裡還沒有您的檔案喔！請先填寫表單或重置。"))
        return
    # 🔥 LINE 圖文選單攔截區
    if msg == "填寫體質表單":
        form_link = get_subscription_form_link(uid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📝 請點擊下方專屬連結，填寫您的體質評估 / 包月資料表單：\n\n{form_link}\n\n(系統已為您自動帶入 LINE 帳號，請直接填寫即可喔！)"))
        return
    elif msg == "填寫滿意度問卷":
        # 👉 老闆注意：請把下面這串網址，換成您剛剛在 Google 表單產生的那串「最後面有 {uid} 的黃金連結」！
        survey_link = f"https://docs.google.com/forms/d/e/1FAIpQLScF6Va_sdq6KMaKFd8BUVB2x5SyLji3JqX28-Z7h-tuLnpB-Q/viewform?usp=pp_url&entry.1048958109={uid}"
        
        reply_text = f"🎁 感謝您對一日樂食的支持！\n請點擊下方專屬連結填寫滿意度調查 (約1分鐘)。\n\n完成填寫後，系統將自動發送【1 點集點卡點數】給您喔！👇\n\n{survey_link}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg in ["查看菜單", "包月菜單", "查看包月菜單"]:
        # ✅ 安全連線
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT summary_text FROM health_profile WHERE user_id=?", (uid,))
            hp = c.fetchone()
            
        reply_text = f"🍽️ 這是為您量身打造的專屬菜單：\n\n{hp[0]}\n\n(若想更換菜色或加購單品，可以直接打字告訴我喔！)" if hp and hp[0] else "您好像還沒填寫體質評估表單喔！請點擊選單來建立專屬檔案吧！📝"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "我要紀錄飲食":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="今天吃了什麼好料呢？📸\n\n您可以直接打字告訴我（例如：我剛吃了一個大麥克和中薯），我會立刻幫您估算熱量，並將紀錄存入您的【專屬 VIP 檔案】中喔！💪"))
        return
    elif msg == "運費怎麼算":
        reply_text = "想知道專屬外送運費嗎？🛵\n\n請直接在對話框輸入：\n「#測距 您的完整地址」\n\n例如：\n#測距 台北市信義區松仁路90號\n\n系統就會立刻為您啟動智能順風車報價喔！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "我的會員狀態":
        allow, q_msg = check_permission_and_quota(uid)
        if allow: reply_text = f"💎 您的 VIP 會員狀態：\n\n您目前還剩下：\n{q_msg}\n\n請繼續保持健康的飲食習慣喔！"
        else: reply_text = "您目前尚未開通 VIP 方案，或是方案已到期。\n請輸入您的 VIP 邀請碼 (例如 #VIP24-XXXXXX) 來解鎖專屬 AI 營養師與訂餐服務！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 👑 老闆專屬指令區 👑
    if msg == "#綁定老闆":
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO admin_settings VALUES ('admin_id', ?)", (uid,))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 老闆好！系統已成功綁定。\n客人的【換餐通知】都會私訊給您！"))
        return

    elif msg.startswith("#喚醒AI "):
        target_uid = msg.replace("#喚醒AI ", "").strip()
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("UPDATE health_profile SET ai_silenced_until='' WHERE user_id LIKE ?", (f"%{target_uid}%",))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已手動解除客人的 AI 靜音！"))
        return

    elif msg == "#點數庫存":
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM reward_links WHERE is_used=0")
            unused_count = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM reward_links WHERE is_used=1")
            used_count = c.fetchone()[0]
            
        reply_msg = f"📊 【老闆專屬：點數庫存報告】\n\n🟢 尚未發送：{unused_count} 張\n🔴 已經發出：{used_count} 張\n\n(歷史總共上傳過 {unused_count + used_count} 張點數網址)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return

    elif msg == "#更新菜單":
        reply_msg = load_menu()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return

    elif msg == "#待核訂單":
        rows = list_pending_subscription_orders(limit=20)
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 目前沒有待確認的包月訂單。"))
            return
        lines = ["🧾【包月待確認訂單】"]
        for oid, cname, meal_count, address, low_total, high_total, created_at, order_user_id, form_payload_json in rows:
            line_display_name = ""
            try:
                if form_payload_json:
                    line_display_name = (json.loads(form_payload_json) or {}).get("line_display_name") or ""
            except Exception:
                line_display_name = ""
            amount_text = f"${low_total:,}" if int(low_total or 0) == int(high_total or 0) else f"${low_total:,}～${high_total:,}"
            lines.append(f"#{oid} 表單:{cname or '未填'}｜LINE:{line_display_name or 'NA'}｜UID末8:{str(order_user_id)[-8:]}")
            lines.append(f"{meal_count}餐｜{amount_text}｜{created_at}")
            lines.append(f"地址：{address or '未提供'}")
            lines.append(f"核准並發匯款資訊：#核准訂單 {oid}｜付款後開通：#開通訂單 {oid}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines[:60])))
        return

    elif msg.startswith("#核准訂單 "):
        raw = msg.replace("#核准訂單 ", "", 1).strip()
        parts = raw.split(" ", 1)
        order_id = parts[0].strip() if parts else ""
        note = parts[1].strip() if len(parts) > 1 else ""
        if not order_id.isdigit():
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 請輸入正確訂單 ID，例如：#核准訂單 12"))
            return
        ok, result = update_subscription_order_status(int(order_id), "approved", uid, note)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        return

    elif msg.startswith("#拒絕訂單 "):
        raw = msg.replace("#拒絕訂單 ", "", 1).strip()
        parts = raw.split(" ", 1)
        order_id = parts[0].strip() if parts else ""
        reason = parts[1].strip() if len(parts) > 1 else "請聯絡客服重新確認"
        if not order_id.isdigit():
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 請輸入正確訂單 ID，例如：#拒絕訂單 12 地址超出範圍"))
            return
        ok, result = update_subscription_order_status(int(order_id), "rejected", uid, reason)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        return

    elif msg.startswith("#開通訂單 "):
        order_id = msg.replace("#開通訂單 ", "", 1).strip()
        if not order_id.isdigit():
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 請輸入正確訂單 ID，例如：#開通訂單 12"))
            return
        ok, result = update_subscription_order_status(int(order_id), "activated", uid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        return

    elif msg == "#今日出餐完成":
        weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        today_str = weekdays[tw_today().weekday()]
        count, notify_count = 0, 0
        
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{today_str}%",))
            users = c.fetchall()
            
            for u in users:
                u_id, u_name = u
                c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (u_id,))
                res = c.fetchone()
                if res and res[0] > 0:
                    old_meals = res[0]
                    new_meals = old_meals - 1
                    c.execute("UPDATE usage SET remaining_meals=? WHERE user_id=?", (new_meals, u_id))
                    count += 1
                    if new_meals <= 3 and new_meals > 0:
                        notify_msg = f"🎉 {u_name} 您好！您的專屬方案只剩最後 {new_meals} 餐囉！\n您可以直接回覆我「我要續約」，系統將為您無縫安排下一期菜單！"
                        try: 
                            line_bot_api.push_message(u_id, TextSendMessage(text=notify_msg))
                            notify_count += 1
                        except Exception: pass
                    elif old_meals == 2 and new_meals == 1:
                        last_meal_msg = f"⏰ {u_name} 提醒您：明天將是您目前方案的最後一餐。\n若想不中斷外送與 AI 營養師服務，建議今天就先續訂下一期包月喔！"
                        try:
                            line_bot_api.push_message(u_id, TextSendMessage(text=last_meal_msg))
                            notify_count += 1
                        except Exception:
                            pass
            conn.commit()
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 報告老闆！今日 ({today_str}) 出餐扣除完畢！\n共扣除了 {count} 份餐點，發送 {notify_count} 則續約推播！"))
        return
    
    elif msg.startswith("#上傳點數\n"):
        links = msg.replace("#上傳點數\n", "").strip().split('\n')
        count = 0
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            for link in links:
                if link.strip():
                    try:
                        c.execute("INSERT INTO reward_links (link, is_used) VALUES (?, 0)", (link.strip(),))
                        count += 1
                    except sqlite3.IntegrityError: pass
            conn.commit()
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 報告老闆！成功存入 {count} 筆全新的點數網址！"))
        return
        
    elif msg == "#發送明日提醒":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=send_tomorrow_reminders()))
        return

    elif msg == "#測試週報":
        if uid != ADMIN_UID:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⛔ 僅限管理員使用（你的UID: {uid}）"))
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🗓️ 週報發送中，請稍候..."))
        threading.Thread(target=auto_weekly_coach_batch, daemon=True).start()
        return

    elif msg == "#測試晚報":
        if uid != ADMIN_UID:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⛔ 僅限管理員使用"))
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⏰ 晚報發送中，請稍候..."))
        threading.Thread(target=auto_daily_evening_report, daemon=True).start()
        return

    elif msg in ["#重新排餐", "重新排餐"]:
        ok, repack_msg = repack_meal_plan_for_user(uid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=repack_msg))
        return

    elif msg == "#生24":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🎁 24餐邀請碼：\n{chr(10).join(generate_package_codes('24m', 3))}"))
        return

    elif msg == "#生48":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔥 48餐邀請碼：\n{chr(10).join(generate_package_codes('48m', 3))}"))
        return

    elif msg.startswith("#VIP"):
        expiry, res = redeem_code(uid, msg)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=res))
        return

    elif msg.startswith("#延餐 "):
        parsed = parse_defer_command(msg)
        if not parsed:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 格式錯誤，請輸入：\n#延餐 5/31 午餐 -> 6/2 午餐"))
            return

        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("SELECT sheet_name, name FROM health_profile WHERE user_id=?", (uid,))
            hp = c.fetchone()
        if not hp or not hp[0]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 找不到您的專屬菜單檔案。"))
            return

        sheet_name, customer_name = hp[0], hp[1] or "顧客"
        try:
            sheet = gc.open_by_url(SHEET_URL).worksheet(sheet_name)
            src = find_meal_slot_in_user_sheet(sheet, parsed["original_date"], parsed["original_meal_type"])
            dst = find_meal_slot_in_user_sheet(sheet, parsed["target_date"], parsed["target_meal_type"])
        except Exception:
            src, dst = None, None

        if not src or not src.get("value") or src.get("value") in ["", "無", "尚未安排"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 找不到原餐：{parsed['original_date']} {parsed['original_meal_type']}"))
            return

        has_conflict = 0
        note = ""
        if not dst:
            has_conflict = 1
            note = "目標日期不存在於目前個人分頁，需人工確認"
        elif dst.get("value") not in ["", "無", "尚未安排"]:
            has_conflict = 1
            note = "目標日期已有餐點，需人工確認"

        is_cross_period = 0
        try:
            od = datetime.strptime(parsed["original_date"], "%m/%d")
            td = datetime.strptime(parsed["target_date"], "%m/%d")
            is_cross_period = 1 if od.month != td.month else 0
        except Exception:
            pass

        request_id = create_deferred_meal_request(
            uid,
            customer_name,
            parsed["original_date"],
            parsed["original_meal_type"],
            parsed["target_date"],
            parsed["target_meal_type"],
            is_cross_period,
            has_conflict,
            note
        )

        admin_msg = (
            f"📦【延餐申請 #{request_id}】\n"
            f"顧客：{customer_name}\n"
            f"原餐：{parsed['original_date']} {parsed['original_meal_type']}\n"
            f"目標：{parsed['target_date']} {parsed['target_meal_type']}\n"
            f"跨期：{'是' if is_cross_period else '否'}\n"
            f"衝突：{'是' if has_conflict else '否'}\n"
            f"備註：{note or '無'}"
        )
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn:
                c = conn.cursor()
                c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
                admin_row = c.fetchone()
            if admin_row:
                line_bot_api.push_message(admin_row[0], TextSendMessage(text=admin_msg))
        except Exception:
            pass

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=(
            f"✅ 已收到您的延餐申請\n"
            f"原餐：{parsed['original_date']} {parsed['original_meal_type']}\n"
            f"目標：{parsed['target_date']} {parsed['target_meal_type']}\n\n"
            f"此申請需由客服確認後安排，確認完成後會再通知您。"
        )))
        return

    elif msg == "#延餐清單":
        rows = list_pending_deferred_meals(limit=20)
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 目前沒有待審核的延餐申請。"))
            return
        lines = ["📦【延餐待審核清單】"]
        for rid, cname, od, omt, td, tmt, cross_p, conflict, note in rows:
            lines.append(f"#{rid} {cname}｜{od} {omt} -> {td} {tmt}｜跨期：{'是' if cross_p else '否'}｜衝突：{'是' if conflict else '否'}")
            if note:
                lines.append(f"備註：{note}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines[:40])))
        return

    elif msg.startswith("#核准延餐 "):
        request_id = msg.replace("#核准延餐 ", "").strip()
        if not request_id.isdigit():
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 請輸入正確申請 ID，例如：#核准延餐 12"))
            return
        result = approve_deferred_meal_request(int(request_id), uid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        return

    elif msg.startswith("#拒絕延餐 "):
        raw = msg.replace("#拒絕延餐 ", "", 1).strip()
        parts = raw.split(" ", 1)
        request_id = parts[0].strip() if parts else ""
        reason = parts[1].strip() if len(parts) > 1 else "請聯絡客服確認"
        if not request_id.isdigit():
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 請輸入正確申請 ID，例如：#拒絕延餐 12 6/2已有餐點"))
            return
        result = reject_deferred_meal_request(int(request_id), reason, uid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        return

    elif msg == "#清空熱量":
        result = clear_daily_food_ledger(
            uid, event_id=f"foodlog:clear:{uid}:{msg_id}"
        )
        count = int(result.get("deleted_count") or 0)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"🔄 今日飲食紀錄已清空（共 {count} 筆），熱量與蛋白質已重新歸零。"),
        )
        return

    elif msg == "#刪除檔案":
        if uid in user_memory: del user_memory[uid]
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM health_profile WHERE user_id=?", (uid,))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="💥 老闆好，檔案與記憶已徹底銷毀！請重新填表！"))
        return

    elif msg == "#重置":
        if uid in user_memory: del user_memory[uid]
        today = tw_today().isoformat()
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO usage (user_id, remaining_chat_quota, remaining_meals, last_date, status, expiry_date, daily_chat_limit) VALUES (?, 50, 99, ?, 'vip', '2099-12-31', 50)", (uid, today))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="👑 老闆特權啟動！系統已強制為您開通 VIP 檔案並補滿 50 次額度！現在請問我熱量！"))
        return
        
    # 🗺️ 智能測距與順風車 🗺️
    elif msg.startswith("#測距 "):
        target_address = msg.replace("#測距 ", "").strip()
        quote = calculate_delivery_quote(target_address)
        if quote.get("success"):
            reply_text = (
                f"🛵 一日樂食 外送試算結果\n"
                f"📍 目的地：{target_address}\n"
                f"📏 距本店距離：{quote.get('distance_text')}\n"
                f"⏱️ 騎車時間：{quote.get('duration_text')}\n"
                f"💰 運費評估：{quote.get('delivery_fee_text')}\n"
                f"🧭 建議分線：{quote.get('route_group')} / {quote.get('delivery_zone')}\n"
                f"💡 {quote.get('carpool_hint')}"
            )
        else:
            reply_text = "地圖系統暫時找不到這個地址，請確認地址是否完整；若是包月估價，可先回覆「開始包月估價」，外送費由客服最後確認。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
      
    # ==========================================
    # 📅 功能四：每週課表觸發（LINE 指令）
    # ==========================================
    if msg in ["請安排下週課表", "排下週課表", "下週課表", "週課表"]:
        # 🌟 重要：先確保 user_id 有被正確賦值
        target_user_id = event.source.user_id 
        
        try:
            # 1. 立即回應，守住 reply_token
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⏳ 收到！正在分析您的體能數據並編排下週課表，請稍等約 30 秒...")
            )

            # 2. 執行 AI 運算 (請確認你的函數名稱與回傳值正確)
            # 注意：這裡會跑到你寫入 Google Sheet 的邏輯
            ai_message, ai_plan = run_weekly_coach(target_user_id) 

            # 3. 運算完後，使用 push_message 推送
            if ai_message:
                line_bot_api.push_message(
                    target_user_id, 
                    TextSendMessage(text=ai_message)
                )
                print(f"✅ 課表已成功推送給用戶: {target_user_id}")
            
        except Exception as e:
            # 🌟 這裡就是報錯的地方！確保這裡面沒有提到 'affected'
            print(f"❌ 安排課表發生錯誤: {e}")
            line_bot_api.push_message(
                target_user_id, 
                TextSendMessage(text="⚠️ 系統編排課表時發生錯誤，請稍後再試。")
            )
        return # 處理完畢，直接返回
     # 🟢 顧客一般對話 (串接 AI) 🟢
    ai_operation_key = f"line-ai:{uid}:{msg_id}"
    is_ai_estimate = should_ai_create_food_log(msg)
    claim_status = ""
    if is_ai_estimate:
        replay_flex = load_ai_estimate_replay(uid, ai_operation_key)
        if replay_flex is not None:
            try:
                line_bot_api.reply_message(event.reply_token, replay_flex)
            except Exception:
                processed_messages.discard(msg_id)
                raise
            return
        claim_status = claim_ai_estimate_request(uid, ai_operation_key)
        if claim_status in {"pending", "complete", "blocked"}:
            try:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="⏳ 這筆飲食紀錄正在處理，請稍候；完成後重送會直接顯示成功卡。"),
                )
            except Exception:
                processed_messages.discard(msg_id)
                raise
            return
    try:
        allow, q_msg = check_permission_and_quota(uid)
    except Exception:
        if claim_status == "claimed":
            fail_ai_estimate_request(
                uid, ai_operation_key, refund_quota=False
            )
        raise
    if not allow:
        if claim_status == "claimed":
            release_ai_estimate_claim(uid, ai_operation_key)
        return
    else:
        try:
            ai_text, meal_flex = get_ai_response_with_memory(
                uid, msg, operation_key=ai_operation_key
            )
        except Exception:
            if is_ai_estimate:
                fail_ai_estimate_request(
                    uid, ai_operation_key, refund_quota=True
                )
            raise
        if is_ai_estimate and meal_flex is None:
            fail_ai_estimate_request(
                uid, ai_operation_key, refund_quota=True
            )
        if meal_flex:
            line_bot_api.reply_message(event.reply_token, meal_flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{ai_text}\n\n{q_msg}"))


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    message_id = str(event.message.id)
    try:
        return _handle_message_impl(event)
    except Exception:
        processed_messages.discard(message_id)
        raise


# ==========================================
# 🤖 隱形店長專用函數 (自動化排程任務)



# ==========================================
# 到期前自動續約提醒
# ==========================================
def auto_expiry_reminder():
    """每天檢查即將到期的顧客，發送續約優惠提醒"""
    # ⚠️ 優惠內容在這裡設定（可未來修改）
    RENEWAL_DISCOUNT = "【神秘續訂優惠】"  # TODO: 以後改為實際優惠內容
    REMINDER_DAYS_BEFORE = 3  # 到期前幾天開始提醒

    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    today = tw_today()
    remind_date = (today + timedelta(days=REMINDER_DAYS_BEFORE)).isoformat()

    c.execute("""
        SELECT u.user_id, COALESCE(h.name, '親愛的會員') AS name, u.expiry_date
        FROM usage u
        LEFT JOIN health_profile h ON h.user_id = u.user_id
        WHERE u.expiry_date=?
    """, (remind_date,))
    expiring_users = c.fetchall()
    conn.close()

    count = 0
    for uid, name, expiry in expiring_users:
        msg = f"\u2605 {name} \u60a8\u597d\uff01\n\n\ud83d\udcc5 \u60a8\u7684\u5c08\u5c6c\u65b9\u6848\u5c07\u65bc {expiry} \u5230\u671f\n\u2728 \u73fe\u5728\u7e41\u8a02\u53ef\u4eab\u6709\u512a\u60e0\uff1a{RENEWAL_DISCOUNT}\n\n\u8acb\u76f4\u63a5\u56de\u8986\u300c\u6211\u8981\u7e31\u7d04\u300d\uff0c\u6211\u5011\u5c07\u70ba\u60a8\u7121\u7f1d\u5b89\u6392\u4e0b\u4e00\u671f\u83c1\u55ae\uff01"
        try:
            line_bot_api.push_message(uid, TextSendMessage(text=msg))
            count += 1
            print(f"✅ 已發送到期提醒給 {name}（UID: {uid}）")
        except Exception as e:
            print(f"⚠️ 發送失敗 {uid}: {e}")

    print(f"📊 到期前提醒報告：共發送 {count} 封")


def auto_daily_meal_deduction():
    """每天自動扣除今日餐點，並發送續約通知"""
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    today_str = weekdays[tw_today().weekday()]
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{today_str}%",))
    users = c.fetchall()
    
    count, notify_count = 0, 0
    for u in users:
        u_id, u_name = u
        c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (u_id,))
        res = c.fetchone()
        if res and res[0] > 0:
            new_meals = res[0] - 1
            c.execute("UPDATE usage SET remaining_meals=? WHERE user_id=?", (new_meals, u_id))
            count += 1
            if new_meals <= 3 and new_meals > 0:
                notify_msg = f"🎉 {u_name} 您好！您的專屬方案只剩最後 {new_meals} 餐囉！\n您可以直接回覆我「我要續約」，系統將為您無縫安排下一期菜單！"
                try: line_bot_api.push_message(u_id, TextSendMessage(text=notify_msg)); notify_count += 1
                except: pass
    conn.commit(); conn.close()
    
    # 任務完成，發報告給老闆
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
    admin_row = c.fetchone()
    conn.close()
    if admin_row:
        try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=f"🤖【隱形店長報告】今日 ({today_str}) 出餐扣除自動完畢！\n共扣 {count} 份餐點，發送 {notify_count} 則續約推播！"))
        except: pass

def auto_send_tomorrow_reminders_to_boss():
    """每天自動發送明日提醒，並跟老闆回報"""
    result_msg = send_tomorrow_reminders() # 呼叫原本寫好的推播函數
    
    # 任務完成，發報告給老闆
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
    admin_row = c.fetchone()
    conn.close()
    if admin_row:
        try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=f"🤖【隱形店長報告】明日提醒推播完畢：\n{result_msg}"))
        except: pass
def auto_daily_evening_report():
    """每天 22:00（週一～週六）：自動扣餐 + 個人化晚報"""
    print("⏰ [22:00 晚報] 開始執行...")
    
    now          = datetime.now(ZoneInfo("Asia/Taipei"))
    today_str    = now.strftime("%Y/%m/%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y/%m/%d")

    if not gc:
        print("⚠️ [22:00 晚報] gc 未連線，跳過")
        return

    # 每天執行時順便檢查並扣除今日餐點
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    today_cht = weekdays[now.weekday()]

    try:
        users_sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("顧客清單")
        api_sheet   = gc.open_by_key(SPREADSHEET_ID).worksheet("Master_API_View")
        users_data  = users_sheet.get_all_records()
        api_data    = api_sheet.get_all_records()

        # 動態找 剩餘餐數 欄位
        user_headers  = users_sheet.row_values(1)
        remain_col    = (user_headers.index("剩餘餐數") + 1
                         if "剩餘餐數" in user_headers else 4)

        for i, user in enumerate(users_data):
            uid  = str(user.get("User_ID", "")).strip()
            name = str(user.get("姓名", "貴賓")).strip()
            status = str(user.get("狀態", "")).strip()
            if not uid or status not in ("vip", "Active", "active"):
                continue

            # ── 從 Master_API_View 抓今日 & 明日資料 ──────────────────
            today_lunch = today_dinner = today_workout = ""
            tmr_lunch   = tmr_dinner   = tmr_workout   = ""
            tmr_intensity = "LOW"

            for row in api_data:
                if str(row.get("User_ID", "")).strip() != uid:
                    continue
                rd = str(row.get("Date", "")).strip()
                if rd == today_str:
                    today_lunch   = str(row.get("Lunch_Item",  "") or "")
                    today_dinner  = str(row.get("Dinner_Item", "") or "")
                    today_workout = str(row.get("Plan_Week",   "") or "")
                elif rd == tomorrow_str:
                    tmr_lunch    = str(row.get("Lunch_Item",  "") or "")
                    tmr_dinner   = str(row.get("Dinner_Item", "") or "")
                    tmr_workout  = str(row.get("Plan_Week",   "") or "")
                    # 判斷明日強度（由課表關鍵字推斷）
                    tw = tmr_workout.lower()
                    if any(k in tw for k in ["高強度","long run","長跑","間歇","節奏","tempo","race","比賽"]):
                        tmr_intensity = "HIGH"
                    elif any(k in tw for k in ["中強度","一般訓練","moderate"]):
                        tmr_intensity = "MED"

            had_meal_today   = bool(today_lunch or today_dinner)
            has_tmr_meal     = bool(tmr_lunch or tmr_dinner)
            has_tmr_workout  = bool(tmr_workout)

            # ── 自動扣餐（今日有排餐才扣）─────────────────────────────
            remaining = int(user.get("剩餘餐數", 0) or 0)
            if had_meal_today and remaining > 0:
                new_remaining = max(0, remaining - 1)
                try:
                    # ✅ 安全連線：同步扣餐 (SQLite + 顧客清單 Sheet)
                    with closing(sqlite3.connect(DB_PATH)) as conn:
                        _c = conn.cursor()
                        _c.execute("UPDATE usage SET remaining_meals=? WHERE user_id=?", (new_remaining, uid))
                        conn.commit()
                        
                    users_sheet.update_cell(i + 2, remain_col, new_remaining)
                    remaining = new_remaining
                    print(f"✅ {name} 扣餐，剩 {remaining} 餐")
                except Exception as _e:
                    print(f"⚠️ {name} 扣餐失敗: {_e}")

            # ── 組合各段描述 ───────────────────────────────────────────
            today_meal_txt = (f"午：{today_lunch} / 晚：{today_dinner}"
                              if had_meal_today else "今日無取餐紀錄")
            today_work_txt = (f"今日課表安排：{today_workout}" if today_workout else "今日無訓練安排（休息日）")

            tmr_meal_txt   = (f"午：{tmr_lunch} / 晚：{tmr_dinner}"
                              if has_tmr_meal else "無排餐日")
            tmr_work_txt   = (f"明日課表安排：{tmr_workout}" if has_tmr_workout else "明日休息日")

            # 加點提醒（只在明日有訓練時出現）
            if tmr_intensity == "HIGH":
                buy_hint = "明天是高強度訓練日，如果你怕能量不夠，可以自行加點【舒肥雞胸肉】、【原型地瓜】或其他主食補充，不強制，依你的飢餓感與訓練量決定就好 💪"
            elif tmr_intensity == "MED":
                buy_hint = "明天有運動，如果覺得訓練前後會餓，可以自己評估要不要多補一點蛋白質或主食。"
            else:
                buy_hint = ""  # 休息日不提醒加點

            # 低餐數警告（≤5 才顯示）
            low_meal_txt = (f"\n\n⚠️ 餐點快用完囉！您目前剩 {remaining} 餐，需要續訂的話直接回覆「續訂」！"
                            if 0 <= remaining <= 5 else "")

            system_prompt = (
                f"你是「一日樂食」的教練助理（不可自稱營養師、醫師或任何專業職稱）。"
                f"現在是 {today_str} 晚上 22:00，請用自然、溫暖、簡潔、像朋友但不油膩的口吻，寫一則【晚安小結】給 {name}。\n\n"
                f"【今日資料】\n"
                f"- 取餐：{today_meal_txt}\n"
                f"- 運動：{today_work_txt}\n\n"
                f"【明日預告】\n"
                f"- 排餐：{tmr_meal_txt}\n"
                f"- 運動：{tmr_work_txt}\n\n"
                f"【撰寫規則（嚴格遵守）】\n"
                f"1. 固定四段結構：🌙開場 / 📊今日回顧 / 🍱明日提醒 / 🏃明日運動預告。\n"
                f"2. 今日運動只能描述為『今天課表安排』或『若今天有照計畫完成』，絕對不可寫成已完成事實。\n"
                f"3. 禁止使用任何把安排誤寫成成果的句子，例如：『你今天游了』、『今天表現太厲害了』、『今天完成了訓練』。\n"
                f"4. 今日取餐可以中性描述『今天有安排/有取餐』，但禁止主觀稱讚自家餐點，例如：『看起來很好吃』、『一定讓你很滿足』、『相當美味』。\n"
                f"5. 有取餐 → 可肯定紀錄與配合度；無取餐 → 輕鬆說今日無紀錄，明天繼續，禁止硬誇狀態。\n"
                f"6. 若今日有課表，可用條件式語氣給恢復建議，例如：『如果今天有照計畫完成，今晚記得補水與休息』。\n"
                f"7. 明日提醒只描述明天排餐內容與安排，不加銷售感形容詞。\n"
                f"8. 若有加購建議，自然放在最後：「{buy_hint}」；休息日絕對不提加購。\n"
                f"9. 全文控制在 180~220 字，避免浮誇、避免過度鼓舞、避免像廣告文案。\n"
                f"10. 直接輸出給使用者看的內容，不要解釋規則。"
            )

            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "system", "content": system_prompt}],
                    temperature=0.75
                )
                final_msg = resp.choices[0].message.content + low_meal_txt
                line_bot_api.push_message(uid, TextSendMessage(text=final_msg))
                print(f"✅ {name} 晚報發送完成")
            except Exception as _e:
                print(f"⚠️ {name} 晚報失敗: {_e}")

    except Exception as e:
        import traceback
        print(f"⚠️ [22:00 晚報] 錯誤: {e}")
        traceback.print_exc()
# ==========================================
# 📅 功能四：每週日自動批次排課
# ==========================================
def auto_weekly_coach_batch():
    """每週日 22:05：發送個人化週報（本週回顧 + 下週預覽 + 低餐數警告）"""
    import time
    print("🗓️ [週報] 開始批次發送週報...")

    if not gc:
        print("⚠️ [週報] gc 未連線，跳過")
        return

    now = datetime.now(ZoneInfo("Asia/Taipei"))
    # 本週：週一到今天（週日）
    this_monday     = now - timedelta(days=now.weekday())
    week_dates      = [(this_monday + timedelta(days=d)).strftime("%Y/%m/%d") for d in range(7)]
    # 下週：明天起 7 天
    next_monday     = now + timedelta(days=1)
    next_week_dates = [(next_monday + timedelta(days=d)).strftime("%Y/%m/%d") for d in range(7)]
    weekday_map = {0:"週一",1:"週二",2:"週三",3:"週四",4:"週五",5:"週六",6:"週日"}

    try:
        users_sheet  = gc.open_by_key(SPREADSHEET_ID).worksheet("顧客清單")
        api_sheet    = gc.open_by_key(SPREADSHEET_ID).worksheet("Master_API_View")
        users_data   = users_sheet.get_all_records()
        api_data     = api_sheet.get_all_records()
        user_headers = users_sheet.row_values(1)
        remain_col   = (user_headers.index("剩餘餐數") + 1
                        if "剩餘餐數" in user_headers else 4)

        success_count = 0
        for i, user in enumerate(users_data):
            uid    = str(user.get("User_ID", "")).strip()
            name   = str(user.get("姓名", "貴賓")).strip()
            status = str(user.get("狀態", "")).strip()
            if not uid or status not in ("vip", "Active", "active"):
                continue

            # ── 本週 & 下週資料 ──────────────────────────────────────
            this_week_rows, next_week_rows = [], []
            for row in api_data:
                if str(row.get("User_ID", "")).strip() != uid:
                    continue
                rd = str(row.get("Date", "")).strip()
                if rd in week_dates:
                    this_week_rows.append(row)
                elif rd in next_week_dates:
                    next_week_rows.append(row)

            if not this_week_rows and not next_week_rows:
                print(f"  ⚠️ {name} 無資料，跳過")
                continue

            # ── 本週統計 ─────────────────────────────────────────────
            meal_days    = sum(1 for r in this_week_rows
                               if r.get("Lunch_Item","") or r.get("Dinner_Item",""))
            workout_days = sum(1 for r in this_week_rows if r.get("Plan_Week",""))
            total_days   = len(this_week_rows)

            # 最佳單日（午晚都有取餐的日子）
            best_day = ""
            for r in this_week_rows:
                if r.get("Lunch_Item","") and r.get("Dinner_Item",""):
                    try:
                        d_obj  = datetime.strptime(str(r.get("Date","")), "%Y/%m/%d")
                        wlabel = weekday_map[d_obj.weekday()]
                        best_day = (f"{r.get('Date','')}（{wlabel}）"
                                    f"午：{r.get('Lunch_Item','')} / 晚：{r.get('Dinner_Item','')}")
                    except:
                        pass

            # ── 下週預覽 ─────────────────────────────────────────────
            next_meal_lines, next_workout_lines = [], []
            for r in sorted(next_week_rows, key=lambda x: str(x.get("Date",""))):
                rd = str(r.get("Date",""))
                try:
                    d_obj  = datetime.strptime(rd, "%Y/%m/%d")
                    wlabel = weekday_map[d_obj.weekday()]
                except:
                    wlabel = rd
                if r.get("Lunch_Item","") or r.get("Dinner_Item",""):
                    next_meal_lines.append(
                        f"  {rd}（{wlabel}）午：{r.get('Lunch_Item','－')} / 晚：{r.get('Dinner_Item','－')}")
                if r.get("Plan_Week",""):
                    next_workout_lines.append(
                        f"  {rd}（{wlabel}）{str(r.get('Plan_Week',''))[:40]}")

            # ── 低餐數警告 ───────────────────────────────────────────
            remaining    = int(user.get("剩餘餐數", 0) or 0)
            low_meal_txt = (f"\n\n⚠️ 餐點快用完了！您目前剩 {remaining} 餐，需要續訂直接回覆「續訂」！"
                            if 0 <= remaining <= 5 else "")

            # ── system prompt ────────────────────────────────────────
            next_meal_txt    = "\n".join(next_meal_lines)    or "  下週尚無取餐安排"
            next_workout_txt = "\n".join(next_workout_lines) or "  下週尚無課表資料"
            best_day_txt     = best_day or "本週尚無完整取餐紀錄"

            system_prompt = (
                f"你是「一日樂食」的教練助理（不可自稱營養師或任何醫療專業職稱）。"
                f"今天是週日 {now.strftime('%Y/%m/%d')}，請為 {name} 撰寫一則【週日週報】。\n\n"
                f"【本週數據（{week_dates[0]} ～ {week_dates[-1]}）】\n"
                f"- 取餐天數：{meal_days} 天（共 {total_days} 天有資料）\n"
                f"- 運動天數：{workout_days} 天\n"
                f"- 🌟 最佳單日：{best_day_txt}\n\n"
                f"【下週預覽（{next_week_dates[0]} ～ {next_week_dates[-1]}）】\n"
                f"📅 取餐安排：\n{next_meal_txt}\n\n"
                f"💪 課表重點：\n{next_workout_txt}\n\n"
                f"【撰寫規則（嚴格遵守）】\n"
                f"1. 📊 本週回顧：取餐與運動達成情況，給予鼓勵或溫和鞭策\n"
                f"2. 🌟 最佳單日：明確點名，給予肯定\n"
                f"3. 📅 下週取餐一覽（條列）\n"
                f"4. 💪 下週課表重點（條列，每天一行）\n"
                f"5. 🎯 一句個人化激勵語收尾\n"
                f"6. 字數控制在 300 字內，口吻像真人朋友，禁止生硬行銷語言"
            )

            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "system", "content": system_prompt}],
                    temperature=0.75
                )
                final_msg = resp.choices[0].message.content + low_meal_txt
                line_bot_api.push_message(uid, TextSendMessage(text=final_msg))
                print(f"  ✅ {name} 週報完成")
                success_count += 1
            except Exception as _e:
                print(f"  ⚠️ {name} 週報失敗: {_e}")

            time.sleep(2)  # 避免打爆 API

        print(f"🗓️ [週報] 完成，共發送 {success_count} 人")

    except Exception as e:
        import traceback
        print(f"⚠️ [週報] 錯誤: {e}")
        traceback.print_exc()

# ==========================================
# 🦞 龍蝦專屬安全通道 (給 OpenClaw 讀取與發送訊息用)
# ==========================================
from pydantic import BaseModel

class LobsterPayload(BaseModel):
    admin_secret: str
    user_id: str
    coach_message: str

# ==========================================
# 🏃 Intervals.icu 數據抓取 (每位運動員個別設定)
# ==========================================
def get_intervals_data(athlete_id, api_key):
    if not athlete_id or not api_key: return None
    try:
        url = f"https://intervals.icu/api/v1/athlete/{athlete_id}"
        resp = requests.get(url, auth=('athlete', api_key), timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "fitness": round(data.get("icu_fitness", 0)),
                "fatigue": round(data.get("icu_fatigue", 0)),
                "form": round(data.get("icu_training_load_balance", 0))
            }
    except Exception:
        return None

@app.get("/api/lobster/daily_targets")
async def get_lobster_targets(admin_secret: str, mode: str = "daily"):
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    today_str = tw_today().strftime("%Y/%m/%d")
    tomorrow_str = (tw_today() + timedelta(days=1)).strftime("%Y/%m/%d")
    targets = []

    # 1. 取得資料庫中的使用者紀錄
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    try:
        c.execute("SELECT user_id, name, today_extra_cal, today_food_items, tdee FROM health_profile WHERE is_coaching_enabled = 1")
        users = c.fetchall()
    except sqlite3.OperationalError:
        return {"status": "success", "targets": []}
    conn.close()

    if not users: return {"status": "success", "targets": []}
    user_dict = {u[0]: {"name": u[1], "extra_cal": u[2], "food_items": u[3], "tdee": u[4]} for u in users}

    # 2. 只有在 Google Sheet 成功連線時執行
    if gc:
        try:
            sheet = gc.open_by_url(SHEET_URL)
            api_sheet = sheet.worksheet("Master_API_View")
            records = api_sheet.get_all_records()
            sheet_data = {(str(r.get("User_ID")), str(r.get("Date"))): r for r in records}

            for uid, u_info in user_dict.items():
                row_today = sheet_data.get((uid, today_str), {})
                plan_type = str(row_today.get("Plan_Type", "一般飲食"))
                is_athlete = any(k in plan_type for k in ["運動", "鐵人", "三鐵"])
                tdee = int(row_today.get("TDEE", 0)) if row_today.get("TDEE") else u_info["tdee"]

                # 從 Sheet 讀取個別 Intervals 設定
                icu_id = str(row_today.get("Intervals_ID", ""))
                icu_key = str(row_today.get("Intervals_API_Key", ""))

                user_data = {
                    "user_id": uid,
                    "name": u_info["name"],
                    "is_athlete": is_athlete,
                    "sport_type": str(row_today.get("Sport_Type", "無")),
                    "plan_week": str(row_today.get("Plan_Week", "計畫未開始")),
                    "tdee": tdee
                }

                if mode == "daily":
                    total_cal = 800 + u_info["extra_cal"]
                    row_tomorrow = sheet_data.get((uid, tomorrow_str), {})
                    user_data["today_summary"] = {
                        "lunch": row_today.get("Lunch_Item", ""),
                        "dinner": row_today.get("Dinner_Item", ""),
                        "extra_food": u_info["food_items"] or "無",
                        "total_consumed_cal": total_cal,
                        "caloric_deficit": tdee - total_cal
                    }

                    # ==========================================
                    # 🔄 功能三：Tomorrow_Workout 動態覆蓋機制
                    # 優先使用客人手動輸入，用完後清空欄位
                    # ==========================================
                    manual_workout = str(row_today.get("Tomorrow_Workout", "")).strip()
                    manual_intensity = str(row_today.get("Tomorrow_Intensity", "")).strip()

                    if manual_workout:
                        # 有手動輸入 → 使用它，並清空（重置迎接下一天）
                        tomorrow_workout = manual_workout
                        tomorrow_intensity = manual_intensity or "MED"
                        try:
                            headers = api_sheet.row_values(1)
                            today_records = api_sheet.get_all_records()
                            today_target = next(
                                (i + 2 for i, r in enumerate(today_records)
                                 if str(r.get("User_ID")) == uid and str(r.get("Date")) == today_str),
                                None
                            )
                            if today_target and "Tomorrow_Workout" in headers:
                                tw_col = headers.index("Tomorrow_Workout") + 1
                                ti_col = headers.index("Tomorrow_Intensity") + 1 if "Tomorrow_Intensity" in headers else None
                                api_sheet.update_cell(today_target, tw_col, "")  # 清空
                                if ti_col:
                                    api_sheet.update_cell(today_target, ti_col, "")  # 清空
                        except Exception as e:
                            print(f"⚠️ 清空 Tomorrow_Workout 失敗: {e}")
                    else:
                        # 無手動輸入 → 退回原本邏輯，抓明天 row 的 Today_Workout
                        tomorrow_workout = str(row_tomorrow.get("Today_Workout", "休息日"))
                        tomorrow_intensity = str(row_tomorrow.get("Workout_Intensity", "LOW")).upper()

                    user_data["tomorrow_preview"] = {
                        "date": tomorrow_str,
                        "workout": tomorrow_workout,
                        "intensity": tomorrow_intensity
                    }
                    targets.append(user_data)

                elif mode == "weekly":
                    # weekly 模式：額外抓 Intervals.icu CTL/ATL/Form
                    user_data["intervals_icu"] = get_intervals_data(icu_id, icu_key) if (is_athlete and icu_id and icu_key) else None
                    targets.append(user_data)

        except Exception as e:
            print(f"⚠️ 龍蝦通道讀取失敗: {e}")

    return {"status": "success", "targets": targets}

@app.post("/api/lobster/send_message")
async def lobster_send_message(payload: LobsterPayload):
    if payload.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        line_bot_api.push_message(payload.user_id, TextSendMessage(text=payload.coach_message))
        return {"status": "success", "msg": f"已發送教練報告給 {payload.user_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 📅 功能四：每週教練系統觸發端點
# ==========================================
class WeeklyCoachPayload(BaseModel):
    admin_secret: str
    user_id: str

@app.post("/api/lobster/weekly_coach")
async def lobster_weekly_coach(payload: WeeklyCoachPayload):
    """系統排程觸發每週教練排課，結果寫入 Plan_Week 並推播 LINE"""
    if payload.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    success, result = run_weekly_coach(payload.user_id)
    if success:
        return {"status": "success", "msg": "每週課表已生成並推播", "plan_preview": result[:100] + "..."}
    else:
        raise HTTPException(status_code=500, detail=result)
