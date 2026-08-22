# GCash workflow provenance and implementation boundary

This note records the protocol facts verified from the supplied browser HAR and
the clean-room implementation boundary. The raw HAR remains outside the
repository and is never used as a fixture.

## Verified browser sequence

The relevant authenticated requests were:

```text
GET  /backend-api/accounts/check/v4-2023-04-27
POST /backend-api/payments/checkout
GET  https://api.stripe.com/v1/elements/sessions
```

No checkout update, tax, resolve, Payment Pages, confirmation, redirect,
subscribe, payment-intent, or custom-payment-start request appeared in the
verified capability flow.

The browser also performs a transport preflight around the Checkout request:

```text
GET  https://chatgpt.com/                           (best-effort bootstrap)
GET  https://chatgpt.com/sentinel/<current>/sdk.js (when advertised)
POST https://chatgpt.com/backend-api/sentinel/req  (flow=chatgpt_checkout)
POST https://chatgpt.com/backend-api/sentinel/ping (best-effort heartbeat)
```

These calls prepare short-lived browser metadata; they are not additional
eligibility stages. The implementation discovers the current Sentinel SDK URL
from the live same-origin response, validates its origin and path, and creates
a fresh token for each probe. It never copies a token, telemetry sample, or
deployment attestation from the HAR. An attestation is used only when the
current response (or a validated operator environment value) supplies one;
signatures are not fabricated or persisted.

The checkout request contains:

```json
{
  "entry_point": "all_plans_pricing_modal",
  "plan_name": "chatgptplusplan",
  "billing_details": {"country": "PH", "currency": "PHP"},
  "checkout_ui_mode": "custom",
  "promo_campaign": {
    "promo_campaign_id": "plus-1-month-free",
    "is_coupon_from_query_param": false
  }
}
```

The body does not contain `check_card_proxy`. Checkout due is read only from
`checkout_state.total.total.minorUnitsAmount`. Stripe receives the exact opaque
custom-method ID and returns a matching method whose safe display name is
`GCash`. Stripe's preference country may be VN and does not invalidate a
PH/PHP checkout.

## Target implementation

`gcash_probe.py` uses one ChatGPT session for accounts/check and checkout, then
an isolated Stripe session. Both sessions use the selected proxy, the same
Chrome impersonation and User-Agent, and no direct or alternate-fingerprint
fallback. The supported automated TLS profile is Chrome 146 because that is
the newest Chrome profile available in the installed `curl_cffi`; the supplied
HAR was captured by Chrome/Brave 151, so the implementation does not claim
byte-for-byte browser fidelity. A transient or strict-HAR contract rejection
may retry once with the identical payload and identity; HTTP 400/422 is only
eligible for this retry on the first strict Checkout attempt.

If the stored account record contains a NextAuth session cookie, an optional
same-origin `/api/auth/session` preflight refreshes the access token before
the three HAR stages. It is not part of the capability decision; refreshed
credentials are accepted only after account-claim correlation and remain in
memory for the current probe.

The combined result contains independent Plus and GCash verdicts. Plus records
the exact campaign, 100% discount, and one-month duration. GCash is available
only after:

1. PH/PHP billing evidence is present;
2. the exact checkout total is structurally readable;
3. the checkout custom ID makes an exact Stripe round trip; and
4. Stripe explicitly names the matching method `GCash`.

Zero due and positive due both keep GCash available; the result separately
reports `amount_minor`, `amount_status`, and `zero_payment`. Malformed due is
incomplete evidence, is never converted to zero, and fails closed.

The WebUI retains its explicit checkout acknowledgement and loopback/admin
token gate. The bootstrap request may carry the selected account's access
token, account header, and sanitized cookie in memory so the current page can
return current metadata; these values are never returned by the probe. No
credential, cookie, proxy secret, customer secret, checkout ID, opaque method
ID, Sentinel token, attestation, or raw response is returned or persisted.

## Source boundary

The reference repository was used only to compare high-level protocol concepts.
No source code, comments, identities, opaque IDs, credentials, or raw response
body was copied. Payment execution remains outside this application's scope.
