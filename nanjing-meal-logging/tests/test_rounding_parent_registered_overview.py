import copy
import server
from tests.test_meal_confirmation_card_and_revision_warning import _texts


def test_registered_overview_adapter_preserves_precision_then_displays_half_up(monkeypatch):
 data={'name':'測試','today_label':'今天','tdee':2000,'protein_goal':100,
       'balance_records':[{'slot':'早餐','name':'無糖豆漿','kcal':80,'protein':7},
                          {'slot':'午餐','name':'白飯','kcal':274.5,'protein':4.65}],
       'balance_sub_meals':[]}
 before=copy.deepcopy(data)
 monkeypatch.setattr(server,'get_dashboard_data',lambda *a,**kw:data)
 text='\n'.join(_texts(server.build_dashboard_flex('test').as_json_dict()))
 assert '已吃 355' in text and '275 kcal' in text and '88.4 g' in text  # target-present UI shows protein remaining, not eaten.
 assert data==before
