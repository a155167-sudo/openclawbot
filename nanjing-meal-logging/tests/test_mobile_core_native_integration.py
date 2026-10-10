import json,sqlite3
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('name,amount,unit,kcal,label',[
 ('無糖豆漿',500,'ml',175,'衛福部資料・以 1ml≈1g 換算'),
 ('白飯',150,'g',274.5,'衛福部資料'),
 ('蒸地瓜',150,'g',196.5,'日本食品成分表參考'),
])
def test_actual_registered_route_fixed_card_and_one_private_safe_log(tmp_path,monkeypatch,capsys,name,amount,unit,kcal,label):
 import server
 monkeypatch.setattr(server,'DB_DIR',str(tmp_path));monkeypatch.setattr(server,'DB_PATH',str(tmp_path/'native.db'))
 server.init_db()
 uid='U-MOBILE-PRIVATE-DO-NOT-LOG'
 with sqlite3.connect(server.DB_PATH) as conn:
  conn.execute('INSERT INTO usage(user_id,remaining_chat_quota,remaining_meals,last_date,status,expiry_date,daily_chat_limit) VALUES (?,9,10,?,\'vip\',\'2099-12-31\',9)',(uid,server.tw_today().isoformat()))
 class Completions:
  calls=[]
  def create(self,**kwargs):
   self.calls.append(kwargs)
   assert len(self.calls)==1,'No second nutrition AI call allowed'
   payload={'intent':'meal_log','meal_slot':'午餐','items':[{'food_name':name,'amount':amount,'unit':unit}]}
   return SimpleNamespace(id='isolated-test',model='fake-parser',choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(payload,ensure_ascii=False),refusal=None))])
 completion=Completions();completion.calls=[]
 monkeypatch.setattr(server,'client',SimpleNamespace(chat=SimpleNamespace(completions=completion)))
 monkeypatch.setattr(server,'classify_service_scope_text',lambda *_:'service')
 monkeypatch.setattr(server,'_refresh_health_check_after_food_log',lambda *_a,**_k:None)
 replies=[]
 monkeypatch.setattr(server.line_bot_api,'reply_message',lambda token,reply:replies.append(reply))
 event=SimpleNamespace(message=SimpleNamespace(id='NATIVE-CORE-1',text=f'午餐我喝了{name}{amount}{unit}'),source=SimpleNamespace(user_id=uid,type='user'),reply_token='private-reply-token',webhook_event_id='private-event-id')
 server.processed_messages.clear()
 capsys.readouterr()
 server.handle_message(event)
 rendered=json.dumps(replies[-1].as_json_dict(),ensure_ascii=False)
 assert '確認後才會寫入' in rendered and '實際入帳' not in rendered
 assert label in rendered
 if name=='無糖豆漿': assert 'Silk' not in rendered and '1ml≈1g' in rendered
 if name=='蒸地瓜': assert '日本食品成分表參考' in rendered and '前處理' not in rendered
 with sqlite3.connect(server.DB_PATH) as conn:
  rows=conn.execute('SELECT estimate_json FROM pending_text_meal_estimates').fetchall()
  assert len(rows)==1
  estimate=json.loads(rows[0][0]);assert estimate['calories_kcal']['estimate']==kcal
  assert conn.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
 log=[s for s in capsys.readouterr().out.splitlines() if s.startswith('NUTRITION_DRAFT ')]
 assert len(log)==1
 assert uid not in log[0] and 'private-reply-token' not in log[0]
 d=json.loads(log[0].split(' ',1)[1])['items'][0]
 assert d['route']=='reference' and d['matched_item']
 assert d['factor']==amount/100 and d['per100']['calories_kcal']==kcal/(amount/100)
 server.processed_messages.clear()
 server.handle_message(event)
 assert not [s for s in capsys.readouterr().out.splitlines() if s.startswith('NUTRITION_DRAFT ')]
