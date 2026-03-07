import requests
import os
import json
import sqlite3
import datetime
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

# Google 試算表設定 (網址公開安全，靠 service_account 保護)
SHEET_URL = "https://docs.google.com/spreadsheets/d/1cf0QhWeYynk9nqsoqMIM-Lkxk_bP57zcd-ES7Sufkqg/edit?gid=0#gid=0"

app = FastAPI()
client = OpenAI(api_key=OPENAI_API_KEY)
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
user_memory = {}
processed_messages = set()

# 喚醒 Google 虛擬助理
try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
    gc = gspread.authorize(creds)
    print("✅ Google 雲端虛擬助理已上線！")
except Exception as e:
    print(f"⚠️ Google 助理連線失敗，請檢查 service_account.json: {e}")
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
                    # 🔥 關鍵濾網：區分主餐與加購單品 🔥
                    main_keywords = ["便當", "麵", "食蔬", "低碳", "沙拉"]
                    if any(kw in name for kw in main_keywords):
                        category = "main"  # 這是正餐，會進入排餐抽籤池
                    else:
                        category = "side"  # 這是單品/飲料，排餐不抽，但 AI 可以推銷
                    MAIN_DISHES.append({"name": name, "cal": cal, "pro": pro, "price": price, "category": category, "ingredients": ingredients})
                except Exception: pass
        print(f"✅ 成功載入 {len(MAIN_DISHES)} 項餐點！")
        return f"✅ 菜單更新成功！共載入 {len(MAIN_DISHES)} 項餐點。"
    except Exception as e:
        return f"⚠️ 菜單更新失敗: {e}"

# 伺服器啟動時，先自動讀取一次
load_menu()

# ==========================================
# 3. 資料庫初始化 (🔥 融合版：包含老闆綁定與使用紀錄)
# ==========================================
def init_db():
    conn = sqlite3.connect('user_quota.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS usage (user_id TEXT PRIMARY KEY, remaining_chat_quota INTEGER, remaining_meals INTEGER, last_date TEXT, status TEXT, expiry_date TEXT, daily_chat_limit INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS vips (code TEXT PRIMARY KEY, meals INTEGER, duration_days INTEGER, chat_limit INTEGER, is_used INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS health_profile (user_id TEXT PRIMARY KEY, name TEXT, tdee INTEGER, protein REAL, goal TEXT, restrictions TEXT, summary_text TEXT, active_days TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    try:
        c.execute("ALTER TABLE health_profile ADD COLUMN today_extra_cal INTEGER DEFAULT 0")
        c.execute("ALTER TABLE health_profile ADD COLUMN today_date TEXT DEFAULT ''")
        c.execute("ALTER TABLE health_profile ADD COLUMN sheet_name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass 
        
    conn.commit(); conn.close()
init_db()

# ==========================================
# 4. 接收表單與配餐 (過敏原雷達 + 完美排序)
# ==========================================
@app.post("/form-data")
async def receive_form_data(request: Request):
    try:
        data = await request.json()
        def get_val(keyword):
            for k, v in data.items():
                if keyword in k and v: 
                    return ",".join([str(i) for i in v]) if isinstance(v, list) else str(v)
            return ""
        
        user_id = get_val("UID")
        if not user_id or user_id == "UID_REPLACE_ME": return {"status": "ignored"}
        if user_id in user_memory: del user_memory[user_id]

        name, goal, restrictions = get_val("稱呼"), get_val("目標"), get_val("禁忌")
        weight, height, age, gender = float(get_val("體重") or 70), float(get_val("身高") or 170), float(get_val("年齡") or 30), get_val("性別")
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
        
        for k, v in data.items():
            if "週" in k and isinstance(v, list) and v[0]:
                days = [d.strip() for d in v[0].split(',')]
                w_num = 99
                for zh, num in week_dict.items():
                    if zh in k: w_num = num; break
                    
                for d in days:
                    if not d: continue
                    d_num = 99
                    for zh, num in week_dict.items():
                        if zh in d: d_num = num; break
                    plan_requests.append((w_num, d_num, k, d))
                    
        plan_requests.sort(key=lambda x: (x[0], x[1]))
        
        for req in plan_requests:
            w_num, d_num, k, d = req
            active_days.add(d)
            l, dn = random.choice(lunch_pool), random.choice(dinner_pool)
            lp, dnp = l.get('price', 150), dn.get('price', 150)
            total_price += (lp + dnp)
            day_cal, day_pro = l['cal'] + dn['cal'], l['pro'] + dn['pro']
            rem_cal, rem_pro = int(tdee - day_cal), int(protein - day_pro)
            
            schedule_lines.append(f"【{k}-{d}】\n  ☀️午:{l['name']} ({l['cal']}kcal/{l['pro']}g/單價{lp}元)\n  🌙晚:{dn['name']} ({dn['cal']}kcal/{dn['pro']}g/單價{dnp}元)\n  👉 當日熱量剩餘:{rem_cal}kcal | 蛋白質需補:{rem_pro}g")
            
            schedule_sheet_rows.append([
                f"{k} - {d}", 
                f"{l['name']} ({l['cal']}kcal)", 
                f"{dn['name']} ({dn['cal']}kcal)", 
                f"剩餘 {rem_cal} kcal / 補 {rem_pro} g"
            ])

        schedule_text = chr(10).join(schedule_lines)
        summary = (f"顧客稱呼:{name}\n首要目標:{goal}\n每日目標: 熱量 {int(tdee)}kcal / 蛋白質 {int(protein)}g\n"
                   f"【總金額】: 預估主餐總價 {total_price} 元\n\n"
                   f"📋【您的排餐菜單與每日剩餘建議】\n{schedule_text}")
        
        safe_name = f"{name}_{user_id[-4:]}"

        conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO health_profile (user_id, name, tdee, protein, goal, restrictions, summary_text, active_days, today_extra_cal, today_date, sheet_name) VALUES (?,?,?,?,?,?,?,?,0,'',?)", (user_id, name, int(tdee), protein, goal, restrictions, summary, ",".join(list(active_days)), safe_name))
        conn.commit(); conn.close()

        if gc:
            try:
                sheet = gc.open_by_url(SHEET_URL)
                main_sheet = sheet.sheet1
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                row_data = [now_str, name, goal, int(tdee), int(protein), restrictions, total_price, ",".join(list(active_days)), schedule_text]
                main_sheet.append_row(row_data)
                
                try:
                    user_sheet = sheet.add_worksheet(title=safe_name, rows="1000", cols="8")
                    profile_data = [["【VIP 客戶檔案】", f"姓名: {name}", f"目標: {goal}", f"TDEE: {int(tdee)} kcal", f"蛋白質: {int(protein)} g", f"禁忌: {restrictions}"], [""]]
                    menu_title = [["【專屬排餐計畫 (第1週~第4週)】"]]
                    tracking_headers = [[""], ["================================================================="], ["【日常飲食與動態追蹤】"], ["紀錄時間", "紀錄類型", "客人傳送內容", "數值變化(kcal)"]]
                    user_sheet.append_rows(profile_data + menu_title + schedule_sheet_rows + tracking_headers)
                except Exception: pass
                print(f"📝 成功寫入總表，並為【{name}】建立含菜單的專屬分頁！")
            except Exception as e:
                print(f"⚠️ 寫入 Google 表單失敗: {e}")

        push_msg = f"🎉 {name} 填表成功！\nAI 營養師已為您精算：\n🔥 TDEE: {int(tdee)} kcal\n🥩 蛋白質: {int(protein)} g\n\n現在請點擊選單的『查看菜單』，我將為您列出每一天的詳細餐點與價格！"
        line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
        return {"status": "success"}
    except Exception as e: return {"status": "error", "msg": str(e)}

# ==========================================
# 5. AI 對話引擎 (🔥 融合版：換餐通知老闆 + 熱量精準追蹤)
# ==========================================
def get_ai_response_with_memory(user_id, user_msg):
    conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
    c.execute("SELECT summary_text, tdee FROM health_profile WHERE user_id=?", (user_id,))
    hp = c.fetchone()
    
    today_str = datetime.date.today().isoformat()
    c.execute("SELECT today_extra_cal, today_date, sheet_name, name FROM health_profile WHERE user_id=?", (user_id,))
    daily_rec = c.fetchone()
    
    if daily_rec and daily_rec[1] != today_str:
        c.execute("UPDATE health_profile SET today_extra_cal=0, today_date=? WHERE user_id=?", (today_str, user_id))
        extra_cal = 0
    else:
        extra_cal = daily_rec[0] if daily_rec else 0
    conn.commit()

    report = f"\n【絕對參考報告內容】:\n{hp[0]}" if hp else "\n檔案未填，請引導客人填表。"
    tdee_val = hp[1] if hp else 2000
    history = user_memory.get(user_id, [])[-6:]
    ingredients_memo = "\n".join([f"- {d['name']}: {d.get('ingredients', '新鮮食材')}" for d in MAIN_DISHES])
    
    system_prompt = f"""你是「一日樂食」的專屬 AI 營養師。你是一位充滿熱情、幽默、且專業的健康顧問！
    {report}
    
    【本店餐點內容物 - 機密小抄】(僅供內部參考)：
    {ingredients_memo}
    
    【🔥 外食熱量計算嚴格規則 🔥】
    顧客今天的「外食累積熱量」為：{extra_cal} 大卡。
    當顧客回報他剛吃了什麼時，請嚴格按照以下步驟回覆：
    1. 估算他剛吃的外食熱量。
    2. 先查看上方報告中，他每天的「👉 當日熱量剩餘」(這是幫他扣除便當後的數字)。
       【真正餘額】 = 【當日熱量剩餘】 - {extra_cal} - 【你剛估算的熱量】。
    3. 告訴他：「系統已經為您預留了一日樂食的便當熱量。扣除您今日紀錄的外食後，您今天還剩下 OOO 大卡的額度！」
    4. ⚠️【最高指令】：回覆最尾端，一定要加上隱藏標籤 `[LOG_CAL: 估算的熱量數字]`。
    
    【🚨 換餐最高指令 🚨】
    只要顧客「確定答應」要更換未來的餐點，請在你整段回覆的最底部，直接加上 [CHANGE_MEAL: 將OOO替換為XXX]。
    ⚠️ 絕對不要輸出「隱藏標籤」這四個字，直接輸出中括號即可！
    """
    
    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_msg}]
    res = client.chat.completions.create(model="gpt-4o-mini", messages=messages, max_tokens=2000, temperature=0.3)
    ans = res.choices[0].message.content
    
    # 處理熱量紀錄
    match = re.search(r'\[LOG_CAL:\s*(\d+)\]', ans)
    if match:
        logged_cal = int(match.group(1))
        new_extra = extra_cal + logged_cal
        c.execute("UPDATE health_profile SET today_extra_cal=? WHERE user_id=?", (new_extra, user_id))
        conn.commit()
        ans = re.sub(r'\[LOG_CAL:\s*\d+\]', '', ans).strip()
        
        if daily_rec and daily_rec[2] and gc:
            try:
                sheet = gc.open_by_url(SHEET_URL)
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.worksheet(daily_rec[2]).append_row([now_str, "外食熱量打卡", user_msg, f"+{logged_cal}"])
            except Exception: pass

    # 處理換餐通知 (發送給老闆)
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
    conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
    today = datetime.date.today().isoformat()
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
    # 計算明天是星期幾
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    tomorrow_str = weekdays[tomorrow.weekday()]
    
    # 去資料庫把明天有排餐的客人抓出來
    conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
    c.execute("SELECT user_id, name FROM health_profile WHERE active_days LIKE ?", (f"%{tomorrow_str}%",))
    users = c.fetchall(); conn.close()
    
    count = 0
    for uid, name in users:
        # 🔥 這裡可以偷偷塞入您的 Upsell 加購單品
        msg = f"🌙 {name} 晚安！\n明天 ({tomorrow_str}) 是您的專屬取餐日喔！\n\n💪 營養師溫馨提醒：\n為確保您的營養達標，明天需要幫您額外準備【洋芋泥】或【燕麥豆漿】來回補體力嗎？\n(直接回覆需要的品項，店長明天就會幫您準備好！)"
        try:
            line_bot_api.push_message(uid, TextSendMessage(text=msg))
            count += 1
        except Exception as e:
            pass
    return f"✅ 報告老闆：成功發送了 {count} 封【{tomorrow_str}】的明日取餐提醒推播！"
    conn.close(); return False, ""

def send_tomorrow_reminders():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    tomorrow_str = weekdays[tomorrow.weekday()]
    conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
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
    conn = sqlite3.connect('user_quota.db'); c = conn.cursor(); codes = []
    m, d, l, p = (24,31,20,"#VIP24-") if t=="24m" else (48,31,30,"#VIP48-")
    for _ in range(n):
        c_str = p + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        c.execute("INSERT INTO vips VALUES (?,?,?,?,0)", (c_str, m, d, l)); codes.append(c_str)
    conn.commit(); conn.close(); return codes

def redeem_code(uid, code):
    conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
    c.execute("SELECT meals, duration_days, chat_limit FROM vips WHERE code=? AND is_used=0", (code,))
    r = c.fetchone()
    if not r: conn.close(); return None, "❌ 無效"
    m, d, l = r; today = datetime.date.today()
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
    try: handler.handle(body.decode("utf-8"), sig)
    except InvalidSignatureError: raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception: pass
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
    elif msg == "查看菜單":
        conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
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
        conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO admin_settings VALUES ('admin_id', ?)", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 老闆好！系統已成功綁定。\n客人的【換餐通知】都會私訊給您！"))
        return
    elif msg == "#更新菜單":
        reply_msg = load_menu()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return
    elif msg == "#今日出餐完成":
        weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        today_str = weekdays[datetime.date.today().weekday()]
        conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
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
        conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
        c.execute("UPDATE health_profile SET today_extra_cal=0 WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🔄 報告老闆，今日偷吃紀錄已歸零！"))
        return
    elif msg == "#刪除檔案":
        if uid in user_memory: del user_memory[uid]
        conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
        c.execute("DELETE FROM health_profile WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="💥 老闆好，檔案與記憶已徹底銷毀！請重新填表！"))
        return
    elif msg == "#重置":
        if uid in user_memory: del user_memory[uid]
        conn = sqlite3.connect('user_quota.db'); c = conn.cursor()
        c.execute("UPDATE usage SET remaining_chat_quota=50 WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🔄 系統已重置！補滿 50 次額度。"))
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