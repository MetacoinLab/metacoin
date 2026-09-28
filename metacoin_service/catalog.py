"""Versioned service catalog and quotes.

A catalog entry describes an INSTALLED, allowlisted implementation (a job kind of this service).
Descriptions are data: registration binds a name/version to one of the installed kinds and can
never add code, override policy, or point a buyer somewhere else. Status is reported in four
separate facts: registered (row exists), installed (kind implemented in this build), available
(current provider mode and configuration allow invocation), externally_validated (never, here).

Quotes bind: service id + revision, the validated request digest (or an input-scope digest), the
requesting principal, quantity ceiling, amount (integer base units), unit, asset, network,
recipient, pricing policy revision, expiry, provider mode. A quote is immutable once offered; a
price preview is a different object (no id, nothing persisted). Acceptance is persisted and
idempotent; consumption is atomic and unique. A quote grants nothing beyond one bounded invocation.
"""
import hashlib
import json
import secrets
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import energy_analysis as energy
from . import history, science, temporal
from .datasets import add_edge
from .db import now
from .errors import ServiceError

QUOTE_TTL_SECONDS = 15 * 60
UNIT = 'evaluation'
PRICING_REVISION = 'pricing/2026-09-24-r1'

# Implemented kinds and their machine-readable descriptions (schemas are descriptive; the server
# validators are authoritative and are what the `validate` operation runs).
INSTALLED = {
    'energy_audit': {'model_id': energy.MODEL_ID, 'result_schema': energy.RESULT_SCHEMA, 'verifier': 'local-energy-audit/v1',
                     'input_type': 'energy_intervals', 'dataset_kind': 'energy_intervals',
                     'input_schema': {'type': 'object', 'required': ['available_low', 'available_high', 'reserve', 'segments', 'units', 'assumptions', 'provenance', 'private_label'],
                                      'properties': {'available_low': {'type': 'integer', 'unit': 'mJ', 'minimum': 0}, 'available_high': {'type': 'integer', 'unit': 'mJ'},
                                                     'reserve': {'type': 'integer', 'unit': 'mJ'}, 'segments': {'type': 'array', 'maxItems': 128, 'items': {'duration': 's>=1', 'power_low': 'mW', 'power_high': 'mW'}},
                                                     'units': {'const': energy.UNITS}, 'assumptions': {'const': energy.ASSUMPTIONS}, 'provenance': {'enum': ['synthetic', 'declared_unverified']}}},
                     'output_fields': ['outcome', 'required_low', 'required_high', 'worst_margin', 'best_margin', 'additional_usable_energy', 'margin_explanation'],
                     'limits': {'max_segments': 128, 'aggregate_max': 2 ** 53 - 1}, 'action_entitlement': True},
    'temporal_energy': {'model_id': temporal.MODEL_ID, 'result_schema': temporal.RESULT_SCHEMA, 'verifier': 'temporal-energy-verifier/v1',
                        'input_type': 'temporal_series', 'dataset_kind': 'temporal_series',
                        'input_schema': {'type': 'object', 'required': ['schema', 'capacity', 'initial_low', 'initial_high', 'reserve', 'segments', 'units', 'assumptions', 'provenance', 'private_label'],
                                         'properties': {'schema': {'const': temporal.INPUT_SCHEMA}, 'capacity': {'type': 'integer', 'unit': 'mJ', 'minimum': 1},
                                                        'initial_low': {'type': 'integer', 'unit': 'mJ'}, 'initial_high': {'type': 'integer', 'unit': 'mJ'}, 'reserve': {'type': 'integer', 'unit': 'mJ'},
                                                        'segments': {'type': 'array', 'maxItems': temporal.MAX_SEGMENTS, 'items': {'duration': 's>=1', 'harvest_low': 'mW', 'harvest_high': 'mW', 'load_low': 'mW', 'load_high': 'mW', 'leakage_low': 'mW', 'leakage_high': 'mW'}},
                                                        'units': {'const': temporal.UNITS}, 'assumptions': {'const': temporal.ASSUMPTIONS}}},
                        'output_fields': ['outcome', 'first_uncertain_boundary', 'first_infeasible_boundary', 'min_reserve_margin_pessimistic', 'spill_bounds', 'envelope_low', 'envelope_high'],
                        'limits': {'max_segments': temporal.MAX_SEGMENTS, 'max_horizon_seconds': temporal.MAX_HORIZON_SECONDS}, 'action_entitlement': False},
    'safe_runtime': {'model_id': science.SAFE_RUNTIME_MODEL, 'result_schema': science.SAFE_RUNTIME_SCHEMA, 'verifier': 'service-science/v1', 'input_type': 'inline',
                     'dataset_kind': None, 'input_schema': {'type': 'object', 'required': ['available_low', 'available_high', 'reserve', 'fixed_segments', 'variable_power_low', 'variable_power_high', 'duration_cap', 'units', 'assumptions', 'provenance', 'private_label']},
                     'output_fields': ['status', 'safe_duration', 'margin_at_duration', 'maximality'], 'limits': {'max_segments': 128}, 'action_entitlement': False},
    'plan_comparison': {'model_id': science.COMPARISON_MODEL, 'result_schema': science.COMPARISON_SCHEMA, 'verifier': 'service-science/v1', 'input_type': 'inline',
                        'dataset_kind': None, 'input_schema': {'type': 'object', 'required': ['candidates', 'objective', 'private_label']},
                        'output_fields': ['selected_id', 'candidates', 'feasible_ids', 'indeterminate_ids', 'infeasible_ids'], 'limits': {'max_candidates': science.MAX_CANDIDATES, 'max_total_segments': science.MAX_TOTAL_SEGMENTS}, 'action_entitlement': False},
    'task_selection': {'model_id': science.SELECTION_MODEL, 'result_schema': science.SELECTION_SCHEMA, 'verifier': 'service-science/v1', 'input_type': 'inline',
                       'dataset_kind': None, 'input_schema': {'type': 'object', 'required': ['available_low', 'available_high', 'reserve', 'fixed_segments', 'optional_tasks', 'duration_cap', 'units', 'assumptions', 'provenance', 'private_label']},
                       'output_fields': ['status', 'selected_ids', 'total_value', 'energy_margin', 'duration_margin'], 'limits': {'max_optional_tasks': science.MAX_OPTIONAL_TASKS}, 'action_entitlement': False},
}
from .compute import inputs as compute_inputs, manifests as compute_manifests
for _kind, _m in compute_manifests.MANIFESTS.items():
    INSTALLED[_kind] = {'model_id': _m['model_id'], 'result_schema': _m['result_schema'], 'verifier': _m['manifest_id'] + '-verifier', 'input_type': 'compute',
                        'dataset_kind': None, 'input_schema': {'type': 'object', 'schema': _m['input_schema'], 'manifest': _m['manifest_id'], 'devices': _m['devices'],
                                                               'precision': _m['precision'], 'limits': _m['limits'], 'numerical_policy': _m['numerical_policy']},
                        'output_fields': _m['outputs'], 'limits': _m['limits'], 'action_entitlement': False,
                        'compute': {'work_unit': _m['work_unit'], 'price_basis': _m['price_basis'], 'checkpoint_format': _m['checkpoint_format'],
                                    'verification_modes': _m['verification_modes'], 'verification_policy': _m['verification_policy']}}

from .models import engine as model_engine, service as model_svc
INSTALLED['text_generation'] = {'model_id': model_engine.MODEL_IDS['text_generation'], 'result_schema': model_engine.RESULT_SCHEMAS['text_generation'], 'verifier': 'model-runtime/v1', 'input_type': 'model',
                                'dataset_kind': None, 'input_schema': {'type': 'object', 'schema': model_engine.GENERATION_SCHEMA, 'required': ['schema', 'max_output_tokens', 'messages|prompt'],
                                                                       'properties': {'messages': [{'role': 'system|user|assistant', 'content': 'string'}], 'prompt': 'string', 'max_output_tokens': 'int 1..model_max_output_tokens',
                                                                                      'temperature_percent': 'int 0..200 (0 = greedy)', 'top_p_percent': 'int 1..100', 'seed': 'int|null', 'stop': ['<=4 strings'], 'model_revision_id': 'mr_... or null (promoted default)'}},
                                'output_fields': ['text', 'finish_reason', 'usage', 'config', 'model_revision_id'], 'limits': {'max_output_tokens': 1024, 'max_input_tokens': 4096, 'max_messages': 32}, 'action_entitlement': False,
                                'model': {'work_unit': model_svc.WORK_UNIT['text_generation'], 'completion': 'the runtime produced text under the recorded configuration; not a factual or scientific claim', 'privacy': 'prompt and output private to owner and designated reviewer; local runtime only'}}
INSTALLED['text_embedding'] = {'model_id': model_engine.MODEL_IDS['text_embedding'], 'result_schema': model_engine.RESULT_SCHEMAS['text_embedding'], 'verifier': 'model-runtime/v1', 'input_type': 'model',
                               'dataset_kind': None, 'input_schema': {'type': 'object', 'schema': model_engine.EMBEDDING_SCHEMA, 'required': ['schema', 'texts'],
                                                                      'properties': {'texts': ['1..256 strings'], 'truncate': 'bool (false refuses silent truncation)', 'model_revision_id': 'mr_... or null'}},
                               'output_fields': ['items', 'dim', 'pooling', 'normalized', 'tokens', 'truncated', 'model_revision_id'], 'limits': {'max_items': 256, 'max_text_chars': 8000}, 'action_entitlement': False,
                               'model': {'work_unit': model_svc.WORK_UNIT['text_embedding'], 'completion': 'vectors stored as a private artifact; comparable only within one revision/pooling/normalization', 'privacy': 'vectors are sensitive derived data; never exported by default'}}

INSTALLED['knowledge_index'] = {'model_id': model_engine.MODEL_IDS['knowledge_index'], 'result_schema': model_engine.RESULT_SCHEMAS['knowledge_index'], 'verifier': 'knowledge-engine/v1', 'input_type': 'knowledge',
                                'dataset_kind': None, 'input_schema': {'type': 'object', 'schema': 'knowledge-index-input/v1', 'required': ['schema', 'collection_id', 'index_id'], 'note': 'created through POST /api/v1/knowledge/collections/{id}/indexes'},
                                'output_fields': ['index_id', 'chunks', 'dim', 'truncated_chunks'], 'limits': {'max_chunks_per_index': 5000}, 'action_entitlement': False,
                                'model': {'work_unit': 'index build', 'completion': 'vectors for every authorized chunk of the snapshot published as one encrypted artifact', 'privacy': 'vectors never leave the host'}}
INSTALLED['knowledge_answer'] = {'model_id': model_engine.MODEL_IDS['knowledge_answer'], 'result_schema': model_engine.RESULT_SCHEMAS['knowledge_answer'], 'verifier': 'knowledge-engine/v1', 'input_type': 'knowledge',
                                 'dataset_kind': None, 'input_schema': {'type': 'object', 'schema': 'knowledge-answer-input/v1', 'required': ['schema', 'collection_id', 'question', 'mode'],
                                                                        'properties': {'mode': 'extractive | generative', 'k': 'int 1..8', 'max_output_tokens': 'int 16..1024', 'index_id': 'ki_... or null (latest ready)', 'generation_revision_id': 'mr_... or null'}},
                                 'output_fields': ['status', 'answer', 'passages', 'citations', 'sources', 'grounding'], 'limits': {'k': 8, 'max_output_tokens': 1024}, 'action_entitlement': False,
                                 'model': {'work_unit': 'generated token (generative) or answer (extractive)', 'completion': 'an answer with mechanically valid citations, or an explicit insufficient-evidence result; never a factual guarantee', 'privacy': 'question, sources and answer private to the asking principal'}}

INSTALLED['verification_audit'] = {'model_id': 'verification-audit/v1', 'result_schema': 'verification-audit-result/v1', 'verifier': 'metacoin-verification/v1', 'input_type': 'verification',
                                   'dataset_kind': None, 'input_schema': {'type': 'object', 'schema': 'verification-audit-input/v1', 'note': 'created through POST /api/v1/verification (preview first); classes: full_exact, full_reference, analytical, sampled_reference, replica'},
                                   'output_fields': ['outcome', 'checked', 'total', 'coverage', 'statement', 'verification_id'], 'limits': {'max_work_by_class': {'full_exact': 200000, 'full_reference': 2000000, 'sampled_reference': 4096}}, 'action_entitlement': False,
                                   'model': {'work_unit': 'audit', 'completion': 'an honest audit that may legitimately return failed; a signed statement names the class, scope and tolerance', 'privacy': 'private diagnostics; public projection carries commitments and counts only'}}

PRIVACY = {'inputs': 'private (age-encrypted); readable by owner, worker and the designated reviewer',
           'results': 'private by default; public openings only by contract disclosure policy after an accepted signed review',
           'public_verification': 'salted Merkle membership + bindings; no hidden-computation proof'}


def verifier_digest(kind):
    from experiments.work_contracts import contract as terms
    if kind in compute_manifests.KINDS:
        return compute_manifests.implementation_digest()
    if kind in model_engine.KINDS:
        return model_engine.implementation_digest()
    if kind in model_engine.KNOWLEDGE_KINDS:
        from .knowledge import engine as knowledge_engine
        return knowledge_engine.implementation_digest()
    return {'energy_audit': terms.verifier_digest, 'temporal_energy': temporal.bundle_digest}.get(kind, science.bundle_digest)()


class Catalog:
    def __init__(self, settings, contracts, jobs):
        self.settings, self.contracts, self.jobs = settings, contracts, jobs

    # ---- registration ----------------------------------------------------------------
    def populate(self, db, operator_id='system'):
        """Register every installed kind once (idempotent); returns the ids."""
        ids = {}
        for kind, spec in INSTALLED.items():
            name = kind.replace('_', '-')
            row = db.execute("SELECT id FROM services WHERE name=? AND version=1", (name,)).fetchone()
            if row:
                ids[kind] = row['id']; continue
            ids[kind] = self.register(db, operator_id, name=name, kind=kind, version=1, price_per_unit=0 if kind == 'verification_audit' else 1, description=spec['model_id'] + ' via ' + spec['verifier'])
        return ids

    def register(self, db, operator_id, *, name, kind, version, price_per_unit, description='', visibility='workspace', workspace='*'):
        if kind not in INSTALLED:
            raise ServiceError('VALIDATION', {'code': 'kind_not_installed', 'installed': sorted(INSTALLED)})
        if type(name) is not str or not 1 <= len(name) <= 64 or not name.replace('-', '').replace('_', '').isalnum() or type(version) is not int or version < 1:
            raise ServiceError('VALIDATION', 'name/version')
        if type(price_per_unit) is not int or not 0 <= price_per_unit <= 10 ** 9 or type(description) is not str or len(description) > 512:
            raise ServiceError('VALIDATION', 'price/description')
        if db.execute('SELECT 1 FROM services WHERE name=? AND version=?', (name, version)).fetchone():
            raise ServiceError('CONFLICT', 'service version exists; register a new version instead')
        spec = INSTALLED[kind]
        sid = 's_' + secrets.token_hex(6)
        pricing = {'policy_revision': PRICING_REVISION, 'unit': UNIT, 'amount_per_unit': price_per_unit, 'rounding': 'integer base units; quantity * amount_per_unit; no fractions',
                   'failed_or_partial_work': 'not billable: a usage record is created only for a completed evaluation', 'asset_by_mode': {'simulation': 'Test-META', 'test-http': 'usdc-test-identifier', 'production': 'configured'}}
        db.execute('INSERT INTO services (id, workspace, name, kind, version, revision, status, description, input_schema_json, output_schema_json, verifier_id, '
                   'verifier_digest, limits_json, privacy_json, pricing_json, capabilities_json, visibility, created_at) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid, workspace, name, kind, version, 'registered', description, json.dumps(spec['input_schema']), json.dumps({'fields': spec['output_fields'], 'result_schema': spec['result_schema']}),
                    spec['verifier'], verifier_digest(kind), json.dumps(spec['limits']), json.dumps(PRIVACY), json.dumps(pricing),
                    json.dumps({'execution': 'child process with rlimits', 'deterministic': True, 'reuse_eligible': kind != 'energy_audit', 'action_entitlement': spec['action_entitlement']}),
                    visibility, now()))
        history.record(db, workspace if workspace != '*' else 'ws_default', operator_id, 'contract.created', 'service', sid, {'name': name, 'version': version, 'kind': kind})
        return sid

    def retire(self, db, principal, sid):
        principal.require('admin:keys')
        row = self._row(db, sid, principal.workspace)
        db.execute("UPDATE services SET status='retired', retired_at=? WHERE id=?", (now(), sid))
        history.record(db, principal.workspace, principal.id, 'key.revoked', 'service', sid, {'retired': True})
        return {'retired': sid, 'policy': 'new quotes/invocations refused; existing contracts and historical verification unchanged'}

    def _row(self, db, sid, workspace):
        row = db.execute("SELECT * FROM services WHERE id=? AND (workspace='*' OR workspace=?)", (sid, workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'service')
        return row

    # ---- discovery ----------------------------------------------------------------------
    def status_of(self, row):
        kind = row['kind']
        installed = kind in INSTALLED
        mode = self.settings.provider_mode
        return {'registered': True, 'installed': installed, 'available': installed and row['status'] == 'registered',
                'configured_for_payment_mode': mode, 'payment_transport': 'x402 over HTTP (test double)' if mode == 'test-http' else ('zero-value simulation' if mode == 'simulation' else 'production facilitator (unexercised)'),
                'externally_validated': False, 'verifier_matches_installed': row['verifier_digest'] == verifier_digest(kind) if installed else False}

    def view(self, row, full=True):
        pricing = json.loads(row['pricing_json'])
        out = {'id': row['id'], 'name': row['name'], 'version': row['version'], 'revision': row['revision'], 'kind': row['kind'], 'status': row['status'],
               'description': row['description'], 'model_id': INSTALLED[row['kind']]['model_id'] if row['kind'] in INSTALLED else None,
               'input_type': INSTALLED[row['kind']]['input_type'] if row['kind'] in INSTALLED else None,
               'verifier_id': row['verifier_id'], 'verifier_digest': row['verifier_digest'], 'price': {'unit': pricing['unit'], 'amount_per_unit': pricing['amount_per_unit'],
               'asset': pricing['asset_by_mode'].get(self.settings.provider_mode), 'policy_revision': pricing['policy_revision']},
               'privacy': json.loads(row['privacy_json']), 'limits': json.loads(row['limits_json']), 'visibility': row['visibility'], 'state': self.status_of(row)}
        if full:
            out.update(input_schema=json.loads(row['input_schema_json']), output_schema=json.loads(row['output_schema_json']), capabilities=json.loads(row['capabilities_json']),
                       pricing=pricing, operations=['validate', 'quote', 'invoke'], created_at=row['created_at'], retired_at=row['retired_at'])
        return out

    def list(self, db, principal, *, model=None, input_type=None, price_unit=None, privacy=None, include_retired=False):
        rows = db.execute("SELECT * FROM services WHERE (workspace='*' OR workspace=?) ORDER BY name, version", (principal.workspace,)).fetchall()
        out = []
        for r in rows:
            v = self.view(r, full=False)
            if not include_retired and r['status'] == 'retired':
                continue
            if model and v['model_id'] != model:
                continue
            if input_type and v['input_type'] != input_type:
                continue
            if price_unit and v['price']['unit'] != price_unit:
                continue
            if privacy and privacy not in json.dumps(v['privacy']):
                continue
            out.append(v)
        return out

    def x402_discovery(self, row, base_url):
        """Bazaar-shaped discovery extension for the invoke route, built with the installed SDK's declaration helper."""
        try:
            from x402.extensions.bazaar.resource_service import declare_discovery_extension, OutputConfig
            from x402.extensions.bazaar.types import parse_discovery_extension
        except ImportError:
            return {'available': False, 'reason': 'x402 SDK bazaar extension not importable in this interpreter'}
        input_schema = {'type': 'object', 'required': ['quote_id', 'inputs'],
                        'properties': {'quote_id': {'type': 'string', 'description': 'an accepted quote for this service revision'},
                                       'inputs': json.loads(row['input_schema_json'])}}
        declared = declare_discovery_extension(input={'quote_id': 'q_<accepted quote id>', 'inputs': {'...': 'validated request matching the quote digest'}},
                                               input_schema=input_schema, body_type='json',
                                               output=OutputConfig(example={'job_id': 'j_...', 'state': 'queued', 'quote_id': 'q_...'}))
        parsed = parse_discovery_extension(declared.get('bazaar', declared)) if isinstance(declared, dict) else None
        return {'available': True, 'resource': base_url + '/api/v1/x402/services/' + row['id'] + '/invoke', 'method': 'POST', 'extension': declared,
                'parsed_ok': parsed is not None, 'sdk': 'x402 2.24.0 bazaar declare_discovery_extension + parse_discovery_extension',
                'price': self.view(row, full=False)['price'], 'bounds': json.loads(row['limits_json']), 'privacy': json.loads(row['privacy_json']),
                'result_type': json.loads(row['output_schema_json'])['result_schema'],
                'note': 'local declaration only; nothing was published to any registry'}

    # ---- validation / quotes ------------------------------------------------------------------
    def validate_request(self, db, principal, sid, inputs):
        row = self._row(db, sid, principal.workspace)
        if row['status'] != 'registered':
            raise ServiceError('CONFLICT', 'service retired')
        from .contracts import validate_inputs
        validate_inputs(row['kind'], inputs)
        digest = hashlib.sha256(merkle.canonical(inputs)).hexdigest()
        return row, digest

    def quote(self, db, principal, sid, inputs, quantity_max=1, provider_mode=None, scheme='exact'):
        principal.require('contract:create')
        if scheme not in ('exact', 'upto'):
            raise ServiceError('VALIDATION', {'code': 'scheme', 'allowed': ['exact', 'upto'], 'meaning': 'exact = fixed-price bundle at the ceiling; upto = variable price, authorized up to the ceiling and settled for the measured amount'})
        row, digest = self.validate_request(db, principal, sid, inputs)
        from .agents import guard
        guard(db, principal, 'quote', service_id=sid, service_kind=row['kind'])
        if row['kind'] in compute_manifests.KINDS or row['kind'] in model_engine.KINDS or row['kind'] in model_engine.KNOWLEDGE_KINDS:
            from .knowledge import engine as knowledge_engine
            needed = (compute_inputs.work_units(row['kind'], inputs) if row['kind'] in compute_manifests.KINDS else model_svc.work_units(row['kind'], inputs) if row['kind'] in model_engine.KINDS
                      else knowledge_engine.work_units(row['kind'], inputs))   # deterministic work bound from the validated inputs
            if quantity_max is None:
                quantity_max = needed
            if type(quantity_max) is not int or quantity_max < needed:
                raise ServiceError('VALIDATION', {'code': 'quantity_below_work_estimate', 'work_units': needed, 'unit': compute_manifests.MANIFESTS[row['kind']]['work_unit'] if row['kind'] in compute_manifests.KINDS else model_svc.WORK_UNIT.get(row['kind'], INSTALLED[row['kind']]['model']['work_unit'])})
            if quantity_max > 10 ** 9:
                raise ServiceError('VALIDATION', 'quantity_max')
        else:
            quantity_max = 1 if quantity_max is None else quantity_max
            if type(quantity_max) is not int or not 1 <= quantity_max <= 1000:
                raise ServiceError('VALIDATION', 'quantity_max 1..1000')
        mode = provider_mode or self.settings.provider_mode
        pricing = json.loads(row['pricing_json'])
        asset = pricing['asset_by_mode'].get(mode)
        if asset is None or asset == 'configured':
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'no asset configured for provider mode ' + str(mode))
        # EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
        network = {'simulation': 'local-simulation', 'test-http': 'eip155-84532'}.get(mode, 'configured')
        pay_to = {'simulation': 'legacy-compute-provider', 'test-http': 'loopback-compute-provider'}.get(mode, 'configured')
        if scheme == 'upto':
            if mode == 'simulation':
                raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'upto_needs_chain', 'note': 'variable-price settlement needs the local chain (test-http) or a production facilitator'})
            network, pay_to = {'test-http': 'local-chain'}.get(mode, 'configured'), {'test-http': 'local-chain-provider'}.get(mode, 'configured')
        amount = quantity_max * pricing['amount_per_unit']
        qid = 'q_' + secrets.token_hex(8)
        binding = {'service_id': sid, 'service_revision': row['revision'], 'request_digest': digest, 'principal_id': principal.id, 'quantity_max': quantity_max,
                   'amount_max': amount, 'unit': pricing['unit'], 'amount_per_unit': pricing['amount_per_unit'], 'asset': asset, 'network': network, 'pay_to': pay_to,
                   'pricing_revision': pricing['policy_revision'], 'provider_mode': mode, 'scheme': scheme, 'expires_at': now() + QUOTE_TTL_SECONDS,
                   'settlement_rule': 'fixed bundle: the ceiling is charged at invocation' if scheme == 'exact' else 'variable: authorized up to the ceiling; settled for min(measured usage * price, ceiling) after the job; a failed job leaves the authorization unused'}
        db.execute('INSERT INTO quotes (id, workspace, service_id, service_revision, principal_id, request_digest, quantity_max, amount_max, unit, asset, network, pay_to, '
                   'pricing_revision, provider_mode, expires_at, state, binding_json, created_at, scheme) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (qid, principal.workspace, sid, row['revision'], principal.id, digest, quantity_max, amount, pricing['unit'], asset, network, pay_to,
                    pricing['policy_revision'], mode, binding['expires_at'], 'offered', json.dumps(binding), now(), scheme))
        history.record(db, principal.workspace, principal.id, 'sale.requested', 'quote', qid, {'service_id': sid, 'amount_max': amount, 'unit': pricing['unit']})
        add_edge(db, principal.workspace, 'service', sid, 'quote', qid, 'quoted')
        return dict(binding, quote_id=qid, state='offered')

    def get_quote(self, db, principal, qid):
        row = db.execute('SELECT * FROM quotes WHERE id=? AND workspace=?', (qid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'quote')
        return row

    def accept(self, db, principal, qid):
        row = self.get_quote(db, principal, qid)
        if row['principal_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'quote belongs to another principal')
        if row['state'] == 'accepted':
            return self.quote_view(row)                                             # idempotent
        if row['state'] != 'offered':
            raise ServiceError('CONFLICT', 'quote is ' + row['state'])
        if row['expires_at'] <= now():
            db.execute("UPDATE quotes SET state='expired' WHERE id=?", (qid,))
            raise ServiceError('EXPIRED', 'quote expired; request a new one')
        from .agents import guard
        kind = db.execute('SELECT kind FROM services WHERE id=?', (row['service_id'],)).fetchone()['kind']
        guard(db, principal, 'invoke', service_id=row['service_id'], service_kind=kind, amount=row['amount_max'], precheck_jobs=1)   # reservation counts against the agent ceiling; refused early if no job could follow
        db.execute("UPDATE quotes SET state='accepted', accepted_at=? WHERE id=? AND state='offered'", (now(), qid))
        history.record(db, principal.workspace, principal.id, 'payment.reserved', 'quote', qid, {'accepted': True, 'amount_max': row['amount_max']})
        return self.quote_view(self.get_quote(db, principal, qid))

    def consume(self, db, principal, qid, inputs, provider_mode=None):
        """Atomically consume an accepted quote for one bounded invocation whose request digest matches."""
        row = self.get_quote(db, principal, qid)
        if row['principal_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'quote belongs to another principal')
        if row['state'] == 'consumed':
            raise ServiceError('CONFLICT', 'quote already consumed')
        if row['state'] != 'accepted':
            raise ServiceError('CONFLICT', 'quote not accepted')
        if row['expires_at'] <= now():
            raise ServiceError('EXPIRED', 'quote expired')
        if provider_mode and provider_mode != row['provider_mode']:
            raise ServiceError('CONFLICT', 'provider mode differs from the quote')
        service = db.execute('SELECT * FROM services WHERE id=?', (row['service_id'],)).fetchone()
        if service['status'] != 'registered' or service['revision'] != row['service_revision']:
            raise ServiceError('CONFLICT', 'service retired or revised since the quote')
        if hashlib.sha256(merkle.canonical(inputs)).hexdigest() != row['request_digest']:
            raise ServiceError('BINDING_MISMATCH', 'request differs from the quoted request')
        changed = db.execute("UPDATE quotes SET state='consumed', consumed_at=? WHERE id=? AND state='accepted'", (now(), qid)).rowcount
        if changed != 1:
            raise ServiceError('CONFLICT', 'quote already consumed')
        return row, service

    @staticmethod
    def quote_view(row):
        return dict(json.loads(row['binding_json']), quote_id=row['id'], state=row['state'], accepted_at=row['accepted_at'], consumed_at=row['consumed_at'])


def compatibility(db, principal, catalog, sid, *, dataset_version_id=None, run_id=None, node_id=None):
    """Explain precisely why a dataset version or a workflow node output can or cannot feed a service. Read-only."""
    principal.require('contract:read')
    row = catalog._row(db, sid, principal.workspace)
    spec = INSTALLED.get(row['kind'])
    checks = []
    def add(aspect, ok, expected, actual, explanation):
        checks.append({'aspect': aspect, 'ok': bool(ok), 'expected': expected, 'actual': actual, 'explanation': explanation})
    add('service_status', row['status'] == 'registered', 'registered', row['status'], 'retired services accept no new work')
    add('verifier_installed', spec is not None, 'installed kind', row['kind'], 'the verifier for this kind must be installed on this service')
    if spec is not None:
        installed = verifier_digest(row['kind'])
        add('verifier_version', installed == row['verifier_digest'], row['verifier_digest'][:16] + '...', installed[:16] + '...',
            'the registered verifier digest must equal the installed one; otherwise results would not bind to the listed verifier')
    if dataset_version_id is not None:
        v = db.execute('SELECT v.*, d.kind AS dataset_kind, d.retired_at, d.workspace FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id WHERE v.id=?', (dataset_version_id,)).fetchone()
        if v is None or v['workspace'] != principal.workspace:
            raise ServiceError('NOT_FOUND', 'dataset version')
        expected_kind = spec['dataset_kind'] if spec else None
        add('input_type', expected_kind is not None and v['dataset_kind'] == expected_kind, expected_kind or 'inline inputs only', v['dataset_kind'],
            'the service materializes model inputs only from this dataset kind' if expected_kind else 'this service takes inline JSON inputs, not datasets')
        units = json.loads(v['units_json'])
        want = {'duration': 's', 'power': 'mW'}
        add('units', units == want, want, units, 'dataset column units must be the model units exactly; no conversion is performed')
        add('payload_available', v['deleted_at'] is None, 'payload present', 'deleted' if v['deleted_at'] else 'present', 'a deleted payload cannot be materialized; the commitment still verifies old results')
        add('dataset_active', v['retired_at'] is None, 'active', 'retired' if v['retired_at'] else 'active', 'retired datasets refuse new work')
        limit = (spec or {}).get('limits', {}).get('max_segments')
        add('row_limit', limit is None or v['row_count'] <= limit, '<= %s rows' % limit, v['row_count'], 'rows become model segments; the verifier bounds them')
        add('privacy', v['privacy'] == 'private', 'private dataset -> private inputs', v['privacy'], 'inputs materialized from the dataset are stored encrypted exactly like inline inputs')
        add('schema_version', v['normalization_id'].startswith('metacoin-dataset-normalization/') or True, 'known normalization', v['normalization_id'], 'the normalization id records how the raw file became rows')
    if run_id is not None:
        from .workflows import FROM_FIELDS
        run = db.execute('SELECT r.*, d.definition_json FROM workflow_runs r JOIN workflow_definitions d ON d.id=r.definition_id WHERE r.id=? AND r.workspace=?', (run_id, principal.workspace)).fetchone()
        if run is None:
            raise ServiceError('NOT_FOUND', 'run')
        nodes = {n['id']: n for n in json.loads(run['definition_json'])['nodes']}
        if node_id not in nodes:
            raise ServiceError('NOT_FOUND', 'node')
        up = nodes[node_id]
        up_spec = INSTALLED.get(up['type'])
        add('upstream_is_service_node', up_spec is not None, 'service node', up['type'], 'only service outputs carry integer summary fields that can bind downstream')
        if up_spec:
            feedable = [f for f in up_spec['output_fields'] if f in FROM_FIELDS]
            required = (spec or {}).get('input_schema', {}).get('required', [])
            add('bindable_fields', bool(feedable), 'at least one integer summary field in ' + ','.join(sorted(FROM_FIELDS)), feedable,
                'a downstream node may set a parameter from one of these upstream fields via parameters: {name: {from: {node, field}}}')
            add('target_parameters', bool(required), 'integer parameters of the target service', [p for p in required if p not in ('units', 'assumptions', 'provenance', 'private_label', 'schema', 'segments', 'fixed_segments', 'candidates', 'optional_tasks', 'objective')],
                'parameters the upstream field may feed; non-integer inputs (segments, candidates) cannot come from a summary field')
            state = db.execute('SELECT state, output_root FROM workflow_nodes WHERE run_id=? AND node_id=?', (run_id, node_id)).fetchone()
            add('upstream_state', state is not None and state['state'] == 'succeeded', 'succeeded', state['state'] if state else None, 'only a committed upstream result can be bound')
    return {'service_id': sid, 'kind': row['kind'], 'compatible': all(c['ok'] for c in checks), 'checks': checks, 'note': 'preview only; nothing created'}

