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
    "img-src 'self' data: blob:; "
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
    details{margin-top:10px}summary{cursor:pointer}.period{line-height:1.6;font-size:14px;overflow-wrap:anywhere}pre{white-space:pre-wrap;word-break:break-word;background:#f6f8f6;padding:10px;border-radius:8px;font-size:12px}
    .empty,.error{background:#fff7e8;border-radius:12px;padding:14px}.error{color:#8a2d22;background:#fff0ee}
    .photo-actions{margin-top:8px}.photo-actions button{margin-right:8px}.photo-preview{margin-top:8px}
    .photo-preview img{display:block;max-width:100%;height:auto;border-radius:10px}.photo-message{margin:8px 0;color:#6a3d27}
    .meal-card{list-style:none;border:1px solid #dce8df;border-radius:12px;padding:12px;margin:10px 0}.nutrition{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 12px;margin-top:8px}
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
let photoGeneration=0;
let activePhotoCaseId=null;
const photoControllers=new Set();
const photoUrls=new Map();
const photoPanels=new Set();
const photoRequests=new Map();
const statusLabels={{collecting:'收集中',ready_for_review:'可審核',needs_more_info:'需補資料',approved_pending_delivery:'已核准待發送',delivery_failed:'發送失敗',delivered:'已送達',expired:'已過期',cancelled:'已取消'}};
const viewablePhotoStatuses=new Set(['ready_for_review','needs_more_info','approved_pending_delivery','delivery_failed']);
const completenessLabels={{qualified:'符合',incomplete:'未符合'}};
function node(tag,text,klass){{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(klass)el.className=klass;return el;}}
function clearPhoto(panel){{
  const request=photoRequests.get(panel);
  if(request){{
    request.controller.abort();
    photoControllers.delete(request.controller);
    photoRequests.delete(panel);
    request.button.disabled=false;
  }}
  const url=photoUrls.get(panel);
  if(url){{URL.revokeObjectURL(url);photoUrls.delete(panel);}}
  panel.replaceChildren();
}}
function clearAllPhotos(){{
  photoGeneration+=1;
  activePhotoCaseId=null;
  for(const controller of photoControllers)controller.abort();
  photoControllers.clear();
  for(const panel of photoPanels)clearPhoto(panel);
  photoPanels.clear();
}}
function rangeText(value){{
  if(value===null||value===undefined)return 'NA';
  if(typeof value==='object'&&Number.isFinite(value.min)&&Number.isFinite(value.max))return `${{value.min}}～${{value.max}}份`;
  return 'NA';
}}
function nutritionEstimateText(point,range,unit){{
  if(!Number.isFinite(point)||!range||typeof range!=='object'||!Number.isFinite(range.min)||!Number.isFinite(range.max)||range.min>point||point>range.max)return 'NA';
  return `約${{point}}${{unit}}（${{range.min}}～${{range.max}}）`;
}}
function nutritionValue(snapshot,key,unit){{
  const value=snapshot&&snapshot[key];
  return Number.isFinite(value)?`${{value}} ${{unit}}`:'NA';
}}
function taipeiTimestamp(value){{
  if(typeof value!=='string')return '未提供';
  const match=value.match(/^([0-9]{{4}})-([0-9]{{2}})-([0-9]{{2}})T([0-9]{{2}}):([0-9]{{2}}):([0-9]{{2}})(?:\\.[0-9]+)?(?:(Z)|([+-])([0-9]{{2}}):([0-9]{{2}}))$/);
  if(!match)return '未提供';
  const hour=Number(match[4]);
  const minute=Number(match[5]);
  const second=Number(match[6]);
  const offsetHour=match[7]?0:Number(match[9]);
  const offsetMinute=match[7]?0:Number(match[10]);
  if(hour>23||minute>59||second>59||offsetHour>23||offsetMinute>59)return '未提供';
  const calendarDate=new Date(`${{match[1]}}-${{match[2]}}-${{match[3]}}T00:00:00Z`);
  if(!Number.isFinite(calendarDate.getTime())||calendarDate.toISOString().slice(0,10)!==`${{match[1]}}-${{match[2]}}-${{match[3]}}`)return '未提供';
  const instant=new Date(value);
  if(!Number.isFinite(instant.getTime()))return '未提供';
  const parts={{}};
  for(const part of new Intl.DateTimeFormat('en-CA',{{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}}).formatToParts(instant))parts[part.type]=part.value;
  return `${{parts.year}}/${{parts.month}}/${{parts.day}} ${{parts.hour}}:${{parts.minute}}`;
}}
function photoUnavailableText(status){{
  if(status==='collecting')return '收集中，照片尚未開放';
  if(status==='delivered')return '案件已送達，照片不再開放';
  if(status==='expired')return '案件已過期，照片不再開放';
  if(status==='cancelled')return '案件已取消，照片不再開放';
  return '目前案件狀態不開放照片';
}}
function photoErrorMessage(status){{
  if(status===401)return 'LINE身分驗證失敗';
  if(status===403)return '此LINE帳號未獲營養師唯讀權限';
  if(status===404)return '照片不可用';
  if(status===503)return '照片暫時無法載入，請重試';
  return `照片讀取失敗（${{status}}）`;
}}
async function loadPhoto(caseId,logId,button,panel){{
  if(activePhotoCaseId!==null&&activePhotoCaseId!==caseId)clearAllPhotos();
  activePhotoCaseId=caseId;
  photoPanels.add(panel);
  clearPhoto(panel);
  const generation=photoGeneration;
  const controller=new AbortController();
  const request={{controller,button}};
  photoControllers.add(controller);
  photoRequests.set(panel,request);
  button.disabled=true;
  try{{
    const path='/api/dietitian/health-checks/'+encodeURIComponent(caseId)+'/sources/'+encodeURIComponent(logId)+'/image';
    const response=await fetch(path,{{method:'GET',headers:{{Authorization:'Bearer '+idToken}},cache:'no-store',credentials:'omit',signal:controller.signal}});
    if(controller.signal.aborted||generation!==photoGeneration)return;
    if(!response.ok){{
      const message=photoErrorMessage(response.status);
      if(response.status===401||response.status===403){{
        clearAllPhotos();
        photoPanels.add(panel);
        panel.replaceChildren(node('p',message,'photo-message'));
        button.disabled=false;
      }}else panel.replaceChildren(node('p',message,'photo-message'));
      return;
    }}
    const blob=await response.blob();
    if(controller.signal.aborted||generation!==photoGeneration)return;
    const url=URL.createObjectURL(blob);
    if(controller.signal.aborted||generation!==photoGeneration){{URL.revokeObjectURL(url);return;}}
    photoUrls.set(panel,url);
    const image=node('img');
    image.setAttribute('src',url);
    image.setAttribute('alt','餐點照片');
    const close=node('button','關閉照片');
    close.setAttribute('type','button');
    close.addEventListener('click',clearAllPhotos);
    panel.replaceChildren(image,close);
  }}catch(error){{
    if(!controller.signal.aborted&&generation===photoGeneration)panel.replaceChildren(node('p','照片暫時無法載入，請重試','photo-message'));
  }}finally{{
    photoControllers.delete(controller);
    if(photoRequests.get(panel)===request){{
      photoRequests.delete(panel);
      button.disabled=false;
    }}
  }}
}}
function renderSource(caseId,caseStatus,log,index){{
  const item=node('li',undefined,'meal-card');
  const isAi=log.trust_type==='user_confirmed_ai_estimate';
  const isApproved=log.approval_status==='approved';
  const label=isAi?'顧客確認・AI估算，非營養師核准':isApproved?'營養師核准':'可驗證營養快照（未標示營養師核准）';
  item.append(node('strong',`餐點紀錄 ${{index+1}}`));
  item.append(node('div',label,'meta'));
  if(isAi){{
    const estimate=log.estimate||{{}};
    if(log.estimate_schema_version==='meal-photo-user-confirmation-v2'){{
      item.append(node('div',`${{nutritionEstimateText(estimate.calories_kcal,estimate.calories_kcal_range,' kcal')}}｜蛋白質${{nutritionEstimateText(estimate.protein_g,estimate.protein_g_range,'g')}}`,'meta'));
      const nutrition=node('div',undefined,'nutrition');
      nutrition.append(node('span',`脂肪：${{nutritionValue(log.nutrition_snapshot,'fat_g','g')}}`),node('span',`碳水化合物：${{nutritionValue(log.nutrition_snapshot,'carbohydrate_g','g')}}`),node('span',`膳食纖維：${{nutritionValue(log.nutrition_snapshot,'fiber_g','g')}}`),node('span',`鈉：${{nutritionValue(log.nutrition_snapshot,'sodium_mg','mg')}}`));
      item.append(nutrition);
    }}else item.append(node('div',`熱量：NA｜蛋白質：${{rangeText(estimate.protein_total_exchange)}}｜主食：${{rangeText(estimate.starch_exchange)}}｜蔬菜：${{rangeText(estimate.vegetable_exchange)}}`,'meta'));
  }}else{{
    const snapshot=log.nutrition_snapshot||{{}};
    const nutrition=node('div',undefined,'nutrition');
    for(const [title,key,unit] of [['熱量','calories_kcal','kcal'],['蛋白質','protein_g','g'],['脂肪','fat_g','g'],['碳水化合物','carbohydrate_g','g'],['膳食纖維','fiber_g','g'],['鈉','sodium_mg','mg']])nutrition.append(node('span',`${{title}}：${{nutritionValue(snapshot,key,unit)}}`));
    item.append(nutrition);
  }}
  const technical=node('details');
  technical.append(node('summary','技術資料'),node('div','日期與餐別尚無可驗證資料','meta'),node('pre',JSON.stringify({{log_id:log.log_id,food_log_version:log.food_log_version,nutrition_snapshot:log.nutrition_snapshot||{{}}}},null,2)));
  item.append(technical);
  const actions=node('div',undefined,'photo-actions');
  const panel=node('div',undefined,'photo-preview');
  if(viewablePhotoStatuses.has(caseStatus)){{
    const view=node('button','查看照片');
    view.setAttribute('type','button');
    view.addEventListener('click',()=>loadPhoto(caseId,log.log_id,view,panel));
    actions.append(view);
  }}else actions.append(node('p',photoUnavailableText(caseStatus),'photo-message'));
  item.append(actions,panel);
  return item;
}}
async function api(path){{
  const response=await fetch(path,{{method:'GET',headers:{{Authorization:`Bearer ${{idToken}}`}},cache:'no-store',credentials:'omit'}});
  if(!response.ok)throw new Error(response.status===403?'此LINE帳號未獲營養師唯讀權限':response.status===401?'LINE身分驗證失敗':`讀取失敗（${{response.status}}）`);
  return response.json();
}}
function isNonnegativeInteger(value){{
  return Number.isInteger(value)&&value>=0;
}}
function validatedValidDays(detail){{
  const days=detail&&detail.valid_days;
  if(!Array.isArray(days))return null;
  const dates=new Set();
  for(const day of days){{
    if(!day||typeof day!=='object'||Array.isArray(day))return null;
    if(typeof day.local_date!=='string'||!/^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$/.test(day.local_date))return null;
    if(dates.has(day.local_date))return null;
    dates.add(day.local_date);
    if(typeof day.rule_version!=='string'||day.rule_version.length===0||day.rule_version.length>100)return null;
    if(!isNonnegativeInteger(day.qualifying_meal_count)||day.qualifying_meal_count>100)return null;
    if(day.completeness_status!=='qualified'&&day.completeness_status!=='incomplete')return null;
    if(typeof day.evaluated_at!=='string'||day.evaluated_at.length===0)return null;
  }}
  return days;
}}
function validatedValidDayCount(item,detail,validDays){{
  const listCount=item&&item.valid_day_count;
  const detailCount=detail&&detail.valid_day_count;
  if(!isNonnegativeInteger(listCount)||!isNonnegativeInteger(detailCount)||validDays===null)return null;
  const qualifiedCount=validDays.filter(day=>day.completeness_status==='qualified').length;
  if(listCount!==detailCount||detailCount!==qualifiedCount)return null;
  return detailCount;
}}
function validatedSourceCounts(detail,sourceLogs){{
  if(!Array.isArray(sourceLogs))return null;
  const integrity=detail&&detail.source_integrity;
  if(!integrity||typeof integrity!=='object'||Array.isArray(integrity))return null;
  const referenced=integrity.referenced_count;
  const available=integrity.available_snapshot_count;
  const complete=integrity.all_snapshots_available;
  if(!isNonnegativeInteger(referenced)||!isNonnegativeInteger(available))return null;
  if(available>referenced||available!==sourceLogs.length)return null;
  if(typeof complete!=='boolean'||complete!==(referenced===available))return null;
  return {{referenced,available}};
}}
function renderCase(item,detail){{
  const card=node('article',undefined,'case');
  card.append(node('h2',statusLabels[item.status]||item.status));
  const validDays=validatedValidDays(detail);
  const validDayCount=validatedValidDayCount(item,detail,validDays);
  card.append(node('div',validDayCount===null?'有效日：資料不可用':`有效日：${{validDayCount}} / 3`,'meta'));
  const period=node('div',undefined,'period');
  period.append(node('div',`開始：${{taipeiTimestamp(item.window_started_at)}}`),node('div',`截止：${{taipeiTimestamp(item.window_ends_at)}}`));
  card.append(period);
  const days=node('ul',undefined,'days');
  if(validDays===null)days.append(node('li','有效日期：資料不可用'));
  else if(validDays.length===0)days.append(node('li','目前尚無可列入的日期'));
  for(const day of validDays||[])days.append(node('li',`${{day.local_date}}｜${{day.qualifying_meal_count}} 餐｜${{completenessLabels[day.completeness_status]||'資料不可用'}}`));
  card.append(days);
  const rawSourceLogs=detail&&detail.source_logs;
  const sourceLogs=Array.isArray(rawSourceLogs)?rawSourceLogs:[];
  const sourceCounts=validatedSourceCounts(detail,rawSourceLogs);
  const disclosure=node('details');
  disclosure.append(node('summary','來源完整性資料'));
  const integrity=node('ul',undefined,'days');
  integrity.append(node('li',sourceCounts===null?'保留來源參照：資料不可用':`保留來源參照：${{sourceCounts.referenced}} 筆`));
  integrity.append(node('li',sourceCounts===null?'目前可驗證快照：資料不可用':`目前可驗證快照：${{sourceCounts.available}} 筆`));
  disclosure.append(integrity);
  card.append(disclosure);
  const sources=node('ul',undefined,'days');
  if(sourceCounts===null&&sourceLogs.length===0)sources.append(node('li','來源快照清單資料不可用'));
  else if(sourceCounts&&sourceCounts.available===0)sources.append(node('li','目前無可顯示的來源快照；不代表沒有飲食紀錄'));
  sourceLogs.forEach((log,index)=>sources.append(renderSource(item.case_id,item.status,log,index)));
  card.append(sources);
  return card;
}}
async function loadCases(){{
  clearAllPhotos();
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
window.addEventListener('pagehide',clearAllPhotos);
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
