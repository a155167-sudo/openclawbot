import asyncio
import os
import sqlite3
from datetime import date

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


class _FakeApiSheet:
    def __init__(self, records):
        self._records = records

    def get_all_records(self):
        return list(self._records)

    def row_values(self, _row):
        return []


class _FakeWorkbook:
    def __init__(self, api_sheet):
        self._api_sheet = api_sheet

    def worksheet(self, name):
        assert name == "Master_API_View"
        return self._api_sheet


class _FakeGoogleClient:
    def __init__(self, records):
        self._workbook = _FakeWorkbook(_FakeApiSheet(records))

    def open_by_url(self, _url):
        return self._workbook


def _install_auto_target_db(tmp_path, monkeypatch):
    db_path = tmp_path / "auto-targets.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE health_profile (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                today_extra_cal INTEGER DEFAULT 0,
                today_food_items TEXT DEFAULT '',
                tdee INTEGER DEFAULT 0,
                is_coaching_enabled INTEGER DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE subscription_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                formalized_at TEXT DEFAULT ''
            )"""
        )
        conn.executemany(
            "INSERT INTO health_profile VALUES (?, ?, 0, '', 2000, 1)",
            [
                ("U_OLD_MONTHLY", "舊包月"),
                ("U_MANUAL_COACH", "非包月教練"),
                ("U_PENDING_ONLY", "未付款待處理"),
                ("U_UID_GUARD", "UID 精確關聯"),
            ],
        )
        conn.executemany(
            "INSERT INTO subscription_orders (user_id, status, formalized_at) VALUES (?, ?, ?)",
            [
                ("U_OLD_MONTHLY", "activated", "2026-08-01 10:00:00"),
                ("U_PENDING_ONLY", "pending", ""),
                ("U_UID_GUARD_OTHER", "activated", "2026-08-01 10:00:00"),
            ],
        )

    fixed_today = date(2026, 9, 15)
    today = fixed_today.strftime("%Y/%m/%d")
    tomorrow = date(2026, 9, 16).strftime("%Y/%m/%d")
    records = []
    for uid in ("U_OLD_MONTHLY", "U_MANUAL_COACH", "U_PENDING_ONLY", "U_UID_GUARD"):
        records.extend(
            [
                {
                    "User_ID": uid,
                    "Date": today,
                    "Plan_Type": "運動員飲食" if uid == "U_MANUAL_COACH" else "一般飲食",
                    "Tomorrow_Workout": "手動跑步" if uid == "U_MANUAL_COACH" else "",
                    "Tomorrow_Intensity": "MED",
                    "Intervals_ID": "athlete-123" if uid == "U_MANUAL_COACH" else "",
                    "Intervals_API_Key": "icu-test-key" if uid == "U_MANUAL_COACH" else "",
                },
                {"User_ID": uid, "Date": tomorrow, "Today_Workout": "輕鬆跑"},
            ]
        )

    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "ADMIN_SECRET", "test-secret")
    monkeypatch.setattr(server, "tw_today", lambda: fixed_today)
    monkeypatch.setattr(server, "gc", _FakeGoogleClient(records))
    return db_path


def test_daily_auto_targets_exclude_only_uid_linked_activated_monthly_orders(
    tmp_path, monkeypatch
):
    _install_auto_target_db(tmp_path, monkeypatch)

    result = asyncio.run(server.get_lobster_targets("test-secret", mode="daily"))

    targets = {target["user_id"]: target for target in result["targets"]}
    assert set(targets) == {"U_MANUAL_COACH", "U_PENDING_ONLY", "U_UID_GUARD"}
    assert targets["U_MANUAL_COACH"]["tomorrow_preview"]["workout"] == "手動跑步"


def test_weekly_auto_targets_keep_non_monthly_garmin_coach(tmp_path, monkeypatch):
    _install_auto_target_db(tmp_path, monkeypatch)
    interval_calls = []
    monkeypatch.setattr(
        server,
        "get_intervals_data",
        lambda athlete_id, api_key: interval_calls.append((athlete_id, api_key)) or {"fitness": 42},
    )

    result = asyncio.run(server.get_lobster_targets("test-secret", mode="weekly"))

    targets = {target["user_id"]: target for target in result["targets"]}
    assert "U_OLD_MONTHLY" not in targets
    assert targets["U_MANUAL_COACH"]["intervals_icu"] == {"fitness": 42}
    assert interval_calls == [("athlete-123", "icu-test-key")]
