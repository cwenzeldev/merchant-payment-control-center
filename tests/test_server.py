import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from payments.server import make_server

class ServerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'STRIPE_TEST_WEBHOOK_SECRET': 'whsec_fixture', 'STRIPE_TEST_SECRET': ''}):
            cls.server = make_server(Path(cls.tmp.name) / 'db.sqlite3', 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        (status, body) = cls.request('GET', '/')
        cls.token = re.search("token='([a-f0-9]+)'", body.decode()).group(1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    @classmethod
    def request(cls, method, path, payload=None, headers=None, raw=None):
        c = http.client.HTTPConnection('127.0.0.1', cls.server.server_port, timeout=5)
        hs = {'X-App-Token': getattr(cls, 'token', ''), 'Content-Type': 'application/json'}
        hs.update(headers or {})
        c.request(method, path, raw if raw is not None else json.dumps(payload) if payload is not None else None, hs)
        r = c.getresponse()
        status = r.status
        body = r.read()
        c.close()
        return (status, body)

    def test_page_has_no_template_token(self):
        (status, body) = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'Stripe sandbox', body)
        self.assertNotIn(b'__TOKEN__', body)

    def test_csrf(self):
        self.assertEqual(self.request('POST', '/api/command', {}, headers={'X-App-Token': 'bad'})[0], 403)

    def test_host_guard(self):
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)

    def test_unknown_path(self):
        self.assertEqual(self.request('GET', '/../../.env')[0], 404)

    def test_full_http_workflow(self):
        (status, body) = self.request('POST', '/api/command', {'key': 'http_create', 'command': {'action': 'create', 'amount': 12500}})
        self.assertEqual(status, 200)
        pid = json.loads(body)['payment']['id']
        self.assertEqual(self.request('POST', '/api/command', {'key': 'http_capture', 'command': {'action': 'capture', 'payment_id': pid, 'amount': 10000}})[0], 200)
        (_, csv) = self.request('POST', '/api/statement', {'scenario': 'mismatch'})
        (status, report) = self.request('POST', '/api/reconcile', {'csv': json.loads(csv)['csv']})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(report)['passed'])

    def test_live_key_http_rejected(self):
        (status, body) = self.request('POST', '/api/connect', {'secret': 'sk_live_do_not_use'})
        self.assertEqual(status, 400)
        self.assertNotIn(b'sk_live_do_not_use', body)

    def test_tlv(self):
        (status, body) = self.request('POST', '/api/tlv', {'hex': '77059F36020001'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['inventory'][1]['tag'], '9F36')

    def test_tlv_bad_input(self):
        self.assertEqual(self.request('POST', '/api/tlv', {'hex': '9F'})[0], 400)

    def test_webhook_signed_dedup(self):
        body = b'{"id":"evt_testreceipt","livemode":false,"type":"payment_intent.succeeded","data":{"object":{"id":"pi_fixture"}}}'
        stamp = str(int(time.time()))
        signature = 't=' + stamp + ',v1=' + hmac.new(b'whsec_fixture', stamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        (status, result) = self.request('POST', '/webhooks/stripe', raw=body, headers={'Stripe-Signature': signature})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(result)['duplicate'])
        (status, result) = self.request('POST', '/webhooks/stripe', raw=body, headers={'Stripe-Signature': signature})
        self.assertTrue(json.loads(result)['duplicate'])

    def test_webhook_unsigned_rejected(self):
        self.assertEqual(self.request('POST', '/webhooks/stripe', {})[0], 400)

    def test_no_key_in_state(self):
        (_, body) = self.request('GET', '/api/state')
        self.assertNotIn(b'whsec_fixture', body)
        self.assertNotIn(b'whsec_', body)

    def test_nonobject_input(self):
        self.assertEqual(self.request('POST', '/api/command', [])[0], 400)
if __name__ == '__main__':
    unittest.main()
