"""metacoin-provider: command line of the portable provider client.

  python -m metacoin_provider --base URL --signer URL --signer-token FILE enroll --admin-cred FILE --name NAME --ceiling N --out CRED
  python -m metacoin_provider --base URL --cred CRED offer-publish compute cpu-1 100000 1 --window-seconds 7200
  python -m metacoin_provider --base URL --cred CRED reservations | activate RID | deliver-compute RID --executor "python -m metacoin_service.lunar task" | settle RID
  python -m metacoin_provider --base URL --cred CRED --signer URL --signer-token FILE --actor NAME pay TO AMOUNT --request-id R
  python -m metacoin_provider --base URL --cred CRED receipts --roots roots.json | export --out record.json
  python -m metacoin_provider verify record.json --roots roots.json          (offline; no base, no credential)
  python -m metacoin_provider conformance                                      (versioned fixtures against this implementation)
"""
import argparse
import json
import shlex
import sys
import time

from .client import Provider
from .api import ApiError


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-provider'); ap.add_argument('--base'); ap.add_argument('--cred'); ap.add_argument('--signer'); ap.add_argument('--signer-token'); ap.add_argument('--actor')
    sub = ap.add_subparsers(dest='command', required=True)
    e = sub.add_parser('enroll'); e.add_argument('--admin-cred', required=True); e.add_argument('--name', required=True); e.add_argument('--ceiling', type=int, default=1000); e.add_argument('--out', required=True)
    r = sub.add_parser('register-work-provider'); r.add_argument('--admin-cred', required=True); r.add_argument('--principal-id', required=True); r.add_argument('--name', required=True); r.add_argument('--kinds', default='terrain_tile,route_feasibility')
    sub.add_parser('account'); sub.add_parser('market'); o = sub.add_parser('offers'); o.add_argument('--kind'); sub.add_parser('reservations'); x = sub.add_parser('reservation'); x.add_argument('rid')
    p = sub.add_parser('offer-publish'); p.add_argument('kind'); p.add_argument('resource_id'); p.add_argument('capacity', type=int); p.add_argument('price', type=int); p.add_argument('--window-seconds', type=int, default=7200); p.add_argument('--energy-mwh-per-unit', type=int, default=0); p.add_argument('--bandwidth', type=int); p.add_argument('--idempotency-key')
    a = sub.add_parser('activate'); a.add_argument('rid'); a.add_argument('--idempotency-key')
    d = sub.add_parser('deliver-compute'); d.add_argument('rid'); d.add_argument('--executor', required=True, help='command that reads a task JSON path and prints the product JSON'); d.add_argument('--task', default='{"kind": "terrain_tile", "source": "apollo11_32"}'); d.add_argument('--idempotency-key')
    d = sub.add_parser('deliver-contact'); d.add_argument('rid'); d.add_argument('bytes', type=int); d.add_argument('--idempotency-key')
    d = sub.add_parser('deliver-storage'); d.add_argument('rid'); d.add_argument('--idempotency-key')
    s = sub.add_parser('settle'); s.add_argument('rid'); s.add_argument('--idempotency-key')
    sub.add_parser('requests'); w = sub.add_parser('work-offer'); w.add_argument('request_id'); w.add_argument('price', type=int); w.add_argument('--asset', required=True); w.add_argument('--class', dest='cls', default='analytical'); w.add_argument('--idempotency-key')
    sub.add_parser('awards'); w = sub.add_parser('award'); w.add_argument('aid'); w = sub.add_parser('award-receipts'); w.add_argument('aid')
    y = sub.add_parser('pay'); y.add_argument('to'); y.add_argument('amount', type=int); y.add_argument('--request-id', required=True); y.add_argument('--obligation-id'); y.add_argument('--asset')
    ex = sub.add_parser('export'); ex.add_argument('--out'); rc = sub.add_parser('receipts'); rc.add_argument('--roots'); sub.add_parser('roots')
    v = sub.add_parser('verify'); v.add_argument('record'); v.add_argument('--roots', required=True)
    b = sub.add_parser('mission-bundle'); b.add_argument('mid'); b.add_argument('--out')
    sub.add_parser('conformance'); sub.add_parser('signing-pending'); sub.add_parser('sign-pending', help='actor-side signing agent: sign and submit my pending economic authorizations through my signer')
    pp = sub.add_parser('signer-policy'); pp.add_argument('--set', help='JSON policy to install at the signer for --actor')
    args = ap.parse_args(argv)
    try:
        out = run(args)
    except ApiError as exc:
        print(json.dumps({'error': True, 'status': exc.status, 'body': exc.body}, indent=1)); return 2
    print(json.dumps(out, indent=1, default=str)); return 0 if not (isinstance(out, dict) and out.get('ok') is False) else 1


def run(a):
    c = a.command
    if c == 'enroll':
        return Provider.enroll(a.base, a.admin_cred, a.signer, a.signer_token, a.name, a.ceiling, a.out)
    if c == 'register-work-provider':
        return Provider.register_work_provider(a.base, a.admin_cred, a.principal_id, a.name, a.kinds.split(','))
    if c == 'verify':
        with open(a.record) as f, open(a.roots) as g:
            return Provider.verify_offline(json.load(f), json.load(g))
    if c == 'conformance':
        from .conformance import run_fixtures
        return run_fixtures()
    P = Provider(a.base, a.cred, signer=a.signer, signer_token_file=a.signer_token, actor=a.actor)
    if c == 'account':
        return P.account()
    if c == 'market':
        return P.market()
    if c == 'offers':
        return P.offers(a.kind)
    if c == 'reservations':
        return P.reservations()
    if c == 'reservation':
        return P.reservation(a.rid)
    if c == 'offer-publish':
        t = int(time.time()); extra = {'energy_mwh_per_unit': a.energy_mwh_per_unit}
        if a.bandwidth:
            extra['bandwidth_bytes_per_s'] = a.bandwidth
        return P.publish_offer(a.kind, a.resource_id, a.capacity, a.price, t - 1, t + a.window_seconds, key=a.idempotency_key, **extra)
    if c == 'activate':
        return P.activate(a.rid, a.idempotency_key)
    if c == 'deliver-compute':
        return P.deliver_compute(a.rid, json.loads(a.task), shlex.split(a.executor), a.idempotency_key)
    if c == 'deliver-contact':
        return P.deliver_contact(a.rid, a.bytes, a.idempotency_key)
    if c == 'deliver-storage':
        return P.deliver_storage(a.rid, a.idempotency_key)
    if c == 'settle':
        return P.settle(a.rid, a.idempotency_key)
    if c == 'requests':
        return P.open_requests()
    if c == 'work-offer':
        return P.work_offer(a.request_id, a.price, a.asset, a.cls, key=a.idempotency_key)
    if c == 'awards':
        return P.awards()
    if c == 'award':
        return P.award(a.aid)
    if c == 'award-receipts':
        return P.award_receipts(a.aid)
    if c == 'pay':
        return P.pay(a.to, a.amount, a.request_id, a.obligation_id, a.asset)
    if c == 'roots':
        return P.roots_from_domain()
    if c == 'signing-pending':
        return P.signing_pending()
    if c == 'sign-pending':
        return P.sign_pending()
    if c == 'signer-policy':
        st, r = (P.signer.set_policy(a.actor, json.loads(a.set)) if a.set else P.signer.policy(a.actor)); return r
    if c == 'export':
        ex = P.export()
        if a.out:
            with open(a.out, 'w') as f:
                json.dump(ex, f)
            return {'written': a.out, 'receipts': len(ex['receipts']), 'intents': len(ex['intents'])}
        return ex
    if c == 'receipts':
        ex = P.export(); mine = P.my_receipts(ex)
        if a.roots:
            with open(a.roots) as f:
                roots = json.load(f)
            from . import verify as V
            return {'receipts': mine, 'verification': V.verify_receipts(mine, roots, ex['domain'])}
        return {'receipts': mine}
    if c == 'mission-bundle':
        b = P.mission_bundle(a.mid)
        if a.out:
            with open(a.out, 'w') as f:
                json.dump(b, f)
            return {'written': a.out, 'seal': b.get('seal')}
        return b
    raise SystemExit('unknown command')


if __name__ == '__main__':
    sys.exit(main())
