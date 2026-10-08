import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server
from customer_reschedule_liff_routes import create_customer_reschedule_router


HTML = Path(__file__).parents[1] / "customer-reschedule-meal-edit-liff.html"


def _setup_db(tmp_path, monkeypatch):
    db = tmp_path / "semantic-native-round3.db"
    monkeypatch.setattr(server, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(db))
    server.init_db()
    return db


def _seed_semantic_draft(db, *, meal_slot="午餐"):
    now = server.tw_now()
    now_text = now.isoformat(timespec="seconds")
    result = {
        "status": "draft",
        "batch_id": "B",
        "meal_slot": meal_slot,
        "clarifications": [],
        "writes_food_log": False,
        "audit": [{"stage": "semantic_parse", "raw_trace_id": "TRACE"}],
        "items": [
            {
                "request": {"item_id": "item-0", "food_name": "青菜", "amount": 100, "unit": "g", "portion_assumption": ""},
                "nutrition": {
                    "calories_kcal": {"estimate": 70, "min": 20, "max": 100, "unit": "kcal"},
                    "protein_g": {"estimate": 7, "min": 2, "max": 10, "unit": "g"},
                    "fat_g": {"estimate": 3, "min": 1, "max": 5, "unit": "g"},
                    "carbohydrate_g": {"estimate": 9, "min": 4, "max": 12, "unit": "g"},
                },
                "source_label": "官方資料",
                "source": {"publisher": "TFDA", "dataset_version": "2026-01"},
            },
            {
                "request": {"item_id": "item-1", "food_name": "茶飲", "amount": 200, "unit": "ml", "portion_assumption": ""},
                "nutrition": {
                    "calories_kcal": {"estimate": 90, "min": 40, "max": 120, "unit": "kcal"},
                    "protein_g": {"estimate": 5, "min": 2, "max": 8, "unit": "g"},
                    "fat_g": {"estimate": 4, "min": 1, "max": 7, "unit": "g"},
                    "carbohydrate_g": {"estimate": 10, "min": 5, "max": 15, "unit": "g"},
                },
                "source_label": "AI估算",
                "source": {"provider": "openai", "model": "x", "basis_unit": "ml", "raw_trace_id": "N1"},
            },
        ],
    }
    with sqlite3.connect(db) as conn:
        server.ensure_daily_food_ledger_schema(conn)
        conn.execute(
            "INSERT INTO semantic_meal_batches VALUES (?,?,?,?,?,'provider_started','{}',?,?)",
            ("B", "U", "M", "a" * 40, "Q", now_text, now_text),
        )
        conn.execute(
            "INSERT INTO text_meal_provider_attempts(attempt_id,token,user_id,quota_attempt_id,state,provider_started_at) VALUES (?,?,?,?,?,?)",
            ("B", "a" * 40, "U", "Q", "provider_started", now_text),
        )
        conn.commit()
    return server._persist_semantic_meal_result(
        user_id="U", message_id="M", batch_id="B", result=result
    )


def test_semantic_v1_point_is_authoritative_but_legacy_range_keeps_midpoint():
    semantic = {"schema_version": "semantic-meal-estimate-v1", "calories_kcal": {"estimate": 160, "min": 60, "max": 220}}
    legacy = {"schema_version": "text-meal-estimate-v1", "calories_kcal": {"estimate": 160, "min": 60, "max": 220}}
    assert server._text_meal_numeric_value(semantic, "calories_kcal") == 160
    assert server._text_meal_numeric_value(legacy, "calories_kcal") == 140
    assert semantic["calories_kcal"] == {"estimate": 160, "min": 60, "max": 220}


@pytest.mark.parametrize("schema,shown", [("semantic-meal-estimate-v1", 160), ("text-meal-estimate-v1", 140)])
def test_real_flex_uses_schema_numeric_value_without_changing_range(tmp_path, monkeypatch, schema, shown):
    db = _setup_db(tmp_path, monkeypatch)
    draft = _seed_semantic_draft(db)
    draft["estimate"]["schema_version"] = schema
    before = json.dumps(draft["estimate"], sort_keys=True)
    card = server.build_text_meal_estimate_flex(draft)
    payload = card.as_json_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert f"{shown} kcal（確認後記錄）" in text
    assert "60–220" in text
    assert json.dumps(draft["estimate"], sort_keys=True) == before
    assert type(card).new_from_json_dict(payload).as_json_dict() == payload


def test_semantic_point_estimate_survives_actual_confirmation(tmp_path, monkeypatch):
    db = _setup_db(tmp_path, monkeypatch)
    draft = _seed_semantic_draft(db)
    outcome = server.apply_text_meal_estimate_action(
        user_id="U", token=draft["token"], expected_version=1, action="confirm"
    )
    if outcome["kind"] == "preview":
        revised = outcome["draft"]
        outcome = server.apply_text_meal_estimate_action(
            user_id="U", token=revised["token"], expected_version=revised["version"], action="confirm"
        )
    with sqlite3.connect(db) as conn:
        stored = json.loads(conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE log_id=?", (outcome["log_id"],)
        ).fetchone()[0])
    assert stored["calories_kcal"] == 160
    assert stored["protein_g"] == 12


def test_semantic_multi_item_draft_persists_ordered_stable_complete_items_and_blank_slot(tmp_path, monkeypatch):
    db = _setup_db(tmp_path, monkeypatch)
    draft = _seed_semantic_draft(db, meal_slot="")
    items = draft["request"]["items"]
    assert [(item["item_id"], item["food_name"], item["amount"], item["unit"]) for item in items] == [
        ("item-0", "青菜", 100.0, "g"), ("item-1", "茶飲", 200.0, "ml")
    ]
    assert all(set(item["nutrition"]) == {"calories_kcal", "protein_g", "fat_g", "carbohydrate_g"} for item in items)
    assert items[0]["source"] == {"publisher": "TFDA", "dataset_version": "2026-01"}
    assert draft["meal_slot"] == ""
    with sqlite3.connect(db) as conn:
        saved = json.loads(conn.execute("SELECT request_json FROM pending_text_meal_estimates").fetchone()[0])
    assert saved["items"] == items
    with pytest.raises(ValueError, match="修改.*餐別"):
        server.apply_text_meal_estimate_action(
            user_id="U", token=draft["token"], expected_version=1, action="confirm"
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0


def test_semantic_multi_item_liff_save_is_per_item_cas_and_preserves_authority(tmp_path, monkeypatch):
    db = _setup_db(tmp_path, monkeypatch)
    draft = _seed_semantic_draft(db)
    projected = server.get_meal_draft_for_liff("U", draft["token"])
    before_quota = None
    with sqlite3.connect(db) as conn:
        before_quota = conn.execute("SELECT * FROM text_meal_estimate_quota_ledger ORDER BY attempt_id").fetchall()
    changed = [{**projected["items"][0], "amount": 150}, {**projected["items"][1], "amount": 100}]
    first = server.save_meal_draft_from_liff(
        user_id="U", token=draft["token"], expected_version=1,
        amount=None, unit="", meal_slot="晚餐", nutrition={}, items=changed,
        draft_type="text-semantic-items",
    )
    replay = server.save_meal_draft_from_liff(
        user_id="U", token=draft["token"], expected_version=1,
        amount=None, unit="", meal_slot="晚餐", nutrition={}, items=changed,
        draft_type="text-semantic-items",
    )
    assert replay["receipt_id"] == first["receipt_id"]
    projected_saved = first["draft"]
    assert projected_saved["version"] == 2
    assert [(item["item_id"], item["amount"], item["unit"]) for item in projected_saved["items"]] == [
        ("item-0", 150.0, "g"), ("item-1", 100.0, "ml")
    ]
    saved = server.get_text_meal_estimate_draft("U", draft["token"])
    assert saved["estimate"]["calories_kcal"] == {"estimate": 150.0, "min": 50.0, "max": 210.0, "unit": "kcal"}
    provenance = saved["estimate"]["provenance"]
    assert provenance["provider"] == "openai" and provenance["model"] == "semantic-meal-v1"
    assert provenance["items"][0]["source"]["publisher"] == "TFDA"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
        assert conn.execute("SELECT * FROM text_meal_estimate_quota_ledger ORDER BY attempt_id").fetchall() == before_quota
    with pytest.raises(ValueError, match="識別|次序|單位"):
        server.save_meal_draft_from_liff(
            user_id="U", token=saved["token"], expected_version=2,
            amount=None, unit="", meal_slot="晚餐", nutrition={},
            items=[{**changed[1], "unit": "g"}, changed[0]], draft_type="text-semantic-items",
        )
    confirmed = server.apply_text_meal_estimate_action(
        user_id="U", token=saved["token"], expected_version=2, action="confirm"
    )
    if confirmed["kind"] == "preview":
        acknowledged = confirmed["draft"]
        confirmed = server.apply_text_meal_estimate_action(
            user_id="U", token=acknowledged["token"],
            expected_version=acknowledged["version"], action="confirm",
        )
    with sqlite3.connect(db) as conn:
        formal = json.loads(conn.execute(
            "SELECT nutrition_snapshot_json FROM food_logs WHERE log_id=?",
            (confirmed["log_id"],),
        ).fetchone()[0])
    assert formal["calories_kcal"] == 150.0


def test_liff_frontend_branches_semantic_items_without_rebinding_photo():
    html = HTML.read_text(encoding="utf-8")
    assert "text-semantic-items" in html
    assert "draft.draft_type==='photo'||draft.draft_type==='text-semantic-items'" in html
    assert "請選擇餐別" in html


def test_actual_liff_routes_load_and_cas_save_semantic_items(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    draft = _seed_semantic_draft(server.DB_PATH)
    normal_html = tmp_path / "normal.html"
    normal_html.write_text(
        '<script>window.__RESCHEDULE_RUNTIME__ = null; /* __CUSTOMER_RESCHEDULE_RUNTIME__ */</script>',
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(create_customer_reschedule_router(
        liff_id="2011528194-test", channel_id="2011528194", db_path=server.DB_PATH,
        app_env="staging", normal_flow=True, token_verifier=lambda token, **_: token,
        html_path=normal_html, meal_edit_html_path=HTML,
        meal_draft_loader=server.get_meal_draft_for_liff,
        meal_draft_saver=server.save_meal_draft_from_liff,
    ))
    client = TestClient(app)
    loaded = client.get(
        f"/customer-reschedule/meal-draft?token={draft['token']}",
        headers={"Authorization": "Bearer U"},
    )
    assert loaded.status_code == 200
    body = loaded.json()
    assert body["draft_type"] == "text-semantic-items"
    assert body["nutrition"]["calories_kcal"] == 160
    assert [(item["item_id"], item["amount"], item["unit"]) for item in body["items"]] == [
        ("item-0", 100.0, "g"), ("item-1", 200.0, "ml")
    ]
    body["items"][0]["amount"] = 150
    body["items"][1]["amount"] = 100
    saved = client.put(
        "/customer-reschedule/meal-draft", headers={"Authorization": "Bearer U"},
        json={"draft_type": body["draft_type"], "token": body["token"],
              "version": body["version"], "meal_slot": "晚餐", "items": body["items"],
              "nutrition": {"calories_kcal": 9999}},
    )
    assert saved.status_code == 200
    saved_body = saved.json()["draft"]
    assert saved_body["draft_type"] == "text-semantic-items"
    assert saved_body["nutrition"]["calories_kcal"] == 150
    assert client.put(
        "/customer-reschedule/meal-draft", headers={"Authorization": "Bearer OTHER"},
        json={"draft_type": body["draft_type"], "token": body["token"],
              "version": 2, "meal_slot": "晚餐", "items": body["items"]},
    ).status_code == 403


def test_browser_semantic_items_require_slot_and_recompute_readonly_totals():
    from tests.test_round2_editor_corrections import _run_browser_case

    result = _run_browser_case(
        """
        const h=window.__MEAL_DRAFT_TEST__;
        h.fill({draft_type:'text-semantic-items',food_name:'甲、乙',source_label:'混合',meal_slot:'',
          nutrition:{calories_kcal:160,protein_g:12,fat_g:7,carbohydrate_g:19},
          display:{calories_kcal:'160',protein_g:'12.0',fat_g:'7.0',carbohydrate_g:'19.0'},
          items:[{item_id:'item-0',name:'甲',amount:100,unit:'g',calories_kcal:70,protein_g:7,fat_g:3,carbohydrate_g:9},
                 {item_id:'item-1',name:'乙',amount:200,unit:'ml',calories_kcal:90,protein_g:5,fat_g:4,carbohydrate_g:10}]});
        const first=document.querySelector('.itemAmount');first.value='150';first.dispatchEvent(new Event('input',{bubbles:true}));
        return {slot:document.getElementById('slot').value,
          itemCount:document.querySelectorAll('.ingredient').length,
          calories:document.getElementById('calories_kcal').value,
          readonly:document.getElementById('calories_kcal').readOnly,
          singleHidden:document.getElementById('singlePortion').classList.contains('hidden')};
        """
    )
    assert result == {"slot": "", "itemCount": 2, "calories": "195", "readonly": True, "singleHidden": True}
