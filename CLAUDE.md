# 一日樂食 openclawbot — 給 Claude 的工作說明

Jason（老闆）用中文溝通，回覆一律用中文、口語、先講結論。他用手機測試，看不到程式碼。

## 環境

| | 南京店（正式） | 電腦營養（測試） |
|---|---|---|
| 分支 | `main`（自動部署） | `staging`（自動部署） |
| Railway project | 06eaa7f5-65a3-4489-9ff5-56318203ce67 | 1220dbe3-b2ea-4119-b21b-0ccdb86a4286 |
| environment | d8770213-033d-4344-8bd6-4c5488f4c9a6 | 82b0ba15-61ce-4a89-b37c-585c29ecedee |
| service | 863ca622-9d37-4c79-a109-1ff74c19466c | a7fa088c-1fe6-4aa9-b9d7-1b5858691bcd |

- staging 的 Google Sheet：1neHTvoOQSOp5Rv7jkT6Ou7yNliELy-ae7woHy6WW6L8
- Railway 上的 Python stdout 有緩衝，log 會在下一個 request 才出現。
- 出單機照 Google Sheet 印單，改期一定要寫回 Sheet。

## 部署流程（一定照做）

1. 從 `origin/staging` 開 feature 分支 → 改 → 跑完整測試，和 baseline 比對，不能有新的失敗：
   `DATA_DIR=$(mktemp -d) timeout 900 python3 -m pytest -q -p no:cacheprovider --timeout=60 --continue-on-collection-errors -rfE -o addopts=""`
   （main 上既有約 175 failed + 6 errors、staging 約 152 + 6，都是舊的；比對 FAILED/ERROR 清單，不是比數量）
2. 推到 `staging` → 告訴 Jason 用手機測什麼。
3. Jason 明確說「上」才推南京店：從 `origin/main` 開 release 分支，cherry-pick 或帶入已測過的改動，再跑一次完整測試，fast-forward 推 `main`，然後看 Railway 部署狀態和 log。
4. 避開 10:00–14:00（午餐尖峰）部署南京店。
5. feature 分支也要 push 到 GitHub 備份（stop hook 會檢查）。

## 規則

- 只有 Claude 推 staging／main；其他 AI 只做分析。
- 不把任何密碼、金鑰寫進文件或 commit；不要讀 Railway 的變數清單（裡面有密鑰）。設定單一變數可以。
- 正式資料（南京店 DB／Sheet）要改，先問 Jason。
- Grok 負責每天備份南京 SQLite；備份的 Drive 資料夾含顧客健康資料，不可分享。Grok 的 SSH key 留在 Jason 電腦。
- 請 Jason 找其他 AI 幫忙時，要避免兩個 AI 做重複的事。
- Hermes 已退役。

## 已定案的產品規則（2026-10）

- **包月三狀態**：午餐 11:30–14:00、晚餐 17:30–20:30 問「吃了嗎」；餐期過後自動算已吃並寫進飲食紀錄；「沒吃」只能當天 23:59 前改。背景工作每 5 分鐘跑（env `PLANNED_MEAL_AUTO_EATEN_ENABLED`）。
- 午晚兩個便當一起送；午餐時可選「吃了 A／吃了 B」。
- **取餐三選一**：🛵 一般外送（原運費，約 11:30–12:00，盡量 12:00 前送達）／🐦 晚鳥外送（運費半價無條件進位，13:00–14:00，不指定時間）／🛍 自取（每週 3 天以上送蛋白補充）。四週運費折抵已取消。
- **運費級距**：≤1km $30、≤1.5km $35、≤2km $40、≤2.5km $50、≤3km $60、>3km 不送。一天一趟。
- **順風車**：和同一種外送的現有客人相比，繞路 ≤7 分鐘就每趟再折 $10。續約不自動沿用，續約重新估價時重新判斷。
- **晚鳥客人午餐時段**改成 13:00–15:30（`subscription_orders.delivery_type='晚鳥'`）。
- **外食估算**：沒寫克數用一般份量估，並設上限（便當／飯 ≤850kcal P≤35、麵 ≤750 P≤35、其他 ≤900 P≤40），顧客自己寫克數不壓。
- 自家菜單的營養以 menu.csv 為準（已校正）；聊天問菜單營養要照 menu.csv 回答。
- **2026-11-11 起不再用營養師名義**（營養師沒有合作了）：對客人一律叫「三日飲食分析」，AI 整理＋店家（Jason）看過才送；報告欄位「店家的話」，報告底部有「不是醫療或營養師診斷」聲明；AI 擬稿禁止出現「營養師／醫師」。工作台內部名稱沒改。
- **三日飲食分析的算法（2026-11-12）**：`health_check_insights.py` 先由程式算好指標（餐次分布、包月便當 vs 便當以外、飲料與點心、蛋白質差距換成食物、資料可信度），並挑出「本週重點」一件事；AI 擬稿只把指標寫成人話，不重新計算。priority 欄第一行固定「這週只做這一件事：…」。
- **三日健檢**：系統先整理三天數據（`dietitian_health_check_initial_draft.py`，是規則算出的數據觀察，**不是 AI 寫的建議**），工作台有「AI 擬稿」按鈕（`dietitian_health_check_ai_draft.py`，OpenAI，env `HEALTH_CHECK_AI_DRAFT_MODEL` 預設 gpt-4o；去識別、不擬個人點評、不儲存，同案 20 秒冷卻、每小時 40 次），營養師修改並核准後才發送；之後營養師持續追蹤是另外付費。不做健康資料同意書、不做慢性病篩檢（Jason 決定）。
- **營養師提醒**：每天 21:00 推清單（吃太少／蛋白質不足 <60% 連 3 天、7 天體重掉 >1%、連 3 天沒記錄），只看昨天往前，不看當天；同人同原因 3 天內不重複。env `DIETITIAN_ALERTS_ENABLED`。管理員指令「#營養師提醒」手動查看。
- **包月首批試跑**（`first_batch_trial.py`，env `FIRST_BATCH_TRIAL_ENABLED`、`FIRST_BATCH_TRIAL_LIMIT` 預設 15）：只限從沒開通過的新客，以人計名額；送出表單建立訂單就佔位，拒絕／逾期釋放；首期**只有餐費** 9 折（捨去），運費不折，續約原價。付款期限＝開始日前一天（週日）20:00：當天 12:00 提醒已核准未付款者，20:00 未開通就標 `expired` 釋放；未核准的單不自動取消，只通知管理員。逾期單客人若其實已付款，直接 `#開通訂單`。管理員看名額：`#首批名額`。
- **包月開始日**：下一個週一；週日 20:00 後送出排到再下一個週一。
- **包月介紹頁**（`plan_landing.py`）：`/` 與 `/plan` 一頁式介紹，所有按鈕導回 LINE（`@a25080337`，帶入「包月方案」）；首批名額即時讀 DB，`/api/first-batch/seats` 只回數字。照片在 `static/plan/`（白名單）。預計綁 Cloudflare 網域 deliexpress-ai.com。
- **包月一餐價格區間** $140–220（`SUBSCRIPTION_MEAL_LOW/HIGH`，估價與介紹頁共用）。三日健檢**每人一次**（第一次訂包月）。
- **南京店健檢設定（2026-10-24）**：LINE Login 頻道「一日樂食(南京店)」`1654353734`。營養師工作台 LIFF `1654353734-ymoTnGmL` 已開（`DIETITIAN_HEALTH_CHECK_*`，名單：Jason `U6c2728f48df81e32dd796d6ea0969f81`＋營養師瀞萱 `Uae8f38df283375f1af24564550b0a9ac`，2026-11-08 加入；每日提醒清單兩人都會收到）；顧客健檢 LIFF `1654353734-uPVw5UvX` 已建但**還沒開**（`VIP_HEALTH_CHECK_LIFF_ID`／`_LINE_LOGIN_CHANNEL_ID`／`VIP_HEALTH_CHECK_ENABLED` 未設），開之前要確認南京店改期 LIFF 也在同一頻道（程式要求）。營養師加名單：她在南京店傳「營養師報到」，UID 會推給老闆；`DIETITIAN_HEALTH_CHECK_ALLOWED_UIDS` 與 `_COMMAND_ALLOWED_UIDS` 必須一致（逗號分隔）。
- **指定品項**（`fixed_meal_picks.py`，2026-11-06）：包月表單選填題「想固定吃的餐點（選填，可複選）」，題型「核取方塊格狀」：列＝蛋白質（豆腐、鷹嘴豆、雞肉、豬肉、牛肉、番牛、鱸魚、鮭魚，**順序不能改**，`GRID_ROWS`）、欄＝便當、食蔬、低碳、沙拉、番茄麵、青蔬麵；品名＝列＋欄。也相容一般核取方塊（完整品名）。勾 2 樣以上輪流排（同天不重複）；只勾 1 樣＝每天午餐固定、晚餐自動配；含禁忌或菜單沒有的略過並通知；全部不能用就自動配餐。有指定的不強制每週食蔬。價格照實際排到的算。
- 非 VIP 問營養問題不回應（只有包月方案介紹、我的方案、找客服、估價、開通）。

## 後端整修（2026-11-12，分支 fix/backend-cleanup，只上 staging）

- 週課表 `weekly_coach_runs.py`：同人同週只跑一次 AI／寫一次 Sheet；「重排下週課表」才重新生成。`run_weekly_coach` 回傳 (ok, 訊息)。
- `user_memory` 存 SQLite（`user_memory_store.py`）：每人 12 則、每則 2000 字、7 天過期、最多 5000 人。
- SQLite 連線一律用 `db_session(sqlite3.connect(...))` 或 `closing(...)`（`db_util.py`）；`with sqlite3.connect() as conn` 不會關連線，不要再用。啟動會檢查 DB 是否在 Railway Volume（兩邊都掛在 `/app/data`）。
- `ADMIN_SECRET` 沒有預設值了，沒設定時 `/api/lobster/*` 一律 401。**推南京店前要先確認南京店有設 ADMIN_SECRET**（不能讀變數值，請 Jason 確認）。
- 其他小模組還有約 40 處 `with sqlite3.connect` 沒改。
- 週課表寫 Sheet 走 `_write_weekly_plan_to_sheet`（只在寫入時拿 workbook fence、保護內重讀列號）；讀表用 `_sheet_records_tolerant`（標題列空白欄不會炸）。
- **Sheet 錯位的原因**：`append_rows` 沒帶 `table_range="A1"` 時 Google 會把新列貼到右邊欄位（staging Master_API_View 的 11 月訂單都錯位在 U 欄、AO 欄之後）。訂單正式化與週課表已改成錨定 A 欄；staging 舊的錯位列還沒清，南京店的表要請 Jason 確認有沒有同樣狀況。

## 待辦

- 健檢全流程（記三天→系統初稿→營養師撰寫核准→客人收到）還沒實測過；首批開跑前要在電腦營養跑通，再開南京店顧客健檢。電腦營養測試捷徑：管理員傳「#健檢測試 <UID>」或「#健檢測試 我」（`health_check_qa.py`，只在 APP_ENV=staging 有效）。
- 圖文選單：先用通用一套，下排「包月方案｜(名稱未定)｜記錄飲食」；系統認得「首頁／今日進度／我的狀態」可開今日總覽。之後人多再做 VIP 專屬選單（Messaging API per-user rich menu）。
- 包月續約優惠（建議：提早續約送三日健檢、續約提醒附本期成果）。
- 改期期限規則；草稿／紀錄卡來源文字統一；非管理員改期頁不應呼叫管理員 API。
- staging 舊測試資料清理（10/05→11/03、11/04 改期請求、許先生錯位列 27–47）。
- 順風車每次估價最多約 31 次 Google Maps 呼叫，客人多了要加快取。
- Hermes 的憑證撤銷（Jason 處理）。
