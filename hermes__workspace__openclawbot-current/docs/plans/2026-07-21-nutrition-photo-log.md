# 客製化營養計畫＋圖片辨識飲食紀錄 Implementation Plan

> **For Hermes:** 使用 TDD 逐一完成可驗證的垂直切片。

**Goal:** 讓 LINE 用戶上傳營養標示照片，確認後建立私人食品與飲食紀錄，計算個人計畫剩餘需求，並從一日樂食菜單提出最接近的餐點推薦。

**Architecture:** 新增獨立 `nutrition_system.py` 保存純資料驗證、SQLite schema、份量縮放、剩餘需求與推薦排序；`server.py` 只負責 LINE 圖片分類、OpenAI Vision、Flex 確認、Google Sheet 同步及既有儀表板相容更新。Google Sheet 增加營養專用分頁，SQLite 保存正式紀錄與營養快照。

**Tech Stack:** Python、FastAPI、SQLite、OpenAI Vision、LINE Messaging API、gspread、pytest。

---

## Task 1：純營養資料層
- 建立 `nutrition_system.py`
- 驗證營養標示 JSON
- 按食用份數縮放營養
- 建立穩定食品指紋
- 計算剩餘目標
- 對菜單候選排序
- 建立 SQLite 營養資料表

## Task 2：Google Sheet schema
- 建立 `營養份量規則`
- 建立 `食品資料庫`
- 建立 `客製化營養計畫`
- 建立 `飲食紀錄`
- 只新增分頁，不更動既有分頁資料

## Task 3：LINE 圖片分類與營養標示辨識
- 把既有 Garmin-only 圖片 handler 改為多類型辨識
- 支援 `garmin_workout` 與 `nutrition_label`
- 營養標示先進 pending 狀態，不直接記錄
- 回傳確認 Flex Message

## Task 4：確認後建立食品與飲食紀錄
- 使用者確認後建立／重用私人食品
- 保存原始 OCR、營養快照、食用量及時間
- 同步到 Google Sheet
- 相容更新既有 `today_extra_cal`、`today_extra_pro`

## Task 5：個人計畫與菜單推薦
- 讀取有效個人計畫
- 扣除當日已確認飲食
- 以份量代號優先、營養素後備計算前三名
- LINE 顯示符合度及缺口

## Task 6：驗證與部署
- 圖3豆漿案例單元測試
- SQLite integration test
- `py_compile`
- 全部 pytest
- 檢查 git diff
- commit/push main
- Railway OpenAPI、webhook與健康路徑驗證
