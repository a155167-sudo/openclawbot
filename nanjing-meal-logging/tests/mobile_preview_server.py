"""Offline synthetic preview only: no database, LINE, deployment, or writes."""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from dietitian_health_check_liff import _html, _javascript

app = FastAPI()
CASE = "case/synthetic-a"
LIST = {"items": [
    {"case_id": CASE, "case_code": "HC-7F3A91", "status": "ready_for_review", "valid_day_count": 4, "record_count": 14, "collected_date_start": "2026-09-13", "collected_date_end": "2026-09-16", "window_started_at": "2026-09-13T00:00:00+08:00", "window_ends_at": "2026-09-20T00:00:00+08:00", "submitted_at": "2026-09-16T18:00:00+08:00", "profile": {"name": None, "goal": None}},
    {"case_id": "case/synthetic-b", "case_code": "HC-2B7C10", "status": "collecting", "valid_day_count": 2, "record_count": 4, "collected_date_start": "2026-09-16", "collected_date_end": "2026-09-17", "window_started_at": "2026-09-16T00:00:00+08:00", "window_ends_at": "2026-09-23T00:00:00+08:00", "submitted_at": None, "profile": {"name": None, "goal": None}},
    {"case_id": "case/synthetic-c", "case_code": "HC-91D4E8", "status": "ready_for_review", "valid_day_count": 3, "record_count": 5, "collected_date_start": "2026-09-14", "collected_date_end": "2026-09-16", "window_started_at": "2026-09-14T00:00:00+08:00", "window_ends_at": "2026-09-21T00:00:00+08:00", "submitted_at": "2026-09-16T20:00:00+08:00", "profile": {"name": None, "goal": None}},
]}
DAYS = [{"local_date": d, "rule_version": "draft-confirmed-meals-v1", "qualifying_meal_count": 2, "completeness_status": "qualified", "evaluated_at": d + "T22:00:00+08:00"} for d in ("2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16")]
DETAIL = {**LIST["items"][0], "valid_days": DAYS, "source_logs": [
    {"log_id": "synthetic-log-1", "food_log_version": 1, "local_date": "2026-09-13", "normalized_meal_slot": "lunch", "nutrition_snapshot": {"calories_kcal": 680, "protein_g": 35, "fat_g": 22, "carbohydrate_g": 78}},
    {"log_id": "synthetic-log-2", "food_log_version": 1, "local_date": "2026-09-14", "normalized_meal_slot": "dinner", "nutrition_snapshot": {"calories_kcal": 520, "protein_g": None, "fat_g": 18, "carbohydrate_g": 62}},
], "source_integrity": {"referenced_count": 2, "available_snapshot_count": 2, "all_snapshots_available": True}, "current_review_version": 0, "source_token": "a" * 64, "latest_review_fresh": None, "latest_review_available": False, "latest_review": None, "system_initial_draft": {"kind": "system_data_summary_v1", "display_label": "系統初稿（未儲存，待營養師審核）", "persistence": "not_saved", "customer_visible": False, "delivery_eligible": False, "requires_dietitian_review": True, "may_generate_preview": True, "source_binding": {"source_token": "a" * 64, "review_version": 0}, "coverage": {"observed_day_count": 4, "qualified_day_count": 4, "source_count": 2}, "nutrition": {"calories_kcal": {"observed_daily_average": 600, "days_with_value": 2, "unit": "kcal"}, "protein_g": {"observed_daily_average": 35, "days_with_value": 1, "unit": "g"}}, "targets": {"calories_kcal": None, "protein_g": None}, "observations": ["合成資料共 4 個符合完整度規則日期；畫面卡片示範 14 筆紀錄。"], "limitations": ["此為離線合成資料，不代表真實顧客或完整每日攝取。"], "editable_seed": {"good": "合成資料顯示已記錄 4 個符合完整度規則日期。", "priority": "資料限制與優先事項仍需由營養師確認。", "next_7_days": "由營養師依已驗證資料與顧客目標完成接下來 7 天內容。", "comment": ""}}, "supplement_request": None, "approval": None, "profile": {"name": None, "goal": None, "tdee": None, "protein": None, "active_days": None, "restrictions": None}}

@app.get("/dietitian-health-check")
def page():
    html = _html().replace("https://static.line-scdn.net/liff/edge/2/sdk.js", "/mock-liff.js").replace("<main>", "<main><p style='padding:8px 16px;background:#fbefd9'>離線合成資料｜非真實顧客、非正式環境</p>")
    return HTMLResponse(html)

@app.get("/mock-liff.js")
def mock_liff():
    return Response("window.liff={init:async()=>{},isLoggedIn:()=>true,getIDToken:()=>\"synthetic-token\",login:()=>{}};", media_type="application/javascript")

@app.get("/dietitian-health-check/app.js")
def script():
    return Response(_javascript("2009251085-dietitianCheck"), media_type="application/javascript")

@app.get("/api/dietitian/health-checks")
def listing():
    return JSONResponse(LIST)

@app.get("/api/dietitian/health-checks/{case_id:path}")
def detail(case_id: str):
    return JSONResponse({**DETAIL, "case_id": case_id})
