# Production VIP Health-Check Dark-Deploy Runbook

This runbook applies only to a dark deployment with both controls pinned off:

```text
ENABLE_SCHEDULER=false
VIP_HEALTH_CHECK_ENABLED=false
```

It does **not** authorize creating or advertising the production customer LIFF, enabling customer routes, or granting a historical-user backfill.

## Roles and stop conditions

- Release owner: Jason (explicit merge/deploy approval).
- Executor: the reviewed automation session acting within that approval.
- Stop before merge if any exact-commit reviewer returns `BLOCK`, the branch changes after review, the production source branch/topology differs from the recorded baseline, or the backup/rehearsal checks are not all green.
- Roll back immediately if Railway does not deploy the exact merge SHA, startup fails, `/health` or `/callback` regresses, the feature routes are visible while disabled, or production logs show a new traceback/5xx pattern.

## 1. Freeze the release candidate

Record all of the following in the release evidence:

```bash
git fetch origin --prune
git rev-parse HEAD
git rev-parse origin/main
git status --porcelain
git diff --check origin/main...HEAD
python3 -m pytest -q
```

The worktree must be clean and all independent reviewers must have reviewed the same HEAD and diff hash.

## 2. Verify live controls before touching data

Read the production Railway variables without printing secret values. Confirm:

```text
APP_ENV=production
ENABLE_SCHEDULER=false
VIP_HEALTH_CHECK_ENABLED=false
SURVEY_REWARD_LINK_COUNT=1
SURVEY_REWARD_POINTS_PER_LINK=2
```

Also record the active service ID, environment ID, deployment ID, commit SHA, public domain, volume mount, and source branch. Abort if the source is not the production `main` branch or the volume is not mounted at `/app/data`.

## 3. Create and verify a timestamped SQLite online backup

Never copy a live SQLite file with plain `cp`. Inside the production container, use the SQLite backup API to write a new timestamped file under `/app/data/backups/`. Open the resulting backup read-only and require:

```sql
PRAGMA integrity_check;      -- exactly: ok
PRAGMA foreign_key_check;    -- zero rows
```

Record the backup path, byte size, SHA-256, table count, and check results. Never print row contents or credentials.

## 4. Rehearse the candidate migration on a backup copy

Fetch `vip_health_check.py` and `scripts/rehearse_vip_health_check_migration.py` from the exact public Git commit into a temporary directory. Do not fetch a mutable branch name. Run:

```bash
python3 /tmp/vip-health-rehearsal/scripts/rehearse_vip_health_check_migration.py \
  --source /app/data/backups/<verified-backup>.db \
  --candidate /tmp/vip-health-rehearsal/user_quota.db
```

The tool opens the source in SQLite read-only mode, creates an online-backup copy, migrates only that copy, and requires:

- source SHA-256 unchanged;
- source and candidate `integrity_check=ok`;
- zero foreign-key violations;
- eight `vip_health_check_%` tables in the candidate, including the activation provenance ledger;
- no overwrite of an existing candidate path.

Keep the JSON result as release evidence.

## 5. Rehearse rollback compatibility

The currently running production image is the rollback SHA. Point a separate process at the migrated `/tmp` copy, force both controls off, and import/start its existing server code:

```bash
DATA_DIR=/tmp/vip-health-rehearsal \
ENABLE_SCHEDULER=false \
VIP_HEALTH_CHECK_ENABLED=false \
python3 -c "import server; print('ROLLBACK_STARTUP_OK')"
```

Then reopen only the rehearsal copy and repeat `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. The live `/app/data/user_quota.db` must never be passed to this rehearsal command.

## 6. Merge and dark deploy

Only after Sections 1–5 pass:

1. Mark the reviewed PR ready and merge it without changing files.
2. Record the merge SHA.
3. Watch Railway until the deployment reaches a terminal `SUCCESS` state.
4. Verify Railway reports the exact merge SHA and `main` source branch.
5. Confirm the five non-secret controls in Section 2 again.

## 7. Post-deploy verification

Require all of the following:

- `/health` returns 200;
- production LINE `/callback` retains its expected signature-validation response;
- customer health-check routes return 404 while the flag is false;
- legacy member and ADMIN smoke tests behave normally;
- SQLite `integrity_check=ok` and `foreign_key_check` returns zero rows;
- the additive VIP schema exists;
- no new traceback or HTTP 5xx pattern appears in deployment logs.

If a stop condition occurs, redeploy the recorded rollback SHA. If data restoration is required, stop writes first and restore only from the verified online backup; never replace the database while the app is writing.

## 8. Historical VIP activation policy — blocks feature enablement, not dark deploy

After this release, every successful VIP redemption writes an auditable activation event even while customer routes are dark. A redemption with no pre-existing `usage` row is classified as `lifetime_first` and may create the one-time seven-day case. A user with any pre-existing `usage` row but no prior health-check provenance is classified as `historical_existing`; the redemption is preserved but no case is granted until Jason approves a historical policy. Later redemptions are classified as `renewal`. Historical generic VIP redemptions made before this release do not retain enough authoritative data to reconstruct their true first activation timestamp safely.

Before setting `VIP_HEALTH_CHECK_ENABLED=true`, Jason must explicitly choose and approve one policy:

1. grant every currently eligible historical VIP a fresh seven-day window from a reviewed rollout timestamp; or
2. limit the benefit to first VIP activations recorded after this dark deployment.

Do not infer dates from `usage.last_date`, do not silently backfill, and do not enable the feature until this decision and its audited backfill/no-backfill evidence are recorded.
