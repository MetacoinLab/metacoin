"""Worker side of document imports: run the extraction child under limits, stream progress into the import record,
publish the extraction revision atomically with the job result (fenced), and decide review-vs-ready."""
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from experiments.private_receipts import receipt as merkle
from .. import auth, history
from ..compute.engine import compute_interpreter, exactable
from ..db import now
from ..errors import ServiceError
from .service import implementation_digest

ROOT = Path(__file__).resolve().parents[2]


class DocumentEngine:
    def __init__(self, worker):
        self.worker = worker
        self.settings = worker.settings
        self.limits = worker.settings.limits
        self.runtime = compute_interpreter(worker.settings)

    def _set(self, iid, **cols):
        cols['updated_at'] = now()
        with self.worker.db.tx() as db:
            db.execute('UPDATE document_imports SET ' + ', '.join(k + '=?' for k in cols) + ' WHERE id=?', (*cols.values(), iid))

    def run(self, job):
        with self.worker.db.read() as db:
            contract, spec = self.worker._spec(db, job)
            params = json.loads(contract['params_json'] or '{}')
            prow = db.execute('SELECT * FROM principals WHERE id=?', (job['submitted_by'],)).fetchone()
            imp = db.execute('SELECT * FROM document_imports WHERE id=? AND workspace=?', (spec['inputs']['import_id'], job['workspace'])).fetchone()
        principal = auth.Principal(prow)
        if params.get('implementation_digest') != implementation_digest():
            if imp is not None:
                self._set(imp['id'], state='failed', stage='failed', error_json=json.dumps({'code': 'implementation_mismatch', 'reason': 'the extractor implementation changed since the import was accepted; retry binds the current one'}))
            return self.worker._finish(job, None, 'MANIFEST_MISMATCH')
        if imp is None or imp['state'] == 'removed' or imp['job_id'] != job['id']:
            if imp is not None and imp['state'] != 'removed':
                self._set(imp['id'], state='failed', stage='failed', error_json=json.dumps({'code': 'stale_attempt', 'reason': 'this job is not the import\'s current attempt'}))
            return self.worker._finish(job, None, 'INPUT_INVALID')
        if not self.runtime:
            self._set(imp['id'], state='failed', stage='no compute interpreter', error_json=json.dumps({'code': 'no_compute_interpreter'}))
            return self.worker._finish(job, None, 'COMPUTATION_ERROR')
        should_cancel, state = self.worker.models.poller(job)
        attempt = spec['inputs']['attempt']
        workdir = self.settings.home / 'documents' / ('import-%s-%d-%s' % (imp['id'], attempt, secrets.token_hex(3)))
        workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with self.worker.db.read() as db:
                source = self.worker.store.load(db, imp['source_artifact_id'], job['workspace'])
            if hashlib.sha256(source).hexdigest() != imp['content_sha256']:
                raise ServiceError('COMPUTATION', 'stored source digest mismatch')
            (workdir / 'source.pdf').write_bytes(source)
            limits = {'max_pages': self.limits['document_max_pages'], 'max_text_chars': self.limits['document_max_text_chars'], 'max_pixels': self.limits['document_max_pixels'], 'preview_dpi': self.limits['document_preview_dpi'],
                      'ocr_dpi': self.limits['document_ocr_dpi'], 'cpu_seconds': self.limits['document_child_cpu_seconds'], 'address_space_bytes': self.limits['document_child_address_space_bytes']}
            (workdir / 'spec.json').write_text(json.dumps({'policy': json.loads(imp['policy_json']), 'limits': limits, 'source_sha256': imp['content_sha256']}))
            self._set(imp['id'], state='validating', stage='validating', progress_json=json.dumps({'pages_done': 0}))
            env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': os.environ.get('HOME', '/'), 'PYTHONUNBUFFERED': '1', 'HF_HUB_OFFLINE': '1', 'no_proxy': '*', 'NO_PROXY': '*', 'OMP_NUM_THREADS': '4'}
            log = open(workdir / 'stderr.log', 'wb')
            proc = subprocess.Popen([self.runtime['python'], '-m', 'metacoin_service.documents.extract_child', str(workdir)], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=log, env=env)
            deadline = time.time() + self.limits['document_child_wall_seconds']
            done, error, pages_done = None, None, 0
            import select
            while True:
                if time.time() > deadline:
                    proc.kill(); error = {'code': 'wall_timeout', 'reason': 'extraction exceeded %d s' % self.limits['document_child_wall_seconds']}; break
                if should_cancel():
                    proc.kill(); error = {'code': 'cancelled'}; break
                r, _, _ = select.select([proc.stdout], [], [], 0.5)
                if not r:
                    if proc.poll() is not None:
                        break
                    continue
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get('event') == 'progress':
                    self._set(imp['id'], state='extracting', stage='extracting', page_count=ev.get('pages'), progress_json=json.dumps({'pages_total': ev.get('pages'), 'pages_done': 0, 'active_content': ev.get('active_content')}))
                elif ev.get('event') == 'page':
                    pages_done += 1
                    self._set(imp['id'], progress_json=json.dumps({'pages_done': pages_done, 'last_method': ev.get('method'), 'last_excluded': ev.get('excluded')}))
                elif ev.get('event') == 'done':
                    done = ev
                elif ev.get('event') == 'error':
                    error = {'code': ev.get('code'), 'reason': ev.get('reason')}
            proc.wait(timeout=30); log.close()
            if error is None and done is None:
                error = {'code': 'child_exit', 'reason': 'extraction child exited with %s without a result' % proc.returncode}
            if error:
                if state['fenced']:
                    return 'fenced'
                st = 'cancelled' if error['code'] == 'cancelled' else 'failed'
                self._set(imp['id'], state=st, stage=st, error_json=json.dumps(error))
                return self.worker._finish(job, None, 'CANCELLED' if st == 'cancelled' else ('INPUT_INVALID' if error['code'] in ('malformed', 'not_a_pdf', 'page_limit', 'active_content_refused', 'encrypted', 'text_expansion_limit') else 'COMPUTATION_ERROR'))
            out = workdir / 'out'
            pages_blob = (out / 'pages.json').read_bytes(); text_blob = (out / 'normalized.txt').read_bytes(); tables_blob = (out / 'tables.json').read_bytes(); page_map = json.loads((out / 'page_map.json').read_text())
            pages_doc = json.loads(pages_blob); tables_doc = json.loads(tables_blob)
            previews = sorted(out.glob('previews/page-*.png'))
            return self._publish(job, contract, spec, principal, imp, attempt, done, pages_doc, pages_blob, text_blob, tables_doc, tables_blob, page_map, previews, state)
        except ServiceError as exc:
            if not state['fenced']:
                self._set(imp['id'], state='failed', stage='failed', error_json=json.dumps(exc.body())[:400])
            return self.worker._finish(job, None, 'COMPUTATION_ERROR')
        finally:
            shutil.rmtree(workdir, ignore_errors=True)          # task-owned directory only

    def _publish(self, job, contract, spec, principal, imp, attempt, done, pages_doc, pages_blob, text_blob, tables_doc, tables_blob, page_map, previews, state):
        L = self.limits
        exid = 'dx_' + secrets.token_hex(6)
        preview_recs = []
        with self.worker.db.tx() as db:
            if self.worker.models._fenced(db, job):
                return 'fenced'
            owner = imp['owner_id']; ws = job['workspace']
            pages_id = self.worker.store.store(db, workspace=ws, kind='document_pages', owner_id=owner, plaintext=pages_blob, recipients=[], intended_use='document-extraction-pages;owner-worker', job_id=job['id'], limit_bytes=L['document_max_text_chars'] * 8 + 10_000_000)
            text_id = self.worker.store.store(db, workspace=ws, kind='document_text', owner_id=owner, plaintext=text_blob, recipients=[], intended_use='document-normalized-text;owner-worker', job_id=job['id'], limit_bytes=L['document_max_text_chars'] * 4 + 1000)
            tables_id = self.worker.store.store(db, workspace=ws, kind='document_tables', owner_id=owner, plaintext=tables_blob, recipients=[], intended_use='document-table-candidates;owner-worker', job_id=job['id'])
            for p in previews:
                idx = int(p.stem.split('-')[1])
                aid = self.worker.store.store(db, workspace=ws, kind='document_page_image', owner_id=owner, plaintext=p.read_bytes(), recipients=[], intended_use='document-page-preview;owner-reviewer', job_id=job['id'], limit_bytes=8_000_000)
                preview_recs.append({'index': idx, 'artifact_id': aid})
            warnings = [{'page': pg['display_number'], 'warnings': pg['warnings']} for pg in pages_doc['pages'] if pg['warnings']]
            db.execute('INSERT INTO document_extractions (id, import_id, workspace, attempt, job_id, parser_json, policy_json, source_sha256, pages_artifact_id, text_artifact_id, tables_artifact_id, page_map_json, previews_json, page_count, excluded_pages, ocr_pages, table_count, warnings_json, active_content_json, created_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (exid, imp['id'], ws, attempt, job['id'], json.dumps(dict(pages_doc['parser'], implementation_digest=implementation_digest())), json.dumps(pages_doc['policy']), pages_doc['source_sha256'], pages_id, text_id, tables_id,
                        json.dumps(page_map['pages']), json.dumps(preview_recs), done['pages'], done['excluded_pages'], done['ocr_pages'], len(tables_doc['tables']), json.dumps(warnings), json.dumps(done.get('active_content') or []), now()))
            for k, t in enumerate(tables_doc['tables']):
                cells = t['n_rows'] * t['n_cols']
                if cells > L['document_max_table_cells']:
                    t['ambiguity'].append('table larger than %d cells: rows truncated in the candidate' % L['document_max_table_cells']); t['rows'] = t['rows'][:max(1, L['document_max_table_cells'] // max(t['n_cols'], 1))]
                db.execute('INSERT INTO document_tables (id, extraction_id, import_id, workspace, ordinal, page_index, method, n_rows, n_cols, header_row, region_json, table_json, table_sha256, ambiguity_json, supported_form, continuation_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('dt_' + secrets.token_hex(6), exid, imp['id'], ws, k, t['page_index'], t['method'], len(t['rows']), t['n_cols'], t['header_row'], json.dumps(t['region']), json.dumps(t['rows']), hashlib.sha256(merkle.canonical(t['rows'])).hexdigest(), json.dumps(t['ambiguity']), t['supported_form'],
                            json.dumps({'continued': True, 'pages': t.get('continuation_pages')}) if t.get('continued') else None, now()))
            review_needed = done['excluded_pages'] > 0 or bool(done.get('active_content'))
            st = 'awaiting_review' if review_needed else 'ready'
            publishing = st == 'ready' and bool(imp['collection_id'])
            # a collection-bound import becomes `ready` only together with its knowledge version (below): no window where the
            # state is terminal but the version id is still missing (found by journey 1 racing the CLI wait loop)
            db.execute("UPDATE document_imports SET state=?, stage=?, extraction_id=?, page_count=?, progress_json=?, updated_at=? WHERE id=?", ('extracting' if publishing else st, 'publishing' if publishing else 'extracted', exid, done['pages'], json.dumps({'pages_done': done['pages'], 'excluded': done['excluded_pages'], 'ocr_pages': done['ocr_pages'], 'tables': len(tables_doc['tables'])}), now(), imp['id']))
            history.record(db, ws, self.worker.worker_id, 'document.extracted', 'document_import', imp['id'], {'extraction_id': exid, 'pages': done['pages'], 'excluded': done['excluded_pages'], 'ocr_pages': done['ocr_pages'], 'tables': len(tables_doc['tables']), 'parser': pages_doc['parser']['id']})
        # publication to the collection outside the fenced write (it is an ordinary authorized operation on the extraction revision)
        if publishing:
            docs = self.worker.documents_service()                 # constructed outside the transaction (its own initialization opens one)
            try:
                with self.worker.db.tx() as db:
                    imp2 = db.execute('SELECT * FROM document_imports WHERE id=?', (imp['id'],)).fetchone()
                    if imp2['stage'] == 'publishing' and imp2['collection_id']:
                        docs._publish(db, principal, dict(imp2, state='ready'), False)
            except Exception as exc:
                with self.worker.db.tx() as db:
                    db.execute("UPDATE document_imports SET state='awaiting_review', stage='publish_failed', error_json=?, updated_at=? WHERE id=? AND stage='publishing'", (json.dumps({'code': 'publish_failed', 'reason': type(exc).__name__}), now(), imp['id']))
                st = 'awaiting_review'
        summary = {'import_id': imp['id'], 'extraction_id': exid, 'pages': done['pages'], 'excluded_pages': done['excluded_pages'], 'ocr_pages': done['ocr_pages'], 'tables': len(tables_doc['tables']), 'chars': done['chars'], 'ms': done['ms'], 'state': st,
                   'parser': pages_doc['parser'], 'pages_sha256': hashlib.sha256(pages_blob).hexdigest(), 'text_sha256': hashlib.sha256(text_blob).hexdigest(), 'tables_sha256': hashlib.sha256(tables_blob).hexdigest(), 'implementation_digest': implementation_digest()}
        evidence = {'contract_digest': spec['contract_digest'], 'input_root': spec['input_root'], 'verifier_id': 'document-extractor/v1', 'verifier_digest': implementation_digest(), 'result_schema': 'document-import-result/v1', 'model_id': 'document-import/v1',
                    'result': exactable(summary), 'output_commitments': {'pages.json': summary['pages_sha256'], 'normalized.txt': summary['text_sha256'], 'tables.json': summary['tables_sha256']}, 'scope': 'local-document-extraction'}
        _, vault = merkle.commit(evidence)
        return self.worker._finish(job, {'evidence_vault': vault, 'outcome': 'EXTRACTED' if st == 'ready' else 'EXTRACTED_REVIEW_NEEDED', 'summary': summary}, None)
