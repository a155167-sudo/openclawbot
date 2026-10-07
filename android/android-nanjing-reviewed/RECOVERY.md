# 安全復原 / 現場處理

1. 立即停用 `--print`，改回預設 dry-run。
2. 保留 `nanjing_dispatch_ledger.sqlite3`，不要刪除、覆蓋或從雲端同步舊版。
3. 若 customer state 是 `sending` 或 `ambiguous`：先查看實體印表機與紙張；程式不會因 timeout 自動重印，也不可清 ledger 強迫 retry。
4. 若是 `sent_pending_writeback`：修復 Google 連線後再跑相同日期；程式只補 `已送印` writeback。
5. 若 backend auth 401/403：停止，請正式授權者在 Android 本機更新獨立 device bearer；不要把 token 傳到聊天。
6. 若 workbook/worksheet/receipt/payload mismatch：不要改程式繞過。由 backend/Sheet owner 核對 export 的 workbook、numeric worksheet ID/title、17 欄 tagged row 與 publication receipt。
7. 若 CP950 警告：先確認 `?` 是否可接受；不可接受就不要使用 `--print`。
8. 若顯示 ledger busy／claim lost：停止該次執行；這是 socket 前的 fail-closed 拒絕。確認沒有另一個程序正在執行後再重跑，不要手改 state/token。

## 不可用 workaround

- 不得恢復逐單人工 mapping。
- 不得硬編碼舊 workbook 或從 worksheet title 猜 UID/order。
- 不得關閉 TLS 驗證、允許 redirect、移除 bearer、增加 UID/order query。
- 不得清 ledger 來強迫重印。
- 不得把 `sendall()` 成功描述為已確認出紙。

正式 credential 尚未核發時只能跑離線測試；連 backend 的 dry-run 也需合法本機 private config。
