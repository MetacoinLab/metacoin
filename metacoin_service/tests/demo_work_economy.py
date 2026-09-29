"""Central demonstration of the core work economy (Order 08 §66) on a disposable instance: one FEASIBLE and one INFEASIBLE
determination contract go through terms → request → offer → award → execution → verification → acceptance → payment; the
negative contract's complete bundle, its restricted (collaborator) and public-ready projections and the offline verifier
report are written to OUT_DIR together with demonstration.json. Synthetic fixtures only; no real funds, nothing anchored.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.demo_work_economy OUT_DIR"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from metacoin_service.tests.journeys_economy import Journeys, energy_inputs, ROOT


def main(out_dir):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    j = Journeys(); record = {'schema': 'metacoin-work-demonstration/v1', 'started_at': int(time.time()), 'contracts': {}}
    try:
        j.start_api(); health = j.http.get('/api/health').json(); record['revision'] = health.get('revision')
        for label, outcome in (('positive', 'FEASIBLE'), ('negative', 'INFEASIBLE')):
            t, f, r, o, a = j.flow(outcome, 'DEMO_' + label.upper(), price=10)
            j.run_workers(2, 'demo-' + label); v = j.verify(a['id']); ev = j.evaluate(a['id']); st, d = j.decide(a['id'])
            st, s = j.pay(d['entitlement']['id']); view = j.aview(a['id'])
            receipts = j.call('get', '/api/v1/work/awards/' + a['id'] + '/receipts')[1]['items']
            record['contracts'][label] = {'terms_id': t['id'], 'terms_digest': f['digest'], 'request_id': r['id'], 'offer_id': o['id'], 'award_id': a['id'], 'job_id': view['milestones'][0]['job_id'],
                                          'evaluation': {k: ev[k] for k in ('decision_candidate', 'science', 'payment_class', 'payable_amount')}, 'trace': [(x['predicate'], x['result']) for x in ev['trace']],
                                          'decision': d['decision'], 'entitlement': d['entitlement']['id'], 'payment': {k: s.get(k) for k in ('state', 'final_amount', 'rail', 'scheme')},
                                          'dimensions': view['milestones'][0]['dimensions'], 'receipts': [{'id': x['id'], 'kind': x['kind']} for x in receipts]}
            if label == 'negative':
                pub = receipts[0]['public_key_hex']
                z = j.http.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=full' % a['id'], headers=j.H()); (out / 'contract-bundle-negative-full.zip').write_bytes(z.content)
                zr = j.http.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=disclosed' % a['id'], headers=j.H()); (out / 'contract-bundle-negative-disclosed.zip').write_bytes(zr.content)
                for aud in ('private', 'collaborator', 'public_ready'):
                    pj = j.call('post', '/api/v1/work/awards/%s/milestones/m1/projection' % a['id'], json={'audience': aud, 'sign': aud == 'public_ready'})[1]
                    (out / ('projection-%s.json' % aud)).write_text(json.dumps(pj, indent=1))
                env = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '/'), 'PYTHONPATH': str(ROOT)}
                for name in ('full', 'disclosed'):
                    p = subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'contract-bundle-negative-%s.zip' % name, '--trust-root', pub, '--json'], cwd=out, env=env, capture_output=True, text=True, timeout=180)
                    (out / ('verifier-report-%s.json' % name)).write_text(p.stdout or json.dumps({'stderr': p.stderr[-800:]}))
                    try:
                        rep = json.loads(p.stdout)
                        record['contracts'][label]['verifier_' + name] = {'exit': p.returncode, 'integrity': rep['integrity']['ok'], 'signer_trust': rep['signer_trust']['trusted'], 'replay': rep['scientific_replay'], 'offline_candidate': rep['acceptance_evaluation']['offline_candidate'], 'verdict': rep.get('verdict')}
                    except ValueError:
                        record['contracts'][label]['verifier_' + name] = {'exit': p.returncode, 'stderr': p.stderr[-300:]}
                record['contracts'][label]['signing_public_key'] = pub
        rep = j.call('post', '/api/v1/work/journal/replay', json={})[1]; record['journal'] = {'consistent': rep.get('consistent'), 'invariants': rep.get('invariants')}
        import hashlib
        base = {l.split()[1]: l.split()[0] for l in (ROOT / 'work/core-work-economy-session/artifacts/BASELINE_DIGESTS.txt').read_text().splitlines() if len(l.split()) == 2} if (ROOT / 'work/core-work-economy-session/artifacts/BASELINE_DIGESTS.txt').exists() else {}
        record['identity_and_ledger_unchanged'] = {k: hashlib.sha256((ROOT / k).read_bytes()).hexdigest() == v for k, v in base.items()} if base else 'baseline digests unavailable'
        record['what_this_shows'] = ['a verified INFEASIBLE determination is accepted under the frozen outcome-neutral policy and paid the same complete amount as the FEASIBLE one',
                                     'four independent dimensions (execution, science, acceptance, payment) per milestone; receipts of four kinds signed by the service key with custody labels',
                                     'the complete bundle replays the determination offline; the disclosed bundle reports the missing private evidence honestly',
                                     'projections declare what they omit; nothing is anchored to the protocol ledger and no base supply moves']
        record['what_this_does_not_show'] = ['price discovery or organizational independence (one operator plays every role on one host)', 'public settlement (the application-journal rail here; the private local chain in journeys 31–37)']
    finally:
        j.close()
    record['finished_at'] = int(time.time())
    (out / 'demonstration.json').write_text(json.dumps(record, indent=1, default=str))
    print(json.dumps({k: record['contracts'][k].get('dimensions') for k in record['contracts']}, indent=1))
    return 0 if all(c.get('decision') == 'accepted' and c.get('payment', {}).get('state') == 'settled' for c in record['contracts'].values()) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
