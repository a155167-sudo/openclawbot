from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import threading

import pytest

from test_dietitian_health_check_api import (
    DIETITIAN_UID,
    _client,
    _populated_db,
)


NOW = datetime(2026, 9, 16, 2, 0, 0, tzinfo=timezone.utc)


def _preview_signature(detail: dict, fields: dict[str, str]) -> str:
    """Mirror the browser's currentPreviewSignature binding, without a browser/provider."""
    return json.dumps(
        {
            "case_id": detail["case_id"],
            "source_token": detail["source_token"],
            "review_version": detail["current_review_version"],
            "recipient": detail["profile"]["name"],
            "review": fields,
            "limitations": detail["latest_review"]["limitations"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sections(message: dict) -> list[tuple[str, str]]:
    return [
        (item["contents"][0]["text"], item["contents"][1]["text"])
        for item in message["contents"]["body"]["contents"]
    ]


def test_fresh_ready_preview_edit_lock_fake_line_success_once_and_concurrent_claim(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_approval import create_health_check_approval_saver
    from dietitian_health_check_delivery import create_health_check_delivery_service
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    draft_saver = create_health_check_draft_saver(path, now=lambda: NOW)
    approval_saver = create_health_check_approval_saver(path, now=lambda: NOW)
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        detail_loader=lambda case_id: load_health_check_detail(
            sqlite3.connect(path), case_id=case_id
        ),
        draft_saver=draft_saver,
        approval_saver=approval_saver,
    )
    headers = {"Authorization": "Bearer signed"}

    ready = client.get("/api/dietitian/health-checks/case-1", headers=headers)
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready_for_review"
    assert ready.json()["valid_day_count"] == 3
    assert [day["qualifying_meal_count"] for day in ready.json()["valid_days"]] == [2, 2, 2]
    token = ready.json()["source_token"]

    first_fields = {
        "good": "早餐紀錄穩定",
        "priority": "蔬菜份量優先",
        "next_7_days": "午餐增加一份蔬菜",
        "comment": "舊點評，不可送出",
    }
    first = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers=headers,
        json={
            **first_fields,
            "expected_source_token": token,
            "expected_review_version": ready.json()["current_review_version"],
            "request_id": "acceptance-draft-v1",
        },
    )
    assert first.status_code == 200
    first_detail = client.get("/api/dietitian/health-checks/case-1", headers=headers).json()
    old_preview = _preview_signature(first_detail, first_fields)

    final_fields = {**first_fields, "comment": "已編輯點評：先穩定做到每天一份蔬菜。"}
    final = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers=headers,
        json={
            **final_fields,
            "expected_source_token": token,
            "expected_review_version": first.json()["review_version"],
            "request_id": "acceptance-draft-v2",
        },
    )
    assert final.status_code == 200
    final_detail = client.get("/api/dietitian/health-checks/case-1", headers=headers).json()
    current_preview = _preview_signature(final_detail, final_fields)
    assert current_preview != old_preview

    with sqlite3.connect(path) as conn:
        before_old_preview = "\n".join(conn.iterdump())
    stale = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers=headers,
        json={
            "expected_source_token": token,
            "expected_review_version": first.json()["review_version"],
            "request_id": "acceptance-old-preview",
        },
    )
    assert stale.status_code == 409
    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before_old_preview

    approved = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers=headers,
        json={
            "expected_source_token": token,
            "expected_review_version": final.json()["review_version"],
            "request_id": "acceptance-final-approval",
        },
    )
    assert approved.status_code == 200
    locked = client.get("/api/dietitian/health-checks/case-1", headers=headers).json()
    assert locked["status"] == "approved_pending_delivery"
    assert locked["latest_review"]["status"] == "approved"
    assert locked["latest_review"]["review"] == final_fields

    rejected_edit = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers=headers,
        json={
            **{**final_fields, "comment": "核准後不可改"},
            "expected_source_token": token,
            "expected_review_version": final.json()["review_version"],
            "request_id": "acceptance-post-lock-edit",
        },
    )
    assert rejected_edit.status_code == 409

    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, dict, str]] = []

    def fake_line(recipient: str, message: dict, retry_key: str) -> None:
        calls.append((recipient, message, retry_key))
        entered.set()
        assert release.wait(5)

    with sqlite3.connect(path) as conn:
        delivery_key = conn.execute(
            "SELECT delivery_key FROM vip_health_check_deliveries WHERE report_id=?",
            (approved.json()["report_id"],),
        ).fetchone()[0]

    deliver = create_health_check_delivery_service(path, sender=fake_line, now=lambda: NOW)
    first_result: list[dict] = []
    worker = threading.Thread(
        target=lambda: first_result.append(deliver(delivery_key))
    )
    worker.start()
    assert entered.wait(5)
    competing = deliver(delivery_key)
    assert competing["status"] == "in_progress"
    assert len(calls) == 1
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert first_result[0]["status"] == "delivered"

    labels_and_text = _sections(calls[0][1])
    assert labels_and_text == [
        ("做得好的地方", final_fields["good"]),
        ("目前優先事項", final_fields["priority"]),
        ("接下來 7 天", final_fields["next_7_days"]),
        ("營養師個人點評", final_fields["comment"]),
        ("資料限制", "資料有限"),
    ]
    assert "舊點評" not in json.dumps(calls[0][1], ensure_ascii=False)

    confirmed_second = deliver(delivery_key)
    assert confirmed_second["status"] == "delivered"
    assert confirmed_second["attempts"] == 1
    assert len(calls) == 1


def test_timeout_unknown_must_not_be_marked_definite_failure_or_blindly_resent(tmp_path):
    """Acceptance requirement: an ambiguous timeout needs reconciliation before resend."""
    from dietitian_health_check_delivery import create_health_check_delivery_service
    from test_dietitian_health_check_api import _produce_approved_health_check

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    calls: list[str] = []

    def ambiguous_timeout(_recipient: str, _message: dict, retry_key: str) -> None:
        calls.append(retry_key)
        raise TimeoutError("provider outcome unknown")

    deliver = create_health_check_delivery_service(path, sender=ambiguous_timeout, now=lambda: NOW)
    first = deliver(approval["delivery_key"])
    with sqlite3.connect(path) as conn:
        stored = conn.execute(
            "SELECT status,attempts,last_error FROM vip_health_check_deliveries"
        ).fetchone()

    # A timeout is not proof of provider rejection. It must remain non-retryable until
    # provider-side reconciliation establishes a definite failure or acceptance.
    assert first["status"] == "outcome_unknown"
    assert stored[0] == "outcome_unknown"
    second = deliver(approval["delivery_key"])
    assert second["status"] == "outcome_unknown"
    assert len(calls) == 1
