import json
import sqlite3
import pytest
import server
from tests.test_nanjing_meal_logging_flow import _setup


@pytest.mark.parametrize('amount,unit',[(400,'ml'),(150,'g')])
def test_half_portion_keeps_measured_quantity_and_nutrition_once(tmp_path,monkeypatch,amount,unit):
    db,_=_setup(tmp_path,monkeypatch)
    draft=server.create_fixed_text_meal_draft(user_id='U-NANJING',message_id='measured-half',request={
        'food_name':'測試餐點','meal_slot':'午餐','amount':amount,'unit':unit,
        'calories_kcal':180.5,'protein_g':11.3})
    server.apply_text_meal_estimate_action(user_id='U-NANJING',token=draft['token'],expected_version=draft['version'],action='portion',multiplier=.5)
    draft=server.get_text_meal_estimate_draft('U-NANJING',draft['token'])
    args=dict(user_id='U-NANJING',token=draft['token'],expected_version=draft['version'],action='confirm')
    first=server.apply_text_meal_estimate_action(**args)
    server.apply_text_meal_estimate_action(**args)
    with sqlite3.connect(db) as conn:
        rows=conn.execute('SELECT consumed_amount,consumed_unit,nutrition_snapshot_json FROM food_logs').fetchall()
    assert len(rows)==1
    assert rows[0][0]==amount*.5 and rows[0][1]==unit
    assert json.loads(rows[0][2])['protein_g']==pytest.approx(5.65)
    card=server.build_food_log_success_messages('U-NANJING',first['log_id'])[0].as_json_dict()
    assert f'{amount*.5:g} {unit}' in json.dumps(card,ensure_ascii=False)
