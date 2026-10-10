Work only in C:\Users\WIN-XI\AppData\Local\Temp\reschedule-customer-liff-staging. This is synthetic staging; no network, credentials, LINE, Railway, Google Sheets, or production. Do not modify server.py or existing modules.

Implement only the backend policy seam in these allowed files:
- customer_reschedule_liff.py
- tests/test_customer_reschedule_liff.py
- DELIVERY-openclaw.json

Keep it bounded and finish early. Create a pure/testable adapter with:
1. allowed_target_dates(expiry_date: str): strict ISO date, expiry through expiry+30 inclusive.
2. validate_target_date(expiry_date, target_date, occupied_dates): reject malformed date, before expiry, after expiry+30, or occupied target with clear ValueError.
3. submit_pending_admin_request(submit_fn, *, owner_id, order_id, source_date, target_date, request_id): require non-empty server-verified owner/order context and delegate exactly once to submit_fn with pending_admin semantics; never approve or write Sheets.

Tests must cover expiry, expiry+30, expiry+31, occupied, malformed date, no-write validation, and request_id delegation/replay. Use write/read tools and a short focused pytest only if it completes quickly; otherwise leave tests for Controller. Write DELIVERY-openclaw.json with status, changed_files, and limitations. Stop after files are persisted.