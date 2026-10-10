from types import SimpleNamespace
import pytest
from customer_health_check_liff import verify_line_id_token, LineAuthenticationError


def test_provider_rejection_has_only_safe_reason_code():
    secret = 'do-not-log-this-token-or-body'
    response = SimpleNamespace(status_code=400, json=lambda: {'error_description': secret})
    with pytest.raises(LineAuthenticationError) as caught:
        verify_line_id_token(secret, channel_id='2011528194', http_post=lambda *a, **k: response)
    assert getattr(caught.value, 'reason_code', None) == 'A02'
    assert secret not in str(caught.value)


@pytest.mark.parametrize('description,code', [
    ('Invalid IdToken.', 'L01'), ('Invalid IdToken Issuer.', 'L02'),
    ('IdToken expired.', 'L03'), ('Invalid IdToken Audience.', 'L04'),
    ('Invalid IdToken Nonce.', 'L05'), ('Invalid IdToken Subject Identifier.', 'L06'),
])
def test_official_provider_rejection_classification(description, code):
    response=SimpleNamespace(status_code=400,json=lambda:{'error':'invalid_request','error_description':description})
    with pytest.raises(LineAuthenticationError) as caught:
        verify_line_id_token('synthetic-secret',channel_id='2011528194',http_post=lambda *a,**k:response)
    assert caught.value.reason_code == code
    assert description not in str(caught.value)


@pytest.mark.parametrize('token', ['', 'null', 'undefined'])
def test_missing_token_never_calls_line(token):
    def forbidden(*a, **k):
        pytest.fail('missing token must not contact LINE')
    with pytest.raises(LineAuthenticationError) as caught:
        verify_line_id_token(token, channel_id='2011528194', http_post=forbidden)
    assert caught.value.reason_code == 'A01'


@pytest.mark.parametrize('field,value,code', [('iss', 'wrong', 'A03'), ('aud', 'wrong', 'A04'), ('sub', 'wrong', 'A05')])
def test_claim_failures_are_distinguishable(field, value, code):
    payload = {'iss':'https://access.line.me', 'aud':'2011528194', 'sub':'U'+'1'*32}
    payload[field] = value
    with pytest.raises(LineAuthenticationError) as caught:
        verify_line_id_token('synthetic', channel_id='2011528194', http_post=lambda *a, **k: SimpleNamespace(status_code=200, json=lambda:payload))
    assert caught.value.reason_code == code


@pytest.mark.parametrize('normal', [False, True])
def test_route_exposes_only_allowlisted_code_in_normal_mode(tmp_path, normal, caplog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from customer_reschedule_liff_routes import create_customer_reschedule_router
    def reject(*a, **k):
        raise LineAuthenticationError('sensitive-body-must-not-escape', reason_code='A04')
    app=FastAPI()
    app.include_router(create_customer_reschedule_router(liff_id='2011528194-td43IPq1',channel_id='2011528194',app_env='staging',db_path=str(tmp_path/'never-created.db'),token_verifier=reject,normal_flow=normal))
    r=TestClient(app).get('/customer-reschedule/context',headers={'Authorization':'Bearer synthetic-secret'})
    assert r.status_code == 401
    assert r.headers['cache-control']=='no-store'
    assert r.json() == ({'detail':'LINE 登入無效','auth_code':'A04'} if normal else {'detail':'LINE 登入無效'})
    assert not (tmp_path/'never-created.db').exists()
    assert 'synthetic-secret' not in caplog.text+r.text
    assert 'sensitive-body' not in caplog.text+r.text
    if normal: assert 'A04' in caplog.text


@pytest.mark.parametrize('token', [None, '', 'synthetic-valid-shape'])
def test_real_page_token_guard_and_safe_code(token):
    import json
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    html=Path(__file__).resolve().parents[1].joinpath('customer-reschedule-normal-liff.html').read_text().replace('window.__RESCHEDULE_RUNTIME__ = null;', 'window.__RESCHEDULE_RUNTIME__ = {"liffId":"2011528194-td43IPq1"};')
    calls=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/home/win-xi/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome',headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport={'width':390,'height':844})
            page.add_init_script('window.__RESCHEDULE_RUNTIME__={liffId:"2011528194-td43IPq1"};window.liff={init:async()=>{},isLoggedIn:()=>true,getIDToken:()=>'+json.dumps(token)+'};')
            def handle(route):
                if route.request.resource_type=='document': route.fulfill(status=200,content_type='text/html',body=html)
                elif route.request.resource_type=='script': route.fulfill(status=200,body='')
                else:
                    calls.append(route.request.url)
                    route.fulfill(status=401,content_type='application/json',body=json.dumps({'detail':'LINE 登入無效','auth_code':'A04'}))
            page.route('**/*',handle)
            page.goto('http://auth.test/customer-reschedule',wait_until='networkidle')
            assert page.locator('#submit').is_disabled()
            if not token:
                assert calls == [], 'missing SDK ID token must not call protected APIs'
                assert 'A01' in page.locator('body').inner_text()
            else:
                assert calls
                assert 'A04' in page.locator('body').inner_text()
        finally: browser.close()


@pytest.mark.parametrize('in_client', [False, True])
def test_relogin_is_explicit_and_does_not_replay_requests(in_client):
    import json
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    html=Path(__file__).resolve().parents[1].joinpath('customer-reschedule-normal-liff.html').read_text().replace('window.__RESCHEDULE_RUNTIME__ = null;', 'window.__RESCHEDULE_RUNTIME__ = {"liffId":"2011528194-td43IPq1"};')
    calls=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/home/win-xi/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome',headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport={'width':390,'height':844})
            page.add_init_script('window.authActions=[];window.liff={init:async()=>{},isLoggedIn:()=>true,getIDToken:()=>"synthetic-expired",isInClient:()=>'+json.dumps(in_client)+',logout:()=>authActions.push("logout"),login:(o)=>authActions.push(o)};')
            def handle(route):
                if route.request.resource_type=='document': route.fulfill(status=200,content_type='text/html',body=html)
                elif route.request.resource_type=='script': route.fulfill(status=200,body='')
                else:
                    calls.append(route.request.url)
                    route.fulfill(status=401,content_type='application/json',body=json.dumps({'detail':'LINE 登入無效','auth_code':'L03'}))
            page.route('**/*',handle)
            page.goto('http://auth.test/customer-reschedule?code=synthetic-old&state=fake',wait_until='networkidle')
            assert len(calls)==1, 'stop subsequent API calls after authentication fails'
            assert 'L03' in page.locator('body').inner_text()
            assert page.evaluate('authActions')==[]
            assert page.locator('#refresh').is_disabled()
            assert page.locator('#reauth').count()==1
            assert page.locator('#reauth').is_visible()
            page.locator('#reauth').click()
            assert len(calls)==1
            if in_client:
                assert page.evaluate('authActions')==[]
                assert '關閉' in page.locator('#contextStatus').inner_text()
            else:
                assert page.evaluate('authActions')==['logout', {'redirectUri':'http://auth.test/customer-reschedule'}]
            assert page.locator('#submit').is_disabled()
        finally: browser.close()
