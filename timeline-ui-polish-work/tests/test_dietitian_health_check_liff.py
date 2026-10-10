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
        'id="latestReview"', 'id="evidenceTab"', 'id="reviewTab"',
    ):
        assert marker in page
    assert "儲存草稿" not in page
    assert ">核准<" not in page
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
    with sqlite3.connect(fixture_db) as conn:
        conn.execute(
            """UPDATE vip_health_check_reviews
               SET status='approved',ai_observations_json=?,review_json=?,
                   suggested_values_json=?,limitations=?,approved_at=?
               WHERE review_version=2""",
            (
                json.dumps({
                    "pattern": "早餐規律", "patterns": ["三日均有早餐"],
                    "observation": "蛋白質分布可再平均", "observations": ["晚餐占比較高"],
                    "strengths": ["有記錄飲水"], "gaps": ["蔬菜不足"], "risks": ["鈉偏高"],
                }),
                json.dumps({
                    "good": "記錄完整", "priority": "增加蔬菜", "summary": "先調整午晚餐",
                    "strengths": ["早餐穩定", "蛋白質來源多元"],
                    "improvements": ["午餐補一份蔬菜"],
                    "recommendations": ["每日飲水 2000 ml"],
                }),
                json.dumps({
                    "calories_kcal": 1900, "protein_g": 105, "fat_g": 60,
                    "carbohydrate_g": 230, "fiber_g": 25, "sodium_mg": 2000,
                    "vegetable_servings": 4, "water_ml": 2000,
                }),
                "資料僅涵蓋三個有效日",
                "2026-09-05T08:30:00+08:00",
            ),
        )
        api_fixture = load_health_check_detail(conn, case_id="case-1")
    assert api_fixture and api_fixture["latest_review_available"] is True
    fixture_path = tmp_path / "api-fixture.json"
    fixture_path.write_text(json.dumps(api_fixture, ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        ["node", "--check", script_path], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    completed = subprocess.run(
        ["node", harness, script_path, fixture_path], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "dietitian LIFF photo behavior: PASS" in completed.stdout
