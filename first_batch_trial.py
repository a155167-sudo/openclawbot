"""包月首批試跑：限量名額、首期餐費 9 折、開始前週日 20:00 付款期限。

規則（Jason 2026-10-21 定案）：
- 名額預設 15 名（env FIRST_BATCH_TRIAL_LIMIT），以「人」計算，不以訂單數計算。
- 只有從沒開通過包月的新客人能拿首批名額；續約恢復原價。
- 送出包月表單、建立訂單時就佔名額；被拒絕或逾期未付款就自動釋放。
- 9 折只打餐費，不打運費。折扣金額無條件捨去到整數元（對客人有利）。
- 付款期限 = 開始日前一天（週日）20:00；當天 12:00 起提醒已核准未付款的客人，
  20:00 仍未開通就把訂單標為 expired，名額釋放。
- 管理員手動處理過的訂單（payment_deadline 清空）不再自動逾期。

本模組只處理 SQLite 與純計算；LINE 推播、訊息文字的組裝在 server.py。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, time, timedelta

DISCOUNT_PERCENT_OFF = 10           # 9 折
PAYMENT_DEADLINE_TIME = time(20, 0)  # 開始前一天 20:00
REMINDER_TIME = time(12, 0)          # 截止當天 12:00 起提醒
OPEN_STATUSES = ("pending", "approved", "activated")
UNPAID_STATUSES = ("pending", "approved")
EXPIRED_STATUS = "expired"
DEADLINE_FORMAT = "%Y-%m-%d %H:%M:%S"
WEEKDAY_LABELS = "一二三四五六日"

_COLUMNS = (
    ("first_batch", "INTEGER DEFAULT 0"),
    ("first_batch_discount", "INTEGER DEFAULT 0"),
    ("payment_deadline", "TEXT DEFAULT ''"),
    ("payment_reminded_at", "TEXT DEFAULT ''"),
    ("expired_at", "TEXT DEFAULT ''"),
)


def is_enabled() -> bool:
    return os.environ.get("FIRST_BATCH_TRIAL_ENABLED", "false").strip().lower() == "true"


def seat_limit() -> int:
    try:
        return max(0, int(os.environ.get("FIRST_BATCH_TRIAL_LIMIT", "15")))
    except ValueError:
        return 15


def ensure_schema(conn: sqlite3.Connection) -> None:
    """補上首批欄位；subscription_orders 不存在時什麼都不做。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscription_orders'"
    ).fetchone()
    if not exists:
        return
    have = {row[1] for row in conn.execute("PRAGMA table_info(subscription_orders)")}
    for name, ddl in _COLUMNS:
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE subscription_orders ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError:
                pass


def meal_discount(meal_total: int) -> int:
    meal_total = max(0, int(meal_total or 0))
    return meal_total * DISCOUNT_PERCENT_OFF // 100


def subscription_start_date(now: datetime) -> date:
    """包月開始日：下一個週一；週日 20:00（付款截止）之後下單排到再下一個週一。"""
    today = now.date()
    days_ahead = 7 - today.weekday()  # 週一→7、週日→1
    if today.weekday() == 6 and now.time() >= PAYMENT_DEADLINE_TIME:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def payment_deadline_for(start: date) -> datetime:
    return datetime.combine(start - timedelta(days=1), PAYMENT_DEADLINE_TIME)


def format_deadline(deadline_text: str) -> str:
    """'2026-10-25 20:00:00' → '10/25（日）20:00'；格式不對回空字串。"""
    try:
        dt = datetime.strptime(str(deadline_text or ""), DEADLINE_FORMAT)
    except ValueError:
        return ""
    return f"{dt.month}/{dt.day}（{WEEKDAY_LABELS[dt.weekday()]}）{dt:%H:%M}"


def _has_activated_order(conn, uid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM subscription_orders WHERE user_id=? AND status='activated' LIMIT 1",
        (uid,),
    ).fetchone() is not None


def _holds_open_seat(conn, uid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM subscription_orders WHERE user_id=? AND first_batch=1 "
        f"AND status IN ({','.join('?' * len(UNPAID_STATUSES))}) LIMIT 1",
        (uid, *UNPAID_STATUSES),
    ).fetchone() is not None


def seats_taken(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM subscription_orders WHERE first_batch=1 "
        f"AND status IN ({','.join('?' * len(OPEN_STATUSES))})",
        OPEN_STATUSES,
    ).fetchone()
    return int(row[0] or 0)


def offer_for(conn, uid: str) -> dict:
    """回傳這位客人現在看到的首批狀態（不佔名額）。

    {"enabled", "eligible", "applies", "seats_left", "limit", "holds_seat"}
    applies=True 代表此刻下單會拿到 9 折。
    """
    limit = seat_limit()
    result = {"enabled": is_enabled(), "eligible": False, "applies": False,
              "seats_left": 0, "limit": limit, "holds_seat": False}
    if not result["enabled"] or limit <= 0:
        return result
    ensure_schema(conn)
    taken = seats_taken(conn)
    result["seats_left"] = max(0, limit - taken)
    if not uid or _has_activated_order(conn, uid):
        return result
    result["eligible"] = True
    result["holds_seat"] = _holds_open_seat(conn, uid)
    result["applies"] = result["holds_seat"] or result["seats_left"] > 0
    return result


def claim_seat(conn, uid: str) -> bool:
    """在呼叫端已開啟的寫入交易（BEGIN IMMEDIATE）內判斷能否佔名額。

    回傳 True 代表新訂單應標 first_batch=1。名額以人計算：同一人重送表單不多佔。
    """
    offer = offer_for(conn, uid)
    return bool(offer["applies"])


def seat_rank(conn, order_id: int) -> int:
    """這筆訂單的客人是第幾位首批（依最早佔位時間）；不是首批回 0。"""
    row = conn.execute(
        "SELECT user_id, first_batch FROM subscription_orders WHERE id=?", (order_id,)
    ).fetchone()
    if not row or not row[1]:
        return 0
    users = [r[0] for r in conn.execute(
        "SELECT user_id, MIN(id) FROM subscription_orders WHERE first_batch=1 "
        f"AND status IN ({','.join('?' * len(OPEN_STATUSES))}) "
        "GROUP BY user_id ORDER BY MIN(id)",
        OPEN_STATUSES,
    )]
    return users.index(row[0]) + 1 if row[0] in users else 0


def order_info(conn, order_id: int) -> dict:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT first_batch, first_batch_discount, payment_deadline FROM subscription_orders WHERE id=?",
        (order_id,),
    ).fetchone()
    if not row:
        return {"first_batch": False, "discount": 0, "payment_deadline": ""}
    return {"first_batch": bool(row[0]), "discount": int(row[1] or 0),
            "payment_deadline": str(row[2] or "")}


def due_reminders(conn, now: datetime) -> list[dict]:
    """截止當天 12:00 之後、20:00 之前，還沒提醒過的首批未付款訂單。"""
    ensure_schema(conn)
    out = []
    for oid, uid, name, status, deadline_text in conn.execute(
        "SELECT id, user_id, customer_name, status, payment_deadline FROM subscription_orders "
        "WHERE first_batch=1 AND payment_deadline<>'' AND payment_reminded_at='' "
        f"AND status IN ({','.join('?' * len(UNPAID_STATUSES))}) ORDER BY id",
        UNPAID_STATUSES,
    ).fetchall():
        try:
            deadline = datetime.strptime(deadline_text, DEADLINE_FORMAT)
        except ValueError:
            continue
        remind_from = datetime.combine(deadline.date(), REMINDER_TIME)
        if remind_from <= now < deadline:
            out.append({"id": oid, "user_id": uid, "name": name or "", "status": status,
                        "payment_deadline": deadline_text})
    return out


def mark_reminded(conn, order_id: int, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE subscription_orders SET payment_reminded_at=? WHERE id=? AND payment_reminded_at=''",
        (now.strftime(DEADLINE_FORMAT), order_id),
    )
    return cur.rowcount == 1


def overdue(conn, now: datetime) -> list[dict]:
    """已過付款期限、仍未開通的首批訂單。"""
    ensure_schema(conn)
    out = []
    for oid, uid, name, status, deadline_text in conn.execute(
        "SELECT id, user_id, customer_name, status, payment_deadline FROM subscription_orders "
        "WHERE first_batch=1 AND payment_deadline<>'' "
        f"AND status IN ({','.join('?' * len(UNPAID_STATUSES))}) ORDER BY id",
        UNPAID_STATUSES,
    ).fetchall():
        try:
            deadline = datetime.strptime(deadline_text, DEADLINE_FORMAT)
        except ValueError:
            continue
        if now >= deadline:
            out.append({"id": oid, "user_id": uid, "name": name or "", "status": status,
                        "payment_deadline": deadline_text})
    return out


def expire(conn, order_id: int, old_status: str, now: datetime) -> bool:
    """compare-and-set：只有狀態沒被管理員改過才逾期。"""
    cur = conn.execute(
        "UPDATE subscription_orders SET status=?, expired_at=?, "
        "admin_note=CASE WHEN COALESCE(admin_note,'')='' THEN ? ELSE admin_note END "
        "WHERE id=? AND status=?",
        (EXPIRED_STATUS, now.strftime(DEADLINE_FORMAT), "首批付款期限已過，自動釋放名額",
         order_id, old_status),
    )
    return cur.rowcount == 1


def roster(conn) -> list[dict]:
    """#首批名額 用：所有佔名額的首批訂單（含已開通），依佔位順序。"""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT id, user_id, customer_name, status, first_batch_discount, payment_deadline, created_at "
        "FROM subscription_orders WHERE first_batch=1 "
        f"AND status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY id",
        OPEN_STATUSES,
    ).fetchall()
    return [{"id": r[0], "user_id": r[1], "name": r[2] or "", "status": r[3],
             "discount": int(r[4] or 0), "payment_deadline": r[5] or "", "created_at": r[6] or ""}
            for r in rows]


def public_status(conn) -> dict:
    """公開頁面用：只回開放狀態與名額數字，不含任何客人資料。"""
    limit = seat_limit()
    if not is_enabled() or limit <= 0:
        return {"enabled": False, "limit": limit, "taken": 0, "left": 0}
    ensure_schema(conn)
    taken = min(limit, seats_taken(conn))
    return {"enabled": True, "limit": limit, "taken": taken, "left": max(0, limit - taken)}
