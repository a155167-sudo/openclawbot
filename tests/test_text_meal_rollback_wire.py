import re
import sqlite3
import pytest
import server
from tests.test_nanjing_meal_logging_flow import _setup,_estimate,_postback,_postback_actions

OLD_WIRE=re.compile(r'tmest:v(?:1|2):([0-9a-f]{24}|[0-9a-f]{32}):(\d+):(confirm|cancel|portion:(0\.5|1(?:\.0)?|1\.5|2(?:\.0)?))')

@pytest.mark.parametrize('fixed',[True,False])
def test_new_drafts_cannot_be_confirmed_through_legacy_wire(tmp_path,monkeypatch,fixed):
    db,replies=_setup(tmp_path,monkeypatch)
    request={'food_name':'測試豆漿','amount':500,'unit':'ml','meal_slot':'午餐'}
    if fixed:
        request.update(calories_kcal=165,protein_g=16)
        draft=server.create_fixed_text_meal_draft(user_id='U-NANJING',message_id='wire',request=request)
    else:
        monkeypatch.setattr(server,'estimate_text_meal_nutrition',lambda *a,**k:_estimate())
        draft=server.create_text_meal_estimate_draft(user_id='U-NANJING',message_id='wire',request=request)
    assert len(draft['token'])==40
    actions=_postback_actions(server.build_text_meal_estimate_flex(draft))
    assert actions and all(x.startswith('tmest:v3:') for x in actions)
    assert all(not OLD_WIRE.fullmatch(x) for x in actions)
    # Even replacing only the wire version cannot make this token valid to old runtime.
    assert all(not OLD_WIRE.fullmatch(x.replace('tmest:v3:',prefix,1)) for x in actions for prefix in ('tmest:v1:','tmest:v2:'))
    confirm=next(x for x in actions if x.endswith(':confirm'))
    server.handle_postback_event(_postback(confirm,'wire-confirm'))
    server.handle_postback_event(_postback(confirm,'wire-confirm'))
    with sqlite3.connect(db) as conn:
        rows=conn.execute('SELECT consumed_amount,consumed_unit FROM food_logs').fetchall()
    assert rows==[(500.0,'ml')]
