import base64
import hashlib
import json
import logging
import uuid
import warnings
from decimal import ROUND_HALF_UP, Decimal
from string import Template
from urllib.parse import urljoin

import requests
from django import forms
from django.http.response import HttpResponse, HttpResponseRedirect
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from payments import FraudStatus, PaymentStatus, RedirectNeeded
from payments.core import BasicProvider, get_base_url
from payments.forms import PaymentForm

logger = logging.getLogger(__name__)

sig_sorted_key_list = [
    "currency-code",
    "customer-email",
    "customer-language",
    "cvv-url",
    "merchant-pos-id",
    "payu-brand",
    "recurring-payment",
    "shop-name",
    "store-card",
    "total-amount",
    "widget-mode",
]

CURRENCY_SUB_UNIT = {
    "PLN": Decimal(100),
    "EUR": Decimal(100),
    "USD": Decimal(100),
    "CZK": Decimal(100),
    "GBP": Decimal(100),
}


CENTS = Decimal("0.01")

GOOGLE_PAY_ALLOWED_AUTH_METHODS = ["PAN_ONLY", "CRYPTOGRAM_3DS"]
GOOGLE_PAY_ALLOWED_CARD_NETWORKS = ["MASTERCARD", "VISA"]

# Rendered with string.Template ($config, $process_url) because the JS is full
# of braces that format_html would treat as placeholders.
GOOGLE_PAY_SCRIPT_TEMPLATE = Template("""
<div id="google-pay-button" class="payu-google-pay-button"></div>
<script>
(function() {
    var cfg = $config;
    var cardPaymentMethod = {
        type: 'CARD',
        parameters: {
            allowedAuthMethods: cfg.allowed_auth_methods,
            allowedCardNetworks: cfg.allowed_card_networks
        },
        tokenizationSpecification: {
            type: 'PAYMENT_GATEWAY',
            parameters: {
                gateway: 'payu',
                gatewayMerchantId: cfg.gateway_merchant_id
            }
        }
    };
    window.onGooglePayLoaded = function() {
        var container = document.getElementById('google-pay-button');
        if (!container || container.hasChildNodes()) {
            // Already initialized (the form was re-rendered into the page and
            // both the inline call and the script onload fired) - do nothing.
            return;
        }
        var client = new google.payments.api.PaymentsClient({environment: cfg.environment});
        client.isReadyToPay({
            apiVersion: 2,
            apiVersionMinor: 0,
            allowedPaymentMethods: [cardPaymentMethod]
        }).then(function(response) {
            if (!response.result) {
                return;
            }
            var buttonOptions = {buttonSizeMode: 'fill', onClick: function() {
                client.loadPaymentData({
                    apiVersion: 2,
                    apiVersionMinor: 0,
                    allowedPaymentMethods: [cardPaymentMethod],
                    transactionInfo: {
                        totalPriceStatus: 'FINAL',
                        totalPrice: cfg.total,
                        currencyCode: cfg.currency
                    },
                    merchantInfo: cfg.merchant_info
                }).then(function(paymentData) {
                    container.classList.add('payu-wallet-processing');
                    document.body.classList.add('payu-wallet-processing');
                    var body = new URLSearchParams();
                    body.append('google_pay_token', paymentData.paymentMethodData.tokenizationData.token);
                    return fetch('$process_url', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: body.toString()
                    }).then(function(response) {
                        return response.text();
                    }).then(function(url) {
                        window.location.href = url;
                    });
                }).catch(function(err) {
                    container.classList.remove('payu-wallet-processing');
                    document.body.classList.remove('payu-wallet-processing');
                    console.log('Google Pay payment did not complete', err);
                });
            }};
            if (cfg.button_radius !== null) {
                buttonOptions.buttonRadius = cfg.button_radius;
            }
            if (cfg.button_color !== null) {
                buttonOptions.buttonColor = cfg.button_color;
            }
            container.appendChild(client.createButton(buttonOptions));
        });
    };
    if (window.google && window.google.payments && window.google.payments.api) {
        // pay.js is already loaded (the form was injected into an existing
        // page) - the script tag below won't be re-evaluated, initialize now.
        window.onGooglePayLoaded();
    }
})();
</script>
<script src="https://pay.google.com/gp/p/js/pay.js" async onload="onGooglePayLoaded()"></script>
""")

APPLE_PAY_SUPPORTED_NETWORKS = ["masterCard", "visa"]
APPLE_PAY_MERCHANT_CAPABILITIES = ["supports3DS"]

# Apple Pay for the Web. The button appears only in Safari on a device that can
# make payments. onvalidatemerchant and onpaymentauthorized both POST back to
# the payment's process URL (dispatched by field name), so no extra endpoints
# are needed. $config is server-built JSON; $process_url is the process URL.
APPLE_PAY_SCRIPT_TEMPLATE = Template("""
<style>
.payu-apple-pay-button {
    display: none;
    -webkit-appearance: -apple-pay-button;
    -apple-pay-button-type: plain;
    -apple-pay-button-style: black;
    width: 100%;
    min-height: 40px;
    cursor: pointer;
}
</style>
<div id="apple-pay-button" class="payu-apple-pay-button" lang="en"></div>
<script>
(function() {
    var cfg = $config;
    function initApplePay() {
        var button = document.getElementById('apple-pay-button');
        if (!button || button.dataset.payuInit) {
            return;
        }
        if (!window.ApplePaySession || !ApplePaySession.canMakePayments()) {
            return;
        }
        button.dataset.payuInit = '1';
        button.style.display = 'block';
        button.addEventListener('click', function() {
            var request = {
                countryCode: cfg.country_code,
                currencyCode: cfg.currency,
                supportedNetworks: cfg.supported_networks,
                merchantCapabilities: cfg.merchant_capabilities,
                total: {label: cfg.label, amount: cfg.total}
            };
            var session = new ApplePaySession(3, request);
            session.onvalidatemerchant = function(event) {
                var body = new URLSearchParams();
                body.append('apple_pay_validation_url', event.validationURL);
                fetch('$process_url', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: body.toString()
                }).then(function(response) {
                    return response.json();
                }).then(function(merchantSession) {
                    session.completeMerchantValidation(merchantSession);
                }).catch(function(err) {
                    console.log('Apple Pay merchant validation failed', err);
                    session.abort();
                });
            };
            session.onpaymentauthorized = function(event) {
                document.body.classList.add('payu-wallet-processing');
                var body = new URLSearchParams();
                body.append('apple_pay_token', JSON.stringify(event.payment.token.paymentData));
                fetch('$process_url', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: body.toString()
                }).then(function(response) {
                    return response.text();
                }).then(function(url) {
                    session.completePayment(ApplePaySession.STATUS_SUCCESS);
                    window.location.href = url;
                }).catch(function(err) {
                    document.body.classList.remove('payu-wallet-processing');
                    session.completePayment(ApplePaySession.STATUS_FAILURE);
                    console.log('Apple Pay payment did not complete', err);
                });
            };
            session.begin();
        });
    }
    initApplePay();
})();
</script>
""")


def add_extra_data(payment, new_extra_data):
    payment.refresh_from_db(fields=["extra_data"])
    if payment.extra_data:
        old_extra_data = json.loads(payment.extra_data)
    else:
        old_extra_data = {}
    extra_data = {**old_extra_data, **new_extra_data}
    payment.extra_data = json.dumps(extra_data, indent=2)
    original_reason = getattr(payment, "_change_reason", None)
    payment._change_reason = (
        "PayU Payments: add_new_status"  # For django-simple-history
    )
    payment.save(update_fields=["extra_data"])
    payment._change_reason = original_reason


def add_new_status(payment, new_status):
    payment.refresh_from_db(fields=["extra_data"])
    if payment.extra_data:
        old_extra_data = json.loads(payment.extra_data)
    else:
        old_extra_data = {}
    if "statuses" not in old_extra_data:
        old_extra_data["statuses"] = []
    old_extra_data["statuses"].append(new_status)
    payment.extra_data = json.dumps(old_extra_data, indent=2)
    original_reason = getattr(payment, "_change_reason", None)
    payment._change_reason = (
        "PayU Payments: add_new_status"  # For django-simple-history
    )
    payment.save(update_fields=["extra_data"])
    payment._change_reason = original_reason


def quantize_price(price, currency):
    price = price * CURRENCY_SUB_UNIT[currency]
    return int(price.quantize(CENTS, rounding=ROUND_HALF_UP))


def dequantize_price(price, currency):
    price = Decimal(price) / CURRENCY_SUB_UNIT[currency]
    return price


# A bit hacky method, how to get html output instead of form (for PayU express form and error form)
class HtmlOutputField(forms.HiddenInput):
    def __init__(self, *args, html="", **kwargs):
        self.html = html
        return super(HtmlOutputField, self).__init__(*args, **kwargs)

    def render(self, *args, **kwargs):
        # mark_safe: a plain str gets autoescaped when the widget is rendered
        # in a template (visible as literal "<br/><strong>..." on Django 5.x).
        # The html is built by this module from trusted, server-side values.
        return mark_safe(self.html)


class WidgetPaymentForm(PaymentForm):
    hide_submit_button = True  # For easy use in templates
    script = forms.CharField(label="Script")

    def __init__(
        self,
        payu_base_url,
        script_params={},
        google_pay_html="",
        apple_pay_html="",
        *args,
        **kwargs,
    ):
        ret = super(WidgetPaymentForm, self).__init__(*args, **kwargs)
        # mark_safe: plain-str concatenation would demote format_html's
        # SafeString and get the whole widget HTML escaped when rendered. The
        # wallet HTML is server-built (no user input, see
        # PayuProvider.get_google_pay_html / get_apple_pay_html).
        form_html = mark_safe(
            google_pay_html
            + apple_pay_html
            + format_html(
                "<script "
                f"src='{payu_base_url}front/widget/js/payu-bootstrap.js' "
                "pay-button='#pay-button' {params} >"
                "</script>"
                """
            <script>
                function cardSuccess($data) {{
                    console.log('callback');
                    console.log($data);
                    $.post(
                        '{process_url}',
                        $data,
                        function(data){{ window.location.href=data; }}
                    );
                }}
                function cvvSuccess($data) {{
                    console.log('cvv success');
                    console.log($data);
                    window.location.href="{success_url}";
                }}
            </script>
            <div id="payu-widget"></div>
            """,
                params=format_html_join(
                    " ", "{}='{}'", ((k, v) for k, v in script_params.items())
                ),
                process_url=urljoin(
                    get_base_url(),
                    self.payment.get_process_url(),
                ),
                success_url=urljoin(
                    get_base_url(),
                    self.payment.get_success_url(),
                ),
            )
        )
        self.fields["script"].widget = HtmlOutputField(html=form_html)
        return ret


class RenewPaymentForm(PaymentForm):
    confirm = forms.BooleanField(label="Renew the payment", required=True)

    def __init__(self, *args, **kwargs):
        ret = super(RenewPaymentForm, self).__init__(*args, **kwargs)
        self.action = urljoin(get_base_url(), self.payment.get_process_url())
        return ret


class PaymentErrorForm(forms.Form):
    script = forms.CharField(
        widget=HtmlOutputField(
            html="<br/><strong>This payment is already being processed.<br/></strong>",
        ),
    )
    hide_submit_button = True
    error_form = True


class PayuApiError(Exception):
    pass


class PayuProvider(BasicProvider):
    def __init__(self, *args, **kwargs):
        self.client_secret = kwargs.pop("client_secret")
        self.second_key = kwargs.pop("second_key")
        self.payu_sandbox = kwargs.pop("sandbox", False)
        self.payu_base_url = kwargs.pop(
            "base_payu_url",
            (
                "https://secure.snd.payu.com/"
                if self.payu_sandbox
                else "https://secure.payu.com/"
            ),
        )
        self.payu_auth_url = kwargs.pop(
            "auth_url", urljoin(self.payu_base_url, "/pl/standard/user/oauth/authorize")
        )
        self.payu_api_url = kwargs.pop(
            "api_url", urljoin(self.payu_base_url, "api/v2_1/")
        )
        self.payu_token_url = kwargs.pop(
            "token_url", urljoin(self.payu_api_url, "tokens/")
        )
        self.payu_api_orders_url = urljoin(self.payu_api_url, "orders/")
        self.payu_api_paymethods_url = urljoin(self.payu_api_url, "paymethods/")
        self.payu_widget_branding = kwargs.pop("widget_branding", False)
        self.payu_store_card = kwargs.pop("store_card", False)
        self.payu_shop_name = kwargs.pop("shop_name", "")
        self.grant_type = kwargs.pop("grant_type", "client_credentials")
        self.recurring_payments = kwargs.pop("recurring_payments", False)
        self.get_refund_description = kwargs.pop(
            "get_refund_description",
            # TODO: The default is deprecated. Remove in the next major release.
            None,
        )
        if self.get_refund_description is None:
            warnings.warn(
                "A default value of get_refund_description is deprecated. Set it to a callable instead.",
                DeprecationWarning,
            )
        self.get_refund_ext_id = kwargs.pop(
            "get_refund_ext_id", lambda payment, amount: str(uuid.uuid4())
        )

        # Use card on file paremeter instead of recurring.
        # PayU asks CVV2 every time with this setting which can be used for testing purposes.
        self.card_on_file = kwargs.pop("card_on_file", False)

        self.express_payments = kwargs.pop("express_payments", False)
        # Google Pay express button, only valid with express_payments=True.
        # A dict with optional keys: merchant_id, merchant_name, environment,
        # gateway_merchant_id, allowed_auth_methods, allowed_card_networks.
        self.google_pay = kwargs.pop("google_pay", None)
        # Apple Pay express button, only valid with express_payments=True. A
        # dict with keys: merchant_id, merchant_name (display name), country_code,
        # certificate (anything requests accepts as cert= for the merchant
        # identity certificate used in Apple merchant validation), and optional
        # supported_networks / merchant_capabilities.
        self.apple_pay = kwargs.pop("apple_pay", None)
        self.retry_count = 5

        self.pos_id = kwargs.pop("pos_id")
        self.token = self.get_access_token(
            self.pos_id, self.client_secret, grant_type=self.grant_type
        )
        self.get_buyer_language = kwargs.pop(
            "get_buyer_language",
            lambda payment: "en",
        )
        super(PayuProvider, self).__init__(*args, **kwargs)

    def _get_payu_api_order_url(self, order_id):
        return urljoin(self.payu_api_orders_url, order_id)

    def get_sig(self, payu_data):
        string = "".join(
            str(payu_data[key]) for key in sig_sorted_key_list if key in payu_data
        )
        string += self.second_key
        return hashlib.sha256(string.encode("utf-8")).hexdigest().lower()

    def autocomplete_with_wallet(self, payment):
        renew_token = payment.get_renew_token()
        url = self.process_widget(
            payment, renew_token, recurring="STANDARD", auto_renew=True
        )
        if url != "success":
            raise RedirectNeeded(urljoin(get_base_url(), url))

    def get_form(self, payment, data={}):
        if not data:
            data = {}

        if not self.express_payments:
            pay_link = self.create_order(payment, self.get_processor(payment))
            raise RedirectNeeded(pay_link)

        cvv_url = None
        if payment.extra_data:
            extra_data = json.loads(payment.extra_data)
            if "3ds_url" in extra_data:
                raise RedirectNeeded(extra_data["3ds_url"])
            cvv_url = extra_data.get("cvv_url", None)

        if payment.status != PaymentStatus.WAITING:
            return PaymentErrorForm()

        renew_token = payment.get_renew_token()
        if renew_token and self.recurring_payments and not cvv_url:
            return RenewPaymentForm(provider=self, payment=payment)
            # Use this, if the user doesn't need to be informed about the recurring payment:
            # raise RedirectNeeded(payment.get_process_url())

        payu_data = {
            "merchant-pos-id": self.pos_id,
            "shop-name": self.payu_shop_name,
            "total-amount": payment.total,
            "currency-code": payment.currency,
            "customer-language": self.get_buyer_language(payment=payment),
            "success-callback": "cardSuccess",
        }
        if cvv_url:
            payu_data.update(
                {
                    "cvv-url": cvv_url,
                    "cvv-success-callback": "cvvSuccess",
                    "widget-type": "cvv",
                }
            )
        else:
            payu_data.update(
                {
                    "customer-email": payment.billing_email,
                    "store-card": str(self.payu_store_card).lower(),
                    "payu-brand": str(self.payu_widget_branding).lower(),
                }
            )
            if self.recurring_payments:
                payu_data["recurring-payment"] = "true"
        payu_data["sig"] = self.get_sig(payu_data)

        return WidgetPaymentForm(
            payu_base_url=self.payu_base_url,
            data=data,
            script_params=payu_data,
            google_pay_html=self.get_google_pay_html(payment) if not cvv_url else "",
            apple_pay_html=self.get_apple_pay_html(payment) if not cvv_url else "",
            provider=self,
            payment=payment,
        )

    def get_google_pay_html(self, payment):
        """Render the Google Pay button + JS for the express payment form."""
        if not self.google_pay:
            return ""
        merchant_info = {}
        if self.google_pay.get("merchant_name"):
            merchant_info["merchantName"] = self.google_pay["merchant_name"]
        if self.google_pay.get("merchant_id"):
            merchant_info["merchantId"] = self.google_pay["merchant_id"]
        config = {
            "environment": self.google_pay.get(
                "environment", "TEST" if self.payu_sandbox else "PRODUCTION"
            ),
            "gateway_merchant_id": str(
                self.google_pay.get("gateway_merchant_id", self.pos_id)
            ),
            "allowed_auth_methods": self.google_pay.get(
                "allowed_auth_methods", GOOGLE_PAY_ALLOWED_AUTH_METHODS
            ),
            "allowed_card_networks": self.google_pay.get(
                "allowed_card_networks", GOOGLE_PAY_ALLOWED_CARD_NETWORKS
            ),
            "total": str(payment.total.quantize(CENTS)),
            "currency": payment.currency,
            "merchant_info": merchant_info,
            "button_radius": self.google_pay.get("button_radius"),
            "button_color": self.google_pay.get("button_color"),
        }
        return GOOGLE_PAY_SCRIPT_TEMPLATE.substitute(
            # All config values come from provider settings, but escape "<"
            # anyway so no value can ever break out of the <script> block
            # (same hardening as Django's json_script).
            config=json.dumps(config).replace("<", "\\u003c"),
            process_url=urljoin(get_base_url(), payment.get_process_url()),
        )

    def get_apple_pay_html(self, payment):
        """Render the Apple Pay button + JS for the express payment form."""
        if not self.apple_pay:
            return ""
        config = {
            "country_code": self.apple_pay.get("country_code", "US"),
            "currency": payment.currency,
            "total": str(payment.total.quantize(CENTS)),
            "label": self.apple_pay.get("merchant_name") or self.payu_shop_name,
            "supported_networks": self.apple_pay.get(
                "supported_networks", APPLE_PAY_SUPPORTED_NETWORKS
            ),
            "merchant_capabilities": self.apple_pay.get(
                "merchant_capabilities", APPLE_PAY_MERCHANT_CAPABILITIES
            ),
        }
        return APPLE_PAY_SCRIPT_TEMPLATE.substitute(
            config=json.dumps(config).replace("<", "\\u003c"),
            process_url=urljoin(get_base_url(), payment.get_process_url()),
        )

    def validate_apple_pay_merchant(self, validation_url):
        """Exchange Apple's validationURL for a merchant session.

        Apple Pay for the Web requires the merchant server to authenticate to
        Apple with the Merchant Identity certificate before the payment sheet
        opens. Returns the merchant session dict to hand back to the browser.
        """
        domain = urljoin(get_base_url(), "/").split("://", 1)[-1].strip("/")
        payload = {
            "merchantIdentifier": self.apple_pay["merchant_id"],
            "displayName": self.apple_pay.get("merchant_name") or self.payu_shop_name,
            "initiative": "web",
            "initiativeContext": domain,
        }
        response = requests.post(
            validation_url,
            json=payload,
            cert=self.apple_pay.get("certificate"),
        )
        return response.json()

    def process_apple_pay_validation(self, payment, validation_url):
        return HttpResponse(
            json.dumps(self.validate_apple_pay_merchant(validation_url)),
            content_type="application/json",
            status=200,
        )

    def process_apple_pay_callback(self, payment, apple_pay_token):
        """Charge an order with an Apple Pay token from the express form.

        PayU expects the raw Apple Pay token base64-encoded in the
        authorizationCode of a "jp" pay-by-link method. As with Google Pay, the
        first payment of a recurring plan is sent with recurring=FIRST so PayU
        issues a multi-use card token for later renewals.
        """
        processor = self.get_processor(payment)
        if self.card_on_file:
            processor.cardOnFile = "FIRST"
        elif self.recurring_payments:
            processor.recurring = "FIRST"
        processor.set_paymethod(
            method_type="PBL",
            value="jp",
            authorization_code=base64.b64encode(apple_pay_token.encode("utf-8")).decode(
                "ascii"
            ),
        )
        data = self.create_order(payment, processor)
        return HttpResponse(data, status=200)

    def get_processor(self, payment):
        order = payment.get_purchased_items()
        notify_url = urljoin(get_base_url(), payment.get_process_url())
        processor = PaymentProcessor(
            order=order,
            notify_url=notify_url,
            currency=payment.currency,
            description=payment.description,
            customer_ip=payment.customer_ip_address,
            total=payment.total,
            tax=payment.tax,
        )
        processor.set_buyer_data(
            first_name=payment.billing_first_name,
            last_name=payment.billing_last_name,
            email=payment.billing_email,
            phone=None,
            lang_code=self.get_buyer_language(payment=payment),
        )
        processor.external_id = payment.token
        processor.continueUrl = urljoin(get_base_url(), payment.get_success_url())
        processor.failureUrl = urljoin(get_base_url(), payment.get_failure_url())
        return processor

    def process_widget(self, payment, card_token, recurring="FIRST", auto_renew=False):
        processor = self.get_processor(payment)
        if self.card_on_file:
            processor.cardOnFile = (
                "FIRST" if recurring == "FIRST" else "STANDARD_CARDHOLDER"
            )
            # TODO: or STANDARD_MERCHANT
        elif self.recurring_payments:
            processor.recurring = recurring
        if self.express_payments:
            processor.set_paymethod(method_type="CARD_TOKEN", value=card_token)
        data = self.create_order(payment, processor, auto_renew)
        return data

    def process_widget_callback(self, payment, card_token, recurring="FIRST"):
        data = self.process_widget(payment, card_token, recurring)
        if recurring == "STANDARD":
            return HttpResponseRedirect(data)
        return HttpResponse(data, status=200)

    def process_google_pay_callback(self, payment, google_pay_token):
        """Charge an order with a Google Pay token from the express form.

        PayU expects the raw Google Pay token base64-encoded in the
        authorizationCode of an "ap" pay-by-link method. The first payment of
        a recurring plan is sent with recurring=FIRST so PayU issues a
        multi-use card token for later renewals.
        """
        processor = self.get_processor(payment)
        if self.card_on_file:
            processor.cardOnFile = "FIRST"
        elif self.recurring_payments:
            processor.recurring = "FIRST"
        processor.set_paymethod(
            method_type="PBL",
            value="ap",
            authorization_code=base64.b64encode(
                google_pay_token.encode("utf-8")
            ).decode("ascii"),
        )
        data = self.create_order(payment, processor)
        return HttpResponse(data, status=200)

    def post_request(self, url, *args, **kwargs):
        for i in range(1, self.retry_count):
            kwargs["headers"] = self.get_token_headers()
            response = requests.post(url, *args, **kwargs)
            try:
                response_dict = json.loads(response.text)
            except ValueError as e:
                # Gateway-level failures (e.g. an HTML 502 page from a proxy)
                # are not JSON; surface them as the provider's own exception.
                raise PayuApiError(
                    f"Non-JSON response from PayU: HTTP {response.status_code}, "
                    f"body: {response.text[:200]!r}"
                ) from e
            # On gateway errors "status" can be a plain integer HTTP code
            # ('{"status": 500}') instead of the usual status dict.
            response_status = response_dict.get("status")
            if (response_dict.get("error") == "invalid_token") or (
                isinstance(response_status, dict)
                and response_status.get("statusCode") == "UNAUTHORIZED"
            ):
                try:
                    self.token = self.get_access_token(
                        self.pos_id, self.client_secret, grant_type=self.grant_type
                    )
                except PayuApiError as e:
                    raise PayuApiError(f"Unable to regain authorization token {e}")
            else:
                return response_dict
        raise PayuApiError("Unable to regain authorization token")

    def get_access_token(
        self,
        client_id,
        client_secret,
        grant_type="client_credentials",
        email=None,
        customer_id=None,
    ):
        """
        Get access token from PayU API
        grant_type: 'trusted_merchant' or 'client_credentials'
        email and customer_id is required only for grant_type=trusted_merchant
        """

        payu_auth_url = self.payu_auth_url
        data = {
            "grant_type": grant_type,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if email:
            data["email"] = email
        if customer_id:
            data["ext_customer_id"] = customer_id

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        response = requests.post(payu_auth_url, data=data, headers=headers)

        try:
            response_dict = json.loads(response.text)
        except json.JSONDecodeError:
            raise PayuApiError(response.text)

        try:
            return response_dict["access_token"]
        except (KeyError, ValueError):
            raise PayuApiError(response_dict)

    def get_token_headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % self.token,
        }

    def delete_card_token(self, card_token):
        "Deactivate card token on PayU"

        payu_delete_token_url = urljoin(self.payu_token_url, card_token)
        response = requests.delete(
            payu_delete_token_url, headers=self.get_token_headers()
        )

        return response.status_code == 204

    def create_order(self, payment, payment_processor, auto_renew=False):
        """
        Create order and return payment link or redirect

        return redirectUrl  url where the user should go next
        """
        payment = payment
        payment_processor.pos_id = self.pos_id
        json_data = payment_processor.as_json()
        response_dict = self.post_request(
            self.payu_api_orders_url,
            data=json.dumps(json_data),
            allow_redirects=False,
        )

        try:
            payment.transaction_id = response_dict["orderId"]
            payment._change_reason = "PayU Payments: create_order -> transaction_id"  # For django-simple-history
            payment.save(update_fields=["transaction_id"])
            payment._change_reason = None

            if "payMethods" in response_dict:
                pay_method = response_dict["payMethods"]["payMethod"]
                # A wallet order (Google Pay) can echo the pay-by-link method
                # ("ap") without card data; that value is not a reusable card
                # token, so don't overwrite the stored renew token with it.
                if "card" in pay_method:
                    payment.set_renew_token(
                        pay_method["value"],
                        card_expire_year=pay_method["card"]["expirationYear"],
                        card_expire_month=pay_method["card"]["expirationMonth"],
                        card_masked_number=pay_method["card"]["number"],
                        renewal_triggered_by=(
                            "task" if self.recurring_payments else "user"
                        ),
                    )
            add_extra_data(payment, {"card_response": response_dict})

            if response_dict["status"]["statusCode"] == "SUCCESS":
                if "redirectUri" in response_dict:
                    return response_dict["redirectUri"]
                else:
                    if auto_renew:
                        return "success"
                    return payment_processor.continueUrl
            elif response_dict["status"]["statusCode"] == "WARNING_CONTINUE_CVV":
                add_extra_data(payment, {"cvv_url": response_dict["redirectUri"]})
                return payment.get_payment_url()
            elif response_dict["status"]["statusCode"] == "WARNING_CONTINUE_3DS":
                add_extra_data(payment, {"3ds_url": response_dict["redirectUri"]})
                return response_dict["redirectUri"]
        except (KeyError, TypeError):
            # TypeError: on gateway errors "status" is a plain integer HTTP
            # code, so the ["statusCode"] subscript above fails like a
            # missing key does.
            pass

        response_status = response_dict.get("status")
        if response_status is not None:
            if (
                isinstance(response_status, dict)
                and response_status.get("statusCode") == "BUSINESS_ERROR"
            ):
                # Payment rejected by PayUs anti-fruad system
                payment.change_fraud_status(FraudStatus.REJECT, message=response_dict)
            else:
                add_extra_data(payment, response_dict)
        if payment.status == PaymentStatus.CONFIRMED:
            # The payment was already confirmed (typically by an asynchronous
            # PayU webhook on a parallel request). A duplicate create_order -
            # e.g. the user's browser POSTing to the process URL again after
            # 3DS or on retry - is rejected by PayU (single-use widget token,
            # ERROR_ORDER_NOT_UNIQUE, antifraud, ...). Don't demote the
            # CONFIRMED state to ERROR; just send the user back to a page
            # where the confirmed status will be reflected.
            logger.info(
                "PayU rejected create_order for an already CONFIRMED payment "
                "(id=%s); keeping CONFIRMED state. Response: %s",
                payment.pk,
                response_dict,
            )
            return ""
        payment.change_status(PaymentStatus.ERROR)
        try:
            raise PayuApiError(response_dict)
        except PayuApiError:
            # codeLiteral is not guaranteed (and "status" may be an integer
            # on gateway errors) - never let the error log itself crash.
            logger.exception(
                "PayU API error:"
                + (
                    response_status.get("codeLiteral", "")
                    if isinstance(response_status, dict)
                    else str(response_status or "")
                )
            )
        return payment_processor.failureUrl

    # Method that returns all pay methods

    def get_paymethod_tokens(self):
        "Get pay methods of POS, if authenticated with 'trusted_merchant' grant type, it will get also card tokens"

        response = requests.get(
            self.payu_api_paymethods_url, headers=self.get_token_headers()
        )
        response_dict = json.loads(response.text)
        return response_dict

    # Method that rejects the order

    def reject_order(self, payment):
        "Reject order"

        url = self._get_payu_api_order_url(payment.transaction_id)

        try:
            # If the payment have status WAITING_FOR_CONFIRMATION, it is needed to make two calls of DELETE
            # http://developers.payu.com/pl/restapi.html#cancellation
            response1 = json.loads(
                requests.delete(url, headers=self.get_token_headers()).text
            )
            response2 = json.loads(
                requests.delete(url, headers=self.get_token_headers()).text
            )

            if (
                response1["status"]["statusCode"]
                == response2["status"]["statusCode"]
                == "SUCCESS"
            ):
                payment.change_status(PaymentStatus.REJECTED)
                return True
            else:
                raise PayuApiError(response1, response2)
        except PayuApiError:
            return False

    def process_notification(self, payment, request):
        try:
            json.loads(request.body.decode("utf8"))
            header = request.META["HTTP_OPENPAYU_SIGNATURE"]
        except KeyError:
            raise PayuApiError("Malformed POST")

        header_data_raw = header.split(";")
        header_data = {}
        for x in header_data_raw:
            key, value = x.split("=")[0], x.split("=")[1]
            header_data[key] = value

        incoming_signature = header_data["signature"]
        algorithm = header_data["algorithm"]

        if algorithm == "MD5":
            m = hashlib.md5()
            key = self.second_key
            signature = request.body + key.encode("utf8")
            m.update(signature)
            signature = m.hexdigest()
            if (
                incoming_signature == signature
            ):  # and not payment.status == PaymentStatus.CONFIRMED:
                data = json.loads(request.body.decode("utf8"))
                add_new_status(payment, data)
                if "refund" in data:
                    refunded_price = dequantize_price(
                        data["refund"]["amount"],
                        data["refund"]["currencyCode"],
                    )
                    print(refunded_price, payment.total)
                    if data["refund"]["status"] == "FINALIZED":
                        payment.message += data["refund"]["reasonDescription"]
                        if refunded_price > payment.captured_amount:
                            logger.error(
                                "refund %s of payment %s has amount greater than the payment's captured_amount: "
                                "%f > %f",
                                data["refund"].get("refundId", "???"),
                                payment.id,
                                refunded_price,
                                payment.captured_amount,
                            )
                        if refunded_price == payment.captured_amount:
                            payment.change_status(PaymentStatus.REFUNDED)
                        else:
                            payment.captured_amount -= refunded_price
                            original_reason = getattr(payment, "_change_reason", None)
                            # Change history for django-simple-history
                            payment._change_reason = (
                                "PayU Payments: process_notification -> refund"
                            )
                            payment.save(update_fields=["captured_amount", "message"])
                            payment._change_reason = original_reason
                        return HttpResponse("ok", status=200)
                    else:
                        raise Exception("Refund was not finelized", data)
                else:
                    status_map = {
                        "COMPLETED": PaymentStatus.CONFIRMED,
                        "PENDING": PaymentStatus.INPUT,
                        "WAITING_FOR_CONFIRMATION": PaymentStatus.INPUT,
                        "CANCELED": PaymentStatus.REJECTED,
                        "NEW": PaymentStatus.WAITING,
                    }
                    status = status_map[data["order"]["status"]]
                    if (
                        status == PaymentStatus.CONFIRMED
                        and "totalAmount" in data["order"]
                    ):
                        payment.captured_amount = dequantize_price(
                            data["order"]["totalAmount"],
                            data["order"]["currencyCode"],
                        )
                        original_reason = getattr(payment, "_change_reason", None)
                        # Change history for django-simple-history
                        payment._change_reason = (
                            "PayU Payments: process_notification -> confirmed"
                        )
                        payment.save(update_fields=["captured_amount"])
                        payment._change_reason = original_reason
                    if (
                        payment.status == PaymentStatus.CONFIRMED
                        and payment.status != status
                    ):
                        # A CONFIRMED payment must not be demoted by a later
                        # notification. PayU can deliver duplicate/out-of-order
                        # callbacks (e.g. a stale CANCELED arriving after
                        # COMPLETED); applying one would clobber a real capture.
                        # Refunds are handled by the "refund" branch above, not
                        # here, so any non-confirmed status for an already
                        # confirmed payment is spurious. Ack it (200) so PayU
                        # stops retrying, but keep the CONFIRMED state.
                        logger.error(
                            "Suspicious status change of payment %s: %s -> %s; "
                            "keeping CONFIRMED",
                            payment.id,
                            payment.status,
                            status,
                        )
                        return HttpResponse("ok", status=200)
                    payment.change_status(status)
                    return HttpResponse("ok", status=200)
        return HttpResponse("not ok", status=500)

    def process_data(self, payment, request, *args, **kwargs):
        self.request = request

        renew_token = payment.get_renew_token()

        if "application/json" in request.META.get("CONTENT_TYPE", {}):
            return self.process_notification(payment, request)
        elif "google_pay_token" in request.POST:
            return self.process_google_pay_callback(
                payment, request.POST["google_pay_token"]
            )
        elif "apple_pay_validation_url" in request.POST:
            return self.process_apple_pay_validation(
                payment, request.POST["apple_pay_validation_url"]
            )
        elif "apple_pay_token" in request.POST:
            return self.process_apple_pay_callback(
                payment, request.POST["apple_pay_token"]
            )
        elif renew_token and self.recurring_payments:
            return self.process_widget_callback(
                payment, renew_token, recurring="STANDARD"
            )
        elif "value" in request.POST:
            return self.process_widget_callback(
                payment, request.POST.get("value"), recurring="FIRST"
            )
        else:
            return HttpResponse(
                "request not recognized by django-payments-payu provider", status=500
            )

    def refund(self, payment, amount=None):
        if self.get_refund_description is None:
            raise ValueError("get_refund_description not set")

        request_url = self._get_payu_api_order_url(payment.transaction_id) + "/refunds"

        request_data = {
            "refund": {
                "currencyCode": payment.currency,
                "description": self.get_refund_description(
                    payment=payment, amount=amount
                ),
            }
        }
        if amount is not None:
            request_data.setdefault("refund", {}).setdefault(
                "amount", quantize_price(amount, payment.currency)
            )
        ext_refund_id = self.get_refund_ext_id(payment=payment, amount=amount)
        if ext_refund_id is not None:
            request_data.setdefault("refund", {}).setdefault(
                "extRefundId", ext_refund_id
            )

        response = self.post_request(request_url, data=json.dumps(request_data))

        payment_extra_data = json.loads(payment.extra_data or "{}")
        payment_extra_data_refund_responses = payment_extra_data.setdefault(
            "refund_responses", []
        )
        payment_extra_data_refund_responses.append(response)
        payment.extra_data = json.dumps(payment_extra_data, indent=2)
        payment._change_reason = "PayU Payments: refund"  # For django-simple-history
        payment.save()
        payment._change_reason = None

        try:
            refund = response["refund"]
            refund_id = refund["refundId"]
        except Exception:
            refund_id = None

        try:
            response_status = dict(response["status"])
            response_status_code = response_status["statusCode"]
        except Exception:
            raise PayuApiError(
                f"invalid response to refund {refund_id or '???'} of payment {payment.id}: {response}"
            )
        if response_status_code != "SUCCESS":
            raise ValueError(
                f"refund {refund_id or '???'} of payment {payment.id} failed: "
                f"code={response_status.get('code', '???')}, "
                f"statusCode={response_status_code}, "
                f"codeLiteral={response_status.get('codeLiteral', '???')}, "
                f"statusDesc={response_status.get('statusDesc', '???')}"
            )
        if refund_id is None:
            raise PayuApiError(
                f"invalid response to refund of payment {payment.id}: {response}"
            )

        try:
            refund_order_id = response["orderId"]
            refund_status = refund["status"]
            refund_currency = refund["currencyCode"]
            refund_amount = dequantize_price(refund["amount"], refund_currency)
        except Exception:
            raise PayuApiError(
                f"invalid response to refund {refund_id} of payment {payment.id}: {response}"
            )
        if refund_order_id != payment.transaction_id:
            raise NotImplementedError(
                f"response of refund {refund_id} of payment {payment.id} containing a different order_id "
                f"not supported yet: {refund_order_id}"
            )
        if refund_status == "CANCELED":
            raise ValueError(f"refund {refund_id} of payment {payment.id} canceled")
        elif refund_status == "FINALIZED":
            raise NotImplementedError(
                f"refund {refund_id} of payment {payment.id} being FINALIZED already is not supported yet"
            )
        elif refund_status not in {"PENDING"}:
            raise PayuApiError(
                f"invalid status of refund {refund_id} of payment {payment.id}"
            )
        if refund_currency != payment.currency:
            raise NotImplementedError(
                f"refund {refund_id} of payment {payment.id} in different currency not supported yet: "
                f"{refund_currency}"
            )
        if amount is not None and refund_amount != amount:
            raise NotImplementedError(
                f"refund {refund_id} of payment {payment.id} having a different amount than requested not supported "
                f"yet: {refund_amount}"
            )
        # Return 0 in order not to change captured_amount yet. If we returned the amount, captured_amount would change
        # twice (now and once we get a notification from PayU).
        return Decimal(0)


class PaymentProcessor(object):
    "Payment processor"

    def __init__(
        self,
        order,
        notify_url,
        currency,
        description,
        customer_ip,
        total,
        tax,
        **kwargs,
    ):
        self.order = order
        self.notify_url = notify_url
        self.currency = currency
        self.description = description
        self.customer_ip = customer_ip
        self.tax = tax
        self.order_items = []
        self.external_id = None
        self.pos_id = None
        self.total = total

    def get_order_items(self):
        for purchased_item in self.order:
            item = {
                "name": purchased_item.name[:127],
                "quantity": purchased_item.quantity,
                "unitPrice": quantize_price(
                    purchased_item.price * (purchased_item.tax_rate or 1), self.currency
                ),
                "currency": purchased_item.currency,
                "subUnit": int(CURRENCY_SUB_UNIT[self.currency]),
            }
            yield item

    def set_paymethod(self, value, method_type="PBL", authorization_code=None):
        "Set payment method, can given by PayuApi.get_paymethod_tokens()"
        if not hasattr(self, "paymethods"):
            self.paymethods = {}
            self.paymethods["payMethod"] = {"type": method_type, "value": value}
            if authorization_code:
                self.paymethods["payMethod"]["authorizationCode"] = authorization_code

    def set_buyer_data(self, first_name, last_name, email, phone, lang_code):
        "Set buyer data"
        if not hasattr(self, "buyer"):
            self.buyer = {
                "email": email,
                "phone": phone,
                "firstName": first_name,
                "lastName": last_name,
                "language": lang_code,
            }

    def as_json(self):
        "Return json for the payment"
        products = list(self.get_order_items())

        json_dict = {
            "notifyUrl": self.notify_url,
            "customerIp": self.customer_ip,
            "extOrderId": self.external_id,
            "merchantPosId": self.pos_id,
            "description": self.description,
            "currencyCode": self.currency,
            "totalAmount": quantize_price(self.total, self.currency),
            "products": products,
        }

        # additional data
        if hasattr(self, "paymethods"):
            json_dict["payMethods"] = self.paymethods

        if hasattr(self, "buyer"):
            json_dict["buyer"] = self.buyer

        if hasattr(self, "continueUrl"):
            json_dict["continueUrl"] = self.continueUrl

        if hasattr(self, "validityTime"):
            json_dict["validityTime"] = self.validityTime

        if hasattr(self, "recurring"):
            json_dict["recurring"] = self.recurring

        if hasattr(self, "cardOnFile"):
            json_dict["cardOnFile"] = self.cardOnFile

        return json_dict
