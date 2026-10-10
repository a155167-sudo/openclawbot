# Round 3 真 AI 驗收 rubric（v2，首次 live 前契約修正版／凍結版）

v1 原檔與 SHA 保存在外部 evidence `round3/eval-v1/`。本 v2 在**第一次 live provider 呼叫前**修正，原因是 v1 將「官方能力顯示 unit mismatch 後，以請求同單位做 AI 每100單位備援」誤寫成強制澄清；本次不是看過 live 結果後調整門檻，亦未改動任何官方 nutrition golden。

## 目的與不可跨越的邊界

本題庫評估「AI 語意解析 → TFDA 相容 lookup → 真 miss 才可做每 100 單位 AI 營養 → 程式依 `amount / 100` 縮放」的公開 pipeline。它不是產品整合已完成的聲明，也不把 fixture/harness 當真 AI。

- `cases.json` 共 **30 品項 × 3 自然說法 = 90 題**；每品項恰有三題。
- expected 與本 rubric 必須在第一次 live provider 呼叫前凍結。看到結果後不得修改 expected、容差或 prompt 來湊分；任何後續修訂必須建立新版本、新 SHA，且保留舊版。
- 本輪 v1 的營養金標只採官方 TFDA ZIP 中明示的「每100克含量」。`g` 與 `ml` 不互換；`每單位重 240.0克` 不是 240 ml，也不是密度。
- 未知便當／手搖配方、未定義碗杯大小及無密度的 ml 題可評解析與路由，但營養為 `NOT_SCORED`，不得算 PASS。
- runner 的 `harness` 模式只驗證報告結構與評分程式，報告必須標為 `HARNESS_FAKE`，不得引用為真 AI 準確率。

## Adapter contract

以 `python3 evaluation/round3/runner.py --adapter module:function ...` 依賴注入。runner 本身不 import `server.py`、不碰 live DB、不呼叫 LINE。adapter 必須是專為評估設計、無持久化副作用的函式：

```python
def run_case(user_text: str, context: dict) -> dict:
    return {
      "execution": {"kind": "live_ai|fake_harness", "provider_calls": 1, "estimated_cost_usd": 0.001},
      "raw": {"semantic_parse": "provider raw/redacted", "nutrition_fallback": None},
      "parsed": {
        "intent": "meal_log|other|clarification",
        "meal_slot": "早餐|午餐|晚餐|點心|",
        "items": [{
          "food_name": "白飯", "food_state": "熟白飯", "amount": 150,
          "unit": "g|ml|natural|serving|package", "portion_assumption": ""
        }],
        "clarification": ""
      },
      "basis": [{
        "status": "official_match|unit_mismatch|fallback_eligible|clarification",
        "basis_amount": 100, "basis_unit": "g",
        "unit_warning": "unit_mismatch 時必填；說明官方單位與 AI 同請求單位備援",
        "nutrition": {"calories_kcal": 183, "protein_g": 3.1, "fat_g": 0.3, "carbohydrate_g": 41.0}
      }],
      "final": [{
        "amount": 150, "unit": "g",
        "nutrition": {"calories_kcal": 274.5, "protein_g": 4.65, "fat_g": 0.45, "carbohydrate_g": 61.5}
      }],
      "source": [{"type": "official|ai", "publisher": "TFDA", "food_code": "A0550601", "basis_unit": "g"}],
      "model": {"semantic": "actual-model-id", "nutrition": None}
    }
```

live adapter 的第一參數只會收到 `user_text` 字串，不能收到 case/expected；不得用 expected 塑造 provider output。harness fake adapter 才可收到完整 case 以驗評分器。`raw` 可是 JSON 或字串，但不得是假造的 provider 回應。敏感 request headers、API key、token 與個資不得寫入報告。adapter 應提供實際 provider/model identity；缺失會記為 schema/trace failure。runner 另行記錄 latency 與 exception，不信任 adapter 自報 latency。

## 維度與判定

### A. 語意解析（每題可評）

1. `intent` 必須等於 expected。
2. `meal_slot` 必須等於 expected；「下午」正規化為 `點心`。
3. 不得漏品項或只取第一項；本 v1 每題 expected `item_count=1`。
4. `food_name` 經 Unicode NFKC、去空白與 ASCII casefold 後，須命中 `acceptable_food_names` 之一。這是名稱正規化，不允許用 expected 回填 provider output。
5. `amount`：數值題採絕對誤差 `1e-9`；未明示可量化份量的題 expected 為 `null`。自然單位的「一／兩／半」可分別為 `1/2/0.5`。
6. `cc` 必須正規化為 `ml`；`公克/克/g` 為 `g`；碗、杯、顆、根、份、個保持 `natural`，不可靜默猜成 g/ml。
7. `portion_assumption`：
   - `empty`：不得添加改變題意的假設；空字串可接受。
   - `nonempty`：須留下非空且與份量／配方相關的假設。
   - `clarification`：`parsed.clarification` 必須非空，且不得產出看似確定的最終營養。
   - `nonempty_or_clarification`：上述任一可接受。
8. 食物狀態另列人工／半自動稽核：輸出不得與 expected `food_state` 衝突（如把「生重」改成熟重、把無糖改全糖）。runner 只做非空與明顯生/熟否定衝突檢查，不把自由文字的模糊語義冒充完全自動準確率。

報告提供：字段正確數／字段可評數、整題解析 exact-match 數／90，以及 food-state review queue。字段缺失是 FAIL，不是不可評分。

### B. 來源與單位路由（每題可評）

expected routing 四類：

- `official_match`：精確食物狀態且單位相容後命中 TFDA；`source.food_code`、publisher、`basis_amount=100`、`basis_unit=g` 必須符合金標。
- `unit_mismatch`：官方 capability 顯示食品存在但單位不相容（例如只有 `g`、請求為 `ml`）。不得把官方 `g` 當 `ml`；必須保留非空 `unit_warning`，並改由明確標為 `source.type=ai` 的**每100請求同單位**備援，最後由程式乘 `amount/100`。這不是強制澄清。
- `fallback_eligible`：確實沒有相容官方金標但已有足夠解析資訊，必須驗證 AI actual source、`basis_amount=100`、basis 與請求同單位、四個營養欄完整，以及程式縮放。這只代表路由／契約正確，不代表 AI 營養準確。
- `clarification`：份量／容量／狀態／配方不足。必須澄清，且不得偽造 final nutrition。

來源路由準確率 = routing exact-match / 90。`unit_mismatch`/`fallback_eligible` 若冒充 official、帶 official food code、basis 不是 100、basis 單位不同、缺四營養欄，均 FAIL；任何 `g→ml` 偷換直接是 critical failure。

### C. 每 100 單位食品實質準確度（只納入 `SCORED`）

v1 有 69 題 `SCORED`、21 題 `NOT_SCORED`。分母只能是 runner 實際完成且具獨立相容金標的 `SCORED` 題；`NOT_SCORED` 永遠不進分子或分母，也不得顯示 PASS。

對 `SCORED` 題，先核對 exact food code/state/source/basis，通過後才比較四個營養欄位。TFDA 金標範圍上下限相同，因來源只發布該點值；為容納序列化／四捨五入，runner 判定容差固定為：

- 熱量：`max(2 kcal, gold × 2%)`
- 蛋白質、脂肪、總碳水：`max(0.2 g, gold × 2%)`
- gold 為 0 時只採上述絕對容差，不得把缺值當 0。

每 nutrient 分別報告 within-tolerance；每題四欄全通過才是 per-100 item pass。缺欄、非有限值、負值、basis/source 錯誤均 FAIL。

`cases.json` 的 `ranges` 不是 AI 生成範圍，而是 TFDA 官方列的逐欄值；來源 ZIP SHA、food code、sample name、欄位名與狀態都隨題保存。容差是測量判定規則，不改寫來源範圍。

### D. 比例計算（與食品實質準確度分開）

對 `SCORED` 且數值 g 題，以及 `unit_mismatch`/`fallback_eligible` 的數值題評估。後兩者即使食品實質營養為 `NOT_SCORED`，程式比例計算仍須評分：

`expected_final[nutrient] = gold_per_100[nutrient] × parsed/requested_amount / 100`

final scaling 以 `max(0.02 單位, expected × 0.1%)` 比較。此維度回答「程式 amount/100 是否正確」，不得用來掩蓋錯食物或錯 per-100 值：

- `scaling_arithmetic_pass`：actual final 是否等於 **actual basis × amount/100**。
- `final_gold_pass`：有獨立 golden 時，actual final 是否等於 **gold basis × amount/100**；無 golden 的 AI fallback 顯示 `null`，不得拿 AI 自己宣告的 range 冒充食品品質驗證。

兩者分開報。錯誤 basis 但自洽縮放可通過前者，仍不得通過食品／final gold 準確度。

## `NOT_SCORED`、`BLOCKED`、`FAIL` 必須分開

- `NOT_SCORED`：題目按凍結設計沒有獨立相容營養金標（例如 g-only TFDA 對 ml、未知便當配方、未定義大杯）。仍評 A/B；營養欄顯示原因，**不是 PASS**。
- `BLOCKED`：本來可評，但因 timeout、rate limit、max calls、budget cap、adapter exception、provider refusal 或無輸出而未取得結果。不得從分母消失後宣稱全綠；報告同時列 eligible、completed、blocked。
- `FAIL`：有輸出但不符合 schema、解析、路由、來源、營養或 scaling 規則。
- 只有 live 模式、沒有 critical failure、且指定門檻達成時，外部決策者才可宣稱接受；runner 不自行把未設定的產品門檻發明成 release PASS。

## 隔離、成本與保存要求

runner 每題在新的 subprocess 與 temporary working directory 執行；預設只傳安全環境變數，清除 `LINE_*`、DB URL/path、webhook/token 等。只有命令列明列的 `--pass-env NAME` 會傳給 adapter，值永不輸出。adapter/module 路徑由 parent 提供，不要求使用者貼 key。

必要選項：`--max-calls`（明確指 **adapter invocation 次數**，不是 adapter 內 provider call 數）、`--requests-per-minute`、`--timeout-seconds`、`--budget-cap-usd`、`--estimated-cost-per-call-usd`。超過限制的題標為 `BLOCKED`。runner 每題完成或 BLOCKED 後立即 append checkpoint JSONL；provider/adapter error 原樣保留在該題 `error`（僅秘密遮罩）。每題報告保存 `raw/parsed/basis/final/source/model/latency/error`，以及 adapter 回報 provider calls/cost；adapter 未回報 cost 時彙總必須是 `unknown`，不得顯示實花費 0。live run 必須使用新輸出檔，不覆寫既有 raw report。

禁止：import 產品 `server.py`、連 live DB、寫 food logs、呼叫 LINE、修改 quota、在命令列放秘密、把 harness 報告稱為 AI 實測。runner 提供的是隔離框架；adapter 若違反契約，其 run 無效。

## 凍結與變更程序

`FREEZE.sha256` 以 SHA-256 記錄 `cases.json` 與 `rubric.md`。runner 啟動時先驗證；不一致即停止，除非是維護者刻意建立新版本，不能用 bypass 執行 v1。官方 ZIP 也會核對 `cases.json` 內記錄的 SHA。第一次 live run 的 prompt/adapter 版本、git commit（若有）、provider/model 與 freeze hashes 都應寫入 report。
