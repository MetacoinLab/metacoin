"""Private work-request board, provider capabilities, binding offers, transparent comparison, atomic awards and
bounded provider execution (Order 08 Group B, §20–§26).

A request is a frozen WorkTerms opened to its declared audience inside this application (never an external listing).
Providers are principals with the `provider` role registered by the operator; their capability records are signed
revisions (application signature by the service key on behalf of the authenticated provider — service custody,
labelled as such). Eligibility is checked before an offer is stored; excluded offers keep structured reasons. Ranking
follows the request's declared selection policy and tie-breaks; manual selection needs a recorded reason. An award
is one guarded transaction: request state, offer validity, requester authority, approval policy, budget reservation,
milestone instantiation and dispatch of the first ready milestones. A lost response returns the same award."""
import hashlib
import json
import re
import secrets

from experiments.private_receipts import receipt as merkle
from .. import auth, budgets, crypto, history, metering
from ..approvals import gate as approval_gate
from ..db import now
from ..errors import ServiceError
from . import terms as terms_mod

PROVIDER_STATEMENT = 'metacoin-provider-capability/v1'
OFFER_STATEMENT = 'metacoin-work-offer/v1'
SIGNATURE_LABEL = 'application signature: Ed25519 by the service signing key on behalf of the authenticated provider principal (service custody); not an interoperable x402 signed offer'
RELATIONSHIPS = ('same_operator', 'affiliated', 'independent_declared', 'unknown')
EXECUTION_TYPES = ('local_worker', 'node')


def _sign(settings, db, statement):
    pub = metering.ensure_service_key(settings, db)
    msg = merkle.canonical(statement)
    return msg.decode(), crypto.sign(crypto.load_signing_key(settings.keys_dir / 'service.ed25519'), msg), crypto.key_id_for(pub)


def _terms(db, terms_id):
    r = db.execute('SELECT * FROM work_terms WHERE id=?', (terms_id,)).fetchone()
    return r, (json.loads(r['terms_json']) if r else None)


class Providers:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def _validate_capabilities(self, db, principal, caps):
        from .. import contracts as contracts_mod
        if type(caps) is not dict or set(caps) - {'kinds', 'packages', 'verification_classes', 'payment_schemes', 'privacy_modes', 'resource_limits', 'environment', 'input_schemas', 'output_schemas'}:
            raise ServiceError('VALIDATION', {'code': 'capabilities', 'allowed': ['kinds', 'packages', 'verification_classes', 'payment_schemes', 'privacy_modes', 'resource_limits', 'environment', 'input_schemas', 'output_schemas']})
        kinds = caps.get('kinds')
        if type(kinds) is not list or not kinds or not set(kinds) <= set(contracts_mod.KINDS):
            raise ServiceError('VALIDATION', {'code': 'capabilities_kinds', 'allowed': list(contracts_mod.KINDS)})
        terms_mod._enum_list(caps.get('verification_classes', ['none']), terms_mod.VERIFICATION_CLASSES, 'capabilities_verification_classes')
        terms_mod._enum_list(caps.get('payment_schemes', ['exact']), terms_mod.SCHEMES, 'capabilities_payment_schemes')
        terms_mod._enum_list(caps.get('privacy_modes', ['requester_private']), ('requester_private', 'shared_with_provider_after_award', 'public_synthetic'), 'capabilities_privacy_modes')
        for k in ('packages',):
            if k in caps and (type(caps[k]) is not list or len(caps[k]) > 32 or not all(type(x) is str for x in caps[k])):
                raise ServiceError('VALIDATION', 'capabilities_' + k)
        for k in ('resource_limits', 'environment', 'input_schemas', 'output_schemas'):
            if k in caps and type(caps[k]) is not dict:
                raise ServiceError('VALIDATION', 'capabilities_' + k)
        return caps

    def _validate_execution(self, db, principal, ex):
        if type(ex) is not dict or ex.get('type') not in EXECUTION_TYPES:
            raise ServiceError('VALIDATION', {'code': 'execution', 'allowed': list(EXECUTION_TYPES)})
        if ex['type'] == 'node':
            node = db.execute('SELECT id, state, capabilities_json FROM nodes WHERE id=?', (ex.get('node_id'),)).fetchone()
            if node is None or node['state'] == 'revoked':
                raise ServiceError('VALIDATION', {'code': 'execution_node', 'note': 'an enrolled node id is required (operator-controlled registration)'})
            return {'type': 'node', 'node_id': node['id'], 'node_kinds': json.loads(node['capabilities_json']), 'transport': 'signed HTTPS node protocol, separate process and state directory'}
        return {'type': 'local_worker', 'transport': 'in-process worker of this instance (same host, same operator fixture)'}

    def _validate_relationship(self, rel):
        if type(rel) is not dict or set(rel) - {'operator_affiliation', 'same_host', 'shared_custody', 'relationship', 'note'}:
            raise ServiceError('VALIDATION', {'code': 'relationship', 'allowed': ['operator_affiliation', 'same_host', 'shared_custody', 'relationship', 'note']})
        if rel.get('relationship', 'unknown') not in RELATIONSHIPS:
            raise ServiceError('VALIDATION', {'code': 'relationship', 'allowed': list(RELATIONSHIPS)})
        out = {'operator_affiliation': str(rel.get('operator_affiliation', 'unknown'))[:64], 'same_host': rel.get('same_host'), 'shared_custody': rel.get('shared_custody'), 'relationship': rel.get('relationship', 'unknown'), 'note': str(rel.get('note', ''))[:256]}
        if out['same_host'] not in (True, False, None) or out['shared_custody'] not in (True, False, None):
            raise ServiceError('VALIDATION', 'relationship booleans')
        return out

    def register(self, db, principal, body):
        """Operator registers a provider: a new principal with the provider role, one credential (shown once) and the
        signed capability record. Independence is never derived from the key; the relationship attributes are declared."""
        principal.require('work:provider_admin'); principal.require('admin:credentials')
        if type(body) is not dict or set(body) - {'name', 'execution', 'capabilities', 'relationship', 'pay_to', 'credential_seconds'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['name', 'execution', 'capabilities', 'relationship', 'pay_to', 'credential_seconds']})
        name = body.get('name')
        if type(name) is not str or not 1 <= len(name) <= 64:
            raise ServiceError('VALIDATION', 'name')
        ex = self._validate_execution(db, principal, body.get('execution') or {'type': 'local_worker'})
        caps = self._validate_capabilities(db, principal, body.get('capabilities') or {})
        rel = self._validate_relationship(body.get('relationship') or {'relationship': 'same_operator', 'same_host': True, 'shared_custody': True, 'operator_affiliation': 'same operator (local fixture)'})
        pay_to = body.get('pay_to') or ('provider:' + name)
        if type(pay_to) is not str or not 1 <= len(pay_to) <= 128:
            raise ServiceError('VALIDATION', 'pay_to')
        pid = auth.create_principal(db, name, 'provider', principal.workspace)
        cid, token = auth.issue_credential(db, pid, body.get('credential_seconds', 30 * 86400))
        prid = 'pv_' + secrets.token_hex(6)
        ev = {'declared': sorted(caps['kinds']), 'exercised': [], 'witnessed': [], 'meaning': 'declared = the provider claims it; exercised = completed on this instance; witnessed = verified by a distinct verifier under a contract'}
        st = {'schema': PROVIDER_STATEMENT, 'provider_id': prid, 'principal_id': pid, 'revision': 1, 'execution': ex, 'capabilities': caps, 'relationship': rel, 'pay_to': pay_to, 'workspace': principal.workspace, 'registered_by': principal.id, 'issued_at': now(), 'signature': SIGNATURE_LABEL}
        msg, sig, kid = _sign(self.settings, db, st)
        db.execute('INSERT INTO providers VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)', (prid, principal.workspace, pid, name, json.dumps(ex), json.dumps(caps), json.dumps(rel), pay_to, json.dumps(ev), msg, sig, kid, 'active', principal.id, now(), now()))
        db.execute('INSERT INTO provider_revisions VALUES (?,?,1,?,?,?,?,?,?,?,?)', ('pr_' + secrets.token_hex(6), prid, json.dumps(ex), json.dumps(caps), json.dumps(rel), pay_to, msg, sig, kid, now()))
        history.record(db, principal.workspace, principal.id, 'work.provider_registered', 'provider', prid, {'principal_id': pid, 'execution': ex['type'], 'kinds': sorted(caps['kinds']), 'relationship': rel['relationship']})
        out = self.view(db, principal, self.row(db, principal, prid))
        out['credential'] = {'credential_id': cid, 'token': token, 'note': 'shown once; store privately (0600); never in URLs, logs or reports'}
        return out

    def revise(self, db, principal, prid, body):
        """A changed executable, model, recipient or relationship is a new signed revision; old revisions stay readable."""
        r = self.row(db, principal, prid)
        if not (principal.can('work:provider_admin') or principal.id == r['principal_id']):
            raise ServiceError('FORBIDDEN', 'provider revision')
        if type(body) is not dict or set(body) - {'execution', 'capabilities', 'relationship', 'pay_to', 'expected_revision'}:
            raise ServiceError('VALIDATION', 'fields: execution, capabilities, relationship, pay_to, expected_revision')
        if body.get('expected_revision') is not None and body['expected_revision'] != r['revision']:
            raise ServiceError('STATE_CONFLICT', {'code': 'revision_changed', 'current': r['revision']})
        ex = self._validate_execution(db, principal, body['execution']) if 'execution' in body else json.loads(r['execution_json'])
        caps = self._validate_capabilities(db, principal, body['capabilities']) if 'capabilities' in body else json.loads(r['capabilities_json'])
        rel = self._validate_relationship(body['relationship']) if 'relationship' in body else json.loads(r['relationship_json'])
        if 'relationship' in body and principal.id == r['principal_id'] and not principal.can('work:provider_admin'):
            raise ServiceError('FORBIDDEN', 'a provider cannot declare its own operator relationship; the operator records it')
        pay_to = body.get('pay_to', r['pay_to'])
        if type(pay_to) is not str or not 1 <= len(pay_to) <= 128:
            raise ServiceError('VALIDATION', 'pay_to')
        rev = r['revision'] + 1
        st = {'schema': PROVIDER_STATEMENT, 'provider_id': prid, 'principal_id': r['principal_id'], 'revision': rev, 'execution': ex, 'capabilities': caps, 'relationship': rel, 'pay_to': pay_to, 'workspace': r['workspace'], 'revised_by': principal.id, 'issued_at': now(), 'signature': SIGNATURE_LABEL}
        msg, sig, kid = _sign(self.settings, db, st)
        db.execute('UPDATE providers SET revision=?, execution_json=?, capabilities_json=?, relationship_json=?, pay_to=?, statement_json=?, signature_hex=?, key_id=?, updated_at=? WHERE id=?', (rev, json.dumps(ex), json.dumps(caps), json.dumps(rel), pay_to, msg, sig, kid, now(), prid))
        db.execute('INSERT INTO provider_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)', ('pr_' + secrets.token_hex(6), prid, rev, json.dumps(ex), json.dumps(caps), json.dumps(rel), pay_to, msg, sig, kid, now()))
        history.record(db, principal.workspace, principal.id, 'work.provider_revised', 'provider', prid, {'revision': rev, 'pay_to_changed': pay_to != r['pay_to'], 'execution': ex['type']})
        return self.view(db, principal, self.row(db, principal, prid))

    def row(self, db, principal, prid):
        r = db.execute('SELECT * FROM providers WHERE id=? AND workspace=?', (prid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'provider')
        return r

    def for_principal(self, db, principal):
        r = db.execute('SELECT * FROM providers WHERE principal_id=?', (principal.id,)).fetchone()
        if r is None:
            raise ServiceError('FORBIDDEN', 'the caller is not a registered provider')
        return r

    def view(self, db, principal, r):
        principal.require('work:read')
        out = {'id': r['id'], 'name': r['name'], 'principal_id': r['principal_id'], 'revision': r['revision'], 'execution': json.loads(r['execution_json']), 'capabilities': json.loads(r['capabilities_json']),
               'relationship': json.loads(r['relationship_json']), 'pay_to': r['pay_to'], 'evidence': json.loads(r['evidence_json']), 'state': r['state'], 'signature': {'key_id': r['key_id'], 'custody': 'service-custodied', 'label': SIGNATURE_LABEL},
               'created_at': r['created_at'], 'updated_at': r['updated_at'],
               'revisions': [{'revision': x['revision'], 'pay_to': x['pay_to'], 'execution': json.loads(x['execution_json'])['type'], 'created_at': x['created_at']} for x in db.execute('SELECT * FROM provider_revisions WHERE provider_id=? ORDER BY revision', (r['id'],)).fetchall()]}
        return out

    def list(self, db, principal):
        principal.require('work:read')
        return [self.view(db, principal, r) for r in db.execute("SELECT * FROM providers WHERE workspace=? ORDER BY created_at", (principal.workspace,)).fetchall()]

    def eligibility(self, db, provider_row, terms, offer=None):
        """Structured reasons why a provider (and optionally its offer) is or is not eligible for the terms."""
        caps, ex, rel = json.loads(provider_row['capabilities_json']), json.loads(provider_row['execution_json']), json.loads(provider_row['relationship_json'])
        el = terms['eligibility']; reasons = []
        kind = terms['operation']['kind']
        if kind not in caps['kinds']:
            reasons.append({'code': 'unsupported_kind', 'kind': kind, 'provider_kinds': sorted(caps['kinds'])})
        if ex['type'] == 'node' and kind not in ex.get('node_kinds', []):
            reasons.append({'code': 'node_cannot_execute_kind', 'kind': kind, 'node_kinds': ex.get('node_kinds')})
        req = terms['acceptance']['required_verification']['class']
        if req != 'none' and req not in caps.get('verification_classes', []):
            reasons.append({'code': 'unsupported_exact_validator' if req in ('full_exact', 'full_reference') else 'unsupported_verification_class', 'required': req, 'provider_classes': caps.get('verification_classes', [])})
        if not set(el['payment_schemes']) & set(caps.get('payment_schemes', [])):
            reasons.append({'code': 'wrong_payment_scheme', 'required_any_of': el['payment_schemes'], 'provider_schemes': caps.get('payment_schemes', [])})
        if terms['privacy']['inputs'] not in caps.get('privacy_modes', ['requester_private']):
            reasons.append({'code': 'unacceptable_evidence_custody', 'required': terms['privacy']['inputs'], 'provider_modes': caps.get('privacy_modes')})
        if 'operator_relationships' in el and rel.get('relationship', 'unknown') not in el['operator_relationships']:
            reasons.append({'code': 'operator_relationship_not_allowed', 'relationship': rel.get('relationship'), 'allowed': el['operator_relationships']})
        if 'execution_types' in el and ex['type'] not in el['execution_types']:
            reasons.append({'code': 'execution_type_not_allowed', 'type': ex['type'], 'allowed': el['execution_types']})
        if 'providers' in el and provider_row['id'] not in el['providers']:
            reasons.append({'code': 'provider_not_in_allowed_set'})
        lim = caps.get('resource_limits', {})
        if lim.get('max_ceiling') is not None and terms['payment']['ceiling'] > lim['max_ceiling']:
            reasons.append({'code': 'insufficient_resource_bound', 'ceiling': terms['payment']['ceiling'], 'provider_max': lim['max_ceiling']})
        if provider_row['state'] != 'active':
            reasons.append({'code': 'provider_retired'})
        if terms['payment']['asset'] == 'local-chain-token' and not re.fullmatch(r'0x[0-9a-fA-F]{40}', provider_row['pay_to'] or ''):
            reasons.append({'code': 'recipient_invalid_for_rail', 'asset': terms['payment']['asset'], 'note': 'the registered payout address is not an address of the chain rail; a chain payment could never be signed to it'})
        if offer is not None:
            if offer['scheme'] not in el['payment_schemes'] or offer['scheme'] not in caps.get('payment_schemes', []):
                reasons.append({'code': 'wrong_payment_scheme', 'offered': offer['scheme'], 'required_any_of': el['payment_schemes']})
            if offer['asset'] != terms['payment']['asset']:
                reasons.append({'code': 'wrong_asset', 'offered': offer['asset'], 'required': terms['payment']['asset']})
            if offer['price_amount'] > terms['payment']['ceiling']:
                reasons.append({'code': 'price_above_ceiling', 'offered': offer['price_amount'], 'ceiling': terms['payment']['ceiling']})
            v = offer['verification']
            if req != 'none' and terms_mod.VERIFICATION_CLASSES.index(v['class']) > terms_mod.VERIFICATION_CLASSES.index(req) and v['class'] != 'none' and False:
                pass
            if req != 'none' and v['class'] not in el['verification_classes']:
                reasons.append({'code': 'weaker_acceptance_class', 'offered': v['class'], 'allowed': el['verification_classes']})
            if terms['acceptance']['required_verification']['distinct_verifier'] and not v.get('distinct_verifier'):
                reasons.append({'code': 'distinct_verifier_required'})
            if offer['privacy_terms'] not in ('as_requested', 'stricter'):
                reasons.append({'code': 'disallowed_disclosure', 'offered': offer['privacy_terms']})
            if offer['window_seconds'] > terms['deadlines']['delivery_seconds']:
                reasons.append({'code': 'window_exceeds_delivery_deadline', 'offered': offer['window_seconds'], 'deadline': terms['deadlines']['delivery_seconds']})
            if offer['pay_to'] != provider_row['pay_to']:
                reasons.append({'code': 'recipient_differs_from_registered', 'note': 'a changed payment recipient needs a new provider revision, not an offer field'})
        return {'eligible': not reasons, 'reasons': reasons}


class Board:
    def __init__(self, settings, services, providers):
        self.settings, self.svc, self.providers = settings, services, providers

    # ---- requests (§20) --------------------------------------------------------------------------------------------
    def request_row(self, db, principal, rid):
        r = db.execute('SELECT * FROM work_requests WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'work request')
        return r

    def _audience_ok(self, principal, r):
        aud = json.loads(r['audience_json'])
        if principal.role in ('owner', 'reviewer', 'viewer') or principal.id == r['requester_id']:
            return True
        if aud.get('scope') == 'workspace':
            return True
        return principal.id in aud.get('principals', [])

    def create_request(self, db, principal, body):
        principal.require('work:request')
        t, terms = _terms(db, body.get('terms_id'))
        if t is None or t['workspace'] != principal.workspace or t['requester_id'] != principal.id:
            raise ServiceError('NOT_FOUND', 'frozen work terms of the caller')
        if t['state'] != 'frozen':
            raise ServiceError('CONFLICT', {'code': 'terms_not_frozen', 'state': t['state'], 'note': 'a request opens frozen terms; drafts are validated and frozen first'})
        if db.execute("SELECT id FROM work_requests WHERE terms_id=? AND state NOT IN ('closed','expired')", (t['id'],)).fetchone():
            raise ServiceError('CONFLICT', 'an open request already exists for these terms')
        aud = body.get('audience') or {'scope': 'workspace'}
        if type(aud) is not dict or aud.get('scope') not in ('workspace', 'principals') or (aud['scope'] == 'principals' and (type(aud.get('principals')) is not list or not all(type(x) is str for x in aud['principals']))):
            raise ServiceError('VALIDATION', {'code': 'audience', 'allowed': "{scope: 'workspace'} or {scope: 'principals', principals: [...]}"})
        rid = 'wr_' + secrets.token_hex(8)
        db.execute('INSERT INTO work_requests (id, workspace, requester_id, terms_id, terms_digest, state, audience_json, max_awards, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (rid, principal.workspace, principal.id, t['id'], t['digest'], 'draft', json.dumps(aud), terms['max_awards'], now(), now()))
        history.record(db, principal.workspace, principal.id, 'work.request_created', 'work_request', rid, {'terms_id': t['id'], 'terms_digest': t['digest'], 'kind': terms['operation']['kind'], 'max_awards': terms['max_awards']})
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'work_terms', t['id'], 'work_request', rid, 'opened_as')
        return self.request_view(db, principal, self.request_row(db, principal, rid))

    def request_view(self, db, principal, r, include_offers=True):
        principal.require('work:read')
        if not self._audience_ok(principal, r):
            raise ServiceError('NOT_FOUND', 'work request')
        t, terms = _terms(db, r['terms_id'])
        is_req = principal.id == r['requester_id'] or principal.can('work:award')
        out = {'id': r['id'], 'state': r['state'], 'requester_id': r['requester_id'], 'terms_id': r['terms_id'], 'terms_digest': r['terms_digest'], 'audience': json.loads(r['audience_json']) if is_req else {'scope': json.loads(r['audience_json'])['scope']},
               'max_awards': r['max_awards'], 'opened_at': r['opened_at'], 'closes_at': r['closes_at'], 'closed_at': r['closed_at'], 'close_reason': r['close_reason'], 'created_at': r['created_at'], 'updated_at': r['updated_at'],
               'title': terms['title'], 'kind': terms['operation']['kind'], 'purpose': terms.get('purpose'),
               # two-stage access (§20): metadata before award; the frozen operation identities are visible, inputs never
               'preview': {'deliverables': [{'key': d['key'], 'type': d['type'], 'required': d['required'], 'schema': d.get('schema')} for d in terms['deliverables']], 'acceptance_class': terms['acceptance']['required_verification'],
                           'outcomes': terms['acceptance']['outcomes'], 'payment': {'ceiling': terms['payment']['ceiling'], 'asset': terms['payment']['asset'], 'scale': terms['payment']['scale'], 'scheme': terms['payment']['scheme'], 'rule': terms['acceptance']['payment_rule']},
                           'deadlines': terms['deadlines'], 'privacy': terms['privacy'], 'eligibility': terms['eligibility'], 'selection': terms['selection'], 'milestones': [{'key': m['key'], 'max_payment': m['max_payment'], 'depends_on': m['depends_on']} for m in terms['milestones']],
                           'operation': {k: terms['operation'].get(k) for k in ('kind', 'model_id', 'verifier_id', 'verifier_digest', 'contract_digest', 'input_root')}, 'input_access': 'after award only (privacy.inputs=%s)' % terms['privacy']['inputs']}}
        if include_offers:
            offers = db.execute('SELECT * FROM work_offers WHERE request_id=? ORDER BY created_at, id', (r['id'],)).fetchall()
            mine = [self.offer_view(db, principal, o) for o in offers if is_req or self._offer_owner(db, principal, o)]
            out['offers'] = mine
            out['offers_count'] = {'total': len(offers), 'offered': sum(o['state'] == 'offered' for o in offers), 'excluded': sum(o['state'] == 'excluded' for o in offers)}
            awards = db.execute('SELECT id, state, provider_id, offer_id FROM work_awards WHERE request_id=? ORDER BY awarded_at', (r['id'],)).fetchall()
            out['awards'] = [dict(a) for a in awards]
        return out

    def _offer_owner(self, db, principal, o):
        p = db.execute('SELECT principal_id FROM providers WHERE id=?', (o['provider_id'],)).fetchone()
        return p is not None and p['principal_id'] == principal.id

    def request_state(self, db, principal, rid, action, body=None):
        body = body or {}
        r = self.request_row(db, principal, rid)
        if r['requester_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the requester controls its request')
        t, terms = _terms(db, r['terms_id'])
        allowed = {'open': ('draft', 'paused'), 'pause': ('open',), 'close': ('draft', 'open', 'paused', 'awarded'), 'validate': ('draft', 'open', 'paused', 'awarded')}
        if action not in allowed:
            raise ServiceError('VALIDATION', {'code': 'action', 'allowed': list(allowed)})
        if r['state'] not in allowed[action]:
            raise ServiceError('CONFLICT', {'code': 'request_state', 'state': r['state'], 'action': action})
        if action == 'validate':
            probs = []
            if t['state'] != 'frozen':
                probs.append({'code': 'terms_superseded', 'state': t['state']})
            if t['expires_at'] and t['expires_at'] < now():
                probs.append({'code': 'terms_expired'})
            budget = budgets.check(db, budgets.root(db, principal.workspace)['id'], terms['payment']['ceiling'])
            if budget is not None:
                probs.append(dict(budget, code='budget_cannot_reserve_ceiling'))
            return {'id': rid, 'valid': not probs, 'problems': probs, 'note': 'nothing reserved; an award reserves under the workspace budget tree'}
        if action == 'open':
            if t['state'] != 'frozen':
                raise ServiceError('CONFLICT', {'code': 'terms_superseded', 'state': t['state']})
            closes = now() + terms['deadlines']['offer_seconds']
            db.execute("UPDATE work_requests SET state='open', opened_at=COALESCE(opened_at, ?), closes_at=?, updated_at=? WHERE id=?", (now(), closes, now(), rid))
            history.record(db, principal.workspace, principal.id, 'work.request_opened', 'work_request', rid, {'closes_at': closes, 'audience': json.loads(r['audience_json'])['scope'], 'external_publication': False})
        elif action == 'pause':
            db.execute("UPDATE work_requests SET state='paused', updated_at=? WHERE id=?", (now(), rid))
            history.record(db, principal.workspace, principal.id, 'work.request_state', 'work_request', rid, {'state': 'paused'})
        elif action == 'close':
            reason = str(body.get('reason', 'closed by requester'))[:256]
            db.execute("UPDATE work_requests SET state='closed', closed_at=?, close_reason=?, updated_at=? WHERE id=?", (now(), reason, now(), rid))
            db.execute("UPDATE work_offers SET state='expired', updated_at=? WHERE request_id=? AND state='offered'", (now(), rid))
            history.record(db, principal.workspace, principal.id, 'work.request_state', 'work_request', rid, {'state': 'closed', 'reason': reason})
        return self.request_view(db, principal, self.request_row(db, principal, rid))

    def expire_requests(self, db):
        n = 0
        for r in db.execute("SELECT id, workspace FROM work_requests WHERE state='open' AND closes_at IS NOT NULL AND closes_at < ?", (now(),)).fetchall():
            db.execute("UPDATE work_requests SET state='expired', closed_at=?, close_reason='offer window elapsed', updated_at=? WHERE id=? AND state='open'", (now(), now(), r['id']))
            db.execute("UPDATE work_offers SET state='expired', updated_at=? WHERE request_id=? AND state='offered'", (now(), r['id']))
            history.record(db, r['workspace'], 'scheduler', 'work.request_state', 'work_request', r['id'], {'state': 'expired'})
            n += 1
        db.execute("UPDATE work_offers SET state='expired', updated_at=? WHERE state='offered' AND expires_at < ?", (now(), now()))
        return n

    def list_requests(self, db, principal, state=None):
        principal.require('work:read')
        sql, args = 'SELECT * FROM work_requests WHERE workspace=?', [principal.workspace]
        if state:
            sql += ' AND state=?'; args.append(state)
        rows = db.execute(sql + ' ORDER BY created_at DESC LIMIT 200', args).fetchall()
        return [self.request_view(db, principal, r, include_offers=False) for r in rows if self._audience_ok(principal, r)]

    # ---- eligibility and offers (§21–§22) ------------------------------------------------------------------------------
    def check_eligibility(self, db, principal, rid, body=None):
        r = self.request_row(db, principal, rid)
        if not self._audience_ok(principal, r):
            raise ServiceError('NOT_FOUND', 'work request')
        t, terms = _terms(db, r['terms_id'])
        body = body or {}
        prow = self.providers.row(db, principal, body['provider_id']) if body.get('provider_id') and principal.can('work:award') else self.providers.for_principal(db, principal)
        offer = None
        if body.get('offer'):
            offer = self._normalize_offer(body['offer'], prow)
        return dict(self.providers.eligibility(db, prow, terms, offer), request_id=rid, provider_id=prow['id'], terms_digest=r['terms_digest'])

    def _normalize_offer(self, o, prow):
        if type(o) is not dict or set(o) - {'price_amount', 'asset', 'scheme', 'window_seconds', 'verification', 'privacy_terms', 'pay_to', 'expires_in_seconds', 'note'}:
            raise ServiceError('VALIDATION', {'code': 'offer_fields', 'allowed': ['price_amount', 'asset', 'scheme', 'window_seconds', 'verification', 'privacy_terms', 'pay_to', 'expires_in_seconds', 'note']})
        terms_mod._int(o.get('price_amount'), 0, 10 ** 15, 'offer_price_amount')
        if o.get('asset') not in terms_mod.ASSETS:
            raise ServiceError('VALIDATION', {'code': 'offer_asset', 'allowed': list(terms_mod.ASSETS)})
        if o.get('scheme') not in terms_mod.SCHEMES:
            raise ServiceError('VALIDATION', {'code': 'offer_scheme', 'allowed': list(terms_mod.SCHEMES)})
        terms_mod._int(o.get('window_seconds'), 1, 365 * 86400, 'offer_window_seconds')
        v = o.get('verification') or {'class': 'none', 'distinct_verifier': False}
        if type(v) is not dict or v.get('class') not in terms_mod.VERIFICATION_CLASSES or type(v.get('distinct_verifier', False)) is not bool:
            raise ServiceError('VALIDATION', {'code': 'offer_verification', 'allowed': list(terms_mod.VERIFICATION_CLASSES)})
        if o.get('privacy_terms', 'as_requested') not in ('as_requested', 'stricter', 'weaker'):
            raise ServiceError('VALIDATION', {'code': 'offer_privacy_terms', 'allowed': ['as_requested', 'stricter', 'weaker']})
        terms_mod._int(o.get('expires_in_seconds', 86400), 60, 90 * 86400, 'offer_expires_in_seconds')
        pay_to = o.get('pay_to', prow['pay_to'])
        if type(pay_to) is not str or not 1 <= len(pay_to) <= 128:
            raise ServiceError('VALIDATION', 'offer_pay_to')
        if 'note' in o and (type(o['note']) is not str or len(o['note']) > 512):
            raise ServiceError('VALIDATION', 'offer_note')
        return {'price_amount': o['price_amount'], 'asset': o['asset'], 'scale': terms_mod.ASSETS[o['asset']]['scale'], 'scheme': o['scheme'], 'window_seconds': o['window_seconds'], 'verification': {'class': v['class'], 'distinct_verifier': v.get('distinct_verifier', False)},
                'privacy_terms': o.get('privacy_terms', 'as_requested'), 'pay_to': pay_to, 'expires_in_seconds': o.get('expires_in_seconds', 86400), 'note': o.get('note', '')}

    def submit_offer(self, db, principal, rid, body):
        principal.require('work:offer')
        r = self.request_row(db, principal, rid)
        if not self._audience_ok(principal, r):
            raise ServiceError('NOT_FOUND', 'work request')
        if r['state'] != 'open':
            raise ServiceError('CONFLICT', {'code': 'request_not_open', 'state': r['state']})
        if r['closes_at'] and r['closes_at'] < now():
            raise ServiceError('EXPIRED', 'offer window closed')
        t, terms = _terms(db, r['terms_id'])
        if t['state'] != 'frozen' or t['digest'] != r['terms_digest']:
            raise ServiceError('CONFLICT', {'code': 'terms_superseded', 'note': 'offers bind the current frozen revision'})
        prow = self.providers.for_principal(db, principal)
        o = self._normalize_offer(body, prow)
        if db.execute("SELECT id FROM work_offers WHERE request_id=? AND provider_id=? AND state='offered'", (rid, prow['id'])).fetchone():
            raise ServiceError('CONFLICT', {'code': 'offer_pending', 'note': 'withdraw the standing offer before submitting another; a provider bidding through several keys still counts per provider'})
        elig = self.providers.eligibility(db, prow, terms, o)
        oid = 'wo_' + secrets.token_hex(8)
        st = {'schema': OFFER_STATEMENT, 'offer_id': oid, 'request_id': rid, 'terms_digest': r['terms_digest'], 'provider_id': prow['id'], 'provider_revision': prow['revision'], 'provider_principal': prow['principal_id'],
              'operation': {k: terms['operation'].get(k) for k in ('kind', 'contract_digest', 'model_id', 'verifier_digest')}, 'deliverables': [d['key'] for d in terms['deliverables']],
              'price': {'amount': o['price_amount'], 'asset': o['asset'], 'scale': o['scale'], 'scheme': o['scheme'], 'note': 'fixed price (exact) or cap (upto) in integer base units'}, 'window_seconds': o['window_seconds'],
              'verification': o['verification'], 'privacy_terms': o['privacy_terms'], 'pay_to': o['pay_to'], 'expires_at': now() + o['expires_in_seconds'], 'issued_at': now(), 'signature': SIGNATURE_LABEL}
        msg, sig, kid = _sign(self.settings, db, st)
        state = 'offered' if elig['eligible'] else 'excluded'
        db.execute('INSERT INTO work_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (oid, principal.workspace, rid, r['terms_digest'], prow['id'], prow['revision'], o['price_amount'], o['asset'], o['scale'], o['scheme'], o['window_seconds'], json.dumps(o['verification']), o['privacy_terms'], o['pay_to'],
                    st['expires_at'], state, json.dumps(elig), o['note'] or None, msg, sig, kid, now(), now()))
        history.record(db, principal.workspace, principal.id, 'work.offer_submitted', 'work_offer', oid, {'request_id': rid, 'provider_id': prow['id'], 'price_amount': o['price_amount'], 'asset': o['asset'], 'scheme': o['scheme'], 'eligible': elig['eligible'], 'reasons': [x['code'] for x in elig['reasons']]})
        return self.offer_view(db, principal, db.execute('SELECT * FROM work_offers WHERE id=?', (oid,)).fetchone())

    def offer_view(self, db, principal, o):
        seq = db.execute('SELECT rowid FROM work_offers WHERE id=?', (o['id'],)).fetchone()[0]
        return {'id': o['id'], 'sequence': seq, 'request_id': o['request_id'], 'state': o['state'], 'provider_id': o['provider_id'], 'provider_revision': o['provider_revision'], 'terms_digest': o['terms_digest'],
                'price_amount': o['price_amount'], 'asset': o['asset'], 'scale': o['scale'], 'scheme': o['scheme'], 'window_seconds': o['window_seconds'], 'verification': json.loads(o['verification_json']), 'privacy_terms': o['privacy_terms'],
                'pay_to': o['pay_to'], 'expires_at': o['expires_at'], 'eligibility': json.loads(o['eligibility_json']), 'reason': o['reason'], 'signature': {'key_id': o['key_id'], 'custody': 'service-custodied', 'label': SIGNATURE_LABEL}, 'created_at': o['created_at']}

    def offer_state(self, db, principal, oid, action, body=None):
        body = body or {}
        o = db.execute('SELECT * FROM work_offers WHERE id=? AND workspace=?', (oid, principal.workspace)).fetchone()
        if o is None:
            raise ServiceError('NOT_FOUND', 'offer')
        r = self.request_row(db, principal, o['request_id'])
        if action == 'withdraw':
            if not self._offer_owner(db, principal, o):
                raise ServiceError('FORBIDDEN', 'not the offering provider')
            if o['state'] != 'offered':
                raise ServiceError('CONFLICT', {'code': 'offer_state', 'state': o['state']})
            db.execute("UPDATE work_offers SET state='withdrawn', reason=?, updated_at=? WHERE id=?", (str(body.get('reason', ''))[:256] or None, now(), oid))
        elif action == 'decline':
            # the provider declines to bid (or withdraws) for a concrete reason; no reputation label is created
            if not self._offer_owner(db, principal, o):
                raise ServiceError('FORBIDDEN', 'not the offering provider')
            db.execute("UPDATE work_offers SET state='declined', reason=?, updated_at=? WHERE id=? AND state IN ('offered','excluded')", (str(body.get('reason', 'declined'))[:256], now(), oid))
        else:
            raise ServiceError('VALIDATION', {'code': 'action', 'allowed': ['withdraw', 'decline']})
        history.record(db, principal.workspace, principal.id, 'work.offer_state', 'work_offer', oid, {'state': action, 'request_id': r['id']})
        return self.offer_view(db, principal, db.execute('SELECT * FROM work_offers WHERE id=?', (oid,)).fetchone())

    # ---- comparison and ranking (§22) --------------------------------------------------------------------------------------
    def compare_offers(self, db, principal, rid):
        r = self.request_row(db, principal, rid)
        if principal.id != r['requester_id'] and not principal.can('work:award'):
            raise ServiceError('FORBIDDEN', 'comparison is the requester\'s view')
        t, terms = _terms(db, r['terms_id'])
        sel = terms['selection']
        offers = [self.offer_view(db, principal, o) for o in db.execute('SELECT * FROM work_offers WHERE request_id=? ORDER BY created_at, id', (rid,)).fetchall()]
        for o in offers:
            if o['state'] == 'offered' and o['expires_at'] < now():
                o['state'] = 'expired'
        eligible = [o for o in offers if o['state'] == 'offered' and o['eligibility']['eligible']]
        rank_of = {c: i for i, c in enumerate(terms_mod.VERIFICATION_CLASSES)}
        def key(o):
            parts = []
            if sel['policy'] == 'lowest_eligible_price':
                parts.append(o['price_amount'])
            elif sel['policy'] == 'weighted':
                w = sel['weights']; ceiling = max(terms['payment']['ceiling'], 1); dl = max(terms['deadlines']['delivery_seconds'], 1)
                score = w['price'] * o['price_amount'] * 1000 // ceiling + w['window_seconds'] * o['window_seconds'] * 1000 // dl + w['verification_class_rank'] * (len(terms_mod.VERIFICATION_CLASSES) - rank_of[o['verification']['class']]) * 1000 // len(terms_mod.VERIFICATION_CLASSES)
                parts.append(score)
            for tb in sel['tie_break']:
                parts.append({'earliest_offer': o['sequence'], 'provider_id': o['provider_id'], 'shortest_window': o['window_seconds']}[tb])
            parts.append(o['id'])
            return tuple(parts)
        ranked = sorted(eligible, key=key) if sel['policy'] != 'manual' else eligible
        fields = ('price_amount', 'asset', 'scheme', 'window_seconds', 'verification', 'privacy_terms', 'pay_to', 'provider_revision', 'expires_at')
        diff = {f: sorted({json.dumps(o[f], sort_keys=True) for o in eligible}) for f in fields}
        return {'request_id': rid, 'policy': {'schema': 'metacoin-selection-policy/v1', 'policy': sel['policy'], 'tie_break': sel['tie_break'], 'weights': sel.get('weights'),
                                              'rule': {'lowest_eligible_price': 'eligibility first; then the lowest fixed price (or cap); ties by the declared tie-breaks in order; then offer id',
                                                       'weighted': 'eligibility first; integer score = sum(weight * normalised value * 1000); lower is better; ties by tie-breaks', 'manual': 'requester chooses among eligible offers with a recorded reason'}[sel['policy']]},
                'eligible': [{'rank': i + 1, 'offer_id': o['id'], 'provider_id': o['provider_id'], 'price_amount': o['price_amount'], 'scheme': o['scheme'], 'window_seconds': o['window_seconds'], 'verification': o['verification'], 'sort_key': list(key(o))[:-1] if sel['policy'] != 'manual' else None} for i, o in enumerate(ranked)],
                'excluded': [{'offer_id': o['id'], 'provider_id': o['provider_id'], 'state': o['state'], 'reasons': [x['code'] for x in o['eligibility']['reasons']] or ([o['state']] if o['state'] != 'offered' else [])} for o in offers if o not in eligible],
                'side_by_side': {'offers': [{f: o[f] for f in fields} | {'offer_id': o['id'], 'provider_id': o['provider_id']} for o in eligible], 'differing_fields': [f for f, vals in diff.items() if len(vals) > 1]},
                'recommended': ranked[0]['id'] if ranked and sel['policy'] != 'manual' else None, 'note': 'no universal trust score: custody, independence, correctness class, latency and price stay separate columns'}

    # ---- award (§23) -----------------------------------------------------------------------------------------------------------
    def award(self, db, principal, rid, body):
        """One guarded transaction (BEGIN IMMEDIATE by the caller): validate, reserve, instantiate milestones, dispatch."""
        principal.require('work:award')
        body = body or {}
        r = self.request_row(db, principal, rid)
        if r['requester_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the requester awards its request')
        oid = body.get('offer_id')
        existing = db.execute('SELECT * FROM work_awards WHERE offer_id=?', (oid,)).fetchone() if oid else None
        if existing is not None:
            return dict(self.award_view(db, principal, existing), replayed=True, note='the award already exists; no second reservation was made')
        if r['state'] not in ('open', 'paused', 'awarded'):
            raise ServiceError('CONFLICT', {'code': 'request_state', 'state': r['state']})
        t, terms = _terms(db, r['terms_id'])
        if t['state'] != 'frozen' or t['digest'] != r['terms_digest']:
            raise ServiceError('CONFLICT', {'code': 'terms_superseded', 'note': 'the request must be re-opened on the current revision'})
        active = db.execute('SELECT COUNT(*) FROM work_awards WHERE request_id=? AND active=1', (rid,)).fetchone()[0]
        if active >= r['max_awards']:
            raise ServiceError('CONFLICT', {'code': 'award_limit_reached', 'max_awards': r['max_awards'], 'active': active})
        cmp = self.compare_offers(db, principal, rid)
        if oid is None:
            oid = cmp['recommended']
            if oid is None:
                raise ServiceError('CONFLICT', {'code': 'no_eligible_offer' if not cmp['eligible'] else 'manual_selection_required', 'excluded': cmp['excluded']})
        o = db.execute('SELECT * FROM work_offers WHERE id=? AND request_id=?', (oid, rid)).fetchone()
        if o is None:
            raise ServiceError('NOT_FOUND', 'offer for this request')
        if o['state'] != 'offered' or o['expires_at'] < now():
            raise ServiceError('CONFLICT', {'code': 'offer_not_awardable', 'state': o['state'] if o['expires_at'] >= now() else 'expired'})
        if o['terms_digest'] != r['terms_digest']:
            raise ServiceError('CONFLICT', {'code': 'stale_offer', 'note': 'the offer bound an earlier terms digest'})
        if not json.loads(o['eligibility_json'])['eligible']:
            raise ServiceError('CONFLICT', {'code': 'offer_ineligible', 'reasons': json.loads(o['eligibility_json'])['reasons']})
        prow = db.execute('SELECT * FROM providers WHERE id=?', (o['provider_id'],)).fetchone()
        if prow['revision'] != o['provider_revision']:
            raise ServiceError('CONFLICT', {'code': 'provider_revised_since_offer', 'offer_revision': o['provider_revision'], 'current': prow['revision'], 'note': 'the award binds the exact offered revision; ask for a fresh offer'})
        if body.get('expected_price') is not None and body['expected_price'] != o['price_amount']:
            raise ServiceError('STATE_CONFLICT', {'code': 'price_changed', 'offer_price': o['price_amount']})
        selection = {'policy': cmp['policy'], 'ranked': [e['offer_id'] for e in cmp['eligible']], 'selected': oid, 'recommended': cmp['recommended'], 'manual': oid != cmp['recommended'], 'reason': body.get('reason')}
        if selection['manual'] and (type(body.get('reason')) is not str or not body['reason'].strip()):
            raise ServiceError('VALIDATION', {'code': 'manual_reason_required', 'recommended': cmp['recommended']})
        approval_gate(self.svc.approvals, db, principal, 'work_award')
        from ..agents import grant_of, guard
        if grant_of(principal):
            guard(db, principal, 'work:award', service_kind=terms['operation']['kind'], amount=o['price_amount'] + terms['payment'].get('verifier_compensation', 0), precheck_jobs=1)
        aid = 'wa_' + secrets.token_hex(8)
        ceiling = o['price_amount'] + terms_mod.fee_amount(terms['payment'], o['price_amount']) + terms['payment'].get('verifier_compensation', 0)
        if terms['payment'].get('funding', 'requester') == 'treasury':
            node, rsv = None, None
            if not db.execute("SELECT id FROM treasury_allocations WHERE terms_id=? AND state='reserved'", (t['id'],)).fetchone():
                raise ServiceError('CONFLICT', {'code': 'treasury_allocation_required', 'note': 'allocate the treasury budget for these terms before awarding'})
        else:
            root = budgets.root(db, principal.workspace)
            node = budgets.create_child(db, principal.workspace, root['id'], 'campaign', 'award:' + aid, min(ceiling, root['ceiling']))
            try:
                rsv = budgets.reserve(db, principal.workspace, node, ceiling, 'work_award', aid)
            except ServiceError as exc:
                db.execute('DELETE FROM budget_nodes WHERE id=?', (node,))
                raise ServiceError('BUDGET_EXHAUSTED', dict(exc.detail if isinstance(exc.detail, dict) else {}, note='nothing awarded; nothing reserved'))
        from .ops import fault
        fault(db, self.settings, 'reservation_posting')
        db.execute('INSERT INTO work_awards (id, workspace, request_id, offer_id, terms_id, terms_digest, provider_id, provider_revision, pay_to, state, active, ceiling, reserved, budget_node_id, reservation_id, ack_deadline, delivery_deadline, selection_json, awarded_by, awarded_at, created_at, updated_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)',
                   (aid, principal.workspace, rid, oid, t['id'], t['digest'], prow['id'], prow['revision'], o['pay_to'], 'awarded', ceiling, ceiling, node, rsv, now() + terms['deadlines']['acknowledge_seconds'], now() + min(o['window_seconds'], terms['deadlines']['delivery_seconds']),
                    json.dumps(selection), principal.id, now(), now(), now()))
        if terms['payment'].get('funding', 'requester') == 'treasury':
            getattr(self, 'treasury').on_award(db, {'id': aid, 'terms_id': t['id']}, terms)
        db.execute("UPDATE work_offers SET state='awarded', updated_at=? WHERE id=?", (now(), oid))
        for other in db.execute("SELECT id FROM work_offers WHERE request_id=? AND state='offered' AND id!=?", (rid, oid)).fetchall():
            if active + 1 >= r['max_awards']:
                db.execute("UPDATE work_offers SET state='superseded', reason='another offer was awarded', updated_at=? WHERE id=?", (now(), other['id']))
        db.execute("UPDATE work_requests SET state='awarded', updated_at=? WHERE id=?", (now(), rid))
        for m in terms['milestones']:
            db.execute('INSERT INTO work_milestones (id, workspace, award_id, key, state, max_payment, depends_json, deadline_at, contract_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       ('wm_' + secrets.token_hex(6), principal.workspace, aid, m['key'], 'pending', min(m['max_payment'], o['price_amount']), json.dumps(m['depends_on']), now() + m['deadline_seconds'], (m.get('operation') or {}).get('contract_id'), now()))
        history.record(db, principal.workspace, principal.id, 'work.awarded', 'work_award', aid, {'request_id': rid, 'offer_id': oid, 'provider_id': prow['id'], 'provider_revision': prow['revision'], 'terms_digest': t['digest'], 'ceiling': ceiling, 'reserved': ceiling, 'budget_node': node, 'selection': selection['policy']['policy'], 'manual': selection['manual']})
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'work_request', rid, 'work_award', aid, 'awarded'); add_edge(db, principal.workspace, 'work_offer', oid, 'work_award', aid, 'bound_offer')
        self.dispatch_ready(db, aid)
        fault(db, self.settings, 'award_commit')
        return self.award_view(db, principal, db.execute('SELECT * FROM work_awards WHERE id=?', (aid,)).fetchone())

    # ---- milestones, dispatch and attempts (§16, §19) --------------------------------------------------------------------------
    def _requester_principal(self, db, award):
        row = db.execute('SELECT * FROM principals WHERE id=?', (award['awarded_by'],)).fetchone()
        p = auth.Principal(row); p.scope = None
        return p

    def dispatch_ready(self, db, aid):
        """Milestones become executable only when their dependencies reached the required ACCEPTANCE state (not when
        worker processes exited). Independent milestones run concurrently within the existing limits."""
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (aid,)).fetchone()
        if award is None or award['state'] in ('withdrawn', 'reassigned', 'closed'):
            return []
        t, terms = _terms(db, award['terms_id'])
        prow = db.execute('SELECT * FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        ex = json.loads(prow['execution_json'])
        ms = {m['key']: m for m in db.execute('SELECT * FROM work_milestones WHERE award_id=?', (aid,)).fetchall()}
        spec = {m['key']: m for m in terms['milestones']}
        started = []
        for key, m in ms.items():
            if m['state'] != 'pending':
                continue
            deps = json.loads(m['depends_json']); need = spec[key].get('requires_acceptance_of', {})
            blocked = []; cancelled = False
            for d in deps:
                want = need.get(d, 'accepted')
                st = ms[d]['state']
                # 'accepted' and 'accepted_or_valid_negative' both mean an ACCEPTANCE decision (a verified negative is accepted, not failed)
                ok = st == 'accepted' if want in ('accepted', 'accepted_or_valid_negative') else st in ('accepted', 'rejected', 'cancelled')
                negative_stop = False
                if want == 'accepted_positive' and st == 'accepted':
                    dec = db.execute('SELECT evaluation_json FROM work_decisions WHERE id=?', (ms[d]['decision_id'],)).fetchone()
                    sci = json.loads(dec['evaluation_json'])['science'] if dec else None
                    ok = sci in ('FEASIBLE', 'not_applicable')
                    negative_stop = not ok
                elif want == 'accepted_positive':
                    ok = False
                if negative_stop:
                    # a valid, PAID negative intentionally stops downstream work (§16): the dependent is cancelled by policy, never failed
                    db.execute("UPDATE work_milestones SET state='cancelled', blocked_reason=?, updated_at=? WHERE id=?", ('dependency %s concluded %s: downstream work stopped by policy (accepted_positive required)' % (d, sci), now(), m['id']))
                    history.record(db, award['workspace'], 'scheduler', 'work.milestone_state', 'work_milestone', m['id'], {'award_id': aid, 'key': key, 'state': 'cancelled', 'reason': 'valid_negative_stops_downstream', 'dependency': d})
                    blocked.append({'milestone': d, 'state': st, 'required': want, 'science': sci}); cancelled = True; continue
                if not ok:
                    blocked.append({'milestone': d, 'state': st, 'required': want})
                    if st in ('rejected', 'cancelled') and spec[d]['on_failure'] in ('stop_downstream', 'cancel_dependents'):
                        db.execute("UPDATE work_milestones SET state='cancelled', blocked_reason=?, updated_at=? WHERE id=?", ('dependency %s %s (policy %s)' % (d, st, spec[d]['on_failure']), now(), m['id']))
                        history.record(db, award['workspace'], 'scheduler', 'work.milestone_state', 'work_milestone', m['id'], {'award_id': aid, 'key': key, 'state': 'cancelled', 'reason': 'dependency_' + st})
                        cancelled = True
            if blocked:
                if not cancelled:
                    db.execute("UPDATE work_milestones SET blocked_reason=?, updated_at=? WHERE id=?", (json.dumps(blocked), now(), m['id']))
                continue
            if m['contract_id'] is None:
                db.execute("UPDATE work_milestones SET blocked_reason=?, updated_at=? WHERE id=?", ('no bound operation for this milestone (freeze with milestone_inputs)', now(), m['id']))
                continue
            req = self._requester_principal(db, award)
            prev = db.execute('SELECT id, state FROM jobs WHERE contract_id=? ORDER BY created_at DESC LIMIT 1', (m['contract_id'],)).fetchone()
            try:
                # a replacement attempt (reassignment / new award on the same frozen operation) supersedes a TERMINAL earlier job;
                # a live earlier job blocks dispatch instead of producing a competing publication
                jid = self.svc.jobs.submit(db, req, m['contract_id'], supersede=prev['id'] if prev else None)
            except ServiceError as exc:
                db.execute("UPDATE work_milestones SET blocked_reason=?, updated_at=? WHERE id=?", (json.dumps(exc.body()), now(), m['id']))
                continue
            loc = json.dumps([ex['node_id']]) if ex['type'] == 'node' else json.dumps(['local'])
            db.execute('UPDATE jobs SET location_policy=? WHERE id=?', (loc, jid))
            gen = (db.execute('SELECT COALESCE(MAX(generation),0) FROM work_attempts WHERE milestone_id=?', (m['id'],)).fetchone()[0] or 0) + 1
            wat = 'wat_' + secrets.token_hex(6)
            db.execute('INSERT INTO work_attempts (id, workspace, award_id, milestone_id, provider_id, generation, job_id, state, started_at) VALUES (?,?,?,?,?,?,?,?,?)', (wat, award['workspace'], aid, m['id'], award['provider_id'], gen, jid, 'dispatched', now()))
            db.execute("UPDATE work_milestones SET state='executing', job_id=?, blocked_reason=NULL, updated_at=? WHERE id=?", (jid, now(), m['id']))
            if award['state'] == 'awarded':
                db.execute("UPDATE work_awards SET state='executing', updated_at=? WHERE id=?", (now(), aid))
            history.record(db, award['workspace'], 'scheduler', 'work.attempt', 'work_attempt', wat, {'award_id': aid, 'milestone': key, 'job_id': jid, 'generation': gen, 'execution': ex['type'], 'location_policy': json.loads(loc)})
            from ..datasets import add_edge
            add_edge(db, award['workspace'], 'work_award', aid, 'job', jid, 'executes_milestone:' + key)
            started.append({'milestone': key, 'job_id': jid, 'attempt': wat})
        return started

    def acknowledge(self, db, principal, aid):
        principal.require('work:ack')
        a = self.award_row(db, principal, aid)
        prow = db.execute('SELECT principal_id FROM providers WHERE id=?', (a['provider_id'],)).fetchone()
        if prow['principal_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'not the awarded provider')
        if a['acknowledged_at'] is not None:
            return dict(self.award_view(db, principal, a), replayed=True)
        if a['state'] in ('withdrawn', 'reassigned', 'closed'):
            raise ServiceError('CONFLICT', {'code': 'award_state', 'state': a['state']})
        late = now() > a['ack_deadline']
        db.execute("UPDATE work_awards SET acknowledged_at=?, state=CASE WHEN state='awarded' THEN 'acknowledged' ELSE state END, updated_at=? WHERE id=?", (now(), now(), aid))
        history.record(db, principal.workspace, principal.id, 'work.acknowledged', 'work_award', aid, {'late': late, 'note': 'acknowledgement records receipt of the award, not completion'})
        return self.award_view(db, principal, self.award_row(db, principal, aid))

    def tick(self, db, aid=None):
        """Advance attempts and milestones from the job records (idempotent; called by the worker loop and on reads)."""
        rows = db.execute("SELECT * FROM work_attempts WHERE state IN ('dispatched','running')" + (' AND award_id=?' if aid else ''), ((aid,) if aid else ())).fetchall()
        changed = 0
        for at in rows:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (at['job_id'],)).fetchone()
            if job is None:
                continue
            ms = db.execute('SELECT * FROM work_milestones WHERE id=?', (at['milestone_id'],)).fetchone()
            award = db.execute('SELECT * FROM work_awards WHERE id=?', (at['award_id'],)).fetchone()
            if job['state'] == 'running' and at['state'] == 'dispatched':
                db.execute("UPDATE work_attempts SET state='running' WHERE id=?", (at['id'],))
                if award['acknowledged_at'] is None:
                    db.execute("UPDATE work_awards SET acknowledged_at=?, state='executing', updated_at=? WHERE id=?", (now(), now(), award['id']))
                    history.record(db, award['workspace'], job['lease_owner'] or 'worker', 'work.acknowledged', 'work_award', award['id'], {'by_claim': True, 'job_id': job['id']})
                changed += 1
            elif job['state'] == 'succeeded':
                db.execute("UPDATE work_attempts SET state='completed', finished_at=?, evidence_root=? WHERE id=?", (job['finished_at'], job['evidence_root'], at['id']))
                db.execute("UPDATE work_milestones SET state='delivered', evidence_root=?, delivered_at=?, updated_at=? WHERE id=? AND state='executing'", (job['evidence_root'], job['finished_at'], now(), ms['id']))
                history.record(db, award['workspace'], 'scheduler', 'work.milestone_state', 'work_milestone', ms['id'], {'award_id': award['id'], 'key': ms['key'], 'state': 'delivered', 'job_id': job['id'], 'evidence_root': job['evidence_root'], 'execution': 'completed', 'science': job['outcome'], 'acceptance': 'pending', 'payment': 'reserved'})
                changed += 1
            elif job['state'] in ('failed', 'cancelled'):
                db.execute("UPDATE work_attempts SET state='failed', finished_at=?, note=? WHERE id=?", (job['finished_at'], job['error_code'], at['id']))
                db.execute("UPDATE work_milestones SET state='delivered', updated_at=?, blocked_reason=? WHERE id=? AND state='executing'", (now(), 'execution ' + job['state'] + ': ' + str(job['error_code']), ms['id']))
                history.record(db, award['workspace'], 'scheduler', 'work.milestone_state', 'work_milestone', ms['id'], {'award_id': award['id'], 'key': ms['key'], 'state': 'delivered', 'execution': job['state'], 'error_code': job['error_code'], 'science': 'no_valid_evidence', 'acceptance': 'pending', 'payment': 'reserved'})
                changed += 1
        return changed

    def award_row(self, db, principal, aid):
        a = db.execute('SELECT * FROM work_awards WHERE id=? AND workspace=?', (aid, principal.workspace)).fetchone()
        if a is None:
            raise ServiceError('NOT_FOUND', 'award')
        return a

    def award_view(self, db, principal, a):
        principal.require('work:read')
        (getattr(self, 'evidence', None).tick(db, a['id']) if getattr(self, 'evidence', None) is not None else self.tick(db, a['id']))
        a = db.execute('SELECT * FROM work_awards WHERE id=?', (a['id'],)).fetchone()
        t, terms = _terms(db, a['terms_id'])
        prow = db.execute('SELECT * FROM providers WHERE id=?', (a['provider_id'],)).fetchone()
        is_party = principal.id in (a['awarded_by'], prow['principal_id']) or principal.can('work:award') or principal.role == 'reviewer'
        ms = []
        for m in db.execute('SELECT * FROM work_milestones WHERE award_id=? ORDER BY rowid', (a['id'],)).fetchall():
            job = db.execute('SELECT state, outcome, error_code, evidence_root, finished_at FROM jobs WHERE id=?', (m['job_id'],)).fetchone() if m['job_id'] else None
            ent = db.execute('SELECT id, state, amount FROM work_entitlements WHERE id=?', (m['entitlement_id'],)).fetchone() if m['entitlement_id'] and self._has_table(db, 'work_entitlements') else None
            ms.append({'id': m['id'], 'key': m['key'], 'state': m['state'], 'max_payment': m['max_payment'], 'depends_on': json.loads(m['depends_json']), 'deadline_at': m['deadline_at'], 'contract_id': m['contract_id'], 'job_id': m['job_id'],
                       'blocked_reason': m['blocked_reason'], 'evidence_root': m['evidence_root'], 'decision_id': m['decision_id'], 'entitlement_id': m['entitlement_id'], 'delivered_at': m['delivered_at'],
                       'dimensions': {'execution': ({'queued': 'queued', 'running': 'running', 'succeeded': 'completed', 'failed': 'failed', 'cancelled': 'cancelled'}[job['state']] if job else 'not_started'),
                                      'science': (job['outcome'] if job and job['state'] == 'succeeded' and is_party else ('no_valid_evidence' if job and job['state'] in ('failed', 'cancelled') else 'unknown' if job else 'not_applicable')),
                                      'acceptance': {'pending': 'pending', 'ready': 'pending', 'executing': 'pending', 'delivered': 'pending', 'accepted': 'accepted', 'rejected': 'rejected', 'disputed': 'disputed', 'cancelled': 'not_applicable', 'superseded': 'superseded'}[m['state']],
                                      'payment': (ent['state'] if ent else ('reserved' if m['state'] not in ('cancelled',) else 'released'))},
                       'attempts': [dict(x) for x in db.execute('SELECT id, generation, job_id, state, started_at, finished_at, evidence_root, receipt_id, note FROM work_attempts WHERE milestone_id=? ORDER BY generation, rowid', (m['id'],)).fetchall()]})
        out = {'id': a['id'], 'state': a['state'], 'active': bool(a['active']), 'request_id': a['request_id'], 'offer_id': a['offer_id'], 'terms_id': a['terms_id'], 'terms_digest': a['terms_digest'], 'provider_id': a['provider_id'], 'provider_revision': a['provider_revision'],
               'provider_execution': json.loads(prow['execution_json'])['type'], 'provider_relationship': json.loads(prow['relationship_json'])['relationship'], 'pay_to': a['pay_to'] if is_party else None,
               'ceiling': a['ceiling'], 'reserved': a['reserved'], 'budget_node_id': a['budget_node_id'], 'reservation_id': a['reservation_id'], 'funds': 'allocated in the application budget tree (reservation); not on-chain escrow; nothing authorized or transferred by the award',
               'ack_deadline': a['ack_deadline'], 'delivery_deadline': a['delivery_deadline'], 'acknowledged_at': a['acknowledged_at'], 'awarded_by': a['awarded_by'], 'awarded_at': a['awarded_at'], 'closed_at': a['closed_at'], 'close_reason': a['close_reason'], 'replaced_by': a['replaced_by'],
               'selection': json.loads(a['selection_json']) if is_party else None, 'milestones': ms, 'kind': terms['operation']['kind'], 'title': terms['title']}
        return out

    @staticmethod
    def _has_table(db, name):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def list_awards(self, db, principal, state=None, mine=False):
        principal.require('work:read')
        sql, args = 'SELECT * FROM work_awards WHERE workspace=?', [principal.workspace]
        if principal.role == 'provider':
            prow = db.execute('SELECT id FROM providers WHERE principal_id=?', (principal.id,)).fetchone()
            sql += ' AND provider_id=?'; args.append(prow['id'] if prow else '-')
        if state:
            sql += ' AND state=?'; args.append(state)
        return [self.award_view(db, principal, a) for a in db.execute(sql + ' ORDER BY awarded_at DESC LIMIT 200', args).fetchall()]

    def milestone_row(self, db, principal, aid, key):
        m = db.execute('SELECT * FROM work_milestones WHERE award_id=? AND key=? AND workspace=?', (aid, key, principal.workspace)).fetchone()
        if m is None:
            raise ServiceError('NOT_FOUND', 'milestone')
        return m
