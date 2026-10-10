import json
import sqlite3

import server
from customer_navigation import _meal_name_without_explicit_price, build_customer_home_contents


def _dashboard_from_sheet(tmp_path, monkeypatch, row):
    db_path = tmp_path / "quality.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    server.init_db()
    uid = "U-QUALITY-NUMERIC"
    today = server.tw_today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile(user_id,name,tdee,protein,today_date,sheet_name) VALUES (?,?,?,?,?,?)",
            (uid, "品質審", 1800, 100, today, "quality-sheet"),
        )
        conn.commit()

    class GC:
        def open_by_key(self, _key):
            return object()

    monkeypatch.setattr(server, "gc", GC())
    monkeypatch.setattr(
        server,
        "get_or_create_user_sheet",
        lambda *_args, **_kwargs: (object(), "quality-sheet", "exact"),
    )
    monkeypatch.setattr(server, "get_user_sheet_rows", lambda _sheet: [row])
    monkeypatch.setattr(server, "find_training_assignment_for_date", lambda *_args: None)
    return server.get_dashboard_data(uid)


def test_actual_writer_price_shape_and_conservative_boundaries():
    assert _meal_name_without_explicit_price("牛肉低碳 ($220)") == "牛肉低碳"
    assert _meal_name_without_explicit_price("雞胸 150g（NT$160）") == "雞胸 150g"
    assert _meal_name_without_explicit_price("雞胸 150g") == "雞胸 150g"
    assert _meal_name_without_explicit_price("雞胸 $160 加蛋") == "雞胸 $160 加蛋"
    assert _meal_name_without_explicit_price("套餐 220") == "套餐 220"


def test_sheet_decimal_and_negative_nutrition_must_not_be_truncated_or_sign_flipped(tmp_path, monkeypatch):
    today = server.tw_today().strftime("%Y/%m/%d")
    data = _dashboard_from_sheet(
        tmp_path,
        monkeypatch,
        {
            "實際日期": today,
            "午餐安排": "雞蛋豆腐 ($55)",
            "午餐熱量": "196.5",
            "午餐蛋白": "18.9",
            "晚餐安排": "異常資料 ($220)",
            "晚餐熱量": "-5",
            "晚餐蛋白": "-1.5",
            "今日排餐總熱量": "191.5",
            "今日排餐總蛋白": "17.4",
        },
    )
    observed = {key: data[key] for key in ("lunch_cal", "lunch_pro", "dinner_cal", "dinner_pro")}
    print("OBSERVED", json.dumps(observed, ensure_ascii=False, sort_keys=True))
    rendered = json.dumps(build_customer_home_contents(data), ensure_ascii=False)
    print("RENDERED_HAS", [text for text in ("196.5 kcal", "18.9 g", "熱量 未知｜蛋白質 未知") if text in rendered])

    assert data["lunch_cal"] == 196.5
    assert data["lunch_pro"] == 18.9
    assert data["dinner_cal"] is None
    assert data["dinner_pro"] is None


def test_sheet_nutrition_accepts_explicit_numbers_units_thousands_and_zero(tmp_path, monkeypatch):
    today = server.tw_today().strftime("%Y/%m/%d")
    data = _dashboard_from_sheet(
        tmp_path,
        monkeypatch,
        {
            "實際日期": today,
            "午餐安排": "合法格式午餐 ($220)",
            "午餐熱量": "1,234.5 kcal",
            "午餐蛋白": "+18.25g",
            "晚餐安排": "零值晚餐 ($0)",
            "晚餐熱量": 0,
            "晚餐蛋白": 0.0,
            "今日排餐總熱量": "1,234.5",
            "今日排餐總蛋白": "18.25 g",
        },
    )

    assert data["lunch_cal"] == 1234.5
    assert data["lunch_pro"] == 18.25
    assert data["dinner_cal"] == 0
    assert data["dinner_pro"] == 0
    assert data["planned_cal"] == 1234.5
    assert data["planned_pro"] == 18.25


def test_sheet_nutrition_rejects_arbitrary_text_prices_bool_and_nonfinite(tmp_path, monkeypatch):
    today = server.tw_today().strftime("%Y/%m/%d")
    bad_values = (
        "約 196.5 kcal", "$220", "NT$1,000", "196 kcal / $220",
        "NaN", "inf", float("nan"), float("inf"), True,
    )

    for index, bad in enumerate(bad_values):
        data = _dashboard_from_sheet(
            tmp_path / str(index),
            monkeypatch,
            {
                "實際日期": today,
                "午餐安排": "異常午餐 ($220)",
                "午餐熱量": bad,
                "午餐蛋白": bad,
                "晚餐安排": "異常晚餐 ($220)",
                "晚餐熱量": bad,
                "晚餐蛋白": bad,
                "今日排餐總熱量": bad,
                "今日排餐總蛋白": bad,
            },
        )
        assert data["lunch_cal"] is None
        assert data["lunch_pro"] is None
        assert data["dinner_cal"] is None
        assert data["dinner_pro"] is None
        assert data["planned_cal"] is None
        assert data["planned_pro"] is None


def test_missing_planned_total_only_falls_back_when_both_meals_are_known(tmp_path, monkeypatch):
    today = server.tw_today().strftime("%Y/%m/%d")
    known = _dashboard_from_sheet(
        tmp_path / "known",
        monkeypatch,
        {
            "實際日期": today,
            "午餐安排": "午餐 ($220)",
            "午餐熱量": "196.5",
            "午餐蛋白": "18.9",
            "晚餐安排": "晚餐 ($220)",
            "晚餐熱量": "203.25",
            "晚餐蛋白": "21.1",
        },
    )
    assert known["planned_cal"] == 399.75
    assert known["planned_pro"] == 40

    unknown = _dashboard_from_sheet(
        tmp_path / "unknown",
        monkeypatch,
        {
            "實際日期": today,
            "午餐安排": "午餐 ($220)",
            "午餐熱量": "196.5",
            "午餐蛋白": "18.9",
            "晚餐安排": "晚餐 ($220)",
            "晚餐熱量": "未知",
            "晚餐蛋白": "未知",
        },
    )
    assert unknown["planned_cal"] is None
    assert unknown["planned_pro"] is None
    assert unknown["extra_cal"] == 0
    assert unknown["extra_pro"] == 0
    assert unknown["cal_planned_segment"] is None
    assert unknown["pro_planned_segment"] is None


def test_invalid_explicit_planned_total_stays_unknown_instead_of_using_meal_sum(tmp_path, monkeypatch):
    today = server.tw_today().strftime("%Y/%m/%d")
    data = _dashboard_from_sheet(
        tmp_path,
        monkeypatch,
        {
            "實際日期": today,
            "午餐安排": "午餐 ($220)",
            "午餐熱量": "196.5",
            "午餐蛋白": "18.9",
            "晚餐安排": "晚餐 ($220)",
            "晚餐熱量": "203.25",
            "晚餐蛋白": "21.1",
            "今日排餐總熱量": "-399.75",
            "今日排餐總蛋白": "$40",
        },
    )

    assert data["planned_cal"] is None
    assert data["planned_pro"] is None
    assert data["cal_planned_segment"] is None
    assert data["pro_planned_segment"] is None
