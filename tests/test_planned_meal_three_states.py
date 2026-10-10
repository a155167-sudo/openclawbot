"""包月餐三狀態：待吃 → 吃了嗎 → 已吃（餐期過後自動）；當天可改「沒吃」."""
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import server
from dashboard_balance_adapter import adapt_dashboard_data
from dashboard_flex import build_dashboard_flex, compute
from planned_meal_status import (
    PHASE_AFTER, PHASE_ASKING, PHASE_BEFORE, can_skip, meal_phase,
    parse_skip_postback, skip_postback_data,
)
from test_normal_reschedule_integration import normal_db
from test_pair_reschedule_coordinator import OWNER

TPE = ZoneInfo("Asia/Taipei")
DAY = "2026-09-27"  # the fixture's published service date


def at(hh, mm, day=DAY):
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=TPE)


@pytest.mark.parametrize("slot,clock,phase", [
    ("午餐", (11, 29), PHASE_BEFORE), ("午餐", (11, 30), PHASE_ASKING),
    ("午餐", (13, 59), PHASE_ASKING), ("午餐", (14, 0), PHASE_AFTER),
    ("晚餐", (17, 29), PHASE_BEFORE), ("晚餐", (17, 30), PHASE_ASKING),
    ("晚餐", (20, 29), PHASE_ASKING), ("晚餐", (20, 30), PHASE_AFTER),
])
def test_meal_phases(slot, clock, phase):
    assert meal_phase(slot, at(*clock)) == phase


def test_skip_only_today_and_postback_round_trip():
    assert can_skip(DAY, at(23, 59)) and not can_skip(DAY, at(0, 0, "2026-09-28"))
    data = skip_postback_data("晚餐", DAY)
    assert parse_skip_postback(data) == ("晚餐", DAY)
    assert parse_skip_postback("pm:v1:skip:早餐:2026-09-27") is None
    assert parse_skip_postback("pm:v1:skip:午餐:bad") is None


def _card(data, now):
    adapted = adapt_dashboard_data(data)
    return adapted, json.dumps(build_dashboard_flex(adapted, now=now), ensure_ascii=False)


def _base(**sub):
    meal = {"slot": "午餐", "name": "雞肉低碳", "kcal": 400, "protein": 40,
            "meal_date": DAY, "subscription_meal_id": "d1:午餐"}
    meal.update(sub)
    return {"name": "Jq", "tdee": 2000, "protein_goal": 120,
            "balance_records": [], "balance_sub_meals": [meal]}


def test_before_window_shows_reserved_without_buttons():
    _, text = _card(_base(), at(10, 0))
    assert "預留 400" in text and "吃了嗎" not in text and "沒吃" not in text


def test_asking_window_offers_eaten_button():
    _, text = _card(_base(), at(12, 0))
    assert "吃了嗎？" in text and '"text": "午餐已吃"' in text


def test_eaten_planned_record_offers_skip_postback():
    data = _base(eaten=True)
    data["balance_records"] = [{"slot": "午餐", "name": "雞肉低碳", "kcal": 400, "protein": 40,
                                "source_type": "planned_meal", "subscription_meal_id": "d1:午餐"}]
    adapted, text = _card(data, at(15, 0))
    assert adapted["sub_meals"][0]["eaten"] is True
    assert skip_postback_data("午餐", DAY) in text
    c = compute(adapted)
    assert c["ek"] == 400 and c["has_sub"] is False  # counted once, as eaten


def test_skipped_meal_is_neither_eaten_nor_reserved_and_can_be_undone():
    adapted, text = _card(_base(skipped=True), at(15, 0))
    c = compute(adapted)
    assert c["has_sub"] is False and (c["rk"] in (None, 0))
    assert "沒吃" in text and "改回已吃" in text and '"text": "午餐已吃"' in text


# --- server integration: background auto-已吃 and 「沒吃」 -------------------

@pytest.fixture
def srv(tmp_path, monkeypatch):
    db = tmp_path / "three-states.sqlite3"
    conn = normal_db(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS health_profile (
            user_id TEXT PRIMARY KEY, today_extra_cal REAL, today_extra_pro REAL,
            today_food_items TEXT, today_date TEXT, tdee REAL, protein REAL);
        CREATE TABLE IF NOT EXISTS planned_meal_checks (
            user_id TEXT, meal_date TEXT, meal_slot TEXT, meal_name TEXT,
            cal REAL, pro REAL, checked_at TEXT,
            PRIMARY KEY (user_id, meal_date, meal_slot));
        CREATE TABLE IF NOT EXISTS recent_meal_logs (
            user_id TEXT PRIMARY KEY, meal_name TEXT, base_cal REAL, base_pro REAL,
            current_cal REAL, current_pro REAL, meal_date TEXT,
            source_text TEXT, updated_at TEXT, food_log_id TEXT);
    """)
    conn.execute("INSERT OR REPLACE INTO health_profile VALUES (?,0,0,'',?,2000,120)", (OWNER, DAY))
    conn.commit()
    conn.close()
    clock = {"now": at(14, 5)}
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "tw_now", lambda: clock["now"])
    monkeypatch.setattr(server, "tw_today", lambda: clock["now"].date())
    return db, clock


def _logs(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            """SELECT meal_slot, operation_key, COALESCE(deleted_at,'')='' FROM food_logs
               WHERE user_id=? ORDER BY created_at""", (OWNER,)).fetchall()


def test_job_writes_only_due_meals_once_with_dispatch_key(srv):
    db, clock = srv
    assert server.auto_mark_due_planned_meals(now=clock["now"]) == 1   # 14:05 → lunch only
    assert server.auto_mark_due_planned_meals(now=clock["now"]) == 0   # idempotent
    assert _logs(db) == [("午餐", "planned-meal:dispatch-old:午餐", 1)]
    clock["now"] = at(20, 35)
    assert server.auto_mark_due_planned_meals(now=clock["now"]) == 1   # dinner
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT today_extra_cal FROM health_profile").fetchone()[0] == 1100


def test_skip_removes_auto_meal_and_job_never_readds(srv):
    db, clock = srv
    server.auto_mark_due_planned_meals(now=clock["now"])
    ok, _ = server.mark_planned_meal_skipped(OWNER, "午餐", DAY, now=clock["now"])
    assert ok
    assert [row[2] for row in _logs(db)] == [0]
    assert server.auto_mark_due_planned_meals(now=at(14, 30)) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT today_extra_cal FROM health_profile").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM planned_meal_checks").fetchone()[0] == 0


def test_eaten_again_after_skip_counts_once(srv):
    db, clock = srv
    server.auto_mark_due_planned_meals(now=clock["now"])
    server.mark_planned_meal_skipped(OWNER, "午餐", DAY, now=clock["now"])
    ok, _ = server._record_planned_meal_eaten(
        OWNER, "午餐", "午餐A", 500, 30, "dispatch-old:午餐", source_text="午餐已吃")
    assert ok
    live = [row for row in _logs(db) if row[2]]
    assert len(live) == 1 and live[0][1].startswith("planned-meal:dispatch-old:午餐:after-skip:")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM planned_meal_skips").fetchone()[0] == 0


def test_skip_refused_for_previous_day(srv):
    _, clock = srv
    ok, text = server.mark_planned_meal_skipped(OWNER, "午餐", "2026-09-26", now=clock["now"])
    assert not ok and "今天" in text
