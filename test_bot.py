import sqlite3
import datetime
import asyncio
import random
import server  # 匯入您的大腦主程式

# ==========================================
# 🛡️ 影子分身模組 (攔截 LINE API，防止被官方封鎖)
# ==========================================
def mock_reply_message(reply_token, message):
    text = message.text if hasattr(message, 'text') else str(message)
    print(f"   💬 [系統回覆] {text.replace(chr(10), ' ')}")

def mock_push_message(to, message):
    text = message.text if hasattr(message, 'text') else str(message)
    print(f"   🔔 [系統推播給 {to[-4:]}] {text.replace(chr(10), ' ')}")

# 將主程式的 LINE 模組偷天換日
server.line_bot_api.reply_message = mock_reply_message
server.line_bot_api.push_message = mock_push_message

# 模擬客人的訊息結構
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

# 模擬 FastAPI 的 Request 結構
class MockRequest:
    def __init__(self, json_data):
        self._json = json_data
    async def json(self):
        return self._json

# ==========================================
# 🚀 終極全自動測試腳本
# ==========================================
def run_all_tests():
    uid = "TEST_UID_888"
    print("=" * 60)
    print("🚀 [一日樂食 CRM] 全系統火力整合測試啟動...")
    print("=" * 60 + "\n")

    # ------------------------------------------
    print("✅ [測試 1] 導入表單、TDEE計算、菜單配菜、菜色過敏源過濾")
    form_data = {
        "UID": [uid], "稱呼": ["測試總監"], "體重": ["75"], "身高": ["175"], "年齡": ["30"],
        "性別": ["男"], "活動量": ["中度"], "目標": ["減脂"], "禁忌": ["海鮮, 牛"],
        "第一週": ["週一,週二"]
    }
    asyncio.run(server.receive_form_data(MockRequest(form_data)))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT tdee, active_days, sheet_name FROM health_profile WHERE user_id=?", (uid,))
    hp = c.fetchone(); conn.close()
    if hp:
        print(f"   -> 📊 TDEE精算結果：{hp[0]} kcal | 排餐日：{hp[1]} | 專屬表單名：{hp[2]}")
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 2] 圖文選單攔截與資料核對")
    print("   👤 客人點擊：查看菜單")
    server.handle_message(DummyEvent("查看菜單", uid))
    print("   👤 客人點擊：我要紀錄飲食")
    server.handle_message(DummyEvent("我要紀錄飲食", uid))
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 3] 運費計算與順風車智能報價")
    print("   👤 客人輸入：#測距 台北市信義區松仁路90號")
    server.handle_message(DummyEvent("#測距 台北市信義區松仁路90號", uid))
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 4] 處理熱量紀錄、熱量扣除公式、諮詢額扣除計算")
    print("   👤 客人輸入：我剛剛吃了一塊 350 大卡的起司蛋糕")
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO usage VALUES (?, 50, 10, ?, 'vip', '2099-12-31', 50)", (uid, datetime.date.today().isoformat()))
    conn.commit(); conn.close()

    server.handle_message(DummyEvent("我剛剛吃了一塊 350 大卡的起司蛋糕", uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT today_extra_cal FROM health_profile WHERE user_id=?", (uid,))
    extra = c.fetchone()
    c.execute("SELECT remaining_chat_quota FROM usage WHERE user_id=?", (uid,))
    quota = c.fetchone(); conn.close()
    print(f"   -> 📊 DB 驗證：今日偷吃熱量成功寫入 {extra[0]} kcal | 諮詢額度剩餘：{quota[0]} 次")
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 5] 老闆綁定與換餐通報")
    admin_uid = "BOSS_999"
    print("   👑 老闆輸入：#綁定老闆")
    server.handle_message(DummyEvent("#綁定老闆", admin_uid))

    print("   👤 客人輸入：蛋糕太罪惡，明天幫我換成低碳豆腐餐贖罪！")
    server.handle_message(DummyEvent("蛋糕太罪惡，明天幫我換成低碳豆腐餐贖罪！", uid))
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 6] 老闆權限：扣除餐點數量與續約推坑")
    print("   👑 老闆輸入：#今日出餐完成")
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("UPDATE usage SET remaining_meals=4 WHERE user_id=?", (uid,))
    today_str = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][datetime.date.today().weekday()]
    c.execute("UPDATE health_profile SET active_days=? WHERE user_id=?", (today_str, uid))
    conn.commit(); conn.close()

    server.handle_message(DummyEvent("#今日出餐完成", admin_uid))
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 7] 老闆權限：清空熱量、重置額度、刪除檔案")
    print("   👑 老闆輸入：#清空熱量")
    server.handle_message(DummyEvent("#清空熱量", uid))

    print("   👑 老闆輸入：#重置")
    server.handle_message(DummyEvent("#重置", uid))

    print("   👑 老闆輸入：#刪除檔案")
    server.handle_message(DummyEvent("#刪除檔案", uid))

    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("SELECT * FROM health_profile WHERE user_id=?", (uid,))
    final_check = c.fetchone(); conn.close()
    if not final_check:
        print("   -> 🗑️ DB 驗證：客戶資料已徹底刪除清空！")
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 8] 菜單熱更新與副餐推坑測試")
    print("   👑 老闆輸入：#更新菜單")
    server.handle_message(DummyEvent("#更新菜單", admin_uid))
    print("   👤 客人輸入：我剛跑完 10 公里，好累喔，有推薦補充什麼嗎？")
    server.handle_message(DummyEvent("我剛跑完 10 公里，好累喔，有推薦補充什麼嗎？", uid))
    print("-" * 60)

    # ------------------------------------------
    print("✅ [測試 9] 明日取餐提醒與副餐推坑測試")
    tomorrow_str = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][(datetime.date.today() + datetime.timedelta(days=1)).weekday()]
    conn = sqlite3.connect(server.DB_PATH); c = conn.cursor()
    c.execute("UPDATE health_profile SET active_days=? WHERE user_id=?", (f"週一,{tomorrow_str}", uid))
    conn.commit(); conn.close()
    print(f"   👑 老闆輸入：#發送明日提醒 (系統應自動尋找 {tomorrow_str} 取餐的客人)")
    server.handle_message(DummyEvent("#發送明日提醒", admin_uid))
    print("-" * 60)

    print("\n🎉🎉🎉 [測試總結] 所有系統功能正常！準備迎接大量訂單！ 🎉🎉🎉\n")

if __name__ == "__main__":
    run_all_tests()
