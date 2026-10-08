import json
import pytest
from nutrition_reference import resolve_reference,find_reference_nutrition,DATA_PATH

def test_approved_soy_500ml_and_original_100g_provenance():
 r=resolve_reference({'food_name':'無糖豆漿','amount':500,'unit':'ml'})
 assert r['nutrition']=={'calories_kcal':175,'protein_g':18,'fat_g':9.5,'carbohydrate_g':3.5}
 assert r['source']['publisher']=='TFDA'
 assert r['source']['source_row_id']=='H1150201'
 assert r['source']['density_g_per_ml']==1.0
 assert r['source']['source_original_basis']=='每100克含量'
 assert r['source']['card_note']=='衛福部資料・以 1ml≈1g 換算'
 assert 'silk' not in DATA_PATH.read_text().lower()

@pytest.mark.parametrize('food_request',[
 {'food_name':'烤地瓜','amount':150,'unit':'g'},
 {'food_name':'地瓜','food_state':'烤','amount':150,'unit':'g'},
 {'food_name':'蒸地瓜','food_name_evidence':'烤地瓜150g','amount':150,'unit':'g'},
])
def test_roasted_cannot_use_steamed(food_request):
 r=find_reference_nutrition(food_request)
 assert not r or r['status']!='matched'

def test_approved_steamed_and_rice():
 r=resolve_reference({'food_name':'蒸地瓜','amount':150,'unit':'g'})
 assert r['nutrition']['calories_kcal']==196.5
 assert r['source']['source_label']=='日本食品成分表參考'
 assert resolve_reference({'food_name':'白飯','amount':150,'unit':'g'})['nutrition']['calories_kcal']==274.5

def test_exact_small_print_on_legacy_and_semantic_cards():
 import server
 source={'card_note':'衛福部資料・以 1ml≈1g 換算'}
 for provenance in [{'source':source},{'items':[{'source':source,'source_label':'官方資料'}]}]:
  notes=server._text_meal_provenance_disclosures({'provenance':provenance})
  assert any(n['text']==source['card_note'] and n['size']=='xxs' for n in notes)
