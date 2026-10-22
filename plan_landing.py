"""包月介紹頁（deliexpress-ai.com / /plan）。

一頁式介紹，所有報名動作都導回 LINE（估價、填表、付款都在 LINE 完成）。
首批剩餘名額在每次請求時從 SQLite 讀，不快取客人資料；/api/first-batch/seats 只回數字。
頁面上的 LINE 畫面是示意，用字對齊 dashboard_flex.py（今日總覽）與
dietitian_health_check_delivery.py（健檢報告欄位）；那邊改字時這裡要跟著改。
"""
from __future__ import annotations

import html
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import first_batch_trial

LINE_OA_ID = "@a25080337"
LINE_START_TEXT = "包月方案"
ASSET_DIR = Path(__file__).with_name("static") / "plan"
ASSETS = {"salmon.jpg", "fish.jpg", "pork.jpg", "chicken.jpg", "couscous.jpg"}

# 菜色名稱待 Jason 確認（沿用 menu.csv 的命名方式）
DISHES = (
    ("salmon.jpg", "鮭魚便當"),
    ("couscous.jpg", "北非小米沙拉（本週特餐）"),
    ("pork.jpg", "豬肉沙拉"),
    ("fish.jpg", "鱸魚食蔬"),
    ("chicken.jpg", "雞肉食蔬"),
)


def line_start_url() -> str:
    return f"https://line.me/R/oaMessage/{quote(LINE_OA_ID)}/?{quote(LINE_START_TEXT)}"


def line_add_friend_url() -> str:
    return f"https://line.me/R/ti/p/{quote(LINE_OA_ID)}"


def seat_status(db_path: str) -> dict:
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            return first_batch_trial.public_status(conn)
    except sqlite3.Error:
        return {"enabled": False, "limit": 0, "taken": 0, "left": 0}


def _money(n: int) -> str:
    return f"${n:,}"


def _seat_block(status: dict) -> str:
    if not status.get("enabled"):
        return ""
    limit, taken, left = status["limit"], status["taken"], status["left"]
    dots = "".join(
        f'<span class="dot{" taken" if i < taken else ""}"></span>' for i in range(limit)
    )
    if left > 0:
        line = f"還剩 <b>{left}</b> 名，首期餐費 9 折"
    else:
        line = f"首批 {limit} 名已額滿，謝謝大家。現在報名是一般價格。"
    return (
        '<div class="seats" aria-label="首批名額">'
        f'<p class="seats-title">首批試跑　限量 {limit} 名</p>'
        f'<div class="dots" aria-hidden="true">{dots}</div>'
        f'<p class="seats-line">{line}</p>'
        "</div>"
    )


CSS = """
/* 品牌黃開場、白底內文；LINE 畫面用 dashboard_flex 的實際配色，讓客人加入後看到的一樣 */
:root {
  --yolk: #F0C457;      /* 與鮭魚照片背景同色，讓盤子浮在頁面上 */
  --brand: #F5C200;
  --char: #2B2622;
  --ink: #4A423B;
  --paper: #FFFFFF;
  --butter: #FFF6D9;
  --leaf: #3E7D4F;
  --line: #06C755;
  --chat: #8FA6C4;      /* LINE 聊天背景 */
  --card: #FFFDF6;      /* 今日總覽卡底色 */
  --teal: #1F4A47;
  --green: #2E7D6B;
  --track: #ECE8DC;
  --res: #8A6D12;
  --pill: #FCEBB0;
  --report: #0F766E;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink);
  font: 400 17px/1.75 "Noto Sans TC", system-ui, sans-serif; }
img { max-width: 100%; display: block; }
a { color: inherit; }
.wrap { max-width: 1040px; margin: 0 auto;
  padding-left: max(20px, env(safe-area-inset-left, 0px)); padding-right: max(20px, env(safe-area-inset-right, 0px)); }
h1, h2, h3 { color: var(--char); font-weight: 900; margin: 0; letter-spacing: .01em; text-wrap: balance; }
h2 { font-size: clamp(26px, 6.4vw, 38px); line-height: 1.3; }
h3 { font-size: 19px; line-height: 1.4; }
p { margin: 0; }
section { padding-block: 64px; }
.lead { margin-top: 10px; max-width: 34em; }
.tag { display: inline-block; font-size: 12px; font-weight: 700; color: var(--char); background: var(--butter);
  border: 1.5px solid var(--char); border-radius: 6px; padding: 0 6px; line-height: 1.6; }

/* hero */
.hero { background: var(--yolk); padding-top: 28px; overflow: hidden; }
.brand { font-weight: 900; color: var(--char); font-size: 17px; display: flex; gap: 8px; align-items: baseline; }
.brand small { font: 400 18px "Bebas Neue", sans-serif; letter-spacing: .06em; color: var(--char); opacity: .7; }
.hero-grid { display: grid; gap: 8px; padding-top: 28px; }
.hero h1 { font-size: clamp(38px, 11vw, 64px); line-height: 1.12; letter-spacing: -.01em; }
.hero .lede { font-size: 18px; color: var(--char); max-width: 30em; margin: 18px 0 22px; }
.plate { margin: 8px -20px 0; }
.plate img { width: 100%; height: auto; }

.seats { background: var(--paper); border-radius: 18px; padding: 18px 18px 14px; max-width: 420px; margin-bottom: 18px;
  box-shadow: 0 0 0 3px var(--char); }
.seats-title { font-weight: 900; color: var(--char); margin: 0 0 10px; font-size: 18px; }
.dots { display: grid; grid-template-columns: repeat(15, 1fr); gap: 6px; max-width: 360px; }
.dot { aspect-ratio: 1; border-radius: 50%; background: var(--yolk); }
.dot.taken { background: var(--char); }
.seats-line { margin: 12px 0 0; color: var(--char); }
.seats-line b { font: 400 34px/1 "Bebas Neue", sans-serif; vertical-align: -3px; margin: 0 2px; }

.cta { display: inline-flex; align-items: center; justify-content: center; gap: 10px; min-height: 56px; padding: 0 30px;
  border-radius: 999px; background: var(--char); color: var(--paper); text-decoration: none; font-weight: 700; font-size: 18px; }
.cta:focus-visible, a:focus-visible, summary:focus-visible { outline: 3px solid var(--line); outline-offset: 3px; }
.cta .line-badge { background: var(--line); color: var(--paper); font-size: 12px; font-weight: 900; border-radius: 6px; padding: 1px 6px; }
.cta-hint { font-size: 14px; color: var(--char); opacity: .8; margin-top: 10px; }

/* dishes：開場後馬上看到吃什麼 */
.dishes { padding-block: 40px 56px; }
.dishes h2 { margin-bottom: 18px; }
.strip { display: grid; grid-auto-flow: column; grid-auto-columns: 74%; gap: 14px; overflow-x: auto;
  scroll-snap-type: x mandatory; padding: 0 20px 10px; scroll-padding-inline: 20px; scrollbar-width: none; }
.strip::-webkit-scrollbar { display: none; }
.strip figure { margin: 0; scroll-snap-align: start; }
.strip img { border-radius: 20px; aspect-ratio: 4 / 3; object-fit: cover; width: 100%; height: auto; }
.strip figcaption { font-weight: 700; color: var(--char); margin-top: 8px; }

/* fit */
.fit { background: var(--butter); }
.fit ul { list-style: none; padding: 0; margin: 22px 0 0; display: grid; gap: 14px; }
.fit li { display: grid; grid-template-columns: 34px 1fr; gap: 12px; align-items: start; font-size: 19px; color: var(--char); font-weight: 500; }
.box { width: 30px; height: 30px; border: 3px solid var(--char); border-radius: 8px; position: relative; margin-top: 2px; background: var(--paper); }
.box::after { content: ""; position: absolute; left: 7px; top: 1px; width: 9px; height: 16px;
  border: solid var(--leaf); border-width: 0 4px 4px 0; transform: rotate(40deg); }

/* 記錄飲食：兩個 LINE 畫面 */
.log-grid { display: grid; gap: 34px; margin-top: 28px; }
.scene { display: grid; gap: 14px; align-content: start; }
.scene h3 { font-size: 20px; }
.phone { background: var(--chat); border: 6px solid var(--char); border-radius: 30px; padding: 18px 12px; max-width: 380px; width: 100%; }
.bubble { background: var(--paper); border-radius: 18px; padding: 10px 14px; font-size: 15px; line-height: 1.5; color: var(--char); max-width: 85%; }
.bubble.me { background: #A7E37F; margin-left: auto; }
.chat { display: grid; gap: 10px; }

.dash { background: var(--card); border-radius: 16px; overflow: hidden; font-size: 14px; line-height: 1.45; color: var(--teal); }
.dash-head { background: #F5C842; padding: 12px 14px; }
.dash-head small { display: flex; justify-content: space-between; font-size: 11px; font-weight: 700; color: #5A4A1E; }
.dash-head b { display: block; font-size: 18px; margin-top: 2px; }
.dash-body { padding: 14px; display: grid; gap: 8px; }
.kcal-row { display: flex; justify-content: space-between; align-items: flex-end; }
.kcal-row span { font-size: 12px; font-weight: 700; color: #4D5F5C; display: block; }
.kcal-row strong { font-size: 28px; line-height: 1.1; }
.kcal-row strong em { font-style: normal; font-size: 13px; margin-left: 3px; }
.kcal-row small { font-size: 11px; color: #4D5F5C; }
.bar { display: flex; height: 12px; border-radius: 99px; background: var(--track); overflow: hidden; }
.bar.thin { height: 8px; }
.bar i { display: block; height: 100%; }
.bar .eat { background: var(--green); }
.bar .rsv { background: #F5C842; }
.legend { display: flex; gap: 10px; font-size: 11px; color: #4D5F5C; flex-wrap: wrap; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 4px; vertical-align: 0; }
.p-row { display: flex; justify-content: space-between; align-items: baseline; margin-top: 6px; font-size: 13px; font-weight: 700; color: #4D5F5C; }
.p-row b { color: var(--teal); font-size: 16px; }
.rows { border-top: 1px solid #E2DBC6; margin-top: 6px; padding-top: 10px; display: grid; gap: 6px; }
.rows > b { font-size: 13px; }
.row { display: flex; justify-content: space-between; gap: 8px; align-items: center; font-size: 13px; }
.row .pill { background: var(--pill); color: #5A4A1E; font-size: 10px; font-weight: 700; border-radius: 8px; padding: 0 6px; margin-right: 4px; }
.row .r { color: var(--res); white-space: nowrap; }
.row .k { white-space: nowrap; }
.act { text-align: right; font-size: 12px; color: var(--green); text-decoration: underline; }
.ai-note { font-size: 10px; color: #6B5A2A; }

.est { background: var(--card); border-radius: 16px; padding: 12px 14px; font-size: 14px; color: var(--teal); max-width: 90%; display: grid; gap: 4px; }
.est b { font-size: 16px; }
.est .btn { margin-top: 6px; background: var(--green); color: var(--paper); border-radius: 10px; text-align: center; padding: 6px; font-weight: 700; }

/* 營養師 */
.rd-grid { display: grid; gap: 34px; margin-top: 28px; }
.timeline { list-style: none; margin: 0; padding: 0; display: grid; gap: 0; }
.timeline li { display: grid; grid-template-columns: 44px 1fr; gap: 14px; position: relative; padding-bottom: 22px; }
.timeline li:last-child { padding-bottom: 0; }
.timeline li::before { content: ""; position: absolute; left: 21px; top: 40px; bottom: 0; width: 2px; background: var(--char); opacity: .25; }
.timeline li:last-child::before { display: none; }
.timeline .n { width: 44px; height: 44px; border-radius: 50%; background: var(--brand); border: 2.5px solid var(--char);
  display: grid; place-items: center; font: 400 26px/1 "Bebas Neue", sans-serif; color: var(--char); }
.timeline .rd .n { background: var(--char); color: var(--brand); }
.timeline h3 { font-size: 18px; margin-top: 8px; }
.timeline p { font-size: 15px; margin-top: 2px; }

.report { background: var(--paper); border: 3px solid var(--char); border-radius: 20px; padding: 20px; display: grid; gap: 14px; max-width: 440px; align-self: start; }
.report-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.report-head b { color: var(--char); font-size: 17px; }
.report dt { color: var(--report); font-weight: 700; font-size: 14px; }
.report dd { margin: 2px 0 0; font-size: 15px; line-height: 1.6; color: #1F2937; }
.report dl { margin: 0; display: grid; gap: 12px; }
.fine { font-size: 14px; margin-top: 18px; max-width: 40em; }

/* steps：真的是先後順序 */
.steps { background: var(--paper); }
.steps ol { list-style: none; margin: 24px 0 0; padding: 0; display: grid; gap: 14px; counter-reset: s; }
.steps li { display: grid; grid-template-columns: 40px 1fr; gap: 12px; align-items: baseline; font-size: 18px; color: var(--char); }
.steps li::before { counter-increment: s; content: counter(s); font: 400 40px/0.9 "Bebas Neue", sans-serif; color: var(--brand);
  -webkit-text-stroke: 1.5px var(--char); }
.steps li b { font-weight: 700; }
.steps li span { color: var(--ink); font-size: 16px; display: block; }

/* price & delivery */
table { width: 100%; border-collapse: collapse; font-size: 16px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 12px 8px; border-bottom: 2px solid var(--butter); }
td:first-child, th:first-child { padding-left: 0; }
td:last-child, th:last-child { padding-right: 0; text-align: right; }
th { color: var(--char); font-weight: 700; font-size: 14px; }
.note { font-size: 15px; margin-top: 14px; }
.note.strong { color: var(--char); font-weight: 700; }
.pickup { display: grid; gap: 12px; margin-top: 24px; }
.pickup div { border: 2.5px solid var(--char); border-radius: 16px; padding: 14px 16px; }
.pickup h3 { font-size: 18px; }
.pickup p { font-size: 15px; margin-top: 4px; }
.fees { margin-top: 16px; }
.fees summary { cursor: pointer; font-weight: 700; color: var(--char); }
.fees table { margin-top: 8px; }

/* faq */
.faq-list { margin-top: 18px; }
details.q { border-bottom: 2px solid var(--butter); padding-block: 16px; }
details.q summary { cursor: pointer; font-weight: 700; color: var(--char); font-size: 18px; list-style: none; display: flex; justify-content: space-between; gap: 12px; }
details.q summary::-webkit-details-marker { display: none; }
details.q summary::after { content: "+"; font: 400 28px/1 "Bebas Neue", sans-serif; }
details.q[open] summary::after { content: "−"; }
details.q p { margin-top: 10px; }

.closing { background: var(--yolk); text-align: center; }
.closing h2 { margin-bottom: 12px; }
.closing p { color: var(--char); margin-bottom: 22px; }
footer { background: var(--char); color: #E9E2D8; padding-block: 28px 40px; font-size: 14px; }
footer a { color: var(--paper); }

@media (min-width: 820px) {
  body { font-size: 18px; }
  .hero-grid { grid-template-columns: 1.05fr 1fr; align-items: center; padding-bottom: 40px; }
  .plate { margin: 0 -60px 0 0; }
  .strip { grid-auto-columns: 30%; padding: 0 max(20px, calc((100vw - 1000px) / 2)); scroll-padding-inline: max(20px, calc((100vw - 1000px) / 2)); }
  .log-grid, .rd-grid { grid-template-columns: 1fr 1fr; column-gap: 56px; }
  .steps ol { grid-template-columns: 1fr 1fr; column-gap: 48px; }
  .pickup { grid-template-columns: repeat(3, 1fr); }
  .two { display: grid; grid-template-columns: 1fr 1.4fr; gap: 48px; }
}
@media (prefers-reduced-motion: no-preference) {
  .dot.taken { animation: fill .5s ease-out both; }
  @keyframes fill { from { transform: scale(.4); background: var(--yolk); } }
}
"""


# 今日總覽示意：數字照 menu.csv（雞肉便當 605 kcal／43.2 g、鱸魚食蔬 466 kcal／31.5 g），
# 目標 1,800 kcal／110 g；早餐外食 380 kcal／15 g。餘額 = 目標 − 已吃 − 預留。
_DASH_MOCK = """
<div class="dash" role="img" aria-label="今日總覽畫面示意：熱量餘額 349 kcal，蛋白質餘額 20.3 g，午餐吃了嗎">
  <div class="dash-head"><small><span>一日樂食 · 今天</span><span>10/27（二）</span></small><b>小美，今天吃得如何？</b></div>
  <div class="dash-body">
    <div class="kcal-row"><div><span>熱量餘額</span><strong>349<em>kcal</em></strong></div><small>目標 1,800</small></div>
    <div class="bar"><i class="eat" style="width:21%"></i><i class="rsv" style="width:59.4%"></i></div>
    <div class="legend"><span><i style="background:var(--green)"></i>已吃 380</span><span><i style="background:#F5C842"></i>預留 1,071</span><span><i style="background:var(--track);border:1px solid #C9C3B2"></i>可用 349</span></div>
    <div class="p-row"><span>蛋白質餘額</span><span><b>20.3 g</b> / 110.0 g</span></div>
    <div class="bar thin"><i class="eat" style="width:13.6%"></i><i class="rsv" style="width:67.9%"></i></div>
    <div class="rows"><b>今日紀錄</b>
      <div class="row"><span>早餐｜鮪魚蛋餅</span><span class="k">380 kcal</span></div>
      <div class="row"><span><span class="pill">包月</span>午餐｜雞肉便當</span><span class="r">吃了嗎？預留 605</span></div>
      <div class="act">吃了 雞肉便當 ›</div>
      <div class="act">吃了 鱸魚食蔬 ›</div>
      <div class="row"><span><span class="pill">包月</span>晚餐｜鱸魚食蔬</span><span class="r">預留 466</span></div>
    </div>
    <p class="ai-note">含 AI 估算紀錄，非營養師審核結果</p>
  </div>
</div>"""

_CHAT_MOCK = """
<div class="chat" role="img" aria-label="外食記錄畫面示意：傳排骨飯，回覆估算的熱量與蛋白質">
  <div class="bubble me">排骨飯</div>
  <div class="est"><span>排骨飯（一般份量估算）</span><b>約 820 kcal・蛋白質 33 g</b>
    <span>沒寫克數就用一般份量估，有寫克數會照你寫的算。</span><div class="btn">確認記錄</div></div>
</div>"""


def render_page(*, status: dict, meal_low: int, meal_high: int, fee_tiers, period_weeks: int,
                store_address: str) -> str:
    start = html.escape(line_start_url(), quote=True)
    friend = html.escape(line_add_friend_url(), quote=True)
    first_batch_open = bool(status.get("enabled") and status.get("left", 0) > 0)

    price_rows = ""
    for days in (2, 3, 5):
        meals = days * 2 * period_weeks
        price_rows += (
            f"<tr><td>每週 {days} 天</td><td>{meals} 餐</td>"
            f"<td>{_money(meals * meal_low)}～{_money(meals * meal_high)}</td></tr>"
        )
    fee_rows = ""
    prev = 0
    for max_m, fee, _zone in fee_tiers:
        label = f"{prev / 1000:g}–{max_m / 1000:g} 公里" if prev else "1 公里內"
        fee_rows += f"<tr><td>{label}</td><td>{_money(fee)}</td><td>{_money(-(-fee // 2))}</td></tr>"
        prev = max_m
    max_km = f"{fee_tiers[-1][0] / 1000:g}"
    first_batch_note = (
        '<p class="note strong">首批試跑期間，第一期餐費再打 9 折（運費不折）。</p>'
        if first_batch_open else ""
    )
    dishes = "".join(
        f'<figure><img src="/plan/assets/{f}" alt="{html.escape(name)}" loading="lazy" '
        f'width="1000" height="750"><figcaption>{html.escape(name)}</figcaption></figure>'
        for f, name in DISHES
    )

    return f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>一日樂食包月｜四週午晚餐幫你排好</title>
<meta name="description" content="依你的目標排好四週午晚餐，每天在 LINE 點一下就記好，營養師看過你的三日飲食。">
<meta property="og:title" content="一日樂食包月｜不用想吃什麼，也不用算熱量">
<meta property="og:description" content="依你的目標排好四週午晚餐，每天在 LINE 點一下就記好。">
<meta property="og:image" content="/plan/assets/salmon.jpg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans+TC:wght@400;500;700;900&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <div class="brand">一日樂食 <small>Deli Express</small></div>
    <div class="hero-grid">
      <div>
        <h1>不用想吃什麼，<br>也不用算熱量。</h1>
        <p class="lede">依你的目標排好四週午晚餐，每天在 LINE 點一下就記好，營養師還會看過你三天的飲食。</p>
        {_seat_block(status)}
        <a class="cta" href="{start}"><span class="line-badge">LINE</span>用 LINE 估價</a>
        <p class="cta-hint">會打開 LINE 並帶入「包月方案」，按送出就能開始估價。</p>
      </div>
      <div class="plate"><img src="/plan/assets/salmon.jpg" alt="鮭魚便當：烤鮭魚、五穀飯、滷蛋和蔬菜" width="1200" height="935"></div>
    </div>
  </div>
</header>

<main>
<section class="dishes" aria-label="菜色">
  <div class="wrap"><h2>這些是包月會吃到的</h2></div>
  <div class="strip">{dishes}</div>
</section>

<section class="fit">
  <div class="wrap">
    <h2>你是不是也這樣？</h2>
    <ul>
      <li><span class="box"></span>中午不知道吃什麼，最後隨便吃</li>
      <li><span class="box"></span>想減脂，但不想每天算熱量</li>
      <li><span class="box"></span>有在運動，蛋白質總是吃不夠</li>
      <li><span class="box"></span>午餐晚餐都想一次搞定</li>
    </ul>
  </div>
</section>

<section class="log">
  <div class="wrap">
    <h2>每天只要點一下</h2>
    <p class="lead">包月餐的熱量和蛋白質系統都算好了，你只要告訴它有沒有吃。</p>
    <div class="log-grid">
      <div class="scene">
        <h3>包月餐：點「吃了」就記好</h3>
        <p>打開今日總覽，熱量和蛋白質還剩多少一看就知道。兩個便當一起送到時，選中午吃的是哪一個。忘了點也沒關係，餐期過了會自動記成已吃。</p>
        <div class="phone">{_DASH_MOCK}</div>
        <p><span class="tag">畫面示意</span></p>
      </div>
      <div class="scene">
        <h3>外面吃的：傳一句話就好</h3>
        <p>傳「排骨飯」或拍照，系統幫你估熱量和蛋白質，確認後一起算進今天。</p>
        <div class="phone">{_CHAT_MOCK}</div>
        <p><span class="tag">畫面示意</span></p>
      </div>
    </div>
  </div>
</section>

<section class="rd" style="background:var(--butter)">
  <div class="wrap">
    <h2>AI 先整理，營養師看過才給你</h2>
    <p class="lead">包月包含一次三日飲食健檢。報告由合作營養師逐條看過、需要時修改，確認後才會送到你的 LINE。</p>
    <div class="rd-grid">
      <ol class="timeline">
        <li><span class="n">1</span><div><h3>照常吃三天</h3><p>包月餐點「吃了」，外食傳給我們，不用多做什麼。</p></div></li>
        <li><span class="n">2</span><div><h3>AI 整理成報告草稿</h3><p>三天吃了多少、蛋白質夠不夠、哪一餐最容易失衡。</p></div></li>
        <li class="rd"><span class="n">3</span><div><h3>營養師逐條看過</h3><p>不對的地方修改，確認沒問題才核准。</p></div></li>
        <li><span class="n">4</span><div><h3>你在 LINE 收到建議</h3><p>接下來 7 天可以怎麼調整，具體到吃什麼。</p></div></li>
      </ol>
      <div class="report" aria-label="營養師三日飲食健檢報告範例">
        <div class="report-head"><b>營養師三日飲食健檢報告</b><span class="tag">範例</span></div>
        <dl>
          <div><dt>做得好的地方</dt><dd>三天的包月餐都有吃完，每天都有兩份以上的蔬菜。</dd></div>
          <div><dt>目前優先事項</dt><dd>早餐蛋白質偏少，三天平均約 12 g，下午比較容易餓。</dd></div>
          <div><dt>接下來 7 天</dt><dd>早餐加一杯無糖豆漿或一顆茶葉蛋，先把早餐蛋白質拉到 20 g 左右。</dd></div>
        </dl>
      </div>
    </div>
    <p class="fine">三日健檢之後，如果想請營養師每週持續追蹤，可以另外加購。</p>
  </div>
</section>

<section class="steps">
  <div class="wrap">
    <h2>從估價到開吃</h2>
    <ol>
      <li><div><b>用 LINE 估價</b><span>選天數和取餐方式，馬上看到金額。</span></div></li>
      <li><div><b>填包月資料</b><span>身高體重、目標、不吃的食物。</span></div></li>
      <li><div><b>匯款開通</b><span>付款後開通，下週一開始。</span></div></li>
      <li><div><b>開吃，每天點一下</b><span>包月餐和外食都記在 LINE。</span></div></li>
    </ol>
  </div>
</section>

<section class="price">
  <div class="wrap two">
    <div>
      <h2>價格</h2>
      <p class="lead">一餐 {_money(meal_low)}～{_money(meal_high)}，依菜色不同。每期 {period_weeks} 週，外送一天兩餐。</p>
    </div>
    <div>
      <table>
        <thead><tr><th>一週吃</th><th>四週共</th><th>餐費</th></tr></thead>
        <tbody>{price_rows}</tbody>
      </table>
      {first_batch_note}
      <p class="note">實際金額以 LINE 估價為準，外送另計運費。</p>
    </div>
  </div>
</section>

<section class="delivery" style="background:var(--butter)">
  <div class="wrap">
    <h2>怎麼拿到便當</h2>
    <div class="pickup">
      <div><h3>一般外送</h3><p>11:30–12:00 左右送達，午晚兩個便當一起送。</p></div>
      <div><h3>晚鳥外送</h3><p>13:00–14:00 送達，運費半價。</p></div>
      <div><h3>到店自取</h3><p>週六也能取。每週 3 天以上送蛋白補充。</p></div>
    </div>
    <details class="fees"><summary>看外送運費</summary>
      <table>
        <thead><tr><th>距離</th><th>一般外送／趟</th><th>晚鳥／趟</th></tr></thead>
        <tbody>{fee_rows}</tbody>
      </table>
      <p class="note">超過 {max_km} 公里暫不外送；週六不外送。順路的客人每趟再折 $10。</p>
    </details>
  </div>
</section>

<section class="faq">
  <div class="wrap two">
    <h2>常見問題</h2>
    <div class="faq-list">
      <details class="q"><summary>什麼時候開始？</summary><p>每期四週，從下週一開始。週日晚上 8 點以後報名，會排到再下一個週一。</p></details>
      <details class="q"><summary>什麼時候要付款？</summary><p>開始前的週日晚上 8 點以前完成匯款。首批名額逾時沒付款，會釋出給下一位。</p></details>
      <details class="q"><summary>某一餐沒吃怎麼辦？</summary><p>在今日總覽點「沒吃」就好，當天晚上 11:59 前都能改。</p></details>
      <details class="q"><summary>營養師健檢要另外付錢嗎？</summary><p>包月包含一次三日飲食健檢，不用另外付費。之後想請營養師持續追蹤，才需要加購。</p></details>
      <details class="q"><summary>我有不吃的食物</summary><p>填包月資料時寫上，排餐會避開。</p></details>
      <details class="q"><summary>首批 9 折怎麼算？</summary><p>只打餐費，運費不折；適用第一期，續約恢復一般價格。限第一次訂包月的朋友。</p></details>
    </div>
  </div>
</section>

<section class="closing">
  <div class="wrap">
    <h2>先估個價，不用馬上決定</h2>
    <p>在 LINE 選好天數和取餐方式，30 秒就知道這一期多少錢。</p>
    <a class="cta" href="{start}"><span class="line-badge">LINE</span>用 LINE 估價</a>
    <p class="cta-hint">還沒加好友？<a href="{friend}">先加一日樂食 LINE</a></p>
  </div>
</section>
</main>

<footer>
  <div class="wrap">
    <p>一日樂食 Deli Express</p>
    <p>{html.escape(store_address)}</p>
  </div>
</footer>
</body>
</html>"""


def register_plan_landing_routes(app, *, db_path_getter, page_context):
    """db_path_getter 回傳目前 DB 路徑；page_context 回傳價格與運費設定。"""

    def _page():
        body = render_page(status=seat_status(db_path_getter()), **page_context())
        return HTMLResponse(body, headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def plan_root():
        return _page()

    @app.get("/plan", response_class=HTMLResponse, include_in_schema=False)
    def plan_page():
        return _page()

    @app.get("/plan/assets/{name}", include_in_schema=False)
    def plan_asset(name: str):
        if name not in ASSETS:
            raise HTTPException(status_code=404)
        return FileResponse(ASSET_DIR / name, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/first-batch/seats", include_in_schema=False)
    def first_batch_seats():
        status = seat_status(db_path_getter())
        return JSONResponse({"enabled": status["enabled"], "limit": status["limit"], "left": status["left"]},
                            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
