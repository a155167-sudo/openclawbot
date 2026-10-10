# VIP 首次 3 日飲食健檢與 4 週營養師陪跑：整合設計

> 日期：2026-09-03
> 狀態：設計階段紀錄；後端基礎已另行實作，功能入口仍由預設關閉的 feature flag 保護
> 本文件不包含任何 Token、密碼、銀行帳號或正式顧客資料。

## 1. 本輪交付邊界

本輪只完成：

1. 既有入口與資料流盤點。
2. 顧客手機互動雛形。
3. VIP 首次權益、3 日基準健檢、4 週陪跑加購的狀態與資料邊界。
4. 後續正式實作計畫。

本輪明確沒有：

- 修改 `server.py`、`nutrition_system.py` 或正式 DB。
- 新增 LINE 指令、LIFF endpoint 或 Rich Menu。
- 部署 Railway。
- 建立付款 webhook 或自動金流。
- 發送新序號。

## 2. 既有兩個顧客入口

### 入口 A：紀錄飲食 / FOOD LOG

**正式程式可驗證內容**

- `server.py:13018-13021`：訊息 `飲食紀錄` 會開啟今天／昨天選擇卡。
- `server.py:930-949`：卡片標題為「我的飲食紀錄」，選擇後以 `foodlog:v1` postback 載入逐筆紀錄。
- 每筆紀錄可調整份量、修正營養與執行更多操作。
- 文字 `搜尋我的食物` 會進入既有私人食品搜尋流程（`server.py:11806-11840`）。

**顧客用途**

- 文字、照片或食品搜尋後建立飲食紀錄。
- 查看今天／昨天紀錄並修正。

### 入口 B：首頁／儀表板

**正式程式可驗證內容**

- `server.py:13023-13041`：`首頁`、`儀表板`、`我的狀態`、`今日進度` 都會開啟同一儀表板。
- `server.py:5707-5711`：今日安排具有「午餐已吃／晚餐已吃」。
- `server.py:13180-13187`：按下後呼叫 `mark_planned_meal_as_eaten()`。
- 現行儀表板不是單一帳本讀取：今日總計主要來自 `health_profile.today_*` 快取，排餐來自顧客個人 Google Sheet，打卡狀態來自 `planned_meal_checks`，常吃清單來自 `frequent_foods`；只有部分已核准餐點照片直接讀 `food_logs`。正式三日健檢不可照抄這個混合讀取方式。

**名稱限制**

- 目前工作目錄連結的 Railway Token 對應測試 Bot「電腦營養」，LINE GET API 回傳沒有 Rich Menu。
- 因此無法由正確正式 Channel 驗證入口 B 在南京店正式 Rich Menu 上的視覺名稱。
- 正式串接前要以南京店 Messaging API Channel 做唯讀 GET 驗證；在此之前文件只稱「首頁／儀表板（正式 Rich Menu 名稱待確認）」。

## 3. planned meal 正式資料流

`mark_planned_meal_as_eaten()` 現況：

1. 從 `get_dashboard_data(user_id)` 取得當日午／晚餐安排；現行原始來源為顧客個人 Google Sheet 當日列，不是 `nutrition_plans`。
2. 檢查 `planned_meal_checks(user_id, meal_date, meal_slot)`，已確認就拒絕重複。
3. 呼叫 `create_daily_food_log()` 寫入正式 `food_logs`：
   - `meal_slot`：午餐或晚餐
   - `consumed_at`：台北時間
   - `nutrition_snapshot_json`：安排餐點當下熱量與蛋白質
   - `source_type='planned_meal'`
   - `confirmation_status='confirmed'`
4. 從 ledger 回算 `health_profile` 今日投影。
5. 寫入 `planned_meal_checks` 防止同餐重複確認。
6. `recent_meal_logs.food_log_id` 指回同一筆 `food_logs.log_id`。

### 健檢使用原則

- `food_logs` 是飲食事實來源。
- `health_profile.today_*` 只是今日快取／投影，不可拿來做跨日健檢主資料。
- 健檢只讀 `confirmation_status='confirmed'` 且未刪除的紀錄。
- `source_type='planned_meal'` 與照片、食品庫、一般估算紀錄使用同一讀取管線。
- 不再新增「健檢餐點表」，不複製或重新估算同一餐。
- 正式實作時 planned meal 應補穩定 operation key，例如：
  `planned-meal:{user_id}:{taipei_date}:{meal_slot}`，以 DB 唯一約束防止並發雙寫；`planned_meal_checks` 保留作顧客操作狀態。

## 4. VIP 首次權益

VIP 首次開通同時建立一次健檢資格：

- 7 天練習／蒐集窗口；窗口固定從首次 VIP 正式生效時間起算，不等待顧客另按開始，也不從第一筆飲食紀錄起算。
- 在窗口內完成 3 個有效紀錄日。
- AI 做結構化彙整。
- 真正營養師審核一次基準報告。
- 不另發新序號。
- 每一個 LINE user 僅有一次首次基準報告權益；補件與退回不消耗第二次資格。
- `VIP_HEALTH_CHECK_ENABLED` 只控制顧客路由是否曝光；暗部署後的成功 VIP 兌換仍必須保存首次 activation provenance，避免續期錯當首次。
- 暗部署前的歷史 generic VIP 無法可靠重建首次日期，不得從 `usage.last_date` 猜測；正式啟用前必須依 production runbook 完成 Jason 核准的 backfill／no-backfill 決策。

### 有效紀錄日 MVP 定義

建議規則（上線前由營養師確認）：

1. 同一台北日至少 2 餐主要餐點。
2. 顧客需回答當日是否另有飲料、點心或宵夜；「沒有」是有效答案，未回答是缺資料。
3. 最好涵蓋 2 個平日與 1 個假日；若 7 天內無法符合，只標註限制，不自動取消資格。
4. 缺照片不能直接推論沒有吃；營養不完整顯示 `NA`。
5. 被刪除或尚未確認的 food log 不計入。

「至少 2 餐」與「2 平日＋1 假日」是產品建議，不是已獲營養師核准的醫療標準。

## 5. 狀態機

| 狀態 | 顧客畫面 | 可進入條件 | 下一步 |
|---|---|---|---|
| `guiding_7d` | 0/3、顯示剩餘窗口 | VIP 首次正式生效時自動建立 | 沿用兩個既有入口開始記錄 |
| `collecting` | 1/3 或 2/3 | 有效日數至少 1 | 沿用兩個既有入口補記 |
| `ready_for_ai` | 3/3 已完成 | 3 個有效日 | 鎖定來源版本並排 AI 工作 |
| `ai_processing` | AI 整理中 | 背景工作已領取 | 產生事實摘要與資料限制 |
| `dietitian_review` | 營養師審核中 | AI 摘要完成 | 核准、修訂或要求補件 |
| `needs_more_info` | 待補資料 | 營養師要求補件 | 顧客沿用既有入口補記 |
| `report_approved` | 報告已核准、待送達 | 營養師核准不可變快照 | 建立唯一 outbox delivery |
| `report_delivery_failed` | 報告待重送 | LINE 投遞失敗 | 以同一 delivery key 重試，不重算報告 |
| `report_delivered` | 報告完成 | LINE 已成功送達 | 顧客查看報告／自行執行／看加購 |
| `completed` | 首次權益已使用 | 顧客已收到報告 | 續期不得再自動建立一次 |
| `expired_incomplete` | 7 天未完成 | 到期仍不足 3 日 | 依人工展延規則處理，不假裝完成 |
| `coaching_payment_pending` | 等待匯款 | 顧客提出加購 | 客服提供匯款資訊 |
| `coaching_transfer_submitted` | 已回報匯款 | 顧客提供核對資訊 | 進入人工付款審核 |
| `coaching_payment_review` | 等待客服確認 | 客服開始核對 | 確認、拒絕或取消 |
| `coaching_payment_rejected` | 匯款未核對成功 | 人工審核未通過 | 補資料或取消，不開通 |
| `coaching_active` | 4 週陪跑進行中 | 客服確認款項 | 同一 LINE 帳號開通 |
| `coaching_paused` | 服務暫停 | 依正式請假規則 | 恢復或結束 |
| `coaching_refunded` | 已退款 | 客服完成退款 | 關閉加購 entitlement |
| `coaching_cancelled` | 申請取消 | 客服取消或逾期 | 不影響原 VIP／基準報告 |

所有狀態轉換需記錄 `actor_type`、`actor_id`、`from_status`、`to_status`、台北時間與 reason；不得只覆寫單一狀態而沒有稽核軌跡。

## 6. 建議資料邊界

### `vip_health_check_cases`

只存案件狀態，不存重複餐點：

- `case_id`
- `user_id`
- `benefit_key`（固定為 `first_vip_baseline_check`）
- `first_vip_activation_id`（首次生效事件來源／外鍵，不作可變 benefit key）
- `activation_event_key`（首次生效事件冪等鍵）
- `window_started_at`、`window_ends_at`
- `status`
- `valid_day_count`
- `submitted_at`、`report_published_at`
- `created_at`、`updated_at`

必要約束：`UNIQUE(user_id, benefit_key)`；固定 `benefit_key='first_vip_baseline_check'`，確保 VIP 續期不會重發首次權益。

### `vip_health_check_valid_days`

每一列只代表「某個台北日是否符合有效日」，不代表一餐：

- `case_id`
- `local_date`
- `rule_version`
- `qualifying_meal_count`
- `completeness_status`
- `evaluated_at`

必要約束：`UNIQUE(case_id, local_date)`。同一天可在 source refs 保留多筆餐點，但只產生一筆每日資格。

### `vip_health_check_source_refs`

只存來源參照與送審時版本：

- `case_id`
- `food_log_id`
- `food_log_version`
- `local_date`
- `included_reason`
- `source_hash`

不複製餐名、熱量與營養數字；需要顯示時讀 `food_logs`。報告發布後保留來源 hash，辨識顧客後續修改造成的版本差異。

必要約束：`UNIQUE(case_id, food_log_id)`，所以同一個有效日可以包含早餐、午餐、晚餐等多筆來源，但同一筆 `food_log` 不會被重複加入。報告輸入指紋由排序後的 `log_id:version`、3 個日期與規則版本產生；相同指紋不得重建草稿。

### `vip_health_check_reviews`

- AI：只存結構化觀察、資料缺口、可追溯來源 ID，不能把推測寫成事實。
- 營養師：另存 `review_json`、`suggested_values_json`、`limitations`、`approved_by`、`approved_at`。
- 不得覆蓋 `food_logs.nutrition_snapshot_json`。
- 專業欄位先做可修改雛形，需由真正營養師驗收。
- 核准採 `expected_version` 樂觀鎖；舊工作台頁面不可覆蓋新草稿。

### `vip_health_check_reports`

發布版應不可變：

- 做得好的地方。
- 一個優先調整重點。
- 接下來 7 天行動。
- 資料限制。
- `source_manifest_hash`、版本與發布人。

必要約束：`UNIQUE(case_id, report_kind, report_version)`。核准快照、投遞意圖與唯一 delivery key 應在同一交易建立；核准後若原始飲食紀錄改動，只能建立修訂版或差異記錄，不可靜默覆寫已送達報告。

### `dietitian_coaching_orders`

與餐食包月訂單分開，避免誤開餐點權益：

- `order_id`、`user_id`、`case_id`
- `product_type='dietitian_coaching_4w'`
- `status`
- `quoted_amount`（由客服／正式商品設定提供，不在雛形猜價格）
- `requested_at`、`payment_reported_at`
- `confirmed_by`、`confirmed_at`
- `starts_at`、`ends_at`

銀行資料不落此表；由既有安全訊息函式在客服核准時傳送。

## 7. 權限與隱私

- 顧客只能以 LIFF ID token 或 LINE webhook 身分存取自己的案件。
- 不接受 query string 傳入 `user_id` 作身分依據。
- 營養師需獨立 allowlist／role，不直接沿用所有 coach 權限。
- 列表 API 不回傳其他顧客不必要的健康資料。
- 圖片仍透過有期限簽章 URL；不可公開檔案路徑。
- 報告發布、補件、人工開通都要冪等。
- 稽核記錄不得包含 Token、銀行帳號或完整敏感健康文字。

## 8. 人工匯款加購

沿用現有訂閱流程的互動模式，而不是資料表與權益混用：

1. 顧客於完成報告頁點「查看 4 週營養師陪跑」。
2. 了解服務後點「索取人工匯款資訊」。
3. 建立 `coaching_payment_pending`，通知客服。
4. 客服核准後才透過正式 LINE 傳送匯款資訊。
5. 顧客匯款後，由客服人工核對。
6. 確認後直接開通同一 LINE user 的 `coaching_active`。
7. 不串 LINE Pay、綠界、藍新、Stripe 或任何自動付款 webhook；不另發序號。
8. 不得呼叫現有會產生 `#VIPORDER-*` 的 activated 分支，也不得寫入 `vips`；付款確認必須直接綁定既有 `user_id + add_on_order_id`。

## 9. 正式實作建議順序

1. **先確認產品決策**
   - 正式 Rich Menu 第二入口名稱。
   - 有效日營養師標準。
   - 報告 SLA、補件逾期與 4 週服務內容／價格。
2. **寫 DB migration 測試**
   - 新增上述健康健檢／review／report／coaching order 表。
   - 模擬既有 Railway DB migration、rollback 與 integrity check。
3. **建立純 domain functions**
   - 台北日有效日判斷。
   - food log source manifest。
   - 狀態轉換與冪等。
4. **串 VIP activation**
   - 以首次 activation event 建唯一 entitlement；不得靠新序號。
5. **建立顧客唯讀 API／LIFF**
   - 先只讀進度與報告；「前往紀錄飲食」只作純導航，不建立資格，也不改 `window_started_at`／`window_ends_at`。
   - 只有「索取匯款」是後續受控寫入；7 天窗口仍由首次 VIP 正式生效事件自動建立。
6. **建立營養師 API**
   - 獨立 role、review、補件與發布；保留原值。
7. **加工作佇列**
   - 3/3 後 enqueue AI 摘要，具 operation key 與 retry 上限。
8. **人工匯款整合**
   - 只重用客服核准與通知模式；coaching entitlement 必須獨立。
9. **Staging 驗收**
   - 假使用者、假付款、不接正式顧客。
   - 顧客與真正營養師各走一次全流程。
10. **正式變更前門檻**
    - 修改 `server.py` 前先備份到 Windows Desktop `新資料夾 (2)`。
    - 完整測試、資料庫副本 migration、安全審查與 Jason 明確核准後才部署。

## 10. 待 Jason／營養師決策

1. 正式 Rich Menu 第二入口的實際名稱與動作。
2. 「有效紀錄日」是否固定至少 2 餐，飲料／點心問題是否必答。
3. 7 天未完成 3 日：延長、重新開始或保留未完成狀態。
4. 營養師收到案件後的服務時限。
5. 三日可否不連續，以及跨午夜、補登與改時間後的台北日歸屬規則。起算點已確定：窗口固定從首次 VIP 正式生效時間開始。
6. 核准後飲食紀錄被修正時，是否免費重發修訂報告。
7. 4 週陪跑價格、每週接觸頻率、回覆時段、請假／退款規則。
8. 舊 VIP 是否追溯享有，或只限功能上線後第一次開通者。
9. 基準報告與 4 週陪跑同時購買時，是否共用案件與營養師。
10. 陪跑是否能修改 `nutrition_plans`，以及誰有最終核准權。
11. 哪些疾病、用藥或醫療風險必須停止自動建議並轉真人處理。

## 11. 本輪原型

- 顧客端：`prototypes/customer-vip-3day-checkup-prototype.html`
- 營養師端既有唯讀原型：`prototypes/dietitian-3day-checkup-prototype.html`
- 顧客端驗收測試：`tests/test_customer_vip_checkup_prototype.py`

顧客端原型所有操作只改瀏覽器記憶體，不連 LINE、不讀正式資料、不寫正式 DB、不送出付款申請。
