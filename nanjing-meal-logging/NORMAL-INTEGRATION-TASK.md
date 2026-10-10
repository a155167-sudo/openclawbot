繁中。你是Jev已實際選定的Codex GPT-6-Sol high normal candidate writer；接續現有單一工作目錄 /mnt/c/Users/WIN-XI/AppData/Local/Temp/computer-nutrition-normal-cutover/candidate，禁止其他writer/source/deploy。先讀CHECKPOINT和NEXT-BLOCKERS，不重做已完成切片。不要派代理、讀credentials、網路、操作真Sheet/DB/LINE。
使用者授權：所有電腦營養舊表都是測試，先備份後依現行schema重整，以正常入口可用為目標。主控已真實完成原staging個人表17欄+Master21欄重建，兩表讀回confirmed且訂單/usage/receipt/authority DB未變；不需再追舊表頭/測試日期。南京正式OA絕對不動。此phase目標完成正常改期整合候選，不是隔離QA，不允許搬QA synthetic writer/receipt到main。
剛取得的真main狀態已在NORMAL-POLICY-STATE.json（去識別）：order1 activated且formalized、唯一owner是已綁admin；usage.status='vip',remaining_meals=88,last_date=2026-09-25,expiry_date=2026-10-26；subscription_menu_entitlements該order同owner statusactive expires_on=2026-10-26；完全無subscription_service_calendar表；8筆舊pending_admin。前一輪audit很多結論錯，不可照搬。保留usage VIP，不可改成active以遷就測試核心。
主控已確認正常路徑真缺口：
A. pair_reschedule_coordinator._validate_policy只接受usage.status active且source,target<=usage.expiry_date；正常VIP會被拒，與UI expiry+30不一致。
B. _persisted_target_master_profile要求target已存在於form_payload_json.master_api_rows；一般新無餐日期根本沒有target row，不能換到新空日。
C. 普通calendar表不存在，且舊router把service_calendar所有is_service_day=1都當occupied，与新空日供餐設定混淆。
D. normal路由context/preview/submit依mutable usage.expiry_date；必須固定同order原到期日而非generic VIP變動的日期。
E. normal只有admin GET pending，QA才有HTTP approve/reconcile與醒目結果UI；server原本的LINE #核准雙餐改期則走普通approve，仍受上述錯誤policy。需正常入口保留可用管理核准（可新增正常HTTP管理API並復用已驗證UI互動，但不可依賴isolated QA fixture/DB）。
請做一個連貫、最小且真正可跑的正常路徑整合：
1) 建立normal order-owned共用policy/讀取當前authority排餐；正常VIP eligibility必須同owner activated/formalized order + active order entitlement及合法usage，不因generic VIP解鎖別人/過期單。保留舊active合成單測相容，不鬆既有權限。
2) 固定原到期日取可信同order entitlement原日期並持久化不可滾動錨點（若選既有不可變order欄位，證明不可因generic續VIP改變）。context/preview/submit/approval用同一截止=原到期日+30。需真雙次改期測試，使用正常vip而非QA fixture。不要改總餐數、remaining、原到期日。
3) source必須是當前已確認午晚餐，target無餐，非過去且同日8點cutoff保持；source/target label使用真日期可計算，不造餐。不要為空日硬插假排餐進calendar。先理解既有coordinator的policy_validator等可注入介面，避免全域猴補/重複改一堆服務。
4) 新空日Master profile只能基於已驗證的當前source/正式同order snapshot。普通配餐（明確非coaching/非carb cycling）可保留來源固定profile、替換date/meal；動態訓練/碳循環資料不足須拒絕，不能猜訓練。二次改期source也可能不是原snapshot date，要用已確認版本/受驗證snapshot。不要篡改immutable publication receipts。
5) 正常HTTP admin approve/reconcile需persisted admin ID +真的LINE token verifier，兩表adapter由server提供正常SPREADSHEET_ID且持久化worksheet identity；不得偷讀QA manifest/allowlist，未授權零Sheet寫。unknown不重送，只readback，pending不當completed。兩次核准/semantic重送無重複mutate。承接現有漂亮QA completion提示於正常UI可復用字型/結果區，但不要copy QA業務設定。保持正常LINE核准入口用同policy，不出現HTTP能核准而LINE不能的差異。
6) TDD先留RED，然後真SQLite+FakeSheets+registered FastAPI路由整合，只有外部LINE verify/Google傳輸用fakes，不能mock掉policy/service/coordinator來假通過。主控最後另做live/瀏覽器與AG獨立審查。source/target有餐、+30邊界/+31拒、generic VIP延期截止不變、非owner/nonadmin、結果未知reconcile、duplicate approve、無coaching profile不能猜都要覆蓋關鍵case。
測試環境主控已準備完整依賴：/home/win-xi/computer-nutrition-normal-cutover/.venv/bin/python；不要再用缺gspread舊venv。server import前DATA_DIR設本工作目錄臨時資料夾，外部設定只fake。若TestClient在Codex sandbox卡住，改用httpx.ASGITransport +asyncio同process明確有界timeout；記錄原因，不假PASS，不花整輪排sandbox問題。
上限840秒；每180秒把已改檔/已跑測試/下一步寫CHECKPOINT.md。最後90秒停止大型改動與fullsuite，優先交可恢復證據/patch/DELIVERY.json。測試失敗交BLOCK而非無交件。專注上述上線必需整合，不碰其他餐點照片/教練/正式環境。
親自交DELIVERY.json：run_id="normal-flow-integration-d3af2814b0",request_nonce="178087658cc1404a891d1c383a378827",executor="codex",role="normal_integration",status="completed"或"partial",result="PASS"或"BLOCK",changed_files相對路徑,tests命令/真結果,findings,limitations。各測試log保留。不得修改前段的run/nonce取巧。