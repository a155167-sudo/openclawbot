import os
import sqlite3

from PIL import Image

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test-secret")

import server


CASE_ID = "case-photo"
LOG_ID = "log-photo"
OWNER_UID = "U11111111111111111111111111111111"
IMAGE_REF = "nutrition-image:" + "a" * 32 + ".jpg"
OTHER_IMAGE_REF = "nutrition-image:" + "b" * 32 + ".jpg"
NUTRITION_JSON = '{"calories_kcal":372}'


def _seed(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE vip_health_check_cases (
                case_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE vip_health_check_source_refs (
                case_id TEXT NOT NULL,
                food_log_id TEXT NOT NULL,
                food_log_version INTEGER NOT NULL,
                source_hash TEXT NOT NULL
            );
            CREATE TABLE food_logs (
                log_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                nutrition_snapshot_json TEXT NOT NULL,
                source_image_ref TEXT NOT NULL,
                confirmation_status TEXT NOT NULL,
                deleted_at TEXT NOT NULL,
                version INTEGER NOT NULL
            );
            CREATE TABLE pending_meal_photo_drafts (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                source_image_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                approved_log_id TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO vip_health_check_cases VALUES (?,?,'collecting')",
            (CASE_ID, OWNER_UID),
        )
        conn.execute(
            "INSERT INTO vip_health_check_source_refs VALUES (?,?,1,?)",
            (
                CASE_ID,
                LOG_ID,
                server.canonical_food_log_source_hash(LOG_ID, 1, NUTRITION_JSON),
            ),
        )
        conn.execute(
            "INSERT INTO food_logs VALUES (?,?,?,?, 'confirmed','',1)",
            (LOG_ID, OWNER_UID, NUTRITION_JSON, IMAGE_REF),
        )
        conn.execute(
            "INSERT INTO pending_meal_photo_drafts VALUES ('draft-photo',?,?, 'approved',?)",
            (OWNER_UID, IMAGE_REF, LOG_ID),
        )


def test_dietitian_photo_loader_is_case_owner_and_approval_bound(tmp_path, monkeypatch):
    db = tmp_path / "health-photo.db"
    image_dir = tmp_path / "nutrition_images"
    image_dir.mkdir()
    image_path = image_dir / ("a" * 32 + ".jpg")
    Image.new("RGB", (40, 30), "orange").save(image_path, format="JPEG")
    _seed(db)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))

    loaded = server.get_dietitian_health_check_photo(CASE_ID, LOG_ID)
    assert loaded is not None
    content, media_type = loaded
    assert content.startswith(b"\xff\xd8\xff")
    assert 100 <= len(content) <= 1024 * 1024
    assert media_type == "image/jpeg"

    assert server.get_dietitian_health_check_photo("other-case", LOG_ID) is None
    assert server.get_dietitian_health_check_photo(CASE_ID, "other-log") is None

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE food_logs SET version=2 WHERE log_id=?", (LOG_ID,))
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE food_logs SET version=1 WHERE log_id=?", (LOG_ID,))
        conn.execute(
            "UPDATE vip_health_check_source_refs SET source_hash=? WHERE food_log_id=?",
            ("0" * 64, LOG_ID),
        )
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE vip_health_check_source_refs SET source_hash=? WHERE food_log_id=?",
            (
                server.canonical_food_log_source_hash(LOG_ID, 1, NUTRITION_JSON),
                LOG_ID,
            ),
        )
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET source_image_ref=? WHERE token='draft-photo'",
            (OTHER_IMAGE_REF,),
        )
    Image.new("RGB", (40, 30), "blue").save(
        image_dir / ("b" * 32 + ".jpg"), format="JPEG"
    )
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET source_image_ref=? WHERE token='draft-photo'",
            (IMAGE_REF,),
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='rejected' WHERE token='draft-photo'"
        )
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET status='approved' WHERE token='draft-photo'"
        )
        conn.execute(
            "UPDATE vip_health_check_cases SET status='delivered' WHERE case_id=?",
            (CASE_ID,),
        )
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None


def test_dietitian_photo_loader_fails_closed_for_missing_or_cross_owner_image(tmp_path, monkeypatch):
    db = tmp_path / "health-photo.db"
    _seed(db)
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))

    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None

    image_dir = tmp_path / "nutrition_images"
    outside_dir = tmp_path / "outside-images"
    outside_dir.mkdir()
    Image.new("RGB", (40, 30), "purple").save(
        outside_dir / ("a" * 32 + ".jpg"), format="JPEG"
    )
    image_dir.symlink_to(outside_dir, target_is_directory=True)
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None
    image_dir.unlink()
    image_dir.mkdir()
    image_path = image_dir / ("a" * 32 + ".jpg")
    outside_image = tmp_path / "outside.jpg"
    Image.new("RGB", (40, 30), "orange").save(outside_image, format="JPEG")
    image_path.symlink_to(outside_image)
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None
    image_path.unlink()
    Image.new("RGB", (40, 30), "orange").save(image_path, format="JPEG")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_meal_photo_drafts SET user_id=? WHERE token='draft-photo'",
            ("U22222222222222222222222222222222",),
        )
    assert server.get_dietitian_health_check_photo(CASE_ID, LOG_ID) is None
