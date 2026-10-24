"""「營養師報到」：把傳訊者自己的 UID 推給老闆，10 分鐘內不重複通知。"""
from types import SimpleNamespace

import pytest

import server


@pytest.fixture
def sent(monkeypatch):
    pushes, replies = [], []
    monkeypatch.setattr(server.line_bot_api, "push_message", lambda to, msg: pushes.append((to, msg.text)))
    monkeypatch.setattr(server.line_bot_api, "reply_message", lambda token, msg: replies.append(msg.text))
    monkeypatch.setattr(server, "get_admin_notify_uid", lambda: "UADMIN")
    monkeypatch.setattr(server, "get_line_display_name_safe", lambda uid: "王營養師")
    monkeypatch.setattr(server, "_dietitian_checkin_last", {})
    return pushes, replies


def _event():
    return SimpleNamespace(reply_token="tok")


def test_checkin_pushes_own_uid_to_admin(sent):
    pushes, replies = sent
    uid = "U" + "a" * 32
    server.handle_dietitian_checkin(_event(), uid)
    assert pushes == [("UADMIN", pushes[0][1])]
    assert uid in pushes[0][1] and "王營養師" in pushes[0][1]
    assert "已通知老闆" in replies[0]


def test_checkin_cooldown(sent):
    pushes, replies = sent
    uid = "U" + "b" * 32
    server.handle_dietitian_checkin(_event(), uid)
    server.handle_dietitian_checkin(_event(), uid)
    assert len(pushes) == 1 and "已經收到" in replies[1]


def test_checkin_texts():
    assert "營養師報到" in server.DIETITIAN_CHECKIN_TEXTS
