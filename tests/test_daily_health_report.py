import sqlite3

import pytest

from daily_health_report import (
    claim_daily_delivery,
    ensure_daily_health_schema,
    finish_daily_delivery,
    format_daily_health_report,
    get_daily_health_checkin,
    parse_health_checkin,
    save_daily_health_checkin,
    summarize_intervals_activities,
)


def test_parse_complete_health_checkin():
    parsed = parse_health_checkin(
        "健康回報｜體重70.2｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好"
    )

    assert parsed == {
        "weight_kg": 70.2,
        "water_ml": 2500,
        "bowel_status": "有",
        "medication": "無",
        "sleep_start": "00:30",
        "sleep_end": "07:15",
        "sleep_quality": "良好",
    }


@pytest.mark.parametrize(
    "text, message",
    [
        ("健康回報｜體重10｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好", "體重"),
        ("健康回報｜體重70｜飲水10001｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好", "飲水"),
        ("健康回報｜體重70｜飲水2500｜排便也許｜用藥無｜睡眠00:30-07:15｜品質良好", "排便"),
        ("健康回報｜體重70｜飲水2500｜排便有｜用藥無｜睡眠25:30-07:15｜品質良好", "睡眠"),
        ("健康回報｜體重70｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質完美", "品質"),
    ],
)
def test_reject_invalid_health_checkin(text, message):
    with pytest.raises(ValueError, match=message):
        parse_health_checkin(text)


def test_daily_checkin_upsert_is_scoped_by_user_and_date():
    conn = sqlite3.connect(":memory:")
    ensure_daily_health_schema(conn)
    first = parse_health_checkin(
        "健康回報｜體重70.2｜飲水2500｜排便有｜用藥無｜睡眠00:30-07:15｜品質良好"
    )
    save_daily_health_checkin(
        conn, user_id="jason", report_date="2026-07-22", values=first,
        updated_at="2026-07-22T22:31:00+08:00",
    )
    second = dict(first, water_ml=3000)
    save_daily_health_checkin(
        conn, user_id="jason", report_date="2026-07-22", values=second,
        updated_at="2026-07-22T22:40:00+08:00",
    )

    assert get_daily_health_checkin(conn, user_id="jason", report_date="2026-07-22")["water_ml"] == 3000
    assert get_daily_health_checkin(conn, user_id="other", report_date="2026-07-22") is None


def test_daily_delivery_is_idempotent_after_success_and_retryable_after_failure():
    conn = sqlite3.connect(":memory:")
    ensure_daily_health_schema(conn)

    first_token = claim_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claimed_at="2026-07-22T23:30:00+08:00",
    )
    assert isinstance(first_token, str) and first_token
    assert claim_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claimed_at="2026-07-22T23:31:00+08:00",
    ) is None

    assert finish_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claim_token=first_token, sent=False,
        finished_at="2026-07-22T23:31:30+08:00", error="temporary",
    ) is True
    retry_token = claim_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claimed_at="2026-07-22T23:40:00+08:00",
    )
    assert isinstance(retry_token, str) and retry_token != first_token
    assert finish_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claim_token=retry_token, sent=True,
        finished_at="2026-07-22T23:40:30+08:00",
    ) is True
    assert claim_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claimed_at="2026-07-22T23:50:00+08:00",
    ) is None


def test_legacy_delivery_schema_adds_fencing_columns_without_losing_state():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE daily_health_deliveries (
            user_id TEXT NOT NULL, report_date TEXT NOT NULL,
            prompt_status TEXT NOT NULL DEFAULT 'pending',
            prompt_claimed_at TEXT NOT NULL DEFAULT '',
            prompt_sent_at TEXT NOT NULL DEFAULT '',
            prompt_attempts INTEGER NOT NULL DEFAULT 0,
            report_status TEXT NOT NULL DEFAULT 'pending',
            report_claimed_at TEXT NOT NULL DEFAULT '',
            report_sent_at TEXT NOT NULL DEFAULT '',
            report_attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, report_date)
        )"""
    )
    conn.execute(
        "INSERT INTO daily_health_deliveries "
        "(user_id,report_date,report_status,report_sent_at) VALUES (?,?,?,?)",
        ("jason", "2026-07-21", "sent", "2026-07-21T23:30:00+08:00"),
    )
    conn.commit()

    ensure_daily_health_schema(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(daily_health_deliveries)")
    }
    assert {"prompt_claim_token", "report_claim_token"} <= columns
    assert conn.execute(
        "SELECT report_status,report_sent_at FROM daily_health_deliveries"
    ).fetchone() == ("sent", "2026-07-21T23:30:00+08:00")


def test_stale_delivery_owner_cannot_overwrite_new_owner_result():
    conn = sqlite3.connect(":memory:")
    ensure_daily_health_schema(conn)
    old_token = claim_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claimed_at="2026-07-22T23:30:00+08:00",
    )
    new_token = claim_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claimed_at="2026-07-22T23:40:00+08:00",
    )
    assert old_token and new_token and old_token != new_token
    assert finish_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claim_token=new_token, sent=True, finished_at="2026-07-22T23:40:30+08:00",
    ) is True
    assert finish_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claim_token=old_token, sent=False, finished_at="2026-07-22T23:41:00+08:00",
        error="old worker failed late",
    ) is False
    row = conn.execute(
        "SELECT report_status,last_error FROM daily_health_deliveries"
    ).fetchone()
    assert row == ("sent", "")
    assert claim_daily_delivery(
        conn, user_id="jason", report_date="2026-07-22", kind="report",
        claimed_at="2026-07-22T23:50:00+08:00",
    ) is None


def test_format_complete_daily_health_report_with_plan_progress():
    report = format_daily_health_report(
        report_date="2026-07-22",
        checkin={
            "weight_kg": 70.2, "water_ml": 2500, "bowel_status": "有",
            "medication": "無", "sleep_start": "00:30", "sleep_end": "07:15",
            "sleep_quality": "良好",
        },
        foods=[
            {"time": "09:05", "name": "無糖豆漿", "calories_kcal": 228, "protein_g": 21.2},
            {"time": "13:10", "name": "舒肥雞胸餐", "calories_kcal": 520, "protein_g": 42},
        ],
        totals={
            "calories_kcal": 748, "protein_g": 63.2, "fat_g": 20,
            "carbohydrate_g": 72, "fiber_g": 9, "sodium_mg": 980,
            "starch_exchange": 4, "protein_low_exchange": 7,
            "protein_medium_exchange": 3, "protein_high_exchange": 0,
            "vegetable_exchange": 5, "fruit_exchange": 2, "milk_exchange": 1,
        },
        target={
            "starch_exchange": 6, "protein_total_exchange": 13,
            "vegetable_exchange": 5, "fruit_exchange": 2, "milk_exchange": 1,
        },
        exercise={
            "items": ["🏃跑步 10.0km／50min／均心率165bpm／消耗520kcal"],
            "total_calories": 520, "total_duration_min": 50, "hr_load": 79,
        },
        pending_reviews=1,
    )

    assert "📋 2026/07/22 一日健康日報" in report
    assert "體重：70.2 kg｜飲水：2500 ml" in report
    assert "睡眠：00:30–07:15（良好）" in report
    assert "🏃跑步 10.0km" in report
    assert "09:05 無糖豆漿｜228 kcal｜蛋白質21.2g" in report
    assert "熱量748 kcal｜蛋白質63.2g" in report
    assert "主食：4／6｜尚缺2" in report
    assert "蛋白質食物：10／13｜尚缺3" in report
    assert "蔬菜：5／5｜✅達標" in report
    assert "1筆交換份尚待審核" in report
    assert len(report) <= 5000


def test_format_report_enforces_line_message_limit_with_worst_case_food_names():
    report = format_daily_health_report(
        report_date="2026-07-22",
        checkin=None,
        foods=[
            {
                "time": "23:59",
                "name": f"第{index}筆" + ("超長食品名稱" * 100),
                "calories_kcal": 9999,
                "protein_g": 999,
            }
            for index in range(200)
        ],
        totals={}, target=None, exercise=None, pending_reviews=999,
    )

    assert len(report) <= 5000
    assert "第0筆" in report


def test_format_report_uses_na_without_guessing_missing_health_or_plan():
    report = format_daily_health_report(
        report_date="2026-07-22", checkin=None, foods=[], totals={}, target=None,
        exercise=None, pending_reviews=0,
    )

    assert "體重：NA｜飲水：NA" in report
    assert "今日無已確認飲食紀錄" in report
    assert "營養計畫：尚未建立或無法取得" in report
    assert "今日運動：NA" in report


def test_summarize_intervals_activities_uses_observed_activity_fields():
    summary = summarize_intervals_activities(
        [
            {
                "type": "Run", "icu_distance": 10000, "moving_time": 3000,
                "average_heartrate": 165, "calories": 520, "hr_load": 79,
            },
            {
                "type": "Swim", "icu_distance": 1500, "moving_time": 1800,
                "average_heartrate": 130, "calories": 280, "hr_load": 35,
            },
        ]
    )

    assert summary["total_calories"] == 800
    assert summary["total_duration_min"] == 80
    assert summary["hr_load"] == 114
    assert summary["items"] == [
        "🏃跑步 10km／50min／均心率165bpm／消耗520kcal",
        "🏊游泳 1.5km／30min／均心率130bpm／消耗280kcal",
    ]
