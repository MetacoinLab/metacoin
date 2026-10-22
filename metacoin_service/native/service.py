"""Native protocol service: the domain identity (authority key, epoch, fenced writer), actor accounts with their own
protected development keys, isolated fixture funding, signed intents, atomic reservation, domain-local settlement of
obligations, bounded partial payments and refunds, receipts, and the account/domain views."""
import json
import secrets

from .. import auth, crypto, history
from ..db import now
from ..errors import ServiceError
from . import protocol as P, ledger

ENV_OF = {'simulation': 'dev', 'test-http': 'dev', 'production': 'production'}
CUSTODY_DEV = 'development key held by this service instance on the actor\'s behalf (0600 file under keys_dir/actors); NOT operator custody and NOT an independent signer'
CUSTODY_AUTHORITY = 'domain authority key held by this service instance (single fenced writer); the operator of the host controls it'


class Native:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services
        self.environment = ENV_OF.get(settings.provider_mode, 'dev')
        self._local = {}

    # ---- domain --------------------------------------------------------------------------------------------------------------
    def _key_path(self, name):
        d = self.settings.keys_dir / 'native'
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d / (name + '.ed25519')

    def _ensure_key(self, name):
        path = self._key_path(name)
        if not path.exists():
            return path, crypto.generate_signing_key(path)
        from cryptography.hazmat.primitives import serialization as _ser
        pub = crypto.load_signing_key(path).public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw).hex()
        return path, pub

    def domain(self, db, workspace='ws_default'):
        r = db.execute('SELECT * FROM native_domains WHERE workspace=?', (workspace,)).fetchone()
        if r is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'domain_not_initialized', 'note': 'POST /api/v1/native/domain (native:admin) initializes this instance as a settlement domain'})
        self._local[workspace] = r['id']
        return self.rollback_check(db, r)

    def network_of(self, asset):
        """Journal scope for a native asset: this domain (claims on another domain are held in this domain's ledger)."""
        if self._local:
            return next(iter(self._local.values()))
        try:
            with self.svc.db.read() as db:
                r = db.execute('SELECT id, workspace FROM native_domains LIMIT 1').fetchone()
            if r is not None:
                self._local[r['workspace']] = r['id']; return r['id']
        except Exception:
            pass
        return asset.split(':')[1]

    def init_domain(self, db, principal, body):
        """Make this instance a settlement domain: identity, authority key (generated locally), epoch 1, operator and host labels."""
        principal.require('native:admin')
        if type(body) is not dict or set(body) - {'domain_id', 'operator', 'host', 'asset_label'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['domain_id', 'operator', 'host', 'asset_label']})
        did = body.get('domain_id')
        if type(did) is not str or not 1 <= len(did) <= 48 or not did.replace('-', '').isalnum() or did != did.lower():
            raise ServiceError('VALIDATION', {'code': 'domain_id', 'rule': 'lowercase letters, digits and hyphens, <= 48'})
        ex = db.execute('SELECT * FROM native_domains WHERE workspace=?', (principal.workspace,)).fetchone()
        if ex is not None:
            if ex['id'] != did:
                raise ServiceError('CONFLICT', {'code': 'domain_exists', 'domain_id': ex['id']})
            return self.domain_view(db, principal)
        path, pub = self._ensure_key('authority-' + did)
        kid = crypto.key_id_for(pub)
        db.execute('INSERT INTO native_domains VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (did, principal.workspace, self.environment, kid, pub, 1, (body.get('operator') or 'same operator (local development)')[:128], (body.get('host') or 'this host')[:128], P.POLICY_VERSION, 'online', 'normal', 0, 0, now(), now()))
        asset = '%s:%s:%s:%s:0' % (self.environment, did, 'ledger-' + did, body.get('asset_label') or 'TMETA')
        P.asset_id(asset)
        db.execute('INSERT INTO native_assets VALUES (?,?,?,?,?,?)', (asset, principal.workspace, 'Test-META of domain %s (zero value, development)' % did, 1, did, now()))
        for kind, label in (('treasury', 'MetaStar treasury (fee-funded; never mints)'), ('fixture', 'development fixture funding source (isolated; zero-value)')):
            db.execute('INSERT INTO native_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)', ('na_' + kind + '_' + did, principal.workspace, None, kind, label, None, None, 'system account of the domain (no signing key; moves only through protocol transitions)', 0, 'active', now()))
        history.record(db, principal.workspace, principal.id, 'native.domain', 'native_domain', did, {'authority_key_id': kid, 'epoch': 1, 'asset': asset})
        return self.domain_view(db, principal)

    def domain_view(self, db, principal):
        principal.require('native:read')
        d = self.domain(db, principal.workspace)
        assets = [dict(a) for a in db.execute('SELECT * FROM native_assets WHERE workspace=?', (principal.workspace,)).fetchall()]
        peers = [{k: p[k] for k in ('domain_id', 'authority_key_id', 'epoch', 'trust_policy', 'state', 'pinned_at')} for p in db.execute('SELECT * FROM native_peers WHERE workspace=?', (principal.workspace,)).fetchall()]
        sys_accts = {r['kind']: r['id'] for r in db.execute("SELECT id, kind FROM native_accounts WHERE workspace=? AND kind IN ('treasury','fixture')", (principal.workspace,)).fetchall()}
        return {'schema': 'metacoin-native-domain/v0', 'protocol': P.PROTOCOL, 'id': d['id'], 'default_asset': self.default_asset(db, principal.workspace), 'accounts': sys_accts, 'environment': d['environment'], 'authority': {'key_id': d['authority_key_id'], 'public_hex': d['authority_public_hex'], 'epoch': d['epoch'], 'custody': CUSTODY_AUTHORITY},
                'operator': d['operator'], 'host': d['host'], 'policy_version': d['policy_version'], 'connectivity': d['connectivity'], 'recovery_state': d['recovery_state'], 'commit_sequence': d['commit_sequence'], 'export_watermark': d['export_watermark'],
                'assets': assets, 'peers': peers, 'trust_model': P.TRUST_MODEL, 'finality': P.FINALITY,
                'what_this_is_not': P.spec()['what_a_development_ledger_is_not'], 'x402': {'required_by_native_core': False, 'imported_by_native_package': False}}

    def set_connectivity(self, db, principal, state):
        principal.require('native:admin')
        if state not in ('online', 'cut'):
            raise ServiceError('VALIDATION', {'code': 'connectivity', 'allowed': ['online', 'cut']})
        d = self.domain(db, principal.workspace)
        db.execute('UPDATE native_domains SET connectivity=?, updated_at=? WHERE id=?', (state, now(), d['id']))
        history.record(db, principal.workspace, principal.id, 'native.domain', 'native_domain', d['id'], {'connectivity': state})
        return self.domain_view(db, principal)

    def next_sequence(self, db, workspace):
        d = self.domain(db, workspace)
        seq = d['commit_sequence'] + 1
        db.execute('UPDATE native_domains SET commit_sequence=?, updated_at=? WHERE id=?', (seq, now(), d['id']))
        return seq

    def authority_sign(self, db, workspace, message_type, body):
        d = self.domain(db, workspace)
        key = crypto.load_signing_key(self._current_key_path(db, d))
        return crypto.sign(key, P.signing_bytes(message_type, body)), d['authority_key_id']

    def default_asset(self, db, workspace):
        d = self.domain(db, workspace)
        a = db.execute('SELECT asset_id FROM native_assets WHERE workspace=? AND origin_domain=? ORDER BY registered_at LIMIT 1', (workspace, d['id'])).fetchone()
        return a['asset_id']

    def is_native_asset(self, db, workspace, asset):
        return db.execute('SELECT 1 FROM native_assets WHERE workspace=? AND asset_id=?', (workspace, asset)).fetchone() is not None

    # ---- accounts and actors -------------------------------------------------------------------------------------------------
    def account_of(self, db, principal_or_id, workspace=None, create=True):
        pid = principal_or_id if isinstance(principal_or_id, str) else principal_or_id.id
        ws = workspace or (principal_or_id.workspace if not isinstance(principal_or_id, str) else 'ws_default')
        r = db.execute('SELECT * FROM native_accounts WHERE workspace=? AND principal_id=?', (ws, pid)).fetchone()
        if r is not None or not create:
            return r
        prow = db.execute('SELECT * FROM principals WHERE id=?', (pid,)).fetchone()
        if prow is None:
            raise ServiceError('NOT_FOUND', 'principal')
        aid = 'na_' + secrets.token_hex(6)
        path, pub = self._ensure_key('actor-' + aid)
        db.execute('INSERT INTO native_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)', (aid, ws, pid, 'actor', prow['name'][:64], crypto.key_id_for(pub), pub, CUSTODY_DEV, 0, 'active', now()))
        history.record(db, ws, pid, 'native.account', 'native_account', aid, {'principal_id': pid, 'key_id': crypto.key_id_for(pub), 'custody': 'development'})
        return db.execute('SELECT * FROM native_accounts WHERE id=?', (aid,)).fetchone()

    def account_row(self, db, workspace, aid):
        r = db.execute('SELECT * FROM native_accounts WHERE id=? AND workspace=?', (aid, workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'native account')
        return r

    def system_account(self, db, workspace, kind):
        d = self.domain(db, workspace)
        return 'na_%s_%s' % (kind, d['id'])

    def create_actor(self, db, principal, body):
        """An operator creates a native actor: a principal with the actor role, one credential (shown once), its own account and key."""
        principal.require('native:admin'); principal.require('admin:credentials')
        if type(body) is not dict or set(body) - {'name', 'credential_seconds'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['name', 'credential_seconds']})
        name = body.get('name')
        if type(name) is not str or not 1 <= len(name) <= 64:
            raise ServiceError('VALIDATION', 'name')
        pid = auth.create_principal(db, name, 'actor', principal.workspace)
        cid, token = auth.issue_credential(db, pid, body.get('credential_seconds', 30 * 86400))
        acct = self.account_of(db, pid, principal.workspace)
        return {'principal_id': pid, 'account_id': acct['id'], 'role': 'actor', 'credential': {'id': cid, 'token': token, 'shown_once': True}, 'key_id': acct['key_id'], 'custody': acct['custody']}

    def account_view(self, db, principal, aid, asset=None):
        principal.require('native:read')
        r = self.account_row(db, principal.workspace, aid)
        d = self.domain(db, principal.workspace); asset = asset or self.default_asset(db, principal.workspace)
        P.asset_id(asset)
        cats = ledger.categories(db, principal.workspace, asset, d['id'], aid)
        earned = db.execute("SELECT COALESCE(SUM(amount - settled_amount),0) FROM native_obligations WHERE workspace=? AND payee_account=? AND asset=? AND state IN ('open','partial')", (principal.workspace, aid, asset)).fetchone()[0]
        owed = db.execute("SELECT COALESCE(SUM(amount - settled_amount),0) FROM native_obligations WHERE workspace=? AND payer_account=? AND asset=? AND state IN ('open','partial')", (principal.workspace, aid, asset)).fetchone()[0]
        pend = db.execute("SELECT COALESCE(SUM(a.amount),0) FROM native_allocations a WHERE a.workspace=? AND a.recipient_account=? AND a.direction='import' AND a.state='imported' AND EXISTS (SELECT 1 FROM native_envelopes e WHERE e.direction='out' AND e.message_type='import_ack' AND e.causality=a.id AND e.state='queued')", (principal.workspace, aid)).fetchone()[0]
        exports = db.execute("SELECT COALESCE(SUM(amount),0) FROM native_allocations WHERE workspace=? AND source_account=? AND direction='export' AND state IN ('reserved','export_committed','certified','imported')", (principal.workspace, aid)).fetchone()[0]
        private = principal.id == r['principal_id'] or principal.can('native:admin')
        return {'schema': 'metacoin-native-account/v0', 'id': aid, 'kind': r['kind'], 'label': r['label'], 'principal_id': r['principal_id'] if private else None, 'domain': d['id'], 'asset': asset,
                'available': cats['available'], 'reserved': cats['reserved'], 'committed_outgoing': cats['committed_outgoing'], 'in_transit': cats['in_transit'], 'disputed': cats['disputed'],
                'earned_unsettled': earned, 'obligations_outstanding': owed, 'pending_inter_domain_claims': pend, 'exported_awaiting_acknowledgment': exports,
                'spendable_now': cats['available'], 'signer': {'key_id': r['key_id'], 'custody': r['custody']} if private else None,
                'meaning': {'available': 'settled, spendable in this domain now', 'reserved': 'set aside for an award or an intent; not spendable, not yet committed', 'committed_outgoing': 'ordered by the authority; leaves on settlement in the same transaction',
                            'in_transit': 'made nonspendable for an export or a return in progress', 'disputed': 'held under a bounded dispute', 'earned_unsettled': 'accepted obligations owed to this account; NOT spendable until settled',
                            'pending_inter_domain_claims': 'imported allocations not yet acknowledged by their source (spendable here under this domain\'s rules; reconciliation pending)'},
                'rebuilt_from_journal': True}

    def accounts(self, db, principal):
        principal.require('native:read')
        return [self.account_view(db, principal, r['id']) for r in db.execute('SELECT id FROM native_accounts WHERE workspace=? ORDER BY created_at', (principal.workspace,)).fetchall()]

    # ---- isolated development fixtures -----------------------------------------------------------------------------------------
    def fixture_fund(self, db, principal, body):
        """Install development funding: zero-value test asset only, native:admin only, recorded as equity:fixture_funding. No ordinary
        API, agent tool or acceptance path reaches this; production assets are refused."""
        principal.require('native:admin')
        if self.settings.provider_mode == 'production':
            raise ServiceError('FORBIDDEN', {'code': 'fixture_in_production', 'note': 'fixture funding exists for development instances only'})
        if type(body) is not dict or set(body) - {'account_id', 'amount', 'asset', 'label'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['account_id', 'amount', 'asset', 'label']})
        d = self.domain(db, principal.workspace); asset = body.get('asset') or self.default_asset(db, principal.workspace)
        arow = db.execute('SELECT * FROM native_assets WHERE asset_id=? AND workspace=?', (asset, principal.workspace)).fetchone()
        if arow is None or not arow['zero_value'] or P.asset_parts(asset)['environment'] == 'production':
            raise ServiceError('FORBIDDEN', {'code': 'fixture_asset', 'note': 'fixtures fund zero-value development assets of this domain only'})
        acct = self.account_row(db, principal.workspace, body.get('account_id'))
        try:
            amt = P.amount(body.get('amount'))
        except P.ProtocolError as exc:
            raise ServiceError('VALIDATION', {'code': exc.code, **exc.detail})
        fid = 'nf_' + secrets.token_hex(6)
        ledger.move(db, principal.workspace, 'fixture:' + fid, 'fixture_funding', asset, d['id'], self.environment, amt, 'equity:fixture_funding', ledger.account_name(acct['id'], 'available'), 'native_account', acct['id'], (body.get('label') or 'development fixture funding')[:200])
        history.record(db, principal.workspace, principal.id, 'native.fixture', 'native_account', acct['id'], {'amount': amt, 'asset': asset, 'fixture': fid, 'note': 'isolated development funding; not issuance'})
        return self.account_view(db, principal, acct['id'], asset)

    # ---- signed intents and transfers -------------------------------------------------------------------------------------------
    def _sign_account(self, db, acct, message_type, body):
        key = crypto.load_signing_key(self._key_path('actor-' + acct['id']))
        return crypto.sign(key, P.signing_bytes(message_type, body))

    def _check_grant(self, db, principal, amount, recipient):
        from ..agents import grant_of, guard
        if grant_of(principal):
            guard(db, principal, 'native:transfer', amount=amount)

    def transfer(self, db, principal, body):
        """Proposed -> reserved -> authorized -> committed -> settled in one domain transaction (each step journaled; fault points at
        the reservation, the commit and the response). Idempotent by (account, request_id)."""
        principal.require('native:transfer')
        if type(body) is not dict or set(body) - {'from_account', 'to_account', 'amount', 'asset', 'obligation_id', 'request_id', 'purpose', 'permitted_recipients'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['from_account?', 'to_account', 'amount', 'asset?', 'obligation_id?', 'request_id', 'purpose?']})
        d = self.domain(db, principal.workspace)
        if d['recovery_state'] != 'normal':
            raise ServiceError('CONFLICT', {'code': 'domain_quarantined', 'recovery_state': d['recovery_state'], 'note': 'outward spending is refused until the domain is reconciled'})
        src = self.account_row(db, principal.workspace, body['from_account']) if body.get('from_account') else self.account_of(db, principal)
        if src['principal_id'] != principal.id and not (src['kind'] == 'treasury' and principal.can('work:treasury')):
            raise ServiceError('FORBIDDEN', {'code': 'not_account_holder', 'note': 'only the account holder (or the treasury authority for the treasury account) can spend'})
        dst = self.account_row(db, principal.workspace, body.get('to_account'))
        asset = body.get('asset') or self.default_asset(db, principal.workspace)
        try:
            P.asset_id(asset); amt = P.amount(body.get('amount'))
        except P.ProtocolError as exc:
            raise ServiceError('VALIDATION', {'code': exc.code, **exc.detail})
        if not self.is_native_asset(db, principal.workspace, asset):
            raise ServiceError('VALIDATION', {'code': 'asset_unknown_here', 'asset': asset, 'note': 'this domain settles only assets it registered or imported; action-units and external assets are not convertible'})
        rid = body.get('request_id')
        if type(rid) is not str or not 1 <= len(rid) <= 128:
            raise ServiceError('VALIDATION', 'request_id')
        ex = db.execute('SELECT * FROM native_intents WHERE actor_account=? AND request_id=?', (src['id'], rid)).fetchone()
        if ex is not None:
            msg = json.loads(ex['message_json'])
            if (msg['amount_ceiling'], msg.get('recipient'), msg['asset'], msg.get('obligation_id')) != (amt, dst['id'], asset, body.get('obligation_id')):
                raise ServiceError('IDEMPOTENCY_CONFLICT', {'code': 'request_id_reused_with_different_content'})
            return dict(self.intent_view(db, principal, ex['id']), replayed=True)
        if src['id'] == dst['id']:
            raise ServiceError('VALIDATION', {'code': 'self_transfer'})
        obligation = None
        if body.get('obligation_id'):
            obligation = db.execute('SELECT * FROM native_obligations WHERE id=? AND workspace=?', (body['obligation_id'], principal.workspace)).fetchone()
            if obligation is None or obligation['payer_account'] != src['id'] or obligation['payee_account'] != dst['id'] or obligation['asset'] != asset:
                raise ServiceError('CONFLICT', {'code': 'obligation_binding', 'note': 'the obligation names another payer, payee or asset'})
            outstanding = obligation['amount'] - obligation['settled_amount']
            if obligation['state'] in ('settled', 'cancelled'):
                raise ServiceError('CONFLICT', {'code': 'obligation_already_settled', 'obligation': obligation['id'], 'settled_amount': obligation['settled_amount']})
            if amt > outstanding:
                raise ServiceError('CONFLICT', {'code': 'amount_above_outstanding', 'outstanding': outstanding})
            if amt < outstanding and not json.loads(obligation['policy_json']).get('partial_payments', False):
                raise ServiceError('CONFLICT', {'code': 'partial_payment_unsupported', 'outstanding': outstanding, 'note': 'this obligation\'s policy does not allow partial payments'})
        self._check_grant(db, principal, amt, dst['id'])
        nonce = '%s-%d' % (src['id'], src['nonce_counter'] + 1)
        msg = {'protocol': P.PROTOCOL, 'type': 'intent', 'environment': self.environment, 'actor_account': src['id'], 'authority': d['id'], 'asset': asset, 'amount_ceiling': amt, 'recipient': dst['id'], 'obligation_id': body.get('obligation_id'),
               'grant_id': (getattr(principal, 'scope', None) or {}).get('grant_id'), 'nonce': nonce, 'policy_version': P.POLICY_VERSION, 'request_id': rid, 'purpose': (body.get('purpose') or '')[:MAX_PURPOSE]}
        P.validate_intent(msg, self.environment, d['id'])
        iid = 'ni_' + secrets.token_hex(8); hist = []
        from ..economy.ops import fault
        # reserve
        ledger.require_available(db, principal.workspace, asset, d['id'], src['id'], amt)
        ledger.move(db, principal.workspace, 'reserve:' + iid, 'native_reservation', asset, d['id'], self.environment, amt, ledger.account_name(src['id'], 'available'), ledger.account_name(src['id'], 'reserved'), 'native_intent', iid, 'reserved for a signed intent')
        hist.append({'state': 'reserved', 'at': now()})
        db.execute('UPDATE native_accounts SET nonce_counter=nonce_counter+1 WHERE id=?', (src['id'],))
        db.execute('INSERT INTO native_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (iid, principal.workspace, d['id'], src['id'], principal.id, 'transfer', asset, amt, None, dst['id'], body.get('obligation_id'), msg['grant_id'], nonce, rid, json.dumps(msg), P.digest(msg), None, 'reserved', None, None, json.dumps(hist), now(), now()))
        fault(db, self.settings, 'native_reserve')
        # authorize (signature by the account's own key)
        sig = self._sign_account(db, src, 'intent', msg)
        hist.append({'state': 'authorized', 'at': now(), 'key_id': src['key_id']})
        db.execute("UPDATE native_intents SET signature_hex=?, state='authorized', history_json=?, updated_at=? WHERE id=?", (sig, json.dumps(hist), now(), iid))
        # commit + settle (domain-local)
        seq = self.next_sequence(db, principal.workspace)
        ledger.move(db, principal.workspace, 'commit:' + iid, 'native_commit', asset, d['id'], self.environment, amt, ledger.account_name(src['id'], 'reserved'), ledger.account_name(src['id'], 'committed_outgoing'), 'native_intent', iid, 'committed by the domain authority (sequence %d)' % seq)
        hist.append({'state': 'committed', 'at': now(), 'sequence': seq})
        db.execute("UPDATE native_intents SET state='committed', commit_sequence=?, history_json=?, updated_at=? WHERE id=?", (seq, json.dumps(hist), now(), iid))
        fault(db, self.settings, 'native_commit')
        ledger.move(db, principal.workspace, 'settle:' + iid, 'native_settlement', asset, d['id'], self.environment, amt, ledger.account_name(src['id'], 'committed_outgoing'), ledger.account_name(dst['id'], 'available'), 'native_intent', iid, 'domain-local settlement')
        if obligation is not None:
            new_settled = obligation['settled_amount'] + amt
            db.execute("UPDATE native_obligations SET settled_amount=?, state=?, updated_at=? WHERE id=?", (new_settled, 'settled' if new_settled == obligation['amount'] else 'partial', now(), obligation['id']))
        hist.append({'state': 'settled', 'at': now(), 'sequence': seq})
        db.execute("UPDATE native_intents SET state='settled', amount_final=?, history_json=?, updated_at=? WHERE id=?", (amt, json.dumps(hist), now(), iid))
        self._receipt(db, principal.workspace, 'settlement', 'native_intent', iid, {'intent_id': iid, 'from': src['id'], 'to': dst['id'], 'asset': asset, 'amount': amt, 'obligation_id': body.get('obligation_id'), 'commit_sequence': seq, 'domain': d['id'], 'epoch': d['epoch'], 'finality': 'domain_local_settlement', 'message_digest': P.digest(msg)})
        history.record(db, principal.workspace, principal.id, 'native.settlement', 'native_intent', iid, {'from': src['id'], 'to': dst['id'], 'amount': amt, 'asset': asset, 'sequence': seq, 'obligation': body.get('obligation_id')})
        fault(db, self.settings, 'native_response')
        return self.intent_view(db, principal, iid)

    def _receipt(self, db, workspace, kind, ref_type, ref_id, claims):
        st = {'schema': 'metacoin-native-receipt/v0', 'protocol': P.PROTOCOL, 'kind': kind, 'ref_type': ref_type, 'ref_id': ref_id, 'claims': claims, 'issued_at': now(), 'nonce': secrets.token_hex(8)}
        sig, kid = self.authority_sign(db, workspace, 'receipt', st)
        rid = 'nr_' + secrets.token_hex(8)
        db.execute('INSERT INTO native_receipts VALUES (?,?,?,?,?,?,?,?,?)', (rid, workspace, kind, ref_type, ref_id, json.dumps(st), sig, kid, now()))
        return rid

    def intent_view(self, db, principal, iid):
        principal.require('native:read')
        r = db.execute('SELECT * FROM native_intents WHERE id=? AND workspace=?', (iid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'native intent')
        acct = db.execute('SELECT principal_id FROM native_accounts WHERE id=?', (r['actor_account'],)).fetchone()
        party = principal.id == acct['principal_id'] or principal.can('native:admin') or (r['recipient'] and db.execute('SELECT principal_id FROM native_accounts WHERE id=?', (r['recipient'],)).fetchone()['principal_id'] == principal.id)
        out = {'schema': 'metacoin-native-intent/v0', 'id': iid, 'type': r['type'], 'domain': r['domain_id'], 'actor_account': r['actor_account'], 'recipient': r['recipient'], 'asset': r['asset'], 'amount_ceiling': r['amount_ceiling'], 'amount_final': r['amount_final'],
               'obligation_id': r['obligation_ref'], 'state': r['state'], 'commit_sequence': r['commit_sequence'], 'reason': r['reason'], 'history': json.loads(r['history_json']), 'created_at': r['created_at'],
               'finality': ('domain_local_settlement' if r['state'] == 'settled' else 'durable_local_commit' if r['state'] == 'committed' else 'local_authorization' if r['state'] in ('reserved', 'authorized') else r['state']),
               'message': json.loads(r['message_json']) if party else None, 'message_digest': r['message_digest'], 'signature_hex': r['signature_hex'] if party else None}
        rc = db.execute("SELECT id, statement_json, signature_hex, key_id FROM native_receipts WHERE ref_type='native_intent' AND ref_id=?", (iid,)).fetchone()
        out['receipt'] = {'id': rc['id'], 'statement': json.loads(rc['statement_json']), 'signature_hex': rc['signature_hex'], 'key_id': rc['key_id']} if rc else None
        return out

    def intents(self, db, principal, account_id=None):
        principal.require('native:read')
        rows = db.execute('SELECT id FROM native_intents WHERE workspace=?' + (' AND (actor_account=? OR recipient=?)' if account_id else '') + ' ORDER BY created_at DESC LIMIT 200', (principal.workspace,) + ((account_id, account_id) if account_id else ())).fetchall()
        return [self.intent_view(db, principal, r['id']) for r in rows]

    # ---- obligations (accepted work, fees, verifier pay) -------------------------------------------------------------------------
    def obligation_create(self, db, workspace, kind, payer_account, payee_account, asset, amount, ref_type, ref_id, policy):
        """Idempotent by (ref_type, ref_id, kind): an accepted entitlement becomes exactly one native obligation."""
        ex = db.execute('SELECT * FROM native_obligations WHERE ref_type=? AND ref_id=? AND kind=?', (ref_type, ref_id, kind)).fetchone()
        if ex is not None:
            return ex['id']
        oid = 'no_' + secrets.token_hex(8)
        db.execute('INSERT INTO native_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (oid, workspace, kind, payer_account, payee_account, asset, amount, 0, ref_type, ref_id, json.dumps(policy), 'open', now(), now()))
        history.record(db, workspace, 'service', 'native.obligation', 'native_obligation', oid, {'kind': kind, 'payer': payer_account, 'payee': payee_account, 'amount': amount, 'asset': asset, 'ref': [ref_type, ref_id]})
        return oid

    def obligation_view(self, db, principal, oid):
        principal.require('native:read')
        r = db.execute('SELECT * FROM native_obligations WHERE id=? AND workspace=?', (oid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'obligation')
        intents = [i['id'] for i in db.execute('SELECT id FROM native_intents WHERE obligation_ref=? ORDER BY created_at', (oid,)).fetchall()]
        return dict({k: r[k] for k in ('id', 'kind', 'payer_account', 'payee_account', 'asset', 'amount', 'settled_amount', 'ref_type', 'ref_id', 'state', 'created_at')}, outstanding=r['amount'] - r['settled_amount'], policy=json.loads(r['policy_json']), intents=intents)

    def obligations(self, db, principal, account_id=None):
        principal.require('native:read')
        rows = db.execute('SELECT id FROM native_obligations WHERE workspace=?' + (' AND (payer_account=? OR payee_account=?)' if account_id else '') + ' ORDER BY created_at DESC LIMIT 200', (principal.workspace,) + ((account_id, account_id) if account_id else ())).fetchall()
        return [self.obligation_view(db, principal, r['id']) for r in rows]

    def refund(self, db, principal, body):
        """A refund is a new transfer from the payee back to the payer, linked to the settled intent and bounded by it; history stays."""
        principal.require('native:transfer')
        if type(body) is not dict or set(body) - {'intent_id', 'amount', 'request_id', 'reason'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['intent_id', 'amount', 'request_id', 'reason']})
        orig = db.execute('SELECT * FROM native_intents WHERE id=? AND workspace=?', (body.get('intent_id'), principal.workspace)).fetchone()
        if orig is None or orig['state'] != 'settled' or orig['type'] != 'transfer':
            raise ServiceError('CONFLICT', {'code': 'refund_source', 'note': 'refunds link to a settled transfer'})
        already = db.execute("SELECT COALESCE(SUM(amount_final),0) FROM native_intents WHERE type='refund' AND obligation_ref=? AND state='settled'", (orig['id'],)).fetchone()[0]
        amt = body.get('amount')
        try:
            P.amount(amt)
        except P.ProtocolError as exc:
            raise ServiceError('VALIDATION', {'code': exc.code, **exc.detail})
        if already + amt > orig['amount_final']:
            raise ServiceError('CONFLICT', {'code': 'refund_above_refundable', 'refundable': orig['amount_final'] - already})
        payee = self.account_row(db, principal.workspace, orig['recipient'])
        if payee['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the recipient of the original transfer refunds it')
        out = self.transfer(db, principal, {'from_account': payee['id'], 'to_account': orig['actor_account'], 'amount': amt, 'asset': orig['asset'], 'request_id': body.get('request_id'), 'purpose': 'refund of ' + orig['id']})
        db.execute("UPDATE native_intents SET type='refund', obligation_ref=? WHERE id=?", (orig['id'], out['id']))
        history.record(db, principal.workspace, principal.id, 'native.settlement', 'native_intent', out['id'], {'refund_of': orig['id'], 'amount': amt, 'reason': (body.get('reason') or '')[:200]})
        return self.intent_view(db, principal, out['id'])

    # ---- audit -------------------------------------------------------------------------------------------------------------------
    def audit(self, db, principal, asset=None):
        principal.require('native:read')
        d = self.domain(db, principal.workspace); asset = asset or self.default_asset(db, principal.workspace)
        a = ledger.audit(db, principal.workspace, asset, d['id'])
        allocs = [dict(x) for x in db.execute("SELECT id, direction, source_domain, destination_domain, amount, state, recipient_account, source_account FROM native_allocations WHERE workspace=? AND asset=?", (principal.workspace, asset)).fetchall()]
        a['allocations'] = allocs; a['unresolved_allocations'] = [x for x in allocs if x['state'] not in ('acknowledged', 'returned')]
        a['operator_trust'] = P.TRUST_MODEL['supported_domain']; a['domain'] = d['id']; a['recovery_state'] = d['recovery_state']
        return a


MAX_PURPOSE = 200


# ---- work-economy hooks (acceptance -> obligations -> native settlement; awards reserve native funds) --------------------------------
def _hooks(cls):
    def reserve_for_award(self, db, principal, aid, terms, ceiling):
        """Atomically with the award: the payer's own funds move available -> reserved (ref award). Treasury-funded awards reserve
        from the treasury account (its allocation was already checked against confirmed fee revenue)."""
        d = self.domain(db, principal.workspace); asset = terms['payment']['asset']; self.require_normal(d, 'award reservation')
        if not self.is_native_asset(db, principal.workspace, asset):
            raise ServiceError('VALIDATION', {'code': 'asset_unknown_here', 'asset': asset})
        funding = terms['payment'].get('funding', 'requester')
        payer = self.system_account(db, principal.workspace, 'treasury') if funding == 'treasury' else self.account_of(db, principal)['id']
        if funding == 'treasury':
            pass                                                   # the treasury allocation already moved available -> reserved for these terms (treasury.allocate)
        else:
            ledger.require_available(db, principal.workspace, asset, d['id'], payer, ceiling)
            ledger.move(db, principal.workspace, 'award-reserve:' + aid, 'native_award_reservation', asset, d['id'], self.environment, ceiling, ledger.account_name(payer, 'available'), ledger.account_name(payer, 'reserved'), 'work_award', aid, 'reserved for an award (ceiling incl. fee and verifier legs)')
        history.record(db, principal.workspace, principal.id, 'native.intent', 'work_award', aid, {'reserved': ceiling, 'payer_account': payer, 'funding': funding})
        return payer

    def _award_payer(self, db, award, terms):
        funding = terms['payment'].get('funding', 'requester')
        return self.system_account(db, award['workspace'], 'treasury') if funding == 'treasury' else self.account_of(db, award['awarded_by'], award['workspace'])['id']

    def _provider_account(self, db, award):
        prow = db.execute('SELECT principal_id FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        return self.account_of(db, prow['principal_id'], award['workspace'])['id']

    def on_accepted(self, db, award, ms, decision_id, amount, terms):
        """Accepted work: provider obligation (payer -> provider account) and, when the terms declare a fee, the fee obligation to the treasury account."""
        asset = terms['payment']['asset']; payer = self._award_payer(db, award, terms)
        policy = {'partial_payments': terms['payment']['scheme'] == 'upto', 'source': 'award_reservation', 'award_id': award['id'], 'milestone_id': ms['id'], 'decision_id': decision_id}
        self.obligation_create(db, award['workspace'], 'provider', payer, self._provider_account(db, award), asset, amount, 'work_milestone', ms['id'], policy)
        from ..economy import terms as terms_mod
        fee = terms_mod.fee_amount(terms['payment'], amount)
        if fee > 0:
            self.obligation_create(db, award['workspace'], 'fee', payer, self.system_account(db, award['workspace'], 'treasury'), asset, fee, 'work_milestone', ms['id'], dict(policy, partial_payments=False, note='protocol fee: additional to the provider amount; funds the MetaStar treasury; never issuance'))

    def on_verifier_obligation(self, db, award, ms, entitlement_id, amount, terms):
        payer = self._award_payer(db, award, terms); payee = terms['payment']['verifier_pay_to']
        if db.execute('SELECT 1 FROM native_accounts WHERE id=? AND workspace=?', (payee, award['workspace'])).fetchone() is None:
            raise ServiceError('CONFLICT', {'code': 'verifier_account_unknown', 'note': 'verifier_pay_to must name a native account of this domain'})
        self.obligation_create(db, award['workspace'], 'verifier', payer, payee, terms['payment']['asset'], amount, 'work_entitlement', entitlement_id, {'partial_payments': False, 'source': 'award_reservation', 'award_id': award['id'], 'milestone_id': ms['id'], 'note': 'verifier compensation is independent of the verdict'})

    def settle_entitlement(self, db, principal, intent_row, e, award, terms, final):
        """Settle one economy entitlement natively from the award reservation: reserved -> committed_outgoing -> payee available,
        the payer account's signed intent recorded, one commit sequence, a signed receipt. Idempotent by the entitlement."""
        d = self.domain(db, principal.workspace); asset = e['asset']
        self.require_normal(d, 'settlement')
        kind = e['kind']; payer = self._award_payer(db, award, terms)
        if kind == 'provider':
            payee = self._provider_account(db, award); ref = ('work_milestone', e['milestone_id'])
        elif kind == 'fee':
            payee = self.system_account(db, principal.workspace, 'treasury'); ref = ('work_milestone', e['milestone_id'])
        else:
            payee = e['recipient']; ref = ('work_entitlement', e['id'])
        ob = db.execute('SELECT * FROM native_obligations WHERE ref_type=? AND ref_id=? AND kind=?', (ref[0], ref[1], kind)).fetchone()
        if ob is None:
            raise ServiceError('CONFLICT', {'code': 'native_obligation_missing', 'entitlement': e['id']})
        ex = db.execute("SELECT * FROM native_intents WHERE obligation_ref=? AND state='settled'", (ob['id'],)).fetchone()
        if ex is not None:
            return self.intent_view(db, principal, ex['id'])
        outstanding = ob['amount'] - ob['settled_amount']
        if final > outstanding:
            raise ServiceError('CONFLICT', {'code': 'amount_above_outstanding', 'outstanding': outstanding})
        reserved = ledger.balance(db, principal.workspace, asset, d['id'], payer, 'reserved')
        if reserved < final:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'reservation_short', 'reserved': reserved, 'requested': final, 'note': 'the award reservation must cover every settlement leg'})
        src = self.account_row(db, principal.workspace, payer)
        nonce = '%s-%d' % (src['id'], src['nonce_counter'] + 1); rid = 'settle-' + e['id']
        msg = {'protocol': P.PROTOCOL, 'type': 'intent', 'environment': self.environment, 'actor_account': payer, 'authority': d['id'], 'asset': asset, 'amount_ceiling': final, 'recipient': payee, 'obligation_id': ob['id'], 'grant_id': None, 'nonce': nonce, 'policy_version': P.POLICY_VERSION, 'request_id': rid, 'purpose': 'settlement of %s obligation %s' % (kind, ob['id'])}
        P.validate_intent(msg, self.environment, d['id'])
        iid = 'ni_' + secrets.token_hex(8)
        sig = self._sign_account(db, src, 'intent', msg) if src['kind'] == 'actor' else None
        seq = self.next_sequence(db, principal.workspace)
        hist = [{'state': 'reserved', 'at': now(), 'from': 'award_reservation'}, {'state': 'authorized', 'at': now(), 'key_id': src['key_id'], 'note': 'treasury account: authority-ordered, no actor signature' if sig is None else None}, {'state': 'committed', 'at': now(), 'sequence': seq}, {'state': 'settled', 'at': now(), 'sequence': seq}]
        db.execute('UPDATE native_accounts SET nonce_counter=nonce_counter+1 WHERE id=?', (src['id'],))
        db.execute('INSERT INTO native_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (iid, principal.workspace, d['id'], payer, principal.id, 'settlement', asset, final, final, payee, ob['id'], None, nonce, rid, json.dumps(msg), P.digest(msg), sig, 'settled', seq, None, json.dumps(hist), now(), now()))
        ledger.move(db, principal.workspace, 'commit:' + iid, 'native_commit', asset, d['id'], self.environment, final, ledger.account_name(payer, 'reserved'), ledger.account_name(payer, 'committed_outgoing'), 'native_intent', iid, 'committed from the award reservation (sequence %d)' % seq)
        from ..economy.ops import fault
        fault(db, self.settings, 'native_commit')
        ledger.move(db, principal.workspace, 'settle:' + iid, 'native_settlement', asset, d['id'], self.environment, final, ledger.account_name(payer, 'committed_outgoing'), ledger.account_name(payee, 'available'), 'native_intent', iid, 'domain-local settlement of a %s obligation' % kind)
        new_settled = ob['settled_amount'] + final
        db.execute("UPDATE native_obligations SET settled_amount=?, state=?, updated_at=? WHERE id=?", (new_settled, 'settled' if new_settled == ob['amount'] else 'partial', now(), ob['id']))
        self._receipt(db, principal.workspace, 'settlement', 'native_intent', iid, {'intent_id': iid, 'from': payer, 'to': payee, 'asset': asset, 'amount': final, 'obligation_id': ob['id'], 'entitlement_id': e['id'], 'kind': kind, 'award_id': award['id'], 'commit_sequence': seq, 'domain': d['id'], 'epoch': d['epoch'], 'finality': 'domain_local_settlement', 'message_digest': P.digest(msg)})
        history.record(db, principal.workspace, principal.id, 'native.settlement', 'native_intent', iid, {'kind': kind, 'from': payer, 'to': payee, 'amount': final, 'asset': asset, 'sequence': seq, 'entitlement': e['id']})
        return self.intent_view(db, principal, iid)

    def release_award(self, db, award, paid):
        """On award close: whatever of the reservation was never committed returns to the payer's available funds."""
        t = db.execute('SELECT terms_json FROM work_terms WHERE id=?', (award['terms_id'],)).fetchone()
        terms = json.loads(t['terms_json']); asset = terms['payment']['asset']
        if not self.is_native_asset(db, award['workspace'], asset):
            return None
        d = self.domain(db, award['workspace']); payer = self._award_payer(db, award, terms)
        reserved_for = award['ceiling']
        settled = db.execute("SELECT COALESCE(SUM(amount_final),0) FROM native_intents WHERE type='settlement' AND state='settled' AND obligation_ref IN (SELECT id FROM native_obligations WHERE json_extract(policy_json, '$.award_id')=?)", (award['id'],)).fetchone()[0]
        keep = reserved_for - settled
        if keep > 0:
            ledger.move(db, award['workspace'], 'award-release:' + award['id'], 'native_award_release', asset, d['id'], self.environment, keep, ledger.account_name(payer, 'reserved'), ledger.account_name(payer, 'available'), 'work_award', award['id'], 'unused reservation released at award close')
        return keep

    def treasury_reserve(self, db, workspace, allocation_id, asset, amount):
        """Treasury-funded terms: the treasury account's own confirmed fee revenue moves available -> reserved (never issuance)."""
        d = self.domain(db, workspace); acct = self.system_account(db, workspace, 'treasury')
        ledger.require_available(db, workspace, asset, d['id'], acct, amount)
        ledger.move(db, workspace, 'treasury-native-reserve:' + allocation_id, 'native_treasury_reservation', asset, d['id'], self.environment, amount, ledger.account_name(acct, 'available'), ledger.account_name(acct, 'reserved'), 'treasury_allocation', allocation_id, 'treasury award reserved from settled fee revenue')

    for f in (reserve_for_award, _award_payer, _provider_account, on_accepted, on_verifier_obligation, settle_entitlement, release_award, treasury_reserve):
        setattr(cls, f.__name__, f)
    return cls


_hooks(Native)
from .domains import _mixin as _domains_mixin   # noqa: E402
_domains_mixin(Native)
