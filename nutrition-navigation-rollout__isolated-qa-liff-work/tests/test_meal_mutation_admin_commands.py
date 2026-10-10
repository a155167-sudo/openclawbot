from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import server
from tests.test_sheet_atomic_meal_updates import _setup


def _event(uid: str, text: str, message_id: str):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text),
        source=SimpleNamespace(user_id=uid),
        reply_token=f"reply-{message_id}",
    )


def _send(monkeypatch, uid: str, text: str, message_id: str):
    replies = []
    monkeypatch.setattr(
        server.line_bot_api,
        "reply_message",
        lambda _token, message: replies.append(message.text),
    )
    server.processed_messages.clear()
    server.handle_message(_event(uid, text, message_id))
    return replies


def _unknown_swap(tmp_path, monkeypatch):
    owner_uid, db_path, worksheet, book = _setup(
        tmp_path, monkeypatch, outcome="apply_then_timeout"
    )
    # General AI is intentionally read-only for meal mutation.  Exercise the
    # real mutation backend directly to construct the unknown operation that
    # these administrative authorization/reconciliation tests require.
    response = server.execute_meal_swap(
        owner_uid,
        "2099/10/01",
        "午餐",
        "2099/10/02",
        "午餐",
        operation_id=f"line-ai:{owner_uid}:UNKNOWN-1",
    )
    assert "結果未確認" in response
    admin_uid = "U" + "a" * 32
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings(key,value) VALUES('admin_id',?)",
            (admin_uid,),
        )
    return owner_uid, admin_uid, db_path, worksheet, book


def _unknown_defer(tmp_path, monkeypatch):
    owner_uid, db_path, worksheet, book = _setup(
        tmp_path, monkeypatch, outcome="apply_then_timeout"
    )
    worksheet.rows[1][2] = ""
    request_id = server.create_deferred_meal_request(
        owner_uid, "測試會員", "2099/10/01", "午餐", "2099/10/02", "午餐", False, False
    )
    ok, message = server.execute_deferred_meal_move(
        owner_uid, "2099/10/01", "午餐", "2099/10/02", "午餐",
        request_id=str(request_id), admin_uid="ORIGINAL-ADMIN",
    )
    assert not ok and "結果未確認" in message
    admin_uid = "U" + "a" * 32
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings(key,value) VALUES('admin_id',?)",
            (admin_uid,),
        )
    return owner_uid, admin_uid, db_path, request_id, book


def _dump(db_path):
    with sqlite3.connect(db_path) as conn:
        return "\n".join(conn.iterdump())


def test_registered_admin_can_inspect_unknown_operation_without_exposing_secrets(tmp_path, monkeypatch):
    owner_uid, admin_uid, db_path, _worksheet, _book = _unknown_swap(tmp_path, monkeypatch)
    operation_id = f"line-ai:{owner_uid}:UNKNOWN-1"

    replies = _send(monkeypatch, admin_uid, f"#查核換餐 {operation_id}", "ADMIN-INSPECT-1")

    assert len(replies) == 1
    text = replies[0]
    assert operation_id in text
    assert "outcome_unknown" in text
    assert f"owner：{owner_uid}" not in text
    assert "owner：U-***ET" in text
    assert "claim_token" not in text
    assert "已確認無延遲請求" in text


@pytest.mark.parametrize("actor_kind", ["public", "vip_public", "coach"])
def test_registered_outer_gate_silently_blocks_non_admin_roles(tmp_path, monkeypatch, actor_kind):
    owner_uid, _admin_uid, db_path, _worksheet, book = _unknown_swap(tmp_path, monkeypatch)
    actor = "U" + ({"public": "b", "vip_public": "c", "coach": "d"}[actor_kind] * 32)
    if actor_kind == "vip_public":
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO usage(user_id,status,expiry_date,remaining_meals,remaining_chat_quota,daily_chat_limit,last_date) "
                "VALUES (?,'vip','2099-12-31',10,10,10,'2099-01-01')",
                (actor,),
            )
    if actor_kind == "coach":
        monkeypatch.setattr(server, "COACH_UIDS", {actor})
    before = _dump(db_path)

    replies = _send(
        monkeypatch, actor, f"#查核換餐 line-ai:{owner_uid}:UNKNOWN-1", f"DENY-{actor_kind}"
    )

    assert replies == []
    assert _dump(db_path) == before
    assert len(book.batch_calls) == 1


@pytest.mark.parametrize("bad_tail", [
    "", "已確認沒有延遲請求 evidence-1", "已確認無延遲請求",
])
def test_confirmation_requires_exact_quiescence_phrase_and_nonempty_evidence(
    tmp_path, monkeypatch, bad_tail
):
    owner_uid, admin_uid, db_path, _worksheet, book = _unknown_swap(tmp_path, monkeypatch)
    operation_id = f"line-ai:{owner_uid}:UNKNOWN-1"
    before = _dump(db_path)
    text = f"#確認換餐已套用 {operation_id} {owner_uid}"
    if bad_tail:
        text += " " + bad_tail

    replies = _send(monkeypatch, admin_uid, text, "BAD-CONFIRM")

    assert len(replies) == 1 and "未寫入" in replies[0]
    assert _dump(db_path) == before
    assert len(book.batch_calls) == 1


def test_registered_applied_command_writes_manual_terminal_once_without_sheet_or_push(tmp_path, monkeypatch):
    owner_uid, admin_uid, db_path, _worksheet, book = _unknown_swap(tmp_path, monkeypatch)
    operation_id = f"line-ai:{owner_uid}:UNKNOWN-1"
    pushes = []
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda *args: pushes.append(args))
    command = (
        f"#確認換餐已套用 {operation_id} {owner_uid} "
        "已確認無延遲請求 ticket-77 provider queue drained"
    )

    first = _send(monkeypatch, admin_uid, command, "APPLY-1")
    after_first = _dump(db_path)
    second = _send(monkeypatch, admin_uid, command, "APPLY-2")

    assert "終態：completed" in first[0] and "保留稽核" in first[0]
    assert "既存終態" in second[0]
    assert _dump(db_path) == after_first
    assert len(book.batch_calls) == 1
    assert pushes == []
    with sqlite3.connect(db_path) as conn:
        status, note, resolver, unknown = conn.execute(
            "SELECT status,resolution_note,resolved_by,unknown_reason FROM meal_mutation_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    assert (status, note, resolver) == (
        "completed", "ticket-77 provider queue drained", admin_uid
    )
    assert unknown


def test_wrong_owner_and_corrupt_terminal_never_render_success(tmp_path, monkeypatch):
    owner_uid, admin_uid, db_path, _worksheet, _book = _unknown_swap(tmp_path, monkeypatch)
    operation_id = f"line-ai:{owner_uid}:UNKNOWN-1"
    wrong = (
        f"#確認換餐已套用 {operation_id} WRONG-OWNER "
        "已確認無延遲請求 ticket-wrong queue drained"
    )
    before = _dump(db_path)
    wrong_reply = _send(monkeypatch, admin_uid, wrong, "WRONG-OWNER-1")[0]
    assert "未寫入" in wrong_reply and "人工查核已完成" not in wrong_reply
    assert _dump(db_path) == before

    valid = (
        f"#確認換餐未套用 {operation_id} {owner_uid} "
        "已確認無延遲請求 ticket-corrupt queue drained"
    )
    assert "manual_not_applied" in _send(monkeypatch, admin_uid, valid, "CORRUPT-SETUP")[0]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE meal_mutation_operations SET result_json=? WHERE operation_id=?",
            ('{"message":"forged success","outcome":"manual_not_applied"}', operation_id),
        )
    corrupted_before = _dump(db_path)

    corrupt_reply = _send(monkeypatch, admin_uid, valid, "CORRUPT-REPLAY")[0]

    assert "未寫入" in corrupt_reply
    assert "既存終態" not in corrupt_reply and "人工查核已完成" not in corrupt_reply
    assert _dump(db_path) == corrupted_before


def test_registered_not_applied_terminal_replays_old_event_without_second_batch(tmp_path, monkeypatch):
    owner_uid, admin_uid, db_path, _worksheet, book = _unknown_swap(tmp_path, monkeypatch)
    operation_id = f"line-ai:{owner_uid}:UNKNOWN-1"
    command = (
        f"#確認換餐未套用 {operation_id} {owner_uid} "
        "已確認無延遲請求 ticket-88 provider rejected and queue drained"
    )

    replies = _send(monkeypatch, admin_uid, command, "NOT-APPLIED-1")
    response = server.execute_meal_swap(
        owner_uid,
        "2099/10/01",
        "午餐",
        "2099/10/02",
        "午餐",
        operation_id=operation_id,
    )

    assert "manual_not_applied" in replies[0]
    assert "未套用且不會晚到" in response
    assert len(book.batch_calls) == 1
    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM meal_mutation_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()[0]
    assert status == "manual_not_applied"


@pytest.mark.parametrize(
    "label,expected_operation_status,expected_request_status",
    [
        ("已套用", "completed", "completed"),
        ("未套用", "manual_not_applied", "pending"),
    ],
)
def test_registered_defer_manual_dispositions_preserve_linked_contract(
    tmp_path, monkeypatch, label, expected_operation_status, expected_request_status
):
    owner_uid, admin_uid, db_path, request_id, book = _unknown_defer(tmp_path, monkeypatch)
    operation_id = f"deferred-meal:{request_id}"
    command = (
        f"#確認換餐{label} {operation_id} {owner_uid} "
        f"已確認無延遲請求 defer-ticket-{label} queue drained"
    )

    replies = _send(monkeypatch, admin_uid, command, f"DEFER-{label}")

    assert expected_operation_status in replies[0]
    assert len(book.batch_calls) == 1
    with sqlite3.connect(db_path) as conn:
        operation_status = conn.execute(
            "SELECT status FROM meal_mutation_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()[0]
        request_status = conn.execute(
            "SELECT status FROM deferred_meals WHERE id=?", (request_id,)
        ).fetchone()[0]
    assert operation_status == expected_operation_status
    assert request_status == expected_request_status


def test_pending_list_and_separate_copy_template_make_operation_and_owner_usable(tmp_path, monkeypatch):
    owner_uid, admin_uid, _db_path, _worksheet, _book = _unknown_swap(tmp_path, monkeypatch)
    operation_id = f"line-ai:{owner_uid}:UNKNOWN-1"

    pending = _send(monkeypatch, admin_uid, "#待核換餐", "PENDING-1")[0]
    template = _send(
        monkeypatch, admin_uid, f"#取得換餐確認指令 {operation_id}", "TEMPLATE-1"
    )[0]

    assert operation_id in pending and "outcome_unknown" in pending
    assert f"owner {_mask_expected(owner_uid)}" in pending
    assert f"{operation_id} {owner_uid} 已確認無延遲請求 <evidence>" in template


@pytest.mark.parametrize("setup_kind", ["swap", "defer"])
@pytest.mark.parametrize("label", ["已套用", "未套用"])
def test_registered_copy_pasted_placeholder_template_fails_closed(
    tmp_path, monkeypatch, setup_kind, label
):
    if setup_kind == "swap":
        owner_uid, admin_uid, db_path, _worksheet, book = _unknown_swap(tmp_path, monkeypatch)
        operation_id = f"line-ai:{owner_uid}:UNKNOWN-1"
    else:
        owner_uid, admin_uid, db_path, request_id, book = _unknown_defer(tmp_path, monkeypatch)
        operation_id = f"deferred-meal:{request_id}"
    template = _send(
        monkeypatch, admin_uid, f"#取得換餐確認指令 {operation_id}",
        f"TEMPLATE-{setup_kind}-{label}",
    )[0]
    command = next(
        line for line in template.splitlines()
        if line.startswith(f"#確認換餐{label} ")
    )
    before = _dump(db_path)
    pushes = []
    core_calls = []
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda *args: pushes.append(args))
    monkeypatch.setattr(
        server, "manually_reconcile_meal_mutation",
        lambda *args, **kwargs: core_calls.append((args, kwargs)),
    )

    replies = _send(monkeypatch, admin_uid, command, f"PASTE-{setup_kind}-{label}")

    assert len(replies) == 1
    assert "未寫入" in replies[0] and "必須替換證據" in replies[0]
    assert _dump(db_path) == before
    assert len(book.batch_calls) == 1
    assert pushes == []
    assert core_calls == []
    with sqlite3.connect(db_path) as conn:
        status, note = conn.execute(
            "SELECT status,resolution_note FROM meal_mutation_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    assert status == "outcome_unknown"
    assert note == ""


@pytest.mark.parametrize("placeholder", ["<evidence>", "  <evidence>  ", "<evidence> ticket-1"])
def test_placeholder_evidence_trim_variants_are_rejected(placeholder):
    with pytest.raises(ValueError, match="必須替換證據"):
        server._parse_manual_meal_confirmation(
            ("#確認換餐已套用 op owner 已確認無延遲請求 " + placeholder).strip()
        )


def _mask_expected(uid):
    return uid[:2] + "***" + uid[-2:]
