import server
from tests.test_meal_confirmation_card_and_revision_warning import _semantic_draft, _texts


def test_fixed_soy_source_note_is_not_overwritten_by_tfda_name():
 source={'publisher':'TFDA','card_note':'衛福部資料・以 1ml≈1g 換算'}
 draft=_semantic_draft(name='無糖豆漿',source_label='衛福部資料',source=source,amount=500,unit='ml')
 draft['estimate']['provenance']={'method':'official_reference','source_label':'衛福部資料','source':source}
 texts=_texts(server.build_text_meal_estimate_flex(draft).as_json_dict())
 assert texts.count('衛福部資料・以 1ml≈1g 換算')==1
 assert '衛福部資料' not in texts
 assert '午餐・500 ml' in texts
