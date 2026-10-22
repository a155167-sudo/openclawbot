"""包月介紹頁（deliexpress-ai.com / /plan）。

一頁式介紹，所有報名動作都導回 LINE（估價、填表、付款都在 LINE 完成）。
首批剩餘名額在每次請求時從 SQLite 讀，不快取客人資料；/api/first-batch/seats 只回數字。
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

# 菜色名稱待 Jason 確認
DISHES = (
    ("pork.jpg", "慢烤豬肉藜麥沙拉"),
    ("couscous.jpg", "北非小米沙拉"),
    ("fish.jpg", "香煎魚排烤時蔬"),
    ("chicken.jpg", "嫩煎雞胸地瓜盤"),
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
        label = f"{prev / 1000:g}–{max_m / 1000:g} 公里" if prev else f"1 公里內"
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
<meta name="description" content="依你的目標排好四週午晚餐，每天在 LINE 幫你記錄。一週 2～5 天，外送或自取。">
<meta property="og:title" content="一日樂食包月｜不用想吃什麼，也不用算熱量">
<meta property="og:description" content="依你的目標排好四週午晚餐，每天在 LINE 幫你記錄。">
<meta property="og:image" content="/plan/assets/salmon.jpg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans+TC:wght@400;500;700;900&display=swap" rel="stylesheet">
<style>
:root {{
  --yolk: #F0C457;      /* 與鮭魚照片背景同色，讓盤子浮在頁面上 */
  --brand: #F5C200;
  --char: #2B2622;
  --ink: #4A423B;
  --paper: #FFFFFF;
  --butter: #FFF6D9;
  --leaf: #3E7D4F;
  --line: #06C755;
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{ margin: 0; background: var(--paper); color: var(--ink);
  font: 400 17px/1.75 "Noto Sans TC", system-ui, sans-serif; }}
img {{ max-width: 100%; display: block; }}
a {{ color: inherit; }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding: 0 20px; }}
h1, h2, h3 {{ color: var(--char); font-weight: 900; margin: 0; letter-spacing: .01em; }}
h2 {{ font-size: clamp(26px, 6vw, 36px); line-height: 1.3; margin-bottom: 20px; }}
h3 {{ font-size: 19px; line-height: 1.4; }}
p {{ margin: 0 0 12px; }}
section {{ padding: 64px 0; }}

/* hero */
.hero {{ background: var(--yolk); padding: 28px 0 0; overflow: hidden; }}
.brand {{ font-weight: 900; color: var(--char); font-size: 17px; display: flex; gap: 8px; align-items: baseline; }}
.brand small {{ font: 400 18px "Bebas Neue", sans-serif; letter-spacing: .06em; color: var(--char); opacity: .7; }}
.hero-grid {{ display: grid; gap: 8px; padding-top: 28px; }}
.hero h1 {{ font-size: clamp(38px, 11vw, 64px); line-height: 1.12; letter-spacing: -.01em; }}
.hero .lede {{ font-size: 18px; color: var(--char); max-width: 30em; margin: 18px 0 22px; }}
.plate {{ margin: 8px -20px 0; }}
.plate img {{ width: 100%; height: auto; }}

.seats {{ background: var(--paper); border-radius: 18px; padding: 18px 18px 14px; max-width: 420px; margin-bottom: 18px;
  box-shadow: 0 0 0 3px var(--char); }}
.seats-title {{ font-weight: 900; color: var(--char); margin: 0 0 10px; font-size: 18px; }}
.dots {{ display: grid; grid-template-columns: repeat(15, 1fr); gap: 6px; max-width: 360px; }}
.dot {{ aspect-ratio: 1; border-radius: 50%; background: var(--yolk); }}
.dot.taken {{ background: var(--char); }}
.seats-line {{ margin: 12px 0 0; color: var(--char); }}
.seats-line b {{ font: 400 34px/1 "Bebas Neue", sans-serif; vertical-align: -3px; margin: 0 2px; }}

.cta {{ display: inline-flex; align-items: center; justify-content: center; gap: 10px; min-height: 56px; padding: 0 30px;
  border-radius: 999px; background: var(--char); color: #fff; text-decoration: none; font-weight: 700; font-size: 18px; }}
.cta:focus-visible, a:focus-visible, summary:focus-visible {{ outline: 3px solid var(--line); outline-offset: 3px; }}
.cta .line-badge {{ background: var(--line); color: #fff; font-size: 12px; font-weight: 900; border-radius: 6px; padding: 1px 6px; }}
.cta-hint {{ font-size: 14px; color: var(--char); opacity: .8; margin-top: 10px; }}

/* fit */
.fit ul {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 14px; }}
.fit li {{ display: grid; grid-template-columns: 34px 1fr; gap: 12px; align-items: start; font-size: 19px; color: var(--char); font-weight: 500; }}
.box {{ width: 30px; height: 30px; border: 3px solid var(--char); border-radius: 8px; position: relative; margin-top: 2px; }}
.box::after {{ content: ""; position: absolute; left: 7px; top: 1px; width: 9px; height: 16px;
  border: solid var(--leaf); border-width: 0 4px 4px 0; transform: rotate(40deg); }}

/* steps: 這是真的順序 */
.steps {{ background: var(--butter); }}
.steps ol {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 26px; }}
.steps li {{ display: grid; grid-template-columns: 56px 1fr; gap: 14px; }}
.steps .n {{ font: 400 56px/0.9 "Bebas Neue", sans-serif; color: var(--brand); -webkit-text-stroke: 1.5px var(--char); }}
.steps p {{ margin: 6px 0 0; }}

/* what you get */
.get dl {{ margin: 0; display: grid; gap: 22px; }}
.get dt {{ font-weight: 900; color: var(--char); font-size: 19px; }}
.get dd {{ margin: 4px 0 0; }}

/* dishes */
.dishes {{ padding: 8px 0 64px; }}
.strip {{ display: grid; grid-auto-flow: column; grid-auto-columns: 78%; gap: 14px; overflow-x: auto;
  scroll-snap-type: x mandatory; padding: 0 20px 10px; scroll-padding-inline: 20px; scrollbar-width: none; }}
.strip::-webkit-scrollbar {{ display: none; }}
.strip figure {{ margin: 0; scroll-snap-align: start; }}
.strip img {{ border-radius: 20px; aspect-ratio: 4 / 3; object-fit: cover; width: 100%; height: auto; }}
.strip figcaption {{ font-weight: 700; color: var(--char); margin-top: 8px; }}

/* tables */
table {{ width: 100%; border-collapse: collapse; font-size: 16px; }}
th, td {{ text-align: left; padding: 12px 8px; border-bottom: 2px solid var(--butter); }}
td:first-child, th:first-child {{ padding-left: 0; }}
td:last-child, th:last-child {{ padding-right: 0; }}
th {{ color: var(--char); font-weight: 700; font-size: 14px; }}
td:last-child, th:last-child {{ text-align: right; }}
.note {{ font-size: 15px; margin-top: 14px; }}
.note.strong {{ color: var(--char); font-weight: 700; }}
.pickup {{ display: grid; gap: 22px; margin-bottom: 30px; }}
.pickup h3 {{ margin-bottom: 4px; }}

/* faq */
details {{ border-bottom: 2px solid var(--butter); padding: 16px 0; }}
summary {{ cursor: pointer; font-weight: 700; color: var(--char); font-size: 18px; list-style: none; display: flex; justify-content: space-between; gap: 12px; }}
summary::-webkit-details-marker {{ display: none; }}
summary::after {{ content: "+"; font: 400 28px/1 "Bebas Neue", sans-serif; }}
details[open] summary::after {{ content: "−"; }}
details p {{ margin: 10px 0 0; }}

.closing {{ background: var(--yolk); text-align: center; }}
.closing h2 {{ margin-bottom: 14px; }}
.closing p {{ color: var(--char); }}
footer {{ background: var(--char); color: #E9E2D8; padding: 28px 0 40px; font-size: 14px; }}
footer a {{ color: #fff; }}

@media (min-width: 820px) {{
  body {{ font-size: 18px; }}
  .hero-grid {{ grid-template-columns: 1.05fr 1fr; align-items: center; padding-bottom: 40px; }}
  .plate {{ margin: 0 -60px 0 0; }}
  .two {{ display: grid; grid-template-columns: 1fr 1.4fr; gap: 48px; }}
  .steps ol, .get dl {{ grid-template-columns: 1fr 1fr; column-gap: 48px; }}
  .strip {{ grid-auto-columns: 30%; padding: 0 max(20px, calc((100vw - 1000px) / 2)); scroll-padding-inline: max(20px, calc((100vw - 1000px) / 2)); }}
  .pickup {{ grid-template-columns: repeat(3, 1fr); }}
}}
@media (prefers-reduced-motion: no-preference) {{
  .dot.taken {{ animation: fill .5s ease-out both; }}
  @keyframes fill {{ from {{ transform: scale(.4); background: var(--yolk); }} }}
}}
</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <div class="brand">一日樂食 <small>Deli Express</small></div>
    <div class="hero-grid">
      <div>
        <h1>不用想吃什麼，<br>也不用算熱量。</h1>
        <p class="lede">一日樂食包月依你的目標排好四週午晚餐，每天在 LINE 幫你記錄吃了什麼。</p>
        {_seat_block(status)}
        <a class="cta" href="{start}"><span class="line-badge">LINE</span>用 LINE 估價</a>
        <p class="cta-hint">會打開 LINE 並帶入「包月方案」，按送出就能開始估價。</p>
      </div>
      <div class="plate"><img src="/plan/assets/salmon.jpg" alt="烤鮭魚、五穀飯、滷蛋和蔬菜的健康餐盤" width="1200" height="935"></div>
    </div>
  </div>
</header>

<main>
<section class="fit">
  <div class="wrap two">
    <h2>你是不是也這樣？</h2>
    <ul>
      <li><span class="box"></span>中午不知道吃什麼，最後隨便吃</li>
      <li><span class="box"></span>想減脂，但不想每天算熱量</li>
      <li><span class="box"></span>有在運動，蛋白質總是吃不夠</li>
      <li><span class="box"></span>午餐晚餐都想一次搞定</li>
    </ul>
  </div>
</section>

<section class="steps">
  <div class="wrap">
    <h2>從估價到開吃，四個步驟</h2>
    <ol>
      <li><span class="n">1</span><div><h3>用 LINE 估價</h3><p>選一週吃幾天、怎麼取餐，馬上看到這一期大概多少錢。</p></div></li>
      <li><span class="n">2</span><div><h3>填包月資料</h3><p>身高體重、目標和不吃的食物。系統依你的熱量和蛋白質需求排好四週菜單。</p></div></li>
      <li><span class="n">3</span><div><h3>匯款開通</h3><p>客服確認後傳匯款資訊給你，付款後開通，下週一開始。</p></div></li>
      <li><span class="n">4</span><div><h3>每天點一下「吃了嗎」</h3><p>用餐時間 LINE 會問你，點一下就記錄。外面吃的也能傳給我們估熱量。</p></div></li>
    </ol>
  </div>
</section>

<section class="get">
  <div class="wrap two">
    <h2>包月裡有什麼</h2>
    <dl>
      <div><dt>四週午晚餐</dt><dd>依你的目標排餐，外送時午晚兩個便當一起送到。</dd></div>
      <div><dt>每天的進度</dt><dd>在 LINE 打開今日總覽，熱量和蛋白質吃到幾成一看就知道。</dd></div>
      <div><dt>營養師看過的三日飲食健檢</dt><dd>記錄三天後，營養師看過你的飲食，給你具體的調整建議。</dd></div>
      <div><dt>臨時有事可以改期</dt><dd>某天不方便吃，在 LINE 申請改到其他日子。</dd></div>
    </dl>
  </div>
</section>

<section class="dishes" aria-label="菜色">
  <div class="wrap"><h2>這些是包月會吃到的</h2></div>
  <div class="strip">{dishes}</div>
</section>

<section class="price" style="background:var(--butter)">
  <div class="wrap two">
    <div>
      <h2>價格</h2>
      <p>一餐 {_money(meal_low)}～{_money(meal_high)}，依菜色不同。每期 {period_weeks} 週，外送一天兩餐。</p>
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

<section class="delivery">
  <div class="wrap">
    <h2>怎麼拿到便當</h2>
    <div class="pickup">
      <div><h3>一般外送</h3><p>約 11:30–12:00 送達，原運費。午晚兩個便當一起送，一天一趟。</p></div>
      <div><h3>晚鳥外送</h3><p>13:00–14:00 送達，不指定時間，運費半價。</p></div>
      <div><h3>到店自取</h3><p>餐數更彈性，週六也能自取。每週 3 天以上送 1 次蛋白補充，5 天送 2 次。</p></div>
    </div>
    <table>
      <thead><tr><th>距離</th><th>一般外送／趟</th><th>晚鳥／趟</th></tr></thead>
      <tbody>{fee_rows}</tbody>
    </table>
    <p class="note">超過 {max_km} 公里暫不外送；週六不外送。順路的客人每趟再折 $10。</p>
  </div>
</section>

<section class="faq">
  <div class="wrap two">
    <h2>常見問題</h2>
    <div>
      <details><summary>什麼時候開始？</summary><p>每期四週，從下週一開始。週日晚上 8 點以後報名，會排到再下一個週一。</p></details>
      <details><summary>什麼時候要付款？</summary><p>開始前的週日晚上 8 點以前完成匯款。首批名額逾時沒付款，會釋出給下一位。</p></details>
      <details><summary>某一餐沒吃怎麼辦？</summary><p>LINE 問你「吃了嗎」的時候點「沒吃」就好，當天晚上 11:59 前都能改。</p></details>
      <details><summary>我有不吃的食物</summary><p>填包月資料時寫上，排餐會避開。</p></details>
      <details><summary>首批 9 折怎麼算？</summary><p>只打餐費，運費不折；適用第一期，續約恢復一般價格。限第一次訂包月的朋友。</p></details>
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
