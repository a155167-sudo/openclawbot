import json,sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import server
import dashboard_flex
from tests.test_nanjing_meal_logging_flow import _setup,_postback,_postback_actions


def test_registered_confirmation_replay_keeps_original_source_without_duplicate_log(tmp_path,monkeypatch):
 db,replies=_setup(tmp_path,monkeypatch)
 d=server.create_fixed_text_meal_draft(user_id='U-NANJING',message_id='SOURCE-REPLAY',method='official_reference',request={'food_name':'白飯','amount':200,'unit':'g','meal_slot':'晚餐','calories_kcal':366,'protein_g':6.2,'fat_g':0.6,'carbohydrate_g':82,'source':{'publisher':'衛福部','card_note':'衛福部資料'}})
 d=server.save_text_meal_draft_from_liff(user_id='U-NANJING',token=d['token'],expected_version=d['version'],amount=200,unit='g',meal_slot='晚餐',nutrition={'calories_kcal':366,'protein_g':6.2,'fat_g':0.6,'carbohydrate_g':82})['draft']
 command=next(a for a in _postback_actions(server.build_text_meal_estimate_flex(d)) if a.endswith(':confirm'))
 for n in ['FIRST','REPLAY']:
  server.handle_postback_event(_postback(command,n))
  payload=replies[-1]; payload=payload if isinstance(payload,list) else [payload]
  text=json.dumps([x.as_json_dict() for x in payload],ensure_ascii=False)
  assert '衛福部資料・顧客修改' in text
 with sqlite3.connect(db) as conn:
  assert conn.execute('SELECT count(*) FROM food_logs').fetchone()[0]==1


def test_real_renderer_boundaries_preserve_all_numeric_values():
 data={'date_label':'10/8','user_name':'隔離測試','target_kcal':2000,'target_protein':120,'records':[{'kcal':2000,'protein':80,'slot':'晚餐','name':'測試餐'}],'sub_meals':[]}
 original=None
 for hour,minute,expect in [(16,59,'蛋白質還差'),(17,0,'可依食慾適量補充'),(20,59,'可依食慾適量補充'),(21,0,None),(23,59,None)]:
  now=datetime(2026,10,8,hour,minute,tzinfo=ZoneInfo('Asia/Taipei'))
  result=dashboard_flex.compute(data,now=now)
  numbers={k:v for k,v in result.items() if k not in ['hint','show_hint']}
  if original is None:original=numbers
  assert numbers==original
  text=json.dumps(dashboard_flex.build_dashboard_flex(data,now=now),ensure_ascii=False)
  assert '蛋白質餘額' in text
  if expect:assert expect in text
  else:
   assert '蛋白質还差' not in text and '蛋白質還差' not in text
   assert '可依食慾適量補充' not in text
