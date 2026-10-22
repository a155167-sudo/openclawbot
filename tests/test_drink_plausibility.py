"""手搖／超商飲料要能記錄：常見飲料名稱必須有可信範圍，不能被當成「類別不明」擋下。"""
import pytest

from nutrition_plausibility import assess_per100


def _n(kcal, p, f, c):
    return {"calories_kcal": kcal, "protein_g": p, "fat_g": f, "carbohydrate_g": c}


@pytest.mark.parametrize("name,per100", [
    ("珍珠奶茶", _n(75, 0.6, 2.0, 13.5)),
    ("拿鐵", _n(55, 3.0, 3.0, 4.5)),
    ("無糖綠茶", _n(0.5, 0, 0, 0.1)),
    ("紅茶", _n(40, 0, 0, 10)),
    ("多多綠茶", _n(45, 0.3, 0, 11)),
    ("柳橙汁", _n(45, 0.6, 0.1, 10.4)),
    ("可可", _n(70, 2, 2, 11)),
    ("芒果冰沙", _n(80, 0.5, 0.2, 19)),
    ("養樂多", _n(70, 1.2, 0, 16)),
])
def test_common_drinks_pass(name, per100):
    assert assess_per100({"food_name": name, "unit": "ml"}, per100)["status"] == "pass"


def test_drink_estimated_like_a_meal_is_still_stopped():
    # 把一杯奶茶估成 350 kcal/100 ml 是類別錯誤，仍要請客人確認。
    result = assess_per100({"food_name": "珍珠奶茶", "unit": "ml"}, _n(350, 5, 15, 45))
    assert result["status"] == "requires_confirmation" and result["category"] == "飲料類"


def test_tea_egg_is_an_egg_not_a_drink():
    result = assess_per100({"food_name": "茶葉蛋", "unit": "g"}, _n(140, 12, 9, 2))
    assert result["status"] == "pass" and result["category"] == "蛋類"


def test_noodles_and_soy_milk_keep_their_categories():
    assert assess_per100({"food_name": "鍋燒烏龍麵", "unit": "g"}, _n(110, 5, 3, 16))["category"] == "主食澱粉類"
    assert assess_per100({"food_name": "無糖豆漿", "unit": "ml"}, _n(35, 3.6, 1.9, 1))["category"] == "豆漿類"
