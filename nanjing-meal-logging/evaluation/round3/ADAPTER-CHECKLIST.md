# Adapter author checklist

1. Implement a side-effect-free `module:function` accepting `(case, context)` and returning the envelope in `rubric.md`.
2. Call only the public semantic pipeline/API under evaluation. Do **not** import `server.py`, initialize the app, connect a database, invoke LINE, write logs, or alter quota.
3. Return actual provider/model identity and actual raw response (privacy-redacted only; never synthesize missing raw data).
4. Mark `execution.kind` as `live_ai`. The runner rejects `fake_harness` in `--mode live`.
5. Take credentials only from environment variables explicitly allowed by parent via `--pass-env`; never print them.
6. Preserve provider failures as exceptions or structured errors. Do not replace failures with expected values.
7. The adapter/prompt revision used for a live run must be identified by `--adapter-version` and must not be edited after viewing results without starting a new frozen evaluation version.

## Frozen live adapter

Use `evaluation.round3.live_adapter:run_case`. It accepts only the live
`user_text`; `context.case_id` is not sent to the provider. It calls the public
`parse_meal_semantics_openai`, `estimate_per_100_openai`, and
`run_semantic_meal_pipeline` functions, with the checked-in reference lookup as
the callback. Callback wrappers retain the **actual** reference/fallback basis
needed by the evaluator; they do not calculate a different product result or
read expected labels. Missing parser fields (notably `food_state`) remain
missing/reviewable and are never filled from the golden.

The adapter creates `OpenAI(api_key=OPENAI_API_KEY, timeout=20,
max_retries=0)`, uses `ROUND3_OPENAI_MODEL` (default `gpt-4o-mini`), caps each
completion at 900 tokens, and enforces at most two provider calls per case.
`execution.estimated_cost_usd` is deliberately `unknown`; token usage comes
from provider response objects.

Parent live command (choose a new output path and a non-zero conservative
reservation; `--max-calls` means adapter invocations, not provider calls):

```bash
python3 evaluation/round3/runner.py \
  --mode live --adapter evaluation.round3.live_adapter:run_case \
  --adapter-version 'r3-v2-frozen-5050c196' \
  --output '/secure/evidence/round3/LIVE-REPORT-<run-id>.json' \
  --max-calls 90 --requests-per-minute 20 --timeout-seconds 20 \
  --budget-cap-usd 5 --estimated-cost-per-call-usd 0.05 \
  --pass-env OPENAI_API_KEY --pass-env ROUND3_OPENAI_MODEL
```
