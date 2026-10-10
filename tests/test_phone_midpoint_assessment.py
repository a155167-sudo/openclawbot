import json,sqlite3
import server
from tests.test_phone_meal_revision import _ai_draft


def test_midpoint_inconsistency_requires_warning_before_any_ledger_write(tmp_path,monkeypatch):
    db,draft=_ai_draft(tmp_path,monkeypatch)
    # Provider point estimates are internally consistent (200 kcal), but
    # independently ranged nutrients have an inconsistent midpoint vector.
    e=draft['estimate']
    e.update(calories_kcal={'estimate':200,'min':0,'max':200,'unit':'kcal'},
             protein_g={'estimate':10,'min':10,'max':50,'unit':'g'},
             fat_g={'estimate':4,'min':4,'max':4,'unit':'g'},
             carbohydrate_g={'estimate':31,'min':31,'max':31,'unit':'g'},
             assessment={'requires_correction':False,'status':'consistent'})
    with sqlite3.connect(db) as c:
        c.execute('UPDATE pending_text_meal_estimates SET estimate_json=? WHERE token=?',(json.dumps(e),draft['token']))
    result=server.apply_text_meal_estimate_action(user_id='U1',token=draft['token'],expected_version=1,action='confirm')
    assert result['kind']=='preview'
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
    fresh=result['draft']
    rendered=json.dumps(server.build_text_meal_estimate_flex(fresh).as_json_dict(),ensure_ascii=False)
    assert '數值較異常' in rendered
    assert '若數值正確' in rendered and '再按' in rendered
    assert '按第一次確認只會解除警示' not in rendered
    confirmed=server.apply_text_meal_estimate_action(user_id='U1',token=fresh['token'],expected_version=fresh['version'],action='confirm')
    assert confirmed['kind']=='confirmed'
