"""包月首批試跑：15 名、首期餐費 9 折、開始前週日 20:00 付款期限。"""
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime

import pytest

import first_batch_trial as fbt
import server


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.setenv("FIRST_BATCH_TRIAL_ENABLED", "true")
    monkeypatch.setenv("FIRST_BATCH_TRIAL_LIMIT", "3")
    with closing(sqlite3.connect(path)) as c:
        c.execute("""CREATE TABLE subscription_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, customer_name TEXT DEFAULT '',
            meal_count INTEGER DEFAULT 0, address TEXT DEFAULT '', distance_text TEXT DEFAULT '',
            delivery_fee INTEGER DEFAULT 0, delivery_count INTEGER DEFAULT 0,
            meal_low_total INTEGER DEFAULT 0, meal_high_total INTEGER DEFAULT 0,
            delivery_total INTEGER DEFAULT 0, quote_low_total INTEGER DEFAULT 0,
            quote_high_total INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
            admin_note TEXT DEFAULT '', created_at TEXT DEFAULT '', approved_at TEXT DEFAULT '',
            approved_by TEXT DEFAULT '', activated_at TEXT DEFAULT '', vip_code TEXT DEFAULT '',
            form_payload_json TEXT DEFAULT '', formalized_at TEXT DEFAULT '', delivery_type TEXT DEFAULT '')""")
        c.commit()
    pushed = []
    monkeypatch.setattr(server, "_first_batch_push", lambda uid, text: pushed.append((uid, text)) or True)
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "ADMIN")
    return path, pushed


def _snapshot(uid, total=3000, delivery_total=400, start="2026-10-26"):
    return {"user_id": uid, "name": uid, "active_days_list": ["一"] * 8, "total_price": total,
            "delivery_total_fee": delivery_total, "total_with_delivery": total + delivery_total,
            "delivery_fee_per_trip": 50, "delivery_days_count": 8, "is_delivery": True,
            "delivery_type": "一般外送", "address": "x", "start_date": start,
            "pickup_method": "外送", "delivery_available": True, "delivery_info": {"delivery_available": True}}


def _rows(path):
    with closing(sqlite3.connect(path)) as c:
        return c.execute("SELECT id,user_id,status,first_batch,first_batch_discount,meal_low_total,"
                         "quote_low_total,payment_deadline FROM subscription_orders ORDER BY id").fetchall()


# ---- 純規則 ----

def test_discount_only_meal_and_floor():
    assert fbt.meal_discount(3000) == 300
    assert fbt.meal_discount(2995) == 299  # 無條件捨去，對客人有利


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 21, 13, 0), date(2026, 10, 26)),   # 週三 → 下週一
    (datetime(2026, 10, 24, 15, 0), date(2026, 10, 26)),   # 週六 → 後天週一
    (datetime(2026, 10, 25, 19, 59), date(2026, 10, 26)),  # 週日 20:00 前 → 明天
    (datetime(2026, 10, 25, 20, 0), date(2026, 11, 2)),    # 週日 20:00 後 → 再下週一
    (datetime(2026, 10, 26, 9, 0), date(2026, 11, 2)),     # 週一 → 下週一
])
def test_start_date(now, expected):
    assert fbt.subscription_start_date(now) == expected


def test_deadline_is_sunday_20():
    assert fbt.payment_deadline_for(date(2026, 10, 26)) == datetime(2026, 10, 25, 20, 0)
    assert fbt.format_deadline("2026-10-25 20:00:00") == "10/25（日）20:00"


# ---- 下單佔名額與折扣 ----

def test_form_order_claims_seat_and_discounts_meal_only(db):
    path, _ = db
    snap = _snapshot("U1")
    oid = server.create_pending_subscription_form_order(snap)
    (row,) = _rows(path)
    assert row[3] == 1 and row[4] == 300
    assert row[5] == 2700            # 餐費已折
    assert row[6] == 2700 + 400       # 運費不折
    assert row[7] == "2026-10-25 20:00:00"
    assert snap["total_with_delivery"] == 3100
    stored = json.loads(sqlite3.connect(path).execute(
        "SELECT form_payload_json FROM subscription_orders WHERE id=?", (oid,)).fetchone()[0])
    assert stored["first_batch_discount"] == 300 and stored["total_price"] == 3000


def test_seats_full_then_original_price(db):
    path, _ = db
    for uid in ("U1", "U2", "U3", "U4"):
        server.create_pending_subscription_form_order(_snapshot(uid))
    rows = _rows(path)
    assert [r[3] for r in rows] == [1, 1, 1, 0]
    assert rows[3][5] == 3000 and rows[3][6] == 3400


def test_same_person_resubmitting_uses_one_seat(db):
    path, _ = db
    server.create_pending_subscription_form_order(_snapshot("U1"))
    server.create_pending_subscription_form_order(_snapshot("U1"))
    with closing(sqlite3.connect(path)) as c:
        assert fbt.seats_taken(c) == 1


def test_renewal_customer_not_eligible(db):
    path, _ = db
    with closing(sqlite3.connect(path)) as c:
        c.execute("INSERT INTO subscription_orders (user_id,status) VALUES ('OLD','activated')")
        c.commit()
    server.create_pending_subscription_form_order(_snapshot("OLD"))
    assert _rows(path)[-1][3] == 0


def test_rejected_and_expired_release_seat(db):
    path, _ = db
    for uid in ("U1", "U2", "U3"):
        server.create_pending_subscription_form_order(_snapshot(uid))
    with closing(sqlite3.connect(path)) as c:
        c.execute("UPDATE subscription_orders SET status='rejected' WHERE user_id='U1'")
        c.execute("UPDATE subscription_orders SET status='expired' WHERE user_id='U2'")
        c.commit()
        assert fbt.seats_taken(c) == 1
    server.create_pending_subscription_form_order(_snapshot("U4"))
    assert _rows(path)[-1][3] == 1


def test_disabled_flag_no_discount(db, monkeypatch):
    path, _ = db
    monkeypatch.setenv("FIRST_BATCH_TRIAL_ENABLED", "false")
    server.create_pending_subscription_form_order(_snapshot("U1"))
    row = _rows(path)[0]
    assert row[3] == 0 and row[5] == 3000 and row[7] == ""


def test_estimate_shows_discount_without_claiming(db):
    path, _ = db
    est = {"meal_low_total": 3000, "meal_high_total": 4000, "quote_low_total": 3400, "quote_high_total": 4400}
    with closing(sqlite3.connect(path)) as c:
        offer = fbt.offer_for(c, "U9")
    server.apply_first_batch_to_estimate(est, offer)
    assert est["meal_low_total"] == 2700 and est["quote_high_total"] == 4000
    assert "剩 3 名" in server.format_first_batch_estimate_line(est)
    assert _rows(path) == []


# ---- 付款期限 ----

def _approved_order(path, uid):
    oid = server.create_pending_subscription_form_order(_snapshot(uid))
    with closing(sqlite3.connect(path)) as c:
        c.execute("UPDATE subscription_orders SET status='approved' WHERE id=?", (oid,))
        c.commit()
    return oid


def test_sunday_noon_reminds_once_then_20_expires(db):
    path, pushed = db
    oid = _approved_order(path, "U1")
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 11, 50))
    assert pushed == []
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 12, 0))
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 12, 10))
    to_customer = [t for u, t in pushed if u == "U1"]
    assert len(to_customer) == 1 and "20:00" in to_customer[0]
    pushed.clear()
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 20, 0))
    assert _rows(path)[0][2] == "expired"
    assert any(u == "U1" and "釋出" in t for u, t in pushed)
    assert any(u == "ADMIN" and f"#{oid}" in t for u, t in pushed)
    pushed.clear()
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 20, 10))
    assert pushed == []


def test_activated_before_deadline_untouched(db):
    path, pushed = db
    oid = _approved_order(path, "U1")
    with closing(sqlite3.connect(path)) as c:
        c.execute("UPDATE subscription_orders SET status='activated' WHERE id=?", (oid,))
        c.commit()
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 21, 0))
    assert _rows(path)[0][2] == "activated" and pushed == []


def test_unapproved_order_kept_and_admin_told_once(db):
    path, pushed = db
    oid = server.create_pending_subscription_form_order(_snapshot("U1"))
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 12, 0))
    assert [u for u, _ in pushed] == ["ADMIN"]
    pushed.clear()
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 20, 0))
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 20, 10))
    assert _rows(path)[0][2] == "pending"
    assert len([1 for u, t in pushed if u == "ADMIN" and f"#{oid}" in t]) == 1


def test_regular_orders_never_expire(db, monkeypatch):
    path, pushed = db
    monkeypatch.setenv("FIRST_BATCH_TRIAL_ENABLED", "false")
    _approved_order(path, "U1")
    server.run_first_batch_payment_deadlines(now=datetime(2026, 10, 25, 21, 0))
    assert _rows(path)[0][2] == "approved" and pushed == []


def test_roster_text(db):
    path, _ = db
    server.create_pending_subscription_form_order(_snapshot("U1"))
    text = server.build_first_batch_roster_text()
    assert "已佔 1/3 名" in text and "待核准" in text and "10/25（日）20:00" in text


def test_admin_command_registered():
    assert server.is_admin_only_command("#首批名額")
