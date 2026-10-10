from types import SimpleNamespace

import pytest

import server
from tests.test_sheet_atomic_meal_updates import _setup, _summary


REQUEST = "把 2099/10/01 午餐與 2099/10/02 午餐互換"
TAG = "[SWAP_MEAL: 2099/10/01_午餐, 2099/10/02_午餐]"


def _model_answer(monkeypatch, text):
    monkeypatch.setattr(
        server,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
        )))),
    )


def _run(tmp_path, monkeypatch, *, answer, request=REQUEST, event="RESULT"):
    uid, db_path, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _model_answer(monkeypatch, answer)
    server.user_memory.pop(uid, None)
    response, flex = server.get_ai_response_with_memory(uid, request, operation_key=f"line-ai:{uid}:{event}")
    return uid, db_path, worksheet, book, response, flex


def _assert_safe(worksheet, book, response):
    assert "一般 AI 對話不會直接修改菜單" in response
    assert "本次未修改菜單" in response
    assert "#延餐 10/15 午餐 -> 10/17 晚餐" in response
    assert "已調整完成" not in response
    assert "成功將" not in response
    assert book.batch_calls == []
    assert (worksheet.rows[0][2], worksheet.rows[1][2]) == ("餐A", "餐B")


def test_general_ai_single_swap_is_safe_refusal_contract(tmp_path, monkeypatch):
    uid, db_path, worksheet, book, response, flex = _run(
        tmp_path, monkeypatch, answer=f"✅ 已調整完成：虛構餐點 {TAG}"
    )
    _assert_safe(worksheet, book, response)
    assert flex is None
    assert _summary(db_path, uid) == "before"


@pytest.mark.parametrize("answer", ("✅ 都改好了", "✅ 都改好了 [SWAP_MEAL: broken]", f"✅ 都改好了 {TAG} {TAG}"))
def test_missing_malformed_or_multiple_tags_cannot_claim_success(tmp_path, monkeypatch, answer):
    _, _, worksheet, book, response, _ = _run(tmp_path, monkeypatch, answer=answer)
    _assert_safe(worksheet, book, response)


def test_unrelated_chat_is_not_rewritten(tmp_path, monkeypatch):
    _, _, _, book, response, _ = _run(
        tmp_path, monkeypatch, answer="我已調整說明方式，以下是熱量建議。",
        request="請把說明寫簡短一點", event="NORMAL-CHAT",
    )
    assert response == "我已調整說明方式，以下是熱量建議。"
    assert book.batch_calls == []


def test_adjacent_confirmation_is_safe_refusal_without_model_call(tmp_path, monkeypatch):
    uid, _, worksheet, book = _setup(tmp_path, monkeypatch, outcome="accept")
    _model_answer(monkeypatch, "✅ 已調整完成")
    server.user_memory[uid] = [
        {"role": "user", "content": "把 10/23 午餐跟晚餐改到 9/26"},
        {"role": "assistant", "content": "請確認是否要調整？"},
    ]
    response, flex = server.get_ai_response_with_memory(uid, "對", operation_key="confirm")
    _assert_safe(worksheet, book, response)
    assert flex is None


def test_source_and_target_slot_failures_remain_precise_backend_contract(tmp_path, monkeypatch):
    uid, _, worksheet, _ = _setup(tmp_path, monkeypatch, outcome="accept")
    source = server.execute_meal_swap(uid, "2099/10/09", "午餐", "2099/10/02", "午餐", operation_id=f"line-ai:{uid}:direct-source")
    target = server.execute_meal_swap(uid, "2099/10/01", "午餐", "2099/10/09", "午餐", operation_id=f"line-ai:{uid}:direct-target")
    assert "唯一且明確的來源餐位" in source and "2099/10/09午餐" in source
    assert "唯一且明確的目標餐位" in target and "2099/10/09午餐" in target
    assert worksheet.update_cell_calls == []
