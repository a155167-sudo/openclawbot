"""Preserve native envelopes while legacy dashboard-only assertions inspect the card.

This is a transport capture adapter, not an application response replacement.
Every two-message committed reply must still carry a genuine success message.
"""
def capture_dashboard_reply(replies, payload):
    if isinstance(payload, list):
        assert len(payload) == 2, 'committed reply must contain success + dashboard'
        assert payload[0].type == 'flex'
        assert payload[1].type == 'text'
        assert payload[1].text.startswith('✅ ')
        assert payload[1].text.endswith(('已記錄', '紀錄已更新'))
        replies.append(payload[0])
    else:
        replies.append(payload)
