"""Observe native owned quota charges without replacing the permission authority."""
import server

def spy_native_charge(monkeypatch, calls):
    native = server._charge_text_meal_estimate_quota
    def charge(conn, **kwargs):
        calls.append(kwargs['user_id'])
        return native(conn, **kwargs)
    monkeypatch.setattr(server,'check_permission_and_quota',server._DEFAULT_CHECK_PERMISSION_AND_QUOTA)
    monkeypatch.setattr(server,'_charge_text_meal_estimate_quota',charge)
