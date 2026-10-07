import hashlib
import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest
import server
from dashboard_balance_adapter import adapt_dashboard_data
from dashboard_flex import compute
from test_dashboard_balance_v53 import _install_db

HEADER = ['實際日期','週期與星期','午餐安排','午餐熱量','午餐蛋白','晚餐安排','晚餐熱量','晚餐蛋白','今日排餐總熱量','今日排餐總蛋白','熱量剩餘 / 蛋白質需補','單日金額','明日預定課表','列印狀態','Dispatch_Row_ID','Order_ID','Menu_Version']

class Sheet:
    title = 'member-sheet'
    id = 17
    def __init__(self, values): self.values=values
    def get_all_values(self): return self.values

class Book:
    id = 'test-spreadsheet-no-network'
    def __init__(self, values): self.sheet=Sheet(values); self.master_reads=0
    def worksheet(self, name):
        if name == self.sheet.title: return self.sheet
        self.master_reads += 1
        raise AssertionError('Home must not substitute Master for printer source')


def setup(tmp_path, monkeypatch, *, marker=True, rows=None):
    uid='U-DISPATCH-TREND'
    monkeypatch.setattr(server,'tw_today',lambda:date(2026,10,7))
    path=_install_db(tmp_path,monkeypatch,uid,rows or [])
    prefix=[[f'User_ID: {uid}']] if marker else []
    book=Book(prefix+[HEADER,['2026/10/07','三','出單午餐','600.5','40','出單晚餐','700','45','','','','','無','已送印','dispatch-today','1','v1']])
    monkeypatch.setattr(server,'gc',SimpleNamespace(open_by_key=lambda key:book))
    monkeypatch.setattr(server,'SPREADSHEET_ID',book.id)
    return uid,path,book


def test_home_uses_exact_printer_sheet_and_printed_does_not_mean_eaten(tmp_path,monkeypatch):
    uid,path,book=setup(tmp_path,monkeypatch)
    before=path.read_bytes()
    raw=server.get_dashboard_data(uid,scope='home')
    assert raw['subscription_source_status']=='ok'
    assert [x['name'] for x in raw['balance_sub_meals']]==['出單午餐','出單晚餐']
    assert compute(adapt_dashboard_data(raw))['rk']==1300.5
    assert book.master_reads==0
    assert path.read_bytes()==before


def test_rescheduled_empty_original_date_does_not_revive_old_master_or_summary(tmp_path,monkeypatch):
    uid,path,book=setup(tmp_path,monkeypatch)
    book.sheet.values[-1][2:8]=['','','','','','']
    with sqlite3.connect(path) as c:
        c.execute('UPDATE health_profile SET summary_text=? WHERE user_id=?',('2026/10/07 午：舊餐 晚：舊餐',uid))
    raw=server.get_dashboard_data(uid,scope='home')
    assert raw['subscription_source_status']=='ok'
    assert raw['balance_sub_meals']==[]
    assert book.master_reads==0


def test_unproven_sheet_is_visible_error_not_silent_no_subscription_or_master_fallback(tmp_path,monkeypatch):
    uid,path,book=setup(tmp_path,monkeypatch,marker=False)
    raw=server.get_dashboard_data(uid,scope='home')
    assert raw['subscription_source_status']=='unavailable'
    assert raw['balance_sub_meals']==[] and book.master_reads==0
    text=json.dumps(server.build_dashboard_flex(uid).as_json_dict(),ensure_ascii=False)
    assert '包月出單資料暫時無法核對' in text
    assert '餘額可能偏高' in text


def test_trend_reads_canonical_real_ledger_without_mutating_any_table(tmp_path,monkeypatch):
    uid,path,book=setup(tmp_path,monkeypatch,rows=[dict(name='已確認',slot='午餐',kcal=600.5,protein=40)])
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    data=server.get_weekly_trend_data(uid)
    assert len(data['days'])==7
    assert data['days'][0]['date']=='2026-10-01'
    assert data['days'][-1]['calories_kcal']==600.5
    assert data['days'][-1]['protein_g']==40
    assert all(x['logged'] is False for x in data['days'][:-1])
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert book.master_reads==0


@pytest.mark.parametrize('command',['一週趨勢','本週趨勢','週趨勢'])
@pytest.mark.parametrize('vip',[True,False])
def test_trend_real_entrypoint_is_readonly_even_with_pending_edit(tmp_path,monkeypatch,command,vip):
    uid,path,book=setup(tmp_path,monkeypatch)
    with sqlite3.connect(path) as c:
        c.execute('INSERT OR REPLACE INTO usage(user_id,remaining_meals,status,expiry_date) VALUES (?,?,?,?)',(uid,10,'vip' if vip else 'free','2026-12-31'))
    calls=[]
    monkeypatch.setattr(server.line_bot_api,'reply_message',lambda token,msg:calls.append(msg))
    def fail(*args,**kwargs): raise AssertionError('trend must bypass mutation / AI paths')
    monkeypatch.setattr(server,'get_daily_food_edit_state',fail)
    monkeypatch.setattr(server,'check_permission_and_quota',fail)
    monkeypatch.setattr(server.line_bot_api,'push_message',fail)
    before=path.read_bytes()
    event=SimpleNamespace(message=SimpleNamespace(id=f'trend-{command}-{vip}',text=command),source=SimpleNamespace(user_id=uid),reply_token='fake-reply')
    server.processed_messages.discard(event.message.id)
    server.handle_message(event)
    assert len(calls)==int(vip)
    if vip: assert calls[0].as_json_dict()['altText']=='一週趨勢'
    assert path.read_bytes()==before


def test_google_worksheet_zero_id_and_numeric_zero_are_not_missing(tmp_path,monkeypatch):
    uid,path,book=setup(tmp_path,monkeypatch)
    book.sheet.id=0
    book.sheet.values[-1][3:5]=[0,0]
    raw=server.get_dashboard_data(uid,scope='home')
    assert raw['subscription_source_status']=='ok'
    assert raw['balance_sub_meals'][0]['kcal']==0
    assert raw['balance_sub_meals'][0]['protein']==0


@pytest.mark.parametrize('plan_state', ['active', 'none', 'expired', 'foreign', 'missing_entitlement_schema'])
def test_empty_mapping_warns_for_subscription_but_not_health_check_only(tmp_path, monkeypatch, plan_state):
    uid,path,book=setup(tmp_path,monkeypatch)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE health_profile SET sheet_name='' WHERE user_id=?", (uid,))
        conn.execute("INSERT INTO usage(user_id,status,remaining_meals,expiry_date) VALUES (?,'vip',20,'2099-12-31')", (uid,))
        if plan_state != 'none':
            owner = 'U-OTHER' if plan_state == 'foreign' else uid
            order = conn.execute("INSERT INTO subscription_orders(user_id,status,formalized_at) VALUES (?,'activated','2026-09-01')", (owner,)).lastrowid
            conn.execute("INSERT INTO subscription_menu_entitlements(user_id,order_id,status,expires_on) VALUES (?,?,'active',?)", (owner,order,'2020-01-01' if plan_state == 'expired' else '2099-12-31'))
        if plan_state == 'missing_entitlement_schema':
            conn.execute('DROP TABLE subscription_menu_entitlements')
        conn.commit()
    monkeypatch.setattr(server,'gc',SimpleNamespace(open_by_key=lambda *a:pytest.fail('Missing mapping must not guess a worksheet')))
    before=path.read_bytes()
    raw=server.get_dashboard_data(uid,scope='home')
    warn = plan_state in {'active','missing_entitlement_schema'}
    assert raw['subscription_source_status']==('unavailable' if warn else 'not_configured')
    assert raw['balance_sub_meals']==[]
    assert adapt_dashboard_data(raw)['subscription_source_unavailable'] is warn
    text=json.dumps(server.build_dashboard_flex(uid).as_json_dict(),ensure_ascii=False)
    assert ('包月出單資料暫時無法核對' in text) is warn
    assert path.read_bytes()==before

