"""Isolated read-only staging service for the dietitian health-check API.

This module intentionally does not import the LINE bot server, scheduler, webhook,
or any production datastore initialization code.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response

from dietitian_health_check_api import (
    DietitianHealthCheckConfig,
    attach_dietitian_health_check_routes,
    load_dietitian_health_check_config,
    load_health_check_detail,
    load_health_check_list,
    verify_line_id_token,
)

NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_fixture(environ: Mapping[str, str]) -> Path:
    raw_path = str(environ.get("DIETITIAN_HEALTH_CHECK_DB_PATH") or "").strip()
    expected = str(environ.get("DIETITIAN_HEALTH_CHECK_DB_SHA256") or "").strip()
    if not raw_path:
        raise RuntimeError("DIETITIAN_HEALTH_CHECK_DB_PATH is required")
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise RuntimeError("DIETITIAN_HEALTH_CHECK_DB_SHA256 must be canonical sha256")
    path = Path(raw_path).resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("dietitian staging fixture must be a regular file")
    actual = _sha256(path)
    if actual != expected:
        raise RuntimeError("dietitian staging fixture hash mismatch")
    return path


def create_app(
    environ: Mapping[str, str] | None = None,
    *,
    token_verifier: Any = verify_line_id_token,
) -> FastAPI:
    selected_environ = os.environ if environ is None else environ
    config: DietitianHealthCheckConfig = load_dietitian_health_check_config(selected_environ)
    if not config.enabled:
        raise RuntimeError("dedicated staging API must be explicitly enabled")
    fixture_path = _validated_fixture(selected_environ)

    app = FastAPI(
        title="一日樂食｜營養師三日健檢唯讀 Staging",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    browser_headers = {
        **NO_STORE_HEADERS,
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self' https://static.line-scdn.net; "
            "connect-src 'self' https://api.line.me https://access.line.me; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        ),
    }

    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>營養師三日健檢｜唯讀 Staging</title>
<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<script src="/staging-app.js" defer></script></head><body>
<h1>營養師三日健檢｜唯讀 Staging</h1>
<p>此頁只顯示去識別測試資料，所有寫入功能均已停用。</p>
<p id="status">正在驗證 LINE 登入…</p><pre id="result"></pre>
</body></html>""",
            headers=browser_headers,
        )

    @app.get("/staging-app.js", response_class=Response)
    def staging_app_script() -> Response:
        liff_id = json.dumps(config.liff_id)
        script = f"""'use strict';
(async () => {{
  const status = document.getElementById('status');
  const result = document.getElementById('result');
  try {{
    await liff.init({{liffId: {liff_id}}});
    if (!liff.isLoggedIn()) {{ liff.login(); return; }}
    const idToken = liff.getIDToken();
    if (!idToken) throw new Error('LINE ID Token unavailable');
    const response = await fetch('/api/dietitian/health-checks?status=ready_for_review', {{
      headers: {{Authorization: 'Bearer ' + idToken}},
      credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer'
    }});
    const payload = await response.json();
    if (!response.ok) throw new Error('授權失敗（HTTP ' + response.status + '）');
    status.textContent = 'LINE 白名單驗證成功；以下為去識別唯讀資料。';
    result.textContent = JSON.stringify(payload, null, 2);
  }} catch (error) {{
    status.textContent = error instanceof Error ? error.message : '驗證失敗';
    result.textContent = '';
  }}
}})();"""
        return Response(
            script,
            media_type="application/javascript",
            headers=browser_headers,
        )

    @app.get("/health", response_class=JSONResponse)
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "dietitian-health-check-read-staging",
                "dataset": "deidentified-fixture",
                "writes_enabled": False,
            },
            headers=NO_STORE_HEADERS,
        )

    def open_read_only_database() -> sqlite3.Connection:
        database_uri = fixture_path.as_uri() + "?mode=ro"
        return sqlite3.connect(database_uri, uri=True)

    def list_fixture_cases(**kwargs: Any) -> Mapping[str, object]:
        with closing(open_read_only_database()) as connection:
            return load_health_check_list(connection, **kwargs)

    def load_fixture_detail(case_id: str) -> Mapping[str, object] | None:
        with closing(open_read_only_database()) as connection:
            return load_health_check_detail(connection, case_id=case_id)

    attach_dietitian_health_check_routes(
        app,
        config=config,
        list_loader=list_fixture_cases,
        detail_loader=load_fixture_detail,
        token_verifier=token_verifier,
    )
    app.state.fixture_path = str(fixture_path)
    app.state.fixture_sha256 = str(selected_environ["DIETITIAN_HEALTH_CHECK_DB_SHA256"])
    return app
