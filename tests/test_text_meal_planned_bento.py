"""「午餐吃了豬肉低碳」：本店便當不用克數；今天的包月便當直接記成已吃。"""
import pytest

import server

DISHES = [
    {"name": "豬肉低碳", "cal": 372.81, "pro": 32.53, "fat": 10.0, "carbs": 20.0, "category": "main"},
    {"name": "雞肉低碳", "cal": 379.41, "pro": 40.17, "fat": 9.0, "carbs": 18.0, "category": "main"},
    {"name": "茶葉蛋", "cal": 75, "pro": 7, "fat": 5, "carbs": 1, "category": "side"},
]


@pytest.fixture
def env(monkeypatch):
    calls = []
    state = {"meals": [
        {"slot": "午餐", "name": "豬肉低碳 ($200)", "eaten": False, "skipped": False, "swappable": True},
        {"slot": "晚餐", "name": "雞肉低碳 ($185)", "eaten": False, "skipped": False, "swappable": False},
    ]}
    monkeypatch.setattr(server, "MAIN_DISHES", DISHES)
    monkeypatch.setattr(server, "get_dashboard_data",
                        lambda uid, scope="full": {"balance_sub_meals": state["meals"]})
    monkeypatch.setattr(server, "mark_planned_meal_as_eaten",
                        lambda uid, slot: (calls.append(("eat", slot)) or ("FLEX", "已幫你確認")))
    monkeypatch.setattr(server, "mark_lunch_ate_dinner_bento",
                        lambda uid, day: (calls.append(("swap", day)) or ("FLEX", "已幫你確認")))
    monkeypatch.setattr(server, "create_fixed_text_meal_draft",
                        lambda **kw: (calls.append(("draft", kw)) or {"draft": kw}))
    monkeypatch.setattr(server, "build_text_meal_estimate_flex", lambda draft: ("DRAFT", draft))
    return calls, state


def test_todays_lunch_bento_is_marked_eaten(env):
    calls, _ = env
    assert server.planned_bento_text_reply("U", "m1", "我今天午餐吃了豬肉低碳") == "FLEX"
    assert calls == [("eat", "午餐")]


def test_dinner_bento_eaten_at_lunch_swaps(env):
    calls, _ = env
    server.planned_bento_text_reply("U", "m1", "中午吃了雞肉低碳")
    assert calls[0][0] == "swap"


def test_dinner_bento_without_slot_word_marks_dinner(env):
    calls, _ = env
    server.planned_bento_text_reply("U", "m1", "吃了雞肉低碳")
    assert calls == [("eat", "晚餐")]


def test_already_eaten_is_not_counted_twice(env):
    calls, state = env
    state["meals"][0]["eaten"] = True
    reply = server.planned_bento_text_reply("U", "m1", "午餐吃了豬肉低碳")
    assert "已經記錄為已吃" in reply.text and calls == []


def test_menu_bento_not_planned_today_uses_existing_menu_flow(env):
    calls, state = env
    state["meals"] = []
    assert server.planned_bento_text_reply("U", "m1", "晚餐吃了雞肉低碳") is None and calls == []


@pytest.mark.parametrize("text", [
    "豬肉低碳熱量多少？", "午餐沒吃豬肉低碳", "我今天吃了茶葉蛋", "我今天吃了白飯150克", "豬肉低碳好吃嗎",
])
def test_questions_negations_sides_and_other_foods_fall_through(env, text):
    calls, _ = env
    assert server.planned_bento_text_reply("U", "m1", text) is None and calls == []


def test_armed_mode_accepts_bare_bento_name(env):
    calls, _ = env
    assert server.planned_bento_text_reply("U", "m1", "豬肉低碳", armed=True) == "FLEX"
    assert server.planned_bento_text_reply("U", "m1", "豬肉低碳") is None  # chat needs 吃/記/餐別
