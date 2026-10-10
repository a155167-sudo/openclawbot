# 固定原始到期日 +30 天改期修復報告

日期：2026-09-29  
工作目錄：`/home/win-xi/nutrition-navigation-rollout/isolated-qa-liff-work`

## 結論

已依最新政策實作「固定原始到期日 +30 天」上限：改期目標日以可信任的原始到期日為下限，最多到原始到期日後 30 天，且不會因再次改期而滾動延長。此政策目前只透過 isolated QA manifest 明確注入；未改變未注入的 production/default coordinator 政策。

已保留既有 owner/order/admin/lease fencing、兩餐一起移動、餐數不變、target 必須是未占用服務日，以及 current dispatch authority 驗證。沒有變更 `usage.expiry_date` 來通過檢查，也沒有接受 client 傳入 expiry。

部署前最後 blocker 已修復：customer submit 的 semantic replay 現在只會重用尚未到期的 `pending_admin` request；到期或 malformed expiry 的 pending rows 保留為 audit rows，但不再卡住同一 intent 的新 request。`sheet_unknown` 不受 expiry 阻擋，仍可 replay/reconcile。

## 政策來源與父層初始化需求

新增 isolated QA manifest 必填欄位：

```json
{
  "original_expiry_date": "2026-10-07"
}
```

父層必須在任何改期前獨立確認並寫入此欄位。本次已按已知 QA 事實建模：原始到期日為 `2026-10-07`。本地程式啟動時會 fail closed：

- manifest 缺少 `original_expiry_date`：拒絕載入。
- manifest 日期格式非 `YYYY-MM-DD`：拒絕載入。
- DB 內該 owner 的 `usage.expiry_date` 與 manifest 原始到期日不一致：拒絕載入。
- 不會在改期後重新擷取可能被延長的 `usage.expiry_date` 作為新 anchor。

若日後不使用 manifest，需由父層提供同等可信任且固定的 DB immutable anchor；本次未做 live DB 初始化。

## Diff 摘要

- `isolated_reschedule_qa.py`
  - 新增 `original_expiry_date` manifest contract 與啟動驗證。
  - 新增 isolated QA 固定期限 predicate：target 必須在 `original_expiry_date..original_expiry_date+30`、service calendar 內、目前未占用；source 必須是 current confirmed meal day。
  - context/preview/submit/coordinator approve 共用此 server-side policy。
  - approve 透過 opt-in policy injection 呼叫 shared coordinator，未改 production default。
  - semantic replay 以 injected router clock 判斷 `now <= expires_at`；expired/malformed `pending_admin` 不 replay，`sheet_unknown` 永遠可 replay/reconcile。
  - admin actionable list 排除 expired/malformed pending；`status=all` 仍曝光 audit rows，回傳 `can_approve=false` 與繁中 disabled reason。

- `pair_reschedule_coordinator.py`
  - `execute_pair_reschedule` 新增 optional `policy_validator`。
  - 未注入時仍走原本 `_validate_policy`，保持 legacy/default regression 行為。

- `reschedule_service_integration.py`
  - `approve_customer_pair_reschedule` 透傳 optional coordinator policy。
  - approval 對 malformed/naive expiry fail closed 為 conflict，不進入 Sheet adapter。

- `customer_reschedule_liff_routes.py`
  - preserve 並強化 semantic dedup。
  - semantic replay 移到 owner/order 授權後、mutable policy 前，讓 `sheet_unknown` replay 不會被 current-version policy failure 擋住。
  - semantic loader 接收 injected clock，確保 submit replay 與 QA 時鐘一致。
  - opt-in hooks 預設關閉。

- `customer-reschedule-qa-liff.html`
  - 管理列表只依 server `can_approve`/`can_reconcile` 顯示按鈕，並顯示 server disabled reason，避免過期 pending 留下可點核准按鈕。

- `tests/test_isolated_reschedule_qa.py`
  - 新增/調整 registered-route two-hop、`+30` accepted、`+31` rejected、manifest anchor fail-closed、unknown replay、duplicate approve zero further mutation、expiry equality boundary、expired duplicate trap、malformed expiry admin projection 等覆蓋。

- `tests/customer_reschedule_qa_liff_behavior.js`
  - real emitted JS 驗證 server action flags：`can_approve=false` 不產生 approve button，且顯示 expired reason。

## RED / GREEN 證據

RED 覆蓋已加入：

- current source `2026-10-07`、原始到期 `2026-10-07` 時，`2026-10-08` 必須可 preview/submit。
- `2026-11-07`（原始到期 +31）必須拒絕且零寫入。
- 第一次 `6->7` 確認後，reload context 顯示 canonical source，再送 `7->8` 可通過 registered route approve。
- repeated moves 不延長 deadline。
- 缺少或 spoofed original anchor fail closed。
- `sheet_unknown` 同 intent 新 request id replay 既有 request，不被 policy failure 蓋掉。
- `pending_admin` expiry equality boundary：`now == expires_at` 仍可 replay，不建立第二 row。
- 到期的同 semantic duplicate pending rows 不再 trap 新 submit；新 request 會建立 exactly one fresh valid row，舊 rows 保留且不更新/刪除。
- malformed expiry pending rows fail closed：不 replay、不出現在 actionable；`status=all` 顯示 disabled reason。
- `sheet_unknown` 超過 TTL 仍可 replay/reconcile。
- canonical request confirmed 後，既有 duplicate pending approve 不產生第二次 Sheet batch，且 audit row 保留 pending，未隱式 invent cancel status。
- emitted JS behavior 透過 Node harness 執行，不只檢查字串。

GREEN gates：

```text
PYTHONPATH=.:tests:android/android-nanjing-reviewed python3 -m pytest tests/test_isolated_reschedule_qa.py -q
18 passed in 1.25s
```

```text
timeout 90s env PYTHONPATH=.:tests:android/android-nanjing-reviewed python3 -m pytest test_customer_reschedule_liff.py test_customer_reschedule_liff_routes.py tests/test_reschedule_service_integration.py tests/test_pair_reschedule_coordinator.py tests/test_isolated_reschedule_qa.py -q
85 passed in 1.83s
```

```text
timeout 240s env PYTHONPATH=.:tests:android/android-nanjing-reviewed python3 -m pytest -q
2381 passed, 190570 warnings, 24 subtests passed in 75.28s
```

## 既有重複申請處理

本 patch 不刪除、不自動取消、不 invent 新 status。既有兩筆 `pending_admin` 仍保留為 audit records。安全行為是：

- 新 reload 或同 intent 新 id 只會 replay canonical actionable request；expired pending 不 replay。
- expired/malformed pending rows 保留原狀，在 `status=all` 顯示但 `can_approve=false`。
- `sheet_unknown` row 即使已超過原 pending TTL，也可 replay 與 reconcile。
- canonical first approve 成功後，再 approve existing duplicate 不會產生第二次 Sheet batch。
- duplicate row 保留原狀，等待父層人工審核或未來明確 schema/action 處理。

## 尚缺 Live Gate

本次依指示未使用 network、credentials、live DB、Google Sheet、deployment 或 commit。父層部署前仍需獨立完成：

- 在 QA manifest 初始化 `original_expiry_date: "2026-10-07"`。
- 驗證 manifest 與 isolated DB owner/order/admin/workbook binding 一致。
- 在 staging 使用真實 Sheet adapter 做一次人工 approve/readback。
- 部署後觀察 duplicate pending admin surface，不以隱式狀態變更清理歷史 row。
