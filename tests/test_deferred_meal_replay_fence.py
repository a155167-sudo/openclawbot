import hashlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

from gspread.exceptions import APIError
import pytest
from requests import Response

import meal_mutation_ledger
import server
from tests.test_sheet_atomic_meal_updates import _setup


ADMIN = "U" + "a" * 32
OTHER = "U" + "b" * 32


def _bind_admin(db_path, monkeypatch, uid=ADMIN):
    monkeypatch.setattr(server, "ADMIN_UID", uid)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings(key,value) VALUES('admin_id',?)",
            (uid,),
        )


def _request(customer):
    return server.create_deferred_meal_request(
        customer, "測試會員", "2099/10/01", "午餐", "2099/10/02", "午餐", False, False,
        "原始人工原因",
    )


def _send(uid, request_id, event_id, monkeypatch):
    replies = []
    monkeypatch.setattr(
        server.line_bot_api, "reply_message", lambda _token, message: replies.append(message.text)
    )
    event = SimpleNamespace(
        message=SimpleNamespace(id=event_id, text=f"#核准延餐 {request_id}"),
        source=SimpleNamespace(user_id=uid), reply_token="reply-" + event_id,
    )
    server.handle_message(event)
    return replies


def test_fresh_success_is_request_owned_and_replays_across_admin_event_ids(tmp_path, monkeypatch):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    pushes = []
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda uid, msg: pushes.append((uid, msg.text)))
    request_id = _request(customer)

    first = _send(ADMIN, request_id, "ADMIN-1", monkeypatch)
    server.processed_messages.clear()
    second = _send(ADMIN, request_id, "ADMIN-2", monkeypatch)

    assert len(book.batch_calls) == 1
    assert "已核准延餐申請" in first[0] and "已核准延餐申請" in second[0]
    assert pushes == [(customer, pushes[0][1])]
    with sqlite3.connect(db_path) as conn:
        op = conn.execute(
            "SELECT operation_id,event_id,owner_user_id,request_id,status,before_json,after_json "
            "FROM meal_mutation_operations WHERE purpose='defer'"
        ).fetchone()
        request = conn.execute(
            "SELECT status,approved_by FROM deferred_meals WHERE id=?", (request_id,)
        ).fetchone()
    assert op[:5] == (f"deferred-meal:{request_id}", f"deferred-meal-request:{request_id}", customer, str(request_id), "completed")
    assert "餐A" in op[5] and "無" in op[6]
    assert request == ("completed", ADMIN)


def test_timeout_persists_warning_and_restart_replay_never_redispatches(tmp_path, monkeypatch):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="apply_then_timeout")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    request_id = _request(customer)

    first = _send(ADMIN, request_id, "ADMIN-1", monkeypatch)
    server.processed_messages.clear()
    second = _send(ADMIN, request_id, "ADMIN-2", monkeypatch)

    assert len(book.batch_calls) == 1
    assert "結果未確認" in first[0] and "結果未確認" in second[0]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status,note FROM deferred_meals WHERE id=?", (request_id,)).fetchone()
        op = conn.execute("SELECT status,unknown_reason FROM meal_mutation_operations").fetchone()
        locks = conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0]
    assert row[0] == "pending"
    assert row[1].startswith("原始人工原因") and row[1].count("結果未確認") == 1
    assert op[0] == "outcome_unknown" and op[1]
    assert locks == 2


def test_confirmed_400_releases_locks_but_429_remains_unknown(tmp_path, monkeypatch):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="reject")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    request_id = _request(customer)
    reply = _send(ADMIN, request_id, "A400", monkeypatch)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM meal_mutation_operations").fetchone()[0] == "rejected"
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 0
    assert "失敗" in reply[0]

    rate_path = tmp_path / "rate"
    rate_path.mkdir()
    customer, db_path, worksheet, book = _setup(rate_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    request_id = _request(customer)
    def rate_limit(_body):
        book.batch_calls.append(_body)
        response = Response(); response.status_code = 429
        response._content = b'{"error":{"code":429,"message":"rate"}}'
        raise APIError(response)
    book.batch_update = rate_limit
    reply = _send(ADMIN, request_id, "A429", monkeypatch)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM meal_mutation_operations").fetchone()[0] == "outcome_unknown"
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_resource_locks").fetchone()[0] == 2
    assert "結果未確認" in reply[0]


def test_local_finalize_failure_stays_reserved_and_blocks_same_request(tmp_path, monkeypatch):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    request_id = _request(customer)
    real = server.complete_meal_mutation
    monkeypatch.setattr(server, "complete_meal_mutation", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("commit fail")))
    first = _send(ADMIN, request_id, "ADMIN-1", monkeypatch)
    monkeypatch.setattr(server, "complete_meal_mutation", real)
    server.processed_messages.clear()
    second = _send(ADMIN, request_id, "ADMIN-2", monkeypatch)
    assert len(book.batch_calls) == 1
    assert "結果未確認" in first[0] and "結果未確認" in second[0]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM meal_mutation_operations").fetchone()[0] == "reserved"
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=?", (request_id,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT summary_text FROM health_profile WHERE user_id=?", (customer,)).fetchone()[0] == "before"


def test_unresolved_request_blocks_different_request_on_same_cells(tmp_path, monkeypatch):
    customer, _db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="apply_then_timeout")
    worksheet.rows[1][2] = ""
    _bind_admin(server.DB_PATH, monkeypatch)
    first_id = _request(customer)
    second_id = _request(customer)
    _send(ADMIN, first_id, "ADMIN-1", monkeypatch)
    server.processed_messages.clear()
    reply = _send(ADMIN, second_id, "ADMIN-2", monkeypatch)
    assert len(book.batch_calls) == 1
    assert "結果未確認" in reply[0]


def test_non_bound_admin_cannot_approve_or_touch_sheet(tmp_path, monkeypatch):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    request_id = _request(customer)
    reply = _send(OTHER, request_id, "FORGED", monkeypatch)
    assert len(book.batch_calls) == 0
    assert not reply or "管理員專用" in reply[0]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM deferred_meals WHERE id=?", (request_id,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT COUNT(*) FROM meal_mutation_operations").fetchone()[0] == 0


def _logical_dump(db_path):
    with sqlite3.connect(db_path) as conn:
        return tuple(conn.iterdump())


def _file_sha256(db_path):
    return hashlib.sha256(db_path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "drift_sql,drift_params",
    [
        ("UPDATE deferred_meals SET approved_by='U-tampered-admin' WHERE id=?", ()),
        ("UPDATE deferred_meals SET approved_at='2099-01-01T00:00:00Z' WHERE id=?", ()),
        ("UPDATE deferred_meals SET status='rejected' WHERE id=?", ()),
        ("UPDATE deferred_meals SET user_id='U-tampered-owner' WHERE id=?", ()),
        ("UPDATE deferred_meals SET original_date='2099/12/30' WHERE id=?", ()),
        ("UPDATE deferred_meals SET target_meal_type='晚餐' WHERE id=?", ()),
        (
            "UPDATE health_profile SET summary_text=summary_text || '不是完整紀錄邊界' "
            "WHERE user_id=(SELECT user_id FROM deferred_meals WHERE id=?)",
            (),
        ),
        (
            "UPDATE meal_mutation_operations SET result_json=? WHERE purpose='defer'",
            (json.dumps({"message": "遭竄改", "outcome": "completed"}, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")),),
        ),
        ("UPDATE meal_mutation_operations SET result_json='{' WHERE purpose='defer'", ()),
    ],
    ids=[
        "approved-by", "approved-at", "status", "owner", "original", "target",
        "summary-boundary", "canonical-result-drift", "malformed-result",
    ],
)
def test_registered_completed_replay_fails_closed_on_receipt_field_drift(
    tmp_path, monkeypatch, drift_sql, drift_params
):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    pushes = []
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda uid, msg: pushes.append((uid, msg.text)),
    )
    request_id = _request(customer)
    first = _send(ADMIN, request_id, "ADMIN-FIRST", monkeypatch)
    assert "已核准延餐申請" in first[0]
    assert len(book.batch_calls) == len(pushes) == 1

    with sqlite3.connect(db_path) as conn:
        params = drift_params + ((request_id,) if drift_sql.count("?") > len(drift_params) else ())
        conn.execute(drift_sql, params)
    before_dump = _logical_dump(db_path)
    before_hash = _file_sha256(db_path)

    server.processed_messages.clear()
    replay = _send(ADMIN, request_id, "ADMIN-DIFFERENT-MESSAGE", monkeypatch)

    assert len(book.batch_calls) == len(pushes) == 1
    assert "已核准延餐申請" not in replay[0]
    assert "結果未確認" in replay[0] or "內容衝突" in replay[0]
    assert _logical_dump(db_path) == before_dump
    assert _file_sha256(db_path) == before_hash


def test_registered_completed_replay_by_new_bound_admin_uses_original_receipt_approver(
    tmp_path, monkeypatch
):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    pushes = []
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda uid, msg: pushes.append((uid, msg.text)),
    )
    request_id = _request(customer)
    first = _send(ADMIN, request_id, "ADMIN-FIRST", monkeypatch)
    assert "已核准延餐申請" in first[0]
    _bind_admin(db_path, monkeypatch, OTHER)
    before_dump = _logical_dump(db_path)
    before_hash = _file_sha256(db_path)

    server.processed_messages.clear()
    replay = _send(OTHER, request_id, "OTHER-ADMIN-DIFFERENT-MESSAGE", monkeypatch)

    assert "已核准延餐申請" in replay[0]
    assert len(book.batch_calls) == len(pushes) == 1
    assert _logical_dump(db_path) == before_dump
    assert _file_sha256(db_path) == before_hash


@pytest.mark.parametrize(
    "drift_sql,change_payload,success_expected",
    [
        ("UPDATE deferred_meals SET approved_by='Ucccccccccccccccccccccccccccccccc' WHERE id=?", False, False),
        ("UPDATE deferred_meals SET approved_at='2099-01-01T00:00:00Z' WHERE id=?", False, False),
        ("UPDATE deferred_meals SET status='rejected' WHERE id=?", False, False),
        (
            "UPDATE health_profile SET summary_text='before' "
            "WHERE user_id=(SELECT user_id FROM deferred_meals WHERE id=?)",
            False,
            False,
        ),
        (None, False, True),
        (None, True, False),
    ],
    ids=[
        "approved-by",
        "approved-at",
        "status",
        "summary",
        "valid-control",
        "changed-payload-conflict",
    ],
)
def test_completed_receipt_race_between_stable_lookup_and_reserve_is_validated(
    tmp_path, monkeypatch, drift_sql, change_payload, success_expected
):
    customer, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    worksheet.rows[1][2] = ""
    _bind_admin(db_path, monkeypatch)
    monkeypatch.setattr(meal_mutation_ledger, "_now_text", lambda: "2099-09-25T12:34:56Z")
    pushes = []
    replies = []
    monkeypatch.setattr(
        server.line_bot_api,
        "push_message",
        lambda uid, msg: pushes.append((uid, msg.text)),
    )
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, msg: replies.append(msg.text),
    )
    request_id = _request(customer)

    real_reserve = server.reserve_meal_mutation
    injected = {"done": False, "dump": None, "hash": None, "kind": None}

    def complete_then_drift_before_original_reserve(db_path_arg, binding, **reserve_options):
        if not injected["done"]:
            injected["done"] = True
            reservation = real_reserve(db_path_arg, binding, **reserve_options)
            assert reservation.kind == "claimed"
            summary_line = (
                "\n⏸ 系統紀錄：09/25 12:34 將 "
                "2099/10/01午餐 延至 2099/10/02午餐。"
            )
            message = "✅ 已將 2099/10/01午餐 延至 2099/10/02午餐"
            result = server.complete_meal_mutation(
                db_path_arg,
                reservation.operation_id,
                reservation.claim_token,
                customer,
                summary_line=summary_line,
                result={
                    "message": message,
                    "outcome": "completed",
                    "summary_line": summary_line,
                    "approved_by": ADMIN,
                },
                approved_by=ADMIN,
            )
            assert result.kind == "newly_completed"
            if drift_sql is not None:
                with sqlite3.connect(db_path_arg) as conn:
                    conn.execute(drift_sql, (request_id,))
            injected["dump"] = _logical_dump(db_path)
            injected["hash"] = _file_sha256(db_path)
        replay_binding = binding
        if change_payload:
            replay_binding = replace(
                binding, payload={**binding.payload, "m2": "晚餐"}
            )
        replay = real_reserve(db_path_arg, replay_binding, **reserve_options)
        injected["kind"] = replay.kind
        return replay

    monkeypatch.setattr(
        server, "reserve_meal_mutation", complete_then_drift_before_original_reserve
    )
    server.processed_messages.clear()
    event = SimpleNamespace(
        message=SimpleNamespace(id="ADMIN-RACE", text=f"#核准延餐 {request_id}"),
        source=SimpleNamespace(user_id=ADMIN),
        reply_token="reply-race",
    )
    server.handle_message(event)

    assert bool(replies and "已核准延餐申請" in replies[0]) is success_expected
    assert len(book.batch_calls) == 0
    assert pushes == []
    assert _logical_dump(db_path) == injected["dump"]
    assert _file_sha256(db_path) == injected["hash"]
    if change_payload:
        assert injected["kind"] == "identity_conflict"
    with sqlite3.connect(db_path, timeout=0.1) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
