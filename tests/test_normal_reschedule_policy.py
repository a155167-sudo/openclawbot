import sqlite3
from datetime import datetime

import pytest

from normal_reschedule_policy import (ensure_normal_reschedule_anchors,
                                      normal_order_expiry, normal_pair_policy)

OWNER = 'U' + '1' * 32
OTHER = 'U' + '2' * 32
NOW = datetime.fromisoformat('2026-09-30T07:59:00+08:00')


def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE subscription_orders(id INTEGER PRIMARY KEY,user_id TEXT,status TEXT,meal_count INTEGER,formalized_at TEXT);
      CREATE TABLE subscription_menu_entitlements(user_id TEXT PRIMARY KEY,order_id INTEGER,status TEXT,expires_on TEXT);
      CREATE TABLE usage(user_id TEXT PRIMARY KEY,remaining_meals INTEGER,last_date TEXT,expiry_date TEXT,status TEXT);
      INSERT INTO subscription_orders VALUES(1,'U11111111111111111111111111111111','activated',20,'2026-09-25T12:00:00+08:00');
      INSERT INTO subscription_menu_entitlements VALUES('U11111111111111111111111111111111',1,'active','2026-10-26');
      INSERT INTO usage VALUES('U11111111111111111111111111111111',88,'2026-09-25','2026-10-26','vip');
    ''')
    return conn


def test_vip_deadline_is_order_owned_immutable_and_does_not_roll_with_generic_vip():
    conn = db()
    ensure_normal_reschedule_anchors(conn)
    assert normal_order_expiry(conn, 1, OWNER) == '2026-10-26'
    assert normal_pair_policy(conn, 1, OWNER, '2026-10-01', '2026-11-25', NOW)
    conn.execute("UPDATE usage SET expiry_date='2027-01-01' WHERE user_id=?", (OWNER,))
    conn.execute("UPDATE subscription_menu_entitlements SET expires_on='2027-01-01' WHERE order_id=1")
    assert normal_order_expiry(conn, 1, OWNER) == '2026-10-26'
    with pytest.raises(Exception):
        normal_pair_policy(conn, 1, OWNER, '2026-10-01', '2026-11-26', NOW)
    with pytest.raises(Exception):
        conn.execute("UPDATE normal_reschedule_expiry_anchors SET original_expiry_date='2027-01-01' WHERE order_id=1")
    with pytest.raises(Exception):
        normal_pair_policy(conn, 1, OTHER, '2026-10-01', '2026-11-25', NOW)


def test_expired_or_unentitled_vip_cannot_move():
    conn = db()
    ensure_normal_reschedule_anchors(conn)
    conn.execute("UPDATE subscription_menu_entitlements SET status='expired' WHERE order_id=1")
    with pytest.raises(Exception):
        normal_pair_policy(conn, 1, OWNER, '2026-10-01', '2026-10-26', NOW)
