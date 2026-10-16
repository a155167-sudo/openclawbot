import json
import sqlite3
from datetime import date, timedelta

import pytest

import dietitian_alerts as da

TODAY = date(2026, 10, 16)


def d(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE subscription_menu_entitlements (user_id TEXT, status TEXT, expires_on TEXT);
        CREATE TABLE health_profile (user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL);
        CREATE TABLE food_logs (log_id INTEGER PRIMARY KEY, user_id TEXT, consumed_at TEXT,
            confirmation_status TEXT, deleted_at TEXT, nutrition_snapshot_json TEXT);
        CREATE TABLE daily_health_checkins (user_id TEXT, report_date TEXT, weight_kg REAL);
        """
    )
    yield c
    c.close()


def add_user(c, uid="u1", name="王小明", tdee=2000, protein=100, status="active", expires="2026-12-31"):
    c.execute("INSERT INTO subscription_menu_entitlements VALUES (?,?,?)", (uid, status, expires))
    c.execute("INSERT INTO health_profile VALUES (?,?,?,?)", (uid, name, tdee, protein))


def log(c, uid, day, cal, pro, deleted=""):
    # 12:00 Taipei == 04:00Z
    c.execute(
        "INSERT INTO food_logs (user_id, consumed_at, confirmation_status, deleted_at, nutrition_snapshot_json)"
        " VALUES (?,?,?,?,?)",
        (uid, f"{day}T04:00:00+00:00", "confirmed", deleted,
         json.dumps({"calories_kcal": cal, "protein_g": pro})),
    )


def fill(c, uid, cals, pros=None):
    """cals[i] for day offset i+1 (0=yesterday; alerts ignore the unfinished today)."""
    for i, cal in enumerate(cals):
        if cal is not None:
            log(c, uid, d(i + 1), cal, (pros[i] if pros else 100))


def weigh(c, uid, offset, kg):
    c.execute("INSERT INTO daily_health_checkins VALUES (?,?,?)", (uid, d(offset), kg))


def rules(alerts, uid="u1"):
    for a in alerts:
        if a.user_id == uid:
            return {r for r, _ in a.reasons}
    return set()


def test_low_cal_boundary(conn):
    add_user(conn)
    fill(conn, "u1", [1180, 1180, 1180])  # 59%
    assert da.RULE_LOW_CAL in rules(da.compute_alerts(conn, TODAY))
    conn.execute("DELETE FROM food_logs")
    fill(conn, "u1", [1220, 1220, 1220])  # 61%
    assert da.RULE_LOW_CAL not in rules(da.compute_alerts(conn, TODAY))


def test_low_cal_needs_all_three_days(conn):
    add_user(conn)
    fill(conn, "u1", [1000, 1000, 1800])
    assert da.RULE_LOW_CAL not in rules(da.compute_alerts(conn, TODAY))


def test_low_cal_zero_log_day_excluded_but_counts_for_no_log_only(conn):
    add_user(conn)
    fill(conn, "u1", [1000, 1000, None])
    r = rules(da.compute_alerts(conn, TODAY))
    assert da.RULE_LOW_CAL not in r and da.RULE_NO_LOG not in r
    conn.execute("DELETE FROM food_logs")
    assert rules(da.compute_alerts(conn, TODAY)) == {da.RULE_NO_LOG}


def test_no_log_two_vs_three_days(conn):
    add_user(conn)
    fill(conn, "u1", [None, None, 2000])
    assert da.RULE_NO_LOG not in rules(da.compute_alerts(conn, TODAY))
    conn.execute("DELETE FROM food_logs")
    fill(conn, "u1", [None, None, None, 2000])
    assert da.RULE_NO_LOG in rules(da.compute_alerts(conn, TODAY))


def test_deleted_logs_ignored(conn):
    add_user(conn)
    for i in range(3):
        log(conn, "u1", d(i), 2000, 100, deleted="2026-10-16T00:00:00")
    assert da.RULE_NO_LOG in rules(da.compute_alerts(conn, TODAY))


def test_protein_boundary(conn):
    add_user(conn)
    fill(conn, "u1", [2000] * 3, [59, 59, 59])
    assert rules(da.compute_alerts(conn, TODAY)) == {da.RULE_LOW_PRO}
    conn.execute("DELETE FROM food_logs")
    fill(conn, "u1", [2000] * 3, [61, 61, 61])
    assert rules(da.compute_alerts(conn, TODAY)) == set()


def test_weight_drop_boundaries(conn):
    add_user(conn)
    fill(conn, "u1", [2000] * 3)
    weigh(conn, "u1", 6, 100.0)
    weigh(conn, "u1", 0, 99.1)  # 0.9%
    assert da.RULE_WEIGHT not in rules(da.compute_alerts(conn, TODAY))
    conn.execute("DELETE FROM daily_health_checkins")
    weigh(conn, "u1", 6, 100.0)
    weigh(conn, "u1", 0, 98.9)  # 1.1%
    assert da.RULE_WEIGHT in rules(da.compute_alerts(conn, TODAY))


def test_weight_single_weighin_and_old_weighin(conn):
    add_user(conn)
    fill(conn, "u1", [2000] * 3)
    weigh(conn, "u1", 0, 90.0)
    assert da.RULE_WEIGHT not in rules(da.compute_alerts(conn, TODAY))
    weigh(conn, "u1", 10, 100.0)  # outside 7-day window
    assert da.RULE_WEIGHT not in rules(da.compute_alerts(conn, TODAY))


def test_inactive_customers_ignored(conn):
    add_user(conn, "u1", status="expired")
    add_user(conn, "u2", expires="2026-10-15")
    add_user(conn, "u3")
    assert [a.user_id for a in da.compute_alerts(conn, TODAY)] == ["u3"]


def test_missing_target_skips_intake_rules(conn):
    add_user(conn, tdee=None, protein=None)
    fill(conn, "u1", [100] * 3, [1] * 3)
    assert rules(da.compute_alerts(conn, TODAY)) == set()


def test_dedup_three_days_then_realert(conn):
    add_user(conn)
    alerts = da.compute_alerts(conn, TODAY)
    assert rules(alerts) == {da.RULE_NO_LOG}
    da.record_sent(conn, alerts, TODAY)
    for k in (1, 2, 3):
        assert da.compute_alerts(conn, TODAY + timedelta(days=k)) == []
    assert rules(da.compute_alerts(conn, TODAY + timedelta(days=4))) == {da.RULE_NO_LOG}
    # admin test run ignores dedup
    assert rules(da.compute_alerts(conn, TODAY, apply_dedup=False)) == {da.RULE_NO_LOG}


def test_dedup_is_per_rule(conn):
    add_user(conn)
    da.record_sent(conn, [da.Alert("u1", "x", [(da.RULE_NO_LOG, "")])], TODAY)
    fill(conn, "u1", [1000] * 3)
    assert rules(da.compute_alerts(conn, TODAY)) == {da.RULE_LOW_CAL}


def test_format_none_when_empty():
    assert da.format_message([]) is None


def test_format_message_sample():
    a = da.Alert("u1", "王小明", [(da.RULE_LOW_CAL, "吃太少（3 天平均 52%）"), (da.RULE_NO_LOG, "沒在記錄（連續 3 天）")])
    b = da.Alert("u2", "李華", [(da.RULE_WEIGHT, "體重掉太快（7 天 -1.4%）")])
    assert da.format_message([b, a]) == (
        "🩺 今日需留意（2 位）\n"
        "・王小明：吃太少（3 天平均 52%）、沒在記錄（連續 3 天）\n"
        "・李華：體重掉太快（7 天 -1.4%）"
    )


def test_format_truncation():
    alerts = [da.Alert(f"u{i}", f"顧客{i:04d}", [(da.RULE_NO_LOG, "沒在記錄（連續 3 天）")]) for i in range(1000)]
    text = da.format_message(alerts)
    assert len(text) <= 5000
    assert text.startswith("🩺 今日需留意（1000 位）")
    shown = text.count("・")
    assert text.endswith(f"…另有 {1000 - shown} 位") and shown < 1000


def test_unfinished_today_is_ignored(conn):
    add_user(conn)
    log(conn, "u1", d(0), 500, 10)          # only today logged, very low
    r = rules(da.compute_alerts(conn, TODAY))
    assert da.RULE_LOW_CAL not in r and da.RULE_NO_LOG in r  # yesterday-back 3 days are empty
