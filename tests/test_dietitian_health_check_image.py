from __future__ import annotations

from io import BytesIO
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

CHANNEL_ID = "2009251085"
LIFF_ID = CHANNEL_ID + "-dietitianCheck"
DIETITIAN_UID = "U1234567890abcdef1234567890abcdef"
OTHER_UID = "Uabcdef1234567890abcdef1234567890"
CUSTOMER_UID = "U11111111111111111111111111111111"


def _jpeg(size=(24, 16), color="red") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG")
    return output.getvalue()


def _png(size=(24, 16), color="red") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def _image_client(*, image_loader, allowed_loader=lambda: frozenset({DIETITIAN_UID}), verifier=None):
    from dietitian_health_check_api import DietitianHealthCheckConfig, attach_dietitian_health_check_routes

    app = FastAPI()
    attach_dietitian_health_check_routes(
        app,
        config=DietitianHealthCheckConfig(True, LIFF_ID, CHANNEL_ID, frozenset({DIETITIAN_UID})),
        list_loader=lambda **_kwargs: {"items": []},
        detail_loader=lambda _case_id: None,
        image_loader=image_loader,
        allowed_uid_loader=allowed_loader,
        token_verifier=verifier or (lambda _token, *, channel_id: DIETITIAN_UID),
    )
    return TestClient(app, raise_server_exceptions=False)


def test_image_route_auth_and_live_allowlist_precede_image_loader():
    calls = []
    allowlist = set()
    client = _image_client(
        image_loader=lambda case_id, log_id: calls.append((case_id, log_id)),
        allowed_loader=lambda: frozenset(allowlist),
    )
    path = "/api/dietitian/health-checks/case-1/sources/log-1/image"

    assert client.get(path).status_code == 401
    assert calls == []
    denied = client.get(path, headers={"Authorization": "Bearer signed"})
    assert denied.status_code == 403
    assert calls == []

    allowlist.add(DIETITIAN_UID)
    missing = client.get(path, headers={"Authorization": "Bearer signed"})
    assert missing.status_code == 404
    assert calls == [("case-1", "log-1")]
    for key, value in {
        "cache-control": "no-store", "pragma": "no-cache",
        "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    }.items():
        assert missing.headers[key] == value


def test_image_route_returns_only_preview_bytes_and_protects_non_get():
    from protected_health_check_image import ImagePreview

    raw = _jpeg()
    client = _image_client(image_loader=lambda _case, _log: ImagePreview(raw, "image/jpeg"))
    path = "/api/dietitian/health-checks/case-1/sources/log-1/image"
    response = client.get(path, headers={"Authorization": "Bearer signed"})
    assert response.status_code == 200
    assert response.content == raw
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert "nutrition-image:" not in response.text
    for method in ("post", "put", "patch", "delete", "options"):
        denied = getattr(client, method)(path)
        assert denied.status_code == 405
        assert denied.headers["allow"] == "GET"
        assert denied.headers["cache-control"] == "no-store"


def test_descriptor_safe_preview_rejects_symlink_image_root(tmp_path):
    from protected_health_check_image import ImageUnavailable, read_bounded_preview

    real_root = tmp_path / "real_nutrition_images"
    real_root.mkdir()
    filename = "8" * 32 + ".jpg"
    (real_root / filename).write_bytes(_jpeg())
    linked_root = tmp_path / "nutrition_images"
    os.symlink(real_root, linked_root)

    with pytest.raises(ImageUnavailable):
        read_bounded_preview(linked_root, "nutrition-image:" + filename)


def test_descriptor_safe_preview_rejects_fifo_without_blocking(tmp_path):
    root = tmp_path / "nutrition_images"
    root.mkdir()
    filename = "a" * 32 + ".jpg"
    os.mkfifo(root / filename)
    project_root = Path(__file__).resolve().parents[1]
    script = """
import sys
from protected_health_check_image import ImageUnavailable, read_bounded_preview

try:
    read_bounded_preview(sys.argv[1], sys.argv[2])
except ImageUnavailable:
    raise SystemExit(0)
raise SystemExit(1)
"""
    completed = subprocess.run(
        [
            "timeout",
            "2s",
            sys.executable,
            "-c",
            script,
            str(root),
            "nutrition-image:" + filename,
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert completed.returncode != 124, "opening an allowlisted FIFO blocked before fstat"
    assert completed.returncode == 0, completed.stderr


def test_descriptor_safe_preview_reencodes_and_rejects_symlink_corrupt_and_bomb(tmp_path, monkeypatch):
    from protected_health_check_image import ImageUnavailable, read_bounded_preview

    root = tmp_path / "nutrition_images"
    root.mkdir()
    name = "a" * 32 + ".jpg"
    (root / name).write_bytes(_jpeg((2000, 1000)))
    preview = read_bounded_preview(root, "nutrition-image:" + name)
    assert preview.media_type == "image/jpeg"
    assert preview.data.startswith(b"\xff\xd8\xff")
    assert len(preview.data) < 950 * 1024
    with Image.open(BytesIO(preview.data)) as image:
        assert image.width <= 1200 and image.height <= 1200

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(_jpeg())
    link_name = "b" * 32 + ".jpg"
    os.symlink(outside, root / link_name)
    bad_name = "c" * 32 + ".png"
    (root / bad_name).write_bytes(b"not an image")
    bomb_name = "d" * 32 + ".png"
    (root / bomb_name).write_bytes(_png((40, 40)))
    mismatch_name = "f" * 32 + ".png"
    (root / mismatch_name).write_bytes(_jpeg((5, 5)))
    monkeypatch.setattr("protected_health_check_image.MAX_SOURCE_PIXELS", 100)

    for ref in (
        "nutrition-image:" + link_name,
        "nutrition-image:" + bad_name,
        "nutrition-image:" + bomb_name,
        "nutrition-image:" + mismatch_name,
        "nutrition-image:../outside.jpg",
    ):
        with pytest.raises(ImageUnavailable):
            read_bounded_preview(root, ref)


def _health_db(path, image_ref: str):
    nutrition = '{"calories_kcal":500}'
    source_hash = hashlib.sha256(f"log-1:3:{nutrition}".encode()).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE vip_health_check_cases(case_id TEXT, user_id TEXT, status TEXT);
        CREATE TABLE vip_health_check_source_refs(case_id TEXT, food_log_id TEXT, food_log_version INTEGER, source_hash TEXT);
        CREATE TABLE food_catalog(food_id TEXT, owner_user_id TEXT, source_type TEXT, original_image_ref TEXT);
        CREATE TABLE food_logs(log_id TEXT, user_id TEXT, food_id TEXT, version INTEGER, nutrition_snapshot_json TEXT,
          source_image_ref TEXT, confirmation_status TEXT, deleted_at TEXT, trust_type TEXT, trust_hash TEXT,
          exchange_snapshot_json TEXT);
        CREATE TABLE pending_meal_photo_drafts(token TEXT, user_id TEXT, source_image_ref TEXT, status TEXT,
          version INTEGER, workflow_version TEXT, confirmed_log_id TEXT, approved_log_id TEXT, confirmed_by TEXT,
          original_confirmation_event_id TEXT);
        """)
        conn.execute("INSERT INTO vip_health_check_cases VALUES ('case-1',?,'ready_for_review')", (CUSTOMER_UID,))
        conn.execute("INSERT INTO vip_health_check_source_refs VALUES ('case-1','log-1',3,?)", (source_hash,))
        conn.execute(
            "INSERT INTO food_catalog VALUES ('food-1',?,'user_meal_photo',?)",
            (CUSTOMER_UID, image_ref),
        )
        conn.execute("INSERT INTO food_logs VALUES ('log-1',?,'food-1',3,?,?, 'confirmed','','','', '{}')", (CUSTOMER_UID, nutrition, image_ref))
        conn.execute("INSERT INTO pending_meal_photo_drafts VALUES ('draft-1',?,?,'approved',2,'expert_review_v1','', 'log-1','', '')", (CUSTOMER_UID, image_ref))
    return source_hash


def test_real_database_legacy_expert_source_loads_bounded_preview(tmp_path):
    from dietitian_health_check_api import load_health_check_image

    root = tmp_path / "nutrition_images"
    root.mkdir()
    image_ref = "nutrition-image:" + "e" * 32 + ".png"
    (root / ("e" * 32 + ".png")).write_bytes(_png())
    db = tmp_path / "health.db"
    _health_db(db, image_ref)
    with sqlite3.connect(db) as conn:
        preview = load_health_check_image(conn, case_id="case-1", log_id="log-1", image_root=root)
    assert preview is not None
    assert preview.media_type == "image/jpeg"


def test_joint_log_and_draft_ref_replacement_cannot_load_foreign_catalog_photo(tmp_path):
    from dietitian_health_check_api import load_health_check_image

    root = tmp_path / "nutrition_images"
    root.mkdir()
    owned_ref = "nutrition-image:" + "e" * 32 + ".png"
    foreign_ref = "nutrition-image:" + "7" * 32 + ".png"
    (root / ("e" * 32 + ".png")).write_bytes(_png(color="red"))
    (root / ("7" * 32 + ".png")).write_bytes(_png(color="blue"))
    db = tmp_path / "health.db"
    _health_db(db, owned_ref)

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO food_catalog VALUES ('food-foreign',?,'user_meal_photo',?)",
            (OTHER_UID, foreign_ref),
        )
        conn.execute(
            "UPDATE food_logs SET food_id='food-foreign',source_image_ref=? WHERE log_id='log-1'",
            (foreign_ref,),
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET source_image_ref=? WHERE token='draft-1'",
            (foreign_ref,),
        )
        assert load_health_check_image(
            conn, case_id="case-1", log_id="log-1", image_root=root
        ) is None


def test_real_database_user_confirmed_source_loads_bounded_preview(tmp_path):
    from dietitian_health_check_api import load_health_check_image
    from meal_photo_system import (
        apply_meal_photo_action,
        get_meal_photo_draft,
        save_meal_photo_draft,
    )

    root = tmp_path / "nutrition_images"
    root.mkdir()
    filename = "9" * 32 + ".jpg"
    image_ref = "nutrition-image:" + filename
    (root / filename).write_bytes(_jpeg())
    db = tmp_path / "health.db"
    with sqlite3.connect(db) as conn:
        token = save_meal_photo_draft(
            conn,
            user_id=CUSTOMER_UID,
            source_message_id="PHOTO-CONFIRMED-1",
            payload={
                "status": "success",
                "image_type": "food_photo",
                "visible_items": [
                    {"name": "高麗菜", "category": "vegetable", "confidence": 0.98}
                ],
                "uncertain_items": [],
                "starch_visibility": "not_visible",
                "oil_sauce_status": "unknown",
                "observed_at": "2026-09-12T12:00:00+08:00",
                "observed_at_confidence": 0.99,
            },
            source_image_ref=image_ref,
            meal_slot="午餐",
            consumed_at="2026-09-12T12:00:00+08:00",
        )
        answers = {
            "scope": "visible_only",
            "protein_type": "chicken",
            "protein_portion": "one_palm",
            "protein_more": "done",
            "starch_portion": "none",
            "vegetable_portion": "two_bowl",
            "cooking_oil": "light",
            "sauce_level": "half",
        }
        for index, (field, value) in enumerate(answers.items(), start=1):
            draft = get_meal_photo_draft(conn, user_id=CUSTOMER_UID, token=token)
            apply_meal_photo_action(
                conn,
                event_id=f"ANSWER-{index}",
                user_id=CUSTOMER_UID,
                token=token,
                expected_version=draft["version"],
                action="answer",
                field=field,
                value=value,
            )
        draft = get_meal_photo_draft(conn, user_id=CUSTOMER_UID, token=token)
        confirmed = apply_meal_photo_action(
            conn,
            event_id="CONFIRM-1",
            user_id=CUSTOMER_UID,
            token=token,
            expected_version=draft["version"],
            action="confirm_estimate",
        )
        log_id = confirmed["result"]["log_id"]
        conn.execute("ALTER TABLE food_logs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE food_logs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        from nutrition_system import user_confirmed_meal_photo_estimate_is_valid
        assert user_confirmed_meal_photo_estimate_is_valid(conn, log_id)
        version, nutrition, trust_hash = conn.execute(
            "SELECT version,nutrition_snapshot_json,trust_hash FROM food_logs WHERE log_id=?",
            (log_id,),
        ).fetchone()
        source_hash = hashlib.sha256(
            f"{log_id}:{version}:{nutrition}:user_confirmed_ai_estimate:{trust_hash}".encode()
        ).hexdigest()
        conn.executescript("""
            CREATE TABLE vip_health_check_cases(case_id TEXT, user_id TEXT, status TEXT);
            CREATE TABLE vip_health_check_source_refs(
              case_id TEXT, food_log_id TEXT, food_log_version INTEGER, source_hash TEXT
            );
        """)
        conn.execute(
            "INSERT INTO vip_health_check_cases VALUES ('case-confirmed',?,'ready_for_review')",
            (CUSTOMER_UID,),
        )
        conn.execute(
            "INSERT INTO vip_health_check_source_refs VALUES ('case-confirmed',?,?,?)",
            (log_id, version, source_hash),
        )
        preview = load_health_check_image(
            conn, case_id="case-confirmed", log_id=log_id, image_root=root
        )

    assert preview is not None
    assert preview.media_type == "image/jpeg"
    assert preview.data.startswith(b"\xff\xd8\xff")


@pytest.mark.parametrize("mutation", [
    "UPDATE vip_health_check_cases SET status='delivered'",
    "UPDATE vip_health_check_cases SET user_id='U22222222222222222222222222222222'",
    "UPDATE food_logs SET version=4",
    "UPDATE food_logs SET source_image_ref=''",
    "UPDATE food_logs SET deleted_at='now'",
    "UPDATE vip_health_check_source_refs SET source_hash='" + "f" * 64 + "'",
    "INSERT INTO vip_health_check_source_refs SELECT * FROM vip_health_check_source_refs",
    "UPDATE pending_meal_photo_drafts SET approved_log_id='other'",
    "UPDATE food_catalog SET owner_user_id='U22222222222222222222222222222222'",
    "UPDATE food_catalog SET original_image_ref='nutrition-image:" + "7" * 32 + ".png'",
    "UPDATE food_catalog SET source_type='user_private_food'",
])
def test_database_binding_tamper_and_terminal_status_are_generic_missing(tmp_path, mutation):
    from dietitian_health_check_api import load_health_check_image

    root = tmp_path / "nutrition_images"
    root.mkdir()
    image_ref = "nutrition-image:" + "e" * 32 + ".png"
    (root / ("e" * 32 + ".png")).write_bytes(_png())
    db = tmp_path / "health.db"
    _health_db(db, image_ref)
    with sqlite3.connect(db) as conn:
        conn.execute(mutation)
        assert load_health_check_image(conn, case_id="case-1", log_id="log-1", image_root=root) is None
