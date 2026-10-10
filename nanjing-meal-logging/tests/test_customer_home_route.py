import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy")

import server


def test_home_route_renders_a_single_today_card_and_uses_working_actions(monkeypatch):
    dashboard = {
        "name": "測試會員", "today_label": "2026/09/25（週五）",
        "extra_cal": 600, "tdee": 1800, "extra_pro": 40, "protein_goal": 100,
        "food_list": ["雞胸便當"], "today_lunch": "雞胸便當", "today_dinner": "尚未安排",
        "lunch_checked": True, "dinner_checked": False, "frequent_foods": [{"name": "常吃食品"}],
        "ai_estimated_count": 0,
    }
    monkeypatch.setattr(server, "get_dashboard_data", lambda _uid, *, scope="full": dashboard)

    message = server.build_dashboard_flex("U-HOME")
    payload = message.as_json_dict()
    serialized = str(payload)

    assert payload["type"] == "flex"
    assert payload["altText"] == "今日總覽"
    assert payload["contents"]["type"] == "bubble"
    assert "今日總覽" in serialized and "雞胸便當" in serialized
    assert "運動戰情指揮中心" not in serialized
    assert "成就中心" not in serialized
    assert "常吃食品" not in serialized
    assert "我要紀錄飲食" in serialized
    assert "我要修改飲食紀錄" in serialized
    assert "功能選單" in serialized


def test_home_route_returns_none_without_customer_dashboard_data(monkeypatch):
    monkeypatch.setattr(server, "get_dashboard_data", lambda _uid, *, scope="full": None)
    assert server.build_dashboard_flex("U-NO-DATA") is None
