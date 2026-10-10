import pytest
import sqlite3
from types import SimpleNamespace

from tests.test_sheet_atomic_meal_updates import _setup
from tests.test_registered_meal_replay_acceptance import _send_message, _setup_vip_for_entrypoint
import server

def _setup_admin(db_path, uid, monkeypatch):
    monkeypatch.setattr(server, 'ADMIN_UID', uid)
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute('INSERT OR REPLACE INTO admin_settings (key, value) VALUES (?, ?)', ('admin_id', uid))

def test_successful_swap_and_reverse_uses_two_batches(tmp_path, monkeypatch):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome='accept')
    _setup_vip_for_entrypoint(db_path, uid)
    result1 = server.execute_meal_swap(
        uid, '2099/10/01', '午餐', '2099/10/02', '午餐',
        operation_id=f'line-ai:{uid}:M1',
    )
    assert len(book.batch_calls) == 1
    assert '成功' in result1
    
    result2 = server.execute_meal_swap(
        uid, '2099/10/01', '午餐', '2099/10/02', '午餐',
        operation_id=f'line-ai:{uid}:M2',
    )
    assert len(book.batch_calls) == 2
    assert worksheet.rows[0][2] == '餐A'
    assert worksheet.rows[1][2] == '餐B'
    assert '成功' in result2


def test_admin_deferred_meal_approval_positive_control(tmp_path, monkeypatch):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome='accept')
    _setup_admin(db_path, uid, monkeypatch)
    worksheet.rows[1][2] = ''
    request_id = server.create_deferred_meal_request(uid, '測試會員', '2099/10/01', '午餐', '2099/10/02', '午餐', False, False)
    server.processed_messages.clear()
    replies = _send_message(uid, f'#核准延餐 {request_id}', 'ADMIN-M1', monkeypatch)
    assert len(book.batch_calls) == 1
    assert '已核准延餐申請' in replies[0]

def test_admin_deferred_meal_timeout_replay_safe(tmp_path, monkeypatch):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome='apply_then_timeout')
    _setup_admin(db_path, uid, monkeypatch)
    worksheet.rows[1][2] = ''
    request_id = server.create_deferred_meal_request(uid, '測試會員', '2099/10/01', '午餐', '2099/10/02', '午餐', False, False)
    
    server.processed_messages.clear()
    replies1 = _send_message(uid, f'#核准延餐 {request_id}', 'ADMIN-M1', monkeypatch)
    assert len(book.batch_calls) == 1
    
    server.processed_messages.clear()
    replies2 = _send_message(uid, f'#核准延餐 {request_id}', 'ADMIN-M1', monkeypatch)
    
    # Must not send another batch, overwrite unknown warning, or send false-success
    assert len(book.batch_calls) == 1
    assert '成功' not in replies2[0]
    assert '已核准' not in replies2[0]
    
    with sqlite3.connect(db_path) as conn:
        note = conn.execute('SELECT note FROM deferred_meals WHERE id=?', (request_id,)).fetchone()[0]
    # The note must not be overwritten by the subsequent failure reason ('沒有可延的餐點').
    # It should still preserve the original unknown warning.
    assert '結果未確認' in note
