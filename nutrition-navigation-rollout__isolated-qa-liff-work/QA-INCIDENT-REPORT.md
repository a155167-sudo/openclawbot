# 隔離改期 QA 事故修復報告

日期：2026-09-29  
工作目錄：`/home/win-xi/nutrition-navigation-rollout/isolated-qa-liff-work`

## 事故摘要

QA runtime 證據顯示：使用權益 `expiry_date=2026-10-07`、`remaining_meals=8`、`status=active`，第一次已確認 `2026-10-06 -> 2026-10-07` 後，canonical current source 變成 `2026-10-07`。頁面仍提供 `2026-10-07 -> 2026-10-08`，但管理員核准時 shared pair coordinator 以 `usage.last_date..usage.expiry_date` 重新檢查，判定 `2026-10-08` 已超出 active entitlement，因此回覆 `reschedule dates are outside active entitlement`。

同時，頁面 reload 後 client 會產生新的 `request_id`；service boundary 原本只用 `request_id` dedup，因此同一個 owner/order/source/target 可形成兩筆不同 id、同為 `pending_admin`、沒有 `operation_id` 的申請。

## 根因

根因是合約衝突，不是 fixture 或 live expiry 資料錯誤：

- QA/public customer target window 使用 `expiry_date..expiry_date+30`。
- shared pair coordinator 實際核准政策使用 `usage.last_date..usage.expiry_date`。
- 因此 `2026-10-08` 可被頁面提供與送出，但不可被核准。
- `request_id` 是 client-local intent id；reload 後會換 id，缺少 server-side semantic dedup，導致相同意圖重複 pending。

本次沒有延長 fixture/live expiry，也沒有放寬 shared coordinator 或正式 business policy。

## 修復內容

- `isolated_reschedule_qa.py`
  - 新增 QA-only eligible target 計算：target 必須同時在 public window、service calendar、current confirmed dispatch 空位、以及 `usage.last_date..usage.expiry_date` 內。
  - isolated context/preview 改用同一個 eligible target 集合。
  - submit hook 重新讀 canonical current version，若 source 不再是 current meal day 或 target 不可核准，直接 fail closed。
  - 新增 actionable semantic replay：同 owner/order/source/target 且狀態為 `pending_admin` 或 `sheet_unknown` 時，回傳既有 request，不建立新 row。

- `customer_reschedule_liff_routes.py`
  - 新增 optional hooks：`target_policy_validator` 與 `semantic_pending_request_loader`。
  - hooks 預設關閉，default routes 不改變。
  - isolated QA submit 使用 `BEGIN IMMEDIATE` 包住 semantic check + insert，避免併發兩筆相同 pending。

- `customer-reschedule-qa-liff.html`
  - 無 eligible target 時顯示明確繁中訊息：`目前沒有有效權益內可送出且可核准的目標日期。`
  - submit 維持 disabled，不會產生 request id 或 POST。

## RED / GREEN

新增 RED 覆蓋：

- `tests/test_isolated_reschedule_qa.py::test_post_confirmed_hop_does_not_offer_or_accept_target_outside_entitlement`
  - 建立 canonical current source `2026-10-07`、expiry `2026-10-07`、calendar 含 `2026-10-08`。
  - 驗證 context 不提供 target。
  - 驗證 preview `2026-10-07 -> 2026-10-08` 回 422。
  - 驗證 submit 回 422，且零寫入。

- `tests/test_isolated_reschedule_qa.py::test_same_semantic_pending_request_replays_existing_request_id`
  - 相同 owner/order/source/target、不同 `request_id` 的第二次送出，回傳第一筆 request id。
  - DB 只保留一筆 pending row。

- `tests/test_isolated_reschedule_qa.py::test_concurrent_same_semantic_submissions_create_one_pending_request`
  - 併發兩次不同 request id 的相同 semantic intent，最後只有一筆 pending row。

- `tests/customer_reschedule_qa_liff_behavior.js`
  - emitted JS 在 `target_dates=[]` 時 submit disabled，點擊不 POST。

GREEN gates：

```text
timeout 90s env PYTHONPATH=.:tests:android/android-nanjing-reviewed python3 -m pytest tests/test_isolated_reschedule_qa.py -q
13 passed in 0.84s
```

```text
timeout 90s env PYTHONPATH=.:tests:android/android-nanjing-reviewed python3 -m pytest test_customer_reschedule_liff.py test_customer_reschedule_liff_routes.py tests/test_reschedule_service_integration.py tests/test_pair_reschedule_coordinator.py tests/test_isolated_reschedule_qa.py -q
80 passed in 1.52s
```

```text
timeout 240s env PYTHONPATH=.:tests:android/android-nanjing-reviewed python3 -m pytest -q
2376 passed, 190570 warnings, 24 subtests passed in 74.45s
```

## 既有重複申請處理

本次不刪除、不自動核准、不自動取消 runtime 既有兩筆 duplicate pending row。這是刻意的：目前 schema 沒有 reviewed cancellation status，且兩筆 row 是已存在的審核物件。

安全處理方式：

- 本 patch 只阻止再建立第三筆同 semantic intent。
- Parent 應以 request id 檢查兩筆既有 pending 的 source/target/sheet 狀態。
- 在 business decision 前，不應嘗試核准 `2026-10-07 -> 2026-10-08`，因為目前 coordinator 仍會拒絕。
- 若日後要清理 duplicate pending，需要新增 reviewed admin action/schema，而不是在本次 QA incident 中隱式 mutate。

## 尚待 business-policy 決策

是否允許 `2026-10-07 -> 2026-10-08` 仍需產品/營運決策。若要允許，必須明確修改 shared coordinator/business policy 與權益模型；不能只改 QA fixture、延長 live expiry，或只放寬 LIFF target window。

在目前政策下，沒有 eligible target 時不是 happy path restored；正確行為是 fail closed 並告知「目前沒有有效權益內可送出且可核准的目標日期」。

## 外部限制

- 未使用 network。
- 未使用 credentials。
- 未部署。
- 未讀寫 live DB 或 Google Sheet。
- 未做 git commit。
