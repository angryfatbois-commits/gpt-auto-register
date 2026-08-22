# GCash checkout compatibility retry TDD evidence

> **Historical record:** The compatibility path described here (including
> promo-dropping and alternate fingerprints) is no longer active. See
> [`har-checkout-workflow.tdd.md`](har-checkout-workflow.tdd.md) for the current
> same-payload retry contract.

> Historical note: the alternate-fingerprint retry documented here was later
> superseded by the stable browser-identity and isolated transport described in
> `gcash-browser-transport.tdd.md`.

## User journey

As an administrator, I want a generic checkout-create rejection to receive one
safe browser-compatible retry, so a payment method that is visible in the
manual PH/PHP checkout is not hidden by optional promotion fields.

## RED/GREEN evidence

| Stage | Command | Result |
| --- | --- | --- |
| RED | `python -m unittest tests.test_gcash_probe.GCashNetworkProbeTests.test_generic_checkout_http_400_retries_with_a_minimal_ph_contract -v` | Failed because the original probe immediately returned `ineligible` after the first HTTP 400. |
| GREEN | Same command after the fix | Passed. |
| Focused regression | `python -m unittest tests.test_gcash_probe tests.test_gcash_source_workflow tests.test_gcash_binary_policy tests.test_eligibility_api tests.test_eligibility_db -v` | 63 tests passed. |
| Full backend | `python -m unittest discover -s tests -p "test_*.py" -v` | 89 tests passed. |
| Frontend | `npm test` in `webui/frontend` | 28 tests passed. |
| Production build | `npm run build` in `webui/frontend` | Passed. |
| Dependency checks | `npm audit --omit=dev --audit-level=high` and `python -m pip check` | 0 vulnerabilities; no broken requirements. |

## Test specification

| # | What is guaranteed | Test target | Type | Result |
| --- | --- | --- | --- | --- |
| 1 | A generic checkout HTTP 400 gets one fresh-session compatibility attempt | `test_generic_checkout_http_400_retries_with_a_minimal_ph_contract` | Transport regression | PASS |
| 2 | The compatibility payload remains PH/PHP and removes only optional promotion/card-proxy fields | Same test | Contract regression | PASS |
| 3 | The selected proxy is retained and the alternate checkout uses a different browser fingerprint | Same test | Privacy regression | PASS |
| 4 | Eligibility still requires affirmative GCash custom-method evidence from Stripe | Same test plus `tests.test_gcash_probe` | Classification regression | PASS |
| 5 | Neither checkout path calls confirmation, custom-payment start, or payment execution | Same test plus `tests.test_gcash_source_workflow` | Security regression | PASS |

## Production evidence and known gap

The sanitized production record that motivated this test ended at
`checkout_http_400`; the comparison account reached an accepted, exact PH/PHP
custom-method round trip. Both accounts had current access tokens, matching
account context, valid `oai-did` cookies, free plans, and Plus-trial eligibility.
No raw upstream body, token, cookie value, email, proxy credential, or checkout
identifier was copied into this report.

The compatibility code has not yet been deployed or exercised against the
affected production account. A new user-confirmed check is required after
deployment to validate the upstream behavior. Numeric Python coverage remains
unavailable unless the optional `coverage` package is installed.
