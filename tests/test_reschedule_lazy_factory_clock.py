"""Fractional real-world clock through factory, real import and mounted approval. All data synthetic."""
import ast
import asyncio
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
import pytest
import httpx
from fastapi import FastAPI
import dispatch_authority_bridge as bridge
import reschedule_dispatch_versions as versions
import test_pair_reschedule_coordinator as fixture
from test_normal_reschedule_integration import normal_db, SOURCE, TARGET, NOW, ADMIN, OWNER
from test_reschedule_service_integration import sheet_fixture
from gspread_pair_reschedule_adapter import GspreadPairRescheduleAdapter
from customer_reschedule_liff_routes import create_customer_reschedule_router
from reschedule_service_integration import verify_customer_reschedule_context,submit_customer_pair_reschedule_pending
ROOT=Path(__file__).resolve().parents[1]
INSTANT=NOW.replace(microsecond=123456)

def setup_cold(tmp_path,monkeypatch,clock=INSTANT):
    # Existing owning fixture normally pre-imports baseline. Skip that setup ONLY;
    # the factory below uses the unmodified real bridge importer.
    with monkeypatch.context() as m:
        m.setattr(fixture,'import_initial_published_version',lambda *a,**k:None)
        conn=normal_db(tmp_path/'cold.sqlite3')
    bridge.ensure_dispatch_authority_bridge_schema(conn)
    conn.commit()
    assert conn.execute('SELECT COUNT(*) FROM dispatch_authority_version_bindings').fetchone()[0]==0
    adapter,book=sheet_fixture()
    book.worksheet=lambda title: next(ws for ws in book.worksheets.values() if ws.title==title)
    calls=[]
    class Client:
        def open_by_key(self,key):
            assert key=='book-1';calls.append(key);return book
    tree=ast.parse((ROOT/'server.py').read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='create_reschedule_sheet_adapter')
    ns={'gc':Client(),'SPREADSHEET_ID':'book-1','tw_now':lambda:clock,
        'import_initial_published_version':bridge.import_initial_published_version,
        'GspreadPairRescheduleAdapter':GspreadPairRescheduleAdapter}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(ROOT/'server.py'),'exec'),ns)
    assert Path(bridge.__file__).resolve()==ROOT/'dispatch_authority_bridge.py'
    assert Path(versions.__file__).resolve()==ROOT/'reschedule_dispatch_versions.py'
    return conn,book,calls,ns['create_reschedule_sheet_adapter']

def test_fractional_clock_factory_imports_real_baseline_preserving_original_rows(tmp_path,monkeypatch):
    conn,book,calls,factory=setup_cold(tmp_path,monkeypatch)
    before=[tuple(r) for r in conn.execute('SELECT * FROM subscription_dispatch_rows')]
    receipts=[tuple(r) for r in conn.execute('SELECT * FROM subscription_dispatch_publication_receipts')]
    result=factory(conn,1)
    assert isinstance(result,GspreadPairRescheduleAdapter)
    assert calls==['book-1'] and book.batch_calls==[]
    baseline=conn.execute('SELECT owner_user_id,created_at FROM reschedule_dispatch_versions').fetchone()
    assert tuple(baseline)==(OWNER,INSTANT.isoformat(timespec='seconds'))
    assert conn.execute('SELECT COUNT(*) FROM dispatch_authority_version_bindings WHERE order_id=1').fetchone()[0]==1
    assert before==[tuple(r) for r in conn.execute('SELECT * FROM subscription_dispatch_rows')]
    assert receipts==[tuple(r) for r in conn.execute('SELECT * FROM subscription_dispatch_publication_receipts')]
    result=factory(conn,1)
    assert conn.execute('SELECT COUNT(*) FROM reschedule_dispatch_versions').fetchone()[0]==1
    assert book.batch_calls==[]

@pytest.mark.parametrize('clock',[INSTANT.replace(tzinfo=None)])
def test_naive_clock_remains_denied_before_google_and_rolls_back(tmp_path,monkeypatch,clock):
    conn,book,calls,factory=setup_cold(tmp_path,monkeypatch,clock)
    with pytest.raises(versions.RescheduleConflict):factory(conn,1)
    assert calls==[] and book.batch_calls==[]
    assert conn.execute('SELECT COUNT(*) FROM reschedule_dispatch_versions').fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM dispatch_authority_version_bindings').fetchone()[0]==0

def test_mounted_approval_with_lazy_factory_and_fractional_clock_confirms_and_replay_no_write(tmp_path,monkeypatch):
    conn,book,calls,factory=setup_cold(tmp_path,monkeypatch)
    context=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
    submit_customer_pair_reschedule_pending(conn,context=context,source_date=SOURCE,target_date=TARGET,request_id='RS_fractional',now=NOW,feature_enabled=True)
    conn.close()
    app=FastAPI();app.include_router(create_customer_reschedule_router(liff_id='1234567890-normal',channel_id='1234567890',db_path=str(tmp_path/'cold.sqlite3'),app_env='staging',pair_reschedule_enabled=True,normal_flow=True,now_factory=lambda:NOW,token_verifier=lambda token,**_:ADMIN if token=='admin' else OWNER,sheet_factory=factory))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            url='/api/admin/customer-pair-reschedule-requests/RS_fractional/approve'
            denied=await client.post(url,headers={'Authorization':'Bearer owner'})
            assert denied.status_code==403 and calls==[] and book.batch_calls==[]
            response=await client.post(url,headers={'Authorization':'Bearer admin'})
            assert response.status_code==200,response.text
            assert response.json()['status']=='confirmed'
            count=len(book.batch_calls);assert count==1
            replay=await client.post(url,headers={'Authorization':'Bearer admin'})
            assert replay.status_code==200 and replay.json()['status']=='confirmed'
            assert len(book.batch_calls)==count
    asyncio.run(exercise())
