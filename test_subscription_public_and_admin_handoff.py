"""Controller-owned regressions. Run with pytest tests/ this_file so suite network guards load."""
import json
from types import SimpleNamespace
import pytest
import server

UID = 'U' + 'e' * 32


def test_completed_activation_pushes_original_code_to_order_owner_once(tmp_path, monkeypatch):
    import sqlite3
    from test_subscription_activation_retry import _install_activation_db, UID as OWNER
    db, pushes, formalizes = _install_activation_db(tmp_path, monkeypatch)
    assert not server.update_subscription_order_status(1, 'activated', 'synthetic-admin')[0]
    assert pushes == []
    ok, message = server.update_subscription_order_status(1, 'activated', 'synthetic-admin')
    assert ok and len(pushes) == 1
    with sqlite3.connect(db) as conn:
        code = conn.execute('SELECT vip_code FROM subscription_orders WHERE id=1').fetchone()[0]
        assert conn.execute('SELECT count(*) FROM vips').fetchone()[0] == 1
    assert pushes[0][0] == OWNER
    assert code in pushes[0][1]
    assert '複製' in pushes[0][1]
    assert '請轉交' not in message
    assert server.update_subscription_order_status(1, 'activated', 'synthetic-admin')[0]
    assert len(pushes) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT vip_code FROM subscription_orders WHERE id=1').fetchone()[0] == code
        assert conn.execute('SELECT count(*) FROM vips').fetchone()[0] == 1


def event(text, ident='v17-public'):
    return SimpleNamespace(reply_token='synthetic-v17-reply', source=SimpleNamespace(user_id=UID), message=SimpleNamespace(id=ident, text=text))

def forbidden(*args, **kwargs):
    raise AssertionError('public input reached private/legacy business processing')

@pytest.mark.parametrize('text', ['包月菜單', '查看菜單', '查看包月菜單', '了解包月方案', '包月方案', '我要包月', '如何訂購'])
def test_nonvip_public_alias_replies_without_private_processing(monkeypatch, text):
    replies = []
    monkeypatch.setattr(server, 'has_active_vip_access', lambda uid: False)
    monkeypatch.setattr(server, '_handle_message_impl', forbidden)
    monkeypatch.setattr(server, 'get_subscription_menu_access', forbidden)
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda token, msg: replies.append(msg))
    monkeypatch.setattr(server, 'clear_pending_subscription_state', forbidden)
    server.handle_message(event(text))
    assert len(replies) == 1
    payload = replies[0].as_json_dict()
    assert payload['type'] == 'flex'
    actions = [item['action']['text'] for item in payload['contents']['footer']['contents']]
    assert actions == ['開始包月估價', '找客服']


def test_rendered_support_cta_has_safe_registered_reply(monkeypatch):
    replies = []
    monkeypatch.setattr(server, 'has_active_vip_access', lambda uid: False)
    monkeypatch.setattr(server, '_handle_message_impl', forbidden)
    monkeypatch.setattr(server, 'clear_pending_subscription_state', forbidden)
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda token, msg: replies.append(msg))
    intro = server.build_subscription_intro_flex(UID).as_json_dict()
    text = intro['contents']['footer']['contents'][1]['action']['text']
    server.handle_message(event(text, 'v17-support'))
    assert len(replies) == 1
    assert replies[0].text == '可以，請直接在這裡留言你的需求，客服會協助確認餐數、外送/自取、金額與付款方式。'


def test_fresh_activation_returns_code_directly_to_customer(tmp_path, monkeypatch):
    import sqlite3
    from test_subscription_activation_retry import _install_activation_db
    db, pushes, formalizes = _install_activation_db(tmp_path, monkeypatch)
    first_ok, first_message = server.update_subscription_order_status(1, 'activated', 'synthetic-admin')
    assert not first_ok
    assert pushes == []
    ok, admin_message = server.update_subscription_order_status(1, 'activated', 'synthetic-admin')
    assert ok
    with sqlite3.connect(db) as conn:
        code = conn.execute('SELECT vip_code FROM subscription_orders WHERE id=1').fetchone()[0]
        assert conn.execute('SELECT count(*) FROM vips').fetchone()[0] == 1
    assert code not in admin_message
    assert '請轉交' not in admin_message
    assert len(pushes) == 1
    assert code in pushes[0][1]
    assert '#VIP' in pushes[0][1]
    assert '複製' in pushes[0][1]
    replay_ok, replay_message = server.update_subscription_order_status(1, 'activated', 'synthetic-admin')
    assert replay_ok and code not in replay_message
    assert len(pushes) == 1
    assert len(formalizes) == 2


@pytest.mark.parametrize('text', [' 查看菜單', '查看菜單 ', '查看菜單#核准訂單 1', '查看其他人的菜單', '客服', '找客服幫我開通', '#VIPORDER-INVALID', '#核准訂單 1'])
def test_nearby_nonvip_inputs_remain_silent(monkeypatch, text):
    monkeypatch.setattr(server, 'has_active_vip_access', lambda uid: False)
    monkeypatch.setattr(server, '_handle_message_impl', forbidden)
    monkeypatch.setattr(server.line_bot_api, 'reply_message', forbidden)
    server.handle_message(event(text))


def test_rendered_quote_cta_enters_real_owned_quote_flow(monkeypatch):
    replies = []
    monkeypatch.setattr(server, 'has_active_vip_access', lambda uid: False)
    monkeypatch.setattr(server.line_bot_api, 'reply_message', lambda token, msg: replies.append(msg))
    monkeypatch.setattr(server, 'pending_subscription_state', {})
    server.processed_messages.clear()
    text = server.build_subscription_intro_flex(UID).as_json_dict()['contents']['footer']['contents'][0]['action']['text']
    server.handle_message(event(text, 'v17-estimate-real'))
    assert len(replies) == 1
    assert server.pending_subscription_state[UID]['step'] == 'days'
    assert replies[0].quick_reply is not None


@pytest.mark.parametrize('state', ['pending', 'sending', 'unknown', 'sent'])
@pytest.mark.parametrize('legacy_scope', ['subscription-activation-success/v1', 'subscription-activation-success/v2-admin-handoff'])
def test_legacy_activation_intents_are_unchanged_and_never_resent(tmp_path, monkeypatch, state, legacy_scope):
    import sqlite3
    from test_subscription_activation_retry import _install_activation_db
    db, pushes, formalizes = _install_activation_db(tmp_path, monkeypatch)
    assert server.update_subscription_order_status(1, 'activated', 'synthetic-admin')[0] is False
    with sqlite3.connect(db) as conn:
        code = conn.execute('SELECT vip_code FROM subscription_orders WHERE id=1').fetchone()[0]
        conn.execute("UPDATE subscription_orders SET formalized_at='done'")
        conn.execute('DELETE FROM subscription_activation_notifications')
        legacy_body = ('🎉 付款已確認，包月已正式開通！\n'
            f'請直接複製並傳送這組開通碼完成會員權限啟用：\n{code}\n\n'
            '您先前填寫的包月資料已轉正式，不需要再填一次表單。\n\n'
            '✅ 專屬排餐與正式試算表已同步建立。')
        with monkeypatch.context() as mp:
            mp.setattr(server, '_ACTIVATION_NOTIFICATION_SCOPE', legacy_scope)
            server._insert_activation_notification_intent(conn, 1, 'U' + 'a'*32, legacy_body)
        conn.execute('UPDATE subscription_activation_notifications SET state=?', (state,))
        before = {table: conn.execute('SELECT * FROM ' + table).fetchall() for table in ('subscription_orders', 'vips', 'subscription_activation_notifications')}
    ok, msg = server.update_subscription_order_status(1, 'activated', 'synthetic-admin')
    assert not ok and '人工核對' in msg and code in msg
    assert pushes == []
    with sqlite3.connect(db) as conn:
        after = {table: conn.execute('SELECT * FROM ' + table).fetchall() for table in before}
    assert after == before
    assert len(formalizes) == 1


def test_approved_order_only_sends_payment_info_and_does_not_issue_code(tmp_path, monkeypatch):
    import sqlite3
    from test_subscription_activation_retry import _install_activation_db
    db, pushes, formalizes = _install_activation_db(tmp_path, monkeypatch)
    ok, msg = server.update_subscription_order_status(1, 'approved', 'synthetic-admin')
    assert ok and len(pushes) == 1
    assert '應匯款金額' in pushes[0][1]
    assert '#VIP' not in pushes[0][1]
    assert formalizes == []
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM vips').fetchone()[0] == 0
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='subscription_activation_notifications'").fetchall()


def test_first_successful_activation_customer_receives_code_and_v3_identity(tmp_path, monkeypatch):
    import sqlite3, uuid
    from test_subscription_activation_retry import _install_activation_db
    db, pushes, formalizes = _install_activation_db(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE subscription_orders SET formalized_at='done'")
    ok, msg = server.update_subscription_order_status(1, 'activated', 'synthetic-admin')
    assert ok and len(pushes) == 1
    with sqlite3.connect(db) as conn:
        code = conn.execute('SELECT vip_code FROM subscription_orders WHERE id=1').fetchone()[0]
        scope, retry, state = conn.execute('SELECT scope,retry_key,state FROM subscription_activation_notifications').fetchone()
    assert code not in msg and '請轉交' not in msg
    assert code in pushes[0][1]
    assert scope == 'subscription-activation-success/v3-customer-code'
    assert retry != str(uuid.uuid5(uuid.NAMESPACE_URL, 'subscription-activation-success/v2-admin-handoff:order:1'))
    assert retry != str(uuid.uuid5(uuid.NAMESPACE_URL, 'subscription-activation-success/v1:order:1'))
    assert state == 'sent'
    assert formalizes == []
