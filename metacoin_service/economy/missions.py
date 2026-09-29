"""Mission-linked work portfolios (Order 08 Group F, §56–§60): read-only import of the anchored mission verdict,
deterministic bottleneck-to-work-request drafts, contribution records without invented usefulness proofs, honest
resource/energy evidence probes, and the bounded physical-work observation boundary (simulated devices only)."""
import hashlib
import json
import re
import secrets
import subprocess
from pathlib import Path

from experiments.private_receipts import receipt as merkle
from .. import crypto, history
from ..db import now
from ..errors import ServiceError
from . import legacy_bridge, terms as terms_mod
from .board import _terms

ROOT = Path(__file__).resolve().parents[2]
VERDICT = ROOT / 'mission_verdict.json'
CONTRIBUTION_TYPES = ('verified_computation', 'reviewed_interpretation', 'measured_observation', 'proposed_hypothesis')
CONTRIBUTION_KINDS = ('new_finding', 'commissioned_replication', 'reused_artifact', 'accepted_negative_closing_branch')
OBSERVATION_SCHEMA = 'metacoin-physical-observation/v1'


class Missions:
    def __init__(self, settings, services, board, evidence):
        self.settings, self.svc, self.board, self.evidence = settings, services, board, evidence

    # ---- portfolios (§56) ---------------------------------------------------------------------------------------------------
    def import_mission(self, db, principal, body):
        """Read-only import of the anchored mission verdict; historical verdicts and task identities are copied verbatim."""
        principal.require('work:request')
        src = body.get('source', 'mission_verdict.json')
        if src != 'mission_verdict.json':
            raise ServiceError('VALIDATION', {'code': 'source', 'allowed': ['mission_verdict.json']})
        doc = json.loads(VERDICT.read_text())
        digest = hashlib.sha256(VERDICT.read_bytes()).hexdigest()
        mid = doc['mission_id']
        ex = db.execute('SELECT id FROM mission_portfolios WHERE workspace=? AND mission_id=?', (principal.workspace, mid)).fetchone()
        if ex:
            return self.portfolio_view(db, principal, ex['id'])
        pid = 'mp_' + secrets.token_hex(6)
        snapshot = {'mission_id': mid, 'verdict_hash': doc['verdict_hash'], 'mission_feasible': doc['mission_feasible'], 'node_verdicts': doc['node_verdicts'], 'bottlenecks': doc['bottlenecks'], 'dag': doc['dag'], 'not_modeled': doc['not_modeled'],
                    'honest_boundary': doc['honest_boundary'], 'what_would_flip_it': doc.get('what_would_flip_it'), 'source_sha256': digest, 'imported_at': now(), 'rule': 'read-only: a new result may justify a new scenario revision; the anchored verdict is never rewritten'}
        db.execute('INSERT INTO mission_portfolios VALUES (?,?,?,?,?,?,?,?,?)', (pid, principal.workspace, mid, body.get('name', mid)[:128], json.dumps(snapshot), json.dumps({'drafts': []}), principal.id, now(), now()))
        history.record(db, principal.workspace, principal.id, 'work.mission', 'mission_portfolio', pid, {'mission_id': mid, 'verdict_hash': doc['verdict_hash'], 'bottlenecks': len(doc['bottlenecks'])})
        return self.portfolio_view(db, principal, pid)

    def portfolio_row(self, db, principal, pid):
        r = db.execute('SELECT * FROM mission_portfolios WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'mission portfolio')
        return r

    def portfolio_view(self, db, principal, pid):
        principal.require('work:read')
        r = self.portfolio_row(db, principal, pid)
        snap = json.loads(r['snapshot_json']); drafts = json.loads(r['drafts_json'])
        links = [dict(l) for l in db.execute('SELECT * FROM mission_links WHERE portfolio_id=? ORDER BY rowid', (pid,)).fetchall()]
        commissioned, budget = [], {'reserved': 0, 'paid': 0, 'ceilings': 0}
        for l in links:
            req = db.execute('SELECT * FROM work_requests WHERE id=?', (l['request_id'],)).fetchone() if l['request_id'] else None
            awards = db.execute('SELECT * FROM work_awards WHERE request_id=?', (l['request_id'],)).fetchall() if req else []
            entry = {'link_id': l['id'], 'node': l['node'], 'question': l['question'], 'evidence_needed': l['evidence_needed'], 'effect_on_model': l['effect_on_model'], 'request_id': l['request_id'], 'request_state': req['state'] if req else None, 'awards': []}
            for a in awards:
                budget['ceilings'] += a['ceiling']; budget['reserved'] += a['reserved'] if a['state'] not in ('closed',) else 0
                for e in db.execute("SELECT amount, state FROM work_entitlements WHERE award_id=? AND kind='provider'", (a['id'],)).fetchall():
                    budget['paid'] += e['amount'] if e['state'] in ('paid', 'refunded') else 0
                ms = db.execute('SELECT key, state, evidence_root, decision_id FROM work_milestones WHERE award_id=?', (a['id'],)).fetchall()
                findings = []
                for m in ms:
                    d = db.execute('SELECT evaluation_json, decision FROM work_decisions WHERE id=?', (m['decision_id'],)).fetchone() if m['decision_id'] else None
                    findings.append({'milestone': m['key'], 'state': m['state'], 'evidence_root': m['evidence_root'], 'acceptance': d['decision'] if d else 'pending', 'science': json.loads(d['evaluation_json'])['science'] if d else 'unknown'})
                entry['awards'].append({'award_id': a['id'], 'state': a['state'], 'ceiling': a['ceiling'], 'findings': findings})
            commissioned.append(entry)
        contribs = [dict(c) for c in db.execute('SELECT * FROM contributions WHERE portfolio_id=? ORDER BY rowid', (pid,)).fetchall()]
        for c in contribs:
            c['learning'] = json.loads(c.pop('learning_json') or 'null')
        learning = {'by_class': {k: sum(1 for c in contribs if (c['learning'] or {}).get('class') == k) for k in self.LEARNING_CLASSES}, 'decisions_changed': [c['id'] for c in contribs if (c['learning'] or {}).get('decision_changed')],
                    'records': [{'contribution_id': c['id'], 'node': c['affected_node'], 'class': (c['learning'] or {}).get('class'), 'declared_verdict': (c['learning'] or {}).get('declared_verdict'), 'commissioned_science': (c['learning'] or {}).get('commissioned_science'), 'decision_changed': (c['learning'] or {}).get('decision_changed', False)} for c in contribs],
                    'note': 'which findings contradicted, confirmed or left the declared record unresolved; a decision change is recorded only when the requester says so; no impact score'}
        unresolved = [b for b in snap['bottlenecks'] if not any(c['affected_node'] == b['task'] and c['contribution_kind'] != 'reused_artifact' for c in contribs)]
        return {'id': pid, 'mission_id': r['mission_id'], 'name': r['name'], 'imported': {k: snap[k] for k in ('verdict_hash', 'mission_feasible', 'source_sha256', 'imported_at', 'rule', 'honest_boundary')},
                'objectives_and_constraints': {'constraining_nodes': [k for k, v in snap['node_verdicts'].items() if v['role'] == 'constraining'], 'node_verdicts': snap['node_verdicts'], 'dag_edges': len(snap['dag']['edges']), 'not_modeled': snap['not_modeled']},
                'bottlenecks': snap['bottlenecks'], 'unresolved_bottlenecks': [b['task'] for b in unresolved], 'what_would_flip_it': snap.get('what_would_flip_it'), 'commissioned_work': commissioned, 'budget': budget, 'contributions': contribs, 'learning': learning, 'drafts': drafts['drafts'],
                'distinctions': 'model feasibility (anchored verdict) != experimental evidence != engineering deployment; accepted findings here update only this service-layer portfolio'}

    def list_portfolios(self, db, principal):
        principal.require('work:read')
        return [self.portfolio_view(db, principal, r['id']) for r in db.execute('SELECT id FROM mission_portfolios WHERE workspace=? ORDER BY rowid', (principal.workspace,)).fetchall()]

    # ---- bottleneck -> work request draft (§57) -----------------------------------------------------------------------------------
    def draft_from_bottleneck(self, db, principal, pid, task, body):
        """Deterministic transformation: structured mission data -> WorkTerms draft (no model invents constants or experiments)."""
        principal.require('work:request')
        r = self.portfolio_row(db, principal, pid); snap = json.loads(r['snapshot_json'])
        body = body or {}
        kind = body.get('kind', 'legacy_replay')
        ceiling = body.get('ceiling', 3)
        terms_mod._int(ceiling, 0, 10 ** 12, 'ceiling')
        deps = sorted({e['dst'] for e in snap['dag']['edges'] if e['src'] == task})
        if kind == 'legacy_replay':
            b = next((x for x in snap['bottlenecks'] if x['task'] == task), None)
            if b is None or task not in legacy_bridge.registry():
                raise ServiceError('NOT_FOUND', {'code': 'bottleneck', 'available': [x['task'] for x in snap['bottlenecks']]})
            reg = legacy_bridge.registry()[task]
            t = terms_mod.template('independent_replay', principal.id, principal.workspace, ceiling=ceiling, registered_hash=reg['registered_hash'], title=('Replay bottleneck ' + task + ': ' + b['quantity'])[:128], asset=body.get('asset', 'action-units'))
            purpose = {'mission_id': snap['mission_id'], 'node': task, 'question': ('Does an independent exact replay reproduce the registered verdict behind: ' + b['statement'])[:2000], 'evidence_needed': 'exact canonical output equal to the registered hash ' + str(reg['registered_hash'])[:16] + '…',
                       'affects': deps or ['mission-0001'], 'source_revision': snap['verdict_hash']}
            effect = 'a matching replay re-derives the constraint (the verdict stays false and anchored); a mismatch would justify a new scenario revision, never a rewrite'
            inputs = {'schema': legacy_bridge.INPUT_SCHEMA, 'task_id': task}
        elif kind == 'resource_plan':
            aid = body.get('analysis_id'); plan_inputs = body.get('inputs')
            if type(plan_inputs) is not dict:
                raise ServiceError('VALIDATION', {'code': 'inputs', 'note': 'a resource_plan draft needs the structured planning inputs (from an analysis session or campaign), not prose'})
            from ..compute import resource_plan as rp
            rp.validate(plan_inputs)
            t = terms_mod.template('infeasibility_witness', principal.id, principal.workspace, ceiling=ceiling, title=('Resource-schedule witness for ' + task)[:128], asset=body.get('asset', 'action-units'))
            purpose = {'mission_id': snap['mission_id'], 'node': task, 'question': ('Is a reserve-respecting schedule feasible under the declared integer model for node ' + task + '?')[:2000], 'evidence_needed': 'a schedule witness checked by the exact simulator, or infeasibility established by the solver/oracle',
                       'affects': deps or ['mission-0001'], 'source_revision': snap['verdict_hash']}
            if aid:
                purpose['question'] += ' (analysis %s)' % aid
            effect = 'a feasible witness or an established infeasibility updates the service-layer scenario; the anchored mission verdict is unchanged'
            inputs = plan_inputs
        else:
            raise ServiceError('VALIDATION', {'code': 'kind', 'allowed': ['legacy_replay', 'resource_plan']})
        t['purpose'] = purpose
        t['notes'] = 'derived deterministically from mission data (bottleneck table, dag, registered hashes); no constant was invented; opening the request needs the requester\'s budget and provider policy review'
        created = self.svc.economy.terms.create(db, principal, {'terms': t})
        drafts = json.loads(r['drafts_json']); drafts['drafts'].append({'terms_id': created['id'], 'node': task, 'kind': kind, 'created_at': now()})
        db.execute('UPDATE mission_portfolios SET drafts_json=?, updated_at=? WHERE id=?', (json.dumps(drafts), now(), pid))
        lid = 'ml_' + secrets.token_hex(6)
        db.execute('INSERT INTO mission_links VALUES (?,?,?,?,?,?,?,?,?,?)', (lid, principal.workspace, pid, task, created['id'], None, purpose['question'], purpose['evidence_needed'], effect, now()))
        history.record(db, principal.workspace, principal.id, 'work.mission', 'mission_portfolio', pid, {'draft_terms_id': created['id'], 'node': task, 'kind': kind, 'spent': 0})
        return {'terms': created, 'suggested_inputs_for_freeze': inputs, 'link_id': lid, 'effect_on_model': effect, 'next': 'review the terms, freeze with the suggested inputs, allocate a budget, open the request; nothing has been spent or contacted'}

    def link_request(self, db, principal, pid, body):
        """Attach a request to a portfolio link (after freezing the drafted terms and creating the request)."""
        principal.require('work:request')
        self.portfolio_row(db, principal, pid)
        l = db.execute('SELECT * FROM mission_links WHERE id=? AND portfolio_id=?', (body.get('link_id'), pid)).fetchone()
        req = db.execute('SELECT * FROM work_requests WHERE id=? AND workspace=?', (body.get('request_id'), principal.workspace)).fetchone()
        if l is None or req is None or req['terms_id'] != l['terms_id'] and db.execute('SELECT lineage_id FROM work_terms WHERE id=?', (req['terms_id'],)).fetchone()['lineage_id'] != db.execute('SELECT lineage_id FROM work_terms WHERE id=?', (l['terms_id'],)).fetchone()['lineage_id']:
            raise ServiceError('VALIDATION', {'code': 'link', 'note': 'the request must open the drafted terms (or a revision in their lineage)'})
        db.execute('UPDATE mission_links SET request_id=? WHERE id=?', (req['id'], l['id']))
        return self.portfolio_view(db, principal, pid)

    # ---- contributions (§58) --------------------------------------------------------------------------------------------------
    def record_contribution(self, db, award, ms, decision_id, ev, terms):
        """Called after an ACCEPTED decision when the award is linked to a portfolio. Never a usefulness score or a mint."""
        req = db.execute('SELECT request_id FROM work_awards WHERE id=?', (award['id'],)).fetchone()
        link = db.execute('SELECT * FROM mission_links WHERE request_id=?', (req['request_id'],)).fetchone()
        if link is None:
            return None
        job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone(); contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        science = ev['science']
        ctype = 'verified_computation' if any(x['type'] == 'verification_passed' and x['result'] == 'passed' for x in ev['trace']) or terms['operation']['kind'] == 'legacy_task_replay' else 'reviewed_interpretation' if any(x['type'] == 'review_signature' for x in ev['trace']) else 'proposed_hypothesis'
        # deduplication by authorized lineage comparison: same input root + same outcome accepted before in this workspace
        prior = db.execute("SELECT c.id, c.award_id FROM contributions c JOIN work_awards a ON a.id=c.award_id JOIN work_milestones m ON m.award_id=a.id JOIN jobs j ON j.id=m.job_id JOIN contracts k ON k.id=j.contract_id WHERE c.workspace=? AND k.inputs_digest=? AND c.evidence_outcome=? AND c.award_id!=?", (award['workspace'], contract['inputs_digest'], str(job['outcome']), award['id'])).fetchone()
        if terms.get('purpose', {}).get('evidence_needed', '').startswith('exact canonical output') or 'replay' in terms['title'].lower():
            kind = 'commissioned_replication'
        elif science == 'INFEASIBLE':
            kind = 'accepted_negative_closing_branch'
        elif prior is not None:
            kind = 'reused_artifact'
        else:
            kind = 'new_finding'
        cid = 'wc_' + secrets.token_hex(6)
        learning = self._learning(db, link['portfolio_id'], link['node'], science, kind, job)
        db.execute('INSERT INTO contributions (id, workspace, portfolio_id, award_id, decision_id, contributor_provider_id, evidence_root, affected_node, reason, contribution_type, contribution_kind, evidence_outcome, dedup_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (cid, award['workspace'], link['portfolio_id'], award['id'], decision_id, award['provider_id'], ms['evidence_root'], link['node'], link['question'][:512], ctype, kind, str(job['outcome']),
                                                                                    json.dumps({'duplicate_of_award': prior['award_id'], 'note': 'same input root and outcome accepted before; a commissioned recheck is valuable as replication, an accidental duplicate is not a second entitlement'} if prior else None), now()))
        db.execute('UPDATE contributions SET learning_json=? WHERE id=?', (json.dumps(learning), cid))
        history.record(db, award['workspace'], 'service', 'work.contribution', 'contribution', cid, {'award_id': award['id'], 'node': link['node'], 'type': ctype, 'kind': kind, 'science': science, 'duplicate_warning': prior is not None,
                                                                                                  'not': 'no usefulness score, no issuance eligibility'})
        return cid

    # ---- mission learning records (§76.10) -------------------------------------------------------------------------------------
    LEARNING_CLASSES = ('contradicts_declared_verdict', 'confirmed_declared_verdict', 'new_evidence_for_unassessed_node', 'inconclusive')

    def _learning(self, db, pid, node, science, kind, job):
        """What an accepted finding means against the DECLARED node verdict of the imported (read-only) mission record. It never
        changes the declared decision: that takes a new verdict record, and the requester records such a change explicitly."""
        r = db.execute('SELECT snapshot_json FROM mission_portfolios WHERE id=?', (pid,)).fetchone()
        declared = (json.loads(r['snapshot_json'])['node_verdicts'].get(node) or {}).get('verdict') if r else None
        if kind == 'commissioned_replication' and job is not None and job['outcome'] == 'EXACT_MATCH':
            cls = 'confirmed_declared_verdict'; note = 'exact replay of the registered output confirms the existing evidence for this node'
        elif science in ('FEASIBLE', 'INFEASIBLE'):
            found = science == 'FEASIBLE'
            if declared is None:
                cls, note = 'new_evidence_for_unassessed_node', 'the declared record carried no verdict for this node'
            elif found == declared:
                cls, note = 'confirmed_declared_verdict', 'commissioned determination agrees with the declared verdict'
            else:
                cls, note = 'contradicts_declared_verdict', 'commissioned determination disagrees with the declared verdict; a decision change requires a new verdict record and an explicit requester record'
        else:
            cls, note = 'inconclusive', 'no scientific conclusion (diagnostic or not applicable)'
        return {'class': cls, 'declared_verdict': declared, 'commissioned_science': science, 'decision_changed': False, 'decision_change_note': None, 'note': note, 'no_impact_score': True}

    def record_learning(self, db, principal, pid, cid, body):
        """The requester states whether a finding actually changed a declared decision (explicit, attributable; never inferred)."""
        principal.require('work:request')
        self.portfolio_row(db, principal, pid)
        c = db.execute('SELECT * FROM contributions WHERE id=? AND portfolio_id=?', (cid, pid)).fetchone()
        if c is None:
            raise ServiceError('NOT_FOUND', 'contribution')
        if type(body.get('decision_changed')) is not bool or (body.get('note') is not None and type(body['note']) is not str):
            raise ServiceError('VALIDATION', {'code': 'fields', 'required': {'decision_changed': 'bool'}, 'optional': {'note': 'str'}})
        learning = json.loads(c['learning_json'] or '{}') or self._learning(db, pid, c['affected_node'], c['evidence_outcome'], c['contribution_kind'], None)
        learning.update({'decision_changed': body['decision_changed'], 'decision_change_note': (body.get('note') or '')[:400], 'recorded_by': principal.id, 'recorded_at': now()})
        db.execute('UPDATE contributions SET learning_json=? WHERE id=?', (json.dumps(learning), cid))
        history.record(db, principal.workspace, principal.id, 'work.mission_learning', 'contribution', cid, {'decision_changed': body['decision_changed'], 'class': learning['class']})
        return dict(dict(c), learning=learning)

    # ---- honest resource / energy evidence (§59) --------------------------------------------------------------------------------
    def resource_probe(self, db, principal):
        principal.require('work:read')
        out = {'host': 'DGX Spark GB10 (unified memory)', 'measurements_available': {}, 'not_available': [], 'attribution': 'device-wide readings; concurrent jobs share the device: per-job energy is an allocation, not a physical measurement'}
        try:
            q = subprocess.run(['nvidia-smi', '--query-gpu=power.draw,power.limit,utilization.gpu', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
            parts = [x.strip() for x in q.stdout.strip().split(',')] if q.returncode == 0 else []
            power = float(parts[0]) if parts and parts[0].replace('.', '', 1).isdigit() else None
            out['measurements_available']['gpu_power_w_sample'] = {'value': power, 'source': 'nvidia-smi power.draw (instantaneous sample)', 'accuracy': 'vendor telemetry; sampling interval and sensor accuracy undeclared here', 'integration': 'the compute engine integrates sampled power over the job as an ESTIMATE (mean power x interval, gaps ignored), labelled so'}
            out['measurements_available']['power_limit_w'] = parts[1] if len(parts) > 1 else None
            if parts and parts[1] in ('[N/A]', 'N/A'):
                out['not_available'].append('power limit (N/A on this device)')
        except Exception as exc:
            out['not_available'].append('nvidia-smi power sample (%s)' % type(exc).__name__)
        out['not_available'].append('calibrated hardware energy counter per job (no NVML energy counter path on this runtime; energy is reported unavailable on receipts unless the compute child returns a counter delta)')
        out['measurements_available']['wall_seconds'] = {'source': 'coordinator clock (claim to publication)', 'resolution': '1 s'}
        out['measurements_available']['cpu_time'] = {'source': 'child resource limits / rusage where recorded', 'note': 'per-attempt, not per-request in batched generation'}
        out['measurements_available']['tokens_and_work_units'] = {'source': 'runtime tokenizer / manifest work units', 'note': 'logical work, not energy'}
        out['rule'] = 'a provider cannot raise its payment by reporting a larger energy figure unless the contract explicitly prices a trusted measured unit; receipts keep measured / estimated / attested apart'
        return out

    # ---- physical-work evidence boundary (§60) ---------------------------------------------------------------------------------
    def ingest_observation(self, db, principal, body):
        """Ingest a signed observation package from a SIMULATED device: structure, signature, calibration, monotonic timestamps and
        declared plausibility bounds are checked; nothing is actuated; the package is labelled simulated end to end."""
        principal.require('work:request')
        pkg = body.get('package')
        if type(pkg) is not dict or pkg.get('schema') != OBSERVATION_SCHEMA:
            raise ServiceError('VALIDATION', {'code': 'schema', 'allowed': [OBSERVATION_SCHEMA]})
        required = ('device', 'calibration', 'samples', 'uncertainty', 'declared_bounds', 'signature_hex', 'simulated')
        missing = [k for k in required if k not in pkg]
        if missing:
            raise ServiceError('VALIDATION', {'code': 'missing_fields', 'missing': missing})
        if pkg['simulated'] is not True:
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'real_hardware_not_authorized', 'note': 'this order ingests simulated or synthetic observations only; hardware integration needs separate authorization'})
        dev = pkg['device']
        if type(dev) is not dict or set(dev) - {'device_id', 'public_key_hex', 'kind', 'label'} or not all(k in dev for k in ('device_id', 'public_key_hex', 'kind')):
            raise ServiceError('VALIDATION', {'code': 'device', 'required': ['device_id', 'public_key_hex', 'kind']})
        cal = pkg['calibration']
        if type(cal) is not dict or not all(k in cal for k in ('calibrated_at', 'reference', 'valid_until')) or not cal.get('reference'):
            raise ServiceError('VALIDATION', {'code': 'calibration_missing', 'required': ['calibrated_at', 'reference', 'valid_until'], 'note': 'an observation without a calibration record is not evidence'})
        samples = pkg['samples']
        if type(samples) is not list or not 1 <= len(samples) <= 4096 or not all(type(s) is dict and type(s.get('t')) is int and type(s.get('value')) is int for s in samples):
            raise ServiceError('VALIDATION', {'code': 'samples', 'note': 'list of {t: int seconds, value: int in the declared unit/scale}, 1..4096 (exact integers; no floats cross this boundary)'})
        if type(pkg.get('declared_bounds')) is not dict or 'unit' not in pkg['declared_bounds'] or type(pkg['declared_bounds'].get('scale')) is not int:
            raise ServiceError('VALIDATION', {'code': 'declared_bounds', 'required': ['unit', 'scale (integer divisor)', 'min?', 'max?', 'max_rate_per_s?']})
        signed = {k: v for k, v in pkg.items() if k != 'signature_hex'}
        if not crypto.verify(dev['public_key_hex'], merkle.canonical(_sign_safe(signed)), pkg['signature_hex']):
            raise ServiceError('VALIDATION', {'code': 'signature_invalid', 'note': 'the package does not verify under the device key: a changed sample or a wrong key'})
        ts = [s['t'] for s in samples]
        checks = [{'check': 'timestamps_monotonic', 'ok': all(b > a for a, b in zip(ts, ts[1:]))}]
        lo, hi = pkg['declared_bounds'].get('min'), pkg['declared_bounds'].get('max')
        checks.append({'check': 'values_within_declared_bounds', 'ok': all((lo is None or s['value'] >= lo) and (hi is None or s['value'] <= hi) for s in samples), 'bounds': pkg['declared_bounds']})
        rate = pkg['declared_bounds'].get('max_rate_per_s')
        if rate is not None and len(samples) > 1:
            checks.append({'check': 'rate_of_change_plausible', 'ok': all(abs(b['value'] - a['value']) / max(b['t'] - a['t'], 1) <= rate for a, b in zip(samples, samples[1:])), 'max_rate_per_s': rate})
        checks.append({'check': 'calibration_valid_at_observation', 'ok': cal['calibrated_at'] <= ts[0] <= cal['valid_until']})
        if not all(c['ok'] for c in checks):
            raise ServiceError('VALIDATION', {'code': 'implausible_sequence', 'checks': checks, 'note': 'rejected by the declared checks; not ingested'})
        oid = 'obs_' + secrets.token_hex(6)
        digest = hashlib.sha256(merkle.canonical(_sign_safe(signed))).hexdigest()
        db.execute('INSERT INTO physical_observations VALUES (?,?,?,?,?,?,?,?,?,?)', (oid, principal.workspace, dev['device_id'], dev['public_key_hex'], digest, json.dumps(_sign_safe(signed)), json.dumps(checks), 'simulated', principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'work.observation', 'physical_observation', oid, {'device_id': dev['device_id'], 'samples': len(samples), 'simulated': True, 'digest': digest})
        return {'id': oid, 'device_id': dev['device_id'], 'digest': digest, 'checks': checks, 'label': 'SIMULATED device observation: a signed record proves control of the device key under its custody model, not tamper-proof physical behaviour; replaying the analysis verifies the calculation, not that a sensor observed the world',
                'no_actuation': True, 'usable_as': ['dispute supplement', 'contribution of type measured_observation (labelled simulated)'], 'not_usable_as': ['proof of real infrastructure work', 'emission eligibility']}

    def observation_view(self, db, principal, oid):
        principal.require('work:read')
        o = db.execute('SELECT * FROM physical_observations WHERE id=? AND workspace=?', (oid, principal.workspace)).fetchone()
        if o is None:
            raise ServiceError('NOT_FOUND', 'observation')
        return {'id': oid, 'device_id': o['device_id'], 'digest': o['digest'], 'checks': json.loads(o['checks_json']), 'label': o['label'], 'package': json.loads(o['package_json']), 'created_at': o['created_at']}


def _sign_safe(obj):
    """Canonical form for observation packages: floats are encoded as repr strings (exact), matching the signer's convention."""
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, dict):
        return {k: _sign_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sign_safe(v) for v in obj]
    return obj


def sign_observation(private_key, package):
    """Fixture helper (tests/journeys): sign a package with the simulated device key using the same canonical form."""
    signed = {k: v for k, v in package.items() if k != 'signature_hex'}
    return crypto.sign(private_key, merkle.canonical(_sign_safe(signed)))
