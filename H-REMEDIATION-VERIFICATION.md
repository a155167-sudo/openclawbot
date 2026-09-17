# H1–H4 修正候選：主控驗證

基準：11313f90e95dc7fb9f768fb836fd486c6b1f6534。此文件不取代独立審查；尚未部署。

逾時後主控直接重跑完整套件：1064 passed / 4 failed，紀錄 /tmp/text-meal-h-fixes-parent.log。四項失敗皆是照片修改 replay 測試對整個 Flex JSON 禁止子字串 `35`，碰到顏色色碼 `#FF6B35`。

主控修正斷言為逐張 Flex 的可見文字數值 token：要求目前 590 / 38 存在，且舊值 680 / 35 / 510 / 31 不存在。保留 DB 全表未重寫、防重與舊 action 禁止斷言，不改 runtime 來迎合測試。

完整重跑指令：
`/tmp/text-meal-regression-venv/bin/python -m pytest -q --disable-warnings`

結果：1068 passed, 212124 warnings in 33.82s，exit 0。
完整紀錄：/tmp/text-meal-h-final-parent.log。

獨立審查 H1–H4 是否完整關閉仍待確認，不能把測試綠燈視為放行。沒有部署、LINE 通知、Google 或線上訂單修改。
