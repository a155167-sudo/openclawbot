from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from dietitian_health_check_staging import create_app
from scripts.build_dietitian_staging_fixture import build_fixture

CHANNEL_ID = "2009251085"
ALLOWED_UID = "U1234567890abcdef1234567890abcdef"
OTHER_UID = "Uabcdef1234567890abcdef1234567890"


def _environment(path: Path, digest: str) -> dict[str, str]:
    return {
        "DIETITIAN_HEALTH_CHECK_READ_ENABLED": "true",
        "DIETITIAN_HEALTH_CHECK_LIFF_ID": CHANNEL_ID + "-dietitianCheck",
        "DIETITIAN_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
        "DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS": ALLOWED_UID,
        "DIETITIAN_HEALTH_CHECK_DB_PATH": "staging-data/deidentified.db",
        "DIETITIAN_HEALTH_CHECK_DB_SHA256": digest,
    }


def _fixture(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "staging-data" / "deidentified.db"
    digest = build_fixture(path)
    return path, digest


def test_staging_adapter_refuses_disabled_or_hash_mismatched_fixture(tmp_path):
    path, digest = _fixture(tmp_path)
    disabled = _environment(path, digest)
    disabled["DIETITIAN_HEALTH_CHECK_READ_ENABLED"] = "false"
    with pytest.raises(RuntimeError, match="explicitly enabled"):
        create_app(disabled, safe_root=tmp_path)

    mismatched = _environment(path, "0" * 64)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        create_app(mismatched, safe_root=tmp_path)


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


def test_staging_adapter_authentication_and_method_boundaries_are_fail_closed(tmp_path):
    path, digest = _fixture(tmp_path)

    def verifier(token: str, *, channel_id: str) -> str:
        assert channel_id == CHANNEL_ID
        return ALLOWED_UID if token == "allowed" else OTHER_UID

    client = TestClient(
        create_app(
            _environment(path, digest), token_verifier=verifier, safe_root=tmp_path
        )
    )
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


def test_authorized_reads_use_deidentified_fixture_without_mutating_it(tmp_path):
    path, digest = _fixture(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == digest

    client = TestClient(
        create_app(
            _environment(path, digest),
            token_verifier=lambda _token, *, channel_id: ALLOWED_UID,
            safe_root=tmp_path,
        )
    )
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
    assert payload["source_integrity"]["all_snapshots_available"] is True
    assert payload["latest_review_fresh"] is True
    serialized = detail.text
    for prohibited in (
        "fixture-activation",
        "fixture-event",
        "U11111111111111111111111111111111",
    ):
        assert prohibited not in serialized

    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert after == before


def test_health_and_liff_shell_disclose_no_identity_and_are_no_store(tmp_path):
    path, digest = _fixture(tmp_path)
    client = TestClient(
        create_app(
            _environment(path, digest),
            token_verifier=lambda _token, *, channel_id: ALLOWED_UID,
            safe_root=tmp_path,
        )
    )
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
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
    assert "credentials: 'omit'" in script.text
    assert CHANNEL_ID + "-dietitianCheck" in script.text
    for prohibited in (ALLOWED_UID, OTHER_UID, "U11111111111111111111111111111111"):
        assert prohibited not in page.text
        assert prohibited not in script.text
