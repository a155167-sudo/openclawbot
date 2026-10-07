from datetime import datetime,timedelta
import pytest
from reschedule_service_integration import submit_customer_pair_reschedule,submit_customer_pair_reschedule_pending,verify_customer_reschedule_context,approve_customer_pair_reschedule,RescheduleRequestConflict
from test_pair_reschedule_coordinator import NOW,OWNER,SOURCE,TARGET,ADMIN,open_db
from test_reschedule_service_integration import sheet_fixture
from pair_reschedule_coordinator import verify_admin_context

@pytest.mark.parametrize('creator',[submit_customer_pair_reschedule,submit_customer_pair_reschedule_pending])
def test_both_creators_persist24h_and_replay_does_not_extend(tmp_path,creator):
 conn,_=open_db(tmp_path/'ttl.sqlite3');ctx=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
 kw=dict(context=ctx,source_date=SOURCE,target_date=TARGET,request_id='ttl-v14',feature_enabled=True)
 r=creator(conn,now=NOW,**kw)
 assert datetime.fromisoformat(r.expires_at)-datetime.fromisoformat(r.created_at)==timedelta(hours=24)
 again=creator(conn,now=NOW+timedelta(hours=1),**kw)
 assert again.expires_at==r.expires_at and again.created_at==r.created_at
 assert conn.execute('select count(*) from customer_pair_reschedule_requests').fetchone()[0]==1

@pytest.mark.parametrize('creator',[submit_customer_pair_reschedule,submit_customer_pair_reschedule_pending])
def test_expired24h_request_does_not_write_sheet(tmp_path,creator):
 conn,_=open_db(tmp_path/'expired.sqlite3');ctx=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
 creator(conn,context=ctx,source_date=SOURCE,target_date=TARGET,request_id='ttl-expired',feature_enabled=True,now=NOW)
 sheet,book=sheet_fixture()
 with pytest.raises(RescheduleRequestConflict,match='expired'):
  approve_customer_pair_reschedule(conn,sheet,request_id='ttl-expired',admin_context=verify_admin_context(conn,ADMIN),feature_enabled=True,now=NOW+timedelta(hours=24,seconds=1))
 assert book.batch_calls==[]
