from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import requests

from customer_health_check_supplement import (
    CustomerSupplementConflict,
    CustomerSupplementNotFound,
)


LINE_ID_TOKEN_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
LINE_ID_TOKEN_ISSUER = "https://access.line.me"
NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class LineAuthenticationError(ValueError):
    """LINE 拒絕或回傳無效 ID token；訊息不得包含 token。"""


class LineAuthenticationUnavailable(RuntimeError):
    """LINE 驗證服務暫時不可用；與顧客未授權分開處理。"""


def verify_line_id_token(
    id_token: str,
    *,
    channel_id: str,
    http_post: Callable[..., Any] = requests.post,
) -> str:
    id_token = str(id_token or "").strip()
    channel_id = str(channel_id or "").strip()
    if not id_token or len(id_token) > 8192 or not re.fullmatch(r"[0-9]{5,}", channel_id):
        raise LineAuthenticationError("LINE ID token 驗證失敗")
    try:
        response = http_post(
            LINE_ID_TOKEN_VERIFY_URL,
            data={"id_token": id_token, "client_id": channel_id},
            timeout=5,
        )
    except requests.RequestException as exc:
        raise LineAuthenticationUnavailable("LINE 驗證服務暫時無法連線") from exc
    except Exception as exc:
        raise LineAuthenticationUnavailable("LINE 驗證服務暫時無法使用") from exc
    try:
        status_code = int(response.status_code)
    except (AttributeError, TypeError, ValueError) as exc:
        raise LineAuthenticationUnavailable("LINE 驗證服務回應異常") from exc
    if status_code == 429 or status_code >= 500:
        raise LineAuthenticationUnavailable("LINE 驗證服務暫時無法使用")
    if status_code != 200:
        raise LineAuthenticationError("LINE ID token 驗證失敗")
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError) as exc:
        raise LineAuthenticationUnavailable("LINE 驗證服務回應異常") from exc
    if not isinstance(payload, Mapping):
        raise LineAuthenticationUnavailable("LINE 驗證服務回應異常")
    subject = str(payload.get("sub") or "")
    if (
        payload.get("iss") != LINE_ID_TOKEN_ISSUER
        or str(payload.get("aud") or "") != channel_id
        or not re.fullmatch(r"U[0-9a-fA-F]{32}", subject)
    ):
        raise LineAuthenticationError("LINE ID token 驗證失敗")
    return subject


def _render_customer_liff_html(
    liff_id: str, *, customer_reschedule_enabled: bool = False
) -> str:
    liff_json = json.dumps(liff_id)
    reschedule_card = ""
    reschedule_script = ""
    if customer_reschedule_enabled:
        reschedule_card = """
    <section id="reschedule" class="card" hidden><h2>申請餐點改期</h2><p>選擇目前已排餐的日期，再到下一頁選擇目標日期。</p><select id="rescheduleSource" aria-label="選擇要改期的排餐日期"></select><button id="openReschedule" type="button">前往選擇改期日期</button><p id="rescheduleState" aria-live="polite"></p></section>"""
        reschedule_script = """
async function loadRescheduleContext(){
  try{
    const response=await fetch('/customer-reschedule/context',{headers:{Authorization:'Bearer '+idToken},cache:'no-store',credentials:'omit'});
    const payload=await response.json().catch(()=>({}));
    $('reschedule').hidden=false;
    if(!response.ok){
      $('rescheduleState').textContent=response.status===401?'LINE 身份驗證失效，請關閉後重新從 LINE 開啟。':(payload.detail||'目前無法載入可改期餐表。');
      $('rescheduleSource').replaceChildren(new Option('目前無法載入日期',''));
      $('openReschedule').disabled=true;
      return;
    }
    const choices=[];
    for(const order of (payload.orders||[]))for(const source of (order.source_dates||[]))choices.push({order_id:order.order_id,source_date:source.date,label:source.label||''});
    if(!choices.length){
      $('rescheduleState').textContent=payload.detail||'此 LINE 帳號目前沒有可改期的包月排餐；請使用已開通包月且已有排餐的顧客帳號。';
      $('rescheduleSource').replaceChildren(new Option('目前沒有可改期日期',''));
      $('openReschedule').disabled=true;
      return;
    }
    $('rescheduleSource').replaceChildren(...choices.map(choice=>{const option=document.createElement('option');option.value=String(choice.order_id)+'|'+choice.source_date;option.textContent='訂單 '+choice.order_id+'・'+choice.source_date+(choice.label?'・'+choice.label:'');return option}));
    $('rescheduleState').textContent='請選擇要改期的目前排餐日期。';
  }catch(_error){
    $('reschedule').hidden=false;
    $('rescheduleState').textContent='目前無法載入可改期餐表，請稍後再試。';
    $('openReschedule').disabled=true;
  }
}
function openReschedule(){const selected=$('rescheduleSource').value.split('|');if(selected.length!==2)return;const query=new URLSearchParams({order_id:selected[0],source_date:selected[1]});window.location.assign('/customer-reschedule?'+query.toString())}
$('openReschedule').addEventListener('click',openReschedule);
"""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>VIP 3 日飲食健檢</title>
  <style>
    :root{{--ink:#16332a;--muted:#61736c;--green:#126b4f;--cream:#faf8f0;--paper:#fffef9;--line:#dce5df;--amber:#9a6214}}
    *{{box-sizing:border-box}}body{{margin:0;background:#e8eee9;color:var(--ink);font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;line-height:1.55}}
    .app{{width:min(100%,430px);min-height:100dvh;margin:auto;background:var(--cream);padding:20px}}
    .brand{{font-weight:900;margin-bottom:20px}}.brand small{{display:block;color:var(--muted);font-size:12px}}
    .card{{background:var(--paper);border:1px solid var(--line);border-radius:20px;padding:18px;margin:12px 0}}
    .progress{{background:var(--green);color:white}}.count{{font-size:42px;font-weight:900;line-height:1}}.count span{{font-size:15px}}
    .bar{{height:8px;background:#ffffff33;border-radius:20px;overflow:hidden;margin:16px 0}}.bar i{{display:block;height:100%;background:#f5d58c;width:0}}
    h1{{font-size:26px;line-height:1.25;margin:4px 0 8px}}h2{{font-size:17px;margin:0 0 8px}}p{{font-size:14px;color:var(--muted)}}
    .progress p{{color:#e6f3ed}}.days{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.day{{border:1px solid var(--line);border-radius:13px;padding:10px;font-size:12px}}
    .day.done{{background:#eaf5ef;border-color:#a8cbbb}}button{{width:100%;min-height:48px;border:0;border-radius:13px;background:var(--green);color:#fff;font:inherit;font-weight:900;padding:10px}}
    .error{{border-color:#e2b9a7;background:#fff5ef}}[hidden]{{display:none!important}}.label{{font-size:11px;font-weight:900;color:var(--green);letter-spacing:.08em}}
  </style>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
</head>
<body><main class="app">
  <div class="brand">一日樂食・VIP 健檢<small>首次 VIP 權益｜7 天內完成 3 個有效紀錄日</small></div>
  <section id="loading" class="card"><h2>正在確認你的健檢進度…</h2><p>請保持此頁開啟。</p></section>
  <section id="error" class="card error" hidden><h2 id="errorTitle">暫時無法載入</h2><p id="errorCopy"></p></section>
  <section id="empty" class="card" hidden><h1>目前沒有進行中的 3 日健檢</h1><p>此權益會在符合資格的首次 VIP 正式開通時自動建立，不需要另外輸入序號。</p></section>
  <div id="content" hidden>
    <section class="card progress"><small>3 日飲食健檢進度</small><div class="count"><b id="count">0</b><span>/3 個有效日</span></div><div class="bar"><i id="bar"></i></div><p id="statusCopy"></p></section>
    <section class="card"><div class="label">紀錄窗口</div><h2 id="windowText"></h2><div id="days" class="days"></div></section>
    <section class="card"><h2>下一步</h2><p id="nextCopy"></p><button id="backToLine">回到 LINE 紀錄飲食</button></section>
    <section id="supplement" class="card" hidden><div class="label">營養師補件要求</div><h2 id="supplementTitle">請先完成指定紀錄</h2><p id="supplementReason"></p><p id="supplementContent"></p><p id="supplementRequestedAt"></p><button id="submitSupplement">已補充，送回審查</button><p id="supplementSubmitState" aria-live="polite"></p></section>
    <section id="report" class="card" hidden><div class="label">營養師核准報告</div><h1>3 日飲食基準報告</h1><h2>做得好的地方</h2><p id="reportGood"></p><h2>優先調整</h2><p id="reportPriority"></p><h2>接下來 7 天</h2><p id="reportNext"></p><h2>資料限制</h2><p id="reportLimit"></p></section>
  </div>{reschedule_card}
</main>
<script>
const LIFF_ID={liff_json};
const REQUEST_TIMEOUT_MS=15000;
const statusCopy={{collecting:'繼續使用原本的飲食紀錄方式。',ready_for_review:'3 個有效日已完成，資料正在整理並等待營養師審核。',needs_more_info:'營養師需要你補充部分紀錄。',approved_pending_delivery:'報告已核准，正在準備送達。',delivery_failed:'報告暫時未送達，系統會重送同一版本。',delivered:'你的基準報告已完成。',expired:'本次 7 天紀錄期限已結束，如需協助請回到 LINE 聯絡客服。',cancelled:'本次健檢已取消，如有疑問請回到 LINE 聯絡客服。'}};
const $=id=>document.getElementById(id);
let idToken='',currentState=null,viewGeneration=0,completionSaving=null,completionRetry=null;
function showError(title,copy){{$('loading').hidden=true;$('error').hidden=false;$('errorTitle').textContent=title;$('errorCopy').textContent=copy}}
function validSupplement(state){{const request=state&&state.supplement_request;return state&&state.status==='needs_more_info'&&request&&typeof request==='object'&&typeof request.supplement_request_id==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}$/.test(request.supplement_request_id)&&typeof request.expected_source_token==='string'&&/^[0-9a-f]{{64}}$/.test(request.expected_source_token)&&typeof request.reason==='string'&&Boolean(request.reason.trim())&&typeof request.required_content==='string'&&Boolean(request.required_content.trim())&&typeof request.requested_at==='string'?request:null}}
function render(payload){{
  const generation=++viewGeneration;completionSaving=null;$('loading').hidden=true;$('empty').hidden=true;$('supplement').hidden=true;$('supplementTitle').textContent='請先完成指定紀錄';$('submitSupplement').hidden=false;$('report').hidden=true;$('supplementSubmitState').textContent='';
  if(!payload||payload.eligible!==true){{$('empty').hidden=false;currentState=null;return}}
  const s=payload.state||{{}};currentState=s;const count=Math.max(0,Math.min(3,Number(s.valid_day_count)||0));$('content').hidden=false;$('count').textContent=String(count);$('bar').style.width=String(Math.round(count/3*100))+'%';$('statusCopy').textContent=statusCopy[s.status]||'正在確認目前狀態。';$('windowText').textContent=(s.window_started_at||'')+' ～ '+(s.window_ends_at||'');$('days').replaceChildren(...[1,2,3].map(n=>{{const d=document.createElement('div');d.className='day '+(n<=count?'done':'');d.textContent=(n<=count?'已完成':'待完成')+'・第 '+n+' 天';return d}}));$('nextCopy').textContent=count<3?'每天至少留下 2 餐主要餐點，並補上飲料、點心或宵夜狀況。':'資料完成後由 AI 整理，再交由真正營養師審核。';
  const request=validSupplement(s);if(request){{$('supplement').hidden=false;$('supplementReason').textContent='補件理由：'+request.reason;$('supplementContent').textContent='需要補充：'+request.required_content;$('supplementRequestedAt').textContent='提出時間：'+request.requested_at;$('submitSupplement').disabled=Boolean(completionSaving);}}
  if(s.report){{$('report').hidden=false;$('reportGood').textContent=s.report.good||'—';$('reportPriority').textContent=s.report.priority||'—';$('reportNext').textContent=s.report.next_7_days||'—';$('reportLimit').textContent=s.report.limitations||'—'}}
  return generation;
}}
function completionMessage(status){{if(status===409)return '尚未偵測到實質補件變更，請先補充紀錄後再提交；不會自動更新或重送。';if(status===401)return 'LINE 身分驗證失敗，請重新開啟頁面。';if(status===404)return '目前找不到可提交的補件，請重新載入。';if(status===422)return '補件提交格式無效，請重新載入。';return '提交結果未知或服務暫時不可用；可用同一請求重試。'}}
async function submitSupplement(){{
  if(completionSaving)return;const request=validSupplement(currentState);if(!request)return;
  if(!window.confirm('確認已依營養師要求補充紀錄，並送回重新審查？'))return;
  const binding=request.supplement_request_id+'\\n'+request.expected_source_token;
  const payload=completionRetry&&completionRetry.binding===binding?completionRetry.payload:{{request_id:crypto.randomUUID(),expected_supplement_request_id:request.supplement_request_id,expected_source_token:request.expected_source_token}};
  completionRetry={{binding,payload}};const ownedGeneration=viewGeneration,ownedCase=currentState.case_id,operation={{}};const controller=new AbortController();let timeoutId,timedOut=false;
  const timeout=new Promise((resolve,reject)=>{{timeoutId=setTimeout(()=>{{timedOut=true;controller.abort();reject(new Error('timeout'));}},REQUEST_TIMEOUT_MS)}});
  completionSaving=operation;$('submitSupplement').disabled=true;$('supplementSubmitState').textContent='正在提交並核對目前狀態…';
  try{{
    const response=await Promise.race([fetch('/api/vip-health-check/me/supplement-completion',{{method:'POST',headers:{{Authorization:'Bearer '+idToken,'Content-Type':'application/json'}},cache:'no-store',credentials:'omit',body:JSON.stringify(payload),signal:controller.signal}}),timeout]);
    if(ownedGeneration!==viewGeneration||!currentState||currentState.case_id!==ownedCase)return;
    if(!response.ok){{$('supplementSubmitState').textContent=completionMessage(response.status);return}}
    const result=await Promise.race([response.json(),timeout]);if(!result||result.status!=='ready_for_review'||result.submitted!==true||typeof result.replayed!=='boolean')throw new Error('invalid completion response');
    const refreshed=await Promise.race([fetch('/api/vip-health-check/me',{{headers:{{Authorization:'Bearer '+idToken}},cache:'no-store',credentials:'omit',signal:controller.signal}}),timeout]);if(!refreshed.ok)throw new Error('reload failed');const payloadAfter=await Promise.race([refreshed.json(),timeout]);
    if(ownedGeneration!==viewGeneration||!currentState||currentState.case_id!==ownedCase)return;
    const stateAfter=payloadAfter&&payloadAfter.eligible===true&&payloadAfter.state;if(!stateAfter||stateAfter.case_id!==ownedCase||stateAfter.status!=='ready_for_review'||stateAfter.supplement_request!==null)throw new Error('completion not confirmed');
    currentState=stateAfter;$('statusCopy').textContent=statusCopy[stateAfter.status];$('supplement').hidden=false;$('supplementTitle').textContent='補件已提交';$('supplementReason').textContent='';$('supplementContent').textContent='';$('supplementRequestedAt').textContent='';$('submitSupplement').hidden=true;$('supplementSubmitState').textContent='已提交，待營養師重新審查';completionRetry=null;
  }}catch(_error){{if(ownedGeneration===viewGeneration&&currentState&&currentState.case_id===ownedCase)$('supplementSubmitState').textContent=completionMessage(timedOut?0:0)}}finally{{clearTimeout(timeoutId);if(completionSaving===operation){{completionSaving=null;if(ownedGeneration===viewGeneration&&currentState&&currentState.case_id===ownedCase)$('submitSupplement').disabled=false}}}}
}}
{reschedule_script}
async function init(){{try{{await liff.init({{liffId:LIFF_ID}});if(!liff.isLoggedIn()){{liff.login();return}}idToken=liff.getIDToken();if(!idToken)throw new Error('missing-id-token');const response=await fetch('/api/vip-health-check/me',{{headers:{{Authorization:'Bearer '+idToken}},cache:'no-store'}});if(response.status===401){{showError('需要重新登入','請關閉頁面後，從 LINE 再開啟一次。');return}}if(response.status===503){{showError('LINE 驗證暫時忙碌','請稍後再試，資料不會遺失。');return}}if(!response.ok)throw new Error('load-failed');render(await response.json());{('await loadRescheduleContext();' if customer_reschedule_enabled else '')}}}catch(_error){{showError('暫時無法載入','請稍後再試，或回到 LINE 聯絡客服。')}}}}
$('backToLine').addEventListener('click',()=>liff.closeWindow());$('submitSupplement').addEventListener('click',submitSupplement);
init();
</script></body></html>"""


def create_customer_health_check_router(
    *,
    liff_id: str,
    channel_id: str,
    state_loader: Callable[[str], Mapping[str, object] | None],
    supplement_completion_saver: Callable[
        [str, str, str, str], Mapping[str, object]
    ] | None = None,
    token_verifier: Callable[..., str] = verify_line_id_token,
    customer_reschedule_enabled: bool = False,
) -> APIRouter:
    liff_id = str(liff_id or "").strip()
    channel_id = str(channel_id or "").strip()
    if not re.fullmatch(r"[0-9]{5,}-[A-Za-z0-9_-]+", liff_id):
        raise ValueError("VIP 健檢 LIFF ID 格式無效")
    if not re.fullmatch(r"[0-9]{5,}", channel_id):
        raise ValueError("VIP 健檢 LINE Login Channel ID 格式無效")
    if not liff_id.startswith(channel_id + "-"):
        raise ValueError("VIP 健檢 LIFF 不屬於設定的 LINE Login Channel")

    router = APIRouter()

    def authenticate(authorization: str | None) -> tuple[JSONResponse | None, str | None]:
        scheme, separator, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            return JSONResponse(
                {"detail": "需要 LINE 登入"}, status_code=401, headers=NO_STORE_HEADERS
            ), None
        try:
            return None, token_verifier(token.strip(), channel_id=channel_id)
        except LineAuthenticationError:
            return JSONResponse(
                {"detail": "LINE 登入無效"}, status_code=401, headers=NO_STORE_HEADERS
            ), None
        except LineAuthenticationUnavailable:
            return JSONResponse(
                {"detail": "LINE 驗證服務暫時無法使用"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            ), None
        except Exception:
            return JSONResponse(
                {"detail": "LINE 驗證服務暫時無法使用"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            ), None

    @router.get("/vip-health-check", response_class=HTMLResponse)
    def customer_health_check_page() -> HTMLResponse:
        return HTMLResponse(
            _render_customer_liff_html(
                liff_id, customer_reschedule_enabled=customer_reschedule_enabled
            ),
            headers=NO_STORE_HEADERS,
        )

    @router.get("/api/vip-health-check/me", response_class=JSONResponse)
    def customer_health_check_state(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        denied, user_id = authenticate(authorization)
        if denied is not None:
            return denied
        assert user_id is not None
        try:
            state = state_loader(user_id)
            return JSONResponse(
                {"eligible": state is not None, "state": state},
                headers=NO_STORE_HEADERS,
            )
        except Exception:
            return JSONResponse(
                {"detail": "健檢資料暫時無法載入"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            )

    if supplement_completion_saver is not None:
        @router.post(
            "/api/vip-health-check/me/supplement-completion",
            response_class=JSONResponse,
        )
        async def submit_supplement_completion(
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> JSONResponse:
            denied, user_id = authenticate(authorization)
            if denied is not None:
                return denied
            assert user_id is not None
            content_length = request.headers.get("content-length", "")
            if (
                content_length
                and (not content_length.isdigit() or int(content_length) > 1000)
            ):
                return JSONResponse(
                    {"detail": "補件提交格式無效"}, status_code=422,
                    headers=NO_STORE_HEADERS,
                )
            body = await request.body()
            if len(body) > 1000:
                return JSONResponse(
                    {"detail": "補件提交格式無效"}, status_code=422,
                    headers=NO_STORE_HEADERS,
                )
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JSONResponse(
                    {"detail": "補件提交格式無效"}, status_code=422,
                    headers=NO_STORE_HEADERS,
                )
            if (
                not isinstance(payload, dict)
                or set(payload) != {
                    "request_id", "expected_supplement_request_id", "expected_source_token"
                }
                or not isinstance(payload["request_id"], str)
                or not isinstance(payload["expected_supplement_request_id"], str)
                or not isinstance(payload["expected_source_token"], str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", payload["request_id"])
                is None
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
                    payload["expected_supplement_request_id"],
                ) is None
                or re.fullmatch(r"[0-9a-f]{64}", payload["expected_source_token"])
                is None
            ):
                return JSONResponse(
                    {"detail": "補件提交格式無效"}, status_code=422,
                    headers=NO_STORE_HEADERS,
                )
            try:
                result = supplement_completion_saver(
                    user_id,
                    payload["request_id"],
                    payload["expected_supplement_request_id"],
                    payload["expected_source_token"],
                )
            except CustomerSupplementNotFound:
                return JSONResponse(
                    {"detail": "找不到可提交的補件"}, status_code=404,
                    headers=NO_STORE_HEADERS,
                )
            except CustomerSupplementConflict:
                return JSONResponse(
                    {"detail": "補件狀態已更新，請重新載入"}, status_code=409,
                    headers=NO_STORE_HEADERS,
                )
            except Exception:
                return JSONResponse(
                    {"detail": "健檢資料暫時無法更新"}, status_code=503,
                    headers=NO_STORE_HEADERS,
                )
            return JSONResponse(
                {
                    key: result[key]
                    for key in ("status", "submitted", "replayed")
                    if key in result
                },
                headers=NO_STORE_HEADERS,
            )

    return router


def attach_customer_health_check_routes(
    app: Any,
    *,
    enabled: bool,
    environ: Mapping[str, str],
    state_loader: Callable[[str], Mapping[str, object] | None],
    supplement_completion_saver: Callable[
        [str, str, str, str], Mapping[str, object]
    ] | None = None,
    token_verifier: Callable[..., str] = verify_line_id_token,
) -> bool:
    """只在功能明確開啟且專用 LINE 身分設定一致時掛載顧客路由。"""
    if not enabled:
        return False
    liff_id = str(environ.get("VIP_HEALTH_CHECK_LIFF_ID") or "").strip()
    channel_id = str(
        environ.get("VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID") or ""
    ).strip()
    if not liff_id:
        raise ValueError("VIP_HEALTH_CHECK_LIFF_ID 不可空白")
    if not channel_id:
        raise ValueError("VIP_HEALTH_CHECK_LINE_LOGIN_CHANNEL_ID 不可空白")
    if not liff_id.startswith(channel_id + "-"):
        raise ValueError("VIP 健檢 LIFF 不屬於設定的 LINE Login Channel")
    customer_reschedule_enabled = (
        str(environ.get("APP_ENV") or "").strip().lower() == "staging"
        and str(environ.get("CUSTOMER_RESCHEDULE_LIFF_ENABLED") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if customer_reschedule_enabled:
        reschedule_channel_id = str(
            environ.get("CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID") or ""
        ).strip()
        if reschedule_channel_id != channel_id:
            raise ValueError("健檢與改期 LIFF 必須使用相同 LINE Login Channel")
    app.include_router(
        create_customer_health_check_router(
            liff_id=liff_id,
            channel_id=channel_id,
            state_loader=state_loader,
            supplement_completion_saver=supplement_completion_saver,
            token_verifier=token_verifier,
            customer_reschedule_enabled=customer_reschedule_enabled,
        )
    )
    return True
