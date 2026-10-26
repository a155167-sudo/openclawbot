"""電腦營養健檢測試捷徑：挪窗口 + 補記 3 天午晚餐後，案件要進到待審。"""
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import server
from vip_health_check import (
    configure_vip_health_check_connection,
    create_first_vip_health_check_case,
    ensure_vip_health_check_schema,
)

TPE = ZoneInfo("Asia/Taipei")
UID = "U" + "c" * 32
NOW = datetime(2026, 10, 27, 21, 0, tzinfo=TPE)


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = str(tmp_path / "qa.db")
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.setattr(server, "APP_ENV", "staging")
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_ENABLED", True)
    monkeypatch.setattr(server, "VIP_HEALTH_CHECK_MAINTENANCE_ENABLED", True)
    monkeypatch.setattr(server, "tw_now", lambda: NOW)
    with closing(sqlite3.connect(path)) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS health_profile (user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, "
                     "protein REAL, goal TEXT, restrictions TEXT, active_days TEXT)")
        conn.execute("INSERT INTO health_profile VALUES (?,?,?,?,?,?,?)", (UID, '測試', 1800, 110, '減脂', '', ''))
        conn.commit()
    return path


def _make_case(path, *, activated_days_ago=10):
    with closing(sqlite3.connect(path)) as conn:
        configure_vip_health_check_connection(conn)
        create_first_vip_health_check_case(
            conn, user_id=UID, first_vip_activation_id="act1", activation_event_key="act1",
            activated_at=NOW - timedelta(days=activated_days_ago))
        conn.commit()


def _case(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT status,valid_day_count,window_started_at FROM vip_health_check_cases WHERE user_id=?",
            (UID,)).fetchone()


def test_seeds_three_days_and_case_becomes_ready(db):
    _make_case(db)  # 窗口早就過期也能挪回來
    text = server.run_staging_health_check_qa(UID)
    assert "待營養師審核" in text and "新增 6 筆" in text
    status, days, start = _case(db)
    assert status == "ready_for_review" and days == 3
    assert start.startswith("2026-10-24T00:00:00")
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_notifications WHERE notification_kind='dietitian_ready_for_review'"
        ).fetchone()[0] == 1


def test_rerun_does_not_duplicate_meals(db):
    _make_case(db)
    server.run_staging_health_check_qa(UID)
    text = server.run_staging_health_check_qa(UID)
    assert "新增 0 筆" in text
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs WHERE user_id=?", (UID,)).fetchone()[0] == 6


def test_no_case_tells_admin_to_activate_first(db):
    assert "還沒有健檢案件" in server.run_staging_health_check_qa(UID)


def test_production_refuses(db, monkeypatch):
    monkeypatch.setattr(server, "APP_ENV", "production")
    _make_case(db)
    assert "只能在電腦營養" in server.run_staging_health_check_qa(UID)
    assert _case(db)[0] == "collecting"


def test_bad_uid(db):
    assert "完整 UID" in server.run_staging_health_check_qa("U123")


def test_command_is_admin_only():
    assert server.is_admin_only_command("#健檢測試 " + UID)


def test_dietitian_detail_and_system_draft_work_on_seeded_case(db):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_initial_draft import assemble_system_data_summary
    _make_case(db)
    server.run_staging_health_check_qa(UID)
    with closing(sqlite3.connect(db)) as conn:
        configure_vip_health_check_connection(conn)
        case_id = conn.execute("SELECT case_id FROM vip_health_check_cases WHERE user_id=?", (UID,)).fetchone()[0]
        detail = load_health_check_detail(conn, case_id=case_id)
    summary = assemble_system_data_summary(detail)
    assert summary["kind"] == "system_data_summary_v1"
    assert summary["coverage"]["qualified_day_count"] == 3
    assert "熱量" in summary["editable_seed"]["good"]
