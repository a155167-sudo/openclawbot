"""電腦營養（staging）專用：三日健檢一晚測完的捷徑。

真實流程要客人開通後 7 天內記滿 3 天（每天至少 2 個餐別的已確認紀錄）。
這裡把測試帳號既有的健檢案件窗口往前挪 3 天，再補記前 3 天的午餐、晚餐，
走正式的飲食入帳與案件刷新，案件就會進到 ready_for_review，營養師工作台馬上看得到。

安全邊界：
- 只在 APP_ENV=staging 由管理員觸發（server.py 負責檢查）。
- 不建立案件：帳號必須已經在電腦營養「第一次開通」過，案件由正式開通流程產生。
- 只動 collecting／ready_for_review／needs_more_info 的案件；已核准、已送達的不碰。
- 補記用固定 operation_key，重跑不會重複入帳。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
QA_SOURCE_TYPE = "qa_health_check_seed"
REFRESHABLE = ("collecting", "ready_for_review", "needs_more_info")
SEED_DAYS = 3
SLOT_TIMES = (("午餐", time(12, 30)), ("晚餐", time(18, 30)))


class QaRefused(Exception):
    """可以直接回給管理員看的拒絕原因。"""


def seed_meals_from_menu(rows: Sequence[dict]) -> list[dict]:
    """從 menu.csv 的列挑便當／食蔬／低碳類，給補記用（品名＋四項營養）。"""
    picks = []
    for row in rows:
        name = str(row.get("品項") or "").strip()
        if not name or not any(tag in name for tag in ("便當", "食蔬", "低碳")):
            continue
        try:
            picks.append({
                "name": name,
                "nutrition": {
                    "calories_kcal": round(float(row["熱量(kcal)"]), 1),
                    "protein_g": round(float(row["蛋白質(g)"]), 1),
                    "fat_g": round(float(row["脂肪(g)"]), 1),
                    "carbohydrate_g": round(float(row["碳水化合物(g)"]), 1),
                },
            })
        except (KeyError, TypeError, ValueError):
            continue
    if len(picks) < SEED_DAYS * len(SLOT_TIMES):
        raise QaRefused("菜單資料不足，無法補記測試餐點。")
    return picks


def run_health_check_qa(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    now: datetime,
    meals: Sequence[dict],
    create_food_log: Callable[..., dict],
    refresh_case: Callable[..., dict | None],
) -> dict:
    """挪窗口、補記 3 天 × 午晚餐、刷新案件。呼叫端負責 commit。"""
    user_id = str(user_id or "").strip()
    if now.tzinfo is None:
        raise ValueError("now 必須包含時區")
    now = now.astimezone(TAIPEI)
    case = conn.execute(
        "SELECT case_id,status FROM vip_health_check_cases WHERE user_id=?", (user_id,)
    ).fetchone()
    if not case:
        raise QaRefused(
            "這個帳號在電腦營養還沒有健檢案件。請先用這個帳號第一次開通包月（兌換開通碼），"
            "系統會自動建立案件，再傳一次這個指令。")
    case_id, status = case
    if status not in REFRESHABLE:
        raise QaRefused(f"這個帳號的健檢已經是「{status}」，不能再補測試資料；請換一個測試帳號。")

    first_day = now.date() - timedelta(days=SEED_DAYS)
    window_start = datetime.combine(first_day, time(0, 0), tzinfo=TAIPEI)
    window_end = window_start + timedelta(days=7)
    conn.execute(
        """UPDATE vip_health_check_cases SET window_started_at=?,window_ends_at=?,updated_at=?
           WHERE case_id=? AND status=?""",
        (window_start.isoformat(timespec="seconds"), window_end.isoformat(timespec="seconds"),
         now.isoformat(timespec="seconds"), case_id, status),
    )
    conn.execute(
        """INSERT INTO vip_health_check_audit_log
           (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
           VALUES (?,'system','staging_qa',?,?,'staging_qa_window_shift',?)""",
        (case_id, status, status, now.isoformat(timespec="seconds")),
    )

    seeded = 0
    for day_index in range(SEED_DAYS):
        day = first_day + timedelta(days=day_index)
        for slot_index, (slot, at) in enumerate(SLOT_TIMES):
            meal = meals[(day_index * len(SLOT_TIMES) + slot_index) % len(meals)]
            result = create_food_log(
                conn, user_id=user_id, product_name=meal["name"], meal_slot=slot,
                consumed_at=datetime.combine(day, at, tzinfo=TAIPEI).isoformat(timespec="seconds"),
                servings=1, nutrition=meal["nutrition"], source_type=QA_SOURCE_TYPE,
                operation_key=f"qa-health-check:{user_id}:{day.isoformat()}:{slot}",
                publish_catalog=False,
            )
            seeded += 0 if result.get("replayed") else 1
    refreshed = refresh_case(conn, user_id=user_id, evaluated_at=now) or {}
    return {
        "case_id": case_id,
        "status": refreshed.get("status", status),
        "valid_day_count": refreshed.get("valid_day_count"),
        "seeded": seeded,
        "first_day": first_day.isoformat(),
    }
