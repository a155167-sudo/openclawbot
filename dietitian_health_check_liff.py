from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from dietitian_health_check_api import DietitianHealthCheckConfig, NO_STORE_HEADERS


_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://static.line-scdn.net; "
    "connect-src 'self' https://api.line.me https://access.line.me "
    "https://liffsdk.line-scdn.net https://uts-front.line-apps.com; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://*.line.me"
)


def _headers() -> dict[str, str]:
    return {**NO_STORE_HEADERS, "Content-Security-Policy": _CSP}


def _html() -> str:
    return """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>營養師三日健檢｜Staging</title>
  <style>
    :root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#24332b;background:#f3f7f4}
    body{margin:0;padding:20px 16px 40px}main{max-width:720px;margin:auto}
    h1{font-size:22px;margin:0 0 6px}.sub{color:#607066;font-size:14px;margin:0 0 18px}
    button{border:0;border-radius:10px;background:#27734b;color:#fff;padding:11px 16px;font-weight:700}
    #status{margin:14px 0;color:#526159}.case{background:#fff;border-radius:14px;padding:16px;margin:12px 0;box-shadow:0 2px 12px #153b2420}
    .case h2{font-size:18px;margin:0 0 8px}.meta{line-height:1.65;font-size:14px}.days{margin:10px 0;padding-left:20px}
    details{margin-top:10px}pre{white-space:pre-wrap;word-break:break-word;background:#f6f8f6;padding:10px;border-radius:8px;font-size:12px}
    .empty,.error{background:#fff7e8;border-radius:12px;padding:14px}.error{color:#8a2d22;background:#fff0ee}
  </style>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js" defer></script>
  <script src="/dietitian-health-check/app.js" defer></script>
</head>
<body><main>
  <h1>營養師三日健檢</h1>
  <p class="sub">電腦營養 Staging｜即時唯讀｜不提供修改與核准</p>
  <button id="refresh" type="button">重新整理</button>
  <p id="status" role="status">正在驗證LINE身分…</p>
  <section id="cases" aria-live="polite"></section>
</main></body></html>"""


def _javascript(liff_id: str) -> str:
    encoded_liff_id = json.dumps(liff_id, ensure_ascii=True)
    return f"""'use strict';
const LIFF_ID={encoded_liff_id};
const statusNode=document.getElementById('status');
const casesNode=document.getElementById('cases');
const refreshButton=document.getElementById('refresh');
let idToken='';
const statusLabels={{collecting:'收集中',ready_for_review:'可審核',needs_more_info:'需補資料',approved_pending_delivery:'已核准待發送',delivery_failed:'發送失敗',delivered:'已送達',expired:'已過期',cancelled:'已取消'}};
function node(tag,text,klass){{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(klass)el.className=klass;return el;}}
async function api(path){{
  const response=await fetch(path,{{method:'GET',headers:{{Authorization:`Bearer ${{idToken}}`}},cache:'no-store',credentials:'omit'}});
  if(!response.ok)throw new Error(response.status===403?'此LINE帳號未獲營養師唯讀權限':response.status===401?'LINE身分驗證失敗':`讀取失敗（${{response.status}}）`);
  return response.json();
}}
function renderCase(item,detail){{
  const card=node('article',undefined,'case');
  card.append(node('h2',`${{statusLabels[item.status]||item.status}}｜有效日 ${{item.valid_day_count}} / 3`));
  card.append(node('div',`收集期間：${{item.window_started_at}} ～ ${{item.window_ends_at}}`,'meta'));
  const days=node('ul',undefined,'days');
  const validDays=(detail&&detail.valid_days)||[];
  if(validDays.length===0)days.append(node('li','目前尚無可列入的日期'));
  for(const day of validDays)days.append(node('li',`${{day.local_date}}｜${{day.qualifying_meal_count}} 餐｜${{day.completeness_status}}`));
  card.append(days);
  const disclosure=node('details');
  disclosure.append(node('summary','查看去識別化來源與營養快照'));
  disclosure.append(node('pre',JSON.stringify((detail&&detail.source_logs)||[],null,2)));
  card.append(disclosure);
  return card;
}}
async function loadCases(){{
  refreshButton.disabled=true;casesNode.replaceChildren();statusNode.textContent='讀取Staging案件中…';
  try{{
    const listing=await api('/api/dietitian/health-checks?limit=25&offset=0');
    const items=listing.items||[];
    if(items.length===0){{casesNode.append(node('div','尚未建立三日健檢案件；請先確認測試VIP首次開通狀態。','empty'));statusNode.textContent='目前沒有案件';return;}}
    for(const item of items){{
      const detail=await api('/api/dietitian/health-checks/'+encodeURIComponent(item.case_id));
      casesNode.append(renderCase(item,detail));
    }}
    statusNode.textContent=`已更新：${{new Date().toLocaleString('zh-TW')}}｜共 ${{items.length}} 案`;
  }}catch(error){{casesNode.append(node('div',error.message||'無法讀取案件','error'));statusNode.textContent='讀取失敗';}}
  finally{{refreshButton.disabled=false;}}
}}
async function boot(){{
  try{{
    await liff.init({{liffId:LIFF_ID}});
    if(!liff.isLoggedIn()){{liff.login({{redirectUri:location.href}});return;}}
    idToken=liff.getIDToken()||'';
    if(!idToken)throw new Error('無法取得LINE身分憑證');
    await loadCases();
  }}catch(error){{casesNode.replaceChildren(node('div',error.message||'初始化失敗','error'));statusNode.textContent='初始化失敗';}}
}}
refreshButton.addEventListener('click',loadCases);
boot();
"""


def attach_dietitian_health_check_liff_routes(app, config: DietitianHealthCheckConfig) -> bool:
    if not config.enabled:
        return False
    if not config.liff_id:
        raise ValueError("dietitian health-check LIFF id is required")

    router = APIRouter()

    @router.get("/dietitian-health-check", response_class=HTMLResponse)
    def dietitian_health_check_page():
        return HTMLResponse(_html(), headers=_headers())

    @router.get("/dietitian-health-check/app.js")
    def dietitian_health_check_script():
        return Response(
            _javascript(config.liff_id),
            media_type="application/javascript; charset=utf-8",
            headers=_headers(),
        )

    app.include_router(router)
    return True
