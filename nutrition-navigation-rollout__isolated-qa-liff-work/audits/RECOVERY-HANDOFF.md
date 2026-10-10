# Staging reschedule recovery handoff

Status: PREPARED, NOT DISPATCHED. No Jev/OpenClaw/Codex/Antigravity execution implied.

## Evidence
- audits/staging_schedule_recovery_audit.md
- tests/test_restore_contract_audit.py: parent rerun 2 passed; defect reproduction, not remediation.
- Live readback unavailable due blocked SSH consent. Do not bypass via another executor or credential path.
- Previous request expired. Do not change expiry or replay it.

## Immediate delivery scope
Offline candidate only, isolated from live service and credentials. First disable/remove dangerous startup repair hooks in candidate, with tests proving zero automatic destructive writes. Never edit/deploy live service as part of this lane.
Build a pure reconciliation planner that consumes sanitized exported evidence, preserves existing dispatch IDs, verifies payload/receipt/authority exact sets and returns a proposed diff or fail-closed blockers. It must not execute Sheet writes. Missing/ambiguous evidence blocks plan generation. Include padded legacy rows, duplicate dates with differing identity, 14-column snapshots, stale receipts, unknown outcomes and unrelated row preservation.

## Gates
1. Frozen baseline and scoped file ownership; backup before any server.py edit.
2. Regression tests RED then GREEN; independent code review of same candidate.
3. No invented writer capability evidence. Verify actual writers before acquiring lease.
4. Separate authorized live readback and immutable backup before proposing mutations.
5. Only after validated diff: separately controlled recovery with lease/CAS, unknown-outcome reconciliation and independent readback.
6. New unexpired customer request only after readiness, then LINE E2E and DB + personal/Master readback.

## External orchestration discovery
Found /home/win-xi/jev-task-orchestrator/prototype-v3/orchestrator.py.
DO NOT RUN AS IS: reads an API secret from historical message DB; prepares a fixed demo workspace by deleting it; contains silent static fallback. Not a validated recovery dispatcher. Do not extract historical credentials. Existing prototype is not evidence of a running team or Jev decision for this task.
