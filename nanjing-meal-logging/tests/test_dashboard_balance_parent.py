import json
import copy
import pytest
import server
from dashboard_balance_adapter import adapt_dashboard_data
from dashboard_flex import build_dashboard_flex, build_message, compute
from test_dashboard_balance_v53 import _data, _record, _sub, _text, _walk, _install_db


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


def test_price_annotations_hidden_without_mutating_canonical_names():
    raw = {
        'name':'顧客', 'tdee':2000, 'protein_goal':120,
        'balance_records':[dict(slot='午餐',name='雞胸150g（低鹽）（NT$120）',kcal=520,protein=42,source_type='manual')],
        'balance_sub_meals':[dict(slot='晚餐',name='鮭魚（$180）',kcal=600,protein=40)],
    }
    saved=copy.deepcopy(raw)
    data=adapt_dashboard_data(raw)
    rendered=json.dumps(build_message(data),ensure_ascii=False)
    assert 'NT$120' not in rendered and '$180' not in rendered
    assert '雞胸150g（低鹽）' in rendered and '晚餐｜鮭魚' in rendered
    assert raw==saved
    assert data['records'][0]['kcal']==520 and data['sub_meals'][0]['kcal']==600


def test_eaten_unknown_nutrition_stays_unknown_without_subscription_warning():
    data = _data(records=[_record(kcal=None, protein=10)],
                 sub_meals=[_sub("晚餐", kcal=500, protein=20, eaten=True)])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert result["ek"] is None and result["left_k"] is None
    assert "未知" in text and "熱量餘額\n2,000" not in text and "已吃 0" not in text
    assert "營養待補" not in text
    assert "部分包月餐營養未提供，餘額可能偏高" not in text


@pytest.mark.parametrize("unknown", [
    _sub("晚餐", "缺熱量", kcal=None, protein=20),
    _sub("晚餐", "缺蛋白", kcal=500, protein=None),
    _sub("晚餐", "全缺", kcal=None, protein=None),
])
def test_uneaten_subscription_with_any_unknown_field_is_wholly_excluded_and_warned(unknown):
    data = _data(records=[_record(kcal=300, protein=15)],
                 sub_meals=[_sub("午餐", "已知餐", kcal=600, protein=40), unknown])
    saved = copy.deepcopy(data)
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert (result["rk"], result["rp"], result["left_k"], result["left_p"]) == (600, 40, 1100, 65)
    assert "預留 600" in text
    assert f"晚餐｜{unknown['name']}\n營養待補" in text
    assert text.count("部分包月餐營養未提供，餘額可能偏高") == 1
    assert data == saved


def test_zero_subscription_nutrition_is_known_and_reserved_without_warning():
    data = _data(sub_meals=[_sub(kcal=0, protein=0)])
    result = compute(data)
    text = _text(build_dashboard_flex(data))
    assert (result["rk"], result["rp"]) == (0, 0)
    assert "預留 0" in text
    assert "營養待補" not in text
    assert "部分包月餐營養未提供，餘額可能偏高" not in text
