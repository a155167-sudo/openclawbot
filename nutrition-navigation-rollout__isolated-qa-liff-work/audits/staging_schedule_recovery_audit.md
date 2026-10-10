# Staging 排程 Sheet 唯讀稽核與離線復原設計

> 範圍：`restore_staging_schedule_sheet_once`、`repair_staging_duplicate_dates_once`、`bootstrap_staging_reschedule_fixture_once`，以及 17 欄 dispatch / publish / receipt / authority 契約。  
> 本文件只根據本機程式與離線測試；未連線、未部署、未讀取 credentials、未查遠端 DB/Sheet，也**不代表 live 已修復**。

## 結論摘要

三個 startup helper 不能作為復原工具：restore 會先 `clear()` 再以舊 14 欄 snapshot 覆寫；duplicate repair 以「實體第一列」作日期權威並逐列刪除；bootstrap 自行寫入「external writer disabled」能力證明，且先提交 success marker、後另開交易 import authority。這些路徑會破壞 17 欄身份契約，或把未驗證事實升格成寫入權限／authority。

## 已證實（本機程式或離線執行直接可證）

### A. 17 欄契約

1. `subscription_dispatch_contract.py:25-30,480-488` 定義 A:N 原始 payload，加上 O:Q 的 `Dispatch_Row_ID / Order_ID / Menu_Version`；發布列必須 17 欄。
2. `subscription_dispatch_contract.py:684-816` 的正常發布路徑會：驗證 staged IDs 與完整 A:N payload、寫 Sheet、立即 readback、再建立 immutable receipt 並轉 published。
3. receipt/authority 的可信範圍是 SQLite 內的 producer provenance，不是外部 Sheet 現況證明；程式自己也在 `subscription_dispatch_contract.py:371-376` 說明 receipt 不是抵抗任意 DB writer 的 MAC。
4. `_trusted_publication_rows` 會精確比對 receipt、dispatch row、order、A:N canonical payload/hash、時間與 published state（`subscription_dispatch_contract.py:371-437`）。但 restore/repair 可在 receipt 建立後直接改 Sheet，沒有同步更新 receipt；因此「DB authority 可信」不等於「目前 Sheet 與 authority 相符」。
5. `import_initial_published_version` 只從 DB receipts 建立 authority（`dispatch_authority_bridge.py:268-307`），不讀 Sheet；若 Sheet 已被 restore/repair 改壞，仍可能把 stale DB facts import 成 authority。

### B. `restore_staging_schedule_sheet_once`

1. **14 欄還原會遺失身份**：來源是 `subscription_orders.form_payload_json.schedule_sheet_rows`（`server.py:4939-4944`），正常 snapshot 契約為 14 欄；函式 `clear()` 後直接寫這些列到 A:Q（`server.py:4946-4949`），沒有重建 O:Q。離線測試已實際證實 durable ID 消失，且 17 欄 adapter 再也找不到該日期。
2. **全表破壞**：`sheet.clear()` 會清掉與 snapshot 無關的列、人工備註、列印狀態、公式及其他 metadata/value；不是按 dispatch ID 的受限 upsert。
3. **非原子外部操作**：`clear()` 與 `update()` 是兩次呼叫；clear 成功而 update timeout/失敗時，結果可能是空表、部分表或 unknown outcome。
4. **來源未驗證**：只要求 identity 存在且 rows 至少兩列；未驗 header、每列 14 欄、日期集合、owner、order/menu、payload hash、receipt set、列數或 worksheet numeric ID。
5. **目標 identity 不完整且可能任意**：SQL `SELECT DISTINCT workbook_id,worksheet_title ... fetchone()` 未要求唯一結果，也沒有 worksheet_id；有多個 distinct target 時，實體選到哪一筆沒有 fail-closed 保證。
6. **沒有安全邊界**：沒有 backup、writer capability 驗證、workbook lease/fence、before-image/CAS、marker、獨立 readback、unknown-outcome reconcile 或 rollback。
7. 名稱含 `once`，但沒有 durable once marker；只要環境旗標保留，每次 lifespan 都會再執行（`server.py:3241-3245`）。

### C. `repair_staging_duplicate_dates_once`

1. **只保留第一筆日期**：`seen[day]` 保存第一個實體 row index，其後同日期全刪（`server.py:4968-4983`）。沒有比較 O:Q dispatch identity、order/menu、owner、A:N payload、receipt 或 authority；Sheet row order 被錯當權威。
2. **gspread padding 偽重複**：判斷只看 `len(item) == 17`。只要 14 欄 legacy row 因讀取矩形範圍被補成 O:Q 空白，它就被當作完整 17 欄列。新增的單一離線測試實際餵入此形狀，結果保留 padded legacy row、刪除具有 durable ID 的 published row。
3. **逐列不可交易刪除**：每個 `delete_rows` 是獨立外部副作用；中途 timeout 可留下部分刪除的 unknown outcome。
4. **無證據即刪除**：沒有完整 before backup，也沒有保存被刪 row 的 A:Q、physical index、sheet revision/metadata 或 hash。
5. **沒有 lease/CAS/readback/rollback/marker**：不受 workbook writer fence 保護；僅第一次 `get_all_values()`，刪除後沒有獨立 readback；log 的 removed 數只代表嘗試迴圈完成。
6. 反向刪 index 可避免本次迴圈的 index shift，但不能解決「刪錯身份」或部分成功。

### D. `bootstrap_staging_reschedule_fixture_once`

1. **未驗證 writer evidence 被自行宣告為真**：函式直接把完整 inventory、`external_writer_state="disabled"` 與自由文字 evidence 寫入 capability（`server.py:11131-11139`）。`require_full_writer_inventory` 只檢查集合、state 和非空字串（`workbook_write_lease.py:100-121`），不驗證外部 writers 是否真的停用；因此 bootstrap 可自行解除本應由 operator/cutover 證據控制的 release gate。
2. **能力設定先行提交**：即使後續 fixture 失敗，虛假的 writer capability 仍已 commit，會影響其他 writer lease。
3. **success marker 先於 authority import commit**：formalize 成功後，`staging_reschedule_bootstrap` 先 `INSERT OR REPLACE` 並 commit（`server.py:11165-11170`），之後才呼叫會自行 `BEGIN IMMEDIATE`/commit 的 `import_initial_published_version`（`server.py:11173-11177`；`dispatch_authority_bridge.py:273-305`）。crash/failure window 可留下 marker=success、published receipts 存在、binding 缺失。
4. **marker 不綁 payload/receipt/authority**：marker 只有 fixture_key/status/detail/time，沒有 operation ID、A:N payload hash、17 欄 expected hash、receipt-set hash、version ID、worksheet ID 或 readback hash；`INSERT OR REPLACE` 也允許覆寫歷史。
5. **完成判斷過弱**：只檢查 marker success、published count 非零、任一 binding 存在（`server.py:11144-11154`）；沒有核對 exact row count/set、每一 receipt、menu version、authority payload hash、Sheet A:Q 或 Master view。
6. **authority import 前無獨立 Sheet 現況驗證**：import 僅重讀 DB receipt facts；不能察覺 restore/repair 或其他 writer 已改壞 Sheet。
7. **多表面副作用非原子**：formalize 依序改 health profile/customer sheet、main sheet append、personal sheet、Master view，最後才 publish receipts（`server.py:10982-11081`）。dispatch publish 對 personal schedule 有精確 readback，但無法把 main/personal/Master/SQLite 包成同一原子交易；main append 也沒有 operation identity。
8. 正常 fence 對 post-write ambiguity 會保留 active lease（`server_workbook_write_fence.py:76-146`），這一點是良好 fail-closed 基礎；但它依賴前述未驗證 capability，所以不能單獨證明 exclusive writer safety。

### E. startup 組合風險

lifespan 固定依序執行 restore → duplicate repair → bootstrap（`server.py:3241-3245`）。也就是說，兩個沒有 lease/readback 的破壞性 helper 可先改壞 Sheet，bootstrap 隨後仍可能根據 stale DB receipts 建立 authority。啟動成功 log 不能證明跨 Sheet/DB 契約一致。

## 假說／尚未由本任務證實

1. **Live Sheet 是否仍為 14 欄、哪些列被刪、是否真的存在 gspread padding**：本任務禁止遠端 readback；離線測試只證明程式在 padded input 下必然刪除後來的 durable row。
2. **先前 19 列刪除的精確內容與可否復原**：題目 context 指出已刪 19 列且沒有 before backup；本機沒有可核對的 live before-image，不能宣稱已重建。
3. **external writers 是否真的 disabled**：本機只證實程式寫入了一段自我宣告文字，沒有 operator evidence。
4. **production 未受影響或 staging 已修好**：沒有 live read-only 證據，不成立。
5. **目前 marker/receipt/authority/lease 的實際 DB 狀態**：未存取遠端 DB，未知。

## 具體復原計畫（只設計，不執行）

### 0. 先凍結，不靠程式自我宣告

1. 關閉三個 startup flags；暫停所有列在 `FULL_REQUIRED_WRITER_INVENTORY` 的 server/job、Apps Script、人工編輯與其他 service account。
2. 由操作者提供可審核 evidence：writer 名單、停用時間、deployment/revision、Apps Script trigger 清單、service-account ACL、人工維護窗口。**不得**由復原程式自行把 external state 設成 disabled。
3. 建立 recovery operation ID；取得同 workbook 的 durable lease。若已有 active/expired lease，視為 unknown outcome，先 reconcile，不覆蓋 lease。

### 1. 必要唯讀證據

在任何寫入前，使用只讀身份各自擷取並封存：

- Sheet：spreadsheet ID、worksheet numeric ID/title、grid row/column count、locale/timezone、完整 A:Q values（明確 value-render/date-render 選項）、公式、row metadata；personal sheet 與 `Master_API_View` 都要取。
- SQLite：read-only snapshot 及 `quick_check`；order 1、`subscription_dispatch_rows`、publication receipts、authority bindings/rows、dispatch versions/operations、printer claims、bootstrap marker、writer capabilities、leases。
- 對每一份 artifact 記錄取得時間、來源 identity、byte length、SHA-256；原始檔唯讀保存，不只存畫面或摘要。
- 精確證據表：以 `(workbook_id, worksheet_id, worksheet_title, order_id, menu_version, service_date, dispatch_row_id)` 聯結 DB 與 Sheet；禁止只按日期聯結。

若缺任何被刪 row 的可信 before-image，標記為 **unrecoverable/needs operator source**，不可猜測餐點或 ID。

### 2. Backup gate

1. 先建立 immutable DB backup 與完整 Sheet export；驗證可重新讀取且 hash 相符。
2. 產生 machine-readable manifest，列出每個 row 的 A:N canonical payload hash、O:Q identity、receipt ID/hash、authority version/hash。
3. 只有第二個獨立 reader 從 backup 重算 manifest 且一致，才允許進入 write phase。

### 3. 既有 ID 重建（不產生新身份冒充舊發布）

1. 唯一可接受的 ID 來源是已驗證的 `subscription_dispatch_rows` + immutable publication receipt + exact A:N source payload/hash。
2. 對每個日期，要求 exactly one matching receipt，且 owner/store/workbook/worksheet/order/menu/date/A:N hash 全部一致；再把既有 `dispatch_row_id, order_id, menu_version` 補回 O:Q。
3. 如果同日期有多個 row，按完整 identity 與 receipt 判定，不按 physical first row。無 receipt、partial receipt、hash mismatch 或多解時 fail closed，交人工裁決。
4. authority binding 若已存在，還要驗 `authority_payload_hash` 與 `receipt_set_hash`；若不存在，只在 Sheet 修復並獨立 readback 成功後，由同一 recovery state machine import。

### 4. 精確 payload / receipt 比對與 repair payload

1. 預先生成唯一 deterministic payload：只包含待修 row 的精確 A:Q before/after、physical target、worksheet numeric ID；另含 personal + Master 的 expected before/after hash。
2. A:N 使用現有 canonicalization；O:Q 必須逐字等於既有 DB identity。列印狀態（N）依契約視為 operator-owned，不得被舊 snapshot 覆蓋。
3. 禁止 `clear()`、全表 replace、按日期 delete；採受限 batch update/upsert，且 batch 前重新讀取 expected before-image 作 CAS。
4. receipt 不因修復而捏造或重寫；只有 Sheet A:Q 與既有 receipt facts 完全一致才可標記 reconciled。

### 5. 寫入防護、lease 與 marker 狀態機

1. lease 綁定 workbook + operation ID + writer ID + payload hash；TTL 到期不自動重用，expired 一律 unknown。
2. recovery ledger 至少有 `prepared → writing → unknown|verified → authority_bound → complete`，append-only，不用 `INSERT OR REPLACE`。
3. `complete` 必須最後寫：Sheet 獨立 readback、DB receipt set、authority binding、Master E2E 全部通過後，才在同一 DB transaction 寫 authority binding與 complete marker。若現有 import API強制自有 commit，先設計可由 caller 擁有 transaction 的窄 API，不能沿用 split commit。

### 6. Unknown outcome 與 rollback

1. provider timeout/5xx/連線中斷後不重送；保留 lease，狀態設 unknown。
2. 用**新的獨立 client/connection**唯讀取得 A:Q 與 Master；只接受三種分類：exact-before（可安全重試）、exact-after（進入 verify）、other/partial（停止自動化、人工 reconcile）。
3. rollback 也必須是 lease + CAS 操作：只有 live hash 等於已知 after hash 才可套回 immutable backup；回復後再獨立 readback。若 live 已被第三方改動，禁止 rollback 覆蓋。
4. rollback 不刪 receipts/authority 來掩蓋事件；保留 audit trail，必要時以 superseding version 表示後續狀態。

### 7. 獨立 readback 與 E2E gate

1. 不使用同一次 write response 當 readback；新 client 讀取 personal A:Q、Master row、worksheet IDs。
2. 重算所有 A:N payload hashes、17 欄 identity set、receipt hashes、receipt-set hash、authority payload hash，要求 exact equality 與 exact row count；拒絕 extra/missing/duplicate。
3. E2E：以 customer reschedule 的 source/target pair 執行 dry-run plan，驗證 before-image；受 lease 控制執行一次；獨立 readback；確認 authority version transition；printer export 僅回傳 current receipted rows；重播同 request 必須零 Sheet writes；timeout 注入後必須進 unknown 且不自動重送；最後驗證其他 row、列印狀態與 Master 不變。

## 單一新增離線回歸測試

- 測試：`test_current_duplicate_repair_treats_padded_legacy_row_as_authority_and_deletes_dispatch_row`
- 檔案：`tests/test_restore_contract_audit.py`
- 覆蓋：14 欄 legacy row 被補成 17 欄、與真正 17 欄 durable row 同日期時，現行 repair 會保留第一列並刪除 durable row；也驗證只有一次 pre-read、沒有 post-delete readback。
- 此測試是**缺陷鎖定測試**：目前通過表示成功重現危險行為，不表示產品已修復。未修改 `server.py` 或任何既有產品程式。

## 稽核限制

此目錄及其父目錄沒有可用 `.git` repository metadata，因此無法核對 recent commit、原始 commit diff 或證明產品檔未受其他歷史變更；本次僅以操作前後 `server.py` SHA-256 與本機檔案內容核對本次工作。
