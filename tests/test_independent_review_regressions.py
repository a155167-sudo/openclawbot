"""Controller reproductions of fixed-version review; no business mocks."""
import json, sqlite3
from datetime import timedelta
import pytest
from test_text_meal_dependency_recovery import runtime, subject, SHARED_QUOTA, _request, _quota


def parent(runtime, charged):
    now=runtime.clock[0].isoformat(timespec='seconds')
    scope={'schema_version':'photo-ingredient-child-scope-v1','request_hash':'HASH','items':[{'index':0,'ai_allowed':True,'request':{'food_name':'木耳','amount':'20','unit':'g','meal_slot':'午餐'}}]}
    with sqlite3.connect(runtime.db) as c:
        c.execute('BEGIN IMMEDIATE')
        if charged:
            assert SHARED_QUOTA._charge_text_meal_estimate_quota(c,user_id='U1',token='PHOTO',attempt_id='PARENT',now_text=now)
        c.execute("INSERT INTO photo_ingredient_batch_quota_ops VALUES (?,?,?,?,?,?,?,'processing',?,?,?,?,?)",('BATCH','U1','PHOTO',7,'PARENT-MSG','HASH',json.dumps(scope),'PARENT',(runtime.clock[0]+timedelta(minutes=2)).isoformat(timespec='seconds'),'PARENT',now,now))
        c.commit()


def child():
    return subject.create_text_meal_estimate_draft(user_id='U1',message_id='photo-add-batch:PHOTO:7:PARENT-MSG:0',request=_request(),quota_batch_key='BATCH',quota_batch_owner='PARENT')


def test_ir001_uncharged_parent_must_not_authorize_provider(runtime):
    parent(runtime,False)
    with pytest.raises((ValueError,PermissionError)):
        child()
    assert runtime.completions.calls==[]
    assert _quota(runtime.db)==3


def test_ir002_expired_prestart_claim_is_safely_recoverable(runtime):
    old=(runtime.clock[0]-timedelta(minutes=5)).isoformat(timespec='seconds')
    lease=(runtime.clock[0]-timedelta(minutes=3)).isoformat(timespec='seconds')
    with sqlite3.connect(runtime.db) as c:
        c.execute('BEGIN IMMEDIATE')
        assert SHARED_QUOTA._charge_text_meal_estimate_quota(c,user_id='U1',token='OLD',attempt_id='OLD-A',now_text=old)
        c.execute("INSERT INTO pending_text_meal_estimates VALUES (?,?,?,?,?,1,?,'estimating',1,'',?,?,?,?,?)",('OLD','U1','OLD-M',json.dumps(_request(),sort_keys=True),'{}','午餐',old,old,(runtime.clock[0]+timedelta(minutes=20)).isoformat(timespec='seconds'),'OLD-A',lease))
        c.execute("INSERT INTO text_meal_provider_attempts(attempt_id,token,user_id,quota_attempt_id,state) VALUES(?,?,?,?,'claimed')",('OLD-A','OLD','U1','OLD-A'))
        c.commit()
    draft=subject.create_text_meal_estimate_draft(user_id='U1',message_id='OLD-M',request=_request())
    assert draft['status']=='pending'
    assert len(runtime.completions.calls)==1
    assert _quota(runtime.db)==2


def test_ir004_completed_child_replays_after_parent_completion(runtime):
    parent(runtime,True)
    first=child()
    with sqlite3.connect(runtime.db) as c:
        c.execute("UPDATE photo_ingredient_batch_quota_ops SET status='completed' WHERE batch_key='BATCH'")
        c.commit()
    assert child()==first
    assert len(runtime.completions.calls)==1
    assert _quota(runtime.db)==2


def test_ir003_actual_shared_refund_cannot_refund_parent_after_child_start(runtime):
    parent(runtime,True)
    child()
    with sqlite3.connect(runtime.db) as c:
        c.execute('BEGIN IMMEDIATE')
        assert c.execute("SELECT state FROM text_meal_provider_attempts WHERE quota_attempt_id='PARENT'").fetchone()==('completed',)
        assert SHARED_QUOTA.refund_owned_chat_attempt(c,user_id='U1',attempt_id='PARENT',now_text=runtime.clock[0].isoformat(timespec='seconds')) is False
        c.commit()
    assert _quota(runtime.db)==2
