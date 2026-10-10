# OFFLINE TEST RESULTS

日期：2026-09-15

- 修復前 backend-bound baseline：`17 tests / OK`（0.021s）。
- 並行重試回歸 RED：雙 connection 都取得 `send`，實際為 `['send', 'send']`，`1 failed`。
- 修復後完整套件：`25 tests / OK`（0.138s）；原 17 tests 全數保留。
- 雙 connection race focused：`1 test / OK`（0.005s），另連跑 100 次皆 PASS。
- 新增安全 focused 7 tests：`7 tests / OK`（0.118s）。
- `py_compile`：PASS（cache 導向 `/tmp` 後刪除）。
- Python：環境未找到候選 repo 或 `/home/win-xi` 的 `.venv/bin/python`；依原審查命令使用 `python3` 3.11.15。
- 測試邊界：fake backend response、fake Google workbook/worksheet、fake socket、暫存 SQLite；未連真 Google/DB/LINE/printer，未 deploy、未讀正式 vars，未封裝。

主要覆蓋：

- exact HTTPS origin、GET query 只有 `store_id/service_date`、dedicated bearer header、timeout；redirect/auth/HTTP/JSON error fail closed 且不洩漏 token。
- exact v1 response、activated/UID/order/menu/worksheet ID+title/service date/formalized time/receipt/14-column payload/publication policy。
- wrong workbook、missing/duplicate/unknown receipt row、extra column、tamper、未知狀態全部在 socket 前停止。
- Sheet 現況已送印 skip；`無骨雞` 不被 absence marker 漏掉；CP950 警告可見。
- default dry-run 零 Sheet write、零 socket、零 customer ledger row。
- 完整 positive：fake export → fake Sheet reconcile → render → fake socket → ledger → writeback failure → 同日再跑只補 writeback、不重印 customer ticket。
- 同 ledger 真 SQLite 兩 connection/thread barrier 的 `retryable → sending` 只有一個 winner；另一程序在第一程序 fake socket 阻塞期間拒絕送印。
- `BEGIN IMMEDIATE` claim 在 socket 前 commit；fake socket 阻塞時第二 connection 可取得 writer 並 commit。DB busy 在 socket 前 fail closed。
- completion 綁定 claim token，舊 generation 無法覆蓋新 retry；`sending`／`ambiguous` 不自動重送。
- 舊 ledger additive migration 保留 `ambiguous`／`manual`，claim 欄位預設空字串。
- 非法 calendar date、Asia/Taipei midnight、跨日 summary 隔離、不同 batch incremental summary 與 replay member 去重。

限制：這是隔離離線證據，不是實體出紙、真 Google、正式 bearer、production deployment 或打包證據。
