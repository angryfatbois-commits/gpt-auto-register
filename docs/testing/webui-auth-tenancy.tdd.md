# WebUI authentication and tenant isolation TDD evidence

## User journeys

- A visitor must sign in before any account, settings, registration, or
  eligibility API can be accessed.
- An administrator can create users and reset or revoke their access.
- Each user sees and changes only their own SQLite data and runtime processes.
- The first administrator can be created once from localhost or explicitly
  from environment variables without a source-code default password.

## RED evidence

The initial tests were run before implementation:

```text
python -m unittest tests.test_auth tests.test_auth_api
FAILED: webui.auth missing and /api/auth/* routes absent

node --test tests/auth.test.mjs
FAILED: /login route and AdminUsers.vue absent
```

Checkpoint: `fb42757 test(auth): define login and tenant isolation contract`.

## GREEN evidence

```text
python -m unittest discover -s tests -v
Ran 83 tests — OK

python -m unittest tests.test_auth tests.test_auth_api
Ran 9 tests — OK

npm test (webui/frontend)
16 tests — 16 passed

npm run build (webui/frontend)
1711 modules transformed — built successfully

npm audit --audit-level=high
found 0 vulnerabilities

python -m compileall -q . && python -m pip check
completed successfully
```

The follow-up regression cycle also covered protected-shell polling/SSE
shutdown, single-owner migration of legacy browser settings, cleanup of
SQLite connections in tenant reads and workers, and rejection of a
cross-origin initial-setup request. A local browser smoke test verified the
setup screen, sign-in, administrator user creation, standard-user navigation
guard, and logout without background 401 errors.

## Test specification

| # | Guarantee | Evidence | Type |
|---|---|---|---|
| 1 | Passwords use salted `scrypt` hashes and opaque sessions can be revoked | `tests/test_auth.py` | Unit |
| 2 | Unauthenticated API access is rejected and writes require CSRF | `tests/test_auth_api.py` | Integration |
| 3 | A normal user cannot call administrator APIs | `tests/test_auth_api.py` | Integration |
| 4 | Alice and Bob read only their own registered-account database | `tests/test_auth_api.py` | Integration |
| 5 | Run queues and auto-loop controllers are keyed by tenant database | `tests/test_auth.py` | Unit |
| 6 | Protected layout stops polling and SSE streams when it unmounts | `webui/frontend/tests/auth.test.mjs` | Frontend regression |
| 7 | Legacy browser settings are claimed by only one administrator | `webui/frontend/tests/auth.test.mjs` | Frontend unit |
| 8 | Auto-loop status polling closes its tenant SQLite connection | `tests/test_auth.py` | Unit |
| 9 | Initial setup rejects a cross-origin browser request | `tests/test_auth_api.py` | Integration |
| 10 | Login and user-management UI routes exist and remain English-only | `webui/frontend/tests/auth.test.mjs` | Frontend regression |

## Known boundaries

- Authentication state is stored in `webui/auth.db`; user account data is never
  stored there.
- Disabling a user revokes sessions but intentionally preserves that user's
  database for recovery. No admin action silently deletes credential data.
- HTTPS termination is deployment-specific and remains the operator's
  responsibility.
- Python coverage tooling is not installed in this workspace, so a numeric
  backend coverage percentage was not claimed. Node's built-in coverage for
  the tenant-storage module reported 94.74% line coverage for its focused
  frontend test.
