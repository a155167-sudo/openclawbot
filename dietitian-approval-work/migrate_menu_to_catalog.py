#!/usr/bin/env python3
"""One-shot migration: load menu.csv items into food_catalog for quick logging."""
import csv, json, os, sqlite3, uuid, sys

DB_PATH = os.environ.get("DB_PATH", "data/user_quota.db")

def new_id(prefix="food"):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"

def main():
    csv_path = os.path.join(os.path.dirname(__file__), "menu.csv")
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found")
        sys.exit(1)

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Parsed {len(rows)} menu items from menu.csv")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Ensure food_catalog table exists
    c.execute("""CREATE TABLE IF NOT EXISTS food_catalog (
        food_id TEXT PRIMARY KEY, product_name TEXT, brand TEXT, barcode TEXT,
        source_type TEXT, owner_user_id TEXT, visibility TEXT,
        package_amount REAL, package_unit TEXT, servings_per_package REAL,
        per_serving_json TEXT, per_100_json TEXT,
        exchange_json TEXT, exchange_review_status TEXT,
        fingerprint TEXT, original_image_ref TEXT,
        recognition_confidence REAL, verification_status TEXT,
        created_at TEXT, updated_at TEXT
    )""")

    inserted = 0
    skipped = 0
    now = __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")

    for row in rows:
        name = row["品項"].strip()
        if not name:
            continue

        # Check if already exists (by product_name + source_type=label)
        existing = c.execute(
            "SELECT food_id FROM food_catalog WHERE product_name=? AND source_type='label'",
            (name,)
        ).fetchone()
        if existing:
            skipped += 1
            continue

        cal = float(row["熱量(kcal)"])
        pro = float(row["蛋白質(g)"])
        fat = float(row["脂肪(g)"])
        carb = float(row["碳水化合物(g)"])
        sugar = float(row["糖(g)"])
        sodium = float(row["鈉(mg)"])

        per_serving = {
            "calories_kcal": cal,
            "protein_g": pro,
            "fat_g": fat,
            "carbohydrate_g": carb,
            "sugar_g": sugar,
            "sodium_mg": sodium,
        }

        fid = new_id("menu")
        c.execute(
            """INSERT INTO food_catalog
               (food_id, product_name, brand, barcode, source_type, owner_user_id, visibility,
                package_amount, package_unit, servings_per_package, per_serving_json, per_100_json,
                exchange_json, exchange_review_status, fingerprint, original_image_ref,
                recognition_confidence, verification_status, created_at, updated_at)
               VALUES (?,?,'','','label','system','public',
                       1,'份',1,?,'{}',
                       '{}','approved',?,'',1.0,'auto',?,?)""",
            (fid, name, json.dumps(per_serving), fid, now, now),
        )
        inserted += 1
        print(f"  ✅ {name}: {cal}kcal P{pro}g F{fat}g C{carb}g")

    conn.commit()
    conn.close()
    print(f"\nDone! Inserted {inserted}, skipped {skipped} (already existed)")

if __name__ == "__main__":
    main()
