import json,sqlite3
from types import SimpleNamespace
import server
from tests.test_nanjing_meal_logging_flow import _setup,_text_event,_postback,_postback_actions


def _install_soy_provider(monkeypatch):
    calls=[]
    payload={
        'food_name':'無糖豆漿','portion_assumption':'完整 500 ml',
        'basis_amount':500,'basis_unit':'ml',
        'calories_kcal':{'estimate':165,'min':150,'max':180,'unit':'kcal'},
        'protein_g':{'estimate':16,'min':14,'max':18,'unit':'g'},
        'fat_g':{'estimate':7,'min':6,'max':8,'unit':'g'},
        'carbohydrate_g':{'estimate':9,'min':7,'max':11,'unit':'g'},
    }
    response=SimpleNamespace(
        model='round2-fake-model',
        choices=[SimpleNamespace(
            finish_reason='stop',
            message=SimpleNamespace(content=json.dumps(payload,ensure_ascii=False),refusal=None),
        )],
    )
    monkeypatch.setattr(server,'client',SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw:calls.append(kw) or response))))
    return calls


def test_registered_soy_reference_discloses_real_publisher_and_no_ai_charge(tmp_path,monkeypatch):
    db,replies=_setup(tmp_path,monkeypatch)
    calls=_install_soy_provider(monkeypatch)
    server.handle_message(_text_event('FOOD','午餐喝了無糖豆漿500ml'))
    text=json.dumps(replies[-1].as_json_dict(),ensure_ascii=False)
    assert '營養估算草稿（尚未記錄）' in text
    assert 'AI估算' in text and 'Silk' not in text
    assert len(calls)==1
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
        assert c.execute('SELECT remaining_chat_quota FROM usage WHERE user_id=?',('U-NANJING',)).fetchone()[0]==2
        request_json,estimate_json=c.execute(
            'SELECT request_json,estimate_json FROM pending_text_meal_estimates'
        ).fetchone()
    request,e=json.loads(request_json),json.loads(estimate_json)
    assert request=={'food_name':'無糖豆漿','amount':500.0,'unit':'ml','meal_slot':'午餐'}
    assert e['basis_amount']==500 and e['basis_unit']=='ml'
    assert e['provenance']['method']=='text_meal_estimate'


def test_customer_revision_real_route_replay_is_exactly_once(tmp_path,monkeypatch):
    db,replies=_setup(tmp_path,monkeypatch)
    calls=_install_soy_provider(monkeypatch)
    server.handle_message(_text_event('FOOD','午餐喝了無糖豆漿500ml'))
    with sqlite3.connect(db) as c:
        token,version=c.execute(
            'SELECT token,version FROM pending_text_meal_estimates'
        ).fetchone()
    saved=server.save_text_meal_draft_from_liff(
        user_id='U-NANJING',token=token,expected_version=version,
        amount=500,unit='ml',meal_slot='午餐',
        nutrition={'calories_kcal':180,'protein_g':16,'fat_g':None,'carbohydrate_g':None},
    )
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM meal_draft_return_receipts').fetchone()[0]==1
        assert c.execute('SELECT remaining_chat_quota FROM usage WHERE user_id=?',('U-NANJING',)).fetchone()[0]==2
    returned_event=_text_event('RETURN',saved['return_command'])
    # A real LINE one-to-one message includes source.type='user'. Missing or
    # group sources must remain rejected by the new privacy boundary.
    returned_event.source.type='user'
    reply_count=len(replies)
    server.handle_message(returned_event)
    assert len(replies)==reply_count+1
    card=replies[-1]
    rendered=json.dumps(card.as_json_dict(),ensure_ascii=False)
    assert '顧客修改' in rendered and '500 ml' in rendered
    confirm=next(a for a in _postback_actions(card) if a.endswith(':confirm'))
    server.handle_postback_event(_postback(confirm,'CONFIRM'))
    assert isinstance(replies[-1],list) and len(replies[-1])==2
    server.handle_postback_event(_postback(confirm,'CONFIRM-REPLAY'))
    with sqlite3.connect(db) as c:
        rows=c.execute('SELECT nutrition_snapshot_json,original_nutrition_snapshot_json,consumed_amount FROM food_logs').fetchall()
        assert len(rows)==1
        n=json.loads(rows[0][0]);original=json.loads(rows[0][1])
        assert n['calories_kcal']==180 and n['protein_g']==16
        assert n['fat_g'] is None and n['carbohydrate_g'] is None
        assert rows[0][2]==500
        assert original['estimate_metadata']['provenance']['original_estimate']['calories_kcal']['estimate']==165
        assert c.execute('SELECT remaining_chat_quota FROM usage WHERE user_id=?',('U-NANJING',)).fetchone()[0]==2
    assert len(calls)==1
