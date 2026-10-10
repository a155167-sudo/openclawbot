from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

import pytest

from test_dietitian_health_check_api import (
    CUSTOMER_UID,
    _produce_approved_health_check,
)


NOW = datetime(2026, 9, 6, 1, 2, 3, tzinfo=timezone.utc)
APPROVED = {
    "good": "好",
    "priority": "優先",
    "next_7_days": "行動",
    "limitations": "限制",
}


class FakeSender:
    def __init__(self, failures: list[Exception] | None = None):
        self.calls: list[tuple[str, dict, str]] = []
        self.failures = list(failures or [])

    def __call__(self, recipient: str, message: dict, retry_key: str) -> None:
        self.calls.append((recipient, message, retry_key))
        if self.failures:
            raise self.failures.pop(0)


def _stored_delivery(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT status,attempts,last_error,delivered_at FROM vip_health_check_deliveries"
        ).fetchone(), conn.execute(
            "SELECT status,report_published_at FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()


def _assert_exact_approved_flex(message: dict) -> None:
    assert message["type"] == "flex"
    assert message["altText"] == "營養師三日飲食健檢報告"
    assert message["contents"]["type"] == "bubble"
    sections = message["contents"]["body"]["contents"]
    assert [
        (item["contents"][0]["text"], item["contents"][1]["text"])
        for item in sections
    ] == [
        ("做得好的地方", "好"),
        ("目前優先事項", "優先"),
        ("接下來 7 天", "行動"),
        ("營養師個人點評", "限制"),
        ("資料限制", "資料有限"),
    ]


def test_versioned_report_renders_personal_comment_and_limitations_separately():
    from dietitian_health_check_delivery import render_approved_health_check_flex

    message = render_approved_health_check_flex({
        "contract_version": "dietitian_health_check_report_v2",
        "good": "完整記錄",
        "priority": "增加蔬菜",
        "next_7_days": "午餐增加一份菜",
        "comment": "你做得到，先穩定一件事。",
        "limitations": "一筆來源缺少可驗證餐別。",
    })
    assert [
        (item["contents"][0]["text"], item["contents"][1]["text"])
        for item in message["contents"]["body"]["contents"]
    ] == [
        ("做得好的地方", "完整記錄"),
        ("目前優先事項", "增加蔬菜"),
        ("接下來 7 天", "午餐增加一份菜"),
        ("營養師個人點評", "你做得到，先穩定一件事。"),
        ("資料限制", "一筆來源缺少可驗證餐別。"),
    ]


def test_legacy_report_renderer_preserves_historical_fourth_label():
    from dietitian_health_check_delivery import render_approved_health_check_flex

    message = render_approved_health_check_flex(APPROVED)
    sections = message["contents"]["body"]["contents"]
    assert [item["contents"][0]["text"] for item in sections] == [
        "做得好的地方", "目前優先事項", "接下來 7 天", "限制與提醒",
    ]


def test_legacy_v1_renderer_keeps_immutable_4000_character_contract():
    from dietitian_health_check_delivery import (
        DeliveryConflict,
        render_approved_health_check_flex,
    )

    accepted = {**APPROVED, "good": "舊" * 4000}
    message = render_approved_health_check_flex(accepted)
    assert message["contents"]["body"]["contents"][0]["contents"][1]["text"] == "舊" * 4000

    with pytest.raises(DeliveryConflict, match="approved report is invalid"):
        render_approved_health_check_flex({**APPROVED, "good": "舊" * 4001})


def test_explicit_v2_renderer_owns_separate_5000_character_contract():
    from dietitian_health_check_delivery import (
        DeliveryConflict,
        render_approved_health_check_flex,
    )

    report = {
        "contract_version": "dietitian_health_check_report_v2",
        "good": "好",
        "priority": "優先",
        "next_7_days": "行動",
        "comment": "點" * 5000,
        "limitations": "限制",
    }
    message = render_approved_health_check_flex(report)
    assert message["contents"]["body"]["contents"][3]["contents"][1]["text"] == "點" * 5000

    with pytest.raises(DeliveryConflict, match="approved report is invalid"):
        render_approved_health_check_flex({**report, "comment": "點" * 5001})


def test_persisted_pending_report_is_sent_once_then_marked_delivered(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    sender = FakeSender()
    cleanup_calls: list[str] = []
    deliver = create_health_check_delivery_service(
        path,
        sender=sender,
        now=lambda: NOW,
        cleanup=lambda case_id: cleanup_calls.append(case_id),
    )

    result = deliver(approval["delivery_key"])

    assert result == {
        "delivery_key": approval["delivery_key"],
        "report_id": approval["report_id"],
        "status": "delivered",
        "attempts": 1,
        "cleanup": "completed",
    }
    assert len(sender.calls) == 1
    recipient, message, retry_key = sender.calls[0]
    assert recipient == CUSTOMER_UID
    _assert_exact_approved_flex(message)
    assert retry_key
    assert _stored_delivery(path) == (
        ("delivered", 1, "", NOW.isoformat(timespec="seconds")),
        ("delivered", NOW.isoformat(timespec="seconds")),
    )
    assert cleanup_calls == ["case-1"]

    replay = deliver(approval["delivery_key"])
    assert replay["status"] == "delivered"
    assert replay["attempts"] == 1
    assert len(sender.calls) == 1
    assert cleanup_calls == ["case-1", "case-1"]


def test_delivery_preserves_attempt_timestamp_offset(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    local_now = datetime(2026, 9, 6, 9, 2, 3, tzinfo=timezone(timedelta(hours=8)))
    deliver = create_health_check_delivery_service(
        path, sender=FakeSender(), now=lambda: local_now
    )

    result = deliver(approval["delivery_key"])

    assert result["status"] == "delivered"
    assert _stored_delivery(path) == (
        ("delivered", 1, "", local_now.isoformat(timespec="seconds")),
        ("delivered", local_now.isoformat(timespec="seconds")),
    )


def test_failed_send_is_persisted_and_retry_reuses_operation_and_exact_report(tmp_path):
    from dietitian_health_check_delivery import (
        DefiniteDeliveryFailure,
        create_health_check_delivery_service,
    )

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    sender = FakeSender([DefiniteDeliveryFailure("LINE rejected before acceptance")])
    deliver = create_health_check_delivery_service(path, sender=sender, now=lambda: NOW)

    failed = deliver(approval["delivery_key"])
    assert failed == {
        "delivery_key": approval["delivery_key"],
        "report_id": approval["report_id"],
        "status": "failed",
        "attempts": 1,
        "cleanup": "not_applicable",
    }
    assert _stored_delivery(path)[0][:3] == (
        "failed", 1, "LINE rejected before acceptance"
    )

    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO vip_health_check_reviews
               SELECT 'new-draft',case_id,review_version+1,'draft',ai_observations_json,
                      ?,suggested_values_json,limitations,source_manifest_hash,'','',?,?
               FROM vip_health_check_reviews WHERE review_id=(
                   SELECT review_id FROM vip_health_check_reports WHERE report_id=?)""",
            (
                json.dumps({
                    "good": "不應傳送的新稿", "priority": "錯誤優先",
                    "next_7_days": "錯誤行動", "comment": "錯誤限制",
                }),
                NOW.isoformat(), NOW.isoformat(), approval["report_id"],
            ),
        )

    retried = deliver(approval["delivery_key"])
    assert retried["status"] == "delivered"
    assert retried["attempts"] == 2
    assert len(sender.calls) == 2
    assert sender.calls[0][2] == sender.calls[1][2]
    _assert_exact_approved_flex(sender.calls[1][1])
    assert "新稿" not in json.dumps(sender.calls[1][1], ensure_ascii=False)


@pytest.mark.parametrize("ambiguous_error", [
    TimeoutError("provider outcome unknown"),
    ConnectionError("connection lost after dispatch"),
    RuntimeError("unclassified sender failure"),
])
def test_ambiguous_sender_failure_is_terminal_until_reconciled_without_resend(
    tmp_path, ambiguous_error
):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    sender = FakeSender([ambiguous_error])
    deliver = create_health_check_delivery_service(path, sender=sender, now=lambda: NOW)

    first = deliver(approval["delivery_key"])
    replay = deliver(approval["delivery_key"])

    assert first["status"] == replay["status"] == "outcome_unknown"
    assert first["attempts"] == replay["attempts"] == 1
    assert len(sender.calls) == 1
    assert _stored_delivery(path)[0][:3] == (
        "outcome_unknown", 1, str(ambiguous_error)
    )


def test_success_marker_failure_leaves_unknown_and_replay_does_not_resend(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TRIGGER reject_delivery_marker
            BEFORE UPDATE OF status ON vip_health_check_deliveries
            WHEN NEW.status='delivered'
            BEGIN SELECT RAISE(ABORT, 'marker failure'); END""")
        conn.commit()
    sender = FakeSender()
    deliver = create_health_check_delivery_service(path, sender=sender, now=lambda: NOW)

    with pytest.raises(sqlite3.IntegrityError, match="marker failure"):
        deliver(approval["delivery_key"])
    replay = deliver(approval["delivery_key"])

    assert replay["status"] == "outcome_unknown"
    assert replay["attempts"] == 1
    assert len(sender.calls) == 1
    assert _stored_delivery(path)[0][0:2] == ("outcome_unknown", 1)


def test_provider_io_releases_sqlite_writer_for_unrelated_connection(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def slow_sender(_recipient: str, _message: dict, _retry_key: str) -> None:
        entered.set()
        assert release.wait(5)

    deliver = create_health_check_delivery_service(path, sender=slow_sender, now=lambda: NOW)

    def run_delivery() -> None:
        try:
            outcome["result"] = deliver(approval["delivery_key"])
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_delivery)
    worker.start()
    assert entered.wait(5)
    competing_error = None
    try:
        with sqlite3.connect(path, timeout=0.2) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO vip_health_check_audit_log
                   (case_id,actor_type,actor_id,from_status,to_status,reason,created_at)
                   VALUES ('case-1','system','barrier','approved_pending_delivery',
                           'approved_pending_delivery','concurrent_writer',?)""",
                (NOW.isoformat(),),
            )
            conn.commit()
    except BaseException as exc:
        competing_error = exc
    finally:
        release.set()
        worker.join(5)

    assert competing_error is None
    assert worker.is_alive() is False
    assert "error" not in outcome
    assert outcome["result"]["status"] == "delivered"


def test_cancel_during_provider_io_fences_late_ack_and_skips_cleanup(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}
    cleanup_calls: list[str] = []

    def slow_sender(_recipient: str, _message: dict, _retry_key: str) -> None:
        entered.set()
        assert release.wait(5)

    deliver = create_health_check_delivery_service(
        path,
        sender=slow_sender,
        now=lambda: NOW,
        cleanup=lambda case_id: cleanup_calls.append(case_id),
    )

    def run_delivery() -> None:
        try:
            outcome["result"] = deliver(approval["delivery_key"])
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_delivery)
    worker.start()
    assert entered.wait(5)
    try:
        with sqlite3.connect(path, timeout=0.2) as conn:
            conn.execute(
                "UPDATE vip_health_check_cases SET status='cancelled' WHERE case_id='case-1'"
            )
            conn.commit()
    finally:
        release.set()
        worker.join(5)

    assert worker.is_alive() is False
    assert "error" not in outcome
    assert outcome["result"] == {
        "delivery_key": approval["delivery_key"],
        "report_id": approval["report_id"],
        "status": "accepted_stale",
        "attempts": 1,
        "provider_accepted": True,
        "cleanup": "not_applicable",
    }
    assert cleanup_calls == []
    assert _stored_delivery(path) == (
        ("outcome_unknown", 1, "", ""),
        ("cancelled", ""),
    )


def test_replacement_delivery_during_provider_io_is_unchanged_by_late_ack(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}
    cleanup_calls: list[str] = []

    def slow_sender(_recipient: str, _message: dict, _retry_key: str) -> None:
        entered.set()
        assert release.wait(5)

    deliver = create_health_check_delivery_service(
        path,
        sender=slow_sender,
        now=lambda: NOW,
        cleanup=lambda case_id: cleanup_calls.append(case_id),
    )

    def run_delivery() -> None:
        try:
            outcome["result"] = deliver(approval["delivery_key"])
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_delivery)
    worker.start()
    assert entered.wait(5)
    try:
        with sqlite3.connect(path, timeout=0.2) as conn:
            original = conn.execute(
                "SELECT report_id,user_id,created_at FROM vip_health_check_deliveries"
            ).fetchone()
            conn.execute(
                "UPDATE vip_health_check_deliveries SET status='failed',last_error='superseded'"
            )
            conn.execute(
                """INSERT INTO vip_health_check_deliveries
                   (delivery_id,report_id,user_id,delivery_key,status,attempts,last_error,
                    created_at,delivered_at)
                   VALUES ('replacement-delivery',?,?,?,'pending',0,'',?,'')""",
                (original[0], original[1], "replacement-operation", original[2]),
            )
            replacement_before = conn.execute(
                "SELECT * FROM vip_health_check_deliveries "
                "WHERE delivery_id='replacement-delivery'"
            ).fetchone()
            conn.commit()
    finally:
        release.set()
        worker.join(5)

    assert worker.is_alive() is False
    assert "error" not in outcome
    assert outcome["result"]["status"] == "accepted_stale"
    assert cleanup_calls == []
    with sqlite3.connect(path) as conn:
        old = conn.execute(
            "SELECT status,attempts,last_error,delivered_at FROM vip_health_check_deliveries "
            "WHERE delivery_key=?",
            (approval["delivery_key"],),
        ).fetchone()
        replacement_after = conn.execute(
            "SELECT * FROM vip_health_check_deliveries "
            "WHERE delivery_id='replacement-delivery'"
        ).fetchone()
        case = conn.execute(
            "SELECT status,report_published_at FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()
    assert old == ("failed", 1, "superseded", "")
    assert replacement_after == replacement_before
    assert case == ("approved_pending_delivery", "")


def test_owner_change_during_ambiguous_provider_exception_stays_unknown(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}
    replacement_owner = "U99999999999999999999999999999999"

    def failing_sender(_recipient: str, _message: dict, _retry_key: str) -> None:
        entered.set()
        assert release.wait(5)
        raise RuntimeError("provider unavailable")

    deliver = create_health_check_delivery_service(path, sender=failing_sender, now=lambda: NOW)

    def run_delivery() -> None:
        try:
            outcome["result"] = deliver(approval["delivery_key"])
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_delivery)
    worker.start()
    assert entered.wait(5)
    try:
        with sqlite3.connect(path, timeout=0.2) as conn:
            conn.execute(
                "UPDATE vip_health_check_cases SET user_id=? WHERE case_id='case-1'",
                (replacement_owner,),
            )
            conn.commit()
    finally:
        release.set()
        worker.join(5)

    assert worker.is_alive() is False
    assert "error" not in outcome
    assert outcome["result"] == {
        "delivery_key": approval["delivery_key"],
        "report_id": approval["report_id"],
        "status": "outcome_unknown",
        "attempts": 1,
        "provider_accepted": None,
        "cleanup": "not_applicable",
    }
    with sqlite3.connect(path) as conn:
        delivery = conn.execute(
            "SELECT user_id,status,attempts,last_error,delivered_at "
            "FROM vip_health_check_deliveries"
        ).fetchone()
        case = conn.execute(
            "SELECT user_id,status,report_published_at FROM vip_health_check_cases "
            "WHERE case_id='case-1'"
        ).fetchone()
    assert delivery == (CUSTOMER_UID, "outcome_unknown", 1, "provider unavailable", "")
    assert case == (replacement_owner, "approved_pending_delivery", "")


def test_host_lock_prevents_second_send_even_when_first_call_outlives_nominal_lease(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_sender(recipient: str, message: dict, retry_key: str) -> None:
        calls.append(retry_key)
        entered.set()
        assert release.wait(5)

    deliver = create_health_check_delivery_service(path, sender=slow_sender, now=lambda: NOW)
    first_result: list[dict] = []
    worker = threading.Thread(target=lambda: first_result.append(deliver(approval["delivery_key"])))
    worker.start()
    assert entered.wait(5)

    competing = deliver(approval["delivery_key"])
    assert competing["status"] == "in_progress"
    assert len(calls) == 1

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert first_result[0]["status"] == "delivered"
    assert len(calls) == 1


def test_delivery_marker_failure_stays_unknown_and_never_blindly_retries(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TRIGGER reject_delivery_marker
            BEFORE UPDATE OF status ON vip_health_check_deliveries
            WHEN NEW.status='delivered'
            BEGIN SELECT RAISE(ABORT, 'marker failure'); END""")
        conn.commit()
    sender = FakeSender()
    cleanup_calls: list[str] = []
    deliver = create_health_check_delivery_service(
        path,
        sender=sender,
        now=lambda: NOW,
        cleanup=lambda case_id: cleanup_calls.append(case_id),
    )

    with pytest.raises(sqlite3.IntegrityError, match="marker failure"):
        deliver(approval["delivery_key"])

    assert _stored_delivery(path) == (
        ("outcome_unknown", 1, "", ""),
        ("approved_pending_delivery", ""),
    )
    assert cleanup_calls == []
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER reject_delivery_marker")
        conn.commit()

    result = deliver(approval["delivery_key"])

    assert result["status"] == "outcome_unknown"
    assert result["attempts"] == 1
    assert len(sender.calls) == 1
    assert cleanup_calls == []


def test_delivery_commit_failure_stays_unknown_and_never_blindly_retries(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker_parent (id TEXT PRIMARY KEY)")
        conn.execute(
            """CREATE TABLE marker_commit_probe (
                   parent_id TEXT NOT NULL,
                   FOREIGN KEY(parent_id) REFERENCES marker_parent(id)
                     DEFERRABLE INITIALLY DEFERRED)"""
        )
        conn.execute("""CREATE TRIGGER reject_delivery_commit
            AFTER UPDATE OF status ON vip_health_check_deliveries
            WHEN NEW.status='delivered'
            BEGIN INSERT INTO marker_commit_probe(parent_id) VALUES ('missing'); END""")
        conn.commit()
    sender = FakeSender()
    cleanup_calls: list[str] = []
    deliver = create_health_check_delivery_service(
        path,
        sender=sender,
        now=lambda: NOW,
        cleanup=lambda case_id: cleanup_calls.append(case_id),
    )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        deliver(approval["delivery_key"])

    assert _stored_delivery(path) == (
        ("outcome_unknown", 1, "", ""),
        ("approved_pending_delivery", ""),
    )
    assert cleanup_calls == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM marker_commit_probe").fetchall() == []
        conn.execute("DROP TRIGGER reject_delivery_commit")
        conn.commit()

    result = deliver(approval["delivery_key"])

    assert result["status"] == "outcome_unknown"
    assert result["attempts"] == 1
    assert len(sender.calls) == 1
    assert cleanup_calls == []


def test_cleanup_failure_does_not_rollback_delivery_and_replay_retries_cleanup(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    sender = FakeSender()
    cleanup_calls = 0

    def cleanup(_case_id: str) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise OSError("storage unavailable")

    deliver = create_health_check_delivery_service(
        path, sender=sender, now=lambda: NOW, cleanup=cleanup
    )
    first = deliver(approval["delivery_key"])
    assert first["status"] == "delivered"
    assert first["cleanup"] == "retry_pending"
    assert _stored_delivery(path)[0][0] == "delivered"

    replay = deliver(approval["delivery_key"])
    assert replay["status"] == "delivered"
    assert replay["cleanup"] == "completed"
    assert len(sender.calls) == 1
    assert cleanup_calls == 2


def test_cleanup_retry_pending_count_does_not_report_completed_and_replay_recovers(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    sender = FakeSender()
    cleanup_results = iter((
        {"deleted": 0, "missing": 0, "blocked": 0, "retry_pending": 1},
        {"deleted": 1, "missing": 0, "blocked": 0, "retry_pending": 0},
    ))
    deliver = create_health_check_delivery_service(
        path, sender=sender, now=lambda: NOW,
        cleanup=lambda _case_id: next(cleanup_results),
    )

    first = deliver(approval["delivery_key"])
    replay = deliver(approval["delivery_key"])

    assert first["status"] == replay["status"] == "delivered"
    assert first["cleanup"] == "retry_pending"
    assert replay["cleanup"] == "completed"
    assert len(sender.calls) == 1


def test_unknown_cleanup_result_fails_closed_without_rolling_back_delivery(tmp_path):
    from dietitian_health_check_delivery import create_health_check_delivery_service

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    sender = FakeSender()
    deliver = create_health_check_delivery_service(
        path, sender=sender, now=lambda: NOW,
        cleanup=lambda _case_id: {"deleted": 0, "missing": 0, "blocked": 1, "retry_pending": 0},
    )

    result = deliver(approval["delivery_key"])

    assert result["status"] == "delivered"
    assert result["cleanup"] == "retry_pending"
    assert _stored_delivery(path)[0][0] == "delivered"
    assert len(sender.calls) == 1
