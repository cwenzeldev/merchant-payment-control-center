"""Executable synthetic walkthrough with evidence; no external provider calls."""
import json
from pathlib import Path
import tempfile
from payments.engine import Engine


def main():
    with tempfile.TemporaryDirectory() as tmp:
        engine=Engine(Path(tmp)/'demo.sqlite3')
        created=engine.command('demo_authorize',{'action':'create','amount':12500,'scenario':'approve'})
        pid=created['payment']['id']
        engine.command('demo_capture',{'action':'capture','payment_id':pid,'amount':10000})
        refund={'action':'refund','payment_id':pid,'amount':2500}
        engine.command('demo_refund',refund)
        duplicate=engine.command('demo_refund',refund)
        state=engine.snapshot()
        newest=state['outbox'][0]
        event=json.dumps({'id':newest['id'],'payment_id':pid,'type':newest['kind'],'created':newest['created']},sort_keys=True).encode()
        first=engine.receive(event);second=engine.receive(event)
        clean=engine.reconcile(engine.statement())
        broken=engine.reconcile(engine.statement('mismatch'))
        assert duplicate['duplicate'] and second['duplicate']
        assert state['ledger']['balanced'] and clean['passed'] and not broken['passed']
        print(json.dumps({'scope':'Synthetic local simulation; no Stripe calls','payment':state['payments'][0],
            'duplicate_refund':duplicate['duplicate'],'event_first_delivery':first,'event_redelivery':second,
            'ledger':state['ledger'],'matching_statement':clean,'mismatched_statement':broken},indent=2))

if __name__=='__main__':main()
