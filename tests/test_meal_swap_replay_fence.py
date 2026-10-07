import sqlite3
from types import SimpleNamespace

import server
from tests.test_sheet_atomic_meal_updates import _setup


MESSAGE = "把 2099/10/01 午餐與 2099/10/02 午餐互換"


def _backend_swap(uid, event_id):
    return server.execute_meal_swap(
        uid,
        "2099/10/01",
        "午餐",
        "2099/10/02",
        "午餐",
        operation_id=f"line-ai:{uid}:{event_id}",
    )


def test_init_db_installs_meal_mutation_component(tmp_path, monkeypatch):
    uid, db_path, _worksheet, _book = _setup(
        tmp_path, monkeypatch, outcome="accept"
    )

    with sqlite3.connect(db_path) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'meal_mutation_%'"
            )
        }
        version = conn.execute(
            "SELECT component,version FROM meal_mutation_schema_versions"
        ).fetchone()
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert uid
    assert "meal_mutation_operations" in names
    assert "meal_mutation_resource_locks" in names
    assert version == ("meal_mutation", 1)
    assert fk_errors == []


def test_mutating_swap_without_stable_identity_fails_closed(tmp_path, monkeypatch):
    uid, _db_path, _worksheet, book = _setup(
        tmp_path, monkeypatch, outcome="accept"
    )

    response, _ = server.get_ai_response_with_memory(uid, MESSAGE)

    assert len(book.batch_calls) == 0
    assert "本次未修改菜單" in response


def test_registered_swap_without_line_message_id_sends_no_batch(tmp_path, monkeypatch):
    uid, _db_path, _worksheet, book = _setup(
        tmp_path, monkeypatch, outcome="accept"
    )
    replies = []
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: True)
    monkeypatch.setattr(server, "check_permission_and_quota", lambda _uid: (True, "quota"))
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message.text),
    )
    server.processed_messages.clear()
    event = SimpleNamespace(
        message=SimpleNamespace(id=None, text=MESSAGE),
        source=SimpleNamespace(user_id=uid),
        reply_token="missing-id",
    )

    server.handle_message(event)

    assert len(book.batch_calls) == 0
    assert replies and "本次未修改菜單" in replies[0]


def test_unknown_operation_blocks_new_event_on_overlapping_cells(tmp_path, monkeypatch):
    uid, _db_path, _worksheet, book = _setup(
        tmp_path, monkeypatch, outcome="apply_then_timeout"
    )

    first = _backend_swap(uid, "UNKNOWN-1")
    second = _backend_swap(uid, "UNKNOWN-2")

    assert "結果未確認" in first
    assert "結果未確認" in second
    assert len(book.batch_calls) == 1


def test_local_finalize_failure_leaves_reserved_fence_and_replay_sends_no_batch(
    tmp_path, monkeypatch
):
    uid, db_path, _worksheet, book = _setup(
        tmp_path, monkeypatch, outcome="accept"
    )
    real_complete = server.complete_meal_mutation

    def fail_finalize(*_args, **_kwargs):
        raise RuntimeError("injected local finalize failure")

    monkeypatch.setattr(server, "complete_meal_mutation", fail_finalize)
    first = _backend_swap(uid, "FINALIZE-FAIL")
    monkeypatch.setattr(server, "complete_meal_mutation", real_complete)
    second = _backend_swap(uid, "FINALIZE-FAIL")

    assert "結果未確認" in first
    assert "結果未確認" in second
    assert len(book.batch_calls) == 1
    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM meal_mutation_operations WHERE event_id=?",
            ("FINALIZE-FAIL",),
        ).fetchone()[0]
        lock_count = conn.execute(
            "SELECT COUNT(*) FROM meal_mutation_resource_locks"
        ).fetchone()[0]
    assert status == "reserved"
    assert lock_count == 2


def test_same_event_changed_swap_payload_conflicts_without_sheet_write(
    tmp_path, monkeypatch
):
    uid, _db_path, _worksheet, book = _setup(
        tmp_path, monkeypatch, outcome="accept"
    )
    operation_id = f"line-ai:{uid}:PAYLOAD-CONFLICT"

    first = server.execute_meal_swap(
        uid,
        "2099/10/01",
        "午餐",
        "2099/10/02",
        "午餐",
        operation_id=operation_id,
    )
    second = server.execute_meal_swap(
        uid,
        "2099/10/02",
        "午餐",
        "2099/10/01",
        "午餐",
        operation_id=operation_id,
    )

    assert "成功" in first
    assert "無法確認" in second or "衝突" in second
    assert len(book.batch_calls) == 1
