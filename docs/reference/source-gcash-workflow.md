# GCash workflow provenance and implementation boundary

This note records the protocol facts inspected in
`2951461586/GPT-Register-Tool` at commit
`593e994de16255d0fa0529ea1222ec1d6f8f4f81` (2026-08-19). The repository has no
root `LICENSE` file. The target therefore uses a clean-room implementation:
the endpoint order and request concepts were recorded, but source code,
comments, fixtures, fake identities, and opaque payment identifiers were not
copied.

## Observed source sequence

The source GCash adapter's capability-only branch performs these stages on one
checkout session:

1. Create a custom ChatGPT Plus checkout and ask Checkout to perform its proxy
   card check.
2. Update the session with the monthly Plus plan and the
   `plus-1-month-free` campaign.
3. Submit a best-effort tax synchronization for the same session.
4. Resolve the session to obtain the current payment-method set.
5. Query Stripe custom-payment capability metadata using a customer-session
   secret and the custom-method type supplied by the checkout/configuration.
6. Return a capability result without calling the source adapter's confirm or
   custom-payment-start stages when probe-only mode is selected.

The source's non-probe branch continues from capability to custom-method
confirmation and provider redirect. That branch is intentionally not imported
into this application.

## Target implementation

`gcash_probe.py` now implements the safe subset with this order:

```text
POST /backend-api/payments/checkout
POST /backend-api/payments/checkout/update
POST /backend-api/payments/checkout/taxes
GET  /backend-api/payments/checkout/{processor}/{checkout_session}
POST /v1/payment_pages/{checkout_session}/init   (standard methods)
GET  /v1/elements/sessions                      (custom method metadata)
```

The Checkout request is explicitly `PH`/`PHP`, carries the exact campaign ID,
and sets `check_card_proxy=true`. Every later ChatGPT request is bound to the
session ID and processor returned by the first response. The tax stage only
uses the observed region and the account email supplied by the local API; it
does not invent a billing address or submit payment credentials. Update/tax
failures are retained as technical diagnostics and do not erase affirmative
method evidence.

`GCASH_CUSTOM_PAYMENT_METHOD_ID` is an optional deployment setting for an
opaque custom-method ID that the operator has independently verified. The
application does not embed the source repository's opaque identifier.

## Classification boundary

The user-facing result remains binary:

- explicit GCash evidence or a GCash custom-method candidate: `GCash available`;
- explicit method evidence without GCash, missing evidence, authentication
  failure, or transport failure: `GCash unavailable`.

Amount and currency are retained for diagnostics and do not override method
availability, matching the requested policy. Technical `decision`, `status`,
and `retryable` fields remain available for troubleshooting.

The probe never calls checkout confirmation, custom-payment start, provider
redirect, or any payment execution endpoint. The WebUI's existing explicit
side-effect acknowledgement remains mandatory because creating/updating an
ephemeral checkout is still a remote side effect.
