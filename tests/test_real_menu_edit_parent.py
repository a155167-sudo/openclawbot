import json,sqlite3
import server
from tests.test_nanjing_official_menu_draft import _setup,_postback,_actions

def test_real_catalog_amount_edit_preserves_source_values_and_confirmation(tmp_path,monkeypatch):
 db,replies=_setup(tmp_path,monkeypatch)
 with sqlite3.connect(db) as conn:
  assert conn.execute("UPDATE food_catalog SET food_id='menu_actual',source_type='label',package_unit='份',per_serving_json=? WHERE food_id='menu_chicken_box'",(json.dumps({'calories_kcal':484,'protein_g':35,'fat_g':19,'carbohydrate_g':17}),)).rowcount==1
 server.handle_postback_event(_postback('nlfood:v1:menu_actual:servings:1:meal:晚餐','SELECT'))
 data=next(a['data'] for a in _actions(replies[-1]) if a.get('data','').endswith(':confirm')).split(':')
 revised=server.apply_text_meal_custom_input(user_id='U-NANJING',token=data[2],expected_version=int(data[3]),mode='amount',text='1.5 份')
 assert server._confirmed_text_meal_source_label(revised['estimate'])=='一日樂食餐點'
 with sqlite3.connect(db) as conn:assert conn.execute('SELECT count(*) FROM food_logs').fetchone()[0]==0
 card=server.build_text_meal_estimate_flex(revised)
 for attempt in range(2):
  confirm=next(a['data'] for a in _actions(card) if a.get('data','').endswith(':confirm'))
  server.handle_postback_event(_postback(confirm,'EDIT-CONFIRM-'+str(attempt)))
  with sqlite3.connect(db) as conn:
   rows=conn.execute('SELECT consumed_amount,consumed_unit,nutrition_snapshot_json FROM food_logs').fetchall()
  if rows:break
  card=replies[-1]
 assert len(rows)==1 and rows[0][:2]==(1.5,'serving')
 values=json.loads(rows[0][2])
 for k,v in {'calories_kcal':484,'protein_g':35,'fat_g':19,'carbohydrate_g':17}.items():assert values[k]==v*1.5
 server.handle_postback_event(_postback(confirm,'EDIT-REPLAY'))
 assert '一日樂食餐點' in json.dumps(replies[-1][0].as_json_dict(),ensure_ascii=False)
 with sqlite3.connect(db) as conn:assert conn.execute('SELECT count(*) FROM food_logs').fetchone()[0]==1
