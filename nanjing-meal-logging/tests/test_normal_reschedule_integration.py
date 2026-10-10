import json
import sqlite3
import asyncio
import threading
import time
from datetime import datetime

import pytest

from normal_reschedule_policy import ensure_normal_reschedule_anchors, normal_pair_policy, normal_semantic_pending_request
from normal_reschedule_context import normal_order_menu_context
from pair_reschedule_coordinator import verify_admin_context, reconcile_pair_reschedule_readback
from reschedule_service_integration import (
    approve_customer_pair_reschedule, submit_customer_pair_reschedule_pending,
    verify_customer_reschedule_context,
)
from test_pair_reschedule_coordinator import ADMIN, OWNER, SOURCE, NOW, open_db
from test_reschedule_service_integration import sheet_fixture
from test_gspread_pair_reschedule_adapter import master_row
from fastapi import FastAPI
import httpx
from customer_reschedule_liff_routes import create_customer_reschedule_router

TARGET = '2026-10-26'
THIRD = '2026-10-27'


def test_normal_approve_keeps_other_http_route_responsive_during_sheet_io(tmp_path):
    path = tmp_path / 'heartbeat.sqlite3'
    conn = normal_db(path)
    context = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    submit_customer_pair_reschedule_pending(
        conn, context=context, source_date=SOURCE, target_date=TARGET,
        request_id='RS_heartbeat', now=NOW, feature_enabled=True)
    conn.close()
    sheet, book = sheet_fixture()
    entered = threading.Event()
    release = threading.Event()
    entered_at = [0.0]
    original_batch_update = book.batch_update

    def delayed_sheet_write(body):
        entered_at[0] = time.monotonic()
        entered.set()
        release.wait(timeout=1.0)
        return original_batch_update(body)

    book.batch_update = delayed_sheet_write
    app = FastAPI()

    @app.get('/heartbeat')
    async def heartbeat():
        return {'ok': True}

    app.include_router(create_customer_reschedule_router(
        liff_id='1234567890-normal', channel_id='1234567890', db_path=str(path),
        app_env='staging', pair_reschedule_enabled=True,
        token_verifier=lambda token, **_: ADMIN if token == 'admin' else 'unknown',
        now_factory=lambda: NOW, normal_flow=True,
        sheet_factory=lambda _conn, _order: sheet))

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            started = time.monotonic()
            approval = asyncio.create_task(client.post(
                '/api/admin/customer-pair-reschedule-requests/RS_heartbeat/approve',
                headers={'Authorization': 'Bearer admin'}))
            try:
                while not entered.is_set() and time.monotonic() - started < 0.8:
                    await asyncio.sleep(0.01)
                assert entered.is_set(), 'approve never reached the synchronous sheet adapter'
                beat = await client.get('/heartbeat')
                assert beat.status_code == 200 and beat.json() == {'ok': True}
                assert time.monotonic() - entered_at[0] < 0.6, 'sheet I/O blocked the event loop'
            finally:
                release.set()
            result = await approval
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'confirmed'
            assert len(book.batch_calls) == 1

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))


def normal_db(path):
    conn, _ = open_db(path)
    conn.execute('DROP TABLE subscription_service_calendar')
    conn.execute('''CREATE TABLE subscription_menu_entitlements(
        user_id TEXT PRIMARY KEY,order_id INTEGER,status TEXT,expires_on TEXT)''')
    conn.execute('INSERT INTO subscription_menu_entitlements VALUES(?,?,?,?)',
                 (OWNER, 1, 'active', '2026-10-26'))
    conn.execute("UPDATE usage SET status='vip',remaining_meals=88,last_date='2026-09-25',expiry_date='2026-10-26'")
    conn.execute('UPDATE subscription_orders SET form_payload_json=? WHERE id=1',
                 (json.dumps({'master_api_rows': [master_row(SOURCE, lunch='午餐A', dinner='晚餐B')]}, ensure_ascii=False),))
    conn.commit()
    ensure_normal_reschedule_anchors(conn)
    return conn


def test_normal_vip_two_moves_new_empty_days_keep_counts_and_receipts(tmp_path):
    conn = normal_db(tmp_path / 'normal.sqlite3')
    sheet, book = sheet_fixture()
    before = tuple(conn.execute('SELECT meal_count FROM subscription_orders WHERE id=1').fetchone()) + tuple(
        conn.execute('SELECT remaining_meals,expiry_date FROM usage WHERE user_id=?', (OWNER,)).fetchone())
    receipts = conn.execute('SELECT count(*) FROM subscription_dispatch_publication_receipts').fetchone()[0]
    for request_id, source, target in [('RS_normal_1', SOURCE, TARGET), ('RS_normal_2', TARGET, THIRD)]:
        context = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
        submitted = submit_customer_pair_reschedule_pending(
            conn, context=context, source_date=source, target_date=target,
            request_id=request_id, now=NOW, feature_enabled=True)
        assert submitted.status == 'pending_admin'
        result = approve_customer_pair_reschedule(
            conn, sheet, request_id=request_id, admin_context=verify_admin_context(conn, ADMIN),
            now=NOW, feature_enabled=True, policy_validator=normal_pair_policy)
        assert result.status == 'confirmed'
        assert approve_customer_pair_reschedule(
            conn, None, request_id=request_id, admin_context=verify_admin_context(conn, ADMIN),
            now=NOW, feature_enabled=True, policy_validator=normal_pair_policy).status == 'confirmed'
        choices = normal_order_menu_context(conn, order_id=1, owner_user_id=OWNER, today=NOW.date())
        assert [item['date'] for item in choices['source_dates']] == [target]
    assert len(book.batch_calls) == 2
    after = tuple(conn.execute('SELECT meal_count FROM subscription_orders WHERE id=1').fetchone()) + tuple(
        conn.execute('SELECT remaining_meals,expiry_date FROM usage WHERE user_id=?', (OWNER,)).fetchone())
    assert after == before
    assert conn.execute('SELECT count(*) FROM subscription_dispatch_publication_receipts').fetchone()[0] == receipts


def test_dynamic_source_profile_is_rejected_before_sheet_write(tmp_path):
    conn = normal_db(tmp_path / 'dynamic.sqlite3')
    row = master_row(SOURCE, lunch='午餐A', dinner='晚餐B')
    row[6] = '1'
    conn.execute('UPDATE subscription_orders SET form_payload_json=? WHERE id=1',
                 (json.dumps({'master_api_rows': [row]}, ensure_ascii=False),))
    conn.commit()
    context = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    submit_customer_pair_reschedule_pending(conn, context=context, source_date=SOURCE,
        target_date=TARGET, request_id='RS_dynamic', now=NOW, feature_enabled=True)
    sheet, book = sheet_fixture()
    with pytest.raises(Exception, match='dynamic target profile'):
        approve_customer_pair_reschedule(conn, sheet, request_id='RS_dynamic',
            admin_context=verify_admin_context(conn, ADMIN), now=NOW,
            feature_enabled=True, policy_validator=normal_pair_policy)
    assert book.batch_calls == []


def test_registered_normal_http_routes_use_real_service_and_coordinator(tmp_path):
    path = tmp_path / 'http.sqlite3'
    conn = normal_db(path)
    conn.close()
    sheet, book = sheet_fixture()
    app = FastAPI()

    def menu(actor):
        with sqlite3.connect(path) as inner:
            inner.row_factory = sqlite3.Row
            return normal_order_menu_context(inner, order_id=1, owner_user_id=actor, today=NOW.date())

    app.include_router(create_customer_reschedule_router(
        liff_id='1234567890-normal', channel_id='1234567890', db_path=str(path),
        app_env='staging', pair_reschedule_enabled=True,
        token_verifier=lambda token, **_: {'owner': OWNER, 'admin': ADMIN, 'other': 'U' + '2' * 32}[token],
        now_factory=lambda: NOW, menu_context_loader=menu,
        normal_flow=True, sheet_factory=lambda _conn, _order: sheet,
        semantic_pending_request_loader=normal_semantic_pending_request,
        html_path='customer-reschedule-normal-liff.html'))

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            def auth(token): return {'Authorization': 'Bearer ' + token}
            assert (await client.get('/customer-reschedule/context', headers=auth('other'))).json()['orders'] == []
            context = (await client.get('/customer-reschedule/context', headers=auth('owner'))).json()
            assert context['orders'][0]['source_dates'][0]['date'] == SOURCE
            assert TARGET in context['orders'][0]['target_dates']
            assert '2026-11-25' in context['orders'][0]['target_dates']
            assert '2026-11-26' not in context['orders'][0]['target_dates']
            with sqlite3.connect(path) as update:
                update.execute("UPDATE usage SET expiry_date='2027-01-01' WHERE user_id=?", (OWNER,))
                update.execute("UPDATE subscription_menu_entitlements SET expires_on='2027-01-01' WHERE order_id=1")
            drifted = (await client.get('/customer-reschedule/context', headers=auth('owner'))).json()
            assert drifted['orders'][0]['target_dates'] == context['orders'][0]['target_dates']
            assert (await client.get('/customer-reschedule/preview', headers=auth('owner'),
                params={'order_id': 1, 'source_date': SOURCE, 'target_date': '2026-11-25'})).status_code == 200
            assert (await client.get('/customer-reschedule/preview', headers=auth('owner'),
                params={'order_id': 1, 'source_date': SOURCE, 'target_date': '2026-11-26'})).status_code == 422
            assert (await client.get('/customer-reschedule/preview', headers=auth('owner'),
                params={'order_id': 1, 'source_date': SOURCE, 'target_date': TARGET})).status_code == 200
            assert (await client.get('/api/admin/customer-pair-reschedule-requests', headers=auth('owner'))).status_code == 403
            body = {'order_id': 1, 'source_date': SOURCE, 'target_date': TARGET, 'request_id': 'RS_http_1'}
            posted = await client.post('/customer-reschedule/pending-request', headers=auth('owner'), json=body)
            assert posted.status_code == 200, posted.text
            semantic = await client.post('/customer-reschedule/pending-request', headers=auth('owner'),
                json={**body, 'request_id': 'RS_http_retry'})
            assert semantic.status_code == 200 and semantic.json()['request_id'] == 'RS_http_1'
            assert (await client.post('/api/admin/customer-pair-reschedule-requests/RS_http_1/approve',
                headers=auth('owner'), json={})).status_code == 403
            approved = await client.post('/api/admin/customer-pair-reschedule-requests/RS_http_1/approve',
                headers=auth('admin'), json={})
            assert approved.status_code == 200, approved.text
            assert approved.json()['status'] == 'confirmed'
            replay = await client.post('/api/admin/customer-pair-reschedule-requests/RS_http_1/approve',
                headers=auth('admin'), json={})
            assert replay.status_code == 200 and len(book.batch_calls) == 1
            context2 = (await client.get('/customer-reschedule/context', headers=auth('owner'))).json()
            assert context2['orders'][0]['source_dates'][0]['date'] == TARGET
            book.outcome = 'apply_then_timeout'
            body2 = {'order_id': 1, 'source_date': TARGET, 'target_date': THIRD, 'request_id': 'RS_http_2'}
            posted2 = await client.post('/customer-reschedule/pending-request', headers=auth('owner'), json=body2)
            assert posted2.status_code == 200, posted2.text
            unknown = await client.post('/api/admin/customer-pair-reschedule-requests/RS_http_2/approve',
                headers=auth('admin'), json={})
            assert unknown.status_code == 200 and unknown.json()['status'] == 'sheet_unknown'
            again = await client.post('/api/admin/customer-pair-reschedule-requests/RS_http_2/approve',
                headers=auth('admin'), json={})
            assert again.json()['status'] == 'sheet_unknown' and len(book.batch_calls) == 2
            listed = await client.get('/api/admin/customer-pair-reschedule-requests', headers=auth('admin'))
            assert listed.json()['requests'][0]['can_reconcile'] is True
            reconciled = await client.post('/api/admin/customer-pair-reschedule-requests/RS_http_2/reconcile',
                headers=auth('admin'), json={})
            assert reconciled.status_code == 200, reconciled.text
            assert reconciled.json()['status'] == 'confirmed' and len(book.batch_calls) == 2
    asyncio.run(asyncio.wait_for(scenario(), timeout=25))


def test_normal_unknown_is_readback_only_and_does_not_repeat_mutation(tmp_path):
    conn = normal_db(tmp_path / 'unknown.sqlite3')
    sheet, book = sheet_fixture()
    book.outcome = 'apply_then_timeout'
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    submit_customer_pair_reschedule_pending(conn, context=customer, source_date=SOURCE,
        target_date=TARGET, request_id='RS_unknown', now=NOW, feature_enabled=True)
    admin = verify_admin_context(conn, ADMIN)
    first = approve_customer_pair_reschedule(conn, sheet, request_id='RS_unknown',
        admin_context=admin, now=NOW, feature_enabled=True, policy_validator=normal_pair_policy)
    assert first.status == 'sheet_unknown'
    replay = approve_customer_pair_reschedule(conn, None, request_id='RS_unknown',
        admin_context=admin, now=NOW, feature_enabled=True, policy_validator=normal_pair_policy)
    assert replay.status == 'sheet_unknown' and len(book.batch_calls) == 1
    recovered = reconcile_pair_reschedule_readback(conn, sheet, order_id=1,
        request_id='RS_unknown', admin_context=admin, now=NOW)
    assert recovered.status == 'confirmed' and len(book.batch_calls) == 1
