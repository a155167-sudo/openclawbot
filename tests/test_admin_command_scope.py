from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import server


FIXED_ADMIN = "U" + "a" * 32
BOUND_ADMIN = "U" + "b" * 32
COACH = "U" + "c" * 32
PUBLIC = "U" + "d" * 32


def _event(uid: str, text: str, event_id: str):
    return SimpleNamespace(
        message=SimpleNamespace(id=event_id, text=text),
        source=SimpleNamespace(user_id=uid),
        reply_token=f"reply-{event_id}",
    )


def _configure(tmp_path, monkeypatch, *, bind_admin=True):
    db_path = tmp_path / "admin-scope.db"
    with sqlite3.connect(db_path) as conn:
        if bind_admin:
            conn.execute("CREATE TABLE admin_settings (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO admin_settings(key,value) VALUES('admin_id',?)",
                (BOUND_ADMIN,),
            )
    monkeypatch.setattr(server, "DB_PATH", str(db_path))
    monkeypatch.setattr(server, "ADMIN_UID", FIXED_ADMIN)
    monkeypatch.setattr(server, "COACH_UIDS", [COACH])
    monkeypatch.setattr(server, "has_active_vip_access", lambda _uid: False)
    replies = []
    monkeypatch.setattr(
        server,
        "line_bot_api",
        SimpleNamespace(
            reply_message=lambda _token, message: replies.append(message.text),
            push_message=lambda *_args, **_kwargs: pytest.fail("authorization called push_message"),
        ),
    )
    server.processed_messages.clear()
    return db_path, replies


def _send(uid: str, text: str, event_id: str):
    server.handle_message(_event(uid, text, event_id))


def test_registered_nutrition_commands_keep_fixed_admin_policy(tmp_path, monkeypatch):
    _db_path, replies = _configure(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "handle_exchange_review_admin_command",
        lambda text, uid: calls.append((text, uid)) or "nutrition-ok",
    )

    _send(FIXED_ADMIN, "#待審營養份量", "nutrition-fixed")
    _send(BOUND_ADMIN, "#待審營養份量", "nutrition-bound")

    assert calls == [("#待審營養份量", FIXED_ADMIN)]
    assert replies == ["nutrition-ok"]


@pytest.mark.parametrize("denied_uid", [FIXED_ADMIN, COACH, PUBLIC])
def test_registered_manual_commands_require_bound_admin(
    tmp_path, monkeypatch, denied_uid
):
    db_path, replies = _configure(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "_pending_manual_meal_operations_text",
        lambda: calls.append("manual") or "manual-ok",
    )

    with sqlite3.connect(db_path) as conn:
        before_denial = tuple(conn.iterdump())
    _send(denied_uid, "#待核換餐", f"manual-denied-{denied_uid[-1]}")
    with sqlite3.connect(db_path) as conn:
        after_denial = tuple(conn.iterdump())
    assert calls == []
    assert replies == []
    assert after_denial == before_denial

    _send(BOUND_ADMIN, "#待核換餐", "manual-bound")
    assert calls == ["manual"]
    assert replies == ["manual-ok"]


def test_registered_defer_commands_keep_bound_admin_policy(tmp_path, monkeypatch):
    _db_path, replies = _configure(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "list_pending_deferred_meals",
        lambda limit=20: calls.append(limit) or [],
    )

    _send(FIXED_ADMIN, "#延餐清單", "defer-fixed-denied")
    _send(BOUND_ADMIN, "#延餐清單", "defer-bound-allowed")

    assert calls == [20]
    assert replies == ["✅ 目前沒有待審核的延餐申請。"]


def test_missing_admin_settings_only_preserves_fixed_nutrition_policy(tmp_path, monkeypatch):
    _db_path, replies = _configure(tmp_path, monkeypatch, bind_admin=False)
    nutrition_calls = []
    manual_calls = []
    monkeypatch.setattr(
        server,
        "handle_exchange_review_admin_command",
        lambda text, uid: nutrition_calls.append((text, uid)) or "nutrition-ok",
    )
    monkeypatch.setattr(
        server,
        "_pending_manual_meal_operations_text",
        lambda: manual_calls.append("manual") or "manual-ok",
    )

    _send(FIXED_ADMIN, "#待審營養份量", "missing-table-nutrition")
    _send(FIXED_ADMIN, "#待核換餐", "missing-table-manual")
    _send(FIXED_ADMIN, "#延餐清單", "missing-table-defer")

    assert nutrition_calls == [("#待審營養份量", FIXED_ADMIN)]
    assert manual_calls == []
    assert replies == ["nutrition-ok"]


def test_unknown_admin_like_command_gets_no_new_authorization(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    assert not server.is_admin_only_command("#未知管理指令")
    assert not server.is_authorized_privileged_text_command(
        FIXED_ADMIN, "#未知管理指令"
    )
    assert not server.is_authorized_privileged_text_command(
        BOUND_ADMIN, "#未知管理指令"
    )
