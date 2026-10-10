# 3 日飲食健檢｜營養師唯讀 MVP

這是與正式 LINE Bot 隔離的示範服務。

## 啟動

```bash
cd /home/win-xi/.hermes/workspace/openclawbot
python3 -m uvicorn health_check_prototype.app:app --host 127.0.0.1 --port 8766
```

開啟：<http://127.0.0.1:8766/>

API 文件：<http://127.0.0.1:8766/prototype-docs>

## 隔離保證

- 不 import `server.py`
- 不讀 `data/user_quota.db`
- 預設示範 DB 位於系統建立的隨機私有暫存目錄，例如 `/tmp/yirile-dietitian-prototype-xxxx/dietitian-demo.db`
- DB 檔名固定為 `dietitian-demo.db`；自訂路徑必須同時指定安全根目錄
- 拒絕 `user_quota.db`、安全目錄外路徑、符號連結及含非示範資料表的既有 DB
- `/api/*` 僅允許 GET；其他方法回傳 HTTP 405
- 所有姓名與案件均為假資料
- 核准、補件、儲存只在瀏覽器內模擬，不會傳送 LINE 訊息
