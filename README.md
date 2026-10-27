# Merchant Payment Control Center

An interactive, local payment-operations application: authorize a synthetic purchase, capture part of it, issue partial refunds, inspect a balanced journal, replay notifications, and reconcile processor-statement fixtures. A **Stripe test-mode adapter is implemented but has not been verified end to end against a Stripe account**. Its code covers hosted Checkout Sessions with manual capture, payment status retrieval, capture, cancellation, refund requests, and signed webhook receipt handling.

The simulator works without an account or network. Stripe integration requires your own test-mode credentials. **No live processing, real card entry, terminal certification, or production-readiness claim is made.** Simulator data and Stripe data are separate views; synthetic fees and statements are never presented as Stripe settlement records.

## Integration status — read this first

**This is a working local simulator with an unverified Stripe test-mode adapter, not a fully integrated Stripe payment-operations system.**

| Component | Current status |
| --- | --- |
| Local payment lifecycle, journal, notification replay and reconciliation | Implemented and tested with synthetic data |
| Stripe Checkout, capture, cancellation and refund adapter | Code implemented; automated tests use mocked provider responses |
| Actual Stripe sandbox checkout, capture, refunds and webhook delivery | Not yet verified with account credentials; setup instructions are provided below |
| Stripe transactions feeding the journal | Not implemented; the journal currently contains simulator transactions only |
| Stripe fees, balance transactions and payouts feeding reconciliation | Not implemented; reconciliation currently compares synthetic CSV statements |
| Physical payment terminal / EMV processing | Not implemented; the terminal view and TLV examples are simulations |

Passing automated tests does **not** establish that the Stripe account integration works. Until account-based testing and provider-to-ledger/reconciliation work are completed, demonstrate the simulator and describe Stripe support as an unverified adapter. No successful Stripe transaction or end-to-end provider validation is claimed.

## Launch

Python 3.10+; runtime and tests use the standard library. From this repository:

```sh
python3 -m payments
```

Open **http://127.0.0.1:8770**. The local SQLite database is created at `data/payments.sqlite3` and excluded from Git. Choose another port/database with `python3 -m payments --port 9000 --db data/another.sqlite3`. Stop with Ctrl+C. Optional installation: `python3 -m pip install .` provides `merchant-control`; installation may download setuptools. Direct execution requires no installation.

## Five-minute demo

1. Choose **Run guided scenario** on Payments: $125 authorized, $100 captured, $25 refunded, then the exact refund command repeated. The duplicate creates no additional refund.
2. Inspect the payment timeline and remaining refundable balance. Try to refund more than $75: the engine rejects it without a partial database update.
3. Open **Ledger & events**. Inspect journal pairs and total debits/credits. Deliver a notification, then redeliver it; the durable inbox deduplicates it. Deliver an older notification after a newer one: the current projection is preserved.
4. Open **Reconciliation**. Generate a matching CSV and compare it. Generate the $1 mismatch fixture and compare again to see the exact reference and difference. Missing, duplicate, unknown, and invalid rows are also detectable. You can import a synthetic CSV file or edit its text.
5. Open **Terminal lab**. Run an approval, decline, or timeout scenario and inspect synthetic BER-TLV field structure. This is a local terminal-shaped simulation, not an SDK, EMV kernel, APDU conversation or physical device test.

Evidence JSON, CSV and reconciliation JSON downloads are available in the UI. The repeatable command-line equivalent is `python3 -m examples.demo`; [captured synthetic output](examples/demo-output.json) is included. The demo runs in a temporary database and does not contact Stripe.

## Stripe sandbox setup

Create your own Stripe sandbox/test-mode account. In the app's **Stripe sandbox** view, enter its `sk_test_…` secret key (or appropriately permitted `rk_test_…` key). Do not put keys in source, issues, screenshots or chat. The app checks the Balance endpoint and refuses credentials or responses indicating live mode. The test key stays in server memory until disconnect/restart; it is not persisted in the database or browser storage. The password fields clear after a connection attempt. For a restricted key, the relevant actions need Balance read, Checkout Session write, PaymentIntent read/write and Refund write access.

1. Connect the test account locally.
2. Enter an authorization amount and create hosted checkout.
3. Follow **Open Stripe-hosted TEST checkout**. Enter only [Stripe's official test payment details](https://docs.stripe.com/testing). Card entry happens on Stripe, never in this app.
4. Return here and choose **Sync Stripe payments**. The app reads the latest 20 PaymentIntents and displays those carrying this app's metadata marker.
5. Select an authorized payment and capture all or part of it. The manual-capture workflow normally releases the uncaptured remainder. Request partial refunds or cancel an uncaptured authorization as appropriate for its state.

A checkout return URL is not treated as proof of success. Sync queries Stripe. Stripe is authoritative for its payment lifecycle, capture limits, authorization validity and refund outcomes. Refund API results include their status; a request result is not a guarantee that a pending refund has completed.

Every Stripe write has an idempotency key derived from an operation ID. **Retry last Stripe request** reuses the last payload and operation ID, including after a page reload in the same browser session. An unavailable response is reported as an uncertain outcome, not a decline. Inspect Stripe and retry with the same operation before creating another one. Stripe may prune API-v1 idempotency records after 24 hours; do not assume an old operation can always be replayed safely. This app is not a durable, distributed payment command queue.

Only payments tagged with `portfolio=merchant-payment-control-center` can be managed through this adapter. The marker is a convenience scope guard, not a multi-user authorization mechanism. The UI currently scans only the latest 20 account PaymentIntents, without pagination. API version `2024-06-20` is pinned; test with your account before demonstrating the provider integration.

### Optional signed Stripe events

The app exposes `POST /webhooks/stripe`. It verifies Stripe's HMAC signature over the exact raw body, checks a ±300-second timestamp window, requires `livemode: false`, and durably deduplicates event IDs. Different bytes under an already-received event ID are rejected. The endpoint records limited receipt metadata (event ID, type, object ID, time), not the raw card/customer payload. It does not post provider transactions into the simulator's journal.

To receive local test events, install the official [Stripe CLI](https://docs.stripe.com/stripe-cli) and run:

```sh
stripe listen --forward-to http://127.0.0.1:8770/webhooks/stripe
```

Enter the CLI's signing secret in the app's optional webhook field when connecting. Restart/reconnect if that secret changes. Check the local verified-receipts table, then Sync for current payment state. A publicly accessible endpoint is not required for CLI forwarding. Actual CLI/provider delivery requires account access and is separate from the automated synthetic signature tests.

Alternatively the server can read `STRIPE_TEST_SECRET` and `STRIPE_TEST_WEBHOOK_SECRET` from its startup environment. They are never printed by the app. Environment management and account provisioning are the operator's responsibility. Never supply live credentials.

## Financial and reliability invariants

| Concern | Implemented behavior |
| --- | --- |
| Money representation | Integer USD cents; decimal, negative, boolean and oversized engine amounts rejected |
| Authorization | No journal posting; approval, decline, authentication and unknown-outcome scenarios |
| Capture | One final capture, positive and no greater than authorization; remainder released |
| Refund | Cumulative refunds bounded by captured funds; partial/full states |
| Command idempotency | SQLite unique key plus canonical payload digest; conflict on changed parameters |
| Atomicity | Domain changes, journal, outbox and command result committed together |
| Journal | Equal debit/credit pairs per movement; no float arithmetic |
| Notification replay | Durable unique inbox ID and body digest; no second projection application |
| Out-of-order delivery | Older synthetic notifications cannot replace a newer projection |
| Reconciliation | Reference-level comparison, duplicates/missing/unknown/invalid rows, exact cent differences |

The synthetic fee assumption is 2.9% + $0.30 per capture, rounded to integer cents. This is a demonstration policy, **not Stripe pricing**. Fees are not refunded in this simulator. Very small captures can therefore have negative net proceeds. Capture posts processor receivable against sales clearing; its simulated fee debits processing fees and credits processor receivable. Refunds reverse sales clearing against processor receivable. There is no bank payout posting or revenue-recognition model.

Statement fixtures contain `reference,net_cents,currency`. Captures expect their net amount after simulated fees; refunds expect negative amounts. The generator deliberately derives clean fixtures from local expectations, so it is a test-data generator, not independent proof of settlement correctness. Importing a different fixture exercises the comparison engine. Duplicate statement rows count in the statement total and are flagged separately. Reconciliation does not move money or alter journal entries.

## Architecture

```text
Browser UI ── local JSON API ── transactional simulator ── SQLite
                          │       ├─ journal + expected settlement
                          │       └─ outbox → inbox → latest projection
                          ├─ BER-TLV structural decoder
                          └─ Stripe test-only REST adapter → Stripe-hosted checkout

Stripe CLI/test endpoint → signature verification → durable receipt metadata
```

`engine.py` owns state transitions, idempotency, journal and reconciliation. `stripe_test.py` owns fixed-host HTTPS requests and test-mode restrictions. `webhooks.py` verifies exact-byte signatures. `server.py` serves a fixed UI and constrained endpoints. No client-specified provider URLs or generic file-serving routes are exposed. The TLV parser comes from the related [EMV workbench](https://github.com/cwenzeldev/emv-certification-workbench), under the same MIT license.

SQLite uses immediate write transactions, WAL and FULL synchronous mode. Connection-per-operation and read transactions give consistent snapshots. This deliberately favors inspectable single-machine behavior over distributed scalability.

## Tests and validation scope

```sh
python3 -m unittest discover -s tests -v
python3 -m examples.demo
```

Tests exercise bounds, invalid transitions, concurrent capture attempts, durable command deduplication, rollback, balanced entries, notification replay/order, reconciliation discrepancies, raw-body signature failures, HTTP endpoints, live-key rejection, and Stripe request construction. Stripe transport responses are **mocked in automated tests**; those tests do not prove that your account's Checkout, capture, refund, or webhook delivery works. The provider integration must also be exercised with your sandbox credentials. No successful provider-side transaction is claimed merely because mock tests pass.

GitHub Actions runs tests and the demo on Python 3.10, 3.12 and 3.13. Inspect actual workflow results for remote validation.

## Boundaries and next steps

This is a single-user local development app, with loopback binding, a Host guard, a per-process token for browser POSTs, no CORS allowance, CSP, and suppressed HTTP request logging. It is not public production ingress: there is no user authentication, tenant isolation, TLS termination, distributed lock service, rate limit or secret vault. A local process/user with access to your machine is outside these protections. Do not expose its port to the internet.

Synthetic notification dispatch is operator-driven, not a background delivery/retry worker. Unknown-outcome recovery and authentication completion in the simulator are scripted teaching states. Real Stripe authentication runs on hosted Checkout. Stripe event receipts are deduplicated, while payment state is explicitly fetched from the provider. Provider transactions, fees, disputes and real payouts are not reconciled into the synthetic ledger.

No real cardholder data, PAN storage, EMV cryptogram generation, PIN processing, physical reader integration, official certification, fraud decisioning, chargebacks or tax accounting. Future work could add provider-balance transaction reconciliation, durable provider-command recovery, refunds/events as independent state machines, pagination, and a real terminal test SDK with the appropriate hardware/account access.

## Primary references

- [Stripe Checkout Session creation](https://docs.stripe.com/api/checkout/sessions/create)
- [Stripe authorization and manual capture](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method)
- [Stripe partial refunds](https://docs.stripe.com/api/refunds/create)
- [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests)
- [Stripe webhook signature verification](https://docs.stripe.com/webhooks/signature)

MIT licensed. Independent portfolio project; no endorsement by Stripe, EMVCo or payment networks.
