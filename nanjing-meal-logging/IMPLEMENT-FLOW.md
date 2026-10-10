# 南京文字記餐第一 checkpoint

- 基線：`feature/nanjing-meal-logging` @ `b7649dd356f0c5b841c51077417475e10737d6e7`
- 範圍：只改 `server.py`；新增 `tests/test_nanjing_meal_logging_flow.py`；不 commit/push/deploy。
- 隔離：只用 pytest temp SQLite、fake provider、fake LINE reply；不讀真 DB、不連網。
- 接手點（RED 前）：既有入口只提示、未持久化 next-text state；explicit values 直接寫 `food_logs`；成功第一則是文字而非記錄卡。
- 預定 tracer：入口→durable awaiting→裸 `無糖豆漿500ml`→pending draft/0 logs；explicit values→固定草稿/零 AI 零扣→confirm；confirm→記錄卡 + 南京現有總覽。

## GREEN 接手點（昂貴測試前）

- `server.py` 已加入 owner-bound `pending_text_meal_inputs`（30 分鐘、version、cancel/consumed/expired）及同 message draft replay。
- explicit calories/protein 改為 deterministic fixed draft，不呼叫 provider、不扣 AI quota；confirm 才寫正式帳本。
- legacy `LOG_NUTRITION` 改為 fixed confirmation draft；decimal parser 保留 `11.3`，不再變成 `113`。
- confirm success 改為「已提交記錄 Flex 卡 + 原 `build_dashboard_flex` 南京總覽」；沒有改 dashboard/首頁/餘額程式。
- 下一步：跑 focused 4 tests、compile、檢查 diff；不跑完整回歸（父控負責）。

## 實跑結果

- RED 1：新 focused 檔最初 `3 failed`（缺 durable table、explicit 仍直寫、取消落 generic chat）。
- RED 2：decimal probe 明確得到 `1805 != 180.5`，證明舊 parser 會吃掉小數點。
- RED 3：時間邊界 00:00 實得早餐、預期點心。
- GREEN：`python -m pytest -q -p tests.conftest tests/test_nanjing_meal_logging_flow.py` → **5 passed**。
- Compile：`python -m py_compile server.py tests/test_nanjing_meal_logging_flow.py` → exit 0。
- Diff hygiene：`git diff --check` → exit 0。
- 已知邊界：只跑本 checkpoint focused tests；完整舊測試/整合回歸依指示留給父控。未 commit、未 push、未 deploy。
