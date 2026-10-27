"""Transactional simulator, command idempotency, journal and reconciliation."""
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
import uuid

class Problem(ValueError):
    pass

def cents(value):
    if type(value) is not int or not 1 <= value <= 10000000:
        raise Problem('Amount must be an integer from 1 to 10,000,000 cents')
    return value

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', value):
        raise Problem('Use a nonempty ID with letters, digits, underscores or hyphens')
    return value

class Engine:

    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('\nCREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, amount INTEGER NOT NULL,\n state TEXT NOT NULL, captured INTEGER NOT NULL DEFAULT 0, refunded INTEGER NOT NULL DEFAULT 0,\n scenario TEXT NOT NULL, channel TEXT NOT NULL, created REAL NOT NULL);\nCREATE TABLE IF NOT EXISTS commands(key TEXT PRIMARY KEY,digest TEXT NOT NULL,response TEXT NOT NULL);\nCREATE TABLE IF NOT EXISTS timeline(seq INTEGER PRIMARY KEY,payment_id TEXT,kind TEXT,detail TEXT,at REAL);\nCREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY,ref TEXT, payment_id TEXT,account TEXT,\n debit INTEGER NOT NULL,credit INTEGER NOT NULL,at REAL);\nCREATE TABLE IF NOT EXISTS expected(ref TEXT PRIMARY KEY,payment_id TEXT,kind TEXT,net INTEGER NOT NULL);\nCREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,payment_id TEXT,kind TEXT,created REAL);\nCREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,digest TEXT NOT NULL,received REAL);\nCREATE TABLE IF NOT EXISTS projections(payment_id TEXT PRIMARY KEY,last_event TEXT,last_created REAL);\n')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def tx(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def event(self, db, pid, kind, detail):
        now = time.time()
        db.execute('INSERT INTO timeline(payment_id,kind,detail,at) VALUES(?,?,?,?)', (pid, kind, detail, now))
        db.execute('INSERT INTO outbox VALUES(?,?,?,?)', ('evt_' + uuid.uuid4().hex[:16], pid, kind, now))

    def post(self, db, pid, ref, debit_account, credit_account, amount):
        if amount <= 0:
            return
        now = time.time()
        db.executemany('INSERT INTO journal(ref,payment_id,account,debit,credit,at) VALUES(?,?,?,?,?,?)', [(ref, pid, debit_account, amount, 0, now), (ref, pid, credit_account, 0, amount, now)])

    def command(self, key, payload):
        identifier(key)
        if not isinstance(payload, dict):
            raise Problem('Command must be an object')
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.tx() as db:
            existing = db.execute('SELECT * FROM commands WHERE key=?', (key,)).fetchone()
            if existing:
                if existing['digest'] != digest:
                    raise Problem('Idempotency key reused with different parameters')
                return {**json.loads(existing['response']), 'duplicate': True}
            op = payload.get('action')
            if op == 'create':
                amount = cents(payload.get('amount'))
                scenario = payload.get('scenario', 'approve')
                channel = payload.get('channel', 'online')
                if scenario not in ('approve', 'decline', 'authenticate', 'timeout') or channel not in ('online', 'terminal'):
                    raise Problem('Unknown payment scenario or channel')
                pid = 'pay_' + uuid.uuid4().hex[:12]
                state = {'approve': 'authorized', 'decline': 'declined', 'authenticate': 'requires_action', 'timeout': 'unknown'}[scenario]
                db.execute('INSERT INTO payments(id,amount,state,scenario,channel,created) VALUES(?,?,?,?,?,?)', (pid, amount, state, scenario, channel, time.time()))
                self.event(db, pid, 'payment.' + state, 'Synthetic ' + channel + ' authorization: ' + state)
            else:
                pid = identifier(payload.get('payment_id'))
                row = db.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone()
                if not row:
                    raise Problem('Payment not found')
                state = row['state']
                if op in ('authenticate', 'recover'):
                    required = 'requires_action' if op == 'authenticate' else 'unknown'
                    if state != required:
                        raise Problem('Payment is not awaiting this action')
                    db.execute("UPDATE payments SET state='authorized' WHERE id=?", (pid,))
                    self.event(db, pid, 'payment.authorized', 'Synthetic ' + op + ' completed; no duplicate authorization')
                elif op == 'capture':
                    if state != 'authorized':
                        raise Problem('Only an authorized payment can be captured')
                    amount = cents(payload.get('amount'))
                    if amount > row['amount']:
                        raise Problem('Capture exceeds authorization')
                    db.execute("UPDATE payments SET state='captured',captured=? WHERE id=?", (amount, pid))
                    ref = 'cap_' + uuid.uuid4().hex[:12]
                    fee = (amount * 29 + 500) // 1000 + 30
                    self.post(db, pid, ref, 'processor_receivable', 'sales_clearing', amount)
                    self.post(db, pid, ref, 'processing_fees', 'processor_receivable', fee)
                    db.execute('INSERT INTO expected VALUES(?,?,?,?)', (ref, pid, 'capture', amount - fee))
                    self.event(db, pid, 'payment.captured', f"Captured {amount} cents; released {row['amount'] - amount} cents. Simulated fee {fee} cents.")
                elif op == 'cancel':
                    if state not in ('authorized', 'requires_action', 'unknown'):
                        raise Problem('Cannot cancel this payment')
                    db.execute("UPDATE payments SET state='canceled' WHERE id=?", (pid,))
                    self.event(db, pid, 'payment.canceled', 'Synthetic authorization released; no journal posting')
                elif op == 'refund':
                    if state not in ('captured', 'partially_refunded'):
                        raise Problem('No captured balance available for refund')
                    amount = cents(payload.get('amount'))
                    total = row['refunded'] + amount
                    if total > row['captured']:
                        raise Problem('Refund exceeds unrefunded captured amount')
                    status = 'refunded' if total == row['captured'] else 'partially_refunded'
                    db.execute('UPDATE payments SET state=?,refunded=? WHERE id=?', (status, total, pid))
                    ref = 'ref_' + uuid.uuid4().hex[:12]
                    self.post(db, pid, ref, 'sales_clearing', 'processor_receivable', amount)
                    db.execute('INSERT INTO expected VALUES(?,?,?,?)', (ref, pid, 'refund', -amount))
                    self.event(db, pid, 'payment.' + status, f'Refunded {amount} cents; total refunds {total} cents')
                else:
                    raise Problem('Unknown action')
            result = {'payment': dict(db.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone()), 'duplicate': False}
            db.execute('INSERT INTO commands VALUES(?,?,?)', (key, digest, json.dumps(result)))
            return result

    def receive(self, body):
        try:
            event = json.loads(body)
        except (ValueError, UnicodeError):
            raise Problem('Invalid event JSON') from None
        if not isinstance(event, dict):
            raise Problem('Invalid event')
        eid = identifier(event.get('id'))
        pid = identifier(event.get('payment_id'))
        if not isinstance(event.get('created'), (int, float)) or isinstance(event.get('created'), bool):
            raise Problem('Event needs numeric creation time')
        digest = hashlib.sha256(body).hexdigest()
        with self.tx() as db:
            source = db.execute('SELECT * FROM outbox WHERE id=?', (eid,)).fetchone()
            if not source or source['payment_id'] != pid or source['kind'] != event.get('type') or (source['created'] != event['created']):
                raise Problem('Event does not match a synthetic outbox record')
            old = db.execute('SELECT digest FROM inbox WHERE id=?', (eid,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise Problem('Event ID reused with different bytes')
                return {'duplicate': True, 'applied': False}
            db.execute('INSERT INTO inbox VALUES(?,?,?)', (eid, digest, time.time()))
            prior = db.execute('SELECT * FROM projections WHERE payment_id=?', (pid,)).fetchone()
            applied = not prior or event['created'] > prior['last_created']
            if applied:
                db.execute('INSERT OR REPLACE INTO projections VALUES(?,?,?)', (pid, event['type'], event['created']))
            db.execute('INSERT INTO timeline(payment_id,kind,detail,at) VALUES(?,?,?,?)', (pid, 'webhook.received', 'Projection updated' if applied else 'Older event ignored', time.time()))
            return {'duplicate': False, 'applied': applied}

    def snapshot(self):
        with self.db() as db:
            db.execute('BEGIN')
            payments = [dict(r) for r in db.execute('SELECT * FROM payments ORDER BY created DESC')]
            journal = [dict(r) for r in db.execute('SELECT * FROM journal ORDER BY seq')]
            debits = sum((r['debit'] for r in journal))
            credits = sum((r['credit'] for r in journal))
            return {'payments': payments, 'timeline': [dict(r) for r in db.execute('SELECT * FROM timeline ORDER BY seq DESC')], 'journal': journal, 'expected': [dict(r) for r in db.execute('SELECT * FROM expected ORDER BY ref')], 'outbox': [dict(r) for r in db.execute('SELECT o.*,i.id IS NOT NULL delivered FROM outbox o LEFT JOIN inbox i ON i.id=o.id ORDER BY o.created DESC')], 'projections': [dict(r) for r in db.execute('SELECT * FROM projections')], 'ledger': {'debits': debits, 'credits': credits, 'balanced': debits == credits}}

    def statement(self, scenario='clean'):
        rows = [{'reference': r['ref'], 'net_cents': r['net'], 'currency': 'USD'} for r in self.snapshot()['expected']]
        if scenario == 'mismatch' and rows:
            rows[0]['net_cents'] -= 100
        elif scenario == 'duplicate' and rows:
            rows.append(rows[0].copy())
        elif scenario == 'missing' and rows:
            rows.pop()
        elif scenario not in ('clean', 'mismatch', 'duplicate', 'missing'):
            raise Problem('Unknown statement scenario')
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=['reference', 'net_cents', 'currency'])
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue()

    def reconcile(self, text):
        if not isinstance(text, str) or len(text) > 500000:
            raise Problem('Statement must be CSV text under 500 KB')
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames != ['reference', 'net_cents', 'currency']:
            raise Problem('CSV columns must be reference,net_cents,currency')
        actual = {}
        issues = []
        count = 0
        statement_net = 0
        for (number, row) in enumerate(reader, 2):
            count += 1
            if count > 5000:
                raise Problem('Statement exceeds 5,000 rows')
            ref = row.get('reference')
            if not ref or None in row or row.get('currency') != 'USD' or (not re.fullmatch('-?[0-9]{1,12}', row.get('net_cents') or '')):
                issues.append({'reference': ref or '(missing)', 'reason': f'Invalid row {number}'})
                continue
            statement_net += int(row['net_cents'])
            if ref in actual:
                issues.append({'reference': ref, 'reason': 'Duplicate statement reference'})
            else:
                actual[ref] = int(row['net_cents'])
        expected = {r['ref']: r['net'] for r in self.snapshot()['expected']}
        matched = 0
        for (ref, amount) in expected.items():
            if ref not in actual:
                issues.append({'reference': ref, 'reason': 'Missing settlement row', 'expected': amount})
            elif actual[ref] != amount:
                issues.append({'reference': ref, 'reason': 'Net amount mismatch', 'expected': amount, 'actual': actual[ref], 'difference': actual[ref] - amount})
            else:
                matched += 1
        for ref in actual.keys() - expected.keys():
            issues.append({'reference': ref, 'reason': 'Unknown settlement reference', 'actual': actual[ref]})
        return {'passed': not issues, 'matched': matched, 'expected_rows': len(expected), 'statement_rows': count, 'issues': issues, 'expected_net': sum(expected.values()), 'statement_net': statement_net, 'scope': 'Synthetic statement comparison; no funds moved'}
