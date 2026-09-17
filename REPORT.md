# 文字記餐／成功後儀表板隔離修正報告

## 本機審查範圍

- Worktree：`/home/win-xi/text-meal-dashboard-audit`
- 基準 commit：`de5a4a1dec49578a7846c4674589973a81ce1c76`
- 範圍：只處理五組回歸中的兩項既有失敗；未擴增功能、未部署、未操作 remote／Google／LINE／外部資料庫。
- 測試環境：`/tmp/text-meal-regression-venv/bin/python`，本機 temporary SQLite DB。

## 修復前檢查點

- 指令：`/tmp/text-meal-regression-venv/bin/python -m pytest -q tests/test_server_nutrition_integration.py tests/test_nutrition_system.py tests/test_meal_photo_system.py tests/test_meal_photo_revision_handlers.py tests/test_food_log_revisions.py`
- 結果：`581 passed, 2 failed, 156213 warnings in 11.99s`
- 原始輸出：`/tmp/text-meal-parent-test.log`
- 修復前 SHA-256：
  - `server.py`：`860867d0893f9bb99b4107f506233902749039fdec701317b1f8494ae0bb2287`
  - `tests/test_server_nutrition_integration.py`：`b630e7d8bf15436b2a2cc56c54d36804dd92df88c26577f66fa5c5341a114a63`

## 兩項根因與最小修復

1. `test_legacy_recent_adjustments_update_the_linked_ledger_log`
   - 根因：`init_db()` 現已建立 `recent_meal_logs` 與 `frequent_foods`，legacy fixture 再次無條件 `CREATE TABLE`，在測試安排資料前即碰撞。
   - 修復：fixture 兩處改為 `CREATE TABLE IF NOT EXISTS`；保留既有 runtime schema、斷言與行為，不 drop 核心表。
2. `test_natural_food_log_uses_private_exact_match_scales_ml_and_replays_once`
   - 根因：canonical ledger 正確保存 `202.88 kcal / 9.7067 g`，但 dashboard projection 將四位小數原樣顯示，與人類可讀的一位小數契約 `202.9 kcal / 9.7 g` 不符。
   - 修復：只在 `get_dashboard_data()` 的 dashboard daily totals 投影四捨五入至一位小數；DB、health profile 與 ledger 快照仍保留完整精度。通用 `display_number()` 維持原本不截斷有意義小數，避免大型數值回歸。

## 驗證結果

### 兩項精準回歸

- 指令：`/tmp/text-meal-regression-venv/bin/python -m pytest -q tests/test_server_nutrition_integration.py::test_legacy_recent_adjustments_update_the_linked_ledger_log tests/test_server_nutrition_integration.py::test_natural_food_log_uses_private_exact_match_scales_ml_and_replays_once`
- 結果：`2 passed, 3762 warnings in 2.78s`

### 修復後關聯精準回歸

- 另加入大型 dashboard 數值完整顯示測試，確認投影修正沒有截斷通用 formatter。
- 結果：`3 passed, 6572 warnings in 2.80s`

### 五組要求回歸

- 指令：`/tmp/text-meal-regression-venv/bin/python -m pytest -q tests/test_server_nutrition_integration.py tests/test_nutrition_system.py tests/test_meal_photo_system.py tests/test_meal_photo_revision_handlers.py tests/test_food_log_revisions.py`
- 結果：`583 passed, 157161 warnings in 11.36s`（shell wall time 13 秒，exit 0）
- 完整輸出：`/tmp/text-meal-five-groups-final.log`

### 完整測試套件

- 指令：`/tmp/text-meal-regression-venv/bin/python -m pytest -q`
- 結果：`1056 passed, 157321 warnings in 32.94s`（shell wall time 34 秒，exit 0）
- 完整輸出：`/tmp/text-meal-full-suite-final.log`
- `git diff --check`：通過。

## 修復後檔案 SHA-256（commit 前內容）

- `server.py`：`cf4692b3b4b0eb8940a2b043ecc7f60b009cfe92e79bbd8244eeb9e5893c6897`
- `tests/test_server_nutrition_integration.py`：`9cf7955d510fd4d8663def3e7a42efd0f98de3f250118173e6faf76b1aa24cdb`

## 尚存事項

- 測試全數通過；僅保留既有 LINE SDK deprecated warnings，未在本次窄幅修復中擴大處理。
- 未部署。
