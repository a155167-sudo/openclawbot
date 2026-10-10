# Staging / Production Environment Split

## Deployment topology

| Environment | Git branch | LINE channel | SQLite volume | Scheduler |
|---|---|---|---|---|
| staging | `staging` | test Messaging API channel | dedicated `/app/data` volume | explicit; template is `false` |
| production | `main` | official Messaging API channel | dedicated `/app/data` volume | explicit; dark-release template is `false` |

Never share LINE credentials, SQLite volumes, Google Sheet IDs, or Google Form webhook destinations between these environments.

## Required Railway variables

Use the checked-in templates:

- `config/railway-staging.variables.example`
- `config/railway-production.variables.example`

When `APP_ENV` is `staging` or `production`, startup fails unless these environment-specific values are explicit:

- `DATA_DIR`
- `ENABLE_SCHEDULER`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `OPENAI_API_KEY`
- `GOOGLE_CREDENTIALS`
- `MEAL_PHOTO_IMAGE_SECRET`
- `ADMIN_SECRET`
- `FORM_WEBHOOK_SECRET`
- `SURVEY_WEBHOOK_SECRET`
- `SURVEY_REWARD_LINK_COUNT` (must be `1`)
- `SURVEY_REWARD_POINTS_PER_LINK` (must be `2`)
- `PUBLIC_BASE_URL` or Railway-provided `RAILWAY_PUBLIC_DOMAIN`
- `SPREADSHEET_ID`
- `ADMIN_UID`
- `COACH_UIDS`
- `LIFF_ID`
- `SUBSCRIPTION_FORM_URL_TEMPLATE` containing `{uid}`
- `SURVEY_FORM_URL_TEMPLATE` containing `{uid}`

If any Railway deployment metadata is present while `APP_ENV` is missing, startup
fails closed instead of falling back to legacy resources or enabling the scheduler.
`VIP_HEALTH_CHECK_ENABLED` is separately fail-closed: an unset or unrecognised value
keeps the feature disabled. `DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED` is a
strict, independent `true|false` control and defaults to `false`; it may run the three
bounded health-check delivery/recovery jobs while `ENABLE_SCHEDULER=false`, without
enabling reminders, weekly reports, nutrition outboxes, or broad image cleanup. It
becomes effective only when the validated dietitian read configuration and existing
VIP benefit are both enabled. Both checked-in Railway templates pin both health-check
flags to `false`; change them only during a reviewed rollout.
`LIFF_ID` must match LINE's numeric-prefix format (for example,
`2000000000-AbCdEfGh`). `GOOGLE_CREDENTIALS` must be valid service-account JSON,
and a named environment stops at startup if its configured Sheet cannot initialize.

`PUBLIC_BASE_URL` must be an HTTPS origin without a path. Form templates must use HTTPS. LINE user IDs are validated before startup.

Named staging and production environments must explicitly set `SURVEY_REWARD_LINK_COUNT=1` and `SURVEY_REWARD_POINTS_PER_LINK=2`: each completed survey receives exactly one two-point `reward_link`. Any other named-environment denomination fails closed at startup. Reservations are atomic: when no link remains, the service consumes none and does not record the respondent as claimed. Reserved links are stored in `survey_reward_deliveries` until LINE push succeeds; a retry for the same UID reuses that exact link instead of consuming another. A per-UID operating-system file lock covers the complete active LINE send, so another callback cannot push the same link even if the SQLite delivery lease expires during a slow request. The OS releases that lock automatically if the sender process exits. The persistent SQLite lease still records delivery ownership; failed sends release it for immediate retry, and abandoned leases expire so the same link remains recoverable.

## External endpoint mapping

| Integration | staging | production |
|---|---|---|
| LINE webhook | `https://<staging-domain>/callback` | `https://<production-domain>/callback` |
| Subscription Google Form Apps Script | POST to staging `/form-data` | POST to production `/form-data` |
| LIFF endpoint | staging `/coach-dashboard` | production `/coach-dashboard` |
| VIP customer health check LIFF | staging `/vip-health-check` | production `/vip-health-check` |
| Dietitian read-only health check LIFF | staging `/dietitian-health-check` | production `/dietitian-health-check` |
| Health check | staging `/health` | production `/health` |

A Google Form link update alone is insufficient: each Form requires its own Apps Script `onFormSubmit` trigger and destination.

Each Apps Script request must also send the matching environment secret without
placing it in the form payload:

```javascript
UrlFetchApp.fetch(destinationUrl, {
  method: "post",
  contentType: "application/json",
  headers: {
    "X-Webhook-Secret": PropertiesService.getScriptProperties()
      .getProperty("WEBHOOK_SECRET")
  },
  payload: JSON.stringify(payload)
});
```

Store `WEBHOOK_SECRET` in Apps Script **Script Properties**. Use separate values
for subscription/survey and for staging/production.

## Health-check deployment boundary

This change reuses the resources already assigned to each environment. A deployment
must not create or replace a LINE Provider, Messaging API Channel, LINE Login
Channel, LIFF app, Railway service, domain, volume, or Google Sheet; it must not
attach a staging resource to production (or the reverse). The endpoint rows above
are route mappings only and are not instructions to provision new resources.

Before any staging or production rollout, the release owner must record and compare
the intended environment's exact:

- Git branch and candidate commit;
- Railway project, environment, and service IDs;
- public domain and attached volume ID/mount;
- Messaging API Channel and LINE Login Channel IDs, their Provider ownership, and
  the target LIFF IDs/endpoints;
- Google Sheet ID and the matching Apps Script destination.

The relevant Messaging API and LINE Login Channels must be verified under the
intended same Provider where Provider-scoped identity is required. Names alone are
not evidence of ownership or isolation. This document and local tests do **not**
claim that Railway, LINE Developers, volumes, domains, Channels, LIFF apps, or
Sheets have been inspected or proven isolated remotely.

A local PASS does not mean the commit has been deployed or that either public LIFF
endpoint is online. Keep `VIP_HEALTH_CHECK_ENABLED=false`,
`DIETITIAN_HEALTH_CHECK_DELIVERY_RECOVERY_ENABLED=false`, and
`ENABLE_SCHEDULER=false` through the dark deployment and identity/resource checks;
enabling any of them requires the reviewed rollout procedure and explicit release-owner
approval.

Before enabling the dedicated delivery/recovery trigger, read back and record that the
deployment has exactly **one replica on one host**, and that its delivery lock directory
and SQLite database reside on the same mounted filesystem. The current `flock` contract
is host-local; more than one replica or a non-shared lock filesystem blocks enablement
and requires a separately reviewed distributed/single-consumer design. The three
10-minute interval jobs are:

1. report delivery: selects at most one `pending`/`failed` persisted delivery and may
   push its immutable approved report to LINE;
2. delivered-image cleanup recovery: selects at most one coherently `delivered` case
   that still retains an authoritative source-image reference and may delete only via
   the reviewed protected cleanup path;
3. supplement notification: selects at most one current `pending_customer` request
   whose notification is `not_sent` and may push its persisted request to LINE.

Startup immediately primes only report delivery and delivered-image cleanup recovery,
once each; supplement notification begins on its interval. Every selector uses a finite
cohort and each invocation processes at most one candidate. The report and supplement
provider calls retain host-local per-operation `flock`; this is not a cross-replica
exactly-once guarantee.

## Release workflow

1. Develop and deploy to `staging`.
2. Test through the test LINE official account.
3. Run the complete pytest suite and exact-commit review.
4. Complete `docs/production-vip-health-check-dark-deploy-runbook.md`, including a verified production online backup, candidate migration rehearsal, and rollback-SHA startup rehearsal on copies only.
5. Merge the reviewed commit into `main`.
6. Verify production Railway deployment metadata, startup logs, and `/health`.
7. Run LINE smoke tests with ordinary member and admin accounts.

## Data policy

Only the one-time initial provisioning of a brand-new production environment may start with a fresh SQLite schema, and even then it must not clone staging `usage`, `vips`, `subscription_orders`, `health_profile`, food logs, entitlements, or admin bindings. Once a production volume exists, this release and every later deployment must preserve that live volume and use the verified backup/copy-only migration workflow above; never replace it with a blank database or any staging database. Official menu data is rebuilt from `menu.csv`; any other table migration requires an explicit table-by-table review.

Both services mount their own Railway volume at `/app/data`. The identical path is
inside separate containers; never attach the same volume resource to both services.
