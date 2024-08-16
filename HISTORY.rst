.. :changelog:

History
-------

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
