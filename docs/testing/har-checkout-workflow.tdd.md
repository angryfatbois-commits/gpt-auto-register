# HAR-backed eligibility workflow: TDD evidence

## Scope

This change replaces the inferred multi-stage GCash sequence with the verified
three-request browser workflow. It keeps Plus and GCash verdicts independent,
reports exact due/zero-payment information, and never executes payment. A
best-effort bootstrap/Sentinel preflight prepares current browser metadata but
is not an eligibility stage and never returns its short-lived values.

## User journeys

1. As an operator, I want a single manual check to update Plus trial and GCash
   columns together, so I do not run duplicate account checks.
2. As an operator, I want GCash availability to reflect an exact Checkout-to-Stripe
   custom-method round trip, so a false `GCash unavailable` is not caused by
   Stripe's preference country.
3. As a security-conscious operator, I want the probe to stop after Stripe
   capability metadata, so no payment can be confirmed or started.
4. As an operator maintaining both registration and eligibility checks, I want
   legacy registration Sentinel calls to retain their session transport while
   Checkout uses isolated, same-identity transport, so the new preflight does
   not regress account creation.

## RED evidence

Before implementation, the new tests executed and failed for the intended
missing behavior:

```text
python -m unittest tests.test_gcash_source_workflow tests.test_plus_probe tests.test_eligibility_api tests.test_eligibility_db -v
ImportError: cannot import name 'probe_checkout_eligibility'
ImportError: cannot import name 'probe_plus_eligibility_in_session'
AttributeError: webui.app has no attribute 'probe_checkout_eligibility'
KeyError: zero_payment
```

The frontend contract tests also failed because due formatting and the new
read-only proxy copy were not implemented.

The security regression tests then confirmed that SDK and challenge requests
were still using the HTTP client's default redirect behavior. After the
minimal fix, the focused transport target passed 12/12.

## GREEN evidence

Focused backend workflow and integration tests:

```text
python -m unittest tests.test_gcash_probe tests.test_gcash_source_workflow tests.test_plus_probe tests.test_eligibility_api tests.test_eligibility_db tests.test_gcash_binary_policy -v
Ran 79 tests ... OK
```

Full backend suite:

```text
python -m unittest discover -s tests -p 'test_*.py' -v
Ran 112 tests ... OK
```

Full frontend suite:

```text
npm test
tests 30, pass 30, fail 0
```

## Guarantees

| Guarantee | Evidence |
|---|---|
| Checkout body exactly matches the HAR and excludes `check_card_proxy` | `tests/test_gcash_source_workflow.py` |
| Promo remains present on the same-identity transient retry | `tests/test_gcash_probe.py` |
| Accounts/check, checkout, and Stripe Elements are the capability stages; an optional same-origin auth refresh is isolated as a credential preflight | `tests/test_gcash_source_workflow.py`, `tests/test_gcash_probe.py` |
| Bootstrap/Sentinel metadata is discovered at runtime, client hints are bounded, and HAR values are not copied | `tests/test_checkout_transport.py` |
| Legacy Sentinel challenge requests keep the session POST path; only explicit Checkout URLs use isolated POST | `tests/test_checkout_transport.py` |
| SDK downloads and Sentinel challenge POSTs reject redirects before any downloaded script is executed or challenge data can leave the allowlisted origin | `tests/test_checkout_transport.py` |
| A strict-HAR Checkout HTTP 400/422 receives at most one authenticated same-identity fallback | `tests/test_checkout_transport.py` |
| Stripe has no beta or browser-timezone parameters and uses `en-US` plus `2025-03-31.basil` | HAR workflow tests |
| Same proxy/profile/User-Agent are used, with no PH egress requirement | HAR workflow tests |
| Exact custom ID and explicit `GCash` label are required | classification tests |
| Due is read only from `checkout_state.total.total.minorUnitsAmount`; malformed due fails closed | malformed-total test |
| Zero and positive due remain independently GCash-eligible | amount tests |
| Plus metadata is returned and both result types are persisted in the tenant DB | API/DB tests |
| Tokens, cookies, proxy credentials, secrets, checkout IDs, and opaque IDs are not returned | redaction tests |
| Confirmation, redirect, payment-intent, subscribe, update, taxes, resolve, and Payment Pages are never called | endpoint deny-list tests |

## Verification notes

No live account or raw HAR was used in automated tests. The raw HAR remains
unchanged outside the repository. The existing explicit acknowledgement and
loopback/admin-token gate remains required for the payment-adjacent endpoint.
The supplied HAR was captured by Chrome/Brave 151; this installation supports
the `curl_cffi` Chrome 146 profile, so the implementation documents that
constraint instead of claiming exact TLS equivalence. Numeric Python coverage
is not reported when the optional `coverage.py` package is unavailable.

## Final verification record

Commands and results:

```text
python -m unittest discover -s tests -p 'test_*.py' -v
Ran 112 tests in 4.488s ... OK

python -m unittest tests.test_checkout_transport -v
Ran 12 tests in 0.044s ... OK

npm test (webui/frontend)
30 passed, 0 failed

npm run build (webui/frontend)
1711 modules transformed; built successfully

python -m compileall -q .
OK

python -m pip check
No broken requirements found.

npm audit --omit=dev --audit-level=high
found 0 vulnerabilities

git diff --check
OK
```

The optional `coverage.py` package is not installed in this environment, so
no numeric coverage percentage is claimed. The frontend has no configured
`lint` script; the production build and test suite are the available frontend
quality gates.
