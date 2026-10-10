import hashlib
import sqlite3
from datetime import timedelta

import pytest

import server
from tests.test_photo_batch_single_quota import _install_usage
from tests.test_photo_natural_ingredient_batch import _setup


def _database_digest(conn):
    return hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()


def _claim_legacy_scope(db, token, draft, *, message_id):
    parsed = server.parse_photo_ingredient_batch("木耳20g")
    with sqlite3.connect(db) as conn:
        batch_key, owner = server._claim_photo_ingredient_batch_quota(
            conn,
            user_id="U1",
            token=token,
            expected_version=draft["version"],
            message_id=message_id,
            parsed_items=parsed,
        )
        conn.execute(
            "UPDATE photo_ingredient_batch_quota_ops SET child_scope_json='' WHERE batch_key=?",
            (batch_key,),
        )
        conn.commit()
    return parsed, batch_key, owner


def test_live_predecessor_claim_is_not_refunded_or_retired(tmp_path, monkeypatch):
    db, token, draft, _ = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(
        server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA
    )
    now = server.tw_now()
    monkeypatch.setattr(server, "tw_now", lambda: now)
    parsed, batch_key, owner = _claim_legacy_scope(
        db, token, draft, message_id="LIVE-LEGACY"
    )
    with sqlite3.connect(db) as conn:
        before_digest = _database_digest(conn)
        before_ledger = conn.execute(
            "SELECT * FROM text_meal_estimate_quota_ledger ORDER BY attempt_id"
        ).fetchall()

    with sqlite3.connect(db) as conn, pytest.raises(ValueError, match="正在估算"):
        server._claim_photo_ingredient_batch_quota(
            conn,
            user_id="U1",
            token=token,
            expected_version=draft["version"],
            message_id="LIVE-LEGACY",
            parsed_items=parsed,
        )

    with sqlite3.connect(db) as conn:
        assert _database_digest(conn) == before_digest
        assert conn.execute(
            "SELECT status,lease_owner FROM photo_ingredient_batch_quota_ops WHERE batch_key=?",
            (batch_key,),
        ).fetchone() == ("processing", owner)
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U1'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT * FROM text_meal_estimate_quota_ledger ORDER BY attempt_id"
        ).fetchall() == before_ledger


def test_legacy_claim_with_missing_lease_fails_closed_without_refund(tmp_path, monkeypatch):
    db, token, draft, _ = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(
        server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA
    )
    parsed, batch_key, owner = _claim_legacy_scope(
        db, token, draft, message_id="NO-LEASE-LEGACY"
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE photo_ingredient_batch_quota_ops SET lease_expires_at='' WHERE batch_key=?",
            (batch_key,),
        )
        conn.commit()
        before_digest = _database_digest(conn)

    with sqlite3.connect(db) as conn, pytest.raises(ValueError, match="租約無效"):
        server._claim_photo_ingredient_batch_quota(
            conn,
            user_id="U1",
            token=token,
            expected_version=draft["version"],
            message_id="NO-LEASE-LEGACY",
            parsed_items=parsed,
        )

    with sqlite3.connect(db) as conn:
        assert _database_digest(conn) == before_digest
        assert conn.execute(
            "SELECT status,lease_owner FROM photo_ingredient_batch_quota_ops WHERE batch_key=?",
            (batch_key,),
        ).fetchone() == ("processing", owner)
        assert conn.execute(
            "SELECT remaining_chat_quota FROM usage WHERE user_id='U1'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT status FROM text_meal_estimate_quota_ledger"
        ).fetchall() == [("charged",)]


def test_expired_legacy_claim_never_refunds_another_users_attempt(tmp_path, monkeypatch):
    db, token, draft, _ = _setup(tmp_path, monkeypatch)
    _install_usage(db, quota=2)
    monkeypatch.setattr(
        server, "check_permission_and_quota", server._DEFAULT_CHECK_PERMISSION_AND_QUOTA
    )
    clock = [server.tw_now()]
    monkeypatch.setattr(server, "tw_now", lambda: clock[0])
    parsed, batch_key, _owner = _claim_legacy_scope(
        db, token, draft, message_id="OTHER-OWNER-LEGACY"
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage VALUES ('U2',1,10,?,'active','',2)",
            (server.tw_today().isoformat(),),
        )
        conn.execute(
            """INSERT INTO text_meal_estimate_quota_ledger
               (attempt_id,token,user_id,status,created_at,updated_at)
               VALUES ('OTHER-ATTEMPT','OTHER-TOKEN','U2','charged','T','T')"""
        )
        conn.execute(
            "UPDATE photo_ingredient_batch_quota_ops SET charge_attempt_id='OTHER-ATTEMPT' WHERE batch_key=?",
            (batch_key,),
        )
        conn.commit()
    clock[0] += timedelta(seconds=server.TEXT_MEAL_ESTIMATE_LEASE_SECONDS + 1)

    with sqlite3.connect(db) as conn, pytest.raises(ValueError, match="重新送出"):
        server._claim_photo_ingredient_batch_quota(
            conn,
            user_id="U1",
            token=token,
            expected_version=draft["version"],
            message_id="OTHER-OWNER-LEGACY",
            parsed_items=parsed,
        )

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM photo_ingredient_batch_quota_ops WHERE batch_key=?",
            (batch_key,),
        ).fetchone() == ("failed",)
        assert conn.execute(
            "SELECT user_id,status FROM text_meal_estimate_quota_ledger ORDER BY user_id"
        ).fetchall() == [("U1", "charged"), ("U2", "charged")]
        assert conn.execute(
            "SELECT user_id,remaining_chat_quota FROM usage ORDER BY user_id"
        ).fetchall() == [("U1", 1), ("U2", 1)]
