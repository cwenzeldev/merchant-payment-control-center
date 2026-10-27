"""Single-user local app. No real card entry or live-mode credentials."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from .engine import Engine, Problem
from .stripe_test import StripeTest
from .tlv import decode, inventory, DecodeError
from .webhooks import verify

def make_server(db_path, port=8770):
    engine = Engine(db_path)
    stripe = StripeTest()
    token = secrets.token_hex(32)
    configuration = {'webhook_secret': os.environ.get('STRIPE_TEST_WEBHOOK_SECRET', '')}
    with engine.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS stripe_inbox(id TEXT PRIMARY KEY,digest TEXT,type TEXT,object_id TEXT,received REAL)')
    if os.environ.get('STRIPE_TEST_SECRET'):
        stripe.connect(os.environ['STRIPE_TEST_SECRET'])

    class Handler(BaseHTTPRequestHandler):

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *args):
            pass

        def reply(self, status, data, kind='application/json'):
            body = json.dumps(data).encode() if kind == 'application/json' else data.encode()
            self.send_response(status)
            for (name, value) in [('Content-Type', kind + '; charset=utf-8'), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'), ('Content-Security-Policy', "default-src 'self'; script-src 'nonce-" + token + "'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")]:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def raw_body(self):
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or (not lengths[0].isdigit()):
                raise Problem('A single Content-Length is required')
            size = int(lengths[0])
            if size > 1000000:
                raise Problem('Request exceeds 1 MB')
            body = self.rfile.read(size)
            if len(body) != size:
                raise Problem('Incomplete request')
            return body

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {'error': 'Use the local app address'})
            path = self.path.split('?')[0]
            if path == '/':
                return self.reply(200, (Path(__file__).parent / 'static/index.html').read_text().replace('__TOKEN__', token), 'text/html')
            if path == '/api/state':
                state = engine.snapshot()
                state['stripe'] = stripe.status()
                with engine.db() as db:
                    state['stripe_events'] = [dict(r) for r in db.execute('SELECT id,type,object_id,received FROM stripe_inbox ORDER BY received DESC LIMIT 50')]
                return self.reply(200, state)
            self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.host_ok():
                return self.reply(403, {'error': 'Use the local app address'})
            if self.path != '/webhooks/stripe' and self.headers.get('X-App-Token') != token:
                return self.reply(403, {'error': 'Refresh the app session'})
            try:
                body = self.raw_body()
                if self.path == '/webhooks/stripe':
                    event = verify(body, self.headers.get('Stripe-Signature'), configuration['webhook_secret'])
                    eid = event.get('id')
                    kind = event.get('type')
                    obj = event.get('data', {}).get('object', {})
                    if not isinstance(eid, str) or not re.fullmatch('evt_[A-Za-z0-9]+', eid) or (not isinstance(kind, str)) or (not isinstance(obj, dict)):
                        raise Problem('Invalid Stripe event envelope')
                    digest = hashlib.sha256(body).hexdigest()
                    with engine.tx() as db:
                        old = db.execute('SELECT digest FROM stripe_inbox WHERE id=?', (eid,)).fetchone()
                        if old and old['digest'] != digest:
                            raise Problem('Stripe event ID collision')
                        if not old:
                            db.execute('INSERT INTO stripe_inbox VALUES(?,?,?,?,?)', (eid, digest, kind, obj.get('id', ''), time.time()))
                    return self.reply(200, {'duplicate': bool(old), 'received': True})
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise Problem('Provide a JSON object')
                if self.path == '/api/command':
                    result = engine.command(payload.get('key'), payload.get('command'))
                elif self.path == '/api/statement':
                    result = {'csv': engine.statement(payload.get('scenario', 'clean'))}
                elif self.path == '/api/reconcile':
                    result = engine.reconcile(payload.get('csv'))
                elif self.path == '/api/event':
                    eid = payload.get('event_id')
                    source = next((r for r in engine.snapshot()['outbox'] if r['id'] == eid), None)
                    if not source:
                        raise Problem('Choose a synthetic event')
                    event = {'id': source['id'], 'payment_id': source['payment_id'], 'type': source['kind'], 'created': source['created']}
                    result = engine.receive(json.dumps(event, sort_keys=True).encode())
                elif self.path == '/api/tlv':
                    result = {'inventory': inventory(decode(payload.get('hex')))}
                elif self.path == '/api/connect':
                    key = payload.get('secret')
                    if not isinstance(key, str):
                        raise Problem('Enter a Stripe test secret key')
                    signing = payload.get('webhook_secret', '')
                    if signing and (not isinstance(signing, str) or not re.fullmatch('whsec_[A-Za-z0-9]+', signing)):
                        raise Problem('Invalid webhook signing secret')
                    result = stripe.connect(key)
                    configuration['webhook_secret'] = signing
                elif self.path == '/api/disconnect':
                    result = stripe.disconnect()
                    configuration['webhook_secret'] = ''
                elif self.path == '/api/stripe':
                    result = stripe.action(payload, f'http://127.0.0.1:{self.server.server_port}')
                else:
                    return self.reply(404, {'error': 'Not found'})
                self.reply(200, result)
            except (Problem, DecodeError) as exc:
                self.reply(400, {'error': str(exc)})
            except sqlite3.Error:
                self.reply(503, {'error': 'Local database unavailable. Retry the same operation key.'})
            except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError, OSError):
                self.reply(400, {'error': 'Invalid request. Check the inputs; no raw request data is logged.'})
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.engine = engine
    server.stripe = stripe
    return server
