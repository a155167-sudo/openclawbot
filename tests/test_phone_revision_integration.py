import json,sqlite3
import server
from tests.test_nanjing_meal_logging_flow import _setup,_text_event,_postback,_postback_actions


def test_registered_soy_reference_discloses_real_publisher_and_no_ai_charge(tmp_path,monkeypatch):
    db,replies=_setup(tmp_path,monkeypatch)
    def forbidden(*a,**kw):raise AssertionError('reference flow must not invoke AI')
    monkeypatch.setattr(server,'estimate_text_meal_nutrition',forbidden)
    server.handle_message(_text_event('ENTER','記一餐'))
    assert '也可以直接傳餐點照片' in replies[-1].text
    server.handle_message(_text_event('FOOD','無糖豆漿 500ml'))
    text=json.dumps(replies[-1].as_json_dict(),ensure_ascii=False)
    assert 'Silk' in text
    assert 'TFDA' not in text
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
        assert c.execute('SELECT remaining_chat_quota FROM usage WHERE user_id=?',('U-NANJING',)).fetchone()[0]==3
        e=json.loads(c.execute('SELECT estimate_json FROM pending_text_meal_estimates').fetchone()[0])
    assert e['calories_kcal']['estimate']==90*500/240
    assert e['protein_g']['estimate']==8*500/240


def test_customer_revision_real_route_replay_is_exactly_once(tmp_path,monkeypatch):
    db,replies=_setup(tmp_path,monkeypatch)
    server.handle_message(_text_event('ENTER','記一餐'))
    server.handle_message(_text_event('FOOD','無糖豆漿 500ml'))
    control=next(x['action']['data'] for x in replies[-1].as_json_dict()['contents']['footer']['contents'] if x['action']['label']=='自己輸入數值')
    server.handle_postback_event(_postback(control,'EDIT'))
    server.handle_message(_text_event('VALUES','180 大卡 16 克'))
    card=replies[-1];assert '顧客修改' in json.dumps(card.as_json_dict(),ensure_ascii=False)
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
        assert original['estimate_metadata']['provenance']['original_estimate']['calories_kcal']['estimate']==90*500/240
        assert c.execute('SELECT remaining_chat_quota FROM usage WHERE user_id=?',('U-NANJING',)).fetchone()[0]==3
