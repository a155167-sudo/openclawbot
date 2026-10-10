import json
import sqlite3
from types import SimpleNamespace
import pytest
import server
from tests.test_nanjing_meal_logging_flow import _setup


def _client(monkeypatch, content):
    response=SimpleNamespace(model='observed-vision-model',choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(server,'client',SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:response))))


def test_photo_adjustment_retains_response_nutrition_and_model_not_identity(tmp_path,monkeypatch):
    _setup(tmp_path,monkeypatch)
    monkeypatch.setattr(server,'_read_valid_nutrition_image',lambda _ref:b'fake-offline-image')
    monkeypatch.setattr(server,'_validate_image_bytes',lambda _bytes:'.png')
    payload={'image_type':'food_photo','ai_estimate':{'items':[{'name':'豆漿','calories_kcal':180.5,'protein_g':11.3}],
             'calories_kcal':{'estimate':180.5,'min':170,'max':190},'protein_g':{'estimate':11.3,'min':10,'max':12}},
             'user_name':'不應保存的姓名'}
    _client(monkeypatch,json.dumps(payload,ensure_ascii=False))
    result=server._estimate_adjusted_meal_photo('private-image-ref',{},'半份')
    path=tmp_path/'nutrition_estimate_audit.db'
    assert path.exists(), 'photo adjustment was not audited'
    with sqlite3.connect(path) as conn:
        model,text,trace=conn.execute('SELECT model,response_json,trace_id FROM estimate_audit').fetchone()
    assert model=='observed-vision-model'
    assert json.loads(text)['ai_estimate']['protein_g']['estimate']==11.3
    assert '不應保存的姓名' not in text
    assert trace
    assert set(result['ai_estimate']['provenance'])=={'provider','model','method','nutrition_basis'}


def test_legacy_nutrition_model_reply_is_audited_without_full_chat(tmp_path,monkeypatch):
    _setup(tmp_path,monkeypatch)
    _client(monkeypatch,'私人閒聊不保存。[LOG_NUTRITION: CAL=180.5, PRO=11.3, NAME=豆漿]')
    server.get_ai_response_with_memory('U-NANJING','請用一般估算記錄 早餐 豆漿','legacy-audit')
    path=tmp_path/'nutrition_estimate_audit.db'
    assert path.exists(), 'legacy nutrition estimate not audited'
    with sqlite3.connect(path) as conn:
        text=conn.execute('SELECT response_json FROM estimate_audit').fetchone()[0]
    assert '11.3' in text
    assert 'CAL=180.5' in text
    assert '私人閒聊' not in text
