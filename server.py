import requests
import os
import json
import sqlite3
import datetime
from zoneinfo import ZoneInfo

# 台灣時區工具函數
TW_TZ = ZoneInfo("Asia/Taipei")
def tw_today():
    return datetime.datetime.now(TW_TZ).date()
def tw_now():
    return datetime.datetime.now(TW_TZ)
import secrets
import string
import csv
import random
import re
import gspread
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextSendMessage, TextMessage
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager

# --- 保險箱初始化設定 ---
# 我們把建立資料夾的邏輯移到 init_db 裡面會更安全，
# 這裡可以先註解掉或保持原樣，但 init_db 一定要改用「絕對路徑」版本。
# -----------------------
# ==========================================
# 1. 設定區 (🔥 安全防護版：金鑰改由 Railway 後台讀取)
# ==========================================
STORE_ADDRESS = "台北市松山區南京東路四段133巷4弄5號"
HUBS = [
    {"name": "Anytime Fitness 信義店", "address": "台北市信義區松仁路89號"},
    {"name": "健身工廠 中山廠", "address": "台北市中山區南京東路二段8號"}
]

# ⚠️ 這裡已經全部改為安全寫法，請至 Railway 的 Variables 後台填寫金鑰！
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "#GEN_CODES")
DB_DIR = os.path.join(os.getcwd(), 'data')
DB_PATH = os.path.join(DB_DIR, 'user_quota.db')

if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR, exist_ok=True)
# Google 試算表設定 (網址公開安全，靠 service_account 保護)
SHEET_URL = "https://docs.google.com/spreadsheets/d/1cf0QhWeYynk9nqsoqMIM-Lkxk_bP57zcd-ES7Sufkqg/edit?gid=0#gid=0"

# 🔥 設定 FastAPI 的生命週期與隱形店長排程
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 伺服器啟動時，喚醒隱形店長
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    
    # ⏰ 排定班表：每天 14:00 自動扣餐與催繳續約
    scheduler.add_job(auto_daily_meal_deduction, 'cron', hour=14, minute=0)
    
    # ⏰ 排定班表：每天 20:00 自動發送明日取餐與加購提醒
    scheduler.add_job(auto_send_tomorrow_reminders_to_boss, 'cron', hour=20, minute=0)
    
    scheduler.start()
    print("✅ 全自動定時器已啟動！系統進入無人駕駛模式 ON！")
    
    yield
    
    # 伺服器關閉時，讓店長下班
    scheduler.shutdown()

# 正式建立啟用了定時器的 FastAPI 應用程式
app = FastAPI(lifespan=lifespan)
client = OpenAI(api_key=OPENAI_API_KEY)
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
user_memory = {}
processed_messages = set()

# 喚醒 Google 虛擬助理 (🔥 卸下裝甲，回歸純淨版)
try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    # 1. 直接從保險箱拿出完美的字串
    creds_str = os.environ.get("GOOGLE_CREDENTIALS")
    
    # 2. 原汁原味轉成字典 (什麼 replace 都不用加，因為您貼得太完美了！)
    creds_dict = json.loads(creds_str)
    
    # 3. 直接拿鑰匙開門
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    print("✅ Google 雲端大門正式開啟！寫入權限 100% 取得！")
    
except Exception as e:
    print(f"⚠️ Google 助理連線失敗: {e}")
    gc = None

# ==========================================
# 2. 菜單資料載入 (🔥 新增：主餐/單品精準分類與熱更新)
# ==========================================
MAIN_DISHES = []
def load_menu():
    global MAIN_DISHES
    MAIN_DISHES.clear()
    try:
        with open("menu.csv", mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_clean = {k.strip() if isinstance(k, str) else k: v for k, v in row.items()}
                name = row_clean.get("品項", "").strip()
                if not name: continue
                try:
                    cal = float(row_clean.get("熱量(kcal)", "0").strip() or 0.0)
                    pro = float(row_clean.get("蛋白質(g)", "0").strip() or 0.0)
                    price = int(row_clean.get("價錢", row_clean.get("價格", "150")).strip() or 150)
                    ingredients = row_clean.get("內容物", "新鮮食材製作").strip()
                    main_keywords = ["便當", "麵", "食蔬", "低碳", "沙拉", "原型"]
                    if any(kw in name for kw in main_keywords):
                        category = "main"  
                    else:
                        category = "side"  
                    MAIN_DISHES.append({"name": name, "cal": cal, "pro": pro, "price": price, "category": category, "ingredients": ingredients})
                except Exception as e:
                    # 🔥 抓蟲程式碼必須放在這裡，對齊內部的 try！
                    print(f"⚠️ 跳過餐點【{name}】: 數字格式有誤，原因：{e}")
                    
        print(f"✅ 成功載入 {len(MAIN_DISHES)} 項餐點！")
        return f"✅ 菜單更新成功！共載入 {len(MAIN_DISHES)} 項餐點。"
    except Exception as e: 
        print(f"⚠️ 讀取 menu.csv 失敗: {e}")
        return "❌ 菜單更新失敗，請檢查檔案。"
# ==========================================
# 3. 資料庫初始化 (🔥 升級版：支援點數網址與發放紀錄)
# ==========================================
def init_db():
    # 🎯 1. 自動定位：確保路徑絕對正確
    db_dir = os.path.join(os.getcwd(), 'data')
    db_path = os.path.join(db_dir, 'user_quota.db')

    # 📂 2. 防撞檢查：如果保險箱資料夾不存在，就立刻建一個
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        print(f"📁 已自動建立資料夾: {db_dir}")

    try:
        # 🔗 3. 安全連線
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # --- 以下是您的原本表格定義 (保持不變) ---
        c.execute('''CREATE TABLE IF NOT EXISTS usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS health_profile (user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL, goal TEXT, restrictions TEXT, summary_text TEXT, active_days TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT)''')
        
        # 🔥 行銷問卷專用的資料表
        c.execute('''CREATE TABLE IF NOT EXISTS reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)''')

        for col, dtype in [("today_extra_cal", "INTEGER DEFAULT 0"), ("today_date", "TEXT DEFAULT ''"), ("sheet_name", "TEXT DEFAULT ''"), ("today_extra_pro", "INTEGER DEFAULT 0"), ("today_food_items", "TEXT DEFAULT ''")]:
            try: 
                c.execute(f"ALTER TABLE health_profile ADD COLUMN {col} {dtype}")
            except sqlite3.OperationalError: 
                pass
        # --- 以上結束 ---

        conn.commit()
        conn.close()
        print(f"✅ 保險箱資料庫連線成功！路徑: {db_path}")

    except Exception as e:
        print(f"❌ 啟動保險箱失敗，錯誤原因: {e}")
init_db()
load_menu()  # 🔥 伺服器啟動時自動載入菜單

# ==========================================
# 4. 接收表單與配餐 (過敏原雷達 + 完美排序)
# ==========================================
@app.post("/form-data")
async def receive_form_data(request: Request):
    try:
        data = await request.json()
        print(f"📦 [表單測試] 收到 Google 傳來的大禮包：{data}")
        
        def get_val(keyword):
            for k, v in data.items():
                if keyword in k and v: 
                    return ",".join([str(i) for i in v]) if isinstance(v, list) else str(v)
            return ""
        
        user_id = get_val("UID")
        print(f"🔍 [表單測試] 抓到的 UID 是：'{user_id}'")
        print(f"🔑 [DEBUG] 表單所有欄位 keys：{list(data.keys())}")
        print(f"📝 [DEBUG] 稱呼欄位比對結果：{ {k: v for k, v in data.items() if '稱呼' in str(k)} }")
        
        if not user_id or user_id == "UID_REPLACE_ME": 
            print("❌ [表單拒絕] 找不到有效的 UID，這張表單我直接丟掉！")
            return {"status": "ignored"}
        if user_id in user_memory: del user_memory[user_id]

        name, goal, restrictions = get_val("稱呼"), get_val("目標"), get_val("禁忌")
        weight, height, age, gender = float(get_val("體重") or 70), float(get_val("身高") or 170), float(get_val("年齡") or 30), get_val("性別")
        # 🔥 身高防呆：如果客人填 1.76 公尺，自動轉成 176 公分
        if height < 3.0:
            height *= 100
        activity = get_val("活動量")
        
        bmr = (10 * weight + 6.25 * height - 5 * age - 161) if "女" in gender else (10 * weight + 6.25 * height - 5 * age + 5)
        act_mult = 1.2
        if "輕" in activity: act_mult = 1.375
        elif "中" in activity: act_mult = 1.55
        elif "高" in activity: act_mult = 1.725
        elif "極" in activity: act_mult = 1.9
        tdee_base = bmr * act_mult
        
        protein = weight * 1.6
        if "減脂" in goal: 
            tdee = tdee_base - 300
            protein = weight * 2.0
        elif "增肌" in goal: 
            tdee = tdee_base + 300
            protein = weight * 2.0
        else: tdee = tdee_base
        
        base_lunch_pool = [d for d in MAIN_DISHES if d.get('category') == 'main']
        base_dinner_pool = [d for d in MAIN_DISHES if d.get('category') == 'main']
        
        if restrictions:
            noise_words = ['跟', '和', '與', '、', '，', ' ', '不吃', '不要', '不能', '不能吃', '過敏', '類', '我對', '另外']
            clean_res = restrictions
            for noise in noise_words:
                clean_res = clean_res.replace(noise, ',')
                
            bad_words = [w.strip() for w in clean_res.split(',')]
            bad_words = [w for w in bad_words if w]
            
            major_allergens = ['牛', '豬', '雞', '羊', '海鮮', '魚', '蝦', '蟹', '堅果', '花生', '起司', '豆']
            for ma in major_allergens:
                if ma in restrictions and ma not in bad_words:
                    bad_words.append(ma)
            
            safe_lunch_pool = [d for d in base_lunch_pool if not any(bw in d['name'] or bw in d.get('ingredients', '') for bw in bad_words)]
            safe_dinner_pool = [d for d in base_dinner_pool if not any(bw in d['name'] or bw in d.get('ingredients', '') for bw in bad_words)]
            
            lunch_pool = safe_lunch_pool if safe_lunch_pool else base_lunch_pool
            dinner_pool = safe_dinner_pool if safe_dinner_pool else base_dinner_pool
        else:
            lunch_pool = base_lunch_pool
            dinner_pool = base_dinner_pool
        
        schedule_lines, total_price, active_days = [], 0, set()
        schedule_sheet_rows = [["週期與星期", "午餐安排", "晚餐安排", "熱量剩餘 / 蛋白質需補"]]
        
        plan_requests = []
        week_dict = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}
        
        # 1. 抓取表單中的關鍵資訊
        date_str = get_val("日期") or get_val("取餐") or get_val("勾選")
        user_restrictions = restrictions.lower() # 顧客禁忌 (小寫化方便比對)
        
        # 2. 抓取顧客喜好標籤
        pref_staple = get_val("主食偏好") or ""
        pref_protein = get_val("蛋白質") or ""
        
        # 🔥 定義真正喜歡的關鍵字 (解決「沒有飯」卻抓到「飯」的 Bug)
        liked_staples = []
        if "飯食" in pref_staple: liked_staples.append("飯")   # 修正：原本錯寫成「飯食派」
        if "原型" in pref_staple: liked_staples.extend(["地瓜", "南瓜", "馬鈴薯", "原型"])  # 加入「原型」本身
        if "低碳" in pref_staple: liked_staples.extend(["低碳", "菜"])
        if "麵" in pref_staple: liked_staples.append("麵")
        if "沙拉" in pref_staple: liked_staples.append("沙拉")

        liked_proteins = []
        if "素食" in pref_protein: liked_proteins.extend(["素", "豆腐", "鷹嘴豆", "鮮蔬"])
        if "雞" in pref_protein: liked_proteins.append("雞")
        if "豬" in pref_protein: liked_proteins.append("豬")
        if "牛" in pref_protein: liked_proteins.append("牛")
        if "海鮮" in pref_protein: liked_proteins.extend(["海鮮", "魚", "鱸魚", "鮭魚"])
        
        # 3. 建立「絕對安全菜單池」 (先過濾掉禁忌，且只挑主餐)
        safe_menu = []
        # 🔥 修正：說「不要海鮮」時，擴展過濾所有魚蝦蟹相關關鍵字
        seafood_sub_words = ["魚", "蝦", "蟹", "花枝", "透抽", "章魚", "牡蠣", "鮭", "鱸", "鮪", "鯖"]
        for dish in MAIN_DISHES:
            if dish.get('category') != 'main':
                continue
            dish_name = dish['name'].lower()
            is_safe = True
            forbidden_keywords = ["牛", "豬", "雞", "魚", "海鮮", "蝦", "蟹"]
            for word in forbidden_keywords:
                if word in user_restrictions and word in dish_name:
                    is_safe = False
                    break
            # 特殊處理：用戶寫「海鮮」禁忌時，同步過濾菜名含魚蝦蟹字樣的餐點
            if is_safe and "海鮮" in user_restrictions:
                if any(sw in dish_name for sw in seafood_sub_words):
                    is_safe = False
            if is_safe:
                safe_menu.append(dish)

        # 4. 解析取餐日期並進行「超級紅娘配對」 (🔥 融合終極穩定版 + 主食黑名單)
        plan_requests = []
        total_price = 0  
        
        if date_str:
            week_dict = {
                "星期一": 1, "週一": 1, "星期二": 2, "週二": 2, 
                "星期三": 3, "週三": 3, "星期四": 4, "週四": 4, 
                "星期五": 5, "週五": 5, "星期六": 6, "週六": 6, "星期日": 7, "週日": 7
            }
            days = [d.strip() for d in date_str.split(',')]
            active_days_list = [] 
            week_tracker = {1:0, 2:0, 3:0, 4:0, 5:0, 6:0, 7:0}
            
            for d in days:
                d_num = next((num for zh, num in week_dict.items() if zh in d), 99)
                if d_num != 99:
                    active_days_list.append(d)
                    week_tracker[d_num] += 1
                    w_num = week_tracker[d_num]
                    
                    # 🔥 終極主食地雷過濾系統 (漏掉的就是這裡！)
                    unliked_staples = []
                    if "都不挑食" not in pref_staple:
                        if "沙拉" not in pref_staple: unliked_staples.append("沙拉")
                        if "麵" not in pref_staple: unliked_staples.extend(["麵", "義大利麵", "烏龍", "筆管"])
                        if "飯" not in pref_staple: unliked_staples.extend(["飯", "燉飯", "紫米", "糙米"])
                    
                    matches = []
                    for dish in safe_menu:
                        d_text = (dish['name'] + dish.get('ingredients', '')).lower()
                        
                        # 1. 踩到主食地雷？直接淘汰！沙拉跟麵絕對進不來
                        if any(us in d_text for us in unliked_staples):
                            continue
                            
                        # 2. 檢查是否命中喜歡的主食 (包含原型地瓜)
                        if "都不挑食" in pref_staple or not liked_staples:
                            matches.append(dish)
                        elif any(ls in d_text for ls in liked_staples):
                            matches.append(dish)
                            
                    # 優先用完美命中的池子，如果不夠，用排除地雷後的安全池
                    if len(matches) >= 2:
                        pool = matches
                    else:
                        pool = [dish for dish in safe_menu if not any(us in (dish['name'] + dish.get('ingredients', '')).lower() for us in unliked_staples)]
                        if len(pool) < 2:
                            pool = safe_menu 
                    
                    # 隨機抽 2 道菜
                    if len(pool) >= 2:
                        daily_pick = random.sample(pool, 2)
                    elif len(pool) == 1:
                        daily_pick = [pool[0], pool[0]]
                    else:
                        continue 
                    
                    plan_requests.append((w_num, d_num, f"第{w_num}週", d, daily_pick[0], daily_pick[1]))
                    # 💡 累加餐點總價
                    total_price += (daily_pick[0]['price'] + daily_pick[1]['price'])

        # 排序確保顯示順序正確
        plan_requests.sort(key=lambda x: (x[0], x[1]))

        # 5. 生成預覽文字與試算表資料
        schedule_text = ""
        # 👉【修改】表頭多加一個「單日金額」
        schedule_sheet_rows = [["週期與星期", "午餐安排", "晚餐安排", "熱量剩餘 / 蛋白質需補", "單日金額"]]
        
        for w_num, d_num, w_label, day_name, lunch, dinner in plan_requests:
            day_tdee_left = int(tdee) - lunch['cal'] - dinner['cal']
            day_p_need = int(protein) - lunch['pro'] - dinner['pro']
            daily_price = lunch['price'] + dinner['price'] # 👉【新增】計算單日金額
            
            # 👉【修改】文字中補上價格 ($) 與 蛋白質需補 (g)，這樣 AI 才看得到蛋白質！
            schedule_text += f"\n【{w_label}-{day_name}】\n☀️午：{lunch['name']} ({lunch['cal']}kcal / ${lunch['price']})\n🌙晚：{dinner['name']} ({dinner['cal']}kcal / ${dinner['price']})\n👉 當日熱量剩餘: {day_tdee_left}kcal\n👉 蛋白質需補: {day_p_need}g\n"
            
            # 👉【修改】試算表行數也補上金額
            schedule_sheet_rows.append([f"{w_label}-{day_name}", f"{lunch['name']} (${lunch['price']})", f"{dinner['name']} (${dinner['price']})", f"剩 {day_tdee_left}kcal / 補 {day_p_need}g", f"${daily_price}"])

        # 更新資料庫紀錄 (保留續約歷史)
        today_str_for_sheet = tw_now().strftime("%Y%m%d")
        safe_name = f"{name}_{user_id[-4:]}_{today_str_for_sheet}"
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO health_profile (user_id, name, tdee, protein, goal, restrictions, summary_text, active_days, today_extra_cal, today_date, sheet_name) VALUES (?,?,?,?,?,?,?,?,0,'',?)", (user_id, name, int(tdee), protein, goal, restrictions, schedule_text, ",".join(active_days_list), safe_name))
        conn.commit(); conn.close()

        # 6. 寫入 Google 試算表 (包含個人分頁功能)
        if gc:
            try:
                sheet = gc.open_by_url(SHEET_URL)
                # 寫入總表
                main_sheet = sheet.sheet1
                now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
                row_data = [now_str, name, goal, int(tdee), int(protein), restrictions, total_price, ",".join(active_days_list), schedule_text]
                main_sheet.append_row(row_data)
                
                # 🔥 為客戶建立或更新專屬分頁 (保留原本功能)
                try:
                    try:
                        user_sheet = sheet.add_worksheet(title=safe_name, rows="1000", cols="8")
                    except:
                        user_sheet = sheet.worksheet(safe_name)
                        user_sheet.clear()
                        
                    # 👉【修改】在個人分頁的第一行，最後面加入「💰 排餐總額」
                    profile_data = [["【VIP 客戶檔案】", f"姓名: {name}", f"目前體重: {weight} kg", f"目標: {goal}", f"TDEE: {int(tdee)} kcal", f"蛋白質: {int(protein)} g", f"禁忌: {restrictions}", f"喜好: {pref_staple} + {pref_protein}", f"💰 排餐總額: ${total_price}"], [""]]
                    menu_title = [["【專屬排餐計畫 (第1週~第4週)】"]]
                    tracking_headers = [[""], ["================================================================="], ["【日常飲食與動態追蹤】"], ["紀錄時間", "紀錄類型", "客人傳送內容", "數值變化(kcal)"]]
                    
                    user_sheet.append_rows(profile_data + menu_title + schedule_sheet_rows + tracking_headers)
                    print(f"✅ 成功為 {name} 建立含菜單的專屬分頁：{safe_name}")
                except Exception as e:
                    print(f"⚠️ 建立專屬分頁失敗: {e}")
                    
            except Exception as e:
                print(f"⚠️ 寫入 Google 試算表失敗: {e}")

        # 最後推播訊息給客人
        # 👉【修改】回覆客人的訊息中，補上本次排餐總額！
        push_msg = f"🎉 {name} 填表成功！\nAI 營養師已為您精算：\n🔥 TDEE: {int(tdee)} kcal\n🥩 蛋白質: {int(protein)} g\n💰 本次排餐總額: ${total_price}\n\n現在請點擊選單的『查看菜單』，我將為您列出每一天的詳細餐點與價格！"
        line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
        return {"status": "success"}

    except Exception as e: 
        print(f"💥 [表單崩潰致命錯誤]: {str(e)}")
        return {"status": "error", "msg": str(e)}
# ==========================================
# 🔥 滿意度問卷接收器 (自動發放不重複點數)
# ==========================================
@app.post("/survey-data")
async def receive_survey_data(request: Request):
    try:
        data = await request.json()
        print(f"📝 [問卷測試] 收到問卷資料：{data}")
        
        # 抓取表單裡的 UID
        user_id = ""
        for k, v in data.items():
            if "UID" in k.upper():
                user_id = str(v).strip()
                break
                
        if not user_id or user_id == "UID_REPLACE_ME":
            return {"status": "ignored", "msg": "無效的 UID"}

        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        
        # 1. 檢查這個人是不是已經領過點數了？(防貪小便宜)
        c.execute("SELECT claim_date FROM survey_records WHERE user_id=?", (user_id,))
        if c.fetchone():
            conn.close()
            # 已經領過，不再發網址，但可以回個感謝訊息
            try: line_bot_api.push_message(user_id, TextSendMessage(text="❤️ 感謝您再次填寫問卷！您之前已經領取過集點卡點數囉，一日樂食祝您有美好的一天！"))
            except: pass
            return {"status": "already_claimed"}

        # 2. 從保險箱抽出一張「還沒被使用」的點數網址
        c.execute("SELECT link FROM reward_links WHERE is_used=0 LIMIT 1")
        row = c.fetchone()
        
        if row:
            reward_link = row[0]
            # 標記為已使用，並記錄這個人已經領過
            c.execute("UPDATE reward_links SET is_used=1 WHERE link=?", (reward_link,))
            c.execute("INSERT INTO survey_records (user_id, claim_date) VALUES (?, ?)", (user_id, tw_today().isoformat()))
            conn.commit()
            
            # 3. 把專屬點數網址私訊給客人
            push_msg = f"🎉 感謝您的寶貴回饋！\n\n這是答應您的專屬獎勵，請點擊下方連結領取【一日樂食集點卡 1 點】👇\n\n{reward_link}\n\n(⚠️ 注意：此連結為專屬一次性連結，點擊後即失效，請勿轉發給他人喔！)"
            line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
        else:
            # 點數發光了，通知老闆！
            c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
            admin_row = c.fetchone()
            if admin_row:
                line_bot_api.push_message(admin_row[0], TextSendMessage(text="🚨 老闆緊急通知：填問卷送點數的「點數網址」已經被抽光啦！請盡快上後台產生新的網址並用 #上傳點數 補貨！"))
        
        conn.close()
        return {"status": "success"}
    except Exception as e:
        print(f"⚠️ 問卷處理錯誤: {e}")
        return {"status": "error"}
# ==========================================
# 5. AI 對話引擎 (🔥 終極防偷懶 + 食物記憶版)
# ==========================================
def get_ai_response_with_memory(user_id, user_msg):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    
    # 抓取客人資料
    c.execute("SELECT summary_text, tdee, active_days, protein FROM health_profile WHERE user_id=?", (user_id,))
    hp = c.fetchone()
    
    today_str = tw_today().isoformat()
    # 抓取今日外食紀錄 (多抓 today_food_items)
    c.execute("SELECT today_extra_cal, today_date, sheet_name, name, today_extra_pro, today_food_items FROM health_profile WHERE user_id=?", (user_id,))
    daily_rec = c.fetchone()
    
    # 判斷是不是新的一天，如果是就歸零 (包含食物清單)
    if daily_rec and daily_rec[1] != today_str:
        c.execute("UPDATE health_profile SET today_extra_cal=0, today_extra_pro=0, today_food_items='', today_date=? WHERE user_id=?", (today_str, user_id))
        conn.commit() 
        extra_cal, extra_pro, food_items = 0, 0, ""
    else:
        extra_cal = daily_rec[0] if daily_rec else 0
        extra_pro = daily_rec[4] if (daily_rec and len(daily_rec) > 4 and daily_rec[4] is not None) else 0
        food_items = daily_rec[5] if (daily_rec and len(daily_rec) > 5 and daily_rec[5] is not None) else ""

    report = f"\n【絕對參考報告內容】:\n{hp[0]}" if hp else "\n檔案未填，請引導客人填表。"
    tdee_val = hp[1] if hp else 2000
    active_days = hp[2] if hp else ""
    protein_val = hp[3] if hp else 100
    history = user_memory.get(user_id, [])[-6:]
    ingredients_memo = "\n".join([f"- {d['name']}: {d.get('ingredients', '新鮮食材')}" for d in MAIN_DISHES])
    
    food_items_text = food_items if food_items else "無"
    
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    today_str_zh = weekdays[tw_today().weekday()]
    
    if today_str_zh in active_days:
        today_status = f"✅ 今天 ({today_str_zh}) 是顧客的【取餐日】。"
        calc_formula = f"""
        2. 依照以下格式列出計算過程（務必在算式中寫出這次紀錄的「食物品項名稱」）：
           【真正熱量餘額】 = 【當日熱量剩餘】 - {extra_cal} (稍早累積: {food_items_text}) - 本次紀錄: [品項名稱] (熱量數字) = OOO 大卡
           【真正蛋白需求】 = 【蛋白質需補】 - {extra_pro} (稍早累積: {food_items_text}) - 本次紀錄: [品項名稱] (蛋白數字) = OOO 克
        3. 告訴他：「今天扣除一日樂食餐點與稍早外食，再加上這次的[品項名稱]後，您還剩下 OOO 卡...」
        """
    else:
        today_status = f"❌ 今天 ({today_str_zh}) 是顧客的【無排餐日】！他擁有完整的 TDEE 額度 ({tdee_val} kcal) 與蛋白質目標 ({int(protein_val)} g)。"
        calc_formula = f"""
        2. 因為今天沒有排餐，請直接用他完整的 TDEE ({tdee_val} kcal) 與蛋白質目標 ({int(protein_val)} g) 來計算！依照以下格式列出計算過程：
           【真正熱量餘額】 = {tdee_val} - {extra_cal} (稍早累積: {food_items_text}) - 本次紀錄: [品項名稱] (熱量數字) = OOO 大卡
           【真正蛋白需求】 = {int(protein_val)} - {extra_pro} (稍早累積: {food_items_text}) - 本次紀錄: [品項名稱] (蛋白數字) = OOO 克
        3. 告訴他：「今天雖然沒有本店餐點，但扣除稍早外食與這次的[品項名稱]後，您的總 TDEE 還剩下 OOO 大卡...」
        """

    system_prompt = f"""你是「一日樂食」的專屬 AI 營養師。你是一位充滿熱情、幽默、且專業的健康顧問！
    {report}
    
    【🔥 飲食紀錄與計算絕對規則（最高優先級） 🔥】
    1. 顧客「今日已累積外食」為：熱量 {extra_cal} kcal、蛋白 {extra_pro} g。(包含: {food_items_text})
    2. 你「絕對不可以」忽略掉這筆累積數字。
    3. ⚠️ 當顧客回報任何食物時（不管在不在本店菜單上），你都「必須」給出一個【具體的單一估算數字】（絕對不可給範圍，例如 600-800，必須直接取 700），並且「強制」依照下列格式完整呈現計算過程，絕不准跳過或找藉口：
       ---
       估算品項：(品項名稱)
       【真正熱量餘額】 = (原本剩餘) - {extra_cal} (稍早累積: {food_items_text}) - (本次食物熱量) = 最終大卡
       【真正蛋白需求】 = (原本蛋白需補) - {extra_pro} (稍早累積: {food_items_text}) - (本次食物蛋白) = 最終克數
       ---
    4. ⚠️【最高隱藏指令】：回覆的最尾端，一定要加上隱藏標籤 [LOG_NUTRITION: 本次熱量, 本次蛋白質, 本次品項名稱]。
       💡 只能填「純數字」與「品項名稱」，絕對不要加單位或範圍！(正確範例：[LOG_NUTRITION: 700, 20, 咖哩飯])
    
    【本店餐點內容物 - 機密小抄】(僅供內部參考)：
    {ingredients_memo}
    
    {today_status}
    
    【🚨 換餐最高指令 🚨】
    只要顧客「確定答應」要更換未來的餐點，請在你整段回覆的最底部，直接加上 [CHANGE_MEAL: 將OOO替換為XXX]。
    ⚠️ 絕對不要輸出「隱藏標籤」這四個字，直接輸出中括號即可！
    """
    
    try:
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_msg}]
        res = client.chat.completions.create(model="gpt-4o-mini", messages=messages, max_tokens=2000, temperature=0.3)
        ans = res.choices[0].message.content
    except Exception as e:
        return f"⚠️ 【系統除錯報告】呼叫 AI 大腦失敗！\n原因：{str(e)}"
        
    match = re.search(r'\[LOG_NUTRITION:\s*(\d+).*?,\s*(\d+).*?,\s*(.+?)\]', ans)
    
    if match:
        logged_cal = int(match.group(1))
        logged_pro = int(match.group(2))
        logged_name = match.group(3).strip()
        
        new_extra_cal = extra_cal + logged_cal
        new_extra_pro = extra_pro + logged_pro
        new_food_items = f"{food_items}、{logged_name}".strip("、") if food_items else logged_name
        
        c.execute("UPDATE health_profile SET today_extra_cal=?, today_extra_pro=?, today_food_items=? WHERE user_id=?", (new_extra_cal, new_extra_pro, new_food_items, user_id))
        conn.commit()
        ans = re.sub(r'\[LOG_NUTRITION:.*?\]', '', ans).strip()
        
        if daily_rec and daily_rec[2] and gc:
            try:
                sheet = gc.open_by_url(SHEET_URL)
                now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.worksheet(daily_rec[2]).append_row([now_str, "外食熱量與蛋白打卡", user_msg, f"+{logged_cal} kcal / +{logged_pro} g ({logged_name})"])
            except Exception: pass

    match_change = re.search(r'\[CHANGE_MEAL:\s*(.+?)\]', ans)
    if match_change:
        change_req = match_change.group(1)
        ans = re.sub(r'\[CHANGE_MEAL:\s*.+?\]', '', ans).strip()
        ans = ans.replace('隱藏標籤', '').replace('`', '').strip()
        
        c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
        admin_row = c.fetchone()
        if admin_row:
            customer_name = daily_rec[3] if daily_rec else "顧客"
            boss_msg = f"⚠️【廚房換餐通知】\n顧客 {customer_name} 要求換餐：\n👉 {change_req}\n\n請廚房注意備餐！"
            try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=boss_msg))
            except Exception: pass

    conn.close()
    user_memory[user_id] = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": ans}]
    return ans

# ==========================================
# 6. 其他輔助函數與 Webhook (🔥 融合版：完整保留測距、VIP功能)
# ==========================================
def check_permission_and_quota(user_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    today = tw_today().isoformat()
    c.execute("SELECT remaining_chat_quota, remaining_meals, last_date, status, expiry_date, daily_chat_limit FROM usage WHERE user_id=?", (user_id,))
    record = c.fetchone()
    if record is None: conn.close(); return False, ""
    q, m, ld, s, ed, dcl = record
    if ed and today > ed: conn.close(); return False, ""
    if ld != today: q = dcl
    if q > 0:
        c.execute("UPDATE usage SET remaining_chat_quota=?, last_date=? WHERE user_id=?", (q-1, today, user_id))
        conn.commit(); conn.close()
        return True, f"(剩{m}餐 | 諮詢:{q-1})"


def send_tomorrow_reminders():
    tomorrow = tw_today() + datetime.timedelta(days=1)
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    tomorrow_str = weekdays[tomorrow.weekday()]
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{tomorrow_str}%",))
    users = c.fetchall(); conn.close()
    count = 0
    for uid, name in users:
        msg = f"🌙 {name} 晚安！\n明天 ({tomorrow_str}) 是您的專屬取餐日喔！\n\n💪 營養師溫馨提醒：\n為確保您的營養達標，明天需要幫您額外準備【舒肥雞胸肉】或【無糖豆漿】來補足蛋白質缺口嗎？\n(直接回覆需要的品項，店長明天就會幫您準備好！)"
        try: line_bot_api.push_message(uid, TextSendMessage(text=msg)); count += 1
        except Exception: pass
    return f"✅ 成功發送了 {count} 封明日取餐提醒推播！"

def get_distance(origin_address, target_address, mode="driving"):
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {"origins": origin_address, "destinations": target_address, "mode": mode, "language": "zh-TW", "key": GOOGLE_MAPS_API_KEY}
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if data.get("status") == "OK":
            element = data["rows"][0]["elements"][0]
            if element.get("status") == "OK":
                return True, element["distance"]["text"], element["distance"]["value"], element["duration"]["text"]
        return False, "", 0, ""
    except: return False, "", 0, ""

def generate_package_codes(t, n):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor(); codes = []
    m, d, l, p = (24,31,20,"#VIP24-") if t=="24m" else (48,31,30,"#VIP48-")
    for _ in range(n):
        c_str = p + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        c.execute("INSERT INTO vips VALUES (?,?,?,?,0)", (c_str, m, d, l)); codes.append(c_str)
    conn.commit(); conn.close(); return codes

def redeem_code(uid, code):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT meals, duration_days, chat_limit FROM vips WHERE code=? AND is_used=0", (code,))
    r = c.fetchone()
    if not r: conn.close(); return None, "❌ 無效"
    m, d, l = r; today = tw_today()
    c.execute("UPDATE vips SET is_used=1 WHERE code=?", (code,))
    c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (uid,))
    u = c.fetchone(); curr_m = u[0] if u else 0
    exp = (today + datetime.timedelta(days=d)).isoformat()
    c.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?)", (uid, l, curr_m+m, today.isoformat(), 'vip', exp, l))
    conn.commit(); conn.close()
    link = f"https://docs.google.com/forms/d/e/1FAIpQLSdVY7Zf-E2zSpsOFmItYHI0YtTujX6Ucux4QTQ3gjg5wcomgA/viewform?usp=pp_url&entry.1461831832={uid}"
    return exp, f"🎉 兌換成功！\n您的專屬排餐表單：\n{link}"

@app.post("/callback")
async def callback(request: Request):
    sig = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try: 
        handler.handle(body.decode("utf-8"), sig)
    except InvalidSignatureError: 
        print("⚠️ LINE 簽章錯誤！請檢查 Railway 的 LINE_CHANNEL_SECRET 是否填錯或有空格！")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e: 
        print(f"⚠️ LINE 訊息處理發生嚴重錯誤: {e}")
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg_id = event.message.id
    if msg_id in processed_messages: return 
    processed_messages.add(msg_id)
    if len(processed_messages) > 1000: processed_messages.clear()

    msg, uid = event.message.text.strip(), event.source.user_id
    
    # 🔥 LINE 圖文選單攔截區
    if msg == "填寫體質表單":
        form_link = f"https://docs.google.com/forms/d/e/1FAIpQLSdVY7Zf-E2zSpsOFmItYHI0YtTujX6Ucux4QTQ3gjg5wcomgA/viewform?usp=pp_url&entry.1461831832={uid}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📝 請點擊下方專屬連結，填寫您的體質評估表單：\n\n{form_link}\n\n(系統已為您自動帶入 LINE 帳號，請直接填寫即可喔！)"))
        return
    elif msg == "填寫滿意度問卷":
        # 👉 老闆注意：請把下面這串網址，換成您剛剛在 Google 表單產生的那串「最後面有 {uid} 的黃金連結」！
        survey_link = f"https://docs.google.com/forms/d/e/1FAIpQLScF6Va_sdq6KMaKFd8BUVB2x5SyLji3JqX28-Z7h-tuLnpB-Q/viewform?usp=pp_url&entry.1048958109={uid}"
        
        reply_text = f"🎁 感謝您對一日樂食的支持！\n請點擊下方專屬連結填寫滿意度調查 (約1分鐘)。\n\n完成填寫後，系統將自動發送【1 點集點卡點數】給您喔！👇\n\n{survey_link}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "查看菜單":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT summary_text FROM health_profile WHERE user_id=?", (uid,))
        hp = c.fetchone(); conn.close()
        reply_text = f"🍽️ 這是為您量身打造的專屬菜單：\n\n{hp[0]}\n\n(若想更換菜色或加購單品，可以直接打字告訴我喔！)" if hp and hp[0] else "您好像還沒填寫體質評估表單喔！請點擊選單來建立專屬檔案吧！📝"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "我要紀錄飲食":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="今天吃了什麼好料呢？📸\n\n您可以直接打字告訴我（例如：我剛吃了一個大麥克和中薯），我會立刻幫您估算熱量，並將紀錄存入您的【專屬 VIP 檔案】中喔！💪"))
        return
    elif msg == "運費怎麼算":
        reply_text = "想知道專屬外送運費嗎？🛵\n\n請直接在對話框輸入：\n「#測距 您的完整地址」\n\n例如：\n#測距 台北市信義區松仁路90號\n\n系統就會立刻為您啟動智能順風車報價喔！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "我的會員狀態":
        allow, q_msg = check_permission_and_quota(uid)
        if allow: reply_text = f"💎 您的 VIP 會員狀態：\n\n您目前還剩下：\n{q_msg}\n\n請繼續保持健康的飲食習慣喔！"
        else: reply_text = "您目前尚未開通 VIP 方案，或是方案已到期。\n請輸入您的 VIP 邀請碼 (例如 #VIP24-XXXXXX) 來解鎖專屬 AI 營養師與訂餐服務！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 👑 老闆專屬指令區 👑
    if msg == "#綁定老闆":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO admin_settings VALUES ('admin_id', ?)", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 老闆好！系統已成功綁定。\n客人的【換餐通知】都會私訊給您！"))
        return
    elif msg == "#點數庫存":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        # 算一下沒用過的 (is_used=0)
        c.execute("SELECT COUNT(*) FROM reward_links WHERE is_used=0")
        unused_count = c.fetchone()[0]
        # 算一下已經發出去的 (is_used=1)
        c.execute("SELECT COUNT(*) FROM reward_links WHERE is_used=1")
        used_count = c.fetchone()[0]
        conn.close()
        
        reply_msg = f"📊 【老闆專屬：點數庫存報告】\n\n🟢 尚未發送：{unused_count} 張\n🔴 已經發出：{used_count} 張\n\n(歷史總共上傳過 {unused_count + used_count} 張點數網址)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return
    elif msg == "#更新菜單":
        reply_msg = load_menu()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return
    elif msg == "#今日出餐完成":
        weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        today_str = weekdays[tw_today().weekday()]
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{today_str}%",))
        users = c.fetchall()
        
        count, notify_count = 0, 0
        for u in users:
            u_id, u_name = u
            c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (u_id,))
            res = c.fetchone()
            if res and res[0] > 0:
                new_meals = res[0] - 1
                c.execute("UPDATE usage SET remaining_meals=? WHERE user_id=?", (new_meals, u_id))
                count += 1
                if new_meals <= 3 and new_meals > 0:
                    notify_msg = f"🎉 {u_name} 您好！您的專屬方案只剩最後 {new_meals} 餐囉！\n您可以直接回覆我「我要續約」，系統將為您無縫安排下一期菜單！"
                    try: 
                        line_bot_api.push_message(u_id, TextSendMessage(text=notify_msg)); notify_count += 1
                    except Exception: pass
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 報告老闆！今日 ({today_str}) 出餐扣除完畢！\n共扣除了 {count} 份餐點，發送 {notify_count} 則續約推播！"))
        return
    
    # 這裡開始的 elif 必須跟上面其他的 elif 對齊 (退回一格)
    elif msg.startswith("#上傳點數\n"):
        links = msg.replace("#上傳點數\n", "").strip().split('\n')
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        count = 0
        for link in links:
            if link.strip():
                try:
                    c.execute("INSERT INTO reward_links (link, is_used) VALUES (?, 0)", (link.strip(),))
                    count += 1
                except sqlite3.IntegrityError: pass # 避免重複存入
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 報告老闆！成功存入 {count} 筆全新的點數網址！"))
        return
        
    elif msg == "#發送明日提醒":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=send_tomorrow_reminders()))
        return
    elif msg == "#生24":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🎁 24餐邀請碼：\n{chr(10).join(generate_package_codes('24m', 3))}"))
        return
    elif msg == "#生48":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔥 48餐邀請碼：\n{chr(10).join(generate_package_codes('48m', 3))}"))
        return
    elif msg.startswith("#VIP"):
        expiry, res = redeem_code(uid, msg)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=res))
        return
    elif msg == "#清空熱量":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("UPDATE health_profile SET today_extra_cal=0, today_extra_pro=0 WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🔄 報告老闆，今日偷吃紀錄（含熱量與蛋白質）已歸零！"))
        return
    elif msg == "#刪除檔案":
        if uid in user_memory: del user_memory[uid]
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("DELETE FROM health_profile WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="💥 老闆好，檔案與記憶已徹底銷毀！請重新填表！"))
        return
    elif msg == "#重置":
        if uid in user_memory: del user_memory[uid]
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        # 🔥 強制寫入一筆無限期、50次額度的紀錄！就算資料庫被洗白也能救回來
        today = tw_today().isoformat()
        c.execute("INSERT OR REPLACE INTO usage (user_id, remaining_chat_quota, remaining_meals, last_date, status, expiry_date, daily_chat_limit) VALUES (?, 50, 99, ?, 'vip', '2099-12-31', 50)", (uid, today))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="👑 老闆特權啟動！系統已強制為您開通 VIP 檔案並補滿 50 次額度！現在請問我熱量！"))
        return
        
    # 🗺️ 智能測距與順風車 🗺️
    elif msg.startswith("#測距 "):
        target_address = msg.replace("#測距 ", "").strip()
        success, dist_text, dist_meters, duration_text = get_distance(STORE_ADDRESS, target_address)
        if success:
            hub_match = None
            for hub in HUBS:
                h_succ, h_d_txt, h_d_m, h_t_txt = get_distance(hub["address"], target_address, mode="walking")
                if h_succ and h_d_m <= 1000: hub_match = hub["name"]; break 
            
            if hub_match: fee_msg = f"20 元 🎉 (恭喜！您符合【{hub_match}】周邊 1 公里專屬順風車特惠！)"
            else:
                if dist_meters <= 2000: fee_msg = "0 元 (2公里內免運專案！)"
                elif dist_meters <= 4000: fee_msg = "40 元"
                elif dist_meters <= 6000: fee_msg = "80 元"
                else: fee_msg = "超出自家車隊範圍，建議自取或由 Lalamove 專車報價配送喔！"
            reply_text = f"🛵 **一日樂食 外送試算結果**\n📍 目的地：{target_address}\n📏 距本店距離：{dist_text}\n⏱️ 騎車時間：{duration_text}\n💰 運費評估：{fee_msg}"
        else: reply_text = "哎呀！地圖系統暫時找不到這個地址，請確認地址是否完整喔！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 🟢 顧客一般對話 (串接 AI) 🟢
    allow, q_msg = check_permission_and_quota(uid)
    if not allow: return
    else: line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{get_ai_response_with_memory(uid, msg)}\n\n{q_msg}"))
# ==========================================
# 🤖 隱形店長專用函數 (自動化排程任務)
# ==========================================
def auto_daily_meal_deduction():
    """每天自動扣除今日餐點，並發送續約通知"""
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    today_str = weekdays[tw_today().weekday()]
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{today_str}%",))
    users = c.fetchall()
    
    count, notify_count = 0, 0
    for u in users:
        u_id, u_name = u
        c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (u_id,))
        res = c.fetchone()
        if res and res[0] > 0:
            new_meals = res[0] - 1
            c.execute("UPDATE usage SET remaining_meals=? WHERE user_id=?", (new_meals, u_id))
            count += 1
            if new_meals <= 3 and new_meals > 0:
                notify_msg = f"🎉 {u_name} 您好！您的專屬方案只剩最後 {new_meals} 餐囉！\n您可以直接回覆我「我要續約」，系統將為您無縫安排下一期菜單！"
                try: line_bot_api.push_message(u_id, TextSendMessage(text=notify_msg)); notify_count += 1
                except: pass
    conn.commit(); conn.close()
    
    # 任務完成，發報告給老闆
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
    admin_row = c.fetchone()
    conn.close()
    if admin_row:
        try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=f"🤖【隱形店長報告】今日 ({today_str}) 出餐扣除自動完畢！\n共扣 {count} 份餐點，發送 {notify_count} 則續約推播！"))
        except: pass

def auto_send_tomorrow_reminders_to_boss():
    """每天自動發送明日提醒，並跟老闆回報"""
    result_msg = send_tomorrow_reminders() # 呼叫原本寫好的推播函數
    
    # 任務完成，發報告給老闆
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
    admin_row = c.fetchone()
    conn.close()
    if admin_row:
        try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=f"🤖【隱形店長報告】明日提醒推播完畢：\n{result_msg}"))
        except: pass
