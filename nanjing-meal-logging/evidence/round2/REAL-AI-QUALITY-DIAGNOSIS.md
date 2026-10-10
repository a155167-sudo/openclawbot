# 500 ml 無糖豆漿：真 AI 品質失準診斷與可驗收修正方案（唯讀）

> 範圍：HEAD `2ebc406` 的唯讀診斷。本文沒有改產品碼、測試碼、資料庫或部署，也沒有呼叫 AI、LINE、真實 DB。本文不宣稱問題已修好。

## 結論

這不是 UI 縮放造成的營養數字錯誤。現役 raw audit 已證明 provider 自己對「無糖豆漿 500 ml」回傳總量基準 `500 ml`，但四項總量只有 `80 kcal / P7 / F4 / C6`，pending DB 原樣保存。現行程式只證明：

1. AI 自稱的 `basis_amount/basis_unit` 與輸入相同；
2. 數值有限、非負、estimate 落在 AI 自己給的 min/max；
3. 熱量與同一份 AI 巨量營養素在 Atwater 關係上沒有明顯矛盾。

它**沒有任何獨立於 AI 的 500 ml 豆漿知識**可驗證絕對量級。觀測值的巨量營養素能量是 88 kcal，和 AI 的 80 kcal 僅差 8 kcal，因此目前會被判為 `consistent`。這只代表「自洽」，不代表「符合食物與份量」。

根因是 **reference capability gap + fail-open quality gate**：`ml` 找不到可靠基準時，流程直接讓同一個 AI 同時扮演估算者與範圍提供者，之後只做自我一致性檢查。加強 prompt、再問同一模型一次或換模型，均不能建立獨立驗證基準。

## 可重現證據與資料流

### 1. 壞值不是顯示層產生

- `server.py:13552-13651`：provider raw JSON 經 audit 後，依序只做 payload 形狀解析、`validate_total_basis`、normalize、`assess_nutrition`，然後直接回傳 estimate。
- `nutrition_estimate_checks.py:7-18`：basis 驗證只比對 AI 回報的 amount/unit 與 request 是否相同。
- `nutrition_estimate_checks.py:20-47`：合理性檢查只驗非負/有限與熱量—巨量營養素內部一致性，沒有 food identity、每 100 ml 量級或外部 reference 比對。
- 唯讀 probe 對事故值的現況結果：`basis=PASS`；assessment 為 `status=consistent, requires_correction=False, macro_energy_kcal=88, difference_kcal=8`。
- 事故值正規化後等於每 100 ml：`16 kcal / P1.4 / F0.8 / C1.2`。這只是換算觀測值，不是新的真值。
- `server.py:15150-15164` 顯示層把 AI 的 min/max 乘 `portion_multiplier`，並把兩端點中點標成「實際入帳」；本次 range 對稱，所以仍顯示 80/7/4/6，並非 UI 把較大的值縮成這組數字。
- `server.py:15351-15360` 確認時也不是寫 AI 的 `estimate`，而是另算 `(min + max) / 2` 入帳。這不影響本次對稱 range 的事故結論，但揭露第二個語意問題：模型的 point estimate 被忽略，range 中點卻被稱作「實際入帳」。未來 point value 必須由核准 source 的明確統計量/規則產生，不可無條件以模型 range 中點代替。

### 2. 現有 reference 明確無法回答 ml

- `nutrition_reference.py:17-31` 僅允許 alias 與 `basis_unit` 精確匹配，再做同單位線性縮放。
- `nutrition_reference_data.json:46-60` 的 TFDA 無糖豆漿資料為每 100 **g**：35 kcal / P3.6 / F1.9 / C0.7；並明載沒有 ml 基準或密度，不得用於 ml 請求。
- `tests/test_tw_reference_round2.py:44-58` 明確要求 500 ml 與未知/品牌品名不得匹配該 g reference。
- `server.py:17562-17580`：reference 無匹配時直接進入 provider estimate 草稿。故本案例必然落入沒有外部基準的 AI 路徑。

TFDA 值線性算至 500 g 會是 175 kcal / P18 / F9.5 / C3.5，但 **500 g 不是 500 ml**；本文不把這組數字當作本次飲品真值，也不建議偷塞假密度換算。

### 3. 現有測試會讓「預填正確答案」冒充 AI 品質

- `tests/test_nanjing_meal_logging_phase2.py:11-23` 的 fake provider 預填 `165 kcal / P16 / F7 / C9`；測試只證明 schema、audit、basis 與寫入傳遞正確。
- `tests/test_nanjing_meal_logging_phase2.py:131-179` 甚至以同一 fake payload 證明 500 ml 走 AI，但沒有測真 provider 是否能產生合格營養結果。
- `tests/test_phone_meal_revision.py:10-20`、`:189-200` 也直接 monkeypatch 已知數字。

這些是必要的 transport/state tests，但不能命名或解讀為 real-AI-quality acceptance。事故 raw fixture `80/7/4/6` 才能讓目前缺失變成 deterministic red-capable regression；現況它會通過 gate。

## 排名後的根因

1. **最高：沒有相容單位的獨立資料基準，卻 fail-open 接受 AI。** 對 generic 500 ml 無糖豆漿，official reference 只有 g；resolver 正確拒絕，但 fallback 沒有 quality authority。
2. **同源自證。** estimate、min/max、份量說明都來自同一 provider response；schema 和 self-declared range 不能提供真實性。
3. **校驗維度不足。** `assess_nutrition` 是跨欄位 consistency checker，不是 food/portion plausibility checker；80 與 P7/F4/C6 恰好自洽。
4. **測試語意錯置。** fake provider + 預填看似合理數字驗證的是 plumbing，不是真 AI 品質，無法捕捉此次 production failure。
5. **獨立但可見的 UI 缺陷。** `server.py:15186-15189` 四個 nutrient text 未設 `wrap: True`，且每行同塞「實際入帳｜估算範圍」，窄螢幕會截字。它不會造成 80 kcal，但會妨礙使用者檢查。

## 最小、可證實的修正架構

### A. 把 reference lookup 從 `match/None` 改成能力結果

回傳至少三種明確狀態：

- `exact_reference`：相同 food scope、相同 unit basis，可 deterministic scale；
- `conversion_reference`：只有在資料列另附**核准且可追溯的密度/體積換算來源與適用範圍**時才可 g↔ml；
- `insufficient_reference`：沒有相容基準，附 reason（例如 `volume_basis_missing`）。

`insufficient_reference` 不應再靜默代表「AI 可自由猜且可確認」。這是最小關鍵行為改變。

### B. 數值生成與計算由 server 擁有，AI 不再同時當裁判

1. AI 可做名稱正規化、狀態/品牌資訊抽取、候選 reference 選擇；不得自行成為營養真值來源。
2. 命中核准 source 後，server 依 source basis 做 deterministic `amount / basis_amount` 比例計算；四項 point/range 都由程式算，不接受模型自行算總量。
3. 若是 generic「無糖豆漿 500 ml」且沒有 volume source：回傳「缺少可驗證的 ml 基準」，要求使用者提供品牌/營養標示、改用實際克重，或以明示的「未驗證估算」顯示但**不可進入一般一鍵確認**。不要用 TFDA g 值猜密度。
4. 若使用者提供品牌/標示，只把該標示套用到該次/該私人食品；不可升格為所有台灣無糖豆漿真值。

### C. 新增獨立 quality gate，而非只看 macro 自洽

對每個候選估算先正規化至 source 的 basis，再檢查：

- food identity/scope 是否相容（generic、brand、濃度、加糖狀態不可混用）；
- unit compatibility 是否可證明；
- 每個營養素的 candidate interval 是否與核准 reference interval 有足夠 overlap；
- point estimate 是否落於核准 hard bounds；
- scaling invariance（同一 source 的 100/250/500 ml 必須完全線性，由 server 計算）；
- calories 與 macros 仍保留現行 consistency check。

任何 reference 缺失、identity 不確定或 hard-bound violation 都應 fail closed 到 `needs_source_or_user_review`，不可把 AI 自己給的寬 range 當成通過依據。不要藉此次修改未知 quota/退款政策；品質拒絕發生在 provider 前或後的計費語意需另案由產品/營運核准。

### D. 不確定性必須有來源

range 不應由模型憑空宣告。可接受的 range source：

- 同一核准資料集的樣本分布/統計區間；
- 可追溯的標示四捨五入誤差；
- 核准密度區間所傳播出的換算區間；
- 使用者明確提供的包裝標示（通常當該產品 point value，另標示 rounding）。

provenance 至少保存 `source_id/version/retrieved_at/basis/scope`、轉換公式、uncertainty method、validator rule version。若沒有上述來源，狀態應是 unknown，而不是製造 `min/max`。

## 是否需要新資料來源與核准

**若產品要在只知道「generic 無糖豆漿 500 ml」時自動給可確認數值：需要。** 現有 TFDA row 只有 g，無法合法填補 volume capability。可選方案按可信度排序：

1. 台灣官方或學術來源中直接提供每 100 ml 的 generic 無糖豆漿資料；
2. 台灣來源、可追溯且限定食品範圍的密度資料，與 TFDA g composition 組合；
3. 經核准的方法學：蒐集多個台灣市售無糖豆漿官方標示，形成「generic market envelope」。這只能作 generic plausibility envelope，不可把任一品牌（尤其 Silk 外國品牌）當通用真值；需定義品牌納入/排除、樣本數、版本、更新週期、離群處理及授權。

以上資料導入前需由產品 owner + 營養/資料治理負責人核准 source scope 與 acceptance bounds。若未核准，最誠實且可驗收的產品行為是要求標示/克重或阻擋一般確認，而不是讓 prompt 補洞。

## 可驗收測試（先 RED，且區分 plumbing 與真品質）

### 1. 事故 raw replay：必須是第一個 deterministic RED

新增 captured-response fixture，內容使用已稽核的 provider raw：500 ml、80 kcal（70–90）、P7（6–8）、F4（3–5）、C6（5–7）。它不是預填「正確答案」，而是實際事故輸出。

驗收：

- basis/schema/macro consistency 仍通過；
- independent quality gate 必須回 `needs_source_or_user_review` 或 `reference_out_of_bounds`，不得產生一般可確認 pending；
- 原始 audit 與拒絕 reason/version 都保留；
- 在沒有核准 ml source 的版本，拒絕理由應是 `volume_basis_missing`，不能假稱已知真正熱量。

這個測試在現行 HEAD 必須紅，修正後才綠。

### 2. Reference capability tests

- TFDA 100/375/500 g 維持 deterministic scale；
- 500 ml 在沒有 conversion source 時回 explicit `insufficient_reference(volume_basis_missing)`；
- 加入核准 volume source fixture 後才允許 100/250/500 ml；所有營養由 server 線性計算；
- g row 缺 density、density scope 不符、source 過期/缺 hash 時皆 fail closed；
- Silk、未知品牌及特定品牌不得 alias 成 generic 台灣 reference。

### 3. Validator boundary / property tests

- 用核准 source fixture 產生 in-bound、剛好邊界、低於/高於 hard bound、區間不相交、food scope 不符、unit 不符案例；
- 事故 80/P7/F4/C6 不得因 Atwater 自洽而自動放行；
- 100→500 的 deterministic 結果必須精確為 5 倍（依既定 rounding policy）；
- 隨機產生 self-consistent 但極低/極高 macro 組合，證明「內部自洽」不等於「reference 合格」。

### 4. 真 provider 品質評測（獨立、顯式啟用，不得 fake）

建立 opt-in evaluation job，不 monkeypatch provider、不在 prompt/fixture 預填答案，輸入固定為使用者原句及少量預先核准 paraphrase。每次保存 request、原始 response、observed model/version、validator result、cost metadata。

最低 release gate：

- 所有 run 的 basis 必須正確；
- **0 個** reference-invalid 結果可進入一般可確認草稿；
- 若架構仍允許 AI 產生 candidate，應預先核准樣本數、合格率與統計準則；不得看到結果後移動門檻；
- live eval 失敗不得用「換成預填正確 fake」取代。

這項測試會連 provider 並可能產生成本，因此本次未執行；必須由 owner 核准 budget、sandbox 與是否計入既有 quota。不要擅自更動 refund policy。

### 5. End-to-end sandbox（不碰真 DB/LINE）

以 isolated temp DB + fake LINE transport 驗證：原句解析為午餐/500/ml；reference capability 決策；品質拒絕不寫 food log；使用者提供核准標示後才建立 source-bound draft；確認後 snapshot 保存來源、公式與 rule version。此層 fake 的是 transport，不是把正確營養數字冒充真 AI 品質。

### 6. UI 可見性（獨立修正、不可當營養修正）

- 四條 nutrient text 皆要求 `wrap: true`（可再設合理 `maxLines`）；
- 建議拆成「入帳值」與「估算範圍」兩行，避免單行過長；
- SDK JSON round-trip 後屬性仍存在；
- 以目標手機寬度做 snapshot/實機驗收，四項 range 完整可見、無省略。

## 建議實施順序與停止條件

1. 先加入事故 raw replay，證明現況會誤放行。
2. 將 resolver 提升為 capability result；對 `volume_basis_missing` 先 fail closed。
3. 取得並核准新資料來源後，才加 deterministic volume calculation 與 source-derived uncertainty。
4. 再加 independent validator/property tests。
5. 最後跑 opt-in 真 provider eval 與 sandbox E2E；UI wrap 單獨驗收。

若第 2 步完成而資料仍未核准，產品可以正確地說「目前無可驗證的 500 ml 基準」，但不能聲稱已能準確估算 500 ml 豆漿。若真 provider eval 改善、但 independent gate 仍可放行 out-of-bound 結果，release 仍應停止。
