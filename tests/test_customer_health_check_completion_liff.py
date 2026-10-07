from __future__ import annotations

import json
from pathlib import Path
import runpy
import sqlite3
import subprocess


def test_customer_completion_emitted_javascript_uses_actual_get_post_contract(tmp_path):
    helpers = runpy.run_path(str(Path(__file__).with_name("test_customer_health_check_supplement_return.py")))
    path = helpers["_pending_request"](tmp_path)
    client = helpers["_customer_client"](path)
    headers = {"Authorization": f"Bearer {helpers['TOKEN']}"}
    pending = client.get("/api/vip-health-check/me", headers=headers)
    assert pending.status_code == 200, pending.text
    supplement = pending.json()["state"]["supplement_request"]
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE food_logs SET nutrition_snapshot_json='{"calories_kcal":520}', version=4
               WHERE log_id='log-owned' AND user_id=?""",
            (helpers["CUSTOMER_UID"],),
        )
    posted = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers=headers,
        json={
            "request_id": "customer-ui-fixture",
            "expected_supplement_request_id": supplement["supplement_request_id"],
            "expected_source_token": supplement["expected_source_token"],
        },
    )
    assert posted.status_code == 200, posted.text
    completed = client.get("/api/vip-health-check/me", headers=headers)
    assert completed.status_code == 200, completed.text
    page = client.get("/vip-health-check")
    html_path = tmp_path / "customer.html"
    fixture_path = tmp_path / "customer-fixtures.json"
    html_path.write_text(page.text, encoding="utf-8")
    fixture_path.write_text(json.dumps({"pending": pending.json(), "post": posted.json(), "completed": completed.json()}, ensure_ascii=False), encoding="utf-8")
    check = subprocess.run(["node", "--check", Path(__file__).with_name("customer_health_check_liff_behavior.js")], capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr
    result = subprocess.run(["node", Path(__file__).with_name("customer_health_check_liff_behavior.js"), html_path, fixture_path], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "customer supplement completion LIFF: PASS" in result.stdout
