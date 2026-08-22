# Automatic Plus eligibility TDD evidence

## Source and user journey

No plan file was supplied. The journey was derived directly from the requested
Automatic Batch behavior:

As an administrator, I want every successfully registered automatic-batch
account to receive its Plus trial eligibility result automatically, so the
Registered Accounts page normally needs no separate manual Plus check.

## RED/GREEN evidence

| Stage | Command | Result |
| --- | --- | --- |
| RED | `python -m unittest tests.test_plus_probe tests.test_realtime_sync -v` | Failed as intended: `plus_probe` did not exist, the auto-loop had no post-registration hook, and no Plus result was persisted. |
| Failure-isolation RED | `python -m unittest tests.test_realtime_sync.RealtimeRunEventTests.test_automatic_plus_hook_crash_does_not_change_registration_success -v` | Failed because the automatic worker never invoked a Plus hook. |
| GREEN | `python -m unittest tests.test_plus_probe tests.test_realtime_sync tests.test_eligibility_api.PlusEligibilityApiTests -v` | 13 focused tests passed after the shared probe and automatic hook were implemented. |
| Full backend | `python -m unittest discover -s tests -q` | 121 tests passed. |
| Focused coverage | `python -m trace --count --missing --summary --module unittest tests.test_plus_probe` | `plus_probe.py`: 92.5% executable-line coverage. |

## Test specification

| # | What is guaranteed | Test target | Type | Result |
| --- | --- | --- | --- | --- |
| 1 | A successful automatic registration invokes the Plus check before worker completion | `test_auto_loop_passes_a_multiplexed_event_sink_to_registration` | Integration | PASS |
| 2 | Providers that replace a placeholder address use the final registered email | `test_post_registration_plus_check_uses_final_email_and_persists_result` | Integration | PASS |
| 3 | The worker's selected proxy is reused and no direct fallback occurs | Plus probe and auto-loop proxy tests | Transport/security | PASS |
| 4 | The request never follows redirects while carrying authorization | `test_eligible_probe_reuses_selected_proxy_and_blocks_redirects` | Security regression | PASS |
| 5 | Header-injection token/device values are rejected or replaced before networking | Plus probe input-validation tests | Security unit | PASS |
| 6 | Exact `plus-1-month-free` evidence is persisted before the final automatic refresh | Plus probe classifier plus auto-loop persistence test | Integration | PASS |
| 7 | Transient network, parsing, or storage failures do not change a completed registration into a failure | Automatic failure-isolation tests | Failure-path regression | PASS |
| 8 | Result/SSE/log surfaces expose only stable decisions, statuses, and allowlisted English labels | Plus probe security tests and auto-loop failure tests | Data-exposure regression | PASS |
| 9 | Existing manual Plus-check API behavior continues to use the shared probe | `PlusEligibilityApiTests` | API regression | PASS |
| 10 | Failed registrations never invoke the supplementary Plus check | `test_failed_registration_never_runs_plus_check` | Failure-path regression | PASS |
| 11 | One manual batch probe failure cannot abort later accounts or expose its exception text | `test_one_plus_probe_failure_does_not_abort_remaining_accounts` | API/security regression | PASS |

## Coverage and known gaps

Python's standard-library `trace` runner reports 92.5% executable-line coverage
for the new `plus_probe.py` module, exceeding the 80% target. Full verification
also covers the auto-loop integration through real temporary tenant databases.

No live ChatGPT account or production Automatic Batch was executed during this
implementation. Upstream transport failures remain intentionally retryable and
are not persisted, so an affected row remains available for a later manual
retry. The manual Registered Accounts action is retained for older accounts and
operational retries.

## Merge evidence

- RED checkpoint: `4909c38` (`test(plus): cover automatic batch eligibility check`)
- GREEN checkpoint: `3eb67eb` (`feat(plus): check eligibility during automatic batch`)

Additional coverage cases were added after GREEN to exercise safe HTTP, JSON,
proxy, missing-token, oversized-proxy, and allowlisted-log branches.
