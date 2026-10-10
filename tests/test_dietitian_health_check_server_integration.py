from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


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


def test_actual_server_startup_installs_customer_submission_table_on_fresh_db(tmp_path):
    result, data_dir = _run_server(
        tmp_path,
        """import json, sqlite3, server
with sqlite3.connect(server.DB_PATH) as conn:
 present=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_check_supplement_submissions'").fetchone() is not None
print(json.dumps({'present':present}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {"present": True}
    assert (data_dir / "user_quota.db").exists()


def test_actual_server_init_upgrades_existing_parent_db_and_preserves_rows(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, sqlite3, server
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute('DROP TABLE health_check_supplement_submissions')
 conn.execute('DROP TABLE health_check_supplement_requests')
 conn.execute("INSERT OR REPLACE INTO admin_settings VALUES ('parent-sentinel','keep')")
 before=conn.execute("SELECT COUNT(*) FROM food_catalog").fetchone()[0]
server.init_db()
with sqlite3.connect(server.DB_PATH) as conn:
 present=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_check_supplement_submissions'").fetchone() is not None
 request_present=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_check_supplement_requests'").fetchone() is not None
 after=conn.execute("SELECT COUNT(*) FROM food_catalog").fetchone()[0]
 sentinel=conn.execute("SELECT value FROM admin_settings WHERE key='parent-sentinel'").fetchone()[0]
print(json.dumps({'present':present,'request_present':request_present,'rows_preserved':before==after,'sentinel':sentinel}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {
        "present": True, "request_present": True,
        "rows_preserved": True, "sentinel": "keep",
    }, result.stdout


@pytest.mark.parametrize("meal_photo_state", ("fresh", "migrating", "current"))
def test_actual_server_init_rolls_back_completion_schema_when_later_initializer_fails(
    tmp_path, meal_photo_state
):
    result, _data_dir = _run_server(
        tmp_path,
        f"""import json, sqlite3, server
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute('DROP TABLE health_check_supplement_submissions')
 conn.execute('DROP TABLE health_check_supplement_requests')
 state={meal_photo_state!r}
 if state == 'fresh':
  for table in ('meal_photo_events','meal_photo_notification_events','meal_photo_notification_claims','meal_photo_image_events','pending_meal_photo_drafts','meal_photo_schema_versions'):
   conn.execute('DROP TABLE '+table)
 elif state == 'migrating':
  conn.execute('DROP TABLE meal_photo_image_events')
  conn.execute("UPDATE meal_photo_schema_versions SET version=7,updated_at='migration-original' WHERE component='meal_photo_system'")
 else:
  conn.execute("UPDATE meal_photo_schema_versions SET updated_at='current-original' WHERE component='meal_photo_system'")
 conn.execute("INSERT OR REPLACE INTO admin_settings VALUES ('rollback-sentinel','keep')")
 conn.commit()
 before='\\n'.join(conn.iterdump())
server.ensure_dietitian_health_check_draft_schema=lambda _conn: (_ for _ in ()).throw(RuntimeError('later schema failure'))
server.init_db()
with sqlite3.connect(server.DB_PATH) as conn:
 after='\\n'.join(conn.iterdump())
 present=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_check_supplement_submissions'").fetchone() is not None
print(json.dumps({{'same':before==after,'present':present}}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {
        "same": True,
        "present": False,
    }, result.stdout


def test_server_import_rejects_unknown_feature_flag_before_creating_database(tmp_path):
    result, data_dir = _run_server(
        tmp_path,
        "import server",
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="tru",
    )
    assert result.returncode != 0
    assert "DIETITIAN_HEALTH_CHECK_READ_ENABLED" in result.stderr
    assert not (data_dir / "user_quota.db").exists()


def test_delivery_recovery_trigger_defaults_off_and_rejects_unknown_value(tmp_path):
    default, _data_dir = _run_server(
        tmp_path / "default",
        "import json, server; print(json.dumps({'enabled':server.DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED}))",
    )
    assert default.returncode == 0, default.stderr
    assert _last_json(default.stdout) == {"enabled": False}

    invalid, data_dir = _run_server(
        tmp_path / "invalid", "import server",
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="tru",
    )
    assert invalid.returncode != 0
    assert "DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED" in invalid.stderr
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


def test_registered_server_http_more_info_then_get_shows_not_sent(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, requests, runpy, sqlite3, time
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
fixture_dir=pathlib.Path(server.DB_DIR)/'supplement-fixture'
fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_populated_db'](fixture_dir))
server.create_health_check_supplement_saver(server.DB_PATH)
client=TestClient(server.app)
headers={'Authorization':'Bearer signed'}
before=client.get('/api/dietitian/health-checks/case-1',headers=headers)
token=before.json()['source_token']
posted=client.post('/api/dietitian/health-checks/case-1/request-more-info',headers=headers,json={
 'reason':'運動日前後資訊不足','required_content':'請補運動前後飲料與點心的時間及份量',
 'expected_source_token':token,'expected_review_version':2,'request_id':'server-more-info-1'})
after=client.get('/api/dietitian/health-checks/case-1',headers=headers)
with sqlite3.connect(server.DB_PATH) as conn:
 counts={'requests':conn.execute('SELECT COUNT(*) FROM health_check_supplement_requests').fetchone()[0],
         'deliveries':conn.execute('SELECT COUNT(*) FROM vip_health_check_deliveries').fetchone()[0]}
print(json.dumps({'before':before.status_code,'post':posted.status_code,'posted':posted.json(),
 'get':after.status_code,'case_status':after.json().get('status'),
 'supplement':after.json().get('supplement_request'),'counts':counts}))""",
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["before"] == evidence["post"] == evidence["get"] == 200
    assert evidence["posted"] == {
        "status": "needs_more_info", "notification_status": "not_sent", "created": True,
    }
    assert evidence["case_status"] == "needs_more_info"
    assert evidence["supplement"]["reason"] == "運動日前後資訊不足"
    assert evidence["supplement"]["required_content"] == "請補運動前後飲料與點心的時間及份量"
    assert evidence["supplement"]["notification_status"] == "not_sent"
    assert evidence["supplement"]["notified"] is False
    assert evidence["counts"] == {"requests": 1, "deliveries": 0}


def test_registered_customer_http_completion_persists_and_dietitian_get_sees_review_ready(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, requests, runpy, sqlite3, time
class VerifyResponse:
 def __init__(self,subject): self.subject=subject
 status_code=200
 def json(self):
  now=int(time.time())
  return {'iss':'https://access.line.me','aud':'2009251085',
          'sub':self.subject,'iat':now-1,'exp':now+300}
requests.post=lambda *args,**kwargs: VerifyResponse(
 'U1234567890abcdef1234567890abcdef' if kwargs['data']['id_token']=='dietitian-signed'
 else 'U11111111111111111111111111111111')
import server
from fastapi.testclient import TestClient
from dietitian_health_check_api import load_health_check_detail
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'customer-return-fixture'
fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_populated_db'](fixture_dir))
saver=server.create_health_check_supplement_saver(server.DB_PATH)
with sqlite3.connect(server.DB_PATH) as conn:
 token=load_health_check_detail(conn,case_id='case-1')['source_token']
saver('case-1','資訊不足','請補資料',token,2,'dietitian-request-1',
      'U1234567890abcdef1234567890abcdef')
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute("UPDATE food_logs SET nutrition_snapshot_json='{" + '"calories_kcal"' + ":520}',version=4 WHERE log_id='log-owned'")
client=TestClient(server.app)
customer_seen=client.get('/api/vip-health-check/me',headers={'Authorization':'Bearer customer-signed'})
assert customer_seen.status_code == 200, customer_seen.text
seen_request=customer_seen.json()['state']['supplement_request']
submitted=client.post('/api/vip-health-check/me/supplement-completion',
 headers={'Authorization':'Bearer customer-signed'},json={
  'request_id':'customer-submit-1',
  'expected_supplement_request_id':seen_request['supplement_request_id'],
  'expected_source_token':seen_request['expected_source_token']})
assert submitted.status_code == 200, submitted.text
dietitian_get=client.get('/api/dietitian/health-checks/case-1',
 headers={'Authorization':'Bearer dietitian-signed'})
with sqlite3.connect(server.DB_PATH) as conn:
 stored=conn.execute("SELECT status FROM health_check_supplement_requests").fetchone()[0]
 submission_rows=conn.execute("SELECT COUNT(*) FROM health_check_supplement_submissions").fetchone()[0]
 submission_fk_errors=len(conn.execute("PRAGMA foreign_key_check('health_check_supplement_submissions')").fetchall())
print(json.dumps({'post':submitted.status_code,'payload':submitted.json(),
 'get':dietitian_get.status_code,'case_status':dietitian_get.json()['status'],
 'latest_review_fresh':dietitian_get.json()['latest_review_fresh'],
 'request_status':stored,'submission_rows':submission_rows,
 'submission_fk_errors':submission_fk_errors}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence == {
        "post": 200,
        "payload": {"status": "ready_for_review", "submitted": True, "replayed": False},
        "get": 200,
        "case_status": "ready_for_review",
        "latest_review_fresh": False,
        "request_status": "resolved",
        "submission_rows": 1,
        "submission_fk_errors": 0,
    }



def test_registered_server_delivery_entry_uses_line_retry_key_and_persists_success(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'delivery-fixture'
fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
import sqlite3
with sqlite3.connect(server.DB_PATH) as conn:
 key=conn.execute('SELECT delivery_key FROM vip_health_check_deliveries').fetchone()[0]
class FakeLine:
 def __init__(self): self.headers={'Authorization':'Bearer dummy'}; self.calls=[]
 def __copy__(self):
  clone=FakeLine(); clone.headers=dict(self.headers); clone.calls=self.calls; return clone
 def push_message(self,target,message,**kwargs): self.calls.append((target,message,kwargs,dict(self.headers)))
fake=FakeLine(); server.line_bot_api=fake
cleaned=[]
server.cleanup_delivered_health_check_images=lambda db,root,case_id=None: cleaned.append((db,root,case_id))
delivery=server.deliver_health_check_report_once(key)
with sqlite3.connect(server.DB_PATH) as conn:
 stored=conn.execute('SELECT status,attempts FROM vip_health_check_deliveries').fetchone()
call=fake.calls[0]
print(json.dumps({'result':delivery,'stored':stored,'target':call[0],
 'alt_text':call[1].alt_text,'retry_key':call[2].get('retry_key'),
 'base_headers':fake.headers,'call_headers':call[3],'cleaned':cleaned}))""",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["result"]["status"] == "delivered"
    assert evidence["result"]["cleanup"] == "completed"
    assert evidence["stored"] == ["delivered", 1]
    assert evidence["target"] == "U11111111111111111111111111111111"
    assert evidence["alt_text"] == "營養師三日飲食健檢報告"
    assert evidence["retry_key"]
    assert evidence["base_headers"] == {"Authorization": "Bearer dummy"}
    assert evidence["call_headers"] == {"Authorization": "Bearer dummy"}
    assert evidence["cleaned"][0][2] == "case-1"


def test_registered_report_delivery_marker_failure_blocks_blind_accepted_retry(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, server, sqlite3
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'delivery-accepted-retry'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
with sqlite3.connect(server.DB_PATH) as conn:
 key=conn.execute('SELECT delivery_key FROM vip_health_check_deliveries').fetchone()[0]
 conn.execute(\"\"\"CREATE TRIGGER reject_delivery_marker BEFORE UPDATE OF status ON vip_health_check_deliveries
 WHEN NEW.status='delivered' BEGIN SELECT RAISE(ABORT, 'marker failure'); END\"\"\"); conn.commit()
class Accepted(Exception):
 status_code=409
 accepted_request_id='provider-request-1'
class FakeLine:
 def __init__(self): self.headers={}; self.calls=[]
 def __copy__(self): clone=FakeLine(); clone.calls=self.calls; return clone
 def push_message(self,target,message,**kwargs):
  self.calls.append((target,message,kwargs))
  if len(self.calls)>1: raise Accepted()
fake=FakeLine(); server.line_bot_api=fake
cleaned=[]; server.cleanup_delivered_health_check_images=lambda db,root,case_id=None: cleaned.append(case_id)
marker_failed=False
try: server.deliver_health_check_report_once(key)
except sqlite3.IntegrityError as exc: marker_failed='marker failure' in str(exc)
with sqlite3.connect(server.DB_PATH) as conn:
 after_failure=conn.execute('SELECT status,attempts,delivered_at FROM vip_health_check_deliveries').fetchone()
 conn.execute('DROP TRIGGER reject_delivery_marker'); conn.commit()
final=server.deliver_health_check_report_once(key)
with sqlite3.connect(server.DB_PATH) as conn:
 stored=conn.execute('SELECT status,attempts FROM vip_health_check_deliveries').fetchone()
print(json.dumps({'marker_failed':marker_failed,'after_failure':after_failure,'final':final,
 'stored':stored,'keys':[call[2]['retry_key'] for call in fake.calls],'cleaned':cleaned}))""",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["marker_failed"] is True
    assert evidence["after_failure"] == ["outcome_unknown", 1, ""]
    assert evidence["final"]["status"] == "outcome_unknown"
    assert evidence["stored"] == ["outcome_unknown", 1]
    assert len(evidence["keys"]) == 1
    assert evidence["cleaned"] == []


def test_actual_line_adapter_classifies_only_documented_400_as_definite_nonretryable(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, server
from types import SimpleNamespace
from linebot.exceptions import LineBotApiError
from dietitian_health_check_delivery import DefiniteDeliveryFailure
class FakeLine:
 def __init__(self,exc): self.headers={}; self.exc=exc; self.calls=[]
 def __copy__(self): clone=FakeLine(self.exc); clone.calls=self.calls; return clone
 def push_message(self,target,message,**kwargs): self.calls.append(kwargs['retry_key']); raise self.exc
def sdk_error(status,accepted=None):
 return LineBotApiError(status,{},request_id='req-'+str(status),accepted_request_id=accepted,error=SimpleNamespace(message='provider rejected'))
observed={}
for name,exc in [('400',sdk_error(400)),('429',sdk_error(429)),('500',sdk_error(500)),('timeout',TimeoutError('timeout')),('connection',ConnectionError('reset'))]:
 server.line_bot_api=FakeLine(exc)
 try: server._send_health_check_report_via_line('U-test',{'altText':'a','contents':{'type':'bubble'}},'same-key')
 except Exception as raised:
  observed[name]={'class':type(raised).__name__,'definite':isinstance(raised,DefiniteDeliveryFailure),'retryable':getattr(raised,'retryable',None),'cause':type(raised.__cause__).__name__ if raised.__cause__ else None}
 else: observed[name]={'accepted':True}
server.line_bot_api=FakeLine(sdk_error(409,'accepted-request'))
server._send_health_check_report_via_line('U-test',{'altText':'a','contents':{'type':'bubble'}},'same-key')
observed['409']={'accepted':True,'keys':server.line_bot_api.calls}
print(json.dumps(observed))""",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["400"] == {
        "class": "DefiniteDeliveryFailure",
        "definite": True,
        "retryable": False,
        "cause": "LineBotApiError",
    }
    for uncertain in ("429", "500", "timeout", "connection"):
        assert evidence[uncertain]["definite"] is False
        assert evidence[uncertain]["retryable"] is None
    assert evidence["409"] == {"accepted": True, "keys": ["same-key"]}


def test_line_400_requires_correction_before_manual_same_key_retry(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, server, sqlite3
from types import SimpleNamespace
from linebot.exceptions import LineBotApiError
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'line-400'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
with sqlite3.connect(server.DB_PATH) as conn: key=conn.execute('SELECT delivery_key FROM vip_health_check_deliveries').fetchone()[0]
class FakeLine:
 def __init__(self): self.headers={}; self.calls=[]; self.fixed=False
 def __copy__(self): clone=FakeLine(); clone.calls=self.calls; clone.fixed=self.fixed; return clone
 def push_message(self,target,message,**kwargs):
  self.calls.append(kwargs['retry_key'])
  if not self.fixed: raise LineBotApiError(400,{},request_id='req-400',error=SimpleNamespace(message='invalid message'))
fake=FakeLine(); server.line_bot_api=fake
first=server.deliver_health_check_report_once(key)
automatic=server.deliver_next_health_check_report()
fake.fixed=True
manual=server.deliver_health_check_report_once(key)
with sqlite3.connect(server.DB_PATH) as conn: stored=conn.execute('SELECT status,attempts,last_error FROM vip_health_check_deliveries').fetchone()
print(json.dumps({'first':first,'automatic':automatic,'manual':manual,'keys':fake.calls,'stored':stored}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["first"]["status"] == "failed"
    assert evidence["first"]["retryable"] is False
    assert evidence["automatic"] == {"status": "empty"}
    assert evidence["manual"]["status"] == "delivered"
    assert evidence["keys"][0] == evidence["keys"][1]
    assert evidence["stored"][:2] == ["delivered", 2]


def test_health_check_delivery_job_has_real_registered_runtime_caller(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, server
class Scheduler:
 def __init__(self): self.calls=[]
 def add_job(self,*args,**kwargs): self.calls.append((args,kwargs))
s=Scheduler(); server.register_health_check_delivery_job(s)
args,kwargs=s.calls[0]
print(json.dumps({'callable':args[0] is server.deliver_next_health_check_report,
 'trigger':args[1],'minutes':kwargs['minutes'],'max_instances':kwargs['max_instances']}))""",
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {
        "callable": True, "trigger": "interval", "minutes": 10, "max_instances": 1,
    }


def test_startup_recovery_queue_never_claims_outcome_unknown(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, sqlite3, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'unknown-recovery'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute("UPDATE vip_health_check_deliveries SET status='outcome_unknown',attempts=1,last_error='timeout'")
calls=[]
server.deliver_health_check_report_once=lambda key:calls.append(key)
result=server.deliver_next_health_check_report()
print(json.dumps({'result':result,'calls':calls}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {"result": {"status": "empty"}, "calls": []}


def test_dedicated_trigger_false_has_zero_scheduler_or_tick_side_effects(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import asyncio, json, server
calls=[]
server.BackgroundScheduler=lambda **kwargs: (_ for _ in ()).throw(AssertionError('scheduler constructed'))
server.deliver_next_health_check_report=lambda: calls.append('delivery')
server.recover_next_delivered_health_check_cleanup=lambda: calls.append('recovery')
async def run():
 async with server.lifespan(server.app): pass
asyncio.run(run())
print(json.dumps({'calls':calls}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="false",
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {"calls": []}


def test_dedicated_trigger_with_global_scheduler_off_starts_health_check_jobs(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import asyncio, json, server
class Scheduler:
 def __init__(self, **kwargs): self.jobs=[]; self.starts=0; self.shutdowns=0
 def add_job(self,*args,**kwargs): self.jobs.append((args,kwargs))
 def start(self): self.starts += 1
 def shutdown(self): self.shutdowns += 1
s=Scheduler(); startup=[]
server.BackgroundScheduler=lambda **kwargs:s
server.deliver_next_health_check_report=lambda:startup.append('delivery')
server.recover_next_delivered_health_check_cleanup=lambda:startup.append('recovery')
async def run():
 async with server.lifespan(server.app): pass
asyncio.run(run())
print(json.dumps({'jobs':[job[0][0].__name__ for job in s.jobs],
 'startup':startup,'starts':s.starts,'shutdowns':s.shutdowns}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    assert _last_json(result.stdout) == {
        "jobs": ["<lambda>", "<lambda>", "notify_next_health_check_supplement"],
        "startup": ["delivery", "recovery"], "starts": 1, "shutdowns": 1,
    }


@pytest.mark.parametrize("cleanup_mode", ["retry_counts", "io_error"])
def test_delivery_tick_cleanup_failure_recovers_on_next_tick_without_resend(tmp_path, cleanup_mode):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, os, pathlib, runpy, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'recovery-fixture'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
class FakeLine:
 def __init__(self): self.headers={}; self.calls=[]
 def __copy__(self): clone=FakeLine(); clone.calls=self.calls; return clone
 def push_message(self,*args,**kwargs): self.calls.append((args,kwargs))
fake=FakeLine(); server.line_bot_api=fake
cleanup_calls=[]
def cleanup(*args,**kwargs):
 cleanup_calls.append(kwargs.get('case_id'))
 if len(cleanup_calls)==1:
  if os.environ['TEST_CLEANUP_MODE']=='io_error': raise OSError('storage unavailable')
  return {'deleted':0,'missing':0,'blocked':0,'retry_pending':1}
 return {'deleted':1,'missing':0,'blocked':0,'retry_pending':0}
server.cleanup_delivered_health_check_images=cleanup
first=server.deliver_next_health_check_report()
second=server.recover_next_delivered_health_check_cleanup()
print(json.dumps({'first':first,'second':second,'line_calls':len(fake.calls),
                  'cleanup_calls':cleanup_calls}))""",
        TEST_CLEANUP_MODE=cleanup_mode,
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["first"]["cleanup"] == "retry_pending"
    assert evidence["second"]["cleanup"] == "completed"
    assert evidence["line_calls"] == 1
    assert evidence["cleanup_calls"] == ["case-1", "case-1"]


def test_registered_server_supplement_notification_uses_persisted_content_and_accepted_retry(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, sqlite3, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'supplement-notification-fixture'; fixture_dir.mkdir(parents=True,exist_ok=True)
path=fixture['_populated_db'](fixture_dir)
server.DB_PATH=str(path)
from dietitian_health_check_api import load_health_check_detail
from dietitian_health_check_supplement import create_health_check_supplement_saver
from linebot.exceptions import LineBotApiError
from types import SimpleNamespace
from vip_health_check import health_check_source_token
saver=create_health_check_supplement_saver(path)
with sqlite3.connect(path) as conn:
 manifest=conn.execute(\"SELECT source_manifest_hash FROM vip_health_check_cases WHERE case_id='case-1'\").fetchone()[0]
 token=health_check_source_token(conn,case_id='case-1',manifest_hash=manifest)
saver('case-1','持久理由','持久必填內容',token,2,'notify-runtime-1','U12345678901234567890123456789012')
accepted=LineBotApiError(409,{},request_id='duplicate',accepted_request_id='provider-request-1',error=SimpleNamespace(message='retry key accepted'))
class FakeLine:
 def __init__(self): self.headers={}; self.calls=[]
 def __copy__(self): clone=FakeLine(); clone.calls=self.calls; return clone
 def push_message(self,target,message,**kwargs): self.calls.append((target,message,kwargs)); raise accepted
fake=FakeLine(); server.line_bot_api=fake
first=server.notify_health_check_supplement_once('case-1')
replay=server.notify_health_check_supplement_once('case-1')
with sqlite3.connect(path) as conn: detail=load_health_check_detail(conn,case_id='case-1')
print(json.dumps({'first':first,'replay':replay,'calls':len(fake.calls),
 'target':fake.calls[0][0],'retry_key':fake.calls[0][2]['retry_key'],
 'alt_text':fake.calls[0][1].alt_text,'detail':detail['supplement_request'],
 'case_status':detail['status']}))""",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["first"]["status"] == evidence["replay"]["status"] == "delivered"
    assert evidence["calls"] == 1
    assert evidence["target"] == "U11111111111111111111111111111111"
    assert evidence["retry_key"]
    assert evidence["alt_text"] == "營養師請您補充三日飲食健檢資料"
    assert evidence["detail"]["notified"] is True
    assert evidence["case_status"] == "needs_more_info"


def test_health_check_scheduler_wires_supplement_notification_selector(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, server
class Scheduler:
 def __init__(self): self.calls=[]
 def add_job(self,*args,**kwargs): self.calls.append((args,kwargs))
s=Scheduler(); server.register_health_check_delivery_job(s)
print(json.dumps({'jobs':[call[0][0].__name__ for call in s.calls]}))""",
    )
    assert result.returncode == 0, result.stderr
    assert "notify_next_health_check_supplement" in _last_json(result.stdout)["jobs"]


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


def test_delivery_queue_round_robins_failed_with_continuous_pending_arrivals(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, sqlite3, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'fair-delivery'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute('DELETE FROM vip_health_check_deliveries')
empty=server.deliver_next_health_check_report()
with sqlite3.connect(server.DB_PATH) as conn:
 conn.executemany('INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)',[
  ('failed-old-id','report-unused','U-fair','failed-old-key','failed',1,'temporary','2026-09-01T00:00:00+08:00',''),
  ('pending-0-id','report-unused','U-fair','pending-0-key','pending',0,'','2026-09-01T00:01:00+08:00','')])
selected=[]
def fake_transport(key):
 selected.append(key)
 with sqlite3.connect(server.DB_PATH) as conn:
  if key.startswith('pending'):
   conn.execute("UPDATE vip_health_check_deliveries SET status='delivered',attempts=attempts+1,delivered_at='done' WHERE delivery_key=?",(key,))
  else:
   conn.execute("UPDATE vip_health_check_deliveries SET attempts=attempts+1 WHERE delivery_key=?",(key,))
 return {'status':'failed' if key.startswith('failed') else 'delivered'}
server.deliver_health_check_report_once=fake_transport
for tick in range(6):
 server.deliver_next_health_check_report()
 with sqlite3.connect(server.DB_PATH) as conn:
  if tick == 0:
   conn.execute('INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)',
    ('pending-smaller-id','report-unused','U-fair','pending-smaller-key','pending',0,'','2026-08-31T23:59:00+08:00',''))
  conn.execute('INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)',
   (f'pending-{tick+1}-id','report-unused','U-fair',f'pending-{tick+1}-key','pending',0,'',f'2026-09-01T00:{tick+2:02d}:00+08:00',''))
server._health_check_delivery_queue_state['db_path']=''
server._health_check_delivery_queue_state['members'].clear()
server.deliver_next_health_check_report()
print(json.dumps({'empty':empty,'selected':selected}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["empty"] == {"status": "empty"}
    selected = evidence["selected"]
    assert "failed-old-key" in selected[:2]
    assert "pending-0-key" in selected[:3]
    assert "pending-smaller-key" in selected[:4]
    assert "pending-1-key" in selected[:6]
    assert selected[-1] == "failed-old-key"


def test_delivery_queue_freezes_in_range_arrivals_out_of_current_round(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, sqlite3, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'finite-in-range'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
created='2026-09-01T00:00:00+08:00'
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute('DELETE FROM vip_health_check_deliveries')
 conn.executemany('INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)',[
  ('vhcd_'+('0'*32),'report-unused','U-fair','pending-first','pending',0,'',created,''),
  ('vhcd_'+('f'*32),'report-unused','U-fair','failed-target','failed',1,'permanent',created,'')])
selected=[]
def fake_transport(key):
 selected.append(key)
 with sqlite3.connect(server.DB_PATH) as conn:
  conn.execute('UPDATE vip_health_check_deliveries SET attempts=attempts+1 WHERE delivery_key=?',(key,))
 return {'status':'failed'}
server.deliver_health_check_report_once=fake_transport
for tick in range(8):
 server.deliver_next_health_check_report()
 with sqlite3.connect(server.DB_PATH) as conn:
  conn.execute('INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)',
   ('vhcd_'+f'{tick+1:032x}','report-unused','U-fair',f'in-range-{tick+1}','pending',0,'',created,''))
print(json.dumps({'selected':selected}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    selected = _last_json(result.stdout)["selected"]
    assert selected[:2] == ["pending-first", "failed-target"]


def test_delivery_queue_consumes_deleted_and_exception_members_before_target(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, sqlite3, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'finite-negative'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
rows=[]
for index,(key,status) in enumerate([('failed-forever','failed'),('deleted-member','pending'),('exception-member','pending'),('locked-member','pending'),('finite-target','pending')]):
 rows.append((f'vhcd_{index:032x}','report-unused','U-fair',key,status,1 if status=='failed' else 0,'permanent' if status=='failed' else '',f'2026-09-01T00:0{index}:00+08:00',''))
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute('DELETE FROM vip_health_check_deliveries')
 conn.executemany('INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)',rows)
selected=[]; per_tick=[]
def fake_transport(key):
 selected.append(key)
 if key=='exception-member': raise RuntimeError('expected service failure')
 if key=='locked-member': raise sqlite3.OperationalError('database is locked')
 return {'status':'failed' if key=='failed-forever' else 'delivered'}
server.deliver_health_check_report_once=fake_transport
for tick in range(5):
 before=len(selected)
 try: server.deliver_next_health_check_report()
 except (RuntimeError,sqlite3.OperationalError): pass
 per_tick.append(len(selected)-before)
 if tick==0:
  with sqlite3.connect(server.DB_PATH) as conn:
   conn.execute("DELETE FROM vip_health_check_deliveries WHERE delivery_key='deleted-member'")
   conn.execute('INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)',
    ('vhcd-smaller','report-unused','U-fair','new-smaller','pending',0,'','2026-08-01T00:00:00+08:00',''))
server.deliver_next_health_check_report()
print(json.dumps({'selected':selected,'per_tick':per_tick}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert evidence["per_tick"] == [1, 1, 1, 1, 1]
    assert evidence["selected"][:5] == [
        "failed-forever",
        "deleted-member",
        "exception-member",
        "locked-member",
        "finite-target",
    ]
    assert evidence["selected"][5] == "new-smaller"


def test_cleanup_queue_skips_permanently_blocked_prefix_without_losing_it(tmp_path):
    result, _data_dir = _run_server(
        tmp_path,
        """import json, pathlib, runpy, sqlite3, server
fixture=runpy.run_path(str(pathlib.Path('tests/test_dietitian_health_check_api.py')))
fixture_dir=pathlib.Path(server.DB_DIR)/'fair-cleanup'; fixture_dir.mkdir(parents=True,exist_ok=True)
server.DB_PATH=str(fixture['_produce_approved_health_check'](fixture_dir)[0])
with sqlite3.connect(server.DB_PATH) as conn:
 conn.execute("UPDATE vip_health_check_cases SET status='delivered'")
 conn.execute("UPDATE vip_health_check_deliveries SET status='delivered',delivered_at='2026-09-01T00:00:00+08:00'")
 for suffix,minute in [('b',1),('c',2)]:
  conn.execute("INSERT INTO vip_health_check_cases SELECT ?,user_id,benefit_key||?,first_vip_activation_id,activation_event_key||?,window_started_at,window_ends_at,'delivered',valid_day_count,source_manifest_hash,submitted_at,report_published_at,created_at,updated_at FROM vip_health_check_cases WHERE case_id='case-1'",(f'case-{suffix}',suffix,suffix))
  conn.execute("INSERT INTO vip_health_check_reviews SELECT ?,?,review_version,status,ai_observations_json,review_json,suggested_values_json,limitations,source_manifest_hash,approved_by,approved_at,created_at,updated_at FROM vip_health_check_reviews WHERE review_id=(SELECT review_id FROM vip_health_check_reports LIMIT 1)",(f'review-{suffix}',f'case-{suffix}'))
  conn.execute("INSERT INTO vip_health_check_reports SELECT ?,?, ?,report_kind,report_version,report_json,source_manifest_hash,published_by,published_at FROM vip_health_check_reports LIMIT 1",(f'report-{suffix}',f'case-{suffix}',f'review-{suffix}'))
  conn.execute("INSERT INTO vip_health_check_deliveries (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?)",(f'delivery-{suffix}',f'report-{suffix}','U11111111111111111111111111111111',f'delivered-{suffix}-key','delivered',1,'',f'2026-09-01T00:0{minute}:00+08:00',f'2026-09-01T00:0{minute}:00+08:00'))
  for row in conn.execute("SELECT food_log_id,food_log_version,local_date,included_reason,source_hash,created_at FROM vip_health_check_source_refs WHERE case_id='case-1'").fetchall():
   old_log=row[0]; new_log=old_log+'-'+suffix
   conn.execute("INSERT INTO food_logs SELECT ?,user_id,food_id,consumed_at,meal_slot,consumed_servings,consumed_amount,consumed_unit,nutrition_snapshot_json,exchange_snapshot_json,approved_exchange_json,source_image_ref,confirmation_status,deleted_at,version FROM food_logs WHERE log_id=?",(new_log,old_log))
   conn.execute("INSERT INTO vip_health_check_source_refs VALUES (?,?,?,?,?,?,?)",(f'case-{suffix}',new_log,*row[1:]))
 keys=conn.execute('SELECT delivery_key FROM vip_health_check_deliveries ORDER BY delivered_at,delivery_id').fetchall()
first_key=keys[0][0]; selected=[]
def fake_cleanup_transport(key):
 selected.append(key)
 if key.endswith('c-key'):
  with sqlite3.connect(server.DB_PATH) as conn:
   case_id=conn.execute('SELECT r.case_id FROM vip_health_check_deliveries d JOIN vip_health_check_reports r ON r.report_id=d.report_id WHERE d.delivery_key=?',(key,)).fetchone()[0]
   conn.execute("UPDATE food_logs SET source_image_ref='' WHERE log_id IN (SELECT food_log_id FROM vip_health_check_source_refs WHERE case_id=?)",(case_id,))
 return {'status':'delivered','cleanup':'retry_pending' if key != 'delivered-c-key' else 'completed'}
server.deliver_health_check_report_once=fake_cleanup_transport
for _ in range(4): server.recover_next_delivered_health_check_cleanup()
with sqlite3.connect(server.DB_PATH) as conn:
 retained=conn.execute("SELECT COUNT(*) FROM vip_health_check_source_refs sr JOIN food_logs fl ON fl.log_id=sr.food_log_id WHERE sr.case_id='case-1' AND fl.source_image_ref<>''").fetchone()[0]
print(json.dumps({'selected':selected,'first_key':first_key,'retained':retained}))""",
        VIP_HEALTH_CHECK_ENABLED="true",
        VIP_HEALTH_CHECK_LIFF_ID=CHANNEL + "-customerCheckup",
        VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_READ_ENABLED="true",
        DIETITIAN_HEALTH_CHECK_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID=CHANNEL,
        DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_COMMAND_LIFF_ID=CHANNEL + "-dietitianCheck",
        DIETITIAN_HEALTH_CHECK_COMMAND_ALLOWED_UIDS=UID,
        DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED="true",
    )
    assert result.returncode == 0, result.stderr
    evidence = _last_json(result.stdout)
    assert "delivered-c-key" in evidence["selected"][:3]
    assert evidence["first_key"] in evidence["selected"]
    assert evidence["retained"] > 0
