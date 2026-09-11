from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from dietitian_health_check_staging import create_app
from scripts.build_dietitian_staging_fixture import build_fixture

CHANNEL_ID = "2009251085"
ALLOWED_UID = "U1234567890abcdef1234567890abcdef"
OTHER_UID = "Uabcdef1234567890abcdef1234567890"


def _environment(path: Path, database_digest: str) -> dict[str, str]:
    image_digest = hashlib.sha256((path.parent / "sample-meal.jpg").read_bytes()).hexdigest()
    return {
        "DIETITIAN_HEALTH_CHECK_READ_ENABLED": "true",
        "DIETITIAN_HEALTH_CHECK_LIFF_ID": CHANNEL_ID + "-dietitianCheck",
        "DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
        "DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS": ALLOWED_UID,
        "DIETITIAN_HEALTH_CHECK_DB_PATH": "staging-data/deidentified.db",
        "DIETITIAN_HEALTH_CHECK_DB_SHA256": database_digest,
        "DIETITIAN_HEALTH_CHECK_IMAGE_SHA256": image_digest,
    }


def _fixture(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "staging-data" / "deidentified.db"
    digest = build_fixture(path)
    return path, digest


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    path, digest = _fixture(tmp_path)

    def verifier(token: str, *, channel_id: str) -> str:
        assert channel_id == CHANNEL_ID
        return ALLOWED_UID if token == "allowed" else OTHER_UID

    return TestClient(
        create_app(
            _environment(path, digest), token_verifier=verifier, safe_root=tmp_path
        )
    ), path


def test_staging_adapter_refuses_disabled_or_hash_mismatched_assets(tmp_path):
    path, digest = _fixture(tmp_path)
    disabled = _environment(path, digest)
    disabled["DIETITIAN_HEALTH_CHECK_READ_ENABLED"] = "false"
    with pytest.raises(RuntimeError, match="explicitly enabled"):
        create_app(disabled, safe_root=tmp_path)

    mismatched_database = _environment(path, "0" * 64)
    with pytest.raises(RuntimeError, match="fixture hash mismatch"):
        create_app(mismatched_database, safe_root=tmp_path)

    mismatched_image = _environment(path, digest)
    mismatched_image["DIETITIAN_HEALTH_CHECK_IMAGE_SHA256"] = "0" * 64
    with pytest.raises(RuntimeError, match="image hash mismatch"):
        create_app(mismatched_image, safe_root=tmp_path)


def test_staging_adapter_rejects_external_paths_and_symlinks(tmp_path):
    path, digest = _fixture(tmp_path)
    for raw_path in (str(path), "../staging-data/deidentified.db", "deidentified.db"):
        environment = _environment(path, digest)
        environment["DIETITIAN_HEALTH_CHECK_DB_PATH"] = raw_path
        with pytest.raises(RuntimeError, match="must be staging-data/deidentified.db"):
            create_app(environment, safe_root=tmp_path)

    external_root = tmp_path / "external"
    external_path = external_root / "deidentified.db"
    external_digest = build_fixture(external_path)
    symlink_root = tmp_path / "symlink-root"
    symlink_root.mkdir()
    (symlink_root / "staging-data").symlink_to(external_root, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinks are forbidden"):
        create_app(
            _environment(external_path, external_digest), safe_root=symlink_root
        )


def test_staging_authentication_and_method_boundaries_fail_closed(tmp_path):
    client, _path = _client(tmp_path)
    endpoint = "/api/dietitian/health-checks"

    unauthenticated = client.get(endpoint)
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["cache-control"] == "no-store"

    forbidden = client.get(endpoint, headers={"Authorization": "Bearer other"})
    assert forbidden.status_code == 403

    for method in ("post", "put", "patch", "delete", "options"):
        response = getattr(client, method)(endpoint)
        assert response.status_code == 405
        assert response.headers["cache-control"] == "no-store"


def test_authorized_reads_show_v2_ranges_na_and_photo_without_mutation(tmp_path):
    client, path = _client(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    headers = {"Authorization": "Bearer allowed"}

    listing = client.get(
        "/api/dietitian/health-checks?status=ready_for_review&limit=25&offset=0",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    assert listing.json()["items"][0]["profile"]["name"] == "測試個案甲"

    detail = client.get("/api/dietitian/health-checks/sample-case-1", headers=headers)
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["source_integrity"] == {
        "referenced_count": 1,
        "available_snapshot_count": 1,
        "all_snapshots_available": True,
    }
    source = payload["source_logs"][0]
    assert source["source_type"] == "user_meal_photo"
    assert source["verification_status"] == "user_confirmed_ai_estimate"
    assert source["nutrition_snapshot"]["calories_kcal"] is None
    assert source["nutrition_snapshot"]["protein_g"] is None
    assert source["customer_estimate"] == {
        "protein_total_exchange": {
            "min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1"
        },
        "starch_exchange": {
            "min": 1.0, "max": 2.0, "basis": "hand_portion_range_v1"
        },
        "vegetable_exchange": {
            "min": 1.0, "max": 1.5, "basis": "hand_portion_range_v1"
        },
    }
    serialized = detail.text
    for prohibited in (
        "fixture-activation",
        "fixture-event",
        "U11111111111111111111111111111111",
        "nutrition-image:",
        "sample-meal.jpg",
    ):
        assert prohibited not in serialized

    photo_url = "/api/dietitian/health-checks/sample-case-1/photos/sample-log-1"
    denied_photo = client.get(photo_url)
    assert denied_photo.status_code == 401
    wrong_photo = client.get(
        "/api/dietitian/health-checks/sample-case-1/photos/other-log",
        headers=headers,
    )
    assert wrong_photo.status_code == 404
    photo = client.get(photo_url, headers=headers)
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["cache-control"] == "no-store"
    assert photo.content.startswith(b"\xff\xd8\xff")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_health_and_liff_shell_are_no_store_and_fetch_photos_with_bearer(tmp_path):
    client, _path = _client(tmp_path)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ok",
        "service": "dietitian-health-check-read-staging",
        "dataset": "deidentified-fixture",
        "writes_enabled": False,
    }

    page = client.get("/")
    script = client.get("/staging-app.js")
    assert page.status_code == script.status_code == 200
    assert page.headers["cache-control"] == script.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "img-src blob:" in page.headers["content-security-policy"]
    assert "/photos/" in script.text
    assert "Authorization: authorization" in script.text
    assert "URL.createObjectURL" in script.text
    assert "URL.revokeObjectURL" in script.text
    assert "AbortController" in script.text
    assert "pagehide" in script.text
    assert "signal: controller.signal" in script.text
    assert "let pageActive = true" in script.text
    assert script.text.index("if (!pageActive || controller.signal.aborted) return") < script.text.index(
        "URL.createObjectURL(photoBlob)"
    )
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
    assert "credentials: 'omit'" in script.text
    assert CHANNEL_ID + "-dietitianCheck" in script.text
    for prohibited in (ALLOWED_UID, OTHER_UID, "U11111111111111111111111111111111"):
        assert prohibited not in page.text
        assert prohibited not in script.text


def test_bundle_cli_is_cwd_independent_minimal_and_factory_bootable(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "railway-bundle"
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "build_dietitian_staging_bundle.py"),
            str(bundle),
        ],
        cwd="/",
        check=True,
        capture_output=True,
        text=True,
    )
    manifest = json.loads(completed.stdout)
    assert manifest["artifact_kind"] == "dietitian-health-check-read-staging"
    assert manifest["contains_real_customer_data"] is False
    actual_files = {
        str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file()
    }
    assert actual_files == {
        "artifact-manifest.json",
        "dietitian_health_check_api.py",
        "dietitian_health_check_staging.py",
        "requirements.txt",
        "railway.toml",
        "staging-data/deidentified.db",
        "staging-data/sample-meal.jpg",
        "vip_health_check.py",
    }
    assert "server:app" not in (bundle / "railway.toml").read_text()
    assert "dietitian_health_check_staging:create_app --factory" in (
        bundle / "railway.toml"
    ).read_text()
    assert not any(
        path.name.startswith(".env") or "google_key" in path.name
        for path in bundle.rglob("*")
    )

    environment = {
        **os.environ,
        "PYTHONPATH": str(bundle),
        "DIETITIAN_HEALTH_CHECK_READ_ENABLED": "true",
        "DIETITIAN_HEALTH_CHECK_LIFF_ID": CHANNEL_ID + "-dietitianCheck",
        "DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
        "DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS": ALLOWED_UID,
        "DIETITIAN_HEALTH_CHECK_DB_PATH": "staging-data/deidentified.db",
        "DIETITIAN_HEALTH_CHECK_DB_SHA256": str(manifest["database_sha256"]),
        "DIETITIAN_HEALTH_CHECK_IMAGE_SHA256": str(manifest["image_sha256"]),
    }
    boot = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dietitian_health_check_staging import create_app; "
            "app=create_app(); print(app.title)",
        ],
        cwd=bundle,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "唯讀 Staging" in boot.stdout
