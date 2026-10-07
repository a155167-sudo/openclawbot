import json
import sqlite3

import pytest
from linebot.models import BubbleContainer, FlexSendMessage

import server
from customer_navigation import (
    build_customer_function_menu_contents,
    build_customer_home_contents,
)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _texts(payload):
    return [node["text"] for node in _walk(payload) if node.get("type") == "text"]


def _home(**overrides):
    data = {
        "name": "測試會員", "today_label": "09/25（五）",
        "extra_cal": 500, "tdee": 1800,
        "extra_pro": 35, "protein_goal": 100,
        "extra_fat": 12,
        "food_list": ["已吃早餐"],
        "today_lunch": "香草雞胸 150g（NT$120）",
        "lunch_cal": 520, "lunch_pro": 42,
        "today_dinner": "鮭魚餐盒 $180",
        "dinner_cal": None, "dinner_pro": None,
        "lunch_checked": True, "dinner_checked": False,
        "ai_estimated_count": 0,
    }
    data.update(overrides)
    return data


def test_real_home_route_removes_only_explicit_price_and_shows_planned_nutrition(monkeypatch):
    data = _home()
    data["balance_records"] = [{
        "slot": "午餐", "name": "香草雞胸 150g", "kcal": 520, "protein": 42,
        "source_type": "planned_meal", "subscription_meal_id": "d1:午餐",
    }]
    data["balance_sub_meals"] = [
        {"slot": "午餐", "name": "香草雞胸 150g", "kcal": 520, "protein": 42,
         "subscription_meal_id": "d1:午餐"},
        {"slot": "晚餐", "name": "鮭魚餐盒", "kcal": None, "protein": None,
         "subscription_meal_id": "d1:晚餐"},
    ]
    monkeypatch.setattr(server, "get_dashboard_data", lambda _uid, *, scope="full": data)

    payload = server.build_dashboard_flex("U-UAT").as_json_dict()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "香草雞胸 150g" in serialized
    assert "NT$120" not in serialized and "$180" not in serialized
    assert "520 kcal" in serialized
    assert "預留 未知" in serialized
    assert "包月預留" in serialized


@pytest.mark.parametrize("meal", ("", "無", "尚未安排"))
def test_unplanned_meal_does_not_claim_nutrition_or_emit_eaten_action(meal):
    payload = build_customer_home_contents(_home(today_lunch=meal, lunch_cal=999, lunch_pro=99))
    serialized = json.dumps(payload, ensure_ascii=False)
    actions = [node.get("action", {}) for node in _walk(payload) if node.get("type") == "button"]

    assert "熱量 999 kcal｜蛋白質 99 g" not in serialized
    assert not any(action.get("text") == "午餐已吃" for action in actions)


def _dashboard_with_logs(tmp_path, monkeypatch, user_id, logs):
    db_path = tmp_path / f"{user_id}.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO health_profile(user_id,name,tdee,protein,today_date) VALUES (?,?,?,?,?)",
            (user_id, "測試會員", 1800, 100, today),
        )
        for index, nutrition in enumerate(logs):
            server.create_daily_food_log(
                conn, user_id=user_id, product_name=f"餐{index}", meal_slot="午餐",
                consumed_at=f"{today}T12:{index:02d}:00+08:00", servings=1,
                nutrition=nutrition, source_type="manual",
                operation_key=f"{user_id}-{index}", publish_catalog=False,
            )
        conn.commit()
    return server.get_dashboard_data(user_id)


@pytest.mark.parametrize(
    ("user_id", "logs", "expected"),
    (
        ("U-FAT-COMPLETE", ({"calories_kcal": 100, "protein_g": 10, "fat_g": 7}, {"calories_kcal": 200, "protein_g": 20, "fat_g": 5}), 12),
        ("U-FAT-UNKNOWN", ({"calories_kcal": 100, "protein_g": 10, "fat_g": 7}, {"calories_kcal": 200, "protein_g": 20}), None),
        ("U-FAT-ZERO", ({"calories_kcal": 0, "protein_g": 0, "fat_g": 0},), 0),
        ("U-FAT-EMPTY", (), 0),
    ),
)
def test_canonical_today_fat_preserves_complete_unknown_and_legal_zero(tmp_path, monkeypatch, user_id, logs, expected):
    data = _dashboard_with_logs(tmp_path, monkeypatch, user_id, logs)
    payload = build_customer_home_contents(data)
    text = "\n".join(_texts(payload))

    assert data["extra_fat"] == expected
    # v52 (product decision 2026-10-07): unknown fat is hidden, known fat is shown.
    assert ("脂肪" in text) == (expected is not None)
    assert "碳水" not in text


def _nutrition_rows(payload):
    nutrition = payload["body"]["contents"][2]
    assert nutrition["type"] == "box"
    assert nutrition["layout"] == "vertical"
    return nutrition["contents"]


def test_home_nutrition_is_three_compact_vertical_game_bars():
    payload = build_customer_home_contents(_home())
    rows = _nutrition_rows(payload)

    assert len(rows) == 3
    assert all(row["layout"] == "vertical" for row in rows)
    assert all(row.get("paddingAll") is None for row in rows)
    assert not any(node.get("size") in {"xl", "xxl", "3xl", "4xl", "5xl"} for row in rows for node in _walk(row))

    labels = [[node["text"] for node in _walk(row) if node.get("type") == "text"] for row in rows]
    assert labels[0] == ["🔥 熱量", "500 / 1,800 kcal", "剩 1,300 kcal"]
    assert labels[1] == ["🥩 蛋白質", "35 / 100 g", "剩 65 g"]
    assert labels[2] == ["🥑 脂肪", "12 g / 未設目標", "僅顯示今日攝取"]

    tracks = [next(node for node in _walk(row) if node.get("height") == "8px") for row in rows]
    assert tracks[0]["contents"][0]["width"] == "28%"
    assert tracks[1]["contents"][0]["width"] == "35%"
    assert tracks[2]["contents"] == []


def test_home_nutrition_clamps_over_goal_and_keeps_unknown_or_missing_goal_neutral():
    over = build_customer_home_contents(_home(extra_cal=2000))
    over_row = _nutrition_rows(over)[0]
    over_text = [node["text"] for node in _walk(over_row) if node.get("type") == "text"]
    over_track = next(node for node in _walk(over_row) if node.get("height") == "8px")
    assert over_text == ["🔥 熱量", "2,000 / 1,800 kcal", "超出 200 kcal"]
    assert over_track["contents"][0]["width"] == "100%"
    assert over_track["contents"][0]["backgroundColor"] == "#EAA75B"

    neutral = build_customer_home_contents(_home(extra_cal=None, protein_goal=0, extra_fat=None))
    rows = _nutrition_rows(neutral)
    text = [[node["text"] for node in _walk(row) if node.get("type") == "text"] for row in rows]
    assert text[0] == ["🔥 熱量", "未知 / 1,800 kcal", "剩餘無法計算"]
    assert text[1] == ["🥩 蛋白質", "35 g / 未設目標", "剩餘無法計算（尚未設定目標）"]
    # v52: unknown fat row is omitted rather than shown as "無法計算".
    assert len(rows) == 2
    assert all(next(node for node in _walk(row) if node.get("height") == "8px")["contents"] == [] for row in rows)


def test_compact_home_is_accepted_by_installed_line_flex_sdk():
    container = BubbleContainer.new_from_json_dict(build_customer_home_contents(_home()))
    serialized = FlexSendMessage(alt_text="營養首頁", contents=container).as_json_dict()

    assert serialized["type"] == "flex"
    assert serialized["contents"]["type"] == "bubble"


def test_menu_omits_my_data_and_unavailable_health_placeholder_but_keeps_eligible_training_entry():
    unavailable = build_customer_function_menu_contents(health_services_available=False)
    unavailable_text = json.dumps(unavailable, ensure_ascii=False)
    unavailable_actions = [node["action"]["text"] for node in _walk(unavailable) if node.get("type") == "button"]
    assert unavailable_actions == ["首頁", "搜尋", "我要紀錄飲食", "重選常吃", "查看菜單"]
    assert "我的資料" not in unavailable_text
    assert "符合資格後顯示" not in unavailable_text
    assert "六格" not in unavailable_text

    eligible = build_customer_function_menu_contents(health_services_available=True)
    eligible_actions = [node["action"]["text"] for node in _walk(eligible) if node.get("type") == "button"]
    assert eligible_actions == ["首頁", "搜尋", "我要紀錄飲食", "重選常吃", "查看菜單", "健康服務"]
    rows = [node for node in eligible["body"]["contents"] if node.get("layout") == "horizontal"]
    assert len(rows) == 3
    assert all(len(row["contents"]) == 2 for row in rows)
