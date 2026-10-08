import pytest
from test_round2_editor_corrections import _run_browser_case

@pytest.mark.parametrize('mode,expected',[('granted',1),('denied',0),('query-error',0),('context-error',0),('send-error',0)])
def test_return_does_not_probe_unsupported_api_and_preserves_saved_receipt(mode,expected):
 result=_run_browser_case('''
 const mode='''+repr(mode)+''';let sent=0,probes=0;
 window.liff={isInClient:()=>true,isApiAvailable:()=>{probes++;throw new Error('Unexpected API name')},
 getContext:()=>{if(mode==='context-error')throw new Error('context failed');return {type:'utou',source:{type:'user'}}},
 permission:{query:async ()=>{if(mode==='query-error')throw new Error('permission failed');return {state:mode==='denied'?'denied':'granted'}}},
 sendMessages:async ()=>{if(mode==='send-error')throw new Error('403');sent++}};
 const h=window.__MEAL_DRAFT_TEST__;h.setReceipt({return_command:'#草稿回傳 same-saved-receipt'});
 await h.deliver();
 return {sent,probes,status:document.getElementById('status').textContent,command:document.getElementById('command').textContent};
 ''')
 assert result['sent']==expected
 assert result['probes']==0
 assert result['command']=='#草稿回傳 same-saved-receipt'
 assert '儲存失敗' not in result['status']
 if not expected:assert '草稿已儲存' in result['status']


def test_put_success_delivery_failure_retry_does_not_put_again():
 result=_run_browser_case('''
 let puts=0,sends=0;
 window.fetch=async()=>{puts++;return {ok:true,json:async()=>({return_command:'#草稿回傳 saved-once'})}};
 window.liff={isInClient:()=>true,isApiAvailable:()=>{throw new Error('Unexpected API name')},
 getContext:()=>({type:'utou'}),permission:{query:async()=>({state:'granted'})},
 sendMessages:async()=>{sends++;if(sends===1)throw new Error('403')}};
 const h=window.__MEAL_DRAFT_TEST__;
 h.fill({token:'test',version:1,draft_type:'text',food_name:'白飯',source_label:'衛福部資料',meal_slot:'午餐',amount:150,unit:'g',
 nutrition:{calories_kcal:275,protein_g:4.7,fat_g:0.5,carbohydrate_g:61.5},
 display:{calories_kcal:'275',protein_g:'4.7',fat_g:'0.5',carbohydrate_g:'61.5'}});
 document.getElementById('form').dispatchEvent(new Event('submit',{cancelable:true}));
 for(let i=0;i<20;i++)await Promise.resolve();
 const failedStatus=document.getElementById('status').textContent;
 await h.deliver();
 return {puts,sends,failedStatus,command:document.getElementById('command').textContent};
 ''')
 assert result['puts']==1 and result['sends']==2
 assert '草稿已儲存' in result['failedStatus']
 assert '儲存失敗' not in result['failedStatus']
 assert result['command']=='#草稿回傳 saved-once'
