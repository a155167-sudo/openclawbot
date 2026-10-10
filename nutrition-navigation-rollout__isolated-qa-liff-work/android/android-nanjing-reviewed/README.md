# 南京店 Android backend-bound 出單器

## 交付狀態

- 已移除逐訂單人工 approval mapping。`backend_v2` 每次執行只使用 authenticated backend contract，逐張先取得 backend `send_permit`，local SQLite commit 後才送出一個 printer packet，再回報 `transport_accepted`／`outcome_unknown`。
- **預設 dry-run**：不更新 Sheet、不建立 printer socket、不建立 customer ledger row；兩種模式都做 backend GET，只有 legacy 另做 Google Sheet 唯讀核對。
- operator config 的 `dispatch_mode` 預設為 `legacy`。只有具備全 consumer 更新與 cache-clear 證據後才可同時啟用 backend feature 與 `backend_v2`；目前沒有該證據，不得切換。
- 正式 device bearer 尚未核發，所以目前是可執行候選，不宣稱 production credential 已配置或整體已上線。
- 未連真 Google、DB、LINE、印表機，未 deploy。backend menu `9028` 主線整合是下一 lane。

## 信任鏈與 fail-closed 規則

固定 GET（只帶兩個 query，不傳 UID/order 或其他欄位）：

```text
GET https://openclawbot-production-production.up.railway.app/internal/printer/v1/dispatch-contract
    ?store_id=nanjing&service_date=YYYY-MM-DD
Authorization: Bearer <獨立裝置憑證>
```

程式要求：

- exact HTTPS 正式 origin；TLS 使用 Python 預設驗證，不可 disable；任何 redirect 都拒絕，因此 bearer 不會跟到跨 origin。
- 1–30 秒 timeout；401/403、timeout、HTTP/JSON 錯誤只輸出縮減訊息，不輸出 token、HTTP body、完整 UID 或 export payload。
- response 必須符合 v1 exact shape，並逐列綁定 `dispatch_row_id`、完整 UID、activated order、menu version、worksheet numeric ID/title、service date、帶時區 formalized time、receipt v1 與完整 14 欄 source。
- local config、backend export、實際 opened workbook ID 必須完全一致；沒有舊 workbook hardcode fallback。
- Sheet 必須有唯一 exact 17 欄 header（原 14 欄 + Dispatch_Row_ID/Order_ID/Menu_Version）。missing row、duplicate、unknown receipt、任一非列印狀態欄 tamper、錯 workbook/worksheet/order/version、非 activated、未知 receipt/policy 均整批停止，尚未觸碰 socket。
- legacy 模式仍保留既有 Sheet reconciliation/writeback 相容性。`backend_v2` 不開 Google Sheet、不讀 cache 列、不寫 N 欄；backend claim/result 才是防重送 authority，`printed:false` 不宣稱實體出紙。
- `無` 是 exact absence marker；`無骨雞便當` 不會被漏掉。CP950 不支援字元會明確警告並以 `?` 送出。

## Android 本機安裝

```sh
pip install -r requirements-android.txt
cp nanjing_printer_private.example.json nanjing_printer_private.json
```

只能由正式授權者在 Android 本機私下填入：

1. 正式核發的獨立 `device_bearer_token`；
2. 與 backend export 完全相同的 `workbook_id`；
3. `google_key.json`（只授權該 workbook、Sheets scope）。

`dispatch_mode` 保持 `legacy`。切換 `backend_v2` 是受控 cutover 動作，不是安裝步驟；v2 不需要 Google key，但不得在 backend feature OFF 或 consumer/cache 證據未完成時切換。

不要把 private config、bearer、Google key、ledger 傳到聊天、Git 或 zip。本交付 zip 不含上述資料。

## 執行

預設 Asia/Taipei 今日 dry-run：

```sh
python nanjing_android_printer.py
```

指定日期仍是 dry-run：

```sh
python nanjing_android_printer.py --date 2026/09/16 \
  --config /本機私有路徑/nanjing_printer_private.json \
  --key /本機私有路徑/google_key.json
```

只有另行授權並先做單張現場測試後才能使用：

```sh
python nanjing_android_printer.py --print --date YYYY/MM/DD \
  --config /本機私有路徑/nanjing_printer_private.json \
  --key /本機私有路徑/google_key.json
```

TCP `sendall()` 成功只表示 bytes 已交給 socket，結果稱「已送印」，不代表已確認出紙。

## Retry / ledger

### backend_v2

- claim timeout、401/403、409（含同 operation replay／舊版本）一律零 socket；v2 不 fallback 到 legacy local claim。
- `send_permit` 收到後先寫入 mode 0600 的 local SQLite，再以 `BEGIN IMMEDIATE` 取得一次 send claim；operation ID 由 immutable dispatch identity 穩定推導，即使 local cache 被清除，backend replay 仍阻止第二包。
- `sendall` 開始後任何錯誤一律 durable `outcome_unknown`，不自動重送。成功只稱 `transport_accepted`，不等於實體紙張。
- result acknowledgement timeout 保留 `pending_report`；下次只重送 result report，不再 claim、不再送 printer packet。
- v2 完全不做直接 Sheet N 欄 writeback，也不送未被 claim protocol 覆蓋的額外 summary packet。

### legacy（feature OFF 相容）

- 只有 printer `connect()` 尚未成功、可確定 payload 完全未進入 `sendall()` 時才記為 `retryable`，不 writeback。
- sendall 開始後失敗：`ambiguous`，下次不盲印。
- `retryable → sending` 以同一 SQLite ledger 的短 `BEGIN IMMEDIATE` 與新 claim token fenced；先 commit 才進 printer I/O。兩個 Android 程序競跑時只有一個取得 send 權，另一個拒絕；ledger busy 也會在 socket 前 fail closed。
- 每次完成更新都必須符合該次 claim token；舊 worker 不可完成或覆蓋新一代 retry。`sending`／`ambiguous` 永遠不依 timeout 自動重送，必須人工核對。
- 送印成功但 writeback 失敗：`sent_pending_writeback`；下次只補 writeback，不再送顧客單。
- 摘要 ledger 也使用短 claim/fencing；完成摘要後以 member key durable 去重，跨 batch、重跑與跨日不會把既有顧客單再次納入摘要。
- 舊 ledger 啟動時只 additive 增加 claim 欄位，既有 `ambiguous`／`manual` 證據原樣保留。
- 不可刪除或同步覆蓋 `nanjing_dispatch_ledger.sqlite3`。

## 完全離線測試

```sh
python -m unittest discover -s tests -v
```

測試以 fake backend response、fake workbook/worksheet、fake socket 與暫存 SQLite 執行；涵蓋 exact GET/auth/timeout/redirect、安全錯誤、正向 response→Sheet→render→fake socket→ledger、writeback failure 再跑不二印、兩個真 SQLite connection 的競態、writer lock 不跨 socket、busy fail closed、非法日期／Asia-Taipei 跨日，以及增量摘要 member 去重。
