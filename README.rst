=============================
Django payments payu
=============================

.. image:: https://badge.fury.io/py/django-payments-payu.svg
    :target: https://badge.fury.io/py/django-payments-payu

.. image:: https://travis-ci.org/PetrDlouhy/django-payments-payu.svg?branch=master
    :target: https://travis-ci.org/PetrDlouhy/django-payments-payu

.. image:: https://codecov.io/gh/PetrDlouhy/django-payments-payu/branch/master/graph/badge.svg
    :target: https://codecov.io/gh/PetrDlouhy/django-payments-payu


NOTE: This project is still in development, so use with extreme caution.

PayU payments provider for django-payments. Uses the new PayU REST API. Supports normal, express and recurring payments.

Documentation
-------------

The full documentation is at https://django-payments-payu.readthedocs.io.

Quickstart
----------

Install `django-payments <https://github.com/mirumee/django-payments>`_ and set up PayU payment provider backend according to `django-payments documentation <https://django-payments.readthedocs.io/en/latest/modules.html>`_:

.. class:: payments_payu.provider.PayuProvider(client_secret, second_key, pos_id, get_refund_description, [sandbox=False, endpoint="https://secure.payu.com/", recurring_payments=False, express_payments=False, widget_branding=False, get_refund_ext_id=_DEFAULT_GET_REFUND_EXT_ID])

   This backend implements payments using `PayU.com <https://payu.com>`_.

Set up the payment provider:

Example::

      # use sandbox
      PAYMENT_VARIANTS = {
          'payu': ('payments_payu.provider.PayuProvider', {
              'pos_id': '123456',
              'second_key': 'iseedeadpeople',
              'client_secret': 'peopleiseedead',
              'sandbox': True,
              'capture': False,
              'get_refund_description': lambda payment, amount: 'My refund',
              'get_refund_ext_id': lambda payment, amount: str(uuid.uuid4()),
          }),
      }

Here are valid parameters for the provider:
   :client_secret:          PayU OAuth protocol client secret
   :pos_id:                 PayU POS ID
   :second_key:             PayU second key (MD5)
   :shop_name:              Name of the shop send to the API
   :sandbox:                if ``True``, set the endpoint to sandbox
   :endpoint:               endpoint URL, if not set, the will be automatically set based on `sandbox` settings
   :recurring_payments:     enable recurring payments, only valid with ``express_payments=True``, see bellow for additional setup, that is needed
   :express_payments:       use PayU express form
   :widget_branding:        tell express form to show PayU branding
   :store_card:             (default: False) whether PayU should store the card
   :get_refund_description: An optional callable that is called with two keyword arguments `payment` and `amount` in order to get the string description of the particular refund whenever ``provider.refund(payment, amount)`` is called. The callable is optional because of backwards compatibility. However, if it is not set, an attempt to refund raises an exception. A default value of `get_refund_description` is deprecated.
   :get_refund_ext_id:      An optional callable that is called with two keyword arguments `payment` and `amount` in order to get the External string refund ID of the particular refund whenever ``provider.refund(payment, amount)`` is called. If ``None`` is returned, no External refund ID is set. An External refund ID is not necessary if partial refunds won't be performed more than once per second. Otherwise, a unique ID is recommended since `PayuProvider.refund` is idempotent and if exactly same data will be provided, it will return the result of the already previously performed refund instead of performing a new refund. Defaults to a random UUID version 4 in the standard form.


   NOTE: notifications about the payment status from PayU are requested to be sent to `django-payments` `process_payment` url. The request from PayU can fail for several reasons (i.e. it can be blocked by proxy). Use "Show reports" page in PayU administration to get more information about the requests.


**Recurring payments**:
   If recurring payments are enabled, the PayU card token needs to be stored in your application for usage in next payments. The next payments can be either initiated by user through (user will be prompted only for payment confirmation by the express form) or by server.
   To enable recurring payments, you will need to set additional things:

   NOTE: Recurring payments are not enabled by default even in Sandbox, you sould consult their helpdesk to enable this.

   * In order to make payments recurring, the card token needs to be stored for the ``Payment``'s user (not just the payment itself). Implement the ``Payment.set_renew_token()`` and ``Payment.get_renew_token()``.
   * Implement ``Payment.get_payment_url()``.
   * For the server initiated recurring payments you will need to create the new payment and then call ``payment.auto_complete_recurring()``.
      * The method returns either string 'success' or url where the user can provide his CVV2 or 3D secure information.
      * The ``'success'`` string means, that the payment is waiting for notification from PayU, but no further user action is required.


Example of triggering recurring payment::

       payment = Payment.objects.create(...)
       redirect_url = payment.auto_complete_recurring()
       if redirect_url != 'success':
           send_mail(
               'Recurring payment - action required',
               'Please renew your CVV2/3DS at %s' % redirect_url,
               'noreply@test.com',
               [user.email],
               fail_silently=False,
           )

Running Tests
-------------

Does the code actually work?

::

    source <YOURVIRTUALENV>/bin/activate
    (myenv) $ pip install tox
    (myenv) $ tox

Credits
-------

Tools used in rendering this package:

*  Cookiecutter_
*  `cookiecutter-djangopackage`_

.. _Cookiecutter: https://github.com/audreyr/cookiecutter
.. _`cookiecutter-djangopackage`: https://github.com/pydanny/cookiecutter-djangopackage
