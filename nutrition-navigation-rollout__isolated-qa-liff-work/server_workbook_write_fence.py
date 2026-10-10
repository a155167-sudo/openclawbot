"""Trusted server-side scope for legacy writers sharing the reschedule workbook.

The scope is a no-op while the reschedule feature is disabled.  When enabled it
requires the durable writer capability and holds one workbook lease across the
caller's complete read/plan/write/readback/local-confirmation operation.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Iterator

from workbook_write_lease import (
    WorkbookLease,
    WorkbookLeaseConflict,
    acquire_workbook_lease,
    ensure_workbook_lease_schema,
    release_workbook_lease,
    require_full_writer_inventory,
)


@dataclass
class ServerWorkbookWriteFence:
    enabled: bool
    workbook_id: str
    lease: WorkbookLease | None = None
    write_started: bool = False
    _confirmed: bool = False
    _known_rejected: bool = False

    def mark_write_started(self) -> None:
        if self.enabled:
            self.write_started = True

    def confirm(self) -> None:
        if self.enabled:
            self._confirmed = True

    def reject_known_not_applied(self) -> None:
        """Record a provider response that proves the write was not accepted."""
        if self.enabled:
            self._known_rejected = True


@dataclass(frozen=True)
class _TrustedScope:
    db_path: str
    workbook_id: str
    fence: ServerWorkbookWriteFence


_ACTIVE_SCOPE: ContextVar[_TrustedScope | None] = ContextVar(
    "server_workbook_write_fence", default=None
)
def current_server_workbook_write_fence() -> ServerWorkbookWriteFence | None:
    """Return the trusted current scope; callers cannot install or forge one."""
    scope = _ACTIVE_SCOPE.get()
    return scope.fence if scope is not None else None


def _writer_inventory_is_complete(db_path: str, workbook_id: str, writer_id: str) -> bool:
    with sqlite3.connect(db_path, timeout=10) as conn:
        try:
            require_full_writer_inventory(
                conn, workbook_id=workbook_id, writer_id=writer_id,
            )
        except WorkbookLeaseConflict:
            return False
        return True


@contextmanager
def server_workbook_write_fence(
    *, db_path: str, workbook_id: str, writer_id: str, operation_id: str,
    enabled: bool, now: datetime, ttl_seconds: int = 120,
) -> Iterator[ServerWorkbookWriteFence]:
    """Hold or trusted-reenter one workbook lease.

    A successful write is released only after ``confirm()``.  A write-started
    scope that exits by exception or without confirmation remains active/unknown.
    """
    if enabled is not True:
        yield ServerWorkbookWriteFence(False, workbook_id)
        return
    if not all(isinstance(value, str) and value.strip() for value in (
        db_path, workbook_id, writer_id, operation_id
    )):
        raise WorkbookLeaseConflict("server workbook writer identity is invalid")

    active = _ACTIVE_SCOPE.get()
    if active is not None:
        if active.db_path != db_path or active.workbook_id != workbook_id:
            raise WorkbookLeaseConflict("nested workbook writer scope differs")
        if not _writer_inventory_is_complete(db_path, workbook_id, writer_id):
            raise WorkbookLeaseConflict("nested writer capability is not proven")
        yield active.fence
        return

    conn = sqlite3.connect(db_path, timeout=10)
    token = None
    fence = None
    try:
        ensure_workbook_lease_schema(conn)
        conn.commit()
        if not _writer_inventory_is_complete(db_path, workbook_id, writer_id):
            raise WorkbookLeaseConflict("controlled server writer inventory is incomplete")
        lease = acquire_workbook_lease(
            conn, workbook_id=workbook_id, writer_id=writer_id,
            operation_id=operation_id, now=now, ttl_seconds=ttl_seconds,
        )
        fence = ServerWorkbookWriteFence(True, workbook_id, lease=lease)
        token = _ACTIVE_SCOPE.set(_TrustedScope(db_path, workbook_id, fence))
        try:
            yield fence
        finally:
            if token is not None:
                _ACTIVE_SCOPE.reset(token)
        if fence._confirmed:
            release_workbook_lease(
                conn, lease_token=lease.lease_token,
                final_outcome="confirmed", now=now,
            )
        elif not fence.write_started or fence._known_rejected:
            release_workbook_lease(
                conn, lease_token=lease.lease_token,
                final_outcome="rejected_before_write", now=now,
            )
        # Otherwise the provider outcome/local confirmation is unknown: retain.
    except Exception:
        # A pre-write exception has a known no-write outcome.  Post-boundary
        # exceptions deliberately leave the active lease untouched.
        if fence is not None and fence.lease is not None and not fence.write_started:
            try:
                release_workbook_lease(
                    conn, lease_token=fence.lease.lease_token,
                    final_outcome="rejected_before_write", now=now,
                )
            except Exception:
                pass
        raise
    finally:
        conn.close()
