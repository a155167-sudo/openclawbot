import json
import pytest
import server
from dashboard_balance_adapter import adapt_dashboard_data
from dashboard_flex import build_dashboard_flex, build_message
from test_dashboard_balance_v53 import _data, _record, _sub, _walk, _install_db


def test_text_ai_estimate_is_disclosed_by_real_dashboard(tmp_path, monkeypatch):
    uid = 'U-AI-TEXT-BALANCE'
    _install_db(tmp_path, monkeypatch, uid, [dict(name='AI餐', slot='點心', kcal=150, protein=9, source_type='ai_text_estimate')])
    raw = server.get_dashboard_data(uid, scope='home')
    assert raw['balance_records'][0]['ai_estimated'] is True
    assert '含 AI 估算紀錄' in json.dumps(server.build_dashboard_flex(uid).as_json_dict(), ensure_ascii=False)


@pytest.mark.parametrize('tk,tp,kcal,protein', [(0,120,None,10),(2000,120,600,None),(2000,0,600,20)])
def test_complete_message_unknown_safe_and_unset_protein_not_fake_zero(tk,tp,kcal,protein):
    msg = build_message(_data(target_kcal=tk, target_protein=tp, records=[_record(kcal=kcal,protein=protein)]))
    assert msg['type'] == 'flex'
    if tp == 0:
        assert '蛋白質餘額 0' not in msg['altText']


def test_large_numeric_texts_are_complete_and_wrap():
    bubble=build_dashboard_flex(_data(target_kcal=2000000,target_protein=100000,records=[_record(kcal=1234567.5,protein=98765.3)]))
    numeric=[n for n in _walk(bubble) if n.get('type')=='text' and any(x.isdigit() for x in n.get('text',''))]
    assert numeric
    assert all(n.get('wrap') is True or
               (n.get('wrap') is False and n.get('maxLines') == 1 and
                (n.get('size') == 'xxs' or n.get('size', '').endswith('px'))) for n in numeric)


def test_v52_idless_log_matches_identified_schedule_without_double_count(tmp_path, monkeypatch):
    uid='U-V52-MIGRATION'
    _install_db(tmp_path,monkeypatch,uid,[dict(name='舊包月餐',slot='午餐',kcal=650,protein=42,source_type='planned_meal')],dispatch_id='existing-dispatch')
    data=adapt_dashboard_data(server.get_dashboard_data(uid,scope='home'))
    assert [m['eaten'] for m in data['sub_meals']]==[True,False]
