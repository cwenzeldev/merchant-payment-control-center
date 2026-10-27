"""Stripe test-only REST adapter. Keys stay in process memory; no card data enters this app."""
import json
import re
import threading
import urllib.request
import urllib.error
import urllib.parse
from .engine import Problem, cents, identifier

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class StripeTest:

    def __init__(self):
        self._key = None
        self._lock = threading.RLock()
        self._account = None

    def request(self, method, path, params=None, key=None):
        secret = key or self._key
        if not secret:
            raise Problem('Connect a Stripe test key first')
        if not re.fullmatch('(?:sk|rk)_test_[A-Za-z0-9]+', secret):
            raise Problem('Only Stripe test-mode secret or restricted keys are allowed')
        params = dict(params or {})
        operation = params.pop('_operation_id', None)
        data = urllib.parse.urlencode(params).encode() if method == 'POST' else None
        request = urllib.request.Request('https://api.stripe.com/v1/' + path, data=data, method=method, headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/x-www-form-urlencoded', 'Stripe-Version': '2024-06-20'})
        if method == 'POST':
            if not operation:
                raise Problem('A stable operation ID is required')
            request.add_header('Idempotency-Key', 'mpcc-' + operation)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=15) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise Problem(f'Stripe returned HTTP {code}. Check test-key permissions and object state in Stripe. Retry the same operation ID after uncertain failures.') from None
        except (OSError, ValueError, urllib.error.URLError):
            raise Problem('Stripe response unavailable. Outcome may be uncertain: retry with the SAME operation ID, or inspect Stripe before proceeding.') from None
        if isinstance(result, dict) and result.get('livemode') is True:
            raise Problem('Live-mode response refused')
        return result

    def connect(self, key):
        with self._lock:
            result = self.request('GET', 'balance', key=key)
            if result.get('livemode') is not False:
                raise Problem('Could not verify a test-mode account')
            self._key = key
            self._account = 'Stripe test mode'
            return {'connected': True, 'mode': 'test'}

    def disconnect(self):
        with self._lock:
            self._key = None
            self._account = None
        return {'connected': False}

    def status(self):
        return {'connected': self._key is not None, 'mode': 'test' if self._key else None}

    def action(self, payload, base_url):
        with self._lock:
            return self._action(payload, base_url)

    def _action(self, payload, base_url):
        if not isinstance(payload, dict):
            raise Problem('Provide an action object')
        op = payload.get('action')
        if op == 'list':
            result = self.request('GET', 'payment_intents?limit=20')
            return {'payments': [self.safe_payment(p) for p in result.get('data', []) if p.get('metadata', {}).get('portfolio') == 'merchant-payment-control-center']}
        operation = identifier(payload.get('operation_id')) if op != 'sync' else None
        if op == 'checkout':
            amount = cents(payload.get('amount'))
            result = self.request('POST', 'checkout/sessions', {'_operation_id': operation, 'mode': 'payment', 'payment_method_types[0]': 'card', 'line_items[0][price_data][currency]': 'usd', 'line_items[0][price_data][unit_amount]': amount, 'line_items[0][price_data][product_data][name]': 'Portfolio sandbox order', 'line_items[0][quantity]': 1, 'payment_intent_data[capture_method]': 'manual', 'payment_intent_data[metadata][portfolio]': 'merchant-payment-control-center', 'metadata[operation_id]': operation, 'success_url': base_url + '/?checkout=returned', 'cancel_url': base_url + '/?checkout=canceled'})
            url = result.get('url', '')
            if not url.startswith('https://checkout.stripe.com/'):
                raise Problem('Unexpected checkout URL')
            return {'checkout_url': url, 'session_id': result['id'], 'mode': 'test'}
        pid = payload.get('payment_id')
        if not isinstance(pid, str) or not re.fullmatch('pi_[A-Za-z0-9]+', pid):
            raise Problem('Choose a Stripe PaymentIntent')
        current = self.request('GET', 'payment_intents/' + pid)
        if current.get('metadata', {}).get('portfolio') != 'merchant-payment-control-center':
            raise Problem("Only this app's sandbox payments can be managed here")
        params = {'_operation_id': operation} if operation else {}
        if op == 'capture':
            params['amount_to_capture'] = cents(payload.get('amount'))
            result = self.request('POST', 'payment_intents/' + pid + '/capture', params)
        elif op == 'cancel':
            result = self.request('POST', 'payment_intents/' + pid + '/cancel', params)
        elif op == 'refund':
            params.update(payment_intent=pid, amount=cents(payload.get('amount')))
            result = self.request('POST', 'refunds', params)
            return {'refund': {k: result.get(k) for k in ('id', 'amount', 'status', 'payment_intent')}}
        elif op == 'sync':
            result = current
        else:
            raise Problem('Unknown Stripe action')
        return {'payment': self.safe_payment(result)}

    @staticmethod
    def safe_payment(result):
        return {k: result.get(k) for k in ('id', 'amount', 'currency', 'status', 'amount_capturable', 'amount_received', 'created', 'livemode', 'metadata')}
