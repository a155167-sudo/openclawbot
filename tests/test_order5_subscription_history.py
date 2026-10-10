"""Real registered defer -> new subscription -> native integrity regression; all I/O synthetic."""
import json
import sqlite3
import pytest

import meal_mutation_ledger as ledger
import server
from subscription_dispatch_contract import ORIGINAL_HEADERS
from tests.test_deferred_meal_replay_fence import ADMIN, _bind_admin, _request, _send
from tests.test_sheet_atomic_meal_updates import _setup


@pytest.mark.parametrize('manual', [False, True])
def test_new_subscription_preserves_completed_defer_receipt_and_survives_restart(tmp_path, monkeypatch, manual):
    uid, db, worksheet, book = _setup(tmp_path, monkeypatch, outcome='apply_then_timeout' if manual else 'accept')
    monkeypatch.setattr(server, 'PAIR_RESCHEDULE_ENABLED', False)
    worksheet.rows[1][2] = ''
    _bind_admin(db, monkeypatch)
    monkeypatch.setattr(server.line_bot_api, 'push_message', lambda *_a, **_k: None)
    request_id = _request(uid)
    replies = _send(ADMIN, request_id, 'parent-v32-native-defer-'+str(manual), monkeypatch)
    if manual:
        with sqlite3.connect(db) as c:
            assert c.execute('SELECT status FROM meal_mutation_operations WHERE operation_id=?', (f'deferred-meal:{request_id}',)).fetchone() == ('outcome_unknown',)
        result = ledger.manually_reconcile_meal_mutation(
            db, operation_id=f'deferred-meal:{request_id}', owner_user_id=uid,
            disposition='applied', evidence_note='synthetic provider applied; synchronous request terminated',
            admin_uid=ADMIN, no_outstanding_late_request_confirmed=True,
        )
        assert result.kind == 'newly_completed'
    else:
        assert '已核准延餐申請' in replies[0]
    with sqlite3.connect(db) as c:
        op_before = c.execute('SELECT * FROM meal_mutation_operations WHERE operation_id=?', (f'deferred-meal:{request_id}',)).fetchone()
        receipt = json.loads(c.execute('SELECT result_json FROM meal_mutation_operations WHERE operation_id=?', (f'deferred-meal:{request_id}',)).fetchone()[0])
        ledger._verify_operation_lock_integrity(c)
        c.execute("INSERT INTO health_profile(user_id,name,summary_text) VALUES('foreign-user','other','foreign untouched')")
    menu = '2026/10/07（週三）\n午：雞肉\n晚：豆腐'
    snapshot = {'user_id': uid, 'name': '測試會員', 'schedule_text': menu, 'safe_name': 'new-customer',
                'tdee': 1800, 'protein': 100, 'active_days_list': ['2026/10/07'], 'meal_count': 2,
                'pickup_method': '自取', 'is_delivery': False, 'delivery_info': {'delivery_available': True},
                'schedule_sheet_rows': [ORIGINAL_HEADERS, ['2026/10/07', '週三', '雞肉', 400, 30, '豆腐', 400, 30, '', '', '', '$400', '', '']],
                'master_api_rows': []}
    order_id = server.create_pending_subscription_form_order(snapshot)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE subscription_orders SET status='activated' WHERE id=?", (order_id,))
    monkeypatch.setattr(server, 'gc', None)
    ok, detail = server.formalize_subscription_snapshot(order_id, snapshot)
    assert not ok and 'Google Sheet 尚未連線' in detail
    with sqlite3.connect(db) as c:
        summary = c.execute('SELECT summary_text FROM health_profile WHERE user_id=?', (uid,)).fetchone()[0]
        assert ledger._summary_contains_complete_record(summary, receipt['summary_line']), 'new paid menu erased completed defer audit'
        assert summary == menu + receipt['summary_line']
        assert c.execute('SELECT * FROM meal_mutation_operations WHERE operation_id=?', (f'deferred-meal:{request_id}',)).fetchone() == op_before
        assert c.execute("SELECT summary_text FROM health_profile WHERE user_id='foreign-user'").fetchone() == ('foreign untouched',)
        ledger._verify_operation_lock_integrity(c)
    server.init_db()  # Actual native bootstrap, not a weakened validator.
    # Same paid order's retry is safe: exactly one old audit, identical terminal receipt.
    ok, detail = server.formalize_subscription_snapshot(order_id, snapshot)
    assert not ok and 'Google Sheet 尚未連線' in detail
    with sqlite3.connect(db) as c:
        retried = c.execute('SELECT summary_text FROM health_profile WHERE user_id=?', (uid,)).fetchone()[0]
        assert retried == summary
        assert c.execute('SELECT * FROM meal_mutation_operations WHERE operation_id=?', (f'deferred-meal:{request_id}',)).fetchone() == op_before
        # Already damaged records must not be healed by ordinary activation.
        c.execute('UPDATE health_profile SET summary_text=? WHERE user_id=?', ('damaged projection', uid))
    ok, detail = server.formalize_subscription_snapshot(order_id, snapshot)
    assert not ok and '安全狀態未確認' in detail
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT summary_text FROM health_profile WHERE user_id=?', (uid,)).fetchone() == ('damaged projection',)
        assert c.execute('SELECT * FROM meal_mutation_operations WHERE operation_id=?', (f'deferred-meal:{request_id}',)).fetchone() == op_before
