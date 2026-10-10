"""Registered form receipt regression: only external I/O is replaced."""
import json
import sqlite3
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def isolated_form(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-isolated-test')
    monkeypatch.setenv('LINE_CHANNEL_ACCESS_TOKEN', 'dummy')
    monkeypatch.setenv('LINE_CHANNEL_SECRET', 'dummy')
    import server
    from test_subscription_form_uid_handler import _dish
    db = tmp_path / 'form.db'
    monkeypatch.setattr(server, 'DB_PATH', str(db))
    server.init_db()
    monkeypatch.setattr(server, 'PAIR_RESCHEDULE_ENABLED', True)
    monkeypatch.setattr(server, 'FORM_WEBHOOK_SECRET', 'isolated-v16-secret')
    monkeypatch.setattr(server, 'MAIN_DISHES', [_dish('雞肉便當',180),_dish('豬肉低碳',190),_dish('雞肉食蔬',200)])
    monkeypatch.setattr(server.random, 'sample', lambda population, count: population[:count])
    pushes=[]
    monkeypatch.setattr(server.line_bot_api, 'push_message', lambda uid,message: pushes.append((uid,message)))
    monkeypatch.setattr(server, 'get_line_display_name_safe', lambda uid:'隔離測試名稱')
    class ForbiddenSheets:
        calls=0
        def __getattr__(self, name):
            self.calls+=1
            raise AssertionError('Pending form must not access any Sheet')
    sheets=ForbiddenSheets()
    monkeypatch.setattr(server,'gc',sheets)
    uid='U'+'a'*32
    payload={'1. LINE UID (系統綁定用，請勿修改)':uid,'您的稱呼 (姓名或暱稱)':'隔離客戶','本期取餐方式':'自取','取餐日期':['週一']}
    with sqlite3.connect(db) as c:
        server.ensure_workbook_lease_schema(c)
        c.execute("INSERT OR REPLACE INTO admin_settings(key,value) VALUES('admin_id',?)",('U'+'b'*32,))
    client=TestClient(server.app,raise_server_exceptions=False)
    yield server,db,client,payload,pushes,sheets
    client.close()


@pytest.mark.parametrize('pair_enabled',[True,False])
def test_registered_pair_mode_receipt_persists_only_pending_order(isolated_form, monkeypatch, pair_enabled):
    server,db,client,payload,pushes,sheets=isolated_form
    monkeypatch.setattr(server,'PAIR_RESCHEDULE_ENABLED',pair_enabled)
    assert server.current_server_workbook_write_fence() is None
    response=client.post('/form-data',json=payload,headers={'X-Webhook-Secret':'isolated-v16-secret'})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='pending',result
    with sqlite3.connect(db) as c:
        rows=c.execute('SELECT id,user_id,status,form_payload_json,formalized_at,vip_code FROM subscription_orders').fetchall()
        assert len(rows)==1
        assert rows[0][:3]==(result['order_id'],payload['1. LINE UID (系統綁定用，請勿修改)'],'pending')
        assert json.loads(rows[0][3])['raw_form_data']==payload
        assert not rows[0][4] and not rows[0][5]
        assert c.execute('SELECT COUNT(*) FROM health_profile').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM subscription_menu_entitlements').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM workbook_write_leases').fetchone()[0]==0
    assert sheets.calls==0
    assert any(uid==payload['1. LINE UID (系統綁定用，請勿修改)'] for uid,_ in pushes)
    assert any(uid=='U'+'b'*32 for uid,_ in pushes)


@pytest.mark.parametrize('headers',[{}, {'X-Webhook-Secret':'wrong'}, {'Authorization':'Bearer isolated-v16-secret'}])
def test_registered_pair_mode_rejects_bad_secret_before_any_effect(isolated_form,headers):
    server,db,client,payload,pushes,sheets=isolated_form
    response=client.post('/form-data',json=payload,headers=headers)
    assert response.status_code==401,response.text
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM subscription_orders').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM health_profile').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM subscription_menu_entitlements').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM workbook_write_leases').fetchone()[0]==0
    assert pushes==[] and sheets.calls==0


def test_pending_form_does_not_relax_shared_workbook_write_fence(isolated_form):
    server,db,client,payload,pushes,sheets=isolated_form
    with pytest.raises(RuntimeError,match='寫入保護尚未完成'):
        server._require_controlled_workbook_writer('legacy_receive_form')
    assert server.current_server_workbook_write_fence() is None
    assert pushes==[] and sheets.calls==0
