# Legacy assertion alignment（13 個新增失敗）

## 結論

- 基準：`baseline=87 failed`、目前完整回歸證據 `candidate=100 failed`，新增 13 個 node ID。
- 分類：**10 個為已批准規格更新並已對齊且實跑通過；3 個為真正 regression，保留失敗、不改產品、不 skip。**
- node ID 全數保留；沒有刪除、重新命名或 skip 測試。
- 只修改允許的既有測試檔：`tests/test_server_nutrition_integration.py`、`tests/test_text_meal_confirmation_ux.py`。`tests/test_dashboard_nanjing_buttons.py` 因揭露真正缺陷而原樣保留。

## 逐項分類

| # | node ID | 分類 | 舊斷言 / 失敗原因 | 更新後與保留的核心驗證 | 實跑 |
|---|---|---|---|---|---|
| 1 | `tests.test_dashboard_nanjing_buttons::test_three_rendered_home_buttons_reach_native_registered_handlers[0]` | **真正 regression** | 點「記一餐」後，緊接著點「今日明細」應進原生明細 handler；actual 卻是 text。 | 不改預期。仍驗證四鈕、零 AI/零 quota、零帳本副作用及原生明細 Flex。 | FAIL（保留） |
| 2 | 同上 `[20]` | **真正 regression** | 與 #1 相同，quota=20 參數。 | 同上。 | FAIL（保留） |
| 3 | `test_ai_estimate_button_records_midpoint_when_model_returns_only_ranges` | 正當規格更新 | 舊期待 AI range 立即入帳及總覽。 | 確認前 0 log、有 confirm；range midpoint 175/16；confirm 後記錄卡；confirm replay 同卡且 1 log。 | PASS |
| 4 | `test_ai_estimate_log_preserves_meal_and_returns_replayable_dashboard` | 正當規格更新 | 舊期待 hidden `LOG_NUTRITION` 立即入帳、frequent-food 副作用。 | hidden tag 只建一般估算草稿；確認前 0 log；草稿 replay 相同；確認後記錄卡+總覽；同 event replay 不重複，早餐與 180/8 保留。 | PASS |
| 5 | `test_ai_estimate_webhook_replay_precedes_quota_and_openai` | 正當規格更新 | 舊期待 preview LINE reply failure 後已經有 1 log。 | preview reply failure/replay 只呼叫 AI 一次、只扣 quota 一次且 0 log；之後 confirm/replay 產生同記錄卡、正式 1 log、quota 不再扣。 | PASS |
| 6 | `test_explicit_text_dashboard_render_failure_reports_committed_without_retry_prompt` | 正當 UI 更新 | 成功首訊息由短文字改為正式記錄 Flex 卡。 | 仍直接驗證已 commit；記錄卡含餐名/餐別/熱量/蛋白/來源；renderer fail 明確稱「已入帳」、不得叫使用者重記；DB 恰 1 log。 | PASS |
| 7 | `test_explicit_text_nutrition_failure_never_returns_dashboard` | 正當流程更新 | 舊測試在文字輸入階段注入 writer failure；新規格該階段只建草稿，故不應觸發 writer。 | 先驗證 preview 且 0 log；於 confirm 階段注入 writer failure；例外向上、0 log、draft 維持 pending，沒有假 dashboard。 | PASS |
| 8 | `test_explicit_text_nutrition_logs_once_and_returns_canonical_dashboard` | 正當規格更新 | 舊期待固定數值文字直接入帳。 | 確認前同 message replay 同草稿、0 log、0 AI、0 quota；confirm 可執行；confirm replay 同記錄卡+同總覽；正式 1 log、同 UID、早餐、350/17、使用者提供來源。 | PASS |
| 9 | `test_natural_food_log_not_found_or_incompatible_unit_requires_ai_confirmation` | 正當 provider contract / confirmation 更新 | 舊 mock 僅 cal/pro，不符合四營養素+basis contract，落入 provider_unknown。 | mock 補成與實際 provider 一致的四素+basis；兩草稿確認前 0 log、quota 各扣一次；第一筆 confirm/replay 同卡、正式 1 log，confirm 不再扣 quota。 | PASS |
| 10 | `test_natural_food_log_uses_private_exact_match_scales_ml_and_replays_once` | **真正 regression（測試已升級至新流程）** | 舊期待私人精確匹配直接入帳；依新規格改成 zero-AI confirmation 後，發現 confirm 把使用者 `400 ml` 攫寫為 `1 serving`。 | 已保留/加強：確認前 0 log、草稿 replay、確認後記錄卡+總覽、confirm replay、1 log、縮放營養 202.88/9.7067；**仍堅持 consumed_amount=400、unit=ml，因此保留 FAIL。** | FAIL（保留） |
| 11 | `test_natural_food_unit_mismatch_offers_executable_ai_estimate` | 正當 provider contract / confirmation 更新 | 舊 mock 缺四素+basis。 | 真實 contract mock；確認前 0 log、pending、quota=0；confirm 與 replay 回同 log id；確認後 1 log、quota 不再扣。 | PASS |
| 12 | `test_draft_is_durable_and_duplicate_message_debits_real_quota_only_once` | 正當 provider contract 更新 | `_mock_estimate` 僅 cal/pro，被新 contract 拒絕。 | mock 補 schema、basis、fat、carbohydrate、assessment；保留 durability、同 token、provider 一次、quota 只扣一次、pending 版本。 | PASS |
| 13 | `test_explicit_user_values_reply_with_short_success_then_dashboard_and_replay_once` | 正當規格更新 | 舊期待固定數值立即短成功+dashboard。 | 確認前草稿 replay、0 log、0 AI、0 quota；confirm/replay 同記錄卡+dashboard；確認後正式 1 log。 | PASS |

## 真正缺陷 trace（交父控修產品）

### A. Dashboard 導航被 awaiting-food 狀態誤攔截（#1/#2）

最小重現順序：同 UID 依序送 `我要紀錄飲食`、`我要修改飲食紀錄`。

實際輸出：

1. 第一則：`請在下一則訊息回覆餐別、餐點和份量...`
2. 第二則：`⚠️ 尚未記錄：估算請求格式無效`
3. DB：`pending_text_meal_inputs = [('awaiting_food', source_message_id='1')]`、無 estimate draft。

因此第二個 dashboard 導航 command 被當作 awaiting-food 的餐點文字，而沒有到「今日明細」handler。這不是本輪批准的 UI 行為變更；四個 dashboard command 必須能覆蓋/離開 awaiting-food 狀態且保持零 AI/零 quota/零帳本副作用。

### B. 私人食品 400 ml 確認後遺失原始 quantity（#10）

草稿與顯示營養已依私人食品 375 ml 基準正確縮放為 400 ml（202.88 kcal / 9.7067 g），但 confirm 後 `food_logs` 實際為：

- `consumed_servings = 1.0`
- `consumed_amount = 1.0`
- `consumed_unit = 'serving'`

產品 trace：`apply_text_meal_estimate_action` 呼叫 `create_daily_food_log` 時只傳 `servings=portion_multiplier`，未把 draft request 的 `amount=400` / `unit='ml'` 傳入。因此 nutrition 與 quantity provenance 不一致。測試保留紅燈，禁止把期待改成 1 serving 洗綠。

## 實跑證據

### 13 affected

```text
3 failed, 10 passed in 3.37s
```

保留的 3 failures 正是上列兩類真正缺陷（dashboard 兩個參數 case + private 400 ml quantity）。其餘 10 個 legacy alignment 全數通過。

### 必要 neighbor

執行 `tests/test_nanjing_meal_logging_flow.py` 全檔，及 phase2 的四素 ledger、私人食品 zero-AI draft、reference-vs-AI 三個鄰近案例：

```text
8 passed in 1.49s
```

未跑 full suite（依任務限制）。
