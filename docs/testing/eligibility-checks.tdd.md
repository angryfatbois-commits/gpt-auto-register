# Eligibility Checks: TDD Evidence

> **Superseded workflow note (2026-08-22):** The historical protocol table in
> this document describes the former update/tax/resolve implementation. The
> current contract is documented and tested in
> [`har-checkout-workflow.tdd.md`](har-checkout-workflow.tdd.md).

## Source

The user journeys and acceptance criteria were derived during the gated
`orch-add-feature` workflow. The implementation is clean-room: protocol facts
were researched, but source code, tests, fixtures, comments, and opaque custom
payment method constants were not copied from the unlicensed reference
repository.

## User journeys

1. As an account operator, I want to see whether a saved Free account is
   eligible for the exact `plus-1-month-free` campaign so that I do not confuse
   another offer with the requested Plus trial.
2. As an account operator, I want to explicitly probe GCash availability so
   that every account has one of two clear outcomes: available or unavailable.
3. As a security-conscious operator, I want the GCash probe to stop before
   checkout confirmation or payment start so that an availability check cannot
   execute a payment.
4. As a WebUI user, I want persisted English binary status labels and stable
   technical reason codes so that every account remains easy to classify while
   operational failures are still diagnosable.

## RED evidence

The initial backend suite was run before production modules or endpoints
existed:

```text
python -m unittest discover -s tests -p "test_*.py" -v
FAILED (errors=9)
ModuleNotFoundError: No module named 'eligibility'
ModuleNotFoundError: No module named 'gcash_probe'
ImportError: cannot import name 'CheckGCashReq' from 'webui.app'
AttributeError: module 'webui.db' has no attribute 'update_eligibility_check'
```

The initial frontend logic test also failed before its implementation existed:

```text
npm test
ERR_MODULE_NOT_FOUND: webui/frontend/src/eligibility.js
```

Additional boundary tests were introduced and observed RED for dynamic custom
method discovery, missing Plus plan evidence, log redaction, and
checkout-session path injection before the corresponding fixes were made.

The method-only policy RED checkpoints are reachable from the active branch:

- `d9458e9` — classifier cases for amount/currency-independent availability;
- `f445eb1` — minimal checkout and capability transport contract.

The source-workflow RED checkpoint is preserved in `31643a6`:

```text
python -m unittest tests.test_gcash_source_workflow -v
RED: 2 failures
- promotion/tax stages were absent from the target probe
- configured custom-method fallback was not queried
```

## GCash availability policy-change evidence

The requested policy change was tested before and after the classifier edit:

```text
python -m unittest tests.test_gcash_probe.GCashClassificationTests -v
RED: 5 failures in the positive-amount, missing-evidence, and conflict cases
GREEN: 10 tests passed after the classifier update
```

The proxy-aware regression was also observed RED before implementation:

```text
python -m unittest tests.test_gcash_probe -v
RED: 3 failures
- checkout still forced billing_details.country=PH
- incomplete capability responses still returned unknown
- billing-country mismatch still returned unknown
```

A controlled localhost check confirmed the old `checkout_http_400` cause was a
billing-country/request-country mismatch. The source-compatible implementation
now makes the PH/PHP contract explicit and documents the required Philippines
egress, so a mismatched proxy is reported with a stable billing-country reason
instead of being silently retried through a direct connection. No credentials
or upstream response bodies were stored in the evidence report.

The full backend suite then passed with 62 tests. The new cases guarantee that
the method-only decision ignores amount/currency, recognizes explicit and
dynamic custom-method evidence, and treats absent/incomplete method evidence as
conclusively unavailable. The GCash checkout is explicitly PH/PHP;
transport/authentication failures are normalized to the requested binary
`GCash unavailable` result while their technical reason codes remain available
for diagnosis.

The source-workflow regression suite additionally guarantees the ordered
`checkout → promotion update → tax sync → resolve → Stripe capability` path,
same-session payload binding, PH/PHP checkout contract, and the absence of
confirmation/start/payment execution calls.

## Test specification

| # | What is guaranteed | Test target | Type | Result |
| --- | --- | --- | --- | --- |
| 1 | Only the exact `plus-1-month-free` campaign is trial-eligible | `tests/test_eligibility.py` | Unit | PASS |
| 2 | Active, deactivated, Free/no-offer, malformed, and missing-plan states are classified explicitly | `tests/test_eligibility.py` | Unit | PASS |
| 3 | Authorization, cookie, and proxy credentials are redacted from diagnostic text | `tests/test_eligibility.py` | Security unit | PASS |
| 4 | GCash availability depends only on payment-method evidence; amount and currency are diagnostics | `tests/test_gcash_probe.py` | Unit | PASS |
| 5 | Explicit GCash, dynamic custom-method IDs, generic method lists, nested method objects, and missing evidence are classified safely | `tests/test_gcash_probe.py` | Unit | PASS |
| 6 | Standard methods use Stripe Payment Pages init; live custom IDs use Stripe Elements, and no copied ID is embedded | `tests/test_gcash_probe.py` | Transport contract | PASS |
| 7 | The probe uses checkout → promotion update → tax sync → resolve → Stripe capability and never confirm, start, or payment execution | `tests/test_gcash_probe.py`, `tests/test_gcash_source_workflow.py` | Security integration | PASS |
| 8 | Upstream session IDs cannot inject a different path | `tests/test_gcash_probe.py` | Security unit | PASS |
| 9 | API input is deduplicated and bounded, missing tokens are safe, and one failure does not abort the batch | `tests/test_eligibility_api.py` | API integration | PASS |
| 10 | Safe results persist without a schema migration and normalize legacy GCash records to the binary verdict | `tests/test_eligibility_db.py`, `tests/test_gcash_binary_policy.py` | Database integration | PASS |
| 11 | English GCash summaries, details, and status colors remain stable | `webui/frontend/tests/eligibility.test.mjs` | Frontend unit | PASS |
| 12 | Plus requests cannot follow redirects while carrying authorization and cannot silently select another account | `tests/test_eligibility_api.py`, `tests/test_eligibility.py` | Security regression | PASS |
| 13 | GCash requires backend side-effect acknowledgement and either loopback access or a matching reverse-proxy admin token | `tests/test_eligibility_api.py` | API security integration | PASS |
| 14 | Concurrent Plus and GCash persistence cannot overwrite the other result | `tests/test_eligibility_db.py` | Concurrency integration | PASS |
| 15 | Resolve/Stripe capability failures do not erase conclusive method evidence | `tests/test_gcash_probe.py` | Transport contract | PASS |
| 16 | Element Plus, project-owned UI text, and shipped static assets are English-only | `webui/frontend/tests/english-ui.test.mjs` | Frontend/build regression | PASS |
| 17 | GCash Checkout uses the explicit PH/PHP contract and reports a mismatched proxy as a billing-country failure | `tests/test_gcash_probe.py`, `tests/test_gcash_source_workflow.py` | Live-regression transport contract | PASS |
| 18 | The UI explains the PH/PHP checkout, promotion update, tax sync, and Philippines proxy requirement | `webui/frontend/tests/eligibility.test.mjs` | Frontend unit | PASS |
| 19 | A nonliteral Elements label is accepted only for an exact PH/PHP Checkout-to-Elements custom-method round trip; mismatched regions/IDs remain unavailable | `tests/test_gcash_probe.py` | Network-probe regression | PASS |
| 20 | Elements capability uses an isolated Stripe session, matching browser fingerprint/UA headers, and transport failures expose only a stable stage code | `tests/test_gcash_probe.py` | Security/transport regression | PASS |
| 21 | GCash detail tooltips show sanitized capability diagnostics without raw secrets or opaque IDs | `webui/frontend/tests/eligibility.test.mjs` | Frontend security regression | PASS |

## GREEN evidence

Backend suite:

```text
python -m unittest discover -s tests -p "test_*.py" -v
Ran 74 tests
OK
```

Frontend suite:

```text
npm --prefix webui/frontend test
tests 12, pass 12, fail 0
```

Production frontend:

```text
npm --prefix webui/frontend run build
1702 modules transformed
built successfully
```

Additional validation:

```text
python -m compileall -q eligibility.py gcash_probe.py webui tests
compileall: OK

python -m pip check
No broken requirements found.

npm audit --omit=dev --audit-level=high
found 0 vulnerabilities
```

Live localhost regression after the Stripe fingerprint/UA fix:

```text
7 accounts checked; 2 eligible (`accepted`, exact custom-method match),
5 ineligible (`not_requested`, no GCash/custom method exposed),
0 Stripe transport failures; all 7 checkout responses were PH/PHP.
```

## Coverage

The environment did not include the third-party `coverage.py` package. Python's
standard-library `trace` module was used instead, with no new project
dependency:

```text
eligibility.py  100.0%
gcash_probe.py  100.0%
```

Both new backend modules exceed the required 80% line coverage.

## Known gaps

- Most tests use dependency-injected fake transports. A controlled localhost
  regression batch was run against seven authorized saved accounts; no payment
  confirmation, custom-payment start, or payment execution endpoint was called.
- GCash availability is regional checkout capability, not proof that a saved
  GCash wallet is attached. Use a working Philippines exit to inspect the PH/PHP
  method set. A direct or non-PH exit can conclusively report GCash unavailable
  for that checkout region.
- The upstream endpoints are not stable public APIs; successful responses with
  absent or incomplete method evidence, as well as transport/authentication
  failures, are treated as unavailable by the requested binary policy. Stable
  decision/status fields retain the operational reason.
- The existing application has no built-in WebUI authentication or global rate
  limiter. It should remain bound to localhost or be placed behind an
  authenticated TLS reverse proxy.
- The original lockfile references a third-party npm mirror that is blocked by
  the local runtime policy. Validation installed the same manifest versions from
  the official npm registry without replacing the project lockfile wholesale.

## Merge evidence

RED is preserved in `d9458e9`, `f445eb1`, `ec8bfb1`, and `131fcf2`. GREEN is
captured by the commands above and by implementation checkpoint commits
`4c9d6b8` and `92d4d5f` on this branch.
