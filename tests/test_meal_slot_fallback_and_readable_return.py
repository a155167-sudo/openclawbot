import copy,json,sqlite3,re
from datetime import datetime
from types import SimpleNamespace
import pytest
import server
from tests.test_nanjing_meal_logging_flow import _setup
from tests.test_meal_confirmation_card_and_revision_warning import _fixed_revision_draft

@pytest.mark.parametrize('text,expected',[('餐我吃了白飯150克','晚餐'),('早餐我吃了白飯150克','早餐')])
def test_registered_semantic_entry_defaults_missing_slot_to_taipei_time(tmp_path,monkeypatch,text,expected):
 db,replies=_setup(tmp_path,monkeypatch)
 monkeypatch.setattr(server,'tw_now',lambda:datetime(2026,10,8,18,47,35,tzinfo=server.TW_TZ))
 class C:
  calls=0
  def create(self,**kw):
   self.calls+=1
   assert self.calls==1
   p={'intent':'meal_log','meal_slot':'','items':[{'food_name':'白飯','amount':150,'unit':'g'}]}
   return SimpleNamespace(id='fake-transport',model='isolated-parser',choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(p),refusal=None))])
 c=C();monkeypatch.setattr(server,'client',SimpleNamespace(chat=SimpleNamespace(completions=c)))
 monkeypatch.setattr(server,'classify_service_scope_text',lambda *_:'service')
 e=SimpleNamespace(message=SimpleNamespace(id='slot-fallback',text=text),source=SimpleNamespace(user_id='U-NANJING',type='user'),reply_token='r',webhook_event_id='w')
 server.processed_messages.clear();server.handle_message(e)
 with sqlite3.connect(db) as conn:
  row=conn.execute('SELECT meal_slot,request_json FROM pending_text_meal_estimates').fetchone()
  assert row and row[0]==expected
  assert json.loads(row[1])['meal_slot']==expected
  assert conn.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
 assert expected in json.dumps(replies[-1].as_json_dict(),ensure_ascii=False)
 calls_before_replay=c.calls
 server.processed_messages.clear();server.handle_message(e)
 assert c.calls==calls_before_replay and c.calls<=1


def _receipt(tmp_path,monkeypatch):
 db,replies=_setup(tmp_path,monkeypatch)
 d=_fixed_revision_draft(user_id='U-NANJING',message_id='readable',amount=150,unit='g')
 args=dict(user_id='U-NANJING',token=d['token'],expected_version=d['version'],meal_slot='晚餐',amount=200,unit='g',nutrition={'calories_kcal':200,'protein_g':10,'fat_g':5,'carbohydrate_g':30})
 r=server.save_text_meal_draft_from_liff(**args)
 return db,replies,r,args


def test_readable_return_stable_replay_and_canonical_owner_scoped_lookup(tmp_path,monkeypatch):
 db,replies,r,args=_receipt(tmp_path,monkeypatch)
 command=r['return_command']
 assert re.fullmatch(r'已修改：顧客餐點 200 g（回傳碼：[0-9a-f]{16}）',command)
 assert r['receipt_id'] not in command
 assert server.save_text_meal_draft_from_liff(**args)['return_command']==command
 for value in [command,command.replace('顧客餐點 200 g','偽造品名 999 g'),'#草稿回傳 '+r['receipt_id']]:
  card=server.consume_meal_draft_return_command('U-NANJING',value)
  rendered=json.dumps(card.as_json_dict(),ensure_ascii=False)
  assert '顧客餐點' in rendered and '偽造品名' not in rendered
 with pytest.raises(PermissionError):server.consume_meal_draft_return_command('other',command)
 with pytest.raises(PermissionError):server.consume_meal_draft_return_command('U-NANJING',command,source_type='group',source_id='group')
 with sqlite3.connect(db) as conn:
  assert conn.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
  assert conn.execute('SELECT COUNT(*) FROM meal_draft_return_receipts').fetchone()[0]==1
  assert conn.execute('SELECT delivered_at FROM meal_draft_return_receipts').fetchone()[0]
 server.processed_messages.clear()
 e=SimpleNamespace(message=SimpleNamespace(id='friendly-return',text=command),source=SimpleNamespace(user_id='U-NANJING',type='user'),reply_token='r',webhook_event_id='w')
 server.handle_message(e)
 assert '顧客餐點' in json.dumps(replies[-1].as_json_dict(),ensure_ascii=False)


def test_short_receipt_collision_fails_closed(tmp_path,monkeypatch):
 db,replies,r,args=_receipt(tmp_path,monkeypatch)
 with sqlite3.connect(db) as conn:
  row=conn.execute('SELECT * FROM meal_draft_return_receipts').fetchone()
  columns=[x[1] for x in conn.execute('PRAGMA table_info(meal_draft_return_receipts)')]
  new=list(row);new[columns.index('receipt_id')]=r['receipt_id'][:16]+'f'*32
  new[columns.index('draft_token')]='collision-draft';new[columns.index('from_version')]=99
  conn.execute('INSERT INTO meal_draft_return_receipts VALUES ('+','.join('?' for _ in new)+')',new)
 with pytest.raises(PermissionError):server.consume_meal_draft_return_command('U-NANJING',r['return_command'])
