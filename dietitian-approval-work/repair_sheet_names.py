import os
import json
import sqlite3
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials

# =========================
# 基本設定
# =========================
DB_PATH = "data/user_quota.db"
SPREADSHEET_ID = "1webSlOkY0OwpY-9_HxxNKowLMoChGaWNlIpUVyJluiQ"
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# =========================
# 如果你已經知道某些舊分頁 -> 新分頁，可填在這裡
# 沒有就先留空
# =========================
RENAME_MAP = {
    # "Jason_8530_20260318": "Jason_8530_20260508",
}

# =========================
# Google 認證
# =========================
creds_json = os.environ.get("GOOGLE_CREDENTIALS")
if not creds_json:
    raise RuntimeError("缺少 GOOGLE_CREDENTIALS，請先在環境變數設定完整 service account JSON")

info = json.loads(creds_json)
creds = Credentials.from_service_account_info(info, scopes=SCOPE)
gc = gspread.authorize(creds)

book = gc.open_by_key(SPREADSHEET_ID)
worksheet_titles = {ws.title for ws in book.worksheets()}

# =========================
# 抓 Master_API_View 裡有哪些 user_id
# 用來判斷「是否可重建個人分頁」
# =========================
master_users = set()
try:
    api_sheet = book.worksheet("Master_API_View")
    records = api_sheet.get_all_records()
    for r in records:
        uid = str(r.get("User_ID", "")).strip()
        if uid:
            master_users.add(uid)
except Exception as e:
    print(f"⚠️ 讀取 Master_API_View 失敗：{e}")

# =========================
# 讀 SQLite
# =========================
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
SELECT user_id, name, sheet_name
FROM health_profile
WHERE sheet_name IS NOT NULL
AND TRIM(sheet_name) != ''
""")
rows = c.fetchall()

invalid_rows = []
updated_count = 0

print("========================================")
print("開始檢查 health_profile.sheet_name 是否失效")
print("========================================")

for user_id, name, sheet_name in rows:
    user_id = str(user_id).strip()
    name = str(name or "").strip()
    sheet_name = str(sheet_name or "").strip()

    if sheet_name in worksheet_titles:
        # 正常存在
        continue

    invalid_rows.append((user_id, name, sheet_name))

print(f"共發現 {len(invalid_rows)} 筆失效 sheet_name")
print("")

# =========================
# 處理失效項目
# =========================
rebuild_candidates = []
manual_review = []

for user_id, name, old_sheet_name in invalid_rows:
    # Case 1: 有手動對應的新分頁名
    if old_sheet_name in RENAME_MAP:
        new_sheet_name = RENAME_MAP[old_sheet_name]

        if new_sheet_name in worksheet_titles:
            c.execute(
                "UPDATE health_profile SET sheet_name=? WHERE user_id=?",
                (new_sheet_name, user_id)
            )
            updated_count += 1
            print(f"✅ 已更新 sheet_name: {name} | {old_sheet_name} -> {new_sheet_name}")
        else:
            print(f"⚠️ RENAME_MAP 指向的新分頁不存在：{name} | {old_sheet_name} -> {new_sheet_name}")
            manual_review.append((user_id, name, old_sheet_name, "RENAME_MAP 指向的新分頁不存在"))
        continue

    # Case 2: 沒有新分頁，但 Master_API_View 還有此 user，可重建
    if user_id in master_users:
        rebuild_candidates.append((user_id, name, old_sheet_name))
        print(f"🛠 可重建個人分頁：{name} | user_id={user_id} | 舊分頁={old_sheet_name}")
        continue

    # Case 3: 沒新分頁、Master_API_View 也沒資料
    manual_review.append((user_id, name, old_sheet_name, "Master_API_View 無資料"))
    print(f"❌ 待人工處理：{name} | user_id={user_id} | 舊分頁={old_sheet_name} | 原因=Master_API_View 無資料")

# =========================
# 寫入更新
# =========================
conn.commit()

print("")
print("========================================")
print("處理結果摘要")
print("========================================")
print(f"已直接更新 sheet_name：{updated_count} 筆")
print(f"可重建個人分頁：{len(rebuild_candidates)} 筆")
print(f"待人工處理：{len(manual_review)} 筆")

print("")
print("====== 可重建名單 ======")
for user_id, name, old_sheet_name in rebuild_candidates:
    print(f"{name} | user_id={user_id} | old_sheet_name={old_sheet_name}")

print("")
print("====== 待人工處理名單 ======")
for user_id, name, old_sheet_name, reason in manual_review:
    print(f"{name} | user_id={user_id} | old_sheet_name={old_sheet_name} | {reason}")

conn.close()