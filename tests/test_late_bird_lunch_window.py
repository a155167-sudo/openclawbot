"""晚鳥外送客戶的午餐餐期 13:00–15:30；自取與其他人維持 11:30–14:00."""
import sqlite3

import pytest

import server
from planned_meal_status import PHASE_AFTER, PHASE_ASKING, PHASE_BEFORE, meal_phase
from test_planned_meal_three_states import DAY, OWNER, _base, _card, at, srv  # noqa: F401


@pytest.mark.parametrize("clock,phase", [
    ((12, 59), PHASE_BEFORE), ((13, 0), PHASE_ASKING),
    ((15, 29), PHASE_ASKING), ((15, 30), PHASE_AFTER),
])
def test_late_bird_lunch_phase(clock, phase):
    assert meal_phase("午餐", at(*clock), late_bird=True) == phase


def test_late_bird_does_not_change_dinner_or_defaults():
    assert meal_phase("晚餐", at(17, 30), late_bird=True) == PHASE_ASKING
    assert meal_phase("晚餐", at(20, 30), late_bird=True) == PHASE_AFTER
    assert meal_phase("午餐", at(11, 30)) == PHASE_ASKING
    assert meal_phase("午餐", at(14, 0)) == PHASE_AFTER


def _set_delivery(db, count, dtype="晚鳥"):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE subscription_orders SET status='activated'")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(subscription_orders)")}
        if "delivery_type" not in cols:
            conn.execute("ALTER TABLE subscription_orders ADD COLUMN delivery_type TEXT DEFAULT ''")
        conn.execute("UPDATE subscription_orders SET delivery_type=?", (dtype,))
        if "delivery_count" not in cols:
            conn.execute("ALTER TABLE subscription_orders ADD COLUMN delivery_count INTEGER DEFAULT 0")
        conn.execute("UPDATE subscription_orders SET delivery_count=?", (count,))
        conn.commit()


def test_is_late_bird_customer(srv):
    db, _ = srv
    _set_delivery(db, 8)
    assert server.is_late_bird_customer(str(db), OWNER) is True
    _set_delivery(db, 0)
    assert server.is_late_bird_customer(str(db), OWNER) is False
    assert server.is_late_bird_customer("/nonexistent/x.db", OWNER) is False


@pytest.mark.parametrize("dtype", ["一般外送", "自取", ""])
def test_only_recorded_late_bird_gets_late_window(srv, dtype):
    db, _ = srv
    _set_delivery(db, 8, dtype=dtype)   # 一般外送 / 舊單(無類型) 都不是晚鳥
    assert server.is_late_bird_customer(str(db), OWNER) is False


def test_regular_delivery_keeps_default_lunch_window(srv):
    db, clock = srv
    _set_delivery(db, 8, dtype="一般外送")
    assert server.auto_mark_due_planned_meals(now=clock["now"]) == 1   # 14:05 已過 14:00


def test_job_waits_for_late_bird_lunch(srv):
    db, clock = srv
    _set_delivery(db, 8)
    assert server.auto_mark_due_planned_meals(now=clock["now"]) == 0   # 14:05
    clock["now"] = at(15, 35)
    assert server.auto_mark_due_planned_meals(now=clock["now"]) == 1


def test_job_still_marks_self_pickup_at_1405(srv):
    db, clock = srv
    _set_delivery(db, 0)
    assert server.auto_mark_due_planned_meals(now=clock["now"]) == 1


def test_dashboard_card_late_bird_before_delivery_has_no_ask():
    data = _base()
    data["late_bird"] = True
    adapted, text = _card(data, at(12, 30))
    assert adapted["late_bird"] is True
    assert "預留 400" in text and "吃了嗎" not in text
    _, text = _card(data, at(13, 0))
    assert "吃了嗎？" in text
    _, text = _card(_base(), at(12, 30))   # not late bird
    assert "吃了嗎？" in text
