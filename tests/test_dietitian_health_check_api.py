from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


CHANNEL_ID = "2009251085"
LIFF_ID = CHANNEL_ID + "-dietitianCheck"
DIETITIAN_UID = "U1234567890abcdef1234567890abcdef"
OTHER_UID = "Uabcdef1234567890abcdef1234567890"
CUSTOMER_UID = "U11111111111111111111111111111111"
NOW = 1_789_056_000
SOURCE_ROWS = (
    ("log-owned", "2026-09-02", "lunch"),
    ("log-02-breakfast", "2026-09-02", "breakfast"),
    ("log-03-breakfast", "2026-09-03", "breakfast"),
    ("log-03-lunch", "2026-09-03", "lunch"),
    ("log-04-breakfast", "2026-09-04", "breakfast"),
    ("log-04-lunch", "2026-09-04", "lunch"),
)
SOURCE_HASHES = {
    log_id: hashlib.sha256(
        f'{log_id}:3:{{"calories_kcal":500}}'.encode()
    ).hexdigest()
    for log_id, _local_date, _meal_slot in SOURCE_ROWS
}
SOURCE_HASH = SOURCE_HASHES["log-owned"]
VALID_DAY_ROWS = (
    ("2026-09-02", "draft-confirmed-meals-v1", 2, "qualified"),
    ("2026-09-03", "draft-confirmed-meals-v1", 2, "qualified"),
    ("2026-09-04", "draft-confirmed-meals-v1", 2, "qualified"),
)


def _manifest_for_source_hash(
    source_hash: str, day_rows=VALID_DAY_ROWS
) -> str:
    hashes = {**SOURCE_HASHES, "log-owned": source_hash}
    return hashlib.sha256(
        "\n".join(
            [f"{log_id}:3:{hashes[log_id]}" for log_id in sorted(hashes)]
            + [
                f"day:{day}:{count}:{rule}"
                for day, rule, count, _status in day_rows
            ]
        ).encode()
    ).hexdigest()


def _v2_source_hash(log_id: str, local_date: str, meal_slot: str) -> str:
    payload = {
        "schema_version": "vip_health_check_source_v2",
        "food_log_id": log_id,
        "food_log_version": 3,
        "nutrition_snapshot_json": '{"calories_kcal":500}',
        "local_date": local_date,
        "normalized_meal_slot": meal_slot.strip() or "unspecified",
        "trust_binding": "",
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _upgrade_fixture_to_v2(conn: sqlite3.Connection) -> None:
    hashes = {
        log_id: _v2_source_hash(log_id, local_date, meal_slot)
        for log_id, local_date, meal_slot in SOURCE_ROWS
    }
    for log_id, source_hash in hashes.items():
        conn.execute(
            "UPDATE vip_health_check_source_refs SET source_hash=? WHERE food_log_id=?",
            (source_hash, log_id),
        )
    manifest = hashlib.sha256(
        "\n".join(
            [f"{log_id}:3:{hashes[log_id]}" for log_id in sorted(hashes)]
            + [
                f"day:{day}:{count}:{rule}"
                for day, rule, count, _status in VALID_DAY_ROWS
            ]
        ).encode()
    ).hexdigest()
    conn.execute("UPDATE vip_health_check_cases SET source_manifest_hash=?", (manifest,))
    conn.execute("UPDATE vip_health_check_reviews SET source_manifest_hash=?", (manifest,))


MANIFEST_HASH = _manifest_for_source_hash(SOURCE_HASH)
ALL_CASE_STATUSES = (
    "collecting",
    "ready_for_review",
    "needs_more_info",
    "approved_pending_delivery",
    "delivery_failed",
    "delivered",
    "expired",
    "cancelled",
)


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE health_profile (
            user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL,
            goal TEXT, restrictions TEXT, active_days TEXT
        );
        CREATE TABLE vip_health_check_cases (
            case_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, benefit_key TEXT NOT NULL,
            first_vip_activation_id TEXT NOT NULL, activation_event_key TEXT NOT NULL,
            window_started_at TEXT NOT NULL, window_ends_at TEXT NOT NULL,
            status TEXT NOT NULL, valid_day_count INTEGER NOT NULL,
            source_manifest_hash TEXT NOT NULL, submitted_at TEXT NOT NULL,
            report_published_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE vip_health_check_valid_days (
            case_id TEXT NOT NULL, local_date TEXT NOT NULL, rule_version TEXT NOT NULL,
            qualifying_meal_count INTEGER NOT NULL, completeness_status TEXT NOT NULL,
            evaluated_at TEXT NOT NULL
        );
        CREATE TABLE vip_health_check_source_refs (
            case_id TEXT NOT NULL, food_log_id TEXT NOT NULL, food_log_version INTEGER NOT NULL,
            local_date TEXT NOT NULL, included_reason TEXT NOT NULL, source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE vip_health_check_reviews (
            review_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, review_version INTEGER NOT NULL,
            status TEXT NOT NULL, ai_observations_json TEXT NOT NULL, review_json TEXT NOT NULL,
            suggested_values_json TEXT NOT NULL, limitations TEXT NOT NULL,
            source_manifest_hash TEXT NOT NULL, approved_by TEXT NOT NULL,
            approved_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE vip_health_check_reports (
            report_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, review_id TEXT NOT NULL UNIQUE,
            report_kind TEXT NOT NULL DEFAULT 'baseline_3day', report_version INTEGER NOT NULL,
            report_json TEXT NOT NULL, source_manifest_hash TEXT NOT NULL,
            published_by TEXT NOT NULL, published_at TEXT NOT NULL,
            UNIQUE(case_id,report_kind,report_version),
            FOREIGN KEY(case_id) REFERENCES vip_health_check_cases(case_id),
            FOREIGN KEY(review_id) REFERENCES vip_health_check_reviews(review_id)
        );
        CREATE TABLE vip_health_check_deliveries (
            delivery_id TEXT PRIMARY KEY, report_id TEXT NOT NULL, user_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, delivered_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(report_id) REFERENCES vip_health_check_reports(report_id)
        );
        CREATE TABLE vip_health_check_audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
            actor_type TEXT NOT NULL, actor_id TEXT NOT NULL DEFAULT '',
            from_status TEXT NOT NULL DEFAULT '', to_status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE food_catalog (food_id TEXT PRIMARY KEY, product_name TEXT NOT NULL);
        CREATE TABLE food_logs (
            log_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, food_id TEXT NOT NULL,
            consumed_at TEXT NOT NULL, meal_slot TEXT, consumed_servings REAL,
            consumed_amount REAL, consumed_unit TEXT, nutrition_snapshot_json TEXT NOT NULL,
            exchange_snapshot_json TEXT NOT NULL, approved_exchange_json TEXT NOT NULL,
            source_image_ref TEXT, confirmation_status TEXT NOT NULL, deleted_at TEXT NOT NULL,
            version INTEGER NOT NULL
        );
        """
    )


def _insert_case(
    conn: sqlite3.Connection,
    case_id: str = "case-1",
    user_id: str = CUSTOMER_UID,
    status: str = "ready_for_review",
) -> None:
    conn.execute(
        f"""INSERT INTO vip_health_check_cases VALUES
           (?,?, 'first_vip_baseline_check','activation-secret','event-secret',
            '2026-09-01T00:00:00+08:00','2026-09-08T00:00:00+08:00',?,3,
            '{MANIFEST_HASH}','2026-09-04T00:00:00+08:00','',
            '2026-09-01T00:00:00+08:00','2026-09-04T00:00:00+08:00')""",
        (case_id, user_id, status),
    )


def _populated_db(tmp_path):
    path = tmp_path / "health.db"
    with sqlite3.connect(path) as conn:
        _schema(conn)
        _insert_case(conn)
        conn.execute(
            "INSERT INTO health_profile VALUES (?,?,?,?,?,?,?)",
            (CUSTOMER_UID, "王小明", 2100, 100.5, "減脂", "花生", "一,三,五"),
        )
        for local_date, rule, meal_count, completeness in VALID_DAY_ROWS:
            conn.execute(
                "INSERT INTO vip_health_check_valid_days VALUES (?,?,?,?,?,?)",
                (
                    "case-1", local_date, rule, meal_count, completeness,
                    "2026-09-05T00:00:00+08:00",
                ),
            )
        conn.execute("INSERT INTO food_catalog VALUES ('food-1','雞胸便當')")
        for log_id, local_date, meal_slot in SOURCE_ROWS:
            conn.execute(
                """INSERT INTO food_logs VALUES
                   (?,?,'food-1',?,?,1,300,'g',
                    '{"calories_kcal":500}','{}','{}','private-image',
                    'confirmed','',3)""",
                (log_id, CUSTOMER_UID, f"{local_date}T12:00:00+08:00", meal_slot),
            )
            conn.execute(
                "INSERT INTO vip_health_check_source_refs VALUES (?,?,?,?,?,?,?)",
                (
                    "case-1", log_id, 3, local_date, "qualifying",
                    SOURCE_HASHES[log_id], "2026-09-05",
                ),
            )
        for version, status in ((1, "superseded"), (2, "draft")):
            conn.execute(
                """INSERT INTO vip_health_check_reviews VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"review-{version}", "case-1", version, status,
                    json.dumps({"observation": f"observation-{version}"}),
                    json.dumps({"good": f"v{version}"}),
                    json.dumps({"protein": 100}), "資料有限", MANIFEST_HASH,
                    "U99999999999999999999999999999999", "",
                    "2026-09-04T00:00:00+08:00", "2026-09-04T00:00:00+08:00",
                ),
            )
    return path


def _client(
    *, loader, verifier=None, allowed=(DIETITIAN_UID,), draft_saver=None,
    approval_saver=None, detail_loader=None,
):
    from dietitian_health_check_api import (
        DietitianHealthCheckConfig,
        attach_dietitian_health_check_routes,
    )

    seen = []

    def recording_loader(*args, **kwargs):
        seen.append((args, kwargs))
        return loader(*args, **kwargs)

    app = FastAPI()
    assert attach_dietitian_health_check_routes(
        app,
        config=DietitianHealthCheckConfig(
            enabled=True,
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            allowed_uids=frozenset(allowed),
        ),
        list_loader=lambda **kwargs: recording_loader("list", **kwargs),
        detail_loader=(
            detail_loader
            if detail_loader is not None
            else lambda case_id: recording_loader("detail", case_id=case_id)
        ),
        draft_saver=draft_saver,
        approval_saver=approval_saver,
        token_verifier=verifier or (lambda _token, *, channel_id: DIETITIAN_UID),
    ) is True
    return TestClient(app, raise_server_exceptions=False), seen


def test_authorized_http_draft_save_reloads_exact_four_field_snapshot(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        source_token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, draft_saver=saver
    )
    fields = {
        "good": "早餐有穩定記錄",
        "priority": "先改善蔬菜份量",
        "next_7_days": "每天午餐加一份蔬菜",
        "comment": "先做一件可持續的事",
    }
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"},
        json={
            **fields,
            "expected_source_token": source_token,
            "expected_review_version": 2,
            "request_id": "save-case-1-001",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"
    assert response.json()["review_version"] == 3
    with sqlite3.connect(path) as conn:
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail["latest_review_fresh"] is True
    assert detail["current_review_version"] == 3
    assert detail["latest_review"]["review"] == fields


def test_authorized_http_approval_locks_saved_review_and_get_shows_pending_delivery(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_approval import create_health_check_approval_saver
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    draft_saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        source_token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    fields = {
        "good": "早餐有穩定記錄",
        "priority": "先改善蔬菜份量",
        "next_7_days": "每天午餐加一份蔬菜",
        "comment": "先做一件可持續的事",
    }
    draft = draft_saver("case-1", fields, source_token, 2, "draft-before-approve", DIETITIAN_UID)
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        detail_loader=lambda case_id: load_health_check_detail(
            sqlite3.connect(path), case_id=case_id
        ),
        approval_saver=create_health_check_approval_saver(path),
    )
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers={"Authorization": "Bearer signed"},
        json={
            "expected_source_token": source_token,
            "expected_review_version": draft["review_version"],
            "request_id": "approve-case-1-001",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["delivery_status"] == "pending"
    detail = client.get(
        "/api/dietitian/health-checks/case-1",
        headers={"Authorization": "Bearer signed"},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "approved_pending_delivery"
    assert detail.json()["latest_review"]["status"] == "approved"
    assert detail.json()["latest_review"]["review"] == fields
    assert detail.json()["approval"]["delivery_status"] == "pending"
    with sqlite3.connect(path) as conn:
        stored = conn.execute(
            "SELECT report_json FROM vip_health_check_reports WHERE review_id=?",
            (draft["review_id"],),
        ).fetchone()[0]
        assert json.loads(stored) == {
            "good": fields["good"], "priority": fields["priority"],
            "next_7_days": fields["next_7_days"], "limitations": fields["comment"],
        }
        assert conn.execute(
            "SELECT status,attempts FROM vip_health_check_deliveries"
        ).fetchone() == ("pending", 0)


def _produce_approved_health_check(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_approval import create_health_check_approval_saver
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    draft_saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        detail = load_health_check_detail(conn, case_id="case-1")
        assert detail is not None
        source_token = str(detail["source_token"])
    draft = draft_saver(
        "case-1",
        {"good": "好", "priority": "優先", "next_7_days": "行動", "comment": "限制"},
        source_token,
        2,
        "projection-draft",
        DIETITIAN_UID,
    )
    approval = create_health_check_approval_saver(path)(
        "case-1",
        source_token,
        draft["review_version"],
        "projection-approval",
        DIETITIAN_UID,
    )
    return path, draft, approval


@pytest.mark.parametrize(
    ("succeeded", "expected_case_status", "expected_delivery_status"),
    (
        (None, "approved_pending_delivery", "pending"),
        (False, "delivery_failed", "failed"),
        (True, "delivered", "delivered"),
    ),
)
def test_approval_get_projection_accepts_canonical_lifecycle_pairs(
    tmp_path, succeeded, expected_case_status, expected_delivery_status
):
    from dietitian_health_check_api import load_health_check_detail
    from vip_health_check import record_health_check_delivery_attempt

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    if succeeded is not None:
        with sqlite3.connect(path) as conn:
            record_health_check_delivery_attempt(
                conn,
                delivery_key=approval["delivery_key"],
                succeeded=succeeded,
                error="temporary failure" if not succeeded else "",
                attempted_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            )
            conn.commit()
    with sqlite3.connect(path) as conn:
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail is not None
    assert detail["status"] == expected_case_status
    assert detail["approval"] == {
        "report_id": approval["report_id"],
        "status": "approved",
        "delivery_status": expected_delivery_status,
    }


@pytest.mark.parametrize(
    ("tamper_sql", "parameters"),
    (
        ("UPDATE vip_health_check_deliveries SET user_id=?", (OTHER_UID,)),
        ("UPDATE vip_health_check_deliveries SET status=?", ("unknown",)),
        ("UPDATE vip_health_check_cases SET status=? WHERE case_id='case-1'", ("delivered",)),
        ("DELETE FROM vip_health_check_deliveries", ()),
        ("UPDATE vip_health_check_deliveries SET report_id=?", ("missing-report",)),
        ("UPDATE vip_health_check_reports SET case_id=?", ("other-case",)),
        ("UPDATE vip_health_check_reports SET review_id=?", ("missing-review",)),
        ("UPDATE vip_health_check_reports SET report_kind=?", ("other-kind",)),
    ),
    ids=(
        "wrong-recipient",
        "unknown-delivery-status",
        "incoherent-case-status",
        "missing-delivery",
        "delivery-report-mapping",
        "report-case-mapping",
        "report-review-mapping",
        "wrong-report-kind",
    ),
)
def test_approval_get_projection_fails_closed_for_tampered_binding_or_state(
    tmp_path, tamper_sql, parameters
):
    from dietitian_health_check_api import load_health_check_detail

    path, _draft, _approval = _produce_approved_health_check(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(tamper_sql, parameters)
        conn.commit()
        with pytest.raises(ValueError, match="invalid approved review projection"):
            load_health_check_detail(conn, case_id="case-1")


def test_approval_get_projection_wrong_recipient_cannot_present_delivered(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from vip_health_check import record_health_check_delivery_attempt

    path, _draft, approval = _produce_approved_health_check(tmp_path)
    with sqlite3.connect(path) as conn:
        record_health_check_delivery_attempt(
            conn,
            delivery_key=str(approval["delivery_key"]),
            succeeded=True,
            error="",
            attempted_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        )
        conn.execute("UPDATE vip_health_check_deliveries SET user_id=?", (OTHER_UID,))
        conn.commit()
        with pytest.raises(ValueError, match="invalid approved review projection"):
            load_health_check_detail(conn, case_id="case-1")


def test_approval_get_projection_never_presents_approved_report_as_draft(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path, draft, _approval = _produce_approved_health_check(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE vip_health_check_reviews SET status='draft' WHERE review_id=?",
            (draft["review_id"],),
        )
        conn.commit()
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail is not None
    assert detail["latest_review"]["status"] == "draft"
    assert detail["approval"] is None


def test_http_approval_stale_source_or_review_is_409_with_zero_writes(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_approval import create_health_check_approval_saver
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    draft_saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        source_token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    draft = draft_saver(
        "case-1",
        {"good": "好", "priority": "優先", "next_7_days": "行動", "comment": "點評"},
        source_token, 2, "draft-for-stale-approve", DIETITIAN_UID,
    )
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        approval_saver=create_health_check_approval_saver(path),
    )
    with sqlite3.connect(path) as conn:
        before = "\n".join(conn.iterdump())
    for suffix, token, version in (
        ("source", "f" * 64, draft["review_version"]),
        ("review", source_token, draft["review_version"] - 1),
    ):
        response = client.post(
            "/api/dietitian/health-checks/case-1/reviews/approve",
            headers={"Authorization": "Bearer signed"},
            json={
                "expected_source_token": token,
                "expected_review_version": version,
                "request_id": f"stale-{suffix}",
            },
        )
        assert response.status_code == 409
        with sqlite3.connect(path) as conn:
            assert "\n".join(conn.iterdump()) == before


def test_http_approval_exact_retry_replays_and_cross_actor_request_id_reuse_conflicts(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_approval import create_health_check_approval_saver
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    draft_saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    draft = draft_saver(
        "case-1",
        {"good": "好", "priority": "優先", "next_7_days": "行動", "comment": "點評"},
        token, 2, "draft-for-retry", DIETITIAN_UID,
    )

    def verify(raw_token, *, channel_id):
        assert channel_id == CHANNEL_ID
        return DIETITIAN_UID if raw_token == "actor-a" else OTHER_UID

    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, verifier=verify,
        allowed=(DIETITIAN_UID, OTHER_UID),
        approval_saver=create_health_check_approval_saver(path),
    )
    payload = {
        "expected_source_token": token,
        "expected_review_version": draft["review_version"],
        "request_id": "approval-retry-001",
    }
    first = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers={"Authorization": "Bearer actor-a"}, json=payload,
    )
    replay = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers={"Authorization": "Bearer actor-a"}, json=payload,
    )
    sibling = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers={"Authorization": "Bearer actor-b"}, json=payload,
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json() == {**first.json(), "created": False}
    assert sibling.status_code == 409
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM vip_health_check_deliveries").fetchone()[0] == 1
        assert conn.execute(
            "SELECT actor_id,status FROM vip_health_check_approval_operations"
        ).fetchone() == (DIETITIAN_UID, "completed")


def test_approval_authorization_and_body_validation_deny_before_writer():
    calls = []
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        approval_saver=lambda *args: calls.append(args),
    )
    valid = {
        "expected_source_token": "a" * 64,
        "expected_review_version": 3,
        "request_id": "approve-valid",
    }
    denied = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve", json=valid
    )
    invalid = client.post(
        "/api/dietitian/health-checks/case-1/reviews/approve",
        headers={"Authorization": "Bearer signed"},
        json={**valid, "review": {"good": "client claim"}},
    )
    assert denied.status_code == 401
    assert invalid.status_code == 422
    assert calls == []


def test_approved_case_rejects_further_draft_changes(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_approval import create_health_check_approval_saver
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    draft_saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    fields = {"good": "好", "priority": "優先", "next_7_days": "行動", "comment": "點評"}
    draft = draft_saver("case-1", fields, token, 2, "draft-before-lock", DIETITIAN_UID)
    create_health_check_approval_saver(path)(
        "case-1", token, draft["review_version"], "approval-lock", DIETITIAN_UID
    )
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, draft_saver=draft_saver
    )
    changed = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"},
        json={
            "good": "試圖改寫", "priority": "試圖改寫",
            "next_7_days": "試圖改寫", "comment": "試圖改寫",
            "expected_source_token": token,
            "expected_review_version": draft["review_version"],
            "request_id": "draft-after-approval",
        },
    )
    assert changed.status_code == 409
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT status,review_json FROM vip_health_check_reviews WHERE case_id='case-1' "
            "ORDER BY review_version"
        ).fetchall()
    assert len(rows) == 3
    assert rows[-1] == ("approved", json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def test_exact_http_draft_retry_returns_same_version_without_extra_row(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        source_token = load_health_check_detail(conn, case_id="case-1")["source_token"]
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        draft_saver=saver,
    )
    payload = {
        "good": "做得好", "priority": "優先事項",
        "next_7_days": "七天行動", "comment": "個人點評",
        "expected_source_token": source_token,
        "expected_review_version": 2, "request_id": "retry-001",
    }
    first = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"}, json=payload,
    )
    replay = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"}, json=payload,
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json()["review_id"] == first.json()["review_id"]
    assert replay.json()["review_version"] == first.json()["review_version"]
    assert replay.json()["created"] is False
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_reviews WHERE case_id='case-1'"
        ).fetchone()[0] == 3


def test_detail_projects_opaque_source_token_and_post_requires_that_observation(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        detail = load_health_check_detail(conn, case_id="case-1")
    assert isinstance(detail["source_token"], str)
    assert len(detail["source_token"]) == 64
    assert detail["source_token"] != detail["updated_at"]
    assert detail["source_token"] != MANIFEST_HASH

    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, draft_saver=saver
    )
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"},
        json={
            **{key: f"token-{key}" for key in ("good", "priority", "next_7_days", "comment")},
            "expected_source_token": detail["source_token"],
            "expected_review_version": 2,
            "request_id": "source-token-001",
        },
    )
    assert response.status_code == 200, response.text


def test_request_id_is_durably_bound_to_verified_actor_and_payload(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        token = load_health_check_detail(conn, case_id="case-1")["source_token"]

    def verify(raw_token, *, channel_id):
        assert channel_id == CHANNEL_ID
        return DIETITIAN_UID if raw_token == "actor-a" else OTHER_UID

    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, verifier=verify,
        allowed=(DIETITIAN_UID, OTHER_UID), draft_saver=saver,
    )
    payload = {
        **{key: f"actor-a-{key}" for key in ("good", "priority", "next_7_days", "comment")},
        "expected_source_token": token,
        "expected_review_version": 2,
        "request_id": "shared-request-id",
    }
    first = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer actor-a"}, json=payload,
    )
    assert first.status_code == 200, first.text
    before = sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM vip_health_check_reviews WHERE case_id='case-1'"
    ).fetchone()[0]

    sibling = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer actor-b"},
        json={**payload, "comment": "actor B changed payload", "expected_review_version": 3},
    )
    assert sibling.status_code == 409
    exact = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer actor-a"}, json=payload,
    )
    assert exact.status_code == 200
    assert exact.json() == {**first.json(), "created": False}
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM vip_health_check_reviews WHERE case_id='case-1'"
        ).fetchone()[0] == before
        operation = conn.execute(
            "SELECT actor_id,status FROM dietitian_health_check_draft_operations "
            "WHERE case_id='case-1' AND request_id='shared-request-id'"
        ).fetchone()
    assert operation == (DIETITIAN_UID, "completed")


def test_unrefreshed_canonical_source_change_conflicts_without_writing(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        token = load_health_check_detail(conn, case_id="case-1")["source_token"]
        before = conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews").fetchone()[0]
        conn.execute(
            """UPDATE food_logs SET version=version+1,
                      nutrition_snapshot_json='{"calories_kcal":777}'
               WHERE log_id='log-owned'"""
        )
    payload = {
        **{key: f"live-{key}" for key in ("good", "priority", "next_7_days", "comment")},
        "expected_source_token": token,
        "expected_review_version": 2,
        "request_id": "unrefreshed-source",
    }
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, draft_saver=saver
    )
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"}, json=payload,
    )
    assert response.status_code == 409
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews").fetchone()[0] == before
        assert conn.execute(
            "SELECT COUNT(*) FROM dietitian_health_check_draft_operations"
        ).fetchone()[0] == 0


def test_same_second_refresh_invalidates_old_source_token(tmp_path):
    from dietitian_health_check_api import load_health_check_detail
    from dietitian_health_check_draft import create_health_check_draft_saver
    from vip_health_check import refresh_case_source_manifest

    path = _populated_db(tmp_path)
    saver = create_health_check_draft_saver(path)
    with sqlite3.connect(path) as conn:
        token = load_health_check_detail(conn, case_id="case-1")["source_token"]
        updated_at = conn.execute(
            "SELECT updated_at FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()[0]
        conn.execute(
            """UPDATE food_logs SET version=version+1,
                      nutrition_snapshot_json='{"calories_kcal":888}'
               WHERE log_id='log-owned'"""
        )
        refresh_case_source_manifest(
            conn, case_id="case-1", evaluated_at=datetime.fromisoformat(updated_at)
        )
        assert conn.execute(
            "SELECT updated_at FROM vip_health_check_cases WHERE case_id='case-1'"
        ).fetchone()[0] == updated_at
        new_token = load_health_check_detail(conn, case_id="case-1")["source_token"]
        assert new_token != token
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, draft_saver=saver
    )
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"},
        json={
            **{key: f"aba-{key}" for key in ("good", "priority", "next_7_days", "comment")},
            "expected_source_token": token,
            "expected_review_version": 2,
            "request_id": "same-second-aba",
        },
    )
    assert response.status_code == 409


def _draft_payload(**changes):
    payload = {
        "good": "做得好", "priority": "優先事項",
        "next_7_days": "七天行動", "comment": "個人點評",
        "expected_source_token": "a" * 64,
        "expected_review_version": 2, "request_id": "request-001",
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    ("headers", "verifier", "allowed", "expected"),
    [
        ({}, None, (DIETITIAN_UID,), 401),
        ({"Authorization": "Bearer bad"}, "invalid", (DIETITIAN_UID,), 401),
        ({"Authorization": "Bearer signed"}, None, (OTHER_UID,), 403),
        ({"Authorization": "Bearer signed"}, "unavailable", (DIETITIAN_UID,), 503),
    ],
)
def test_draft_authorization_denies_before_writer(headers, verifier, allowed, expected):
    from dietitian_health_check_api import LineAuthenticationError, LineAuthenticationUnavailable

    calls = []
    def verify(_token, *, channel_id):
        if verifier == "invalid":
            raise LineAuthenticationError("private")
        if verifier == "unavailable":
            raise LineAuthenticationUnavailable("private")
        return DIETITIAN_UID
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []}, verifier=verify,
        allowed=allowed, draft_saver=lambda *args: calls.append(args),
    )
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers=headers, json=_draft_payload(),
    )
    assert response.status_code == expected
    assert calls == []
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text


@pytest.mark.parametrize(
    "mutation",
    [
        {"actor": DIETITIAN_UID}, {"user_id": CUSTOMER_UID}, {"state": "draft"},
        {"good": ""}, {"comment": "x" * 4001},
        {"expected_review_version": True}, {"expected_review_version": -1},
        {"expected_case_updated_at": "not-a-version"}, {"request_id": "bad request"},
    ],
)
def test_draft_body_allowlist_and_bounds_reject_before_writer(mutation):
    calls = []
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        draft_saver=lambda *args: calls.append(args),
    )
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"}, json=_draft_payload(**mutation),
    )
    assert response.status_code == 422
    assert calls == []


def test_draft_unknown_stale_review_stale_source_and_frozen_fail_without_write(tmp_path):
    from dietitian_health_check_draft import create_health_check_draft_saver

    path = _populated_db(tmp_path)
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        draft_saver=create_health_check_draft_saver(path),
    )
    headers = {"Authorization": "Bearer signed"}
    probes = [
        ("missing-case", _draft_payload(), 404),
        ("case-1", _draft_payload(expected_review_version=1), 409),
        ("case-1", _draft_payload(expected_source_token="b" * 64), 409),
    ]
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews").fetchone()[0]
    for case_id, payload, expected in probes:
        response = client.post(
            f"/api/dietitian/health-checks/{case_id}/reviews",
            headers=headers, json=payload,
        )
        assert response.status_code == expected
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vip_health_check_cases SET status='approved_pending_delivery'")
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews", headers=headers,
        json=_draft_payload(),
    )
    assert response.status_code == 409
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vip_health_check_reviews").fetchone()[0] == before


def test_draft_writer_failure_maps_to_generic_503_without_leak():
    client, _seen = _client(
        loader=lambda *_args, **_kwargs: {"items": []},
        draft_saver=lambda *_args: (_ for _ in ()).throw(RuntimeError("private-db-path")),
    )
    response = client.post(
        "/api/dietitian/health-checks/case-1/reviews",
        headers={"Authorization": "Bearer signed"}, json=_draft_payload(),
    )
    assert response.status_code == 503
    assert "private-db-path" not in response.text


def test_feature_flag_and_enabled_identity_configuration_are_fail_closed():
    from dietitian_health_check_api import load_dietitian_health_check_config

    assert load_dietitian_health_check_config({}).enabled is False
    assert load_dietitian_health_check_config({"DIETITIAN_HEALTH_CHECK_READ_ENABLED": "false"}).enabled is False
    with pytest.raises(ValueError):
        load_dietitian_health_check_config({"DIETITIAN_HEALTH_CHECK_READ_ENABLED": "TRUE"})
    with pytest.raises(ValueError):
        load_dietitian_health_check_config({"DIETITIAN_HEALTH_CHECK_READ_ENABLED": "true"})

    valid = load_dietitian_health_check_config(
        {
            "DIETITIAN_HEALTH_CHECK_READ_ENABLED": "true",
            "DIETITIAN_HEALTH_CHECK_LIFF_ID": LIFF_ID,
            "DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
            "DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS": f"{DIETITIAN_UID},{OTHER_UID}",
        }
    )
    assert valid.allowed_uids == frozenset({DIETITIAN_UID, OTHER_UID})

    base = {
        "DIETITIAN_HEALTH_CHECK_READ_ENABLED": "true",
        "DIETITIAN_HEALTH_CHECK_LIFF_ID": LIFF_ID,
        "DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
        "DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS": DIETITIAN_UID,
    }
    for key, value in (
        ("DIETITIAN_HEALTH_CHECK_LIFF_ID", "9999999999-wrong"),
        ("DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID", "abc"),
        ("DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID", "12345"),
        ("DIETITIAN_HEALTH_CHECK_LIFF_ID", CHANNEL_ID + "-x"),
        ("DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS", "Ushort"),
        ("DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS", DIETITIAN_UID + ","),
    ):
        with pytest.raises(ValueError):
            load_dietitian_health_check_config({**base, key: value})


def test_line_verifier_checks_official_endpoint_and_required_temporal_claims():
    from dietitian_health_check_api import verify_line_id_token

    calls = []

    def post(url, *, data, timeout):
        calls.append((url, data, timeout))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "iss": "https://access.line.me", "aud": CHANNEL_ID,
                "sub": DIETITIAN_UID, "exp": NOW + 300, "iat": NOW - 10,
            },
        )

    assert verify_line_id_token("signed", channel_id=CHANNEL_ID, http_post=post, now=lambda: NOW) == DIETITIAN_UID
    assert calls == [("https://api.line.me/oauth2/v2.1/verify", {"id_token": "signed", "client_id": CHANNEL_ID}, 5)]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_line_verifier_maps_provider_unavailability_to_503_class(status):
    from dietitian_health_check_api import LineAuthenticationUnavailable, verify_line_id_token

    with pytest.raises(LineAuthenticationUnavailable):
        verify_line_id_token(
            "secret-token", channel_id=CHANNEL_ID,
            http_post=lambda *_args, **_kwargs: SimpleNamespace(status_code=status, json=lambda: {}),
            now=lambda: NOW,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"iss": "https://access.line.me", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": NOW + 1},
        {"iss": "https://access.line.me", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": True, "iat": NOW},
        ["not", "mapping"],
    ],
)
def test_line_verifier_treats_malformed_success_as_unavailable(payload):
    from dietitian_health_check_api import LineAuthenticationUnavailable, verify_line_id_token

    with pytest.raises(LineAuthenticationUnavailable):
        verify_line_id_token(
            "secret", channel_id=CHANNEL_ID,
            http_post=lambda *_args, **_kwargs: SimpleNamespace(status_code=200, json=lambda: payload),
            now=lambda: NOW,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"iss": "bad", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": NOW + 1, "iat": NOW},
        {"iss": "https://access.line.me", "aud": "999", "sub": DIETITIAN_UID, "exp": NOW + 1, "iat": NOW},
        {"iss": "https://access.line.me", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": NOW, "iat": NOW - 1},
        {"iss": "https://access.line.me", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": NOW + 100, "iat": NOW + 61},
        {"iss": "https://access.line.me", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": NOW + 100, "iat": 0},
        {"iss": "https://access.line.me", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": NOW + 100, "iat": -1},
        {"iss": "https://access.line.me", "aud": CHANNEL_ID, "sub": DIETITIAN_UID, "exp": NOW + 10**12, "iat": NOW},
    ],
)
def test_line_verifier_rejects_invalid_identity_or_time_without_secret_leak(payload):
    from dietitian_health_check_api import LineAuthenticationError, verify_line_id_token

    with pytest.raises(LineAuthenticationError) as exc:
        verify_line_id_token(
            "secret-token", channel_id=CHANNEL_ID,
            http_post=lambda *_args, **_kwargs: SimpleNamespace(status_code=200, json=lambda: payload),
            now=lambda: NOW,
        )
    assert "secret-token" not in str(exc.value)


@pytest.mark.parametrize(
    ("headers", "verifier", "allowed", "expected"),
    [
        ({}, None, (DIETITIAN_UID,), 401),
        ({"Authorization": "Basic x"}, None, (DIETITIAN_UID,), 401),
        ({"Authorization": "Bearer bad"}, "invalid", (DIETITIAN_UID,), 401),
        ({"Authorization": "Bearer valid"}, "unavailable", (DIETITIAN_UID,), 503),
        ({"Authorization": "Bearer valid"}, None, (OTHER_UID,), 403),
    ],
)
def test_permission_matrix_denials_happen_before_database_loader(headers, verifier, allowed, expected):
    from dietitian_health_check_api import LineAuthenticationError, LineAuthenticationUnavailable

    def verify(_token, *, channel_id):
        if verifier == "invalid":
            raise LineAuthenticationError("private diagnostic")
        if verifier == "unavailable":
            raise LineAuthenticationUnavailable("private provider diagnostic")
        return DIETITIAN_UID

    client, seen = _client(loader=lambda *_args, **_kwargs: {"items": []}, verifier=verify, allowed=allowed)
    response = client.get("/api/dietitian/health-checks?user_id=" + CUSTOMER_UID, headers=headers)
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"
    assert seen == []
    assert "private" not in response.text
    assert CUSTOMER_UID not in response.text


def test_list_query_is_strict_and_bounded_before_loader():
    client, seen = _client(loader=lambda *_args, **_kwargs: {"items": []})
    headers = {"Authorization": "Bearer signed"}
    for query in (
        "?status=unknown", "?limit=0", "?limit=101", "?offset=-1", "?offset=10001",
        "?limit=" + "9" * 5000, "?offset=" + "9" * 5000,
        "?" + "&".join("status=collecting" for _ in range(100)),
    ):
        response = client.get("/api/dietitian/health-checks" + query, headers=headers)
        assert response.status_code == 422
    assert seen == []

    response = client.get(
        "/api/dietitian/health-checks?status=collecting&status=delivered&limit=10&offset=2"
        f"&user_id={CUSTOMER_UID}&uid={OTHER_UID}",
        headers={**headers, "X-User-Id": OTHER_UID},
    )
    assert response.status_code == 200
    assert seen == [(('list',), {"statuses": ("collecting", "delivered"), "limit": 10, "offset": 2})]


def test_detail_case_id_is_strict_and_rejected_before_loader():
    client, seen = _client(loader=lambda *_args, **_kwargs: {"items": []})
    headers = {"Authorization": "Bearer signed"}
    for case_id, expected in (
        ("contains space", 422), ("../escape", 404), ("x" * 129, 422)
    ):
        response = client.get("/api/dietitian/health-checks/" + case_id, headers=headers)
        assert response.status_code == expected
    assert seen == []


def test_projection_lists_and_details_canonical_owned_data_without_secrets(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        listing = load_health_check_list(conn, statuses=("ready_for_review",), limit=10, offset=0)
        detail = load_health_check_detail(conn, case_id="case-1")

    assert detail is not None
    assert listing["total"] == 1
    assert listing["items"][0]["case_id"] == "case-1"
    assert detail["profile"] == {
        "name": "王小明", "tdee": 2100, "protein": 100.5,
        "goal": "減脂", "restrictions": "花生", "active_days": "一,三,五",
    }
    assert detail["valid_days"][0]["local_date"] == "2026-09-02"
    source_logs = detail["source_logs"]
    assert isinstance(source_logs, list)
    source_by_id = {item["log_id"]: item for item in source_logs}
    assert source_by_id["log-owned"] == {
        "log_id": "log-owned",
        "food_log_version": 3,
        "nutrition_snapshot": {"calories_kcal": 500},
    }
    assert detail["latest_review"]["review_version"] == 2
    serialized = json.dumps({"listing": listing, "detail": detail}, ensure_ascii=False)
    for forbidden in (
        CUSTOMER_UID, "user_id", "source_image_ref", "private-image",
        "activation-secret", "event-secret", "manifest-secret", "hash-secret",
        "approved_by", "U99999999999999999999999999999999", "benefit_key", "vip_code",
    ):
        assert forbidden not in serialized


def test_projection_excludes_foreign_unconfirmed_and_deleted_source_logs(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        for log_id, owner, confirmed, deleted in (
            ("foreign", OTHER_UID, "confirmed", ""),
            ("pending", CUSTOMER_UID, "pending", ""),
            ("deleted", CUSTOMER_UID, "confirmed", "2026-09-05"),
        ):
            conn.execute(
                """INSERT INTO food_logs VALUES (?,?, 'food-1','2026-09-02','lunch',1,1,'份',
                   '{}','{}','{}','image','?',?,1)""".replace("'?'", "?"),
                (log_id, owner, confirmed, deleted),
            )
            conn.execute(
                "INSERT INTO vip_health_check_source_refs VALUES (?,?,?,?,?,?,?)",
                ("case-1", log_id, 1, "2026-09-02", "reason", "secret", "2026-09-03"),
            )
        with pytest.raises(ValueError):
            load_health_check_detail(conn, case_id="case-1")


def test_projection_never_substitutes_mutated_log_for_snapshotted_version(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET version=4,nutrition_snapshot_json=? WHERE log_id='log-owned'",
            (json.dumps({"marker": "CURRENT-MUTATED"}),),
        )
        with pytest.raises(ValueError, match="invalid source reference semantics"):
            load_health_check_detail(conn, case_id="case-1")


def test_projection_verifies_source_hash_even_when_writer_failed_to_increment_version(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json=? WHERE log_id='log-owned'",
            (json.dumps({"calories_kcal": 999, "marker": "MUTATED-SAME-VERSION"}),),
        )
        with pytest.raises(ValueError, match="invalid source reference semantics"):
            load_health_check_detail(conn, case_id="case-1")
        with pytest.raises(ValueError, match="invalid source reference semantics"):
            load_health_check_list(
                conn, statuses=("ready_for_review",), limit=10, offset=0
            )


def test_case_source_relationship_corruption_fails_closed_in_list_and_detail(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    day_only_manifest = hashlib.sha256(
        "\n".join(
            f"day:{day}:{count}:{rule}"
            for day, rule, count, _status in VALID_DAY_ROWS
        ).encode()
    ).hexdigest()
    corruptions = (
        (
            "DELETE FROM vip_health_check_source_refs",
            (
                "UPDATE vip_health_check_cases SET source_manifest_hash=?",
                (day_only_manifest,),
            ),
        ),
        (
            "UPDATE food_logs SET consumed_at='2026-09-09T12:00:00+08:00' WHERE log_id='log-owned'",
            None,
        ),
        (
            "UPDATE vip_health_check_source_refs SET local_date='2026-09-03' WHERE food_log_id='log-owned'",
            None,
        ),
    )
    for statement, follow_up in corruptions:
        with sqlite3.connect(path) as conn:
            conn.execute(statement)
            if follow_up is not None:
                conn.execute(*follow_up)
                conn.execute(
                    "UPDATE vip_health_check_reviews SET source_manifest_hash=?",
                    (day_only_manifest,),
                )
            with pytest.raises(ValueError):
                load_health_check_detail(conn, case_id="case-1")
            with pytest.raises(ValueError):
                load_health_check_list(
                    conn, statuses=("ready_for_review",), limit=10, offset=0
                )
            conn.rollback()


def test_terminal_cases_do_not_depend_on_mutable_canonical_source_rows(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vip_health_check_cases SET status='delivered'")
        conn.execute("UPDATE food_logs SET version=4 WHERE log_id='log-owned'")
        detail = load_health_check_detail(conn, case_id="case-1")
        listing = load_health_check_list(
            conn, statuses=("delivered",), limit=10, offset=0
        )
    assert detail is not None
    assert detail["status"] == "delivered"
    integrity = detail["source_integrity"]
    assert isinstance(integrity, dict)
    assert integrity["all_snapshots_available"] is False
    assert listing["total"] == 1

    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE vip_health_check_source_refs SET source_hash='not-a-sha256'
               WHERE food_log_id='log-owned'"""
        )
        with pytest.raises(ValueError, match="invalid source reference"):
            load_health_check_detail(conn, case_id="case-1")
        with pytest.raises(ValueError, match="invalid source reference"):
            load_health_check_list(
                conn, statuses=("delivered",), limit=10, offset=0
            )


def test_delivered_detail_keeps_three_days_and_six_references_when_no_snapshot_is_available(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vip_health_check_cases SET status='delivered'")
        conn.execute("UPDATE food_logs SET version=version+1")
        detail = load_health_check_detail(conn, case_id="case-1")

    assert detail is not None
    assert detail["valid_day_count"] == 3
    assert detail["source_logs"] == []
    assert detail["source_integrity"] == {
        "referenced_count": 6,
        "available_snapshot_count": 0,
        "all_snapshots_available": False,
    }


def test_valid_day_completeness_must_match_registered_producer_rule(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    one_meal_days = (
        ("2026-09-02", "draft-confirmed-meals-v1", 1, "qualified"),
        *VALID_DAY_ROWS[1:],
    )
    manifest = _manifest_for_source_hash(SOURCE_HASH, one_meal_days)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE food_logs SET meal_slot='lunch'
               WHERE log_id='log-02-breakfast'"""
        )
        conn.execute(
            """UPDATE vip_health_check_valid_days SET qualifying_meal_count=1
               WHERE local_date='2026-09-02'"""
        )
        conn.execute(
            "UPDATE vip_health_check_cases SET source_manifest_hash=?",
            (manifest,),
        )
        conn.execute(
            "UPDATE vip_health_check_reviews SET source_manifest_hash=?",
            (manifest,),
        )
        for loader in (
            lambda: load_health_check_detail(conn, case_id="case-1"),
            lambda: load_health_check_list(
                conn, statuses=("ready_for_review",), limit=10, offset=0
            ),
        ):
            with pytest.raises(ValueError, match="invalid valid_day.completeness_status"):
                loader()

        conn.execute(
            """UPDATE vip_health_check_valid_days
               SET rule_version='unknown-rule',qualifying_meal_count=2
               WHERE local_date='2026-09-02'"""
        )
        with pytest.raises(ValueError, match="invalid valid_day.completeness_status"):
            load_health_check_detail(conn, case_id="case-1")


def test_empty_cancelled_case_accepts_canonical_empty_manifest(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM vip_health_check_source_refs")
        conn.execute("DELETE FROM vip_health_check_valid_days")
        conn.execute(
            """UPDATE vip_health_check_cases
               SET status='cancelled',valid_day_count=0,source_manifest_hash=''"""
        )
        detail = load_health_check_detail(conn, case_id="case-1")
        listing = load_health_check_list(
            conn, statuses=("cancelled",), limit=10, offset=0
        )
    assert detail is not None
    assert detail["status"] == "cancelled"
    assert detail["valid_days"] == []
    assert listing["total"] == 1


def test_projection_never_labels_unhashed_mutable_fields_as_snapshot_evidence(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_catalog SET product_name='CATALOG-NAME-CHANGED' WHERE food_id='food-1'"
        )
        conn.execute(
            """UPDATE food_logs SET consumed_servings=9,consumed_amount=999,
                      consumed_unit='private-unit' WHERE log_id='log-owned'"""
        )
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail is not None
    source_logs = detail["source_logs"]
    assert isinstance(source_logs, list)
    source = next(item for item in source_logs if item["log_id"] == "log-owned")
    assert source == {
        "log_id": "log-owned",
        "food_log_version": 3,
        "nutrition_snapshot": {"calories_kcal": 500},
    }
    assert detail["source_integrity"]["all_snapshots_available"] is True
    serialized = json.dumps(detail)
    for forbidden in ("CATALOG-NAME-CHANGED", "dinner", "private-unit", "999"):
        assert forbidden not in serialized


def test_delivered_projection_does_not_expose_unbound_source_ref_date(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vip_health_check_cases SET status='delivered'")
        conn.execute(
            "UPDATE vip_health_check_source_refs SET local_date='2026-09-03' "
            "WHERE food_log_id='log-owned'"
        )
        detail = load_health_check_detail(conn, case_id="case-1")
        source = next(item for item in detail["source_logs"] if item["log_id"] == "log-owned")
    assert "local_date" not in source
    assert "meal_slot" not in source


def test_ready_projection_does_not_expose_hash_unbound_legal_meal_slot_swap(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET meal_slot=CASE log_id "
            "WHEN 'log-owned' THEN 'breakfast' WHEN 'log-02-breakfast' THEN 'lunch' "
            "ELSE meal_slot END WHERE log_id IN ('log-owned','log-02-breakfast')"
        )
        detail = load_health_check_detail(conn, case_id="case-1")
        source = next(item for item in detail["source_logs"] if item["log_id"] == "log-owned")
    assert "local_date" not in source
    assert "meal_slot" not in source


def test_v2_projection_exposes_bound_timeline_fields_and_rejects_legal_slot_swap(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        _upgrade_fixture_to_v2(conn)
        detail = load_health_check_detail(conn, case_id="case-1")
        assert detail is not None
        source = next(item for item in detail["source_logs"] if item["log_id"] == "log-owned")
        assert source["local_date"] == "2026-09-02"
        assert source["normalized_meal_slot"] == "lunch"

        conn.execute(
            "UPDATE food_logs SET meal_slot=CASE log_id "
            "WHEN 'log-owned' THEN 'breakfast' WHEN 'log-02-breakfast' THEN 'lunch' "
            "ELSE meal_slot END WHERE log_id IN ('log-owned','log-02-breakfast')"
        )
        with pytest.raises(ValueError, match="invalid source reference semantics"):
            load_health_check_detail(conn, case_id="case-1")


def test_frozen_v2_case_retains_history_but_omits_tampered_reference_date(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        _upgrade_fixture_to_v2(conn)
        conn.execute("UPDATE vip_health_check_cases SET status='delivered'")
        conn.execute(
            "UPDATE vip_health_check_source_refs SET local_date='2026-09-03' "
            "WHERE food_log_id='log-owned'"
        )
        detail = load_health_check_detail(conn, case_id="case-1")

    assert detail is not None
    assert detail["status"] == "delivered"
    assert "log-owned" not in {item["log_id"] for item in detail["source_logs"]}
    assert detail["source_integrity"]["all_snapshots_available"] is False


def test_refreshable_projection_rejects_stale_source_date_or_version(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    for statement in (
        "UPDATE vip_health_check_source_refs SET local_date='2026-09-03' WHERE food_log_id='log-owned'",
        "UPDATE vip_health_check_source_refs SET food_log_version=2 WHERE food_log_id='log-owned'",
    ):
        with sqlite3.connect(path) as conn:
            conn.execute(statement)
            with pytest.raises(ValueError):
                load_health_check_detail(conn, case_id="case-1")
            conn.rollback()


def test_source_approval_label_requires_complete_verified_approval_contract(monkeypatch):
    import dietitian_health_check_api as api

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE food_logs (
          log_id TEXT, user_id TEXT, food_id TEXT, consumed_servings REAL,
          approved_exchange_json TEXT, exchange_approval_id TEXT
        );
        CREATE TABLE food_catalog (
          food_id TEXT, source_type TEXT, owner_user_id TEXT, fingerprint TEXT
        );
        CREATE TABLE food_exchange_approvals (
          approval_id TEXT, food_id TEXT, food_fingerprint TEXT,
          suggestion_rule_version TEXT, approved_exchange_json TEXT,
          approved_exchange_hash TEXT
        );
        INSERT INTO food_logs VALUES ('log-1','owner','food-1',1,'{}','approval-1');
        INSERT INTO food_catalog VALUES ('food-1','user_meal_photo','owner','fingerprint');
        INSERT INTO food_exchange_approvals VALUES
          ('approval-1','food-1','fingerprint','meal-photo-admin-v2','{}','hash');
        """
    )
    seen = []

    def verify(**kwargs):
        seen.append(kwargs)
        return {"is_valid": True}

    monkeypatch.setattr(api, "verified_exchange_approval_projection", verify)
    assert api._verified_source_approval_status(conn, log_id="log-1") == "approved"
    assert seen[0]["log_user_id"] == "owner"
    assert seen[0]["catalog_owner_user_id"] == "owner"
    assert seen[0]["approval_id"] == "approval-1"

    monkeypatch.setattr(
        api, "verified_exchange_approval_projection", lambda **_kwargs: {"is_valid": False}
    )
    assert api._verified_source_approval_status(conn, log_id="log-1") is None
    conn.execute("DELETE FROM food_exchange_approvals")
    assert api._verified_source_approval_status(conn, log_id="log-1") is None


def test_projection_suppresses_review_from_stale_source_manifest(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE vip_health_check_reviews SET source_manifest_hash=?,review_json=? WHERE review_version=2",
            ("c" * 64, json.dumps({"good": "STALE-REVIEW"})),
        )
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail["latest_review"] is None
    assert detail["latest_review_fresh"] is False
    assert detail["current_review_version"] == 2
    assert "STALE-REVIEW" not in json.dumps(detail)


def test_json_projection_allowlists_domain_fields_and_strips_nested_identifiers(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    embedded_uid = "U99999999999999999999999999999999"
    lowercase_uid = embedded_uid.lower()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcml2YXRlIn0.signature123456"
    nutrition_payload = json.dumps(
        {"calories_kcal": 500, "user_id": embedded_uid, "vip_code": "#VIP24-SECRET"},
        separators=(",", ":"),
        sort_keys=True,
    )
    source_hash = hashlib.sha256(
        f"log-owned:3:{nutrition_payload}".encode()
    ).hexdigest()
    bound_manifest = _manifest_for_source_hash(source_hash)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE food_logs SET nutrition_snapshot_json=?,exchange_snapshot_json=?,approved_exchange_json=?
               WHERE log_id='log-owned'""",
            (
                nutrition_payload,
                json.dumps({"source_image_ref": "private-image"}),
                json.dumps({"approved_by": embedded_uid}),
            ),
        )
        conn.execute(
            "UPDATE vip_health_check_source_refs SET source_hash=? WHERE food_log_id='log-owned'",
            (source_hash,),
        )
        conn.execute(
            "UPDATE vip_health_check_cases SET source_manifest_hash=? WHERE case_id='case-1'",
            (bound_manifest,),
        )
        conn.execute(
            "UPDATE vip_health_check_reviews SET source_manifest_hash=? WHERE case_id='case-1'",
            (bound_manifest,),
        )
        conn.execute(
            """UPDATE vip_health_check_reviews
               SET ai_observations_json=?,review_json=?,suggested_values_json=?
               WHERE review_version=2""",
            (
                json.dumps({
                    "patterns": [
                        "早餐穩定", "a" * 64, "activation-secret-PRIVATE",
                        lowercase_uid, jwt, "db=C:\\private\\customer.db",
                        "next=%2Fsrv%2Fprivate%2Fcustomer.db", "activation_id=private-123",
                        "Stored at /opt/app/private.db",
                        "file:///opt/app/private.db",
                    ],
                    "observation": "https://host/x?next=/srv/private/customer.db",
                    "source_hash": "secret-hash",
                }),
                json.dumps({
                    "good": "穩定", "priority": "The token economy affects nutrition pricing.",
                    "improvements": ["https://example.com/nutrition/guide"],
                    "summary": "sk-private-secret", "approved_by": embedded_uid
                }),
                json.dumps({"protein_g": 100, "vip_code": "#VIP24-SECRET"}),
            ),
        )
        detail = load_health_check_detail(conn, case_id="case-1")
    serialized = json.dumps(detail, ensure_ascii=False)
    assert detail["source_logs"][0]["nutrition_snapshot"] == {"calories_kcal": 500}
    assert "exchange_snapshot" not in detail["source_logs"][0]
    assert "approved_exchange" not in detail["source_logs"][0]
    assert detail["latest_review"]["ai_observations"] == {"patterns": ["早餐穩定"]}
    assert detail["latest_review"]["review"] == {
        "good": "穩定",
        "priority": "The token economy affects nutrition pricing.",
        "improvements": ["https://example.com/nutrition/guide"],
    }
    assert detail["latest_review"]["suggested_values"] == {"protein_g": 100}
    for forbidden in (
        embedded_uid, "user_id", "source_image_ref", "source_hash", "approved_by",
        "vip_code", "#VIP24-SECRET", "a" * 64, "activation-secret-PRIVATE",
        "/srv/private/customer.db", "sk-private-secret", lowercase_uid, jwt,
        "C:\\private\\customer.db", "%2Fsrv%2Fprivate", "activation_id=private-123",
        "/opt/app/private.db",
        "file:///opt/app/private.db",
    ):
        assert forbidden not in serialized


def test_semantically_corrupt_identifiers_and_timestamps_fail_closed(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    for statement in (
        "UPDATE vip_health_check_cases SET case_id='bad id'",
        "UPDATE vip_health_check_cases SET window_started_at='not-a-time'",
        "UPDATE vip_health_check_cases SET window_ends_at='0000'",
        "UPDATE vip_health_check_cases SET window_ends_at=window_started_at",
        "UPDATE vip_health_check_cases SET window_started_at='2026-09-01',window_ends_at='2026-09-08'",
        "UPDATE vip_health_check_valid_days SET local_date='2026-99-99'",
    ):
        with sqlite3.connect(path) as conn:
            conn.execute(statement)
            target_id = "bad id" if "case_id" in statement else "case-1"
            with pytest.raises(ValueError):
                load_health_check_detail(conn, case_id=target_id)
            if "valid_days" not in statement:
                with pytest.raises(ValueError):
                    load_health_check_list(
                        conn, statuses=ALL_CASE_STATUSES, limit=10, offset=0
                    )
            conn.rollback()


def test_cross_field_case_semantics_fail_closed(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vip_health_check_cases SET valid_day_count=2")
        with pytest.raises(ValueError):
            load_health_check_detail(conn, case_id="case-1")
        conn.rollback()

    with sqlite3.connect(path) as conn:
        two_days = VALID_DAY_ROWS[:2]
        conn.execute("DELETE FROM vip_health_check_valid_days WHERE local_date='2026-09-04'")
        conn.execute(
            "UPDATE vip_health_check_cases SET valid_day_count=2,source_manifest_hash=?",
            (_manifest_for_source_hash(SOURCE_HASH, two_days),),
        )
        with pytest.raises(ValueError):
            load_health_check_detail(conn, case_id="case-1")
        with pytest.raises(ValueError):
            load_health_check_list(
                conn, statuses=("ready_for_review",), limit=10, offset=0
            )
        conn.rollback()

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE vip_health_check_valid_days SET local_date='2026-09-09' WHERE local_date='2026-09-04'"
        )
        with pytest.raises(ValueError):
            load_health_check_detail(conn, case_id="case-1")
        with pytest.raises(ValueError):
            load_health_check_list(
                conn, statuses=("ready_for_review",), limit=10, offset=0
            )
        conn.rollback()


def test_valid_day_on_window_end_date_matches_exact_timestamp_producer_semantics(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE vip_health_check_cases
               SET window_ends_at='2026-09-04T09:00:00+08:00'
               WHERE case_id='case-1'"""
        )
        conn.execute(
            """UPDATE food_logs SET consumed_at='2026-09-04T07:00:00+08:00'
               WHERE log_id='log-04-breakfast'"""
        )
        conn.execute(
            """UPDATE food_logs SET consumed_at='2026-09-04T08:00:00+08:00'
               WHERE log_id='log-04-lunch'"""
        )
        detail = load_health_check_detail(conn, case_id="case-1")
        listing = load_health_check_list(
            conn, statuses=("ready_for_review",), limit=10, offset=0
        )
    assert detail is not None
    valid_days = detail["valid_days"]
    items = listing["items"]
    assert isinstance(valid_days, list) and isinstance(valid_days[-1], dict)
    assert isinstance(items, list) and isinstance(items[0], dict)
    assert valid_days[-1]["local_date"] == "2026-09-04"
    assert items[0]["valid_day_count"] == 3


def test_valid_day_window_uses_taipei_dates_after_offset_conversion(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE vip_health_check_cases
               SET window_started_at='2026-09-01T16:00:00Z',
                   window_ends_at='2026-09-03T17:00:00Z'
               WHERE case_id='case-1'"""
        )
        conn.execute(
            """UPDATE food_logs SET consumed_at='2026-09-04T00:10:00+08:00'
               WHERE log_id='log-04-breakfast'"""
        )
        conn.execute(
            """UPDATE food_logs SET consumed_at='2026-09-04T00:20:00+08:00'
               WHERE log_id='log-04-lunch'"""
        )
        assert load_health_check_detail(conn, case_id="case-1") is not None
        assert load_health_check_list(
            conn, statuses=("ready_for_review",), limit=10, offset=0
        )["total"] == 1

        shifted_days = (
            ("2026-09-01", "draft-confirmed-meals-v1", 2, "qualified"),
            ("2026-09-02", "draft-confirmed-meals-v1", 2, "qualified"),
            ("2026-09-03", "draft-confirmed-meals-v1", 2, "qualified"),
        )
        conn.execute(
            """UPDATE vip_health_check_valid_days SET local_date='2026-09-01'
               WHERE local_date='2026-09-04'"""
        )
        conn.execute(
            "UPDATE vip_health_check_cases SET source_manifest_hash=? WHERE case_id='case-1'",
            (_manifest_for_source_hash(SOURCE_HASH, shifted_days),),
        )
        with pytest.raises(ValueError, match="invalid valid-day window"):
            load_health_check_detail(conn, case_id="case-1")
        with pytest.raises(ValueError, match="invalid valid-day window"):
            load_health_check_list(
                conn, statuses=("ready_for_review",), limit=10, offset=0
            )


def test_case_window_normalizes_mixed_naive_and_aware_boundaries(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE vip_health_check_cases
               SET window_started_at='2026-09-02T00:00:00',
                   window_ends_at='2026-09-05T00:00:00+08:00'
               WHERE case_id='case-1'"""
        )
        assert load_health_check_detail(conn, case_id="case-1") is not None
        assert load_health_check_list(
            conn, statuses=("ready_for_review",), limit=10, offset=0
        )["total"] == 1


def test_valid_day_on_exact_midnight_end_date_is_rejected(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE vip_health_check_cases
               SET window_ends_at='2026-09-04T00:00:00+08:00'
               WHERE case_id='case-1'"""
        )
        with pytest.raises(ValueError, match="invalid valid-day window"):
            load_health_check_detail(conn, case_id="case-1")
        with pytest.raises(ValueError, match="invalid valid-day window"):
            load_health_check_list(
                conn, statuses=("ready_for_review",), limit=10, offset=0
            )


def test_invalid_empty_manifests_never_bind_a_review(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE vip_health_check_cases SET source_manifest_hash='' WHERE case_id='case-1'"
        )
        conn.execute(
            "UPDATE vip_health_check_reviews SET source_manifest_hash='',review_json=? WHERE review_version=2",
            (json.dumps({"good": "UNBOUND-REVIEW"}),),
        )
        with pytest.raises(ValueError):
            load_health_check_detail(conn, case_id="case-1")


def test_field_specific_json_types_fail_closed(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    bad_nutrition = json.dumps(
        {"calories_kcal": "five", "fat_g": True, "protein_g": [1, [2, 3]]},
        separators=(",", ":"),
        sort_keys=True,
    )
    source_hash = hashlib.sha256(f"log-owned:3:{bad_nutrition}".encode()).hexdigest()
    bound_manifest = _manifest_for_source_hash(source_hash)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vip_health_check_cases SET status='delivered'")
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json=? WHERE log_id='log-owned'",
            (bad_nutrition,),
        )
        conn.execute(
            "UPDATE vip_health_check_source_refs SET source_hash=? WHERE food_log_id='log-owned'",
            (source_hash,),
        )
        conn.execute(
            "UPDATE vip_health_check_cases SET source_manifest_hash=? WHERE case_id='case-1'",
            (bound_manifest,),
        )
        conn.execute(
            "UPDATE vip_health_check_reviews SET source_manifest_hash=? WHERE case_id='case-1'",
            (bound_manifest,),
        )
        conn.execute(
            "UPDATE vip_health_check_reviews SET review_json=? WHERE review_version=2",
            (json.dumps({"good": 123}),),
        )
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail is not None
    source_logs = detail["source_logs"]
    assert isinstance(source_logs, list)
    assert len(source_logs) == 5
    assert all(item["log_id"] != "log-owned" for item in source_logs)
    assert detail["source_integrity"] == {
        "referenced_count": 6,
        "available_snapshot_count": 5,
        "all_snapshots_available": False,
    }
    assert detail["latest_review"] is None
    assert detail["latest_review_available"] is False


def test_semantically_corrupt_case_or_profile_fails_closed(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    corruptions = (
        "UPDATE vip_health_check_cases SET status='evil'",
        "UPDATE vip_health_check_cases SET valid_day_count=-99",
        "UPDATE health_profile SET tdee='not-a-number'",
        "UPDATE health_profile SET protein='oops'",
    )
    for statement in corruptions:
        with sqlite3.connect(path) as conn:
            conn.execute(statement)
            with pytest.raises(ValueError):
                load_health_check_detail(conn, case_id="case-1")
            if "status='evil'" not in statement:
                with pytest.raises(ValueError):
                    load_health_check_list(
                        conn, statuses=ALL_CASE_STATUSES, limit=10, offset=0
                    )
            conn.rollback()


def test_projection_never_returns_raw_malformed_json_and_missing_profile_is_null(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vip_health_check_cases SET status='delivered'")
        conn.execute("DELETE FROM health_profile")
        conn.execute("UPDATE food_logs SET nutrition_snapshot_json='raw-secret-broken-json'")
        conn.execute("UPDATE vip_health_check_reviews SET review_json='review-secret-broken-json' WHERE review_version=2")
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail["profile"] == {
        "name": None, "tdee": None, "protein": None,
        "goal": None, "restrictions": None, "active_days": None,
    }
    assert detail["source_logs"] == []
    assert detail["source_integrity"]["all_snapshots_available"] is False
    assert detail["latest_review"] is None
    assert detail["latest_review_available"] is False
    assert "broken-json" not in json.dumps(detail)


def test_detail_not_found_is_404_and_database_error_is_generic_503(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    client, _seen = _client(
        loader=lambda kind, **kwargs: (
            load_health_check_detail(sqlite3.connect(path), case_id=kwargs["case_id"])
            if kind == "detail" else {"items": []}
        )
    )
    headers = {"Authorization": "Bearer signed"}
    response = client.get("/api/dietitian/health-checks/missing?user_id=" + CUSTOMER_UID, headers=headers)
    assert response.status_code == 404
    assert CUSTOMER_UID not in response.text

    broken_client, _ = _client(loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("private-db-path")))
    response = broken_client.get("/api/dietitian/health-checks", headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "health-check data unavailable"}
    assert "private-db-path" not in response.text


@pytest.mark.parametrize(
    "method",
    ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "PROPFIND"],
)
def test_disabled_routes_are_dark_for_every_method(method):
    from dietitian_health_check_api import attach_dietitian_health_check_routes

    app = FastAPI()
    assert attach_dietitian_health_check_routes(
        app, config=SimpleNamespace(enabled=False), list_loader=lambda **_: None,
        detail_loader=lambda _case_id: None,
    ) is False
    client = TestClient(app)
    for path in ("/api/dietitian/health-checks", "/api/dietitian/health-checks/case-1"):
        assert client.request(method, path).status_code == 404


@pytest.mark.parametrize(
    "method",
    ["HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "PROPFIND"],
)
def test_enabled_wrong_methods_are_405_with_sensitive_headers(method):
    client, seen = _client(loader=lambda *_args, **_kwargs: {"items": []})
    for path in ("/api/dietitian/health-checks", "/api/dietitian/health-checks/case-1"):
        response = client.request(method, path)
        assert response.status_code == 405
        assert response.headers["allow"] == "GET"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert seen == []
