# Mobile soy/server fix checkpoint — 2026-10-08

Scope: server.py + new webhook module + unique mobile-soy tests only.

Root cause found: registered semantic dispatcher is reached at server.py:22975, but both `awaiting_food` (20865–20915) and generic `natural_log` (20957–20967) return earlier through `build_natural_food_log_reply`; its unmatched branch at 18287 calls legacy `create_text_meal_estimate_draft`, so the real `無糖豆漿 500ml` event never creates `semantic_meal_batches`.

Callback finding: `/callback` calls synchronous `handler.handle(...)` inside the async route before returning, so ACK necessarily waits for all AI/LINE handling. No durable `line_webhook_inbox` implementation exists in repo despite live-schema compatibility requirement.

TDD plan: add exact registered-handler regression; add callback signature/shape/durable ACK-order/dedupe/restart-state tests; then minimal route + inbox worker implementation. Preserve deterministic private catalog/reference paths and existing quota semantics.
