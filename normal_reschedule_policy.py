"""Order-owned normal reschedule window. No Sheet or fixture dependency."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta

from pair_reschedule_coordinator import PairRescheduleConflict


def ensure_normal_reschedule_anchors(conn: sqlite3.Connection) -> None:
    """Backfill eligible orders and install transaction-bound lifecycle capture."""
    capture = '''INSERT OR IGNORE INTO normal_reschedule_expiry_anchors
      (order_id,owner_user_id,original_expiry_date)
      SELECT o.id,o.user_id,e.expires_on FROM subscription_orders o
      JOIN subscription_menu_entitlements e ON e.order_id=o.id AND e.user_id=o.user_id
      WHERE o.status='activated' AND length(trim(o.formalized_at))>0
        AND NOT EXISTS (SELECT 1 FROM normal_reschedule_expiry_anchors a WHERE a.order_id=o.id)
        AND typeof(o.id)='integer' AND o.id>0
        AND typeof(o.user_id)='text' AND length(trim(o.user_id))>0
        AND typeof(o.meal_count)='integer' AND o.meal_count>0
        AND e.status='active' AND typeof(e.expires_on)='text'
        AND length(e.expires_on)=10
        AND e.expires_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(e.expires_on,'+0 days')=e.expires_on'''
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS normal_reschedule_expiry_anchors(
        order_id INTEGER PRIMARY KEY,owner_user_id TEXT NOT NULL,
        original_expiry_date TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
      CREATE TRIGGER IF NOT EXISTS normal_reschedule_anchor_no_update
        BEFORE UPDATE ON normal_reschedule_expiry_anchors
        BEGIN SELECT RAISE(ABORT,'reschedule expiry anchor is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS normal_reschedule_anchor_no_delete
        BEFORE DELETE ON normal_reschedule_expiry_anchors
        BEGIN SELECT RAISE(ABORT,'reschedule expiry anchor is immutable'); END;
    ''')
    # Either table may be written first. These triggers run inside the producer's
    # transaction, including redemption's entitlement UPSERT, so rollback removes
    # the anchor along with the activation or entitlement.
    for table, suffix, row_filter in (
        ('subscription_orders', 'order', 'o.id=NEW.id'),
        ('subscription_menu_entitlements', 'entitlement', 'e.user_id=NEW.user_id'),
    ):
        for operation in ('INSERT', 'UPDATE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS normal_reschedule_capture_{suffix}_{operation.lower()}
              AFTER {operation} ON {table}
              BEGIN {capture} AND {row_filter}; END''')
    conn.execute(capture)
    conn.commit()


def normal_order_expiry(conn: sqlite3.Connection, order_id: int, owner: str) -> str:
    order = conn.execute('''SELECT o.status,o.formalized_at,o.meal_count,
        e.status,e.expires_on,a.original_expiry_date
      FROM subscription_orders o
      JOIN subscription_menu_entitlements e ON e.order_id=o.id AND e.user_id=o.user_id
      JOIN normal_reschedule_expiry_anchors a ON a.order_id=o.id AND a.owner_user_id=o.user_id
      WHERE o.id=? AND o.user_id=?''', (order_id, owner)).fetchone()
    if not order or order[0] != 'activated' or not str(order[1] or '').strip() or not isinstance(order[2], int) or order[2] <= 0 or order[3] != 'active':
        raise PairRescheduleConflict('active formalized order entitlement is required')
    try:
        original = date.fromisoformat(str(order[5]))
        current = date.fromisoformat(str(order[4]))
    except ValueError as exc:
        raise PairRescheduleConflict('order entitlement date is invalid') from exc
    if original.isoformat() != order[5] or current.isoformat() != order[4] or current < original:
        raise PairRescheduleConflict('order entitlement no longer covers its original expiry')
    usage = conn.execute('''SELECT remaining_meals,last_date,expiry_date,status,
        typeof(remaining_meals) FROM usage WHERE user_id=?''', (owner,)).fetchone()
    if not usage or usage[4] != 'integer' or usage[0] <= 0 or usage[3] not in ('vip','active'):
        raise PairRescheduleConflict('valid remaining order usage is required')
    try:
        last = date.fromisoformat(str(usage[1]))
        end = date.fromisoformat(str(usage[2]))
    except ValueError as exc:
        raise PairRescheduleConflict('usage date range is invalid') from exc
    if last > end or end < original:
        raise PairRescheduleConflict('usage no longer covers the order expiry')
    return original.isoformat()


def normal_pair_policy(conn: sqlite3.Connection, order_id: int, owner: str,
                       source: str, target: str, now: datetime) -> tuple[str, str]:
    original = date.fromisoformat(normal_order_expiry(conn, order_id, owner))
    try:
        src, tgt = date.fromisoformat(source), date.fromisoformat(target)
        if src.isoformat() != source or tgt.isoformat() != target:
            raise ValueError
    except ValueError as exc:
        raise PairRescheduleConflict('reschedule date must be full ISO date') from exc
    if src == tgt or src < now.date() or tgt < max(now.date(), original) or tgt > original + timedelta(days=30):
        raise PairRescheduleConflict('reschedule dates outside original expiry + 30 window')
    if now.time() >= time(8) and (src == now.date() or tgt == now.date()):
        raise PairRescheduleConflict('same-day pair reschedule cutoff has passed')
    return source + ' 午餐／晚餐', target + ' 午餐／晚餐'


def normal_semantic_pending_request(conn: sqlite3.Connection, order_id: int,
                                    owner: str, source: str, target: str,
                                    now: datetime) -> dict | None:
    """Reuse one unresolved intent even if a client generates a fresh request ID."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='customer_pair_reschedule_requests'").fetchone():
        return None
    rows = conn.execute('''SELECT request_id,order_id,owner_user_id,source_date,target_date,
        status,created_at,expires_at,admin_notification_status
        FROM customer_pair_reschedule_requests
        WHERE order_id=? AND owner_user_id=? AND source_date=? AND target_date=?
          AND status IN ('pending_admin','sheet_unknown')
        ORDER BY created_at,request_id''', (order_id, owner, source, target)).fetchall()
    for row in rows:
        if row[5] == 'pending_admin':
            try:
                expiry = datetime.fromisoformat(str(row[7]))
            except ValueError:
                continue
            if expiry.tzinfo is None or expiry < now:
                continue
        return dict(zip(('request_id','order_id','owner_user_id','source_date',
            'target_date','status','created_at','expires_at','admin_notification_status'), row))
    return None
