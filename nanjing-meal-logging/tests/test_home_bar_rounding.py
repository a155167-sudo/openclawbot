from decimal import Decimal
import pytest
from customer_navigation import build_customer_home_contents

@pytest.mark.parametrize('consumed,goal,value,status', [
 ('99.95','100','100 / 100 kcal','剩 0 kcal'),
 ('33.35','100','33.4 / 100 kcal','剩 66.6 kcal'),
 ('100.05','100','100.1 / 100 kcal','超出 0.1 kcal'),
 ('100.04','100','100 / 100 kcal','剩 0 kcal'),
 ('0','100','0 / 100 kcal','剩 100 kcal'),
])
def test_visible_bar_values_use_consistent_precision(consumed, goal, value, status):
    data={'extra_cal':Decimal(consumed),'tdee':Decimal(goal),'extra_pro':None,'protein_goal':None,'extra_fat':None}
    before=dict(data)
    output=build_customer_home_contents(data)
    row=output['body']['contents'][2]['contents'][0]
    assert row['contents'][0]['contents'][1]['text']==value
    assert row['contents'][2]['text']==status
    assert data==before
