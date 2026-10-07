from test_normal_reschedule_integration import (normal_db,sheet_fixture,OWNER,ADMIN,NOW,SOURCE,TARGET,normal_pair_policy,verify_customer_reschedule_context,submit_customer_pair_reschedule_pending,verify_admin_context,approve_customer_pair_reschedule,reconcile_pair_reschedule_readback)

def test_existing_approved_vip_owner_can_reconcile_activated_order(tmp_path):
    conn=normal_db(tmp_path/'vip.sqlite3')
    sheet,book=sheet_fixture();book.outcome='apply_then_timeout'
    context=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
    submit_customer_pair_reschedule_pending(conn,context=context,source_date=SOURCE,target_date=TARGET,request_id='RS_vip_recovery',now=NOW,feature_enabled=True)
    admin=verify_admin_context(conn,ADMIN)
    outcome=approve_customer_pair_reschedule(conn,sheet,request_id='RS_vip_recovery',admin_context=admin,now=NOW,feature_enabled=True,policy_validator=normal_pair_policy)
    assert outcome.status=='sheet_unknown'
    outcome=reconcile_pair_reschedule_readback(conn,sheet,order_id=1,request_id='RS_vip_recovery',admin_context=admin,now=NOW)
    assert outcome.status=='confirmed'
    assert conn.execute('SELECT status FROM usage WHERE user_id=?',(OWNER,)).fetchone()[0]=='vip'
    assert len(book.batch_calls)==1


def test_normal_entitlement_revocation_during_read_stays_unknown(tmp_path):
    import sqlite3
    import pytest
    from pair_reschedule_coordinator import PairRescheduleConflict
    path=tmp_path/'revoke.sqlite3'
    conn=normal_db(path)
    sheet,book=sheet_fixture();book.outcome='apply_then_timeout'
    context=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
    submit_customer_pair_reschedule_pending(conn,context=context,source_date=SOURCE,target_date=TARGET,request_id='RS_vip_revoke',now=NOW,feature_enabled=True)
    admin=verify_admin_context(conn,ADMIN)
    outcome=approve_customer_pair_reschedule(conn,sheet,request_id='RS_vip_revoke',admin_context=admin,now=NOW,feature_enabled=True,policy_validator=normal_pair_policy)
    assert outcome.status=='sheet_unknown'
    confirmations_before=[tuple(r) for r in conn.execute('SELECT * FROM reschedule_dispatch_confirmations ORDER BY rowid')]
    original=sheet.read_pair_rows
    def revoke(**kwargs):
        rows=original(**kwargs)
        with sqlite3.connect(path) as other:
            other.execute("UPDATE subscription_menu_entitlements SET status='revoked' WHERE order_id=1")
            other.commit()
        return rows
    sheet.read_pair_rows=revoke
    with pytest.raises(PairRescheduleConflict):
        reconcile_pair_reschedule_readback(conn,sheet,order_id=1,request_id='RS_vip_revoke',admin_context=admin,now=NOW)
    assert conn.execute("SELECT status FROM reschedule_dispatch_operations WHERE request_id='RS_vip_revoke'").fetchone()[0]=='sheet_unknown'
    assert conn.execute("SELECT status FROM customer_pair_reschedule_requests WHERE request_id='RS_vip_revoke'").fetchone()[0]=='sheet_unknown'
    assert conn.execute('SELECT status,final_outcome FROM workbook_write_leases').fetchone()[0]=='active'
    assert [tuple(r) for r in conn.execute('SELECT * FROM reschedule_dispatch_confirmations ORDER BY rowid')]==confirmations_before
    assert len(book.batch_calls)==1
