# 3 日飲食健檢營養師唯讀 MVP Implementation Plan

> **For Hermes:** 依 TDD 逐項執行；此階段不得修改 `server.py`、不得接觸正式資料或傳送 LINE 訊息。

**Goal:** 將已確認的互動 HTML 原型升級成由獨立 SQLite 示範資料與唯讀 API 驅動的營養師案件工作台。

**Architecture:** 建立獨立 `health_check_prototype` FastAPI 應用，使用自己的 SQLite 路徑與假資料，只提供 GET API。現有 HTML 在 API 可用時讀取 API；直接下載開啟或 API 失敗時仍使用內建假資料。正式 `server.py`、`#教練`、VIP 權限與 Railway 均不掛接。

**Tech Stack:** Python 3.11、FastAPI、SQLite、原生 HTML/CSS/JavaScript、pytest/TestClient。

---

### Task 1：建立案件唯讀 repository

**Files:**
- Create: `health_check_prototype/__init__.py`
- Create: `health_check_prototype/repository.py`
- Test: `tests/test_health_check_prototype.py`

1. 先寫測試：初始化暫存 DB 後，待審核案件為 3 件且依等待時間排序。
2. 執行測試並確認因模組不存在而失敗。
3. 實作獨立 schema、假資料 seed、清單與詳細資料查詢。
4. 驗證測試通過。

### Task 2：建立唯讀 FastAPI

**Files:**
- Create: `health_check_prototype/app.py`
- Modify: `tests/test_health_check_prototype.py`

1. 先寫 GET 清單、GET 詳細資料、錯誤狀態、POST 405 測試。
2. 執行並確認失敗。
3. 實作獨立 app 與 API；不得 import `server`。
4. 驗證所有 API 測試通過。

### Task 3：讓 HTML 接入 API 且保留離線備援

**Files:**
- Modify: `prototypes/dietitian-3day-checkup-prototype.html`
- Modify: `health_check_prototype/app.py`
- Modify: `tests/test_health_check_prototype.py`

1. 先寫首頁會提供原型 HTML、API 資料可供 UI 使用的測試。
2. 執行並確認失敗。
3. 加入 API 啟動模式；API 失敗時保留現有內建假資料。
4. 確認 UI 上的核准、補件仍只改前端狀態，不呼叫 POST。

### Task 4：完整驗證

1. 跑專項測試。
2. 跑現有完整測試，確認沒有回歸。
3. 驗證 `server.py` 無 diff。
4. 啟動獨立 localhost app，瀏覽器測試案件清單、詳細頁、日期切換及顧客報告預覽。
5. 檢查 Console 無錯誤、API 無 POST 路由、正式 DB 未被讀取。

### 停止點

完成後只交付 localhost 唯讀 MVP。未經 Jason 再次確認，不建立 LINE LIFF App、不修改 `server.py`、不部署 Railway、不讀正式顧客資料。
