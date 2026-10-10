# 第四輪手機驗收評準 v5（新版本 sidecar；舊金標不動）

## 狀態與範圍

- 本檔與 `user_acceptance_v5.json` 是本次**有限 staging live 前修正並定版**的使用者驗收 sidecar；沿用不看 AI 輸出、不動態放寬的預先固定數值規則，不宣稱較早 v5 草稿已正式凍結。
- 只重新解讀第三輪 33 筆 `missing → missing`，不修改、不覆寫、不重算舊 `cases.json`、`rubric.md`、`runner.py`、`live_adapter.py` 或歷史 LIVE-REPORT。
- 本次不跑第四輪、不碰產品、DB、LINE、部署或 reference data。
- v5 目標不是追求 100% official 命中：未走安全 DB match 時，**合理 AI 數字 + 真卡片可見「AI估算」**可通過；資訊不足時，真正 pipeline 澄清且沒有假確定營養亦可通過。

## 三類新分類（33 筆）

| 類別 | 數量 | v5 行為 |
|---|---:|---|
| 1. DB 已有相容列、純名稱命中缺口 | 11 | 輸入與 parsed state 足夠；可提出 exact alias／名稱正規化，但本工作不改 data。 |
| 2. parsed 資訊不足 | 21 | 可能是原文不足，也可能原文其實足夠而模型漏 state/品種；真正澄清可 PASS，或卡片明示 scope 後 AI估算並通過獨立範圍。 |
| 3. 獨立同單位範圍不足 | 1 | 可透明 AI估算，但沒有獨立範圍時只能 `UNVERIFIED`。 |

> 33 筆保持互斥。第三輪的 fallback 不等於「DB 本來沒有」：蒸鯖魚、生毛豆、生花椰菜、生燕麥、生甘藍均已有相容列，只是名稱夾量；菠菜、乾麵、高麗菜、水果小黃瓜則是原文足夠但模型漏 state/品種。

### 逐筆分類與依據

| case | 類 | 固定 benchmark | 分類依據／必要假設 |
|---|---:|---|---|
| R3-03-1 | 1 | 全脂鮮乳 g | 「全脂鮮奶」與「全脂鮮乳」同 scope。 |
| R3-04-2 | 1 | 生去皮雞胸 g | 去皮、生、雞胸完整，僅詞序/「肉」字差。 |
| R3-18-1 | 1 | 去皮去核蘋果 g | 食品與可食部完整，名稱只是夾帶量/處理詞。 |
| R3-21-3 | 1 | 乾麵條 g | 「乾麵」為同 scope 常見同義。 |
| R3-26-3 | 1 | 生豬小里肌 g | 「生小里肌」scope 完整，只省略豬字。 |
| R3-27-3 | 1 | 生冷凍牛肉火鍋片 g | 「生火鍋牛肉」為同 scope 詞序。 |
| R3-03-2 | 2 | 鮮奶 ml：UNVERIFIED | 未明示全脂/品牌，且 TFDA 列為 g，不能轉 ml。 |
| R3-04-1 | 2 | 生去皮雞胸 g | 未說去皮；須問皮別或卡片明示去皮假設。 |
| R3-04-3 | 2 | 生去皮雞胸 g | 未說去皮；須問皮別或卡片明示去皮假設。 |
| R3-08-2 | 2 | 半糖珍奶 ml：UNVERIFIED | 有 700ml/半糖但奶茶、珍珠、冰量/配方仍未知。 |
| R3-10-1 | 2 | 生去皮黃肉甘藷 g | 未說黃肉、去皮；須澄清或明示這兩項 AI 假設。 |
| R3-11-1 | 2 | 生去皮去骨大西洋鮭 g | 未說品種、皮骨。 |
| R3-11-2 | 2 | 生去皮去骨大西洋鮭 g | 已知大西洋/去皮，但未明示生熟與去骨。 |
| R3-11-3 | 2 | 生去皮去骨大西洋鮭 g | 只知生鮭魚，品種與皮骨不足。 |
| R3-17-1 | 2 | 生紅色系大番茄 g | 未說大果/去蒂可食部。 |
| R3-18-2 | 2 | 去皮去核混色蘋果 g | 「果肉」未可靠固定品種與去皮去核稱重。 |
| R3-23-2 | 2 | 生甜玉米粒 g | 未明示甜玉米品種。 |
| R3-24-1 | 2 | 生帶膜花生仁 g | 未明示帶膜/去膜。 |
| R3-24-3 | 2 | 生帶膜花生仁 g | 未明示帶膜/去膜。 |
| R3-26-2 | 2 | 生豬小里肌 g | 豬里肌不必然是小里肌。 |
| R3-27-2 | 2 | 生冷凍牛肉火鍋片 g | 牛肉片不必然是冷凍火鍋片。 |
| R3-29-2 | 2 | 生水果小胡瓜 g | 小黃瓜未明示水果品種。 |
| R3-30-2 | 2 | 中脂無糖纖維優格 g | 未明示纖維強化。 |
| R3-12-1 | 1 | 蒸鯖魚 g | DB 已有相容列；state 完整，food_name 夾帶100克。 |
| R3-14-2 | 1 | 生毛豆仁 g | DB 已有相容列；state 完整，food_name 夾帶80g。 |
| R3-16-2 | 1 | 生花椰菜 g | DB 已有相容列；state 完整，food_name 夾帶200g。 |
| R3-19-2 | 1 | 生燕麥乾重 g | DB 已有相容列；state 完整，food_name 夾帶乾重80g。 |
| R3-28-2 | 1 | 生甘藍 g | DB 已有相容列；state 完整，food_name 夾帶200g。 |
| R3-15-2 | 2 | 生菠菜 g | DB 已有；原文「還沒煮」足夠，模型漏 food_state。 |
| R3-21-2 | 2 | 乾麵條 g | DB 已有；原文明示乾重，模型漏 food_state。 |
| R3-28-3 | 2 | 生甘藍 g | DB 已有；原文明示「生」，模型漏 food_state。 |
| R3-29-1 | 2 | 生水果小胡瓜 g | DB 已有；原文明示「水果」，模型漏品種詞。 |
| R3-08-3 | 3 | 無糖紅茶 ml：UNVERIFIED | 食品/500cc 足夠，但目前沒有固定的相容每100ml來源。 |

## 六個 alias 候選（只提案，不改資料）

1. `全脂鮮奶` → `L01021`，只限全脂 3.0–3.8% 鮮乳、g；不能擴成泛稱鮮奶或 ml。
2. `去皮生雞胸` → `I04024`，只限生、雞胸、去皮、g。
3. `蘋果，去皮去核` → `D32004`，只限去皮去核可食部、g。
4. `乾麵` → `R2000101`，只限未煮乾麵條、g。
5. `生小里肌` → `I0306101`，只限生豬小里肌、g。
6. `生火鍋牛肉` → `I01101`，只限生冷凍牛肉火鍋片、g。

不得把量詞/數字整串永久存成 alias；實作 alias 前仍需 stateworker/data owner 審批。

## 獨立合理範圍

### 固定方法

- 來源：固定 TFDA ZIP（SHA-256 `755d2f...c6064`）中的 54-row catalog；只取食品、狀態、可食部、basis unit 相容列。
- 若只有單一相容列，v5 hard plausibility envelope 在第四輪前固定為：
  - 熱量：來源值的 70%–130%。
  - 蛋白質、脂肪、碳水：來源值的 60%–140%。
  - 官方明列 0：0–1g；缺值不是 0。
- 這是使用者驗收用的 hard bound，不冒稱官方信賴區間。它不讀本輪 AI 輸出，也不使用 AI 自己宣告的 range。
- 若同食品有多個狀態/可食部相容來源，才可先形成逐欄來源 min/max；不相容列不得混入。
- 完整逐食品數值固定於 `user_acceptance_v5.json`，helper 只讀該檔，不動態放寬。

### 白飯與地瓜

- 白飯只可比 `A0550601` 的**熟白飯、白米加水電鍋烹煮**；不得套到生米。
- 地瓜 benchmark 只可比 `B0400601` 的**生、去皮、黃肉**。只說「地瓜」時須澄清，或卡片明示「AI 假設生、去皮、黃肉」；熟地瓜/帶皮地瓜與此 benchmark 不相容。

### ml 與無糖豆漿 500ml

- 絕不把任何 TFDA 每100g列當每100ml，也不假設密度 1。
- v5 固定一個真包裝標示 benchmark：義美無加糖豆奶，條碼 `4710126194403`；包裝照片明列每份 **250ml**：84.9 kcal、P 8.5g、F 4.3g、C 3.0g。包裝直接標示每100ml為 34.0 kcal、P 3.4g、F 1.7g、C 1.2g。
- 來源照片：<https://images.openfoodfacts.org/images/products/471/012/619/4403/nutrition_en.5.400.jpg>
- 此 benchmark 只限該品牌產品，是同類豆奶的合理性參考，不是 generic 台灣豆漿官方真值，也不代表使用者喝該牌。`R3-02-2/4/5` 是未知品牌合理估算，**不得**新增「卡片必須寫義美」的手機阻擋；在補到第二個獨立每100ml真包裝標示形成範圍前，數字透明但只能 `UNVERIFIED`。

## v5 判定

### 可 PASS

- 類 2：pipeline 真正回 clarification、有非空澄清訊息，且沒有 final nutrition。
- 安全 official：食品 scope/state/可食部/單位與 food code 相容，且 final 是 basis × amount/100。
- AI：`source.type=ai`、basis=每100請求同單位、四欄完整、final 為程式線性縮放、卡片可見 `AI估算`，且四欄全在預先固定的獨立範圍內；類 2 另須可見 scope 假設。

### `UNVERIFIED`，不可算 PASS

沒有獨立同單位範圍時，即使 AI 數字看似合理或自洽，也只能 `UNVERIFIED`。目前明列：泛稱鮮奶 ml、半糖珍奶 ml、無糖紅茶 ml。來源不足屬透明非 critical finding，但不可假綠。

### 零容忍 hard fail

1. 生/熟、去皮/帶皮、帶膜/去膜、乾重/熟重錯配。
2. g↔ml 偷換。
3. AI 冒稱 official，或掛錯 TFDA food code。
4. 未知/缺值補 0。
5. 數量未確定卻硬產 final nutrition。

舊 raw/parsed field FAIL 與產品最終安全/可用性是兩層：side report 保留 `legacy_evaluation`，但 v5 verdict 不覆寫舊分數。

狀態判定分兩層，禁止錯配：

- 只有**實際採用的 source/official 描述**與原文／parsed state 明確生熟、皮膜或乾濕重相反，才記 `state_or_edible_part_mismatch` critical；生熟與皮膜皆雙向檢查，`花生` 的字內「生」不算狀態。
- 固定 benchmark 與本次食品狀態不同，只代表該數值範圍不可比較，判 `UNVERIFIED / benchmark_scope_not_comparable`，不可偽稱產品已跨生熟取錯 source。固定數值範圍不因 AI 輸出而放寬。

## Helper 與輸出

```bash
python3 evaluation/round3/user_acceptance_v5.py \
  /path/to/round4/LIVE-REPORT.json \
  /path/to/round4/USER-ACCEPTANCE-V5-REPORT.json \
  --card-evidence /path/to/round4/real_renderer_cards.json
```

helper：

- 啟動先核對四個歷史檔 SHA；不同即停止。
- 只讀 LIVE-REPORT 的 `raw/parsed/pipeline/basis/final/source`，並把這些欄位完整放進獨立 side report。
- `--card-evidence` 接受 `case_id -> {user_text 或 user_text_sha256, flex, pipeline_status}` map，或 `{"results": [...]}`。`flex` 必須是 server renderer 的 `actual.as_json_dict()`，不得自行合成；只掃 Flex 元件的 `text` 欄位，source/type/pipeline 任意字串都不能證明卡片可見。case 必須以相同 user_text 或其 UTF-8 SHA-256 綁定，且 pipeline status 一致。
- 不信任舊 parsed FAIL 作 v5 最終安全 verdict，也不讀 AI 自己的 min/max 定容差。
- 不覆寫輸入報告或已存在輸出。
- summary 的 `missing_33_acceptance_gate` 僅要求這 33 筆全 PASS；不是整體部署結論。`hard_safety_gate` 另列，`phone_core_gate` 明示本 helper 未評。豆漿手機核心三例由第四輪 owner 父控納入 staging phoneflow gate；alias 六案不列為本輪 mandatory。
