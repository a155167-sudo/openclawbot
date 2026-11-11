"""SQLite 連線小工具。

`with sqlite3.connect(...) as conn:` 只會 commit／rollback，**不會關閉連線**；
在長時間執行的服務裡，沒關的連線會佔住檔案鎖，是「database is locked」的常見來源。
db_session 的行為和原本一樣（成功 commit、例外 rollback），多做一件事：離開時關閉連線。
"""
from __future__ import annotations

import os
from contextlib import contextmanager


@contextmanager
def db_session(conn):
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def describe_storage(db_dir: str, environ=os.environ) -> tuple[bool, str]:
    """資料庫資料夾是不是在 Railway Volume 上。回傳 (ok, 說明文字)。

    Railway 容器的檔案系統每次部署都會重置，只有 Volume 掛載路徑底下的檔案會留下來。
    Railway 會把掛載路徑放在 RAILWAY_VOLUME_MOUNT_PATH；不在 Railway 上（本機、測試）一律視為 ok。
    """
    on_railway = bool(environ.get("RAILWAY_ENVIRONMENT") or environ.get("RAILWAY_SERVICE_ID"))
    if not on_railway:
        return True, "not_on_railway"
    mount = str(environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if not mount:
        return False, "no_volume_attached"
    real_dir = os.path.realpath(db_dir)
    real_mount = os.path.realpath(mount)
    if real_dir == real_mount or real_dir.startswith(real_mount.rstrip("/") + "/"):
        return True, f"on_volume mount={mount}"
    return False, f"outside_volume mount={mount} db_dir={db_dir}"
