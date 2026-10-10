from fastapi import FastAPI
from fastapi.testclient import TestClient

from dietitian_health_check_api import DietitianHealthCheckConfig
from dietitian_health_check_liff import attach_dietitian_health_check_liff_routes


def _page() -> str:
    app = FastAPI()
    attached = attach_dietitian_health_check_liff_routes(
        app,
        DietitianHealthCheckConfig(
            enabled=True,
            channel_id="2011528194",
            allowed_uids=frozenset({"U-AUTHORIZED"}),
            liff_id="2011528194-EsxeCZ2a",
        ),
    )
    assert attached is True
    client = TestClient(app)
    return client.get("/dietitian-health-check").text + client.get(
        "/dietitian-health-check/app.js"
    ).text


def test_mobile_queue_restores_v1_kpis_tabs_and_cards_without_fake_completeness():
    page = _page()
    for marker in (
        'class="metrics"',
        'id="pendingMetric"',
        'id="supplementMetric"',
        'id="completedMetric"',
        "待審核",
        "待補件",
        "已完成",
        'class="case-list"',
    ):
        assert marker in page
    assert "82%" not in page
    assert "資料完整度" not in page
    assert "假資料" not in page


def test_detail_sections_follow_mobile_tabs_and_desktop_two_column_direction():
    page = _page()
    ordered = (
        'id="detailSummary"',
        'id="mealTimeline"',
        'id="aiSummary"',
        'id="dataLimitations"',
        'id="draftEditor"',
        'id="draftPreview"',
        'id="bottomActions"',
    )
    positions = [page.index(marker) for marker in ordered]
    assert positions == sorted(positions)
    assert 'id="recordsTab"' in page and "紀錄" in page
    assert 'id="highlightsTab"' in page and "重點" in page
    assert 'id="reportTab"' in page and "報告" in page
    assert 'id="reviewGrid" class="review-grid" data-mobile="records"' in page
    assert "@media(min-width:900px)" in page
    assert "grid-template-columns:minmax(0,1.08fr) minmax(360px,.92fr)" in page
    assert "顧客與案件摘要" in page
    assert "三天餐點紀錄" in page
    assert "AI 初步整理" in page
    assert "資料限制" in page
    assert "營養師最後審核" in page
    assert "顧客報告預覽" in page


def test_sticky_preview_is_primary_and_approval_exists_only_in_visible_preview():
    page = _page()
    assert '.bottom-actions{position:fixed' in page
    assert '.preview-actions{position:sticky' in page
    assert 'id="previewActions" class="draft-actions preview-actions"' in page
    assert 'padding-bottom:calc(104px + env(safe-area-inset-bottom))' in page
    assert '<button id="requestMoreInfo"' in page
    assert '<button id="saveDraft"' in page
    assert '<button id="openDraftPreview" class="primary"' in page
    assert page.index('id="approveReview"') > page.index('id="draftPreview"')
    assert "要求補件" in page
    assert "儲存草稿" in page
    assert "預覽正式報告" in page
    assert "確認核准" in page
    assert "核准送出" not in page
    assert "核准並送出" not in page
    assert "核准不等於送達" in page
    assert "資料限制：NA" not in page
    assert "審核參考，不含於送出內容" in page
    for label in ("做得好的地方", "目前優先事項", "接下來 7 天", "限制與提醒"):
        assert label in page
    assert 'id="mobileCaseContext"' in page
    assert 'id="mobileDraftState"' in page


def test_secondary_details_and_desktop_columns_do_not_reintroduce_mobile_overflow():
    page = _page()
    assert "技術資料" not in page
    assert "來源明細（選用）" in page
    assert ".secondary-details>summary" in page
    assert "height:calc(100vh - 220px);overflow:hidden" in page
    assert ".review-grid>#evidence,.review-grid>#reviewPane{height:100%;min-height:0;overflow-y:auto" in page
    assert "@media(min-width:900px)" in page
    assert "@media(max-width:480px)" in page


def test_protected_photos_are_real_on_demand_not_decorative_meal_plates():
    page = _page()
    assert "meal-photo::before" not in page
    assert "meal-photo::after" not in page
    assert "按需讀取受保護照片" in page
    assert "URL.createObjectURL(blob)" in page
    assert "response.blob()" in page
