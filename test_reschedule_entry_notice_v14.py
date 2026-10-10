import os,tempfile
from types import SimpleNamespace
from datetime import datetime
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='v14-entry-tests-')
os.environ.setdefault('OPENAI_API_KEY','sk-test');os.environ.setdefault('LINE_CHANNEL_ACCESS_TOKEN','dummy');os.environ.setdefault('LINE_CHANNEL_SECRET','dummy')
import server
from reschedule_service_integration import CustomerPairRescheduleRequest,render_admin_pair_reschedule_pending_receipt

def test_actual_menu_single_reschedule_entry(monkeypatch):
 replies=[]
 monkeypatch.setattr(server,'has_active_vip_access',lambda _:True)
 monkeypatch.setattr(server,'get_subscription_menu_access',lambda _:('active','2026/10/01\n午：牛肉\n晚：豆腐',2,'2026-10-26'))
 monkeypatch.setattr(server,'get_active_subscription_order_id',lambda _:123)
 monkeypatch.setattr(server,'CUSTOMER_RESCHEDULE_LIFF_ENABLED',True)
 monkeypatch.setattr(server,'CUSTOMER_RESCHEDULE_LIFF_ID','2011528194-td43IPq1')
 monkeypatch.setattr(server.line_bot_api,'reply_message',lambda token,msg:replies.append(msg))
 server.processed_messages.clear()
 server.handle_message(SimpleNamespace(reply_token='synthetic',source=SimpleNamespace(user_id='U'+'a'*32),message=SimpleNamespace(id='v14-menu',text='查看菜單')))
 msg=replies[0];msg=msg[-1] if isinstance(msg,list) else msg
 actions=[b.action for b in msg.quick_reply.items]
 assert [a.label for a in actions].count('餐點改期')==1
 assert not any(a.label in ('申請延餐','申請改期') for a in actions)
 action=next(a for a in actions if a.label=='餐點改期')
 assert action.uri=='https://liff.line.me/2011528194-td43IPq1?order_id=123'

def test_admin_pending_copy_is_concise():
 request=SimpleNamespace(request_id='RS_synthetic',order_id=123,source_date='2026-10-01',target_date='2026-10-29',status='pending_admin',created_at='2026-09-30T12:00:00+08:00',expires_at='2026-10-01T12:00:00+08:00')
 text=render_admin_pair_reschedule_pending_receipt(request,meal_content={'2026-10-01':('牛肉低碳','鷹嘴豆低碳')})
 assert text.startswith('餐點改期待確認');assert '牛肉低碳、鷹嘴豆低碳' in text
 assert '2026-10-01' in text and '2026-10-29' in text
 assert '2026-10-01 12:00' in text
 assert not any(s in text for s in ('request_id:','order_id:','status:','created:','expires:','lunch=','dinner='))

def test_line_notification_has_readonly_button(monkeypatch):
 pushes=[]
 class Client:
  headers={}
  def push_message(self,recipient,message,**kwargs):pushes.append((recipient,message,kwargs))
 monkeypatch.setattr(server,'line_bot_api',Client())
 monkeypatch.setattr(server,'CUSTOMER_RESCHEDULE_LIFF_ID','2011528194-td43IPq1')
 monkeypatch.setattr(server,'CUSTOMER_RESCHEDULE_LIFF_ENABLED',True)
 server._send_pair_reschedule_admin_receipt_via_line('bound-admin','餐點改期待確認','stable-retry',request_id='RS_synthetic')
 _,message,options=pushes[0];d=message.as_json_dict();assert d['type']=='flex'
 button=d['contents']['footer']['contents'][0]['action'];assert button['type']=='uri';assert button['label']=='查看並核准'
 assert button['uri']=='https://liff.line.me/2011528194-td43IPq1?request_id=RS_synthetic'
 assert '/approve' not in button['uri'];assert options['retry_key']=='stable-retry'
