from __future__ import annotations

from datetime import datetime, timezone
import runpy
import sqlite3
from pathlib import Path
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


HERE = Path(__file__).resolve().parent
CUSTOMER_UID = "U11111111111111111111111111111111"
TOKEN = "signed-customer-token"


def _pending_request(tmp_path):
    fixture = runpy.run_path(str(HERE / "test_dietitian_health_check_api.py"))
    path = fixture["_populated_db"](tmp_path)
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_supplement import create_health_check_supplement_saver

    request_more_info = create_health_check_supplement_saver(
        path, now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc)
    )
    with sqlite3.connect(path) as conn:
        token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    request_more_info(
        "case-1",
        "運動日前後資訊不足",
        "請補運動前後飲料與點心",
        token,
        2,
        "dietitian-more-info-1",
        "U1234567890abcdef1234567890abcdef",
    )
    return path


def _customer_client(path, *, verified_uid=CUSTOMER_UID):
    from customer_health_check_liff import create_customer_health_check_router
    from customer_health_check_supplement import create_customer_supplement_completion_saver
    from vip_health_check import get_customer_health_check_state

    def load_state(user_id):
        with sqlite3.connect(path) as conn:
            return get_customer_health_check_state(conn, user_id=user_id)

    app = FastAPI()
    app.include_router(
        create_customer_health_check_router(
            liff_id="2009251085-customerCheckup",
            channel_id="2009251085",
            state_loader=load_state,
            supplement_completion_saver=create_customer_supplement_completion_saver(
                path, now=lambda: datetime(2026, 9, 6, tzinfo=timezone.utc)
            ),
            token_verifier=lambda _token, *, channel_id: verified_uid,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _completion_payload(client, operation_id):
    seen = client.get(
        "/api/vip-health-check/me",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert seen.status_code == 200, seen.text
    supplement = seen.json()["state"]["supplement_request"]
    return {
        "request_id": operation_id,
        "expected_supplement_request_id": supplement["supplement_request_id"],
        "expected_source_token": supplement["expected_source_token"],
    }


def test_registered_customer_http_changed_source_returns_case_to_dietitian_review(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _pending_request(tmp_path)
    # 顧客先沿既有 canonical food-log writer 完成資料；明確提交才可轉狀態。
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE food_logs
               SET nutrition_snapshot_json='{"calories_kcal":520}', version=4
               WHERE log_id='log-owned' AND user_id=?""",
            (CUSTOMER_UID,),
        )

    client = _customer_client(path)
    payload = _completion_payload(client, "customer-submit-1")
    response = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=payload,
    )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ready_for_review",
        "submitted": True,
        "replayed": False,
    }
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        request = conn.execute(
            """SELECT status,resolved_at FROM health_check_supplement_requests
               WHERE case_id='case-1'"""
        ).fetchone()
        case = conn.execute(
            "SELECT status,source_manifest_hash FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()
        event = conn.execute(
            """SELECT request_id,actor_id,case_id,supplement_request_id,result_status
               FROM health_check_supplement_submissions"""
        ).fetchone()
        detail = load_health_check_detail(conn, case_id="case-1")
    assert tuple(request) == ("resolved", "2026-09-06T00:00:00+00:00")
    assert case["status"] == "ready_for_review"
    assert tuple(event)[:3] == ("customer-submit-1", CUSTOMER_UID, "case-1")
    assert event["result_status"] == "ready_for_review"
    assert detail["status"] == "ready_for_review"
    assert detail["supplement_request"] is None
    assert detail["latest_review_fresh"] is False


def test_submission_initializer_rejects_malformed_existing_table_without_mutation(tmp_path):
    from customer_health_check_supplement import create_customer_supplement_completion_saver

    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE health_check_supplement_submissions (
               request_id TEXT PRIMARY KEY, actor_id TEXT, poison TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO health_check_supplement_submissions VALUES ('old','actor','keep')"
        )
        before = "\n".join(conn.iterdump())

    with pytest.raises(sqlite3.IntegrityError, match="submission schema"):
        create_customer_supplement_completion_saver(path)

    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_submission_initializer_rejects_semantically_misbound_rows_without_mutation(tmp_path):
    import customer_health_check_supplement as completion

    path = _pending_request(tmp_path)
    completion.create_customer_supplement_completion_saver(path)
    with sqlite3.connect(path) as conn:
        supplement_id = conn.execute(
            "SELECT supplement_request_id FROM health_check_supplement_requests"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO health_check_supplement_submissions
               (request_id,actor_id,case_id,supplement_request_id,payload_hash,
                observed_source_token,result_status,submitted_at)
               VALUES ('poison-operation','foreign-actor','case-1',?,?,?,
                       'ready_for_review','2026-09-06T00:00:00+00:00')""",
            (supplement_id, completion._ACTION_HASH, "0" * 64),
        )
        before = "\n".join(conn.iterdump())

    with pytest.raises(sqlite3.IntegrityError, match="submission rows"):
        completion.create_customer_supplement_completion_saver(path)

    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_unchanged_source_is_conflict_and_rolls_back_every_write(tmp_path):
    path = _pending_request(tmp_path)
    client = _customer_client(path)
    payload = _completion_payload(client, "customer-unchanged-1")
    with sqlite3.connect(path) as conn:
        before = "\n".join(conn.iterdump())

    response = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=payload,
    )

    assert response.status_code == 409
    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_http_version_only_change_is_conflict_with_zero_transition(tmp_path):
    from vip_health_check import configure_vip_health_check_connection, refresh_case_source_manifest

    path = _pending_request(tmp_path)
    client = _customer_client(path)
    payload = _completion_payload(client, "customer-pre-refreshed")
    with sqlite3.connect(path) as conn:
        configure_vip_health_check_connection(conn)
        conn.execute("UPDATE food_logs SET version=4 WHERE log_id='log-owned'")
        refreshed = refresh_case_source_manifest(
            conn, case_id="case-1", evaluated_at=datetime(2026, 9, 6, tzinfo=timezone.utc)
        )
        assert refreshed["status"] == "needs_more_info"

    with sqlite3.connect(path) as conn:
        before_submission_count = conn.execute(
            "SELECT COUNT(*) FROM health_check_supplement_submissions"
        ).fetchone()[0]
        revision = conn.execute(
            "SELECT revision FROM dietitian_health_check_source_revisions WHERE case_id='case-1'"
        ).fetchone()[0]

    response = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=payload,
    )
    assert response.status_code == 409
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()[0] == "needs_more_info"
        assert conn.execute(
            "SELECT status FROM health_check_supplement_requests WHERE case_id='case-1'"
        ).fetchone()[0] == "pending_customer"
        assert conn.execute(
            "SELECT COUNT(*) FROM health_check_supplement_submissions"
        ).fetchone()[0] == before_submission_count
        # The prior refresh remains monotonic; the rejected POST adds no revision.
        assert conn.execute(
            "SELECT revision FROM dietitian_health_check_source_revisions WHERE case_id='case-1'"
        ).fetchone()[0] == revision


def test_http_aba_content_is_conflict_with_zero_transition(tmp_path):
    from vip_health_check import configure_vip_health_check_connection, refresh_case_source_manifest

    path = _pending_request(tmp_path)
    client = _customer_client(path)
    payload = _completion_payload(client, "customer-aba")
    with sqlite3.connect(path) as conn:
        original = conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE log_id='log-owned'"
        ).fetchone()[0]
        configure_vip_health_check_connection(conn)
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json='{}',version=version+1 "
            "WHERE log_id='log-owned'"
        )
        refresh_case_source_manifest(
            conn, case_id="case-1", evaluated_at=datetime(2026, 9, 6, tzinfo=timezone.utc)
        )
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json=?,version=version+1 "
            "WHERE log_id='log-owned'",
            (original,),
        )
        before_audit = conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_audit_log WHERE reason='supplement_submitted'"
        ).fetchone()[0]

    response = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=payload,
    )

    assert response.status_code == 409
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()[0] == "needs_more_info"
        assert conn.execute(
            "SELECT status FROM health_check_supplement_requests WHERE case_id='case-1'"
        ).fetchone()[0] == "pending_customer"
        assert conn.execute(
            "SELECT COUNT(*) FROM health_check_supplement_submissions"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_audit_log WHERE reason='supplement_submitted'"
        ).fetchone()[0] == before_audit


def test_http_new_canonical_source_is_effective_change_positive_control(tmp_path):
    path = _pending_request(tmp_path)
    client = _customer_client(path)
    payload = _completion_payload(client, "customer-added-source")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO food_logs VALUES
               ('log-added',?,'food-1','2026-09-04T18:00:00+08:00','dinner',1,300,'g',
                '{"calories_kcal":330,"protein_g":18}','{}','{}','',
                'confirmed','',1)""",
            (CUSTOMER_UID,),
        )

    response = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=payload,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready_for_review"


def test_completion_initializer_preserves_caller_transaction_ownership(tmp_path):
    from customer_health_check_supplement import ensure_customer_supplement_completion_schema

    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        original_version = conn.execute(
            "SELECT version FROM food_logs WHERE log_id='log-owned'"
        ).fetchone()[0]
        conn.execute("BEGIN")
        conn.execute("UPDATE food_logs SET version=99 WHERE log_id='log-owned'")
        ensure_customer_supplement_completion_schema(conn)
        assert conn.in_transaction is True
        assert conn.execute(
            "SELECT version FROM food_logs WHERE log_id='log-owned'"
        ).fetchone()[0] == 99
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='health_check_supplement_submissions'"
        ).fetchone() is not None
        conn.rollback()
        assert conn.execute(
            "SELECT version FROM food_logs WHERE log_id='log-owned'"
        ).fetchone()[0] == original_version
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='health_check_supplement_submissions'"
        ).fetchone() is None


def test_wrong_customer_seen_source_fence_is_conflict_without_refresh_or_transition(tmp_path):
    path = _pending_request(tmp_path)
    client = _customer_client(path)
    payload = _completion_payload(client, "customer-wrong-fence")
    payload["expected_source_token"] = "0" * 64
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE food_logs SET version=4 WHERE log_id='log-owned'")
        before = "\n".join(conn.iterdump())

    response = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=payload,
    )
    assert response.status_code == 409
    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_exact_replay_is_durable_and_does_not_repeat_transition(tmp_path):
    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json='{}',version=4 WHERE log_id='log-owned'"
        )
    client = _customer_client(path)
    payload = _completion_payload(client, "customer-replay-1")

    first = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=payload,
    )
    replay = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=payload,
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["replayed"] is False
    assert replay.json() == {
        "status": "ready_for_review", "submitted": True, "replayed": True,
    }
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM health_check_supplement_submissions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_audit_log WHERE reason='supplement_submitted'"
        ).fetchone()[0] == 1


def test_foreign_verified_customer_and_client_claims_cannot_select_request(tmp_path):
    path = _pending_request(tmp_path)
    foreign_client = _customer_client(
        path, verified_uid="U22222222222222222222222222222222"
    )
    with sqlite3.connect(path) as conn:
        before = "\n".join(conn.iterdump())

    foreign = foreign_client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "request_id": "foreign-submit-1",
            "expected_supplement_request_id": "supplement-hidden",
            "expected_source_token": "0" * 64,
        },
    )
    claimed = _customer_client(path).post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "request_id": "claimed-submit-1", "user_id": CUSTOMER_UID,
            "expected_supplement_request_id": "supplement-hidden",
            "expected_source_token": "0" * 64,
            "status": "ready_for_review",
        },
    )

    assert foreign.status_code == 404
    assert claimed.status_code == 422
    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_terminal_parent_rejects_old_exact_replay_without_mutation(tmp_path):
    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json="
            "'{\"calories_kcal\":520}',version=4 WHERE log_id='log-owned'"
        )
    client = _customer_client(path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = _completion_payload(client, "terminal-replay-1")
    assert client.post(
        "/api/vip-health-check/me/supplement-completion", headers=headers, json=payload
    ).status_code == 200
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE vip_health_check_cases SET status='approved_pending_delivery' WHERE case_id='case-1'"
        )
        before = "\n".join(conn.iterdump())

    replay = client.post(
        "/api/vip-health-check/me/supplement-completion", headers=headers, json=payload
    )

    assert replay.status_code == 409
    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_missing_authentication_stops_before_writer():
    from customer_health_check_liff import create_customer_health_check_router

    calls = []
    app = FastAPI()
    app.include_router(create_customer_health_check_router(
        liff_id="2009251085-customerCheckup",
        channel_id="2009251085",
        state_loader=lambda _uid: None,
        supplement_completion_saver=lambda *args: calls.append(args),
    ))
    response = TestClient(app).post(
        "/api/vip-health-check/me/supplement-completion",
        json={"request_id": "unauthenticated-1"},
    )
    assert response.status_code == 401
    assert calls == []


def test_authenticated_legacy_body_is_rejected_before_writer():
    from customer_health_check_liff import create_customer_health_check_router

    calls = []
    app = FastAPI()
    app.include_router(create_customer_health_check_router(
        liff_id="2009251085-customerCheckup",
        channel_id="2009251085",
        state_loader=lambda _uid: None,
        supplement_completion_saver=lambda *args: calls.append(args),
        token_verifier=lambda _token, *, channel_id: CUSTOMER_UID,
    ))
    response = TestClient(app).post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"request_id": "legacy-body-only"},
    )
    assert response.status_code == 422
    assert calls == []


def test_notification_inflight_late_ack_cannot_mark_resolved_request(tmp_path):
    from customer_health_check_supplement import create_customer_supplement_completion_saver
    from dietitian_health_check_supplement_notification import (
        create_health_check_supplement_notification_service,
    )

    path = _pending_request(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json="
            "'{\"calories_kcal\":520}',version=4 WHERE log_id='log-owned'"
        )
    sender_entered = threading.Event()
    sender_release = threading.Event()

    def delayed_sender(_recipient, _message, _retry_key):
        sender_entered.set()
        assert sender_release.wait(5)

    notify = create_health_check_supplement_notification_service(
        path, sender=delayed_sender
    )
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(notify("case-1")))
    worker.start()
    assert sender_entered.wait(5)

    client = _customer_client(path)
    payload = _completion_payload(client, "customer-during-notify-1")
    submitted = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=payload,
    )
    sender_release.set()
    worker.join(5)

    assert submitted.status_code == 200
    assert submitted.json()["status"] == "ready_for_review"
    assert outcome == [{
        "case_id": "case-1",
        "supplement_request_id": outcome[0]["supplement_request_id"],
        "status": "accepted_stale",
        "provider_accepted": True,
    }]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status,notification_status FROM health_check_supplement_requests"
        ).fetchone() == ("resolved", "not_sent")


def test_old_customer_view_new_nonce_cannot_resolve_replacement_request(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver
    from dietitian_health_check_supplement import create_health_check_supplement_saver

    path = _pending_request(tmp_path)
    client = _customer_client(path)
    old_view = _completion_payload(client, "old-view-first-submit")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json="
            "'{\"calories_kcal\":520}',version=4 WHERE log_id='log-owned'"
        )
    first = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=old_view,
    )
    assert first.status_code == 200, first.text

    with sqlite3.connect(path) as conn:
        detail = load_health_check_detail(conn, case_id="case-1")
    draft = create_health_check_draft_saver(path)(
        "case-1",
        {
            "good": "早餐穩定",
            "priority": "補充點心",
            "next_7_days": "持續記錄",
            "comment": "",
        },
        detail["source_token"], 2, "replacement-draft", "dietitian-1",
    )
    replacement = create_health_check_supplement_saver(path)(
        "case-1", "仍缺份量", "請補充實際份量", detail["source_token"],
        draft["review_version"], "replacement-request", "dietitian-1",
    )
    assert replacement["status"] == "needs_more_info"

    stale_payload = dict(old_view, request_id="old-view-new-nonce")
    with sqlite3.connect(path) as conn:
        before = "\n".join(conn.iterdump())
    stale = client.post(
        "/api/vip-health-check/me/supplement-completion",
        headers={"Authorization": f"Bearer {TOKEN}"}, json=stale_payload,
    )
    assert stale.status_code == 409
    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before
        assert conn.execute(
            """SELECT status FROM health_check_supplement_requests
               WHERE request_id='replacement-request'"""
        ).fetchone()[0] == "pending_customer"
