"""
一日樂食 今日總覽（餘額版）Flex Message 產生器
用法：build_dashboard_flex(data) -> dict，直接當作 LINE FlexMessage 的 contents。
data 結構見 SPEC.md 第 2 節。
"""

C_TEAL = "#1F4A47"      # 主文字
C_GREEN = "#2E7D6B"     # 已吃
C_YELLOW = "#F5C842"    # 包月預留 / 品牌黃
C_TRACK = "#ECE8DC"     # 進度條底
C_SUB = "#4D5F5C"       # 次要文字
C_OVER = "#D9772E"      # 超出（進度條）
C_OVER_TXT = "#B4531F"  # 超出（數字）
C_RES_TXT = "#8A6D12"   # 預留文字
C_BG = "#FFFDF6"
C_HEAD_TXT = "#5A4A1E"

MAX_ROWS = 6


def _kcal(n):
    return f"{int(round(n)):,}"


def _g(n):
    n = round(n, 1)
    return f"{int(n)}" if n == int(n) else f"{n}"


def _pct(x):
    x = max(0.0, min(1.0, x))
    return f"{round(x * 100, 1)}%"


def compute(data):
    """純計算，方便單元測試。"""
    tk = data.get("target_kcal") or 0
    tp = data.get("target_protein") or 0
    eaten = data.get("records", [])
    uneaten = [m for m in data.get("sub_meals", []) if not m.get("eaten")]
    reserved = [m for m in uneaten
                if m.get("kcal") is not None and m.get("protein") is not None]
    unknown_reserved = [m for m in uneaten if m not in reserved]

    def known_sum(rows, field):
        values = [row.get(field) for row in rows]
        if any(value is None for value in values):
            return None
        return sum(values)

    ek = known_sum(eaten, "kcal")
    ep = known_sum(eaten, "protein")
    rk = known_sum(reserved, "kcal")
    rp = known_sum(reserved, "protein")

    left_k = tk - ek - rk if ek is not None and rk is not None else None
    left_p = tp - ep - rp if ep is not None and rp is not None else None

    if tk <= 0:
        state = "no_target"      # 目標未設定：只顯示已吃，不算餘額
    elif left_k is None:
        state = "unknown"
    elif ek > tk:
        state = "over"
    elif left_k < 0:
        state = "sub_over"       # 還沒超，但包月預留加起來超過目標
    else:
        state = "normal"

    e_ratio = min(ek / tk, 1.0) if tk > 0 and ek is not None else 0
    r_ratio = min(rk / tk, 1.0 - e_ratio) if tk > 0 and rk is not None else 0
    ep_ratio = min(ep / tp, 1.0) if tp > 0 and ep is not None else 0
    rp_ratio = min(rp / tp, 1.0 - ep_ratio) if tp > 0 and rp is not None else 0

    # 蛋白質落後判斷（兩個目標都有值才判斷）
    protein_short = False
    if tk > 0 and tp > 0 and left_k is not None and left_p is not None and left_p >= 15:
        protein_short = (left_p / tp) - (max(left_k, 0) / tk) >= 0.10

    hint = ""
    if state == "no_target":
        hint = "還沒有設定每日目標，設定後就能看到熱量與蛋白質餘額。"
    elif state == "sub_over":
        # 包月超標提示優先；預留不是已吃，不可說「熱量已達標」
        hint = f"今天的包月餐合計比目標多 {_kcal(-left_k)} kcal，包月餐照常吃，其他時間盡量不加餐即可。"
        if protein_short:
            hint += f"吃完包月餐後蛋白質還差 {_g(left_p)} g，可以補一杯無糖豆漿。"
    elif state == "unknown":
        hint = "部分紀錄的營養資料未知，暫時無法計算精確餘額。"
    elif protein_short:
        if ek >= tk:
            hint = f"熱量已達標，蛋白質還差 {_g(left_p)} g。可以補一杯無糖豆漿或一顆茶葉蛋。"
        elif left_k <= 0:
            hint = f"吃完包月餐後熱量剛好達標，蛋白質還差 {_g(left_p)} g，可以補一杯無糖豆漿。"
        else:
            hint = f"蛋白質還差 {_g(left_p)} g，剩下的熱量建議優先選高蛋白、低熱量的食物，例如雞胸、豆腐、無糖豆漿。"

    return dict(tk=tk, tp=tp, ek=ek, ep=ep, rk=rk, rp=rp, left_k=left_k, left_p=left_p,
                state=state, e_ratio=e_ratio, r_ratio=r_ratio, ep_ratio=ep_ratio,
                rp_ratio=rp_ratio, has_sub=bool(reserved),
                has_unknown_sub=bool(unknown_reserved),
                show_hint=bool(hint), hint=hint)


def _dot(color, border=None):
    d = {"type": "box", "layout": "vertical", "contents": [], "width": "6px", "height": "6px",
         "cornerRadius": "5px", "backgroundColor": color, "flex": 0}
    if border:
        d["borderColor"] = border
        d["borderWidth"] = "1px"
    return d


def _legend(color, text, border=None):
    # Budget for a narrow bubble; use SDK-v2/replay-safe pixel sizes for long values.
    units = sum(2 if ord(char) > 127 else 1 for char in text)
    size = "xxs" if units <= 10 else f"{max(1, 104 // units)}px"
    return {"type": "box", "layout": "horizontal", "flex": 1, "spacing": "xs",
            "alignItems": "center", "contents": [
                _dot(color, border),
                {"type": "text", "text": text, "size": size, "color": "#3E504D", "flex": 1,
                 "wrap": False, "maxLines": 1}]}


def _bar(e_ratio, r_ratio, eat_color, height):
    segs = []
    if e_ratio > 0:
        segs.append({"type": "box", "layout": "vertical", "contents": [],
                     "width": _pct(e_ratio), "backgroundColor": eat_color})
    if r_ratio > 0:
        segs.append({"type": "box", "layout": "vertical", "contents": [],
                     "width": _pct(r_ratio), "backgroundColor": C_YELLOW})
    return {"type": "box", "layout": "horizontal", "contents": segs, "height": height,
            "cornerRadius": "7px", "backgroundColor": C_TRACK, "margin": "md"}


def _btn(label, color, style):
    actions = {
        "記一餐": "我要紀錄飲食",
        "今日明細": "我要修改飲食紀錄",
        "一週趨勢": "一週趨勢",
        "功能選單": "功能選單",
    }
    return {"type": "button", "style": style, "color": color, "height": "md", "flex": 1,
            "action": {"type": "message", "label": label, "text": actions[label]}}


def build_dashboard_flex(data):
    c = compute(data)
    over = c["state"] == "over"
    eat_color = C_OVER if over else C_GREEN

    # 熱量主數字
    no_target = c["state"] == "no_target"
    if no_target:
        k_label = "今日已吃"
        k_big = _kcal(c["ek"]) if c["ek"] is not None else "未知"
        k_color = C_TEAL
        left_txt = "目標未設定"
    elif over:
        k_label, k_big, k_color = "已超出", _kcal(c["ek"] - c["tk"]), C_OVER_TXT
        left_txt = "餘額 0"
    elif c["state"] == "unknown":
        k_label, k_big, k_color = "熱量餘額", "未知", C_TEAL
        left_txt = "可用 未知"
    else:
        k_label, k_big, k_color = "熱量餘額", _kcal(max(c["left_k"], 0)), C_TEAL
        left_txt = f"可用 {_kcal(max(c['left_k'], 0))}"

    legends = [_legend(eat_color, f"已吃 {_kcal(c['ek']) if c['ek'] is not None else '未知'}")]
    if c["has_sub"]:
        legends.append(_legend(C_YELLOW, f"預留 {_kcal(c['rk']) if c['rk'] is not None else '未知'}"))
    legends.append(_legend(C_TRACK, left_txt, border="#C9C3B2"))

    body = [
        {"type": "box", "layout": "horizontal", "alignItems": "flex-end", "contents": [
            {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": k_label, "size": "sm", "weight": "bold", "color": C_SUB},
                {"type": "box", "layout": "baseline", "spacing": "xs", "contents": [
                    {"type": "text", "text": k_big, "size": "3xl", "weight": "bold", "color": k_color, "flex": 0},
                    {"type": "text", "text": "kcal", "size": "sm", "weight": "bold", "color": k_color, "flex": 0}]}]},
            {"type": "text", "text": "目標未設定" if no_target else f"目標 {_kcal(c['tk'])}", "size": "xs", "color": C_SUB,
             "align": "end", "gravity": "bottom"}]},
        _bar(c["e_ratio"], c["r_ratio"], eat_color, "14px"),
        {"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "md", "contents": legends},

        {"type": "box", "layout": "baseline", "margin": "xl", "contents": [
            {"type": "text", "text": "蛋白質餘額" if c["tp"] > 0 else "今日蛋白質", "size": "sm",
             "weight": "bold", "color": C_SUB},
            {"type": "text", "text": f"{_g(max(c['left_p'], 0)) if c['tp'] > 0 and c['left_p'] is not None else (_g(c['ep']) if c['tp'] <= 0 and c['ep'] is not None else '未知')} g",
             "size": "lg", "weight": "bold", "color": C_TEAL, "align": "end", "flex": 0},
            {"type": "text", "text": f"/ {_g(c['tp'])} g" if c["tp"] > 0 else "目標未設定", "size": "xs",
             "color": C_SUB, "flex": 0, "margin": "sm"}]},
        _bar(c["ep_ratio"], c["rp_ratio"], C_GREEN, "10px"),
    ]

    if c["show_hint"]:
        body.append({"type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "12px",
                     "cornerRadius": "12px", "backgroundColor": "#EEF6E2", "contents": [
                         {"type": "text", "text": c["hint"], "size": "sm", "wrap": True, "color": "#2F4A22"}]})

    # 今日紀錄
    rows = []
    for r in data.get("records", []):
        rows.append((r.get("is_sub", False), f"{r['slot']}｜{r['name']}", f"{_kcal(r['kcal']) if r.get('kcal') is not None else '未知'} kcal", "#3E504D"))
    for m in data.get("sub_meals", []):
        if not m.get("eaten"):
            nutrition = (f"預留 {_kcal(m['kcal'])}"
                         if m.get("kcal") is not None and m.get("protein") is not None
                         else "營養待補")
            rows.append((True, f"{m['slot']}｜{m['name']}", nutrition, C_RES_TXT))
    extra = len(rows) - MAX_ROWS
    rows = rows[:MAX_ROWS]

    record_box = [{"type": "text", "text": "今日紀錄", "size": "sm", "weight": "bold", "color": C_TEAL}]
    if not rows:
        record_box.append({"type": "text", "text": "今天還沒有紀錄", "size": "sm", "color": C_SUB, "margin": "sm"})
    for is_sub, name, kcal, color in rows:
        left = []
        if is_sub:
            left.append({"type": "box", "layout": "vertical", "flex": 0, "paddingStart": "6px",
                         "paddingEnd": "6px", "paddingTop": "1px", "paddingBottom": "1px",
                         "cornerRadius": "8px", "backgroundColor": "#FCEBB0", "justifyContent": "center",
                         "contents": [{"type": "text", "text": "包月", "size": "xxs", "weight": "bold",
                                       "color": C_HEAD_TXT}]})
        left.append({"type": "text", "text": name, "size": "sm", "color": C_TEAL, "wrap": True})
        record_box.append({"type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm",
                           "alignItems": "center", "contents": [
                               {"type": "box", "layout": "horizontal", "spacing": "xs", "alignItems": "center",
                                "contents": left},
                               {"type": "text", "text": kcal, "size": "sm", "color": color, "align": "end",
                                "flex": 0}]})
    if extra > 0:
        record_box.append({"type": "text", "text": f"還有 {extra} 筆，請看今日明細", "size": "xs",
                           "color": C_SUB, "margin": "sm"})

    body.append({"type": "separator", "margin": "xl", "color": "#E2DBC6"})
    body.append({"type": "box", "layout": "vertical", "margin": "lg", "contents": record_box})

    if any(r.get("ai_estimated") for r in data.get("records", [])):
        body.append({"type": "text", "text": "含 AI 估算紀錄，非營養師審核結果", "size": "xxs",
                     "color": "#6B5A2A", "margin": "lg"})

    if data.get("subscription_source_unavailable"):
        body.append({"type": "text", "text": "包月出單資料暫時無法核對，未計入預留，餘額可能偏高。",
                     "size": "xxs", "color": C_SUB, "margin": "lg", "wrap": True})

    if c["has_unknown_sub"]:
        body.append({"type": "text", "text": "部分包月餐營養未提供，餘額可能偏高", "size": "xxs",
                     "color": C_SUB, "margin": "lg", "wrap": True})

    bubble = {
        "type": "bubble", "size": "mega",
        "styles": {"header": {"backgroundColor": C_YELLOW},
                   "body": {"backgroundColor": C_BG},
                   "footer": {"backgroundColor": C_BG}},
        "header": {"type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "xs", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": "一日樂食 · 今天", "size": "xs", "weight": "bold", "color": C_HEAD_TXT},
                {"type": "text", "text": data["date_label"], "size": "xs", "color": C_HEAD_TXT, "align": "end"}]},
            {"type": "text", "text": f"{data['user_name']}，今天吃得如何？", "size": "xl",
             "weight": "bold", "color": C_TEAL, "wrap": True}]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px", "contents": [
            {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                _btn("記一餐", C_GREEN, "primary"), _btn("今日明細", "#F2EEE2", "secondary")]},
            {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                _btn("一週趨勢", "#F2EEE2", "secondary"),
                _btn("功能選單", C_YELLOW, "secondary")]}]},
    }
    # LINE must retain full values on narrow clients and unusually large totals.
    def wrap_text(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                node.setdefault("wrap", True)
                if node["wrap"] and sum(ch.isdigit() for ch in node.get("text", "")) >= 5:
                    node["size"] = "sm"
            for value in node.values():
                wrap_text(value)
        elif isinstance(node, list):
            for value in node:
                wrap_text(value)
    wrap_text(bubble)
    return bubble


def build_message(data):
    """包成完整的 flex message（含 altText）。"""
    c = compute(data)
    if c["state"] == "no_target":
        alt = f"今日總覽：今天已吃 {_kcal(c['ek']) if c['ek'] is not None else '未知'} kcal"
    elif c["state"] == "over":
        alt = f"今日總覽：已超出 {_kcal(c['ek'] - c['tk'])} kcal"
    elif c["state"] == "unknown":
        alt = "今日總覽：部分營養資料未知，暫時無法計算精確餘額"
    else:
        alt = f"今日總覽：熱量餘額 {_kcal(max(c['left_k'], 0))} kcal"
        if c["tp"] > 0:
            alt += f"、蛋白質餘額 {_g(max(c['left_p'], 0)) if c['left_p'] is not None else '未知'} g"
    return {"type": "flex", "altText": alt, "contents": build_dashboard_flex(data)}
