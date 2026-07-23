"""無營養標示餐點照片的安全草稿、確認與估算呈現。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from nutrition_system import ensure_nutrition_schema, insert_approved_meal_photo_log


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
VISIBLE_CATEGORIES = {"vegetable", "protein", "starch", "fruit", "milk", "unknown"}
STARCH_VISIBILITY = {"visible", "not_visible", "unknown"}
OIL_SAUCE_STATUS = {"visible", "not_visible", "unknown"}
ANSWER_VALUES = {
    "scope": {"visible_only", "has_unseen", "unknown"},
    "protein_type": {"chicken", "pork", "fish", "egg", "tofu", "other", "none", "unknown"},
    "protein_portion": {"half_palm", "one_palm", "one_half_palm", "two_palm", "none", "unknown"},
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
    return {
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


def ensure_meal_photo_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
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
            approved_by TEXT NOT NULL DEFAULT ''
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
    now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO meal_photo_schema_versions(component,version,updated_at)
           VALUES('meal_photo_system',3,?)
           ON CONFLICT(component) DO UPDATE SET
             version=MAX(version,excluded.version),updated_at=excluded.updated_at""",
        (now,),
    )
    conn.commit()


def _blank_answers() -> dict[str, str | None]:
    return {field: None for field in ANSWER_VALUES}


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
) -> str:
    ensure_meal_photo_schema(conn)
    user_id = _short_text(user_id, "user_id", maximum=120)
    source_message_id = _short_text(
        source_message_id, "source_message_id", maximum=160, required=False
    )
    if consumed_time_source not in {"photo_timestamp", "line_timestamp", "manual"}:
        raise ValueError("consumed_time_source 不支援")
    normalized = normalize_meal_photo_payload(payload)
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
                status,created_at,updated_at,expires_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'awaiting_confirmation',?,?,?)""",
            (
                token,
                user_id,
                source_message_id,
                str(source_image_ref or "")[:240],
                json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False),
                json.dumps(_blank_answers(), ensure_ascii=False, sort_keys=True),
                "{}",
                str(meal_slot or "")[:30],
                str(consumed_at or now)[:50],
                consumed_time_source,
                now,
                now,
                (now_dt + timedelta(hours=24)).isoformat(timespec="seconds"),
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


def get_meal_photo_draft(
    conn: sqlite3.Connection, *, user_id: str, token: str
) -> dict[str, Any]:
    ensure_meal_photo_schema(conn)
    row = conn.execute(
        """SELECT source_message_id,source_image_ref,observed_payload_json,answers_json,
                  estimate_json,meal_slot,consumed_at,consumed_time_source,status,
                  created_at,updated_at,expires_at,version,review_json,approved_log_id,
                  approved_at,approved_by
           FROM pending_meal_photo_drafts WHERE token=? AND user_id=?""",
        (token, user_id),
    ).fetchone()
    if not row:
        raise ValueError("找不到這筆餐點照片草稿")
    if _expired(row[11]) and row[8] in {
        "awaiting_confirmation", "confirming", "estimated", "reviewing", "review_ready"
    }:
        retired = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE pending_meal_photo_drafts SET status='expired',
               observed_payload_json='{}',answers_json='{}',estimate_json='{}',review_json='{}',retired_at=?
               WHERE token=? AND user_id=?""",
            (retired, token, user_id),
        )
        conn.commit()
        raise ValueError("這筆餐點照片草稿已逾時")
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
    }


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
                 'awaiting_confirmation','confirming','estimated','reviewing','review_ready'
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
                           'awaiting_confirmation','confirming','estimated','reviewing','review_ready'
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
    for field in (
        "scope", "protein_type", "protein_portion", "starch_portion",
        "vegetable_portion", "cooking_oil", "sauce_level",
    ):
        if field == "protein_portion" and answers.get("protein_type") == "none":
            continue
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
    return {
        "calories_kcal": None,
        "protein_g": None,
        "fat_g": None,
        "carbohydrate_g": None,
        "protein_total_exchange": _range_value(
            "protein_portion", answers.get("protein_portion"), confirmed_none=protein_none
        ),
        "starch_exchange": _range_value("starch_portion", answers.get("starch_portion")),
        "vegetable_exchange": _range_value("vegetable_portion", answers.get("vegetable_portion")),
        "cooking_oil_confirmation": answers.get("cooking_oil"),
        "sauce_confirmation": answers.get("sauce_level"),
        "formal_status": "pending_review_not_counted",
        "rule_version": "hand-portion-range-v1",
    }


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
) -> dict[str, Any]:
    """以durable event與版本鎖原子套用LINE Postback；重送回傳原result。"""
    ensure_meal_photo_schema(conn)
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
    elif action == "cancel":
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
            conn.commit()
            return {
                "replayed": True,
                "result": result,
                "draft": get_meal_photo_draft(conn, user_id=user_id, token=token),
            }
        row = conn.execute(
            """SELECT answers_json,status,expires_at,version,source_image_ref
               FROM pending_meal_photo_drafts WHERE token=? AND user_id=?""",
            (token, user_id),
        ).fetchone()
        if not row:
            raise ValueError("找不到這筆餐點照片草稿")
        answers = {**_blank_answers(), **json.loads(row[0] or "{}")}
        status, expires_at, current_version = row[1], row[2], int(row[3])
        if _expired(expires_at):
            raise ValueError("這筆餐點照片草稿已逾時")
        if status not in {"awaiting_confirmation", "confirming"}:
            raise ValueError("這筆餐點照片不能再修改")
        if current_version != expected_version:
            raise ValueError("餐點確認畫面已更新，請使用最新按鈕")
        next_version = current_version + 1
        now = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
        if action == "cancel":
            result = {
                "kind": "cancel", "version": next_version, "source_image_ref": row[4],
            }
            changed = conn.execute(
                """UPDATE pending_meal_photo_drafts
                   SET observed_payload_json='{}',answers_json='{}',estimate_json='{}',
                       status='cancelled',retired_at=?,updated_at=?,version=?
                   WHERE token=? AND user_id=? AND version=?
                     AND status IN ('awaiting_confirmation','confirming')""",
                (now, now, next_version, token, user_id, current_version),
            ).rowcount
        else:
            expected_step = next_meal_photo_step({"answers": answers})
            if field != expected_step:
                raise ValueError("餐點確認步驟不符，請使用最新按鈕")
            answers[field] = value
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
    if user_id != admin_user_id:
        raise PermissionError("管理員限定，且目前只能核准自己的餐點照片")
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise ValueError("餐點草稿token無效")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("餐點審核畫面版本無效") from exc
    if expected_version < 1 or action not in {"start", "set", "cancel_review", "approve"}:
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
        else:
            if status != "review_ready" or next_meal_photo_review_step({"review": review}) != "complete":
                raise ValueError("正式份量尚未選完")
            exact = _meal_photo_exact_exchange(review)
            formal_result = insert_approved_meal_photo_log(
                conn, token=token, user_id=user_id, reviewer=admin_user_id,
                consumed_at=row[9], meal_slot=row[8], source_image_ref=row[7],
                observed_payload=observed, answers=answers, exact_exchange=exact,
            )
            status = "approved"
            result = {
                "kind": "approved", "version": next_version,
                "log_id": formal_result["log_id"],
                "approval_id": formal_result["approval_id"],
                "approved_exchange": exact,
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
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFF3CD",
            "contents": [line("📊 照片估算｜尚未計入正式份量", "#7A4E00", "md")],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                line("熱量：NA（沒有營養標示，無法精確判定）", "#B00020"),
                line(_format_exchange_range(estimate.get("starch_exchange"), "主食")),
                line(_format_exchange_range(estimate.get("protein_total_exchange"), "蛋白質食物")),
                line(_format_exchange_range(estimate.get("vegetable_exchange"), "蔬菜")),
                line(f"烹調用油：{oil_text}（使用者確認）"),
                line(f"湯汁／醬汁：{sauce_text}（使用者確認）"),
                line("⚠️ 待營養師審核，尚未扣入個人營養計畫。", "#B26A00"),
                line("未知維持NA；只有你明確選『沒有』才顯示0份。", "#777777", "xs"),
            ],
        },
    }
    if allow_admin_review:
        token = str(draft.get("token") or "")
        version = int(draft.get("version") or 0)
        if not re.fullmatch(r"[0-9a-f]{12}", token) or version < 1:
            raise ValueError("餐點審核資料無效")
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
    return bubble


def build_meal_photo_confirmation_bubble(
    payload: Mapping[str, Any], *, token: str, consumed_at: str, version: int = 1
) -> dict[str, Any]:
    normalized = normalize_meal_photo_payload(payload)
    if not re.fullmatch(r"[0-9a-f]{12}", str(token or "")):
        raise ValueError("餐點草稿token無效")
    visible = "、".join(item["name"] for item in normalized["visible_items"])
    protein_names = "、".join(
        item["name"] for item in normalized["visible_items"] if item["category"] == "protein"
    )
    protein = (
        f"畫面可見 {protein_names}；種類／份量仍為NA（待確認）"
        if protein_names else "NA（待確認；畫面未見不代表沒有吃）"
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
                line(f"看見：{visible}"),
                line(f"不確定：{uncertain}", "#B26A00"),
                line(f"蛋白質食物：{protein}"),
                line(f"主食：{starch}"),
                line("水果／奶類／其他未入鏡：NA（待確認）"),
                line(f"烹調用油／醬汁：{sauce}"),
                line("熱量與交換份：NA（尚未估算）", "#B00020"),
                line("未知不會當成0；確認前不計入正式紀錄。", "#777777", "xs"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
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
