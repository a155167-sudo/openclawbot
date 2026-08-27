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
- `FORM_WEBHOOK_SECRET`
- `SURVEY_WEBHOOK_SECRET`
- `PUBLIC_BASE_URL` or Railway-provided `RAILWAY_PUBLIC_DOMAIN`
- `SPREADSHEET_ID`
- `ADMIN_UID`
- `COACH_UIDS`
- `LIFF_ID`
- `SUBSCRIPTION_FORM_URL_TEMPLATE` containing `{uid}`
- `SURVEY_FORM_URL_TEMPLATE` containing `{uid}`

If any Railway deployment metadata is present while `APP_ENV` is missing, startup
fails closed instead of falling back to legacy resources or enabling the scheduler.
`LIFF_ID` must match LINE's numeric-prefix format (for example,
`2000000000-AbCdEfGh`). `GOOGLE_CREDENTIALS` must be valid service-account JSON,
and a named environment stops at startup if its configured Sheet cannot initialize.

`PUBLIC_BASE_URL` must be an HTTPS origin without a path. Form templates must use HTTPS. LINE user IDs are validated before startup.

## External endpoint mapping

| Integration | staging | production |
|---|---|---|
| LINE webhook | `https://<staging-domain>/callback` | `https://<production-domain>/callback` |
| Subscription Google Form Apps Script | POST to staging `/form-data` | POST to production `/form-data` |
| LIFF endpoint | staging `/coach-dashboard` | production `/coach-dashboard` |
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

## Release workflow

1. Develop and deploy to `staging`.
2. Test through the test LINE official account.
3. Run the complete pytest suite and exact-commit review.
4. Merge the reviewed commit into `main`.
5. Verify production Railway deployment metadata, startup logs, and `/health`.
6. Run LINE smoke tests with ordinary member and admin accounts.

## Data policy

Production starts with a fresh SQLite schema. Do not clone staging `usage`, `vips`, `subscription_orders`, `health_profile`, food logs, entitlements, or admin bindings. Official menu data is rebuilt from `menu.csv`; any other table migration requires an explicit table-by-table review.

Both services mount their own Railway volume at `/app/data`. The identical path is
inside separate containers; never attach the same volume resource to both services.
