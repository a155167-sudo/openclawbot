import pytest
from nutrition_plausibility import assess_draft_for_display


def old_draft():
 return {'request':{'food_name':'無糖豆漿','amount':500,'unit':'ml'},'estimate':{
  'basis_amount':500,'basis_unit':'ml','provenance':{'method':'text_meal_estimate','provider':'openai'},
  **{k:{'estimate':v,'min':v,'max':v} for k,v in {'calories_kcal':60,'protein_g':5,'fat_g':2,'carbohydrate_g':6}.items()}}}


def test_real_old_soy_estimate_cannot_render_actionable_numbers():
 d=old_draft()
 assert assess_draft_for_display(d)['status']=='requires_confirmation'
 import server
 reply=server.build_text_meal_estimate_flex(d)
 shown=reply.as_json_dict()
 assert shown['type']=='text'
 assert '確認' in shown['text']
 assert '60' not in shown['text']


def test_real_old_soy_confirm_is_blocked_without_mutating_history(tmp_path,monkeypatch):
 import server,sqlite3,json
 monkeypatch.setattr(server,'DB_DIR',str(tmp_path));monkeypatch.setattr(server,'DB_PATH',str(tmp_path/'old.db'))
 server.init_db();d=old_draft();encoded=json.dumps(d['estimate'])
 with sqlite3.connect(server.DB_PATH) as conn:
  conn.execute('''INSERT INTO pending_text_meal_estimates
    (token,user_id,source_message_id,request_json,estimate_json,portion_multiplier,meal_slot,status,version,confirmed_log_id,created_at,updated_at,expires_at)
    VALUES (?,?,?,?,?,1,'午餐','pending',1,'','','','2099-12-31T23:59:59+08:00')''',
    ('a'*40,'test-user','test-message',json.dumps(d['request']),encoded))
 with pytest.raises(ValueError,match='確認'):
  server.apply_text_meal_estimate_action(user_id='test-user',token='a'*40,expected_version=1,action='confirm')
 with sqlite3.connect(server.DB_PATH) as conn:
  assert conn.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
  assert conn.execute('SELECT estimate_json,status FROM pending_text_meal_estimates').fetchone()==(encoded,'pending')


def test_manual_revision_is_not_overridden_by_ai_policy():
 d=old_draft();d['estimate']['provenance']={'method':'customer_revision','provider':'none'}
 assert assess_draft_for_display(d)['status']=='allowed'
