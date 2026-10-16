"""每日「營養師提醒清單」：找出今天需要營養師留意的包月顧客。

Pure logic only (no LINE / Flask / server imports) so it is unit-testable.
server.py wires scheduling, sending and the admin command.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

# ---- Tunable thresholds -------------------------------------------------
LOW_INTAKE_RATIO = 0.60          # 吃太少 / 蛋白質不足：低於目標的 60%
LOW_INTAKE_DAYS = 3              # 連續天數（昨天往前 3 天，不含今天）
NO_LOG_DAYS = 3                  # 沒在記錄：連續天數
WEIGHT_WINDOW_DAYS = 7           # 體重觀察視窗
WEIGHT_DROP_RATIO = 0.01         # 視窗內掉超過 1%
DEDUP_DAYS = 3                   # 同顧客同規則 3 天內不重複提醒
LINE_TEXT_LIMIT = 5000

RULE_LOW_CAL = "low_cal"
RULE_LOW_PRO = "low_pro"
RULE_WEIGHT = "weight_drop"
RULE_NO_LOG = "no_log"

DDL = """CREATE TABLE IF NOT EXISTS dietitian_alerts_sent (
    user_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    sent_date TEXT NOT NULL
)"""

# (calories, protein, logged)  -- calories/protein None = unknown nutrition
DayTotals = tuple


@dataclass
class Alert:
    user_id: str
    name: str
    reasons: list = field(default_factory=list)  # [(rule, text)]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dietitian_alerts_sent "
        "ON dietitian_alerts_sent(user_id, rule, sent_date)"
    )


def default_daily_totals(conn: sqlite3.Connection, user_id: str, day: str) -> DayTotals:
    """Fallback intake: confirmed, non-deleted food_logs for one Taipei date."""
    rows = conn.execute(
        """SELECT nutrition_snapshot_json FROM food_logs
            WHERE user_id=? AND date(consumed_at,'+8 hours')=?
              AND confirmation_status='confirmed' AND COALESCE(deleted_at,'')=''""",
        (user_id, day),
    ).fetchall()
    cal = pro = 0.0
    cal_unknown = pro_unknown = False
    for (raw,) in rows:
        try:
            nut = json.loads(raw or "{}")
        except (TypeError, ValueError):
            nut = {}
        if not isinstance(nut, dict):
            nut = {}
        if nut.get("calories_kcal") is None:
            cal_unknown = True
        else:
            cal += float(nut["calories_kcal"])
        if nut.get("protein_g") is None:
            pro_unknown = True
        else:
            pro += float(nut["protein_g"])
    return (None if cal_unknown else cal, None if pro_unknown else pro, bool(rows))


def active_customers(conn: sqlite3.Connection, today: str) -> list:
    """Active 包月 entitlement joined to health_profile (same as auto_mark_due_planned_meals)."""
    return conn.execute(
        """SELECT DISTINCT e.user_id, COALESCE(h.name,''), h.tdee, h.protein
             FROM subscription_menu_entitlements e
             JOIN health_profile h ON h.user_id=e.user_id
            WHERE e.status='active' AND e.expires_on>=?
            ORDER BY e.user_id""",
        (today,),
    ).fetchall()


def _weight_drop(conn, user_id: str, today: date):
    start = (today - timedelta(days=WEIGHT_WINDOW_DAYS)).isoformat()
    try:
        rows = conn.execute(
            """SELECT report_date, weight_kg FROM daily_health_checkins
                WHERE user_id=? AND report_date>=? AND report_date<=?
                ORDER BY report_date""",
            (user_id, start, today.isoformat()),
        ).fetchall()
    except sqlite3.Error:
        return None
    rows = [r for r in rows if r[1] and float(r[1]) > 0]
    if len(rows) < 2:
        return None
    first, last = float(rows[0][1]), float(rows[-1][1])
    drop = (first - last) / first
    return drop if drop > WEIGHT_DROP_RATIO else None


def _recently_sent(conn, user_id: str, rule: str, today: date) -> bool:
    row = conn.execute(
        "SELECT MAX(sent_date) FROM dietitian_alerts_sent WHERE user_id=? AND rule=?",
        (user_id, rule),
    ).fetchone()
    if not row or not row[0]:
        return False
    try:
        last = date.fromisoformat(row[0])
    except ValueError:
        return False
    return (today - last).days <= DEDUP_DAYS


def compute_alerts(
    conn: sqlite3.Connection,
    today,
    *,
    daily_totals: Optional[Callable] = None,
    apply_dedup: bool = True,
) -> list:
    """Return [Alert] for active 包月 customers needing attention on ``today``."""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    daily_totals = daily_totals or default_daily_totals
    ensure_schema(conn)
    # 21:00 時今天可能還沒記完，所以只看已結束的日子（昨天往前算）。
    days = [(today - timedelta(days=i)).isoformat() for i in range(1, max(LOW_INTAKE_DAYS, NO_LOG_DAYS) + 1)]
    alerts = []
    for user_id, name, tdee, protein in active_customers(conn, today.isoformat()):
        totals = {d: daily_totals(conn, user_id, d) for d in days}
        reasons = []

        low = days[:LOW_INTAKE_DAYS]
        if tdee and float(tdee) > 0:
            vals = [totals[d][0] for d in low]
            if all(v is not None and v > 0 and v < LOW_INTAKE_RATIO * float(tdee) for v in vals):
                avg = sum(vals) / len(vals) / float(tdee) * 100
                reasons.append((RULE_LOW_CAL, f"吃太少（{LOW_INTAKE_DAYS} 天平均 {avg:.0f}%）"))
        if protein and float(protein) > 0:
            vals = [totals[d][1] for d in low]
            if all(v is not None and v > 0 and v < LOW_INTAKE_RATIO * float(protein) for v in vals):
                avg = sum(vals) / len(vals) / float(protein) * 100
                reasons.append((RULE_LOW_PRO, f"蛋白質不足（{LOW_INTAKE_DAYS} 天平均 {avg:.0f}%）"))
        drop = _weight_drop(conn, user_id, today)
        if drop is not None:
            reasons.append((RULE_WEIGHT, f"體重掉太快（{WEIGHT_WINDOW_DAYS} 天 -{drop * 100:.1f}%）"))
        if all(not totals[d][2] for d in days[:NO_LOG_DAYS]):
            reasons.append((RULE_NO_LOG, f"沒在記錄（連續 {NO_LOG_DAYS} 天）"))

        if apply_dedup:
            reasons = [r for r in reasons if not _recently_sent(conn, user_id, r[0], today)]
        if reasons:
            alerts.append(Alert(str(user_id), (name or "").strip() or "未命名顧客", reasons))
    return alerts


def record_sent(conn: sqlite3.Connection, alerts: list, today) -> None:
    if not isinstance(today, str):
        today = today.isoformat()
    ensure_schema(conn)
    conn.executemany(
        "INSERT INTO dietitian_alerts_sent (user_id, rule, sent_date) VALUES (?,?,?)",
        [(a.user_id, rule, today) for a in alerts for rule, _ in a.reasons],
    )
    conn.commit()


def format_message(alerts: list, *, limit: int = LINE_TEXT_LIMIT) -> Optional[str]:
    """One plain-text LINE message, or None when nobody needs attention."""
    if not alerts:
        return None
    ordered = sorted(alerts, key=lambda a: (-len(a.reasons), a.name))
    header = f"🩺 今日需留意（{len(ordered)} 位）"
    lines = [f"・{a.name}：{'、'.join(t for _, t in a.reasons)}" for a in ordered]
    kept = []
    used = len(header)
    for i, line in enumerate(lines):
        remaining = len(lines) - i
        footer = f"\n…另有 {remaining} 位"
        # keep room for a footer unless this is the last line and it fits outright
        reserve = 0 if remaining == 1 else len(footer)
        if used + 1 + len(line) + reserve > limit:
            break
        kept.append(line)
        used += 1 + len(line)
    text = "\n".join([header] + kept)
    skipped = len(lines) - len(kept)
    if skipped:
        text += f"\n…另有 {skipped} 位"
    return text[:limit]
