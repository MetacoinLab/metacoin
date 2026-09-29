"""Verifier challenge packages (Order 08 §76.3): a recipient of a receipt proposes a BOUNDED, reproducible counterexample —
the same declared operation run on inputs of the challenger's choosing with an asserted outcome — without any access to the
contract's private evidence or inputs. The service executes the counterexample as an ordinary job under the challenger's
own authority, records what happened, and packages statement + inputs + result under the service signing key so the
package can be attached to a dispute or verified offline. A reproduced counterexample says what the METHOD does on those
inputs; it never rewrites the receipt it challenges."""
import copy
import io
import json
import secrets
import zipfile

from .. import history, metering
from ..db import now
from ..errors import ServiceError
from experiments.private_receipts.receipt import canonical
from .board import _sign, _terms

SCHEMA = 'metacoin-verifier-challenge/v1'
MAX_INPUT_BYTES = 32 * 1024
MAX_OPEN_PER_CHALLENGER = 4
ASSERTABLE = ('FEASIBLE', 'INFEASIBLE', 'INDETERMINATE', 'EXACT_MATCH', 'VERIFIED')


class Challenges:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def row(self, db, principal, chid):
        r = db.execute('SELECT * FROM work_challenges WHERE id=? AND workspace=?', (chid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'challenge')
        return r

    def open(self, db, principal, rid, body):
        principal.require('work:dispute')
        rec = db.execute('SELECT * FROM work_receipts WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if rec is None:
            raise ServiceError('NOT_FOUND', 'receipt')
        if rec['kind'] not in ('verification', 'acceptance', 'provider'):
            raise ServiceError('CONFLICT', {'code': 'receipt_kind', 'note': 'settlement receipts carry no scientific claim to challenge'})
        if type(body) is not dict or set(body) - {'claim', 'counterexample_inputs', 'asserted_outcome'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['claim', 'counterexample_inputs', 'asserted_outcome']})
        claim = body.get('claim'); inputs = body.get('counterexample_inputs'); asserted = body.get('asserted_outcome')
        if type(claim) is not str or not 1 <= len(claim) <= 1000 or type(inputs) is not dict or asserted not in ASSERTABLE:
            raise ServiceError('VALIDATION', {'code': 'challenge', 'asserted_outcome': list(ASSERTABLE)})
        if len(canonical(inputs)) > MAX_INPUT_BYTES:
            raise ServiceError('PAYLOAD_TOO_LARGE', {'code': 'counterexample_inputs', 'max_bytes': MAX_INPUT_BYTES})
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (rec['award_id'],)).fetchone(); t, terms = _terms(db, award['terms_id'])
        kind = terms['operation']['kind']
        st = json.loads(rec['statement_json'])
        open_n = db.execute("SELECT COUNT(*) FROM work_challenges WHERE challenger_id=? AND state='executing'", (principal.id,)).fetchone()[0]
        if open_n >= MAX_OPEN_PER_CHALLENGER:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'open_challenges', 'max': MAX_OPEN_PER_CHALLENGER, 'note': 'a challenger runs a bounded number of counterexamples at a time'})
        rrow = db.execute("SELECT id FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL ORDER BY created_at LIMIT 1", (principal.workspace,)).fetchone()
        pol = {'reviewer_id': rrow['id'] if rrow else None, 'accepted_outcomes': ['FEASIBLE', 'INFEASIBLE', 'INDETERMINATE'], 'disclose_outcome': True, 'expires_in_seconds': 7 * 86400, 'execution_locations': ['*']}
        # the counterexample runs as an ordinary bounded job created by the SERVICE on the challenger's behalf: the challenger's
        # role (provider, reviewer, requester) carries work:dispute, not general contract authority; attribution stays with the challenger
        runner = copy.copy(principal); runner.role = 'owner'; runner.scope = None
        cid = self.svc.contracts.create_draft(db, runner, kind=kind, title=('challenge of ' + rid)[:128], inputs=inputs, policy=pol, datasets=self.svc.datasets)
        self.svc.contracts.freeze(db, runner, cid)
        jid = self.svc.jobs.submit(db, runner, cid)
        c = db.execute('SELECT * FROM contracts WHERE id=?', (cid,)).fetchone()
        chid = 'wch_' + secrets.token_hex(8)
        disclosed = {'receipt_id': rid, 'kind': rec['kind'], 'terms_digest': st.get('terms_digest'), 'award_id': rec['award_id'], 'milestone': st.get('milestone'), 'claims_disclosed': {k: v for k, v in (st.get('claims') or {}).items() if k in ('class', 'outcome', 'scope', 'result_commitment', 'claim', 'decision', 'science', 'payment_class')}}
        db.execute('INSERT INTO work_challenges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (chid, principal.workspace, rid, rec['award_id'], principal.id, claim, json.dumps(inputs), asserted, kind, cid, jid, 'executing', None, now()))
        history.record(db, principal.workspace, principal.id, 'work.challenge', 'work_challenge', chid, {'receipt_id': rid, 'kind': kind, 'asserted_outcome': asserted, 'job_id': jid, 'private_evidence_accessed': False})
        return self.view(db, principal, chid)

    def view(self, db, principal, chid):
        principal.require('work:read')
        r = self.row(db, principal, chid)
        job = db.execute('SELECT * FROM jobs WHERE id=?', (r['job_id'],)).fetchone(); c = db.execute('SELECT * FROM contracts WHERE id=?', (r['contract_id'],)).fetchone()
        rec = db.execute('SELECT * FROM work_receipts WHERE id=?', (r['receipt_id'],)).fetchone(); st = json.loads(rec['statement_json']) if rec else {}
        if job['state'] == 'succeeded':
            reproduced = job['outcome'] == r['asserted_outcome']
            conclusion = 'counterexample_reproduced' if reproduced else 'counterexample_not_reproduced'
            state = 'concluded'
        elif job['state'] in ('failed', 'cancelled'):
            conclusion, state = 'counterexample_failed_to_execute', 'concluded'
        else:
            conclusion, state = 'pending', 'executing'
        if state != r['state']:
            db.execute('UPDATE work_challenges SET state=?, result_json=? WHERE id=?', (state, json.dumps({'conclusion': conclusion, 'job_state': job['state'], 'outcome': job['outcome'], 'evidence_root': job['evidence_root']}), chid))
        return {'schema': SCHEMA, 'id': chid, 'receipt_id': r['receipt_id'], 'award_id': r['award_id'], 'challenger_id': r['challenger_id'], 'claim': r['claim'], 'operation_kind': r['kind'], 'asserted_outcome': r['asserted_outcome'],
                'counterexample': {'contract_id': r['contract_id'], 'input_root': c['input_root'], 'contract_digest': c['contract_digest'], 'job_id': r['job_id'], 'job_state': job['state'], 'outcome': job['outcome'], 'evidence_root': job['evidence_root']},
                'receipt_disclosed': {'kind': rec['kind'] if rec else None, 'terms_digest': st.get('terms_digest'), 'claims': {k: v for k, v in (st.get('claims') or {}).items() if k in ('class', 'outcome', 'scope', 'result_commitment', 'claim', 'decision', 'science')}},
                'state': state, 'conclusion': conclusion,
                'meaning': {'counterexample_reproduced': 'the declared method produced the asserted outcome on the challenger\'s inputs; this is evidence about the METHOD on those inputs, not a rewrite of the receipt (whose inputs differ: %s vs %s)' % ((st.get('claims') or {}).get('result_commitment', 'commitment withheld')[:16] if isinstance((st.get('claims') or {}).get('result_commitment'), str) else 'commitment', c['input_root'][:16]),
                            'counterexample_not_reproduced': 'the method did not produce the asserted outcome on these inputs; the challenge does not support the claim',
                            'counterexample_failed_to_execute': 'the counterexample did not execute (see the job error); no scientific conclusion',
                            'pending': 'the counterexample job has not concluded'}[conclusion],
                'private_evidence_accessed': False, 'attach': 'POST /work/disputes/{id}/evidence {"challenge_id": "%s"} within an open dispute' % chid, 'created_at': r['created_at']}

    def list(self, db, principal, award_id=None):
        principal.require('work:read')
        rows = db.execute('SELECT id FROM work_challenges WHERE workspace=?' + (' AND award_id=?' if award_id else '') + ' ORDER BY rowid', (principal.workspace,) + ((award_id,) if award_id else ())).fetchall()
        return [self.view(db, principal, r['id']) for r in rows]

    def package(self, db, principal, chid):
        """Signed zip: statement (this view), the counterexample inputs, the job summary and the receipt's disclosed statement."""
        v = self.view(db, principal, chid)
        if v['state'] != 'concluded':
            raise ServiceError('CONFLICT', {'code': 'challenge_pending'})
        r = self.row(db, principal, chid); job = db.execute('SELECT * FROM jobs WHERE id=?', (r['job_id'],)).fetchone(); rec = db.execute('SELECT * FROM work_receipts WHERE id=?', (r['receipt_id'],)).fetchone()
        files = {'challenge.json': canonical({k: v[k] for k in ('schema', 'id', 'receipt_id', 'award_id', 'challenger_id', 'claim', 'operation_kind', 'asserted_outcome', 'counterexample', 'conclusion', 'private_evidence_accessed')}),
                 'counterexample-inputs.json': canonical(json.loads(r['inputs_json'])),
                 'result.json': canonical({'job_id': job['id'], 'state': job['state'], 'outcome': job['outcome'], 'evidence_root': job['evidence_root'], 'summary': json.loads(job['summary_json']) if job['summary_json'] else None}),
                 'receipt.json': canonical({'statement': json.loads(rec['statement_json']), 'signature_hex': rec['signature_hex'], 'key_id': rec['key_id']})}
        import hashlib
        manifest = {'schema': SCHEMA + '/package', 'challenge_id': chid, 'file_sha256': {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}, 'issued_at': now(), 'note': 'private evidence of the challenged contract is not included and was not read'}
        msg, sig, kid = _sign(self.settings, db, manifest)
        pub = metering.ensure_service_key(self.settings, db)
        files['manifest.json'] = msg if isinstance(msg, bytes) else canonical(manifest)
        files['statement.json'] = canonical({'manifest_sha256': hashlib.sha256(files['manifest.json']).hexdigest(), 'signature': sig, 'key_id': kid, 'public_key': pub})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for n, b in sorted(files.items()):
                z.writestr(n, b)
        history.record(db, principal.workspace, principal.id, 'work.challenge', 'work_challenge', chid, {'packaged': True})
        return buf.getvalue()
