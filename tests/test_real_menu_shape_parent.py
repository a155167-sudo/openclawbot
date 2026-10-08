import json,sqlite3
import pytest
import server
from tests.test_nanjing_official_menu_draft import _setup,_text,_postback,_actions

@pytest.mark.parametrize('route',['picker','amount'])
def test_real_menu_csv_shape_needs_confirmation(tmp_path,monkeypatch,route):
 db,replies=_setup(tmp_path,monkeypatch)
 with sqlite3.connect(db) as conn:
  assert conn.execute("UPDATE food_catalog SET food_id='menu_86e8576a9f27469a',source_type='label',package_unit='份',per_serving_json=? WHERE food_id='menu_chicken_box'",(json.dumps({'calories_kcal':484,'protein_g':35,'fat_g':19,'carbohydrate_g':17}),)).rowcount==1
 if route=='picker':
  server.handle_message(_text('ENTRY','我要紀錄飲食'));server.handle_message(_text('NAME','雞肉便當'))
  action=next(a['data'] for a in _actions(replies[-1]) if a.get('data','').endswith(':servings:1:meal:晚餐'))
  server.handle_postback_event(_postback(action,'REAL-PICK'))
 elif route=='amount':server.handle_postback_event(_postback('nlfood:v1:menu_86e8576a9f27469a:amount:1:serving:meal:晚餐','REAL-AMOUNT'))

 with sqlite3.connect(db) as conn:assert conn.execute('SELECT count(*) FROM food_logs').fetchone()[0]==0
 draft=replies[-1];body=json.dumps(draft.as_json_dict(),ensure_ascii=False)
 assert '一日樂食餐點' in body and '484' in body and '尚未記錄' in body
 cancel=next(a['data'] for a in _actions(draft) if a.get('data','').endswith(':cancel'))
 confirm=next(a['data'] for a in _actions(draft) if a.get('data','').endswith(':confirm'))
 if route=='picker':
  server.handle_postback_event(_postback(cancel,'CANCEL'));server.handle_postback_event(_postback(confirm,'STALE-CONFIRM'))
  expected=0
 else:
  server.handle_postback_event(_postback(confirm,'CONFIRM'))
  # Preserve the existing nutrition-discrepancy acknowledgement contract.
  # Real catalog values484/P35/F19/C17 need a second explicit acknowledgement.
  with sqlite3.connect(db) as conn:assert conn.execute('SELECT count(*) FROM food_logs').fetchone()[0]==0
  assert '數值較異常' in json.dumps(replies[-1].as_json_dict(),ensure_ascii=False)
  confirm=next(a['data'] for a in _actions(replies[-1]) if a.get('data','').endswith(':confirm'))
  server.handle_postback_event(_postback(confirm,'ACK'));server.handle_postback_event(_postback(confirm,'ACK-REPLAY'));expected=1
 with sqlite3.connect(db) as conn:
  rows=conn.execute('SELECT nutrition_snapshot_json FROM food_logs').fetchall();assert len(rows)==expected, str(replies[-2:])
  if rows:assert json.loads(rows[0][0])['protein_g']==35 and json.loads(rows[0][0])['calories_kcal']==484
  assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U-NANJING'").fetchone()[0]==3

def test_label_non_menu_or_private_not_mislabeled():
 assert not server._is_official_menu_item({'food_id':'food_label','source_type':'label','owner_user_id':'system'})
 assert not server._is_official_menu_item({'food_id':'menu_fake','source_type':'label','owner_user_id':'customer'})
