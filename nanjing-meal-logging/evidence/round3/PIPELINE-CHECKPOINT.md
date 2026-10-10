# Round 3 semantic pipeline checkpoint

## Delivered tracer bullet

- Public contract: `evidence/round3/PIPELINE-CONTRACT.md`
- Executable, persistence-free pipeline: `semantic_meal_pipeline.py`
- Existing-router-first handler seam: `dispatch_semantic_meal_text(...)`
- Native admission is dependency-injected and runs before either AI seam.
- One batch claim covers semantic parsing plus one batched per-100 fallback call.
- Official reference is attempted per item before fallback.
- Official unit mismatch clarifies and never falls through to AI nutrition.
- g/ml are exact-match only; natural units clarify rather than becoming measured g/ml.
- Multi-item output retains all items.
- No `food_logs` write; confirmation remains a later boundary.

## TDD evidence

RED 1: `pytest tests/test_semantic_meal_pipeline.py -q` failed during collection with `ModuleNotFoundError: semantic_meal_pipeline`.

GREEN 1: same file passed: `4 passed in 0.03s`.

RED 2: `pytest tests/test_semantic_meal_registered_flow.py -q` failed during collection because `dispatch_semantic_meal_text` was absent.

GREEN combined: `pytest tests/test_semantic_meal_pipeline.py tests/test_semantic_meal_registered_flow.py -q` => `6 passed in 0.04s`.

Syntax/whitespace: `python3 -m py_compile semantic_meal_pipeline.py && git diff --check` exited 0.

## Explicitly incomplete

This tracer bullet is not yet wired into the registered LINE `server.py` handler and does not call live AI, live DB, LINE, or a real quota authority. The adapter for native provider-attempt audit, existing shared quota claim, `find_reference_nutrition(request)`, draft persistence/rendering, and confirmed logging remains for the next vertical slice. Therefore these fake-provider tests prove contract/control flow, not AI parsing quality or production acceptance.
