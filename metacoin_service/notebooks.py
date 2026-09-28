"""§65-2 private experiment notebooks: immutable versions of structured narrative blocks that link to immutable
objects (jobs, artifacts, verification records, knowledge answers, calibration models/datasets, evaluation runs).
No notebook code is executed: text blocks are data; link blocks are checked to exist in the workspace and the
referenced object's commitment (digest / evidence root / statement) is snapshotted at version time, so a later
reader can tell whether the referenced state changed. Export is authorized and carries only identifiers, digests
and the author's own text: never private payloads."""
import hashlib
import json
import secrets

from experiments.private_receipts import receipt as merkle
from . import history
from .db import now
from .errors import ServiceError

MAX_BLOCKS, MAX_TEXT, MAX_NAME = 200, 20000, 96
LINK_KINDS = ('job', 'artifact', 'verification', 'knowledge_answer', 'calibration_model', 'calibration_dataset', 'evaluation_run', 'knowledge_document_version')


def validate_blocks(blocks):
    if type(blocks) is not list or not 1 <= len(blocks) <= MAX_BLOCKS:
        raise ServiceError('VALIDATION', 'blocks: 1..%d' % MAX_BLOCKS)
    ids = set()
    for b in blocks:
        if type(b) is not dict or type(b.get('id')) is not str or not 1 <= len(b['id']) <= 32 or b['id'] in ids:
            raise ServiceError('VALIDATION', 'block: unique string id')
        ids.add(b['id'])
        if b.get('type') == 'text':
            if type(b.get('text')) is not str or not 1 <= len(b['text']) <= MAX_TEXT or type(b.get('heading', '')) is not str:
                raise ServiceError('VALIDATION', 'text block: text (1..%d chars), optional heading' % MAX_TEXT)
        elif b.get('type') == 'link':
            if b.get('ref_kind') not in LINK_KINDS or type(b.get('ref_id')) is not str or not b['ref_id'] or type(b.get('label', '')) is not str or len(b.get('label', '')) > 200:
                raise ServiceError('VALIDATION', 'link block: ref_kind in %s, ref_id, optional label' % (LINK_KINDS,))
        else:
            raise ServiceError('VALIDATION', "block type: 'text' or 'link' (no executable blocks)")
        if set(b) - {'id', 'type', 'text', 'heading', 'ref_kind', 'ref_id', 'label'}:
            raise ServiceError('VALIDATION', 'unknown block field')


class Notebooks:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def _snapshot(self, db, workspace, kind, ref_id):
        """The referenced object's current commitment, or a NOT_FOUND refusal (links may only point inside the workspace)."""
        q = {'job': ('SELECT state, evidence_root AS c, kind FROM jobs WHERE id=? AND workspace=?', 'evidence_root'),
             'artifact': ('SELECT kind AS state, sha256_plaintext AS c, kind FROM artifacts WHERE id=? AND workspace=? AND deleted_at IS NULL', 'sha256_plaintext'),
             'verification': ('SELECT state, result_commitment AS c, class AS kind FROM verification_jobs WHERE id=? AND workspace=?', 'result_commitment'),
             'knowledge_answer': ('SELECT status AS state, citations_json AS c, mode AS kind FROM knowledge_answers WHERE id=? AND workspace=?', 'citations_sha256'),
             'calibration_model': ('SELECT state, job_id AS c, kind FROM calibration_models WHERE id=? AND workspace=?', 'job_id'),
             'calibration_dataset': ('SELECT kind AS state, digest AS c, kind FROM calibration_datasets WHERE id=? AND workspace=?', 'digest'),
             'evaluation_run': ('SELECT state, results_json AS c, suite_id AS kind FROM evaluation_runs WHERE id=? AND workspace=?', 'results_sha256'),
             'knowledge_document_version': ('SELECT CASE WHEN d.revoked_at IS NULL THEN \'active\' ELSE \'revoked\' END AS state, v.text_sha256 AS c, d.name AS kind FROM knowledge_versions v JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=? AND v.workspace=?', 'text_sha256')}[kind]
        r = db.execute(q[0], (ref_id, workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', {'link': kind, 'ref_id': ref_id})
        c = r['c']
        if q[1].endswith('_sha256') and c is not None and not (len(c) == 64 and all(ch in '0123456789abcdef' for ch in c)):
            c = hashlib.sha256(c.encode()).hexdigest()
        return {'commitment_field': q[1], 'commitment': c, 'state': r['state'], 'kind': r['kind']}

    def create(self, db, principal, name, blocks, note=''):
        principal.require('knowledge:write')
        if type(name) is not str or not 1 <= len(name) <= MAX_NAME:
            raise ServiceError('VALIDATION', 'name')
        nid = 'nb_' + secrets.token_hex(6)
        db.execute('INSERT INTO notebooks (id, workspace, name, owner_id, created_at) VALUES (?,?,?,?,?)', (nid, principal.workspace, name, principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'knowledge.collection', 'notebook', nid, {'name': name})
        return self.add_version(db, principal, nid, blocks, note)

    def _nb(self, db, principal, nid):
        r = db.execute('SELECT * FROM notebooks WHERE id=? AND workspace=?', (nid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'notebook')
        return r

    def add_version(self, db, principal, nid, blocks, note=''):
        principal.require('knowledge:write')
        nb = self._nb(db, principal, nid)
        validate_blocks(blocks)
        if type(note) is not str or len(note) > 500:
            raise ServiceError('VALIDATION', 'note')
        links = {}
        for b in blocks:
            if b['type'] == 'link':
                links[b['id']] = self._snapshot(db, principal.workspace, b['ref_kind'], b['ref_id'])
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM notebook_versions WHERE notebook_id=?', (nid,)).fetchone()[0]
        canon = merkle.canonical(blocks)
        digest = hashlib.sha256(canon).hexdigest()
        vid = 'nv_' + secrets.token_hex(6)
        db.execute('INSERT INTO notebook_versions (id, notebook_id, version, blocks_json, links_json, digest, note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                   (vid, nid, version, canon.decode(), json.dumps(links), digest, note, principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'knowledge.document', 'notebook', nid, {'version': version, 'digest': digest, 'blocks': len(blocks), 'links': len(links)})
        return self.view(db, principal, nid, version)

    def _version(self, db, nid, version):
        r = db.execute('SELECT * FROM notebook_versions WHERE notebook_id=? AND version=?', (nid, version)).fetchone() if version else db.execute('SELECT * FROM notebook_versions WHERE notebook_id=? ORDER BY version DESC LIMIT 1', (nid,)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'notebook version')
        return r

    def view(self, db, principal, nid, version=None, check_links=True):
        principal.require('knowledge:read')
        nb = self._nb(db, principal, nid); v = self._version(db, nid, version)
        blocks, links = json.loads(v['blocks_json']), json.loads(v['links_json'])
        drift = {}
        if check_links:
            for bid, snap in links.items():
                b = next(x for x in blocks if x['id'] == bid)
                try:
                    cur = self._snapshot(db, principal.workspace, b['ref_kind'], b['ref_id'])
                    drift[bid] = 'unchanged' if cur['commitment'] == snap['commitment'] else 'changed'
                except ServiceError:
                    drift[bid] = 'missing'
        versions = [dict(r) for r in db.execute('SELECT version, digest, note, created_by, created_at FROM notebook_versions WHERE notebook_id=? ORDER BY version', (nid,)).fetchall()]
        return {'id': nid, 'name': nb['name'], 'owner_id': nb['owner_id'], 'version': v['version'], 'version_id': v['id'], 'digest': v['digest'], 'note': v['note'], 'blocks': blocks, 'links': links, 'link_drift': drift,
                'versions': versions, 'created_at': v['created_at'], 'execution': 'none: blocks are data; links are checked references with snapshotted commitments'}

    def list(self, db, principal):
        principal.require('knowledge:read')
        out = []
        for r in db.execute('SELECT n.*, (SELECT MAX(version) FROM notebook_versions WHERE notebook_id=n.id) AS latest FROM notebooks n WHERE workspace=? ORDER BY created_at DESC', (principal.workspace,)).fetchall():
            out.append({'id': r['id'], 'name': r['name'], 'owner_id': r['owner_id'], 'latest_version': r['latest'], 'created_at': r['created_at']})
        return out

    def compare(self, db, principal, nid, va, vb):
        principal.require('knowledge:read')
        self._nb(db, principal, nid)
        a, b = self._version(db, nid, va), self._version(db, nid, vb)
        ba = {x['id']: x for x in json.loads(a['blocks_json'])}; bb = {x['id']: x for x in json.loads(b['blocks_json'])}
        la, lb = json.loads(a['links_json']), json.loads(b['links_json'])
        changed = [i for i in ba if i in bb and merkle.canonical(ba[i]) != merkle.canonical(bb[i])]
        relinked = [i for i in la if i in lb and la[i]['commitment'] != lb[i]['commitment']]
        return {'notebook_id': nid, 'a': {'version': a['version'], 'digest': a['digest']}, 'b': {'version': b['version'], 'digest': b['digest']}, 'added': sorted(set(bb) - set(ba)), 'removed': sorted(set(ba) - set(bb)),
                'changed': sorted(changed), 'reordered': [i for i in ba if i in bb] != [i for i in bb if i in ba], 'links_pointing_at_changed_objects': sorted(relinked), 'identical': a['digest'] == b['digest']}

    def export(self, db, principal, nid, version=None):
        """Authorized export: text blocks verbatim, links as identifiers + snapshotted commitments; no payloads."""
        principal.require('artifact:export')
        v = self.view(db, principal, nid, version)
        bundle = {'schema': 'metacoin-notebook-export/v1', 'notebook_id': nid, 'name': v['name'], 'version': v['version'], 'digest': v['digest'], 'blocks': v['blocks'], 'links': v['links'], 'exported_at': now(), 'exported_by': principal.id,
                  'contents': 'author text and object references with commitments only; no artifact payloads, documents, vectors, credentials or model outputs'}
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'notebook', nid, {'version': v['version'], 'digest': v['digest']})
        return bundle
