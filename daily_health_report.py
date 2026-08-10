"""Deterministic storage and formatting for Jason's daily health report."""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping


_CHECKIN_RE = re.compile(
    r"^健康回報\s*[｜|]\s*體重(?P<weight>[^｜|]+)\s*"
    r"[｜|]\s*飲水(?P<water>[^｜|]+)\s*"
    r"[｜|]\s*排便(?P<bowel>[^｜|]+)\s*"
    r"[｜|]\s*用藥(?P<medication>[^｜|]+)\s*"
    r"[｜|]\s*睡眠(?P<sleep>[^｜|]+)\s*"
    r"[｜|]\s*品質(?P<quality>[^｜|]+)\s*$"
)


def _clock(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError("睡眠時間格式須為 HH:MM-HH:MM") from exc


def parse_health_checkin(text: str) -> dict[str, Any]:
    """Parse the copyable one-line LINE check-in without any LLM guessing."""
    match = _CHECKIN_RE.fullmatch(str(text or "").strip())
    if not match:
        raise ValueError("格式錯誤，請使用健康回報範本")

    try:
        weight = float(match.group("weight").strip())
    except ValueError as exc:
        raise ValueError("體重必須是數字") from exc
    if not 20 <= weight <= 300:
        raise ValueError("體重須介於20至300公斤")

    try:
        water = int(match.group("water").strip())
    except ValueError as exc:
        raise ValueError("飲水必須是整數毫升") from exc
    if not 0 <= water <= 10000:
        raise ValueError("飲水須介於0至10000毫升")

    bowel = match.group("bowel").strip()
    if bowel not in {"有", "無", "NA"}:
        raise ValueError("排便只能填有、無或NA")

    medication = match.group("medication").strip()
    if not medication or len(medication) > 100:
        raise ValueError("用藥內容不可空白且最多100字")

    sleep_parts = [part.strip() for part in match.group("sleep").split("-", 1)]
    if len(sleep_parts) != 2:
        raise ValueError("睡眠時間格式須為 HH:MM-HH:MM")
    sleep_start, sleep_end = map(_clock, sleep_parts)

    quality = match.group("quality").strip()
    if quality not in {"良好", "普通", "不佳", "NA"}:
        raise ValueError("品質只能填良好、普通、不佳或NA")

    return {
        "weight_kg": weight,
        "water_ml": water,
        "bowel_status": bowel,
        "medication": medication,
        "sleep_start": sleep_start,
        "sleep_end": sleep_end,
        "sleep_quality": quality,
    }


_DELIVERY_KINDS = {"prompt", "report"}


def ensure_daily_health_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_health_checkins (
            user_id TEXT NOT NULL,
            report_date TEXT NOT NULL,
            weight_kg REAL NOT NULL,
            water_ml INTEGER NOT NULL,
            bowel_status TEXT NOT NULL,
            medication TEXT NOT NULL,
            sleep_start TEXT NOT NULL,
            sleep_end TEXT NOT NULL,
            sleep_quality TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, report_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_health_deliveries (
            user_id TEXT NOT NULL,
            report_date TEXT NOT NULL,
            prompt_status TEXT NOT NULL DEFAULT 'pending',
            prompt_claimed_at TEXT NOT NULL DEFAULT '',
            prompt_claim_token TEXT NOT NULL DEFAULT '',
            prompt_sent_at TEXT NOT NULL DEFAULT '',
            prompt_attempts INTEGER NOT NULL DEFAULT 0,
            report_status TEXT NOT NULL DEFAULT 'pending',
            report_claimed_at TEXT NOT NULL DEFAULT '',
            report_claim_token TEXT NOT NULL DEFAULT '',
            report_sent_at TEXT NOT NULL DEFAULT '',
            report_attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, report_date)
        )
        """
    )
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(daily_health_deliveries)")
    }
    migrations = {
        "prompt_claim_token": (
            "ALTER TABLE daily_health_deliveries ADD COLUMN "
            "prompt_claim_token TEXT NOT NULL DEFAULT ''"
        ),
        "report_claim_token": (
            "ALTER TABLE daily_health_deliveries ADD COLUMN "
            "report_claim_token TEXT NOT NULL DEFAULT ''"
        ),
    }
    for column, sql in migrations.items():
        if column not in existing_columns:
            conn.execute(sql)
    conn.commit()


def save_daily_health_checkin(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    report_date: str,
    values: Mapping[str, Any],
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO daily_health_checkins (
            user_id,report_date,weight_kg,water_ml,bowel_status,medication,
            sleep_start,sleep_end,sleep_quality,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,report_date) DO UPDATE SET
            weight_kg=excluded.weight_kg,
            water_ml=excluded.water_ml,
            bowel_status=excluded.bowel_status,
            medication=excluded.medication,
            sleep_start=excluded.sleep_start,
            sleep_end=excluded.sleep_end,
            sleep_quality=excluded.sleep_quality,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            report_date,
            float(values["weight_kg"]),
            int(values["water_ml"]),
            str(values["bowel_status"]),
            str(values["medication"]),
            str(values["sleep_start"]),
            str(values["sleep_end"]),
            str(values["sleep_quality"]),
            updated_at,
        ),
    )
    conn.commit()


def get_daily_health_checkin(
    conn: sqlite3.Connection, *, user_id: str, report_date: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT weight_kg,water_ml,bowel_status,medication,sleep_start,sleep_end,
               sleep_quality,updated_at
        FROM daily_health_checkins WHERE user_id=? AND report_date=?
        """,
        (user_id, report_date),
    ).fetchone()
    if not row:
        return None
    keys = (
        "weight_kg", "water_ml", "bowel_status", "medication", "sleep_start",
        "sleep_end", "sleep_quality", "updated_at",
    )
    return dict(zip(keys, row))


def _delivery_columns(kind: str) -> tuple[str, str, str, str, str]:
    if kind not in _DELIVERY_KINDS:
        raise ValueError("delivery kind must be prompt or report")
    return (
        f"{kind}_status",
        f"{kind}_claimed_at",
        f"{kind}_claim_token",
        f"{kind}_sent_at",
        f"{kind}_attempts",
    )


def claim_daily_delivery(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    report_date: str,
    kind: str,
    claimed_at: str,
) -> str | None:
    status_col, claimed_col, token_col, _sent_col, attempts_col = _delivery_columns(kind)
    claim_token = secrets.token_hex(16)
    try:
        claimed_dt = datetime.fromisoformat(claimed_at)
        stale_before = (claimed_dt - timedelta(minutes=8)).isoformat(timespec="seconds")
    except ValueError:
        stale_before = ""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO daily_health_deliveries (user_id,report_date) VALUES (?,?) "
            "ON CONFLICT(user_id,report_date) DO NOTHING",
            (user_id, report_date),
        )
        sql = f"""
            UPDATE daily_health_deliveries
            SET {status_col}='processing', {claimed_col}=?, {token_col}=?,
                {attempts_col}={attempts_col}+1, last_error=''
            WHERE user_id=? AND report_date=? AND {status_col}!='sent'
              AND ({status_col}!='processing' OR {claimed_col}<=?)
        """
        cursor = conn.execute(
            sql, (claimed_at, claim_token, user_id, report_date, stale_before)
        )
        conn.commit()
        return claim_token if cursor.rowcount == 1 else None
    except Exception:
        conn.rollback()
        raise


def finish_daily_delivery(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    report_date: str,
    kind: str,
    claim_token: str,
    sent: bool,
    finished_at: str,
    error: str = "",
) -> bool:
    status_col, _claimed_col, token_col, sent_col, _attempts_col = _delivery_columns(kind)
    status = "sent" if sent else "pending"
    sent_at = finished_at if sent else ""
    cursor = conn.execute(
        f"""
        UPDATE daily_health_deliveries
        SET {status_col}=?, {sent_col}=?, last_error=?
        WHERE user_id=? AND report_date=? AND {status_col}='processing'
          AND {token_col}=?
        """,
        (
            status, sent_at, str(error or "")[:500], user_id, report_date,
            claim_token,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def _fmt_number(value: Any) -> str:
    if value is None:
        return "NA"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "NA"
    return str(int(number)) if number.is_integer() else f"{number:.1f}".rstrip("0").rstrip(".")


def summarize_intervals_activities(activities: list[Mapping[str, Any]]) -> dict[str, Any]:
    type_labels = {
        "Run": "🏃跑步", "Ride": "🚴騎車", "VirtualRide": "🚴室內騎",
        "Swim": "🏊游泳", "Walk": "🚶健走",
    }
    result: dict[str, Any] = {
        "total_calories": 0.0,
        "total_duration_min": 0.0,
        "hr_load": 0.0,
        "items": [],
    }
    for activity in activities:
        label = type_labels.get(str(activity.get("type") or ""), str(activity.get("type") or "運動"))
        distance_km = float(activity.get("icu_distance") or 0) / 1000
        duration_min = round(float(activity.get("moving_time") or 0) / 60)
        average_hr = round(float(activity.get("average_heartrate") or 0))
        calories = round(float(activity.get("calories") or 0))
        load = float(activity.get("hr_load") or 0)
        result["total_calories"] += calories
        result["total_duration_min"] += duration_min
        result["hr_load"] += load
        result["items"].append(
            f"{label} {_fmt_number(distance_km)}km／{_fmt_number(duration_min)}min／"
            f"均心率{_fmt_number(average_hr)}bpm／消耗{_fmt_number(calories)}kcal"
        )
    for key in ("total_calories", "total_duration_min", "hr_load"):
        result[key] = round(result[key], 1)
    return result


def format_daily_health_report(
    *,
    report_date: str,
    checkin: Mapping[str, Any] | None,
    foods: list[Mapping[str, Any]],
    totals: Mapping[str, Any],
    target: Mapping[str, Any] | None,
    exercise: Mapping[str, Any] | None,
    pending_reviews: int,
) -> str:
    """Render a deterministic LINE-safe daily summary (no inferred facts)."""
    date_display = report_date.replace("-", "/")
    lines = [f"📋 {date_display} 一日健康日報", "────────────"]

    if checkin:
        lines.extend(
            [
                "🩺 健康紀錄",
                f"體重：{_fmt_number(checkin.get('weight_kg'))} kg｜飲水：{int(checkin.get('water_ml') or 0)} ml",
                f"排便：{checkin.get('bowel_status') or 'NA'}｜用藥：{checkin.get('medication') or 'NA'}",
                f"睡眠：{checkin.get('sleep_start') or 'NA'}–{checkin.get('sleep_end') or 'NA'}（{checkin.get('sleep_quality') or 'NA'}）",
            ]
        )
    else:
        lines.extend(
            [
                "🩺 健康紀錄",
                "體重：NA｜飲水：NA",
                "排便：NA｜用藥：NA",
                "睡眠：NA（未回報）",
            ]
        )

    lines.append("")
    if exercise is None:
        lines.append("🏃 今日運動：NA（Intervals.icu無法取得）")
    elif exercise.get("items"):
        lines.append("🏃 今日運動")
        lines.extend(str(item)[:250] for item in list(exercise.get("items") or [])[:10])
        lines.append(
            f"合計：{_fmt_number(exercise.get('total_duration_min'))}min｜"
            f"消耗{_fmt_number(exercise.get('total_calories'))}kcal｜"
            f"負荷{_fmt_number(exercise.get('hr_load'))}"
        )
    else:
        lines.append("🏃 今日運動：無活動紀錄")

    lines.extend(["", "🍽 飲食明細"])
    if foods:
        for item in foods[:20]:
            lines.append(
                f"{item.get('time') or '--:--'} {str(item.get('name') or '未命名食品')[:60]}｜"
                f"{_fmt_number(item.get('calories_kcal'))} kcal｜"
                f"蛋白質{_fmt_number(item.get('protein_g'))}g"
            )
        if len(foods) > 20:
            lines.append(f"另有{len(foods) - 20}筆，請至飲食紀錄查看")
    else:
        lines.append("今日無已確認飲食紀錄")

    lines.extend(
        [
            "",
            "📊 今日攝取總計",
            f"熱量{_fmt_number(totals.get('calories_kcal'))} kcal｜蛋白質{_fmt_number(totals.get('protein_g'))}g",
            f"脂肪{_fmt_number(totals.get('fat_g'))}g｜碳水{_fmt_number(totals.get('carbohydrate_g'))}g",
            f"纖維{_fmt_number(totals.get('fiber_g'))}g｜鈉{_fmt_number(totals.get('sodium_mg'))}mg",
        ]
    )
    lines.extend(["", "資料截止：23:30；晚於此時間補記可輸入「今日健康日報」重新整理。"])
    return "\n".join(lines)[:5000]
