import json,sqlite3
from types import SimpleNamespace
import pytest
import server
from nutrition_estimate_audit import record_estimate
from tests.test_nanjing_meal_logging_flow import _setup


def test_provider_exception_never_persists_echoed_name_or_request(tmp_path,monkeypatch):
    _setup(tmp_path,monkeypatch)
    def create(**kwargs):
        raise RuntimeError('provider rejected request for 王小明 0912345678 private meal')
    monkeypatch.setattr(server,'client',SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(server.TextMealProviderError):
        server.estimate_text_meal_nutrition({'food_name':'豆漿','amount':500,'unit':'ml'},operation_id='privacy')
    with sqlite3.connect(tmp_path/'nutrition_estimate_audit.db') as conn:
        text=conn.execute('SELECT response_json FROM estimate_audit').fetchone()[0]
    assert json.loads(text)=={'error':'RuntimeError'}


def test_malformed_response_does_not_persist_unstructured_private_text(tmp_path):
    db=tmp_path/'audit.db'
    record_estimate(db,model='fake',response='broken 王小明 0912345678 request: ...')
    with sqlite3.connect(db) as conn:
        text,digest=conn.execute('SELECT response_json,response_hash FROM estimate_audit').fetchone()
    assert json.loads(text)=={'error':'invalid_json'}
    assert len(digest)==64


@pytest.mark.parametrize('payload',[{'error':'provider rejected 王小明','message':'王小明 private request'},'王小明 private request'])
def test_non_nutrition_error_text_is_not_an_audit_field(tmp_path,payload):
    db=tmp_path/'audit.db'
    record_estimate(db,model='fake',response=json.dumps(payload,ensure_ascii=False))
    with sqlite3.connect(db) as conn:
        text=conn.execute('SELECT response_json FROM estimate_audit').fetchone()[0]
    assert '王小明' not in text and 'private request' not in text
