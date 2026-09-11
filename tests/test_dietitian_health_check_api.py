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
SOURCE_HASH = hashlib.sha256(b'log-owned:3:{"calories_kcal":500}').hexdigest()
VALID_DAY_ROWS = (
    ("2026-09-02", "v1", 2, "qualified"),
    ("2026-09-03", "v1", 2, "qualified"),
    ("2026-09-04", "v1", 2, "qualified"),
)
def _manifest_for_source_hash(
    source_hash: str, day_rows=VALID_DAY_ROWS
) -> str:
    return hashlib.sha256(
        "\n".join(
            [f"log-owned:3:{source_hash}"]
            + [
                f"day:{day}:{count}:{rule}"
                for day, rule, count, _status in day_rows
            ]
        ).encode()
    ).hexdigest()


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
        conn.execute(
            """INSERT INTO food_logs VALUES
               ('log-owned',?,'food-1','2026-09-02T12:00:00+08:00','lunch',1,300,'g',
                '{"calories_kcal":500}','{}','{}','private-image',
                'confirmed','',3)""",
            (CUSTOMER_UID,),
        )
        conn.execute(
            "INSERT INTO vip_health_check_source_refs VALUES (?,?,?,?,?,?,?)",
            ("case-1", "log-owned", 3, "2026-09-02", "qualifying", SOURCE_HASH, "2026-09-03"),
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


def _client(*, loader, verifier=None, allowed=(DIETITIAN_UID,), image_loader=None):
    from dietitian_health_check_api import create_dietitian_health_check_router

    seen = []

    def recording_loader(*args, **kwargs):
        seen.append((args, kwargs))
        return loader(*args, **kwargs)

    app = FastAPI()
    app.include_router(
        create_dietitian_health_check_router(
            channel_id=CHANNEL_ID,
            allowed_uids=frozenset(allowed),
            list_loader=lambda **kwargs: recording_loader("list", **kwargs),
            detail_loader=lambda case_id: recording_loader("detail", case_id=case_id),
            image_loader=image_loader,
            token_verifier=verifier or (lambda _token, *, channel_id: DIETITIAN_UID),
        )
    )
    return TestClient(app, raise_server_exceptions=False), seen


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


def test_source_photo_requires_authorized_line_identity_before_loading():
    calls = []
    image_loader = lambda case_id, log_id: calls.append((case_id, log_id))
    path = "/api/dietitian/health-checks/case-1/photos/log-owned"

    client, _ = _client(loader=lambda *_args, **_kwargs: {}, image_loader=image_loader)
    assert client.get(path).status_code == 401

    forbidden, _ = _client(
        loader=lambda *_args, **_kwargs: {},
        allowed=(OTHER_UID,),
        image_loader=image_loader,
    )
    assert forbidden.get(path, headers={"Authorization": "Bearer signed"}).status_code == 403
    assert calls == []


def test_source_photo_is_case_bound_no_store_and_fail_closed():
    photo = b"\xff\xd8\xff" + b"x" * 200
    calls = []

    def load_photo(case_id, log_id):
        calls.append((case_id, log_id))
        if (case_id, log_id) == ("case-1", "log-owned"):
            return photo, "image/jpeg"
        return None

    client, _ = _client(loader=lambda *_args, **_kwargs: {}, image_loader=load_photo)
    headers = {"Authorization": "Bearer signed"}
    path = "/api/dietitian/health-checks/case-1/photos/log-owned"
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.content == photo
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    assert client.get(
        "/api/dietitian/health-checks/other-case/photos/log-owned", headers=headers
    ).status_code == 404
    assert client.get(
        "/api/dietitian/health-checks/case-1/photos/contains%20space", headers=headers
    ).status_code == 422

    broken, _ = _client(
        loader=lambda *_args, **_kwargs: {},
        image_loader=lambda *_args: (_ for _ in ()).throw(RuntimeError("private-path")),
    )
    failed = broken.get(path, headers=headers)
    assert failed.status_code == 503
    assert failed.json() == {"detail": "source photo unavailable"}
    assert "private-path" not in failed.text


def test_projection_lists_and_details_canonical_owned_data_without_secrets(tmp_path):
    from dietitian_health_check_api import load_health_check_detail, load_health_check_list

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        listing = load_health_check_list(conn, statuses=("ready_for_review",), limit=10, offset=0)
        detail = load_health_check_detail(conn, case_id="case-1")

    assert listing["total"] == 1
    assert listing["items"][0]["case_id"] == "case-1"
    assert detail["profile"] == {
        "name": "王小明", "tdee": 2100, "protein": 100.5,
        "goal": "減脂", "restrictions": "花生", "active_days": "一,三,五",
    }
    assert detail["valid_days"][0]["local_date"] == "2026-09-02"
    assert detail["source_logs"][0] == {
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
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail["source_logs"] == []
    assert detail["source_integrity"] == {
        "referenced_count": 1,
        "available_snapshot_count": 0,
        "all_snapshots_available": False,
    }
    assert "CURRENT-MUTATED" not in json.dumps(detail)


def test_projection_verifies_source_hash_even_when_writer_failed_to_increment_version(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_logs SET nutrition_snapshot_json=? WHERE log_id='log-owned'",
            (json.dumps({"calories_kcal": 999, "marker": "MUTATED-SAME-VERSION"}),),
        )
        detail = load_health_check_detail(conn, case_id="case-1")
    assert detail["source_logs"] == []
    assert detail["source_integrity"]["all_snapshots_available"] is False
    assert "MUTATED-SAME-VERSION" not in json.dumps(detail)


def test_projection_never_labels_unhashed_mutable_fields_as_snapshot_evidence(tmp_path):
    from dietitian_health_check_api import load_health_check_detail

    path = _populated_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE food_catalog SET product_name='CATALOG-NAME-CHANGED' WHERE food_id='food-1'"
        )
        conn.execute(
            """UPDATE food_logs SET consumed_at='2026-09-07T22:00:00+08:00',
                      meal_slot='dinner',consumed_servings=9,consumed_amount=999,
                      consumed_unit='private-unit' WHERE log_id='log-owned'"""
        )
        detail = load_health_check_detail(conn, case_id="case-1")
    source = detail["source_logs"][0]
    assert source == {
        "log_id": "log-owned",
        "food_log_version": 3,
        "nutrition_snapshot": {"calories_kcal": 500},
    }
    assert detail["source_integrity"]["all_snapshots_available"] is True
    serialized = json.dumps(detail)
    for forbidden in ("CATALOG-NAME-CHANGED", "dinner", "private-unit", "999"):
        assert forbidden not in serialized


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
            "UPDATE food_logs SET nutrition_snapshot_json=?,exchange_snapshot_json=?,approved_exchange_json=?",
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
        conn.rollback()


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
    assert detail["source_logs"] == []
    assert detail["source_integrity"]["all_snapshots_available"] is False
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
    "method", ["get", "head", "post", "put", "patch", "delete", "options", "trace"]
)
def test_disabled_routes_are_dark_for_every_method(method):
    from dietitian_health_check_api import attach_dietitian_health_check_routes

    app = FastAPI()
    assert attach_dietitian_health_check_routes(
        app, config=SimpleNamespace(enabled=False), list_loader=lambda **_: None,
        detail_loader=lambda _case_id: None,
    ) is False
    client = TestClient(app)
    for path in (
        "/api/dietitian/health-checks",
        "/api/dietitian/health-checks/case-1",
        "/api/dietitian/health-checks/case-1/photos/log-owned",
    ):
        assert client.request(method.upper(), path).status_code == 404


@pytest.mark.parametrize(
    "method", ["head", "post", "put", "patch", "delete", "options", "trace"]
)
def test_enabled_wrong_methods_are_405_with_sensitive_headers(method):
    client, seen = _client(loader=lambda *_args, **_kwargs: {"items": []})
    for path in (
        "/api/dietitian/health-checks",
        "/api/dietitian/health-checks/case-1",
        "/api/dietitian/health-checks/case-1/photos/log-owned",
    ):
        response = client.request(method.upper(), path)
        assert response.status_code == 405
        assert response.headers["allow"] == "GET"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert seen == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/dietitian/health-checks/",
        "/api/dietitian/health-checks/case-1/",
        "/api/dietitian/health-checks/case-1/photos/log-owned/",
    ],
)
def test_trailing_slash_routes_do_not_redirect_around_sensitive_policy(path):
    client, seen = _client(loader=lambda *_args, **_kwargs: {"items": []})

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 401
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert seen == []
