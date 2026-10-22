"""Product views for the lunar kinds: scope, provenance, assumptions, verification state and reproduction path for anyone
who may read the job; the private result (grids, paths, manifest) only for the owner, administrators and the designated
reviewer, exactly as the job view policy decides."""
import base64
import json

from ..errors import ServiceError
from . import operations, reference, scenario, sources

KINDS = operations.KINDS


class Lunar:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def _job(self, db, principal, jid):
        principal.require('job:read')
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (jid, principal.workspace)).fetchone()
        if job is None or job['kind'] not in KINDS:
            raise ServiceError('NOT_FOUND', 'lunar product job')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        private = principal.can('job:read_private') or (principal.role == 'reviewer' and contract['reviewer_id'] == principal.id)
        return job, contract, private

    def _result(self, db, job):
        if job['state'] != 'succeeded':
            return None
        vault = self.svc.store.load_json(db, job['evidence_artifact_id'], job['workspace'])
        from experiments.work_contracts import acceptance as v0
        return operations.reassemble(v0.full_values(vault, vault['receipt']['root']))

    def _inputs(self, db, job, contract):
        vault = self.svc.store.load_json(db, contract['input_artifact_id'], job['workspace'])
        return {f['name']: f['value'] for f in vault['fields']}['inputs']

    def list(self, db, principal, kind=None):
        principal.require('job:read')
        q = 'SELECT id, kind, state, outcome, created_at FROM jobs WHERE workspace=? AND kind IN (?,?,?)' + (' AND kind=?' if kind else '') + ' ORDER BY created_at DESC LIMIT 200'
        rows = db.execute(q, (principal.workspace,) + KINDS + ((kind,) if kind else ())).fetchall()
        return [{'job_id': r['id'], 'kind': r['kind'], 'state': r['state'], 'outcome': r['outcome'] if principal.can('job:read_private') else None, 'created_at': r['created_at']} for r in rows]

    def view(self, db, principal, jid):
        job, contract, private = self._job(db, principal, jid)
        jv = self.svc.jobs.view(db, principal, job, contract)
        inputs = self._inputs(db, job, contract)
        src = inputs.get('source') if job['kind'] == 'terrain_tile' else inputs['terrain']['source'] if job['kind'] == 'route_feasibility' else inputs['base_terrain']['source']
        prov = {k: src.get(k) for k in ('kind', 'label', 'product_id', 'data_set_id', 'product_version', 'source_url', 'label_url', 'image_sha256', 'label_sha256', 'acquired_at', 'projection', 'datum', 'units', 'orientation', 'origin_line', 'origin_sample', 'lines', 'samples', 'first_line_latitude_milli_deg', 'first_sample_longitude_milli_deg', 'redistribution', 'region', 'what_this_is_not')}
        from . import route as route_mod, terrain as terrain_mod
        out = {'schema': 'metacoin-lunar-product/v0', 'job_id': jid, 'kind': job['kind'], 'state': job['state'], 'job': jv, 'outcome': jv.get('outcome'), 'provenance': prov,
               'scope': {'terrain_tile': 'grid-scale elevation and slope of the declared tile', 'route_feasibility': 'exact bounded search on the declared tile and rover model', 'scenario_pack': 'declared model only; variations perturb declared inputs'}[job['kind']],
               'assumptions': terrain_mod.ASSUMPTIONS + (route_mod.ASSUMPTIONS if job['kind'] != 'terrain_tile' else []), 'model_id': operations.MODEL_IDS[job['kind']], 'implementation_digest': operations.implementation_digest(), 'reference_checker': {'id': reference.CHECKER_ID, 'digest': operations.reference_digest()},
               'verifications': [{'id': v['id'], 'class': v['class'], 'state': v['state'], 'support': __import__('metacoin_service.verification', fromlist=['SUPPORT']).SUPPORT[job['kind']].get(v['class'])} for v in self.svc.verification.list(db, principal, jid)],
               'reproduction': {'command': {'terrain_tile': 'PYTHONPATH=. python3 -m metacoin_service.lunar terrain inputs.json --png preview.png', 'route_feasibility': 'PYTHONPATH=. python3 -m metacoin_service.lunar route inputs.json', 'scenario_pack': scenario.REPRODUCE}[job['kind']],
                                'check': 'PYTHONPATH=. python3 -m metacoin_service.lunar check %s inputs.json result.json' % job['kind'], 'inputs': 'the frozen input vault (private to the requester, the reviewer and administrators) or the pack download'},
               'downloads': {'preview_png': '/api/v1/lunar/products/%s/preview.png' % jid if job['kind'] in ('terrain_tile', 'route_feasibility') else None, 'product': '/api/v1/lunar/products/%s/download' % jid},
               'what_this_is_not': 'a traversability, traction, illumination or communication guarantee; a coarse DEM does not establish safe physical navigation'}
        if private:
            res = self._result(db, job)
            if res is not None and 'preview_png_base64' in res:
                res = dict(res); res.pop('preview_png_base64')
            out['result'] = res; out['inputs'] = inputs; out['summary'] = jv.get('summary')
            if res is not None and job['kind'] == 'route_feasibility':
                out['decision_reading'] = {'FEASIBLE': 'a route exists under the declared model with margin for the slope uncertainty', 'INFEASIBLE': 'no route exists under the declared model even with every uncertainty resolved favourably (closure witness or energy bound)', 'INDETERMINATE': 'the answer depends on uncertain or missing cells, or a search limit was hit: buy evidence or change the model, do not force a decision'}[res['outcome']]
        return out

    def preview_png(self, db, principal, jid):
        job, contract, private = self._job(db, principal, jid)
        if not private:
            raise ServiceError('FORBIDDEN', 'preview follows the private result policy')
        res = self._result(db, job)
        if res is None:
            raise ServiceError('CONFLICT', 'no result yet')
        if job['kind'] == 'route_feasibility':
            from . import terrain as terrain_mod
            res = terrain_mod.compute(self._inputs(db, job, contract)['terrain'])
        return base64.b64decode(res['preview_png_base64'])

    def download(self, db, principal, jid):
        """The product file: the scenario pack zip (recomputed deterministically from the frozen inputs and compared with the committed manifest) or the JSON product."""
        job, contract, private = self._job(db, principal, jid)
        if not private:
            raise ServiceError('FORBIDDEN', 'download follows the private result policy')
        res = self._result(db, job)
        if res is None:
            raise ServiceError('CONFLICT', 'no result yet')
        inputs = self._inputs(db, job, contract)
        if job['kind'] == 'scenario_pack':
            fresh, data = scenario.pack_zip(inputs)
            if fresh['manifest']['pack_id'] != res['manifest']['pack_id']:
                raise ServiceError('CONFLICT', {'code': 'pack_not_reproducible', 'note': 'the installed implementation no longer reproduces the committed manifest; export the evidence vault instead'})
            return data, 'application/zip', 'lunar-scenario-pack-%s.zip' % jid
        out = dict(res); out.pop('preview_png_base64', None)
        return json.dumps({'schema': 'metacoin-lunar-product-file/v0', 'job_id': jid, 'kind': job['kind'], 'inputs': inputs, 'result': out}, separators=(',', ':')).encode(), 'application/json', 'lunar-%s-%s.json' % (job['kind'], jid)

    def sources(self, db, principal):
        principal.require('job:read')
        return sources.catalog()

    def example(self, db, principal, name, source='apollo11_32'):
        principal.require('job:read')
        from .__main__ import example
        if source not in sources.BUNDLED:
            raise ServiceError('NOT_FOUND', 'bundled source')
        try:
            return example(name, source)
        except SystemExit:
            raise ServiceError('VALIDATION', {'code': 'example', 'allowed': ['terrain', 'route', 'pack']})
