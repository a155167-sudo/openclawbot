import sqlite3
import datetime
import asyncio
import random
import server  # 匯入主程式

# ==========================================
# 🛡️ 影子分身模組 (攔截 LINE API，防止被官方封鎖)
# ==========================================
def mock_reply_message(reply_token, message):
    text = message.text if hasattr(message, 'text') else str(message)
    print(f"   💬 [系統回覆] {text.replace(chr(10), ' ')}")

def mock_push_message(to, message):
    text = message.text if hasattr(message, 'text') else str(message)
    print(f"   🔔 [系統推播給 {to[-4:]}] {text.replace(chr(10), ' ')}")

server.line_bot_api.reply_message = mock_reply_message
server.line_bot_api.push_message = mock_push_message

# 模擬結構
class DummyMessage:
    def __init__(self, text):
        self.text = text
        self.id = str(random.randint(10000, 99999))

class DummySource:
    def __init__(self, user_id):
        self.user_id = user_id

class DummyEvent:
    def __init__(self, text, user_id="TEST_UID_888"):
        self.message = DummyMessage(text)
        self.source = DummySource(user_id)
        self.reply_token = "dummy_token"

class MockRequest:
    def __init__(self, json_data):
        self._json = json_data
    async def json(self):
        return self._json

# ==========================================
# 🚀 終極全自動測試腳本 (完整版)
# ==========================================
def run_all_tests():
    uid = "TEST_UID_888"
    admin_uid = "BOSS_999"
    today_iso = datetime.date.today().isoformat()
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    today_str = weekdays[datetime.date.today().weekday()]
    tomorrow_str = weekdays[(datetime.date.today() + datetime.timedelta(days=1)).weekday()]

    print("=" * 60)
    print("🚀 [一日樂食 CRM] 全系統火力整合測試啟動...")
    print(f"📅 今天: {today_iso} ({today_str})")
    print("=" * 60 + "\n")

    # ------------------------------------------
    # 測試 1: 菜單載入
    # ------------------------------------------
    print("✅ [測試 1] 菜單載入 (load_menu)")
    result = server.load_menu()
    print(f"   -> {result}")
    print(f"   -> 主餐數量: {len([d for d in server.MAIN_DISHES if d['category']=='main'])}")
    print(f"   -> 單品數量: {len([d for d in server.MAIN_DISHES if d['category']=='side'])}")
    print("-" * 60)

    # ------------------------------------------
    # 測試 2: 表單接收 + TDEE計算 + 過敏原過濾 + 配餐
    # ------------------------------------------
    print("✅ [測試 2] 表單接收、TDEE計算、過敏原過濾、自動配餐")
    form_data = {
        "UID": [uid], "稱呼": ["測試總監"], "體重": ["75"], "身高": ["175"], "年齡": ["30"],
        "性別": ["男"], "活動量": ["中度"], "目標": ["減脂"], "禁忌": ["海鮮, 牛"],
        "取餐": [f"{today_str},{tomorrow_str}"],
        "主食偏好": ["飯食派"], "蛋白質": ["雞"]
    }
    asyncio.run(server.receive_form_data(MockRequest(form_data)))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT tdee, protein, active_days, sheet_name, summary_text FROM health_profile WHERE user_id=?", (uid,))
    hp = c.fetchone(); conn.close()
    if hp:
        print(f"   -> 📊 TDEE: {hp[0]} kcal | 蛋白質: {hp[1]}g | 排餐日: {hp[2]}")
        print(f"   -> 📋 分頁名: {hp[3]}")
        # 驗證過敏原：菜單中不應出現牛或海鮮
        if hp[4] and ("牛" not in hp[4]) and ("海鮮" not in hp[4]) and ("魚" not in hp[4]):
            print("   -> ✅ 過敏原過濾正常：菜單中無牛/海鮮！")
        else:
            print("   -> ⚠️ 過敏原可能未完全過濾，請人工檢查菜單內容")
    else:
        print("   -> ❌ 表單寫入失敗！health_profile 無資料")
    print("-" * 60)

    # ------------------------------------------
    # 測試 3: 圖文選單 - 所有按鈕
    # ------------------------------------------
    print("✅ [測試 3] 圖文選單攔截 (全部 6 個按鈕)")

    print("   👤 客人點擊：填寫體質表單")
    server.handle_message(DummyEvent("填寫體質表單", uid))

    print("   👤 客人點擊：填寫滿意度問卷")
    server.handle_message(DummyEvent("填寫滿意度問卷", uid))

    print("   👤 客人點擊：查看菜單")
    server.handle_message(DummyEvent("查看菜單", uid))

    print("   👤 客人點擊：我要紀錄飲食")
    server.handle_message(DummyEvent("我要紀錄飲食", uid))

    print("   👤 客人點擊：運費怎麼算")
    server.handle_message(DummyEvent("運費怎麼算", uid))

    print("   👤 客人點擊：我的會員狀態 (尚未開通)")
    server.handle_message(DummyEvent("我的會員狀態", uid))
    print("-" * 60)

    # ------------------------------------------
    # 測試 4: VIP 邀請碼 (生成 + 兌換)
    # ------------------------------------------
    print("✅ [測試 4] VIP 邀請碼生成與兌換")

    print("   👑 老闆輸入：#生24 (生成 24 餐邀請碼)")
    server.handle_message(DummyEvent("#生24", admin_uid))

    print("   👑 老闆輸入：#生48 (生成 48 餐邀請碼)")
    server.handle_message(DummyEvent("#生48", admin_uid))

    # 從 DB 抓一個未使用的邀請碼來測試兌換
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT code FROM vips WHERE is_used=0 LIMIT 1")
    code_row = c.fetchone(); conn.close()
    if code_row:
        test_code = code_row[0]
        print(f"   👤 客人輸入：{test_code} (兌換邀請碼)")
        server.handle_message(DummyEvent(test_code, uid))

        # 驗證兌換結果
        conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
        c.execute("SELECT remaining_meals, status, expiry_date FROM usage WHERE user_id=?", (uid,))
        usage = c.fetchone(); conn.close()
        if usage:
            print(f"   -> 📊 兌換後：剩餘 {usage[0]} 餐 | 狀態: {usage[1]} | 到期: {usage[2]}")
    else:
        print("   -> ⚠️ 沒有可用的邀請碼，跳過兌換測試")

    # 測試會員狀態 (已開通)
    print("   👤 客人點擊：我的會員狀態 (已開通)")
    server.handle_message(DummyEvent("我的會員狀態", uid))
    print("-" * 60)

    # ------------------------------------------
    # 測試 5: 運費計算與順風車
    # ------------------------------------------
    print("✅ [測試 5] 運費計算與順風車智能報價")
    print("   👤 2公里內免運測試：#測距 台北市松山區南京東路四段")
    server.handle_message(DummyEvent("#測距 台北市松山區南京東路四段", uid))
    print("   👤 遠距離測試：#測距 台北市信義區松仁路90號")
    server.handle_message(DummyEvent("#測距 台北市信義區松仁路90號", uid))
    print("-" * 60)

    # ------------------------------------------
    # 測試 6: 老闆綁定
    # ------------------------------------------
    print("✅ [測試 6] 老闆綁定")
    print("   👑 老闆輸入：#綁定老闆")
    server.handle_message(DummyEvent("#綁定老闆", admin_uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
    admin = c.fetchone(); conn.close()
    if admin and admin[0] == admin_uid:
        print("   -> ✅ 老闆綁定成功！")
    else:
        print("   -> ❌ 老闆綁定失敗！")
    print("-" * 60)

    # ------------------------------------------
    # 測試 7: AI 對話 + 熱量蛋白質追蹤
    # ------------------------------------------
    print("✅ [測試 7] AI 對話 + 熱量與蛋白質雙軌追蹤")

    # 確保有額度
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO usage VALUES (?, 50, 10, ?, 'vip', '2099-12-31', 50)", (uid, today_iso))
    conn.commit(); conn.close()

    print("   👤 客人輸入：我剛剛吃了一塊 350 大卡的起司蛋糕")
    server.handle_message(DummyEvent("我剛剛吃了一塊 350 大卡的起司蛋糕", uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT today_extra_cal, today_extra_pro FROM health_profile WHERE user_id=?", (uid,))
    extra = c.fetchone()
    c.execute("SELECT remaining_chat_quota FROM usage WHERE user_id=?", (uid,))
    quota = c.fetchone(); conn.close()
    if extra:
        print(f"   -> 📊 DB 驗證：今日外食熱量 {extra[0]} kcal | 外食蛋白質 {extra[1]} g")
    if quota:
        print(f"   -> 📊 諮詢額度剩餘：{quota[0]} 次")
    print("-" * 60)

    # ------------------------------------------
    # 測試 8: 換餐通報 (AI → 老闆)
    # ------------------------------------------
    print("✅ [測試 8] 換餐通報 (客人要求 → 通知老闆)")
    print("   👤 客人輸入：蛋糕太罪惡，明天幫我換成低碳豆腐餐贖罪！")
    server.handle_message(DummyEvent("蛋糕太罪惡，明天幫我換成低碳豆腐餐贖罪！", uid))
    print("-" * 60)

    # ------------------------------------------
    # 測試 9: 今日出餐完成 + 續約推播
    # ------------------------------------------
    print("✅ [測試 9] 今日出餐完成 + 續約推播")
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("UPDATE usage SET remaining_meals=4 WHERE user_id=?", (uid,))
    c.execute("UPDATE health_profile SET active_days=? WHERE user_id=?", (today_str, uid))
    conn.commit(); conn.close()

    print("   👑 老闆輸入：#今日出餐完成")
    server.handle_message(DummyEvent("#今日出餐完成", admin_uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (uid,))
    meals = c.fetchone(); conn.close()
    if meals:
        print(f"   -> 📊 扣餐後剩餘：{meals[0]} 餐 (應為 3，觸發續約推播)")
    print("-" * 60)

    # ------------------------------------------
    # 測試 10: 點數系統 (上傳 + 庫存 + 問卷領取)
    # ------------------------------------------
    print("✅ [測試 10] 點數系統 (上傳、庫存、問卷領取)")

    print("   👑 老闆輸入：#上傳點數 (3 筆測試網址)")
    server.handle_message(DummyEvent("#上傳點數\nhttps://test-reward-1.com\nhttps://test-reward-2.com\nhttps://test-reward-3.com", admin_uid))

    print("   👑 老闆輸入：#點數庫存")
    server.handle_message(DummyEvent("#點數庫存", admin_uid))

    # 模擬問卷提交 → 自動發點數
    print("   📝 模擬問卷提交 (應自動發放點數網址)")
    survey_data = {"UID": uid}
    asyncio.run(server.receive_survey_data(MockRequest(survey_data)))

    # 再次提交 → 應該提示已領過
    print("   📝 重複提交問卷 (應提示已領過)")
    asyncio.run(server.receive_survey_data(MockRequest(survey_data)))

    print("   👑 老闆輸入：#點數庫存 (應少 1 張)")
    server.handle_message(DummyEvent("#點數庫存", admin_uid))
    print("-" * 60)

    # ------------------------------------------
    # 測試 11: 明日取餐提醒
    # ------------------------------------------
    print("✅ [測試 11] 明日取餐提醒推播")
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("UPDATE health_profile SET active_days=? WHERE user_id=?", (f"{today_str},{tomorrow_str}", uid))
    conn.commit(); conn.close()

    print(f"   👑 老闆輸入：#發送明日提醒 (尋找 {tomorrow_str} 取餐的客人)")
    server.handle_message(DummyEvent("#發送明日提醒", admin_uid))
    print("-" * 60)

    # ------------------------------------------
    # 測試 12: 菜單熱更新
    # ------------------------------------------
    print("✅ [測試 12] 菜單熱更新")
    print("   👑 老闆輸入：#更新菜單")
    server.handle_message(DummyEvent("#更新菜單", admin_uid))
    print("-" * 60)

    # ------------------------------------------
    # 測試 13: 清空熱量
    # ------------------------------------------
    print("✅ [測試 13] 清空熱量紀錄")
    print("   👑 老闆輸入：#清空熱量")
    server.handle_message(DummyEvent("#清空熱量", uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT today_extra_cal, today_extra_pro FROM health_profile WHERE user_id=?", (uid,))
    cleared = c.fetchone(); conn.close()
    if cleared and cleared[0] == 0 and cleared[1] == 0:
        print("   -> ✅ 熱量與蛋白質已歸零！")
    else:
        print(f"   -> ⚠️ 歸零異常: cal={cleared[0] if cleared else '?'}, pro={cleared[1] if cleared else '?'}")
    print("-" * 60)

    # ------------------------------------------
    # 測試 14: 重置 (老闆特權)
    # ------------------------------------------
    print("✅ [測試 14] 老闆特權重置")
    print("   👑 老闆輸入：#重置")
    server.handle_message(DummyEvent("#重置", uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT remaining_chat_quota, remaining_meals, expiry_date FROM usage WHERE user_id=?", (uid,))
    reset = c.fetchone(); conn.close()
    if reset:
        print(f"   -> 📊 重置後：額度 {reset[0]} 次 | 餐點 {reset[1]} 份 | 到期 {reset[2]}")
    print("-" * 60)

    # ------------------------------------------
    # 測試 15: 刪除檔案
    # ------------------------------------------
    print("✅ [測試 15] 刪除客戶檔案")
    print("   👑 老闆輸入：#刪除檔案")
    server.handle_message(DummyEvent("#刪除檔案", uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT * FROM health_profile WHERE user_id=?", (uid,))
    final_check = c.fetchone(); conn.close()
    if not final_check:
        print("   -> 🗑️ DB 驗證：客戶資料已徹底刪除！")
    else:
        print("   -> ❌ 刪除失敗，資料仍存在！")
    print("-" * 60)

    # ------------------------------------------
    # 清理測試資料
    # ------------------------------------------
    print("🧹 [清理] 移除所有測試資料...")
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM health_profile WHERE user_id=?", (uid,))
    c.execute("DELETE FROM usage WHERE user_id=?", (uid,))
    c.execute("DELETE FROM admin_settings WHERE key='admin_id' AND value=?", (admin_uid,))
    c.execute("DELETE FROM vips WHERE code LIKE '#VIP24-TEST%' OR code LIKE '#VIP48-TEST%'")
    c.execute("DELETE FROM reward_links WHERE link LIKE 'https://test-reward%'")
    c.execute("DELETE FROM survey_records WHERE user_id=?", (uid,))
    conn.commit(); conn.close()
    print("   -> ✅ 測試資料已清理完畢！")

    print("\n" + "=" * 60)
    print("🎉🎉🎉 [測試總結] 全部 15 項測試完成！ 🎉🎉🎉")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    run_all_tests()
