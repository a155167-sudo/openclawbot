from __future__ import annotations

import copy
import inspect
import json
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
import anyio
from fastapi import FastAPI

from customer_health_check_liff import LineAuthenticationError
from customer_reschedule_liff_routes import create_customer_reschedule_router
from isolated_reschedule_qa import (
    ALLOWLISTED_WORKBOOK_ID,
    create_isolated_reschedule_qa_router,
    isolated_reschedule_qa_enabled,
    load_isolated_reschedule_fixture,
    attach_isolated_reschedule_qa_routes,
)
from test_pair_reschedule_coordinator import (
    NOW,
    OWNER,
    SOURCE,
    TARGET,
    THIRD,
    STORE,
    old_source_row,
    open_db,
)


ADMIN = "U" + "9" * 32
CHANNEL_ID = "1234567890"
LIFF_ID = f"{CHANNEL_ID}-isolated"


def verifier(token: str, *, channel_id: str) -> str:
    if channel_id != CHANNEL_ID or not token.startswith("tok-"):
        raise LineAuthenticationError("bad token")
    uid = token[4:]
    if not re.fullmatch(r"U[0-9a-fA-F]{32}", uid):
        raise LineAuthenticationError("bad uid")
    return uid


class IsolatedSheetFake:
    def __init__(self, rows, *, outcome="ok"):
        self.rows = {row[0].replace("/", "-"): list(row) for row in rows}
        self.outcome = outcome
        self.batch_calls = []

    def read_rows(self, service_dates):
        return {day: copy.deepcopy(self.rows.get(day)) for day in service_dates}

    def plan_pair_reschedule(
        self,
        *,
        workbook_id,
        worksheet_id,
        owner_user_id,
        source_date,
        target_date,
        schedule_rows,
        expected_schedule_before,
        target_master_profile=None,
    ):
        assert workbook_id == ALLOWLISTED_WORKBOOK_ID
        assert worksheet_id == 17
        assert owner_user_id == OWNER
        assert {
            day: copy.deepcopy(self.rows.get(day)) for day in (source_date, target_date)
        } == expected_schedule_before
        master = {
            day: [day.replace("-", "/"), OWNER] + [""] * 19
            for day in (source_date, target_date)
        }
        return SimpleNamespace(
            source_date=source_date,
            target_date=target_date,
            schedule_rows=copy.deepcopy(schedule_rows),
            expected_readback={"schedule": copy.deepcopy(schedule_rows), "master": master},
        )

    def apply_pair_reschedule(self, plan):
        updates = [
            {"service_date": day, "values": plan.schedule_rows[day]}
            for day in (plan.source_date, plan.target_date)
        ]
        self.batch_calls.append(copy.deepcopy(updates))
        staged = copy.deepcopy(self.rows)
        for update in updates:
            staged[update["service_date"]] = list(update["values"])
        self.rows = staged
        if self.outcome == "timeout":
            raise TimeoutError("lost response")

    def read_pair_reschedule(self, plan):
        observed = {
            day: copy.deepcopy(self.rows.get(day))
            for day in (plan.source_date, plan.target_date)
        }
        if observed != plan.expected_readback["schedule"]:
            raise RuntimeError("readback differs")
        return {
            "schedule": observed,
            "master": copy.deepcopy(plan.expected_readback["master"]),
        }

    def read_pair_rows(self, *, owner_user_id, source_date, target_date):
        assert owner_user_id == OWNER
        return {
            "schedule": {
                day: copy.deepcopy(self.rows.get(day))
                for day in (source_date, target_date)
            },
            "master": {
                day: [day.replace("-", "/"), OWNER] + [""] * 19
                for day in (source_date, target_date)
            },
        }


class StalledSheetFake(IsolatedSheetFake):
    def __init__(self, rows, *, entered: threading.Event, release: threading.Event):
        super().__init__(rows)
        self.entered = entered
        self.release = release

    def apply_pair_reschedule(self, plan):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test stalled sheet was not released")
        super().apply_pair_reschedule(plan)


def isolated_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "customer-uat.sqlite3"
    conn, _ = open_db(
        db_path,
        workbook_id=ALLOWLISTED_WORKBOOK_ID,
        worksheet_id=17,
        worksheet_title="owner-fixture",
    )
    conn.execute("UPDATE admin_settings SET value=? WHERE key='admin_id'", (ADMIN,))
    conn.execute(
        "UPDATE usage SET last_date='2026-09-01', expiry_date=? WHERE user_id=?",
        (TARGET, OWNER),
    )
    conn.commit()
    conn.close()
    return db_path


def manifest(tmp_path: Path, db_path: Path, **overrides) -> Path:
    payload = {
        "workbook_id": ALLOWLISTED_WORKBOOK_ID,
        "personal_worksheet_id": 17,
        "personal_worksheet_title": "owner-fixture",
        "master_worksheet_id": 202,
        "master_worksheet_title": "Master_API_View",
        "order_id": 1,
        "owner_user_id": OWNER,
        "admin_user_id": ADMIN,
        "original_expiry_date": TARGET,
        "db_path": str(db_path),
    }
    payload.update(overrides)
    path = tmp_path / "customer-uat.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def client_for(tmp_path: Path, *, sheet: IsolatedSheetFake | None = None):
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)
    active_sheet = sheet or IsolatedSheetFake([old_source_row() + ["dispatch-old", "1", "order-1-v1"]])
    calls = []

    def factory(conn, order_id, fixture):
        calls.append((order_id, fixture.workbook_id, fixture.personal_worksheet_id))
        return active_sheet

    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: NOW,
            adapter_factory=factory,
        )
    )
    return ASGIClient(app), active_sheet, calls, db_path, manifest_path


class ASGIClient:
    def __init__(self, app):
        self._app = app

    def get(self, url: str, **kwargs):
        return anyio.run(self._bounded_request, "GET", url, kwargs)

    def post(self, url: str, **kwargs):
        return anyio.run(self._bounded_request, "POST", url, kwargs)

    async def _bounded_request(self, method: str, url: str, kwargs):
        with anyio.fail_after(5):
            return await self._request(method, url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs):
        transport = httpx.ASGITransport(app=self._app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, url, **kwargs)


def auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer tok-{uid}"}


def submit_payload(request_id: str = "qa-request-1") -> dict[str, object]:
    return {
        "order_id": 1,
        "source_date": SOURCE,
        "target_date": TARGET,
        "request_id": request_id,
    }


def submit_payload_for(
    source_date: str,
    target_date: str,
    request_id: str = "qa-request-1",
) -> dict[str, object]:
    return {
        "order_id": 1,
        "source_date": source_date,
        "target_date": target_date,
        "request_id": request_id,
    }


def rewrite_current_dispatch_source(
    db_path: Path,
    *,
    source_date: str,
    expiry_date: str,
    next_date: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE usage SET last_date=?, expiry_date=? WHERE user_id=?",
            ("2026-09-01", expiry_date, OWNER),
        )
        row = old_source_row()
        row[0] = source_date
        row[1] = "權益最後日"
        payload = {
            "owner_user_id": OWNER,
            "store_id": STORE,
            "workbook_id": ALLOWLISTED_WORKBOOK_ID,
            "worksheet_id": 17,
            "rows": [{
                "service_date": source_date,
                "columns": row,
                "dispatch_row_id": "dispatch-current",
            }],
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute("DROP TRIGGER IF EXISTS reschedule_dispatch_versions_no_update")
        conn.execute(
            """UPDATE reschedule_dispatch_versions
                  SET payload_json=?
                WHERE order_id=1 AND owner_user_id=?""",
            (payload_json, OWNER),
        )
        conn.execute(
            "DELETE FROM subscription_service_calendar WHERE order_id=1 AND service_date IN (?,?)",
            (source_date, next_date),
        )
        conn.executemany(
            "INSERT INTO subscription_service_calendar VALUES(1,?,1,?)",
            [(source_date, "權益最後日"), (next_date, "權益外隔日")],
        )
        conn.commit()


def test_default_disabled_and_production_enabled_rejected(tmp_path):
    assert isolated_reschedule_qa_enabled({}) is False
    assert isolated_reschedule_qa_enabled({"ISOLATED_RESCHEDULE_QA_ENABLED": "false"}) is False
    assert isolated_reschedule_qa_enabled({"ISOLATED_RESCHEDULE_QA_ENABLED": "true"}) is True
    with pytest.raises(ValueError):
        isolated_reschedule_qa_enabled({"ISOLATED_RESCHEDULE_QA_ENABLED": "yes"})

    app = FastAPI()
    mounted = attach_isolated_reschedule_qa_routes(
        app,
        environ={
            "APP_ENV": "staging",
            "ISOLATED_RESCHEDULE_QA_ENABLED": "false",
            "CUSTOMER_RESCHEDULE_LIFF_ID": LIFF_ID,
            "CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
        },
        main_db_path=str(tmp_path / "main.sqlite3"),
    )
    assert mounted is False
    assert not any(
        getattr(route, "path", "") == "/customer-reschedule"
        for route in app.router.routes
    )

    with pytest.raises(RuntimeError, match="staging"):
        attach_isolated_reschedule_qa_routes(
            FastAPI(),
            environ={
                "APP_ENV": "production",
                "ISOLATED_RESCHEDULE_QA_ENABLED": "true",
                "CUSTOMER_RESCHEDULE_LIFF_ID": LIFF_ID,
                "CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
            },
            main_db_path=str(tmp_path / "main.sqlite3"),
        )


def test_shared_customer_reschedule_get_endpoints_remain_sync_by_default():
    router = create_customer_reschedule_router(
        liff_id=LIFF_ID,
        channel_id=CHANNEL_ID,
        db_path="/tmp/nonexistent-customer-reschedule.db",
        app_env="staging",
        token_verifier=verifier,
    )
    endpoints = {
        (next(iter(route.methods)), route.path): route.endpoint
        for route in router.routes
        if hasattr(route, "methods")
    }

    for key in (
        ("GET", "/customer-reschedule"),
        ("GET", "/customer-reschedule/context"),
        ("GET", "/api/admin/customer-pair-reschedule-requests"),
        ("GET", "/customer-reschedule/preview"),
    ):
        assert not inspect.iscoroutinefunction(endpoints[key])
    assert inspect.iscoroutinefunction(
        endpoints[("POST", "/customer-reschedule/pending-request")]
    )


def test_manifest_and_db_guard_fail_closed_before_network(tmp_path):
    db_path = isolated_db(tmp_path)
    bad_book = manifest(tmp_path, db_path, workbook_id="book-1")
    with pytest.raises(RuntimeError, match="workbook"):
        load_isolated_reschedule_fixture(
            db_path=str(db_path),
            manifest_path=str(bad_book),
            main_db_path=str(tmp_path / "main.sqlite3"),
        )

    missing_db_manifest = manifest(tmp_path, tmp_path / "missing.sqlite3")
    with pytest.raises(RuntimeError, match="database"):
        load_isolated_reschedule_fixture(
            db_path=str(tmp_path / "missing.sqlite3"),
            manifest_path=str(missing_db_manifest),
            main_db_path=str(tmp_path / "main.sqlite3"),
        )

    good_manifest = manifest(tmp_path, db_path)
    with pytest.raises(RuntimeError, match="main DB"):
        load_isolated_reschedule_fixture(
            db_path=str(db_path),
            manifest_path=str(good_manifest),
            main_db_path=str(db_path),
        )

    missing_anchor = manifest(tmp_path, db_path)
    payload = json.loads(missing_anchor.read_text(encoding="utf-8"))
    payload.pop("original_expiry_date")
    missing_anchor.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="original_expiry_date"):
        load_isolated_reschedule_fixture(
            db_path=str(db_path),
            manifest_path=str(missing_anchor),
            main_db_path=str(tmp_path / "main.sqlite3"),
        )

    spoofed_anchor = manifest(tmp_path, db_path, original_expiry_date="2026-11-30")
    with pytest.raises(RuntimeError, match="original expiry anchor"):
        load_isolated_reschedule_fixture(
            db_path=str(db_path),
            manifest_path=str(spoofed_anchor),
            main_db_path=str(tmp_path / "main.sqlite3"),
        )

    good_manifest = manifest(tmp_path, db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE subscription_dispatch_rows SET workbook_id='wrong'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="publication"):
        load_isolated_reschedule_fixture(
            db_path=str(db_path),
            manifest_path=str(good_manifest),
            main_db_path=str(tmp_path / "main.sqlite3"),
        )


def test_context_auth_and_spoofed_uid_ignored(tmp_path):
    client, _sheet, _calls, _db_path, _manifest_path = client_for(tmp_path)

    assert client.get("/customer-reschedule/context").status_code == 401

    wrong = "U" + "2" * 32
    assert client.get("/customer-reschedule/context", headers=auth(wrong)).status_code == 403

    response = client.get(
        "/customer-reschedule/context?user_id=" + wrong,
        headers=auth(OWNER),
    )
    assert response.status_code == 200
    assert response.json()["orders"][0]["order_id"] == 1
    assert response.json()["orders"][0]["source_dates"][0]["date"] == SOURCE


def test_post_confirmed_hop_accepts_target_within_original_expiry_plus_30(tmp_path):
    db_path = isolated_db(tmp_path)
    rewrite_current_dispatch_source(
        db_path,
        source_date="2026-10-07",
        expiry_date="2026-10-07",
        next_date="2026-10-08",
    )
    manifest_path = manifest(tmp_path, db_path, original_expiry_date="2026-10-07")
    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: datetime.fromisoformat("2026-10-06T07:59:00+08:00"),
            adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        )
    )
    client = ASGIClient(app)

    context = client.get("/customer-reschedule/context", headers=auth(OWNER))
    assert context.status_code == 200
    order = context.json()["orders"][0]
    assert [item["date"] for item in order["source_dates"]] == ["2026-10-07"]
    assert "2026-10-08" in order["target_dates"]

    preview = client.get(
        "/customer-reschedule/preview?order_id=1&source_date=2026-10-07&target_date=2026-10-08",
        headers=auth(OWNER),
    )
    assert preview.status_code == 200
    assert preview.json()["target_date"] == "2026-10-08"

    submitted = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload_for("2026-10-07", "2026-10-08", "qa-second-hop"),
        headers=auth(OWNER),
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "pending_admin"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM customer_pair_reschedule_requests WHERE request_id='qa-second-hop'"
        ).fetchone()[0] == 1


def test_post_confirmed_hop_rejects_target_after_original_expiry_plus_30(tmp_path):
    db_path = isolated_db(tmp_path)
    rewrite_current_dispatch_source(
        db_path,
        source_date="2026-10-07",
        expiry_date="2026-10-07",
        next_date="2026-11-07",
    )
    manifest_path = manifest(tmp_path, db_path, original_expiry_date="2026-10-07")
    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: datetime.fromisoformat("2026-10-06T07:59:00+08:00"),
            adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        )
    )
    client = ASGIClient(app)

    context = client.get("/customer-reschedule/context", headers=auth(OWNER))
    assert context.status_code == 200
    assert "2026-11-07" not in context.json()["orders"][0]["target_dates"]

    preview = client.get(
        "/customer-reschedule/preview?order_id=1&source_date=2026-10-07&target_date=2026-11-07",
        headers=auth(OWNER),
    )
    assert preview.status_code == 422

    submitted = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload_for("2026-10-07", "2026-11-07", "qa-plus-31"),
        headers=auth(OWNER),
    )
    assert submitted.status_code == 422
    assert "30 days" in submitted.json()["detail"] or "有效權益" in submitted.json()["detail"]
    with sqlite3.connect(db_path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_pair_reschedule_requests'"
        ).fetchone()
        exists = None if table is None else conn.execute(
            "SELECT 1 FROM customer_pair_reschedule_requests WHERE request_id='qa-plus-31'"
        ).fetchone()
        assert exists is None


def test_submit_pending_admin_then_admin_approve_confirmed_and_replay_one_batch(tmp_path):
    client, sheet, calls, db_path, _manifest_path = client_for(tmp_path)

    context = client.get("/customer-reschedule/context", headers=auth(OWNER))
    assert context.status_code == 200
    order = context.json()["orders"][0]
    source_item = order["source_dates"][0]
    offered_source = source_item["date"]
    offered_target = order["target_dates"][0]
    assert offered_source == SOURCE
    assert offered_target == TARGET
    assert source_item["meals"] == [
        {"meal": "午餐", "label": "午餐A"},
        {"meal": "晚餐", "label": "晚餐B"},
    ]

    preview = client.get(
        f"/customer-reschedule/preview?order_id=1&source_date={offered_source}&target_date={offered_target}",
        headers=auth(OWNER),
    )
    assert preview.status_code == 200
    assert preview.json()["source_meals"] == source_item["meals"]
    assert preview.json()["target_date"] == offered_target

    response = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload_for(offered_source, offered_target),
        headers=auth(OWNER),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending_admin"
    assert response.json()["admin_notification_status"] == "deferred"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO customer_pair_reschedule_requests
               (request_id,order_id,owner_user_id,source_date,target_date,status,
                operation_id,created_at,expires_at,admin_notification_status)
               VALUES('qa-request-duplicate',1,?,?,?,'pending_admin',NULL,?,?, 'deferred')""",
            (
                OWNER,
                offered_source,
                offered_target,
                "2026-09-25T07:59:00+08:00",
                "2026-09-26T08:14:00+08:00",
            ),
        )
        conn.commit()

    pending = client.get(
        "/api/admin/customer-pair-reschedule-requests",
        headers=auth(ADMIN),
    )
    assert pending.status_code == 200
    assert pending.json()["requests"][0]["request_id"] == "qa-request-1"
    assert "owner_user_id" not in pending.text

    wrong_admin = "U" + "8" * 32
    assert client.post(
        "/api/admin/customer-pair-reschedule-requests/qa-request-1/approve",
        json={"actor_id": ADMIN},
        headers=auth(wrong_admin),
    ).status_code == 403

    spoofed_actor = client.post(
        "/api/admin/customer-pair-reschedule-requests/qa-request-1/approve",
        json={"actor_id": wrong_admin},
        headers=auth(ADMIN),
    )
    assert spoofed_actor.status_code == 422

    approved = client.post(
        "/api/admin/customer-pair-reschedule-requests/qa-request-1/approve",
        json={},
        headers=auth(ADMIN),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "confirmed"
    assert len(sheet.batch_calls) == 1
    assert calls == [(1, ALLOWLISTED_WORKBOOK_ID, 17)]

    replay = client.post(
        "/api/admin/customer-pair-reschedule-requests/qa-request-1/approve",
        json={},
        headers=auth(ADMIN),
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "confirmed"
    assert len(sheet.batch_calls) == 1
    assert calls == [(1, ALLOWLISTED_WORKBOOK_ID, 17)]

    duplicate_after_canonical = client.post(
        "/api/admin/customer-pair-reschedule-requests/qa-request-duplicate/approve",
        json={},
        headers=auth(ADMIN),
    )
    assert duplicate_after_canonical.status_code == 409
    assert len(sheet.batch_calls) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT status FROM customer_pair_reschedule_requests WHERE request_id='qa-request-1'"
        ).fetchone()[0] == "confirmed"
        assert conn.execute(
            "SELECT status FROM customer_pair_reschedule_requests WHERE request_id='qa-request-duplicate'"
        ).fetchone()[0] == "pending_admin"
        assert conn.execute("SELECT meal_count FROM subscription_orders WHERE id=1").fetchone()[0] == 20
        assert conn.execute(
            "SELECT remaining_meals FROM usage WHERE user_id=?",
            (OWNER,),
        ).fetchone()[0] == 8

    refreshed = client.get("/customer-reschedule/context", headers=auth(OWNER))
    assert refreshed.status_code == 200
    refreshed_order = refreshed.json()["orders"][0]
    assert [item["date"] for item in refreshed_order["source_dates"]] == [TARGET]
    assert refreshed_order["source_dates"][0]["meals"] == source_item["meals"]
    assert SOURCE not in refreshed_order["target_dates"]

    second_target = THIRD
    assert second_target in refreshed_order["target_dates"]
    second = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload_for(TARGET, second_target, "qa-request-2"),
        headers=auth(OWNER),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "pending_admin"
    second_approved = client.post(
        "/api/admin/customer-pair-reschedule-requests/qa-request-2/approve",
        json={},
        headers=auth(ADMIN),
    )
    assert second_approved.status_code == 200
    assert second_approved.json()["status"] == "confirmed"
    assert len(sheet.batch_calls) == 2


def test_same_semantic_pending_request_replays_existing_request_id(tmp_path):
    client, _sheet, _calls, db_path, _manifest_path = client_for(tmp_path)

    first = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-request-original"),
        headers=auth(OWNER),
    )
    assert first.status_code == 200
    assert first.json()["request_id"] == "qa-request-original"

    second = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-request-after-reload"),
        headers=auth(OWNER),
    )
    assert second.status_code == 200
    assert second.json()["request_id"] == "qa-request-original"
    assert second.json()["status"] == "pending_admin"

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT request_id,status,operation_id
                 FROM customer_pair_reschedule_requests
                WHERE order_id=1 AND owner_user_id=? AND source_date=? AND target_date=?""",
            (OWNER, SOURCE, TARGET),
        ).fetchall()
    assert rows == [("qa-request-original", "pending_admin", None)]


def test_semantic_pending_replay_accepts_expiry_equality_boundary(tmp_path):
    current_now = {"value": NOW}
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)
    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: current_now["value"],
            adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        )
    )
    client = ASGIClient(app)

    first = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-boundary-original"),
        headers=auth(OWNER),
    )
    assert first.status_code == 200
    current_now["value"] = datetime.fromisoformat(first.json()["expires_at"])

    replay = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-boundary-after-reload"),
        headers=auth(OWNER),
    )

    assert replay.status_code == 200
    assert replay.json()["request_id"] == "qa-boundary-original"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM customer_pair_reschedule_requests"
        ).fetchone()[0] == 1


def test_expired_semantic_pending_rows_do_not_trap_fresh_submit(tmp_path):
    current_now = {"value": NOW}
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)
    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: current_now["value"],
            adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        )
    )
    client = ASGIClient(app)

    first = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-expired-original"),
        headers=auth(OWNER),
    )
    assert first.status_code == 200
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO customer_pair_reschedule_requests
               (request_id,order_id,owner_user_id,source_date,target_date,status,
                operation_id,created_at,expires_at,admin_notification_status)
               VALUES('qa-expired-duplicate',1,?,?,?,'pending_admin',NULL,?,?, 'deferred')""",
            (
                OWNER,
                SOURCE,
                TARGET,
                "2026-09-26T08:00:00+08:00",
                "2026-09-26T08:15:00+08:00",
            ),
        )
        conn.commit()

    current_now["value"] = datetime.fromisoformat("2026-09-26T08:16:00+08:00")
    fresh = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-fresh-after-expired"),
        headers=auth(OWNER),
    )

    assert fresh.status_code == 200
    assert fresh.json()["request_id"] == "qa-fresh-after-expired"
    assert fresh.json()["expires_at"] == "2026-09-26T08:31:00+08:00"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT request_id,status,created_at,expires_at
                 FROM customer_pair_reschedule_requests
                WHERE order_id=1 AND owner_user_id=? AND source_date=? AND target_date=?
                ORDER BY created_at ASC, request_id ASC""",
            (OWNER, SOURCE, TARGET),
        ).fetchall()
    assert [tuple(row[:2]) for row in rows] == [
        ("qa-expired-original", "pending_admin"),
        ("qa-expired-duplicate", "pending_admin"),
        ("qa-fresh-after-expired", "pending_admin"),
    ]
    valid_fresh = [
        row for row in rows
        if row[0] == "qa-fresh-after-expired"
        and datetime.fromisoformat(row[3]) >= current_now["value"]
    ]
    assert len(valid_fresh) == 1


def test_sheet_unknown_semantic_replay_and_admin_action_ignore_expiry(tmp_path):
    current_now = {"value": NOW}
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)
    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: current_now["value"],
            adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        )
    )
    client = ASGIClient(app)
    assert client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-unknown-original"),
        headers=auth(OWNER),
    ).status_code == 200
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE customer_pair_reschedule_requests
                  SET status='sheet_unknown',expires_at='2026-09-26T08:00:00+08:00'
                WHERE request_id='qa-unknown-original'"""
        )
        conn.commit()
    current_now["value"] = datetime.fromisoformat("2026-09-26T09:00:00+08:00")

    replay = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-unknown-after-ttl"),
        headers=auth(OWNER),
    )
    actionable = client.get(
        "/api/admin/customer-pair-reschedule-requests",
        headers=auth(ADMIN),
    )

    assert replay.status_code == 200
    assert replay.json()["request_id"] == "qa-unknown-original"
    assert replay.json()["status"] == "sheet_unknown"
    assert actionable.status_code == 200
    assert actionable.json()["requests"][0]["can_reconcile"] is True
    assert actionable.json()["requests"][0]["can_approve"] is False


def test_admin_all_exposes_expired_and_malformed_pending_as_disabled(tmp_path):
    current_now = {"value": NOW}
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)
    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: current_now["value"],
            adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        )
    )
    client = ASGIClient(app)
    assert client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("qa-expired-for-admin"),
        headers=auth(OWNER),
    ).status_code == 200
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO customer_pair_reschedule_requests
               (request_id,order_id,owner_user_id,source_date,target_date,status,
                operation_id,created_at,expires_at,admin_notification_status)
               VALUES('qa-malformed-expiry',1,?,?,?,'pending_admin',NULL,?,?, 'deferred')""",
            (
                OWNER,
                SOURCE,
                THIRD,
                "2026-09-26T08:01:00+08:00",
                "not-a-date",
            ),
        )
        conn.commit()
    current_now["value"] = datetime.fromisoformat("2026-09-26T08:15:01+08:00")

    actionable = client.get(
        "/api/admin/customer-pair-reschedule-requests",
        headers=auth(ADMIN),
    )
    all_rows = client.get(
        "/api/admin/customer-pair-reschedule-requests?status=all",
        headers=auth(ADMIN),
    )

    assert actionable.status_code == 200
    assert actionable.json()["requests"] == []
    assert all_rows.status_code == 200
    by_id = {item["request_id"]: item for item in all_rows.json()["requests"]}
    assert by_id["qa-expired-for-admin"]["can_approve"] is False
    assert by_id["qa-expired-for-admin"]["disabled_reason"] == "申請已逾期，請顧客重新送出申請。"
    assert by_id["qa-malformed-expiry"]["can_approve"] is False
    assert by_id["qa-malformed-expiry"]["disabled_reason"] == "申請期限格式異常，請重新送出申請。"


def test_concurrent_same_semantic_submissions_create_one_pending_request(tmp_path):
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)
    app = FastAPI()
    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: NOW,
            adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        )
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=5,
        ) as async_client:
            responses = []

            async def submit(request_id):
                responses.append(await async_client.post(
                    "/customer-reschedule/pending-request",
                    json=submit_payload(request_id),
                    headers=auth(OWNER),
                ))

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(submit, "qa-concurrent-1")
                task_group.start_soon(submit, "qa-concurrent-2")

        return responses

    responses = anyio.run(scenario)
    assert sorted(response.status_code for response in responses) == [200, 200]
    request_ids = {response.json()["request_id"] for response in responses}
    assert len(request_ids) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            """SELECT COUNT(*)
                 FROM customer_pair_reschedule_requests
                WHERE order_id=1 AND owner_user_id=? AND source_date=? AND target_date=?""",
            (OWNER, SOURCE, TARGET),
        ).fetchone()[0] == 1


def test_timeout_stays_sheet_unknown_then_bounded_readback_can_confirm(tmp_path):
    sheet = IsolatedSheetFake(
        [old_source_row() + ["dispatch-old", "1", "order-1-v1"]],
        outcome="timeout",
    )
    client, sheet, _calls, db_path, _manifest_path = client_for(tmp_path, sheet=sheet)
    assert client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("timeout-request"),
        headers=auth(OWNER),
    ).status_code == 200

    approved = client.post(
        "/api/admin/customer-pair-reschedule-requests/timeout-request/approve",
        json={},
        headers=auth(ADMIN),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "sheet_unknown"
    assert len(sheet.batch_calls) == 1

    unknown = client.get(
        "/api/admin/customer-pair-reschedule-requests",
        headers=auth(ADMIN),
    )
    assert unknown.status_code == 200
    assert unknown.json()["requests"][0]["request_id"] == "timeout-request"
    assert unknown.json()["requests"][0]["status"] == "sheet_unknown"
    assert unknown.json()["requests"][0]["can_reconcile"] is True

    replay = client.post(
        "/customer-reschedule/pending-request",
        json=submit_payload("timeout-request-after-reload"),
        headers=auth(OWNER),
    )
    assert replay.status_code == 200
    assert replay.json()["request_id"] == "timeout-request"
    assert replay.json()["status"] == "sheet_unknown"

    reconciled = client.post(
        "/api/admin/customer-pair-reschedule-requests/timeout-request/reconcile",
        json={},
        headers=auth(ADMIN),
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "confirmed"
    assert len(sheet.batch_calls) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT status FROM customer_pair_reschedule_requests WHERE request_id='timeout-request'"
        ).fetchone()[0] == "confirmed"


def test_stalled_admin_approve_does_not_block_independent_async_endpoint(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    sheet = StalledSheetFake(
        [old_source_row() + ["dispatch-old", "1", "order-1-v1"]],
        entered=entered,
        release=release,
    )
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)

    def factory(_conn, _order_id, _fixture):
        return sheet

    app = FastAPI()

    @app.get("/async-health")
    async def async_health():
        return {"ok": True}

    app.include_router(
        create_isolated_reschedule_qa_router(
            liff_id=LIFF_ID,
            channel_id=CHANNEL_ID,
            app_env="staging",
            db_path=str(db_path),
            manifest_path=str(manifest_path),
            token_verifier=verifier,
            now_factory=lambda: NOW,
            adapter_factory=factory,
        )
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=5,
        ) as async_client:
            created = await async_client.post(
                "/customer-reschedule/pending-request",
                json=submit_payload("stalled-request"),
                headers=auth(OWNER),
            )
            assert created.status_code == 200
            approved_holder = {}

            async def approve_request():
                approved_holder["response"] = await async_client.post(
                    "/api/admin/customer-pair-reschedule-requests/stalled-request/approve",
                    json={},
                    headers=auth(ADMIN),
                )

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(approve_request)
                assert await anyio.to_thread.run_sync(lambda: entered.wait(2))
                health = await async_client.get("/async-health")
                assert health.status_code == 200
                assert health.json() == {"ok": True}
                release.set()
            assert approved_holder["response"].status_code == 200
            assert approved_holder["response"].json()["status"] == "confirmed"

    anyio.run(scenario)


def test_attach_does_not_touch_main_db_bytes(tmp_path):
    main_db = tmp_path / "main.sqlite3"
    main_db.write_bytes(b"main-db-sentinel")
    db_path = isolated_db(tmp_path)
    manifest_path = manifest(tmp_path, db_path)
    before = main_db.read_bytes()

    app = FastAPI()
    mounted = attach_isolated_reschedule_qa_routes(
        app,
        environ={
            "APP_ENV": "staging",
            "ISOLATED_RESCHEDULE_QA_ENABLED": "true",
            "CUSTOMER_RESCHEDULE_LIFF_ID": LIFF_ID,
            "CUSTOMER_RESCHEDULE_LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
            "ISOLATED_RESCHEDULE_QA_DB_PATH": str(db_path),
            "ISOLATED_RESCHEDULE_QA_MANIFEST_PATH": str(manifest_path),
        },
        main_db_path=str(main_db),
        token_verifier=verifier,
        adapter_factory=lambda _conn, _order_id, _fixture: IsolatedSheetFake([]),
        now_factory=lambda: NOW,
    )
    assert mounted is True
    assert main_db.read_bytes() == before
    page = ASGIClient(app).get("/customer-reschedule")
    assert page.status_code == 200
    assert "TEST ONLY" in page.text


def test_runtime_adapter_uses_google_credentials_json_and_timeout(monkeypatch):
    import sys
    import types

    from isolated_reschedule_qa import _runtime_adapter_factory

    calls = {}

    class FakeCredentials:
        @classmethod
        def from_service_account_info(cls, info, *, scopes):
            calls["credentials_info"] = info
            calls["scopes"] = scopes
            return "credentials"

    class FakeClient:
        def set_timeout(self, value):
            calls["timeout"] = value

        def open_by_key(self, key):
            calls["book_key"] = key
            return FakeBook()

    class FakeWorksheet:
        def __init__(self, title):
            self.title = title

    class FakeBook:
        def get_worksheet_by_id(self, worksheet_id):
            if worksheet_id == 17:
                return FakeWorksheet("owner-fixture")
            if worksheet_id == 202:
                return FakeWorksheet("Master_API_View")
            return None

    class FakeAdapter:
        def __init__(self, book, schedule, master, *, workbook_id):
            calls["adapter"] = (schedule.title, master.title, workbook_id)

    monkeypatch.setenv("GOOGLE_CREDENTIALS", json.dumps({"client_email": "qa@example.test"}))
    monkeypatch.setitem(sys.modules, "gspread", types.SimpleNamespace(authorize=lambda credentials: FakeClient()))
    monkeypatch.setitem(
        sys.modules,
        "google.oauth2.service_account",
        types.SimpleNamespace(Credentials=FakeCredentials),
    )
    monkeypatch.setitem(
        sys.modules,
        "gspread_pair_reschedule_adapter",
        types.SimpleNamespace(GspreadPairRescheduleAdapter=FakeAdapter),
    )
    fixture = SimpleNamespace(
        workbook_id=ALLOWLISTED_WORKBOOK_ID,
        personal_worksheet_id=17,
        personal_worksheet_title="owner-fixture",
        master_worksheet_id=202,
        master_worksheet_title="Master_API_View",
    )

    _runtime_adapter_factory(None, 1, fixture)

    assert calls["credentials_info"] == {"client_email": "qa@example.test"}
    assert calls["timeout"] == 15
    assert calls["book_key"] == ALLOWLISTED_WORKBOOK_ID
    assert calls["adapter"] == ("owner-fixture", "Master_API_View", ALLOWLISTED_WORKBOOK_ID)


def test_customer_reschedule_qa_liff_emitted_javascript_behavior():
    harness = Path(__file__).with_name("customer_reschedule_qa_liff_behavior.js")
    html_path = Path(__file__).resolve().parents[1] / "customer-reschedule-qa-liff.html"
    check = subprocess.run(
        ["node", "--check", str(harness)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    result = subprocess.run(
        ["node", str(harness), str(html_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
