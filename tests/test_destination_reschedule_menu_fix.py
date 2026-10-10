from types import SimpleNamespace
import pytest
import server
from subscription_meal_plan import sanitize_legacy_subscription_menu


@pytest.mark.parametrize('enabled,order_id,expected', [(False,123,False),(True,None,False),(True,123,True)])
def test_menu_footer_matches_actual_reschedule_button(monkeypatch,enabled,order_id,expected):
    replies=[]
    monkeypatch.setattr(server,'has_active_vip_access',lambda _:True)
    monkeypatch.setattr(server,'get_subscription_menu_access',lambda _:('active','2026/10/22（週四）\n午：雞胸輕便當\n晚：鮭魚飯',2,'2026-10-31'))
    monkeypatch.setattr(server,'get_active_subscription_order_id',lambda _:order_id)
    monkeypatch.setattr(server,'CUSTOMER_RESCHEDULE_LIFF_ENABLED',enabled)
    monkeypatch.setattr(server,'CUSTOMER_RESCHEDULE_LIFF_ID','2011335793-DESTINATION-FIXTURE')
    monkeypatch.setattr(server.line_bot_api,'reply_message',lambda _,messages:replies.append(messages))
    server.processed_messages.clear()
    event=SimpleNamespace(reply_token='fake-reply',source=SimpleNamespace(user_id='U'+'a'*32),message=SimpleNamespace(id='menu-destination-'+str(enabled)+'-'+str(order_id),text='查看菜單'))
    server.handle_message(event)
    messages=replies[0] if isinstance(replies[0],list) else [replies[0]]
    texts='\n'.join(message.text for message in messages)
    actions=messages[-1].quick_reply.items
    assert ('餐點改期' in texts)==expected
    assert any(item.action.label=='餐點改期' for item in actions)==expected
    assert any(item.action.label=='互換餐點' for item in actions)
    assert '雞胸輕便當' in texts and '鮭魚飯' in texts
    if expected:assert actions[-1].action.uri=='https://liff.line.me/2011335793-DESTINATION-FIXTURE?order_id=123'


def test_customer_menu_hides_internal_audit_without_altering_canonical_summary():
    summary='2026/10/22（週四）\n午：雞胸輕便當\n晚：鮭魚飯\n\n⏸ 人工查核[defer-meal:PRIVATE-AUDIT]：確認 10/02午餐 延至 10/22午餐 已套用；before=["雞胸便當",170]；after=["無",0]\n🔄 人工查核[swap-meal:PRIVATE-AUDIT2]：確認互換已套用；before=[180]；after=[200]'
    original=summary
    rendered=sanitize_legacy_subscription_menu(summary)
    assert 'PRIVATE-AUDIT' not in rendered and '人工查核' not in rendered
    assert 'before=' not in rendered and 'after=' not in rendered
    assert rendered=='2026/10/22（週四）\n午：雞胸輕便當\n晚：鮭魚飯'
    assert summary==original


def test_food_names_containing_audit_words_are_not_deleted():
    summary='2026/10/22（週四）\n午：before風味雞胸\n晚：人工查核招牌飯'
    assert sanitize_legacy_subscription_menu(summary)==summary
