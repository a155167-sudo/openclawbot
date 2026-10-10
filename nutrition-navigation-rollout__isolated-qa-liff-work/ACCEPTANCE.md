# 電腦營養 staging 改期驗收清單

## 已可驗收的本機 staging candidate

檔案：`customer-reschedule-liff.html`

瀏覽器驗收結果：

- 顯示「電腦營養測試」與 staging 標示
- 來源日同時顯示午餐與晚餐
- 選擇已有排餐日會拒絕
- 選擇包月最後日後第 30 天可預覽
- 選擇第 31 天會拒絕
- 按「送出申請」只顯示「等待營養師核准」
- 來源餐點不會在客戶端被搬走

## API contract 驗收

Router 只在 `APP_ENV=staging` 且明確設定 `CUSTOMER_RESCHEDULE_LIFF_ENABLED=true` 時掛載。

Endpoints：

- `GET /customer-reschedule`
- `GET /customer-reschedule/preview`
- `POST /customer-reschedule/pending-request`

身份必須使用：

```text
Authorization: Bearer <LINE ID token>
```

request body 不接受 `actor_id`；owner 由 server 驗證 token 後取得。

## 已驗證

```text
61 passed
Python compile：PASS
```

## 尚未做的真實帳號步驟

在真正打開電腦營養 LINE 測試入口前，仍需 staging runtime 提供：

```text
CUSTOMER_RESCHEDULE_LIFF_ENABLED=true
CUSTOMER_RESCHEDULE_LIFF_ID=<staging LIFF ID>
CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID=<staging Login channel ID>
```

這三個值未設定前，server 不會掛載 route；production 也不會掛載。
