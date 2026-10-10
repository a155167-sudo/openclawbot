"""Adversarial recovery checks through the registered normal HTTP route."""
import asyncio
import sqlite3

import httpx
import pytest
from fastapi import FastAPI

from customer_reschedule_liff_routes import create_customer_reschedule_router
from pair_reschedule_coordinator import verify_admin_context
from reschedule_service_integration import (
    approve_customer_pair_reschedule, submit_customer_pair_reschedule_pending,
    verify_customer_reschedule_context,
)
from test_normal_reschedule_integration import (
    ADMIN, NOW, OWNER, SOURCE, TARGET, normal_db, normal_pair_policy, sheet_fixture,
)


REQUEST = 'RS_atomic_readback'


def _case(tmp_path):
    path = tmp_path / 'atomic.sqlite3'
    conn = normal_db(path)
    sheet, book = sheet_fixture()
    book.outcome = 'apply_then_timeout'
    customer = verify_customer_reschedule_context(conn, actor_id=OWNER, order_id=1)
    submit_customer_pair_reschedule_pending(
        conn, context=customer, source_date=SOURCE, target_date=TARGET,
        request_id=REQUEST, now=NOW, feature_enabled=True,
    )
    admin = verify_admin_context(conn, ADMIN)
    assert approve_customer_pair_reschedule(
        conn, sheet, request_id=REQUEST, admin_context=admin, now=NOW,
        feature_enabled=True, policy_validator=normal_pair_policy,
    ).status == 'sheet_unknown'
    conn.close()
    app = FastAPI()
    app.include_router(create_customer_reschedule_router(
        liff_id='1234567890-normal', channel_id='1234567890', db_path=str(path),
        app_env='staging', pair_reschedule_enabled=True,
        token_verifier=lambda token, **_: ADMIN if token == 'admin' else 'unknown',
        now_factory=lambda: NOW, normal_flow=True,
        sheet_factory=lambda _conn, _order: sheet,
    ))
    return path, sheet, book, app


def _post(app):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.post(
                f'/api/admin/customer-pair-reschedule-requests/{REQUEST}/reconcile',
                headers={'Authorization': 'Bearer admin'},
                json={},
            )
    return asyncio.run(run())


def _assert_unknown(path):
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT status FROM reschedule_dispatch_operations').fetchone()[0] == 'sheet_unknown'
        assert conn.execute('SELECT status,final_outcome FROM workbook_write_leases').fetchone() == ('active', None)
        assert conn.execute('SELECT status FROM customer_pair_reschedule_requests').fetchone()[0] == 'sheet_unknown'
        assert conn.execute('SELECT count(*) FROM reschedule_dispatch_confirmations WHERE operation_id IS NOT NULL').fetchone()[0] == 0


@pytest.mark.parametrize('change', [
    "UPDATE workbook_writer_capabilities SET external_writer_state='unknown',cutover_evidence=''",
    "UPDATE customer_pair_reschedule_requests SET owner_user_id='revoked-owner'",
    "UPDATE customer_pair_reschedule_requests SET target_date='2026-11-01'",
])
def test_registered_readback_revocation_during_sheet_read(tmp_path, change):
    path, sheet, book, app = _case(tmp_path)
    original = sheet.read_pair_rows

    def raced(**kwargs):
        rows = original(**kwargs)
        with sqlite3.connect(path) as other:
            other.execute(change)
        return rows

    sheet.read_pair_rows = raced
    response = _post(app)
    assert response.status_code != 200
    _assert_unknown(path)
    assert len(book.batch_calls) == 1


def test_registered_projection_failure_rolls_back_confirmation_and_release(tmp_path):
    path, _sheet, book, app = _case(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TRIGGER fail_confirmed_request_projection
            BEFORE UPDATE OF status ON customer_pair_reschedule_requests
            WHEN NEW.status='confirmed' AND
                 (SELECT status FROM reschedule_dispatch_operations WHERE request_id=NEW.request_id)='confirmed' AND
                 (SELECT status FROM workbook_write_leases)='released'
            BEGIN SELECT RAISE(ABORT,'injected projection failure'); END''')
    with sqlite3.connect(path) as conn:
        before = tuple(conn.iterdump())
    response = _post(app)
    assert response.status_code != 200
    with sqlite3.connect(path) as conn:
        assert tuple(conn.iterdump()) == before
    _assert_unknown(path)
    assert len(book.batch_calls) == 1


def test_registered_success_projects_before_commit_and_replays_without_writes(tmp_path):
    path, _sheet, book, app = _case(tmp_path)
    assert _post(app).json()['status'] == 'confirmed'
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT status,operation_id FROM customer_pair_reschedule_requests').fetchone() == (
            'confirmed', conn.execute('SELECT operation_id FROM reschedule_dispatch_operations').fetchone()[0])
        before = tuple(conn.iterdump())
    assert _post(app).json()['status'] == 'confirmed'
    with sqlite3.connect(path) as conn:
        assert tuple(conn.iterdump()) == before
    assert len(book.batch_calls) == 1
