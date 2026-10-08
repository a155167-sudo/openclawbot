import pytest
from nutrition_reference import find_reference_nutrition


@pytest.mark.parametrize('name,unit', [('無糖豆漿','ml'),('白飯','g'),('蒸地瓜','g')])
def test_three_core_foods_have_fixed_compatible_references(name,unit):
    request={'food_name':name,'amount':150,'unit':unit}
    values=[find_reference_nutrition(request) for _ in range(5)]
    assert all(v and v.get('status')=='matched' for v in values)
    assert all(v['basis_unit']==unit for v in values)
    assert all(v==values[0] for v in values)
    assert all(values[0]['nutrition'][key] is not None for key in ('calories_kcal','protein_g','fat_g','carbohydrate_g'))
    source=values[0]['source']
    assert source.get('source_url')
    if name=='蒸地瓜':
        assert '蒸' in source['state']
        assert source.get('publisher') != 'TFDA'  # TFDA pinned core list has raw, not steamed.
    if name=='無糖豆漿':
        assert source.get('publisher') == 'TFDA'
        assert source['density_g_per_ml'] == 1.0  # Explicit user-approved approximation.


def test_unit_specific_rows_are_not_ambiguous_and_never_convert(monkeypatch):
    import nutrition_reference as module
    def row(unit, value):
        return {'name':'test drink','aliases':['test drink'],'basis_amount':100,'basis_unit':unit,
                'state':'','nutrition':{'calories_kcal':value}, 'reference_label':'test-only'}
    monkeypatch.setattr(module,'_document',lambda:{'version':'test','items':[row('g',35),row('ml',40)]})
    assert module.find_reference_nutrition({'food_name':'test drink','amount':500,'unit':'ml'})['nutrition']['calories_kcal']==40
    assert module.find_reference_nutrition({'food_name':'test drink','amount':500,'unit':'g'})['nutrition']['calories_kcal']==35
    assert module.reference_capability('test drink','ml')['status']=='exact'
    monkeypatch.setattr(module,'_document',lambda:{'version':'test','items':[row('g',35),row('g',40)]})
    assert module.find_reference_nutrition({'food_name':'test drink','amount':500,'unit':'g'})['status']=='ambiguous_reference'


def test_approved_tfda_density_reference_is_not_labeled_manufacturer_or_ai():
    from semantic_meal_pipeline import run_semantic_meal_pipeline
    def no_estimate(*args):
        raise AssertionError('core nutrition must never be re-estimated by AI')
    result=run_semantic_meal_pipeline(
        text='午餐無糖豆漿500ml',user_id='test',message_id='test',
        claim_batch=lambda *_:{'allowed':True,'batch_id':'test'},
        parse_semantics=lambda *_:{'intent':'meal_log','items':[{'food_name':'無糖豆漿','amount':500,'unit':'ml'}]},
        find_reference_nutrition=find_reference_nutrition,estimate_per_100=no_estimate)
    item=result['items'][0]
    assert item['source_label']=='衛福部資料'
    assert item['source']['card_note']=='衛福部資料・以 1ml≈1g 換算'
    assert item['nutrition']['calories_kcal']==175


@pytest.mark.parametrize('state',['生','熟，帶皮','水煮','烤'])
def test_incompatible_core_state_clarifies_instead_of_random_ai(state):
    from semantic_meal_pipeline import run_semantic_meal_pipeline
    calls=[]
    result=run_semantic_meal_pipeline(
        text='地瓜150g',user_id='test',message_id='test',
        claim_batch=lambda *_:{'allowed':True,'batch_id':'test'},
        parse_semantics=lambda *_:{'intent':'meal_log','items':[{'food_name':'地瓜','food_state':state,'amount':150,'unit':'g'}]},
        find_reference_nutrition=find_reference_nutrition,
        estimate_per_100=lambda *args:calls.append(args) or [{'item_id':'item-0','food_name':'地瓜','basis_amount':100,'basis_unit':'g','nutrition':{'calories_kcal':131,'protein_g':1.2,'fat_g':0.2,'carbohydrate_g':31.9}}])
    assert not calls
    assert result['status']=='clarification'
    assert result['items']==[]


def test_soy_grams_preserve_original_tfda_reference():
    r=find_reference_nutrition({'food_name':'無糖豆漿','amount':100,'unit':'g'})
    assert r['source']['food_code']=='H1150201'
    assert r['nutrition']=={'calories_kcal':35,'protein_g':3.6,'fat_g':1.9,'carbohydrate_g':0.7}
