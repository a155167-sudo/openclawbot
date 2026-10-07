# Normal cutover 後續阻礙

1. **原到期日錨點尚未在正常路徑固定。** `customer_reschedule_liff.py:24-46` 的日期運算確為傳入到期日加 30 天，沒有以 today 加 30 天；但 `customer_reschedule_liff_routes.py:193-200,508-535,734-758` 每次從可變的 `usage.expiry_date` 讀錨點。正常路徑沒有像 `isolated_reschedule_qa.py:139-172,374-418` 那樣驗證固定的 `original_expiry_date`。若 `usage.expiry_date` 日後變更，截止也會漂移；現有測試只證明傳入日期加 30 天，未證明它是**原**到期日。建議主控於正常訂單權威資料保存不可變原到期日，且 context／preview／submit／admin approval 共用此錨點與邊界測試。
2. **核准層與申請窗口衝突。** 路由允許 `usage.expiry_date + 30`；但 `reschedule_service_integration.py:760-795` 核准時呼叫 `execute_pair_reschedule`，其 `pair_reschedule_coordinator.py:229-248` 的 `_validate_policy` 要求 `target <= usage.expiry_date`，會拒絕申請窗口中到期日後的日期。既有 `test_customer_reschedule_liff.py` 允許到期日加 30，無法證明核准可用。建議主控在單一權威 policy 中統一申請和核准的固定原到期日窗口，再驗證 calendar、Sheet、receipt、dispatch 的整合；本輪不改多層 policy，也不放寬授權。
3. **測試環境阻礙。** 指定 venv 收集 `tests/test_reschedule_registered_routes.py` 時，`import server` 因缺 `gspread` 報 `ModuleNotFoundError`；依任務未安裝套件。較大 customer route 測試在 `test_page_serves_html_in_staging` 的 TestClient 呼叫逾時（25s），單獨 admin readback TestClient 測試亦逾時（15s）。應由主控在具備相依套件及可運作 ASGI TestClient 的隔離環境重跑；server import 僅可用本目錄暫存 DATA_DIR，不能連主 DB 或網路。

本輪 helper 修復只消除 `parent_version_id IS NULL` 的誤擋。`exportable_versions` 對 pending／sheet_unknown／manual_hold 仍封鎖，且 helper 仍驗證 owner 與 snapshot payload；未偽造確認或 receipt。
