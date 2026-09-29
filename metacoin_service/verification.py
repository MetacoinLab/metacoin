"""Independent verification as a service: audit jobs over immutable completed results.

Classes (each states a different claim; none collapses into a generic "verified" flag):
  full_exact         every item recomputed by an independent reference and compared exactly
  full_reference     the whole result recomputed/re-evaluated by a reference within a bound tolerance or scope
  analytical         invariants and closed-form cases only (cheap; catches structural corruption, not every error)
  sampled_reference  a bound challenge (server-side random seed recorded after the result was committed) selects
                     items for reference recomputation without replacement; the statement names the scope
  replica            the same bound request re-run on another permitted backend by the ordinary compute path;
                     disagreement is a first-class disputed record, never averaged or hidden

Shared code is declared per kind: parameter expansion (compute.inputs.scenario), array codecs, and the initial-field
constructor are shared with the production path; the numerical kernels are not. Non-compute science kinds have no
second implementation: their recomputation is a regression check by the same implementation and is labelled so.
The signed statement is issued with the service signing key; a public projection contains commitments and counts,
never private values."""
import hashlib
import json
import secrets

from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import acceptance
from . import auth, crypto, history, metering, science, temporal
from .compute import container, inputs as compute_inputs, manifests as compute_manifests, npy, reference, verify as compute_verify
from .compute.kernels import TEMPORAL_COLUMNS, OUTCOME_CODES, heat_initial_field
from .db import now
from .errors import ServiceError

CLASSES = ('full_exact', 'full_reference', 'analytical', 'sampled_reference', 'replica')
STATEMENT_SCHEMA = 'metacoin-verification-statement/v1'
CHALLENGE_POLICY = 'verification-challenge/v1: seed = 16 server-side random bytes drawn after the result commitment; item i ranks by sha256(seed || result_commitment || i); the first k ranks are audited (without replacement)'
SUPPORT = {
    'temporal_batch': {'full_exact': 'every scenario recomputed by temporal-energy/v1 (reference verifier) and compared exactly on all %d columns' % len(TEMPORAL_COLUMNS),
                       'sampled_reference': 'challenge-selected scenarios recomputed by temporal-energy/v1 and compared exactly', 'analytical': 'row invariants (outcome codes, margin ordering, bounds consistency)',
                       'replica': 'same bound request on the other backend; results.npy must be byte-identical'},
    'monte_carlo_reliability': {'full_reference': 'every audited sample (regenerated parameters stored by the producer) re-evaluated by temporal-energy/v1; counts and interval recomputed; the sampler mapping is NOT independently regenerated',
                                'analytical': 'count consistency and Wilson interval recomputation', 'replica': 'same bound request on the other backend; event counts and audit must be identical'},
    'heat_diffusion': {'full_reference': 'scalar pure-Python FTCS from the initial condition (bounded cell-steps) within tolerance', 'analytical': 'invariants plus the closed-form eigenmode solution when the initial field is a single sine mode',
                       'sampled_reference': 'last step recomputed from the stored penultimate field (scope: one transition)', 'replica': 'same bound request on the other backend within the manifest tolerance'},
    'calibration_fit': {'full_reference': 'independent Householder QR refit of the training design compared through predictions'},
    'resource_plan': {'full_reference': 'stored assignments replayed by the exact integer simulator (objective, margins, trajectory, alternatives); solver status consistency; exhaustive oracle re-run on small instances',
                      'analytical': 'status/summary/plan consistency and reserve invariants of the stored trajectory without re-solving'},
    'temporal_energy': {'full_exact': 'recomputation by temporal.analyze (same implementation: regression check, not independent)'},
    'energy_audit': {'full_exact': 'acceptance.audit over the input and evidence vaults (same implementation: regression check)'},
    'safe_runtime': {'full_exact': 'recomputation by science.safe_runtime (same implementation)'}, 'plan_comparison': {'full_exact': 'recomputation by science.compare_plans (same implementation)'},
    'task_selection': {'full_exact': 'recomputation by science.select_tasks (same implementation)'},
    'legacy_task_replay': {'full_exact': 'frozen task recomputed by its registered implementation and compared exactly with the producer hash and the public ledger registration (legacy exact rule)'},
}
MAX_WORK = {'full_exact': 200_000, 'full_reference': 2_000_000, 'sampled_reference': 4096, 'analytical': 10 ** 9, 'replica': 10 ** 9}
SHARED_CODE = {'temporal_batch': ['compute.inputs.scenario (parameter expansion)', 'compute.npy (array codec)'], 'monte_carlo_reliability': ['compute.inputs.apply_variation', 'stored samples_audit.json (producer-generated parameters)'],
               'heat_diffusion': ['compute.kernels.heat_initial_field (initial condition constructor)', 'compute.npy'], 'calibration_fit': ['compute.calibration.design/standardize (shared conventions)'], 'resource_plan': ['compute.resource_plan.simulate (the declared model; independent of the MILP encoding)']}


class Verification:
    def __init__(self, store, settings, contracts, jobs):
        self.store, self.settings, self.contracts, self.jobs = store, settings, contracts, jobs

    # ---- request ---------------------------------------------------------------------------------
    def _target(self, db, principal, job_id):
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
        if job is None:
            raise ServiceError('NOT_FOUND', 'job')
        if job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'target job has no committed result')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        if principal.role == 'reviewer' and contract['reviewer_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'not the designated reviewer for this contract')
        return job, contract

    def _work_estimate(self, db, job, contract, cls, params):
        kind = job['kind']
        run = db.execute('SELECT work_total FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone()
        total = run['work_total'] if run else 1
        if cls == 'sampled_reference':
            k = params.get('sample_count', 64)
            return min(k, total), 'reference evaluations'
        if cls == 'analytical':
            return 1, 'invariant pass'
        if cls == 'replica':
            return total, 'work units of a second compute job (billed as ordinary compute)'
        if kind == 'heat_diffusion' and cls == 'full_reference':
            inputs = self._inputs(db, job, contract)
            return inputs['nx'] * inputs['ny'] * inputs['steps'], 'cell-steps (scalar reference)'
        return total, 'reference evaluations'

    def _inputs(self, db, job, contract):
        vault = self.store.load_json(db, contract['input_artifact_id'], job['workspace'])
        return {f['name']: f['value'] for f in vault['fields']}['inputs']

    def preview(self, db, principal, job_id, cls, params=None):
        principal.require('verification:submit')
        params = params or {}
        job, contract = self._target(db, principal, job_id)
        if cls not in CLASSES or cls not in SUPPORT.get(job['kind'], {}):
            raise ServiceError('VALIDATION', {'code': 'class_unsupported_for_kind', 'kind': job['kind'], 'supported': sorted(SUPPORT.get(job['kind'], {}))})
        if type(params) is not dict or set(params) - {'sample_count', 'backend'}:
            raise ServiceError('VALIDATION', 'params: {sample_count?, backend?}')
        if 'sample_count' in params and (type(params['sample_count']) is not int or not 1 <= params['sample_count'] <= MAX_WORK['sampled_reference']):
            raise ServiceError('VALIDATION', 'sample_count: 1..%d' % MAX_WORK['sampled_reference'])
        if cls == 'replica':
            run = db.execute('SELECT selected_backend FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()
            man = compute_manifests.MANIFESTS.get(job['kind'])
            other = [d for d in (man['devices'] if man else []) if d != (run['selected_backend'] if run else None)]
            backend = params.get('backend') or (other[0] if other else None)
            if backend is None or backend not in (man['devices'] if man else []) or backend == (run['selected_backend'] if run else None):
                raise ServiceError('VALIDATION', {'code': 'no_other_backend', 'ran_on': run['selected_backend'] if run else None, 'devices': man['devices'] if man else []})
            params['backend'] = backend
        work, unit = self._work_estimate(db, job, contract, cls, params)
        affordable = work <= MAX_WORK[cls]
        return {'target_job_id': job_id, 'kind': job['kind'], 'class': cls, 'params': params, 'claim': SUPPORT[job['kind']][cls], 'shared_code': SHARED_CODE.get(job['kind'], ['same implementation']),
                'estimated_work': work, 'work_unit': unit, 'max_work': MAX_WORK[cls], 'affordable': affordable, 'price': 'zero-price service on this instance (verification_audit); replica compute is billed as ordinary compute',
                'independence': 'same host, same operator: evidence against accidental error and corrupted outputs; not organizational independence'}

    def request(self, db, principal, job_id, cls, params=None):
        pv = self.preview(db, principal, job_id, cls, params)
        if not pv['affordable']:
            raise ServiceError('CONFLICT', {'code': 'verification_budget_exceeded', 'estimated_work': pv['estimated_work'], 'max_work': pv['max_work'], 'action': 'choose sampled_reference or analytical'})
        job, contract = self._target(db, principal, job_id)
        evidence = self.store.load_json(db, job['evidence_artifact_id'], job['workspace'])
        commitment = job['evidence_root']
        vid = 'vf_' + secrets.token_hex(6)
        challenge = None
        if cls == 'sampled_reference':
            challenge = {'policy': CHALLENGE_POLICY, 'seed': secrets.token_hex(16), 'result_commitment': commitment, 'sample_count': pv['params'].get('sample_count', 64), 'drawn_at': now(), 'drawn_by': 'service (server-side CSPRNG), not the producer'}
        db.execute('INSERT INTO verification_jobs (id, workspace, target_job_id, requested_by, class, params_json, state, preview_json, challenge_json, result_commitment, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (vid, principal.workspace, job_id, principal.id, cls, json.dumps(pv['params']), 'queued', json.dumps(pv), json.dumps(challenge) if challenge else None, commitment, now()))
        elevated = _elevated(principal, {'contract:create', 'contract:freeze', 'job:submit'})
        if cls == 'replica':
            inputs = dict(self._inputs(db, job, contract), device_policy={'cpu': 'cpu', 'cuda': 'gpu'}[pv['params']['backend']])
            rcid = self.contracts.create_draft(db, elevated, kind=job['kind'], title='replica of ' + job_id, inputs=inputs, policy={'reviewer_id': contract['reviewer_id']})
            self.contracts.freeze(db, elevated, rcid)
            rjid = self.jobs.submit(db, elevated, rcid)
            db.execute("UPDATE verification_jobs SET replica_job_id=?, state='awaiting_replica' WHERE id=?", (rjid, vid))
        else:
            acid = self.contracts.create_draft(db, elevated, kind='verification_audit', title='audit ' + job_id, inputs={'schema': 'verification-audit-input/v1', 'verification_id': vid, 'target_job_id': job_id, 'class': cls, 'params': pv['params']},
                                               policy={'reviewer_id': contract['reviewer_id']})
            self.contracts.freeze(db, elevated, acid)
            ajid = self.jobs.submit(db, elevated, acid)
            db.execute('UPDATE verification_jobs SET audit_job_id=? WHERE id=?', (ajid, vid))
        from .datasets import add_edge
        add_edge(db, principal.workspace, 'job', job_id, 'verification', vid, 'audited_by')
        if (params or {}).get('policy_id'):
            add_edge(db, principal.workspace, 'verification_policy', params['policy_id'], 'verification', vid, 'used_input')
        history.record(db, principal.workspace, principal.id, 'verification.requested', 'verification', vid, {'target_job_id': job_id, 'class': cls, 'params': pv['params'], 'challenge': bool(challenge)})
        return self.view(db, principal, vid)

    # ---- views ----------------------------------------------------------------------------------
    def row(self, db, principal, vid):
        r = db.execute('SELECT * FROM verification_jobs WHERE id=? AND workspace=?', (vid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'verification')
        return r

    def view(self, db, principal, vid):
        principal.require('job:read')
        r = self.row(db, principal, vid)
        contract = db.execute('SELECT reviewer_id FROM contracts WHERE id=(SELECT contract_id FROM jobs WHERE id=?)', (r['target_job_id'],)).fetchone()
        private = principal.can('job:read_private') or (principal.role == 'reviewer' and contract and contract['reviewer_id'] == principal.id)
        out = {'id': vid, 'target_job_id': r['target_job_id'], 'class': r['class'], 'params': json.loads(r['params_json']), 'state': r['state'], 'requested_by': r['requested_by'], 'audit_job_id': r['audit_job_id'],
               'replica_job_id': r['replica_job_id'], 'result_commitment': r['result_commitment'], 'created_at': r['created_at'], 'finished_at': r['finished_at'], 'preview': json.loads(r['preview_json']),
               'statement': json.loads(r['statement_json']) if r['statement_json'] else None, 'signature_hex': r['signature_hex'], 'key_id': r['key_id'],
               'resolution': json.loads(r['resolution_json']) if r['resolution_json'] else None, 'resolved_by': r['resolved_by']}
        if private:
            out['challenge'] = json.loads(r['challenge_json']) if r['challenge_json'] else None
            out['result'] = json.loads(r['result_json']) if r['result_json'] else None
            out['dispute'] = json.loads(r['dispute_json']) if r['dispute_json'] else None
        else:
            res = json.loads(r['result_json']) if r['result_json'] else None
            out['result'] = {'outcome': res.get('outcome'), 'checked': res.get('checked'), 'total': res.get('total')} if res else None
        return out

    def list(self, db, principal, job_id=None):
        principal.require('job:read')
        sql, args = 'SELECT id FROM verification_jobs WHERE workspace=?', [principal.workspace]
        if job_id:
            sql += ' AND target_job_id=?'; args.append(job_id)
        return [self.view(db, principal, r['id']) for r in db.execute(sql + ' ORDER BY created_at DESC LIMIT 100', args).fetchall()]

    def projection(self, db, principal, vid):
        """Public projection: the signed statement, signature and issuer public key (no private values)."""
        principal.require('job:read')
        r = self.row(db, principal, vid)
        if not r['statement_json']:
            raise ServiceError('CONFLICT', 'no statement yet (state %s)' % r['state'])
        pub = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()['value']
        return {'statement': json.loads(r['statement_json']), 'signature_hex': r['signature_hex'], 'public_key_hex': pub, 'key_id': r['key_id'],
                'verify_with': 'POST /api/v1/verification/verify-statement', 'meaning': 'authenticates that this service made the bound statement; it does not let a reader recompute private science'}

    @staticmethod
    def verify_statement(db, bundle, expected=None):
        if type(bundle) is not dict or not all(k in bundle for k in ('statement', 'signature_hex', 'public_key_hex')) or type(bundle['statement']) is not dict:
            raise ServiceError('VALIDATION', 'bundle: {statement, signature_hex, public_key_hex}')
        st = bundle['statement']
        trusted = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()
        known = trusted is not None and trusted['value'] == bundle['public_key_hex']
        try:
            ok = crypto.verify(bundle['public_key_hex'], merkle.canonical(st), bundle['signature_hex'])
        except Exception:
            ok = False
        out = {'signature_valid': bool(ok), 'issuer_trusted_by_this_service': known, 'schema_ok': st.get('schema') == STATEMENT_SCHEMA, 'outcome': st.get('outcome') if ok else None, 'claim': st.get('claim') if ok else None,
               'scope': st.get('scope') if ok else None, 'bindings_match': None, 'sufficient_for_current_policy': None}
        if ok and expected:
            mism = {k: [st.get(k), v] for k, v in expected.items() if st.get(k) != v}
            out['bindings_match'] = not mism; out['binding_mismatches'] = mism
        if ok:
            out['sufficient_for_current_policy'] = st.get('statement_version') == STATEMENT_SCHEMA and st.get('outcome') == 'passed'
        return out

    # ---- execution (worker) -----------------------------------------------------------------------
    def digest(self):
        return implementation_digest()

    def compute_audit(self, db, worker, job):
        """Load the bound target and run the audit (read-only; no writes). Returns (vrow, result, target, tcontract)."""
        contract, spec = worker._spec(db, job)
        inputs = spec['inputs']
        vrow = db.execute('SELECT * FROM verification_jobs WHERE id=? AND audit_job_id=?', (inputs['verification_id'], job['id'])).fetchone()
        if vrow is None:
            raise ServiceError('CONFLICT', 'verification record not bound to this audit job')
        target = db.execute('SELECT * FROM jobs WHERE id=?', (vrow['target_job_id'],)).fetchone()
        tcontract = db.execute('SELECT * FROM contracts WHERE id=?', (target['contract_id'],)).fetchone()
        if target['evidence_root'] != vrow['result_commitment']:
            raise ServiceError('CONFLICT', 'target result commitment changed since the audit was requested')
        tinputs = self._inputs(db, target, tcontract)
        evidence = self.store.load_json(db, target['evidence_artifact_id'], target['workspace'])
        values = acceptance.full_values(evidence, evidence['receipt']['root'])
        cls, params = vrow['class'], json.loads(vrow['params_json'])
        challenge = json.loads(vrow['challenge_json']) if vrow['challenge_json'] else None
        kind = target['kind']
        run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (target['id'],)).fetchone()
        files = None
        if run is not None:
            if not run['output_artifact_id']:
                raise ServiceError('CONFLICT', 'target has no output artifact')
            files = container.unpack(self.store.load(db, run['output_artifact_id'], target['workspace']))
            commitments = values['output_commitments']
            bad = [n for n in commitments if hashlib.sha256(files.get(n, b'')).hexdigest() != commitments[n]]
            if bad:
                return vrow, {'outcome': 'failed', 'checked': 0, 'total': 0, 'checks': [{'check': 'output_commitment', 'ok': False, 'detail': {'files': bad}}],
                              'statement': 'stored outputs do not match the committed output digests: the result was altered after commitment'}, target, tcontract
        if kind == 'energy_audit':
            # WorkContract v0 determination: the full private audit recomputes every hidden field of the evidence vault from the
            # input vault under the frozen contract (same implementation: a regression check, labelled so in SUPPORT)
            doc = merkle.parse(tcontract['contract_json'])
            ivault = self.store.load_json(db, tcontract['input_artifact_id'], target['workspace'])
            try:
                audited = acceptance.audit(doc, tcontract['contract_digest'], ivault, evidence)
                result = {'outcome': 'passed', 'checked': 1, 'total': 1, 'checks': [{'check': 'full_private_recomputation', 'ok': True, 'detail': {'scientific_outcome': audited['scientific_outcome']}}],
                          'coverage': 'every committed evidence field recomputed', 'statement': SUPPORT['energy_audit']['full_exact']}
            except merkle.Invalid as exc:
                result = {'outcome': 'failed', 'checked': 1, 'total': 1, 'checks': [{'check': 'full_private_recomputation', 'ok': False, 'detail': str(exc)[:200]}], 'statement': SUPPORT['energy_audit']['full_exact']}
        else:
            result = audit(kind, cls, tinputs, files, values['result'], params, challenge, run)
        from .economy.ops import fault as _fault
        _fault(db, self.settings, 'verifier_completion')
        fault = db.execute("SELECT value FROM meta WHERE key=?", ('fault:verification_fail:' + target['id'],)).fetchone() if self.settings.limits.get('test_hooks') else None
        if fault is not None:
            result = {'outcome': 'failed', 'checked': result.get('checked', 0), 'total': result.get('total', 0), 'checks': result.get('checks', []) + [{'check': 'fault_injection', 'ok': False, 'detail': 'FAULT INJECTED (test hook): forced verification failure'}],
                      'statement': 'FAULT INJECTED (test hook, disposable instance): the verification outcome was forced to failed; not evidence about the result'}
        return vrow, result, target, tcontract

    def finish_audit(self, db, vrow, result, target, tcontract):
        return self._finish(db, vrow, result, target, tcontract)

    def _finish(self, db, vrow, result, target, tcontract):
        outcome = result['outcome']
        state = {'passed': 'passed', 'failed': 'failed', 'incomplete': 'incomplete'}[outcome]
        statement = self._statement(db, vrow, result, target, tcontract)
        msg = merkle.canonical(statement)
        pub = metering.ensure_service_key(self.settings, db)
        sig = crypto.sign(crypto.load_signing_key(self.settings.keys_dir / 'service.ed25519'), msg)
        db.execute('UPDATE verification_jobs SET state=?, result_json=?, statement_json=?, signature_hex=?, key_id=?, finished_at=? WHERE id=?',
                   (state, json.dumps(_exactable(result)), msg.decode(), sig, crypto.key_id_for(pub), now(), vrow['id']))
        history.record(db, vrow['workspace'], 'worker', 'verification.completed', 'verification', vrow['id'], {'target_job_id': target['id'], 'class': vrow['class'], 'outcome': outcome, 'checked': result.get('checked'), 'total': result.get('total')})
        return result, statement

    def _statement(self, db, vrow, result, target, tcontract):
        doc = merkle.parse(tcontract['contract_json'])
        from . import verification as self_mod
        return _exactable({'schema': STATEMENT_SCHEMA, 'statement_version': STATEMENT_SCHEMA, 'verification_id': vrow['id'], 'workspace': vrow['workspace'], 'target_job_id': target['id'], 'kind': target['kind'],
                           'contract_digest': tcontract['contract_digest'], 'input_root': tcontract['input_root'], 'result_commitment': vrow['result_commitment'], 'producer_verifier_id': doc.get('verifier_id'),
                           'producer_verifier_digest': doc.get('verifier_digest'), 'auditor_id': 'metacoin-verification/v1', 'auditor_digest': implementation_digest(), 'class': vrow['class'],
                           'params': json.loads(vrow['params_json']), 'challenge_digest': hashlib.sha256(vrow['challenge_json'].encode()).hexdigest() if vrow['challenge_json'] else None,
                           'tolerance': result.get('tolerance'), 'scope': {'checked': result.get('checked'), 'total': result.get('total'), 'items': result.get('scope_items'), 'coverage': result.get('coverage')},
                           'outcome': result['outcome'], 'claim': SUPPORT[target['kind']][vrow['class']], 'shared_code': SHARED_CODE.get(target['kind'], ['same implementation']),
                           'independence': 'same host and operator; not organizational independence; no attestation, no consensus, no sensor truth',
                           'issued_at': now(), 'nonce': secrets.token_hex(8)})

    def tick(self, db):
        """Finalize replica verifications whose replica job reached a terminal state."""
        n = 0
        for v in db.execute("SELECT * FROM verification_jobs WHERE state='awaiting_replica'").fetchall():
            rj = db.execute('SELECT * FROM jobs WHERE id=?', (v['replica_job_id'],)).fetchone()
            if rj is None or rj['state'] in ('queued', 'running'):
                continue
            target = db.execute('SELECT * FROM jobs WHERE id=?', (v['target_job_id'],)).fetchone()
            tcontract = db.execute('SELECT * FROM contracts WHERE id=?', (target['contract_id'],)).fetchone()
            if rj['state'] != 'succeeded':
                result = {'outcome': 'incomplete', 'checked': 0, 'total': 1, 'checks': [{'check': 'replica_completed', 'ok': False, 'detail': {'state': rj['state'], 'error': rj['error_code']}}], 'statement': 'the replica run did not complete; no agreement claim'}
            else:
                result = compare_replica(db, self.store, target, rj)
            self._finish(db, v, result, target, tcontract)
            if result['outcome'] == 'failed':
                db.execute("UPDATE verification_jobs SET state='disputed', dispute_json=? WHERE id=?", (json.dumps({'opened_at': now(), 'discrepancy': result.get('discrepancy'), 'runs': [target['id'], rj['id']], 'policy': 'no automatic acceptance while disputed; a reviewer resolves administratively with evidence'}), v['id']))
                history.record(db, v['workspace'], 'worker', 'verification.disputed', 'verification', v['id'], {'runs': [target['id'], rj['id']]})
            n += 1
        return n

    def resolve(self, db, principal, vid, decision, note=''):
        principal.require('review:decide')
        r = self.row(db, principal, vid)
        if r['state'] != 'disputed':
            raise ServiceError('CONFLICT', 'only a disputed verification can be resolved')
        if decision not in ('original_accepted', 'replica_accepted', 'both_rejected', 'inconclusive') or type(note) is not str or len(note) > 1024:
            raise ServiceError('VALIDATION', 'decision/note')
        contract = db.execute('SELECT reviewer_id FROM contracts WHERE id=(SELECT contract_id FROM jobs WHERE id=?)', (r['target_job_id'],)).fetchone()
        if contract['reviewer_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'not the designated reviewer for the target contract')
        res = {'decision': decision, 'note': note, 'evidence': {'result': json.loads(r['result_json'] or '{}').get('discrepancy'), 'runs': [r['target_job_id'], r['replica_job_id']]}, 'resolved_at': now(),
               'effect': 'administrative record only: the verification stays failed for gating purposes; scientific acceptance still requires a passing verification of the required class'}
        db.execute("UPDATE verification_jobs SET state='resolved', resolution_json=?, resolved_by=?, resolved_at=? WHERE id=?", (json.dumps(res), principal.id, now(), vid))
        history.record(db, principal.workspace, principal.id, 'verification.resolved', 'verification', vid, {'decision': decision})
        return self.view(db, principal, vid)

    # ---- policy templates (§65-6): reusable, immutable audit requirements ---------------------------------
    def create_policy(self, db, principal, body):
        principal.require('verification:submit'); principal.require('contract:create')
        if type(body) is not dict or set(body) - {'name', 'class', 'params', 'max_work', 'scope'}:
            raise ServiceError('VALIDATION', 'fields: name, class, params, max_work, scope')
        name, cls, params, scope = body.get('name'), body.get('class'), body.get('params') or {}, body.get('scope', 'all-results')
        if type(name) is not str or not 1 <= len(name) <= 64 or cls not in CLASSES or type(params) is not dict or set(params) - {'sample_count', 'backend'} or type(scope) is not str or len(scope) > 200:
            raise ServiceError('VALIDATION', 'name/class/params/scope')
        max_work = body.get('max_work', MAX_WORK[cls])
        if type(max_work) is not int or not 1 <= max_work <= MAX_WORK[cls]:
            raise ServiceError('VALIDATION', {'code': 'max_work', 'allowed_max': MAX_WORK[cls]})
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM verification_policies WHERE workspace=? AND name=?', (principal.workspace, name)).fetchone()[0]
        pid = 'vp_' + secrets.token_hex(6)
        db.execute('INSERT INTO verification_policies (id, workspace, name, version, class, params_json, max_work, verifier_digest, scope, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (pid, principal.workspace, name, version, cls, json.dumps(params), max_work, implementation_digest(), scope, principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'verification.requested', 'verification_policy', pid, {'name': name, 'version': version, 'class': cls})
        return self.policy_view(self.policy(db, principal, pid))

    def policy(self, db, principal, pid):
        r = db.execute('SELECT * FROM verification_policies WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'verification policy')
        return r

    @staticmethod
    def policy_view(r):
        return {'id': r['id'], 'name': r['name'], 'version': r['version'], 'class': r['class'], 'params': json.loads(r['params_json']), 'max_work': r['max_work'], 'verifier_digest': r['verifier_digest'],
                'verifier_current': r['verifier_digest'] == implementation_digest(), 'scope': r['scope'], 'created_by': r['created_by'], 'created_at': r['created_at'], 'retired_at': r['retired_at'],
                'immutable': 'a new version is a new id; contracts bind the id before execution'}

    def list_policies(self, db, principal):
        principal.require('job:read')
        return [self.policy_view(r) for r in db.execute('SELECT * FROM verification_policies WHERE workspace=? ORDER BY name, version', (principal.workspace,)).fetchall()]

    def retire_policy(self, db, principal, pid):
        principal.require('verification:submit'); principal.require('contract:create')
        self.policy(db, principal, pid)
        db.execute('UPDATE verification_policies SET retired_at=? WHERE id=? AND retired_at IS NULL', (now(), pid))
        return self.policy_view(self.policy(db, principal, pid))

    # ---- gate ------------------------------------------------------------------------------------
    @staticmethod
    def gate(db, job, contract):
        """Returns None when the contract's required verification is satisfied, else a refusal detail. The requirement
        is the class named in the policy or the template bound at freeze time (class, minimum sample count, verifier)."""
        pol = json.loads(contract['policy_json'])
        req, min_samples, verifier = pol.get('required_verification'), None, None
        if pol.get('verification_policy_id'):
            tpl = db.execute('SELECT * FROM verification_policies WHERE id=?', (pol['verification_policy_id'],)).fetchone()
            if tpl is None:
                return {'code': 'verification_policy_missing', 'policy_id': pol['verification_policy_id']}
            req = tpl['class']; min_samples = json.loads(tpl['params_json']).get('sample_count'); verifier = tpl['verifier_digest']
        if not req:
            return None
        for ok in db.execute("SELECT * FROM verification_jobs WHERE target_job_id=? AND class=? AND state='passed' AND result_commitment=?", (job['id'], req, job['evidence_root'])).fetchall():
            params = json.loads(ok['params_json'])
            if min_samples is not None and (params.get('sample_count') or 0) < min_samples:
                continue
            st = json.loads(ok['statement_json'] or '{}')
            if verifier is not None and st.get('auditor_digest') != verifier:
                continue
            return None
        pending = db.execute("SELECT id, state FROM verification_jobs WHERE target_job_id=? AND class=? ORDER BY created_at DESC LIMIT 1", (job['id'], req)).fetchone()
        return {'code': 'awaiting_verification', 'required_class': req, 'minimum_sample_count': min_samples, 'required_verifier_digest': verifier, 'latest': dict(pending) if pending else None,
                'note': 'bound before execution; a weaker audit cannot substitute for the required policy'}


def _elevated(principal, ops):
    """Server-side internal capability: the verification service may create the audit contract/job on behalf of a
    principal holding verification:submit, without widening that principal's other permissions."""
    class Elevated(auth.Principal):
        pass
    e = Elevated.__new__(Elevated)
    e.__dict__.update(principal.__dict__)
    e.scope = principal.scope
    base_can = principal.can
    e.can = lambda op, _ops=frozenset(ops): True if op in _ops else base_can(op)
    e.require = lambda op: e if e.can(op) else (_ for _ in ()).throw(ServiceError('FORBIDDEN', op))
    return e


def _exactable(o):
    if isinstance(o, float):
        return repr(o)
    if isinstance(o, dict):
        return {k: _exactable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_exactable(v) for v in o]
    return o


def implementation_digest():
    from pathlib import Path
    h = hashlib.sha256(b'metacoin/verification/v1\0')
    h.update((Path(__file__)).read_bytes())
    h.update((Path(__file__).parent / 'compute' / 'reference.py').read_bytes())
    h.update(temporal.bundle_digest().encode())
    return h.hexdigest()


def challenge_indices(challenge, total):
    seed, commitment, k = challenge['seed'], challenge['result_commitment'], min(challenge['sample_count'], total)
    ranked = sorted(range(total), key=lambda i: hashlib.sha256((seed + commitment + str(i)).encode()).digest())
    return sorted(ranked[:k])


def audit(kind, cls, inputs, files, result, params, challenge, run):
    """Dispatch to the audit implementation; returns {outcome, checked, total, checks, statement, tolerance?, scope_items?, coverage?}."""
    if kind == 'temporal_batch':
        return _audit_temporal_batch(cls, inputs, files, challenge)
    if kind == 'monte_carlo_reliability':
        return _audit_monte_carlo(cls, inputs, files, result)
    if kind == 'heat_diffusion':
        return _audit_heat(cls, inputs, files, result)
    if kind == 'calibration_fit':
        man = compute_manifests.manifest('calibration_fit')
        v = compute_verify.calibration(inputs, files, man, result, None)
        return {'outcome': 'passed' if v['passed'] else 'failed', 'checked': 1, 'total': 1, 'checks': v['checks'], 'tolerance': v['tolerance'], 'statement': v['statement'] + ' (training rows were not re-read: metrics and coefficient consistency only)'}
    if kind == 'resource_plan':
        return _audit_resource_plan(cls, inputs, files, result)
    if kind == 'legacy_task_replay':
        from .economy import legacy_bridge
        return legacy_bridge.audit(inputs, result)
    return _audit_science(kind, inputs, result)


def _audit_resource_plan(cls, inputs, files, result):
    from .compute import resource_plan as rp
    man = compute_manifests.manifest('resource_plan')
    if cls == 'full_reference':
        v = compute_verify.resource_plan(inputs, files, man, result, None)
        return {'outcome': 'passed' if v['passed'] else 'failed', 'checked': 1, 'total': 1, 'checks': v['checks'], 'tolerance': v['tolerance'], 'statement': v['statement']}
    plan = json.loads(files['plan.json'])
    checks = []
    def add(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
    add('status_declared', plan.get('status') in rp.STATUSES and result.get('status') == plan.get('status'), {'status': plan.get('status')})
    traj = plan.get('trajectory') or []
    add('reserve_invariant_in_stored_trajectory', all(t['energy'] >= inputs['reserve'] and t['energy'] <= inputs['capacity'] for t in traj), {'boundaries': len(traj)})
    add('objective_is_sum_of_selected_utilities', plan.get('objective') is None or plan['objective'] == sum(t.get('utility', 0) for t in inputs['tasks'] if t['id'] in (plan.get('selected') or [])), {'objective': plan.get('objective')})
    add('mandatory_tasks_selected', plan.get('assignments') is None or all(t['id'] in plan['assignments'] for t in inputs['tasks'] if t.get('mandatory')), {})
    ok = all(c['ok'] for c in checks)
    return {'outcome': 'passed' if ok else 'failed', 'checked': len(checks), 'total': len(checks), 'checks': checks, 'coverage': 'invariants only',
            'statement': 'stored plan invariants (status, reserve bounds along the stored trajectory, objective arithmetic, mandatory selection) checked without re-solving or replaying; not a feasibility proof'}


def _audit_temporal_batch(cls, inputs, files, challenge):
    vals, dtype, shape = npy.decode(files['results.npy'])
    total = compute_inputs.batch_total(inputs)
    w = len(TEMPORAL_COLUMNS)
    if shape != [total, w] or dtype != '<i8':
        return {'outcome': 'failed', 'checked': 0, 'total': total, 'checks': [{'check': 'shape', 'ok': False, 'detail': shape}], 'statement': 'result shape does not match the accepted batch'}
    rows = [vals[i * w:(i + 1) * w] for i in range(total)]
    if cls == 'analytical':
        bad = []
        cap = inputs['base']['capacity']
        for i, r in enumerate(rows):
            ok = r[0] in OUTCOME_CODES.values() and r[1] <= r[3] and r[13] <= r[14] and 0 <= r[15] <= r[16] <= cap and (r[0] != 0 or r[1] >= 0) and (r[0] != 1 or r[3] < 0) and (r[0] != 2 or (r[1] < 0 <= r[3]))
            if not ok:
                bad.append(i)
                if len(bad) >= 20:
                    break
        return {'outcome': 'passed' if not bad else 'failed', 'checked': total, 'total': total, 'checks': [{'check': 'row_invariants', 'ok': not bad, 'detail': {'violations': bad}}],
                'statement': 'row invariants (outcome codes, margin ordering, spill and final-energy bounds, outcome/margin consistency) over every scenario; no scenario was recomputed', 'coverage': 'invariants only'}
    if cls == 'sampled_reference':
        indices = challenge_indices(challenge, total)
        coverage = 'challenge-selected subset without replacement'
    else:
        indices = range(total); coverage = 'every scenario'
    mism, checked = [], 0
    for i in indices:
        sid, sc, _ = compute_inputs.scenario(inputs, i)
        ref = reference.temporal_reference_row(sc)
        checked += 1
        if rows[i] != ref:
            mism.append({'index': i, 'id': sid, 'columns': [c for c, a, b in zip(TEMPORAL_COLUMNS, rows[i], ref) if a != b]})
            if len(mism) >= 20:
                break
    return {'outcome': 'passed' if not mism else 'failed', 'checked': checked, 'total': total, 'checks': [{'check': 'reference_rows_exact', 'ok': not mism, 'detail': {'mismatches': mism}}],
            'scope_items': list(indices) if cls == 'sampled_reference' else None, 'coverage': coverage, 'tolerance': 'exact (integers)',
            'statement': ('%d of %d scenarios' % (checked, total)) + ' recomputed by the temporal-energy/v1 reference and compared exactly' + ('' if cls == 'full_exact' else '; unchecked scenarios are not individually proven (detection of a single wrong row is not guaranteed by a sample)')}


def _audit_monte_carlo(cls, inputs, files, result):
    audit_entries = json.loads(files['samples_audit.json'])
    total = inputs['samples']
    from .compute.kernels import wilson_interval
    events, n = int(result['events']), int(result['samples'])
    ci = wilson_interval(events, n, inputs['confidence_percent'])
    checks = []
    consistent = 0 <= events <= n == total
    checks.append({'check': 'counts_consistent', 'ok': consistent, 'detail': {'events': events, 'samples': n}})
    stored = result.get('interval') or {}
    same = all(abs(float(stored.get(k, 'nan')) - float(ci[k])) <= 1e-12 for k in ('low', 'high')) if stored else False
    checks.append({'check': 'wilson_interval_recomputed', 'ok': same, 'detail': {'recomputed': {k: repr(ci[k]) for k in ('low', 'high')}}})
    if cls == 'analytical':
        return {'outcome': 'passed' if all(c['ok'] for c in checks) else 'failed', 'checked': 1, 'total': total, 'checks': checks, 'statement': 'count consistency and interval recomputation only; no sample was re-evaluated', 'coverage': 'invariants only'}
    mism, checked = [], 0
    for e in audit_entries:
        p = e['params']; var = {}
        if 'initial_energy' in p:
            var['initial_low'] = var['initial_high'] = p['initial_energy']
        for k in ('reserve', 'harvest_scale_percent', 'load_scale_percent', 'leakage_scale_percent'):
            if k in p:
                var[k] = p[k]
        ref = temporal.analyze(compute_inputs.apply_variation(inputs['base'], var))
        checked += 1
        if (1 if ref['outcome'] == 'FEASIBLE' else 0) != e['outcome']:
            mism.append({'index': e['index'], 'kernel': e['outcome'], 'reference': ref['outcome']})
            if len(mism) >= 20:
                break
    checks.append({'check': 'audited_samples_reference', 'ok': not mism, 'detail': {'mismatches': mism, 'audited': checked}})
    return {'outcome': 'passed' if all(c['ok'] for c in checks) else 'failed', 'checked': checked, 'total': total, 'checks': checks, 'coverage': 'producer-stored audit samples (%d of %d)' % (checked, total),
            'statement': '%d producer-stored samples re-evaluated by temporal-energy/v1; counts and the Wilson interval recomputed; the Philox sampler mapping was not independently regenerated (no second implementation available in this process)' % checked}


def _audit_heat(cls, inputs, files, result):
    p = compute_inputs.validate_heat(inputs)
    rx, ry = float(p['rx']), float(p['ry']); nx, ny = p['nx'], p['ny']
    fvals, fd, fshape = npy.decode(files['field.npy'])
    rows = [fvals[j * nx:(j + 1) * nx] for j in range(ny)]
    tol = compute_manifests.MANIFESTS['heat_diffusion']['verification_policy']['tolerance']
    f0 = heat_initial_field(inputs, p)
    lo0, hi0 = min(min(r) for r in f0), max(max(r) for r in f0)
    scale = max(1.0, abs(lo0), abs(hi0))
    checks = []
    checks.append({'check': 'shape_and_dtype', 'ok': fshape == [ny, nx] and fd == '<f8', 'detail': fshape})
    checks.append({'check': 'finite', 'ok': all(v == v and abs(v) != float('inf') for v in fvals), 'detail': None})
    from decimal import Decimal
    bv = {s: float(Decimal(inputs['boundary']['values'][s])) for s in ('left', 'right', 'top', 'bottom')}
    checks.append({'check': 'boundary_fixed', 'ok': all(rows[j][0] == bv['left'] and rows[j][-1] == bv['right'] for j in range(ny)) and all(rows[0][i] == bv['bottom'] and rows[-1][i] == bv['top'] for i in range(nx)), 'detail': None})
    checks.append({'check': 'discrete_maximum_principle', 'ok': lo0 - 1e-9 * scale <= min(fvals) and max(fvals) <= hi0 + 1e-9 * scale, 'detail': {'initial_range': [lo0, hi0], 'final_range': [min(fvals), max(fvals)]}})
    coverage, statement = 'invariants', 'invariants only'
    if cls == 'analytical':
        init = inputs['initial']
        if init['type'] == 'sine_mode' and all(bv[s] == 0.0 for s in bv):
            factor = reference.heat_eigenmode_factor(init['m'], init['n'], nx, ny, rx, ry) ** p['steps']
            expected = [[f0[j][i] * factor for i in range(nx)] for j in range(ny)]
            for j in range(ny):
                expected[j][0] = expected[j][-1] = 0.0
            for i in range(nx):
                expected[0][i] = expected[-1][i] = 0.0
            diff = reference.heat_max_abs_diff(expected, rows)
            checks.append({'check': 'closed_form_eigenmode', 'ok': diff <= tol['abs'] + tol['rel'] * scale, 'detail': {'max_abs_diff': diff, 'per_step_factor': factor ** (1.0 / p['steps']) if p['steps'] else None}})
            coverage, statement = 'invariants + closed-form eigenmode solution of the discrete scheme', 'closed-form discrete eigenmode compared over the whole field'
        else:
            statement = 'invariants only (no closed form for this initial condition)'
    elif cls == 'sampled_reference':
        pv, _, _ = npy.decode(files['field_prev.npy'])
        prev = [pv[j * nx:(j + 1) * nx] for j in range(ny)]
        diff = reference.heat_max_abs_diff(reference.heat_reference(prev, rx, ry, 1), rows)
        checks.append({'check': 'last_step_recomputed', 'ok': diff <= tol['abs'] + tol['rel'] * scale, 'detail': {'max_abs_diff': diff}})
        coverage, statement = 'one transition (penultimate to final field)', 'the final step recomputed from the stored penultimate field; the prior trajectory is not checked by this class'
    else:
        cell_steps = nx * ny * p['steps']
        if cell_steps > MAX_WORK['full_reference']:
            return {'outcome': 'incomplete', 'checked': 0, 'total': cell_steps, 'checks': checks, 'tolerance': tol, 'statement': 'full scalar recomputation exceeds the bound (%d cell-steps); use analytical or sampled_reference' % MAX_WORK['full_reference']}
        diff = reference.heat_max_abs_diff(reference.heat_reference(f0, rx, ry, p['steps']), rows)
        checks.append({'check': 'full_scalar_reference', 'ok': diff <= tol['abs'] + tol['rel'] * scale, 'detail': {'max_abs_diff': diff, 'cell_steps': cell_steps}})
        coverage, statement = 'whole trajectory from the initial condition', 'the whole run recomputed by the scalar pure-Python reference from the initial condition and compared within tolerance'
    return {'outcome': 'passed' if all(c['ok'] for c in checks) else 'failed', 'checked': nx * ny, 'total': nx * ny, 'checks': checks, 'tolerance': tol, 'coverage': coverage,
            'statement': statement + '; evidence under the FTCS model assumptions, not a physical validation'}


def _audit_science(kind, inputs, result):
    fn = {'temporal_energy': temporal.analyze, 'safe_runtime': science.safe_runtime, 'plan_comparison': science.compare_plans, 'task_selection': science.select_tasks}
    if kind not in fn:
        return {'outcome': 'incomplete', 'checked': 0, 'total': 1, 'checks': [], 'statement': 'no audit implementation for this kind'}
    fresh = fn[kind](inputs)
    same = merkle.canonical(fresh) == merkle.canonical(result)
    return {'outcome': 'passed' if same else 'failed', 'checked': 1, 'total': 1, 'checks': [{'check': 'recomputation_same_implementation', 'ok': same, 'detail': None}],
            'statement': 'recomputed by the same installed implementation (regression check; not an independent reference)', 'coverage': 'whole result, same implementation'}


def compare_replica(db, store, target, replica):
    tr = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (target['id'],)).fetchone()
    rr = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (replica['id'],)).fetchone()
    tf = container.unpack(store.load(db, tr['output_artifact_id'], target['workspace']))
    rf = container.unpack(store.load(db, rr['output_artifact_id'], target['workspace']))
    kind = target['kind']
    checks = []
    if kind == 'heat_diffusion':
        tol = compute_manifests.MANIFESTS['heat_diffusion']['verification_policy']['tolerance']
        a, _, sa = npy.decode(tf['field.npy']); b, _, sb = npy.decode(rf['field.npy'])
        scale = max(1.0, max(abs(v) for v in a))
        diff = max(abs(x - y) for x, y in zip(a, b)) if sa == sb else float('inf')
        ok = sa == sb and diff <= tol['abs'] + tol['rel'] * scale
        checks.append({'check': 'field_agreement', 'ok': ok, 'detail': {'max_abs_diff': diff, 'tolerance': tol}})
        disc = {'metric': 'max_abs_diff', 'value': diff, 'tolerance': tol}
        tolerance = tol
    else:
        name = 'results.npy' if kind == 'temporal_batch' else 'samples_audit.json'
        ok = tf.get(name) == rf.get(name)
        checks.append({'check': 'byte_identical_' + name.replace('.', '_'), 'ok': ok, 'detail': {'target_sha256': hashlib.sha256(tf.get(name, b'')).hexdigest(), 'replica_sha256': hashlib.sha256(rf.get(name, b'')).hexdigest()}})
        if kind == 'monte_carlo_reliability':
            ts, rs = json.loads(target['summary_json']), json.loads(replica['summary_json'])
            same = ts.get('events') == rs.get('events') and ts.get('samples') == rs.get('samples')
            checks.append({'check': 'event_counts_identical', 'ok': same, 'detail': {'target': ts.get('events'), 'replica': rs.get('events')}})
            ok = ok and same
        disc = {'metric': 'byte_equality', 'value': 'identical' if ok else 'different'}
        tolerance = 'exact'
    return {'outcome': 'passed' if ok else 'failed', 'checked': 1, 'total': 1, 'checks': checks, 'tolerance': tolerance, 'discrepancy': None if ok else disc,
            'replica': {'job_id': replica['id'], 'backend': rr['selected_backend'], 'versions': json.loads(rr['versions_json'] or '{}'), 'precision': rr['precision']},
            'original': {'job_id': target['id'], 'backend': tr['selected_backend'], 'versions': json.loads(tr['versions_json'] or '{}'), 'precision': tr['precision']},
            'coverage': 'whole output, two backends on the same host', 'statement': 'agreement between the %s and %s backends on this host (implementation evidence, not organizational independence)' % (tr['selected_backend'], rr['selected_backend'])}


def validate_audit_input(data):
    from .models.service import ModelInvalid
    if type(data) is not dict or data.get('schema') != 'verification-audit-input/v1':
        raise ModelInvalid('schema must be verification-audit-input/v1')
    if set(data) != {'schema', 'verification_id', 'target_job_id', 'class', 'params'}:
        raise ModelInvalid('fields: verification_id, target_job_id, class, params')
    if type(data['verification_id']) is not str or not data['verification_id'].startswith('vf_') or type(data['target_job_id']) is not str or not data['target_job_id'].startswith('j_'):
        raise ModelInvalid('ids')
    if data['class'] not in CLASSES or type(data['params']) is not dict:
        raise ModelInvalid('class/params')
    return data
