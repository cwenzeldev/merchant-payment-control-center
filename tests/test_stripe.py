import hashlib, hmac, io, json, unittest, urllib.error
from unittest.mock import patch
from payments.engine import Problem
from payments.stripe_test import StripeTest
from payments.webhooks import verify
KEY = 'sk_test_syntheticfixture'

class Response(io.BytesIO):

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

class Opener:

    def __init__(self, values):
        self.values = iter(values)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return Response(json.dumps(next(self.values)).encode())

class StripeTests(unittest.TestCase):

    def test_live_key_refused_before_network(self):
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaises(Problem):
                StripeTest().connect('sk_live_forbidden')
            opener.assert_not_called()

    def test_connect_disconnect(self):
        with patch('urllib.request.build_opener', return_value=Opener([{'livemode': False}])):
            s = StripeTest()
            self.assertTrue(s.connect(KEY)['connected'])
            self.assertNotIn(KEY, json.dumps(s.status()))
            s.disconnect()
            self.assertFalse(s.status()['connected'])

    def test_live_response_refused(self):
        with patch('urllib.request.build_opener', return_value=Opener([{'livemode': True}])):
            with self.assertRaises(Problem):
                StripeTest().connect(KEY)

    def test_checkout_manual_capture_idempotency(self):
        opener = Opener([{'livemode': False}, {'id': 'cs_test_fixture', 'livemode': False, 'url': 'https://checkout.stripe.com/c/pay/cs_test_fixture'}])
        with patch('urllib.request.build_opener', return_value=opener):
            s = StripeTest()
            s.connect(KEY)
            s.action({'action': 'checkout', 'amount': 2500, 'operation_id': 'test_operation'}, 'http://127.0.0.1:8770')
        req = opener.requests[1]
        self.assertEqual(req.get_header('Idempotency-key'), 'mpcc-test_operation')
        self.assertIn(b'capture_method%5D=manual', req.data)
        self.assertNotIn(b'_operation_id', req.data)

    def test_foreign_payment_refused(self):
        opener = Opener([{'livemode': False}, {'id': 'pi_other', 'livemode': False, 'metadata': {}}])
        with patch('urllib.request.build_opener', return_value=opener):
            s = StripeTest()
            s.connect(KEY)
            with self.assertRaises(Problem):
                s.action({'action': 'cancel', 'payment_id': 'pi_other', 'operation_id': 'op1'}, 'http://127.0.0.1:8770')
        self.assertEqual(len(opener.requests), 2)

    def test_cancel_parameters(self):
        opener = Opener([{'livemode': False}, {'id': 'pi_demo', 'livemode': False, 'metadata': {'portfolio': 'merchant-payment-control-center'}}, {'id': 'pi_demo', 'livemode': False, 'status': 'canceled'}])
        with patch('urllib.request.build_opener', return_value=opener):
            s = StripeTest()
            s.connect(KEY)
            s.action({'action': 'cancel', 'payment_id': 'pi_demo', 'operation_id': 'op1'}, 'http://127.0.0.1:8770')
        self.assertEqual(opener.requests[-1].data, b'')
        self.assertEqual(opener.requests[-1].get_header('Idempotency-key'), 'mpcc-op1')

    def test_errors_do_not_expose_key(self):

        class Broken:

            def open(self, *args, **kwargs):
                raise urllib.error.URLError('do not expose ' + KEY)
        with patch('urllib.request.build_opener', return_value=Broken()):
            with self.assertRaises(Problem) as cm:
                StripeTest().connect(KEY)
        self.assertNotIn(KEY, str(cm.exception))

    def signature(self, body, stamp='1800000000'):
        return 't=' + stamp + ',v1=' + hmac.new(b'whsec_synthetic', stamp.encode() + b'.' + body, hashlib.sha256).hexdigest()

    def test_signature_valid(self):
        body = b'{"id":"evt_test","livemode":false}'
        self.assertEqual(verify(body, self.signature(body), 'whsec_synthetic', 1800000000)['id'], 'evt_test')

    def test_signature_failures(self):
        body = b'{"livemode":false}'
        header = self.signature(body)
        for (content, stamp) in [(body + b' ', 1800000000), (body, 1800000301)]:
            with self.assertRaises(Problem):
                verify(content, header, 'whsec_synthetic', stamp)

    def test_live_event_refused(self):
        body = b'{"livemode":true}'
        with self.assertRaises(Problem):
            verify(body, self.signature(body), 'whsec_synthetic', 1800000000)
if __name__ == '__main__':
    unittest.main()
