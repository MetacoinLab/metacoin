"""Bounded extraction child (runs under the compute interpreter, never inside the API process).

    python -m metacoin_service.documents.extract_child <workdir>

workdir/spec.json: {"policy": {"mode": native|ocr_needed|ocr_forced, "ocr_language": "en"}, "limits": {...}, "source_sha256": "...", "workdir": ...}
workdir/source.pdf: the bytes to parse (written by the worker after decryption; removed by the worker afterwards).
stdout: JSON lines {"event": "progress"|"page"|"done"|"error", ...}. The final "done" event names the artifacts written under
workdir/out/: pages.json (raw per-page text, spans with geometry, method, rotation, dimensions, warnings, OCR diagnostics),
normalized.txt (retrieval text with page boundaries), page_map.json, tables.json, previews/page-<n>.png.

What this is: pypdf (pure Python, BSD) for native text and structure, poppler's pdftoppm as a separate process for
rendering, RapidOCR (ONNX, CPU) for recognition; a process boundary with CPU/address-space limits and no network
environment. What it is not: a hardened sandbox for hostile PDFs. Embedded JavaScript, launch/URI actions and external
references are never executed: they are detected from the catalog and reported (policy may refuse the file)."""
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

PARSER_ID = 'metacoin-pdf-extractor/v1'
LIMITS = {'max_pages': 60, 'max_text_chars': 2_000_000, 'preview_dpi': 60, 'ocr_dpi': 200, 'max_pixels': 12_000_000, 'min_native_chars_per_page': 20,
          'max_tables_per_page': 6, 'ocr_min_score': 0.0}


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n'); sys.stdout.flush()


def active_content(reader):
    """Names of active features found in the catalog/pages (never executed)."""
    found = set()
    try:
        root = reader.trailer['/Root']
        for key in ('/OpenAction', '/AA', '/Names', '/AcroForm'):
            if key in root:
                obj = root[key]
                try:
                    obj = obj.get_object()
                except Exception:
                    pass
                s = str(obj)[:2000]
                if '/JavaScript' in s or '/JS' in s:
                    found.add('javascript')
                if key == '/OpenAction':
                    found.add('open_action')
                if '/Launch' in s:
                    found.add('launch_action')
    except Exception:
        pass
    for i, page in enumerate(reader.pages):
        try:
            annots = page.get('/Annots') or []
            for a in annots:
                s = str(a.get_object())[:600]
                if '/Launch' in s:
                    found.add('launch_action')
                if '/URI' in s:
                    found.add('uri_link')
                if '/JavaScript' in s or '/JS' in s:
                    found.add('javascript')
        except Exception:
            continue
        if i >= 200:
            break
    return sorted(found)


def native_spans(page):
    """Text pieces with positions from pypdf's visitor: [{text, x, y}], plus the joined raw text."""
    spans = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text and text.strip():
            x = float(tm[4]) if tm else 0.0; y = float(tm[5]) if tm else 0.0
            spans.append({'text': text, 'x': round(x, 2), 'y': round(y, 2), 'size': float(font_size or 0)})
    try:
        raw = page.extract_text(visitor_text=visitor) or ''
    except Exception as exc:
        return None, [], 'native_extract_error:' + type(exc).__name__
    return raw, spans, None


def lines_from_spans(spans, tol=2.5):
    """Group spans into lines by baseline y (descending = top first), sorted by x within a line."""
    rows = []
    for s in sorted(spans, key=lambda s: (-s['y'], s['x'])):
        if rows and abs(rows[-1]['y'] - s['y']) <= tol:
            rows[-1]['spans'].append(s)
        else:
            rows.append({'y': s['y'], 'spans': [s]})
    for r in rows:
        r['spans'].sort(key=lambda s: s['x'])
        r['text'] = ' '.join(s['text'].strip() for s in r['spans'] if s['text'].strip())
        r['x0'] = min(s['x'] for s in r['spans']); r['x1'] = max(s['x'] for s in r['spans'])
    return rows


NUM_RE = re.compile(r'^[\-−+]?\d[\d.,  ]*(?:[eE][\-+]?\d+)?%?$|^[—–-]$|^n/?a$', re.I)


def cells_of_line(line, gap):
    """Split a line's spans into cells at horizontal gaps wider than `gap`."""
    cells, cur = [], None
    for s in line['spans']:
        t = s['text'].strip()
        if not t:
            continue
        width = max(len(t) * s['size'] * 0.5, 1.0)
        if cur is not None and s['x'] - cur['x1'] > gap:
            cells.append(cur); cur = None
        if cur is None:
            cur = {'text': t, 'x0': s['x'], 'x1': s['x'] + width}
        else:
            cur['text'] += ' ' + t; cur['x1'] = s['x'] + width
    if cur is not None:
        cells.append(cur)
    return cells


def detect_tables(lines, page_index, method, max_tables):
    """Regular rectangular tables: >= 3 consecutive lines with the same cell count >= 2 whose column starts align.
    Header = first row when it is mostly non-numeric. Anything else is not claimed as a table."""
    tables = []
    i = 0
    while i < len(lines) and len(tables) < max_tables:
        cells = cells_of_line(lines[i], gap=12.0)
        n = len(cells)
        if n < 2:
            i += 1; continue
        block = [(lines[i], cells)]
        j = i + 1
        while j < len(lines):
            c2 = cells_of_line(lines[j], gap=12.0)
            if len(c2) != n:
                break
            # column alignment: each cell start within 25 units of the block's column starts
            starts = [c['x0'] for c in block[0][1]]
            if any(abs(c2[k]['x0'] - starts[k]) > 25 for k in range(n)):
                break
            block.append((lines[j], c2)); j += 1
        if len(block) >= 3:
            rows = [[c['text'] for c in cells] for (_, cells) in block]
            first_numeric = sum(1 for c in rows[0] if NUM_RE.match(c.replace(' ', '')))
            header = 0 if first_numeric <= n // 2 else None
            numeric_cols = [k for k in range(n) if all(NUM_RE.match(r[k].replace(' ', '')) for r in rows[(1 if header == 0 else 0):])]
            x0 = min(c['x0'] for (_, cs) in block for c in cs); x1 = max(c['x1'] for (_, cs) in block for c in cs)
            y_top = block[0][0]['y']; y_bot = block[-1][0]['y']
            ambiguity = []
            if header is None:
                ambiguity.append('no header row recognized (first row is numeric)')
            if any(len(set(len(r) for r in rows)) != 1 for _ in [0]):
                ambiguity.append('ragged rows')
            tables.append({'page_index': page_index, 'method': method, 'rows': rows, 'n_rows': len(rows), 'n_cols': n, 'header_row': header, 'numeric_columns': numeric_cols,
                           'region': {'x0': round(x0, 1), 'y0': round(y_bot, 1), 'x1': round(x1, 1), 'y1': round(y_top, 1), 'space': 'pdf-user-units, origin bottom-left'} if method == 'native' else {'x0': round(x0, 1), 'y0': round(y_top, 1), 'x1': round(x1, 1), 'y1': round(y_bot, 1), 'space': 'image-pixels, origin top-left'},
                           'ambiguity': ambiguity, 'supported_form': 'regular rectangular table with explicit header' if header == 0 and not ambiguity else 'candidate only (see ambiguity)'})
            i = j
        else:
            i += 1
    return tables


def render_page(pdf_path, page_index, dpi, out_png_prefix, max_pixels, width_pt, height_pt):
    """pdftoppm as a separate process; dpi reduced so the decoded image stays under max_pixels."""
    px = (width_pt / 72.0 * dpi) * (height_pt / 72.0 * dpi)
    if px > max_pixels:
        dpi = int(dpi * (max_pixels / px) ** 0.5)
    cmd = ['pdftoppm', '-r', str(max(dpi, 20)), '-f', str(page_index + 1), '-l', str(page_index + 1), '-png', '-singlefile', pdf_path, out_png_prefix]
    p = subprocess.run(cmd, capture_output=True, timeout=120, env={'PATH': os.environ.get('PATH', '')})
    if p.returncode != 0:
        return None, dpi, 'render_failed:' + p.stderr.decode(errors='replace')[:120]
    return out_png_prefix + '.png', dpi, None


def ocr_page(png_path, engine):
    result, elapse = engine(png_path)
    boxes = []
    for item in (result or []):
        box, text, score = item[0], item[1], float(item[2])
        xs = [pt[0] for pt in box]; ys = [pt[1] for pt in box]
        boxes.append({'text': text, 'score': round(score, 4), 'x0': round(min(xs), 1), 'y0': round(min(ys), 1), 'x1': round(max(xs), 1), 'y1': round(max(ys), 1)})
    return boxes, elapse


def ocr_lines(boxes, tol=12.0):
    rows = []
    for b in sorted(boxes, key=lambda b: ((b['y0'] + b['y1']) / 2, b['x0'])):
        yc = (b['y0'] + b['y1']) / 2
        if rows and abs(rows[-1]['y'] - yc) <= tol:
            rows[-1]['spans'].append({'text': b['text'], 'x': b['x0'], 'y': yc, 'size': max(b['y1'] - b['y0'], 8.0) * 0.6, 'score': b['score'], 'x1': b['x1']})
        else:
            rows.append({'y': yc, 'spans': [{'text': b['text'], 'x': b['x0'], 'y': yc, 'size': max(b['y1'] - b['y0'], 8.0) * 0.6, 'score': b['score'], 'x1': b['x1']}]})
    for r in rows:
        r['spans'].sort(key=lambda s: s['x']); r['text'] = ' '.join(s['text'] for s in r['spans']); r['x0'] = min(s['x'] for s in r['spans']); r['x1'] = max(s['x1'] for s in r['spans'])
    return rows


def normalize_page_text(raw):
    """Retrieval text: fix hyphenated line breaks and collapse whitespace; the RAW text is kept separately. Minus signs and
    digits are never altered (quote verification binds to whichever representation the citation names)."""
    t = raw.replace('\r\n', '\n').replace('\r', '\n')
    t = re.sub(r'(\w)-\n(\w)', r'\1\2', t)                     # hyphenated line break (recorded as a normalization, not applied to numbers: \w excludes '-')
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


def main():
    workdir = Path(sys.argv[1])
    spec = json.loads((workdir / 'spec.json').read_text())
    limits = dict(LIMITS, **(spec.get('limits') or {}))
    policy = spec.get('policy') or {}
    mode = policy.get('mode', 'ocr_needed')
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (int(limits.get('cpu_seconds', 600)), int(limits.get('cpu_seconds', 600)) + 30))
        resource.setrlimit(resource.RLIMIT_AS, (int(limits.get('address_space_bytes', 32 * 1024 ** 3)),) * 2)     # virtual address space: onnxruntime/numpy map far more than they touch
    except Exception:
        pass
    out = workdir / 'out'; (out / 'previews').mkdir(parents=True, exist_ok=True)
    src = workdir / 'source.pdf'
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec.get('source_sha256'):
        emit({'event': 'error', 'code': 'source_digest_mismatch'}); return 2
    if not data.startswith(b'%PDF-'):
        emit({'event': 'error', 'code': 'not_a_pdf', 'reason': 'declared pdf but the file does not start with %PDF-'}); return 2
    import pypdf
    t0 = time.time()
    try:
        reader = pypdf.PdfReader(str(src))
        if reader.is_encrypted:
            try:
                reader.decrypt('')
            except Exception:
                emit({'event': 'error', 'code': 'encrypted', 'reason': 'password-protected PDFs are not supported'}); return 2
        n_pages = len(reader.pages)
    except Exception as exc:
        emit({'event': 'error', 'code': 'malformed', 'reason': 'pypdf could not open the file: ' + type(exc).__name__}); return 2
    if n_pages == 0 or n_pages > int(limits['max_pages']):
        emit({'event': 'error', 'code': 'page_limit', 'reason': '%d pages (limit %d)' % (n_pages, limits['max_pages'])}); return 2
    active = active_content(reader)
    if active and policy.get('refuse_active_content', True) and any(a in ('javascript', 'launch_action') for a in active):
        emit({'event': 'error', 'code': 'active_content_refused', 'reason': 'embedded ' + ', '.join(active) + ' (never executed; refused by policy)'}); return 2
    emit({'event': 'progress', 'stage': 'validated', 'pages': n_pages, 'active_content': active, 'parser': {'id': PARSER_ID, 'pypdf': pypdf.__version__}})
    engine = None
    pages, normalized_parts, page_map, tables, total_chars = [], [], [], [], 0
    prev_table = None
    for i, page in enumerate(reader.pages):
        try:
            box = page.mediabox; width, height = float(box.width), float(box.height)
        except Exception:
            width = height = 0.0
        rotation = int(page.get('/Rotate') or 0) % 360
        raw, spans, err = native_spans(page)
        warnings = [] if err is None else [err]
        native_chars = len(re.sub(r'\s', '', raw or ''))
        needs_ocr = native_chars < int(limits['min_native_chars_per_page'])
        method, ocr_info, lines = 'native', None, lines_from_spans(spans)
        preview, pdpi, perr = render_page(str(src), i, int(limits['preview_dpi']), str(out / 'previews' / ('page-%d' % i)), int(limits['max_pixels']), width or 612, height or 792)
        if perr:
            warnings.append(perr)
        do_ocr = (mode == 'ocr_forced') or (mode == 'ocr_needed' and needs_ocr)
        if needs_ocr and mode == 'native':
            warnings.append('no adequate native text (%d chars) and OCR disabled by policy: page excluded from retrieval text' % native_chars)
        if do_ocr:
            if engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    engine = RapidOCR()
                    import rapidocr_onnxruntime as ro
                    ocr_version = getattr(ro, '__version__', '1.4.4')
                except Exception as exc:
                    engine = False; warnings.append('ocr_unavailable:' + type(exc).__name__)
            if engine:
                png, odpi, oerr = render_page(str(src), i, int(limits['ocr_dpi']), str(workdir / ('ocr-%d' % i)), int(limits['max_pixels']), width or 612, height or 792)
                if oerr:
                    warnings.append(oerr)
                else:
                    t1 = time.time()
                    boxes, elapse = ocr_page(png, engine)
                    ocr_text = '\n'.join(l['text'] for l in ocr_lines(boxes))
                    ocr_info = {'engine': 'rapidocr_onnxruntime', 'engine_version': ocr_version, 'models': 'bundled PP-OCR detection/recognition ONNX', 'language': policy.get('ocr_language', 'en'), 'dpi': odpi,
                                'boxes': len(boxes), 'mean_score': round(sum(b['score'] for b in boxes) / len(boxes), 4) if boxes else None, 'min_score': round(min(b['score'] for b in boxes), 4) if boxes else None,
                                'ms': int((time.time() - t1) * 1000), 'preprocessing': 'pdftoppm render at %d dpi, no binarization' % odpi, 'confidence_meaning': 'engine diagnostic per text box; not a probability that the statement is correct'}
                    if needs_ocr or mode == 'ocr_forced':
                        method = 'ocr'; raw = ocr_text; spans = [{'text': b['text'], 'x': b['x0'], 'y': -b['y0'], 'size': max(b['y1'] - b['y0'], 8.0) * 0.6, 'score': b['score']} for b in boxes]
                        lines = ocr_lines(boxes)
                    try:
                        os.remove(png)
                    except OSError:
                        pass
        normalized = normalize_page_text(raw or '')
        excluded = not normalized.strip()
        if excluded:
            warnings.append('no extractable text on this page' + (' (OCR found nothing)' if do_ocr and engine else ''))
        total_chars += len(normalized)
        if total_chars > int(limits['max_text_chars']):
            emit({'event': 'error', 'code': 'text_expansion_limit', 'reason': 'normalized text exceeds %d chars' % limits['max_text_chars']}); return 2
        page_tables = detect_tables(lines, i, method, int(limits['max_tables_per_page'])) if lines else []
        # bounded multi-page continuation: a table whose header equals the previous page's last table header continues it
        for t in page_tables:
            if prev_table is not None and prev_table['page_index'] == i - 1 and t['header_row'] == 0 and prev_table['header_row'] == 0 and t['rows'][0] == prev_table['rows'][0] and t['n_cols'] == prev_table['n_cols']:
                prev_table['continuation_pages'] = prev_table.get('continuation_pages', []) + [i]; prev_table['rows'] += t['rows'][1:]; prev_table['n_rows'] = len(prev_table['rows']); prev_table['continued'] = True
                t['merged_into_previous'] = True
            elif prev_table is not None and prev_table['page_index'] == i - 1 and t['header_row'] == 0 and prev_table['header_row'] == 0 and t['n_cols'] == prev_table['n_cols'] and t['rows'][0] != prev_table['rows'][0]:
                t['ambiguity'].append('possible continuation of the previous page\'s table but the header differs: not merged')
        page_tables = [t for t in page_tables if not t.get('merged_into_previous')]
        tables += page_tables
        prev_table = page_tables[-1] if page_tables else (prev_table if not page_tables and prev_table and prev_table['page_index'] == i - 1 and False else None)
        pages.append({'index': i, 'display_number': i + 1, 'width': width, 'height': height, 'rotation': rotation, 'method': method, 'native_chars': native_chars, 'needs_ocr': needs_ocr, 'excluded': excluded,
                      'raw_text': raw or '', 'normalized_text': normalized, 'spans': spans[:4000], 'geometry': 'span baseline origin (x, y) in PDF user units, origin bottom-left' if method == 'native' else 'OCR box (x0,y0,x1,y1) in image pixels at the recorded dpi, origin top-left (spans carry y negated for ordering)',
                      'ocr': ocr_info, 'preview': ('previews/page-%d.png' % i) if preview else None, 'preview_dpi': pdpi, 'warnings': warnings, 'sha256_raw': hashlib.sha256((raw or '').encode()).hexdigest()})
        start = sum(len(p) for p in normalized_parts)
        part = ('' if not normalized_parts else '\n\n') + ('[page %d]\n' % (i + 1)) + normalized + '\n'
        normalized_parts.append(part)
        page_map.append({'index': i, 'start_byte': len(''.join(normalized_parts[:-1]).encode('utf-8')), 'end_byte': len(''.join(normalized_parts).encode('utf-8')), 'excluded': excluded})
        emit({'event': 'page', 'index': i, 'method': method, 'excluded': excluded, 'tables': len(page_tables), 'warnings': len(warnings)})
    text = ''.join(normalized_parts)
    (out / 'pages.json').write_text(json.dumps({'schema': 'metacoin-pdf-pages/v1', 'parser': {'id': PARSER_ID, 'pypdf': pypdf.__version__, 'renderer': 'pdftoppm (poppler) separate process', 'ocr': ('rapidocr_onnxruntime' if engine else None)},
                                                  'policy': policy, 'limits': limits, 'source_sha256': digest, 'page_count': n_pages, 'pages': pages, 'active_content': active,
                                                  'indexing_convention': 'index is zero-based and internal; display_number = index + 1 is what humans see'}, ensure_ascii=False))
    (out / 'normalized.txt').write_text(text)
    (out / 'page_map.json').write_text(json.dumps({'schema': 'metacoin-pdf-page-map/v1', 'pages': page_map, 'representation': 'normalized retrieval text; byte offsets into normalized.txt (UTF-8); each page starts with a "[page N]" marker line'}))
    (out / 'tables.json').write_text(json.dumps({'schema': 'metacoin-pdf-tables/v1', 'tables': tables, 'supported_forms': ['regular rectangular table with explicit header (native)', 'bounded multi-page continuation with identical header', 'reviewed OCR table'],
                                                   'note': 'candidates only; interpretation (locale, units, missing values) is a separate reviewed mapping'}, ensure_ascii=False))
    emit({'event': 'done', 'pages': n_pages, 'excluded_pages': sum(1 for p in pages if p['excluded']), 'ocr_pages': sum(1 for p in pages if p['method'] == 'ocr'), 'tables': len(tables), 'chars': len(text), 'ms': int((time.time() - t0) * 1000),
          'parser': {'id': PARSER_ID, 'pypdf': pypdf.__version__, 'ocr': ('rapidocr_onnxruntime ' + (ocr_version if engine else '')) if engine else None}, 'active_content': active})
    return 0


if __name__ == '__main__':
    sys.exit(main())
