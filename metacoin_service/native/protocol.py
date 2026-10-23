"""Protocol types, canonical encoding, signed messages and the published transition tables (standard library only).

Everything economic in the native protocol is one of these records, canonically encoded (sorted keys, ASCII, integers
only, no floats, no duplicate keys), bound to the protocol version and a message-type domain tag before it is signed.
A signature over one message type cannot be replayed as another type, in another environment, or at another authority."""
import hashlib
import json
import re

PROTOCOL = 'metacoin-native/v0'
POLICY_VERSION = 'native-policy/v0'
MAX_AMOUNT = (1 << 62) - 1                    # bounded integer base units; sums of a few amounts never overflow 64 bits
MAX_STR = 128
_ASSET_RE = re.compile(r'^(dev|test|production):[a-z0-9][a-z0-9-]{0,47}:[a-z0-9][a-z0-9-]{0,47}:[A-Za-z0-9][A-Za-z0-9-]{0,31}:(0|[1-9]|1[0-8])$')
ENVIRONMENTS = ('dev', 'test', 'production')

# message types and their domain-separation tags (the tag is hashed into every signature)
MESSAGE_TYPES = {
    'intent': b'metacoin/native/v0/intent\0',                 # an actor's signed authorization to move value
    'allocation_export': b'metacoin/native/v0/export\0',     # a domain's export commitment
    'allocation_certificate': b'metacoin/native/v0/certificate\0',
    'import_ack': b'metacoin/native/v0/import-ack\0',
    'return_request': b'metacoin/native/v0/return-request\0',
    'return_certificate': b'metacoin/native/v0/return-certificate\0',
    'commit_checkpoint': b'metacoin/native/v0/checkpoint\0',
    'envelope': b'metacoin/native/v0/envelope\0',
    'receipt': b'metacoin/native/v0/receipt\0',
    'rotation': b'metacoin/native/v0/rotation\0',           # an authority announces its next epoch key, signed by the retiring key
    'observation': b'metacoin/native/v0/observation\0',     # a peer's signed list of the allocations it imported from a domain (recovery evidence)
}

# the economic categories of an account (disjoint; views derive each from the journal postings)
CATEGORIES = ('available', 'reserved', 'committed_outgoing', 'in_transit', 'disputed')
DERIVED_VIEWS = ('earned_unsettled', 'pending_claims', 'settled_in', 'settled_out')

# intent / transfer state machine (published; every transition names its precondition, effect and idempotency rule)
INTENT_STATES = ('proposed', 'reserved', 'authorized', 'committed', 'settled', 'pending', 'uncertain', 'disputed', 'rejected', 'cancelled')
INTENT_TRANSITIONS = [
    {'from': 'proposed', 'to': 'reserved', 'precondition': 'actor may act within its grant; available >= amount ceiling; recipient permitted; asset/domain/environment match', 'effect': 'available -> reserved (journal); grant exposure counted', 'idempotency': 'request_id: a repeated proposal returns the same intent'},
    {'from': 'reserved', 'to': 'authorized', 'precondition': 'signer holds the account key; canonical message bound to protocol, environment, actor, authority, asset, ceiling, recipient, obligation, grant, nonce, policy', 'effect': 'signature stored; nothing moves', 'idempotency': 'same nonce -> same signature; a changed field is a new intent'},
    {'from': 'authorized', 'to': 'committed', 'precondition': 'the domain authority (single fenced writer) orders the transition; obligation (if any) outstanding >= amount', 'effect': 'reserved -> committed_outgoing (payer); obligation partially/fully satisfied', 'idempotency': 'commit sequence assigned once; retries return the recorded commit'},
    {'from': 'committed', 'to': 'settled', 'precondition': 'same transaction as commit in the local domain (domain-local settlement)', 'effect': 'committed_outgoing -> recipient available; fee legs to the treasury account; receipt issued', 'idempotency': 'settlement sequence equals commit sequence'},
    {'from': 'reserved', 'to': 'cancelled', 'precondition': 'not yet committed', 'effect': 'reserved -> available (release of provably unexposed amount)', 'idempotency': 'repeat returns cancelled'},
    {'from': 'authorized', 'to': 'cancelled', 'precondition': 'not yet committed; signature retained as evidence', 'effect': 'reserved -> available', 'idempotency': 'repeat returns cancelled'},
    {'from': 'proposed', 'to': 'rejected', 'precondition': 'any precondition failed before value changed', 'effect': 'none', 'idempotency': 'the refusal is recorded with its reason'},
    {'from': 'committed', 'to': 'uncertain', 'precondition': 'the response to the submitter was lost after commit', 'effect': 'none: the commit stands; the submitter must reconcile by intent id', 'idempotency': 'reconciliation returns settled'},
    {'from': 'settled', 'to': 'disputed', 'precondition': 'a bounded dispute referencing this settlement is opened', 'effect': 'the disputed amount is marked on the recipient (available -> disputed) only to the extent still available; history untouched', 'idempotency': 'one dispute per settlement'},
]
ALLOCATION_STATES = ('reserved', 'export_committed', 'certified', 'imported', 'acknowledged', 'return_requested', 'returned', 'quarantined')
ALLOCATION_TRANSITIONS = [
    {'from': '-', 'to': 'reserved', 'where': 'source', 'precondition': 'source account available >= amount; destination domain pinned', 'effect': 'available -> in_transit (journal)', 'crash_boundary': 'source reservation'},
    {'from': 'reserved', 'to': 'export_committed', 'where': 'source', 'precondition': 'authority orders the export; epoch current', 'effect': 'in_transit -> backing:exported (nonspendable at the source, irrevocable once a certificate is issued)', 'crash_boundary': 'source export commit'},
    {'from': 'export_committed', 'to': 'certified', 'where': 'source', 'precondition': 'certificate signed by the source authority (bound to destination, asset, amount, allocation id, epoch, policy, commit evidence) and placed in the outbox', 'effect': 'certificate durable; source backing stays locked', 'crash_boundary': 'certificate publication'},
    {'from': 'certified', 'to': 'imported', 'where': 'destination', 'precondition': 'certificate verifies under the pinned source root; destination == this domain; allocation id unseen (tombstone check); epoch policy', 'effect': 'claim credited: backing:claim -> recipient available at the destination (spendable there)', 'crash_boundary': 'destination durable import'},
    {'from': 'imported', 'to': 'acknowledged', 'where': 'both', 'precondition': 'signed import acknowledgment delivered to the source', 'effect': 'source marks the allocation acknowledged; nothing moves', 'crash_boundary': 'acknowledgment'},
    {'from': 'imported|acknowledged', 'to': 'return_requested', 'where': 'destination', 'precondition': 'the full allocation is unspent at the destination (partial return unsupported)', 'effect': 'destination claim made nonspendable (available -> in_transit) and a signed return request issued', 'crash_boundary': 'return initiation'},
    {'from': 'return_requested', 'to': 'returned', 'where': 'source', 'precondition': 'return request verifies under the pinned destination root and names an allocation this source exported', 'effect': 'backing:exported -> source account available; destination burns its claim on the return certificate', 'crash_boundary': 'return completion'},
    {'from': '*', 'to': 'quarantined', 'where': 'either', 'precondition': 'conflicting signed histories, cloned authority or an unresolved rollback watermark', 'effect': 'the allocation and the domain\'s outward spending are frozen until an administrator reconciles', 'crash_boundary': 'none (safety state)'},
]
FINALITY = {
    'local_authorization': 'the actor may attempt the action within its grant (nothing recorded yet)',
    'durable_local_commit': 'the domain authority ordered and durably recorded the economic transition (commit sequence)',
    'delivery_acknowledgment': 'another endpoint received a message or artifact (says nothing about acceptance or settlement)',
    'work_acceptance': 'the frozen contract policy produced an acceptance decision on the delivered evidence root',
    'cross_domain_pending_claim': 'an exported allocation whose import or backing is still being reconciled',
    'domain_local_settlement': 'the obligation was paid under this domain\'s declared rules (spendable here)',
    'reconciled_inter_domain_settlement': 'both domains\' durable state and the backing satisfy the transfer policy (acknowledged allocation; no duplicated ownership)',
}
TRUST_MODEL = {
    'supported_domain': 'a trusted ordering authority with one fenced writer (SQLite BEGIN IMMEDIATE on durable storage), authenticated actors, explicit administrator powers',
    'not_provided': ['Byzantine fault tolerance', 'independent validators (replicated processes of one operator are not independent)', 'equivocation prevention by signature alone (a key holder can sign two histories)', 'public-network finality', 'launched currency'],
    'detected': ['conflicting signed histories for one allocation id', 'certificates addressed to another domain', 'replayed allocation ids (tombstones)', 'rollback below the export watermark after a backup restore'],
    'boundary': 'crash / retry tolerance is provided by durable commits and idempotent request ids; Byzantine authority behaviour is detected and quarantined where two signed histories meet, never prevented',
}


class ProtocolError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code); self.code = code; self.detail = detail or {}


def canonical(obj):
    """Canonical JSON bytes: sorted keys, ASCII, no floats, no bools-as-int confusion, bounded strings."""
    def check(v, depth=0):
        if depth > 16:
            raise ProtocolError('canonical_depth')
        if isinstance(v, bool) or v is None or isinstance(v, int):
            if isinstance(v, int) and not isinstance(v, bool) and abs(v) > (1 << 63):
                raise ProtocolError('integer_out_of_bounds')
            return
        if isinstance(v, float):
            raise ProtocolError('float_forbidden')
        if isinstance(v, str):
            if len(v) > 4096:
                raise ProtocolError('string_too_long')
            return
        if isinstance(v, list):
            [check(x, depth + 1) for x in v]; return
        if isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(k, str):
                    raise ProtocolError('key_not_string')
                check(x, depth + 1)
            return
        raise ProtocolError('unsupported_type', {'type': type(v).__name__})
    check(obj)
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def parse_strict(raw):
    """Strict JSON: duplicate keys and floats are rejected."""
    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ProtocolError('duplicate_key', {'key': k})
            d[k] = v
        return d
    try:
        return json.loads(raw, object_pairs_hook=no_dupes, parse_float=lambda s: (_ for _ in ()).throw(ProtocolError('float_forbidden')), parse_constant=lambda s: (_ for _ in ()).throw(ProtocolError('constant_forbidden')))
    except ProtocolError:
        raise
    except Exception as exc:
        raise ProtocolError('malformed_json', {'error': type(exc).__name__})


def amount(v, allow_zero=False):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError('amount_not_integer')
    if v < 0 or (v == 0 and not allow_zero) or v > MAX_AMOUNT:
        raise ProtocolError('amount_out_of_bounds', {'value': v, 'max': MAX_AMOUNT})
    return v


def asset_id(s):
    """environment:authority:ledger:asset:scale — every part explicit; a resource unit is never an asset."""
    if not isinstance(s, str) or not _ASSET_RE.match(s):
        raise ProtocolError('asset_id_invalid', {'format': 'environment:authority:ledger:asset:scale'})
    return s


def asset_parts(s):
    env, authority, ledger, asset, scale = asset_id(s).split(':')
    return {'environment': env, 'authority': authority, 'ledger': ledger, 'asset': asset, 'scale': int(scale)}


def short_str(v, name):
    if not isinstance(v, str) or not 1 <= len(v) <= MAX_STR:
        raise ProtocolError('field_invalid', {'field': name})
    return v


def signing_bytes(message_type, body):
    tag = MESSAGE_TYPES.get(message_type)
    if tag is None:
        raise ProtocolError('unknown_message_type', {'type': message_type})
    return tag + hashlib.sha256(canonical(body)).digest()


def digest(body):
    return hashlib.sha256(canonical(body)).hexdigest()


# ---- message schemas (explicit field sets; unknown fields are refused) ------------------------------------------------------------
INTENT_FIELDS = {'protocol', 'type', 'environment', 'actor_account', 'authority', 'asset', 'amount_ceiling', 'recipient', 'permitted_recipients', 'obligation_id', 'grant_id', 'nonce', 'policy_version', 'request_id', 'purpose', 'expires_at'}
INTENT_REQUIRED = {'protocol', 'type', 'environment', 'actor_account', 'authority', 'asset', 'amount_ceiling', 'nonce', 'policy_version', 'request_id'}
ENVELOPE_FIELDS = {'protocol', 'type', 'id', 'environment', 'sender', 'receiver', 'message_type', 'causality', 'payload_digest', 'expires_at', 'created_at'}
ACK_FIELDS = {'protocol', 'type', 'allocation_id', 'environment', 'source_authority', 'destination_authority', 'import_sequence', 'imported_at'}
RETURN_REQUEST_FIELDS = {'protocol', 'type', 'allocation_id', 'environment', 'source_authority', 'destination_authority', 'asset', 'amount', 'recipient_account', 'destination_sequence', 'requested_at'}
RETURN_CERT_FIELDS = {'protocol', 'type', 'allocation_id', 'environment', 'source_authority', 'destination_authority', 'asset', 'amount', 'source_sequence', 'returned_at'}
ROTATION_FIELDS = {'protocol', 'type', 'authority', 'environment', 'old_epoch', 'new_epoch', 'old_key_id', 'new_key_id', 'new_public_hex', 'grace_seconds', 'rotated_at'}
OBSERVATION_FIELDS = {'protocol', 'type', 'observer', 'observed', 'environment', 'allocations', 'issued_at'}
MESSAGE_FIELDS = {'import_ack': ACK_FIELDS, 'return_request': RETURN_REQUEST_FIELDS, 'return_certificate': RETURN_CERT_FIELDS, 'rotation': ROTATION_FIELDS, 'observation': OBSERVATION_FIELDS}
CERT_FIELDS = {'protocol', 'type', 'allocation_id', 'environment', 'source_authority', 'source_epoch', 'destination_authority', 'asset', 'amount', 'recipient_account', 'policy_version', 'commit_sequence', 'commit_digest', 'issued_at'}


def validate_intent(body, environment, authority):
    if not isinstance(body, dict):
        raise ProtocolError('intent_not_object')
    unknown = set(body) - INTENT_FIELDS
    if unknown:
        raise ProtocolError('unknown_critical_field', {'fields': sorted(unknown)})
    missing = INTENT_REQUIRED - set(body)
    if missing:
        raise ProtocolError('missing_field', {'fields': sorted(missing)})
    if body['protocol'] != PROTOCOL or body['type'] != 'intent':
        raise ProtocolError('protocol_mismatch', {'expected': PROTOCOL})
    if body['environment'] != environment:
        raise ProtocolError('environment_mismatch', {'expected': environment, 'got': body['environment']})
    if body['authority'] != authority:
        raise ProtocolError('authority_mismatch', {'expected': authority, 'got': body['authority']})
    asset_id(body['asset']); amount(body['amount_ceiling'])
    if asset_parts(body['asset'])['environment'] != environment:
        raise ProtocolError('asset_environment_mismatch')
    for f in ('actor_account', 'nonce', 'policy_version', 'request_id'):
        short_str(body[f], f)
    if body.get('recipient') is not None:
        short_str(body['recipient'], 'recipient')
    if body.get('permitted_recipients') is not None and (not isinstance(body['permitted_recipients'], list) or not all(isinstance(x, str) for x in body['permitted_recipients'])):
        raise ProtocolError('field_invalid', {'field': 'permitted_recipients'})
    if body['policy_version'] != POLICY_VERSION:
        raise ProtocolError('policy_version_unknown', {'expected': POLICY_VERSION})
    if 'expires_at' in body and (type(body['expires_at']) is not int or isinstance(body['expires_at'], bool) or body['expires_at'] <= 0):
        raise ProtocolError('expires_at', {'note': 'integer unix seconds; a signer refuses to sign after it and a domain refuses to settle after it; expiry never moves value'})
    return body


def validate_certificate(body, environment):
    if not isinstance(body, dict) or set(body) != CERT_FIELDS:
        raise ProtocolError('certificate_fields', {'expected': sorted(CERT_FIELDS), 'got': sorted(body) if isinstance(body, dict) else None})
    if body['protocol'] != PROTOCOL or body['type'] != 'allocation_certificate' or body['environment'] != environment or body['policy_version'] != POLICY_VERSION:
        raise ProtocolError('certificate_binding')
    asset_id(body['asset']); amount(body['amount'])
    for f in ('allocation_id', 'source_authority', 'destination_authority', 'recipient_account', 'commit_digest'):
        short_str(body[f], f)
    if not isinstance(body['source_epoch'], int) or not isinstance(body['commit_sequence'], int) or not isinstance(body['issued_at'], int):
        raise ProtocolError('certificate_integers')
    return body


def validate_message(message_type, body, environment):
    """Field-exact validation of the small inter-domain control messages (certificates have their own validator)."""
    if message_type == 'allocation_certificate':
        return validate_certificate(body, environment)
    fields = MESSAGE_FIELDS.get(message_type)
    if fields is None:
        raise ProtocolError('unsupported_message_type', {'type': message_type})
    if not isinstance(body, dict) or set(body) != fields:
        raise ProtocolError('message_fields', {'type': message_type, 'expected': sorted(fields), 'got': sorted(body) if isinstance(body, dict) else None})
    if body['protocol'] != PROTOCOL or body['type'] != message_type or body['environment'] != environment:
        raise ProtocolError('message_binding', {'type': message_type})
    for k, v in body.items():
        if isinstance(v, str):
            short_str(v, k)
        elif isinstance(v, bool) or (not isinstance(v, (int, list)) and v is not None):
            raise ProtocolError('message_value', {'field': k})
    if 'amount' in body:
        amount(body['amount'])
    if 'asset' in body:
        asset_id(body['asset'])
    if message_type == 'observation':
        if not isinstance(body['allocations'], list) or len(body['allocations']) > 10000:
            raise ProtocolError('observation_list')
        for a in body['allocations']:
            if not isinstance(a, dict) or set(a) != {'allocation_id', 'commit_sequence', 'commit_digest', 'state'} or not isinstance(a['commit_sequence'], int):
                raise ProtocolError('observation_item')
    return body


def validate_envelope(body):
    if not isinstance(body, dict) or set(body) != ENVELOPE_FIELDS:
        raise ProtocolError('envelope_fields', {'expected': sorted(ENVELOPE_FIELDS)})
    if body['protocol'] != PROTOCOL or body['type'] != 'envelope' or body['environment'] not in ENVIRONMENTS:
        raise ProtocolError('envelope_binding')
    for f in ('id', 'sender', 'receiver', 'message_type', 'payload_digest'):
        short_str(body[f], f)
    if body['message_type'] not in MESSAGE_TYPES:
        raise ProtocolError('unknown_message_type', {'type': body['message_type']})
    if body['causality'] is not None:
        short_str(body['causality'], 'causality')
    if not isinstance(body['expires_at'], int) or not isinstance(body['created_at'], int):
        raise ProtocolError('envelope_integers')
    return body


def spec():
    """The machine-readable protocol specification (rendered to PROTOCOL.md by the delivery)."""
    return {'protocol': PROTOCOL, 'policy_version': POLICY_VERSION, 'environments': ENVIRONMENTS, 'asset_id': 'environment:authority:ledger:asset:scale (regex %s)' % _ASSET_RE.pattern, 'amount': 'integer base units 1..%d (0 only where a zero amount is explicitly allowed)' % MAX_AMOUNT,
            'encoding': 'canonical JSON (sorted keys, ASCII, integers only, depth <= 16, strings <= 4096); strict parsing rejects duplicate keys, floats and constants', 'message_types': {k: v.decode().rstrip('\0') for k, v in MESSAGE_TYPES.items()},
            'signature': 'Ed25519 over tag || sha256(canonical(body)); the tag separates message types; the body binds protocol, environment and authority',
            'account_categories': CATEGORIES, 'derived_views': DERIVED_VIEWS, 'intent_states': INTENT_STATES, 'intent_transitions': INTENT_TRANSITIONS, 'allocation_states': ALLOCATION_STATES, 'allocation_transitions': ALLOCATION_TRANSITIONS,
            'finality': FINALITY, 'trust_model': TRUST_MODEL,
            'intent_fields': sorted(INTENT_FIELDS), 'certificate_fields': sorted(CERT_FIELDS), 'envelope_fields': sorted(ENVELOPE_FIELDS),
            'ordering': 'one commit sequence per domain, assigned by the fenced writer in transaction order; messages carry a causality reference and are applied idempotently by id with durable tombstones',
            'replay': 'intent nonces are unique per account; allocation ids and envelope ids are tombstoned for as long as they can affect economic state (never garbage-collected below the export watermark)',
            'failure_and_recovery': 'every transition is one durable transaction with its outbox record; a lost response is reconciled by id (uncertain != not committed); a restored backup below the export watermark enters quarantine until reconciled; timeouts never recreate value',
            'what_a_development_ledger_is_not': ['a launched currency', 'decentralized consensus', 'public-network adoption', 'space-qualified deployment']}
