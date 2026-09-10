from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .repository import HealthCheckRepository


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DEMO_ROOT = Path(tempfile.mkdtemp(prefix="yirile-dietitian-prototype-"))
DEFAULT_DEMO_DB = DEFAULT_DEMO_ROOT / "dietitian-demo.db"
PROTOTYPE_HTML = PACKAGE_DIR.parent / "prototypes" / "dietitian-3day-checkup-prototype.html"


def create_app(
    db_path: str | Path | None = None,
    *,
    safe_root: str | Path | None = None,
) -> FastAPI:
    """Create an isolated demo app; it never imports or opens the production bot DB."""
    if db_path is None:
        selected_db = DEFAULT_DEMO_DB
        selected_root = DEFAULT_DEMO_ROOT
    else:
        if safe_root is None:
            raise ValueError("自訂示範資料庫必須明確提供 safe_root")
        selected_db = Path(db_path)
        selected_root = Path(safe_root)
    repository = HealthCheckRepository(selected_db, safe_root=selected_root)
    repository.initialize_demo_data()

    app = FastAPI(
        title="一日樂食｜3日飲食健檢唯讀原型",
        docs_url="/prototype-docs",
        redoc_url=None,
    )
    app.state.prototype_repository = repository
    app.state.prototype_db_path = str(selected_db)

    @app.middleware("http")
    async def enforce_readonly_api(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method != "GET":
            return JSONResponse(
                status_code=405,
                content={"detail": "唯讀原型不接受寫入操作"},
                headers={"Allow": "GET"},
            )
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def prototype_home():
        html = PROTOTYPE_HTML.read_text(encoding="utf-8")
        html = html.replace(
            '<html lang="zh-Hant">',
            '<html lang="zh-Hant" data-api-mode="readonly">',
            1,
        )
        return HTMLResponse(html)

    @app.get("/api/health-checks")
    def list_health_checks(
        status: Literal["pending", "more", "done"] = Query(default="pending"),
    ):
        items = repository.list_cases(status)
        return {"status": status, "count": len(items), "items": items, "demo": True}

    @app.get("/api/health-checks/{case_id}")
    def get_health_check(case_id: str):
        case = repository.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="找不到示範案件")
        return case

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "service": "dietitian-readonly-prototype",
            "demo": True,
            "writes_enabled": False,
        }

    return app


app = create_app()
