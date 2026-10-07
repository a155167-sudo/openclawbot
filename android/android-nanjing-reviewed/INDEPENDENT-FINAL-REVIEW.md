# PASS — Android 出單競態修復獨立複審

日期：2026-09-15  
範圍：僅複審前次 BLOCK 的 Android producer/consumer 最終競態修復；不重做已 PASS 的 backend contract 全面審查。  
固定來源：`/home/win-xi/nanjing-android-bound-work`  
固定 manifest SHA-256：`27a3cc6149566df55dcc287d2ce5c72b4e85b8801c796c7d40905ee4a20cf8a9`

## Verdict

**PASS（可封裝為未啟用正式列印的離線交付候選）**。

前次 B1/B2/B3 已關閉：customer 與 summary 的 `retryable → sending` 均在短 `BEGIN IMMEDIATE` 中，以 state CAS、`rowcount == 1` 與每代 durable claim token 取得唯一送印權；claim commit/connection close 後才進 fake printer socket；所有完成更新均以 token/state fence 拒絕 stale worker。SQLite busy 在 socket 前 fail closed；既有 `sending`／`ambiguous`／`manual` 不自動重送。舊 ledger 只 additive 增加 `claim_token`，歷史狀態與日期資料保留。

此 PASS **不是** production enablement、部署或實體出紙授權。正式 device bearer 尚未核發；backend 尚未完成 menu `9028` 主線整合／部署；未連真 Google、LINE、正式 DB 或印表機。不得把 zip 中的 `--print` 說明視為目前可直接正式列印；README 要求另行授權及單張現場測試。

## 獨立動態證據

- 完整 stdlib unittest：`Ran 25 tests in 0.136s`，`OK`；原 17-test baseline 保留。
- customer 兩真 SQLite connection retry claim race：100/100 PASS，每輪恰為一個 `send`、一個 `manual`。
- summary 兩真 SQLite connection retry claim race：100/100 PASS，每輪恰為一個 `send`、一個 `manual`。
- whole-job customer socket barrier race：30/30 PASS；fake socket 阻塞時，另一 fresh connection 可 `BEGIN IMMEDIATE`、寫入並 commit；競爭 job 零 socket send。
- whole-job summary socket barrier race：30/30 PASS；同樣證明 summary claim 不跨 socket 持 writer lock，競爭 job 零 socket send。
- stale summary completion：100/100 拒絕舊 token；新 token 可完成。
- busy fail-closed：customer（suite）與 summary（outer-job probe）皆在 socket 前停止且 payload 為空。
- dry-run baseline：零 Sheet update、零 socket payload、零 customer ledger row。
- 邊界資料：非法 calendar date fail closed；Asia/Taipei midnight；跨日 summary 隔離；不同 batch incremental summary 只納入未摘要 member；replay 不重算；`無骨雞` 保留。
- 舊 ledger：customer `ambiguous`、summary `manual`、既有日期與資料在 additive migration 後不變，新增 token 為空字串。
- syntax：`py_compile=PASS`（cache 在 `/tmp`）。
- source 與 pristine 前後 manifest 均為 `27a3cc6149566df55dcc287d2ce5c72b4e85b8801c796c7d40905ee4a20cf8a9`。
- artifact scan：無 `.db/.sqlite/.sqlite3`、private config、Google key、private-key 檔或非 placeholder credential literal。

全部測試僅使用 fake backend/workbook/socket 與暫存 SQLite；未執行正式列印、Google/LINE/正式 DB、vars、commit 或 deploy。

## 重大檢查清單

| Gate | 結果 |
|---|---|
| customer 唯一 claim（BEGIN IMMEDIATE + state CAS + token） | PASS |
| summary 唯一 claim（BEGIN IMMEDIATE + state CAS + token） | PASS |
| provider/socket I/O 不持 SQLite writer lock | PASS |
| customer + summary busy 均 fail closed、零 socket | PASS |
| stale customer/summary completion 拒絕 | PASS |
| retryable 可重試；sending/ambiguous/manual 不盲印 | PASS |
| dry-run 非破壞性 | PASS |
| malformed date / Taipei midnight / cross-day / incremental summary exact data | PASS |
| 舊 ledger additive migration 與歷史 evidence 保留 | PASS |
| exact backend-bound row shape 未受最終 delta 影響 | PASS（沿用前審已核實範圍，並由保留 suite 驗證） |
| 無 secret、ledger、key、真 UID 交付物 | PASS |
| 正式列印/部署界線明確保留 | PASS |

## 固定完整來源 manifest

格式：`SHA-256  bytes  relative-path`

```text
725e788d7b73a8312f7b9f0bb5b58d904aac7ae321917b7d396741562b5c9e11  2435  OFFLINE-TEST-RESULTS.md
58e47dc24907420ff8599e1f4fea5da20713b3c132e8574a39993cb6404bf8d1  5001  README.md
6790d87d049ec31412b24c2931aff9b074f9d02c31f8fafc891990bb67127002  1519  RECOVERY.md
72a033d091f5bb2457281af5c3c376ad7f1fa0f946ce3c2b7c6bbd5d3c7f2ff9  38408  nanjing_android_printer.py
c4d2a029a92350aba200276d05e5e98d474d514c95acc3940dc0c0d41658a91f  257  nanjing_printer_private.example.json
f92cd776809168ea4c5c07608c503d7aac05af9c0e397a0780e67f326e22c67f  36  requirements-android.txt
66e797b2c1fb12422a85c105bad280fa4619d721fba4683e614cfb402962f446  27002  tests/test_backend_contract_binding.py
```

Manifest aggregate recipe：依 relative path 排序，串接 `sha256␠␠bytes␠␠path\n` 後再取 SHA-256；結果為上述固定 manifest SHA-256。
