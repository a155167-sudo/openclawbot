"""Authenticated native form -> emitted button -> registered owner-only preview, no entitlement."""
import json
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import server
from tests.test_subscription_form_uid_handler import ACTUAL_UID_TITLE, VALID_UID, _dish, _text_event


@pytest.fixture
def pending_form(tmp_path, monkeypatch):
    db = tmp_path / 'native-preview.db'
    monkeypatch.setattr(server, 'DB_PATH', str(db))
    server.init_db()
    monkeypatch.setattr(server, 'FORM_WEBHOOK_SECRET', 'synthetic-preview-secret')
    monkeypatch.setattr(server, 'gc', None)
    monkeypatch.setattr(server, 'MAIN_DISHES', [_dish('雞肉便當'+'甲'*180, 180), _dish('豬肉低碳'+'乙'*180, 190), _dish('雞肉食蔬'+'丙'*180, 200)])
    monkeypatch.setattr(server.random, 'sample', lambda population, count: population[:count])
    monkeypatch.setattr(server, 'get_line_display_name_safe', lambda _uid: '測試名稱')
    pushes = []
    monkeypatch.setattr(server.line_bot_api, 'push_message', lambda uid, message, **_k: pushes.append((uid, message)))
    payload = {ACTUAL_UID_TITLE: VALID_UID, '您的稱呼 (姓名或暱稱)': '預覽會員', '本期取餐方式': '自取',
               '您的主食選擇（可複選）': ['都不挑食'], '您最喜歡的蛋白質是？（可複選）': ['雞肉','豬肉']}
    for week in ('第一','第二','第三','第四'):
        payload[f'{week}週想取餐的日期'] = ['週一','週二','週三','週四','週五']
    with TestClient(server.app, base_url='https://testserver') as client:
        response = client.post('/form-data', json=payload, headers={'X-Webhook-Secret': 'synthetic-preview-secret'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'pending'
    order_id = body['order_id']
    with sqlite3.connect(db) as c:
        snapshot = json.loads(c.execute('SELECT form_payload_json FROM subscription_orders WHERE id=?', (order_id,)).fetchone()[0])
        assert c.execute('SELECT count(*) FROM health_profile WHERE user_id=?', (VALID_UID,)).fetchone() == (0,)
    return db, pushes, order_id, snapshot


def test_form_emitted_preview_button_reads_all_owner_dates_without_writes(pending_form, monkeypatch):
    db, pushes, order_id, snapshot = pending_form
    customer_messages = [msg for uid, msg in pushes if uid == VALID_UID]
    assert len(customer_messages) == 1
    controls = customer_messages[0].quick_reply.items
    actions = [x.action for x in controls if x.action.label == '查看配餐預覽']
    assert len(actions) == 1, 'submitted form has no usable preview button'
    replies = []
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda token, msgs: replies.append(msgs))
    monkeypatch.setattr(server, 'has_active_vip_access', lambda _uid: False)
    monkeypatch.setattr(server, '_handle_message_impl', lambda *_a, **_k: pytest.fail('preview entered private AI/state path'))
    with sqlite3.connect(db) as c:
        before = '\n'.join(c.iterdump())
    server.handle_message(_text_event('parent-preview-click', actions[0].text))
    assert len(replies) == 1
    messages = replies[0] if isinstance(replies[0], list) else [replies[0]]
    assert 1 <= len(messages) <= 5 and all(len(msg.text) <= 5000 for msg in messages)
    joined = '\n'.join(msg.text for msg in messages)
    assert '尚未正式開通' in joined
    for row in snapshot['schedule_sheet_rows'][1:]:
        assert row[0][:10] in joined
        assert row[2].split(' ($', 1)[0] in joined and row[5].split(' ($', 1)[0] in joined
    assert len(snapshot['schedule_sheet_rows'][1:]) == 20
    assert '$' not in joined and '午：' not in joined and '晚：' not in joined
    with sqlite3.connect(db) as c:
        assert '\n'.join(c.iterdump()) == before
    assert len([msg for uid,msg in pushes if uid == VALID_UID]) == 1


@pytest.mark.parametrize('case', ['foreign','missing','cancelled','malformed_json','foreign_payload','wrong_header','duplicate_date','invalid_date','nonstring_dish','malformed_command','trailing_date','noncanonical_date','bare_price','embedded_price','missing_price','dish_newline','dish_tab','dish_control','dish_bidi'])
def test_preview_rejects_invalid_or_foreign_input_silently_without_mutation(pending_form, monkeypatch, case):
    db, pushes, order_id, snapshot = pending_form
    command = f'配餐預覽 #{order_id}'
    with sqlite3.connect(db) as c:
        if case == 'foreign':
            c.execute('UPDATE subscription_orders SET user_id=? WHERE id=?', ('U'+'b'*32, order_id))
        elif case == 'missing':
            command = '配餐預覽 #999999'
        elif case == 'cancelled':
            c.execute("UPDATE subscription_orders SET status='cancelled' WHERE id=?", (order_id,))
        elif case == 'malformed_command':
            command = f'配餐預覽 #{order_id} extra'
        elif case == 'malformed_json':
            c.execute('UPDATE subscription_orders SET form_payload_json=? WHERE id=?', ('{invalid', order_id))
        else:
            if case == 'foreign_payload':
                snapshot['user_id'] = 'U'+'b'*32
            elif case == 'wrong_header':
                snapshot['schedule_sheet_rows'][0][0] = 'incorrect'
            elif case == 'duplicate_date':
                snapshot['schedule_sheet_rows'][2][0] = snapshot['schedule_sheet_rows'][1][0]
            elif case == 'invalid_date':
                snapshot['schedule_sheet_rows'][1][0] = '2026/99/99'
            elif case == 'trailing_date':
                snapshot['schedule_sheet_rows'][1][0] += 'TRAILING'
            elif case == 'noncanonical_date':
                snapshot['schedule_sheet_rows'][1][0] = '2026/1/07'
            elif case in {'bare_price','embedded_price','missing_price','dish_newline','dish_tab','dish_control','dish_bidi'}:
                snapshot['schedule_sheet_rows'][1][2] = {
                    'bare_price': '雞肉 $999',
                    'embedded_price': '雞肉 $999 ($180)',
                    'missing_price': '雞肉',
                    'dish_newline': '雞肉\n另一行 ($180)',
                    'dish_tab': '雞肉\t ($180)',
                    'dish_control': '雞肉\x00 ($180)',
                    'dish_bidi': '雞肉\u202e ($180)',
                }[case]
            elif case == 'nonstring_dish':
                snapshot['schedule_sheet_rows'][1][2] = {'unsafe':'nested'}
            c.execute('UPDATE subscription_orders SET form_payload_json=? WHERE id=?', (json.dumps(snapshot), order_id))
    replies = []
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda *_a: replies.append('unexpected'))
    monkeypatch.setattr(server, 'has_active_vip_access', lambda _uid: False)
    monkeypatch.setattr(server, '_handle_message_impl', lambda *_a, **_k: pytest.fail('private path invoked'))
    with sqlite3.connect(db) as c:
        before = '\n'.join(c.iterdump())
    server.handle_message(_text_event('negative-'+case, command))
    assert replies == []
    with sqlite3.connect(db) as c:
        assert '\n'.join(c.iterdump()) == before


@pytest.mark.parametrize('status,formalized', [('approved',''),('activated',''),('activated','2026-10-07T12:00:00+08:00')])
def test_preview_pending_approved_partial_or_redirects_completed_without_entitlement(pending_form, monkeypatch, status, formalized):
    db, pushes, order_id, snapshot = pending_form
    with sqlite3.connect(db) as c:
        c.execute('UPDATE subscription_orders SET status=?,formalized_at=? WHERE id=?', (status, formalized, order_id))
        before = '\n'.join(c.iterdump())
    replies = []
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda _t,msg: replies.append(msg))
    monkeypatch.setattr(server, '_handle_message_impl', lambda *_a, **_k: pytest.fail('private path invoked'))
    server.handle_message(_text_event('preview-'+status+formalized, f'配餐預覽 #{order_id}'))
    assert len(replies) == 1
    msgs = replies[0] if isinstance(replies[0], list) else [replies[0]]
    text = '\n'.join(msg.text for msg in msgs)
    assert ('不使用填表時的舊預覽' in text) if formalized else ('尚未正式開通' in text)
    with sqlite3.connect(db) as c:
        assert '\n'.join(c.iterdump()) == before


def test_emitted_my_plan_button_is_owned_nonvip_readonly(pending_form, monkeypatch):
    db, pushes, order_id, snapshot = pending_form
    replies = []
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda _t,msg: replies.append(msg))
    monkeypatch.setattr(server, 'has_active_vip_access', lambda _uid: False)
    monkeypatch.setattr(server, '_handle_message_impl', lambda *_a, **_k: pytest.fail('status entered private path'))
    with sqlite3.connect(db) as c:
        before = '\n'.join(c.iterdump())
    server.handle_message(_text_event('owner-status', '我的方案'))
    assert len(replies) == 1 and f'#{order_id}' in replies[0].text
    with sqlite3.connect(db) as c:
        assert '\n'.join(c.iterdump()) == before
