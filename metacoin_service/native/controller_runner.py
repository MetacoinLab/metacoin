"""Restart-safe mission controller runner (order 2026-10-23 group H). A separate process that drives one mission through the public
API under a fenced generation granted by the authoritative domain. On start it reconstructs the mission state from durable records
(continuity view), obtains a new generation (takeover) unless told to resume an existing one, lets the actor's signer answer
pending signing requests, and steps the controller. A superseded generation is refused by the authority (exit 3). If the
authority is unreachable the runner cannot grant itself a generation: it reports `authority_unreachable` (exit 4) and may only
continue with a generation it already holds. Signer unavailable: the step reports await_signature and the runner keeps waiting.

    python -m metacoin_service.native.controller_runner --base URL --cred FILE --mission ID --holder NAME [--generation N] [--signer URL --signer-token FILE --actor NAME] [--steps N] [--interval S] [--forge-generation N]
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def client(base, cred_file):
    token = json.load(open(cred_file))['token']
    def go(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}; h.update(headers or {})
        req = urllib.request.Request(base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            except ValueError:
                return e.code, {}
        except urllib.error.URLError as e:
            return 0, {'error': True, 'code': 'authority_unreachable', 'detail': str(e.reason)[:120]}
    return go


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-controller-runner'); ap.add_argument('--base', required=True); ap.add_argument('--cred', required=True); ap.add_argument('--mission', required=True); ap.add_argument('--holder', required=True)
    ap.add_argument('--generation', type=int, help='resume with an already granted generation instead of a takeover'); ap.add_argument('--forge-generation', type=int, help='TEST ONLY: present a generation the authority never granted'); ap.add_argument('--signer'); ap.add_argument('--signer-token'); ap.add_argument('--actor')
    ap.add_argument('--steps', type=int, default=20); ap.add_argument('--interval', type=float, default=1.0); ap.add_argument('--log'); a = ap.parse_args(argv)
    go = client(a.base, a.cred); log = []
    def emit(ev):
        ev['at'] = int(time.time()); log.append(ev); print(json.dumps(ev), flush=True)
        if a.log:
            with open(a.log, 'a') as f:
                f.write(json.dumps(ev) + '\n')
    st, cont = go('GET', '/api/v1/missions/%s/continuity' % a.mission)
    if st == 0:
        emit({'event': 'authority_unreachable', 'phase': 'reconstruct', 'detail': cont.get('detail')}); return 4
    if st != 200:
        emit({'event': 'reconstruct_failed', 'status': st, 'body': cont}); return 2
    emit({'event': 'reconstructed', 'state': cont['state'], 'active_generation': (cont.get('active_generation') or {}).get('generation'), 'pending_operations': len(cont['pending_operations']), 'pending_signing_requests': len(cont['pending_signing_requests']), 'safe_actions': [x['action'] for x in cont['safe_actions']], 'blocked': [(x['action'], (x.get('reason') or '')[:60]) for x in cont['blocked']][:5]})
    gen = a.forge_generation
    if gen is None:
        if a.generation is not None:
            gen = a.generation; emit({'event': 'resuming_generation', 'generation': gen})
        else:
            st, t = go('POST', '/api/v1/missions/%s/takeover' % a.mission, {'holder': a.holder, 'reason': 'runner start'})
            if st == 0:
                emit({'event': 'authority_unreachable', 'phase': 'takeover', 'note': 'no generation can be self-issued; stopping'}); return 4
            if st != 200:
                emit({'event': 'takeover_refused', 'status': st, 'body': t}); return 2
            gen = t['generation']; emit({'event': 'generation_granted', 'generation': gen, 'superseded': t.get('superseded')})
    else:
        emit({'event': 'presenting_forged_generation', 'generation': gen, 'note': 'test only'})
    signer = None
    if a.signer and a.actor:
        from .signer import SignerClient
        signer = SignerClient(a.signer, a.signer_token)
    for i in range(a.steps):
        if signer is not None:
            st, pend = go('GET', '/api/v1/native/signing/pending')
            for req in (pend.get('items') or []) if st == 200 else []:
                ss, sig = signer.sign(a.actor, req['message'], context=req['context'])
                if ss == 200:
                    s2, r2 = go('POST', '/api/v1/native/signing/requests/%s/submit' % req['id'], {'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}); emit({'event': 'signed', 'request': req['id'], 'kind': req['kind'], 'status': s2})
                else:
                    emit({'event': 'signer_refused_or_unreachable', 'request': req['id'], 'status': ss, 'code': sig.get('code')})
        st, s = go('POST', '/api/v1/missions/%s/step' % a.mission, {'generation': gen})
        if st == 0:
            emit({'event': 'authority_unreachable', 'phase': 'step', 'generation': gen, 'note': 'continuing only with the generation already held; nothing new granted'}); time.sleep(a.interval); continue
        if st == 409 and (s.get('detail') or {}).get('code') == 'stale_generation':
            emit({'event': 'stale_generation_refused', 'presented': gen, 'active': s['detail'].get('active'), 'active_holder': s['detail'].get('active_holder')}); return 3
        if st != 200:
            emit({'event': 'step_refused', 'status': st, 'body': {k: s.get(k) for k in ('code', 'detail')}}); time.sleep(a.interval); continue
        p = s.get('performed'); emit({'event': 'step', 'performed': p, 'state': s.get('state'), 'wait_reason': (s.get('wait_reason') or '')[:120] if not p else None})
        if s.get('state') in ('completed', 'cancelled', 'stopped'):
            emit({'event': 'done', 'state': s['state']}); return 0
        if p is None:
            time.sleep(a.interval)
    emit({'event': 'steps_exhausted', 'generation': gen}); return 0


if __name__ == '__main__':
    sys.exit(main())
