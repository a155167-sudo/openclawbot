"""Exact read-only functions/constants from v35/reference-android-sheet-printer.py.
Legacy compatibility fixture only. Not the current versioned Android consumer or physical E2E.
No credentials, provider initialization, print socket, or Sheet writes are included.
"""
PENDING_STATUS = "待列印"
PRINTED_STATUSES = ("已送印", "已列印")
ABSENT_MEALS = ("", "無", "無餐", "不供餐", "不出餐", "N/A", "NA", "—", "-")
COL_DATE = 0
COL_LUNCH = 2
COL_DINNER = 5
COL_PRINT_STATUS = 13
EXPECTED_HEADER_PREFIX = ("實際日期", "週期與星期", "午餐安排")
EXPECTED_HEADER_STATUS = "列印狀態"


def _norm(value):
    return str(value or "").strip()


def _is_absent(value):
    return _norm(value).upper() in ABSENT_MEALS


def _norm_date(value):
    return _norm(value).replace("/", "-")


def get_pending_orders():
    """讀南京店 workbook：實際日期=今天且列印狀態=待列印 的列。
    回傳格式與原本 Apps Script 版一致。"""
    today_iso = datetime.now(TAIPEI).date().isoformat()
    today_display = datetime.now(TAIPEI).strftime("%Y/%m/%d")
    workbook = _connect_workbook()

    orders = []
    lunch_summary = {}
    dinner_summary = {}

    for ws in workbook.worksheets():
        values = ws.get_all_values()

        # 找唯一一組 14 欄表頭（前 15 列內）
        header_index = -1
        for i, row in enumerate(values[:15]):
            row = [_norm(c) for c in row]
            if (len(row) > COL_PRINT_STATUS
                    and tuple(row[:3]) == EXPECTED_HEADER_PREFIX
                    and row[COL_PRINT_STATUS] == EXPECTED_HEADER_STATUS):
                header_index = i
                break
        if header_index < 0:
            continue

        customer_name = ws.title.split("_", 1)[0].strip() or "顧客"

        for zero_index, raw_row in enumerate(values[header_index + 1:], start=header_index + 1):
            row = [_norm(c) for c in raw_row] + [""] * (COL_PRINT_STATUS + 1 - len(raw_row))
            if not any(row):
                continue
            if _norm_date(row[COL_DATE]) != today_iso:
                continue
            status = row[COL_PRINT_STATUS]
            if status in PRINTED_STATUSES:
                continue  # 已印過，防重印
            if status != PENDING_STATUS:
                continue

            lunch = row[COL_LUNCH] or "無"
            dinner = row[COL_DINNER] or "無"

            orders.append({
                "customerName": customer_name,
                "date": today_display,
                "lunchItem": lunch,
                "dinnerItem": dinner,
                "sheetName": ws.title,
                "rowNumber": zero_index + 1,  # 1-indexed（Sheet 列號）
            })

            if not _is_absent(lunch):
                lunch_summary[lunch] = lunch_summary.get(lunch, 0) + 1
            if not _is_absent(dinner):
                dinner_summary[dinner] = dinner_summary.get(dinner, 0) + 1

    return {
        "ok": True,
        "today": today_display,
        "orders": orders,
        "lunchSummary": lunch_summary,
        "dinnerSummary": dinner_summary,
    }
