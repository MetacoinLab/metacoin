"""Cross-instance contract and evidence packages (Order 08 §61): an import preview that validates a bundle produced by
another instance, states what it contains, what trust it would need and what this instance could execute, without
awarding work, changing balances, recording revenue or trusting a signer automatically."""
import hashlib
import json
import os
import tempfile

from .. import catalog, crypto, history
from ..db import now
from ..errors import ServiceError
from ..verification import SUPPORT
from . import verify_work

MANDATORY = ('manifest.json', 'statement.json', 'terms.json', 'offer.json', 'award.json', 'milestone.json')
MAX_PACKAGE_BYTES = 64 * 1024 * 1024


def import_preview(db, principal, settings, access, raw, trust_roots=()):
    principal.require('work:read')
    if not raw or len(raw) > MAX_PACKAGE_BYTES:
        raise ServiceError('VALIDATION', {'code': 'package_bytes', 'max': MAX_PACKAGE_BYTES})
    if any(type(x) is not str or not len(x) == 64 for x in trust_roots):
        raise ServiceError('VALIDATION', {'code': 'trust_roots', 'note': 'hex public keys of signers the caller already trusts'})
    local = access.trust_history(db) if access else {'keys': []}
    local_keys = {k['key_id']: k['public_key_hex'] for k in local['keys']}
    journal_before = db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
    fd, path = tempfile.mkstemp(prefix='pkg-', suffix='.zip', dir=str(settings.home / 'run') if (settings.home / 'run').exists() else None)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(raw)
        ok, checks, files = verify_work._archive(path)
        candidates = {crypto.key_id_for(r): r for r in list(trust_roots) + list(local_keys.values())}
        kid = None
        if ok and 'statement.json' in files:
            try:
                kid = json.loads(files['statement.json']).get('key_id')
            except Exception:
                kid = None
        root = candidates.get(kid)                                             # the manifest signer's key, only if the caller or this instance already trusts it
        report = verify_work.verify(path, trust_root=root, trust_roots=[r for k, r in candidates.items() if k != kid])
    finally:
        os.unlink(path)
    digest = hashlib.sha256(raw).hexdigest()
    if not ok:
        history.record(db, principal.workspace, principal.id, 'work.package_preview', 'package', digest[:16], {'outcome': 'rejected', 'bytes': len(raw)})
        return {'schema': 'metacoin-package-preview/v1', 'package_sha256': digest, 'structural': {'ok': False, 'checks': checks}, 'verdict': 'rejected before parsing: unsafe or malformed archive', 'effects': _effects(db, journal_before)}
    names = set(files)
    manifest = json.loads(files['manifest.json']) if 'manifest.json' in files else {}
    statement = json.loads(files['statement.json']) if 'statement.json' in files else {}
    terms = json.loads(files['terms.json']).get('terms', {}) if 'terms.json' in files else {}
    structural = {'mandatory_present': {n: n in names for n in MANDATORY}, 'receipts': sorted(n for n in names if n.startswith('receipts/')), 'decisions': sorted(n for n in names if n.startswith('decisions/')),
                  'verifications': sorted(n for n in names if n.startswith('verifications/')), 'disclosed_evidence': sorted(n for n in names if n.startswith('evidence/')), 'private_disclosures': sorted(n for n in names if n.startswith('private/')),
                  'schema': manifest.get('schema'), 'undisclosed': manifest.get('undisclosed')}
    structural['complete'] = all(structural['mandatory_present'].values()) and bool(report['integrity'] and report['integrity']['ok'])
    # trust: which keys the package relies on and whether this instance or the caller already trusts them
    key_ids = {statement.get('key_id')} | {json.loads(files[n]).get('key_id') for n in names if n.startswith('receipts/') or n.startswith('verifications/')}
    supplied = {crypto.key_id_for(r): r for r in trust_roots}
    trust = []
    for kid in sorted(k for k in key_ids if k):
        src = 'this instance\'s own signing key history' if kid in local_keys else 'trust root supplied by the caller' if kid in supplied else None
        trust.append({'key_id': kid, 'trusted': src is not None, 'source': src or 'unknown signer: not trusted automatically; obtain its public key out of band and pass it as a trust root'})
    # methods: could THIS instance execute or verify the operation?
    kind = (terms.get('operation') or {}).get('kind'); req = ((terms.get('acceptance') or {}).get('required_verification') or {}).get('class')
    methods = {'operation_kind': kind, 'installed_here': kind in catalog.INSTALLED, 'verification_classes_here': sorted(SUPPORT.get(kind, {})), 'required_class': req, 'required_class_available': req in SUPPORT.get(kind, {}) if kind else False,
               'replay_possible_from_package': bool(structural['private_disclosures']) and kind in catalog.INSTALLED,
               'note': 'a structurally complete package can still lack the private evidence needed to replay the science; the verifier report says which'}
    # local knowledge: does this instance already hold these records? (an imported receipt never becomes a local claim)
    award = json.loads(files['award.json']) if 'award.json' in files else {}
    known_award = db.execute('SELECT 1 FROM work_awards WHERE id=?', (award.get('id'),)).fetchone() is not None if award.get('id') else False
    rec_ids = [json.loads(files[n])['statement'].get('receipt_id') for n in names if n.startswith('receipts/')]
    known_receipts = [r for r in rec_ids if r and db.execute('SELECT 1 FROM work_receipts WHERE id=?', (r,)).fetchone()]
    settlements = [json.loads(files[n])['statement'] for n in names if n.startswith('receipts/') and json.loads(files[n]).get('kind') == 'settlement']
    out = {'schema': 'metacoin-package-preview/v1', 'package_sha256': digest, 'bytes': len(raw), 'structural': structural, 'trust_requirements': trust, 'method_availability': methods,
           'verifier_report': {k: report.get(k) for k in ('parsing', 'integrity', 'signer_trust', 'scientific_replay', 'acceptance_evaluation', 'missing_private_evidence', 'verdict')},
           'local_knowledge': {'award_known_here': known_award, 'receipts_known_here': known_receipts, 'origin': 'this instance' if known_award else 'foreign instance (or deleted locally)'},
           'payment_observations': [{'entitlement_id': s.get('claims', {}).get('entitlement_id'), 'rail': s.get('claims', {}).get('rail'), 'final_amount': s.get('claims', {}).get('final_amount'), 'status': 'sender-reported observation; not a live rail query by this instance; creates no revenue, entitlement or journal entry here'} for s in settlements],
           'effects': _effects(db, journal_before), 'next_steps': ['pass the signer keys you trust as trust_roots to turn "unknown signer" into a verified signature',
                                                                   'to execute the same operation here, draft new terms from the package terms (a fresh request, fresh inputs and fresh offers are required); nothing is awarded by importing']}
    history.record(db, principal.workspace, principal.id, 'work.package_preview', 'package', digest[:16], {'outcome': 'previewed', 'structural_complete': structural['complete'], 'trusted_keys': sum(t['trusted'] for t in trust), 'bytes': len(raw)})
    return out


def _effects(db, journal_before):
    after = db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
    return {'awards_created': 0, 'journal_entries_added': after - journal_before, 'signers_trusted': 0, 'balances_changed': False, 'statement': 'preview only: no award, balance, revenue, entitlement or trust change'}
