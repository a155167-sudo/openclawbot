from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import sqlite3
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dietitian_health_check_api import DietitianHealthCheckConfig


def _config(*, enabled: bool = True) -> DietitianHealthCheckConfig:
    return DietitianHealthCheckConfig(
        enabled=enabled,
        channel_id="2011528194",
        allowed_uids=frozenset({"U-AUTHORIZED"}),
        liff_id="2011528194-EsxeCZ2a",
    )


def test_liff_routes_are_dark_when_read_api_is_disabled():
    from dietitian_health_check_liff import attach_dietitian_health_check_liff_routes

    app = FastAPI()
    assert attach_dietitian_health_check_liff_routes(app, _config(enabled=False)) is False
    client = TestClient(app)
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        for path in ("/dietitian-health-check", "/dietitian-health-check/app.js"):
            assert client.request(method, path).status_code == 404


def test_liff_shell_is_get_only_no_store_and_has_restrictive_csp():
    from dietitian_health_check_liff import attach_dietitian_health_check_liff_routes

    app = FastAPI()
    assert attach_dietitian_health_check_liff_routes(app, _config()) is True
    client = TestClient(app)

    page = client.get("/dietitian-health-check")
    script = client.get("/dietitian-health-check/app.js")

    assert page.status_code == 200
    assert script.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert script.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "https://static.line-scdn.net" in page.headers["content-security-policy"]
    assert "https://liffsdk.line-scdn.net" in page.headers["content-security-policy"]
    assert "https://uts-front.line-apps.com" in page.headers["content-security-policy"]
    assert "img-src 'self' data: blob:" in page.headers["content-security-policy"]
    assert client.post("/dietitian-health-check").status_code == 405
    assert client.post("/dietitian-health-check/app.js").status_code == 405


def test_liff_script_reads_live_collecting_and_ready_cases_without_persisting_token():
    from dietitian_health_check_liff import attach_dietitian_health_check_liff_routes

    app = FastAPI()
    attach_dietitian_health_check_liff_routes(app, _config())
    client = TestClient(app)
    script = client.get("/dietitian-health-check/app.js").text

    assert "2011528194-EsxeCZ2a" in script
    assert "/api/dietitian/health-checks?limit=25&offset=0" in script
    assert "/api/dietitian/health-checks/" in script
    assert "status=ready_for_review" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "source_image_ref" not in script
    assert "original_image_ref" not in script
    assert "U-AUTHORIZED" not in script
    assert "Authorization" in script
    assert "liff.getIDToken" in script
    assert "expected_source_token" in script
    assert "expected_case_updated_at" not in script
    assert "顧客確認・AI估算" in script
    assert "NA" in script
    assert "JSON.stringify((detail&&detail.source_logs)" not in script
    assert "有效日：" in script
    assert "保留來源參照：" in script
    assert "目前可驗證快照：" in script
    assert "目前無可顯示的來源快照；不代表沒有飲食紀錄" in script
    assert "資料不可用" in script
    assert "customer_name" not in script
    assert "waiting_hours" not in script
    assert "待投遞" in script
    assert "已送達" in script
    # Per-case hydration must not wait for all details before showing the list.
    assert "Promise.allSettled" not in script


def test_liff_shell_restores_v2_queue_and_read_only_evidence_workspace():
    from dietitian_health_check_liff import attach_dietitian_health_check_liff_routes

    app = FastAPI()
    attach_dietitian_health_check_liff_routes(app, _config())
    page = TestClient(app).get("/dietitian-health-check").text

    for marker in (
        'id="queue"', 'id="filters"', 'id="search"', 'id="caseList"',
        'id="review"', 'id="backQueue"', 'id="profile"', 'id="limitations"',
        'id="validDays"', 'id="sourceIntegrity"', 'id="timelineTabs"',
        'id="timelineDays"', 'id="sourceLogs"',
        'id="latestReview"', 'id="recordsTab"', 'id="highlightsTab"', 'id="reportTab"',
    ):
        assert marker in page
    for marker in (
        'id="good"', 'id="priority"', 'id="next_7_days"', 'id="comment"',
        'id="saveDraft"', 'id="openDraftPreview"', 'id="draftPreview"',
        'id="draftState"', 'id="draftError"', 'id="approveReview"',
        'id="approvalState"', 'id="approvalError"', 'id="supplementEditor"',
        'id="supplementControls"',
        'id="supplementReason"', 'id="supplementRequiredContent"',
        'id="requestMoreInfo"', 'id="supplementState"',
        'id="supplementError"', 'id="supplementReadback"',
    ):
        assert marker in page
    assert "儲存草稿" in page
    assert "草稿未送達" in page
    assert ">確認核准<" in page
    assert "鎖版尚未送達" in page
    assert "模擬送出" not in page


def test_liff_shell_keeps_mobile_status_and_nutrition_values_readable():
    from dietitian_health_check_liff import _html

    page = _html()
    assert ".heading>div{min-width:0}" in page
    assert ".pill{display:inline-block;flex:0 0 auto;min-width:max-content;white-space:nowrap" in page
    assert ".nutrition span{min-width:0;overflow-wrap:anywhere}" in page
    assert ".nutrition-value{white-space:nowrap}" in page
    assert '@media(max-width:480px){.nutrition{grid-template-columns:1fr}' in page


def test_liff_photo_behavior_runs_in_real_javascript(tmp_path):
    from dietitian_health_check_liff import attach_dietitian_health_check_liff_routes
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_approval import create_health_check_approval_saver
    from dietitian_health_check_draft import create_health_check_draft_saver
    from dietitian_health_check_delivery import create_health_check_delivery_service
    from dietitian_health_check_supplement import create_health_check_supplement_saver

    app = FastAPI()
    attach_dietitian_health_check_liff_routes(app, _config())
    script_path = tmp_path / "app.js"
    script_path.write_text(
        TestClient(app).get("/dietitian-health-check/app.js").text,
        encoding="utf-8",
    )
    harness = Path(__file__).with_name("dietitian_health_check_liff_behavior.js")
    api_test_path = Path(__file__).with_name("test_dietitian_health_check_api.py")
    api_test_spec = importlib.util.spec_from_file_location("health_check_api_fixture", api_test_path)
    assert api_test_spec and api_test_spec.loader
    api_test_module = importlib.util.module_from_spec(api_test_spec)
    api_test_spec.loader.exec_module(api_test_module)
    fixture_db = api_test_module._populated_db(tmp_path)
    draft_saver = create_health_check_draft_saver(fixture_db)
    approval_saver = create_health_check_approval_saver(fixture_db)
    with sqlite3.connect(fixture_db) as conn:
        before = load_health_check_detail(conn, case_id="case-1")
    assert before and isinstance(before.get("source_token"), str)
    fields = {
        "good": "記錄完整",
        "priority": "增加蔬菜",
        "next_7_days": "午餐補一份蔬菜",
        "comment": "每日飲水 2000 ml",
    }
    saved = draft_saver(
        "case-1", fields, before["source_token"], before["current_review_version"],
        "ui-fixture-draft", "U-AUTHORIZED",
    )
    client, _seen = api_test_module._client(
        loader=lambda *_args, **_kwargs: {"items": []},
        detail_loader=lambda case_id: load_health_check_detail(
            sqlite3.connect(fixture_db), case_id=case_id
        ),
        approval_saver=approval_saver,
    )
    approved_response = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers={"Authorization": "Bearer signed"},
        json={
            "expected_source_token": before["source_token"],
            "expected_review_version": saved["review_version"],
            "request_id": "ui-fixture-approve",
        },
    )
    assert approved_response.status_code == 200, approved_response.text
    detail_response = client.get(
        "/api/dietitian/health-checks/case-1",
        headers={"Authorization": "Bearer signed"},
    )
    assert detail_response.status_code == 200, detail_response.text
    api_fixture = detail_response.json()
    assert api_fixture["latest_review_available"] is True
    assert api_fixture["approval"]["report_id"] == approved_response.json()["report_id"]
    assert api_fixture["approval"]["delivery_status"] == "pending"
    with sqlite3.connect(fixture_db) as conn:
        persisted_report = json.loads(conn.execute(
            "SELECT report_json FROM vip_health_check_reports WHERE report_id=?",
            (approved_response.json()["report_id"],),
        ).fetchone()[0])
        delivery_key = conn.execute(
            "SELECT delivery_key FROM vip_health_check_deliveries WHERE report_id=?",
            (approved_response.json()["report_id"],),
        ).fetchone()[0]
    sent: list[dict[str, object]] = []
    delivered = create_health_check_delivery_service(
        fixture_db,
        sender=lambda recipient, message, retry_key: sent.append(
            {"recipient": recipient, "message": message, "retry_key": retry_key}
        ),
    )(delivery_key)
    assert delivered["status"] == "delivered"
    assert len(sent) == 1
    line_sections = [
        {"label": section["contents"][0]["text"], "text": section["contents"][1]["text"]}
        for section in sent[0]["message"]["contents"]["body"]["contents"]
    ]
    report_contract_path = tmp_path / "actual-report-contract.json"
    report_contract_path.write_text(json.dumps({
        "saved_review": fields,
        "persisted_report": persisted_report,
        "line_sections": line_sections,
    }, ensure_ascii=False), encoding="utf-8")
    fixture_path = tmp_path / "api-fixture.json"
    fixture_path.write_text(json.dumps(api_fixture, ensure_ascii=False), encoding="utf-8")

    supplement_dir = tmp_path / "supplement-fixture"
    supplement_dir.mkdir()
    supplement_db = api_test_module._populated_db(supplement_dir)
    supplement_draft_saver = create_health_check_draft_saver(supplement_db)
    supplement_saver = create_health_check_supplement_saver(supplement_db)
    with sqlite3.connect(supplement_db) as conn:
        supplement_before = load_health_check_detail(conn, case_id="case-1")
    assert supplement_before
    supplement_saved = supplement_draft_saver(
        "case-1", fields, supplement_before["source_token"],
        supplement_before["current_review_version"], "ui-fixture-supplement-draft",
        "U-AUTHORIZED",
    )
    supplement_client, _seen = api_test_module._client(
        loader=lambda *_args, **_kwargs: {"items": []},
        detail_loader=lambda case_id: load_health_check_detail(
            sqlite3.connect(supplement_db), case_id=case_id
        ),
        supplement_saver=supplement_saver,
    )
    supplement_response = supplement_client.post(
        "/api/dietitian/health-checks/case-1/request-more-info",
        headers={"Authorization": "Bearer signed"},
        json={
            "reason": "資料不足以判讀",
            "required_content": "請補早餐照片與份量",
            "expected_source_token": supplement_before["source_token"],
            "expected_review_version": supplement_saved["review_version"],
            "request_id": "ui-fixture-supplement",
        },
    )
    assert supplement_response.status_code == 200, supplement_response.text
    assert supplement_response.json() == {
        "status": "needs_more_info", "notification_status": "not_sent", "created": True,
    }
    supplement_detail_response = supplement_client.get(
        "/api/dietitian/health-checks/case-1",
        headers={"Authorization": "Bearer signed"},
    )
    assert supplement_detail_response.status_code == 200, supplement_detail_response.text
    supplement_fixture_path = tmp_path / "supplement-api-fixture.json"
    supplement_fixture_path.write_text(
        json.dumps(supplement_detail_response.json(), ensure_ascii=False), encoding="utf-8"
    )
    with sqlite3.connect(supplement_db) as conn:
        conn.execute(
            """UPDATE health_check_supplement_requests
               SET notification_status='queued' WHERE case_id='case-1'"""
        )
    queued_detail_response = supplement_client.get(
        "/api/dietitian/health-checks/case-1",
        headers={"Authorization": "Bearer signed"},
    )
    assert queued_detail_response.status_code == 200, queued_detail_response.text
    queued_fixture_path = tmp_path / "supplement-queued-api-fixture.json"
    queued_fixture_path.write_text(
        json.dumps(queued_detail_response.json(), ensure_ascii=False), encoding="utf-8"
    )
    with sqlite3.connect(supplement_db) as conn:
        conn.execute(
            """UPDATE health_check_supplement_requests
               SET notification_status='not_sent' WHERE case_id='case-1'"""
        )
    from dietitian_health_check_supplement_notification import (
        create_health_check_supplement_notification_service,
    )
    delivered = create_health_check_supplement_notification_service(
        supplement_db, sender=lambda _uid, _message, _retry_key: None
    )("case-1")
    assert delivered["status"] == "delivered"
    delivered_detail_response = supplement_client.get(
        "/api/dietitian/health-checks/case-1",
        headers={"Authorization": "Bearer signed"},
    )
    assert delivered_detail_response.status_code == 200, delivered_detail_response.text
    delivered_fixture_path = tmp_path / "supplement-delivered-api-fixture.json"
    delivered_fixture_path.write_text(
        json.dumps(delivered_detail_response.json(), ensure_ascii=False), encoding="utf-8"
    )

    completed = subprocess.run(
        ["node", "--check", script_path], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    completed = subprocess.run(
        ["node", harness, script_path, fixture_path, supplement_fixture_path, delivered_fixture_path, queued_fixture_path, report_contract_path],
        capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "dietitian LIFF photo behavior: PASS" in completed.stdout
