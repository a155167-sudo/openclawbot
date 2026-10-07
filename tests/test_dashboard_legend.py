import pytest
from dashboard_flex import build_message
from linebot.models import FlexSendMessage


def fixture_data(eaten=650, reserve=741, target=2000):
    return dict(user_name='測試', date_label='10/07', target_kcal=target, target_protein=120, records=[dict(name='午餐', slot='午餐', kcal=eaten, protein=35)], sub_meals=[dict(name='晚餐', slot='晚餐', kcal=reserve, protein=40, eaten=False)])


@pytest.mark.parametrize('eaten,reserve,target,expected', [
    (650,741,2000,['已吃 650','預留 741','可用 609']),
    (2150,741,2000,['已吃 2,150','預留 741','餘額 0']),
    (0,2200,2000,['已吃 0','預留 2,200','可用 0']),
    (123456,234567,500000,['已吃 123,456','預留 234,567','可用 141,977']),
])
def test_compact_legends_survive_sdk_without_wrapping(monkeypatch,eaten,reserve,target,expected):
    import server
    import customer_navigation
    msg=build_message(fixture_data(eaten,reserve,target))
    monkeypatch.setattr(server, 'get_dashboard_data', lambda *a, **k: {'synthetic': True})
    monkeypatch.setattr(customer_navigation, 'build_customer_balance_home_contents', lambda _: msg['contents'])
    payload=server.build_dashboard_flex('U-LEGEND-TEST').as_json_dict()
    legends=payload['contents']['body']['contents'][2]['contents']
    assert [x['contents'][1]['text'] for x in legends]==expected
    for item in legends:
        text=item['contents'][1]
        assert text['wrap'] is False
        assert text['maxLines']==1
        assert text['size']==('8px' if eaten == 123456 else 'xxs')
    assert FlexSendMessage.new_from_json_dict(payload).as_json_dict()==payload
