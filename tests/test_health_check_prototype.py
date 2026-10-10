import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def demo_db(tmp_path: Path) -> Path:
    return tmp_path / "dietitian-demo.db"


def make_repository(demo_db: Path):
    from health_check_prototype.repository import HealthCheckRepository

    return HealthCheckRepository(demo_db, safe_root=demo_db.parent)


def make_app(demo_db: Path):
    from health_check_prototype.app import create_app

    return create_app(demo_db, safe_root=demo_db.parent)


def test_repository_seeds_three_pending_cases_sorted_oldest_first(demo_db):
    repo = make_repository(demo_db)
    repo.initialize_demo_data()

    cases = repo.list_cases("pending")

    assert [case["customer_name"] for case in cases] == ["陳○○", "林○○", "王○○"]
    assert [case["waiting_hours"] for case in cases] == [18, 6, 2]
    assert all(case["is_demo"] is True for case in cases)


def test_custom_database_requires_explicit_safe_root(demo_db):
    from health_check_prototype.app import create_app

    with pytest.raises(ValueError, match="safe_root"):
        create_app(demo_db)


def test_repository_rejects_production_filename_and_path_outside_safe_root(tmp_path):
    from health_check_prototype.repository import HealthCheckRepository

    with pytest.raises(ValueError, match="檔名"):
        HealthCheckRepository(tmp_path / "user_quota.db", safe_root=tmp_path)
    with pytest.raises(ValueError, match="安全目錄"):
        HealthCheckRepository(tmp_path.parent / "dietitian-demo.db", safe_root=tmp_path)


def test_repository_rejects_symlink_database(tmp_path):
    from health_check_prototype.repository import HealthCheckRepository

    target = tmp_path / "target.db"
    sqlite3.connect(target).close()
    link = tmp_path / "dietitian-demo.db"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="符號連結"):
        HealthCheckRepository(link, safe_root=tmp_path)


def test_repository_rejects_symlink_safe_root(tmp_path):
    from health_check_prototype.repository import HealthCheckRepository

    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(actual_root, target_is_directory=True)

    with pytest.raises(ValueError, match="安全目錄.*符號連結"):
        HealthCheckRepository(linked_root / "dietitian-demo.db", safe_root=linked_root)


def test_repository_rejects_existing_database_with_foreign_tables(demo_db):
    from health_check_prototype.repository import HealthCheckRepository

    with sqlite3.connect(demo_db) as connection:
        connection.execute("CREATE TABLE usage (user_id TEXT)")

    repo = HealthCheckRepository(demo_db, safe_root=demo_db.parent)
    with pytest.raises(ValueError, match="非示範資料表"):
        repo.initialize_demo_data()


def test_readonly_api_lists_cases_and_returns_distinct_full_details(demo_db):
    client = TestClient(make_app(demo_db))

    listing = client.get("/api/health-checks", params={"status": "pending"})
    chen = client.get("/api/health-checks/demo-chen").json()
    lin = client.get("/api/health-checks/demo-lin").json()

    assert listing.status_code == 200
    assert listing.json()["count"] == 3
    assert listing.json()["items"][0]["case_id"] == "demo-chen"
    assert len(chen["detail"]["days"]) == 3
    assert len(lin["detail"]["days"]) == 3
    assert chen["detail"]["days"] != lin["detail"]["days"]
    assert chen["detail"]["draft"]["action"] != lin["detail"]["draft"]["action"]
    assert "飯糰" not in str(lin["detail"])


def test_readonly_api_rejects_invalid_status_and_all_write_methods(demo_db):
    client = TestClient(make_app(demo_db))

    assert client.get("/api/health-checks", params={"status": "unknown"}).status_code == 422
    assert client.get("/api/health-checks/not-found").status_code == 404
    for method in ("post", "put", "patch", "delete", "options"):
        response = getattr(client, method)("/api/health-checks/demo-chen/approve")
        assert response.status_code == 405
        assert response.headers["allow"] == "GET"


def test_root_serves_api_enabled_interactive_prototype(demo_db):
    client = TestClient(make_app(demo_db))

    response = client.get("/")

    assert response.status_code == 200
    assert "營養師工作台" in response.text
    assert 'data-api-mode="readonly"' in response.text
    assert "/api/health-checks" in response.text


def test_frontend_has_only_readonly_fetches_and_resets_detail_before_switching():
    prototype = Path("prototypes/dietitian-3day-checkup-prototype.html").read_text(
        encoding="utf-8"
    )

    assert prototype.count("fetch(") == 1
    assert "function readonlyFetch(url)" in prototype
    assert "fetch(url,{method:'GET',credentials:'same-origin'})" in prototype
    assert "method: 'POST'" not in prototype
    assert 'method: "POST"' not in prototype
    assert "resetDetailForCase(currentCase)" in prototype
    assert "function escapeHtml(value)" in prototype
    for api_value in ("c.name", "c.goal", "c.wait", "c.days", "c.complete", "c.insight", "m[0]", "m[1]", "m[2]", "m.label", "m.value"):
        assert f"escapeHtml({api_value})" in prototype


def test_importing_prototype_app_does_not_import_server(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd())
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import health_check_prototype.app; "
            "raise SystemExit(1 if 'server' in sys.modules else 0)",
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_default_demo_database_is_outside_production_data_paths():
    from health_check_prototype.app import DEFAULT_DEMO_DB

    normalized = str(DEFAULT_DEMO_DB.resolve())
    assert DEFAULT_DEMO_DB.name == "dietitian-demo.db"
    assert "user_quota.db" not in normalized
    assert "/openclawbot/data/" not in normalized
    assert "/health_check_prototype/data/" not in normalized


def test_environment_cannot_override_default_demo_database(monkeypatch, tmp_path):
    from health_check_prototype.app import DEFAULT_DEMO_DB, create_app

    unsafe = tmp_path / "user_quota.db"
    monkeypatch.setenv("DIETITIAN_PROTOTYPE_DB", str(unsafe))
    app = create_app()

    assert Path(app.state.prototype_db_path) == DEFAULT_DEMO_DB
    assert not unsafe.exists()
