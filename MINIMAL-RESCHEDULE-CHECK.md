# 未來日期雙餐搬餐：最小修補核實

## 已收斂範圍

- 僅接受 `#延餐 YYYY-MM-DD 午餐+晚餐 -> YYYY-MM-DD 午餐+晚餐`。
- 來源日與目標日必須都 **strictly later than 台北今天**；當日、過期、23:45 後跨日 close window 一律寫前拒絕。
- 原本 `M/D + 單餐` 解析與執行路徑保留不變。
- 接在既有顧客申請 `#延餐`、既有 `deferred_meals`、既有 `#核准延餐 ID` 與 DB `admin_settings.admin_id` 授權入口。
- 不修改 Android 出單程式、不清 cache、不碰設備、Garmin 或其他周邊系統。

## 依實際 10:30 出單程式核對

使用者提供的 `/tmp/nanjing-lf5167-review.py`（僅 AST 離線抽取函式，未 import、未連網）顯示：

- `run_once()` 每次執行才呼叫 `get_pending_orders()`；
- 只讀 `實際日期 == 台北今天` 且 N 欄為 `待列印`；
- 個人表欄位為日期 0、午餐 2、晚餐 5、狀態 13；
- 以當次讀取的 `rowNumber` 回寫，不存在跨日 future meal cache。

因此修補不再要求改出單機。搬餐同批會把來源列兩餐清為「無」且清掉來源 `待列印`，避免來源日產生空白票；目標列設為 `待列印`。離線 AST 測試證明：來源日不取 future row，到了目標日會取新列。

## 寫入契約

`minimal_pair_reschedule.py` 現在包含真正可執行 handler：

1. 從 SQLite 重驗 request ID、owner、pending、完整日期及雙餐 payload。
2. stable replay lookup 在 Sheet read 前；completed replay 不再搬，unknown replay 為 0 Sheet read / 0 batch。
3. personal 17 欄與 `Master_API_View` 21 欄 exact snapshot。
4. source 兩餐、兩 view 必須一致；`已送印` / `已列印` 拒絕。
5. target occupied 拒絕。缺一側 target row 時從另一側已存在的 target-date metadata 衍生；兩側皆缺時，正式 adapter 以 SQLite 中唯一 active entitlement + activated/formalized order snapshot 的服務區間、方案起日，以及來源 personal/Master 穩定 profile 建立兩列。日期超出 entitlement 或四週方案、order/menu/owner/TDEE 不一致、calendar 缺失或歧義皆在 reserve/write 前具體拒絕。
6. 不 insert/delete/sort；既存列只 `updateCells`，缺列只 `appendCells`。
7. personal source、personal target、Master source、Master target 放在單一 `spreadsheets.batchUpdate`。
8. 寫入前再次檢查 future/date close window 與完整 before-image；寫後 personal + Master exact readback 才可成功。
9. 沿用 `meal_mutation_ledger` 的 durable reserve/lock/complete/unknown journal；request ledger 一定早於外部 write。
10. timeout 或 readback 不一致記為 unknown，不自動 retry；confirmed replay 不再做外部 I/O。

`published` 不再單獨阻擋 future pair move：publication receipt 仍保留做 audit，實體列印判斷以個人表 N 欄 `已送印` / `已列印` 為準。

## 已知限制（不誇稱 v2 / CAS）

- Google Sheets 的「最後 read → batchUpdate」不是 CAS；本修補是既有 SQLite workflow 的 durable request fence + exact before/readback，不是全世界共享鎖。
- target-date 兩側都不存在時，只在唯一 active entitlement 與 activated/formalized order snapshot 能共同證明 target 位於服務期及原四週方案內時建立；未知/歧義欄位具體拒絕，不猜負週次、課表或碳循環。
- 新列沿用並搬移來源既有 dispatch/order/menu identity，不產生新 dispatch receipt；來源 identity 與待列印狀態在同 batch 清除。既有 publication receipt 保持 immutable audit，未宣稱改寫 backend projection。

## 離線驗證

```text
python3 -m pytest -q -p no:cacheprovider \
  tests/test_future_pair_reschedule_handler.py \
  tests/test_minimal_pair_reschedule.py \
  tests/test_deferred_meal_replay_fence.py \
  tests/test_sheet_atomic_meal_updates.py

47 passed, 7 pre-existing warnings in 3.75s
```

涵蓋：完整年份雙餐 parser、保留舊 parser、existing admin approval 真入口、fake Sheet + real SQLite、target append、單 batch、餐數 2→2、舊 rowNumber 不變、來源不出空白票、實際 10:30 reader future/target-day 行為、printed 拒絕、當日/過期拒絕、timeout unknown、unknown/confirmed replay 0 I/O。
