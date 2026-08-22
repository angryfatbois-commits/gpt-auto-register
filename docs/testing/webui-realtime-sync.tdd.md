# WebUI realtime synchronization TDD evidence

## User journeys

- After importing mailboxes, a user sees the new rows in Mailbox Pool without
  pressing Refresh.
- During an automatic batch, logs from every worker remain visible in one live
  log panel.
- When a registration completes, its credential row appears in Registered
  Accounts without a manual reload.
- Header statistics and dashboard totals reflect the latest pool snapshot while
  a run is active or finishing.

## RED evidence

The regression tests were run before the corresponding fixes:

```text
node --test tests/realtime-sync.test.mjs
FAILED: concurrent worker two closed worker one; pool_stats was ignored;
missed workers had no stream; stream errors did not invalidate data; import
did not apply the response snapshot.

python -m unittest tests.test_realtime_sync -v
FAILED: registrar.start_registration() did not accept an event sink and the
auto-loop did not multiplex worker events.

node --test tests/auth.test.mjs
FAILED: protected layout had no delayed-auth realtime startup watcher.
```

Checkpoint commits: `677da35`, `2fbeeaf`, `3e134c2`, `fbe2a0e`, and `f429c72`.

## GREEN evidence

```text
node --test tests/*.test.mjs
28 tests passed

python -m unittest discover -s tests -p "test_*.py" -q
86 tests passed

npm run build (webui/frontend)
1711 modules transformed; production build succeeded

node --test --experimental-test-coverage tests/realtime-sync.test.mjs
9 tests passed; the runner emitted no instrumented production-source rows

npm audit --audit-level=high
found 0 vulnerabilities

python -m pip check
No broken requirements found.
```

## Test specification

| # | Guarantee | Evidence | Type | Result |
|---|---|---|---|---|
| 1 | Automatic worker logs use one multiplexed SSE channel and do not exceed browser connection limits | `webui/frontend/tests/realtime-sync.test.mjs`; `tests/test_realtime_sync.py` | Unit/integration | PASS |
| 2 | Worker events emitted before a browser connects are replayed for active workers | `tests/test_realtime_sync.py::test_subscriber_replays_events_emitted_before_the_browser_connected` | Backend unit | PASS |
| 3 | `pool_stats` snapshots update the shared statistics store immediately | `webui/frontend/tests/realtime-sync.test.mjs` | Frontend unit | PASS |
| 4 | Older polling responses cannot overwrite newer realtime snapshots | `webui/frontend/tests/realtime-sync.test.mjs` | Frontend unit | PASS |
| 5 | Import applies committed backend totals and invalidates cached tables | `webui/frontend/tests/realtime-sync.test.mjs` | Frontend regression | PASS |
| 6 | Pool, Registered Accounts, and Runs hydrate on first mount and refresh on activation/invalidation | `webui/frontend/tests/realtime-sync.test.mjs` | Frontend regression | PASS |
| 7 | Authenticated realtime polling/SSE starts when delayed authentication resolves | `webui/frontend/tests/auth.test.mjs` | Frontend regression | PASS |
| 8 | Stale overlapping table requests cannot overwrite newer rows | `webui/frontend/tests/realtime-sync.test.mjs` | Frontend regression | PASS |
| 9 | Local browser smoke flow shows imported mailbox, completed account, live multiplexed log, and updated totals without manual refresh | Isolated `127.0.0.1:8766` QA run with synthetic data | Browser QA | PASS |

## Implementation summary

- `registrar.py` now supports an optional per-run event sink while preserving
  the private queue used by manual registration streams.
- `auto_loop.py` forwards log/status packets for all workers over its existing
  single SSE channel and replays a bounded history for active workers.
- `runtime.js` consumes multiplexed events, reconciles final states, and keeps
  manual and automatic run state separate.
- `stats.js` accepts realtime snapshots and guards against stale polling.
- Pool, Registered Accounts, and Runs use request generations plus first-mount
  hydration.
- The protected layout watches for the authenticated tenant instead of
  permanently returning when auth is still loading.

## Coverage and known gaps

The runtime-store tests load controlled source through an in-memory harness;
Node's built-in coverage runner did not attribute those executed lines to a
production file. The repository also does not include Python `coverage.py`.
Therefore no numeric coverage percentage is claimed. Behavior is covered by
the focused frontend/backend regression tests, the full 28-test frontend and
86-test backend suites, and isolated browser QA. Browser QA used synthetic
local data only; no real ChatGPT registration, mailbox credential, OTP,
payment, or production account was exercised.

## Merge evidence

The RED/GREEN checkpoint sequence is preserved on the active branch. The final
production commit includes the source changes and rebuilt `webui/static`
assets served by FastAPI.
