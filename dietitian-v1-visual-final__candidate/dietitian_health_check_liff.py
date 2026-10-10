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
  <meta name="theme-color" content="#f5f4ef"><title>一日樂食｜營養師三日健檢工作台</title>
  <style>
    :root{--bg:#f5f4ef;--paper:#fffdf8;--ink:#24312c;--muted:#708078;--green:#216a55;--green2:#e4f1eb;--line:#dfe4df;--amber:#a86516;--sand:#fbefd9;--red:#a8473c;font-family:"PingFang TC","Microsoft JhengHei",sans-serif;color:var(--ink);background:var(--bg)}
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg)}body{padding-bottom:calc(104px + env(safe-area-inset-bottom))}button,input,textarea{font:inherit}button{cursor:pointer}[hidden]{display:none!important}.shell{width:min(100%,1440px);min-height:100vh;margin:auto}.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;padding:13px 16px;background:rgba(245,244,239,.96);border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}.brand{display:flex;align-items:center;gap:10px;font-weight:800}.brand::before{content:"日";display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:var(--green);color:white}.brand small{display:block;font-size:11px;font-weight:400;color:var(--muted)}#refresh{min-height:36px;border:1px solid #c7d7cf;border-radius:999px;background:#edf5f1;color:var(--green);padding:5px 10px;font-size:11px;font-weight:800}main{padding:16px}h1,h2,h3,p{margin-top:0}.heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.heading>div{min-width:0}.heading h1{font-size:25px;margin:0 0 5px}.sub,.meta,small{color:var(--muted)}.sub{font-size:13px}.eyebrow{display:none}#status{font-size:12px;color:var(--muted)}
    .metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0}.metric{min-height:106px;padding:13px 11px;border:1px solid var(--line);border-radius:17px;background:var(--paper)}.metric span{font-size:11px;color:var(--muted)}.metric b{display:block;margin:8px 0 6px;font-size:26px}.metric.wait b{color:var(--amber)}.metric.more b{color:#376d82}.metric.done b{color:var(--green)}.filters{display:flex;gap:7px;overflow:auto;margin:0 -16px 14px;padding:0 16px;scrollbar-width:none}.filters button{flex:none;min-height:42px;padding:0 15px;border:1px solid var(--line);border-radius:999px;background:var(--paper);color:var(--muted);font-weight:800}.filters button[aria-pressed="true"]{border-color:var(--green);background:var(--green);color:white}.filters button span{margin-left:6px}.search{display:flex;margin-bottom:13px}.search span{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}.search input{width:100%;min-height:44px;border:1px solid var(--line);border-radius:12px;background:var(--paper);padding:10px 12px}.case-list{display:grid;gap:11px}.case-row{width:100%;display:grid;grid-template-columns:1fr auto;gap:10px;text-align:left;padding:15px;border:1px solid var(--line);border-radius:18px;background:var(--paper);color:var(--ink)}.person{min-width:0}.person strong{display:block;font-size:16px}.person small{display:block;font-size:12px}.records{grid-column:1/-1;padding:9px;border-radius:10px;background:#f2f3ef;color:var(--green);font-size:12px;font-weight:800}.missing{grid-column:1/-1;padding-top:10px;border-top:1px solid var(--line);color:#506159;font-size:12px;line-height:1.5}.row-action{font-size:11px;color:var(--amber);white-space:nowrap}.queue-foot{margin:15px 0;padding:12px;border:1px dashed #bcc9c2;border-radius:14px;color:var(--muted);font-size:11px;line-height:1.55}.queue-foot span{display:block}.empty{padding:30px 18px;text-align:center;border:1px dashed var(--line);border-radius:18px;color:var(--muted)}
    .back{min-height:44px;margin:-5px 0 4px;padding:8px 2px;border:0;background:transparent;color:var(--green);font-weight:800}.pill{display:inline-block;flex:0 0 auto;min-width:max-content;white-space:nowrap;padding:6px 9px;border-radius:999px;background:var(--sand);color:var(--amber);font-size:11px;font-weight:800}.mobile-case-context{position:sticky;top:65px;z-index:18;display:flex;justify-content:space-between;gap:8px;margin:0 -16px;padding:8px 16px;background:rgba(245,244,239,.97);border-bottom:1px solid var(--line);font-size:12px}.mobile-case-context strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mobile-tabs{position:sticky;top:105px;z-index:17;display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:0 -16px 12px;padding:8px 16px;background:var(--bg)}.mobile-tabs button{min-height:40px;border:1px solid var(--line);border-radius:11px;background:var(--paper);color:var(--muted);font-weight:800}.mobile-tabs button[aria-selected="true"]{border-color:var(--green);background:var(--green);color:white}.review-grid{display:block;margin-top:13px}.review-grid[data-mobile="records"] #aiSummary,.review-grid[data-mobile="records"] #dataLimitations,.review-grid[data-mobile="records"] #reviewPane,.review-grid[data-mobile="highlights"] #detailSummary,.review-grid[data-mobile="highlights"] #mealTimeline,.review-grid[data-mobile="highlights"] #reviewPane,.review-grid[data-mobile="report"] #evidence{display:none}.panel{margin-bottom:12px;padding:15px;border:1px solid var(--line);border-radius:18px;background:var(--paper)}.panel h2{margin-bottom:12px;font-size:15px}.profile{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:0}.profile div{padding:9px;border-radius:10px;background:#f2f3ef}.profile dt{font-size:11px;color:var(--muted)}.profile dd{margin:3px 0 0;font-size:13px;font-weight:800;overflow-wrap:anywhere}.summary-note{font-size:11px;color:var(--muted)}.timeline-tabs{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(100px,1fr);gap:7px;overflow:auto;margin-bottom:11px}.timeline-tabs button{min-height:42px;border:1px solid var(--line);border-radius:11px;background:#f2f3ef;color:var(--muted);font-size:12px;font-weight:800}.timeline-tabs button[aria-selected="true"]{border-color:#a9c9ba;background:var(--green2);color:var(--green)}.days{padding-left:18px}.meal-card{list-style:none;margin:8px 0;padding:12px;border:1px solid var(--line);border-radius:13px;background:white}.meal-group h3{margin:15px 0 6px;font-size:14px}.nutrition{display:grid;grid-template-columns:1fr;gap:5px;margin-top:8px;font-size:12px}.nutrition span{min-width:0;overflow-wrap:anywhere}.nutrition-value{white-space:nowrap}.source-warning,.error{color:var(--red)}details{margin-top:10px}pre{overflow:auto;white-space:pre-wrap}.photo-actions button{min-height:40px;margin-top:9px;border:1px solid #aac8ba;border-radius:10px;background:var(--green2);color:var(--green);font-weight:800}.photo-preview img{display:block;width:100%;max-height:72vh;object-fit:contain;margin:10px auto;border-radius:12px;background:#111}.photo-preview img.expanded{position:fixed;z-index:50;inset:0;width:100vw;height:100vh;max-height:none;margin:0;border-radius:0}.photo-preview button{width:100%;min-height:44px}.photo-message{color:var(--muted);font-size:12px}.ai-box{line-height:1.65}.limitation{padding:12px;border:1px solid #eed3a3;border-radius:13px;background:var(--sand);color:#745023;font-size:12px;line-height:1.55}
    textarea{width:100%;min-height:88px;padding:11px;border:1px solid #cfd7d1;border-radius:12px;background:white;color:var(--ink);resize:vertical;line-height:1.55}.draft-field{display:block;margin:13px 0}.draft-field span{display:block;margin-bottom:6px;color:#4e5d56;font-size:12px;font-weight:800}.draft-actions{margin-top:12px}.preview-actions{position:sticky;z-index:24;bottom:calc(8px + env(safe-area-inset-bottom));display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px;border:1px solid var(--line);border-radius:14px;background:rgba(255,253,248,.97);box-shadow:0 8px 28px rgba(36,49,44,.14);backdrop-filter:blur(12px)}.preview-button{width:100%;min-height:48px;border:1px solid #aac8ba;border-radius:12px;background:var(--green2);color:var(--green);font-weight:800}.preview-actions #approveReview{border-color:var(--green);background:var(--green);color:white}.supplement-readback,.draft-preview-card{padding:13px;border-left:4px solid var(--green);background:var(--green2)}.secondary-details{margin-top:10px;color:var(--muted);font-size:11px}.secondary-details>summary{padding:6px 0;color:var(--muted);font-weight:600}.bottom-actions{position:fixed;z-index:25;left:50%;bottom:0;width:min(100%,480px);transform:translateX(-50%);display:grid;grid-template-columns:1fr 1fr 1.25fr;gap:8px;padding:10px 14px calc(10px + env(safe-area-inset-bottom));border-top:1px solid var(--line);background:rgba(255,253,248,.97);backdrop-filter:blur(12px)}.bottom-actions button{min-height:48px;padding:6px;border:1px solid var(--line);border-radius:12px;background:white;color:#58665f;font-size:13px;font-weight:800}.bottom-actions .warn{border-color:#e3c38e;background:#fffaf1;color:var(--amber)}.bottom-actions .primary{border-color:var(--green);background:var(--green);color:white}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
    @media(max-width:480px){.nutrition{grid-template-columns:1fr}.preview-actions{grid-template-columns:1fr 1fr}}@media(min-width:900px){body{padding:0}.top{padding:16px 38px}.shell{background:var(--bg)}main{padding:30px 38px 70px}.mobile-case-context,.mobile-tabs{display:none}.review-grid{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(360px,.92fr);gap:22px;align-items:stretch;height:calc(100vh - 220px);overflow:hidden}.review-grid[data-mobile] #evidence,.review-grid[data-mobile] #reviewPane,.review-grid[data-mobile] #detailSummary,.review-grid[data-mobile] #mealTimeline,.review-grid[data-mobile] #aiSummary,.review-grid[data-mobile] #dataLimitations{display:block}.review-grid>#evidence,.review-grid>#reviewPane{height:100%;min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding-bottom:86px}.nutrition{grid-template-columns:repeat(2,minmax(0,1fr))}.bottom-actions{left:auto;right:38px;width:min(43vw,590px);transform:none}.heading h1{font-size:30px}}
  </style>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js" defer></script><script src="/dietitian-health-check/app.js" defer></script>
</head><body><div class="shell"><header class="top"><div class="brand"><span>一日樂食<small>營養師工作台</small></span></div><button id="refresh" type="button">重新整理</button></header><main>
<section id="queue"><div class="heading"><div><h1>健檢案件</h1><p class="sub">先處理待審核、資料已備妥的案件</p></div></div><p id="status" role="status">正在驗證 LINE 身分…</p><div class="metrics"><div class="metric wait"><span>待營養師處理</span><b id="pendingMetric">0</b><span>此頁已載入</span></div><div class="metric more"><span>等顧客補資料</span><b id="supplementMetric">0</b><span>收到後再審核</span></div><div class="metric done"><span>已核准／完成</span><b id="completedMetric">0</b><span>投遞狀態另列</span></div></div><div id="filters" class="filters" aria-label="案件狀態篩選"></div><label class="search"><span>搜尋案件</span><input id="search" type="search" placeholder="姓名、目標或案件編號" aria-label="搜尋姓名、目標或案件編號"></label><div id="caseList" class="case-list" aria-live="polite"></div><div class="queue-foot"><span id="queueCount"></span><span>此頁最多 25 筆；未提供不代表已確認沒有。</span></div></section>
<section id="review" hidden><button id="backQueue" class="back" type="button">‹ 返回案件清單</button><div class="heading"><div><h1 id="caseName"></h1><p id="caseSubtitle" class="sub"></p></div><span id="caseStatus" class="pill"></span></div><div id="mobileCaseContext" class="mobile-case-context"><strong id="mobileCaseName">顧客未提供</strong><span id="mobileDraftState">草稿狀態讀取中</span></div><div class="mobile-tabs" role="tablist" aria-label="案件內容"><button id="recordsTab" role="tab" aria-selected="true">紀錄</button><button id="highlightsTab" role="tab" aria-selected="false">重點</button><button id="reportTab" role="tab" aria-selected="false">報告</button></div><div id="reviewGrid" class="review-grid" data-mobile="records"><div id="evidence">
<section id="detailSummary" class="panel"><h2>顧客與案件摘要</h2><dl id="profile" class="profile"></dl><ul id="validDays" class="days"></ul><div id="sourceIntegrity"></div><p class="summary-note">無可信完整度百分比時不顯示進度條；來源日期依後端資料，不推定連續三天。</p></section>
<section id="mealTimeline" class="panel"><h2>三天餐點紀錄</h2><p class="meta">按需讀取受保護照片；切換日期或離開案件會立即清除。</p><div id="timelineTabs" class="timeline-tabs" role="tablist" aria-label="餐點日期"></div><div id="timelineDays"></div><section id="legacySources" hidden><h3>日期／餐別尚無可驗證資料</h3><p class="meta">保留既有未綁定來源；不借用其他日期推斷。</p><ul id="sourceLogs" class="days"></ul></section></section>
<section id="aiSummary" class="panel"><h2>AI 初步整理</h2><div id="latestReview" class="ai-box"></div><p class="meta">只顯示後端可驗證的 AI 觀察；無資料時不生成分類次數。</p></section>
<section id="dataLimitations" class="panel"><h2>資料限制</h2><div id="limitations" class="limitation"></div></section></div>
<aside id="reviewPane"><section id="draftEditor" class="panel"><h2>營養師最後審核</h2><p class="meta">四欄文字沿用版本控管草稿；鎖版尚未送達，核准不等於送達。</p><label class="draft-field"><span>做得好的地方</span><textarea id="good" maxlength="4000"></textarea></label><label class="draft-field"><span>最優先改善</span><textarea id="priority" maxlength="4000"></textarea></label><label class="draft-field"><span>接下來 7 天的行動</span><textarea id="next_7_days" maxlength="4000"></textarea></label><label class="draft-field"><span>一句提醒</span><textarea id="comment" maxlength="4000"></textarea></label><p id="draftState" role="status"></p><p id="draftError" class="error" role="alert"></p><p id="approvalState" role="status"></p><p id="approvalError" class="error" role="alert"></p><details id="supplementEditor"><summary>補件內容</summary><p id="supplementState" role="status"></p><div id="supplementControls"><label class="draft-field"><span>補件理由</span><textarea id="supplementReason" maxlength="1000"></textarea></label><label class="draft-field"><span>需要補充的內容</span><textarea id="supplementRequiredContent" maxlength="1000"></textarea></label></div><p id="supplementError" class="error" role="alert"></p><div id="supplementReadback" class="supplement-readback" hidden></div></details></section>
<section id="draftPreview" class="panel" hidden><h2>顧客報告預覽</h2><p class="meta">草稿未送達；核准只鎖定這份已儲存且與目前來源一致的預覽，不承諾投遞已送達。</p><div id="draftPreviewContent" class="draft-preview-card"></div><div id="previewActions" class="draft-actions preview-actions"><button id="backDraftEdit" class="preview-button" type="button">返回編輯</button><button id="approveReview" class="preview-button" type="button">確認核准</button></div></section></aside></div>
<div id="bottomActions" class="bottom-actions"><button id="requestMoreInfo" class="warn" type="button">要求補件</button><button id="saveDraft" type="button">儲存草稿</button><button id="openDraftPreview" class="primary" type="button">預覽正式報告</button></div></section>
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
let selectedStatus='review';
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
const statusTabs=[['review','待審核'],['supplement','待補件'],['completed','已完成']];
function caseGroup(status){{
  if(status==='needs_more_info')return 'supplement';
  if(['approved_pending_delivery','delivery_failed','delivered','expired','cancelled'].includes(status))return 'completed';
  return 'review';
}}
const viewablePhotoStatuses=new Set(['ready_for_review','needs_more_info','approved_pending_delivery','delivery_failed']);
const completenessLabels={{qualified:'符合',incomplete:'未符合'}};
const draftKeys=['good','priority','next_7_days','comment'];
let draftBaseline={{good:'',priority:'',next_7_days:'',comment:''}};
let draftCaseId=null,draftSourceToken='',draftReviewVersion=null,draftSaving=false,draftRetry=null,draftGeneration=0;
let approvalSaving=false,approvalRetry=null;
let previewSignature='';
let supplementBaseline={{reason:'',required_content:''}},supplementSaving=false,supplementRetry=null;
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
    image.setAttribute('tabindex','0');
    image.setAttribute('aria-label','餐點照片，點擊切換放大');
    image.addEventListener('click',()=>{{image.className=image.className==='expanded'?'':'expanded';}});
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
  const technical=node('details');technical.className='secondary-details';
  technical.append(node('summary','來源明細（選用）'),node('div',bindingText,'meta'),node('pre',JSON.stringify({{log_id:log.log_id,food_log_version:log.food_log_version,nutrition_snapshot:log.nutrition_snapshot||{{}}}},null,2)));
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
  const technical=node('details');technical.className='secondary-details';technical.append(node('summary','來源明細（選用）'),node('pre',JSON.stringify({{source_identity:identityText}},null,2)));item.append(technical);
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
    for(const button of tabs.children)button.setAttribute('aria-selected',String(button.dataset.date===selected));
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
  dates.forEach((date,index)=>{{const shortDate=date.slice(5).replace('-','/');const button=node('button',`第${{index+1}}天・${{shortDate}}`);button.dataset.date=date;button.setAttribute('type','button');button.setAttribute('role','tab');button.addEventListener('click',()=>showDate(date));tabs.append(button);}});
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
function sourceLimitationTexts(detail){{
  const texts=[];
  const sourceLogs=detail&&detail.source_logs;
  const counts=validatedSourceCounts(detail,sourceLogs);
  if(counts===null)texts.push('來源完整性資料不可用，無法確認餐次對照範圍。');
  else{{
    if(counts.available<counts.referenced)texts.push(`保留 ${{counts.referenced}} 筆來源參照，目前僅有 ${{counts.available}} 筆可驗證快照。`);
    if(counts.available===0&&counts.referenced===0)texts.push('目前沒有可驗證來源快照，無法建立逐日餐次對照。');
    const unbound=sourceLogs.filter(log=>!completeTimelineBinding(log)).length;
    if(unbound>0)texts.push(`目前 ${{unbound}} 筆可驗證來源未同時綁定日期與餐別，未納入逐日餐次對照。`);
  }}
  const reviewLimit=detail&&detail.latest_review&&detail.latest_review.limitations;
  if(typeof reviewLimit==='string'&&reviewLimit.trim())texts.push(`營養師註記：${{reviewLimit.trim()}}`);
  if(texts.length===0)texts.push('目前來源均具可驗證日期與餐別綁定；營養師未另列限制。');
  return texts;
}}
function provided(value){{return value===null||value===undefined||value===''?'未提供':String(value);}}
function draftValues(){{const values={{}};for(const key of draftKeys)values[key]=document.getElementById(key).value;return values;}}
function sameDraft(left,right){{return draftKeys.every(key=>left[key]===right[key]);}}
function draftDirty(){{return draftCaseId!==null&&!sameDraft(draftValues(),draftBaseline);}}
function setDraftDisabled(disabled){{for(const key of draftKeys)document.getElementById(key).disabled=disabled;document.getElementById('saveDraft').disabled=disabled;document.getElementById('openDraftPreview').disabled=draftSaving||approvalSaving||supplementSaving;}}
function draftWritable(){{return ['ready_for_review','needs_more_info'].includes((detailsByCase.get(draftCaseId)||{{}}).value?.status)&&typeof draftSourceToken==='string'&&/^[0-9a-f]{{64}}$/.test(draftSourceToken)&&Number.isInteger(draftReviewVersion)&&draftReviewVersion>=0;}}
function approvalDraft(){{const detail=(detailsByCase.get(draftCaseId)||{{}}).value;return detail&&detail.latest_review_available===true&&detail.latest_review_fresh===true&&detail.latest_review&&detail.latest_review.status==='draft'&&detail.latest_review.review_version===draftReviewVersion?detail.latest_review:null;}}
function previewable(){{const fields=draftValues();return draftWritable()&&!draftDirty()&&!draftSaving&&!approvalSaving&&!supplementSaving&&draftKeys.every(key=>typeof fields[key]==='string'&&Boolean(fields[key].trim()))&&approvalDraft()!==null;}}
function currentPreviewSignature(){{
  const detail=(detailsByCase.get(draftCaseId)||{{}}).value;
  return JSON.stringify({{case_id:draftCaseId,source_token:draftSourceToken,review_version:draftReviewVersion,recipient:profileValue(detail,'name'),review:draftValues(),limitations:detail&&detail.latest_review&&detail.latest_review.limitations}});
}}
function approvalWritable(){{return previewable()&&previewSignature===currentPreviewSignature();}}
function updateApprovalState(){{
  const detail=(detailsByCase.get(draftCaseId)||{{}}).value;const approval=detail&&detail.approval;
  const button=document.getElementById('approveReview');button.disabled=!approvalWritable();
  document.getElementById('approvalState').textContent=approvalSaving?'核准中；正在鎖定版本並重載確認，尚未送達。':approval&&approval.status==='approved'&&approval.delivery_status==='pending'?'已核准，等待投遞（尚未送達）。':approvalWritable()?'此預覽已核對，可進行最後核准；尚未送達。':approvalDraft()&&!draftDirty()?'已存草稿；請先預覽正式報告，再進行最後核准。':draftDirty()?'有未儲存變更；請先儲存草稿。':'核准前請確認：鎖版尚未送達。';
}}
function updateDraftState(){{
  const writable=draftWritable();
  const state=draftSaving?'正在儲存並重載確認…':!writable?'此案件目前為唯讀':draftDirty()?'有未儲存變更':'草稿已同步';
  document.getElementById('draftState').textContent=state;document.getElementById('mobileDraftState').textContent=state;
  setDraftDisabled(!writable||draftSaving||approvalSaving||supplementSaving);updateApprovalState();
}}
function initializeDraft(detail){{
  draftGeneration+=1;draftCaseId=detail.case_id;draftSourceToken=typeof detail.source_token==='string'?detail.source_token:'';draftReviewVersion=Number.isInteger(detail.current_review_version)?detail.current_review_version:null;draftSaving=false;draftRetry=null;approvalSaving=false;approvalRetry=null;previewSignature='';supplementSaving=false;supplementRetry=null;
  const payload=detail.latest_review_available===true&&detail.latest_review&&detail.latest_review.review&&typeof detail.latest_review.review==='object'?detail.latest_review.review:{{}};
  draftBaseline={{}};for(const key of draftKeys){{const value=typeof payload[key]==='string'?payload[key]:'';draftBaseline[key]=value;document.getElementById(key).value=value;}}
  document.getElementById('draftError').textContent='';document.getElementById('approvalError').textContent='';document.getElementById('draftEditor').hidden=false;document.getElementById('draftPreview').hidden=true;document.getElementById('bottomActions').hidden=false;updateDraftState();initializeSupplement(detail);
}}
function draftErrorMessage(status){{
  if(status===401)return 'LINE 身分驗證失敗；不會自動切換身分。';
  if(status===403)return '此 LINE 帳號沒有草稿儲存權限；不會自動切換身分。';
  if(status===409)return '案件或審核資料已變動。草稿仍保留，請人工比較後再決定。';
  if(status===422)return '四個欄位內容不符合草稿格式。';
  return '儲存結果未知或服務暫時不可用；可用同一請求重試。';
}}
function requestId(){{return crypto.randomUUID();}}
async function saveDraft(){{
  if(draftSaving||draftCaseId!==activeCaseId||!draftWritable())return;
  const fields=draftValues();
  if(draftKeys.some(key=>!fields[key].trim())){{document.getElementById('draftError').textContent='請先填寫四個草稿欄位。';return;}}
  if(!draftDirty())return;
  const retry=draftRetry&&sameDraft(draftRetry.fields,fields)?draftRetry:null;
  const payload=retry?retry.payload:{{...fields,expected_source_token:draftSourceToken,expected_review_version:draftReviewVersion,request_id:requestId()}};
  draftRetry={{fields:{{...fields}},payload}};
  const ownedCase=draftCaseId,ownedGeneration=draftGeneration,ownedAppGeneration=appGeneration;
  const controller=new AbortController();apiControllers.add(controller);let timeoutId;let timedOut=false;
  const timeout=new Promise((resolve,reject)=>{{timeoutId=setTimeout(()=>{{timedOut=true;controller.abort();reject(new Error('timeout'));}},REQUEST_TIMEOUT_MS);}});
  draftSaving=true;document.getElementById('draftError').textContent='';updateDraftState();
  try{{
    const path='/api/dietitian/health-checks/'+encodeURIComponent(ownedCase)+'/reviews';
    const response=await Promise.race([fetch(path,{{method:'POST',headers:{{Authorization:'Bearer '+idToken,'Content-Type':'application/json'}},cache:'no-store',credentials:'omit',body:JSON.stringify(payload),signal:controller.signal}}),timeout]);
    if(ownedCase!==activeCaseId||ownedGeneration!==draftGeneration||ownedAppGeneration!==appGeneration)return;
    if(!response.ok){{document.getElementById('draftError').textContent=draftErrorMessage(response.status);return;}}
    const saved=await Promise.race([response.json(),timeout]);
    if(!saved||saved.status!=='draft'||!Number.isInteger(saved.review_version))throw new Error('invalid save response');
    const reloaded=validatedDetail({{case_id:ownedCase}},await api('/api/dietitian/health-checks/'+encodeURIComponent(ownedCase)));
    if(ownedCase!==activeCaseId||ownedGeneration!==draftGeneration||ownedAppGeneration!==appGeneration)return;
    const projected=reloaded.latest_review_available===true&&reloaded.latest_review&&reloaded.latest_review.review;
    if(reloaded.current_review_version!==saved.review_version||!projected||!sameDraft(projected,fields))throw new Error('saved draft not confirmed by reload');
    detailsByCase.set(ownedCase,{{status:'fulfilled',value:reloaded}});const item=listedCases.find(candidate=>candidate.case_id===ownedCase);if(item)item.status=reloaded.status;
    renderDetail(item||{{case_id:ownedCase}},reloaded);document.getElementById('draftState').textContent='草稿已儲存';
  }}catch(error){{
    if(ownedCase===activeCaseId&&ownedGeneration===draftGeneration&&ownedAppGeneration===appGeneration)document.getElementById('draftError').textContent=timedOut?draftErrorMessage(0):draftErrorMessage(error&&error.status||0);
  }}finally{{
    clearTimeout(timeoutId);apiControllers.delete(controller);
    if(ownedCase===activeCaseId&&ownedGeneration===draftGeneration&&ownedAppGeneration===appGeneration){{draftSaving=false;updateDraftState();}}
  }}
}}
function approvalErrorMessage(status){{
  if(status===409)return '案件或審核資料已變動。內容仍保留，請人工比較後再決定；不會自動改用新版重送。';
  if(status===401)return 'LINE 身分驗證失敗；不會自動切換身分。';
  if(status===403)return '此 LINE 帳號沒有核准權限。';
  if(status===422)return '核准條件不符合；內容仍保留。';
  return '核准結果未知或服務暫時不可用；可用同一請求重試。';
}}
async function approveReview(){{
  if(approvalSaving||draftCaseId!==activeCaseId)return;
  const fields=draftValues();
  if(draftDirty()){{document.getElementById('approvalError').textContent='有未儲存變更，請先儲存草稿；本次未核准。';return;}}
  if(!approvalWritable()){{document.getElementById('approvalError').textContent='必須先預覽四欄完整、已存且可驗證的新鮮草稿；本次未核准。';return;}}
  if(!window.confirm('確認核准並鎖定目前已存草稿？核准後等待投遞，尚未送達。'))return;
  const retry=approvalRetry&&approvalRetry.sourceToken===draftSourceToken&&approvalRetry.reviewVersion===draftReviewVersion?approvalRetry:null;
  const payload=retry?retry.payload:{{expected_source_token:draftSourceToken,expected_review_version:draftReviewVersion,request_id:requestId()}};
  approvalRetry={{sourceToken:draftSourceToken,reviewVersion:draftReviewVersion,payload}};
  const ownedCase=draftCaseId,ownedGeneration=draftGeneration,ownedAppGeneration=appGeneration;
  const controller=new AbortController();apiControllers.add(controller);let timeoutId;let timedOut=false;
  const timeout=new Promise((resolve,reject)=>{{timeoutId=setTimeout(()=>{{timedOut=true;controller.abort();reject(new Error('timeout'));}},REQUEST_TIMEOUT_MS);}});
  approvalSaving=true;document.getElementById('approvalError').textContent='';updateDraftState();
  try{{
    const path='/api/dietitian/health-checks/'+encodeURIComponent(ownedCase)+'/reviews/approve';
    const response=await Promise.race([fetch(path,{{method:'POST',headers:{{Authorization:'Bearer '+idToken,'Content-Type':'application/json'}},cache:'no-store',credentials:'omit',body:JSON.stringify(payload),signal:controller.signal}}),timeout]);
    if(ownedCase!==activeCaseId||ownedGeneration!==draftGeneration||ownedAppGeneration!==appGeneration)return;
    if(!response.ok){{document.getElementById('approvalError').textContent=approvalErrorMessage(response.status);return;}}
    const result=await Promise.race([response.json(),timeout]);
    if(!result||typeof result.report_id!=='string'||!result.report_id||result.delivery_status!=='pending')throw new Error('invalid approval response');
    const reloaded=validatedDetail({{case_id:ownedCase}},await api('/api/dietitian/health-checks/'+encodeURIComponent(ownedCase)));
    if(ownedCase!==activeCaseId||ownedGeneration!==draftGeneration||ownedAppGeneration!==appGeneration)return;
    if(reloaded.status!=='approved_pending_delivery'||!reloaded.latest_review||reloaded.latest_review.status!=='approved'||!reloaded.approval||reloaded.approval.report_id!==result.report_id||reloaded.approval.status!=='approved'||reloaded.approval.delivery_status!=='pending')throw new Error('approval not confirmed by reload');
    detailsByCase.set(ownedCase,{{status:'fulfilled',value:reloaded}});const item=listedCases.find(candidate=>candidate.case_id===ownedCase);if(item)item.status=reloaded.status;
    renderDetail(item||{{case_id:ownedCase}},reloaded);
  }}catch(error){{if(ownedCase===activeCaseId&&ownedGeneration===draftGeneration&&ownedAppGeneration===appGeneration)document.getElementById('approvalError').textContent=approvalErrorMessage(timedOut?0:error&&error.status||0);}}
  finally{{clearTimeout(timeoutId);apiControllers.delete(controller);if(ownedCase===activeCaseId&&ownedGeneration===draftGeneration&&ownedAppGeneration===appGeneration){{approvalSaving=false;updateDraftState();}}}}
}}
function supplementValues(){{return {{reason:document.getElementById('supplementReason').value,required_content:document.getElementById('supplementRequiredContent').value}};}}
function sameSupplement(left,right){{return left.reason===right.reason&&left.required_content===right.required_content;}}
function supplementDirty(){{return draftCaseId!==null&&(detailsByCase.get(draftCaseId)||{{}}).value?.status==='ready_for_review'&&!sameSupplement(supplementValues(),supplementBaseline);}}
function supplementWritable(){{return (detailsByCase.get(draftCaseId)||{{}}).value?.status==='ready_for_review'&&approvalDraft()!==null&&!draftDirty()&&!draftSaving&&!approvalSaving&&!supplementSaving;}}
function setSupplementDisabled(disabled){{document.getElementById('supplementReason').disabled=disabled;document.getElementById('supplementRequiredContent').disabled=disabled;document.getElementById('requestMoreInfo').disabled=disabled;}}
function supplementErrorMessage(status){{
  if(status===401)return 'LINE 身分驗證失敗；不會自動切換身分。';
  if(status===403)return '此 LINE 帳號沒有提出補件的權限。';
  if(status===409)return '案件或審核資料已變動。補件輸入仍保留，請人工比較；不會自動改用新版重送。';
  if(status===422)return '補件理由或需要補充的內容不符合格式。';
  return '補件結果未知或服務暫時不可用；可用同一請求重試。';
}}
function notificationPair(pending){{
  if(!pending||typeof pending.notification_status!=='string'||typeof pending.notified!=='boolean')return null;
  if(pending.notification_status==='not_sent'&&pending.notified===false)return 'not_sent';
  if(pending.notification_status==='queued'&&pending.notified===false)return 'queued';
  if(pending.notification_status==='delivered'&&pending.notified===true)return 'delivered';
  return null;
}}
function updateSupplementState(){{
  const detail=(detailsByCase.get(draftCaseId)||{{}}).value;
  const pending=detail&&detail.status==='needs_more_info'&&detail.supplement_request;
  const notification=notificationPair(pending);
  const resolved=detail&&detail.status==='ready_for_review'&&detail.latest_review_fresh===false;
  document.getElementById('supplementState').textContent=supplementSaving?'正在提交並重載確認；通知狀態以重載結果為準。':notification==='delivered'?'案件待補件；已通知顧客。':notification==='queued'?'案件待補件；通知處理中，尚未確認送達。':notification==='not_sent'?'案件待補件；尚未 LINE 通知。':resolved?'顧客已補件，待重新審查。':detail&&detail.status==='ready_for_review'?'尚未提交；提交後仍尚未 LINE 通知。':'此案件目前不可提出補件。';
  setSupplementDisabled(!supplementWritable());
}}
function initializeSupplement(detail){{
  const controls=document.getElementById('supplementControls'),reason=document.getElementById('supplementReason'),required=document.getElementById('supplementRequiredContent'),readback=document.getElementById('supplementReadback');
  const pending=detail&&detail.status==='needs_more_info'&&detail.supplement_request&&typeof detail.supplement_request==='object'?detail.supplement_request:null;
  const notification=notificationPair(pending);
  const validPending=pending&&typeof pending.reason==='string'&&pending.reason.trim()&&typeof pending.required_content==='string'&&pending.required_content.trim()&&notification!==null&&typeof pending.requested_at==='string';
  const resolved=detail&&detail.status==='ready_for_review'&&detail.latest_review_fresh===false;
  controls.hidden=Boolean(resolved);reason.value=validPending?pending.reason:'';required.value=validPending?pending.required_content:'';supplementBaseline={{reason:reason.value,required_content:required.value}};
  document.getElementById('supplementError').textContent='';readback.replaceChildren();readback.hidden=true;
  if(validPending){{const heading=notification==='delivered'?'目前待補件（已通知顧客）':notification==='queued'?'目前待補件（通知處理中）':'目前待補件（尚未 LINE 通知）';readback.append(node('h3',heading),node('p',`補件理由：${{pending.reason}}`),node('p',`需要補充：${{pending.required_content}}`),node('p',`提出時間：${{taipeiTimestamp(pending.requested_at)}}`,'meta'));readback.hidden=false;}}
  else if(pending)document.getElementById('supplementError').textContent='待補件資料不可驗證；不顯示內容。';
  updateSupplementState();
}}
async function requestMoreInfo(){{
  if(supplementSaving||draftCaseId!==activeCaseId)return;
  const fields=supplementValues();
  if(!fields.reason.trim()||!fields.required_content.trim()){{document.getElementById('supplementError').textContent='補件理由與需要補充的內容皆為必填。';return;}}
  if(draftDirty()){{document.getElementById('supplementError').textContent='有未儲存草稿；本次不會儲存、捨棄或提交補件。';return;}}
  if(!supplementWritable()){{document.getElementById('supplementError').textContent='必須使用目前可審核案件的已存新鮮草稿；本次未提交。';return;}}
  if(!window.confirm('確認建立待補件紀錄？提交後尚未 LINE 通知顧客。'))return;
  const retry=supplementRetry&&sameSupplement(supplementRetry.fields,fields)&&supplementRetry.sourceToken===draftSourceToken&&supplementRetry.reviewVersion===draftReviewVersion?supplementRetry:null;
  const payload=retry?retry.payload:{{...fields,expected_source_token:draftSourceToken,expected_review_version:draftReviewVersion,request_id:requestId()}};
  supplementRetry={{fields:{{...fields}},sourceToken:draftSourceToken,reviewVersion:draftReviewVersion,payload}};
  const ownedCase=draftCaseId,ownedGeneration=draftGeneration,ownedAppGeneration=appGeneration;
  const controller=new AbortController();apiControllers.add(controller);let timeoutId;let timedOut=false;
  const timeout=new Promise((resolve,reject)=>{{timeoutId=setTimeout(()=>{{timedOut=true;controller.abort();reject(new Error('timeout'));}},REQUEST_TIMEOUT_MS);}});
  supplementSaving=true;document.getElementById('supplementError').textContent='';updateDraftState();updateSupplementState();
  try{{
    const path='/api/dietitian/health-checks/'+encodeURIComponent(ownedCase)+'/request-more-info';
    const response=await Promise.race([fetch(path,{{method:'POST',headers:{{Authorization:'Bearer '+idToken,'Content-Type':'application/json'}},cache:'no-store',credentials:'omit',body:JSON.stringify(payload),signal:controller.signal}}),timeout]);
    if(ownedCase!==activeCaseId||ownedGeneration!==draftGeneration||ownedAppGeneration!==appGeneration)return;
    if(!response.ok){{document.getElementById('supplementError').textContent=supplementErrorMessage(response.status);return;}}
    const result=await Promise.race([response.json(),timeout]);
    if(!result||result.status!=='needs_more_info'||result.notification_status!=='not_sent'||typeof result.created!=='boolean')throw new Error('invalid supplement response');
    const reloaded=validatedDetail({{case_id:ownedCase}},await api('/api/dietitian/health-checks/'+encodeURIComponent(ownedCase)));
    if(ownedCase!==activeCaseId||ownedGeneration!==draftGeneration||ownedAppGeneration!==appGeneration)return;
    const pending=reloaded.supplement_request;const notification=notificationPair(pending);
    if(reloaded.status!=='needs_more_info'||!pending||pending.reason!==fields.reason||pending.required_content!==fields.required_content||notification===null)throw new Error('supplement request not confirmed by reload');
    detailsByCase.set(ownedCase,{{status:'fulfilled',value:reloaded}});const item=listedCases.find(candidate=>candidate.case_id===ownedCase);if(item)item.status=reloaded.status;
    renderDetail(item||{{case_id:ownedCase}},reloaded);
  }}catch(error){{if(ownedCase===activeCaseId&&ownedGeneration===draftGeneration&&ownedAppGeneration===appGeneration)document.getElementById('supplementError').textContent=supplementErrorMessage(timedOut?0:error&&error.status||0);}}
  finally{{clearTimeout(timeoutId);apiControllers.delete(controller);if(ownedCase===activeCaseId&&ownedGeneration===draftGeneration&&ownedAppGeneration===appGeneration){{supplementSaving=false;updateDraftState();updateSupplementState();}}}}
}}
function previewDraft(){{
  const fields=draftValues();if(!previewable()){{document.getElementById('draftError').textContent=draftDirty()?'有未儲存變更，請先儲存並等待重載確認。':'只能預覽四欄完整、已存且與目前來源一致的新鮮草稿。';return;}}
  const detail=(detailsByCase.get(draftCaseId)||{{}}).value;const recipient=profileValue(detail,'name');
  const content=document.getElementById('draftPreviewContent');content.replaceChildren();
  content.append(node('h3','報告對象'),node('p',recipient==='未提供'?'顧客姓名：未提供（API 未提供可信姓名）':`顧客姓名：${{recipient}}`),node('h3','本次預覽可確認的限制'));
  const limitations=node('ul',undefined,'limitation');for(const text of sourceLimitationTexts(detail))limitations.append(node('li',text));content.append(limitations);
  for(const [key,label] of [['good','做得好的地方'],['priority','最優先改善'],['next_7_days','接下來 7 天的行動'],['comment','營養師個人點評']])content.append(node('h3',label),node('p',fields[key]));
  previewSignature=currentPreviewSignature();document.getElementById('draftEditor').hidden=true;document.getElementById('draftPreview').hidden=false;document.getElementById('bottomActions').hidden=true;updateApprovalState();
}}
function leaveWithDraftGuard(action){{if((!draftDirty()&&!supplementDirty())||window.confirm('尚有未儲存草稿或未提交的補件輸入。確定捨棄並離開？'))action();}}
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
  document.getElementById('pendingMetric').textContent=String(listedCases.filter(item=>caseGroup(item.status)==='review').length);
  document.getElementById('supplementMetric').textContent=String(listedCases.filter(item=>caseGroup(item.status)==='supplement').length);
  document.getElementById('completedMetric').textContent=String(listedCases.filter(item=>caseGroup(item.status)==='completed').length);
  filtersNode.replaceChildren();
  for(const [value,label] of statusTabs){{
    const count=listedCases.filter(item=>caseGroup(item.status)===value).length;
    const button=node('button',label);
    button.setAttribute('type','button');button.setAttribute('aria-pressed',String(selectedStatus===value));
    button.append(node('span',String(count)));
    button.addEventListener('click',()=>{{selectedStatus=value;renderQueue();}});
    filtersNode.append(button);
  }}
  const query=String(searchNode.value||'').trim().toLocaleLowerCase('zh-TW');
  const items=listedCases.filter(item=>{{
    if(caseGroup(item.status)!==selectedStatus)return false;
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
    row.addEventListener('click',()=>leaveWithDraftGuard(()=>openCase(item)));casesNode.append(row);
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
    ['good','做得好的地方'],['priority','優先調整'],['next_7_days','接下來 7 天'],
    ['comment','營養師個人點評'],['summary','審核摘要'],
    ['strengths','審核優勢'],['improvements','改善項目'],['recommendations','建議'],
  ])values.push(`${{label}}：${{['good','priority','next_7_days','comment','summary'].includes(key)?reviewText(payload[key]):reviewTextOrList(payload[key])}}`);
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
  document.getElementById('mobileCaseName').textContent=profileValue(detail,'name');
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
  document.getElementById('limitations').replaceChildren(...sourceLimitationTexts(detail).map(text=>node('p',text)));
  const integrity=document.getElementById('sourceIntegrity');integrity.replaceChildren(node('p',sourceCounts===null?'保留來源參照：資料不可用':`保留來源參照：${{sourceCounts.referenced}} 筆`),node('p',sourceCounts===null?'目前可驗證快照：資料不可用':`目前可驗證快照：${{sourceCounts.available}} 筆`));
  const sources=document.getElementById('sourceLogs');sources.replaceChildren();
  renderTimeline(item.case_id,currentStatus,sourceLogs);
  if(sourceCounts===null&&sourceLogs.length===0){{document.getElementById('legacySources').hidden=false;sources.append(node('li','來源快照清單資料不可用'));}}else if(sourceCounts&&sourceCounts.available===0){{document.getElementById('legacySources').hidden=false;sources.append(node('li','目前無可顯示的來源快照；不代表沒有飲食紀錄'));}}
  const latest=document.getElementById('latestReview');const reviewVerified=detail&&detail.latest_review_fresh===true&&detail.latest_review_available===true;
  latest.replaceChildren(node('p',reviewVerified?'新鮮度：與目前來源一致':'新鮮度：沒有可證明與目前來源一致的審核'),node('p',readableReview(reviewVerified?detail.latest_review:null)));
  initializeDraft(detail);
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
refreshButton.addEventListener('click',()=>leaveWithDraftGuard(()=>{{draftGeneration+=1;draftCaseId=null;loadCases();}}));
searchNode.addEventListener('input',renderQueue);
document.getElementById('backQueue').addEventListener('click',()=>leaveWithDraftGuard(()=>{{clearAllPhotos();draftGeneration+=1;draftCaseId=null;activeCaseId=null;reviewNode.hidden=true;queueNode.hidden=false;}}));
for(const key of draftKeys)document.getElementById(key).addEventListener('input',()=>{{draftRetry=null;approvalRetry=null;previewSignature='';document.getElementById('draftError').textContent='';document.getElementById('approvalError').textContent='';updateDraftState();}});
for(const id of ['supplementReason','supplementRequiredContent'])document.getElementById(id).addEventListener('input',()=>{{supplementRetry=null;document.getElementById('supplementError').textContent='';updateSupplementState();}});
document.getElementById('saveDraft').addEventListener('click',saveDraft);
document.getElementById('approveReview').addEventListener('click',approveReview);
document.getElementById('requestMoreInfo').addEventListener('click',requestMoreInfo);
document.getElementById('openDraftPreview').addEventListener('click',previewDraft);
document.getElementById('backDraftEdit').addEventListener('click',()=>{{document.getElementById('draftPreview').hidden=true;document.getElementById('draftEditor').hidden=false;document.getElementById('bottomActions').hidden=false;}});
function mobileTab(value){{document.getElementById('reviewGrid').dataset.mobile=value;for(const key of ['records','highlights','report'])document.getElementById(key+'Tab').setAttribute('aria-selected',String(key===value));}}
document.getElementById('recordsTab').addEventListener('click',()=>mobileTab('records'));
document.getElementById('highlightsTab').addEventListener('click',()=>mobileTab('highlights'));
document.getElementById('reportTab').addEventListener('click',()=>mobileTab('report'));
window.addEventListener('beforeunload',event=>{{if(draftDirty()||supplementDirty()){{event.preventDefault();event.returnValue='';}}}});
window.addEventListener('pagehide',()=>{{appGeneration+=1;draftGeneration+=1;draftCaseId=null;activeCaseId=null;cancelApiRequests();clearAllPhotos();}});
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
