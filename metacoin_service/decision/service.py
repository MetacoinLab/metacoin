"""Decision evidence service (Deliverables C, D, E, G): analyses as ordinary jobs, private witnesses with disclosure
policy, acquisition plans bound to a frozen candidate model and an immutable quoted menu, materialisation into existing
work terms / requests / awards (existing budget reservation), provider observation deliveries released to the held
milestone job, assurance previews on awards, and authorized decision bundles for offline replay."""
import hashlib
import io
import json
import secrets
import zipfile

from experiments.private_receipts import receipt as merkle
from .. import crypto, history, metering
from ..db import now
from ..errors import ServiceError
from . import assurance, kernels as K, operations as ops, records

BUNDLE_SCHEMA = 'metacoin-decision-bundle/v1'
MAX_MENU_BYTES = 64 * 1024


def stored_delivery(db, store, job):
    """The provider's observation delivery attached to an evidence_acquisition job (None before delivery)."""
    d = db.execute('SELECT * FROM job_deliveries WHERE job_id=?', (job['id'],)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_deliveries'").fetchone() else None
    if d is None:
        return None
    return store.load_json(db, d['artifact_id'], job['workspace'])


class Decisions:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- analyses (decision_observability jobs) ------------------------------------------------------------------------------
    def analyze(self, db, principal, body):
        from ..api import quick_submit
        if type(body) is not dict or 'inputs' not in body or set(body) - {'inputs', 'title'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['inputs', 'title?']})
        title = body.get('title') or 'decision observability'
        if type(title) is not str or not 1 <= len(title) <= 128:
            raise ServiceError('VALIDATION', 'title')
        out = quick_submit(self.svc, db, principal, 'decision_observability', body['inputs'], title)
        history.record(db, principal.workspace, principal.id, 'job.queued', 'decision', out['job_id'], {'operation': 'decision.analyze'})
        return dict(out, decision_job_id=out['job_id'], next='GET /api/v1/decisions/{job_id} after the worker runs it')

    def _job(self, db, principal, jid, kinds=('decision_observability', 'evidence_acquisition')):
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (jid, principal.workspace)).fetchone()
        if job is None or job['kind'] not in kinds:
            raise ServiceError('NOT_FOUND', 'decision job')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        private = principal.can('job:read_private') or (principal.role == 'reviewer' and contract['reviewer_id'] == principal.id)
        return job, contract, private

    def _result(self, db, job):
        if job['state'] != 'succeeded':
            return None
        vault = self.svc.store.load_json(db, job['evidence_artifact_id'], job['workspace'])
        from experiments.work_contracts import acceptance as v0
        return v0.full_values(vault, vault['receipt']['root'])['result']

    def _inputs(self, db, job, contract):
        vault = self.svc.store.load_json(db, contract['input_artifact_id'], job['workspace'])
        return {f['name']: f['value'] for f in vault['fields']}['inputs']

    def view(self, db, principal, jid):
        principal.require('job:read')
        job, contract, private = self._job(db, principal, jid)
        jv = self.svc.jobs.view(db, principal, job, contract)
        out = {'job': jv, 'kind': job['kind'], 'state': job['state'], 'evidence_root': job['evidence_root'], 'projection': self.projection(db, principal, jid)}
        verifs = self.svc.verification.list(db, principal, jid)
        out['verifications'] = [{'id': v['id'], 'class': v['class'], 'state': v['state'], 'claim': (v.get('preview') or {}).get('claim')} for v in verifs]
        out['assurance_statements'] = [{k: s[k] for k in ('verification_id', 'class', 'claim_type', 'coverage', 'outcome', 'independence')} for s in records.statements_for_job(db, job, contract)]
        plans = db.execute('SELECT id, state, cost, budget, terms_id, award_id FROM acquisition_plans WHERE analysis_job_id=? ORDER BY created_at', (jid,)).fetchall()
        out['acquisition_plans'] = [dict(p) for p in plans]
        if job['kind'] == 'evidence_acquisition':
            d = db.execute('SELECT id, status, delivery_digest, delivered_by, created_at, award_id, milestone_id FROM job_deliveries WHERE job_id=?', (jid,)).fetchone()
            out['delivery'] = dict(d) if d else None
            inp = self._inputs(db, job, contract) if private else None
            out['plan_id'] = inp['plan_id'] if inp else None
        if private:
            out['result'] = self._result(db, job)
            out['inputs'] = self._inputs(db, job, contract)
        out['readings'] = self._readings(out)
        return out

    @staticmethod
    def _readings(out):
        """Plain console wording: separated within these assumptions / ambiguous / missing evidence / unsupported."""
        job = out['job']
        if job['state'] in ('queued', 'running'):
            return {'label': 'missing evidence', 'text': 'the analysis has not produced a result yet'}
        if job['state'] != 'succeeded':
            return {'label': 'unsupported', 'text': 'the analysis did not complete (%s): no conclusion' % job.get('error_code')}
        c = out['projection'].get('conclusion')
        return {'AMBIGUOUS': {'label': 'ambiguous', 'text': 'at least one pair of hypotheses with different decision labels cannot be told apart by the declared views under the declared noise'},
                'SEPARATED_ON_DECLARED_SET': {'label': 'separated within these assumptions', 'text': 'every pair with different decision labels is separated by some declared view; this holds for the enumerated hypotheses only'},
                'NO_DECISION_RELEVANT_ALTERNATIVES': {'label': 'vacuous', 'text': 'the candidate set carries one decision label only: nothing can be separated; this is not evidence'},
                'DETERMINED_WITHIN_DECLARED_MODEL': {'label': 'determined within the declared model', 'text': 'every hypothesis compatible with the observations carries the same decision label; states outside the enumerated set are not considered'},
                'AMBIGUOUS_AFTER_OBSERVATION': {'label': 'ambiguous', 'text': 'hypotheses with different decision labels remain compatible with the observations'},
                'MODEL_INCONSISTENT_WITH_OBSERVATIONS': {'label': 'unsupported', 'text': 'no declared hypothesis is compatible with the observations: the model, not the decision, needs revision; no nearest hypothesis is chosen'},
                'DECISION_RESOLVED': {'label': 'determined within the declared model', 'text': 'the delivered measurement leaves one decision label'},
                'DECISION_AMBIGUOUS': {'label': 'ambiguous', 'text': 'the delivered measurement is valid but hypotheses with different labels remain'},
                'DELIVERY_INVALID': {'label': 'unsupported', 'text': 'the delivery was malformed, had invalid provenance or inconsistent units; not evidence'},
                'ACQUISITION_FAILED': {'label': 'missing evidence', 'text': 'the provider could not acquire the evidence'}}.get(c, {'label': 'unsupported', 'text': 'no reading for conclusion %s' % c})

    def projection(self, db, principal, jid):
        """Counts, conclusion and digests; ids, labels and witness values only when the disclosure policy of the input allows."""
        principal.require('job:read')
        job, contract, private = self._job(db, principal, jid)
        summary = json.loads(job['summary_json']) if job['summary_json'] else {}
        disc = summary.get('disclosure') or {'ids': False, 'labels': False, 'witness_values': False}
        out = {'job_id': jid, 'kind': job['kind'], 'state': job['state'], 'conclusion': job['outcome'] if job['state'] == 'succeeded' else None, 'evidence_root': job['evidence_root'], 'disclosure': disc,
               'counts': {k: summary.get(k) for k in ('hypotheses', 'views', 'decision_relevant_pairs', 'ambiguous_pairs', 'separated_pairs', 'surviving_count', 'remaining_ambiguities') if k in summary},
               'digests': {k: summary.get(k) for k in ('candidate_model_digest', 'views_digest', 'plan_digest', 'delivery_digest') if k in summary},
               'versions': {k: summary.get(k) for k in ('model_id', 'model_version', 'candidate_set_version', 'measurement_menu_version') if k in summary},
               'scope': summary.get('scope'), 'policy': 'ids, labels and witness values are private unless the input disclosure policy allows them; counts never reveal which hypothesis is which'}
        if job['state'] == 'succeeded' and (disc['ids'] or disc['labels'] or disc['witness_values']):
            res = self._result(db, job)
            an = res if job['kind'] == 'decision_observability' else (res.get('analysis') or {})
            pub = []
            for w in an.get('ambiguity_witnesses') or []:
                pub.append({'pair': w['pair'] if disc['ids'] else None, 'labels': w['labels'] if disc['labels'] else None, 'shared_observation': w['shared_observation'] if disc['witness_values'] else None})
            out['disclosed_witnesses'] = pub
            if disc['labels'] and an.get('conditioning'):
                out['determined_label'] = an['conditioning']['determined_label']
        return out

    def witnesses(self, db, principal, jid):
        """Private: the raw ambiguity witnesses with states (owner / designated reviewer only)."""
        job, contract, private = self._job(db, principal, jid)
        if not private:
            raise ServiceError('FORBIDDEN', 'witnesses are private to the owner and the designated reviewer')
        if job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'no result yet')
        res = self._result(db, job)
        an = res if job['kind'] == 'decision_observability' else (res.get('analysis') or {})
        return {'job_id': jid, 'conclusion': job['outcome'], 'ambiguity_witnesses': an.get('ambiguity_witnesses', []), 'separated_pairs': an.get('separated_pairs', []), 'conditioning': an.get('conditioning'),
                'checker': 'POST /api/v1/verification {job_id, class: analytical} re-decides every witness with decision-witness-checker/v1', 'private': True}

    def list(self, db, principal, kind=None):
        principal.require('job:read')
        kinds = (kind,) if kind in ('decision_observability', 'evidence_acquisition') else ('decision_observability', 'evidence_acquisition')
        rows = db.execute('SELECT j.*, c.title AS title FROM jobs j JOIN contracts c ON c.id=j.contract_id WHERE j.workspace=? AND j.kind IN (%s) ORDER BY j.created_at DESC LIMIT 100' % ','.join('?' * len(kinds)), (principal.workspace, *kinds)).fetchall()
        return [{'job_id': r['id'], 'kind': r['kind'], 'state': r['state'], 'conclusion': r['outcome'] if r['state'] == 'succeeded' else None, 'title': r['title'], 'created_at': r['created_at'], 'evidence_root': r['evidence_root']} for r in rows]

    # ---- acquisition plans -----------------------------------------------------------------------------------------------------
    def _plan_input(self, db, principal, body):
        if type(body) is not dict or set(body) - {'analysis_job_id', 'menu', 'budget', 'objective', 'weights', 'preview'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['analysis_job_id', 'menu', 'budget', 'objective?', 'weights?']})
        job, contract, private = self._job(db, principal, body.get('analysis_job_id'), kinds=('decision_observability',))
        if not private:
            raise ServiceError('FORBIDDEN', 'planning needs private access to the candidate model')
        if job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'the analysis has no committed result yet')
        menu = body.get('menu')
        if type(menu) is not list or not menu or len(merkle.canonical(menu)) > MAX_MENU_BYTES:
            raise ServiceError('VALIDATION', {'code': 'menu', 'note': 'non-empty list of candidate views with quoted integer costs (<= 64 KiB)'})
        for m in menu:
            if type(m) is not dict or 'cost' not in m or 'quote_expires_at' not in m:
                raise ServiceError('VALIDATION', {'code': 'menu_entry', 'required': ['id', 'rows', 'noise', 'output_units', 'cost', 'quote_expires_at', 'execution_modes?', 'provider_id?']})
            if type(m['quote_expires_at']) is not int or m['quote_expires_at'] <= now():
                raise ServiceError('VALIDATION', {'code': 'quote_expired', 'view': m.get('id'), 'note': 'every menu quote needs a future integer expiry'})
        inputs = self._inputs(db, job, contract)
        try:
            plan = K.plan_acquisition(inputs, menu, body.get('budget'), body.get('objective', 'unweighted_pairs/v1'), body.get('weights'))
        except K.KernelInvalid as exc:
            raise ServiceError('VALIDATION', {'code': exc.code, **exc.detail})
        plan['analysis_job_id'] = job['id']; plan['analysis_evidence_root'] = job['evidence_root']; plan['question'] = inputs.get('question')
        plan['ambiguities_before'] = [w['pair'] for w in (self._result(db, job).get('ambiguity_witnesses') or [])]
        plan['expires_at'] = min(m['quote_expires_at'] for m in menu if m['id'] in plan['selected_views']) if plan['selected_views'] else None
        plan['rounds'] = {'max_rounds': 1, 'round': 1, 'stopping_rule': 'single round: a second purchase needs a new explicit plan and award; no autonomous budget escalation'}
        plan['plan_digest'] = hashlib.sha256(b'metacoin/acquisition-plan/v1\0' + merkle.canonical({k: v for k, v in plan.items() if k != 'plan_digest'})).hexdigest()
        return plan, menu, job

    def plan_preview(self, db, principal, body):
        plan, menu, job = self._plan_input(db, principal, body)
        return dict(plan, id=None, state='preview', stored=False, note='nothing stored or reserved; the next useful measurement under the declared model and this menu')

    def plan_create(self, db, principal, body):
        principal.require('contract:create')
        plan, menu, job = self._plan_input(db, principal, body)
        pid = 'ap_' + secrets.token_hex(6)
        selected = [m for m in menu if m['id'] in plan['selected_views']]
        db.execute('INSERT INTO acquisition_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (pid, principal.workspace, job['id'], plan['candidate_model_digest'], plan['menu_digest'], plan['budget'], plan['objective'], json.dumps(plan), plan['plan_digest'], plan['cost'],
                                                                                                json.dumps(selected), plan['expires_at'], 'planned' if plan['selected_views'] else 'nothing_to_acquire', None, None, None, principal.id, now(), now()))
        history.record(db, principal.workspace, principal.id, 'work.terms_created', 'acquisition_plan', pid, {'analysis_job_id': job['id'], 'cost': plan['cost'], 'budget': plan['budget'], 'selected': plan['selected_views'], 'resolved_after': plan['resolved_after']})
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'job', job['id'], 'acquisition_plan', pid, 'planned_from')
        return self.plan_view(db, principal, pid)

    def plan_row(self, db, principal, pid):
        r = db.execute('SELECT * FROM acquisition_plans WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'acquisition plan')
        return r

    def plan_view(self, db, principal, pid):
        principal.require('job:read')
        r = self.plan_row(db, principal, pid)
        plan = json.loads(r['plan_json'])
        job, contract, private = self._job(db, principal, r['analysis_job_id'], kinds=('decision_observability',))
        if not private:
            plan = {k: plan[k] for k in ('schema', 'objective', 'budget', 'cost', 'resolved_before', 'resolved_after', 'total_relevant_pairs', 'optimization_status', 'optimality', 'nothing_to_acquire', 'expires_at', 'plan_digest', 'candidate_model_digest', 'menu_digest')}
        return dict(plan, id=pid, state=r['state'], terms_id=r['terms_id'], request_id=r['request_id'], award_id=r['award_id'], analysis_job_id=r['analysis_job_id'], created_by=r['created_by'], created_at=r['created_at'],
                    expired=bool(r['expires_at'] and r['expires_at'] < now()), expected_cost=r['cost'], actual_cost=self._actual_cost(db, r))

    def _actual_cost(self, db, r):
        if not r['award_id']:
            return {'state': 'not_awarded', 'amount': None}
        ents = db.execute("SELECT state, amount FROM work_entitlements WHERE award_id=? AND kind='provider'", (r['award_id'],)).fetchall()
        if not ents:
            return {'state': 'reserved', 'amount': None, 'reserved': db.execute('SELECT reserved FROM work_awards WHERE id=?', (r['award_id'],)).fetchone()['reserved']}
        e = ents[-1]
        return {'state': e['state'], 'amount': e['amount'], 'settled': e['state'] == 'paid'}

    def plan_list(self, db, principal):
        principal.require('job:read')
        return [self.plan_view(db, principal, r['id']) for r in db.execute('SELECT id FROM acquisition_plans WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (principal.workspace,)).fetchall()]

    # ---- materialisation into existing work terms ----------------------------------------------------------------------------------
    def materialize(self, db, principal, pid, body=None):
        """Create and freeze decision-evidence-acquisition terms from a stored plan, then open the request. Quote expiry and the
        candidate model binding are rechecked here and again at award. Nothing is reserved until an award."""
        principal.require('work:request'); principal.require('contract:freeze')
        body = body or {}
        if type(body) is not dict or set(body) - {'diagnostic_amount', 'title', 'open'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['diagnostic_amount?', 'title?', 'open?']})
        r = self.plan_row(db, principal, pid)
        if r['created_by'] != principal.id and not principal.can('work:award'):
            raise ServiceError('FORBIDDEN', 'only the planner materializes a plan')
        if r['state'] == 'nothing_to_acquire':
            raise ServiceError('CONFLICT', {'code': 'nothing_to_acquire', 'note': 'the plan selects no view under its budget: no contract, no reservation, no charge'})
        if r['state'] != 'planned':
            raise ServiceError('CONFLICT', {'code': 'plan_state', 'state': r['state'], 'terms_id': r['terms_id']})
        if r['expires_at'] is not None and r['expires_at'] < now():
            db.execute("UPDATE acquisition_plans SET state='expired', updated_at=? WHERE id=?", (now(), pid))
            raise ServiceError('EXPIRED', {'code': 'quote_expired', 'expires_at': r['expires_at'], 'note': 'a stale preview is never trusted: re-plan with fresh quotes'})
        job, contract, private = self._job(db, principal, r['analysis_job_id'], kinds=('decision_observability',))
        inputs = self._inputs(db, job, contract)
        plan = json.loads(r['plan_json']); selected = json.loads(r['selected_json'])
        if K.analyze(dict(inputs, observations=None))['candidate_model_digest'] != r['candidate_model_digest']:
            raise ServiceError('CONFLICT', {'code': 'candidate_model_changed'})
        diag = body.get('diagnostic_amount', 0)
        if type(diag) is not int or isinstance(diag, bool) or not 0 <= diag <= r['cost']:
            raise ServiceError('VALIDATION', {'code': 'diagnostic_amount', 'allowed': 'integer 0..%d' % r['cost']})
        from ..economy import terms as terms_mod
        purpose = {'question': (inputs.get('question') or 'decision evidence acquisition')[:2000], 'evidence_needed': 'the selected measurements of plan %s' % pid, 'source_revision': plan['plan_digest'][:64]}
        t = terms_mod.decision_evidence_terms(principal.id, principal.workspace, ceiling=r['cost'], amount=r['cost'], diagnostic=diag, title=(body.get('title') or ('Acquire: ' + ', '.join(plan['selected_views'])))[:128], purpose=purpose)
        t['eligibility']['providers'] = sorted({m['provider_id'] for m in selected if m.get('provider_id')}) or []
        if not t['eligibility']['providers']:
            del t['eligibility']['providers']
        t['notes'] = 'frozen inputs: decision question, candidate-model digest %s, menu digest %s, validation %s, disclosure policy of the analysis, max budget %d' % (r['candidate_model_digest'][:16], r['menu_digest'][:16], ops.VALIDATION_METHOD, r['cost'])
        created = self.svc.economy.terms.create(db, principal, {'terms': t})
        frozen_inputs = {'schema': ops.ACQUISITION_SCHEMA, 'plan_id': pid, 'plan_digest': plan['plan_digest'], 'decision_job_id': job['id'], 'question': inputs.get('question'),
                         'candidate_model': {k: inputs[k] for k in inputs if k not in ('observations',)}, 'selected_views': [{k: v for k, v in m.items() if k in ('id', 'rows', 'noise', 'output_units', 'conversion', 'cost', 'provenance', 'quote_expires_at', 'provider_id', 'execution_modes', 'instrument')} for m in selected],
                         'validation': {'method': ops.VALIDATION_METHOD, 'checker_id': 'decision-witness-checker/v1', 'checker_digest': ops.checker_digest()}, 'max_budget': r['cost'],
                         'accepted_delivery_outcomes': ['DECISION_RESOLVED', 'DECISION_AMBIGUOUS', 'MODEL_INCONSISTENT_WITH_OBSERVATIONS'] + (['ACQUISITION_FAILED'] if diag > 0 else []), 'rounds': plan['rounds'], 'private_label': 'ACQ_' + pid}
        frozen = self.svc.economy.terms.freeze(db, principal, created['id'], {'inputs': frozen_inputs})
        req = None
        if body.get('open', True):
            req = self.svc.economy.board.create_request(db, principal, {'terms_id': created['id']})
            req = self.svc.economy.board.request_state(db, principal, req['id'], 'open', {})
        db.execute("UPDATE acquisition_plans SET state='materialized', terms_id=?, request_id=?, updated_at=? WHERE id=?", (created['id'], req['id'] if req else None, now(), pid))
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'acquisition_plan', pid, 'work_terms', created['id'], 'materialized_into')
        history.record(db, principal.workspace, principal.id, 'work.terms_frozen', 'acquisition_plan', pid, {'terms_id': created['id'], 'request_id': req['id'] if req else None, 'ceiling': r['cost']})
        return {'plan': self.plan_view(db, principal, pid), 'terms': frozen, 'request': req, 'expected_cost': r['cost'], 'reserved': 0, 'note': 'nothing reserved until an award; the award rechecks quote expiry and the plan binding'}

    def award_guard(self, db, terms):
        """Called inside the award transaction for evidence_acquisition terms: refuse a stale plan or a changed binding."""
        if terms['operation']['kind'] != 'evidence_acquisition':
            return None
        tid = db.execute('SELECT id FROM work_terms WHERE contract_id=?', (terms['operation']['contract_id'],)).fetchone()
        plan = db.execute('SELECT * FROM acquisition_plans WHERE terms_id=?', (tid['id'],)).fetchone() if tid else None
        if plan is None:
            raise ServiceError('CONFLICT', {'code': 'acquisition_plan_missing', 'note': 'evidence_acquisition terms are awarded only from a stored plan'})
        if plan['expires_at'] is not None and plan['expires_at'] < now():
            db.execute("UPDATE acquisition_plans SET state='expired', updated_at=? WHERE id=?", (now(), plan['id']))
            db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')          # the expiry is a durable fact even though the award is refused (nothing else was written yet)
            raise ServiceError('EXPIRED', {'code': 'quote_expired_at_award', 'plan_id': plan['id'], 'expires_at': plan['expires_at'], 'note': 'the quoted menu expired between preview and award; nothing reserved'})
        if plan['state'] not in ('materialized',):
            raise ServiceError('CONFLICT', {'code': 'plan_state', 'state': plan['state']})
        return plan

    def on_award(self, db, award_id, terms_id):
        db.execute("UPDATE acquisition_plans SET state='awarded', award_id=?, updated_at=? WHERE terms_id=? AND state='materialized'", (award_id, now(), terms_id))

    # ---- provider delivery -----------------------------------------------------------------------------------------------------------------
    def deliver(self, db, principal, aid, key, body):
        """The awarded provider attaches an observation delivery to the held milestone job; the job is released to the worker.
        Idempotent for an identical delivery; a different second delivery is refused (the first attempt is the record)."""
        principal.require('work:deliver')
        award = self.svc.economy.board.award_row(db, principal, aid)
        prow = db.execute('SELECT * FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        if prow['principal_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the awarded provider delivers')
        ms = self.svc.economy.board.milestone_row(db, principal, aid, key)
        if not ms['job_id']:
            raise ServiceError('CONFLICT', {'code': 'milestone_not_dispatched', 'state': ms['state'], 'blocked_reason': ms['blocked_reason']})
        job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone()
        if job['kind'] != 'evidence_acquisition':
            raise ServiceError('CONFLICT', {'code': 'kind', 'note': 'deliveries apply to evidence_acquisition milestones'})
        delivery = body.get('delivery') if isinstance(body, dict) else None
        if type(delivery) is not dict:
            raise ServiceError('VALIDATION', {'code': 'delivery', 'schema': ops.DELIVERY_SCHEMA})
        if len(merkle.canonical(delivery)) > 64 * 1024:
            raise ServiceError('PAYLOAD_TOO_LARGE', 'delivery')
        if delivery.get('schema') != ops.DELIVERY_SCHEMA:
            raise ServiceError('VALIDATION', {'code': 'delivery_schema', 'allowed': [ops.DELIVERY_SCHEMA]})
        digest = hashlib.sha256(merkle.canonical(delivery)).hexdigest()
        existing = db.execute('SELECT * FROM job_deliveries WHERE job_id=?', (job['id'],)).fetchone()
        if existing is not None:
            if existing['delivery_digest'] == digest:
                return dict(self.delivery_view(db, principal, existing['id']), replayed=True)
            raise ServiceError('CONFLICT', {'code': 'delivery_already_recorded', 'delivery_id': existing['id'], 'note': 'one delivery per attempt; a correction goes through a dispute or a new award'})
        if job['state'] != 'queued' or not job['hold']:
            raise ServiceError('CONFLICT', {'code': 'job_state', 'state': job['state'], 'hold': bool(job['hold'])})
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        reviewer_pub = db.execute('SELECT value FROM meta WHERE key=?', ('age_public:' + str(contract['reviewer_id']),)).fetchone()
        art = self.svc.store.store(db, workspace=principal.workspace, kind='observation_delivery', owner_id=contract['owner_id'], plaintext=merkle.canonical(delivery), recipients=[reviewer_pub['value']] if reviewer_pub else [],
                                   intended_use='provider-observation-delivery;owner-reviewer-and-worker', job_id=job['id'], contract_id=contract['id'], retention_deadline=now() + json.loads(contract['policy_json'])['retention_seconds'])
        did = 'dl_' + secrets.token_hex(6)
        db.execute('INSERT INTO job_deliveries VALUES (?,?,?,?,?,?,?,?,?,?)', (did, principal.workspace, job['id'], aid, ms['id'], art, digest, delivery.get('status') if delivery.get('status') in ('acquired', 'could_not_acquire') else 'malformed', principal.id, now()))
        db.execute('UPDATE jobs SET hold=0, updated_at=? WHERE id=?', (now(), job['id']))
        db.execute("UPDATE work_milestones SET blocked_reason=NULL, updated_at=? WHERE id=?", (now(), ms['id']))
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'artifact', art, 'job', job['id'], 'delivered_observation')
        history.record(db, principal.workspace, principal.id, 'work.attempt', 'work_milestone', ms['id'], {'award_id': aid, 'delivery_id': did, 'delivery_digest': digest, 'status': delivery.get('status'), 'job_id': job['id'], 'released_to_worker': True})
        return self.delivery_view(db, principal, did)

    def delivery_spec(self, db, principal, aid, key):
        """What the provider must measure: the selected views (ids, rows, output units, noise bound, quoted cost) and the delivery
        schema. The candidate hypotheses are NOT disclosed (requester_private inputs)."""
        principal.require('work:read')
        award = self.svc.economy.board.award_row(db, principal, aid); ms = self.svc.economy.board.milestone_row(db, principal, aid, key)
        prow = db.execute('SELECT * FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        if principal.id not in (award['awarded_by'], prow['principal_id']) and principal.role != 'reviewer' and not principal.can('work:award'):
            raise ServiceError('FORBIDDEN', 'parties of the award only')
        from ..economy.board import _terms
        t, terms = _terms(db, award['terms_id'])
        if terms['operation']['kind'] != 'evidence_acquisition':
            raise ServiceError('CONFLICT', 'not an evidence acquisition award')
        plan = db.execute('SELECT * FROM acquisition_plans WHERE terms_id=?', (award['terms_id'],)).fetchone()
        selected = json.loads(plan['selected_json']) if plan else []
        job = db.execute('SELECT state, hold FROM jobs WHERE id=?', (ms['job_id'],)).fetchone() if ms['job_id'] else None
        return {'award_id': aid, 'milestone': key, 'plan_id': plan['id'] if plan else None, 'plan_digest': plan['plan_digest'] if plan else None,
                'views': [{'id': v['id'], 'rows': v['rows'], 'output_units': v['output_units'], 'noise_bound': v['noise'], 'cost': v['cost'], 'execution_modes': v.get('execution_modes'), 'conversion': v.get('conversion')} for v in selected],
                'coordinates_note': 'row coefficients apply to the requester\'s private state coordinates; the provider measures the declared outputs in the declared units',
                'delivery_schema': {'schema': ops.DELIVERY_SCHEMA, 'status': 'acquired | could_not_acquire', 'observations': '{view_id: [exact values as integers or decimal/p/q strings]}', 'provenance': {'instrument': 'str', 'acquired_at': 'int', 'units_confirmed': True, 'simulated': 'bool', 'operator?': 'str', 'method?': 'str'}, 'note?': 'str'},
                'deliver': 'POST /api/v1/work/awards/%s/milestones/%s/deliver {"delivery": {...}}' % (aid, key), 'job_state': dict(job) if job else None, 'ready_for_delivery': bool(job and job['state'] == 'queued' and job['hold']),
                'payment_rule': terms['acceptance']['payment_rule'], 'accepted_outcomes': terms['acceptance']['outcomes']}

    def delivery_view(self, db, principal, did):
        d = db.execute('SELECT * FROM job_deliveries WHERE id=? AND workspace=?', (did, principal.workspace)).fetchone()
        if d is None:
            raise ServiceError('NOT_FOUND', 'delivery')
        job = db.execute('SELECT state, outcome, hold FROM jobs WHERE id=?', (d['job_id'],)).fetchone()
        return {'id': did, 'job_id': d['job_id'], 'award_id': d['award_id'], 'milestone_id': d['milestone_id'], 'artifact_id': d['artifact_id'], 'delivery_digest': d['delivery_digest'], 'status': d['status'], 'delivered_by': d['delivered_by'], 'created_at': d['created_at'],
                'job_state': job['state'], 'job_hold': bool(job['hold']), 'outcome': job['outcome'], 'next': 'the worker conditions the frozen candidate model on this delivery; then evaluate the milestone'}

    def on_dispatch(self, db, award, ms, jid):
        job = db.execute('SELECT kind FROM jobs WHERE id=?', (jid,)).fetchone()
        if job and job['kind'] == 'evidence_acquisition':
            db.execute("UPDATE work_milestones SET blocked_reason=?, updated_at=? WHERE id=?", ('awaiting the provider\'s observation delivery (POST /work/awards/{id}/milestones/{key}/deliver); the job is held, nothing runs', now(), ms['id']))

    # ---- assurance preview on an award milestone --------------------------------------------------------------------------------------------
    def assurance_preview(self, db, principal, aid, key):
        principal.require('work:read')
        award = self.svc.economy.board.award_row(db, principal, aid); ms = self.svc.economy.board.milestone_row(db, principal, aid, key)
        from ..economy.board import _terms
        t, terms = _terms(db, award['terms_id'])
        job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone() if ms['job_id'] else None
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone() if job else None
        prow = db.execute('SELECT * FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        pex = json.loads(prow['execution_json'])['type']
        pol = terms['acceptance']
        reqs = [{'predicate': p['id'], 'requirement': p['params']['requirement']} for p in pol['predicates'] if p['type'] == 'assurance_satisfied']
        legacy = [{'predicate': p['id'], 'class': p['params']['class'], 'semantics': 'same class on the delivered root (policy v2)' if pol['schema'] != 'metacoin-acceptance-policy/v1' else assurance.LEGACY_RANK_NOTE} for p in pol['predicates'] if p['type'] == 'verification_passed']
        if job is None or job['state'] != 'succeeded':
            return {'award_id': aid, 'milestone': key, 'policy_schema': pol['schema'], 'requirements': reqs, 'legacy_class_predicates': legacy, 'available_evidence': [], 'status': 'missing evidence', 'plain': 'nothing delivered yet: no evidence root to bind a check to', 'previews': []}
        sts = records.statements_for_job(db, job, contract, pex)
        binding = records.binding_for(job, contract)
        previews = [dict(assurance.preview(r['requirement'], sts, binding), predicate=r['predicate']) for r in reqs]
        out = {'award_id': aid, 'milestone': key, 'evidence_root': job['evidence_root'], 'policy_schema': pol['schema'], 'policy_semantics': terms_semantics(pol['schema']), 'requirements': reqs, 'legacy_class_predicates': legacy,
               'available_evidence': [{k: s[k] for k in ('verification_id', 'class', 'claim_type', 'coverage', 'outcome')} for s in sts], 'previews': previews,
               'status': ('satisfied' if previews and all(p['satisfied'] for p in previews) else ('failed' if any(p['status'] == 'failed' for p in previews) else 'missing evidence')) if reqs else 'no typed requirement in these terms',
               'next_checks': sorted({(c['class']) for p in previews for c in p['next_checks']}), 'plain': ' '.join(p['plain'] for p in previews) if previews else 'these terms carry class-named predicates only; see legacy_class_predicates'}
        return out

    # ---- authorized bundle and replay ---------------------------------------------------------------------------------------------------------
    def bundle(self, db, principal, jid):
        """Zip for offline replay: raw inputs (private), result, stored delivery (when any), every signed verification statement
        and sampling plan, the service public key, a signed manifest. Owner / designated reviewer only; no service secrets."""
        job, contract, private = self._job(db, principal, jid)
        if not private:
            raise ServiceError('FORBIDDEN', 'a decision bundle discloses the private candidate model; owner or designated reviewer only')
        if job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'no committed result')
        files = {'inputs.json': merkle.canonical(self._inputs(db, job, contract)), 'result.json': merkle.canonical(self._result(db, job)),
                 'job.json': merkle.canonical({'job_id': jid, 'kind': job['kind'], 'outcome': job['outcome'], 'evidence_root': job['evidence_root'], 'contract_digest': contract['contract_digest'], 'input_root': contract['input_root'], 'summary': json.loads(job['summary_json'] or '{}')})}
        if job['kind'] == 'evidence_acquisition':
            d = stored_delivery(db, self.svc.store, job)
            if d is not None:
                files['delivery.json'] = merkle.canonical(d)
            inp = self._inputs(db, job, contract)
            pr = db.execute('SELECT plan_json FROM acquisition_plans WHERE id=?', (inp['plan_id'],)).fetchone()
            if pr:
                files['acquisition-plan.json'] = pr['plan_json'].encode()
        pub = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()['value']
        for v in db.execute('SELECT * FROM verification_jobs WHERE target_job_id=? AND statement_json IS NOT NULL ORDER BY created_at', (jid,)).fetchall():
            files['verifications/%s.json' % v['id']] = merkle.canonical({'statement': json.loads(v['statement_json']), 'signature_hex': v['signature_hex'], 'public_key_hex': pub, 'key_id': v['key_id']})
        for sp in db.execute('SELECT * FROM sampling_plans WHERE target_job_id=? ORDER BY created_at', (jid,)).fetchall():
            files['sampling-plans/%s.json' % sp['id']] = sp['plan_json'].encode()
        manifest = {'schema': BUNDLE_SCHEMA, 'job_id': jid, 'kind': job['kind'], 'evidence_root': job['evidence_root'], 'file_sha256': {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}, 'issued_at': now(), 'public_key_hex': pub,
                    'replay': 'python -m metacoin_service.decision.replay bundle.zip [--trust-root HEX]: recomputes witnesses, conditioning and sampling arithmetic with the independent checker; signatures are reported separately',
                    'contains_private_model': True, 'service_secrets': 'none (public key only)'}
        msg = merkle.canonical(manifest)
        sig = crypto.sign(crypto.load_signing_key(self.settings.keys_dir / 'service.ed25519'), msg)
        files['manifest.json'] = msg; files['statement.json'] = merkle.canonical({'manifest_sha256': hashlib.sha256(msg).hexdigest(), 'signature_hex': sig, 'key_id': crypto.key_id_for(pub), 'public_key_hex': pub})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for n, b in sorted(files.items()):
                z.writestr(n, b)
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'decision', jid, {'bundle': True, 'files': len(files)})
        return buf.getvalue()


def terms_digest(terms):
    from ..economy import terms as terms_mod
    return terms_mod.digest(terms)


def terms_semantics(schema):
    from ..economy import terms as terms_mod
    return terms_mod.policy_semantics(schema)
