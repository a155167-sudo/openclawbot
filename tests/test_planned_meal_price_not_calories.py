"""Regression: the AI saw '豬肉低碳 ($200)' from the Sheet and logged 200 kcal.

The planned-meal text given to the AI must drop the price and carry the menu's
own calories/protein, and the meal-log prompt must offer a 「取消」 button.
"""
import server
from tests.test_nanjing_meal_logging_flow import _setup, _text_event


def _menu(monkeypatch):
    monkeypatch.setattr(server, "MAIN_DISHES", [
        {"name": "豬肉低碳", "cal": 372.81, "pro": 32.53, "price": 200},
        {"name": "雞肉低碳", "cal": 379.41, "pro": 40.17, "price": 185},
        {"name": "雞肉便當", "cal": 604.585, "pro": 43.16, "price": 160},
    ])


def test_price_is_removed_and_menu_nutrition_attached(monkeypatch):
    _menu(monkeypatch)
    text = server.describe_planned_meal_for_ai("豬肉低碳 ($200)")
    assert "200" not in text.replace("372.81", "")
    assert "$" not in text
    assert "372.81 大卡" in text and "32.53 克" in text


def test_full_width_and_bare_price_formats(monkeypatch):
    _menu(monkeypatch)
    assert "185" not in server.describe_planned_meal_for_ai("雞肉低碳（$185）")
    assert "185" not in server.describe_planned_meal_for_ai("雞肉低碳 $185")
    assert "379.41 大卡" in server.describe_planned_meal_for_ai("雞肉低碳 $185")


def test_unknown_dish_is_flagged_not_priced(monkeypatch):
    _menu(monkeypatch)
    text = server.describe_planned_meal_for_ai("神秘特餐 ($250)")
    assert "250" not in text
    assert "無此品項營養資料" in text


def test_empty_cell_stays_none(monkeypatch):
    _menu(monkeypatch)
    assert server.describe_planned_meal_for_ai("無") == "無"
    assert server.describe_planned_meal_for_ai("") == "無"


def test_meal_log_prompt_has_cancel_button(tmp_path, monkeypatch):
    _db, replies = _setup(tmp_path, monkeypatch)
    server._handle_message_impl(_text_event("cancel-btn-1", "我要紀錄飲食"))
    assert replies, "expected a reply"
    payload = replies[-1].as_json_dict()
    items = payload["quickReply"]["items"]
    assert [i["action"]["text"] for i in items] == ["取消"]

    server._handle_message_impl(_text_event("cancel-btn-2", "取消"))
    assert "已取消" in replies[-1].text
