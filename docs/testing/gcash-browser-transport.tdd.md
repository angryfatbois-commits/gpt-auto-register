# GCash browser transport TDD evidence

> **Historical record:** This document covers a superseded transport variant.
> The current HAR-backed three-request workflow and its evidence are in
> [`har-checkout-workflow.tdd.md`](har-checkout-workflow.tdd.md).

## Source and user journey

No plan file was supplied. The journey was derived from a production result
where both the rich Checkout request and its minimal compatibility retry ended
at `checkout_http_400` before Stripe capability metadata could be read.

As an administrator, I want the GCash probe's checkout requests to match one
stable browser identity and avoid reusable-session cookie contamination, so an
account whose manual PH/PHP checkout exposes GCash is not hidden by a malformed
automated browser contract.

## RED/GREEN evidence

| Stage | Command | Result |
| --- | --- | --- |
| RED | `python -m unittest tests.test_gcash_probe.GCashNetworkProbeTests.test_generic_checkout_http_400_retries_with_a_minimal_ph_contract tests.test_http_client.HttpClientIsolationTests.test_isolated_post_matches_tls_profile_and_keeps_proxy -v` | Failed as intended: the probe used `chrome110` then `firefox144`, and the HTTP wrapper had no isolated POST method. |
| GREEN | Same command after implementation | 2 tests passed. |
| Focused regression | `python -m unittest tests.test_gcash_probe tests.test_gcash_source_workflow tests.test_gcash_binary_policy tests.test_eligibility_api tests.test_eligibility_db tests.test_http_client -v` | 66 tests passed. |
| Full backend | `python -m unittest discover -s tests -p "test_*.py" -v` | 92 tests passed. |
| Frontend | `npm test` in `webui/frontend` | 28 tests passed. |
| Production build | `npm run build` in `webui/frontend` | Passed. |
| Dependency checks | `python -m pip check` and `npm audit --omit=dev --audit-level=high` | No broken requirements; 0 vulnerabilities. |

## Test specification

| # | What is guaranteed | Test target | Type | Result |
| --- | --- | --- | --- | --- |
| 1 | Checkout TLS impersonation, User-Agent, and client hints all identify Chrome 146 | `test_generic_checkout_http_400_retries_with_a_minimal_ph_contract` | Protocol regression | PASS |
| 2 | Device ID, ChatGPT account context, client version/build, and per-probe session ID remain stable across retry and follow-up requests | Same test | Identity regression | PASS |
| 3 | A generic 400 retry keeps the selected proxy and PH/PHP payload while removing only optional create fields | Same test | Routing/contract regression | PASS |
| 4 | ChatGPT JSON POSTs use a one-shot curl transport that does not inherit a session cookie jar | `test_isolated_post_matches_tls_profile_and_keeps_proxy` | Transport regression | PASS |
| 5 | SOCKS DNS continues through the proxy and neither checkout attempt invokes confirmation, custom-method start, or payment execution | Both targets plus the GCash regression suite | Privacy/security regression | PASS |
| 6 | Access tokens, cookies, proxy credentials, checkout IDs, and the browser session ID are not returned in the eligibility result | GCash network-probe security tests | Data-exposure regression | PASS |
| 7 | Recognized HTTP 400/422 messages become stable allowlisted codes while unknown messages stay generic and no body text is returned | `test_checkout_400_reason_is_reduced_to_a_safe_stable_code` | Diagnostic/security regression | PASS |
| 8 | A recognized promotion rejection still receives the safe minimal checkout retry | `test_checkout_promotion_rejection_still_uses_minimal_retry` | Compatibility regression | PASS |

## Coverage and known gaps

The optional Python `coverage` module is not installed, so a numeric backend
coverage percentage was not produced. All 92 discovered backend tests and all
28 frontend tests passed. Pyright, Ruff, and a frontend lint script are not
available in this repository; Python compilation, backend tests, the frontend
production build, dependency checks, and diff validation passed instead.

No production-account checkout was executed during this implementation. The
deployed server still runs the prior commit until the operator explicitly
approves push and deployment, after which the affected account must be checked
once from the WebUI to validate the upstream behavior.

## Merge evidence

- RED checkpoint: `277daa9` (`test: cover browser-compatible gcash checkout transport`)
- GREEN checkpoint: `9edb108` (`fix: align gcash checkout browser transport`)
- Diagnostic RED checkpoint: `57ce5fe` (`test: classify safe gcash checkout rejection reasons`)
- Diagnostic GREEN checkpoint: `c56f9f3` (`fix: classify safe gcash checkout rejection reasons`)
- Promotion-retry RED checkpoint: `35723ad` (`test: preserve minimal retry for rejected promotion`)
- Promotion-retry GREEN checkpoint: `8012268` (`fix: retry checkout without rejected promotion`)
- Client-hints RED checkpoint: `daaf5db` (`test: require complete chrome client hints`)
- Client-hints GREEN checkpoint: `6ffcad3` (`fix: complete gcash chrome client hints`)
