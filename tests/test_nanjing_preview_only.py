from pathlib import Path
import inspect
from fastapi import FastAPI
from fastapi.testclient import TestClient
import customer_reschedule_liff_routes as routes


def test_preview_denies_every_native_post_before_database_or_external_work(tmp_path, monkeypatch):
    assert Path(routes.__file__).resolve() == Path(__file__).resolve().parents[1] / 'customer_reschedule_liff_routes.py'
    assert 'preview_only' in inspect.signature(routes.create_customer_reschedule_router).parameters, 'native preview-only admission is missing'
    db = tmp_path / 'must-not-be-created.db'
    app=FastAPI()
    app.include_router(routes.create_customer_reschedule_router(
        liff_id='1234567890-preview',channel_id='1234567890',db_path=str(db),app_env='production',
        pair_reschedule_enabled=True,normal_flow=True,preview_only=True,
        token_verifier=lambda token,**kwargs:'synthetic-owner',
        sheet_factory=lambda *a,**k: (_ for _ in ()).throw(AssertionError('external factory reached')),

    ))
    import sqlite3
    def forbidden(*a,**k): raise AssertionError('preview write admission opened SQLite')
    monkeypatch.setattr(sqlite3,'connect',forbidden)
    client=TestClient(app)
    for url in ['/customer-reschedule/pending-request','/api/admin/customer-pair-reschedule-requests/synthetic/approve','/api/admin/customer-pair-reschedule-requests/synthetic/reconcile']:
        response=client.post(url,headers={'Authorization':'Bearer synthetic-token'},json={})
        assert response.status_code==403
        assert response.json()['code']=='RESCHEDULE_PREVIEW_ONLY'
        assert 'no-store' in response.headers['cache-control']
        assert not db.exists()
        assert client.post(url,json={}).status_code==401


def test_preview_native_admin_get_and_page_are_explicitly_readonly(tmp_path):
    import sqlite3
    from datetime import datetime
    from test_normal_reschedule_integration import normal_db, TARGET
    from test_pair_reschedule_coordinator import ADMIN, OWNER, SOURCE, NOW
    from reschedule_service_integration import submit_customer_pair_reschedule_pending, verify_customer_reschedule_context
    db=tmp_path/'readable.db'; conn=normal_db(db)
    owner=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
    submit_customer_pair_reschedule_pending(conn,context=owner,source_date=SOURCE,target_date=TARGET,request_id='RS_preview',now=NOW,feature_enabled=True)
    conn.commit(); before=list(conn.iterdump()); conn.close()
    app=FastAPI(); app.include_router(routes.create_customer_reschedule_router(
        liff_id='1234567890-preview',channel_id='1234567890',db_path=str(db),app_env='production',
        pair_reschedule_enabled=True,normal_flow=True,preview_only=True,now_factory=lambda:NOW,
        token_verifier=lambda token,**kwargs:ADMIN,
        html_path=Path(routes.__file__).with_name('customer-reschedule-normal-liff.html')))
    client=TestClient(app); page=client.get('/customer-reschedule')
    assert '"previewOnly":true' in page.text
    result=client.get('/api/admin/customer-pair-reschedule-requests',headers={'Authorization':'Bearer synthetic-token'})
    assert result.status_code==200
    rows=result.json()['requests']; assert len(rows)==1
    assert rows[0]['can_approve'] is False
    assert '唯讀預覽' in rows[0]['disabled_reason']
    assert rows[0]['source_date']==SOURCE and rows[0]['target_date']==TARGET
    with sqlite3.connect(db) as check: assert list(check.iterdump())==before


def test_server_and_native_attach_bind_production_preview_mode(tmp_path):
    import ast
    assert 'preview_only' in inspect.signature(routes.attach_customer_reschedule_liff_routes).parameters, 'native mount lacks preview forwarding'
    app=FastAPI()
    assert routes.attach_customer_reschedule_liff_routes(app,enabled=True,
        environ={'APP_ENV':'production','CUSTOMER_RESCHEDULE_LIFF_ID':'1234567890-preview','CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID':'1234567890'},
        db_path=str(tmp_path/'absent.db'),normal_flow=True,pair_reschedule_enabled=True,preview_only=True,
        token_verifier=lambda *a,**k:'synthetic-owner')
    response=TestClient(app).post('/api/admin/customer-pair-reschedule-requests/synthetic/approve',headers={'Authorization':'Bearer synthetic-token'},json={})
    assert response.status_code==403 and response.json()['code']=='RESCHEDULE_PREVIEW_ONLY'
    tree=ast.parse(Path(routes.__file__).with_name('server.py').read_text())
    calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='attach_customer_reschedule_liff_routes' and any(k.arg=='normal_flow' and isinstance(k.value,ast.Constant) and k.value.value is True for k in node.keywords)]
    assert len(calls)==1
    keyword=next((k for k in calls[0].keywords if k.arg=='preview_only'),None)
    assert keyword is not None, 'actual production server mount lacks preview-only binding'
    assert ast.dump(keyword.value)==ast.dump(ast.parse("APP_ENV == 'production'",mode='eval').body)


def test_preview_unknown_projection_never_advertises_reconcile_permission(tmp_path):
    import sqlite3
    from test_normal_reschedule_integration import normal_db,TARGET
    from test_pair_reschedule_coordinator import ADMIN,OWNER,SOURCE,NOW
    from reschedule_service_integration import submit_customer_pair_reschedule_pending,verify_customer_reschedule_context
    path=tmp_path/'unknown.sqlite3';conn=normal_db(path)
    owner=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
    submit_customer_pair_reschedule_pending(conn,context=owner,source_date=SOURCE,target_date=TARGET,request_id='RS_unknown_preview',now=NOW,feature_enabled=True)
    # Synthetic legacy unknown projection, not a fabricated successful receipt.
    conn.execute("UPDATE customer_pair_reschedule_requests SET status='sheet_unknown' WHERE request_id='RS_unknown_preview'")
    conn.commit();before=list(conn.iterdump());conn.close()
    app=FastAPI();app.include_router(routes.create_customer_reschedule_router(
        liff_id='1234567890-preview',channel_id='1234567890',db_path=str(path),app_env='production',
        pair_reschedule_enabled=True,normal_flow=True,preview_only=True,now_factory=lambda:NOW,
        token_verifier=lambda token,**kwargs:ADMIN))
    result=TestClient(app).get('/api/admin/customer-pair-reschedule-requests',headers={'Authorization':'Bearer synthetic-token'})
    assert result.status_code==200
    row=result.json()['requests'][0]
    assert row['status']=='sheet_unknown' and row['can_approve'] is False
    assert row['can_reconcile'] is False
    with sqlite3.connect(path) as check:assert list(check.iterdump())==before
