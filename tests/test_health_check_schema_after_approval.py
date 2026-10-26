"""回歸：營養師第一次核准後會多一張 approval_operations 表，重啟時的結構檢查不能因此失敗。"""
import sqlite3
from contextlib import closing

from dietitian_health_check_approval import _ensure_operation_schema
from vip_health_check import configure_vip_health_check_connection, ensure_vip_health_check_schema


def test_restart_after_first_approval(tmp_path):
    path = str(tmp_path / "hc.db")
    with closing(sqlite3.connect(path)) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        _ensure_operation_schema(conn)
        conn.commit()
    with closing(sqlite3.connect(path)) as conn:  # 模擬重新部署後的啟動
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "vip_health_check_approval_operations" in names


def test_unknown_extra_table_still_refused(tmp_path):
    import pytest
    path = str(tmp_path / "hc2.db")
    with closing(sqlite3.connect(path)) as conn:
        configure_vip_health_check_connection(conn)
        ensure_vip_health_check_schema(conn)
        conn.execute("CREATE TABLE vip_health_check_mystery (x)")
        conn.commit()
    with closing(sqlite3.connect(path)) as conn:
        configure_vip_health_check_connection(conn)
        with pytest.raises(sqlite3.IntegrityError):
            ensure_vip_health_check_schema(conn)
