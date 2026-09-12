from __future__ import annotations

from pathlib import Path
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


def test_liff_photo_behavior_runs_in_real_javascript(tmp_path):
    from dietitian_health_check_liff import attach_dietitian_health_check_liff_routes

    app = FastAPI()
    attach_dietitian_health_check_liff_routes(app, _config())
    script_path = tmp_path / "app.js"
    script_path.write_text(
        TestClient(app).get("/dietitian-health-check/app.js").text,
        encoding="utf-8",
    )
    harness = Path(__file__).with_name("dietitian_health_check_liff_behavior.js")

    completed = subprocess.run(
        ["node", "--check", script_path], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    completed = subprocess.run(
        ["node", harness, script_path], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "dietitian LIFF photo behavior: PASS" in completed.stdout
