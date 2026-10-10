from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import text_meal_dependency_recovery as subject

PARENT_ROOT_TEXT = os.environ.get("TEXT_MEAL_PARENT_ROOT", "").strip()
if not PARENT_ROOT_TEXT:
    pytest.skip(
        "set TEXT_MEAL_PARENT_ROOT to the parent checkout for shared-quota integration",
        allow_module_level=True,
    )
ROOT = Path(PARENT_ROOT_TEXT).resolve()
if not (ROOT / "shared_ai_quota.py").is_file():
    pytest.skip("TEXT_MEAL_PARENT_ROOT has no shared_ai_quota.py", allow_module_level=True)


def _load_shared_quota():
    spec = importlib.util.spec_from_file_location("recovery_shared_ai_quota", ROOT / "shared_ai_quota.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


SHARED_QUOTA = _load_shared_quota()


def test_binder_rejects_missing_provider_before_any_database_or_quota_work(tmp_path):
    missing_db = tmp_path / "must-not-exist.sqlite3"
    with pytest.raises(ValueError, match="provider"):
        subject.install_host(subject.HostDependencies(
            db_path=str(missing_db),
            ensure_schema=_ensure_host_schema,
            now=lambda: datetime(2026, 10, 5, tzinfo=ZoneInfo("Asia/Taipei")),
            current_meal_slot=lambda: "午餐",
            provider_client=None,
            charge_quota=SHARED_QUOTA._charge_text_meal_estimate_quota,
        ))
    assert not missing_db.exists()


def _ensure_host_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage (
            user_id TEXT PRIMARY KEY,
            remaining_chat_quota INTEGER,
            remaining_meals INTEGER,
            last_date TEXT,
            status TEXT,
            expiry_date TEXT,
            daily_chat_limit INTEGER
        );
        CREATE TABLE IF NOT EXISTS pending_text_meal_estimates (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            request_json TEXT NOT NULL,
            estimate_json TEXT NOT NULL DEFAULT '{}',
            portion_multiplier REAL NOT NULL DEFAULT 1,
            meal_slot TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            confirmed_log_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            UNIQUE(user_id, source_message_id)
        );
        CREATE TABLE IF NOT EXISTS photo_ingredient_batch_quota_ops (
            batch_key TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            parent_token TEXT NOT NULL,
            parent_version INTEGER NOT NULL,
            source_message_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            child_scope_json TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            charge_attempt_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        message = SimpleNamespace(content=json.dumps(outcome, ensure_ascii=False), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _payload(name="木耳"):
    return {
        "food_name": name,
        "portion_assumption": "20 g",
        "calories_kcal": {"estimate": 5, "min": 3, "max": 8, "unit": "kcal"},
        "protein_g": {"estimate": 0.3, "min": 0.1, "max": 0.6, "unit": "g"},
    }


def _request(name="木耳"):
    return {"food_name": name, "amount": 20, "unit": "g", "meal_slot": "午餐"}


@pytest.fixture
def runtime(tmp_path):
    db = tmp_path / "real.sqlite3"
    clock = [datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))]
    completions = FakeCompletions([_payload(), _payload(), _payload()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    subject.install_host(subject.HostDependencies(
        db_path=str(db),
        ensure_schema=_ensure_host_schema,
        now=lambda: clock[0],
        current_meal_slot=lambda: "午餐",
        provider_client=client,
        charge_quota=SHARED_QUOTA._charge_text_meal_estimate_quota,
        refund_quota=SHARED_QUOTA.refund_owned_chat_attempt,
    ))
    subject.ensure_text_meal_runtime_schema()
    with sqlite3.connect(db) as conn:
        _ensure_host_schema(conn)
        conn.execute(
            "INSERT INTO usage VALUES (?,?,?,?,?,?,?)",
            ("U1", 3, 10, "2026-10-05", "vip", "2099-12-31", 3),
        )
        conn.commit()
    return SimpleNamespace(db=db, clock=clock, completions=completions, client=client)


def _quota(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT remaining_chat_quota FROM usage WHERE user_id='U1'").fetchone()[0]


def test_success_uses_actual_provider_shape_real_sqlite_and_one_shared_charge(runtime):
    draft = subject.create_text_meal_estimate_draft(
        user_id="U1", message_id="M1", request=_request()
    )

    assert draft["status"] == "pending"
    assert draft["estimate"]["calories_kcal"] == {"estimate": 5.0, "min": 3.0, "max": 8.0}
    assert _quota(runtime.db) == 2
    assert len(runtime.completions.calls) == 1
    call = runtime.completions.calls[0]
    assert call["response_format"]["json_schema"]["strict"] is True
    with sqlite3.connect(runtime.db) as conn:
        assert conn.execute(
            "SELECT state,quota_attempt_id FROM text_meal_provider_attempts"
        ).fetchone()[0] == "completed"
        assert conn.execute(
            "SELECT status FROM text_meal_estimate_quota_ledger"
        ).fetchone() == ("charged",)


def test_redelivery_replays_draft_without_second_provider_or_charge(runtime):
    first = subject.create_text_meal_estimate_draft(user_id="U1", message_id="M1", request=_request())
    second = subject.create_text_meal_estimate_draft(user_id="U1", message_id="M1", request=_request())

    assert second == first
    assert len(runtime.completions.calls) == 1
    assert _quota(runtime.db) == 2


@pytest.mark.parametrize("bad_request", [
    {"food_name": "", "amount": 20, "unit": "g", "meal_slot": "午餐"},
    {"food_name": "木耳", "amount": float("nan"), "unit": "g", "meal_slot": "午餐"},
    {"food_name": "木耳", "amount": -1, "unit": "g", "meal_slot": "午餐"},
    {"food_name": "木耳", "amount": 20, "unit": "", "meal_slot": "午餐"},
])
def test_input_validation_is_pre_ai_and_pre_charge(runtime, bad_request):
    with pytest.raises(ValueError, match="估算請求"):
        subject.create_text_meal_estimate_draft(user_id="U1", message_id="BAD", request=bad_request)

    assert runtime.completions.calls == []
    assert _quota(runtime.db) == 3
    with sqlite3.connect(runtime.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_text_meal_estimates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM text_meal_provider_attempts").fetchone()[0] == 0


def test_provider_error_becomes_unknown_without_refund_and_never_auto_retries(runtime):
    runtime.completions.outcomes[:] = [TimeoutError("socket outcome unknown"), _payload("不得呼叫")]

    with pytest.raises(subject.TextMealProviderError, match="結果未知") as caught:
        subject.create_text_meal_estimate_draft(user_id="U1", message_id="UNKNOWN", request=_request())
    assert caught.value.quota_refunded is False
    assert _quota(runtime.db) == 2

    with pytest.raises(subject.TextMealProviderError, match="結果未知"):
        subject.create_text_meal_estimate_draft(user_id="U1", message_id="UNKNOWN", request=_request())
    assert len(runtime.completions.calls) == 1
    assert _quota(runtime.db) == 2
    with sqlite3.connect(runtime.db) as conn:
        assert conn.execute("SELECT status FROM pending_text_meal_estimates").fetchone() == ("provider_unknown",)
        assert conn.execute("SELECT state FROM text_meal_provider_attempts").fetchone() == ("unknown",)
        assert conn.execute("SELECT status FROM text_meal_estimate_quota_ledger").fetchone() == ("charged",)


def test_expired_started_attempt_recovers_to_unknown_without_refund_or_blind_retry(runtime):
    subject.ensure_text_meal_runtime_schema()
    old = (runtime.clock[0] - timedelta(minutes=5)).isoformat(timespec="seconds")
    expired = (runtime.clock[0] - timedelta(minutes=3)).isoformat(timespec="seconds")
    request_json = json.dumps(_request(), ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(runtime.db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO pending_text_meal_estimates
               VALUES (?,?,?,?,?,1,?,'estimating',1,'',?,?,?,?,?)""",
            ("T-OLD", "U1", "M-OLD", request_json, "{}", "午餐", old, old,
             (runtime.clock[0] + timedelta(minutes=20)).isoformat(timespec="seconds"),
             "A-OLD", expired),
        )
        assert SHARED_QUOTA._charge_text_meal_estimate_quota(
            conn, user_id="U1", token="T-OLD", attempt_id="A-OLD", now_text=old
        )
        conn.execute(
            """INSERT INTO text_meal_provider_attempts
               (attempt_id,token,user_id,quota_attempt_id,state,provider_started_at,completed_at,error_kind)
               VALUES (?,?,?,?,'provider_started',?,'','')""",
            ("A-OLD", "T-OLD", "U1", "A-OLD", old),
        )
        conn.commit()

    with pytest.raises(subject.TextMealProviderError, match="結果未知"):
        subject.create_text_meal_estimate_draft(user_id="U1", message_id="M-OLD", request=_request())

    assert runtime.completions.calls == []
    assert _quota(runtime.db) == 2
    with sqlite3.connect(runtime.db) as conn:
        assert conn.execute("SELECT status FROM pending_text_meal_estimates").fetchone() == ("provider_unknown",)
        assert conn.execute("SELECT state FROM text_meal_provider_attempts").fetchone() == ("unknown",)


def test_photo_child_uses_parent_charge_attempt_and_does_not_spend_again(runtime):
    canonical_request = {
        "food_name": "木耳", "amount": "20", "unit": "g", "meal_slot": "午餐"
    }
    scope = {
        "schema_version": "photo-ingredient-child-scope-v1",
        "request_hash": "HASH",
        "items": [{"index": 0, "ai_allowed": True, "request": canonical_request}],
    }
    lease = (runtime.clock[0] + timedelta(minutes=2)).isoformat(timespec="seconds")
    now = runtime.clock[0].isoformat(timespec="seconds")
    with sqlite3.connect(runtime.db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        assert SHARED_QUOTA._charge_text_meal_estimate_quota(
            conn,user_id='U1',token='PHOTO',attempt_id='PARENT-ATTEMPT',now_text=now,
        )
        conn.execute(
            """INSERT INTO photo_ingredient_batch_quota_ops VALUES
               (?,?,?,?,?,?,?,'processing',?,?,?,?,?)""",
            ("BATCH", "U1", "PHOTO", 7, "PARENT-MSG", "HASH",
             json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             "PARENT-ATTEMPT", lease, "PARENT-ATTEMPT", now, now),
        )
        conn.commit()

    draft = subject.create_text_meal_estimate_draft(
        user_id="U1",
        message_id="photo-add-batch:PHOTO:7:PARENT-MSG:0",
        request=_request(),
        quota_batch_key="BATCH",
        quota_batch_owner="PARENT-ATTEMPT",
    )

    assert draft["status"] == "pending"
    assert _quota(runtime.db) == 2
    with sqlite3.connect(runtime.db) as conn:
        assert conn.execute(
            "SELECT quota_attempt_id FROM text_meal_provider_attempts"
        ).fetchone() == ("PARENT-ATTEMPT",)
        assert conn.execute(
            "SELECT attempt_id,token,user_id,status FROM text_meal_estimate_quota_ledger"
        ).fetchall() == [('PARENT-ATTEMPT','PHOTO','U1','charged')]


def test_photo_child_scope_rejects_unapproved_child_before_provider(runtime):
    canonical_request = {
        "food_name": "木耳", "amount": "20", "unit": "g", "meal_slot": "午餐"
    }
    scope = {
        "schema_version": "photo-ingredient-child-scope-v1",
        "request_hash": "HASH",
        "items": [{"index": 0, "ai_allowed": False, "request": canonical_request}],
    }
    lease = (runtime.clock[0] + timedelta(minutes=2)).isoformat(timespec="seconds")
    now = runtime.clock[0].isoformat(timespec="seconds")
    with sqlite3.connect(runtime.db) as conn:
        conn.execute(
            "INSERT INTO photo_ingredient_batch_quota_ops VALUES (?,?,?,?,?,?,?,'processing',?,?,?,?,?)",
            ("BATCH", "U1", "PHOTO", 7, "PARENT-MSG", "HASH", json.dumps(scope),
             "PARENT-ATTEMPT", lease, "PARENT-ATTEMPT", now, now),
        )
        conn.commit()

    with pytest.raises(ValueError, match="批次AI估算額度範圍無效"):
        subject.create_text_meal_estimate_draft(
            user_id="U1", message_id="photo-add-batch:PHOTO:7:PARENT-MSG:0",
            request=_request(), quota_batch_key="BATCH", quota_batch_owner="PARENT-ATTEMPT",
        )
    assert runtime.completions.calls == []
    assert _quota(runtime.db) == 3
