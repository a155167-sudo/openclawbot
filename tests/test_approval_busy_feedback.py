import json
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


HTML = Path(__file__).resolve().parents[1] / "customer-reschedule-normal-liff.html"
CHROME = "/home/win-xi/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"
REQUEST_ID = "RS_ui_feedback_1"
CAPABILITY = "claim-aware consumer capability is not active"


@pytest.fixture
def page_and_api():
    html = HTML.read_text().replace(
        "window.__RESCHEDULE_RUNTIME__ = null;",
        'window.__RESCHEDULE_RUNTIME__ = {"liffId":"synthetic-liff"};',
    )
    api = {"mode": "held", "approvals": [], "refreshes": 0, "reconciles": 0}
    item = {
        "request_id": REQUEST_ID,
        "source_date": "2026-10-01",
        "target_date": "2026-10-02",
        "status": "pending_admin",
        "can_approve": True,
        "can_reconcile": True,
    }

    def reply(route, status, body):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def handle(route):
        path = route.request.url.split("?", 1)[0]
        if route.request.resource_type == "document":
            route.fulfill(status=200, content_type="text/html", body=html)
        elif route.request.resource_type == "script":
            route.fulfill(status=200, body="")
        elif path.endswith("/customer-reschedule/context"):
            reply(route, 403, {"detail": "admin only"})
        elif path.endswith("/customer-pair-reschedule-requests"):
            api["refreshes"] += 1
            reply(route, 200, {"requests": [item]})
        elif path.endswith(f"/{REQUEST_ID}/approve"):
            api["approvals"].append(route)
            if api["mode"] == "capability":
                reply(route, 403, {"detail": CAPABILITY})
            elif api["mode"] == "network":
                route.abort("failed")
            elif api["mode"] == "other":
                reply(route, 409, {"detail": "另一個明確拒絕"})
        elif path.endswith(f"/{REQUEST_ID}/reconcile"):
            api["reconciles"] += 1
            reply(route, 200, {"status": "pending_admin"})
        else:
            pytest.fail(f"unexpected request: {route.request.url}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.set_default_timeout(3000)
            page.add_init_script(
                'window.liff={init:async()=>{},isLoggedIn:()=>true,getIDToken:()=>"synthetic-token"};'
            )
            page.route("**/*", handle)
            page.goto("http://approval.test/customer-reschedule", wait_until="networkidle")
            expect(page.locator("#pending > div")).to_have_count(1)
            yield page, api
        finally:
            browser.close()


def card(page):
    return page.locator("#pending > div").filter(has_text=REQUEST_ID)


def approve_button(page):
    return card(page).get_by_role("button", name="核准此申請")


def test_busy_feedback_survives_refresh_and_one_post(page_and_api):
    page, api = page_and_api
    approve_button(page).click()
    expect(card(page)).to_contain_text("核准處理中，請勿重複點擊")
    expect(card(page).get_by_role("button", name="核准處理中")).to_be_disabled()
    page.locator("#refresh").click()
    expect(card(page)).to_contain_text("核准處理中，請勿重複點擊")
    expect(card(page).get_by_role("button", name="核准處理中")).to_be_disabled()
    card(page).get_by_role("button", name="核准處理中").evaluate("button => button.click()")
    assert len(api["approvals"]) == 1
    api["approvals"][0].fulfill(
        status=200, content_type="application/json", body=json.dumps({"status": "confirmed"})
    )
    expect(card(page)).to_contain_text("午、晚餐一起改期")
    assert len(api["approvals"]) == 1


def test_capability_rejection_stays_on_card_and_blocks_until_reload(page_and_api):
    page, api = page_and_api
    api["mode"] = "capability"
    approve_button(page).click()
    message = "出單安全準備尚未完成，本次未執行改期，請勿重複核准"
    expect(card(page)).to_contain_text(message)
    assert CAPABILITY not in card(page).inner_text()
    page.locator("#refresh").click()
    expect(card(page)).to_contain_text(message)
    assert card(page).get_by_role("button", name="核准此申請").is_disabled()
    card(page).get_by_role("button", name="核准此申請").evaluate("button => button.click()")
    assert len(api["approvals"]) == 1
    page.reload(wait_until="networkidle")
    expect(approve_button(page)).to_be_enabled()


def test_network_unknown_blocks_approve_but_allows_refresh_and_reconcile(page_and_api):
    page, api = page_and_api
    api["mode"] = "network"
    approve_button(page).click()
    message = "結果待確認，請刷新查核，不要重送"
    expect(card(page)).to_contain_text(message)
    page.locator("#refresh").click()
    expect(card(page)).to_contain_text(message)
    assert card(page).get_by_role("button", name="核准此申請").is_disabled()
    card(page).get_by_role("button", name="核准此申請").evaluate("button => button.click()")
    assert len(api["approvals"]) == 1
    card(page).get_by_role("button", name="讀回確認狀態").click()
    expect(page.locator("#result")).to_contain_text("讀回結果")
    assert api["reconciles"] == 1
    assert api["refreshes"] >= 2


def test_other_http_rejection_keeps_card_error_and_allows_retry(page_and_api):
    page, api = page_and_api
    api["mode"] = "other"
    approve_button(page).click()
    expect(card(page)).to_contain_text("另一個明確拒絕")
    expect(approve_button(page)).to_be_enabled()
    page.locator("#refresh").click()
    expect(card(page)).to_contain_text("另一個明確拒絕")
    with page.expect_request(f"**/{REQUEST_ID}/approve"):
        approve_button(page).click()
    assert len(api["approvals"]) == 2
