import server
from dashboard_balance_acceptance_runner import run_cases


def test_thirteen_cases_through_native_producer_and_sdk(tmp_path, monkeypatch):
    result=run_cases(server,tmp_path)
    assert [c['case'] for c in result['cases']]==list(range(1,14))
    assert all(c['result']=='PASS' for c in result['cases'])
    assert set(result['examples'])=={'with_subscription','no_subscription','over'}
