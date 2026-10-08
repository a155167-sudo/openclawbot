import json
import sqlite3
from types import SimpleNamespace
import server
from tests.test_nanjing_meal_logging_flow import _setup


def test_inconsistent_ai_rechecks_once_and_keeps_both_audits(tmp_path,monkeypatch):
    _setup(tmp_path,monkeypatch)
    payload={'food_name':'豆漿','portion_assumption':'500 ml','basis_amount':500,'basis_unit':'ml'}
    for key,n,unit in [('calories_kcal',287,'kcal'),('protein_g',113,'g'),('fat_g',5,'g'),('carbohydrate_g',49,'g')]:
        payload[key]={'estimate':n,'min':n-1,'max':n+1,'unit':unit}
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model='observed',choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(payload),refusal=None))])
    monkeypatch.setattr(server,'client',SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result=server.estimate_text_meal_nutrition({'food_name':'豆漿','amount':500,'unit':'ml'},operation_id='one-log')
    assert len(calls)==2
    assert result['assessment']['requires_correction']
    with sqlite3.connect(tmp_path/'nutrition_estimate_audit.db') as conn:
        assert conn.execute('SELECT count(*) FROM estimate_audit').fetchone()[0]==2
        assert conn.execute('SELECT count(DISTINCT operation_hash) FROM estimate_audit').fetchone()[0]==1
