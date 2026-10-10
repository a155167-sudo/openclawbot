import sqlite3
from datetime import timedelta

import pytest

import server
from meal_photo_system import get_meal_photo_draft
from tests.test_photo_batch_single_quota import _enter_add, _install_usage
from tests.test_photo_ingredient_controls import _text
from tests.test_photo_natural_ingredient_batch import _estimate, _setup


def test_expired_old_worker_cleanup_cannot_retire_child_adopted_by_new_batch_owner(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", _estimate)
    parsed = server.parse_photo_ingredient_batch("木耳20g")

    with sqlite3.connect(db) as conn:
        batch_key, old_owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="STALE-CLEANUP", parsed_items=parsed,
        )
    child = server.create_text_meal_estimate_draft(
        user_id="U1",
        message_id=f"photo-add-batch:{token}:{draft['version']}:STALE-CLEANUP:0",
        request={"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""},
        quota_batch_key=batch_key, quota_batch_owner=old_owner,
    )
    assert child["status"] == "pending"

    clock[0] += timedelta(seconds=server.TEXT_MEAL_ESTIMATE_LEASE_SECONDS + 1)
    with sqlite3.connect(db) as conn:
        same_key, new_owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="STALE-CLEANUP", parsed_items=parsed,
        )
        assert same_key == batch_key and new_owner != old_owner
        server._fail_photo_ingredient_batch_children(
            conn, user_id="U1", child_tokens=[child["token"]],
            batch_key=batch_key, batch_owner=old_owner,
        )

    with sqlite3.connect(db) as conn:
        observed = (
            conn.execute(
                "SELECT status FROM pending_text_meal_estimates WHERE token=?", (child["token"],)
            ).fetchone()[0],
            conn.execute(
                "SELECT lease_owner,status FROM photo_ingredient_batch_quota_ops WHERE batch_key=?",
                (batch_key,),
            ).fetchone(),
            conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0],
            conn.execute(
                "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status ORDER BY status"
            ).fetchall(),
        )
    assert observed == (
        "pending", (new_owner, "processing"), 1, [("charged", 1), ("refunded", 1)]
    )


def test_expired_owner_cleanup_is_noop_even_before_takeover(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", _estimate)
    parsed = server.parse_photo_ingredient_batch("木耳20g")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="EXPIRED-NO-TAKEOVER", parsed_items=parsed,
        )
    child = server.create_text_meal_estimate_draft(
        user_id="U1",
        message_id=f"photo-add-batch:{token}:{draft['version']}:EXPIRED-NO-TAKEOVER:0",
        request={"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""},
        quota_batch_key=batch_key, quota_batch_owner=owner,
    )
    clock[0] += timedelta(seconds=server.TEXT_MEAL_ESTIMATE_LEASE_SECONDS + 1)

    with sqlite3.connect(db) as conn:
        assert server._fail_photo_ingredient_batch_children(
            conn, user_id="U1", child_tokens=[child["token"]],
            batch_key=batch_key, batch_owner=owner,
        ) is False
        assert conn.execute(
            "SELECT status FROM pending_text_meal_estimates WHERE token=?", (child["token"],)
        ).fetchone() == ("pending",)
        assert conn.execute(
            "SELECT status FROM photo_ingredient_batch_quota_ops WHERE batch_key=?", (batch_key,)
        ).fetchone() == ("processing",)
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone() == (1,)


def test_batch_scope_rejects_unbound_child_index_and_payload_before_provider(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    parsed = server.parse_photo_ingredient_batch("木耳20g")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="BOUND-SCOPE", parsed_items=parsed,
        )
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))

    caught = None
    try:
        created = server.create_text_meal_estimate_draft(
            user_id="U1",
            message_id=f"photo-add-batch:{token}:{draft['version']}:BOUND-SCOPE:99",
            request={"food_name": "未授權額外食材", "amount": 999, "unit": "g", "meal_slot": ""},
            quota_batch_key=batch_key, quota_batch_owner=owner,
        )
    except ValueError as exc:
        caught, created = exc, None
    with sqlite3.connect(db) as conn:
        observed = (
            type(caught).__name__ if caught else None,
            str(caught or ""),
            len(calls),
            created and created["status"],
            conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0],
        )
    assert observed == ("ValueError", "批次AI估算額度範圍無效", 0, None, 1)


@pytest.mark.parametrize(
    ("child_index", "child_request"),
    [
        (0, {"food_name": "偽造木耳", "amount": 20, "unit": "g", "meal_slot": ""}),
        (0, {"food_name": "木耳", "amount": 21, "unit": "g", "meal_slot": ""}),
        (1, {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""}),
        (0, {"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": "", "source": "forged"}),
    ],
)
def test_batch_scope_rejects_valid_index_payload_substitution_plan_swap_and_extra_fields(
    tmp_path, monkeypatch, child_index, child_request
):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    parsed = server.parse_photo_ingredient_batch("木耳20g、青菜30g")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="BOUND-PAYLOAD", parsed_items=parsed,
        )
        before = list(conn.iterdump())
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))

    with pytest.raises(ValueError, match="批次AI估算額度範圍無效"):
        server.create_text_meal_estimate_draft(
            user_id="U1",
            message_id=(
                f"photo-add-batch:{token}:{draft['version']}:BOUND-PAYLOAD:{child_index}"
            ),
            request=child_request,
            quota_batch_key=batch_key,
            quota_batch_owner=owner,
        )

    with sqlite3.connect(db) as conn:
        assert list(conn.iterdump()) == before
    assert calls == []


def test_batch_scope_accepts_canonical_equivalent_numeric_and_unit_payload(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    parsed = server.parse_photo_ingredient_batch("木耳20公克、豆漿1/6份")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="CANONICAL", parsed_items=parsed,
        )
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))

    first = server.create_text_meal_estimate_draft(
        user_id="U1",
        message_id=f"photo-add-batch:{token}:{draft['version']}:CANONICAL:0",
        request={"food_name": "木耳", "amount": 20.0, "unit": "克", "meal_slot": ""},
        quota_batch_key=batch_key, quota_batch_owner=owner,
    )
    second = server.create_text_meal_estimate_draft(
        user_id="U1",
        message_id=f"photo-add-batch:{token}:{draft['version']}:CANONICAL:1",
        request={"food_name": "豆漿", "amount": float(1 / 6), "unit": "serving", "meal_slot": ""},
        quota_batch_key=batch_key, quota_batch_owner=owner,
    )

    assert (first["status"], second["status"]) == ("pending", "pending")
    assert [call["food_name"] for call in calls] == ["木耳", "豆漿"]


def test_batch_scope_is_checked_before_replaying_existing_child(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", _estimate)
    parsed = server.parse_photo_ingredient_batch("木耳20g")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="REPLAY-SCOPE", parsed_items=parsed,
        )
    message_id = f"photo-add-batch:{token}:{draft['version']}:REPLAY-SCOPE:0"
    child = server.create_text_meal_estimate_draft(
        user_id="U1", message_id=message_id,
        request={"food_name": "木耳", "amount": 20, "unit": "g", "meal_slot": ""},
        quota_batch_key=batch_key, quota_batch_owner=owner,
    )
    with sqlite3.connect(db) as conn:
        before = list(conn.iterdump())

    with pytest.raises(ValueError, match="批次AI估算額度範圍無效"):
        server.create_text_meal_estimate_draft(
            user_id="U1", message_id=message_id,
            request={"food_name": "木耳", "amount": 999, "unit": "g", "meal_slot": ""},
            quota_batch_key=batch_key, quota_batch_owner=owner,
        )
    with sqlite3.connect(db) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute(
            "SELECT status FROM pending_text_meal_estimates WHERE token=?", (child["token"],)
        ).fetchone() == ("pending",)


def test_batch_scope_rejects_full_plan_item_not_selected_for_ai(tmp_path, monkeypatch):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    parsed = server.parse_photo_ingredient_batch("已知20g、未知30g")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="AI-SUBSET", parsed_items=parsed, allowed_child_indexes=[1],
        )
        before = list(conn.iterdump())
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))

    with pytest.raises(ValueError, match="批次AI估算額度範圍無效"):
        server.create_text_meal_estimate_draft(
            user_id="U1",
            message_id=f"photo-add-batch:{token}:{draft['version']}:AI-SUBSET:0",
            request={"food_name": "已知", "amount": 20, "unit": "g", "meal_slot": ""},
            quota_batch_key=batch_key, quota_batch_owner=owner,
        )
    with sqlite3.connect(db) as conn:
        assert list(conn.iterdump()) == before
    assert calls == []


def test_batch_scope_column_upgrades_predecessor_before_write_and_is_idempotent(tmp_path):
    db = tmp_path / "predecessor.db"
    predecessor_ddl = """CREATE TABLE photo_ingredient_batch_quota_ops (
        batch_key TEXT PRIMARY KEY,user_id TEXT NOT NULL,parent_token TEXT NOT NULL,
        parent_version INTEGER NOT NULL,source_message_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,status TEXT NOT NULL,lease_owner TEXT NOT NULL DEFAULT '',
        lease_expires_at TEXT NOT NULL DEFAULT '',charge_attempt_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(user_id,parent_token,parent_version,source_message_id))"""
    with sqlite3.connect(db) as conn:
        conn.execute(predecessor_ddl)
        conn.execute(
            """INSERT INTO photo_ingredient_batch_quota_ops
               VALUES ('B','U','P',1,'M','H','failed','','','','T','T')"""
        )
        server.ensure_daily_food_ledger_schema(conn)
        conn.commit()
        assert "child_scope_json" in {
            row[1] for row in conn.execute("PRAGMA table_info(photo_ingredient_batch_quota_ops)")
        }
        assert conn.execute(
            "SELECT batch_key,child_scope_json,status FROM photo_ingredient_batch_quota_ops"
        ).fetchone() == ("B", "", "failed")
        first = list(conn.iterdump())
        server.ensure_daily_food_ledger_schema(conn)
        conn.commit()
        assert list(conn.iterdump()) == first


def test_legacy_claim_without_child_scope_fails_closed_refunds_once_and_requires_fresh_event(
    tmp_path, monkeypatch
):
    db, token, draft, _replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    parsed = server.parse_photo_ingredient_batch("木耳20g")
    with sqlite3.connect(db) as conn:
        batch_key, _owner = server._claim_photo_ingredient_batch_quota(
            conn, user_id="U1", token=token, expected_version=draft["version"],
            message_id="LEGACY-SCOPE", parsed_items=parsed,
        )
        conn.execute(
            "UPDATE photo_ingredient_batch_quota_ops SET child_scope_json='' WHERE batch_key=?",
            (batch_key,),
        )
        conn.commit()
    clock[0] += timedelta(seconds=server.TEXT_MEAL_ESTIMATE_LEASE_SECONDS + 1)

    for _ in range(2):
        with sqlite3.connect(db) as conn, pytest.raises(ValueError, match="重新送出"):
            server._claim_photo_ingredient_batch_quota(
                conn, user_id="U1", token=token, expected_version=draft["version"],
                message_id="LEGACY-SCOPE", parsed_items=parsed,
            )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 2
        assert conn.execute(
            "SELECT status FROM text_meal_estimate_quota_ledger"
        ).fetchall() == [("refunded",)]
        assert conn.execute(
            "SELECT status FROM photo_ingredient_batch_quota_ops WHERE batch_key=?", (batch_key,)
        ).fetchone() == ("failed",)


def test_manual_nutrition_pipe_is_zero_debit_and_zero_batch_claim(tmp_path, monkeypatch):
    db, _token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req) or _estimate(req))
    _enter_add(draft, replies, "REQUEST-MANUAL-ZERO")
    server.handle_message(_text("無糖豆漿｜1杯｜100｜9", "MANUAL-ZERO"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM photo_ingredient_batch_quota_ops").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM text_meal_estimate_quota_ledger").fetchone()[0] == 0
    assert calls == []


def test_crash_after_all_children_before_parent_event_recovers_without_reinference_or_double_debit(tmp_path, monkeypatch):
    db, token, draft, replies = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    calls = []
    monkeypatch.setattr(server, "estimate_text_meal_nutrition", lambda req: calls.append(req["food_name"]) or _estimate(req))
    _enter_add(draft, replies, "REQUEST-POST-CHILD-CRASH")
    real_apply = server.apply_meal_photo_action
    crashed = {"done": False}

    def crash_before_parent(*args, **kwargs):
        if not crashed["done"]:
            crashed["done"] = True
            raise KeyboardInterrupt("after children")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(server, "apply_meal_photo_action", crash_before_parent)
    event = _text("木耳20g、青菜20g", "POST-CHILD-CRASH")
    with pytest.raises(KeyboardInterrupt, match="after children"):
        server.handle_message(event)
    assert calls == ["木耳", "青菜"]

    clock[0] += timedelta(seconds=server.TEXT_MEAL_ESTIMATE_LEASE_SECONDS + 1)
    server.processed_messages.clear()
    server.handle_message(event)
    assert calls == ["木耳", "青菜"]
    with sqlite3.connect(db) as conn:
        assert get_meal_photo_draft(conn, user_id="U1", token=token)["status"] == "estimated"
        assert conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,COUNT(*) FROM text_meal_estimate_quota_ledger GROUP BY status ORDER BY status"
        ).fetchall() == [("charged", 1), ("refunded", 1)]
