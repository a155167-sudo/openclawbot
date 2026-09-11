import subscription_meal_plan as meal_plan
import pytest
from subscription_meal_plan import (
    ensure_light_bento_coverage,
    get_subscription_form_value,
)


def _dish(name, *, price=180):
    return {
        "name": name,
        "price": price,
        "cal": 400,
        "pro": 30,
        "ingredients": name,
        "category": "main",
        "carb_type": "高碳",
    }


def test_form_value_prefers_positive_new_column_over_disliked_column():
    payload = {
        "您不喜歡的蛋白質（可複選）": ["牛肉"],
        "您最喜歡的蛋白質是？（可複選）": ["雞肉", "魚"],
    }

    value = get_subscription_form_value(
        payload,
        "您最喜歡的蛋白質是",
        "蛋白質偏好",
        "蛋白質",
        excluded_fragments=("不喜歡", "避免"),
    )

    assert value == "雞肉,魚"


def test_form_value_prefers_specific_new_column_over_generic_exact_column():
    payload = {
        "您偏好的蛋白質種類（可複選）": ["雞肉"],
        "蛋白質": ["牛肉"],
    }

    assert get_subscription_form_value(
        payload,
        "您最喜歡的蛋白質是",
        "蛋白質偏好",
        "偏好的蛋白質",
        "蛋白質",
        excluded_fragments=("不喜歡", "避免"),
    ) == "雞肉"


def test_form_value_keeps_old_columns_and_normalizes_wrapped_new_labels():
    assert get_subscription_form_value(
        {"您的主食偏好是？(可複選)": ["飯食派"]}, "主食偏好"
    ) == "飯食派"
    assert get_subscription_form_value(
        {"您的主食\n選擇（可複選）": ["都不挑食"]}, "主食選擇"
    ) == "都不挑食"


def test_arbitrary_restriction_terms_match_name_and_hidden_ingredients():
    assert meal_plan.dish_matches_restrictions(
        {"name": "香料便當", "ingredients": "孜然羊肉"}, "羊肉"
    )
    assert meal_plan.dish_matches_restrictions(
        {"name": "雞肉食蔬", "ingredients": "雞肉,起司"}, "避免起司"
    )
    assert not meal_plan.dish_matches_restrictions(
        {"name": "雞肉便當", "ingredients": "雞肉"}, "羊肉,起司"
    )
    assert meal_plan.dish_matches_restrictions(
        {"name": "雞肉便當", "ingredients": "雞肉,香菜"}, "不吃香菜和芹菜"
    )
    assert meal_plan.dish_matches_restrictions(
        {"name": "香菜雞肉便當", "ingredients": "雞肉,香菜"}, "我對香菜過敏"
    )
    assert meal_plan.dish_matches_restrictions(
        {"name": "香蔥雞肉便當", "ingredients": "雞肉,蔥"}, "不吃蔥"
    )


def test_all_eater_plan_gets_one_safe_light_bento_per_active_week():
    chicken_bento = _dish("雞肉便當")
    chicken_low_carb = _dish("雞肉低碳")
    chicken_light = _dish("雞肉食蔬")
    fish_light = _dish("鱸魚食蔬")
    requests = [
        (1, 1, "第1週", "週一", chicken_bento, chicken_low_carb),
        (1, 3, "第1週", "週三", chicken_low_carb, chicken_bento),
        (2, 1, "第2週", "週一", chicken_bento, chicken_low_carb),
    ]

    result = ensure_light_bento_coverage(
        requests,
        safe_menu=[chicken_bento, chicken_low_carb, chicken_light, fish_light],
        pref_staple="都不挑食",
        liked_proteins=["雞"],
    )

    for week in (1, 2):
        week_meals = [
            dish["name"]
            for row in result if row[0] == week
            for dish in (row[4], row[5])
        ]
        assert sum("食蔬" in name for name in week_meals) == 1
        assert all("鱸魚" not in name for name in week_meals)


def test_specific_staple_preference_is_not_forced_to_light_bento():
    chicken_bento = _dish("雞肉便當")
    chicken_low_carb = _dish("雞肉低碳")
    requests = [(1, 1, "第1週", "週一", chicken_bento, chicken_low_carb)]

    result = ensure_light_bento_coverage(
        requests,
        safe_menu=[chicken_bento, chicken_low_carb, _dish("雞肉食蔬")],
        pref_staple="飯食派",
        liked_proteins=["雞"],
    )

    assert result == requests


def test_all_eater_does_not_force_wrong_protein_or_duplicate_existing_light_bento():
    chicken_bento = _dish("雞肉便當")
    chicken_light = _dish("雞肉食蔬")
    fish_light = _dish("鱸魚食蔬")

    no_matching_light = [(1, 1, "第1週", "週一", chicken_bento, chicken_bento)]
    assert ensure_light_bento_coverage(
        no_matching_light,
        safe_menu=[chicken_bento, fish_light],
        pref_staple="都不挑食",
        liked_proteins=["雞"],
    ) == no_matching_light

    already_covered = [(1, 1, "第1週", "週一", chicken_light, chicken_bento)]
    assert ensure_light_bento_coverage(
        already_covered,
        safe_menu=[chicken_bento, chicken_light],
        pref_staple="都不挑食",
        liked_proteins=["雞"],
    ) == already_covered

    duplicate_light = [(1, 1, "第1週", "週一", chicken_light, _dish("雞胸食蔬"))]
    deduplicated = ensure_light_bento_coverage(
        duplicate_light,
        safe_menu=[chicken_bento, chicken_light, _dish("雞胸食蔬")],
        pref_staple="都不挑食",
        liked_proteins=["雞"],
    )
    assert sum("食蔬" in meal["name"] for meal in deduplicated[0][4:6]) == 1

    wrong_protein_light = [(1, 1, "第1週", "週一", fish_light, _dish("鱸魚便當"))]
    corrected = ensure_light_bento_coverage(
        wrong_protein_light,
        safe_menu=[fish_light, _dish("鱸魚便當"), chicken_light, chicken_bento],
        pref_staple="都不挑食",
        liked_proteins=["雞"],
    )
    assert sum(
        "食蔬" in meal["name"] and "雞" in meal["name"]
        for meal in corrected[0][4:6]
    ) == 1
    assert all("鱸魚食蔬" not in meal["name"] for meal in corrected[0][4:6])

    with pytest.raises(ValueError, match="non-light"):
        ensure_light_bento_coverage(
            [(1, 1, "第1週", "週一", chicken_light, _dish("雞胸食蔬"))],
            safe_menu=[chicken_light, _dish("雞胸食蔬")],
            pref_staple="都不挑食",
            liked_proteins=["雞"],
        )
