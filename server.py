import requests
import os
import json
import sqlite3
import datetime
from zoneinfo import ZoneInfo

# ?? ?�灣?��?工具?�數
TW_TZ = ZoneInfo("Asia/Taipei")
def tw_today():
    """?��??�灣今天?�日??(date ?�件)"""
    return datetime.datetime.now(TW_TZ).date()
def tw_now():
    """?��??�灣?�在?��???(datetime ?�件)"""
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

# --- 保險箱�?始�?設�? ---
# ?�們�?建�?資�?夾�??�輯移到 init_db 裡面?�更安全�?
# ?�裡?�以?�註�???��??��?�??�?init_db 一定�??�用?��?對路徑」�??��?
# -----------------------
# ==========================================
# 1. 設�??� (?�� 安全?�護?��??�鑰?�由 Railway 後台讀??
# ==========================================
STORE_ADDRESS = "?��?市松山�??�京?�路?�段133�?�???
HUBS = [
    {"name": "Anytime Fitness 信義�?, "address": "?��?市信義�??��?�?9??},
    {"name": "?�身工�? 中山�?, "address": "?��?市中山�??�京?�路二段8??}
]

# ?��? ?�裡已�??�部?�為安全寫�?，�???Railway ??Variables 後台填寫?�鑰�?
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "#GEN_CODES")
DB_DIR = os.path.join(os.getcwd(), 'data')
DB_PATH = os.path.join(DB_DIR, 'user_quota.db')

if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR, exist_ok=True)
# Google 試�?表設�?(網�??��?安全，�? service_account 保護)
SHEET_URL = "https://docs.google.com/spreadsheets/d/1cf0QhWeYynk9nqsoqMIM-Lkxk_bP57zcd-ES7Sufkqg/edit?gid=0#gid=0"

# ?�� 設�? FastAPI ?��??�週�??�隱形�??��?�?
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 伺�??��??��?，�??�隱形�???
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    
    # ???��??�表：�?�?14:00 ?��?????�催繳�?�?
    scheduler.add_job(auto_daily_meal_deduction, 'cron', hour=14, minute=0)
    
    # ???��??�表：�?�?20:00 ?��??�送�??��?餐�??�購?��?
    scheduler.add_job(auto_send_tomorrow_reminders_to_boss, 'cron', hour=20, minute=0)
    
    scheduler.start()
    print("???�自?��??�器已�??��?系統?�入?�人駕�?模�? ON�?)
    
    yield
    
    # 伺�??��??��?，�?店長下班
    scheduler.shutdown()

# �??建�??�用了�??�器??FastAPI ?�用程�?
app = FastAPI(lifespan=lifespan)
client = OpenAI(api_key=OPENAI_API_KEY)
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
user_memory = {}
processed_messages = set()

# ?��? Google ?�擬?��? (?�� ?��?裝甲，�?歸�?淨�?)
try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    # 1. ?�接從�??�箱?�出完�??��?�?
    creds_str = os.environ.get("GOOGLE_CREDENTIALS")
    
    # 2. ?��??�味轉�?字典 (什�?replace ?��??��?，�??�您貼�?太�?美�?�?
    creds_dict = json.loads(creds_str)
    
    # 3. ?�接?�鑰?��??�
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    print("??Google ?�端大�?�???��?！寫?��???100% ?��?�?)
    
except Exception as e:
    print(f"?��? Google ?��????失�?: {e}")
    gc = None

# ==========================================
# 2. ?�單資�?載入 (?�� ?��?：主�??��?精�??��??�熱?�新)
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
                name = row_clean.get("?��?", "").strip()
                if not name: continue
                try:
                    cal = float(row_clean.get("?��?(kcal)", "0").strip() or 0.0)
                    pro = float(row_clean.get("?�白�?g)", "0").strip() or 0.0)
                    price = int(row_clean.get("?�錢", row_clean.get("?�格", "150")).strip() or 150)
                    ingredients = row_clean.get("?�容??, "?�鮮食�?製�?").strip()
                    main_keywords = ["便當", "�?, "食蔬", "低碳", "沙�?"]
                    if any(kw in name for kw in main_keywords):
                        category = "main"  
                    else:
                        category = "side"  
                    MAIN_DISHES.append({"name": name, "cal": cal, "pro": pro, "price": price, "category": category, "ingredients": ingredients})
                except Exception as e:
                    # ?�� ?�蟲程�?碼�??�放?�這裡，�?齊內?��? try�?
                    print(f"?��? 跳�?餐�??�{name}?? ?��??��??�誤，�??��?{e}")
                    
        print(f"???��?載入 {len(MAIN_DISHES)} ?��?點�?")
        return f"???�單?�新?��?！共載入 {len(MAIN_DISHES)} ?��?點�?
    except Exception as e: 
        print(f"?��? 讀??menu.csv 失�?: {e}")
        return "???�單?�新失�?，�?檢查檔�???
# ==========================================
# 3. 資�?庫�?始�? (?�� ?��??��??�援點數網�??�發?��???
# ==========================================
def init_db():
    # ?�� 1. ?��?定�?：確保路徑�?對正�?
    db_dir = os.path.join(os.getcwd(), 'data')
    db_path = os.path.join(db_dir, 'user_quota.db')

    # ?? 2. ?��?檢查：�??��??�箱資�?夾�?存在，就立刻建�???
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        print(f"?? 已自?�建立�??�夾: {db_dir}")

    try:
        # ?? 3. 安全???
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # --- 以�??�您?��??�表?��?�?(保�?不�?) ---
        c.execute('''CREATE TABLE IF NOT EXISTS usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS health_profile (user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL, goal TEXT, restrictions TEXT, summary_text TEXT, active_days TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT)''')
        
        # ?�� 行銷?�卷專用?��??�表
        c.execute('''CREATE TABLE IF NOT EXISTS reward_links (link TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS survey_records (user_id TEXT PRIMARY KEY, claim_date TEXT)''')

        for col, dtype in [("today_extra_cal", "INTEGER DEFAULT 0"), ("today_date", "TEXT DEFAULT ''"), ("sheet_name", "TEXT DEFAULT ''"), ("today_extra_pro", "INTEGER DEFAULT 0")]:
            try: 
                c.execute(f"ALTER TABLE health_profile ADD COLUMN {col} {dtype}")
            except sqlite3.OperationalError: 
                pass
        # --- 以�?結�? ---

        conn.commit()
        conn.close()
        print(f"??保險箱�??�庫????��?！路�? {db_path}")

    except Exception as e:
        print(f"???��?保險箱失?��??�誤?��?: {e}")
init_db()
load_menu()  # ?�� 伺�??��??��??��?載入?�單

# ==========================================
# 4. ?�收表單?��?�?(?��??�雷??+ 完�??��?)
# ==========================================
@app.post("/form-data")
async def receive_form_data(request: Request):
    try:
        data = await request.json()
        print(f"?�� [表單測試] ?�到 Google ?��??�大禮�?：{data}")
        
        def get_val(keyword):
            for k, v in data.items():
                if keyword in k and v: 
                    return ",".join([str(i) for i in v]) if isinstance(v, list) else str(v)
            return ""
        
        user_id = get_val("UID")
        print(f"?? [表單測試] ?�到??UID ?��?'{user_id}'")
        
        if not user_id or user_id == "UID_REPLACE_ME": 
            print("??[表單?��?] ?��??��??��? UID，這張表單?�直?��??��?")
            return {"status": "ignored"}
        if user_id in user_memory: del user_memory[user_id]

        name, goal, restrictions = get_val("稱呼"), get_val("?��?"), get_val("禁�?")
        weight, height, age, gender = float(get_val("體�?") or 70), float(get_val("身�?") or 170), float(get_val("年齡") or 30), get_val("?�別")
        activity = get_val("活�???)
        
        bmr = (10 * weight + 6.25 * height - 5 * age - 161) if "�? in gender else (10 * weight + 6.25 * height - 5 * age + 5)
        act_mult = 1.2
        if "�? in activity: act_mult = 1.375
        elif "�? in activity: act_mult = 1.55
        elif "�? in activity: act_mult = 1.725
        elif "�? in activity: act_mult = 1.9
        tdee_base = bmr * act_mult
        
        protein = weight * 1.6
        if "減�?" in goal: 
            tdee = tdee_base - 300
            protein = weight * 2.0
        elif "增�?" in goal: 
            tdee = tdee_base + 300
            protein = weight * 2.0
        else: tdee = tdee_base
        
        base_lunch_pool = [d for d in MAIN_DISHES if d.get('category') == 'main']
        base_dinner_pool = [d for d in MAIN_DISHES if d.get('category') == 'main']
        
        if restrictions:
            noise_words = ['�?, '??, '??, '??, '�?, ' ', '不�?', '不�?', '不能', '不能??, '?��?', '�?, '?��?', '?��?']
            clean_res = restrictions
            for noise in noise_words:
                clean_res = clean_res.replace(noise, ',')
                
            bad_words = [w.strip() for w in clean_res.split(',')]
            bad_words = [w for w in bad_words if w]
            
            major_allergens = ['??, '�?, '??, '�?, '海鮮', '�?, '??, '??, '?��?', '?��?', '起司', '�?]
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
        schedule_sheet_rows = [["?��??��???, "?��?安�?", "?��?安�?", "?��??��? / ?�白質�?�?]]
        
        plan_requests = []
        week_dict = {"一": 1, "�?: 2, "�?: 3, "??: 4, "�?: 5, "??: 6, "??: 7}
        
        # 1. ?��?表單中�??�鍵資�?
        date_str = get_val("?��?") or get_val("?��?") or get_val("?�選")
        user_restrictions = restrictions.lower() # 顧客禁�? (小寫?�方便�?�?
        
        # 2. ?��?顧客?�好標籤
        pref_staple = get_val("主�??�好") or ""
        pref_protein = get_val("?�白�?) or ""
        
        # ?�� 定義?�正?�歡?��??��? (�?��?��??�飯?�卻?�到?�飯?��? Bug)
        liked_staples = []
        if "飯�?�? in pref_staple: liked_staples.append("�?)
        if "?��?" in pref_staple: liked_staples.extend(["?��?", "?��?", "馬鈴??])
        if "低碳" in pref_staple: liked_staples.extend(["低碳", "??])
        if "�? in pref_staple: liked_staples.append("�?)
        if "沙�?" in pref_staple: liked_staples.append("沙�?")

        liked_proteins = []
        if "素�?" in pref_protein: liked_proteins.extend(["�?, "豆�?", "鷹嘴�?, "鮮蔬"])
        if "?? in pref_protein: liked_proteins.append("??)
        if "�? in pref_protein: liked_proteins.append("�?)
        if "?? in pref_protein: liked_proteins.append("??)
        if "海鮮" in pref_protein: liked_proteins.extend(["海鮮", "�?, "鱸�?", "鮭�?"])
        
        # 3. 建�??��?對�??��??��???(?��?濾�?禁�?，�??��?主�?)
        safe_menu = []
        for dish in MAIN_DISHES:
            if dish.get('category') != 'main':
                continue
            dish_name = dish['name'].lower()
            is_safe = True
            forbidden_keywords = ["??, "�?, "??, "�?, "海鮮", "??]
            for word in forbidden_keywords:
                if word in user_restrictions and word in dish_name:
                    is_safe = False
                    break
            if is_safe:
                safe_menu.append(dish)

        # 4. �???��??��?並進�??��?級�?娘�?�?(?��??�格?�濾)??
        if date_str:
            days = [d.strip() for d in date_str.split(',') if "?? in d]
            active_days = set(days)
            week_tracker = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0}
            
            for d in days:
                d_num = next((num for zh, num in week_dict.items() if zh in d), 99)
                if d_num != 99:
                    week_tracker[d_num] += 1
                    w_num = week_tracker[d_num]
                    
                    perfect_matches = []
                    good_matches = []
                    
                    for dish in safe_menu:
                        name = dish['name'].lower()
                        
                        # 檢查?�白質�?主�??�否?�中 (如�??�都不�?食�?就�???True)
                        has_pro = any(p in name for p in liked_proteins) if liked_proteins and "?��??��?" not in pref_protein else True
                        has_sta = any(s in name for s in liked_staples) if liked_staples and "?��??��?" not in pref_staple else True
                        
                        # ?�� ?�雷?�白質濾網�?如�??��??��?客人?��??�選?��??��?，直?�判?��?�?
                        # 例�?：客人�??�「海鮮」�????字�??�鱸魚」�??�鮭魚」�?餐�?就�?被踢?��?
                        unliked_proteins = [p for p in ["�?, "??, "�?, "??, "�?, "海鮮", "鱸�?", "鮭�?", "豆�?", "鷹嘴�?] if p not in liked_proteins and "?��??��?" not in pref_protein]
                        if any(up in name for up in unliked_proteins):
                            has_pro = False # 強制不�???
                            
                        # 依�?符�?程度?�入池�?
                        if has_pro and has_sta:
                            perfect_matches.append(dish) # ?�白質�?主�??��?
                        elif has_pro: 
                            good_matches.append(dish) # ?��??�白質是對�?
                            
                    # ?��?級�?完�??�中 > ?�白質命�?> 安全??> ?�部主�? (保�?)
                    if len(perfect_matches) >= 2:
                        pool = perfect_matches
                    elif len(good_matches) >= 2:
                        pool = good_matches
                    elif len(safe_menu) >= 2:
                        pool = safe_menu
                    else:
                        # ?�終�?底�??�?�主餐�?確�?不�??��?
                        pool = [d for d in MAIN_DISHES if d.get('category') == 'main']
                    
                    # 安全?�樣：pool 不�? 2 ?��??�許?��???
                    if len(pool) >= 2:
                        daily_pick = random.sample(pool, 2)
                    elif len(pool) == 1:
                        daily_pick = [pool[0], pool[0]]
                    else:
                        continue  # ?��?沒�?，跳?�這天
                    
                    plan_requests.append((w_num, d_num, f"第{w_num}??, d, daily_pick[0], daily_pick[1]))

        plan_requests.sort(key=lambda x: (x[0], x[1]))

        # 5. ?��??�覽?��??�試算表資�?
        schedule_text = ""
        schedule_sheet_rows = [["?��??��???, "?��?安�?", "?��?安�?", "?��??��? / ?�白質�?�?]]
        total_price = 0
        
        for w_num, d_num, w_label, day_name, lunch, dinner in plan_requests:
            day_tdee_left = int(tdee) - lunch['cal'] - dinner['cal']
            day_p_need = int(protein) - lunch['pro'] - dinner['pro']
            
            schedule_text += f"\n?�{w_label}-{day_name}?�\n?�️�?：{lunch['name']} ({lunch['cal']}kcal)\n???��?{dinner['name']} ({dinner['cal']}kcal)\n?? ?�日?��??��?: {day_tdee_left}kcal\n"
            schedule_sheet_rows.append([f"{w_label}-{day_name}", lunch['name'], dinner['name'], f"??{day_tdee_left}kcal / �?{day_p_need}g"])
            total_price += (lunch['price'] + dinner['price'])
        
        # ?�� ?��??��??�入?��??��?，�?美�??��?一次�?續�?歷史�?
        today_str_for_sheet = tw_now().strftime("%Y%m%d")
        safe_name = f"{name}_{user_id[-4:]}_{today_str_for_sheet}"

        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO health_profile (user_id, name, tdee, protein, goal, restrictions, summary_text, active_days, today_extra_cal, today_date, sheet_name) VALUES (?,?,?,?,?,?,?,?,0,'',?)", (user_id, name, int(tdee), protein, goal, restrictions, schedule_text, ",".join(list(active_days)), safe_name))
        conn.commit(); conn.close()

        if gc:
            try:
                sheet = gc.open_by_url(SHEET_URL)
                main_sheet = sheet.sheet1
                now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
                row_data = [now_str, name, goal, int(tdee), int(protein), restrictions, total_price, ",".join(list(active_days)), schedule_text]
                main_sheet.append_row(row_data)
                
                try:
                    # ?�� 建�??�新?��?約�???(?�為檔�??��??��?，�?資�?不�?被�??��?)
                    try:
                        user_sheet = sheet.add_worksheet(title=safe_name, rows="1000", cols="8")
                    except:
                        # ?��?客人?��?天填了兩次表?��??��??��?天�?
                        user_sheet = sheet.worksheet(safe_name)
                        user_sheet.clear()
                        
                    # ?�� 第�?行直?��?上「�??�」�?位�??�便?��?追蹤�?
                    # ?�� 第�?行直?��?上�??�客?��?訊�??�含?�?��??�飲食�?好」�?
                    profile_data = [["?�VIP 客戶檔�???, f"姓�?: {name}", f"?��?體�?: {weight} kg", f"?��?: {goal}", f"TDEE: {int(tdee)} kcal", f"?�白�? {int(protein)} g", f"禁�?: {restrictions}", f"?�好: {pref_staple} + {pref_protein}"], [""]]
                    menu_title = [["?��?屬�?餐�???(�??�~�?????]]
                    tracking_headers = [[""], ["================================================================="], ["?�日常飲食�??��?追蹤??], ["紀?��???, "紀?��???, "客人?�送內�?, "?�值�???kcal)"]]
                    
                    user_sheet.append_rows(profile_data + menu_title + schedule_sheet_rows + tracking_headers)
                    print(f"???��?將�??��?美寫??{safe_name} 專屬?��?�?)
                except Exception as e: 
                    print(f"?��? 寫入專屬?��?失�?: {e}")
                print(f"?? ?��?寫入總表，並?�【{name}?�建立含?�單?��?屬�??��?")
            except Exception as e:
                print(f"?��? 寫入 Google 表單失�?: {e}")

        push_msg = f"?? {name} 填表?��?！\nAI ?��?師已?�您精�?：\n?�� TDEE: {int(tdee)} kcal\n?�� ?�白�? {int(protein)} g\n\n?�在請�??�選?��??�查?��??�』�??��??�您?�出每�?天�?詳細餐�??�價?��?"
        line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
        return {"status": "success"}
    except Exception as e: 
        print(f"?�� [表單崩潰?�命?�誤] ?��??�命?��?{str(e)}")
        return {"status": "error", "msg": str(e)}
# ==========================================
# ?�� 滿�?度�??�接?�器 (?��??�放不�?複�???
# ==========================================
@app.post("/survey-data")
async def receive_survey_data(request: Request):
    try:
        data = await request.json()
        print(f"?? [?�卷測試] ?�到?�卷資�?：{data}")
        
        # ?��?表單裡�? UID
        user_id = ""
        for k, v in data.items():
            if "UID" in k.upper():
                user_id = str(v).strip()
                break
                
        if not user_id or user_id == "UID_REPLACE_ME":
            return {"status": "ignored", "msg": "?��???UID"}

        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        
        # 1. 檢查?�個人?��??�已經�??��??��?�??�貪小便�?
        c.execute("SELECT claim_date FROM survey_records WHERE user_id=?", (user_id,))
        if c.fetchone():
            conn.close()
            # 已�??��?，�??�發網�?，�??�以?�個�?謝�???
            try: line_bot_api.push_message(user_id, TextSendMessage(text="?��? ?��??��?次填寫�??��??��??�已經�??��??��??��??��?，�??��?食�??��?美好?��?天�?"))
            except: pass
            return {"status": "already_claimed"}

        # 2. 從�??�箱?�出一張「�?沒被使用?��?點數網�?
        c.execute("SELECT link FROM reward_links WHERE is_used=0 LIMIT 1")
        row = c.fetchone()
        
        if row:
            reward_link = row[0]
            # 標�??�已使用，並記�??�個人已�??��?
            c.execute("UPDATE reward_links SET is_used=1 WHERE link=?", (reward_link,))
            c.execute("INSERT INTO survey_records (user_id, claim_date) VALUES (?, ?)", (user_id, tw_today().isoformat()))
            conn.commit()
            
            # 3. ?��?屬�??�網?�私�?給客�?
            push_msg = f"?? ?��??��?寶貴?��?！\n\n?�是答�??��?專屬?�勵，�?點�?下方????��??��??��?食�?點卡 1 點】�?�\n\n{reward_link}\n\n(?��? 注�?：此????��?屬�?次性�??，�??��??�失?��?請勿轉發給�?人�?�?"
            line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
        else:
            # 點數?��?了�??�知?��?�?
            c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
            admin_row = c.fetchone()
            if admin_row:
                line_bot_api.push_message(admin_row[0], TextSendMessage(text="?�� ?��?緊急通知：填?�卷?��??��??��??�網?�?�已經被?��??��?請盡快�?後台?��??��?網�?並用 #上傳點數 補貨�?))
        
        conn.close()
        return {"status": "success"}
    except Exception as e:
        print(f"?��? ?�卷?��??�誤: {e}")
        return {"status": "error"}
# ==========================================
# 5. AI 對話引�? (?�� ?��??��??��??��??�質?��?追蹤)
# ==========================================
def get_ai_response_with_memory(user_id, user_msg):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    
    # ?�� ?��?客人資�? (保�? active_days，並多�? protein)
    c.execute("SELECT summary_text, tdee, active_days, protein FROM health_profile WHERE user_id=?", (user_id,))
    hp = c.fetchone()
    
    today_str = tw_today().isoformat()
    # ?�� ?��?今日外�?紀??(多�? today_extra_pro)
    c.execute("SELECT today_extra_cal, today_date, sheet_name, name, today_extra_pro FROM health_profile WHERE user_id=?", (user_id,))
    daily_rec = c.fetchone()
    
    # ?�斷?��??�新?��?天�?如�??�就歸零
    if daily_rec and daily_rec[1] != today_str:
        c.execute("UPDATE health_profile SET today_extra_cal=0, today_extra_pro=0, today_date=? WHERE user_id=?", (today_str, user_id))
        extra_cal, extra_pro = 0, 0
    else:
        extra_cal = daily_rec[0] if daily_rec else 0
        extra_pro = daily_rec[4] if (daily_rec and len(daily_rec) > 4 and daily_rec[4] is not None) else 0

    report = f"\n?��?對�??�報?�內容�?\n{hp[0]}" if hp else "\n檔�??�填，�?引�?客人填表??
    tdee_val = hp[1] if hp else 2000
    active_days = hp[2] if hp else ""
    protein_val = hp[3] if hp else 100
    history = user_memory.get(user_id, [])[-6:]
    ingredients_memo = "\n".join([f"- {d['name']}: {d.get('ingredients', '?�鮮食�?')}" for d in MAIN_DISHES])
    
    # ?�� ?�能?��?：判?��?天是?��?幾�?以�?客人今天?��??��?餐�?
    weekdays = ["?��?", "?��?", "?��?", "?��?", "?��?", "?�六", "?�日"]
    today_str_zh = weekdays[tw_today().weekday()]
    
    if today_str_zh in active_days:
        today_status = f"??今天 ({today_str_zh}) ?�顧客�??��?餐日?��?系統已�??��??��?了本店便?��??��??��??�質??
        calc_formula = f"""
        2. ?�查?��??�報?�中，�??�天?�「�???�日?��??��??��??��??�質?�補」�?
           ?��?�?��?��?額�?= ?�當?�熱?�剩餘�?- {extra_cal} - ?��??�估算�??��??��?
           ?��?�???��?求�?= ?��??�質?�補�?- {extra_pro} - ?��??�估算�??�白質】�?
        3. ?�訴他�??�系統已?�您?��?了�??��?食便?�。扣?��?食�?，您今天?�剩�?OOO 大卡?��?度�?並�??��?要�???OOO ?��??�質?��???
        """
    else:
        today_status = f"??今天 ({today_str_zh}) ?�顧客�??�無?��??�】�?他�??��??��? TDEE 額度 ({tdee_val} kcal) ?��??�質?��? ({int(protein_val)} g)??
        calc_formula = f"""
        2. ?�為今天沒�??��?，�??�接?��?完整??TDEE ({tdee_val} kcal) ?��??�質?��? ({int(protein_val)} g) 來�?算�?
           ?��?�?��?��?額�?= {tdee_val} - {extra_cal} - ?��??�估算�??��??��?
           ?��?�???��?求�?= {int(protein_val)} - {extra_pro} - ?��??�估算�??�白質】�?
        3. ?�訴他�??��?天�??��??�本店�?點�?但扣?��?食�?，您?�總 TDEE ?�剩�?OOO 大卡，�??��??�質?��??�差 OOO ?��?請繼續�??��?！�?
        """

    system_prompt = f"""你是?��??��?食」�?專屬 AI ?��?師。�??��?位�?滿熱?�、幽默、�?專業?�健康顧?��?
    {report}
    
    ?�本店�?點內容物 - 機�?小�????��??�部?��?�?
    {ingredients_memo}
    
    ?��??外�?計�??�格規�? ?��??
    顧客今天?�「�?食累積熱?�」為：{extra_cal} 大卡??
    顧客今天?�「�?食累積�??�質?�為：{extra_pro} ?��?
    {today_status}
    
    ?�顧客�??��??��?了�?麼�?，�??�格?�照以�?步�??��?�?
    1. 估�?他�??��?外�??�熱?�」�??��??�質?��?
    {calc_formula}
    4. ?��??��?高�?令】�??��??�尾端，�?定�??��??��?標籤 [LOG_NUTRITION: ?��??��?, ?�白質數字]??(例�?：[LOG_NUTRITION: 450, 20])
    
    ?��???��??�高�?�??��??
    ?��?顧客?�確定�??�」�??��??��??��?點�?請在你整段�?覆�??�底部，直?��?�?[CHANGE_MEAL: 將OOO?��??�XXX]??
    ?��? 絕�?不�?輸出?�隱?��?籤」這�??��?，直?�輸?�中?��??�可�?
    """
    
    # ?�叫大腦
    try:
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_msg}]
        res = client.chat.completions.create(model="gpt-4o-mini", messages=messages, max_tokens=2000, temperature=0.3)
        ans = res.choices[0].message.content
    except Exception as e:
        # ??保�??��??��?貼�??�錯?�示
        return f"?��? ?�系統除?�報?�】呼??AI 大腦失�?！\n?��?：{str(e)}\n\n?? ?��?，這通常?��???Railway 後台??Variables 沒�?設�?�?OPENAI_API_KEY，�??�設定�?沒�??�新 Deploy (?�署) ?��?"
        
    # ?�� ?��?紀??(?��??��??��? + ?�白質�??��?)
    match = re.search(r'\[LOG_NUTRITION:\s*(\d+),\s*(\d+)\]', ans)
    if match:
        logged_cal = int(match.group(1))
        logged_pro = int(match.group(2))
        new_extra_cal = extra_cal + logged_cal
        new_extra_pro = extra_pro + logged_pro
        c.execute("UPDATE health_profile SET today_extra_cal=?, today_extra_pro=? WHERE user_id=?", (new_extra_cal, new_extra_pro, user_id))
        conn.commit()
        ans = re.sub(r'\[LOG_NUTRITION:\s*\d+,\s*\d+\]', '', ans).strip()
        
        # ??保�?寫入 Google Sheet，並?��??�白質數?��?
        if daily_rec and daily_rec[2] and gc:
            try:
                sheet = gc.open_by_url(SHEET_URL)
                now_str = tw_now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.worksheet(daily_rec[2]).append_row([now_str, "外�??��??��??��???, user_msg, f"+{logged_cal} kcal / +{logged_pro} g"])
            except Exception: pass

    # ???��??��??�知 (?�送給?��?) (完整保�?)
    match_change = re.search(r'\[CHANGE_MEAL:\s*(.+?)\]', ans)
    if match_change:
        change_req = match_change.group(1)
        ans = re.sub(r'\[CHANGE_MEAL:\s*.+?\]', '', ans).strip()
        ans = ans.replace('?��?標籤', '').replace('`', '').strip()
        
        c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
        admin_row = c.fetchone()
        if admin_row:
            customer_name = daily_rec[3] if daily_rec else "顧客"
            boss_msg = f"?��??��??��?餐通知?�\n顧客 {customer_name} 要�??��?：\n?? {change_req}\n\n請�??�注?��?餐�?"
            try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=boss_msg))
            except Exception: pass

    conn.close()
    # ?�新記憶
    user_memory[user_id] = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": ans}]
    return ans

# ==========================================
# 6. ?��?輔助?�數??Webhook (?�� ?��??��?完整保�?測�??�VIP?�能)
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
        return True, f"(?�{m}�?| 諮詢:{q-1})"


def send_tomorrow_reminders():
    tomorrow = tw_today() + datetime.timedelta(days=1)
    weekdays = ["?��?", "?��?", "?��?", "?��?", "?��?", "?�六", "?�日"]
    tomorrow_str = weekdays[tomorrow.weekday()]
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{tomorrow_str}%",))
    users = c.fetchall(); conn.close()
    count = 0
    for uid, name in users:
        msg = f"?? {name} ?��?！\n?�天 ({tomorrow_str}) ?�您?��?屬�?餐日?��?\n\n?�� ?��?師溫馨�??��?\n?�確保您?��?養�?標�??�天?�要幫?��?外�??�【�??��??��??��??�無糖�?漿】�?補足?�白質缺???？\n(?�接?��??�要�??��?，�??��?天就?�幫?��??�好�?"
        try: line_bot_api.push_message(uid, TextSendMessage(text=msg)); count += 1
        except Exception: pass
    return f"???��??�送�? {count} 封�??��?餐�??�推?��?"

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
    if not r: conn.close(); return None, "???��?"
    m, d, l = r; today = tw_today()
    c.execute("UPDATE vips SET is_used=1 WHERE code=?", (code,))
    c.execute("SELECT remaining_meals FROM usage WHERE user_id=?", (uid,))
    u = c.fetchone(); curr_m = u[0] if u else 0
    exp = (today + datetime.timedelta(days=d)).isoformat()
    c.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?)", (uid, l, curr_m+m, today.isoformat(), 'vip', exp, l))
    conn.commit(); conn.close()
    link = f"https://docs.google.com/forms/d/e/1FAIpQLSdVY7Zf-E2zSpsOFmItYHI0YtTujX6Ucux4QTQ3gjg5wcomgA/viewform?usp=pp_url&entry.1461831832={uid}"
    return exp, f"?? ?��??��?！\n?��?專屬?��?表單：\n{link}"

@app.post("/callback")
async def callback(request: Request):
    sig = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try: 
        handler.handle(body.decode("utf-8"), sig)
    except InvalidSignatureError: 
        print("?��? LINE 簽�??�誤！�?檢查 Railway ??LINE_CHANNEL_SECRET ?�否填錯?��?空格�?)
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e: 
        print(f"?��? LINE 訊息?��??��??��??�誤: {e}")
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg_id = event.message.id
    if msg_id in processed_messages: return 
    processed_messages.add(msg_id)
    if len(processed_messages) > 1000: processed_messages.clear()

    msg, uid = event.message.text.strip(), event.source.user_id
    
    # ?�� LINE ?��??�單?�截?�
    if msg == "填寫體質表單":
        form_link = f"https://docs.google.com/forms/d/e/1FAIpQLSdVY7Zf-E2zSpsOFmItYHI0YtTujX6Ucux4QTQ3gjg5wcomgA/viewform?usp=pp_url&entry.1461831832={uid}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"?? 請�??��??��?屬�??，填寫您?��?質�?估表?��?\n\n{form_link}\n\n(系統已為?�自?�帶??LINE 帳�?，�??�接填寫?�可?��?)"))
        return
    elif msg == "填寫滿�?度�???:
        # ?? ?��?注�?：�??��??�這串網�?，�??�您?��???Google 表單?��??�那串「�?後面??{uid} ?��??��???��?
        survey_link = f"https://docs.google.com/forms/d/e/1FAIpQLScF6Va_sdq6KMaKFd8BUVB2x5SyLji3JqX28-Z7h-tuLnpB-Q/viewform?usp=pp_url&entry.1048958109={uid}"
        
        reply_text = f"?? ?��??��?一?��?食�??��?！\n請�??��??��?屬�??填寫滿�?度調??(�??��?)?�\n\n完�?填寫後�?系統將自?�發?��? 點�?點卡點數?�給?��?！�?�\n\n{survey_link}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "?��??�單":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT summary_text FROM health_profile WHERE user_id=?", (uid,))
        hp = c.fetchone(); conn.close()
        reply_text = f"?���??�是?�您?�身?�造�?專屬?�單：\n\n{hp[0]}\n\n(?�想?��??�色?��?購單?��??�以?�接?��??�訴?��?�?" if hp and hp[0] else "?�好?��?沒填寫�?質�?估表?��?！�?點�??�單來建立�?屬�?案吧！�??
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "?��?紀?�飲�?:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="今天?��?什麼好?�呢？�?�\n\n?�可以直?��?字�?訴�?（�?如�??��??��?一?�大麥�??�中?��?，�??��??�幫?�估算熱?��?並�?紀?��??�您?�【�?�?VIP 檔�??�中?��??��"))
        return
    elif msg == "?�費?�麼�?:
        reply_text = "?�知?��?屬�??��?費�?？�?�\n\n請直?�在對話框輸?��?\n??測�? ?��?完整?��??�\n\n例�?：\n#測�? ?��?市信義�??��?�?0?�\n\n系統就�?立刻?�您?��??�能?�風車報?��?�?
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    elif msg == "?��??�員?�??:
        allow, q_msg = check_permission_and_quota(uid)
        if allow: reply_text = f"?? ?��? VIP ?�員?�?��?\n\n?�目?��??��?：\n{q_msg}\n\n請繼續�??�健康�?飲�?習慣?��?"
        else: reply_text = "?�目?��??��???VIP ?��?，�??�方案已?��??�\n請輸?�您??VIP ?�請碼 (例�? #VIP24-XXXXXX) 來解?��?�?AI ?��?師�?訂�??��?�?
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # ?? ?��?專屬?�令?� ??
    if msg == "#綁�??��?":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO admin_settings VALUES ('admin_id', ?)", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="???��?好�?系統已�??��?定。\n客人?�【�?餐通知?�都?��?訊給?��?"))
        return
    elif msg == "#點數庫�?":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        # 算�?下�??��???(is_used=0)
        c.execute("SELECT COUNT(*) FROM reward_links WHERE is_used=0")
        unused_count = c.fetchone()[0]
        # 算�?下已經發?�去??(is_used=1)
        c.execute("SELECT COUNT(*) FROM reward_links WHERE is_used=1")
        used_count = c.fetchone()[0]
        conn.close()
        
        reply_msg = f"?? ?�老�?專屬：�??�庫存報?�】\n\n?�� 尚未?�送�?{unused_count} 張\n?�� 已�??�出：{used_count} 張\n\n(歷史總共上傳??{unused_count + used_count} 張�??�網?�)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return
    elif msg == "#?�新?�單":
        reply_msg = load_menu()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return
    elif msg == "#今日?��?完�?":
        weekdays = ["?��?", "?��?", "?��?", "?��?", "?��?", "?�六", "?�日"]
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
                    notify_msg = f"?? {u_name} ?�好！您?��?屬方案只?��?�?{new_meals} 餐�?！\n?�可以直?��?覆�??��?要�?約」�?系統將為?�無縫�??��?一?��??��?"
                    try: 
                        line_bot_api.push_message(u_id, TextSendMessage(text=notify_msg)); notify_count += 1
                    except Exception: pass
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"???��??��?！�???({today_str}) ?��???��完畢！\n?�扣?��? {count} 份�?點�??��?{notify_count} ?��?約推?��?"))
        return
    
    # ?�裡?��???elif 必�?跟�??�其他�? elif 對�? (?�?��???
    elif msg.startswith("#上傳點數\n"):
        links = msg.replace("#上傳點數\n", "").strip().split('\n')
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        count = 0
        for link in links:
            if link.strip():
                try:
                    c.execute("INSERT INTO reward_links (link, is_used) VALUES (?, 0)", (link.strip(),))
                    count += 1
                except sqlite3.IntegrityError: pass # ?��??��?存入
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"???��??��?！�??��???{count} 筆全?��?點數網�?�?))
        return
        
    elif msg == "#?�送�??��???:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=send_tomorrow_reminders()))
        return
    elif msg == "#??4":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"?? 24餐�?請碼：\n{chr(10).join(generate_package_codes('24m', 3))}"))
        return
    elif msg == "#??8":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"?�� 48餐�?請碼：\n{chr(10).join(generate_package_codes('48m', 3))}"))
        return
    elif msg.startswith("#VIP"):
        expiry, res = redeem_code(uid, msg)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=res))
        return
    elif msg == "#清空?��?":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("UPDATE health_profile SET today_extra_cal=0, today_extra_pro=0 WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="?? ?��??��?，�??�偷?��??��??�熱?��??�白質�?已歸?��?"))
        return
    elif msg == "#?�除檔�?":
        if uid in user_memory: del user_memory[uid]
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("DELETE FROM health_profile WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="?�� ?��?好�?檔�??��??�已徹�??��?！�??�新填表�?))
        return
    elif msg == "#?�置":
        if uid in user_memory: del user_memory[uid]
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        # ?�� 強制寫入一筆無?��???0次�?度�?紀?��?就�?資�?庫被洗白也能?��?�?
        today = tw_today().isoformat()
        c.execute("INSERT OR REPLACE INTO usage (user_id, remaining_chat_quota, remaining_meals, last_date, status, expiry_date, daily_chat_limit) VALUES (?, 50, 99, ?, 'vip', '2099-12-31', 50)", (uid, today))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="?? ?��??��??��?！系統已強制?�您?��?VIP 檔�?並�?�?50 次�?度�??�在請�??�熱?��?"))
        return
        
    # ?���??�能測�??��?風�? ?���?
    elif msg.startswith("#測�? "):
        target_address = msg.replace("#測�? ", "").strip()
        success, dist_text, dist_meters, duration_text = get_distance(STORE_ADDRESS, target_address)
        if success:
            hub_match = None
            for hub in HUBS:
                h_succ, h_d_txt, h_d_m, h_t_txt = get_distance(hub["address"], target_address, mode="walking")
                if h_succ and h_d_m <= 1000: hub_match = hub["name"]; break 
            
            if hub_match: fee_msg = f"20 ???? (?��?！您符�??�{hub_match}?�周??1 ?��?專屬?�風車特?��?)"
            else:
                if dist_meters <= 2000: fee_msg = "0 ??(2?��??��??��?案�?)"
                elif dist_meters <= 4000: fee_msg = "40 ??
                elif dist_meters <= 6000: fee_msg = "80 ??
                else: fee_msg = "超出?�家車�?範�?，建議自?��???Lalamove 專�??�價?�送�?�?
            reply_text = f"?�� **一?��?�?外送試算�???*\n?? ?��??��?{target_address}\n?? 距本店�??��?{dist_text}\n?��? 騎�??��?：{duration_text}\n?�� ?�費評估：{fee_msg}"
        else: reply_text = "?��?！地?�系統暫?�找不到?�個地?�，�?確�??��??�否完整?��?"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # ?�� 顧客一?��?�?(串接 AI) ?��
    allow, q_msg = check_permission_and_quota(uid)
    if not allow: return
    else: line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{get_ai_response_with_memory(uid, msg)}\n\n{q_msg}"))
# ==========================================
# ?? ?�形店長專用?�數 (?��??��?程任??
# ==========================================
def auto_daily_meal_deduction():
    """每天?��???��今日餐�?，並?�送�?約通知"""
    weekdays = ["?��?", "?��?", "?��?", "?��?", "?��?", "?�六", "?�日"]
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
                notify_msg = f"?? {u_name} ?�好！您?��?屬方案只?��?�?{new_meals} 餐�?！\n?�可以直?��?覆�??��?要�?約」�?系統將為?�無縫�??��?一?��??��?"
                try: line_bot_api.push_message(u_id, TextSendMessage(text=notify_msg)); notify_count += 1
                except: pass
    conn.commit(); conn.close()
    
    # 任�?完�?，發?��?給老�?
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
    admin_row = c.fetchone()
    conn.close()
    if admin_row:
        try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=f"???�隱形�??�報?�】�???({today_str}) ?��???��?��?完畢！\n?�扣 {count} 份�?點�??��?{notify_count} ?��?約推?��?"))
        except: pass

def auto_send_tomorrow_reminders_to_boss():
    """每天?��??�送�??��??��?並�??��??�報"""
    result_msg = send_tomorrow_reminders() # ?�叫?�本寫好?�推?�函??
    
    # 任�?完�?，發?��?給老�?
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_id'")
    admin_row = c.fetchone()
    conn.close()
    if admin_row:
        try: line_bot_api.push_message(admin_row[0], TextSendMessage(text=f"???�隱形�??�報?�】�??��??�推?��??��?\n{result_msg}"))
        except: pass
