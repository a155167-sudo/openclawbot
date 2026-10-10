# Confirmed meal-photo revision data slice

Status: revision-aware readers and the feature-flagged writer flow are connected locally. Deployment remains out of scope, and `CONFIRMED_MEAL_PHOTO_REVISION_WRITER_ENABLED` remains default-off.

## Connected user flow

- When the writer flag is enabled, a validated `user_confirmed_ai_nutrition_v2` meal appears in the daily ledger with version-fenced `重新查看` / `修改這餐` / `撤銷紀錄` controls.
- `修改這餐` routes through the real LINE handlers: start → correction text → original-image/current-estimate inference → preview → confirm/cancel.
- Confirmation updates the same `food_logs` row and returns a fresh success card with the latest version's `重新查看` / `再次修改` / `撤銷紀錄` actions. A second pass can therefore advance v2 to v3 without returning to an old card.
- Old-card postbacks are rejected by owner + `log_id + version` fencing before provider work or mutation. Cancellation leaves the original log and daily projection unchanged.
- If the writer flag is off, revision controls are hidden from recorded, daily, and success cards where applicable, and existing `mealrev` postbacks/text states fail closed. The remote/default setting was not changed.
- A v2-shaped row whose shared trust projection fails verification never receives a revision control. Legacy direct-edit actions remain blocked for confirmed v2 photo logs; removal still uses the existing version-fenced ledger delete flow.

## Trust and transaction contract

- The original confirmed draft, confirmation event, `trust_payload_json`, and `trust_hash` are immutable genesis evidence.
- Every confirmed edit appends one `daily_food_log_events.action = 'confirm_ai_revision'` envelope. Its canonical SHA-256 links to the original trust hash (first revision) or prior revision hash (later revisions).
- The shared validator checks genesis plus the complete contiguous chain, typed owner/log/version parent, before-state hash, estimate provenance, snapshot semantics, and agreement with the current `food_logs` row. Readers fail closed on any mismatch.
- Confirmation owns `BEGIN IMMEDIATE`, revalidates under the write lock, appends the event, CAS-updates the same log, marks only the independent revision draft, and updates the Sheet outbox in one transaction.
- Same-event replay does not append or rewrite history. It revalidates the chain and repairs outbox scheduling. A processing outbox retains its lease and is marked `resync_required=1`.
- Cancellation changes only the independent revision draft.

## Verified local acceptance scope

The network-isolated tests exercise the real postback and text handlers through daily v1 entry → preview → confirm v2 → success-card `再次修改` → preview → confirm v3. They assert one `food_logs` row, two linked revision events, a valid latest chain, and the daily consumer returning v3 nutrition. Separate cases cover cancel, stale old cards, writer-off rejection/hiding, and tampered-v2 hiding. No live LINE, AI, Google, remote DB, commit, push, or deployment is part of this slice.

## Remaining quality item: provider timeout semantics

`_estimate_adjusted_meal_photo()` passes `timeout=30` to OpenAI Python SDK 2.24.0. The installed SDK defaults to `max_retries=2`; inspection of its synchronous request loop shows that a timeout can be retried, and the timeout is applied by the HTTP client per attempt/phase rather than acting as a proven end-to-end handler deadline. The current regression test injects `TimeoutError` and verifies safe recovery only. It does **not** prove a hard 30-second wall-clock upper bound, so no such guarantee is claimed. Explicit retry policy and a measurable end-to-end deadline remain a separate quality review; this slice intentionally adds no thread-based timeout architecture.

## Rollback limitation

Once any version-2-or-later revision exists, an older binary whose validator only understands the version-1 current row will classify that meal as untrusted. Rollout must remain reader-first with the writer disabled until enabled deliberately. After any writer activation, rollback means disabling the writer while retaining a revision-aware reader; it is not safe to roll the application back to a pre-revision reader. Confirmed revisions are append-only. A future restore operation must append another revision rather than delete history.

Current-version reads remain wired for refreshable health-check cases: revision confirmation/replay repairs the canonical manifest, daily ledger/profile and summary projections use revised kcal/protein while preserving unknown fat/carbohydrate, Sheet/API/LIFF readers use the shared validator, source hashes bind the effective latest revision hash, and private-image reads stay bound to the original confirmed owner/draft. Terminal health-check cases retain the existing no-refresh policy.
