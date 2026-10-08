import pytest
from nutrition_plausibility import assess_per100
from semantic_meal_pipeline import run_semantic_meal_pipeline


def run(kcal, *, food_name='豆漿', protein=3.6, fat=1.9, carbs=0.7):
    return run_semantic_meal_pipeline(
        text='午餐豆漿500ml', user_id='isolated-user', message_id='isolated-message',
        claim_batch=lambda *_: {'allowed': True, 'batch_id': 'batch'},
        parse_semantics=lambda *_: {'intent':'meal_log','meal_slot':'午餐','items':[
            {'food_name':food_name,'amount':500,'unit':'ml','food_name_evidence':food_name,'food_state':''}]},
        find_reference_nutrition=lambda _: None,
        estimate_per_100=lambda *_:[{'item_id':'item-0','food_name':food_name,'basis_amount':100,'basis_unit':'ml',
            'nutrition':{'calories_kcal':kcal,'protein_g':protein,'fat_g':fat,'carbohydrate_g':carbs}}])


def test_mobile_soy_twelve_kcal_per100_must_clarify_without_displaying_bad_values():
    result=run(12, protein=1, fat=0.4, carbs=1.2)
    assert result['status']=='clarification'
    assert result['items']==[]
    assert '確認' in ''.join(result['clarifications'])
    assert not result['writes_food_log']


@pytest.mark.parametrize('kcal',[25,33,60])
def test_allowed_soy_energy_boundaries_still_form_draft(kcal):
    result=run(kcal)
    assert result['status']=='draft'
    assert result['items'][0]['nutrition']['calories_kcal']==kcal*5


@pytest.mark.parametrize('kcal',[24.999,60.001,0,900])
def test_outside_soy_energy_boundaries_never_forms_actionable_draft(kcal):
    assert run(kcal)['status']=='clarification'


@pytest.mark.parametrize('bad', [None, float('nan'), -1])
def test_missing_nonfinite_or_negative_per100_values_require_confirmation(bad):
    nutrition = {'calories_kcal': 35, 'protein_g': 3.6, 'fat_g': 1.9, 'carbohydrate_g': 0.7}
    nutrition['protein_g'] = bad
    assert assess_per100({'food_name': '豆漿', 'unit': 'ml'}, nutrition)['status'] == 'requires_confirmation'


def test_physically_implausible_pfc_per100_requires_confirmation():
    result = run(35, protein=90, fat=90, carbs=90)
    assert result['status'] == 'clarification'
    assert result['items'] == []


def test_unknown_food_category_is_conservatively_withheld():
    result = run(35, food_name='神秘配方飲品')
    assert result['status'] == 'clarification'
    assert result['items'] == []
    assert '類別' in ''.join(result['clarifications'])


def test_multi_item_any_failed_guard_withholds_entire_draft():
    result = run_semantic_meal_pipeline(
        text='午餐豆漿500ml與牛奶200ml', user_id='u', message_id='m',
        claim_batch=lambda *_: {'allowed': True, 'batch_id': 'b'},
        parse_semantics=lambda *_: {'intent': 'meal_log', 'meal_slot': '午餐', 'items': [
            {'food_name': '豆漿', 'amount': 500, 'unit': 'ml'},
            {'food_name': '牛奶', 'amount': 200, 'unit': 'ml'},
        ]},
        find_reference_nutrition=lambda _: None,
        estimate_per_100=lambda misses, _: [
            {'item_id': misses[0]['item_id'], 'food_name': '豆漿', 'basis_amount': 100,
             'basis_unit': 'ml', 'nutrition': {'calories_kcal': 35, 'protein_g': 3.6,
                                               'fat_g': 1.9, 'carbohydrate_g': 0.7}},
            {'item_id': misses[1]['item_id'], 'food_name': '牛奶', 'basis_amount': 100,
             'basis_unit': 'ml', 'nutrition': {'calories_kcal': 900, 'protein_g': 3,
                                               'fat_g': 3, 'carbohydrate_g': 5}},
        ],
    )
    assert result['status'] == 'clarification'
    assert result['items'] == []
