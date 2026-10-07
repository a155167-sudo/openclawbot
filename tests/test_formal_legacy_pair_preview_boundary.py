"""Production preview must never enable the legacy full-year pair write path."""
import pytest
import server


@pytest.mark.parametrize('entrypoint', ['_execute_deferred_meal_move_unfenced', 'execute_deferred_meal_move'])
@pytest.mark.parametrize('enabled', [False, True])
def test_production_legacy_pair_denies_before_database_or_sheet(monkeypatch, entrypoint, enabled):
    monkeypatch.setattr(server, 'APP_ENV', 'production')
    monkeypatch.setattr(server, 'PAIR_RESCHEDULE_ENABLED', enabled)
    monkeypatch.setattr(server.sqlite3, 'connect', lambda *_a, **_k: pytest.fail('preview touched DB'))
    result = getattr(server, entrypoint)(
        'synthetic-owner', '2099-10-23', '午餐+晚餐', '2099-10-26', '午餐+晚餐',
        request_id='1', admin_uid='synthetic-admin')
    assert result[0] is False
    assert '唯讀預覽' in result[1]


def test_production_parser_does_not_enable_legacy_pair_submission(monkeypatch):
    monkeypatch.setattr(server, 'APP_ENV', 'production')
    assert server.parse_defer_command('#延餐 2099-10-23 午餐+晚餐 -> 2099-10-26 午餐+晚餐') is None
