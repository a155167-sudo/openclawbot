"""Build deterministic, deidentified assets for read-only dietitian staging."""
from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from PIL import Image, ImageDraw

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vip_health_check import canonical_food_log_source_hash

CUSTOMER_UID = "U11111111111111111111111111111111"
IMAGE_REF = "nutrition-image:" + "a" * 32 + ".jpg"
NUTRITION = {
    "calories_kcal": None,
    "protein_g": None,
    "fat_g": None,
    "carbohydrate_g": None,
    "sugar_g": None,
    "fiber_g": None,
    "sodium_mg": None,
}
EXCHANGE = {
    "protein_total_exchange": {"min": 2.0, "max": 3.0, "basis": "hand_portion_range_v1"},
    "starch_exchange": {"min": 1.0, "max": 2.0, "basis": "hand_portion_range_v1"},
    "vegetable_exchange": {"min": 1.0, "max": 1.5, "basis": "hand_portion_range_v1"},
}
NUTRITION_JSON = json.dumps(
    NUTRITION, ensure_ascii=False, sort_keys=True, separators=(",", ":")
)
EXCHANGE_JSON = json.dumps(
    EXCHANGE, ensure_ascii=False, sort_keys=True, separators=(",", ":")
)
SOURCE_HASH = canonical_food_log_source_hash(
    "sample-log-1",
    1,
    NUTRITION_JSON,
    IMAGE_REF,
    exchange_snapshot_json=EXCHANGE_JSON,
    source_type="user_meal_photo",
    verification_status="user_confirmed_ai_estimate",
)
DAYS = (
    ("2026-09-01", "v1", 3, "qualified"),
    ("2026-09-02", "v1", 3, "qualified"),
    ("2026-09-03", "v1", 3, "qualified"),
)
MANIFEST_HASH = hashlib.sha256(
    "\n".join(
        [f"sample-log-1:1:{SOURCE_HASH}"]
        + [f"day:{day}:{count}:{rule}" for day, rule, count, _ in DAYS]
    ).encode()
).hexdigest()

SCHEMA = """
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
CREATE TABLE food_catalog (
 food_id TEXT PRIMARY KEY, product_name TEXT NOT NULL,
 source_type TEXT NOT NULL, verification_status TEXT NOT NULL
);
CREATE TABLE food_logs (
 log_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, food_id TEXT NOT NULL,
 consumed_at TEXT NOT NULL, meal_slot TEXT, consumed_servings REAL,
 consumed_amount REAL, consumed_unit TEXT, nutrition_snapshot_json TEXT NOT NULL,
 exchange_snapshot_json TEXT NOT NULL, approved_exchange_json TEXT NOT NULL,
 source_image_ref TEXT, confirmation_status TEXT NOT NULL, deleted_at TEXT NOT NULL,
 version INTEGER NOT NULL
);
"""


def _sample_image_bytes() -> bytes:
    image = Image.new("RGB", (480, 320), "#f3ead8")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((45, 35, 435, 285), radius=24, fill="#ffffff", outline="#315c45", width=5)
    draw.rectangle((70, 65, 200, 255), fill="#70a45b")
    draw.rectangle((205, 65, 320, 255), fill="#d7ab58")
    draw.rectangle((325, 65, 410, 255), fill="#a65f45")
    output = BytesIO()
    image.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue()


def build_fixture(destination: str | Path) -> str:
    path = Path(destination)
    image_path = path.parent / "sample-meal.jpg"
    if path.exists() or image_path.exists():
        raise FileExistsError(f"refusing to overwrite staging asset under: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO health_profile VALUES (?,?,?,?,?,?,?)",
                (CUSTOMER_UID, "測試個案甲", 2050, 95.0, "維持健康", "無", "二,四,六"),
            )
            connection.execute(
                """INSERT INTO vip_health_check_cases VALUES
                   (?,?, 'first_vip_baseline_check','fixture-activation','fixture-event',
                    '2026-09-01T00:00:00+08:00','2026-09-08T00:00:00+08:00',
                    'ready_for_review',3,?,'2026-09-04T08:00:00+08:00','',
                    '2026-09-01T00:00:00+08:00','2026-09-04T08:00:00+08:00')""",
                ("sample-case-1", CUSTOMER_UID, MANIFEST_HASH),
            )
            connection.executemany(
                "INSERT INTO vip_health_check_valid_days VALUES (?,?,?,?,?,?)",
                [
                    ("sample-case-1", day, rule, count, status, "2026-09-04T07:00:00+08:00")
                    for day, rule, count, status in DAYS
                ],
            )
            connection.execute(
                "INSERT INTO food_catalog VALUES (?,?,?,?)",
                (
                    "sample-food-1", "去識別顧客確認餐盒", "user_meal_photo",
                    "user_confirmed_ai_estimate",
                ),
            )
            connection.execute(
                """INSERT INTO food_logs VALUES
                   ('sample-log-1',?,'sample-food-1','2026-09-01T12:00:00+08:00',
                    'lunch',1,1,'meal',?,?, '{}',?, 'confirmed','',1)""",
                (CUSTOMER_UID, NUTRITION_JSON, EXCHANGE_JSON, IMAGE_REF),
            )
            connection.execute(
                "INSERT INTO vip_health_check_source_refs VALUES (?,?,?,?,?,?,?)",
                (
                    "sample-case-1", "sample-log-1", 1, "2026-09-01",
                    "confirmed_canonical_food_log_in_activation_window", SOURCE_HASH,
                    "2026-09-02T00:00:00+08:00",
                ),
            )
            connection.execute(
                """INSERT INTO vip_health_check_reviews VALUES
                   ('sample-review-1','sample-case-1',1,'draft',?,?,?,?,?,?,'',?,?)""",
                (
                    json.dumps({"observation": "三日飲食規律，蔬菜量可再增加"}, ensure_ascii=False),
                    json.dumps({"good": "蛋白質來源分配平均", "priority": "增加蔬菜"}, ensure_ascii=False),
                    json.dumps({"protein_g": 95, "fiber_g": 25}),
                    "僅為去識別測試資料，不可用於真實營養判斷",
                    MANIFEST_HASH,
                    "fixture-reviewer",
                    "2026-09-04T08:30:00+08:00",
                    "2026-09-04T08:30:00+08:00",
                ),
            )
            connection.execute("PRAGMA user_version=2")
            connection.commit()
            connection.execute("VACUUM")
        image_path.write_bytes(_sample_image_bytes())
        path.chmod(0o444)
        image_path.chmod(0o444)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        path.unlink(missing_ok=True)
        image_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_dietitian_staging_fixture.py OUTPUT.db")
    database = Path(sys.argv[1])
    database_hash = build_fixture(database)
    image_hash = hashlib.sha256((database.parent / "sample-meal.jpg").read_bytes()).hexdigest()
    print(json.dumps({"database_sha256": database_hash, "image_sha256": image_hash}))
