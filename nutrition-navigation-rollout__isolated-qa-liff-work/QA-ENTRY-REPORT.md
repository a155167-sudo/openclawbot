# 隔離改期 QA checkpoint 報告

日期：2026-09-29  
工作目錄：`/home/win-xi/nutrition-navigation-rollout/isolated-qa-liff-work`

## 本次完成

- FINAL release-blocker repair:
  - `customer_reschedule_liff_routes.py` restored the four non-body shared endpoints to sync `def` by default:
    - `GET /customer-reschedule`
    - `GET /customer-reschedule/context`
    - `GET /api/admin/customer-pair-reschedule-requests`
    - `GET /customer-reschedule/preview`
  - `POST /customer-reschedule/pending-request` remains async only for body read; isolated QA opts into bounded worker execution for blocking auth/DB work via `offload_blocking_submit=True`.
  - Isolated QA GET endpoints are sync handlers, so FastAPI runs token auth and SQLite reads in its worker pool instead of blocking the event loop.
  - Isolated admin approve/reconcile now authenticate before body/DB/sheet operations, read the small async body, then run the SQLite connection and sheet adapter work inside one bounded worker.
  - Added concurrent responsiveness regression with a stalled fake sheet adapter and independent async health route.
  - Isolated QA LIFF HTML now uses actual `AbortController` request timeout at 40s, no automatic POST retry, disabled invalid/loading controls, source/target disable while submitting, and stable `request_id` reuse for unchanged source/target intent after uncertain failures.
  - Operation result text survives context refresh when the refreshed context is empty or forbidden.
  - UI labels are plain Traditional Chinese user-facing text; no `pending_admin` / raw pending status appears in primary UI copy.
  - Added Node VM behavior test for the emitted HTML covering timeout, idempotent request IDs, no auto retry, and empty/forbidden context gates.
- 新增 opt-in、staging-only 隔離 QA surface：`isolated_reschedule_qa.py`
  - `ISOLATED_RESCHEDULE_QA_ENABLED` 僅接受 `true` / `false`，預設關閉。
  - 僅 `APP_ENV=staging` 可掛載。
  - 固定 allowlisted workbook：`10lktGaFncSi-AEzAs0egh_od7yN1Lt7nKkl_uM7zeTM`。
  - 預設 DB：`/app/data/isolated-reschedule-qa/customer-uat.sqlite3`。
  - 預設 manifest：`/app/data/isolated-reschedule-qa/customer-uat.json`。
  - 啟動時 fail-closed 驗證 DB、manifest、owner/admin、publication workbook/worksheet binding，並拒絕 main DB path collision。
- `server.py` 加入 opt-in wiring：
  - 隔離 QA enabled 時 `/customer-reschedule` 掛 QA router。
  - 未 enabled 時保留既有 default customer reschedule route。
- `customer_reschedule_liff_routes.py` 只加可選 injection：
  - `html_path`
  - `customer_authorizer`
  - `target_occupied_dates_loader`
  - 預設行為未改；隔離 QA 才注入。
- 隔離 context/preview 修正 parent review blocker：
  - 來源餐點與 occupancy 來自 confirmed current dispatch version。
  - `subscription_service_calendar.is_service_day=1` 只作為允許日期，不再被當成 occupied dates。
  - target window 保留 `expiry_date..expiry_date+30` policy。
  - pending/sheet_unknown/manual_hold 時 `exportable_versions` 回空，context 顯示需管理 QA 檢查。
- 管理 QA API：
  - authenticated admin GET 支援 `actionable`，會列出 `pending_admin` 與 `sheet_unknown`，unknown reload 後仍可 discover/reconcile。
  - approve 呼叫既有 `approve_customer_pair_reschedule`。
  - reconcile 呼叫既有 `reconcile_pair_reschedule_readback`。
  - body 中 `actor_id` 直接拒絕；admin identity 只取 verified ID token 與 DB/manifest。
- Runtime Google adapter 修正：
  - 使用 `GOOGLE_CREDENTIALS` JSON。
  - `Credentials.from_service_account_info(...)`。
  - `client.set_timeout(15)`。
  - 開啟固定 allowlisted book，並驗證 personal/Master worksheet ID/title。
- 隔離 QA UI：`customer-reschedule-qa-liff.html`
  - TEST ONLY banner。
  - 繁中 friendly 狀態文字，不以 raw JSON 作為主要 UI。
  - 顯示來源日期、午餐/晚餐、目標日期、pending/confirmed/unknown 狀態。
  - 管理區只在 server role-authorized pending endpoint 成功時顯示。
  - approve、refresh、sheet_unknown reconcile 都有 busy disabled 與 try/catch/finally。
  - submit timeout/error 不自動重送。
- 測試 fixture 修正：
  - `tests/test_pair_reschedule_coordinator.py::open_db` 加參數，讓 isolated fixture 從一開始就產生正確 workbook/worksheet receipt。
  - 不再用 UPDATE 改 immutable publication receipts。
- `tests/test_isolated_reschedule_qa.py`
  - 使用 bounded ASGI client，不用可能 hang 的 TestClient。
  - 正向流程從實際 context offered date 選 target，preview，再 submit，再 admin approve，再 refresh context。
  - 驗證 one batch、replay no second batch、meal_count/remaining_meals 未變、refreshed context 只剩新 source。
  - 驗證 timeout -> `sheet_unknown` reload discoverable -> reconcile -> confirmed。
  - 驗證 wrong owner/admin、spoofed actor、manifest/DB guard、main DB bytes untouched、GOOGLE_CREDENTIALS JSON + `set_timeout(15)`。

## Manifest schema

Parent staging fixture manifest 應為 JSON object：

```json
{
  "workbook_id": "10lktGaFncSi-AEzAs0egh_od7yN1Lt7nKkl_uM7zeTM",
  "personal_worksheet_id": 18,
  "personal_worksheet_title": "QA_Customer_UAT",
  "master_worksheet_id": 202,
  "master_worksheet_title": "Master_API_View",
  "order_id": 1,
  "owner_user_id": "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "admin_user_id": "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "db_path": "/app/data/isolated-reschedule-qa/customer-uat.sqlite3"
}
```

備註：
- `owner_user_id` 與 `admin_user_id` 可相同，但程式不 hardcode 相等。
- DB 的 `admin_settings.admin_id` 必須等於 `admin_user_id`。
- `subscription_orders.id=order_id` 必須為 `owner_user_id` 且 `status='activated'`。
- `subscription_dispatch_rows` published rows 必須全部指向 manifest 的 workbook/personal worksheet。

## Runtime env

Required for parent staging only：

```text
APP_ENV=staging
ISOLATED_RESCHEDULE_QA_ENABLED=true
CUSTOMER_RESCHEDULE_LIFF_ID=<existing dedicated reschedule LIFF id>
CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID=<matching LINE Login channel id>
GOOGLE_CREDENTIALS=<service account JSON string>
ISOLATED_RESCHEDULE_QA_DB_PATH=/app/data/isolated-reschedule-qa/customer-uat.sqlite3
ISOLATED_RESCHEDULE_QA_MANIFEST_PATH=/app/data/isolated-reschedule-qa/customer-uat.json
```

`ISOLATED_RESCHEDULE_QA_DB_PATH` / `ISOLATED_RESCHEDULE_QA_MANIFEST_PATH` 可省略，省略時使用上述預設路徑。

## 已執行測試

```text
timeout 90s python3 -m pytest tests/test_isolated_reschedule_qa.py -q
10 passed in 0.74s
```

```text
timeout 90s python3 -m pytest test_customer_reschedule_liff.py test_customer_reschedule_liff_routes.py tests/test_reschedule_service_integration.py tests/test_pair_reschedule_coordinator.py tests/test_isolated_reschedule_qa.py -q
77 passed in 1.44s
```

```text
timeout 240s python3 -m pytest -q
FAILED during collection:
ModuleNotFoundError: No module named 'nanjing_android_printer'
Affected collection files:
- android/android-nanjing-reviewed/tests/test_backend_claim_consumer.py
- android/android-nanjing-reviewed/tests/test_backend_contract_binding.py
```

```text
timeout 240s python3 -m pytest --ignore=android/android-nanjing-reviewed -q
2342 passed, 190570 warnings, 5 subtests passed in 74.94s
```

## 未執行 / 外部 gate

- 未做 deployment。
- 未讀取真實 credentials。
- 未呼叫 Google Sheets、LINE、AI、quota 或 real DB。
- Playwright 未安裝：`ModuleNotFoundError: No module named 'playwright'`，因此本地無法做真正 browser automation。
- 真機 LIFF/browser、實際 LINE ID token、真 Google adapter transport、staging isolated workbook 寫入與讀回，由 parent live verify/deploy。

## 主要檔案

- `isolated_reschedule_qa.py`
- `customer-reschedule-qa-liff.html`
- `customer_reschedule_liff_routes.py`
- `server.py`
- `tests/test_isolated_reschedule_qa.py`
- `tests/test_pair_reschedule_coordinator.py`
