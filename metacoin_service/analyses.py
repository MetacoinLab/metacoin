"""Group E: structured scientific analysis sessions on top of the notebook foundation.

An analysis is a notebook whose blocks are typed (source_note, dataset_ref, assumption_table, operation_draft, run_result,
comparison, verification, conclusion; plain text blocks stay allowed). Executable values live in typed fields; text is data
and nothing in an analysis executes. Every revision is an immutable notebook version; revising requires the expected current
version (optimistic concurrency), a frozen revision is the only thing a report or a signed projection may reference, and a
revision that changes an assumption or a source marks the blocks that depend on it (declared `depends_on` plus reference
drift) stale without touching the earlier revision.

Dependencies are two graphs joined together: the block graph declared by the author (`depends_on`, references) and the
lineage edges the services record while executing (contracts, jobs, artifacts, versions, mappings, model revisions,
verification policies, campaigns, workflow runs). Impact analysis walks both, bounded, reports directly and transitively
affected objects, unaffected objects and objects whose dependencies are unknown (no declared or recorded dependency is not
proof of independence), and flags cycles.

Reports are built deterministically from validated structured data (source facts, declared assumptions, computed findings,
verification scope, unresolved limitations); an optional local-model interpretation is a separately labelled section whose
numbers are checked against the structured values. Projections are explicit allowlists over blocks and fields, previewed
byte-exactly, signed with the service key, and verified with an honest evidence-scope statement."""
import hashlib
import html as html_mod
import json
import re
import secrets

from experiments.private_receipts import receipt as merkle
from . import crypto, history
from .datasets import add_edge
from .db import now
from .errors import ServiceError

BLOCK_SCHEMA = 'metacoin-analysis-block/v1'
REPORT_SCHEMA = 'metacoin-analysis-report/v1'
PROJECTION_SCHEMA = 'metacoin-report-projection/v1'
BLOCK_TYPES = ('source_note', 'dataset_ref', 'assumption_table', 'operation_draft', 'run_result', 'comparison', 'verification', 'conclusion')
REF_KIND_OF = {'source_note': ('knowledge_document_version', 'document_import'), 'dataset_ref': ('dataset_version', 'dataset_mapping'), 'operation_draft': ('workflow_definition',), 'run_result': ('job',),
               'comparison': ('campaign', 'workflow_run', None), 'verification': ('verification',), 'assumption_table': (None,), 'conclusion': (None,)}
ASSUMPTION_SOURCES = ('user_edit', 'reviewed_table_correction', 'model_suggestion', 'measured_update', 'document')
REQUIRES = {'run_result': 'regeneration', 'comparison': 'regeneration', 'verification': 're-verification', 'conclusion': 're-review', 'operation_draft': 'revision', 'dataset_ref': 'remapping', 'source_note': 'review of the source', 'assumption_table': 'review', 'text': 'review'}
LIMITS = {'max_rows': 64, 'max_claims': 32, 'max_fields': 32, 'quote_chars': 1000, 'impact_depth': 8, 'impact_edges': 2000, 'report_chars': 400_000, 'model_prose_tokens': 160}
STALE_REASONS = ('reference_changed', 'reference_missing', 'dependency_changed', 'dependency_stale', 'source_revoked')


def validate_block(b, ids):
    """Typed block: schema, declared dependencies, one checked reference where the type requires it, executable values only
    in typed fields. `author` and `schema` are set by the server on write."""
    t = b.get('type')
    allowed = {'id', 'type', 'schema', 'author', 'depends_on', 'ref_kind', 'ref_id', 'label', 'text', 'heading'}
    typed = {'source_note': {'quote', 'page_number'}, 'dataset_ref': {'role'}, 'assumption_table': {'rows'}, 'operation_draft': {'kind', 'inputs_digest'}, 'run_result': {'fields'},
             'comparison': {'against'}, 'verification': set(), 'conclusion': {'claims'}}[t]
    if set(b) - allowed - typed:
        raise ServiceError('VALIDATION', {'code': 'unknown_block_field', 'block': b['id'], 'fields': sorted(set(b) - allowed - typed)})
    if b.get('schema', BLOCK_SCHEMA) != BLOCK_SCHEMA:
        raise ServiceError('VALIDATION', {'code': 'block_schema', 'expected': BLOCK_SCHEMA})
    deps = b.get('depends_on', [])
    if type(deps) is not list or len(deps) > 32 or not all(type(d) is str and d != b['id'] for d in deps):
        raise ServiceError('VALIDATION', {'code': 'depends_on', 'block': b['id'], 'expected': 'list of other block ids'})
    for k in ('text', 'heading', 'label'):
        if k in b and (type(b[k]) is not str or len(b[k]) > 20000):
            raise ServiceError('VALIDATION', {'code': 'text_field', 'block': b['id'], 'field': k})
    kinds = REF_KIND_OF[t]
    if b.get('ref_kind') is not None or None not in kinds:
        if b.get('ref_kind') not in [k for k in kinds if k] or type(b.get('ref_id')) is not str or not b['ref_id']:
            raise ServiceError('VALIDATION', {'code': 'block_reference', 'block': b['id'], 'type': t, 'ref_kinds': [k for k in kinds if k]})
    if t == 'source_note':
        if type(b.get('quote', '')) is not str or len(b.get('quote', '')) > LIMITS['quote_chars'] or (b.get('page_number') is not None and (type(b['page_number']) is not int or b['page_number'] < 1)):
            raise ServiceError('VALIDATION', {'code': 'source_note', 'block': b['id'], 'expected': 'quote ≤ %d chars, page_number ≥ 1' % LIMITS['quote_chars']})
    elif t == 'dataset_ref':
        if b.get('role', 'input') not in ('input', 'calibration', 'reference'):
            raise ServiceError('VALIDATION', {'code': 'dataset_role', 'block': b['id']})
    elif t == 'assumption_table':
        rows = b.get('rows')
        if type(rows) is not list or not 1 <= len(rows) <= LIMITS['max_rows']:
            raise ServiceError('VALIDATION', {'code': 'assumption_rows', 'block': b['id'], 'max': LIMITS['max_rows']})
        names = set()
        for r in rows:
            if type(r) is not dict or set(r) - {'name', 'value', 'unit', 'source', 'note'} or type(r.get('name')) is not str or not 1 <= len(r['name']) <= 64 or r['name'] in names \
                    or isinstance(r.get('value'), bool) or type(r.get('value')) not in (int, str) or (type(r['value']) is str and len(r['value']) > 200) or type(r.get('unit', '')) is not str or len(r.get('unit', '')) > 16 \
                    or r.get('source', 'user_edit') not in ASSUMPTION_SOURCES or type(r.get('note', '')) is not str or len(r.get('note', '')) > 300:
                raise ServiceError('VALIDATION', {'code': 'assumption_row', 'block': b['id'], 'expected': {'name': 'str', 'value': 'int|str', 'unit': 'str', 'source': list(ASSUMPTION_SOURCES)}})
            names.add(r['name'])
    elif t == 'operation_draft':
        if type(b.get('kind', '')) is not str or (b.get('inputs_digest') is not None and not (type(b['inputs_digest']) is str and len(b['inputs_digest']) == 64)):
            raise ServiceError('VALIDATION', {'code': 'operation_draft', 'block': b['id']})
    elif t == 'run_result':
        f = b.get('fields', [])
        if type(f) is not list or len(f) > LIMITS['max_fields'] or not all(type(x) is str and 1 <= len(x) <= 64 for x in f):
            raise ServiceError('VALIDATION', {'code': 'run_fields', 'block': b['id']})
    elif t == 'comparison':
        ag = b.get('against', [])
        if type(ag) is not list or not all(type(x) is str for x in ag) or len(ag) > 8 or (b.get('ref_kind') is None and len(ag) < 2):
            raise ServiceError('VALIDATION', {'code': 'comparison', 'block': b['id'], 'expected': 'a campaign/run reference or at least two run_result block ids in against'})
    elif t == 'conclusion':
        claims = b.get('claims', [])
        if type(claims) is not list or len(claims) > LIMITS['max_claims']:
            raise ServiceError('VALIDATION', {'code': 'claims', 'block': b['id'], 'max': LIMITS['max_claims']})
        for c in claims:
            if type(c) is not dict or set(c) - {'text', 'values', 'refs'} or type(c.get('text')) is not str or not 1 <= len(c['text']) <= 2000 or type(c.get('values', {})) is not dict or len(c.get('values', {})) > 16 \
                    or not all(type(k) is str and '.' in k and (type(v) in (int, str) or v is None) and not isinstance(v, bool) for k, v in c.get('values', {}).items()) or type(c.get('refs', [])) is not list:
                raise ServiceError('VALIDATION', {'code': 'claim', 'block': b['id'], 'expected': {'text': 'str', 'values': {'<block>.<field>': 'int|str'}, 'refs': ['block ids']}})
    return b


def _digest(obj):
    return hashlib.sha256(merkle.canonical(obj)).hexdigest()


class Analyses:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- sessions and revisions ---------------------------------------------------------------------------------------
    def _row(self, db, principal, aid):
        r = db.execute('SELECT * FROM analyses WHERE id=? AND workspace=?', (aid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'analysis')
        return r

    def _stamp(self, principal, blocks):
        out = []
        for b in blocks:
            b = dict(b)
            if b.get('type') in BLOCK_TYPES:
                b['schema'] = BLOCK_SCHEMA; b.setdefault('author', principal.id); b.setdefault('depends_on', [])
            out.append(b)
        # declared dependencies must name existing blocks and must not form a cycle
        ids = {b['id'] for b in out}
        for b in out:
            for d in b.get('depends_on', []):
                if d not in ids:
                    raise ServiceError('VALIDATION', {'code': 'unknown_dependency', 'block': b['id'], 'depends_on': d})
        cycles = self._cycles({b['id']: b.get('depends_on', []) for b in out})
        if cycles:
            raise ServiceError('VALIDATION', {'code': 'dependency_cycle', 'cycles': cycles})
        return out

    @staticmethod
    def _cycles(graph):
        color, cycles = {}, []
        def visit(n, path):
            color[n] = 1; path.append(n)
            for m in graph.get(n, []):
                if color.get(m) == 1:
                    cycles.append(path[path.index(m):] + [m])
                elif color.get(m) is None:
                    visit(m, path)
            path.pop(); color[n] = 2
        for n in graph:
            if color.get(n) is None:
                visit(n, [])
        return cycles[:8]

    def create(self, db, principal, name, blocks=None, from_document=None, from_workflow=None, note=''):
        principal.require('knowledge:write')
        blocks = list(blocks or [])
        if from_document:
            imp = db.execute('SELECT * FROM document_imports WHERE id=? AND workspace=?', (from_document, principal.workspace)).fetchone()
            if imp is not None and imp['version_id']:
                blocks.insert(0, {'id': 'source', 'type': 'source_note', 'ref_kind': 'knowledge_document_version', 'ref_id': imp['version_id'], 'label': imp['name'], 'text': 'Imported document (revision bound).'})
            elif db.execute('SELECT 1 FROM knowledge_versions WHERE id=? AND workspace=?', (from_document, principal.workspace)).fetchone():
                blocks.insert(0, {'id': 'source', 'type': 'source_note', 'ref_kind': 'knowledge_document_version', 'ref_id': from_document, 'text': 'Source document (revision bound).'})
            else:
                raise ServiceError('NOT_FOUND', {'code': 'from_document', 'detail': 'a published document import or a knowledge version id'})
        if from_workflow:
            wd = self.svc.workflows.get_definition(db, principal, from_workflow)
            blocks.append({'id': 'operation', 'type': 'operation_draft', 'ref_kind': 'workflow_definition', 'ref_id': from_workflow, 'label': wd['name'], 'kind': 'workflow', 'text': 'Operation draft bound to the workflow definition digest.'})
        if not blocks:
            blocks = [{'id': 'aim', 'type': 'text', 'heading': 'Aim', 'text': 'New analysis.'}]
        nb = self.svc.notebooks.create(db, principal, name, self._stamp(principal, blocks), note)
        aid = 'an_' + secrets.token_hex(6)
        db.execute('INSERT INTO analyses (id, workspace, name, owner_id, notebook_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)', (aid, principal.workspace, name, principal.id, nb['id'], now(), now()))
        db.execute('INSERT INTO analysis_versions (analysis_id, version, stale_json, changed_json, created_by, created_at) VALUES (?,?,?,?,?,?)', (aid, 1, '{}', '[]', principal.id, now()))
        for b in nb['blocks']:
            if b.get('ref_kind'):
                add_edge(db, principal.workspace, b['ref_kind'], b['ref_id'], 'analysis', aid, 'used_input')
        history.record(db, principal.workspace, principal.id, 'analysis.created', 'analysis', aid, {'name': name, 'notebook_id': nb['id'], 'blocks': len(nb['blocks'])})
        return self.view(db, principal, aid)

    def revise(self, db, principal, aid, blocks, expected_version, note=''):
        """A new immutable revision. Refused unless `expected_version` is the current version (409 version_conflict). Blocks
        whose content changed are recorded; blocks depending (transitively) on a changed block, or whose reference drifted,
        are marked stale in the new revision unless they changed themselves."""
        principal.require('knowledge:write')
        a = self._row(db, principal, aid)
        cur = self.svc.notebooks.view(db, principal, a['notebook_id'], check_links=False)
        if type(expected_version) is not int or expected_version != cur['version']:
            raise ServiceError('CONFLICT', {'code': 'version_conflict', 'expected_version': expected_version, 'current_version': cur['version'], 'detail': 'reload the current revision and re-apply your change'})
        stamped = self._stamp(principal, blocks)
        old = {b['id']: b for b in cur['blocks']}
        changed = sorted(b['id'] for b in stamped if b['id'] not in old or merkle.canonical({k: v for k, v in old[b['id']].items() if k != 'author'}) != merkle.canonical({k: v for k, v in b.items() if k != 'author'}))
        nb = self.svc.notebooks.add_version(db, principal, a['notebook_id'], stamped, note)
        stale = self._compute_stale(nb, changed, set(changed))
        db.execute('INSERT INTO analysis_versions (analysis_id, version, stale_json, changed_json, created_by, created_at) VALUES (?,?,?,?,?,?)', (aid, nb['version'], json.dumps(stale), json.dumps(changed), principal.id, now()))
        db.execute('UPDATE analyses SET updated_at=? WHERE id=?', (now(), aid))
        for b in nb['blocks']:
            if b.get('ref_kind') and b['id'] in changed:
                add_edge(db, principal.workspace, b['ref_kind'], b['ref_id'], 'analysis', aid, 'used_input')
        history.record(db, principal.workspace, principal.id, 'analysis.revised', 'analysis', aid, {'version': nb['version'], 'changed': changed, 'stale': sorted(stale)})
        return self.view(db, principal, aid)

    def _compute_stale(self, nb, changed, exclude):
        """Blocks depending transitively on a changed block (declared depends_on), plus reference drift, minus blocks updated now."""
        deps = {b['id']: b.get('depends_on', []) for b in nb['blocks']}
        rev = {}
        for b, ds in deps.items():
            for d in ds:
                rev.setdefault(d, []).append(b)
        stale = {}
        frontier = list(changed)
        seen = set(changed)
        while frontier:
            n = frontier.pop()
            for m in rev.get(n, []):
                if m not in seen:
                    seen.add(m); frontier.append(m)
                    if m not in exclude:
                        stale[m] = {'reason': 'dependency_changed' if n in changed else 'dependency_stale', 'via': n}
        for bid, d in (nb.get('link_drift') or {}).items():
            if d == 'changed' and bid not in exclude:
                stale[bid] = {'reason': 'reference_changed'}
            elif d == 'missing' and bid not in exclude:
                stale[bid] = {'reason': 'reference_missing'}
        return stale

    def freeze(self, db, principal, aid, version, reason=''):
        principal.require('knowledge:write')
        a = self._row(db, principal, aid)
        row = db.execute('SELECT * FROM analysis_versions WHERE analysis_id=? AND version=?', (aid, version)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'analysis version')
        if row['frozen_at']:
            return self.view(db, principal, aid, version)
        db.execute('UPDATE analysis_versions SET frozen_at=?, frozen_by=?, freeze_reason=? WHERE analysis_id=? AND version=?', (now(), principal.id, (reason or '')[:300], aid, version))
        history.record(db, principal.workspace, principal.id, 'analysis.frozen', 'analysis', aid, {'version': version, 'reason': (reason or '')[:100]})
        return self.view(db, principal, aid, version)

    def view(self, db, principal, aid, version=None):
        principal.require('knowledge:read')
        a = self._row(db, principal, aid)
        nb = self.svc.notebooks.view(db, principal, a['notebook_id'], version)
        av = db.execute('SELECT * FROM analysis_versions WHERE analysis_id=? AND version=?', (aid, nb['version'])).fetchone()
        stale = json.loads(av['stale_json']) if av else {}
        # live staleness: reference drift since the revision was written, and revoked sources
        for bid, d in nb['link_drift'].items():
            if d == 'changed' and bid not in stale:
                stale[bid] = {'reason': 'reference_changed'}
            elif d == 'missing' and bid not in stale:
                stale[bid] = {'reason': 'reference_missing'}
        for b in nb['blocks']:
            if b.get('ref_kind') == 'knowledge_document_version' and b['id'] not in stale:
                try:
                    live = self.svc.notebooks._snapshot(db, principal.workspace, b['ref_kind'], b['ref_id'])
                except ServiceError:
                    live = None
                if live and live.get('state') == 'revoked':
                    stale[b['id']] = {'reason': 'source_revoked'}
        # propagate live staleness through declared dependencies
        stale = dict(stale)
        deps = {b['id']: b.get('depends_on', []) for b in nb['blocks']}
        changed_any = True
        while changed_any:
            changed_any = False
            for bid, ds in deps.items():
                if bid not in stale and any(d in stale for d in ds):
                    stale[bid] = {'reason': 'dependency_stale', 'via': next(d for d in ds if d in stale)}; changed_any = True
        freezes = {r['version']: {'frozen_at': r['frozen_at'], 'frozen_by': r['frozen_by'], 'reason': r['freeze_reason']} for r in db.execute('SELECT version, frozen_at, frozen_by, freeze_reason FROM analysis_versions WHERE analysis_id=? AND frozen_at IS NOT NULL', (aid,))}
        reports = [dict(r) for r in db.execute('SELECT id, version, mode, digest, created_by, created_at FROM analysis_reports WHERE analysis_id=? ORDER BY created_at', (aid,)).fetchall()]
        blocks = []
        for b in nb['blocks']:
            item = dict(b)
            item['status'] = 'stale' if b['id'] in stale else 'current'
            if b['id'] in stale:
                item['stale'] = stale[b['id']]; item['requires'] = REQUIRES.get(b['type'], 'review')
            if b['id'] in nb['links']:
                item['reference'] = nb['links'][b['id']]; item['reference_drift'] = nb['link_drift'].get(b['id'])
            blocks.append(item)
        return {'id': aid, 'name': a['name'], 'owner_id': a['owner_id'], 'notebook_id': a['notebook_id'], 'version': nb['version'], 'digest': nb['digest'], 'note': nb['note'], 'blocks': blocks,
                'stale': stale, 'changed_in_this_revision': json.loads(av['changed_json']) if av else [], 'frozen': nb['version'] in freezes, 'freezes': freezes,
                'versions': nb['versions'], 'reports': reports, 'created_at': a['created_at'], 'updated_at': a['updated_at'],
                'execution': 'none: blocks are data; typed fields hold values; references are checked and snapshotted', 'concurrency': 'revise with expected_version = ' + str(nb['version'])}

    def list(self, db, principal):
        principal.require('knowledge:read')
        out = []
        for r in db.execute('SELECT a.*, (SELECT MAX(version) FROM analysis_versions WHERE analysis_id=a.id) AS latest, (SELECT COUNT(*) FROM analysis_versions WHERE analysis_id=a.id AND frozen_at IS NOT NULL) AS frozen FROM analyses a WHERE workspace=? ORDER BY updated_at DESC', (principal.workspace,)).fetchall():
            out.append({'id': r['id'], 'name': r['name'], 'owner_id': r['owner_id'], 'latest_version': r['latest'], 'frozen_versions': r['frozen'], 'updated_at': r['updated_at']})
        return out

    # ---- dependency graph and impact -----------------------------------------------------------------------------------
    def _forward(self, db, workspace, roots):
        """Bounded forward walk over recorded lineage edges (upstream → downstream). Returns affected objects with depth and
        the edges used; a cycle is reported when a root is reached again."""
        seen, out, edges, cycle = {}, [], [], False
        frontier = [(t, i, 0) for t, i in roots]
        for t, i, _ in frontier:
            seen[(t, i)] = 0
        while frontier and len(edges) < LIMITS['impact_edges']:
            t, i, d = frontier.pop(0)
            if d >= LIMITS['impact_depth']:
                continue
            for r in db.execute('SELECT from_type, from_id, to_type, to_id, relation FROM lineage_edges WHERE workspace=? AND from_type=? AND from_id=? LIMIT 500', (workspace, t, i)).fetchall():
                key = (r['to_type'], r['to_id'])
                edges.append({'from': [t, i], 'to': list(key), 'relation': r['relation']})
                if key in {(a, b) for a, b in roots}:
                    cycle = True
                if key not in seen:
                    seen[key] = d + 1; out.append({'type': key[0], 'id': key[1], 'depth': d + 1, 'via': r['relation']}); frontier.append((key[0], key[1], d + 1))
        return out, edges, cycle, len(edges) >= LIMITS['impact_edges']

    def impact(self, db, principal, aid, changed, version=None):
        """`changed`: {'block': id} or {'object_type', 'object_id'}. Classifies blocks and recorded objects into directly
        affected, transitively affected, unaffected, unknown (no declared or recorded dependency), with what each requires."""
        v = self.view(db, principal, aid, version)
        blocks = {b['id']: b for b in v['blocks']}
        if type(changed) is not dict:
            raise ServiceError('VALIDATION', 'changed: {block} or {object_type, object_id}')
        roots_blocks, roots_objects = set(), []
        if 'block' in changed:
            if changed['block'] not in blocks:
                raise ServiceError('NOT_FOUND', 'block')
            roots_blocks.add(changed['block'])
            b = blocks[changed['block']]
            if b.get('ref_kind'):
                roots_objects.append((b['ref_kind'], b['ref_id']))
        elif 'object_type' in changed and 'object_id' in changed:
            roots_objects.append((changed['object_type'], changed['object_id']))
            for bid, b in blocks.items():
                if b.get('ref_kind') == changed['object_type'] and b.get('ref_id') == changed['object_id']:
                    roots_blocks.add(bid)
        else:
            raise ServiceError('VALIDATION', 'changed: {block} or {object_type, object_id}')
        affected_objects, edges, cycle, truncated = self._forward(db, principal.workspace, roots_objects) if roots_objects else ([], [], False, False)
        affected_keys = {(o['type'], o['id']): o for o in affected_objects}
        # block graph: declared depends_on (reverse) + references to affected objects
        rev = {}
        for bid, b in blocks.items():
            for d in b.get('depends_on', []):
                rev.setdefault(d, []).append(bid)
        direct, transitive = {}, {}
        for bid, b in blocks.items():
            if bid in roots_blocks:
                continue
            if any(d in roots_blocks for d in b.get('depends_on', [])):
                direct[bid] = {'via': 'depends_on', 'on': [d for d in b.get('depends_on', []) if d in roots_blocks]}
            elif b.get('ref_kind') and (b['ref_kind'], b['ref_id']) in affected_keys:
                o = affected_keys[(b['ref_kind'], b['ref_id'])]
                (direct if o['depth'] == 1 else transitive)[bid] = {'via': 'lineage', 'depth': o['depth'], 'relation': o['via']}
        frontier = list(direct)
        seen = set(roots_blocks) | set(direct) | set(transitive)
        while frontier:
            n = frontier.pop()
            for m in rev.get(n, []):
                if m not in seen:
                    seen.add(m); transitive[m] = {'via': 'depends_on', 'on': [n]}; frontier.append(m)
        cycles = self._cycles({bid: b.get('depends_on', []) for bid, b in blocks.items()})
        unaffected, unknown = [], []
        for bid, b in blocks.items():
            if bid in roots_blocks or bid in direct or bid in transitive:
                continue
            has_declared = bool(b.get('depends_on')) or bool(b.get('ref_kind'))
            recorded = bool(b.get('ref_kind')) and db.execute('SELECT 1 FROM lineage_edges WHERE workspace=? AND ((to_type=? AND to_id=?) OR (from_type=? AND from_id=?)) LIMIT 1', (principal.workspace, b['ref_kind'], b['ref_id'], b['ref_kind'], b['ref_id'])).fetchone() is not None
            if b['type'] == 'text' or has_declared and (recorded or not b.get('ref_kind')):
                unaffected.append(bid)
            else:
                unknown.append({'block': bid, 'reason': 'no declared dependencies and no recorded lineage for its reference; independence is not established'})
        def req(bid):
            return REQUIRES.get(blocks[bid]['type'], 'review')
        return {'analysis_id': aid, 'version': v['version'], 'changed': changed, 'root_blocks': sorted(roots_blocks),
                'directly_affected': [{'block': b, 'type': blocks[b]['type'], 'requires': req(b), **direct[b]} for b in sorted(direct)],
                'transitively_affected': [{'block': b, 'type': blocks[b]['type'], 'requires': req(b), **transitive[b]} for b in sorted(transitive)],
                'unaffected': sorted(unaffected), 'unknown': unknown, 'affected_objects': affected_objects, 'lineage_edges_walked': len(edges), 'truncated': truncated,
                'cycles': cycles + (['lineage cycle reaching the changed object'] if cycle else []),
                'note': 'prior results keep their historical validity under old inputs; affected blocks are not current answers for the changed analysis. A missing dependency declaration is not proof that an object is unaffected.'}

    # ---- selective regeneration -----------------------------------------------------------------------------------------
    def regeneration_plan(self, db, principal, aid, run_id, changes, budget_ceiling=None):
        """From a finished workflow run of this analysis and a set of node input changes, plan a new run that reruns only the
        changed nodes and everything downstream of them, and reuses committed results of the other service nodes when the
        exact cache contract allows it (identical inputs, identical verifier digest, evidence present, readable now)."""
        principal.require('job:read_private')
        from . import reuse as reuse_mod, workflows as wf_mod
        run = self.svc.workflows._run(db, run_id, principal.workspace)
        drow = self.svc.workflows.get_definition(db, principal, run['definition_id'])
        definition = merkle.parse(drow['definition_json'])
        nodes = {n['id']: n for n in definition['nodes']}
        if type(changes) is not dict or not changes or not all(k in nodes and type(v) is dict for k, v in changes.items()):
            raise ServiceError('VALIDATION', {'code': 'changes', 'expected': '{node_id: {input field: new value}}', 'nodes': sorted(nodes)})
        downstream = {}
        for n in definition['nodes']:
            for d in n.get('depends_on', []) or []:
                downstream.setdefault(d['node'] if isinstance(d, dict) else d, []).append(n['id'])
            if n.get('input'):
                downstream.setdefault(n['input'], []).append(n['id'])
        affected = set(changes)
        frontier = list(changes)
        while frontier:
            x = frontier.pop()
            for y in downstream.get(x, []):
                if y not in affected:
                    affected.add(y); frontier.append(y)
        state = {r['node_id']: dict(r) for r in db.execute('SELECT * FROM workflow_nodes WHERE run_id=?', (run_id,)).fetchall()}
        new_nodes, plan = [], []
        for n in definition['nodes']:
            nd = json.loads(merkle.canonical(n))
            entry = {'node': n['id'], 'type': n['type']}
            if n['type'] not in wf_mod.SERVICE_TYPES:
                entry.update(action='keep', reason='not a service node')
            elif n['id'] in changes:
                if 'inputs' not in nd:
                    raise ServiceError('VALIDATION', {'code': 'node_has_no_inline_inputs', 'node': n['id']})
                unknown = [k for k in changes[n['id']] if k not in nd['inputs']]
                if unknown:
                    raise ServiceError('VALIDATION', {'code': 'unknown_input_field', 'node': n['id'], 'fields': unknown})
                nd['inputs'] = dict(nd['inputs'], **changes[n['id']])
                from .contracts import validate_inputs
                validate_inputs(n['type'], nd['inputs'])
                entry.update(action='rerun', reason='inputs_changed', changed_fields=sorted(changes[n['id']]))
            elif n['id'] in affected:
                entry.update(action='rerun', reason='downstream_of_change')
            elif n['type'] in wf_mod.SERVICE_TYPES:
                st = state.get(n['id']) or {}
                job = db.execute('SELECT j.*, a.deleted_at AS evidence_deleted FROM jobs j LEFT JOIN artifacts a ON a.id=j.evidence_artifact_id WHERE j.id=?', (st.get('job_id'),)).fetchone() if st.get('job_id') else None
                if job is None or job['state'] != 'succeeded':
                    entry.update(action='rerun', reason='no_prior_success')
                elif job['evidence_deleted'] is not None:
                    entry.update(action='rerun', reason='evidence_deleted')
                else:
                    contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
                    vd = reuse_mod.verifier_digest_for(n['type'])
                    cache = db.execute('SELECT verifier_digest FROM result_cache WHERE workspace=? AND kind=? AND inputs_digest=?', (principal.workspace, n['type'], contract['inputs_digest'])).fetchone()
                    if cache is None:
                        entry.update(action='rerun', reason='not_in_reuse_index')
                    elif cache['verifier_digest'] != vd:
                        entry.update(action='rerun', reason='verifier_digest_changed', recorded=cache['verifier_digest'][:16], installed=vd[:16])
                    elif 'input' in nd and any(k in changes for k in [nd['input']]):
                        entry.update(action='rerun', reason='upstream_binding_changed')
                    else:
                        entry.update(action='reuse', reason='identical_inputs_and_verifier', prior_job=job['id'], evidence_root=job['evidence_root'], charge=0, charge_basis='reused results meter at zero; no access price is configured for internal reuse')
            new_nodes.append(nd); plan.append(entry)
        new_def = dict(definition, nodes=new_nodes, name=(definition['name'] + ' / regeneration')[:128])
        reruns = [p for p in plan if p['action'] == 'rerun']; reuses = [p for p in plan if p['action'] == 'reuse']
        est = wf_mod.estimate(new_def, self.settings.limits)
        return {'analysis_id': aid, 'source_run': run_id, 'definition_id': run['definition_id'], 'plan': plan, 'new_definition': new_def, 'reruns': [p['node'] for p in reruns], 'reuses': [p['node'] for p in reuses],
                'quote': {'service_nodes_to_execute': len(reruns), 'service_nodes_reused': len(reuses), 'estimate': est, 'budget_ceiling': budget_ceiling, 'charges': 'each executed node is admitted by its own contract quote and the run ceiling; reused nodes charge 0'},
                'access_recheck': 'reused private artifacts are re-checked for the requesting principal at materialization and export time', 'note': 'the original run and analysis revision are preserved; nothing dispatched yet'}

    def regenerate(self, db, principal, aid, run_id, changes, budget_ceiling=None, note=''):
        principal.require('job:submit')
        plan = self.regeneration_plan(db, principal, aid, run_id, changes, budget_ceiling)
        wid, digest, created = self.svc.workflows.create(db, principal, plan['new_definition'])
        add_edge(db, principal.workspace, 'workflow_definition', plan['definition_id'], 'workflow_definition', wid, 'derived_from')
        started = self.svc.workflows.start_run(db, principal, wid, bindings=None, budget_ceiling=budget_ceiling, reuse_nodes=plan['reuses'])
        add_edge(db, principal.workspace, 'workflow_run', run_id, 'workflow_run', started['run_id'], 'derived_from')
        # the analysis gets a new revision pointing at the regeneration run; the old run_result blocks stay as history
        a = self._row(db, principal, aid)
        cur = self.view(db, principal, aid)
        blocks = [{k: v for k, v in b.items() if k not in ('status', 'stale', 'requires', 'reference', 'reference_drift')} for b in cur['blocks']]
        blocks.append({'id': 'regen_' + started['run_id'][-6:], 'type': 'comparison', 'ref_kind': 'workflow_run', 'ref_id': started['run_id'], 'label': 'regeneration run', 'against': [],
                       'text': 'Regeneration after changing %s: rerun %s; reused %s.' % (', '.join(sorted(changes)), ', '.join(plan['reruns']) or 'none', ', '.join(plan['reuses']) or 'none')})
        view = self.revise(db, principal, aid, blocks, cur['version'], note or 'regeneration ' + started['run_id'])
        history.record(db, principal.workspace, principal.id, 'analysis.regenerated', 'analysis', aid, {'run_id': started['run_id'], 'source_run': run_id, 'reruns': plan['reruns'], 'reuses': plan['reuses']})
        return {'run_id': started['run_id'], 'definition_id': wid, 'plan': plan['plan'], 'reruns': plan['reruns'], 'reuses': plan['reuses'], 'analysis_version': view['version'], 'estimate': started['estimate']}

    # ---- evidence-linked reports -----------------------------------------------------------------------------------------
    def _job_facts(self, db, principal, job_id, fields):
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
        if job is None:
            return None
        contract = db.execute('SELECT contract_json, contract_digest FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        doc = json.loads(contract['contract_json']) if contract and contract['contract_json'] else {}
        summary = json.loads(job['summary_json'] or '{}') if principal.can('job:read_private') else {}
        ver = db.execute("SELECT id, class, state FROM verification_jobs WHERE target_job_id=? AND state='passed' ORDER BY finished_at DESC LIMIT 1", (job_id,)).fetchone()
        crun = db.execute('SELECT verification_json FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()
        cver = json.loads(crun['verification_json']) if crun and crun['verification_json'] else None
        vals = {f: summary.get(f) for f in fields} if fields else {k: summary[k] for k in sorted(summary)[:LIMITS['max_fields']] if type(summary[k]) in (int, str, float) or summary[k] is None}
        return {'job_id': job_id, 'kind': job['kind'], 'state': job['state'], 'outcome': job['outcome'], 'review_state': job['review_state'], 'reused_from': job['reused_from'] if 'reused_from' in job.keys() else None,
                'evidence_root': job['evidence_root'], 'contract_digest': contract['contract_digest'] if contract else None, 'model_id': doc.get('model_id'), 'verifier_id': doc.get('verifier_id'), 'verifier_digest': (doc.get('verifier_digest') or '')[:16] or None,
                'values': vals, 'private_values_visible': principal.can('job:read_private'), 'verification': ({'class': ver['class'], 'id': ver['id']} if ver else None), 'producer_check': ({'mode': cver.get('mode'), 'passed': cver.get('passed')} if cver else None)}

    def _verification_facts(self, db, principal, vid):
        r = db.execute('SELECT * FROM verification_jobs WHERE id=? AND workspace=?', (vid, principal.workspace)).fetchone()
        if r is None:
            return None
        res = json.loads(r['result_json']) if r['result_json'] else {}
        return {'verification_id': vid, 'target_job_id': r['target_job_id'], 'class': r['class'], 'state': r['state'], 'checked': res.get('checked'), 'total': res.get('total'), 'coverage': res.get('coverage'),
                'statement': (res.get('statement') or '')[:400], 'sampled': r['class'] == 'sampled_reference', 'result_commitment': r['result_commitment']}

    def _source_facts(self, db, principal, b):
        if b['ref_kind'] == 'knowledge_document_version':
            r = db.execute('SELECT v.version, v.text_sha256, v.parser_id, v.provenance, d.name, d.revoked_at FROM knowledge_versions v JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=? AND v.workspace=?', (b['ref_id'], principal.workspace)).fetchone()
            if r is None:
                return None
            return {'document': r['name'], 'version': r['version'], 'text_sha256': r['text_sha256'], 'parser_id': r['parser_id'], 'provenance': r['provenance'], 'revoked': r['revoked_at'] is not None, 'page_number': b.get('page_number'), 'quote': b.get('quote', '')}
        r = db.execute('SELECT name, state, content_sha256, page_count FROM document_imports WHERE id=? AND workspace=?', (b['ref_id'], principal.workspace)).fetchone()
        return None if r is None else {'document': r['name'], 'import_state': r['state'], 'content_sha256': r['content_sha256'], 'pages': r['page_count'], 'page_number': b.get('page_number'), 'quote': b.get('quote', '')}

    def _dataset_facts(self, db, principal, b):
        if b['ref_kind'] == 'dataset_version':
            r = db.execute('SELECT v.version, v.normalized_commitment, v.normalization_id, v.row_count, d.kind, d.name FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id WHERE v.id=? AND d.workspace=?', (b['ref_id'], principal.workspace)).fetchone()
            return None if r is None else {'dataset': r['name'], 'kind': r['kind'], 'version': r['version'], 'commitment': r['normalized_commitment'], 'normalization': r['normalization_id'], 'rows': r['row_count'], 'role': b.get('role', 'input')}
        r = db.execute('SELECT target, state, digest, dataset_version_id, confirmed_by FROM dataset_mappings WHERE id=? AND workspace=?', (b['ref_id'], principal.workspace)).fetchone()
        return None if r is None else {'mapping': b['ref_id'], 'target': r['target'], 'state': r['state'], 'digest': r['digest'], 'dataset_version_id': r['dataset_version_id'], 'confirmed': r['confirmed_by'] is not None, 'role': b.get('role', 'input')}

    def _comparison_facts(self, db, principal, b, run_facts):
        if b.get('ref_kind') == 'campaign':
            c = db.execute('SELECT name, kind, state, digest FROM sci_campaigns WHERE id=? AND workspace=?', (b['ref_id'], principal.workspace)).fetchone()
            if c is None:
                return None
            outcomes = {r['outcome']: r['n'] for r in db.execute("SELECT outcome, COUNT(*) AS n FROM sci_campaign_candidates WHERE campaign_id=? AND state='succeeded' GROUP BY outcome", (b['ref_id'],))}
            return {'campaign': b['ref_id'], 'name': c['name'], 'kind': c['kind'], 'state': c['state'], 'digest': c['digest'], 'outcomes': outcomes}
        if b.get('ref_kind') == 'workflow_run':
            r = self.svc.workflows.view(db, principal, b['ref_id'])
            return {'workflow_run': b['ref_id'], 'state': r['state'], 'nodes': {n['node_id']: {'state': n['state'], 'job_id': n.get('job_id'), 'reused_from': (db.execute('SELECT reused_from FROM jobs WHERE id=?', (n['job_id'],)).fetchone() or {'reused_from': None})['reused_from'] if n.get('job_id') else None} for n in r['nodes']}}
        rows = {}
        for bid in b.get('against', []):
            rows[bid] = run_facts.get(bid)
        return {'against': rows}

    def build_report(self, db, principal, aid, version, mode='deterministic', model_host=None):
        """Deterministic report from validated structured data; optional model prose as a labelled, number-checked section."""
        principal.require('knowledge:read')
        a = self._row(db, principal, aid)
        v = self.view(db, principal, aid, version)
        if not v['frozen']:
            raise ServiceError('CONFLICT', {'code': 'revision_not_frozen', 'version': v['version'], 'detail': 'freeze the revision first; reports bind to immutable revisions'})
        if mode not in ('deterministic', 'model'):
            raise ServiceError('VALIDATION', 'mode: deterministic | model')
        blocks = v['blocks']
        facts, flags, artifacts, methods = {}, [], [], {}
        run_facts = {}
        for b in blocks:
            t = b['type']
            if t == 'run_result':
                f = self._job_facts(db, principal, b['ref_id'], b.get('fields', []))
                run_facts[b['id']] = f
            elif t == 'verification':
                f = self._verification_facts(db, principal, b['ref_id'])
            elif t == 'source_note':
                f = self._source_facts(db, principal, b)
            elif t == 'dataset_ref':
                f = self._dataset_facts(db, principal, b)
            elif t == 'comparison':
                f = self._comparison_facts(db, principal, b, run_facts)
            elif t == 'operation_draft':
                f = {'kind': b.get('kind'), 'reference': b.get('ref_id'), 'digest': (v['blocks'][[x['id'] for x in v['blocks']].index(b['id'])].get('reference') or {}).get('commitment')}
            elif t == 'assumption_table':
                f = {'rows': b['rows']}
            elif t == 'conclusion':
                f = {'claims': b.get('claims', [])}
            else:
                f = None
            if f is None and t not in ('text',):
                flags.append({'block': b['id'], 'code': 'reference_unresolved', 'detail': 'the referenced object is not readable in this workspace'})
            facts[b['id']] = f
            if b.get('reference'):
                artifacts.append({'block': b['id'], 'type': t, 'ref_kind': b['ref_kind'], 'ref_id': b['ref_id'], 'commitment_field': b['reference']['commitment_field'], 'commitment': b['reference']['commitment'], 'drift': b.get('reference_drift')})
            if t == 'run_result' and f:
                methods.setdefault('models', set()).add(f['model_id'] or 'unknown'); methods.setdefault('verifiers', set()).add(f['verifier_id'] or 'unknown')
            if b['status'] == 'stale':
                flags.append({'block': b['id'], 'code': 'stale', 'detail': b['stale']})
        # claims are checked against structured values; contradictions and unsupported references are flagged, never rewritten
        claim_checks = []
        for b in blocks:
            if b['type'] != 'conclusion':
                continue
            for i, c in enumerate(b.get('claims', [])):
                for ref in c.get('refs', []):
                    if ref not in facts:
                        flags.append({'block': b['id'], 'code': 'unsupported_reference', 'claim': i, 'ref': ref}); claim_checks.append({'block': b['id'], 'claim': i, 'ref': ref, 'ok': False, 'reason': 'unknown block'})
                for key, expected in c.get('values', {}).items():
                    bid, _, field = key.partition('.')
                    src = facts.get(bid)
                    actual = None; known = False
                    if src and bid in run_facts and run_facts[bid]:
                        known = field in run_facts[bid]['values']; actual = run_facts[bid]['values'].get(field)
                    elif src and 'rows' in src:
                        for r in src['rows']:
                            if r['name'] == field:
                                known = True; actual = r['value']
                    elif src and isinstance(src, dict) and field in src:
                        known = True; actual = src[field]
                    ok = known and (actual == expected or (type(actual) in (int, float) and type(expected) is str and expected.strip() == str(actual)))
                    claim_checks.append({'block': b['id'], 'claim': i, 'key': key, 'expected': expected, 'actual': actual if known else None, 'ok': ok, 'reason': None if ok else ('value not found in structured data' if not known else 'contradicts structured result')})
                    if not ok:
                        flags.append({'block': b['id'], 'code': 'claim_contradicts_structured_result' if known else 'claim_value_unsupported', 'claim': i, 'key': key, 'expected': expected, 'actual': actual if known else None})
        interpretation = None
        if mode == 'model':
            interpretation = self._model_interpretation(db, principal, v, facts, run_facts, model_host)
            flags.extend(interpretation.get('flags', []))
        limitations = self._limitations(v, facts, run_facts, flags, interpretation)
        md = self._markdown(v, facts, run_facts, claim_checks, interpretation, limitations, methods)
        if len(md) > LIMITS['report_chars']:
            raise ServiceError('VALIDATION', 'report too large')
        html = self._html(v['name'], md)
        manifest = {'schema': REPORT_SCHEMA, 'analysis_id': aid, 'analysis_version': v['version'], 'analysis_digest': v['digest'], 'mode': mode, 'artifacts': artifacts, 'claim_checks': claim_checks,
                    'methods': {k: sorted(x) for k, x in methods.items()}, 'sections': ['source_facts', 'declared_assumptions', 'computed_findings', 'verification_scope'] + (['model_interpretation'] if interpretation else []) + ['limitations'],
                    'markdown_sha256': hashlib.sha256(md.encode()).hexdigest(), 'html_sha256': hashlib.sha256(html.encode()).hexdigest(), 'blocks': [{'id': b['id'], 'type': b['type'], 'status': b['status']} for b in blocks],
                    'interpretation': ({k: interpretation[k] for k in ('model_id', 'revision', 'grounding')} if interpretation else None),
                    'excludes': 'private labels, prompts, raw documents beyond the author\'s quotes, database paths, credentials'}
        rid = 'rp_' + secrets.token_hex(6)
        digest = _digest({'manifest': manifest, 'markdown': md})
        db.execute('INSERT INTO analysis_reports (id, analysis_id, workspace, version, mode, digest, markdown, html, manifest_json, flags_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, aid, principal.workspace, v['version'], mode, digest, md, html, json.dumps(manifest), json.dumps(flags), principal.id, now()))
        add_edge(db, principal.workspace, 'analysis', aid, 'report', rid, 'produced')
        history.record(db, principal.workspace, principal.id, 'analysis.report', 'analysis', aid, {'report_id': rid, 'version': v['version'], 'mode': mode, 'flags': len(flags), 'digest': digest})
        return self.report(db, principal, rid)

    def _model_interpretation(self, db, principal, v, facts, run_facts, model_host):
        if model_host is None:
            return {'model_id': None, 'revision': None, 'text': None, 'grounding': 'no generation model available: interpretation omitted', 'flags': [{'block': None, 'code': 'model_interpretation_unavailable'}]}
        rev = self.svc.models.resolve(db, 'generate', None); row = self.svc.models.row(db, rev['id'])
        evidence = []
        for bid, f in run_facts.items():
            if f:
                evidence.append('%s: kind %s, outcome %s, values %s' % (bid, f['kind'], f['outcome'], json.dumps(f['values'], sort_keys=True)))
        for b in v['blocks']:
            if b['type'] == 'assumption_table':
                evidence.append('%s assumptions: %s' % (b['id'], '; '.join('%s=%s %s' % (r['name'], r['value'], r.get('unit', '')) for r in b['rows'])))
        prompt = ('Write two short sentences interpreting the following scientific evidence for a reader. Use only the numbers given; do not invent measurements or claim verification.\n' + '\n'.join(evidence)[:3000] + '\nInterpretation:')
        pieces = []
        model_host.generate(row, {'messages': [{'role': 'user', 'content': prompt}], 'max_new_tokens': LIMITS['model_prose_tokens'], 'temperature_percent': 0, 'top_p_percent': 100, 'seed': 0}, on_segment=lambda seq, t: pieces.append(t))
        text = ''.join(pieces).strip()[:2000]
        allowed = set()
        for f in run_facts.values():
            if f:
                for val in f['values'].values():
                    if type(val) in (int, float):
                        allowed.add(str(val))
        for b in v['blocks']:
            if b['type'] == 'assumption_table':
                for r in b['rows']:
                    allowed.add(str(r['value']))
        flags = []
        for num in re.findall(r'(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])', text):
            if num not in allowed and num.lstrip('-') not in allowed and not (num.isdigit() and int(num) <= 2):
                flags.append({'block': None, 'code': 'model_numeric_claim_unsupported', 'value': num})
        for word in ('verified', 'proven', 'proof', 'guarantee'):
            if word in text.lower():
                flags.append({'block': None, 'code': 'model_overclaims_verification', 'word': word})
        return {'model_id': row['model_id'], 'revision': row['revision'], 'text': text, 'grounding': 'prompted only with the structured values listed in computed findings and declared assumptions; numbers checked against them', 'flags': flags}

    @staticmethod
    def _limitations(v, facts, run_facts, flags, interpretation):
        out = []
        unverified = [bid for bid, f in run_facts.items() if f and not f.get('verification') and not (f.get('producer_check') or {}).get('passed')]
        if unverified:
            out.append('Results without any passing verification: %s (computed, not independently checked).' % ', '.join(unverified))
        sampled = [b['id'] for b in v['blocks'] if b['type'] == 'verification' and (facts.get(b['id']) or {}).get('sampled')]
        if sampled:
            out.append('Sampled audits (%s) check a challenge-selected subset; they do not prove every item.' % ', '.join(sampled))
        stale = [b['id'] for b in v['blocks'] if b['status'] == 'stale']
        if stale:
            out.append('Stale blocks whose inputs changed after they were written: %s; they record what ran, not the current answer.' % ', '.join(stale))
        reused = [bid for bid, f in run_facts.items() if f and f.get('reused_from')]
        if reused:
            out.append('Reused results (no new computation): %s, bound to the original evidence roots.' % ', '.join(reused))
        contradictions = [f for f in flags if f['code'] in ('claim_contradicts_structured_result', 'claim_value_unsupported', 'unsupported_reference')]
        if contradictions:
            out.append('%d conclusion claim(s) contradict or lack structured support and are marked in the findings; they were not rewritten.' % len(contradictions))
        if interpretation and interpretation.get('text'):
            out.append('The interpretation section is model-written (%s @ %s) and is not evidence; its numbers were checked against the structured values (%d flag(s)).' % (interpretation['model_id'], interpretation['revision'], len(interpretation.get('flags', []))))
        hidden = [bid for bid, f in run_facts.items() if f and not f['private_values_visible']]
        if hidden:
            out.append('Private result values not visible to the report author for: %s.' % ', '.join(hidden))
        if not out:
            out.append('No limitation was detected by the structured checks; absence of a flag is not a proof of correctness.')
        return out

    @staticmethod
    def _markdown(v, facts, run_facts, claim_checks, interpretation, limitations, methods):
        L = ['# %s' % v['name'], '', 'Analysis `%s`, revision %d (digest `%s`), frozen. Report schema %s.' % (v['id'], v['version'], v['digest'][:16], REPORT_SCHEMA), '']
        def table(headers, rows):
            out = ['| ' + ' | '.join(headers) + ' |', '|' + '---|' * len(headers)]
            for r in rows:
                out.append('| ' + ' | '.join(str(x).replace('|', '\\|').replace('\n', ' ') for x in r) + ' |')
            return out
        L += ['## Source facts', '']
        srcs = [b for b in v['blocks'] if b['type'] == 'source_note']
        if not srcs:
            L.append('No source notes.')
        for b in srcs:
            f = facts.get(b['id'])
            if not f:
                L.append('- **%s**: reference unresolved.' % b['id']); continue
            L.append('- **%s** — %s%s (%s)%s%s' % (b['id'], f.get('document'), (' v%d' % f['version']) if f.get('version') else '', 'text sha256 ' + f['text_sha256'][:16] if f.get('text_sha256') else 'content sha256 ' + (f.get('content_sha256') or '')[:16],
                                                (' page %d' % f['page_number']) if f.get('page_number') else '', ' **[revoked source]**' if f.get('revoked') else ''))
            if f.get('quote'):
                L.append('  > ' + f['quote'].replace('\n', ' '))
            if b.get('text'):
                L.append('  ' + b['text'])
        L += ['', '## Declared assumptions', '']
        for b in [b for b in v['blocks'] if b['type'] == 'assumption_table']:
            L.append('**%s**%s' % (b['id'], ' (stale: %s)' % b['stale']['reason'] if b['status'] == 'stale' else ''))
            L += table(['name', 'value', 'unit', 'source', 'note'], [(r['name'], r['value'], r.get('unit', ''), r.get('source', 'user_edit'), r.get('note', '')) for r in b['rows']]) + ['']
        for b in [b for b in v['blocks'] if b['type'] == 'dataset_ref']:
            f = facts.get(b['id']) or {}
            L.append('- dataset **%s**: %s' % (b['id'], json.dumps(f, sort_keys=True) if f else 'unresolved'))
        for b in [b for b in v['blocks'] if b['type'] == 'operation_draft']:
            f = facts.get(b['id']) or {}
            L.append('- operation **%s**: kind %s, reference %s, digest %s' % (b['id'], f.get('kind'), f.get('reference'), (f.get('digest') or '')[:16]))
        L += ['', '## Computed findings', '']
        for b in [b for b in v['blocks'] if b['type'] == 'run_result']:
            f = run_facts.get(b['id'])
            if not f:
                L.append('- **%s**: job not readable.' % b['id']); continue
            L.append('**%s** — job `%s` (%s): state %s, outcome %s, review %s%s%s' % (b['id'], f['job_id'], f['kind'], f['state'], f['outcome'], f['review_state'], ', reused from `%s`' % f['reused_from'] if f['reused_from'] else '', ' **[stale]**' if b['status'] == 'stale' else ''))
            L.append('  evidence root `%s`; model %s; verifier %s' % ((f['evidence_root'] or '')[:16], f['model_id'], f['verifier_id']))
            if f['values']:
                L += table(['field', 'value'], [(k, json.dumps(val) if not isinstance(val, str) else val) for k, val in f['values'].items()])
            elif not f['private_values_visible']:
                L.append('  (private values withheld from this reader)')
            L.append('')
        for b in [b for b in v['blocks'] if b['type'] == 'comparison']:
            f = facts.get(b['id'])
            L.append('**%s** (comparison)%s: %s' % (b['id'], ' **[stale]**' if b['status'] == 'stale' else '', json.dumps(f, sort_keys=True)[:2000] if f else 'unresolved'))
            if b.get('text'):
                L.append('  ' + b['text'])
        L += ['', '## Verification scope', '']
        vers = [b for b in v['blocks'] if b['type'] == 'verification']
        if not vers:
            L.append('No independent verification records are attached to this revision.')
        for b in vers:
            f = facts.get(b['id'])
            if not f:
                L.append('- **%s**: unresolved.' % b['id']); continue
            L.append('- **%s** — `%s` class %s on job `%s`: %s; checked %s of %s%s. %s' % (b['id'], f['verification_id'], f['class'], f['target_job_id'], f['state'], f['checked'], f['total'], (', coverage ' + str(f['coverage'])) if f.get('coverage') else '', f['statement']))
        L += ['', '## Conclusions', '']
        for b in [b for b in v['blocks'] if b['type'] == 'conclusion']:
            L.append('**%s**%s' % (b['id'], ' **[stale: re-review required]**' if b['status'] == 'stale' else ''))
            if b.get('text'):
                L.append(b['text'])
            for i, c in enumerate(b.get('claims', [])):
                checks = [x for x in claim_checks if x['block'] == b['id'] and x['claim'] == i]
                bad = [x for x in checks if not x['ok']]
                L.append('- %s%s' % (c['text'], '' if not bad else ' **[FLAGGED: %s]**' % '; '.join('%s %s (expected %s, structured %s)' % (x.get('key') or x.get('ref'), x['reason'], x.get('expected'), x.get('actual')) for x in bad)))
            L.append('')
        if interpretation and interpretation.get('text'):
            L += ['## Model-written interpretation (not evidence)', '', '_Generated by %s @ %s; %s._' % (interpretation['model_id'], interpretation['revision'], interpretation['grounding']), '', interpretation['text'], '']
            if interpretation.get('flags'):
                L.append('Flags: ' + '; '.join(json.dumps(f) for f in interpretation['flags']))
        L += ['', '## Limitations', ''] + ['- ' + x for x in limitations]
        L += ['', '## Methods', '', 'Models: %s. Verifiers: %s.' % (', '.join(sorted(methods.get('models', []))) or 'none', ', '.join(sorted(methods.get('verifiers', []))) or 'none'), '']
        return '\n'.join(L)

    @staticmethod
    def _html(title, md):
        """Minimal deterministic Markdown → HTML for the subset the report writer emits (headings, tables, lists, quotes, code)."""
        out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><title>%s</title><style>body{font-family:sans-serif;max-width:60em;margin:1em auto;padding:0 1em}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:2px 6px}blockquote{border-left:3px solid #ccc;margin:0;padding-left:1em;color:#444}</style></head><body>' % html_mod.escape(title)]
        in_table, in_list = False, False
        def inline(s):
            s = html_mod.escape(s)
            s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
            s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
            s = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'<em>\1</em>', s)
            return s
        for line in md.split('\n'):
            if line.startswith('|'):
                cells = [c.strip() for c in line.strip().strip('|').split('|')]
                if all(set(c) <= {'-'} for c in cells):
                    continue
                if not in_table:
                    out.append('<table>'); in_table = True
                    out.append('<tr>' + ''.join('<th>%s</th>' % inline(c) for c in cells) + '</tr>')
                else:
                    out.append('<tr>' + ''.join('<td>%s</td>' % inline(c) for c in cells) + '</tr>')
                continue
            if in_table:
                out.append('</table>'); in_table = False
            if line.startswith('- ') or line.startswith('  > ') or line.startswith('  '):
                if line.startswith('- '):
                    if not in_list:
                        out.append('<ul>'); in_list = True
                    out.append('<li>%s</li>' % inline(line[2:]))
                elif line.startswith('  > '):
                    out.append('<blockquote>%s</blockquote>' % inline(line[4:]))
                else:
                    out.append('<p>%s</p>' % inline(line.strip()))
                continue
            if in_list:
                out.append('</ul>'); in_list = False
            if line.startswith('# '):
                out.append('<h1>%s</h1>' % inline(line[2:]))
            elif line.startswith('## '):
                out.append('<h2>%s</h2>' % inline(line[3:]))
            elif line.strip():
                out.append('<p>%s</p>' % inline(line))
        if in_table:
            out.append('</table>')
        if in_list:
            out.append('</ul>')
        out.append('</body></html>')
        return '\n'.join(out)

    def report(self, db, principal, rid):
        principal.require('knowledge:read')
        r = db.execute('SELECT * FROM analysis_reports WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'report')
        return {'id': rid, 'analysis_id': r['analysis_id'], 'version': r['version'], 'mode': r['mode'], 'digest': r['digest'], 'markdown': r['markdown'], 'manifest': json.loads(r['manifest_json']), 'flags': json.loads(r['flags_json']), 'created_by': r['created_by'], 'created_at': r['created_at']}

    def report_html(self, db, principal, rid):
        principal.require('knowledge:read')
        r = db.execute('SELECT html FROM analysis_reports WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'report')
        return r['html']

    def report_bundle(self, db, principal, rid):
        """Portable Markdown-plus-artifact bundle: report.md, report.html, manifest.json with per-file digests."""
        principal.require('artifact:export')
        rep = self.report(db, principal, rid); html = self.report_html(db, principal, rid)
        files = {'report.md': rep['markdown'], 'report.html': html, 'manifest.json': json.dumps(rep['manifest'], indent=1, sort_keys=True)}
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'report', rid, {'digest': rep['digest']})
        return {'schema': 'metacoin-report-bundle/v1', 'report_id': rid, 'analysis_id': rep['analysis_id'], 'version': rep['version'], 'digest': rep['digest'], 'files': files,
                'file_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}, 'contents': 'report text, HTML rendering and the artifact manifest (identifiers, commitments); no payloads'}

    # ---- reviewable disclosure projections --------------------------------------------------------------------------------
    def _project(self, db, principal, rep, scope):
        """Deterministic projection of a report by block allowlist and per-block field allowlist. Returns (projected markdown,
        included blocks, omitted blocks, warnings about indirect disclosure)."""
        if type(scope) is not dict or set(scope) - {'blocks', 'fields', 'include_quotes', 'include_assumption_values', 'include_interpretation'}:
            raise ServiceError('VALIDATION', {'code': 'scope', 'allowed': ['blocks', 'fields', 'include_quotes', 'include_assumption_values', 'include_interpretation']})
        v = self.view(db, principal, rep['analysis_id'], rep['version'])
        all_ids = [b['id'] for b in v['blocks']]
        blocks = scope.get('blocks')
        if blocks is None:
            blocks = all_ids
        if type(blocks) is not list or not all(b in all_ids for b in blocks):
            raise ServiceError('VALIDATION', {'code': 'scope_blocks', 'known': all_ids})
        fields = scope.get('fields') or {}
        if type(fields) is not dict or not all(type(k) is str and type(x) is list for k, x in fields.items()):
            raise ServiceError('VALIDATION', 'scope.fields: {block: [field names]}')
        include_quotes = bool(scope.get('include_quotes', False)); include_vals = bool(scope.get('include_assumption_values', True)); include_interp = bool(scope.get('include_interpretation', False))
        omitted = [b for b in all_ids if b not in blocks]
        warnings = []
        md = rep['markdown']
        # rebuild from the report's markdown by section-aware filtering is fragile; project from the structured facts instead
        lines = ['# %s' % v['name'], '', 'Projection of report `%s` (analysis `%s` revision %d). Included blocks: %s. Omitted blocks: %d.' % (rep['id'], v['id'], v['version'], ', '.join(blocks) or 'none', len(omitted)), '']
        bmap = {b['id']: b for b in v['blocks']}
        included_values = {}
        for bid in blocks:
            b = bmap[bid]
            if b['type'] == 'source_note':
                lines.append('- source **%s**: reference %s `%s`%s' % (bid, b['ref_kind'], b['ref_id'], (' page %d' % b['page_number']) if b.get('page_number') else ''))
                if include_quotes and b.get('quote'):
                    lines.append('  > ' + b['quote'].replace('\n', ' '))
                    warnings.append({'block': bid, 'code': 'source_quote_disclosed', 'detail': 'a verbatim passage of the source is included'})
                name = (b.get('label') or '')
                if re.search(r'\.\w{2,4}$', name):
                    warnings.append({'block': bid, 'code': 'filename_in_label', 'detail': 'the block label looks like a filename and is included: ' + name})
                    lines.append('  label: ' + name)
            elif b['type'] == 'assumption_table':
                if include_vals:
                    lines.append('- assumptions **%s**: ' % bid + '; '.join('%s=%s %s' % (r['name'], r['value'], r.get('unit', '')) for r in b['rows']))
                    for r in b['rows']:
                        included_values[bid + '.' + r['name']] = r['value']
                else:
                    lines.append('- assumptions **%s**: %d named assumptions (values withheld)' % (bid, len(b['rows'])))
            elif b['type'] == 'run_result':
                f = self._job_facts(db, principal, b['ref_id'], b.get('fields', []))
                allow = fields.get(bid)
                vals = {k: val for k, val in (f['values'] if f else {}).items() if allow is None or k in allow}
                lines.append('- result **%s**: job `%s` outcome %s, review %s, evidence root `%s`' % (bid, b['ref_id'], f['outcome'] if f else None, f['review_state'] if f else None, (f['evidence_root'] or '')[:16] if f else ''))
                for k, val in vals.items():
                    lines.append('  - %s: %s' % (k, json.dumps(val))); included_values[bid + '.' + k] = val
                if allow is not None:
                    lines.append('  (fields withheld: %s)' % ', '.join(sorted(set((f['values'] if f else {})) - set(allow))))
            elif b['type'] == 'verification':
                f = self._verification_facts(db, principal, b['ref_id'])
                if f:
                    lines.append('- verification **%s**: class %s, %s, checked %s of %s; commitment `%s`' % (bid, f['class'], f['state'], f['checked'], f['total'], f['result_commitment'][:16]))
                    if f['target_job_id'] not in [bmap[x]['ref_id'] for x in blocks if bmap[x]['type'] == 'run_result']:
                        warnings.append({'block': bid, 'code': 'verification_of_hidden_result', 'detail': 'the verified job is not among the included results; its id is disclosed by the record'})
            elif b['type'] == 'comparison':
                f = self._comparison_facts(db, principal, b, {x: self._job_facts(db, principal, bmap[x]['ref_id'], bmap[x].get('fields', [])) for x in b.get('against', []) if x in bmap})
                hidden = [x for x in b.get('against', []) if x not in blocks]
                if hidden:
                    warnings.append({'block': bid, 'code': 'comparison_reveals_hidden_results', 'detail': 'the comparison carries values of omitted blocks: ' + ', '.join(hidden)})
                lines.append('- comparison **%s**: %s' % (bid, json.dumps(f, sort_keys=True)[:1500] if f else 'unresolved'))
            elif b['type'] == 'conclusion':
                for c in b.get('claims', []):
                    leaks = [k for k in c.get('values', {}) if k.partition('.')[0] in omitted or (k.partition('.')[0] in fields and k.partition('.')[2] not in fields[k.partition('.')[0]])]
                    if leaks:
                        warnings.append({'block': bid, 'code': 'claim_text_carries_hidden_values', 'detail': 'claim references values of omitted blocks/fields: ' + ', '.join(leaks)})
                    lines.append('- conclusion **%s**: %s' % (bid, c['text']))
                if b.get('text'):
                    lines.append('  ' + b['text'])
            elif b['type'] in ('dataset_ref', 'operation_draft'):
                lines.append('- %s **%s**: %s `%s`' % (b['type'], bid, b.get('ref_kind'), b.get('ref_id')))
            elif b['type'] == 'text':
                lines.append(b.get('text', ''))
        rep_manifest = rep['manifest']
        if include_interp and rep_manifest.get('interpretation'):
            m = re.search(r'## Model-written interpretation \(not evidence\)\n\n(.*?)\n\n## ', md, re.S)
            if m:
                lines += ['', '## Model-written interpretation (not evidence)', '', m.group(1)]
                warnings.append({'block': None, 'code': 'interpretation_may_paraphrase_hidden_values', 'detail': 'model prose was generated from the full evidence set, not from this projection'})
        for f in rep['flags']:
            if f.get('code') in ('claim_contradicts_structured_result', 'claim_value_unsupported') and f.get('block') in blocks:
                lines.append('- flag on %s: %s' % (f['block'], f['code']))
        text = '\n'.join(lines) + '\n'
        for w in list(warnings):
            pass
        # error messages / provenance fields: the projection never includes flags of omitted blocks, private labels or paths
        if any(x in text for x in ('/home/', 'private_label', 'bootstrap.json')):
            raise ServiceError('CONFLICT', 'projection would carry a path or private label; refused')
        return text, blocks, omitted, warnings, included_values

    def projection_preview(self, db, principal, rid, scope):
        principal.require('job:read_private')
        rep = self.report(db, principal, rid)
        text, blocks, omitted, warnings, values = self._project(db, principal, rep, scope or {})
        return {'report_id': rid, 'scope': scope or {}, 'included_blocks': blocks, 'omitted_blocks': omitted, 'projected_markdown': text, 'projected_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'warnings': warnings, 'disclosed_values': values, 'note': 'this is the exact text a signed export would carry; warnings describe indirect disclosure through included fields, not a model judgement'}

    def export_projection(self, db, principal, rid, scope, acknowledge_warnings=False):
        principal.require('artifact:export'); principal.require('job:read_private')
        rep = self.report(db, principal, rid)
        text, blocks, omitted, warnings, values = self._project(db, principal, rep, scope or {})
        if warnings and not acknowledge_warnings:
            raise ServiceError('CONFLICT', {'code': 'disclosure_warnings', 'warnings': warnings, 'action': 'review the preview and export with acknowledge_warnings=true to make the authorized disclosure'})
        from . import metering
        pub = metering.ensure_service_key(self.settings, db)
        statement = {'schema': PROJECTION_SCHEMA, 'report_id': rid, 'analysis_id': rep['analysis_id'], 'analysis_version': rep['version'], 'report_digest': rep['digest'], 'analysis_digest': rep['manifest']['analysis_digest'],
                     'scope': scope or {}, 'included_blocks': blocks, 'omitted_block_count': len(omitted), 'projected_sha256': hashlib.sha256(text.encode()).hexdigest(),
                     'artifacts': [a for a in rep['manifest']['artifacts'] if a['block'] in blocks], 'warnings_acknowledged': warnings, 'issued_at': now(), 'issued_by': principal.id, 'workspace': principal.workspace, 'issuer_key_id': crypto.key_id_for(pub),
                     'claims': 'ordinary selective disclosure signed by the issuing service; omitted content is absent from the bytes, which is not a cryptographic proof about it'}
        signature = crypto.sign(crypto.load_signing_key(self.settings.keys_dir / 'service.ed25519'), merkle.canonical(statement))
        bundle = {'statement': statement, 'signature': signature, 'public_key': pub, 'files': {'projection.md': text}}
        pid = 'pj_' + secrets.token_hex(6)
        db.execute('INSERT INTO analysis_projections (id, report_id, workspace, scope_json, digest, bundle_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (pid, rid, principal.workspace, json.dumps(scope or {}), statement['projected_sha256'], json.dumps(bundle), principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'analysis.projection', 'report', rid, {'projection_id': pid, 'included': blocks, 'omitted': len(omitted), 'warnings': len(warnings)})
        return dict(bundle, projection_id=pid)

    def verify_projection(self, db, bundle):
        if type(bundle) is not dict or not {'statement', 'signature', 'public_key', 'files'} <= set(bundle) or type(bundle['statement']) is not dict:
            raise ServiceError('VALIDATION', 'bundle: {statement, signature, public_key, files}')
        st = bundle['statement']
        row = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()
        known = row is not None and row['value'] == bundle['public_key']
        try:
            ok = crypto.verify(bundle['public_key'], merkle.canonical(st), bundle['signature'])
        except Exception:
            ok = False
        text = (bundle.get('files') or {}).get('projection.md')
        digest_ok = type(text) is str and hashlib.sha256(text.encode()).hexdigest() == st.get('projected_sha256')
        rep = db.execute('SELECT digest FROM analysis_reports WHERE id=?', (st.get('report_id'),)).fetchone()
        return {'signature_valid': bool(ok), 'issuer_is_this_service': known, 'schema_ok': st.get('schema') == PROJECTION_SCHEMA, 'projected_text_matches_statement': digest_ok,
                'report_known_here': rep is not None and rep['digest'] == st.get('report_digest'),
                'evidence_scope': {'verified': 'the signature of the issuing service over the statement and the digest of the disclosed projection text', 'disclosed_blocks': st.get('included_blocks'), 'omitted_blocks': st.get('omitted_block_count'),
                                   'not_verified': 'the underlying inputs, results and omitted blocks were not disclosed and are not independently verified by this check'}}
