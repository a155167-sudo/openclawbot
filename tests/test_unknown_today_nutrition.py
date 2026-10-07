import json
import sqlite3

import pytest

import server
from customer_navigation import build_customer_home_contents


def _texts(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            yield node.get("text")
        for value in node.values():
            yield from _texts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _texts(value)


def _setup_dashboard(tmp_path, monkeypatch, *, user_id, tdee=1800, protein_goal=100, logs=()):
    db_path = tmp_path / f"{user_id}.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile(user_id,name,tdee,protein,today_date) VALUES (?,?,?,?,?)",
            (user_id, "測試會員", tdee, protein_goal, today),
        )
        for index, nutrition in enumerate(logs):
            server.create_daily_food_log(
                conn,
                user_id=user_id,
                product_name=f"測試餐{index + 1}",
                meal_slot="午餐",
                consumed_at=f"{today}T12:{index:02d}:00+08:00",
                servings=1,
                nutrition=nutrition,
                source_type="manual",
                operation_key=f"{user_id}-{index}",
                publish_catalog=False,
            )
        conn.commit()
    data = server.get_dashboard_data(user_id)
    return data, list(_texts(build_customer_home_contents(data)))


def test_today_nutrition_unknown_is_tracked_per_field_through_real_renderer(tmp_path, monkeypatch):
    data, text = _setup_dashboard(
        tmp_path,
        monkeypatch,
        user_id="U-FIELD-SPLIT",
        logs=({"calories_kcal": None, "protein_g": 10},),
    )

    assert data["extra_cal"] is None
    assert data["extra_pro"] == 10
    # v52: the always-unknown fat row is hidden; unknown calories stay explicit.
    assert "未知 / 1,800 kcal" in text and "剩餘無法計算" in text
    assert any(item.startswith("10 / 100 g") for item in text)
    assert "剩 90 g" in text


def test_mixed_known_and_unknown_logs_do_not_claim_partial_sum_as_total(tmp_path, monkeypatch):
    data, text = _setup_dashboard(
        tmp_path,
        monkeypatch,
        user_id="U-MIXED",
        logs=(
            {"calories_kcal": 200, "protein_g": 5},
            {"protein_g": 7},
        ),
    )

    assert data["extra_cal"] is None
    assert data["extra_pro"] == 12
    assert "200" not in text
    assert "剩 1,600 kcal" not in text
    assert "剩餘無法計算" in text
    assert "剩 88 g" in text


@pytest.mark.parametrize(
    ("user_id", "logs"),
    [
        ("U-TRUE-ZERO", ({"calories_kcal": 0, "protein_g": 0},)),
        ("U-EMPTY", ()),
    ],
)
def test_true_zero_and_empty_day_remain_legal_zero(tmp_path, monkeypatch, user_id, logs):
    data, text = _setup_dashboard(
        tmp_path,
        monkeypatch,
        user_id=user_id,
        logs=logs,
    )

    assert data["extra_cal"] == 0
    assert data["extra_pro"] == 0
    assert sum(item.startswith("0 / ") for item in text) >= 2
    assert "剩 1,800 kcal" in text
    assert "剩 100 g" in text


def test_missing_goals_are_not_fabricated_and_remaining_is_not_precise(tmp_path, monkeypatch):
    data, text = _setup_dashboard(
        tmp_path,
        monkeypatch,
        user_id="U-NO-GOALS",
        tdee=None,
        protein_goal=None,
    )

    assert data["tdee"] is None
    assert data["protein_goal"] is None
    assert sum("尚未設定" in item for item in text) == 2
    assert sum(item.startswith("剩餘無法計算") for item in text) == 2
    assert "/ 0 kcal" not in text
    assert "/ 0 g" not in text
