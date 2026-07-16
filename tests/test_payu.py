from __future__ import unicode_literals

import base64
import contextlib
import json
import warnings
from django.template import Context, Template
from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

from mock import MagicMock, Mock, patch
from payments import FraudStatus, PaymentStatus, PurchasedItem, RedirectNeeded

from payments_payu.provider import PayuApiError, PayuProvider

SECRET = "123abc"
SECOND_KEY = "123abc"
POS_ID = "123abc"
VARIANT = "wallet"

PROCESS_DATA = {
    "name": "John Doe",
    "number": "371449635398431",
    "expiration_0": "5",
    "expiration_1": "2020",
    "cvv2": "1234",
}


class JSONEquals(str):
    def __init__(self, json):
        self.json = json

    def __eq__(self, other):
        return self.json == json.loads(other)


class PaymentQuerySet(Mock):
    __payments = {}

    def create(self, **kwargs):
        if kwargs:
            raise NotImplementedError(f"arguments not supported yet: {kwargs}")
        id_ = max(self.__payments) + 1 if self.__payments else 1
        self.__payments[id_] = {}
        payment = Payment()
        payment.id = id_
        payment.save()
        return payment

    def get(self, *args, **kwargs):
        if args or kwargs:
            return self.filter(*args, **kwargs).get()
        payment = Payment()
        (payment_fields,) = self.__payments.values()
        for payment_field_name, payment_field_value in payment_fields.items():
            setattr(payment, payment_field_name, deepcopy(payment_field_value))
        return payment

    def filter(self, *args, pk=None, **kwargs):
        if args or kwargs:
            raise NotImplementedError(f"arguments not supported yet: {args}, {kwargs}")
        if pk is not None:
            return PaymentQuerySet(
                {pk_: payment for pk_, payment in self.__payments.items() if pk_ == pk}
            )
        return self

    def update(self, **kwargs):
        for payment in self.__payments.values():
            for field_name, field_value in kwargs.items():
                if not any(
                    field.name == field_name
                    for field in Payment._meta.get_fields(
                        include_parents=True, include_hidden=True
                    )
                ):
                    raise NotImplementedError(
                        f"updating unknown field not supported yet: {field_name}"
                    )
                payment[field_name] = deepcopy(field_value)

    def delete(self):
        self.__payments.clear()


class Payment(Mock):
    UNSET = object()

    objects = PaymentQuerySet()

    id = 1
    description = "payment"
    currency = "USD"
    delivery = Decimal(10)
    status = PaymentStatus.WAITING
    fraud_status = FraudStatus.UNKNOWN
    tax = Decimal(10)
    total = Decimal(220)
    billing_first_name = "Foo"
    billing_last_name = "Bar"
    billing_email = "foo@bar.com"
    captured_amount = Decimal(0)
    variant = VARIANT
    transaction_id = None
    message = ""
    customer_ip_address = "123"
    token = "bar_token"
    extra_data = json.dumps(
        {
            "links": {
                "approval_url": None,
                "capture": {"href": "http://capture.com"},
                "refund": {"href": "http://refund.com"},
                "execute": {"href": "http://execute.com"},
            }
        }
    )

    @property
    def pk(self):
        return self.id

    def change_fraud_status(self, status, message="", commit=True):
        self.fraud_status = status
        self.message = message
        if commit:
            self.save()

    def change_status(self, status, message=""):
        self.status = status
        self.message = message
        self.save(update_fields=["status", "message"])

    def get_failure_url(self):
        return "http://cancel.com"

    def get_process_url(self):
        return "/process_url/token"

    def get_payment_url(self):
        return "/payment/token"

    def get_purchased_items(self):
        return [
            PurchasedItem(
                name="foo", quantity=10, price=Decimal("20"), currency="USD", sku="bar"
            )
        ]

    def get_success_url(self):
        return "http://foo_succ.com"

    def get_renew_token(self):
        return self.token

    def set_renew_token(
        self,
        token,
        card_expire_year=None,
        card_expire_month=None,
        card_masked_number=None,
        automatic_renewal=UNSET,
        renewal_triggered_by=UNSET,
    ):
        self.token = token
        self.card_expire_year = card_expire_year
        self.card_expire_month = card_expire_month
        self.card_masked_number = card_masked_number
        self.automatic_renewal = automatic_renewal
        self.renewal_triggered_by = renewal_triggered_by

    def save(self, *args, update_fields=None, **kwargs):
        if args or kwargs:
            raise NotImplementedError(f"arguments not supported yet: {args}, {kwargs}")
        if update_fields is None:
            update_fields = {
                field.name
                for field in self._meta.get_fields(
                    include_parents=True, include_hidden=True
                )
            }
        Payment.objects.filter(pk=self.pk).update(
            **{field: getattr(self, field) for field in update_fields}
        )

    def refresh_from_db(self, *args, fields=None, **kwargs):
        if args or kwargs:
            raise NotImplementedError(f"arguments not supported yet: {args}, {kwargs}")
        payment_from_db = Payment.objects.get(pk=self.pk)
        refresh_fields = self._meta.get_fields(
            include_parents=True, include_hidden=True
        )
        if fields is not None:
            refresh_fields = [field for field in refresh_fields if field.name in fields]
        for field in refresh_fields:
            field_value_from_db = getattr(payment_from_db, field.name)
            setattr(self, field.name, field_value_from_db)

    class Meta(Mock):
        def get_fields(self, include_parents=True, include_hidden=False):
            fields = []
            for field_name in {
                "id",
                "description",
                "currency",
                "delivery",
                "status",
                "fraud_status",
                "tax",
                "total",
                "billing_first_name",
                "billing_last_name",
                "billing_email",
                "captured_amount",
                "variant",
                "transaction_id",
                "message",
                "customer_ip_address",
                "token",
                "extra_data",
            }:
                field = Mock()
                field.name = field_name
                fields.append(field)
            return tuple(fields)

    _meta = Meta()


class TestPayuProvider(TestCase):
    urls = "myapp.test_urls"

    def setUp(self):
        Payment.objects.delete()
        self.payment = Payment.objects.create()

    def set_up_provider(self, recurring, express, **kwargs):
        with patch("requests.post") as mocked_post:
            data = MagicMock()
            data = '{"access_token": "test_access_token"}'
            json.loads(data)
            post = MagicMock()
            post.text = data
            post.status_code = 200
            mocked_post.return_value = post
            self.provider = PayuProvider(
                client_secret=SECRET,
                second_key=SECOND_KEY,
                pos_id=POS_ID,
                base_payu_url="http://mock.url/",
                recurring_payments=recurring,
                express_payments=express,
                **kwargs,
            )

    def test_redirect_to_recurring_payment(self):
        """Test that if the payment recurrence is set, the user is redirected to renew payment form"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        form = self.provider.get_form(payment=self.payment)
        self.assertEqual(form.__class__.__name__, "RenewPaymentForm")
        self.assertEqual(form.action, "https://example.com/process_url/token")
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_redirect_payu(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"redirectUri": "test_redirect_uri", "status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "test_redirect_uri")

    def test_redirect_payu_store_token(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "test_redirect_uri",
                    "status": {"statusCode": "SUCCESS"},
                    "orderId": 123,
                    "payMethods": {
                        "payMethod": {
                            "value": 1211,
                            "card": {
                                "expirationYear": 2021,
                                "expirationMonth": 1,
                                "number": "1234xxx",
                            },
                        }
                    },
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "test_redirect_uri")
            self.assertEqual(self.payment.token, 1211)
            self.assertEqual(self.payment.card_expire_year, 2021)
            self.assertEqual(self.payment.card_expire_month, 1)
            self.assertEqual(self.payment.card_masked_number, "1234xxx")
            self.assertEqual(self.payment.automatic_renewal, Payment.UNSET)
            self.assertEqual(self.payment.renewal_triggered_by, "task")

    def test_redirect_payu_unknown_status(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post_text = {
                "redirectUri": "test_redirect_uri",
                "status": {"statusCode": "FOO", "codeLiteral": "Foo code"},
                "orderId": 123,
            }
            post.text = json.dumps(post_text)
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "http://cancel.com")

            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "buyer": {
                            "email": "foo@bar.com",
                            "language": "en",
                            "lastName": "Bar",
                            "firstName": "Foo",
                            "phone": None,
                        },
                        "description": "payment",
                        "totalAmount": 22000,
                        "merchantPosId": "123abc",
                        "customerIp": "123",
                        "notifyUrl": "https://example.com/process_url/token",
                        "extOrderId": "bar_token",
                        "products": [
                            {
                                "currency": "USD",
                                "name": "foo",
                                "quantity": 10,
                                "unitPrice": 2000,
                                "subUnit": 100,
                            }
                        ],
                        "continueUrl": "http://foo_succ.com",
                        "currencyCode": "USD",
                    },
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )

    def test_redirect_payu_bussiness_error(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post_text = {
                "redirectUri": "test_redirect_uri",
                "status": {"statusCode": "BUSINESS_ERROR", "codeLiteral": "Foo code"},
                "orderId": 123,
            }
            post.text = json.dumps(post_text)
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "http://cancel.com")
            self.assertEqual(self.payment.fraud_status, FraudStatus.REJECT)

    def test_redirect_payu_duplicate_order(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.status = PaymentStatus.CONFIRMED
        self.payment.save()
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post_text = {
                "redirectUri": "test_redirect_uri",
                "status": {
                    "statusCode": "ERROR_ORDER_NOT_UNIQUE",
                    "codeLiteral": "Foo code",
                },
                "orderId": 123,
            }
            post.text = json.dumps(post_text)
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "")
            self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)

    def test_redirect_payu_confirmed_payment_other_error(self):
        """A duplicate create_order on a CONFIRMED payment must not demote it.

        PayU may reject a re-submitted order with any of several status codes
        (single-use widget token expired, antifraud, generic BUSINESS_ERROR,
        plain ERROR, ...) - not just ERROR_ORDER_NOT_UNIQUE. Whatever the
        rejection reason, if our payment is already CONFIRMED (typically via
        an asynchronous PayU webhook), we must keep the CONFIRMED state.
        """
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.status = PaymentStatus.CONFIRMED
        self.payment.save()
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post_text = {
                "redirectUri": "test_redirect_uri",
                "status": {
                    "statusCode": "ERROR_VALUE_INVALID",
                    "codeLiteral": "Foo code",
                },
                "orderId": 123,
            }
            post.text = json.dumps(post_text)
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "")
            self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)

    def test_redirect_payu_confirmed_payment_business_error(self):
        """A BUSINESS_ERROR on a CONFIRMED payment must not demote it.

        BUSINESS_ERROR has a side effect of setting fraud_status=REJECT, but
        that must not also flip a CONFIRMED payment to ERROR.
        """
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.status = PaymentStatus.CONFIRMED
        self.payment.save()
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post_text = {
                "redirectUri": "test_redirect_uri",
                "status": {"statusCode": "BUSINESS_ERROR", "codeLiteral": "Foo code"},
                "orderId": 123,
            }
            post.text = json.dumps(post_text)
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "")
            self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
            self.assertEqual(self.payment.fraud_status, FraudStatus.REJECT)

    def test_redirect_payu_confirmed_payment_no_status_in_response(self):
        """Even a malformed PayU response must not demote a CONFIRMED payment."""
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.status = PaymentStatus.CONFIRMED
        self.payment.save()
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps({"redirectUri": "test_redirect_uri", "orderId": 123})
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "")
            self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)

    def test_redirect_payu_no_status_code(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post_text = {
                "redirectUri": "test_redirect_uri",
                "orderId": 123,
            }
            post.text = json.dumps(post_text)
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "http://cancel.com")

            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "buyer": {
                            "email": "foo@bar.com",
                            "language": "en",
                            "lastName": "Bar",
                            "firstName": "Foo",
                            "phone": None,
                        },
                        "description": "payment",
                        "totalAmount": 22000,
                        "merchantPosId": "123abc",
                        "customerIp": "123",
                        "notifyUrl": "https://example.com/process_url/token",
                        "extOrderId": "bar_token",
                        "products": [
                            {
                                "currency": "USD",
                                "name": "foo",
                                "quantity": 10,
                                "unitPrice": 2000,
                                "subUnit": 100,
                            }
                        ],
                        "continueUrl": "http://foo_succ.com",
                        "currencyCode": "USD",
                    },
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )

    def test_redirect_payu_unauthorized_status(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "test_redirect_uri",
                    "status": {"statusCode": "UNAUTHORIZED"},
                    "orderId": 123,
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(PayuApiError) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(
                context.exception.args[0],
                "Unable to regain authorization token "
                "{'redirectUri': 'test_redirect_uri', 'status': {'statusCode': 'UNAUTHORIZED'}, 'orderId': 123}",
            )

            mocked_post.assert_called_with(
                "http://mock.url/pl/standard/user/oauth/authorize",
                data={
                    "grant_type": "client_credentials",
                    "client_id": "123abc",
                    "client_secret": "123abc",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    def test_redirect_payu_unauthorized_error(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )

        with patch("requests.post") as mocked_post:
            mocked_post.return_value = MagicMock(
                status_code=401,
                text='{"error": "invalid_token", "error_description": "Access token expired"}',
            )

            with self.assertRaisesRegex(
                PayuApiError,
                r"^Unable to regain authorization token "
                r"\{'error': 'invalid_token', 'error_description': 'Access token expired'}$",
            ):
                self.provider.get_form(payment=self.payment)

        mocked_post.assert_called_with(
            "http://mock.url/pl/standard/user/oauth/authorize",
            data={
                "grant_type": "client_credentials",
                "client_id": "123abc",
                "client_secret": "123abc",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_redirect_payu_integer_status_error(self):
        """PayU gateway-level failures answer with a plain integer status
        ('{"status": 500}') instead of the usual status dict. That must fail
        the payment gracefully (ERROR + redirect to the failure page), not
        crash with TypeError: argument of type 'int' is not iterable."""
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": 500, "error_description": "Internal Server Error"}'
            post.status_code = 500
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "http://cancel.com")
            self.assertEqual(self.payment.status, PaymentStatus.ERROR)

    def test_redirect_payu_error_without_code_literal(self):
        """Error statuses are not guaranteed to carry codeLiteral; logging the
        failure must not die with KeyError inside the except block."""
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "status": {
                        "statusCode": "ERROR_VALUE_INVALID",
                        "statusDesc": "Order fields are invalid",
                    }
                }
            )
            post.status_code = 400
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            self.assertEqual(context.exception.args[0], "http://cancel.com")
            self.assertEqual(self.payment.status, PaymentStatus.ERROR)

    def test_post_request_non_json_response_raises_payu_api_error(self):
        """A gateway HTML error page (502 from a proxy) is not JSON; surface
        it as PayuApiError instead of a raw JSONDecodeError."""
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = "<html><body>502 Bad Gateway</body></html>"
            post.status_code = 502
            mocked_post.return_value = post
            with self.assertRaisesRegex(
                PayuApiError, r"Non-JSON response from PayU: HTTP 502"
            ):
                self.provider.get_form(payment=self.payment)

    def test_get_access_token_trusted_merchant(self):
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "test_redirect_uri",
                    "token_type": "test_token_type",
                    "access_token": "test_access_token",
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            token = self.provider.get_access_token(
                "123abc", "123abc", "trusted_merchant", "foo@bar.com", 123
            )
            self.assertEqual(token, "test_access_token")

            mocked_post.assert_called_with(
                "http://mock.url/pl/standard/user/oauth/authorize",
                data={
                    "grant_type": "trusted_merchant",
                    "client_id": "123abc",
                    "client_secret": "123abc",
                    "email": "foo@bar.com",
                    "ext_customer_id": 123,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    def test_redirect_cvv_form(self):
        """Test redirection to CVV form if requested by PayU"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "test_redirect_uri",
                    "status": {"statusCode": "WARNING_CONTINUE_CVV"},
                    "orderId": 123,
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            redirect = self.provider.process_data(payment=self.payment, request=post)
            self.assertEqual(redirect.__class__.__name__, "HttpResponseRedirect")
            self.assertEqual(redirect.url, "/payment/token")

            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "products": [
                            {
                                "currency": "USD",
                                "quantity": 10,
                                "name": "foo",
                                "unitPrice": 2000,
                                "subUnit": 100,
                            }
                        ],
                        "extOrderId": "bar_token",
                        "buyer": {
                            "phone": None,
                            "email": "foo@bar.com",
                            "lastName": "Bar",
                            "language": "en",
                            "firstName": "Foo",
                        },
                        "merchantPosId": "123abc",
                        "notifyUrl": "https://example.com/process_url/token",
                        "payMethods": {
                            "payMethod": {"value": "bar_token", "type": "CARD_TOKEN"}
                        },
                        "totalAmount": 22000,
                        "continueUrl": "http://foo_succ.com",
                        "customerIp": "123",
                        "description": "payment",
                        "recurring": "STANDARD",
                        "currencyCode": "USD",
                    },
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )

    def test_showing_cvv_form(self):
        """Test redirection to CVV form if requested by PayU"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            get_buyer_language=lambda payment: "cs",
        )
        self.payment.extra_data = json.dumps({"cvv_url": "foo_url"})
        self.provider.payu_shop_name = "<script> alert('foo')</script>"  # XSS test
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "http://test_redirect_uri.com/",
                    "status": {"statusCode": "SUCCESS"},
                    "orderId": 123,
                },
            )
            post.status_code = 200
            mocked_post.return_value = post
            form = self.provider.get_form(payment=self.payment)
            self.assertEqual(form.__class__.__name__, "WidgetPaymentForm")

            template = Template("{{form.as_p}}")
            rendered_html = template.render(Context({"form": form}))
            self.assertIn("payu-widget", rendered_html)
            self.assertIn("https://example.com/process_url/token", rendered_html)
            self.assertIn("cvv-url='foo_url'", rendered_html)
            self.assertIn(
                "shop-name='&lt;script&gt; alert(&#x27;foo&#x27;)&lt;/script&gt;'",
                rendered_html,
            )
            self.assertIn(
                "</script>", rendered_html
            )  # Test, that escaping works correctly
            self.assertIn("customer-language='cs'", rendered_html)

    def test_payment_error_form_html_not_escaped(self):
        """Test that the payment error form renders its html unescaped.

        HtmlOutputField.render must return a safe string: a plain str gets
        autoescaped when the form is rendered in a template on Django 5.x,
        showing literal "<br/><strong>..." to the user.
        """
        from django.utils.safestring import SafeString

        from payments_payu.provider import PaymentErrorForm

        form = PaymentErrorForm()
        rendered_widget = form.fields["script"].widget.render("script", None)
        self.assertIsInstance(rendered_widget, SafeString)

        template = Template("{{form.as_p}}")
        rendered_html = template.render(Context({"form": form}))
        self.assertIn("<strong>This payment is already being processed.", rendered_html)
        self.assertNotIn("&lt;strong&gt;", rendered_html)

    def test_redirect_3ds_form(self):
        """Test redirection to 3DS page if requested by PayU"""
        self.set_up_provider(
            True, False, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "test_redirect_uri",
                    "status": {"statusCode": "WARNING_CONTINUE_3DS"},
                    "orderId": 123,
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            with self.assertRaises(RedirectNeeded) as context:
                self.provider.get_form(payment=self.payment)
            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "merchantPosId": "123abc",
                        "continueUrl": "http://foo_succ.com",
                        "buyer": {
                            "lastName": "Bar",
                            "phone": None,
                            "email": "foo@bar.com",
                            "firstName": "Foo",
                            "language": "en",
                        },
                        "description": "payment",
                        "notifyUrl": "https://example.com/process_url/token",
                        "totalAmount": 22000,
                        "currencyCode": "USD",
                        "extOrderId": "bar_token",
                        "products": [
                            {
                                "name": "foo",
                                "quantity": 10,
                                "subUnit": 100,
                                "currency": "USD",
                                "unitPrice": 2000,
                            }
                        ],
                        "customerIp": "123",
                    },
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(context.exception.args[0], "test_redirect_uri")

    def test_payu_renew_form(self):
        """Test showing PayU card form"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        transaction_id = "1234"
        data = MagicMock()
        data.return_value = {
            "id": transaction_id,
            "token_type": "test_token_type",
            "access_token": "test_access_token",
            "links": [{"rel": "approval_url", "href": "http://approval_url.com"}],
        }
        post = MagicMock()
        post.json = data
        post.status_code = 200
        form = self.provider.get_form(payment=self.payment)
        self.assertEqual(form.__class__.__name__, "RenewPaymentForm")
        self.assertEqual(form.action, "https://example.com/process_url/token")
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_payu_widget_form(self):
        """Test showing PayU card widget"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.token = None
        transaction_id = "1234"
        data = MagicMock()
        data.return_value = {
            "id": transaction_id,
            "token_type": "test_token_type",
            "access_token": "test_access_token",
            "links": [{"rel": "approval_url", "href": "http://approval_url.com"}],
        }
        post = MagicMock()
        post.json = data
        post.status_code = 200
        form = self.provider.get_form(payment=self.payment)
        self.assertEqual(form.__class__.__name__, "WidgetPaymentForm")
        self.assertTrue("payu-widget" in form.fields["script"].widget.render("a", "b"))
        self.assertTrue(
            "https://example.com/process_url/token"
            in form.fields["script"].widget.render("a", "b")
        )
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.assertNotIn(
            "google-pay-button", form.fields["script"].widget.render("a", "b")
        )

    def test_payu_widget_form_google_pay(self):
        """Test that the Google Pay button is rendered in the express form"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={
                "merchant_id": "test_merchant_id",
                "merchant_name": "Test shop",
            },
        )
        self.payment.token = None
        form = self.provider.get_form(payment=self.payment)
        self.assertEqual(form.__class__.__name__, "WidgetPaymentForm")
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn("payu-widget", html)
        self.assertIn("google-pay-button", html)
        self.assertIn("https://pay.google.com/gp/p/js/pay.js", html)
        self.assertIn('"environment": "PRODUCTION"', html)
        self.assertIn('"gateway_merchant_id": "123abc"', html)
        self.assertIn('"merchantId": "test_merchant_id"', html)
        self.assertIn('"merchantName": "Test shop"', html)
        self.assertIn('"total": "220.00"', html)
        self.assertIn('"currency": "USD"', html)
        self.assertIn("https://example.com/process_url/token", html)

    def test_payu_widget_form_google_pay_button_radius(self):
        """Test that a configured button radius reaches the Google Pay config"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id", "button_radius": 22},
        )
        self.payment.token = None
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn('"button_radius": 22', html)

    def test_payu_widget_form_google_pay_button_color(self):
        """Test that a configured button color reaches the Google Pay config

        Google's brand guidelines require the white button on dark backgrounds.
        """
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id", "button_color": "white"},
        )
        self.payment.token = None
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn('"button_color": "white"', html)
        self.assertIn("buttonOptions.buttonColor = cfg.button_color", html)

    def test_payu_widget_form_google_pay_reinit_guards(self):
        """Test the re-injection guards: skip double init, init without onload"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
        )
        self.payment.token = None
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn("container.hasChildNodes()", html)
        self.assertIn("window.google && window.google.payments", html)
        self.assertIn('"button_radius": null', html)
        self.assertIn('"button_color": null', html)

    def test_payu_widget_form_google_pay_sandbox(self):
        """Test that the Google Pay button uses TEST environment on sandbox"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
            sandbox=True,
        )
        self.payment.token = None
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn('"environment": "TEST"', html)

    def test_payu_widget_form_google_pay_not_on_cvv(self):
        """Test that the Google Pay button is not rendered on the CVV form"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
        )
        self.payment.token = None
        self.payment.extra_data = json.dumps({"cvv_url": "http://cvv.url"})
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn("payu-widget", html)
        self.assertNotIn("google-pay-button", html)

    def test_process_google_pay(self):
        """Test processing a Google Pay token callback with recurring FIRST"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
        )
        self.payment.token = None
        google_pay_token = '{"signature": "foo", "signedMessage": "bar"}'
        mocked_request = MagicMock()
        mocked_request.POST = {"google_pay_token": google_pay_token}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            response = self.provider.process_data(
                payment=self.payment, request=mocked_request
            )
            self.assertEqual(response.__class__.__name__, "HttpResponse")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"http://foo_succ.com")
            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "recurring": "FIRST",
                        "customerIp": "123",
                        "totalAmount": 22000,
                        "description": "payment",
                        "extOrderId": None,
                        "products": [
                            {
                                "name": "foo",
                                "subUnit": 100,
                                "currency": "USD",
                                "unitPrice": 2000,
                                "quantity": 10,
                            }
                        ],
                        "continueUrl": "http://foo_succ.com",
                        "merchantPosId": "123abc",
                        "currencyCode": "USD",
                        "payMethods": {
                            "payMethod": {
                                "type": "PBL",
                                "value": "ap",
                                "authorizationCode": base64.b64encode(
                                    google_pay_token.encode("utf-8")
                                ).decode("ascii"),
                            }
                        },
                        "buyer": {
                            "firstName": "Foo",
                            "email": "foo@bar.com",
                            "language": "en",
                            "phone": None,
                            "lastName": "Bar",
                        },
                        "notifyUrl": "https://example.com/process_url/token",
                    }
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_process_google_pay_non_recurring(self):
        """Test that a one-off Google Pay order is sent without recurring"""
        self.set_up_provider(
            False,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
        )
        self.payment.token = None
        google_pay_token = '{"signature": "foo"}'
        mocked_request = MagicMock()
        mocked_request.POST = {"google_pay_token": google_pay_token}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            response = self.provider.process_data(
                payment=self.payment, request=mocked_request
            )
            self.assertEqual(response.status_code, 200)
            sent_data = json.loads(mocked_post.call_args[1]["data"])
            self.assertNotIn("recurring", sent_data)
            self.assertEqual(
                sent_data["payMethods"]["payMethod"],
                {
                    "type": "PBL",
                    "value": "ap",
                    "authorizationCode": base64.b64encode(
                        google_pay_token.encode("utf-8")
                    ).decode("ascii"),
                },
            )

    def test_process_google_pay_3ds(self):
        """Test that a Google Pay order requiring 3DS returns the redirect URL"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
        )
        self.payment.token = None
        mocked_request = MagicMock()
        mocked_request.POST = {"google_pay_token": '{"signature": "foo"}'}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "test_redirect_uri",
                    "status": {"statusCode": "WARNING_CONTINUE_3DS"},
                    "orderId": 123,
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            response = self.provider.process_data(
                payment=self.payment, request=mocked_request
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"test_redirect_uri")
        self.assertEqual(
            json.loads(self.payment.extra_data)["3ds_url"], "test_redirect_uri"
        )

    def test_process_google_pay_stores_multi_use_token(self):
        """Test that a multi-use card token from a Google Pay order is stored"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
        )
        self.payment.token = None
        mocked_request = MagicMock()
        mocked_request.POST = {"google_pay_token": '{"signature": "foo"}'}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "status": {"statusCode": "SUCCESS"},
                    "orderId": 123,
                    "payMethods": {
                        "payMethod": {
                            "value": "TOKC_MULTI_USE",
                            "card": {
                                "expirationYear": 2031,
                                "expirationMonth": 6,
                                "number": "1234xxx",
                            },
                        }
                    },
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            self.provider.process_data(payment=self.payment, request=mocked_request)
        self.assertEqual(self.payment.token, "TOKC_MULTI_USE")
        self.assertEqual(self.payment.card_expire_year, 2031)
        self.assertEqual(self.payment.card_expire_month, 6)
        self.assertEqual(self.payment.card_masked_number, "1234xxx")
        self.assertEqual(self.payment.renewal_triggered_by, "task")

    def test_process_google_pay_paymethod_echo_not_stored(self):
        """Test that a pay method echo without card data is not stored as a renew token"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
        )
        self.payment.token = None
        mocked_request = MagicMock()
        mocked_request.POST = {"google_pay_token": '{"signature": "foo"}'}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "status": {"statusCode": "SUCCESS"},
                    "orderId": 123,
                    "payMethods": {"payMethod": {"value": "ap", "type": "PBL"}},
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            self.provider.process_data(payment=self.payment, request=mocked_request)
        self.assertIsNone(self.payment.token)

    def test_payu_widget_form_apple_pay(self):
        """Test that the Apple Pay button is rendered in the express form"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            apple_pay={
                "merchant_id": "merchant.com.test",
                "merchant_name": "Test shop",
                "country_code": "CZ",
            },
        )
        self.payment.token = None
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn("payu-widget", html)
        self.assertIn("apple-pay-button", html)
        self.assertIn("ApplePaySession", html)
        self.assertIn('"country_code": "CZ"', html)
        self.assertIn('"label": "Test shop"', html)
        self.assertIn('"total": "220.00"', html)
        self.assertIn('"currency": "USD"', html)
        self.assertIn("https://example.com/process_url/token", html)
        # PayU decrypts token.paymentData (its data/signature/header/version),
        # not the whole ApplePayPaymentToken wrapper.
        self.assertIn("JSON.stringify(event.payment.token.paymentData)", html)

    def test_payu_widget_form_apple_pay_not_on_cvv(self):
        """Test that the Apple Pay button is not rendered on the CVV form"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            apple_pay={"merchant_id": "merchant.com.test"},
        )
        self.payment.token = None
        self.payment.extra_data = json.dumps({"cvv_url": "http://cvv.url"})
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn("payu-widget", html)
        self.assertNotIn("apple-pay-button", html)

    def test_apple_pay_merchant_validation(self):
        """Test that merchant validation POSTs to Apple with the identity cert"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            apple_pay={
                "merchant_id": "merchant.com.test",
                "merchant_name": "Test shop",
                "certificate": ("/tmp/cert.pem", "/tmp/key.pem"),
            },
        )
        mocked_request = MagicMock()
        mocked_request.POST = {
            "apple_pay_validation_url": "https://apple.example/validate"
        }
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.json.return_value = {"merchantSessionIdentifier": "abc"}
            mocked_post.return_value = post
            response = self.provider.process_data(
                payment=self.payment, request=mocked_request
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/json")
            self.assertEqual(
                json.loads(response.content), {"merchantSessionIdentifier": "abc"}
            )
            mocked_post.assert_called_once_with(
                "https://apple.example/validate",
                json={
                    "merchantIdentifier": "merchant.com.test",
                    "displayName": "Test shop",
                    "initiative": "web",
                    "initiativeContext": "example.com",
                },
                cert=("/tmp/cert.pem", "/tmp/key.pem"),
            )

    def test_process_apple_pay(self):
        """Test processing an Apple Pay token callback with recurring FIRST"""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            apple_pay={"merchant_id": "merchant.com.test"},
        )
        self.payment.token = None
        apple_pay_token = '{"paymentData": {"data": "encrypted"}}'
        mocked_request = MagicMock()
        mocked_request.POST = {"apple_pay_token": apple_pay_token}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            response = self.provider.process_data(
                payment=self.payment, request=mocked_request
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"http://foo_succ.com")
            sent_data = json.loads(mocked_post.call_args[1]["data"])
            self.assertEqual(sent_data["recurring"], "FIRST")
            self.assertEqual(
                sent_data["payMethods"]["payMethod"],
                {
                    "type": "PBL",
                    "value": "jp",
                    "authorizationCode": base64.b64encode(
                        apple_pay_token.encode("utf-8")
                    ).decode("ascii"),
                },
            )

    def test_process_apple_pay_non_recurring(self):
        """Test that a one-off Apple Pay order is sent without recurring"""
        self.set_up_provider(
            False,
            True,
            get_refund_description=lambda payment, amount: "test",
            apple_pay={"merchant_id": "merchant.com.test"},
        )
        self.payment.token = None
        mocked_request = MagicMock()
        mocked_request.POST = {"apple_pay_token": '{"paymentData": {}}'}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            self.provider.process_data(payment=self.payment, request=mocked_request)
            sent_data = json.loads(mocked_post.call_args[1]["data"])
            self.assertNotIn("recurring", sent_data)
            self.assertEqual(sent_data["payMethods"]["payMethod"]["value"], "jp")

    def test_process_apple_pay_card_on_file(self):
        """With card_on_file, an Apple Pay order uses cardOnFile=FIRST, not recurring."""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            apple_pay={"merchant_id": "merchant.com.test"},
            card_on_file=True,
        )
        self.payment.token = None
        mocked_request = MagicMock()
        mocked_request.POST = {"apple_pay_token": '{"paymentData": {}}'}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            self.provider.process_data(payment=self.payment, request=mocked_request)
            sent_data = json.loads(mocked_post.call_args[1]["data"])
            self.assertEqual(sent_data["cardOnFile"], "FIRST")
            self.assertNotIn("recurring", sent_data)
            self.assertEqual(sent_data["payMethods"]["payMethod"]["value"], "jp")

    def test_process_google_pay_card_on_file(self):
        """With card_on_file, a Google Pay order uses cardOnFile=FIRST, not recurring."""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_id": "test_merchant_id"},
            card_on_file=True,
        )
        self.payment.token = None
        mocked_request = MagicMock()
        mocked_request.POST = {"google_pay_token": '{"signature": "foo"}'}
        mocked_request.META = {}
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            self.provider.process_data(payment=self.payment, request=mocked_request)
            sent_data = json.loads(mocked_post.call_args[1]["data"])
            self.assertEqual(sent_data["cardOnFile"], "FIRST")
            self.assertNotIn("recurring", sent_data)
            self.assertEqual(sent_data["payMethods"]["payMethod"]["value"], "ap")

    def test_payu_widget_form_google_pay_without_merchant_id(self):
        """Google Pay in TEST mode needs no merchant_id; merchantId is then omitted."""
        self.set_up_provider(
            True,
            True,
            get_refund_description=lambda payment, amount: "test",
            google_pay={"merchant_name": "Test shop"},
        )
        self.payment.token = None
        form = self.provider.get_form(payment=self.payment)
        html = form.fields["script"].widget.render("a", "b")
        self.assertIn("google-pay-button", html)
        self.assertIn('"merchantName": "Test shop"', html)
        self.assertNotIn("merchantId", html)

    def test_process_notification(self):
        """Test processing PayU notification"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        mocked_request = MagicMock()
        mocked_request.body = json.dumps({"order": {"status": "COMPLETED"}}).encode(
            "utf8"
        )
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=a12fbd21c48e69bedee18edf042b816c;algorithm=MD5",
        }
        mocked_request.status_code = 200
        ret_val = self.provider.process_data(
            payment=self.payment, request=mocked_request
        )
        self.assertEqual(ret_val.__class__.__name__, "HttpResponse")
        self.assertEqual(ret_val.status_code, 200)
        self.assertEqual(ret_val.content, b"ok")
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_process_notification_cancelled(self):
        """Test processing PayU cancelled notification"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.transaction_id = "123"
        self.payment.save()
        mocked_request = MagicMock()
        mocked_request.body = json.dumps(
            {
                "order": dict(
                    self.provider.get_processor(self.payment).as_json(),
                    orderId=self.payment.transaction_id,
                    orderCreateDate="2012-12-31T12:00:00",
                    status="CANCELED",
                )
            }
        ).encode("utf8")
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=f376048898aa0c629d1f64317ce13736;algorithm=MD5",
        }
        mocked_request.status_code = 200

        ret_val = self.provider.process_data(
            payment=self.payment, request=mocked_request
        )

        self.assertEqual(ret_val.__class__.__name__, "HttpResponse")
        self.assertEqual(ret_val.status_code, 200)
        self.assertEqual(ret_val.content, b"ok")
        self.assertEqual(self.payment.status, PaymentStatus.REJECTED)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REJECTED)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_process_notification_does_not_demote_confirmed(self):
        """A stale CANCELED notification must not demote a CONFIRMED payment.

        PayU can deliver duplicate/out-of-order callbacks; a CANCELED arriving
        after COMPLETED must not clobber a real capture. The notification is
        acked (200 "ok") so PayU stops retrying, but the status stays CONFIRMED.
        """
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.transaction_id = "123"
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        mocked_request = MagicMock()
        mocked_request.body = json.dumps(
            {
                "order": dict(
                    self.provider.get_processor(self.payment).as_json(),
                    orderId=self.payment.transaction_id,
                    orderCreateDate="2012-12-31T12:00:00",
                    status="CANCELED",
                )
            }
        ).encode("utf8")
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=f376048898aa0c629d1f64317ce13736;algorithm=MD5",
        }
        mocked_request.status_code = 200

        ret_val = self.provider.process_data(
            payment=self.payment, request=mocked_request
        )

        self.assertEqual(ret_val.__class__.__name__, "HttpResponse")
        self.assertEqual(ret_val.status_code, 200)
        self.assertEqual(ret_val.content, b"ok")
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)

    def test_process_notification_refund(self):
        """Test processing PayU refund notification"""
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()

        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        mocked_request = MagicMock()
        mocked_request.body = json.dumps(
            {
                "order": {"status": "COMPLETED"},
                "refund": {
                    "amount": "22000",
                    "currencyCode": "USD",
                    "status": "FINALIZED",
                    "reasonDescription": "BlenderKit refund",
                },
            }
        ).encode("utf8")
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=dd8cdddaa98438e7a76f5e830395d7e8;algorithm=MD5",
        }
        mocked_request.status_code = 200
        ret_val = self.provider.process_data(
            payment=self.payment, request=mocked_request
        )
        self.assertEqual(ret_val.__class__.__name__, "HttpResponse")
        self.assertEqual(ret_val.status_code, 200)
        self.assertEqual(ret_val.content, b"ok")
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))

    def test_process_notification_partial_refund(self):
        """Test processing PayU partial refund notification"""
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.total = 220
        self.payment.captured_amount = self.payment.total
        self.payment.save()
        self.payment.refresh_from_db()

        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        mocked_request = MagicMock()
        mocked_request.body = json.dumps(
            {
                "order": {"status": "COMPLETED"},
                "refund": {
                    "amount": "11000",
                    "currencyCode": "USD",
                    "status": "FINALIZED",
                    "reasonDescription": "BlenderKit refund",
                },
            }
        ).encode("utf8")
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=6f1076d9d2fa7dc58a87f20f2c69ebf8;algorithm=MD5",
        }
        mocked_request.status_code = 200
        ret_val = self.provider.process_data(
            payment=self.payment, request=mocked_request
        )
        self.assertEqual(ret_val.__class__.__name__, "HttpResponse")
        self.assertEqual(ret_val.status_code, 200)
        self.assertEqual(ret_val.content, b"ok")
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal("110"))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal("110"))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)

    def test_process_notification_refund_not_finalized(self):
        """Test processing PayU partial refund notification"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        mocked_request = MagicMock()
        mocked_request.body = json.dumps(
            {
                "order": {"status": "COMPLETED"},
                "refund": {
                    "amount": "11000",
                    "currencyCode": "USD",
                    "status": "FOO",
                    "reasonDescription": "BlenderKit refund",
                },
            }
        ).encode("utf8")
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=0af4d2830ed40ec2cea5249a172bf6d9;algorithm=MD5",
        }
        mocked_request.status_code = 200
        with self.assertRaisesRegex(Exception, "Refund was not finelized"):
            self.provider.process_data(payment=self.payment, request=mocked_request)

    def test_process_notification_total_amount(self):
        """Test processing PayU notification if it captures correct amount"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        mocked_request = MagicMock()
        mocked_request.body = json.dumps(
            {
                "order": {
                    "status": "COMPLETED",
                    "totalAmount": 200,
                    "currencyCode": "USD",
                }
            },
        ).encode("utf8")
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=01a0e768ab1f762da4b955585aa4e59e;algorithm=MD5",
        }
        mocked_request.status_code = 200
        ret_val = self.provider.process_data(
            payment=self.payment, request=mocked_request
        )
        self.assertEqual(ret_val.__class__.__name__, "HttpResponse")
        self.assertEqual(ret_val.status_code, 200)
        self.assertEqual(ret_val.content, b"ok")
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(self.payment.captured_amount, Decimal("2"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(self.payment.captured_amount, Decimal("2"))

    def test_process_notification_error(self):
        """Test processing PayU notification with wrong signature"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        mocked_request = MagicMock()
        mocked_request.body = b"{}"
        mocked_request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_OPENPAYU_SIGNATURE": "signature=foo;algorithm=MD5",
        }
        ret_val = self.provider.process_data(
            payment=self.payment, request=mocked_request
        )
        self.assertEqual(ret_val.__class__.__name__, "HttpResponse")
        self.assertEqual(ret_val.status_code, 500)
        self.assertEqual(ret_val.content, b"not ok")
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_process_notification_error_malformed_post(self):
        """Test processing PayU notification with malformed POST"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        mocked_request = MagicMock()
        mocked_request.body = b"{}"
        mocked_request.META = {"CONTENT_TYPE": "application/json"}
        with self.assertRaises(PayuApiError) as context:
            self.provider.process_data(payment=self.payment, request=mocked_request)
        self.assertEqual(context.exception.args[0], "Malformed POST")

    def test_process_first_renew(self):
        """Test processing first renew"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.token = None
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.POST = {"value": "renew_token"}
            mocked_post.return_value = post
            response = self.provider.process_data(
                payment=self.payment, request=mocked_post
            )
            self.assertEqual(response.__class__.__name__, "HttpResponse")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"http://foo_succ.com")
            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "recurring": "FIRST",
                        "customerIp": "123",
                        "totalAmount": 22000,
                        "description": "payment",
                        "extOrderId": None,
                        "products": [
                            {
                                "name": "foo",
                                "subUnit": 100,
                                "currency": "USD",
                                "unitPrice": 2000,
                                "quantity": 10,
                            }
                        ],
                        "continueUrl": "http://foo_succ.com",
                        "merchantPosId": "123abc",
                        "currencyCode": "USD",
                        "payMethods": {
                            "payMethod": {"value": "renew_token", "type": "CARD_TOKEN"}
                        },
                        "buyer": {
                            "firstName": "Foo",
                            "email": "foo@bar.com",
                            "language": "en",
                            "phone": None,
                            "lastName": "Bar",
                        },
                        "notifyUrl": "https://example.com/process_url/token",
                    }
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_process_renew(self):
        """Test processing renew"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "http://test_redirect_uri.com/",
                    "status": {"statusCode": "SUCCESS"},
                    "orderId": 123,
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            redirect = self.provider.process_data(
                payment=self.payment, request=mocked_post
            )
            self.assertEqual(redirect.__class__.__name__, "HttpResponseRedirect")
            self.assertEqual(redirect.url, "http://test_redirect_uri.com/")
            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "products": [
                            {
                                "currency": "USD",
                                "quantity": 10,
                                "name": "foo",
                                "unitPrice": 2000,
                                "subUnit": 100,
                            }
                        ],
                        "extOrderId": "bar_token",
                        "buyer": {
                            "phone": None,
                            "email": "foo@bar.com",
                            "lastName": "Bar",
                            "language": "en",
                            "firstName": "Foo",
                        },
                        "merchantPosId": "123abc",
                        "notifyUrl": "https://example.com/process_url/token",
                        "payMethods": {
                            "payMethod": {"value": "bar_token", "type": "CARD_TOKEN"}
                        },
                        "totalAmount": 22000,
                        "continueUrl": "http://foo_succ.com",
                        "customerIp": "123",
                        "description": "payment",
                        "recurring": "STANDARD",
                        "currencyCode": "USD",
                    },
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_process_renew_card_on_file(self):
        """Test processing renew"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.provider.card_on_file = True
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "http://test_redirect_uri.com/",
                    "status": {"statusCode": "SUCCESS"},
                    "orderId": 123,
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            redirect = self.provider.process_data(
                payment=self.payment, request=mocked_post
            )
            self.assertEqual(redirect.__class__.__name__, "HttpResponseRedirect")
            self.assertEqual(redirect.url, "http://test_redirect_uri.com/")
            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "products": [
                            {
                                "currency": "USD",
                                "quantity": 10,
                                "name": "foo",
                                "unitPrice": 2000,
                                "subUnit": 100,
                            }
                        ],
                        "extOrderId": "bar_token",
                        "buyer": {
                            "phone": None,
                            "email": "foo@bar.com",
                            "lastName": "Bar",
                            "language": "en",
                            "firstName": "Foo",
                        },
                        "merchantPosId": "123abc",
                        "notifyUrl": "https://example.com/process_url/token",
                        "payMethods": {
                            "payMethod": {"value": "bar_token", "type": "CARD_TOKEN"}
                        },
                        "totalAmount": 22000,
                        "continueUrl": "http://foo_succ.com",
                        "customerIp": "123",
                        "description": "payment",
                        "cardOnFile": "STANDARD_CARDHOLDER",
                        "currencyCode": "USD",
                    },
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_autocomplete_with_wallet(self):
        """Test processing renew. The function should return 'success' string, if nothing is required from user."""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}, "orderId": 123}'
            post.status_code = 200
            mocked_post.return_value = post
            self.provider.autocomplete_with_wallet(self.payment)
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_autocomplete_with_wallet_cvv2(self):
        """Test processing renew when cvv2 form is required - it should return the payment processing URL"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        with patch("requests.post") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {
                    "redirectUri": "test_redirect_uri",
                    "status": {"statusCode": "WARNING_CONTINUE_CVV"},
                    "orderId": 123,
                }
            )
            post.status_code = 200
            mocked_post.return_value = post
            try:
                self.provider.autocomplete_with_wallet(self.payment)
            except RedirectNeeded as redirect_url:
                self.assertEqual(str(redirect_url), "https://example.com/payment/token")
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.assertEqual(self.payment.captured_amount, Decimal("0"))

    def test_delete_card_token(self):
        """Test delete_card_token()"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.transaction_id = "1234"
        with patch("requests.delete") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}}'
            post.status_code = 204
            mocked_post.return_value = post
            rejected = self.provider.delete_card_token("FOO_TOKEN")
            self.assertTrue(rejected)
            mocked_post.assert_called_with(
                "http://mock.url/api/v2_1/tokens/FOO_TOKEN",
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )

    def test_get_paymethod_tokens(self):
        """Test delete_card_token()"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.transaction_id = "1234"
        with patch("requests.get") as mocked_post:
            post = MagicMock()
            post.text = json.dumps(
                {"cardTokens": [{"name": "Google Pay", "status": "ENABLED"}]}
            )
            post.status_code = 200
            mocked_post.return_value = post
            rdict = self.provider.get_paymethod_tokens()
            self.assertEqual(rdict["cardTokens"][0]["name"], "Google Pay")
            mocked_post.assert_called_with(
                "http://mock.url/api/v2_1/paymethods/",
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )

    def test_reject_order(self):
        """Test processing renew"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.transaction_id = "1234"
        with patch("requests.delete") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "SUCCESS"}}'
            post.status_code = 200
            mocked_post.return_value = post
            rejected = self.provider.reject_order(self.payment)
            self.assertTrue(rejected)
            mocked_post.assert_called_with(
                "http://mock.url/api/v2_1/orders/1234",
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
        self.assertEqual(self.payment.status, PaymentStatus.REJECTED)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REJECTED)

    def test_reject_order_error(self):
        """Test processing renew"""
        self.set_up_provider(
            True, True, get_refund_description=lambda payment, amount: "test"
        )
        self.payment.transaction_id = "1234"
        with patch("requests.delete") as mocked_post:
            post = MagicMock()
            post.text = '{"status": {"statusCode": "FAIL"}}'
            post.status_code = 200
            mocked_post.return_value = post
            rejected = self.provider.reject_order(self.payment)
            self.assertFalse(rejected)
            mocked_post.assert_called_with(
                "http://mock.url/api/v2_1/orders/1234",
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.WAITING)

    def test_refund(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
                get_refund_ext_id=lambda payment, amount: f"ext {payment.transaction_id} {amount}",
            )
        payment_extra_data_refund_response_previous = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009986",
                "extRefundId": "ext 1234 10",
                "amount": "1000",
                "currencyCode": "USD",
                "description": "desc 1234 10",
                "creationDateTime": "2020-07-02T08:19:03.896+02:00",
                "status": "PENDING",
                "statusDateTime": "2020-07-02T08:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = Decimal(210)
        self.payment.extra_data = json.dumps(
            {"refund_responses": [payment_extra_data_refund_response_previous]}
        )
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009987",
                "extRefundId": "ext 1234 110",
                "amount": "11000",
                "currencyCode": "USD",
                "description": "desc 1234 110",
                "creationDateTime": "2020-07-02T09:19:03.896+02:00",
                "status": "PENDING",
                "statusDateTime": "2020-07-02T09:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=11000,
            currency_code="USD",
            description="desc 1234 110",
            ext_refund_id="ext 1234 110",
            response_body=refund_request_response_body,
        )

        with refund_request_patch as refund_request_mock:
            amount = self.provider.refund(self.payment, Decimal(110))

        self.assertEqual(refund_request_mock.call_count, 1)
        self.assertEqual(amount, Decimal(0))
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(210))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [payment_extra_data_refund_response_previous, refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(210))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [payment_extra_data_refund_response_previous, refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_no_amount(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
                get_refund_ext_id=lambda payment, amount: f"ext {payment.transaction_id} {amount}",
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009987",
                "extRefundId": "ext 1234 None",
                "amount": "22000",
                "currencyCode": "USD",
                "description": "desc 1234 None",
                "creationDateTime": "2020-07-02T09:19:03.896+02:00",
                "status": "PENDING",
                "statusDateTime": "2020-07-02T09:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=None,
            currency_code="USD",
            description="desc 1234 None",
            ext_refund_id="ext 1234 None",
            response_body=refund_request_response_body,
        )

        with refund_request_patch as refund_request_mock:
            amount = self.provider.refund(self.payment)

        self.assertEqual(refund_request_mock.call_count, 1)
        self.assertEqual(amount, Decimal(0))
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_no_get_refund_ext_id(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009987",
                "extRefundId": "caf231c5-cbc1-4af3-96b7-95798b1cb846",
                "amount": "11000",
                "currencyCode": "USD",
                "description": "desc 1234 110",
                "creationDateTime": "2020-07-02T09:19:03.896+02:00",
                "status": "PENDING",
                "statusDateTime": "2020-07-02T09:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=11000,
            currency_code="USD",
            description="desc 1234 110",
            ext_refund_id="caf231c5-cbc1-4af3-96b7-95798b1cb846",
            response_body=refund_request_response_body,
        )

        with refund_request_patch as refund_request_mock:
            with patch(
                "uuid.uuid4", return_value="caf231c5-cbc1-4af3-96b7-95798b1cb846"
            ):
                amount = self.provider.refund(self.payment, Decimal(110))

        self.assertEqual(refund_request_mock.call_count, 1)
        self.assertEqual(amount, Decimal(0))
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_no_ext_id(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
                get_refund_ext_id=lambda payment, amount: None,
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009987",
                "amount": "11000",
                "currencyCode": "USD",
                "description": "desc 1234 110",
                "creationDateTime": "2020-07-02T09:19:03.896+02:00",
                "status": "PENDING",
                "statusDateTime": "2020-07-02T09:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=11000,
            currency_code="USD",
            description="desc 1234 110",
            ext_refund_id=None,
            response_body=refund_request_response_body,
        )

        with refund_request_patch as refund_request_mock:
            amount = self.provider.refund(self.payment, Decimal(110))

        self.assertEqual(refund_request_mock.call_count, 1)
        self.assertEqual(amount, Decimal(0))
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_no_ext_id_twice(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
                get_refund_ext_id=lambda payment, amount: None,
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009987",
                "amount": "20000",
                "currencyCode": "USD",
                "description": "desc 1234 200",
                "creationDateTime": "2020-07-02T09:19:03.896+02:00",
                "status": "PENDING",
                "statusDateTime": "2020-07-02T09:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=20000,
            currency_code="USD",
            description="desc 1234 200",
            ext_refund_id=None,
            response_body=refund_request_response_body,
        )

        with refund_request_patch as refund_request_mock:
            amount1 = self.provider.refund(self.payment, Decimal(200))
            amount2 = self.provider.refund(self.payment, Decimal(200))

        self.assertEqual(refund_request_mock.call_count, 2)
        self.assertEqual(amount2, amount1)
        self.assertEqual(amount2, Decimal(0))
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body, refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body, refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_pending(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
                get_refund_ext_id=lambda payment, amount: f"ext {payment.transaction_id} {amount}",
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009987",
                "extRefundId": "ext 1234 110",
                "amount": "11000",
                "currencyCode": "USD",
                "description": "desc 1234 110",
                "creationDateTime": "2020-07-02T09:19:03.896+02:00",
                "status": "PENDING",
                "statusDateTime": "2020-07-02T09:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=11000,
            currency_code="USD",
            description="desc 1234 110",
            ext_refund_id="ext 1234 110",
            response_body=refund_request_response_body,
        )

        with refund_request_patch as refund_request_mock:
            amount = self.provider.refund(self.payment, Decimal(110))

        self.assertEqual(refund_request_mock.call_count, 1)
        self.assertEqual(amount, Decimal(0))
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_canceled(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
                get_refund_ext_id=lambda payment, amount: f"ext {payment.transaction_id} {amount}",
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "orderId": "1234",
            "refund": {
                "refundId": "5000009987",
                "extRefundId": "ext 1234 110",
                "amount": "11000",
                "currencyCode": "USD",
                "description": "desc 1234 110",
                "creationDateTime": "2020-07-02T09:19:03.896+02:00",
                "status": "CANCELED",
                "statusDateTime": "2020-07-02T09:19:04.013+02:00",
            },
            "status": {
                "statusCode": "SUCCESS",
                "statusDesc": "Refund queued for processing",
            },
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=11000,
            currency_code="USD",
            description="desc 1234 110",
            ext_refund_id="ext 1234 110",
            response_body=refund_request_response_body,
        )

        with self.assertRaisesRegex(
            ValueError, "refund 5000009987 of payment 1 canceled"
        ):
            with refund_request_patch as refund_request_mock:
                self.provider.refund(self.payment, Decimal(110))

        self.assertEqual(refund_request_mock.call_count, 1)
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_error(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_description=lambda payment, amount: f"desc {payment.transaction_id} {amount}",
                get_refund_ext_id=lambda payment, amount: f"ext {payment.transaction_id} {amount}",
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()
        refund_request_response_body = {
            "status": {
                "statusCode": "OPENPAYU_BUSINESS_ERROR",
                "severity": "ERROR",
                "code": "9102",
                "codeLiteral": "NO_BALANCE",
                "statusDesc": "Lack of funds in account",
            }
        }
        refund_request_patch = self._patch_refund(
            base_payu_url="http://mock.url",
            order_id="1234",
            access_token="test_access_token",
            amount=11000,
            currency_code="USD",
            description="desc 1234 110",
            ext_refund_id="ext 1234 110",
            response_body=refund_request_response_body,
        )

        with self.assertRaisesRegex(
            ValueError,
            r"refund \?\?\? of payment 1 failed: code=9102, "
            r"statusCode=OPENPAYU_BUSINESS_ERROR, "
            r"codeLiteral=NO_BALANCE, "
            r"statusDesc=Lack of funds in account",
        ):
            with refund_request_patch as refund_request_mock:
                self.provider.refund(self.payment, Decimal(110))

        self.assertEqual(refund_request_mock.call_count, 1)
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(
            json.loads(self.payment.extra_data)["refund_responses"],
            [refund_request_response_body],
        )
        self.assertFalse(caught_warnings)

    def test_refund_no_get_refund_description(self):
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            self.set_up_provider(
                True,
                True,
                get_refund_ext_id=lambda payment, amount: f"ext {payment.transaction_id} {amount}",
            )
        self.payment.transaction_id = "1234"
        self.payment.captured_amount = self.payment.total
        self.payment.change_status(PaymentStatus.CONFIRMED)
        self.payment.save()

        with self.assertRaisesRegex(ValueError, r"^get_refund_description not set"):
            self.provider.refund(self.payment, Decimal(110))

        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertFalse(
            json.loads(self.payment.extra_data).get("refund_responses", [])
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.total, Decimal(220))
        self.assertEqual(self.payment.captured_amount, Decimal(220))
        self.assertEqual(self.payment.status, PaymentStatus.CONFIRMED)
        self.assertFalse(
            json.loads(self.payment.extra_data).get("refund_responses", [])
        )
        self.assertEqual(len(caught_warnings), 1)
        self.assertTrue(issubclass(caught_warnings[0].category, DeprecationWarning))
        self.assertEqual(
            str(caught_warnings[0].message),
            "A default value of get_refund_description is deprecated. Set it to a callable instead.",
        )

    @contextlib.contextmanager
    def _patch_refund(
        self,
        base_payu_url,
        order_id,
        access_token,
        currency_code,
        description,
        response_body,
        amount=None,
        ext_refund_id=None,
    ):
        requests_post_patch = patch(
            "requests.post",
            return_value=MagicMock(status_code=200, text=json.dumps(response_body)),
        )
        with requests_post_patch as requests_post_mock:
            yield requests_post_mock
            for requests_post_mock_call in requests_post_mock.call_args_list:
                requests_post_mock_call_data_actual_json = (
                    requests_post_mock_call.kwargs.pop("data")
                )
                self.assertEqual(
                    requests_post_mock_call.args,
                    (f"{base_payu_url}/api/v2_1/orders/{order_id}/refunds",),
                )
                self.assertEqual(
                    requests_post_mock_call.kwargs,
                    {
                        "headers": {
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                        }
                    },
                )
                requests_post_mock_call_data_expected = {
                    "refund": {
                        "currencyCode": currency_code,
                        "description": description,
                    }
                }
                if amount is not None:
                    requests_post_mock_call_data_expected["refund"]["amount"] = amount
                if ext_refund_id is not None:
                    requests_post_mock_call_data_expected["refund"][
                        "extRefundId"
                    ] = ext_refund_id
                self.assertEqual(
                    json.loads(requests_post_mock_call_data_actual_json),
                    requests_post_mock_call_data_expected,
                )

    def test_buyer_language(self):
        self.set_up_provider(
            True,
            False,
            get_refund_description=lambda payment, amount: "test",
            get_buyer_language=lambda payment: "pl",
        )
        with patch("requests.post", return_value=MagicMock(text="{}")) as mocked_post:
            with self.assertRaises(RedirectNeeded):
                self.provider.get_form(payment=self.payment)

            mocked_post.assert_called_once_with(
                "http://mock.url/api/v2_1/orders/",
                allow_redirects=False,
                data=JSONEquals(
                    {
                        "buyer": {
                            "email": "foo@bar.com",
                            "language": "pl",
                            "lastName": "Bar",
                            "firstName": "Foo",
                            "phone": None,
                        },
                        "description": "payment",
                        "totalAmount": 22000,
                        "merchantPosId": "123abc",
                        "customerIp": "123",
                        "notifyUrl": "https://example.com/process_url/token",
                        "extOrderId": "bar_token",
                        "products": [
                            {
                                "currency": "USD",
                                "name": "foo",
                                "quantity": 10,
                                "unitPrice": 2000,
                                "subUnit": 100,
                            }
                        ],
                        "continueUrl": "http://foo_succ.com",
                        "currencyCode": "USD",
                    },
                ),
                headers={
                    "Authorization": "Bearer test_access_token",
                    "Content-Type": "application/json",
                },
            )
