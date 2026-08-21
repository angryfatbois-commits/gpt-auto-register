# GPT Auto Register

Protocol-based ChatGPT account registration with a FastAPI and Vue 3 WebUI.
The project uses `curl_cffi` browser impersonation, OpenAI Sentinel proof-of-work,
and mailbox providers to complete registration without browser automation.

> Use this software only with accounts, email addresses, phone numbers, and
> payment profiles you are authorized to operate. You are responsible for
> complying with OpenAI's terms and all applicable laws.

## Highlights

- Browserless ChatGPT registration through the authorization protocol
- Outlook IMAP XOAUTH2 and Cloudflare catch-all mailbox support
- Optional SMS verification through supported activation providers
- Concurrent registration workers and proxy-pool rotation
- FastAPI backend with a Vue 3 and Element Plus WebUI
- Account credential storage and configurable export formats
- Exact `plus-1-month-free` Plus trial eligibility checks
- Side-effect-limited GCash payment-method availability checks

## Requirements

- Python 3.10 or newer
- Node.js 18 or newer for the WebUI build and Sentinel QuickJS path
- A supported mailbox configuration
- Optional proxy, SMS provider, and export-panel credentials

## Installation

```bash
git clone https://github.com/Regert888/gpt-auto-register.git
cd gpt-auto-register
python -m pip install -r requirements.txt
```

Build the frontend after changing files under `webui/frontend`:

```bash
npm --prefix webui/frontend ci
npm --prefix webui/frontend run build
```

## Start the WebUI

```bash
python start_webui.py
```

The default address is <http://127.0.0.1:8765/>.

To listen on another interface or port:

```bash
python start_webui.py --host 0.0.0.0 --port 8765
```

Do not expose the WebUI to an untrusted network without an authentication
layer. It can display and export account credentials.

## Command-line Registration

For an Outlook mailbox record:

```bash
python register_outlook.py "email----password----client_id----refresh_token"
```

The Microsoft refresh token must include the IMAP and offline-access scopes
required by the mailbox provider.

## Eligibility Checks

Eligibility checks are available from the **Registered accounts** page. Select
one or more rows and use the dedicated action for the check you want to run.

### Plus trial promotion

The Plus check calls:

```text
GET https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27
```

An account is reported as trial-eligible only when all of the following are
true:

- the account is on the Free plan;
- it does not have an active subscription; and
- `eligible_promo_campaigns.plus.id` is exactly `plus-1-month-free`.

The structured result includes the campaign ID, title, discount percentage,
duration, current plan, decision code, timestamp, and one of these
classifications:

- `eligible` — the exact promotion is available;
- `ineligible` — a conclusive response shows it is unavailable; or
- `unknown` — the request or response was inconclusive.

The existing `POST /api/registered/check_plus` request format and legacy UI
status fields remain supported.

### GCash payment-method availability

GCash uses a separate, explicit action and endpoint:

```text
POST /api/registered/check_gcash
```

Request body:

```json
{
  "emails": ["person@example.com"],
  "proxy": "http://proxy.example:8080"
}
```

The backend accepts this payment-adjacent endpoint from loopback clients and
requires an explicit acknowledgement header:

```text
X-GCash-Probe-Confirmation: checkout-side-effects-acknowledged
```

The WebUI adds that header only after the operator accepts the confirmation
dialog. Direct API clients must make the same acknowledgement. If the WebUI is
published through a reverse proxy, keep FastAPI private on loopback and require
authentication and TLS at the proxy. If the proxy must reach FastAPI from a
non-loopback address, set `GPT_AUTO_REGISTER_ADMIN_TOKEN` on the backend and
have the trusted proxy inject the matching `X-GPT-Admin-Token` header. Never
embed that admin token in frontend JavaScript.

The probe follows the source project's GCash capability sequence, adapted
clean-room and bounded to payment-capability stages:

1. Create a Philippines/PHP Checkout for the ChatGPT Plus monthly plan with
   the exact `plus-1-month-free` campaign and `check_card_proxy=true`. Use a
   Philippines proxy; a mismatched egress can be rejected as a billing-country
   mismatch.
2. Update the same checkout session with the campaign, plan, interval, and
   seat quantity.
3. Synchronize taxes for the same session using the observed PH/PHP region and
   the account email. Tax synchronization is best-effort; a rejection does not
   erase method evidence.
4. Resolve the same checkout session and collect payment-method evidence.
5. Ask Stripe Payment Pages or Stripe Elements for capability metadata. A live
   `cpmt_...` ID returned by Checkout is preferred; an ID may be supplied
   explicitly through `GCASH_CUSTOM_PAYMENT_METHOD_ID` when the upstream only
   returns an opaque custom-method slot. When Elements is queried, an opaque
   method is accepted only when the same ID is returned for the PH/PHP session;
   a localized or merchant-defined display label is not required.

The probe stops before checkout confirmation, custom-payment start, provider
redirect, or payment execution. It never submits a GCash account or authorizes
a charge. Creating and updating an ephemeral checkout are remote side effects,
so the WebUI always requires explicit confirmation before it runs.

See [GCash workflow provenance](docs/reference/source-gcash-workflow.md) for the
source commit, clean-room boundary, endpoint map, and excluded payment stages.

The decision depends only on payment-method evidence:

- an explicit GCash method is present -> `eligible` (`GCash available`);
- a live opaque `cpmt_...` entry makes an exact PH/PHP round trip through Stripe
  Elements -> `eligible` (`GCash available`);
- an explicit method list without GCash is present -> `ineligible`
  (`GCash unavailable`);
- a successful checkout has no usable method list -> `ineligible` with the
  `gcash_evidence_missing` decision, per the binary policy.
- a rejected or incomplete checkout response -> `ineligible` with a stable
  `checkout_*` or `gcash_evidence_incomplete` decision.

Amount values are retained only as diagnostics. Zero and positive amounts do not
change availability. The capability request itself remains restricted to the
PH/PHP checkout contract; an explicit non-PH/PHP response is not trusted for an
opaque custom-method match.

The WebUI deliberately exposes only two account outcomes: `GCash available` or
`GCash unavailable`. Missing credentials, authentication failures, malformed
responses, and transport failures are also normalized to `GCash unavailable`
because the requested contract has no third `unknown` state. Their stable
`decision`, `status`, `retryable`, and (when applicable)
`custom_method_probe_status`/`custom_method_probe_failure` fields remain
available in the detail view/logs so an operator can distinguish “not present”
from “could not read”. These diagnostics contain stage codes only, never raw
Stripe responses or credentials.
For a real PH GCash check, use a working Philippines proxy; a direct/US exit
will normally return the US method set and therefore `GCash unavailable`.
If a deployment needs to supply a known opaque custom-method ID for the
optional Stripe Elements request, set `GCASH_CUSTOM_PAYMENT_METHOD_ID`; no ID
is hardcoded in the application.

The endpoint name and stored key remain `check_gcash` for API compatibility.
Results saved by an older build (for example, `checkout_http_400` with the
legacy `GCash status unknown` label) are normalized to `GCash unavailable` when
read. Running the check again replaces the legacy record with the new
proxy-aware result and preserves its technical decision code.

### Proxy behavior

The selected proxy is used consistently for checkout, promotion update, tax
sync, and capability reads. GCash uses the explicit PH/PHP checkout contract,
so the proxy must exit in the Philippines; a failed or mismatched proxy request
does not silently fall back to a direct connection. Stripe Elements capability
reads use an isolated browser session with a matching fingerprint/User-Agent and
one bounded retry on transport-only failures. Proxy credentials, access
tokens, cookies, checkout session IDs, customer secrets, and raw upstream
bodies are excluded from stored eligibility results.

## Stored Results

Eligibility data is stored inside the existing `registered.extra_json` field:

```text
plus_check
gcash_check
```

No database migration is required. If a later attempt is inconclusive, its
record retains the last conclusive verdict for reference.

The local database is `webui/webui.db`. It is ignored by Git and may contain
sensitive credentials. Back it up before upgrading, and never commit it.

## Tests

Run the backend tests:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Run the frontend logic tests:

```bash
npm --prefix webui/frontend test
```

Build the production frontend:

```bash
npm --prefix webui/frontend run build
```

The eligibility tests use fake transports. They do not contact ChatGPT or
Stripe and explicitly verify that no payment confirmation or start endpoint is
called.

## Project Layout

| Path | Purpose |
| --- | --- |
| `auth_flow.py` | ChatGPT authorization and registration protocol |
| `eligibility.py` | Plus promotion parsing and safe result contracts |
| `gcash_probe.py` | Side-effect-limited GCash capability probe |
| `http_client.py` | TLS impersonation and proxy-aware HTTP sessions |
| `mail_providers/` | Mailbox provider implementations |
| `sms_provider.py` | Optional SMS activation providers |
| `webui/app.py` | FastAPI routes |
| `webui/db.py` | SQLite persistence |
| `webui/frontend/` | Vue 3 frontend source |
| `tests/` | Offline unit and integration tests |

## Security Notes

- Keep access tokens, refresh tokens, cookies, mailbox credentials, proxy
  credentials, SMS API keys, and export-panel keys out of Git.
- Keep the WebUI bound to localhost unless a trusted reverse proxy provides
  authentication and TLS.
- Treat a negative GCash result as unavailable under the binary policy; inspect
  its decision/status fields before retrying repeatedly, since retries can
  trigger upstream rate limits.
- ChatGPT and Stripe endpoints used by the eligibility checks are not stable
  public APIs and may change without notice.
- Review `git diff` before publishing changes and run dependency audits as part
  of release preparation.

## License

This repository is licensed under the GNU Affero General Public License v3.0.
See [LICENSE](LICENSE).
