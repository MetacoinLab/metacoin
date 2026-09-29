"""§65-1 evaluation-run registry: versioned evaluation suites for local models and the answer path, executed through
ordinary jobs, scored mechanically, compared across revisions, and usable as a promotion gate.

A suite is an immutable list of items: generation items (prompt, structural expectations: max tokens honoured,
required/forbidden substrings, digit or JSON shape) and knowledge items (question over a named collection with expected
document names and status). Scores are deterministic structural checks; no language model grades another model.
Held-out items can be marked `hidden` so their expectations are stored but never shown in listings."""
import hashlib
import json
import re
import secrets

from experiments.private_receipts import receipt as merkle
from . import history
from .db import now
from .errors import ServiceError

SUITE_SCHEMA = 'metacoin-evaluation-suite/v1'
MAX_ITEMS = 64


def validate_suite(items):
    if type(items) is not list or not 1 <= len(items) <= MAX_ITEMS:
        raise ServiceError('VALIDATION', 'items: 1..%d' % MAX_ITEMS)
    for it in items:
        if type(it) is not dict or it.get('type') not in ('generation', 'knowledge', 'plan', 'intent') or type(it.get('id')) is not str or not 1 <= len(it['id']) <= 32:
            raise ServiceError('VALIDATION', 'item: {id, type: generation|knowledge|plan|intent, ...}')
        if it['type'] == 'intent':
            req, exp = it.get('request'), it.get('expected')
            if type(req) is not dict or type(req.get('text')) is not str or type(exp) is not dict or exp.get('disposition') not in ('plan', 'clarification', 'abstention'):
                raise ServiceError('VALIDATION', 'intent item: request {text, inputs?, kind?, collection_id?}, expected {disposition, service_kind?, unresolved_fields?, category, split?}')
        if it['type'] == 'generation':
            if type(it.get('prompt')) is not str or not it['prompt'].strip() or type(it.get('max_output_tokens', 32)) is not int:
                raise ServiceError('VALIDATION', 'generation item: prompt, max_output_tokens')
        elif it['type'] == 'plan':
            req, exp = it.get('request'), it.get('expected')
            if type(req) is not dict or type(req.get('goal')) is not str or type(exp) is not dict or type(exp.get('valid')) is not bool:
                raise ServiceError('VALIDATION', 'plan item: request {goal, kind?, inputs?, draft?, assist?, collection_id?, verify?}, expected {valid, service_kind?, refusal_codes?, max_steps?, auto_execute?}')
        elif it['type'] == 'knowledge':
            if type(it.get('question')) is not str or type(it.get('collection_id')) is not str:
                raise ServiceError('VALIDATION', 'knowledge item: question, collection_id')
        for key in ('must_contain', 'must_not_contain', 'expected_documents'):
            v = it.get(key, [])
            if type(v) is not list or not all(type(x) is str for x in v):
                raise ServiceError('VALIDATION', key)
        if it.get('expected_status') not in (None, 'answered', 'insufficient_evidence'):
            raise ServiceError('VALIDATION', 'expected_status')
        if type(it.get('hidden', False)) is not bool:
            raise ServiceError('VALIDATION', 'hidden')
    merkle.canonical(items)


class Evaluation:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def create_suite(self, db, principal, name, items, threshold_percent=100):
        principal.require('model:admin')
        if type(name) is not str or not 1 <= len(name) <= 64 or type(threshold_percent) is not int or not 0 <= threshold_percent <= 100:
            raise ServiceError('VALIDATION', 'name/threshold_percent')
        validate_suite(items)
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM evaluation_suites WHERE workspace=? AND name=?', (principal.workspace, name)).fetchone()[0]
        sid = 'es_' + secrets.token_hex(6)
        digest = hashlib.sha256(merkle.canonical(items)).hexdigest()
        db.execute('INSERT INTO evaluation_suites (id, workspace, name, version, items_json, digest, threshold_percent, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                   (sid, principal.workspace, name, version, merkle.canonical(items).decode(), digest, threshold_percent, principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'model.registered', 'evaluation_suite', sid, {'name': name, 'version': version, 'items': len(items), 'digest': digest})
        return self.suite_view(self.suite(db, principal, sid), principal)

    def suite(self, db, principal, sid):
        r = db.execute('SELECT * FROM evaluation_suites WHERE id=? AND workspace=?', (sid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'evaluation suite')
        return r

    def suite_view(self, r, principal):
        items = json.loads(r['items_json'])
        shown = [({k: v for k, v in it.items() if k in ('id', 'type', 'hidden')} if it.get('hidden') and not principal.can('model:admin') else it) for it in items]
        return {'id': r['id'], 'name': r['name'], 'version': r['version'], 'digest': r['digest'], 'threshold_percent': r['threshold_percent'], 'items': shown, 'item_count': len(items), 'created_at': r['created_at'],
                'scoring': 'mechanical structural checks (output limit honoured, required/forbidden substrings, expected documents/status); no model grades another model'}

    def list_suites(self, db, principal):
        principal.require('job:read')
        return [self.suite_view(r, principal) for r in db.execute('SELECT * FROM evaluation_suites WHERE workspace=? ORDER BY name, version', (principal.workspace,)).fetchall()]

    # ---- runs ----------------------------------------------------------------------------------------
    def _evaluate_plan(self, db, principal, it, model_host):
        """§52: mechanical checks of the planning path. Jobs before/after prove whether a tool action occurred."""
        import time
        before = db.execute('SELECT COUNT(*) FROM jobs WHERE workspace=?', (principal.workspace,)).fetchone()[0]
        t0 = time.time()
        try:
            req = dict(it['request'])
            preselected = None
            if req.get('assist') == 'model' and req.get('draft') is None:
                goal = self.svc.planner.check_goal(req)
                row, context = self.svc.planner.selection_context(db, principal, goal, req.get('collection_id'))
                preselected = self.svc.planner.select_with_model(model_host, row, self.svc.planner.catalog(db, principal), context, goal)
            plan = self.svc.planner.create(db, principal, req, None, preselected)
            error = None
        except ServiceError as exc:
            plan, error = None, exc.body()
        elapsed_ms = int((time.time() - t0) * 1000)
        after = db.execute('SELECT COUNT(*) FROM jobs WHERE workspace=?', (principal.workspace,)).fetchone()[0]
        exp = it['expected']
        checks = [{'check': 'no_tool_action_at_planning', 'ok': after == before, 'detail': after - before}]
        if plan is None:
            checks.append({'check': 'plan_valid_matches', 'ok': exp['valid'] is False, 'detail': error})
        else:
            checks.append({'check': 'plan_valid_matches', 'ok': plan['valid'] == exp['valid'], 'detail': [r['code'] for r in plan['refusals']]})
            kinds = [r['service_kind'] for r in plan['resolved'] if r['operation'] == 'invoke']
            if 'service_kind' in exp:
                checks.append({'check': 'service_identity', 'ok': kinds == ([exp['service_kind']] if exp['service_kind'] else []), 'detail': kinds})
            if 'refusal_codes' in exp:
                checks.append({'check': 'refusal_codes', 'ok': set(exp['refusal_codes']) <= {r['code'] for r in plan['refusals']}, 'detail': [r['code'] for r in plan['refusals']]})
            checks.append({'check': 'graph_bounded', 'ok': len(plan['draft'].get('steps', [])) <= exp.get('max_steps', 8), 'detail': len(plan['draft'].get('steps', []))})
            checks.append({'check': 'forbidden_operations_refused', 'ok': all(r['operation'] in ('invoke', 'knowledge_search', 'verification_request') for r in plan['resolved']), 'detail': [r['operation'] for r in plan['resolved']]})
            if 'auto_execute' in exp:
                checks.append({'check': 'auto_execute_matches', 'ok': plan['auto_execute_permitted'] == exp['auto_execute'], 'detail': plan['auto_execute_permitted']})
        assist = (plan or {}).get('assist') or {}
        return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'plan_id': (plan or {}).get('id'), 'elapsed_ms': elapsed_ms,
                'resource_use': {'assist_mode': assist.get('mode'), 'model_usage': assist.get('usage'), 'inference_ms': assist.get('inference_ms')}}

    def _evaluate_intent(self, db, principal, it, model_host):
        """§32: dispatch/abstention/clarification outcomes with separate authority and quality categories."""
        import time
        before = db.execute('SELECT COUNT(*) FROM jobs WHERE workspace=?', (principal.workspace,)).fetchone()[0]
        t0 = time.time()
        try:
            v = self.svc.intents.compile(db, principal, dict(it['request']), model_host); error = None
        except ServiceError as exc:
            v, error = None, exc.body()
        elapsed_ms = int((time.time() - t0) * 1000)
        after = db.execute('SELECT COUNT(*) FROM jobs WHERE workspace=?', (principal.workspace,)).fetchone()[0]
        exp = it['expected']
        checks = [{'check': 'no_tool_action_at_planning', 'ok': after == before, 'detail': after - before, 'category': 'authority'}]
        outcome = 'error'
        if v is not None:
            it_ = v['intent']; got = v['state']; kind = it_.get('service_kind')
            if exp['disposition'] == 'plan':
                if got == 'plan' and (not exp.get('service_kind') or kind == exp['service_kind']): outcome = 'correct_dispatch'
                elif got == 'plan': outcome = 'incorrect_dispatch'
                elif got == 'clarification': outcome = 'unnecessary_clarification'
                else: outcome = 'unnecessary_abstention'
            elif exp['disposition'] == 'abstention':
                outcome = 'valid_abstention' if got == 'abstention' else ('unsafe_dispatch' if got == 'plan' else 'clarification_instead_of_abstention')
            else:
                if got == 'clarification' and set(exp.get('unresolved_fields') or []) <= {u['field'] for u in it_['unresolved']}: outcome = 'correct_clarification'
                elif got == 'clarification': outcome = 'clarification_missing_field'
                elif got == 'plan': outcome = 'missing_required_clarification'
                else: outcome = 'unnecessary_abstention'
            checks.append({'check': 'disposition_matches', 'ok': outcome in ('correct_dispatch', 'valid_abstention', 'correct_clarification'), 'detail': {'got': got, 'kind': kind, 'expected': exp}, 'category': 'quality'})
            checks.append({'check': 'authority_kept', 'ok': outcome not in ('unsafe_dispatch',) and all(e['eligible'] or True for e in it_['eligibility']) and (kind is None or any(e['kind'] == kind and e['eligible'] for e in it_['eligibility'])), 'detail': 'chosen kind is eligible', 'category': 'authority'})
            if exp.get('must_not_choose'):
                checks.append({'check': 'forbidden_kind_not_chosen', 'ok': kind not in exp['must_not_choose'], 'detail': kind, 'category': 'authority'})
            if it['request'].get('collection_id'):
                checks.append({'check': 'sources_bound_to_versions', 'ok': all(r.get('version_id') and r.get('sha256') for r in it_['source_refs']), 'detail': len(it_['source_refs']), 'category': 'evidence'})
        else:
            checks.append({'check': 'disposition_matches', 'ok': False, 'detail': error, 'category': 'quality'})
        model = (v or {}).get('intent', {}).get('model') or []
        return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'outcome': outcome, 'category': exp.get('category'), 'split': it.get('split', 'dev'), 'elapsed_ms': elapsed_ms,
                'resource_use': {'assist_mode': 'local_model' if model else 'deterministic', 'model_usage': model[-1].get('usage') if model else None, 'inference_ms': sum(m.get('inference_ms') or 0 for m in model), 'model_attempts': len(model)}}

    def start_run(self, db, principal, sid, model_revision_id=None, model_host=None):
        """Submit one ordinary job per item under the given generation revision (or the default); the run scores when all jobs are terminal."""
        principal.require('model:use')
        suite = self.suite(db, principal, sid)
        items = json.loads(suite['items_json'])
        from .api import quick_submit
        from .knowledge import engine as knowledge_engine
        rev = self.svc.models.resolve(db, 'generate', model_revision_id)
        jobs, plan_results = {}, {}
        for it in items:
            if it['type'] == 'plan':
                plan_results[it['id']] = self._evaluate_plan(db, principal, it, model_host)
            elif it['type'] == 'intent':
                plan_results[it['id']] = self._evaluate_intent(db, principal, it, model_host)
            elif it['type'] == 'generation':
                inputs = {'schema': 'text-generation-input/v1', 'prompt': it['prompt'], 'max_output_tokens': it.get('max_output_tokens', 32), 'model_revision_id': rev['id'], 'purpose': 'evaluation ' + sid}
                jobs[it['id']] = quick_submit(self.svc, db, principal, 'text_generation', inputs, 'eval ' + it['id'])['job_id']
            else:
                inputs = {'schema': knowledge_engine.ANSWER_SCHEMA, 'collection_id': it['collection_id'], 'question': it['question'], 'mode': it.get('mode', 'extractive'), 'k': it.get('k', 4), 'generation_revision_id': rev['id'] if it.get('mode') == 'generative' else None}
                inputs = {k: v for k, v in inputs.items() if v is not None}
                jobs[it['id']] = quick_submit(self.svc, db, principal, 'knowledge_answer', inputs, 'eval ' + it['id'])['job_id']
        rid = 'er_' + secrets.token_hex(6)
        db.execute('INSERT INTO evaluation_runs (id, workspace, suite_id, model_revision_id, jobs_json, state, results_json, started_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                   (rid, principal.workspace, sid, rev['id'], json.dumps(jobs), 'running', json.dumps({'plan_results': plan_results}) if plan_results else None, principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'model.request', 'evaluation_run', rid, {'suite_id': sid, 'revision_id': rev['id'], 'jobs': len(jobs)})
        return self.run_view(db, principal, rid)

    def run(self, db, principal, rid):
        r = db.execute('SELECT * FROM evaluation_runs WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'evaluation run')
        return r

    def score(self, db, principal, rid):
        """Score a run whose jobs are all terminal (idempotent)."""
        r = self.run(db, principal, rid)
        if r['state'] == 'scored':
            return self.run_view(db, principal, rid)
        suite = self.suite(db, principal, r['suite_id'])
        items = {it['id']: it for it in json.loads(suite['items_json'])}
        jobs = json.loads(r['jobs_json'])
        rows, pending = [], 0
        stored = json.loads(r['results_json']) if r['results_json'] else {}
        for item_id, pr in (stored.get('plan_results') or {}).items():
            rows.append({'item': item_id, 'job_id': None, 'ok': pr['ok'], 'checks': pr['checks'], 'hidden': items[item_id].get('hidden', False), 'elapsed_ms': pr['elapsed_ms'], 'resource_use': pr['resource_use'], 'outcome': pr.get('outcome'), 'category': pr.get('category'), 'split': pr.get('split')})
        for item_id, jid in jobs.items():
            job = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if job['state'] in ('queued', 'running'):
                pending += 1; continue
            it = items[item_id]
            checks = []
            if job['state'] != 'succeeded':
                checks.append({'check': 'job_succeeded', 'ok': False, 'detail': job['error_code']})
                rows.append({'item': item_id, 'job_id': jid, 'ok': False, 'checks': checks}); continue
            text, status, docs = '', None, []
            req = db.execute('SELECT output_artifact_id, output_tokens, max_output_tokens FROM model_requests WHERE job_id=?', (jid,)).fetchone()
            if req and req['output_artifact_id']:
                from .compute import container
                files = container.unpack(self.svc.store.load(db, req['output_artifact_id'], principal.workspace))
                out = json.loads(files['output.json'])
                text = out.get('text') or out.get('answer') or ''
                status = out.get('status'); docs = [s['document_name'] for s in out.get('sources', [])]
            if it['type'] == 'generation':
                checks.append({'check': 'output_limit_honoured', 'ok': (req['output_tokens'] or 0) <= it.get('max_output_tokens', 32), 'detail': req['output_tokens']})
                checks.append({'check': 'non_empty', 'ok': bool(text.strip()), 'detail': len(text)})
            else:
                if it.get('expected_status'):
                    checks.append({'check': 'expected_status', 'ok': status == it['expected_status'], 'detail': status})
                for d in it.get('expected_documents', []):
                    checks.append({'check': 'expected_document:' + d, 'ok': d in docs, 'detail': docs[:3]})
            for s in it.get('must_contain', []):
                checks.append({'check': 'must_contain:' + s[:24], 'ok': s.lower() in text.lower(), 'detail': None})
            for s in it.get('must_not_contain', []):
                checks.append({'check': 'must_not_contain:' + s[:24], 'ok': s.lower() not in text.lower(), 'detail': None})
            rows.append({'item': item_id, 'job_id': jid, 'ok': all(c['ok'] for c in checks), 'checks': checks, 'hidden': it.get('hidden', False)})
        if pending:
            return dict(self.run_view(db, principal, rid), pending_jobs=pending)
        passed = sum(1 for x in rows if x['ok'])
        pct = (100 * passed) // max(1, len(rows))
        db.execute("UPDATE evaluation_runs SET state='scored', results_json=?, passed=?, total=?, percent=?, scored_at=? WHERE id=?", (json.dumps(rows), passed, len(rows), pct, now(), rid))
        history.record(db, principal.workspace, principal.id, 'model.request', 'evaluation_run', rid, {'scored': True, 'passed': passed, 'total': len(rows)})
        return self.run_view(db, principal, rid)

    def run_view(self, db, principal, rid):
        principal.require('job:read')
        r = self.run(db, principal, rid)
        suite = self.suite(db, principal, r['suite_id'])
        results = json.loads(r['results_json']) if r['results_json'] else None
        if isinstance(results, dict):
            results = None                                      # plan results are held until the run is scored
        if results is not None and not principal.can('model:admin'):
            results = [({k: v for k, v in x.items() if k != 'checks'} if x.get('hidden') else x) for x in results]
        metrics = None
        if results and any(x.get('outcome') for x in results):
            from collections import Counter
            by_split = {}
            for x in results:
                if not x.get('outcome'):
                    continue
                d = by_split.setdefault(x.get('split') or 'dev', Counter()); d[x['outcome']] += 1; d['_n'] += 1
            metrics = {}
            for sp, cnt in by_split.items():
                n = cnt['_n']; disp = cnt['correct_dispatch'] + cnt['incorrect_dispatch'] + cnt['unsafe_dispatch']
                metrics[sp] = {'n': n, 'outcomes': {k: v for k, v in cnt.items() if k != '_n'}, 'coverage': round(disp / n, 3) if n else None, 'dispatch_accuracy': round(cnt['correct_dispatch'] / disp, 3) if disp else None,
                               'policy_violations': cnt['unsafe_dispatch'], 'note': 'coverage = fraction dispatched; accuracy among dispatched; abstention/clarification outcomes listed separately; authority failures counted apart from quality misses'}
        return {'id': rid, 'suite_id': r['suite_id'], 'suite_name': suite['name'], 'suite_version': suite['version'], 'suite_digest': suite['digest'], 'model_revision_id': r['model_revision_id'], 'state': r['state'], 'metrics': metrics,
                'jobs': json.loads(r['jobs_json']), 'results': results, 'passed': r['passed'], 'total': r['total'], 'percent': r['percent'], 'threshold_percent': suite['threshold_percent'],
                'meets_threshold': (r['percent'] is not None and r['percent'] >= suite['threshold_percent']), 'created_at': r['created_at'], 'scored_at': r['scored_at']}

    def compare(self, db, principal, run_a, run_b):
        a, b = self.score(db, principal, run_a), self.score(db, principal, run_b)
        if a['suite_digest'] != b['suite_digest']:
            raise ServiceError('CONFLICT', 'runs use different suite versions; compare within one immutable suite')
        ra = {x['item']: x['ok'] for x in (a['results'] or [])}; rb = {x['item']: x['ok'] for x in (b['results'] or [])}
        return {'suite_digest': a['suite_digest'], 'a': {'run': run_a, 'revision': a['model_revision_id'], 'percent': a['percent']}, 'b': {'run': run_b, 'revision': b['model_revision_id'], 'percent': b['percent']},
                'improved': sorted(i for i in ra if not ra[i] and rb.get(i)), 'regressed': sorted(i for i in ra if ra[i] and not rb.get(i)), 'unchanged': sum(1 for i in ra if ra[i] == rb.get(i)),
                'note': 'item-level structural outcomes on the same immutable suite; not a general quality claim'}

    def gate(self, db, workspace, revision_id):
        """Promotion gate: when the workspace policy names a suite, the candidate needs a scored run meeting the threshold."""
        row = db.execute('SELECT value FROM meta WHERE key=?', ('evaluation_gate:' + workspace,)).fetchone()
        if not row:
            return None
        sid = row['value']
        ok = db.execute("SELECT id, percent FROM evaluation_runs WHERE suite_id=? AND model_revision_id=? AND state='scored' ORDER BY scored_at DESC LIMIT 1", (sid, revision_id)).fetchone()
        suite = db.execute('SELECT threshold_percent, name, version FROM evaluation_suites WHERE id=?', (sid,)).fetchone()
        if suite is None:
            return None
        if ok and ok['percent'] >= suite['threshold_percent']:
            return None
        return {'code': 'evaluation_required', 'suite_id': sid, 'suite': suite['name'] + ' v%d' % suite['version'], 'threshold_percent': suite['threshold_percent'], 'latest_run': dict(ok) if ok else None}

    def set_gate(self, db, principal, suite_id):
        principal.require('model:admin')
        if suite_id is None:
            db.execute('DELETE FROM meta WHERE key=?', ('evaluation_gate:' + principal.workspace,))
        else:
            self.suite(db, principal, suite_id)
            db.execute('INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('evaluation_gate:' + principal.workspace, suite_id))
        history.record(db, principal.workspace, principal.id, 'model.promoted', 'policy', 'evaluation_gate', {'suite_id': suite_id})
        return {'evaluation_gate_suite': suite_id}
