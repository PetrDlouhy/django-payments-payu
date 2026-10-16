.. :changelog:

History
-------
unreleased
**********
* Python 3.8 and 3.9 are no longer supported (``python_requires>=3.10``).
  Both are end-of-life, and GitHub's ``ubuntu-latest`` runner (Ubuntu 26.04
  from 2026-10-19) no longer provides them, so they can't be tested any more.
  Django 3.0 and 3.1, which run only on Python 3.9 and older, leave the test
  matrix with them.

2.7.1 (2026-10-15)
******************
* Apple Pay merchant validation only contacts Apple Pay gateway hosts
  (``https://(cn-)apple-pay-gateway(-*).apple.com/``); other validation URLs
  are refused with HTTP 400 and a warning log.

2.7.0 (2026-07-31)
******************
* the card widget's ``cardSuccess`` callback posts the tokenized card with
  ``fetch`` instead of jQuery's ``$.post``. The generated snippet was the
  package's only jQuery dependency, and it was invisible to host projects:
  a site that does not load jQuery (or whose jQuery is blocked) raised
  "ReferenceError: $ is not defined" the moment the buyer confirmed their
  card, so the token POST never fired and the payment silently died. The
  body stays form-encoded, since ``process_data`` reads
  ``request.POST['value']`` and routes ``application/json`` to the
  notification handler. A failed post is logged instead of surfacing as an
  unhandled promise rejection, and the callbacks no longer ``console.log``
  the token payload.

2.6.1 (2026-07-30)
******************
* ``create_order`` no longer creates a duplicate PayU order for a payment
  that already has one (``transaction_id`` set): duplicate submissions -
  a double POST, the browser returning from 3DS, a refresh - now look the
  existing order up at PayU and re-serve its continuation (3DS/CVV/pay-by-
  link URL) or the payment page when it is completed. Only a finally dead
  order (CANCELED/REJECTED) falls through to a fresh create. This removes
  the guaranteed-rejection retry that burned single-use tokens and, in
  pay-by-link flows, the risk of opening a second payable order.
* the don't-demote-CONFIRMED guards (duplicate ``create_order`` rejections,
  late/out-of-order notifications) now re-read the payment status from the
  database under ``select_for_update`` instead of trusting the in-memory
  instance. An instance loaded before a parallel webhook's confirmation
  committed could still demote a captured payment to ERROR seconds after the
  capture (and cancel its order in django-plans-payments hosts); the fresh
  locked read closes that window on every demotion path.

2.6.0 (2026-07-23)
******************
* permanent token errors (PayU ``INVALID_TOKEN``) now invalidate the stored
  renew token via the host's optional ``payment.invalidate_renew_token()``
  hook - renewal tasks stop retrying a token that can never work again and
  interactive users get a fresh card widget instead of a guaranteed
  failure. Retryable declines (insufficient funds etc.) are unaffected.
* rejected payments store the PayU transaction detail (``paymentFlow`` +
  card response codes, e.g. "114 - 3ds authentication error") in
  ``extra_data["rejection_transaction"]`` - the CANCELED notification
  itself carries no reason, so this is the only durable record of why a
  payment was declined.

2.5.0 (2026-07-21)
******************
* replace the stray debug ``print`` in the refund notification path and the
  bare ``Exception("Refund was not finelized")`` with a proper
  ``PayuApiError("Refund was not finalized")``.
* test suite: 100% line and branch coverage of the provider, and third-party
  warnings (e.g. mock's DeprecationWarning on Python 3.14) no longer break
  the no-warnings assertions.
* ``process_notification``: never demote a ``CONFIRMED`` payment. PayU can
  deliver duplicate/out-of-order notifications (e.g. a stale ``CANCELED``
  after ``COMPLETED``); applying one used to clobber a real capture, moving
  the payment to ``REJECTED``/``ERROR`` while it had actually been paid. The
  spurious callback is now acked (``200 ok``, so PayU stops retrying) and the
  ``CONFIRMED`` status is kept. Legitimate refunds are unaffected (they are
  handled by the dedicated ``refund`` branch). Mirrors the existing
  ``create_order`` guard added in 2.1.2.
* wallet tokens (Google Pay / Apple Pay) are validated before creating the
  PayU order: a token missing its cryptographic parts now fails the payment
  immediately with ``ERROR_WALLET_TOKEN_MALFORMED`` in ``extra_data``
  instead of PayU's vague ``APPLE_PAY_CLIENT_ERROR``, and each accepted
  token logs its non-sensitive metadata (the Apple Pay ``publicKeyHash``,
  which identifies the payment-processing certificate the token was
  encrypted for, and the Google Pay ``protocolVersion``) for diagnostics.
* add optional Apple Pay button to the express payment form (``apple_pay``
  provider parameter). The Apple Pay ``token.paymentData`` (which carries the
  ``data``/``signature``/``header``/``version`` PayU decrypts) is charged
  through a standard PayU order (pay-by-link method ``jp`` with base64
  ``authorizationCode``); with
  ``recurring_payments=True`` the first order is sent with ``recurring=FIRST``
  and the returned multi-use card token is stored for renewals. Merchant
  validation (``onvalidatemerchant``) is handled by the provider using the
  configured Merchant Identity certificate.

2.4.0 (2026-07-15)
******************
* Google Pay: add an optional ``button_color`` key in the ``google_pay``
  config passed to the button as ``buttonColor`` (``"black"`` / ``"white"``
  / ``"default"``). Google's brand guidelines require the white button on
  dark backgrounds.

2.3.0 (2026-07-04)
******************
* survive malformed PayU error responses: an integer ``status``
  (``{"status": 500}``) no longer crashes ``post_request`` with
  ``TypeError``, a missing ``codeLiteral`` no longer crashes the error log
  with ``KeyError``, and a non-JSON body (e.g. an HTML 502 page from a
  proxy) raises ``PayuApiError`` instead of a raw ``JSONDecodeError``. The
  payment now fails cleanly (``ERROR`` status + redirect to the failure
  page) in the first two cases.
* Google Pay: re-initialization guards for hosts that inject the express
  form dynamically (skip double init, initialize when pay.js is already
  loaded), and an optional ``button_radius`` key in the ``google_pay``
  config passed to the button as ``buttonRadius``.

2.2.0 (2026-07-03)
******************
* add optional Google Pay button to the express payment form (``google_pay``
  provider parameter). The Google Pay token is charged through a standard
  PayU order (pay-by-link method ``ap`` with base64 ``authorizationCode``);
  with ``recurring_payments=True`` the first order is sent with
  ``recurring=FIRST`` and the returned multi-use card token is stored for
  server-initiated renewals.
* create_order: don't overwrite the stored renew token when the order
  response echoes a pay method without card data (e.g. a wallet pay-by-link
  method), which is not a reusable card token.
* fix HtmlOutputField rendering its html autoescaped on Django 5.x - the
  "This payment is already being processed" error form showed literal
  "<br/><strong>..." markup to the user.

2.1.1 (2026-05-07)
******************
* fix create_order demoting an already CONFIRMED payment to ERROR when PayU
  rejects a duplicate request (e.g. the user's browser POSTs to the process
  URL again after 3DS while a parallel webhook has already confirmed the
  payment). The existing narrow guard for ERROR_ORDER_NOT_UNIQUE is
  generalised to keep CONFIRMED state regardless of PayU's rejection reason.

2.1.0 (2026-01-24)
******************
* add optional get_buyer_language argument to PayuProvider

2.0.0 (2025-05-29)
******************
* implement the wallet logic
* change extra_data with atomic operations to reduce concurrent problems
* support Django 5.2
* drop support for Python 3.7
* log error if the payment is switching status out from confirmed
* fix log warning if the payment is switching status out from confirmed

1.5.0 (2024-08-16)
******************
* log warning when status changes out from confirmed
* fix problem with form in Django 5.0
* support Django 5.1

1.4.6 (2024-07-23)
******************
* remove debug code committed by mistake

1.4.5 (2024-07-23)
******************
* fix safestring in Django 5.0

1.4.4 (2024-06-27)
******************
* fix PayuApiError: invalid response to refund

1.4.3 (2024-05-15)
******************
* fix "AttributeError: Manager isn't accessible via Payment instances" introduced in 1.4.1

1.4.2 (2024-05-14)
******************
* fix multiple deduction of the refund amount from `payment.captured_amount`
* change statuses of payments refunded with an amount greater than `payment.captured_amount` to `REFUNDED` instead of just deducing `captured_amount`

1.4.1 (2024-05-14)
******************
* fix captured_amount not being saved when processing data

1.4.0 (2024-04-12)
******************
* fix backward compatibility by making PayuProvider's get_refund_description argument optional
* add `renewal_triggered_by` parameter to `payment.set_renew_token`
* make PayuProvider.refund fail if get_refund_description is not provided
* make PayuProvider.refund raise PayuApiError if an unexpected response is received
* deprecate the default value of get_refund_description; set it to a callable instead
* deprecate `automatic_renewal` parameter of `payment.set_renew_token`; use `renewal_triggered_by` parameter instead
* deprecate `None` value of `renewal_triggered_by` parameter of `payment.set_renew_token`; set `"user"`/`"task"`/`"other"` instead

1.3.1 (2024-03-19)
******************
* Fix description on PyPI

1.3.0 (2024-03-19)
******************
* add get_refund_description and get_refund_ext_id arguments to PayuProvider
* add PayuProvider.refund
* update payment.captured_amount only when order is completed
* subtract refunds from payment.captured_amount rather than from payment.total
* rename PayuProvider.payu_api_order_url to payu_api_orders_url
* tests for Django 2.2-5.0 Python 3.7-3.12

1.2.4 (2022-03-17)
******************
* treat partial refunds
* tests for Django 2.2-4.0 Python 3.7-3.10


1.2.3 (2022-01-25)
******************
* better distinct PayU API errors

1.2.2 (2021-11-30)
******************
* solve the duplicate order case that errored already confirmed payment

1.2.1 (2021-10-29)
******************
* set fraud status if PayU anti-froud error
* store PayU error on payment

1.2.0 (2021-10-11)
******************
* user Payment.billing_* correctly - the functions like ``get_user`` or ``get_user_email``, ``get_user_first_name`` and ``get_user_last_name`` were redundant and are not called anymore.
* Shop name is taken from provider configuration variable ``shop_name``

1.1.0 (2021-10-05)
******************
* redirect to payment.get_failure_url() after API error, log the error

1.0.0 (2020-10-21)
******************
* first major release
* many fixes
* recurring payments working
* proved by production environment

0.3.0 (2020-05-30)
******************
* fix amount quantization
* add store_card parameter
* fix base url parameter for express form

0.2.0 (2020-04-13)
******************
* Second release
* Fixed testing matrix

0.1.0 (2020-04-06)
******************

* First release on PyPI.
* Still in development.
