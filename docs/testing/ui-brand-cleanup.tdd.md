# UI Brand Cleanup TDD Evidence

## Source and user journey

No separate plan document was supplied. The journey was derived from the two
annotated production UI elements:

> As an authenticated operator, I see the project name and no third-party
> community or hosting promotion, so the WebUI presents only this project's
> identity.

## RED and GREEN evidence

| Stage | Command | Result | Evidence |
| --- | --- | --- | --- |
| RED | `node --test --test-name-pattern "active shell uses" webui/frontend/tests/english-ui.test.mjs` | Expected failure | `0` passed, `1` failed because `AdminLayout.vue` still contained `Outlook Register` and the promotional banner. |
| GREEN | Same focused command | Pass | `1` passed, `0` failed after the banner, its state, and its styling were removed and the project name was applied. |
| Frontend regression | `npm --prefix webui/frontend test` | Pass | `18` passed, `0` failed. |
| Backend regression | `python -m unittest discover -s tests -p "test_*.py" -q` | Pass | `83` tests ran successfully. |
| Production build | `npm --prefix webui/frontend run build` | Pass | Vite transformed `1711` modules and completed the production build. |
| Dependency audit | `npm --prefix webui/frontend audit --omit=dev` | Pass | `0` vulnerabilities. |

## Test specification

| # | What is guaranteed | Test or command | Type | Result |
| --- | --- | --- | --- | --- |
| 1 | The authenticated shell displays `GPT Auto Register`. | `english-ui.test.mjs: active shell uses the project name...` | UI contract | PASS |
| 2 | The active source and compiled production assets contain no QQ group, hosting recommendation, Ransuyun link, or promotional-banner state/style. | Same test | UI/build integration | PASS |
| 3 | Browser state remains isolated and fails closed when identity or storage is unavailable. | `auth.test.mjs: tenant browser storage fails closed...` | Unit | PASS |
| 4 | Existing authentication, eligibility, and GCash frontend contracts still pass. | Full frontend suite | Regression | PASS |
| 5 | Existing backend authentication, tenancy, registration, relay, and eligibility contracts still pass. | Full backend suite | Regression | PASS |

## Coverage and known gaps

`node --test --experimental-test-coverage webui/frontend/tests/*.test.mjs`
reported `100.00%` line coverage, `93.75%` branch coverage, and `90.00%`
function coverage for the instrumented frontend logic modules. Vue single-file
components are verified by the source/build contract and post-deployment browser
QA rather than Node's JavaScript coverage instrumentation.

## Checkpoints

- RED: `ff740f2 test(ui): require clean project branding`
- GREEN: `feat(ui): remove promotional banner and use project branding`

The checkpoints remain separate so the failing contract and the implementation
that satisfies it are both preserved in branch history.
