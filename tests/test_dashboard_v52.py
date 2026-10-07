import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from linebot.models import BubbleContainer, FlexSendMessage

import server
from customer_navigation import (
    build_customer_home_contents,
    build_next_meal_tip_nodes,
    build_weekly_trend_contents,
)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _text(payload):
    return "\n".join(n["text"] for n in _walk(payload) if n.get("type") == "text")


def _home(**overrides):
    data = {
        "name": "測試會員", "today_label": "10/07（三）",
        "extra_cal": 600, "tdee": 1900, "extra_pro": 40, "protein_goal": 120,
        "extra_fat": None, "food_list": [], "food_entries": [],
        "today_lunch": "無", "today_dinner": "無",
        "lunch_cal": None, "lunch_pro": None, "dinner_cal": None, "dinner_pro": None,
        "lunch_checked": False, "dinner_checked": False, "ai_estimated_count": 0,
        "now_hour": 12,
    }
    data.update(overrides)
    return data


# --- fat row -------------------------------------------------------------

def test_unknown_fat_row_is_hidden():
    text = _text(build_customer_home_contents(_home(extra_fat=None)))
    assert "脂肪" not in text and "無法計算" not in text


def test_known_fat_is_still_shown():
    assert "🥑 脂肪" in _text(build_customer_home_contents(_home(extra_fat=12)))


# --- 今日安排 ------------------------------------------------------------

def test_no_planned_meals_is_one_clear_line_without_uneaten_status():
    text = _text(build_customer_home_contents(_home()))
    assert "今天沒有安排包月餐點" in text
    assert "未吃" not in text


def test_unplanned_slot_next_to_planned_one_says_unscheduled_not_uneaten():
    payload = build_customer_home_contents(_home(today_lunch="香草雞胸", lunch_cal=520, lunch_pro=42))
    text = _text(payload)
    assert "香草雞胸" in text and "未安排" in text
    assert text.count("未吃") == 1  # only the planned lunch
    actions = [n["action"]["text"] for n in _walk(payload) if n.get("type") == "button"]
    assert "午餐已吃" in actions and "晚餐已吃" not in actions


# --- 飲食紀錄分餐別 -----------------------------------------------------

def test_food_log_is_grouped_by_slot_with_calories_and_subtotals():
    entries = [
        {"name": "蘋果", "meal_slot": "早餐", "calories_kcal": 80},
        {"name": "無糖豆漿", "meal_slot": "早餐", "calories_kcal": 170},
        {"name": "餐點照片：雞肉、高麗菜", "meal_slot": "午餐", "calories_kcal": 354},
        {"name": "神秘餐", "meal_slot": "", "calories_kcal": None},
    ]
    text = _text(build_customer_home_contents(_home(food_entries=entries, food_list=[e["name"] for e in entries])))
    assert text.index("早餐") < text.index("蘋果") < text.index("午餐") < text.index("餐點照片")
    assert "250 kcal" in text and "354 kcal" in text and "170 kcal" in text
    assert "其他" in text and "未知" in text


def test_food_log_falls_back_to_names_without_entries():
    text = _text(build_customer_home_contents(_home(food_list=["舊資料餐"])))
    assert "舊資料餐" in text


# --- 下一餐建議 ---------------------------------------------------------

def _tip_text(data):
    return "\n".join(n["text"] for n in _walk(build_next_meal_tip_nodes(data)) if n.get("type") == "text")


def test_tip_splits_what_is_left_over_remaining_meals():
    text = _tip_text(_home(now_hour=12))  # lunch + dinner left, nothing planned
    assert "下一餐建議約 650 kcal、蛋白質 40 g" in text


def test_tip_uses_planned_meal_then_advises_the_rest():
    text = _tip_text(_home(now_hour=12, today_lunch="香草雞胸 150g（NT$120）", lunch_cal=520, lunch_pro=42))
    assert "午餐是「香草雞胸 150g」，約 520 kcal、蛋白質 42 g" in text
    assert "晚餐建議約 780 kcal、蛋白質 38 g" in text
    assert "NT$" not in text


def test_tip_after_planned_dinner_reports_protein_gap():
    text = _tip_text(_home(now_hour=18, today_dinner="鮭魚餐盒", dinner_cal=600, dinner_pro=35))
    assert "晚餐是「鮭魚餐盒」" in text and "蛋白質還差約 45 g" in text


def test_tip_over_goal_suggests_light_meal():
    assert "熱量已接近目標" in _tip_text(_home(extra_cal=1950))


@pytest.mark.parametrize("override", ({"now_hour": 21}, {"tdee": None}, {"protein_goal": 0}, {"extra_cal": None}, {"now_hour": None}))
def test_tip_is_omitted_when_it_cannot_be_honest(override):
    assert build_next_meal_tip_nodes(_home(**override)) == []


def test_home_with_tip_keeps_metric_box_index_and_is_valid_flex():
    payload = build_customer_home_contents(_home(extra_fat=12))
    assert payload["body"]["contents"][2]["type"] == "box"
    assert "💡 下一餐建議" in _text(payload)
    FlexSendMessage(alt_text="今日總覽", contents=BubbleContainer.new_from_json_dict(payload)).as_json_dict()


def test_home_footer_offers_weekly_trend_and_keeps_function_menu():
    payload = build_customer_home_contents(_home())
    actions = [n["action"]["text"] for n in _walk(payload["footer"]) if n.get("type") == "button"]
    assert {"一週趨勢", "功能選單", "我要紀錄飲食", "我要修改飲食紀錄"} <= set(actions)


# --- 一週趨勢 -----------------------------------------------------------

def test_weekly_trend_bubble_is_valid_and_greys_unlogged_days():
    days = [{"label": l, "logged": l not in {"二", "四"}, "calories_kcal": None if l in {"二", "四"} else 1500.0,
             "protein_g": None if l in {"二", "四"} else 90.0} for l in ("四", "五", "六", "日", "一", "二", "今")]
    days[3]["logged"] = False
    days[3]["calories_kcal"] = days[3]["protein_g"] = None
    payload = build_weekly_trend_contents(days, calorie_goal=1900, protein_goal=120)
    text = _text(payload)
    assert "有紀錄 4 天" in text and "平均 1,500 kcal" in text and "目標 1,900 kcal" in text
    FlexSendMessage(alt_text="一週趨勢", contents=BubbleContainer.new_from_json_dict(payload)).as_json_dict()
    grey = [n for n in _walk(payload) if n.get("backgroundColor") == "#E6E9E7" and n.get("height") == "2px"]
    assert len(grey) == 2 * 3  # 3 unlogged days x 2 charts


def test_weekly_trend_empty_week_is_explicit():
    days = [{"label": str(i), "logged": False, "calories_kcal": None, "protein_g": None} for i in range(7)]
    assert "這週還沒有紀錄" in _text(build_weekly_trend_contents(days))


def test_weekly_trend_data_uses_the_same_countable_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "trend.db"))
    monkeypatch.setattr(server, "gc", None)
    server.init_db()
    today = server.tw_today()
    with sqlite3.connect(server.DB_PATH) as conn:
        conn.execute("INSERT INTO health_profile(user_id,name,tdee,protein,today_date) VALUES ('U-T','T',1900,120,?)", (today.isoformat(),))
        for offset, cal in ((0, 400), (2, 1200)):
            day = (today - timedelta(days=offset)).isoformat()
            server.create_daily_food_log(
                conn, user_id="U-T", product_name=f"餐{offset}", meal_slot="午餐",
                consumed_at=f"{day}T12:00:00+08:00", servings=1,
                nutrition={"calories_kcal": cal, "protein_g": 30}, source_type="manual",
                operation_key=f"trend-{offset}", publish_catalog=False,
            )
        conn.commit()
    data = server.get_weekly_trend_data("U-T")
    assert [d["logged"] for d in data["days"]] == [False, False, False, False, True, False, True]
    assert data["days"][-1]["calories_kcal"] == 400 and data["days"][-1]["label"] == "今"
    assert data["days"][-3]["calories_kcal"] == 1200
    assert data["calorie_goal"] == 1900 and data["protein_goal"] == 120
    today_home = server.get_dashboard_data("U-T", scope="home")
    assert today_home["extra_cal"] == data["days"][-1]["calories_kcal"]
    assert today_home["food_entries"] == [{"name": "餐0", "meal_slot": "午餐", "calories_kcal": 400}]


# --- 依身分切換圖文選單 -------------------------------------------------

class _FakeLineApi:
    def __init__(self):
        self.calls = []

    def link_rich_menu_to_user(self, uid, menu_id):
        self.calls.append(("link", uid, menu_id))

    def unlink_rich_menu_from_user(self, uid):
        self.calls.append(("unlink", uid))


@pytest.fixture
def menu_env(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "menu.db"))
    api = _FakeLineApi()
    monkeypatch.setattr(server, "line_bot_api", api)
    monkeypatch.setattr(server, "RICH_MENU_MEMBER_ID", "richmenu-member")
    with sqlite3.connect(server.DB_PATH) as conn:
        conn.execute("CREATE TABLE usage(user_id TEXT PRIMARY KEY,status TEXT,expiry_date TEXT)")
    return api


def _set_usage(uid, status, expiry):
    with sqlite3.connect(server.DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO usage VALUES(?,?,?)", (uid, status, expiry))


def test_rich_menu_disabled_without_menu_id(menu_env, monkeypatch):
    monkeypatch.setattr(server, "RICH_MENU_MEMBER_ID", "")
    assert server.sync_customer_rich_menu("U1") == "disabled" and menu_env.calls == []


def test_active_vip_is_linked_once_then_unchanged(menu_env):
    _set_usage("U1", "vip", "2099-12-31")
    assert server.sync_customer_rich_menu("U1") == "linked"
    assert server.sync_customer_rich_menu("U1") == "unchanged"
    assert menu_env.calls == [("link", "U1", "richmenu-member")]


def test_guest_never_linked_costs_no_api_call(menu_env):
    assert server.sync_customer_rich_menu("U2") == "unchanged" and menu_env.calls == []


def test_expired_member_is_returned_to_default_menu(menu_env):
    _set_usage("U3", "vip", "2099-12-31")
    server.sync_customer_rich_menu("U3")
    _set_usage("U3", "vip", "2000-01-01")
    assert server.sync_customer_rich_menu("U3") == "unlinked"
    assert menu_env.calls[-1] == ("unlink", "U3")


def test_api_failure_is_retried_on_next_message(menu_env, monkeypatch):
    _set_usage("U4", "vip", "2099-12-31")
    def boom(uid, menu_id):
        raise RuntimeError("line down")
    monkeypatch.setattr(menu_env, "link_rich_menu_to_user", boom)
    with pytest.raises(RuntimeError):
        server.sync_customer_rich_menu("U4")
    recovered = _FakeLineApi()
    monkeypatch.setattr(server, "line_bot_api", recovered)
    assert server.sync_customer_rich_menu("U4") == "linked"
    assert recovered.calls == [("link", "U4", "richmenu-member")]


def test_delivery_sync_only_touches_user_sources(menu_env):
    _set_usage("U5", "vip", "2099-12-31")
    body = json.dumps({"events": [
        {"source": {"type": "user", "userId": "U5"}},
        {"source": {"type": "group", "groupId": "C1", "userId": "U5"}},
        {"source": {"type": "user", "userId": "U5"}},
    ]})
    server._sync_rich_menus_for_delivery(body)
    assert menu_env.calls == [("link", "U5", "richmenu-member")]


def test_rich_menu_script_builds_six_full_bounds_and_rejects_placeholders(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rm", "ops/create_member_rich_menu.py")
    rm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rm)
    config = json.loads(open("ops/member_rich_menu.example.json", encoding="utf-8").read())
    with pytest.raises(SystemExit, match="尚未填好"):
        rm.build_payload(config)
    for tile in config["tiles"]:
        if tile["action"]["type"] == "uri":
            tile["action"]["uri"] = "https://example.com/x"
    payload = rm.build_payload(config)
    assert len(payload["areas"]) == 6
    assert sum(a["bounds"]["width"] * a["bounds"]["height"] for a in payload["areas"]) == 2500 * 1686
