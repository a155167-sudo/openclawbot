# REPORT — TIMELINE UI

## 隔離範圍
- 工作副本：`/home/win-xi/timeline-ui-work`
- 唯讀來源完整複製（含既有 uncommitted）；未修改來源 manifest。
- 未 commit、未 push、未 deploy、未連外或存取真實 LINE／Google／AI／正式 DB。

## 可執行 checkpoint
- 日期 tabs 直接取自 API `source_logs[].local_date`，嚴格驗證實際曆日並排序；不固定三天、不取今天，四個以上記錄日也全部保留。
- 僅在同筆來源同時具有合法 `local_date` 與 server contract 的 `normalized_meal_slot` 時分組。
- `unspecified` 顯示「餐別未提供」；其他 server 餐別文字原樣以 `textContent` 顯示。
- legacy、缺單一 binding 欄位、非法日期均保留在獨立「日期／餐別尚無可驗證資料」區；不借 `valid_days` 推斷、不重算營養、不假造食物描述。
- 同一 `log_id + food_log_version` 只顯示一次。
- 卡片沿用 API 真營養快照／顧客確認 AI 估算與既有受保護照片 route；照片不預載。
- 切日期會 abort 並 revoke 舊照片 ownership；late blob 不得建立 URL 或污染新日期。既有切案、返回、refresh、pagehide、timeout/fence 行為保留。

## 驗證結果
- RED：timeline 行為測試先失敗（日期 tabs 為空；HTML 缺 timeline 容器）。
- GREEN：`tests/test_dietitian_health_check_liff.py`：`5 passed`（30 秒 timeout；Node `--check` 與真 route JS harness 均執行）。
- 最終 focused LIFF/API/image：`96 passed, 1 warning in 1.17s`（30 秒 timeout）。warning 為既有 Starlette `httpx` deprecation。
- `git diff --check`：PASS。
- 授權三檔相對 copy source 的 unified delta SHA-256：`f9d5144610d7aad9443421f2d7014fa7e3be9e8fd09116bcc66bd8d0de95753f`（28,976 bytes）。
- backend 與 backend tests 均逐檔 `cmp` 為 `UNCHANGED`；staged diff 仍為空（SHA-256 `e3b0c442…b855`）。
- 依「browser 太長先交測試成果」界線，本 checkpoint 未啟動瀏覽器或建立合成截圖。

## 修改檔案
- `dietitian_health_check_liff.py`
- `tests/test_dietitian_health_check_liff.py`
- `tests/dietitian_health_check_liff_behavior.js`
- 本報告
