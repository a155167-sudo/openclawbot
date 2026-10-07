from customer_navigation import build_customer_home_contents


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _dashboard_data():
    return {
        "name": "小美", "today_label": "2026/09/25（週五）",
        "extra_cal": 812.5, "tdee": 1800, "cal_remaining": 987.5,
        "extra_pro": 51.2, "protein_goal": 100, "pro_remaining": 48.8,
        "food_list": ["雞胸便當", "無糖豆漿"],
        "today_lunch": "雞胸便當", "today_dinner": "鮭魚餐盒",
        "lunch_checked": True, "dinner_checked": False,
        "frequent_foods": [{"name": "隱藏常吃品項"}],
        "today_workout": "隱藏課表", "xp_total": 900,
        "current_badge_name": "隱藏成就", "race_date": "2026/10/01",
    }


def test_customer_home_shows_today_nutrition_meals_and_verified_navigation_actions():
    contents = build_customer_home_contents(_dashboard_data())
    nodes = list(_walk(contents))
    text = "\n".join(node["text"] for node in nodes if node.get("type") == "text")
    actions = [node["action"] for node in nodes if node.get("type") == "button"]

    assert contents["type"] == "bubble"
    assert "2026/09/25（週五）" in text
    assert "812.5" in text and "1,800" in text
    assert "雞胸便當" in text and "鮭魚餐盒" in text
    assert any(action.get("text") == "我要紀錄飲食" for action in actions)
    assert any(action.get("text") == "我要修改飲食紀錄" for action in actions)
    assert any(action.get("text") == "功能選單" for action in actions)


def test_customer_home_keeps_non_today_items_out_and_does_not_duplicate_frequent_list():
    contents = build_customer_home_contents(_dashboard_data())
    text = "\n".join(node["text"] for node in _walk(contents) if node.get("type") == "text")
    serialized = str(contents)

    for hidden in ("隱藏課表", "隱藏成就", "隱藏常吃品項", "賽事倒數", "XP"):
        assert hidden not in text
    assert "直接加入" not in serialized
    assert serialized.count("雞胸便當") <= 2


def test_customer_home_marks_ai_estimated_logs_and_uses_white_green_palette():
    data = _dashboard_data()
    data["ai_estimated_count"] = 1
    contents = build_customer_home_contents(data)
    serialized = str(contents)
    assert "AI 估算" in serialized
    assert "#FFFDF8" in serialized
    assert "#6B8F71" in serialized
