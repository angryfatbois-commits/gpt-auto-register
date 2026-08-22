# GCash device-identity fallback TDD evidence

## User journey

As an administrator, I want the GCash capability check to reuse the account's
stored ChatGPT device identity, so a valid account is not rejected because the
request header and the `oai-did` cookie describe different devices.

## RED/GREEN evidence

| Stage | Command | Result |
| --- | --- | --- |
| RED | `python -m unittest tests.test_eligibility_api.GCashEligibilityApiTests.test_uses_oai_did_cookie_when_persisted_device_id_is_missing -v` | Failed: the old code generated a different UUID. |
| GREEN | Same command after the fix | Passed. |
| Regression | `python -m unittest discover -s tests -v` | 88 tests passed. |
| Frontend | `npm test` in `webui/frontend` | 28 tests passed. |
| Build | `npm run build` in `webui/frontend` | Passed. |
| Dependency checks | `npm audit --omit=dev` and `python -m pip check` | 0 vulnerabilities; no broken requirements. |

## Guarantees

| # | Guarantee | Test |
| --- | --- | --- |
| 1 | A valid UUID in the stored `oai-did` cookie is selected when `device_id` is empty. | `test_uses_oai_did_cookie_when_persisted_device_id_is_missing` |
| 2 | A malformed cookie value cannot inject data into the device header; the deterministic fallback is used. | `test_rejects_an_invalid_oai_did_cookie_before_building_headers` |
| 3 | Existing loopback/acknowledgement authorization remains enforced. | `test_requires_explicit_checkout_side_effect_acknowledgement` |
| 4 | GCash classification, persistence, and source-compatible Stripe probing remain regression-free. | `tests.test_gcash_probe`, `tests.test_gcash_source_workflow`, `tests.test_gcash_binary_policy`, `tests.test_eligibility_db` |

## Known gap

The bundled Python environment does not include the `coverage` package, so a
numeric coverage report was not generated. The complete backend and frontend
test suites were run instead.
