import asyncio
import json
import sqlite3
from types import SimpleNamespace
from typing import Any, cast

import pytest

import fixed_meal_picks as fmp
from tests.test_subscription_form_uid_handler import (
    ACTUAL_UID_TITLE,
    VALID_UID,
    _dish,
    _install_isolated_form_dependencies,
    _request,
)


def _plan(days, a, b):
    return [(1, d, "第1週", f"週{d}", a, b) for d in range(1, days + 1)]


def test_parse_names_splits_dedupes_and_ignores_none():
    assert fmp.parse_fixed_pick_names("雞肉低碳, 豬肉便當，雞肉低碳、 無") == ["雞肉低碳", "豬肉便當"]
    assert fmp.parse_fixed_pick_names("") == []
    assert fmp.parse_fixed_pick_names(None) == []


def test_resolve_separates_unsafe_and_unknown():
    a, b, c = _dish("雞肉低碳", 185), _dish("牛肉便當", 195), _dish("豬肉沙拉", 200)
    usable, unsafe, unknown = fmp.resolve_fixed_picks(
        ["雞肉低碳", "牛肉便當", "龍蝦便當"], safe_menu=[a, c], all_menu=[a, b, c]
    )
    assert [d["name"] for d in usable] == ["雞肉低碳"]
    assert unsafe == ["牛肉便當"]
    assert unknown == ["龍蝦便當"]


def test_three_picks_rotate_and_never_repeat_same_day():
    picks = [_dish(n, 180) for n in ("A", "B", "C")]
    x, y = _dish("X", 150), _dish("Y", 150)
    out = fmp.apply_fixed_picks(_plan(6, x, y), picks)
    pairs = [(row[4]["name"], row[5]["name"]) for row in out]
    assert pairs == [("A", "B"), ("C", "A"), ("B", "C"), ("A", "B"), ("C", "A"), ("B", "C")]
    assert all(lunch != dinner for lunch, dinner in pairs)


def test_single_pick_is_always_lunch_and_dinner_is_auto_and_different():
    only = _dish("雞肉低碳", 185)
    x = _dish("鮭魚便當", 195)
    plan = [(1, 1, "第1週", "週一", x, only), (1, 2, "第1週", "週二", only, x)]
    out = fmp.apply_fixed_picks(plan, [only])
    assert [(r[4]["name"], r[5]["name"]) for r in out] == [("雞肉低碳", "鮭魚便當")] * 2


def test_no_picks_keeps_plan():
    plan = _plan(2, _dish("X", 1), _dish("Y", 1))
    assert fmp.apply_fixed_picks(plan, []) == plan


def test_messages_only_when_customer_answered():
    assert fmp.admin_line([], [], []) == ""
    assert fmp.customer_lines([], [], []) == ""
    one = [_dish("雞肉低碳", 185)]
    assert "每天午餐" in fmp.customer_lines(one, [], [])
    assert "輪流排" in fmp.customer_lines(one + [_dish("豬肉便當", 175)], [], [])
    assert "已略過" in fmp.customer_lines(one, ["牛肉便當"], [])
    assert "自動配餐" in fmp.admin_line([], ["牛肉便當"], [])


def _run_form(tmp_path, monkeypatch, extra):
    db_path, pushes = _install_isolated_form_dependencies(tmp_path, monkeypatch)
    monkeypatch.setattr(
        __import__("server"),
        "MAIN_DISHES",
        [_dish("雞肉便當", 180), _dish("豬肉低碳", 190), _dish("雞肉食蔬", 200), _dish("牛肉沙拉", 220)],
    )
    payload = {
        ACTUAL_UID_TITLE: VALID_UID,
        "您的稱呼 (姓名或暱稱)": "固定吃客人",
        "本期取餐方式": "自取",
        "第一週想取餐的日期": ["週一", "週三"],
        "第二週想取餐的日期": ["週二"],
        "第三週想取餐的日期": ["週三"],
        "第四週想取餐的日期": ["週四"],
        "您的主食選擇（可複選）": ["都不挑食"],
        "您最喜歡的蛋白質是？（可複選）": ["雞肉", "豬肉"],
        **extra,
    }
    import server

    result = asyncio.run(server.receive_form_data(_request(payload), cast(Any, SimpleNamespace())))
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT form_payload_json FROM subscription_orders WHERE id=?", (result["order_id"],)
        ).fetchone()[0]
    return json.loads(raw), pushes


def test_form_single_pick_fixes_lunch_and_prices_follow(tmp_path, monkeypatch):
    snapshot, pushes = _run_form(
        tmp_path, monkeypatch, {"想固定吃的餐點（選填，可複選）": ["豬肉低碳"]}
    )
    rows = snapshot["master_api_rows"]
    assert len(rows) == 5
    assert all(row[3] == "豬肉低碳" and row[4] != "豬肉低碳" for row in rows)
    prices = {"雞肉便當": 180, "豬肉低碳": 190, "雞肉食蔬": 200, "牛肉沙拉": 220}
    assert snapshot["total_price"] == sum(prices[r[3]] + prices[r[4]] for r in rows)
    assert snapshot["fixed_picks"] == ["豬肉低碳"]
    assert "指定品項：豬肉低碳" in snapshot["fixed_picks_admin_line"]
    assert any("每天午餐" in text for _uid, text in pushes)


def test_form_two_picks_only_those_dishes(tmp_path, monkeypatch):
    snapshot, _ = _run_form(
        tmp_path, monkeypatch, {"想固定吃的餐點（選填，可複選）": ["雞肉便當", "牛肉沙拉"]}
    )
    names = {meal for row in snapshot["master_api_rows"] for meal in (row[3], row[4])}
    assert names == {"雞肉便當", "牛肉沙拉"}


def test_form_unsafe_pick_falls_back_to_auto(tmp_path, monkeypatch):
    snapshot, pushes = _run_form(
        tmp_path,
        monkeypatch,
        {"想固定吃的餐點（選填，可複選）": ["牛肉沙拉"], "飲食禁忌": "不吃牛"},
    )
    assert snapshot["fixed_picks"] == []
    names = {meal for row in snapshot["master_api_rows"] for meal in (row[3], row[4])}
    assert "牛肉沙拉" not in names
    assert "略過（含禁忌）：牛肉沙拉" in snapshot["fixed_picks_admin_line"]


def test_form_without_question_is_unchanged(tmp_path, monkeypatch):
    snapshot, _ = _run_form(tmp_path, monkeypatch, {})
    assert snapshot["fixed_picks"] == []
    assert snapshot["fixed_picks_admin_line"] == ""


def test_grid_answer_from_apps_script_maps_rows_in_order():
    answer = [None] * len(fmp.GRID_ROWS)
    answer[fmp.GRID_ROWS.index("雞肉")] = ["低碳", "便當"]
    answer[fmp.GRID_ROWS.index("鮭魚")] = ["沙拉"]
    assert fmp.extract_form_names({"想固定吃的餐點（選填，可複選）": answer}) == [
        "雞肉低碳", "雞肉便當", "鮭魚沙拉"]


def test_grid_answer_from_named_values():
    data = {"想固定吃的餐點（選填，可複選） [雞肉]": "低碳, 便當", "想固定吃的餐點 [鮭魚]": ["沙拉"],
            "飲食禁忌": "不吃牛"}
    assert fmp.extract_form_names(data) == ["雞肉低碳", "雞肉便當", "鮭魚沙拉"]


def test_grid_with_wrong_row_count_is_not_guessed():
    names = fmp.extract_form_names({"想固定吃的餐點": [None, ["低碳"]]})
    usable, unsafe, unknown = fmp.resolve_fixed_picks(names, [], [])
    assert usable == [] and unknown and "表格" in fmp.admin_line(usable, unsafe, unknown)
    assert "表格" not in fmp.customer_lines(usable, unsafe, unknown)


def test_form_grid_single_pick(tmp_path, monkeypatch):
    answer = [None] * len(fmp.GRID_ROWS)
    answer[fmp.GRID_ROWS.index("豬肉")] = ["低碳"]
    snapshot, _ = _run_form(tmp_path, monkeypatch, {"想固定吃的餐點（選填，可複選）": answer})
    assert snapshot["fixed_picks"] == ["豬肉低碳"]
    assert all(row[3] == "豬肉低碳" for row in snapshot["master_api_rows"])
