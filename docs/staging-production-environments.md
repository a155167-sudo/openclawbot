# Staging / Production Environment Split

## Deployment topology

| Environment | Git branch | LINE channel | SQLite volume | Scheduler |
|---|---|---|---|---|
| staging | `staging` | test Messaging API channel | dedicated `/app/data` volume | disabled by default |
| production | `main` | official Messaging API channel | dedicated `/app/data` volume | enabled by default |

Never share LINE credentials, SQLite volumes, Google Sheet IDs, or Google Form webhook destinations between these environments.

## Required Railway variables

Use the checked-in templates:

- `config/railway-staging.variables.example`
- `config/railway-production.variables.example`

When `APP_ENV` is `staging` or `production`, startup fails unless these environment-specific values are explicit:

- `DATA_DIR`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `OPENAI_API_KEY`
- `GOOGLE_CREDENTIALS`
- `MEAL_PHOTO_IMAGE_SECRET`
- `ADMIN_SECRET`
- `PUBLIC_BASE_URL` or Railway-provided `RAILWAY_PUBLIC_DOMAIN`
- `SPREADSHEET_ID`
- `ADMIN_UID`
- `COACH_UIDS`
- `LIFF_ID`
- `SUBSCRIPTION_FORM_URL_TEMPLATE` containing `{uid}`
- `SURVEY_FORM_URL_TEMPLATE` containing `{uid}`

`PUBLIC_BASE_URL` must be an HTTPS origin without a path. Form templates must use HTTPS. LINE user IDs are validated before startup.

## External endpoint mapping

| Integration | staging | production |
|---|---|---|
| LINE webhook | `https://<staging-domain>/callback` | `https://<production-domain>/callback` |
| Subscription Google Form Apps Script | POST to staging `/form-data` | POST to production `/form-data` |
| LIFF endpoint | staging `/coach-dashboard` | production `/coach-dashboard` |
| Health check | staging `/health` | production `/health` |

A Google Form link update alone is insufficient: each Form requires its own Apps Script `onFormSubmit` trigger and destination.

## Release workflow

1. Develop and deploy to `staging`.
2. Test through the test LINE official account.
3. Run the complete pytest suite and exact-commit review.
4. Merge the reviewed commit into `main`.
5. Verify production Railway deployment metadata, startup logs, and `/health`.
6. Run LINE smoke tests with ordinary member and admin accounts.

## Data policy

Production starts with a fresh SQLite schema. Do not clone staging `usage`, `vips`, `subscription_orders`, `health_profile`, food logs, entitlements, or admin bindings. Official menu data is rebuilt from `menu.csv`; any other table migration requires an explicit table-by-table review.
