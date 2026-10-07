import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from fastapi import FastAPI
from playwright.sync_api import expect,sync_playwright
from customer_reschedule_liff_routes import create_customer_reschedule_router
from reschedule_service_integration import submit_customer_pair_reschedule_pending,verify_customer_reschedule_context
from test_normal_reschedule_integration import normal_db,TARGET
from test_pair_reschedule_coordinator import ADMIN,OWNER,SOURCE,NOW
ROOT=Path(__file__).resolve().parents[1]
CHROME='/home/win-xi/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'


def test_real_preview_page_at_390px_has_no_write_controls_or_database_changes(tmp_path):
    path=tmp_path/'preview.sqlite3'; conn=normal_db(path)
    owner=verify_customer_reschedule_context(conn,actor_id=OWNER,order_id=1)
    submit_customer_pair_reschedule_pending(conn,context=owner,source_date=SOURCE,target_date=TARGET,request_id='RS_preview_phone',now=NOW,feature_enabled=True)
    conn.commit();before=list(conn.iterdump());conn.close()
    app=FastAPI();app.include_router(create_customer_reschedule_router(
        liff_id='1234567890-preview',channel_id='1234567890',db_path=str(path),app_env='production',
        pair_reschedule_enabled=True,normal_flow=True,preview_only=True,now_factory=lambda:NOW,
        token_verifier=lambda token,**_:ADMIN,html_path=ROOT/'customer-reschedule-normal-liff.html'))
    audit=[];errors=[]
    async def native(method,url,headers,content):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://preview.test') as client:
            return await client.request(method,url,headers=headers,content=content)
    def handle(route):
        if route.request.resource_type=='script':
            route.fulfill(status=200,body='');return
        args=(route.request.method,route.request.url,route.request.headers,route.request.post_data or '')
        response=pool.submit(lambda:asyncio.run(native(*args))).result(timeout=10)
        audit.append((route.request.method,route.request.url,response.status_code))
        route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k!='content-length'},body=response.content)
    with ThreadPoolExecutor(max_workers=1) as pool,sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=CHROME,headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport={'width':390,'height':844})
            page.on('pageerror',lambda err:errors.append(str(err)))
            page.add_init_script('window.liff={init:async()=>{},isLoggedIn:()=>true,getIDToken:()=>"synthetic-admin"};')
            page.route('**/*',handle)
            page.goto('https://preview.test/customer-reschedule?request_id=RS_preview_phone',wait_until='networkidle')
            expect(page.locator('#previewOnlyBanner')).to_be_visible()
            card=page.locator('#pending > div').filter(has_text='RS_preview_phone')
            expect(card).to_contain_text('唯讀預覽')
            expect(card.get_by_role('button',name='核准此申請')).to_have_count(0)
            expect(card.get_by_role('button',name='讀回確認狀態')).to_have_count(0)
            expect(page.locator('#submit')).to_be_disabled()
            page.locator('#refresh').click();expect(card).to_contain_text(SOURCE)
            assert not any(method!='GET' for method,_,_ in audit)
            assert not errors
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(ROOT.parent/'NANJING-PREVIEW-390.png'),full_page=True)
        finally:browser.close()
    with sqlite3.connect(path) as check:assert list(check.iterdump())==before
    (ROOT.parent/'PREVIEW-BROWSER-AUDIT.json').write_text(json.dumps({'fixture':'synthetic native database / fake verified token, NOT live phone E2E','native_http_audit':audit,'javascript_errors':errors,'database_unchanged':True,'automatic_write_requests':0},ensure_ascii=False,indent=2))
