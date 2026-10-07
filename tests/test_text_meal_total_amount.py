import pytest
import server


def _est(assumption):
    return {"portion_assumption": assumption}


@pytest.mark.parametrize("assumption", ["500ml", "500 ml", "約500毫升（500ml）", "５００ml", "總量500ml無糖豆漿"])
def test_total_amount_is_accepted_when_provider_states_the_same_total(assumption):
    server._require_total_amount_in_assumption(
        {"food_name": "無糖豆漿", "amount": 500.0, "unit": "ml", "meal_slot": ""}, _est(assumption)
    )


@pytest.mark.parametrize("assumption", ["100ml", "每100ml", "1杯", ""])
def test_per_100_or_missing_total_is_rejected(assumption):
    with pytest.raises(ValueError, match="總量不一致"):
        server._require_total_amount_in_assumption(
            {"food_name": "無糖豆漿", "amount": 500.0, "unit": "ml", "meal_slot": ""}, _est(assumption)
        )


def test_non_metric_units_are_not_checked():
    server._require_total_amount_in_assumption(
        {"food_name": "蘋果", "amount": 1.0, "unit": "serving", "meal_slot": ""}, _est("1顆中型")
    )


def test_prompt_states_amount_is_the_total():
    import inspect
    source = inspect.getsource(server.estimate_text_meal_nutrition)
    assert "總量的合計" in source and "portion_assumption必須寫出" in source
