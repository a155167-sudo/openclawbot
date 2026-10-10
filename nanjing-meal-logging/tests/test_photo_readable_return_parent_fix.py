import json,sqlite3
import server
from tests.test_round2_meal_draft_liff import _photo_draft


def test_photo_saved_return_names_portions_replay_and_no_food_write(tmp_path,monkeypatch):
 db,draft=_photo_draft(tmp_path,monkeypatch)
 items=server.get_meal_draft_for_liff('U1',draft['token'])['items']
 kwargs=dict(user_id='U1',token=draft['token'],expected_version=1,meal_slot='晚餐',items=[{**items[0],'amount':150},{**items[1],'amount':100}],nutrition={'calories_kcal':400,'protein_g':30,'fat_g':None,'carbohydrate_g':None})
 result=server.save_photo_meal_draft_from_liff(**kwargs)
 assert result['return_command'].startswith('已修改：雞腿 150 g、豆漿 100 ml（回傳碼：')
 assert server.save_photo_meal_draft_from_liff(**kwargs)['return_command']==result['return_command']
 card=server.consume_meal_draft_return_command('U1',result['return_command'])
 assert '雞腿' in json.dumps(card.as_json_dict(),ensure_ascii=False)
 with sqlite3.connect(db) as conn:
  assert conn.execute('SELECT COUNT(*) FROM food_logs').fetchone()[0]==0
