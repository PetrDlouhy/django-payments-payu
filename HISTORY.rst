.. :changelog:

History
-------

1.2.0 (2021-10-11)
++++++++++++++++++
* user Payment.billing_* correctly - the functions like ``get_user`` or ``get_user_email``, ``get_user_first_name`` and ``get_user_last_name`` were redundant and are not called anymore.
* Shop name is taken from provider configuration variable ``shop_name``

1.1.0 (2021-10-05)
++++++++++++++++++
* redirect to payment.get_failure_url() after API error, log the error

1.0.0 (2020-10-21)
++++++++++++++++++
* first major release
* many fixes
* recurring payments working
* proved by production environment

0.3.0 (2020-05-30)
++++++++++++++++++
* fix amount quantization
* add store_card parameter
* fix base url parameter for express form

0.2.0 (2020-04-13)
++++++++++++++++++
* Second release
* Fixed testing matrix

0.1.0 (2020-04-06)
++++++++++++++++++

* First release on PyPI.
* Still in development.
