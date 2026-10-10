繁中。唯一工作目錄 /mnt/c/Users/WIN-XI/AppData/Local/Temp/computer-nutrition-normal-cutover/candidate。你是Jev已指派的Codex coding lane有界接續，不重新派工。前一輪390秒逾時，已留下normal_reschedule_context.py、server.py和customer_reschedule_liff_routes.py與tests修改，.normal_cutover_green.log 2 passed，但未DELIVERY。請先讀現有檔與日誌，續作，不重做。
只做以下有限收尾：
1) 先交CHECKPOINT.md列現有改動/測試；不能沒檔就跑長測試。
2) 主控發現helper只要有任何operation history且current.parent_version_id=None就回空，檢查這是否誤擋已失敗/已取消且exportable initial仍有效的狀態；用真模組/SQLite陽性反例驗證，必要最小修。不能為修UI鬆綁授權或偽造receipt。
3) 正常路徑截止必須原到期日+30天固定而非today+MAX_FORWARD_DAYS。檢查現行路由/服務是否已遵守；若未，先交具體證據NEXT-BLOCKERS.md及建議，不在本輪匆忙大改多層policy。
4) 用 /home/win-xi/jev-original-recovery/.venv/bin/python 跑已修改的focused+原customer_reschedule相關測試；server import只用本目錄temp DATA_DIR，no external network/no main DB。不安裝套件。
5) 舊四欄測試表不必保留，主控已備份並做21欄Master/原snapshot吻合dryrun；不要歷史考古。禁止設定external_writer_state=disabled冒充外部證據；不啟用STAGING_RESCHEDULE_BOOTSTRAP舊自證hook。
本輪最長270秒，最後60秒只做交件，不啟動fullsuite；即使未修完也親自交DELIVERY.json。若測試紅則BLOCK，不捏造PASS。本輪不部署不碰真Sheet/DB/LINE，不改QA產品。原server已由主控備份，保持單writer。
交付CHECKPOINT.md, NEXT-BLOCKERS.md及DELIVERY.json：run_id="normal-cutover-recovery-7777920807",request_nonce="c949928461e8487c82433a3bdbd78716",executor="codex",role="candidate",status="completed"或"partial",result="PASS"或"BLOCK",changed_files相對路徑,tests命令和真結果,findings,limitations。PASS只是此切片，正常核准API/可用上線由主控後續整合。