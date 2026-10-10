from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
UID = "U1234567890abcdef1234567890abcdef"
CHANNEL = "2009251085"


def _run_server(tmp_path, script: str, **feature_env: str):
    data_dir = tmp_path / "isolated-data"
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("DIETITIAN_HEALTH_CHECK_") or key.startswith("VIP_HEALTH_CHECK_"):
            env.pop(key)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "APP_ENV": "legacy",
            "ENABLE_SCHEDULER": "false",
            "DATA_DIR": str(data_dir),
            "OPENAI_API_KEY": "dummy",
            "LINE_CHANNEL_ACCESS_TOKEN": "dummy",
            "LINE_CHANNEL_SECRET": "dummy",
            **feature_env,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=90,
    )
    return result, data_dir


def _last_json(stdout: str):
    return json.loads(stdout.strip().splitlines()[-1])


def test_server_import_defaults_dietitian_routes_dark_in_isolated_data_dir(tmp_path):
    result, data_dir = _run_server(
        tmp_path,
        """import json, server
paths={r.path for r in server.app.routes if hasattr(r,'path')}
print(json.dumps({'enabled':server.DIETITIAN_HEALTH_CHECK_CONFIG.enabled,
 'list':'/api/dietitian/health-checks' in paths,
 'detail':'/api/dietitian/health-checks/{case_id}' in paths,
 'image':'/api/dietitian/health-checks/{case_id}/sources/{log_id}/image' in paths,
 'page':'/dietitian-health-check' in paths,
 'script':'/dietitian-health-check/app.js' in paths}))""",
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {
        "enabled": False, "list": False, "detail": False, "image": False,
        "page": False, "script": False,
    }
    assert (data_dir / "user_quota.db").exists()


def test_server_import_rejects_unknown_feature_flag_before_creating_database(tmp_path):
    result, data_dir = _run_server(
        tmp_path,
        "import server",
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="tru",
    )
    assert result.returncode != 0
    assert "DIETITIAN_HEALTH_CHECK_READ_ENABLED" in result.stderr
    assert not (data_dir / "user_quota.db").exists()


def test_server_registers_enabled_routes_and_read_only_loaders_fail_without_creating_db(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, sqlite3, server
from fastapi.testclient import TestClient
client=TestClient(server.app)
list_status=client.get('/api/dietitian/health-checks').status_code
detail_status=client.get('/api/dietitian/health-checks/case-1').status_code
image_status=client.get('/api/dietitian/health-checks/case-1/sources/log-1/image').status_code
page_status=client.get('/dietitian-health-check').status_code
script_status=client.get('/dietitian-health-check/app.js').status_code
missing=pathlib.Path(server.DB_DIR)/'missing-read-only.db'
server.DB_PATH=str(missing)
errors=[]
for call in (
 lambda: server.list_dietitian_health_checks(statuses=('collecting',),limit=10,offset=0),
 lambda: server.get_dietitian_health_check('case-1'),
):
 try: call()
 except sqlite3.Error: errors.append(True)
print(json.dumps({'list':list_status != 404,
 'detail':detail_status != 404,
 'image':image_status == 401,
 'page':page_status,
 'script':script_status,
 'errors':len(errors),'missing_exists':missing.exists()}))""",
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {
        "list": True, "detail": True, "image": True, "page": 200, "script": 200,
        "errors": 2, "missing_exists": False,
    }


def test_server_registers_enabled_draft_and_approval_writers_with_exact_post_routes(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, server
from fastapi.testclient import TestClient
client=TestClient(server.app)
draft=client.post('/api/dietitian/health-checks/case-1/reviews',json={})
approval=client.post('/api/dietitian/health-checks/case-1/reviews/approve',json={})
print(json.dumps({'draft_writer':callable(server.save_dietitian_health_check_draft),
 'approval_writer':callable(server.approve_dietitian_health_check_review),
 'draft_status':draft.status_code,'approval_status':approval.status_code}))""",
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {
        "draft_writer": True, "approval_writer": True,
        "draft_status": 401, "approval_status": 401,
    }


def test_registered_server_http_approval_then_get_shows_approved_pending_delivery(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, requests, runpy, time
class VerifyResponse:
 status_code=200
 def json(self):
  now=int(time.time())
  return {'iss':'https://access.line.me','aud':'2009251085',
          'sub':'U1234567890abcdef1234567890abcdef','iat':now-1,'exp':now+300}
requests.post=lambda *args,**kwargs: VerifyResponse()
import server
from fastapi.testclient import TestClient
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'approval-fixture'
fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_populated_db'](fixture_dir))
server.create_health_check_draft_saver(server.DB_PATH)
client=TestClient(server.app)
headers={'Authorization':'Bearer signed'}
before=client.get('/api/dietitian/health-checks/case-1',headers=headers)
assert before.status_code == 200, before.text
token=before.json()['source_token']
fields={'good':'早餐穩定','priority':'增加蔬菜','next_7_days':'午餐加一份蔬菜','comment':'先求持續'}
draft=client.post('/api/dietitian/health-checks/case-1/reviews',headers=headers,json={
 **fields,'expected_source_token':token,'expected_review_version':2,'request_id':'server-draft-1'})
approval=client.post('/api/dietitian/health-checks/case-1/reviews/approve',headers=headers,json={
 'expected_source_token':token,'expected_review_version':draft.json()['review_version'],
 'request_id':'server-approval-1'})
after=client.get('/api/dietitian/health-checks/case-1',headers=headers)
print(json.dumps({'before':before.status_code,'draft':draft.status_code,
 'approval_status':approval.status_code,'approval':approval.json(),
 'get_status':after.status_code,'case_status':after.json().get('status'),
 'review_status':(after.json().get('latest_review') or {}).get('status'),
 'review':(after.json().get('latest_review') or {}).get('review'),
 'get_approval':after.json().get('approval')}))""",
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["before"] == evidence["draft"] == 200
    assert evidence["approval_status"] == evidence["get_status"] == 200
    assert evidence["approval"]["delivery_status"] == "pending"
    assert evidence["case_status"] == "approved_pending_delivery"
    assert evidence["review_status"] == "approved"
    assert evidence["review"] == {
        "good": "早餐穩定", "priority": "增加蔬菜",
        "next_7_days": "午餐加一份蔬菜", "comment": "先求持續",
    }
    assert evidence["get_approval"] == {
        "report_id": evidence["approval"]["report_id"],
        "status": "approved", "delivery_status": "pending",
    }


def test_server_read_only_loaders_fail_closed_for_corrupt_database(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, sqlite3, server
corrupt=pathlib.Path(server.DB_DIR)/'corrupt.db'
corrupt.write_bytes(b'not sqlite')
server.DB_PATH=str(corrupt)
errors=0
for call in (
 lambda: server.list_dietitian_health_checks(statuses=('collecting',),limit=10,offset=0),
 lambda: server.get_dietitian_health_check('case-1'),
):
 try: call()
 except sqlite3.Error: errors += 1
print(json.dumps({'errors':errors,'content':corrupt.read_bytes().decode()}))""",
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {"errors": 2, "content": "not sqlite"}


def test_server_rejects_read_and_command_identity_drift_before_creating_database(tmp_path):
    common = {
        "DIETITIAN_HEALTH_CHECK_READ_ENABLED": "true",
        "DIETITIAN_HEALTH_CHECK_LIFF_ID": CHANNEL + "-dietitianCheck",
        "DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": CHANNEL,
        "DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS": UID,
        "DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID": CHANNEL + "-dietitianCheck",
        "DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS": UID,
    }
    mismatches = (
        {"DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID": CHANNEL + "-otherApp"},
        {"DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS": "U-other"},
    )
    for index, mismatch in enumerate(mismatches):
        result, data_dir = _run_server(
            tmp_path / str(index), "import server", **(common | mismatch)
        )
        assert result.returncode != 0
        assert "DIETITIAN_HEALTH_CHECK" in result.stderr
        assert not (data_dir / "user_quota.db").exists()
