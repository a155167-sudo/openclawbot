# Round 3 v4 evaluator-projection revision record

- **This is evaluator projection v4, not a product, parser, case-bank, rubric, or nutrition-golden revision.** `cases.json` remains SHA-256 `8d8a3acb940fcd7937977aff3eadc49074198e3341716523ba0f7d08f67203dc`; `rubric.md`, all accepted food names, allowed values, tolerances, and every numerical golden are unchanged.
- The complete v3 evaluator freeze was copied before this revision to external evidence `round3/eval-v3-before-projection/`. Existing first- and second-live v3 raw reports were not recalculated, edited, or overwritten.
- `live_adapter.py` now exposes the real pipeline projection separately as `pipeline.status` and `pipeline.clarifications`, while preserving the provider parser result byte-for-structure under `parsed`. Pipeline-generated clarification text is not represented as parser/AI output.
- `runner.py` keeps parser scoring—including `portion_assumption`—strictly on `parsed`. Only routing `clarification_present` may additionally accept a non-empty string from `pipeline.clarifications`, and only when `pipeline.status == "clarification"`. Whitespace, dictionaries, numbers, and `not_meal` do not count.
- Evaluated report rows retain `pipeline` for traceability. New reports explicitly declare `evaluator_projection: "v4"` while retaining the established report schema identifier.
- The adapter no longer projects pipeline `not_meal` as a clarification basis.
- Contract tests cover a real natural-portion pipeline path through adapter and runner, prove parser clarification stays empty, and include no-message/wrong-type/`not_meal` negatives.
- Any third live report must be stored separately under this v4 freeze. Its v4 routing score is **not directly comparable** to first/second v3 routing scores because evaluator projection changed. No live AI, DB, LINE, or deployment action was performed for this revision.

## Preserved v3 history

- v3 was completed before its first live AI run and preserved the complete v2 contract under external evidence `round3/eval-v2/`.
- v3 corrected natural-portion count goldens and natural mixed-meal routing, added three exact soy-milk regressions, and expanded the bank to 93 cases without changing existing TFDA nutrition golden values.
