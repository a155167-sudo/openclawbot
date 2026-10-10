from types import SimpleNamespace
import sqlite3

import pytest
from gspread.exceptions import APIError
from requests import Response

import tests.conftest  # install the suite's disposable environment before server import
import server
from tests.test_sheet_atomic_meal_updates import _setup
from tests.test_registered_meal_replay_acceptance import _setup_vip_for_entrypoint

def _swap(uid, event_id):
    """Exercise the mutation backend directly; general AI is intentionally read-only."""
    return server.execute_meal_swap(
        uid,
        "2099/10/01",
        "午餐",
        "2099/10/02",
        "午餐",
        operation_id=f"line-ai:{uid}:{event_id}",
    )


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503])
def test_http_ambiguous_keeps_resend_fence(tmp_path, monkeypatch, code):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _setup_vip_for_entrypoint(db_path, uid)

    calls = []
    def rate_limited(_body):
        import json
        calls.append(_body)
        response = Response()
        response.status_code = code
        response._content = json.dumps({"error": {"code": code, "message": "ambiguous", "status": "UNKNOWN"}}).encode()
        raise APIError(response)

    book.batch_update = rate_limited
    first = _swap(uid, "RATE-429")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status,unknown_reason FROM meal_mutation_operations WHERE event_id='RATE-429'"
        ).fetchone()
        locks = conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0]
        summary = conn.execute(
            "SELECT summary_text FROM health_profile WHERE user_id=?", (uid,)
        ).fetchone()[0]
    observed = (
        "結果未確認" in first,
        row,
        locks,
        summary,
        (worksheet.rows[0][2], worksheet.rows[1][2]),
    )
    expected = (
        True,
        ("outcome_unknown", "Google Sheet 原子批次結果未確認"),
        2,
        "before",
        ("餐A", "餐B"),
    )
    assert observed == expected
    for event_id in ("RATE-429", "RATE-NEW"):
        assert "結果未確認" in _swap(uid, event_id)
    assert len(calls) == 1


def test_reply_failure_replay_does_not_repeat_swap_or_audit(tmp_path, monkeypatch):
    uid, db_path, _worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _setup_vip_for_entrypoint(db_path, uid)
    result = _swap(uid, "REPLY-FAIL")

    def fail_reply(_message):
        raise RuntimeError("injected LINE reply failure")

    with pytest.raises(RuntimeError, match="LINE reply failure"):
        fail_reply(result)
    replay = _swap(uid, "REPLY-FAIL")

    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM meal_mutation_operations WHERE event_id='REPLY-FAIL'"
        ).fetchone()[0]
        operation_count = conn.execute(
            "SELECT COUNT(*) FROM meal_mutation_operations WHERE event_id='REPLY-FAIL'"
        ).fetchone()[0]
        lock_count = conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0]
        summary = conn.execute(
            "SELECT summary_text FROM health_profile WHERE user_id=?", (uid,)
        ).fetchone()[0]
    assert len(book.batch_calls) == 1
    assert status == "completed"
    assert operation_count == 1
    assert lock_count == 0
    assert summary.count("🔄 系統紀錄") == 1
    assert replay == result
