# GCash auth-session refresh TDD evidence

## Source and user journeys

No plan file was supplied. The journeys were derived from the repeated
production result `checkout_http_400`, which occurs before Stripe payment-method
evidence can be read, and from the requirement that diagnostic data must not
expose credentials.

- As an administrator, I want the GCash probe to refresh an existing ChatGPT
  browser session through the selected proxy, so Checkout can use a current
  access token and payment cookies.
- As an administrator, I want refreshed credentials accepted only for the
  selected account, so one account can never inherit another account's session.
- As an administrator, I want failed-check diagnostics to be useful but
  allowlisted and sanitized, so investigating a false negative does not expose
  tokens, cookies, response values, or account identifiers.
- As an administrator, I want every registered-account read surface to remove
  credential-like metadata recursively, including camelCase and snake_case key
  variants.

## RED/GREEN evidence

| Stage | Command | Result |
| --- | --- | --- |
| Metadata RED | `python -m unittest tests.test_eligibility_db.EligibilityPersistenceTests.test_read_paths_resanitize_tampered_eligibility_results -v` | Failed as intended because `metadata.accessToken` and related variants were returned by a registered-account read surface. |
| Metadata GREEN | Same command after canonical recursive key filtering | 1 test passed. |
| Auth/session GREEN | `python -m unittest tests.test_gcash_probe tests.test_eligibility_db -v` | 59 tests passed during the security re-review. The pre-implementation RED output for the auth/session cases was not preserved in this handoff, so it is not claimed here. |
| Full backend | `python -m unittest discover -s tests -q` | 106 tests passed. |
| Frontend | `npm test` in `webui/frontend` | 29 tests passed. |
| Python compilation | `python -m compileall -q gcash_probe.py webui tests` | Passed. |
| Production build | `npm run build` in `webui/frontend` | Passed. |
| Dependency checks | `python -m pip check` and `npm audit --omit=dev --audit-level=high` | No broken requirements; 0 vulnerabilities. |
| Diff validation | `git diff --check` | Passed; only Git's existing LF-to-CRLF checkout warnings were reported. |

## Test specification

| # | What is guaranteed | Test target | Type | Result |
| --- | --- | --- | --- | --- |
| 1 | Invalid access-token header values are rejected before any network request | `test_invalid_access_token_header_value_is_rejected_without_network` | Unit/security | PASS |
| 2 | A NextAuth session refresh uses the same selected transport and applies accepted credentials only to the current probe | `test_auth_session_refresh_uses_fresh_token_and_cookies_for_checkout_only` | Protocol integration | PASS |
| 3 | Tokens with an untrusted issuer, mismatched account, mismatched claims, or invalid header bytes are not adopted | Refreshed-token rejection tests in `tests/test_gcash_probe.py` | Security regression | PASS |
| 4 | A refresh failure falls back to the stored credentials without changing the GCash evidence rule | `test_auth_session_refresh_failure_falls_back_to_existing_credentials` | Failure-path regression | PASS |
| 5 | Invalid cookie pairs and identity header values are not forwarded | Cookie/header validation tests in `tests/test_gcash_probe.py` | Input-validation regression | PASS |
| 6 | Refreshed cookies are not adopted when the token cannot be bound to the selected account | `test_auth_session_without_bound_token_does_not_adopt_refreshed_cookies` | Tenant-isolation regression | PASS |
| 7 | Checkout HTTP 400 diagnostics are disabled by default and, when enabled, expose only status/shape buckets | GCash safe-diagnostic tests in `tests/test_gcash_probe.py` | Data-exposure regression | PASS |
| 8 | Persisted eligibility output retains only allowlisted scalar fields and the UI accepts only allowlisted refresh status codes | DB and frontend eligibility tests | Storage/UI security | PASS |
| 9 | Registered-account read surfaces recursively remove credential-like metadata keys across common naming variants | `test_read_paths_resanitize_tampered_eligibility_results` | API data-exposure regression | PASS |

## Coverage and known gaps

The optional Python `coverage` module is not installed, so a numeric backend
coverage percentage could not be produced. The repository also has no frontend
lint script and does not declare Pyright or Ruff. Python compilation, all 106
discovered backend tests, all 29 frontend tests, the production build, the
dependency checks, and diff validation passed.

No production account was probed during this verification. A `GCash available`
result still requires affirmative GCash method evidence; PH/PHP, a zero amount,
or a successful session refresh alone never produces an available result. A
user-confirmed WebUI retry after deployment remains the required production
validation step.

## Merge evidence

The implementation, regression tests, and generated frontend assets are kept in
one reviewed fix commit. This report preserves the available RED/GREEN evidence
and explicitly records the missing historical auth/session RED output rather
than inventing it.
