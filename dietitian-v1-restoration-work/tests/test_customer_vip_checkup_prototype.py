from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototypes" / "customer-vip-3day-checkup-prototype.html"
PLAN = ROOT / "docs" / "plans" / "2026-09-03-vip-health-check-customer-flow.md"
DIETITIAN_PROTOTYPE = ROOT / "prototypes" / "dietitian-3day-checkup-prototype.html"


def _html() -> str:
    assert PROTOTYPE.exists(), "顧客端 VIP 3 日健檢雛形尚未建立"
    return PROTOTYPE.read_text(encoding="utf-8")


def test_customer_prototype_covers_complete_vip_checkup_journey():
    html = _html()
    required = (
        "VIP 首次開通權益",
        "7 天練習期",
        "3 個有效紀錄日",
        "0/3",
        "1/3",
        "2/3",
        "3/3",
        "AI 整理中",
        "營養師審核中",
        "3 日飲食基準報告",
        "你做得好的地方",
        "優先調整建議",
        "接下來 7 天行動",
        "4 週營養師陪跑",
        "人工匯款",
        "等待客服確認",
    )
    for text in required:
        assert text in html


def test_customer_prototype_reuses_existing_entrypoints_and_food_logs():
    html = _html()
    required = (
        "紀錄飲食 / FOOD LOG",
        "首頁／儀表板",
        "正式 Rich Menu 名稱待確認",
        "food_logs",
        "planned_meal",
        "午餐已吃",
        "晚餐已吃",
        "不新增第三個飲食紀錄入口",
        "不重複計算",
    )
    for text in required:
        assert text in html


def test_customer_prototype_keeps_payment_and_demo_data_safe():
    html = _html()
    assert "不另發序號" in html
    assert "不串自動金流" in html
    assert "此為假資料互動雛形" in html
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in html
    assert "GOOGLE_CREDENTIALS" not in html
    assert "215540069587" not in html
    assert "215-540-069587" not in html


def test_customer_prototype_has_interactive_state_controls():
    html = _html()
    for marker in (
        'data-demo-state="0"',
        'data-demo-state="1"',
        'data-demo-state="2"',
        'data-demo-state="3"',
        'data-demo-state="approved"',
        'data-demo-state="delivery-failed"',
        'data-demo-state="done"',
        'id="startCheckup"',
        'id="viewReport"',
        'id="viewCoaching"',
        'id="requestTransfer"',
        'id="backToHome"',
        "renderState",
        "7 天窗口已自 VIP 正式生效啟動",
        'id="coachingActiveScreen"',
        "4 週陪跑已開通",
    ):
        assert marker in html
    assert "開始我的 3 日健檢" not in html


def test_design_plan_separates_source_of_truth_delivery_and_add_on_entitlement():
    assert PLAN.exists(), "VIP 健檢整合設計文件尚未建立"
    plan = PLAN.read_text(encoding="utf-8")
    for marker in (
        "`food_logs` 是飲食事實來源",
        "health_profile.today_*` 只是今日快取／投影",
        "report_approved",
        "report_delivery_failed",
        "report_delivered",
        "唯一 delivery key",
        "UNIQUE(user_id, benefit_key)",
        "benefit_key",
        "first_vip_activation_id",
        "vip_health_check_valid_days",
        "UNIQUE(case_id, local_date)",
        "UNIQUE(case_id, food_log_id)",
        "窗口固定從首次 VIP 正式生效時間",
        "不得覆蓋 `food_logs.nutrition_snapshot_json`",
        "不得呼叫現有會產生 `#VIPORDER-*` 的 activated 分支",
        "不得寫入 `vips`",
    ):
        assert marker in plan
    assert "215540069587" not in plan


def test_dietitian_prototype_separates_approval_from_delivery():
    html = DIETITIAN_PROTOTYPE.read_text(encoding="utf-8")
    for marker in (
        "核准報告",
        "等待獨立投遞",
        'data-filter="approved"',
        'data-filter="failed"',
        "approved:[]",
        "failed:[]",
        'id="deliveryFailBtn"',
        'id="deliverySuccessBtn"',
        'id="retryDeliveryBtn"',
        "moveCurrentCase('pending','approved'",
        "moveCurrentCase('approved','failed'",
        "moveCurrentCase('approved','done'",
        "moveCurrentCase('failed','approved'",
        "approvedSnapshot",
        "applyApprovedSnapshot",
        "field.readOnly=locked",
        "localWorkflowRevision",
        "revisionAtStart",
        "requestCase",
        "currentCase?.approvedSnapshot||",
    ):
        assert marker in html
    assert "核准並送出" not in html


def test_design_plan_has_no_customer_start_write():
    plan = PLAN.read_text(encoding="utf-8")
    assert "「前往紀錄飲食」只作純導航" in plan
    assert "不改 `window_started_at`／`window_ends_at`" in plan
    assert "「開始／索取匯款」" not in plan
