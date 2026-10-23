"""Reusable mission products (order 2026-10-23 group E). A completed, verified lunar product (terrain tile, route feasibility,
scenario pack) becomes a versioned listing: content hash, producer, pinned source data (checksums, resolution, coordinate and
unit conventions), compatibility requirements, a supported license and permitted uses. Native payment buys the stated service
(packaging, processing, verified access) and an access entitlement; it never transfers copyright and never proves scientific
validity, and the public source data stays public. The purchase binds the version and price to an obligation; access is granted
once per entitlement and retries are idempotent (same request id -> same entitlement, no second charge). A crash after settlement
but before delivery is recovered by the retry: the settled intent is found by its request id and the entitlement delivered
without charging again. Downloaded bytes cannot be made to cease to exist: the remedies are stated, not implied. Reuse requires
compatibility: a mission needing another resolution, other units or assumptions the product does not carry is refused."""
import hashlib
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError
from . import protocol as P

KINDS = {'terrain_tile': 'terrain_scenario_pack', 'route_feasibility': 'verified_computation', 'scenario_pack': 'terrain_scenario_pack'}
LICENSES = {'processing-service': 'payment buys the processing/packaging/verification service and access to this packaged artifact; the public source data (NASA PDS LOLA) remains public and free; no copyright or exclusivity is transferred; redistribution of the packaged artifact is not granted',
            'verification-service': 'payment buys the independent verification service and access to the verified record; no rights in the underlying data or result beyond access'}
PERMITTED = ('mission_planning', 'evidence_gate', 'reuse_as_input', 'inspection')
RIGHTS = 'access entitlement only: not ownership of public data, not scientific validity, not exclusivity; revocation or refund cannot recall bytes already downloaded — the remedies are the declared dispute policy and the receipt record'


class Products:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- publication ---------------------------------------------------------------------------------------------------------------
    def publish(self, db, principal, body):
        principal.require('work:offer')
        if type(body) is not dict or set(body) - {'source_job', 'title', 'price', 'asset', 'license', 'permitted_uses', 'compatibility', 'version_of'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['source_job', 'title', 'price', 'asset?', 'license (%s)' % '|'.join(LICENSES), 'permitted_uses?', 'compatibility {assumptions?}', 'version_of? (previous product id)']})
        lic = body.get('license')
        if lic not in LICENSES:
            raise ServiceError('VALIDATION', {'code': 'license_unsupported', 'allowed': list(LICENSES), 'note': 'ambiguous or unsupported rights are refused rather than invented'})
        uses = body.get('permitted_uses') or ['mission_planning', 'reuse_as_input', 'inspection']
        if type(uses) is not list or not uses or not set(uses) <= set(PERMITTED):
            raise ServiceError('VALIDATION', {'code': 'permitted_uses', 'allowed': list(PERMITTED)})
        try:
            price = P.amount(body.get('price'), allow_zero=True)
        except P.ProtocolError as exc:
            raise ServiceError('VALIDATION', {'code': exc.code})
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (body.get('source_job'), principal.workspace)).fetchone()
        if job is None or job['kind'] not in KINDS or job['state'] != 'succeeded':
            raise ServiceError('NOT_FOUND', 'succeeded lunar product job')
        award = db.execute("SELECT a.* FROM work_awards a JOIN work_milestones m ON m.award_id=a.id WHERE m.job_id=?", (job['id'],)).fetchone()
        prow = db.execute('SELECT principal_id FROM providers WHERE id=?', (award['provider_id'],)).fetchone() if award else None
        if not (job['submitted_by'] == principal.id or (award and award['awarded_by'] == principal.id) or (prow and prow['principal_id'] == principal.id) or principal.can('job:read_private')):
            raise ServiceError('FORBIDDEN', {'code': 'not_a_party', 'note': 'only the requester or the provider of the job publishes it as a product'})
        L = self.svc.lunar; data, media, name = L.download(db, self._private(principal), job['id']); chash = hashlib.sha256(data).hexdigest()
        res = json.loads(data) if media == 'application/json' else None; inputs = (res or {}).get('inputs') or {}
        from ..lunar import sources as SRC
        desc = inputs.get('source') or (inputs.get('terrain') or {}).get('source') or {}
        if isinstance(desc, dict) and desc.get('kind') == 'ldem_tile':
            src_name = desc.get('region') or 'ldem_tile'
            src = {'name': src_name, 'product_id': desc.get('product_id'), 'data_set_id': desc.get('data_set_id'), 'image_sha256': desc.get('image_sha256'), 'label_sha256': desc.get('label_sha256'), 'extract_sha256': hashlib.sha256(json.dumps(desc.get('dn', []), separators=(',', ':')).encode()).hexdigest() if desc.get('dn') else None, 'lines': desc.get('lines'), 'samples': desc.get('samples'), 'pixel_spacing_mm': desc.get('pixel_spacing_mm'), 'projection': desc.get('projection'), 'datum': desc.get('datum'), 'redistribution': desc.get('redistribution'), 'what_this_is_not': desc.get('what_this_is_not'), 'label': desc.get('label')}
        else:
            src_name = desc if isinstance(desc, str) else (desc.get('region') if isinstance(desc, dict) else None) or 'synthetic'
            src = {'name': src_name, 'note': 'synthetic or non-archive source: labelled, never presented as NASA data', 'label': desc.get('label') if isinstance(desc, dict) else None}
        conv = {'coordinate_system': 'LDEM_4 simple cylindrical, planetocentric latitude/longitude in milli-degrees', 'units': {'elevation': 'metres (0.5 m per DN, offset 1737400 m)', 'slope': 'milli-degrees', 'distance': 'metres'}, 'resolution_mm_per_pixel': int(round(SRC.LDEM_4['map_scale_m_per_pixel'] * 1000)), 'grid': [src.get('lines'), src.get('samples')], 'interpretation': 'grid-scale demonstration product; not flight navigation and not a hazard-safe route selection'}
        comp = dict({'resolution_mm_per_pixel': conv['resolution_mm_per_pixel'], 'units': conv['units'], 'source': src_name, 'assumptions': ['grid-scale slopes from a 7.58 km/pixel DEM', 'declared rover model as frozen in the job inputs', 'no thermal or illumination model']}, **(body.get('compatibility') or {}))
        ver = 1; prev = None
        if body.get('version_of'):
            prev = self._row(db, principal.workspace, body['version_of'])
            if prev['producer_principal'] != principal.id:
                raise ServiceError('FORBIDDEN', 'only the producer versions its product')
            ver = prev['version'] + 1
        ex = db.execute('SELECT id FROM mission_products WHERE content_hash=? AND version=?', (chash, ver)).fetchone()
        if ex:
            return dict(self.view(db, principal, ex['id']), replayed=True)
        acct = self.svc.native.account_of(db, principal); asset = body.get('asset') or self.svc.native.default_asset(db, principal.workspace)
        art = self.svc.store.store(db, workspace=principal.workspace, kind='export', owner_id=principal.id, plaintext=data, recipients=[], intended_use='mission-product-' + KINDS[job['kind']], public=False)
        measured = {'recomputation_wall_s_on_this_host': max(job['updated_at'] - job['created_at'], 0), 'note': 'wall time of the original job on this host (job created -> succeeded); compare only with an equivalent recomputation measured the same way; no invented benefit figure'}
        pid = 'mp_' + secrets.token_hex(6); t = now(); title = body.get('title') or ('%s of %s' % (KINDS[job['kind']], src_name))
        db.execute('INSERT INTO mission_products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (pid, principal.workspace, acct['id'], principal.id, ver, title[:128], KINDS[job['kind']], chash, art, job['id'], award['id'] if award else None, json.dumps(src), json.dumps(conv), json.dumps(comp), lic, json.dumps(uses), price, asset, json.dumps(measured), 'listed', t, t))
        from .. import datasets as DS
        DS.add_edge(db, principal.workspace, 'lunar_job', job['id'], 'mission_product', pid, 'packaged_as')
        if prev:
            DS.add_edge(db, principal.workspace, 'mission_product', prev['id'], 'mission_product', pid, 'superseded_by')
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'mission_product', pid, {'published': True, 'content_hash': chash, 'version': ver, 'price': price, 'license': lic})
        return self.view(db, principal, pid)

    def _private(self, principal):
        class _P:
            pass
        p = _P(); p.id = principal.id; p.workspace = principal.workspace; p.role = principal.role; p.can = lambda perm: True; p.require = lambda perm: None; return p

    def _row(self, db, ws, pid):
        r = db.execute('SELECT * FROM mission_products WHERE id=? AND workspace=?', (pid, ws)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'mission product')
        return r

    def withdraw(self, db, principal, pid):
        r = self._row(db, principal.workspace, pid)
        if r['producer_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the producer withdraws')
        db.execute("UPDATE mission_products SET state='withdrawn', updated_at=? WHERE id=?", (now(), pid)); return self.view(db, principal, pid)

    # ---- compatibility ---------------------------------------------------------------------------------------------------------------
    def check_reuse(self, db, principal, pid, req):
        """Compatibility of a mission requirement with the product's frozen conventions and assumptions (exact resolution/units; the
        requirement's assumptions must be carried by the product; a different source tile is a different product)."""
        r = self._row(db, principal.workspace, pid); comp = json.loads(r['compatibility_json']); conv = json.loads(r['conventions_json']); reasons = []
        req = req or {}
        if req.get('resolution_mm_per_pixel') is not None and req['resolution_mm_per_pixel'] != comp['resolution_mm_per_pixel']:
            reasons.append('resolution: product %s mm/px, required %s mm/px (grid-scale products cannot be resampled into finer evidence)' % (comp['resolution_mm_per_pixel'], req['resolution_mm_per_pixel']))
        for k, v in (req.get('units') or {}).items():
            if comp['units'].get(k) != v:
                reasons.append('units[%s]: product %r, required %r' % (k, comp['units'].get(k), v))
        if req.get('source') and req['source'] != comp['source']:
            reasons.append('source tile: product %s, required %s' % (comp['source'], req['source']))
        missing = [a for a in (req.get('assumptions') or []) if a not in comp['assumptions']]
        if missing:
            reasons.append('assumptions not carried by the product: %s (fresh evidence required)' % missing)
        if r['state'] != 'listed':
            reasons.append('product withdrawn')
        return {'product': pid, 'version': r['version'], 'compatible': not reasons, 'reasons': reasons, 'product_conventions': conv, 'product_compatibility': comp, 'note': 'a reused result keeps its original assumptions and verification scope'}

    # ---- purchase and access ---------------------------------------------------------------------------------------------------------
    def purchase(self, db, principal, pid, body):
        """Idempotent by (buyer, request_id): the obligation binds version and price; native settlement grants the entitlement."""
        principal.require('native:transfer'); r = self._row(db, principal.workspace, pid)
        if type(body) is not dict or type(body.get('request_id')) is not str or not 1 <= len(body['request_id']) <= 128:
            raise ServiceError('VALIDATION', {'code': 'request_id'})
        rid = body['request_id']; buyer = self.svc.native.account_of(db, principal)
        ex = db.execute('SELECT * FROM product_entitlements WHERE buyer_account=? AND request_id=?', (buyer['id'], rid)).fetchone()
        if ex:
            return dict(self.entitlement_view(db, principal, ex['id']), replayed=True)
        if r['state'] != 'listed':
            raise ServiceError('CONFLICT', {'code': 'product_withdrawn'})
        if buyer['id'] == r['producer_account']:
            raise ServiceError('VALIDATION', {'code': 'self_purchase'})
        eid = 'pe_' + secrets.token_hex(6); t = now()
        ob = self.svc.native.obligation_create(db, principal.workspace, 'product', buyer['id'], r['producer_account'], r['asset'], r['price'], 'product_entitlement', eid, {'partial_payments': False, 'product': pid, 'version': r['version'], 'content_hash': r['content_hash'], 'license': r['license'], 'rights': RIGHTS}) if r['price'] else None
        obid = (ob['id'] if isinstance(ob, dict) else ob) if ob else None
        db.execute('INSERT INTO product_entitlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (eid, principal.workspace, pid, r['version'], buyer['id'], principal.id, obid, None, rid, 'purchasing', None, None, t, t))
        if r['price'] == 0:
            db.execute("UPDATE product_entitlements SET state='purchased', updated_at=? WHERE id=?", (now(), eid)); return self.entitlement_view(db, principal, eid)
        tbody = {'from_account': buyer['id'], 'to_account': r['producer_account'], 'amount': r['price'], 'asset': r['asset'], 'obligation_id': obid, 'request_id': 'product-' + rid, 'purpose': 'product %s v%d (%s)' % (pid, r['version'], r['license'])}
        if (buyer['custody'] or '').startswith('external-signer'):
            prep = self.svc.native.transfer_prepare(db, principal, tbody); ctx = {'kind': 'product_purchase', 'product': pid, 'version': r['version'], 'content_hash': r['content_hash'], 'license': r['license'], 'recipient': r['producer_account'], 'amount': r['price'], 'asset': r['asset'], 'obligation_id': obid}
            sr = self.svc.signing.create(db, principal.workspace, buyer['id'], 'product_purchase', prep['message'], ctx, 'product_entitlement', eid)
            db.execute("UPDATE product_entitlements SET state='awaiting_signature', delivery_json=?, updated_at=? WHERE id=?", (json.dumps({'signing_request': sr['id']}), now(), eid))
            return dict(self.entitlement_view(db, principal, eid), awaiting_signature=sr['id'])
        it = self.svc.native.transfer(db, principal, tbody)
        db.execute("UPDATE product_entitlements SET state='purchased', intent_id=?, updated_at=? WHERE id=?", (it['id'], now(), eid))
        db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')                                       # settlement + entitlement durable before the response
        from ..economy.ops import fault
        fault(db, self.settings, 'product_delivery')                                                # a crash here loses the response, never the entitlement
        return self.entitlement_view(db, principal, eid)

    def on_signed(self, db, principal, req, result):
        eid = req['ref_id']; db.execute("UPDATE product_entitlements SET state='purchased', intent_id=?, updated_at=? WHERE id=?", (result.get('id'), now(), eid))

    def deliver(self, db, principal, eid):
        """Access delivery (idempotent): the artifact bytes for a purchased entitlement; marks delivered once."""
        e = db.execute('SELECT * FROM product_entitlements WHERE id=? AND workspace=?', (eid, principal.workspace)).fetchone()
        if e is None:
            raise ServiceError('NOT_FOUND', 'entitlement')
        if e['buyer_principal'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the buyer accesses its entitlement')
        if e['state'] not in ('purchased', 'delivered'):
            raise ServiceError('CONFLICT', {'code': 'entitlement_state', 'state': e['state'], 'note': 'nothing is delivered before settlement; an awaiting-signature purchase delivers after its signer answers'})
        r = self._row(db, principal.workspace, e['product_id']); data = self.svc.store.load(db, r['artifact_id'], principal.workspace)
        if hashlib.sha256(data).hexdigest() != r['content_hash']:
            raise ServiceError('INTERNAL_DEFECT', 'product content hash mismatch')
        if e['state'] == 'purchased':
            db.execute("UPDATE product_entitlements SET state='delivered', delivered_at=?, updated_at=? WHERE id=?", (now(), now(), eid))
            history.record(db, principal.workspace, principal.id, 'native.obligation', 'product_entitlement', eid, {'delivered': True, 'content_hash': r['content_hash']})
        return data, 'application/json', 'mission-product-%s-v%d.json' % (r['id'], r['version'])

    # ---- views ------------------------------------------------------------------------------------------------------------------------
    def view(self, db, principal, pid):
        principal.require('work:read'); r = self._row(db, principal.workspace, pid)
        sold = db.execute("SELECT COUNT(*), SUM(CASE WHEN state='delivered' THEN 1 ELSE 0 END) FROM product_entitlements WHERE product_id=? AND state IN ('purchased','delivered')", (pid,)).fetchone()
        return {'schema': 'metacoin-mission-product/v1', 'id': pid, 'version': r['version'], 'title': r['title'], 'kind': r['kind'], 'content_hash': r['content_hash'], 'producer_account': r['producer_account'], 'source_job': r['source_job'], 'source_award': r['source_award'], 'source_data': json.loads(r['source_data_json']), 'conventions': json.loads(r['conventions_json']), 'compatibility': json.loads(r['compatibility_json']),
                'license': {'id': r['license'], 'terms': LICENSES[r['license']]}, 'permitted_uses': json.loads(r['permitted_uses_json']), 'rights': RIGHTS, 'price': r['price'], 'asset': r['asset'], 'measured': json.loads(r['measured_json']), 'state': r['state'], 'entitlements': {'sold': sold[0] or 0, 'delivered': sold[1] or 0}, 'created_at': r['created_at']}

    def entitlement_view(self, db, principal, eid):
        e = db.execute('SELECT * FROM product_entitlements WHERE id=? AND workspace=?', (eid, principal.workspace)).fetchone()
        if e is None:
            raise ServiceError('NOT_FOUND', 'entitlement')
        ob = db.execute('SELECT amount, settled_amount, state FROM native_obligations WHERE id=?', (e['obligation_id'],)).fetchone() if e['obligation_id'] else None
        return {'schema': 'metacoin-product-entitlement/v1', 'id': eid, 'product_id': e['product_id'], 'version': e['version'], 'buyer_account': e['buyer_account'], 'state': e['state'], 'obligation': dict(ob) if ob else None, 'intent_id': e['intent_id'], 'request_id': e['request_id'], 'delivered_at': e['delivered_at'], 'extra': json.loads(e['delivery_json']) if e['delivery_json'] else None, 'rights': RIGHTS, 'access': '/api/v1/products/entitlements/%s/download' % eid}

    def list(self, db, principal, state='listed'):
        principal.require('work:read')
        return [self.view(db, principal, r['id']) for r in db.execute('SELECT id FROM mission_products WHERE workspace=? AND (state=? OR ?=?) ORDER BY created_at', (principal.workspace, state, state, 'all')).fetchall()]

    def my_entitlements(self, db, principal):
        acct = self.svc.native.account_of(db, principal, create=False)
        if acct is None:
            return []
        return [self.entitlement_view(db, principal, r['id']) for r in db.execute('SELECT id FROM product_entitlements WHERE buyer_account=? ORDER BY created_at', (acct['id'],)).fetchall()]
