import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from payments.engine import Engine, Problem

class EngineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.e = Engine(self.path)
        self.seq = 0

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, action, **kwargs):
        self.seq += 1
        return self.e.command('key_' + str(self.seq), {'action': action, **kwargs})['payment']

    def create(self, **kwargs):
        return self.command('create', amount=12500, **kwargs)

    def capture(self):
        p = self.create()
        return self.command('capture', payment_id=p['id'], amount=10000)

    def test_lifecycle_and_journal(self):
        p = self.capture()
        p = self.command('refund', payment_id=p['id'], amount=2500)
        self.assertEqual(p['state'], 'partially_refunded')
        self.assertEqual(p['refunded'], 2500)
        s = self.e.snapshot()
        self.assertTrue(s['ledger']['balanced'])
        self.assertEqual(s['ledger']['debits'], 12820)
        self.assertEqual(sum((r['net'] for r in s['expected'])), 7180)

    def test_auth_no_postings(self):
        self.create()
        self.assertEqual(self.e.snapshot()['journal'], [])

    def test_decline(self):
        p = self.create(scenario='decline')
        self.assertEqual(p['state'], 'declined')
        with self.assertRaises(Problem):
            self.command('capture', payment_id=p['id'], amount=100)

    def test_authentication(self):
        p = self.create(scenario='authenticate')
        with self.assertRaises(Problem):
            self.command('capture', payment_id=p['id'], amount=100)
        self.assertEqual(self.command('authenticate', payment_id=p['id'])['state'], 'authorized')

    def test_timeout_recovery(self):
        p = self.create(scenario='timeout')
        self.assertEqual(self.command('recover', payment_id=p['id'])['state'], 'authorized')
        self.assertEqual(len(self.e.snapshot()['payments']), 1)

    def test_cancel(self):
        p = self.create()
        self.assertEqual(self.command('cancel', payment_id=p['id'])['state'], 'canceled')
        self.assertEqual(self.e.snapshot()['journal'], [])

    def test_final_partial_capture(self):
        p = self.capture()
        with self.assertRaises(Problem):
            self.command('capture', payment_id=p['id'], amount=100)

    def test_over_capture(self):
        p = self.create()
        with self.assertRaises(Problem):
            self.command('capture', payment_id=p['id'], amount=12501)
        self.assertFalse(self.e.snapshot()['journal'])

    def test_over_refund(self):
        p = self.capture()
        with self.assertRaises(Problem):
            self.command('refund', payment_id=p['id'], amount=10001)
        self.assertEqual(self.e.snapshot()['payments'][0]['refunded'], 0)

    def test_full_refund_multiple_parts(self):
        p = self.capture()
        self.command('refund', payment_id=p['id'], amount=2500)
        self.assertEqual(self.command('refund', payment_id=p['id'], amount=7500)['state'], 'refunded')
        with self.assertRaises(Problem):
            self.command('refund', payment_id=p['id'], amount=1)

    def test_duplicate_survives_restart(self):
        args = {'action': 'create', 'amount': 1000}
        a = self.e.command('durable_key', args)
        self.e = Engine(self.path)
        b = self.e.command('durable_key', args)
        self.assertEqual(a['payment']['id'], b['payment']['id'])
        self.assertTrue(b['duplicate'])
        self.assertEqual(len(self.e.snapshot()['payments']), 1)

    def test_idempotency_conflict(self):
        self.e.command('same_key', {'action': 'create', 'amount': 1000})
        with self.assertRaises(Problem):
            self.e.command('same_key', {'action': 'create', 'amount': 2000})

    def test_duplicate_refund_no_second_posting(self):
        p = self.capture()
        args = {'action': 'refund', 'payment_id': p['id'], 'amount': 2500}
        self.e.command('refund_key', args)
        before = self.e.snapshot()
        self.e.command('refund_key', args)
        self.assertEqual(before, self.e.snapshot())

    def test_concurrent_identical_requests(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            r = list(pool.map(lambda _: self.e.command('concurrent', {'action': 'create', 'amount': 1500}), range(16)))
        self.assertEqual(sum((not x['duplicate'] for x in r)), 1)

    def test_concurrent_capture_one_winner(self):
        p = self.create()

        def task(i):
            try:
                self.e.command('capture_' + str(i), {'action': 'capture', 'payment_id': p['id'], 'amount': 1000})
                return True
            except Problem:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            r = list(pool.map(task, range(8)))
        self.assertEqual(sum(r), 1)
        self.assertTrue(self.e.snapshot()['ledger']['balanced'])

    def test_amount_validation(self):
        for value in [True, 0, -1, 1.5, '100', 10000001, None]:
            with self.subTest(value=value):
                with self.assertRaises(Problem):
                    self.e.command('invalid_key', {'action': 'create', 'amount': value})

    def test_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.e.tx() as db:
                db.execute("INSERT INTO commands VALUES('x','x','x')")
                raise RuntimeError()
        with self.e.db() as db:
            self.assertIsNone(db.execute("SELECT * FROM commands WHERE key='x'").fetchone())

    def test_reconcile_clean(self):
        self.capture()
        self.assertTrue(self.e.reconcile(self.e.statement())['passed'])

    def test_reconcile_mismatch(self):
        self.capture()
        r = self.e.reconcile(self.e.statement('mismatch'))
        self.assertFalse(r['passed'])
        self.assertEqual(r['issues'][0]['difference'], -100)

    def test_reconcile_missing(self):
        self.capture()
        self.assertFalse(self.e.reconcile(self.e.statement('missing'))['passed'])

    def test_reconcile_duplicate_total(self):
        self.capture()
        r = self.e.reconcile(self.e.statement('duplicate'))
        self.assertFalse(r['passed'])
        self.assertEqual(r['statement_net'], r['expected_net'] * 2)

    def test_reconcile_unknown(self):
        self.assertFalse(self.e.reconcile('reference,net_cents,currency\nunknown,100,USD\n')['passed'])

    def test_reconcile_bad_rows(self):
        with self.assertRaises(Problem):
            self.e.reconcile('bad,columns\nx,y\n')
        self.assertFalse(self.e.reconcile('reference,net_cents,currency\nx,1.2,USD\n')['passed'])

    def event(self, row):
        return json.dumps({'id': row['id'], 'payment_id': row['payment_id'], 'type': row['kind'], 'created': row['created']}, sort_keys=True).encode()

    def test_event_dedup(self):
        self.create()
        body = self.event(self.e.snapshot()['outbox'][0])
        self.assertTrue(self.e.receive(body)['applied'])
        self.assertTrue(self.e.receive(body)['duplicate'])

    def test_out_of_order_event(self):
        self.capture()
        events = self.e.snapshot()['outbox']
        self.e.receive(self.event(events[0]))
        self.assertFalse(self.e.receive(self.event(events[-1]))['applied'])
        self.assertEqual(self.e.snapshot()['projections'][0]['last_event'], 'payment.captured')

    def test_event_must_match_outbox(self):
        self.create()
        e = json.loads(self.event(self.e.snapshot()['outbox'][0]))
        e['type'] = 'forged'
        with self.assertRaises(Problem):
            self.e.receive(json.dumps(e).encode())

    def test_terminal_channel(self):
        self.assertEqual(self.create(channel='terminal')['channel'], 'terminal')
if __name__ == '__main__':
    unittest.main()
