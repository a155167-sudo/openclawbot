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
<html lang="zh-Hant"><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>一日樂食｜營養師三日健檢工作台</title>
  <style>
    :root{--bg:#f3f0e8;--paper:#fffdf8;--ink:#253e34;--muted:#637068;--green:#234f40;--line:#d9ddd3;--soft:#e8eee5;--amber:#805927;--sand:#f5ecd9;font-family:"PingFang TC","Microsoft JhengHei",sans-serif;color:var(--ink);background:var(--bg)}*{box-sizing:border-box}body{margin:0}button,input{font:inherit}button{min-height:44px;border:1px solid var(--line);background:var(--paper);color:var(--ink);padding:9px 16px;border-radius:7px;font-weight:650;cursor:pointer}button:hover{background:var(--soft)}button:disabled{opacity:.55;cursor:not-allowed}[hidden]{display:none!important}.shell{max-width:1440px;margin:auto}.top{padding:18px 38px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;background:var(--paper)}#refresh{white-space:nowrap;flex:0 0 auto}.brand{font-family:"Iowan Old Style","Songti TC",serif;font-size:22px;font-weight:bold;letter-spacing:.1em}.sub,.meta,small{color:var(--muted)}main{padding:30px 38px 80px}h1,h2,h3,p{margin-top:0}.heading{display:flex;justify-content:space-between;gap:20px;align-items:end}.heading>div{min-width:0}.eyebrow{font-size:11px;letter-spacing:.15em;color:var(--muted)}#status{margin:12px 0}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0}.filters button[aria-pressed="true"]{background:var(--green);color:white}.filters button span{margin-left:8px}.search{display:flex;align-items:center;gap:8px;margin-left:auto}.search input{min-height:44px;border:1px solid var(--line);border-radius:7px;padding:8px 12px;background:var(--paper)}.list-head,.case-row{display:grid;grid-template-columns:minmax(180px,1.2fr) minmax(0,1fr) minmax(0,1.3fr) 110px;gap:18px;align-items:center}.list-head{padding:9px 16px;color:var(--muted);font-size:12px}.case-row{width:100%;text-align:left;margin:8px 0;padding:18px 16px}.person strong{display:block;font-size:16px}.pill{display:inline-block;flex:0 0 auto;min-width:max-content;white-space:nowrap;font-size:12px;padding:3px 9px;border-radius:4px;background:var(--soft)}.pill.pending{background:var(--sand);color:var(--amber)}.queue-foot{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:18px}.back{border-color:transparent;background:transparent;padding-left:0}.review-grid{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(320px,.8fr);gap:20px}.panel{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:22px;margin-bottom:18px}.profile{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.profile div{border-bottom:1px solid var(--line)}dt{font-size:12px;color:var(--muted)}dd{margin:3px 0 10px}.limitation{padding:13px;background:var(--sand);border-left:3px solid #ab7630}.days{padding-left:20px}.meal-card{list-style:none;border:1px solid var(--line);border-radius:8px;padding:14px;margin:10px 0}.nutrition{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 12px}.nutrition span{min-width:0;overflow-wrap:anywhere}.nutrition-value{white-space:nowrap}.photo-preview img{display:block;max-width:100%;height:auto;border-radius:8px}.photo-message{color:#6a3d27}.timeline-tabs{display:flex;gap:8px;overflow-x:auto;margin:12px 0}.timeline-tabs button[aria-selected="true"]{background:var(--green);color:white}.meal-group{margin:18px 0}.meal-group h3{padding-bottom:8px;border-bottom:1px solid var(--line)}.mobile-tabs{display:none}.error,.empty{padding:14px;background:#fff0ee;border-radius:8px}.empty{background:var(--sand)}pre{white-space:pre-wrap;word-break:break-word;font-size:12px}
    @media(max-width:480px){.nutrition{grid-template-columns:1fr}}@media(max-width:800px){.top,main{padding-left:18px;padding-right:18px}.heading{align-items:start}.list-head{display:none}.case-row{grid-template-columns:1fr auto}.case-row .records,.case-row .missing{grid-column:1/-1}.filters{gap:6px}.search{width:100%;margin-left:0}.search input{flex:1;min-width:0}.review-grid{display:block}.mobile-tabs{display:flex;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:2}.mobile-tabs button{flex:1}.review-grid[data-mobile="review"] #evidence,.review-grid[data-mobile="evidence"] #reviewPane{display:none}.profile{grid-template-columns:1fr}.queue-foot{display:block}}
  </style>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js" defer></script><script src="/dietitian-health-check/app.js" defer></script>
</head><body><div class="shell"><header class="top"><div class="brand">一日樂食 <small>營養師工作台 / 三日健檢</small></div><button id="refresh" type="button">重新整理</button></header><main>
<section id="queue"><div class="heading"><div><p class="eyebrow">CASE WORKSPACE / 案件工作區</p><h1>健檢案件</h1><p class="sub">真實唯讀資料；本階段只恢復案件清單與證據檢視。</p></div></div><p id="status" role="status">正在驗證 LINE 身分…</p><div id="filters" class="filters" aria-label="案件狀態篩選"></div><label class="search"><span>搜尋案件</span><input id="search" type="search" placeholder="姓名、目標或案件編號" aria-label="搜尋姓名、目標或案件編號"></label><div class="list-head"><span>顧客 / 目標</span><span>狀態與有效日</span><span>等待依據 / 資料限制</span><span>下一步</span></div><div id="caseList" aria-live="polite"></div><div class="queue-foot"><span id="queueCount"></span><span>此頁最多 25 筆；未提供 ≠ 已確認無。</span></div></section>
<section id="review" hidden><button id="backQueue" class="back" type="button">← 返回案件清單</button><div class="heading"><div><p class="eyebrow">REVIEW / 證據與判讀</p><h1 id="caseName"></h1><p id="caseSubtitle" class="sub"></p></div><span id="caseStatus" class="pill"></span></div><div class="mobile-tabs" role="tablist" aria-label="案件內容"><button id="evidenceTab" role="tab" aria-selected="true">案件證據</button><button id="reviewTab" role="tab" aria-selected="false">審核資料</button></div><div id="reviewGrid" class="review-grid" data-mobile="evidence"><div id="evidence"><section class="panel"><h2>顧客資料與限制</h2><dl id="profile" class="profile"></dl><div id="limitations" class="limitation"></div></section><section class="panel"><h2>已驗證有效日</h2><ul id="validDays" class="days"></ul><p class="meta">本階段不把來源推定為日期或餐別；僅列後端已驗證有效日。</p></section><section class="panel"><h2>三日餐點時間軸</h2><div id="sourceIntegrity"></div><div id="timelineTabs" class="timeline-tabs" role="tablist" aria-label="餐點日期"></div><div id="timelineDays"></div><section id="legacySources" hidden><h3>日期／餐別尚無可驗證資料</h3><p class="meta">保留既有未綁定來源；不借用有效日推斷、不重新計算營養。</p><ul id="sourceLogs" class="days"></ul></section></section></div><aside id="reviewPane"><section class="panel"><h2>最新審核與新鮮度</h2><div id="latestReview"></div><p class="meta">唯讀階段成果；尚未提供儲存、核准或投遞操作。</p></section></aside></div></section>
</main></div></body></html>"""


def _javascript(liff_id: str) -> str:
    encoded_liff_id = json.dumps(liff_id, ensure_ascii=True)
    return f"""'use strict';
const LIFF_ID={encoded_liff_id};
const statusNode=document.getElementById('status');
const queueNode=document.getElementById('queue');
const reviewNode=document.getElementById('review');
const casesNode=document.getElementById('caseList');
const refreshButton=document.getElementById('refresh');
const filtersNode=document.getElementById('filters');
const searchNode=document.getElementById('search');
const queueCountNode=document.getElementById('queueCount');
let idToken='';
let listedCases=[];
let selectedStatus='all';
let detailsByCase=new Map();
let appGeneration=0;
let activeCaseId=null;
const apiControllers=new Set();
const REQUEST_TIMEOUT_MS=15000;
let photoGeneration=0;
let activePhotoCaseId=null;
const photoControllers=new Set();
const photoUrls=new Map();
const photoPanels=new Set();
const photoRequests=new Map();
const statusLabels={{collecting:'收集中',ready_for_review:'可審核',needs_more_info:'需補資料',approved_pending_delivery:'待投遞',delivery_failed:'投遞失敗',delivered:'已送達',expired:'已過期',cancelled:'已取消'}};
const statusTabs=[['all','全部'],['collecting','收集中'],['ready_for_review','可審核'],['needs_more_info','需補資料'],['approved_pending_delivery','待投遞'],['delivery_failed','投遞失敗'],['delivered','已送達']];
const viewablePhotoStatuses=new Set(['ready_for_review','needs_more_info','approved_pending_delivery','delivery_failed']);
const completenessLabels={{qualified:'符合',incomplete:'未符合'}};
function node(tag,text,klass){{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(klass)el.className=klass;return el;}}
function cancelApiRequests(){{for(const controller of apiControllers)controller.abort();apiControllers.clear();}}
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
function nutritionItem(title,value){{
  const item=node('span',undefined,'nutrition-item');
  item.append(node('span',`${{title}}：`),node('span',value,'nutrition-value'));
  return item;
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
  let timeoutId;
  let timedOut=false;
  const timeout=new Promise((resolve,reject)=>{{timeoutId=setTimeout(()=>{{timedOut=true;reject(new Error('照片讀取逾時'));controller.abort();}},REQUEST_TIMEOUT_MS);}});
  const cancelled=new Promise((resolve,reject)=>controller.signal.addEventListener('abort',()=>reject(new Error('照片讀取已取消')),{{once:true}}));
  photoControllers.add(controller);
  photoRequests.set(panel,request);
  button.disabled=true;
  try{{
    const path='/api/dietitian/health-checks/'+encodeURIComponent(caseId)+'/sources/'+encodeURIComponent(logId)+'/image';
    const response=await Promise.race([fetch(path,{{method:'GET',headers:{{Authorization:'Bearer '+idToken}},cache:'no-store',credentials:'omit',signal:controller.signal}}),timeout,cancelled]);
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
    const blob=await Promise.race([response.blob(),timeout,cancelled]);
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
    if((timedOut||!controller.signal.aborted)&&generation===photoGeneration)panel.replaceChildren(node('p','照片暫時無法載入，請重試','photo-message'));
  }}finally{{
    clearTimeout(timeoutId);
    photoControllers.delete(controller);
    if(photoRequests.get(panel)===request){{
      photoRequests.delete(panel);
      button.disabled=false;
    }}
  }}
}}
function renderSource(caseId,caseStatus,log,index,bindingText='日期／餐別尚無可驗證資料',warningText='',allowPhoto=true){{
  const item=node('li',undefined,'meal-card');
  const isAi=log.trust_type==='user_confirmed_ai_estimate';
  const isApproved=log.approval_status==='approved';
  const label=isAi?'顧客確認・AI估算，非營養師核准':isApproved?'營養師核准':'可驗證營養快照（未標示營養師核准）';
  item.append(node('strong',`餐點紀錄 ${{index+1}}`));
  item.append(node('div',label,'meta'));
  if(warningText)item.append(node('p',warningText,'source-warning'));
  if(isAi){{
    const estimate=log.estimate||{{}};
    if(log.estimate_schema_version==='meal-photo-user-confirmation-v2'){{
      item.append(node('div',`${{nutritionEstimateText(estimate.calories_kcal,estimate.calories_kcal_range,' kcal')}}｜蛋白質${{nutritionEstimateText(estimate.protein_g,estimate.protein_g_range,'g')}}`,'meta'));
      const nutrition=node('div',undefined,'nutrition');
      nutrition.append(nutritionItem('脂肪',nutritionValue(log.nutrition_snapshot,'fat_g','g')),nutritionItem('碳水化合物',nutritionValue(log.nutrition_snapshot,'carbohydrate_g','g')),nutritionItem('膳食纖維',nutritionValue(log.nutrition_snapshot,'fiber_g','g')),nutritionItem('鈉',nutritionValue(log.nutrition_snapshot,'sodium_mg','mg')));
      item.append(nutrition);
    }}else item.append(node('div',`熱量：NA｜蛋白質：${{rangeText(estimate.protein_total_exchange)}}｜主食：${{rangeText(estimate.starch_exchange)}}｜蔬菜：${{rangeText(estimate.vegetable_exchange)}}`,'meta'));
  }}else{{
    const snapshot=log.nutrition_snapshot||{{}};
    const nutrition=node('div',undefined,'nutrition');
    for(const [title,key,unit] of [['熱量','calories_kcal','kcal'],['蛋白質','protein_g','g'],['脂肪','fat_g','g'],['碳水化合物','carbohydrate_g','g'],['膳食纖維','fiber_g','g'],['鈉','sodium_mg','mg']])nutrition.append(nutritionItem(title,nutritionValue(snapshot,key,unit)));
    item.append(nutrition);
  }}
  const technical=node('details');
  technical.append(node('summary','技術資料'),node('div',bindingText,'meta'),node('pre',JSON.stringify({{log_id:log.log_id,food_log_version:log.food_log_version,nutrition_snapshot:log.nutrition_snapshot||{{}}}},null,2)));
  item.append(technical);
  const actions=node('div',undefined,'photo-actions');
  const panel=node('div',undefined,'photo-preview');
  if(allowPhoto&&viewablePhotoStatuses.has(caseStatus)){{
    const view=node('button','查看照片');
    view.setAttribute('type','button');
    view.addEventListener('click',()=>loadPhoto(caseId,log.log_id,view,panel));
    actions.append(view);
  }}else if(!allowPhoto)actions.append(node('p','來源識別或重複資料異常，照片不開放','photo-message'));
  else actions.append(node('p',photoUnavailableText(caseStatus),'photo-message'));
  item.append(actions,panel);
  return item;
}}
function verifiedLocalDate(value){{
  if(typeof value!=='string'||!/^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$/.test(value))return false;
  const parsed=new Date(value+'T00:00:00Z');
  return Number.isFinite(parsed.getTime())&&parsed.toISOString().slice(0,10)===value;
}}
function completeTimelineBinding(log){{
  if(!log||typeof log!=='object'||Array.isArray(log))return null;
  const hasDate=Object.prototype.hasOwnProperty.call(log,'local_date');
  const hasSlot=Object.prototype.hasOwnProperty.call(log,'normalized_meal_slot');
  const slot=hasSlot&&typeof log.normalized_meal_slot==='string'?log.normalized_meal_slot.trim():'';
  return hasDate&&hasSlot&&verifiedLocalDate(log.local_date)&&slot?{{date:log.local_date,slot}}:null;
}}
function mealSlotPresentation(slot){{
  const normalized=slot.toLocaleLowerCase('en-US');
  const known={{'早餐':[0,'早餐'],'breakfast':[0,'早餐'],'午餐':[1,'午餐'],'lunch':[1,'午餐'],'晚餐':[2,'晚餐'],'dinner':[2,'晚餐'],'點心':[3,'點心'],'snack':[3,'點心']}};
  if(Object.prototype.hasOwnProperty.call(known,normalized))return {{rank:known[normalized][0],label:known[normalized][1],key:normalized}};
  if(normalized==='unspecified')return {{rank:5,label:'餐別未提供',key:normalized}};
  return {{rank:4,label:slot,key:slot}};
}}
function compareMealSlots(a,b){{
  const left=mealSlotPresentation(a),right=mealSlotPresentation(b);
  return left.rank-right.rank||(left.key<right.key?-1:left.key>right.key?1:0);
}}
function canonicalSource(value){{
  if(Array.isArray(value))return value.map(canonicalSource);
  if(value&&typeof value==='object'){{const result={{}};for(const key of Object.keys(value).sort())result[key]=canonicalSource(value[key]);return result;}}
  return value;
}}
function sourceFingerprint(log){{return JSON.stringify(canonicalSource(log));}}
function sourceIdentity(log){{
  if(!log||typeof log!=='object'||Array.isArray(log)||typeof log.log_id!=='string'||!log.log_id.trim()||!Number.isInteger(log.food_log_version)||log.food_log_version<1)return null;
  return `${{log.log_id}}\u0000${{log.food_log_version}}`;
}}
function renderSourceConflict(index,identityText){{
  const item=node('li',undefined,'meal-card source-conflict');
  item.append(node('strong',`餐點紀錄 ${{index+1}}`),node('p','來源資料衝突：相同來源識別有互相矛盾的完整內容；未顯示營養或照片。','source-warning'));
  const technical=node('details');technical.append(node('summary','技術資料'),node('pre',JSON.stringify({{source_identity:identityText}},null,2)));item.append(technical);
  return item;
}}
function renderTimeline(caseId,caseStatus,sourceLogs){{
  const tabs=document.getElementById('timelineTabs');
  const days=document.getElementById('timelineDays');
  const legacy=document.getElementById('legacySources');
  const legacyList=document.getElementById('sourceLogs');
  tabs.replaceChildren();days.replaceChildren();legacyList.replaceChildren();
  const byDate=new Map();const unbound=[];const identityGroups=new Map();
  sourceLogs.forEach((rawLog,index)=>{{
    const log=rawLog&&typeof rawLog==='object'&&!Array.isArray(rawLog)?rawLog:{{}};
    const identity=sourceIdentity(log);
    if(identity===null){{unbound.push({{kind:'source',log,index,warning:'來源識別不完整，未套用去重且不作日期／餐別推定。',allowPhoto:typeof log.log_id==='string'&&Boolean(log.log_id.trim())}});return;}}
    if(!identityGroups.has(identity))identityGroups.set(identity,[]);
    identityGroups.get(identity).push({{log,index,fingerprint:sourceFingerprint(log),binding:completeTimelineBinding(log)}});
  }});
  for(const [identity,entries] of identityGroups){{
    const unique=[...new Map(entries.map(entry=>[entry.fingerprint,entry])).values()];
    const complete=unique.filter(entry=>entry.binding);
    if(complete.length>1||(!complete.length&&unique.length>1)){{unbound.push({{kind:'conflict',index:entries[0].index,identity}});continue;}}
    const selected=complete.length===1?complete[0]:unique[0];
    const warning=unique.length>1?'重複來源資料不一致；僅顯示具完整日期／餐別綁定的整筆來源，未合併欄位。':'';
    const record={{kind:'source',log:selected.log,index:selected.index,warning,allowPhoto:unique.length===1}};
    if(!selected.binding){{unbound.push(record);continue;}}
    const {{date,slot}}=selected.binding;
    if(!byDate.has(date))byDate.set(date,new Map());
    const groups=byDate.get(date);if(!groups.has(slot))groups.set(slot,[]);groups.get(slot).push(record);
  }}
  const dates=[...byDate.keys()].sort();
  function showDate(selected){{
    clearAllPhotos();days.replaceChildren();
    for(const button of tabs.children)button.setAttribute('aria-selected',String(button.textContent===selected));
    const groups=byDate.get(selected);
    for(const slot of [...groups.keys()].sort(compareMealSlots)){{
      const slotLabel=mealSlotPresentation(slot).label;
      const section=node('section',undefined,'meal-group');
      section.append(node('h3',slotLabel));
      const list=node('ul',undefined,'days');
      for(const record of groups.get(slot))list.append(renderSource(caseId,caseStatus,record.log,record.index,`來源綁定：${{selected}}｜${{slotLabel}}`,record.warning,record.allowPhoto));
      section.append(list);days.append(section);
    }}
  }}
  for(const date of dates){{const button=node('button',date);button.setAttribute('type','button');button.setAttribute('role','tab');button.addEventListener('click',()=>showDate(date));tabs.append(button);}}
  if(dates.length)showDate(dates[0]);else days.append(node('p','目前沒有具可驗證日期與餐別綁定的來源。','empty'));
  legacy.hidden=unbound.length===0;
  for(const record of unbound)legacyList.append(record.kind==='conflict'?renderSourceConflict(record.index,record.identity):renderSource(caseId,caseStatus,record.log,record.index,'日期／餐別尚無可驗證資料',record.warning,record.allowPhoto));
}}
async function api(path){{
  const controller=new AbortController();
  apiControllers.add(controller);
  let timeoutId;
  const timeout=new Promise((resolve,reject)=>{{timeoutId=setTimeout(()=>{{reject(new Error('讀取逾時，請重試'));controller.abort();}},REQUEST_TIMEOUT_MS);}});
  const cancelled=new Promise((resolve,reject)=>controller.signal.addEventListener('abort',()=>reject(new Error('讀取已取消')),{{once:true}}));
  try{{
    const response=await Promise.race([fetch(path,{{method:'GET',headers:{{Authorization:`Bearer ${{idToken}}`}},cache:'no-store',credentials:'omit',signal:controller.signal}}),timeout,cancelled]);
    if(!response.ok)throw new Error(response.status===403?'此LINE帳號未獲營養師唯讀權限':response.status===401?'LINE身分驗證失敗':`讀取失敗（${{response.status}}）`);
    return await Promise.race([response.json(),timeout,cancelled]);
  }}finally{{
    clearTimeout(timeoutId);
    apiControllers.delete(controller);
  }}
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
function provided(value){{return value===null||value===undefined||value===''?'未提供':String(value);}}
function profileValue(item,key){{const profile=item&&item.profile;return profile&&typeof profile==='object'?provided(profile[key]):'未提供';}}
function waitingText(item){{
  if(!item||typeof item!=='object')return '案件狀態資料不可用｜尚無後端送出時間';
  const sentAt=item.submitted_at?`送出時間：${{taipeiTimestamp(item.submitted_at)}}`:'尚無後端送出時間';
  if(item.status==='approved_pending_delivery')return `等待投遞｜${{sentAt}}`;
  if(item.status==='delivery_failed')return `投遞失敗｜${{sentAt}}`;
  if(item.status==='delivered')return `案件已送達｜${{sentAt}}`;
  if(item.status==='expired')return `案件已過期｜${{sentAt}}`;
  if(item.status==='cancelled')return `案件已取消｜${{sentAt}}`;
  if(item.status==='needs_more_info')return `等待補充資料｜${{sentAt}}`;
  if(item.submitted_at)return `已送出，等待處理｜${{taipeiTimestamp(item.submitted_at)}}`;
  return item.status==='collecting'?'尚在收集資料｜尚無後端送出時間':'尚無後端送出時間';
}}
function renderQueue(){{
  filtersNode.replaceChildren();
  for(const [value,label] of statusTabs){{
    const count=listedCases.filter(item=>value==='all'||item.status===value).length;
    const button=node('button',label);
    button.setAttribute('type','button');button.setAttribute('aria-pressed',String(selectedStatus===value));
    button.append(node('span',String(count)));
    button.addEventListener('click',()=>{{selectedStatus=value;renderQueue();}});
    filtersNode.append(button);
  }}
  const query=String(searchNode.value||'').trim().toLocaleLowerCase('zh-TW');
  const items=listedCases.filter(item=>{{
    if(selectedStatus!=='all'&&item.status!==selectedStatus)return false;
    const haystack=[item.case_id,profileValue(item,'name'),profileValue(item,'goal')].join(' ').toLocaleLowerCase('zh-TW');
    return !query||haystack.includes(query);
  }});
  casesNode.replaceChildren();
  for(const item of items){{
    const row=node('button',undefined,'case-row');row.setAttribute('type','button');
    const person=node('span',undefined,'person');person.append(node('strong',profileValue(item,'name')),node('small',`目標：${{profileValue(item,'goal')}}`));
    const records=node('span',`${{statusLabels[item.status]||provided(item.status)}}｜有效日 ${{isNonnegativeInteger(item.valid_day_count)?item.valid_day_count:'未提供'}} / 3`,'records');
    const limitation=node('span',waitingText(item),'missing');
    row.append(person,records,limitation,node('span','開啟案件 →','row-action'));
    row.addEventListener('click',()=>openCase(item));casesNode.append(row);
  }}
  if(items.length===0)casesNode.append(node('div',listedCases.length===0?'此頁目前沒有案件。':'此頁沒有符合篩選的案件。','empty'));
  queueCountNode.textContent=`目前顯示 ${{items.length}} 筆（此頁載入 ${{listedCases.length}} 筆，上限 25）`;
}}
function addProfile(label,value){{const wrap=node('div');wrap.append(node('dt',label),node('dd',provided(value)));document.getElementById('profile').append(wrap);}}
function reviewText(value){{return typeof value==='string'&&value.length?value:'NA';}}
function reviewTextOrList(value){{
  if(typeof value==='string'&&value.length)return value;
  if(Array.isArray(value)&&value.length&&value.every(item=>typeof item==='string'&&item.length))return value.join('、');
  return 'NA';
}}
function readableReview(review){{
  if(!review||typeof review!=='object'||Array.isArray(review))return '尚無可用的最新審核';
  const status={{draft:'草稿',approved:'已核准',superseded:'已取代'}}[review.status]||'NA';
  const values=[
    `審核版本：${{Number.isInteger(review.review_version)&&review.review_version>0?review.review_version:'NA'}}`,
    `審核狀態：${{status}}`,
    `核准時間：${{review.approved_at===null||review.approved_at===undefined?'未提供':taipeiTimestamp(review.approved_at)}}`,
    `建立時間：${{taipeiTimestamp(review.created_at)}}`,
    `更新時間：${{taipeiTimestamp(review.updated_at)}}`,
    `資料限制：${{reviewText(review.limitations)}}`,
  ];
  const ai=review.ai_observations;
  if(!ai||typeof ai!=='object'||Array.isArray(ai))values.push('AI 審核觀察：NA');
  else for(const [key,label] of [
    ['pattern','AI 模式'],['patterns','AI 模式列表'],['observation','AI 觀察'],
    ['observations','AI 觀察列表'],['strengths','AI 優勢'],['gaps','AI 缺口'],['risks','AI 風險'],
  ])values.push(`${{label}}：${{key==='pattern'||key==='observation'?reviewText(ai[key]):reviewTextOrList(ai[key])}}`);
  const payload=review.review;
  if(!payload||typeof payload!=='object'||Array.isArray(payload))values.push('審核內容：NA');
  else for(const [key,label] of [
    ['good','做得好的地方'],['priority','優先調整'],['summary','審核摘要'],
    ['strengths','審核優勢'],['improvements','改善項目'],['recommendations','建議'],
  ])values.push(`${{label}}：${{['good','priority','summary'].includes(key)?reviewText(payload[key]):reviewTextOrList(payload[key])}}`);
  const suggested=review.suggested_values;
  if(!suggested||typeof suggested!=='object'||Array.isArray(suggested))values.push('建議數值：NA');
  else for(const [key,label,unit] of [
    ['calories_kcal','建議熱量','kcal'],['protein_g','建議蛋白質','g'],['fat_g','建議脂肪','g'],
    ['carbohydrate_g','建議碳水化合物','g'],['fiber_g','建議膳食纖維','g'],['sodium_mg','建議鈉','mg'],
    ['vegetable_servings','建議蔬菜','份'],['water_ml','建議飲水','ml'],
  ])values.push(`${{label}}：${{Number.isFinite(suggested[key])?`${{suggested[key]}} ${{unit}}`:'NA'}}`);
  return values.join('｜');
}}
function renderDetail(item,detail){{
  const currentStatus=detail.status;
  document.getElementById('caseName').textContent=profileValue(detail,'name');
  document.getElementById('caseSubtitle').textContent=`案件 ${{provided(item.case_id)}}｜開始：${{taipeiTimestamp(item.window_started_at)}}｜截止：${{taipeiTimestamp(item.window_ends_at)}}`;
  document.getElementById('caseStatus').textContent=statusLabels[currentStatus];
  const profile=document.getElementById('profile');profile.replaceChildren();
  addProfile('姓名',profileValue(detail,'name'));addProfile('目標',profileValue(detail,'goal'));addProfile('TDEE',profileValue(detail,'tdee'));addProfile('蛋白質目標',profileValue(detail,'protein'));addProfile('活動日',profileValue(detail,'active_days'));addProfile('飲食限制',profileValue(detail,'restrictions'));
  document.getElementById('limitations').textContent=`飲食限制：${{profileValue(detail,'restrictions')}}。缺值一律顯示未提供；不以缺值推定沒有。`;
  const validDays=validatedValidDays(detail);const validDayCount=validatedValidDayCount(item,detail,validDays);const days=document.getElementById('validDays');days.replaceChildren();
  days.append(node('li',validDayCount===null?'有效日：資料不可用':`有效日：${{validDayCount}} / 3`));
  if(validDays===null)days.append(node('li','有效日期：資料不可用'));else if(validDays.length===0)days.append(node('li','目前尚無可列入的日期'));
  for(const day of validDays||[])days.append(node('li',`${{day.local_date}}｜${{day.qualifying_meal_count}} 餐｜${{completenessLabels[day.completeness_status]||'資料不可用'}}`));
  const rawSourceLogs=detail&&detail.source_logs;const sourceLogs=Array.isArray(rawSourceLogs)?rawSourceLogs:[];const sourceCounts=validatedSourceCounts(detail,rawSourceLogs);
  const integrity=document.getElementById('sourceIntegrity');integrity.replaceChildren(node('p',sourceCounts===null?'保留來源參照：資料不可用':`保留來源參照：${{sourceCounts.referenced}} 筆`),node('p',sourceCounts===null?'目前可驗證快照：資料不可用':`目前可驗證快照：${{sourceCounts.available}} 筆`));
  const sources=document.getElementById('sourceLogs');sources.replaceChildren();
  renderTimeline(item.case_id,currentStatus,sourceLogs);
  if(sourceCounts===null&&sourceLogs.length===0){{document.getElementById('legacySources').hidden=false;sources.append(node('li','來源快照清單資料不可用'));}}else if(sourceCounts&&sourceCounts.available===0){{document.getElementById('legacySources').hidden=false;sources.append(node('li','目前無可顯示的來源快照；不代表沒有飲食紀錄'));}}
  const latest=document.getElementById('latestReview');const reviewVerified=detail&&detail.latest_review_fresh===true&&detail.latest_review_available===true;
  latest.replaceChildren(node('p',reviewVerified?'新鮮度：與目前來源一致':'新鮮度：沒有可證明與目前來源一致的審核'),node('p',readableReview(reviewVerified?detail.latest_review:null)));
}}
async function openCase(item){{
  clearAllPhotos();activeCaseId=item.case_id;queueNode.hidden=true;reviewNode.hidden=false;statusNode.textContent='';
  const stored=detailsByCase.get(item.case_id);
  if(!stored||stored.status!=='fulfilled'){{
    for(const id of ['profile','limitations','validDays','sourceIntegrity','timelineTabs','timelineDays','sourceLogs','latestReview'])document.getElementById(id).replaceChildren();
    document.getElementById('legacySources').hidden=true;
    document.getElementById('caseName').textContent=profileValue(item,'name');document.getElementById('caseSubtitle').textContent=`案件 ${{provided(item.case_id)}}`;
    document.getElementById('caseStatus').textContent=statusLabels[item.status]||provided(item.status);
    const rejected=stored&&stored.status==='rejected';
    document.getElementById('latestReview').append(node('div',rejected&&stored.reason&&stored.reason.message||'案件明細載入中…',rejected?'error':'empty'));return;
  }}
  renderDetail(item,stored.value);
}}
function validatedDetail(item,detail){{
  if(!detail||typeof detail!=='object'||Array.isArray(detail))throw new Error('案件明細資料不可用');
  if(detail.case_id!==item.case_id)throw new Error('案件明細識別不一致');
  if(!Object.prototype.hasOwnProperty.call(statusLabels,detail.status))throw new Error('案件明細狀態不可用');
  return detail;
}}
async function hydrateDetail(item,generation){{
  try{{
    const detail=validatedDetail(item,await api('/api/dietitian/health-checks/'+encodeURIComponent(item.case_id)));
    if(generation!==appGeneration)return;
    detailsByCase.set(item.case_id,{{status:'fulfilled',value:detail}});
    item.status=detail.status;
    renderQueue();
    if(activeCaseId===item.case_id)renderDetail(item,detail);
  }}catch(error){{
    if(generation!==appGeneration)return;
    detailsByCase.set(item.case_id,{{status:'rejected',reason:error}});
    if(activeCaseId===item.case_id)openCase(item);
  }}
}}
async function loadCases(){{
  const generation=++appGeneration;cancelApiRequests();clearAllPhotos();activeCaseId=null;queueNode.hidden=false;reviewNode.hidden=true;refreshButton.disabled=true;casesNode.replaceChildren();statusNode.textContent='讀取真實案件中…';
  try{{
    const listing=await api('/api/dietitian/health-checks?limit=25&offset=0');
    if(generation!==appGeneration)return;
    listedCases=Array.isArray(listing.items)?listing.items:[];detailsByCase=new Map();
    for(const item of listedCases)detailsByCase.set(item.case_id,{{status:'pending'}});
    renderQueue();
    statusNode.textContent=`已更新 ${{new Date().toLocaleString('zh-TW')}}；此頁載入 ${{listedCases.length}} 筆（最多 25 筆）`;
    for(const item of listedCases)hydrateDetail(item,generation);
  }}catch(error){{if(generation===appGeneration){{listedCases=[];renderQueue();casesNode.replaceChildren(node('div',error.message||'無法讀取案件','error'));statusNode.textContent='讀取失敗';}}}}
  finally{{if(generation===appGeneration)refreshButton.disabled=false;}}
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
searchNode.addEventListener('input',renderQueue);
document.getElementById('backQueue').addEventListener('click',()=>{{clearAllPhotos();activeCaseId=null;reviewNode.hidden=true;queueNode.hidden=false;}});
function mobileTab(value){{document.getElementById('reviewGrid').dataset.mobile=value;for(const key of ['evidence','review'])document.getElementById(key+'Tab').setAttribute('aria-selected',String(key===value));}}
document.getElementById('evidenceTab').addEventListener('click',()=>mobileTab('evidence'));
document.getElementById('reviewTab').addEventListener('click',()=>mobileTab('review'));
window.addEventListener('pagehide',()=>{{appGeneration+=1;activeCaseId=null;cancelApiRequests();clearAllPhotos();}});
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
