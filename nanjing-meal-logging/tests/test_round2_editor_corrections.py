import json
import os
import sqlite3
import subprocess
import textwrap
from pathlib import Path

import pytest

import server
from meal_photo_system import apply_meal_photo_action
from tests.test_round2_meal_draft_liff import _photo_draft


HTML = Path(__file__).parents[1] / "customer-reschedule-meal-edit-liff.html"


def test_photo_save_keeps_ai_snapshot_and_persists_explicit_whole_meal_override(tmp_path, monkeypatch):
    _db, draft = _photo_draft(tmp_path, monkeypatch)
    before = json.loads(json.dumps(draft["estimate"]))
    items = server.get_meal_draft_for_liff("U1", draft["token"])["items"]

    saved = server.save_photo_meal_draft_from_liff(
        user_id="U1",
        token=draft["token"],
        expected_version=1,
        meal_slot="晚餐",
        items=[{**items[0], "amount": 150}, {**items[1], "amount": 100}],
        nutrition={
            "calories_kcal": 777.25,
            "protein_g": 66.5,
            "fat_g": 22.25,
            "carbohydrate_g": 88.75,
        },
    )["draft"]

    assert saved["review"]["liff_initial_ai"]["calories_kcal"] == before["calories_kcal"]
    assert saved["review"]["liff_initial_items"] == before["estimate_items"]
    assert saved["review"]["liff_customer_nutrition_override"] == {
        "calories_kcal": 777.25,
        "protein_g": 66.5,
        "fat_g": 22.25,
        "carbohydrate_g": 88.75,
    }
    # The canonical AI estimate remains a valid AI/items snapshot.  UI/formal
    # projections consume the separate explicit customer override.
    projected = server.get_meal_draft_for_liff("U1", draft["token"])
    assert projected["nutrition"] == saved["review"]["liff_customer_nutrition_override"]
    assert saved["estimate"]["fat_g"] is None
    assert saved["estimate"]["carbohydrate_g"] is None
    card_text = json.dumps(server.build_meal_photo_estimate_bubble(saved), ensure_ascii=False)
    assert "實際入帳：熱量 777 kcal｜蛋白質 66.5 g｜脂肪 22.2 g｜碳水 88.8 g" in card_text


def test_photo_confirm_promotes_customer_override_to_the_single_formal_log(tmp_path, monkeypatch):
    db, draft = _photo_draft(tmp_path, monkeypatch)
    items = server.get_meal_draft_for_liff("U1", draft["token"])["items"]
    override = {
        "calories_kcal": 701.125,
        "protein_g": 61.25,
        "fat_g": 21.5,
        "carbohydrate_g": 82.75,
    }
    saved = server.save_photo_meal_draft_from_liff(
        user_id="U1", token=draft["token"], expected_version=1,
        meal_slot="晚餐", items=items, nutrition=override,
    )["draft"]
    with sqlite3.connect(db) as conn:
        result = apply_meal_photo_action(
            conn, event_id="confirm-customer-override", user_id="U1",
            token=draft["token"], expected_version=saved["version"],
            action="confirm_estimate",
        )
        server._promote_photo_customer_override_to_confirmed_log(conn, result["draft"], result["result"])
        conn.commit()
        rows = conn.execute(
            """SELECT l.nutrition_snapshot_json,f.source_type
               FROM food_logs l JOIN food_catalog f ON f.food_id=l.food_id
               WHERE l.user_id='U1'"""
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == override
    assert rows[0][1] == "user_meal_photo_customer_override"


def _run_browser_case(js_body: str) -> dict:
    script = textwrap.dedent(
        f"""
        const fs=require('fs');
        const {{chromium}}=require('playwright');
        (async()=>{{
          const browser=await chromium.launch({{headless:true}});
          const page=await browser.newPage();
          let html=fs.readFileSync({json.dumps(str(HTML))},'utf8')
            .replace('<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>','')
            .replace('null; /* __MEAL_DRAFT_RUNTIME_MARKER__ */','{{"liffId":"test","disableAutoBoot":true}};');
          await page.addInitScript(()=>{{ window.liff={{}}; }});
          await page.setContent(html);
          const answer=await page.evaluate(async()=>{{ {js_body} }});
          console.log(JSON.stringify(answer));
          await browser.close();
        }})().catch(e=>{{console.error(e);process.exit(1)}});
        """
    )
    env = {**os.environ, "NODE_PATH": "/home/win-xi/jev-ui-integration-trial/tooling/node_modules"}
    run = subprocess.run(["node", "-e", script], text=True, capture_output=True, env=env)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_browser_text_manual_field_stays_fixed_and_clean_fields_rescale_from_unrounded_baseline():
    result = _run_browser_case(
        """
        const h=window.__MEAL_DRAFT_TEST__;
        h.fill({draft_type:'text',food_name:'豆漿',source_label:'AI',meal_slot:'午餐',amount:3,unit:'份',
          nutrition:{calories_kcal:100.49,protein_g:10.04,fat_g:5.05,carbohydrate_g:null},
          display:{calories_kcal:'100',protein_g:'10.0',fat_g:'5.1',carbohydrate_g:''}});
        const protein=document.getElementById('protein_g');
        protein.value='12.34'; protein.dispatchEvent(new Event('input',{bubbles:true}));
        const amount=document.getElementById('amount'); amount.value='6'; amount.dispatchEvent(new Event('input',{bubbles:true}));
        return {protein:protein.value,calories:document.getElementById('calories_kcal').value,
          fat:document.getElementById('fat_g').value,carb:document.getElementById('carbohydrate_g').value};
        """
    )
    assert result == {"protein": "12.34", "calories": "201", "fat": "10.1", "carb": ""}


def test_browser_photo_all_four_fields_are_editable_and_blank_amount_is_not_coerced_to_zero():
    result = _run_browser_case(
        """
        const h=window.__MEAL_DRAFT_TEST__;
        h.fill({draft_type:'photo',food_name:'照片',source_label:'AI',meal_slot:'午餐',
          nutrition:{calories_kcal:300,protein_g:20,fat_g:10,carbohydrate_g:40},
          display:{calories_kcal:'300',protein_g:'20.0',fat_g:'10.0',carbohydrate_g:'40.0'},
          items:[{item_id:'i1',name:'雞肉',amount:100,unit:'g',calories_kcal:200,protein_g:20,fat_g:8,carbohydrate_g:null},
                 {item_id:'i2',name:'飯',amount:1,unit:'碗',calories_kcal:100,protein_g:0,fat_g:2,carbohydrate_g:40}]});
        const editable=['calories_kcal','protein_g','fat_g','carbohydrate_g'].every(id=>!document.getElementById(id).readOnly);
        const first=document.querySelector('.itemAmount'); first.value=''; first.dispatchEvent(new Event('input',{bubbles:true}));
        return {editable,status:document.getElementById('status').textContent,calories:document.getElementById('calories_kcal').value};
        """
    )
    assert result["editable"] is True
    assert "份量" in result["status"]
    assert result["calories"] == "300"


@pytest.mark.parametrize(
    "context,permission,expected",
    [
        ({"type": "group", "source": {"type": "group"}}, "granted", 0),
        ({"type": "utou", "source": {"type": "user"}}, "denied", 0),
        ({"type": "utou", "source": {"type": "user"}}, "granted", 1),
    ],
)
def test_browser_return_requires_original_one_to_one_context_and_chat_write_scope(context, permission, expected):
    result = _run_browser_case(
        f"""
        let sent=[];
        window.liff={{isInClient:()=>true,isApiAvailable:n=>n==='sendMessages',getContext:()=>({json.dumps(context)}),
          permission:{{query:async scope=>({{state:{json.dumps(permission)}}})}},
          sendMessages:async messages=>sent.push(messages)}};
        const h=window.__MEAL_DRAFT_TEST__; h.setReceipt({{return_command:"#草稿回傳 {'b'*48}"}}); await h.deliver();
        return {{count:sent.length,status:document.getElementById('status').textContent}};
        """
    )
    assert result["count"] == expected
    if expected:
        assert "不代表已送達" in result["status"]
    else:
        assert "複製" in result["status"]
