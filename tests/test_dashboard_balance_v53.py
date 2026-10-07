import json
import sqlite3
from collections import Counter
from datetime import timedelta

import pytest
from linebot.models import BubbleContainer, FlexSendMessage

import server
from dashboard_balance_adapter import adapt_dashboard_data
from dashboard_flex import C_OVER, build_dashboard_flex, compute


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _text(payload):
    return "\n".join(node["text"] for node in _walk(payload) if node.get("type") == "text")


def _actions(payload):
    return [node["action"] for node in _walk(payload) if node.get("type") == "button"]


def _data(**overrides):
    data = {
        "user_name": "Jq", "date_label": "10/07（三）",
        "target_kcal": 2000, "target_protein": 120,
        "records": [], "sub_meals": [],
    }
    data.update(overrides)
    return data


def _record(slot="午餐", name="雞胸", kcal=650, protein=42, **extra):
    return {"slot": slot, "name": name, "kcal": kcal, "protein": protein,
            "is_sub": extra.pop("is_sub", False), "ai_estimated": extra.pop("ai_estimated", False), **extra}


def _sub(slot="午餐", name="包月雞胸", kcal=650, protein=42, eaten=False, **extra):
    return {"slot": slot, "name": name, "kcal": kcal, "protein": protein,
            "eaten": eaten, **extra}


# SPEC 1
def test_spec_01_no_subscription_one_record_has_no_reserve_legend():
    data = _data(records=[_record(kcal=600, protein=40)])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert result["left_k"] == 1400
    assert "包月預留" not in text and "熱量餘額\n1,400" in text


# SPEC 2
def test_spec_02_two_uneaten_subscription_meals_are_both_reserved():
    data = _data(sub_meals=[_sub(), _sub("晚餐", "包月鮭魚", 741, 45)])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert (result["ek"], result["rk"], result["left_k"]) == (0, 1391, 609)
    assert "預留 650" in text and "預留 741" in text and "預留 1,391" in text


# SPEC 3
def test_spec_03_eaten_lunch_is_not_double_counted_while_dinner_is_reserved():
    data = _data(records=[_record(is_sub=True, subscription_meal_id="d1:午餐")],
                 sub_meals=[_sub(eaten=True, subscription_meal_id="d1:午餐"),
                            _sub("晚餐", "包月鮭魚", 741, 45, subscription_meal_id="d1:晚餐")])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert (result["ek"], result["rk"], result["left_k"]) == (650, 741, 609)
    assert text.count("午餐｜雞胸") == 1 and "預留 650" not in text and "預留 741" in text


# SPEC 4
def test_spec_04_all_subscription_meals_eaten_have_zero_reserve_and_green_progress():
    meals = [_sub(eaten=True), _sub("晚餐", "鮭魚", 741, 45, eaten=True)]
    data = _data(records=[_record(is_sub=True), _record("晚餐", "鮭魚", 741, 45, is_sub=True)], sub_meals=meals)
    result = compute(data)
    payload = build_dashboard_flex(data)
    assert result["rk"] == 0 and result["left_k"] == 609
    bars = [node for node in _walk(payload["body"]) if node.get("height") in {"14px", "10px"}]
    assert not any(segment.get("backgroundColor") == "#F5C842" for bar in bars for segment in bar["contents"])


# SPEC 5
def test_spec_05_eaten_over_target_is_orange_and_never_negative():
    data = _data(records=[_record(kcal=2150, protein=102)])
    result = compute(data)
    payload = build_dashboard_flex(data)
    text = _text(payload)
    assert result["state"] == "over"
    assert "已超出\n150\nkcal" in text and "-150" not in text
    assert any(node.get("backgroundColor") == C_OVER for node in _walk(payload))


# SPEC 6
def test_spec_06_subscription_total_over_target_clamps_balance_and_hints_without_restriction():
    data = _data(sub_meals=[_sub(kcal=1100), _sub("晚餐", "鮭魚", 1100, 40)])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert result["state"] == "sub_over" and "熱量餘額\n0\nkcal" in text
    assert "包月餐照常吃" in text and "少吃" not in text


# SPEC 7
def test_spec_07_calories_eaten_to_goal_and_protein_gap_shows_reached_hint():
    data = _data(records=[_record(kcal=2000, protein=80)])
    assert "熱量已達標，蛋白質還差 40 g" in compute(data)["hint"]


# SPEC 8
def test_spec_08_asia_taipei_new_day_excludes_previous_day_records(tmp_path, monkeypatch):
    uid = "U-CROSS-DAY"
    _install_db(tmp_path, monkeypatch, uid, rows=[])
    yesterday = server.tw_today() - timedelta(days=1)
    with sqlite3.connect(server.DB_PATH) as conn:
        server.create_daily_food_log(conn, user_id=uid, product_name="昨晚餐", meal_slot="晚餐",
            consumed_at=f"{yesterday.isoformat()}T23:59:59+08:00", servings=1,
            nutrition={"calories_kcal": 900, "protein_g": 50}, source_type="manual",
            operation_key="yesterday", publish_catalog=False)
        conn.commit()
    adapted = adapt_dashboard_data(server.get_dashboard_data(uid, scope="home"))
    assert adapted["records"] == [] and compute(adapted)["ek"] == 0


# SPEC 9
def test_spec_09_more_than_six_rows_shows_exact_overflow_count():
    data = _data(records=[_record(slot="點心", name=f"餐{i}", kcal=10, protein=1) for i in range(8)])
    text = _text(build_dashboard_flex(data))
    assert "還有 2 筆，請看今日明細" in text


# SPEC 10
def test_spec_10_ai_disclaimer_only_when_any_record_is_estimated():
    clean = _data(records=[_record()])
    estimated = _data(records=[_record(ai_estimated=True)])
    assert "AI 估算" not in _text(build_dashboard_flex(clean))
    assert "含 AI 估算紀錄，非營養師審核結果" in _text(build_dashboard_flex(estimated))


# SPEC 11
@pytest.mark.parametrize("protein_target", (120, 0))
def test_spec_11_zero_calorie_target_is_safe_and_reports_no_target(protein_target):
    data = _data(target_kcal=0, target_protein=protein_target, records=[_record(kcal=321, protein=20)])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert result["state"] == "no_target"
    assert "今日已吃\n321\nkcal" in text and "目標未設定" in text


# SPEC 12
def test_spec_12_subscription_over_hint_precedes_protein_and_never_says_calories_reached():
    data = _data(target_kcal=1000, target_protein=120,
                 sub_meals=[_sub(kcal=1100, protein=20)])
    hint = compute(data)["hint"]
    assert hint.startswith("今天的包月餐合計比目標多 100 kcal")
    assert "吃完包月餐後蛋白質還差 100 g" in hint
    assert "熱量已達標" not in hint


# SPEC 13
def test_spec_13_reservation_exactly_fills_target_uses_after_meal_wording():
    data = _data(target_kcal=1000, target_protein=120,
                 sub_meals=[_sub(kcal=1000, protein=20)])
    hint = compute(data)["hint"]
    assert "吃完包月餐後熱量剛好達標" in hint and "熱量已達標" not in hint


def test_adapter_matches_subscription_by_id_and_refuses_date_slot_fallback_on_id_mismatch():
    dashboard = _dashboard_fixture(
        balance_records=[_raw_record("午餐", "已吃包月", 650, 42, source_type="planned_meal", subscription_meal_id="dispatch-X:午餐")],
        balance_sub_meals=[_raw_sub("午餐", "排定包月", 650, 42, subscription_meal_id="dispatch-Y:午餐")],
    )
    data = adapt_dashboard_data(dashboard)
    assert data["records"][0]["is_sub"] is True
    assert data["sub_meals"][0]["eaten"] is False
    assert compute(data)["rk"] == 650


def test_adapter_legacy_matches_only_same_slot_with_planned_meal_marker():
    marked = _dashboard_fixture(
        balance_records=[_raw_record("午餐", "名字可不同", 650, 42, source_type="planned_meal")],
        balance_sub_meals=[_raw_sub("午餐", "排定包月", 650, 42)],
    )
    ordinary = _dashboard_fixture(
        balance_records=[_raw_record("午餐", "一般午餐", 650, 42, source_type="manual")],
        balance_sub_meals=[_raw_sub("午餐", "排定包月", 650, 42)],
    )
    assert adapt_dashboard_data(marked)["sub_meals"][0]["eaten"] is True
    assert adapt_dashboard_data(ordinary)["sub_meals"][0]["eaten"] is False


def test_unknown_nutrition_is_not_coerced_to_zero_or_claimed_as_precise_balance():
    data = _data(records=[_record(kcal=None, protein=10)], sub_meals=[_sub("晚餐", kcal=None, protein=20)])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert result["ek"] is None and result["rk"] == 0 and result["left_k"] is None
    assert "未知" in text and "熱量餘額\n2,000" not in text and "已吃 0" not in text
    assert "營養待補" in text


def test_balance_flex_roundtrips_installed_line_sdk_and_preserves_four_actions():
    payload = build_dashboard_flex(_data())
    serialized = FlexSendMessage(alt_text="今日總覽", contents=BubbleContainer.new_from_json_dict(payload)).as_json_dict()
    assert serialized["contents"]["type"] == "bubble"
    assert _actions(payload) == [
        {"type": "message", "label": "記一餐", "text": "我要紀錄飲食"},
        {"type": "message", "label": "今日明細", "text": "我要修改飲食紀錄"},
        {"type": "message", "label": "一週趨勢", "text": "一週趨勢"},
        {"type": "message", "label": "功能選單", "text": "功能選單"},
    ]


class _Sheet:
    id = 17
    def __init__(self, title, values):
        self.title = title
        self.values = values
    def get_all_values(self):
        return self.values


class _Book:
    id = "test-spreadsheet-no-network"
    def __init__(self, sheet):
        self.sheet = sheet
    def worksheet(self, title):
        if title != self.sheet.title:
            raise KeyError(title)
        return self.sheet


class _GC:
    def __init__(self, book):
        self.book = book
    def open_by_key(self, _key):
        return self.book


def _install_db(tmp_path, monkeypatch, uid, rows, *, target=2000, protein=120, dispatch_id=""):
    path = tmp_path / f"{uid}.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(path))
    server.init_db()
    today_iso = server.tw_today().isoformat()
    today_slash = server.tw_today().strftime("%Y/%m/%d")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO health_profile(user_id,name,tdee,protein,today_date,sheet_name) VALUES (?,?,?,?,?,?)",
                     (uid, "整合會員", target, protein, today_iso, "member-sheet"))
        for item in rows:
            server.create_daily_food_log(conn, user_id=uid, product_name=item["name"], meal_slot=item["slot"],
                consumed_at=f"{today_iso}T12:00:00+08:00", servings=1,
                nutrition={"calories_kcal": item.get("kcal"), "protein_g": item.get("protein")},
                source_type=item.get("source_type", "manual"), operation_key=item.get("operation_key", item["name"]),
                publish_catalog=False)
        conn.commit()
    headers = ["實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排", "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白", "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態", "Dispatch_Row_ID", "Order_ID", "Menu_Version"]
    values = [["【VIP 客戶檔案】", "姓名: 整合會員", f"User_ID: {uid}"], headers,
              [today_slash, "三", "包月午餐", "650", "42", "包月晚餐", "741", "45", "1391", "87", "", "", "無", "待列印", dispatch_id, "", ""]]
    monkeypatch.setattr(server, "SPREADSHEET_ID", _Book.id)
    monkeypatch.setattr(server, "gc", _GC(_Book(_Sheet("member-sheet", values))))
    return path


def test_real_get_dashboard_to_real_card_uses_dispatch_id_without_double_counting(tmp_path, monkeypatch):
    uid = "U-INTEGRATED-ID"
    _install_db(tmp_path, monkeypatch, uid, [{"name": "包月午餐", "slot": "午餐", "kcal": 650, "protein": 42,
        "source_type": "planned_meal", "operation_key": "planned-meal:dispatch-1:午餐"}], dispatch_id="dispatch-1")
    raw = server.get_dashboard_data(uid, scope="home")
    data = adapt_dashboard_data(raw)
    payload = server.build_dashboard_flex(uid).as_json_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert data["sub_meals"][0]["eaten"] is True and data["sub_meals"][1]["eaten"] is False
    assert compute(data)["left_k"] == 609
    assert "預留 650" not in text and "預留 741" in text
    assert payload["contents"]["type"] == "bubble"


def test_real_get_dashboard_legacy_marker_matches_same_date_and_slot(tmp_path, monkeypatch):
    uid = "U-INTEGRATED-LEGACY"
    _install_db(tmp_path, monkeypatch, uid, [{"name": "舊包月午餐", "slot": "午餐", "kcal": 650, "protein": 42,
        "source_type": "planned_meal", "operation_key": "legacy-planned"}], dispatch_id="")
    data = adapt_dashboard_data(server.get_dashboard_data(uid, scope="home"))
    assert [meal["eaten"] for meal in data["sub_meals"]] == [True, False]


def _dashboard_fixture(**overrides):
    data = {"name": "Jq", "today_label": "10/07（三）", "tdee": 2000, "protein_goal": 120,
            "balance_records": [], "balance_sub_meals": [], "ai_estimated_count": 0}
    data.update(overrides)
    return data


def _raw_record(slot, name, kcal, protein, **extra):
    return {"slot": slot, "name": name, "kcal": kcal, "protein": protein, **extra}


def _raw_sub(slot, name, kcal, protein, **extra):
    return {"slot": slot, "name": name, "kcal": kcal, "protein": protein, **extra}
