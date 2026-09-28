"""Synthetic, distributable document fixtures with an INDEPENDENT known-answer manifest (written from the generator's own
content, never from parser output). Run under the compute interpreter (reportlab, PIL, pdftoppm):

    python -m metacoin_service.documents.fixtures <outdir>

Development set: report (normal technical report), multicolumn, table-units, scanned (image-only page), mixed
(native + scanned page), rotated, unicode, repeated-headers (multi-page table with identical headers), locale-ambiguous
(1,250 style numbers), malformed (not a PDF). Held-out set: three layout variations produced by the same generator with
different content, used only for the final evaluation."""
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

W, H = letter


def report_pdf(path, title, paragraphs, table=None, table_title=None):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle(title); c.setFont('Helvetica-Bold', 14); c.drawString(72, H - 72, title)
    y = H - 100; c.setFont('Helvetica', 10)
    for para in paragraphs:
        for line in wrap(para, 90):
            c.drawString(72, y, line); y -= 14
        y -= 8
    if table:
        y -= 6; c.setFont('Helvetica-Bold', 10); c.drawString(72, y, table_title or 'Table 1'); y -= 16
        xs = [72 + k * 110 for k in range(len(table[0]))]
        for r, row in enumerate(table):
            c.setFont('Helvetica-Bold' if r == 0 else 'Helvetica', 10)
            for k, cell in enumerate(row):
                c.drawString(xs[k], y, cell)
            y -= 14
    c.showPage(); c.save()


def wrap(text, n):
    words, out, cur = text.split(), [], ''
    for w in words:
        if len(cur) + len(w) + 1 > n:
            out.append(cur); cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        out.append(cur)
    return out


def image_pdf(path, src_pdf, page_index, dpi=150):
    """Render a page with pdftoppm and embed the bitmap as an image-only page (a 'scanned' page: no text layer)."""
    with tempfile.TemporaryDirectory() as td:
        prefix = os.path.join(td, 'p')
        subprocess.run(['pdftoppm', '-r', str(dpi), '-f', str(page_index + 1), '-l', str(page_index + 1), '-png', '-singlefile', str(src_pdf), prefix], check=True, capture_output=True)
        c = canvas.Canvas(str(path), pagesize=letter)
        c.drawImage(prefix + '.png', 0, 0, width=W, height=H)
        c.showPage(); c.save()


def concat(paths, out):
    from pypdf import PdfWriter
    w = PdfWriter()
    for p in paths:
        w.append(str(p))
    w.write(str(out)); w.close()


def main(outdir):
    out = Path(outdir); (out / 'dev').mkdir(parents=True, exist_ok=True); (out / 'heldout').mkdir(parents=True, exist_ok=True)
    manifest = {'schema': 'metacoin-document-fixtures/v1', 'generator': 'metacoin_service.documents.fixtures (reportlab %s)' % __import__('reportlab').Version, 'dev': {}, 'heldout': {}}
    # 1. normal technical report with a units table
    table = [['segment', 'duration_s', 'power_mW'], ['eclipse', '1800', '450'], ['sunlit', '3600', '120'], ['downlink', '600', '980']]
    report_pdf(out / 'dev' / 'report.pdf', 'Thermal-power budget of the demonstration node',
               ['The demonstration node stores 12000 mJ of usable energy and keeps a reserve of 2000 mJ at every boundary. Load segments are declared as intervals; supply bounds come from the panel datasheet.',
                'Measured decode energy per token was 0.62 mJ on the reference device. The temperature of the radiator plate must stay below 45 °C during downlink.',
                'Table 1 lists the declared segments used by the temporal analysis.'], table, 'Table 1. Declared segments')
    manifest['dev']['report.pdf'] = {'pages': 1, 'method': ['native'], 'passages': [{'page': 1, 'text': 'keeps a reserve of 2000 mJ'}, {'page': 1, 'text': '0.62 mJ'}], 'tables': [{'page': 1, 'header': table[0], 'rows': len(table) - 1, 'cell': {'row': 2, 'col': 2, 'value': '120'}}], 'expected_import': 'ready'}
    # 2. multi-column page
    c = canvas.Canvas(str(out / 'dev' / 'multicolumn.pdf'), pagesize=letter); c.setFont('Helvetica', 10)
    left = ['Left column starts here.', 'Heat diffusion on a 128 by 128 grid uses a stability limit of 0.25.', 'The solver checks the condition every step.']
    right = ['Right column starts here.', 'Monte Carlo reliability uses 50000 samples per candidate.', 'The interval is a Wilson score interval.']
    for i, line in enumerate(left):
        c.drawString(60, H - 80 - 14 * i, line)
    for i, line in enumerate(right):
        c.drawString(320, H - 80 - 14 * i, line)
    c.showPage(); c.save()
    manifest['dev']['multicolumn.pdf'] = {'pages': 1, 'method': ['native'], 'passages': [{'page': 1, 'text': 'stability limit of 0.25'}, {'page': 1, 'text': 'Wilson score interval'}], 'expected_import': 'ready',
                                          'note': 'both columns must be present; column order is not asserted'}
    # 3. scanned page (image only) from a rendered native page with a number field
    native_tmp = out / 'dev' / '_scan_source.pdf'
    report_pdf(native_tmp, 'Scanned calibration sheet', ['Reference resistor value: 4700 ohm. Serial number SN-2291. Ambient temperature 21.5 C.', 'This page is distributed only as a bitmap.'])
    image_pdf(out / 'dev' / 'scanned.pdf', native_tmp, 0); native_tmp.unlink()
    manifest['dev']['scanned.pdf'] = {'pages': 1, 'method': ['ocr'], 'fields': [{'page': 1, 'name': 'resistor_ohm', 'value': '4700'}, {'page': 1, 'name': 'serial', 'value': 'SN-2291'}], 'expected_import': 'ready', 'ocr_tolerance': 'field-level exact match; character error rate reported'}
    # 4. mixed: native page + scanned page
    native_p1 = out / 'dev' / '_mixed_p1.pdf'; report_pdf(native_p1, 'Mixed document, page one (native)', ['Native page. Battery capacity is 12000 mJ. The scanned appendix follows.'])
    native_p2 = out / 'dev' / '_mixed_p2.pdf'; report_pdf(native_p2, 'Appendix (scanned)', ['Appendix value: leakage 15 mW measured at 300 K.'])
    scan_p2 = out / 'dev' / '_mixed_p2_scan.pdf'; image_pdf(scan_p2, native_p2, 0)
    concat([native_p1, scan_p2], out / 'dev' / 'mixed.pdf'); native_p1.unlink(); native_p2.unlink(); scan_p2.unlink()
    manifest['dev']['mixed.pdf'] = {'pages': 2, 'method': ['native', 'ocr'], 'passages': [{'page': 1, 'text': 'capacity is 12000 mJ'}], 'fields': [{'page': 2, 'name': 'leakage_mW', 'value': '15'}], 'expected_import': 'ready'}
    # 5. rotated page
    c = canvas.Canvas(str(out / 'dev' / 'rotated.pdf'), pagesize=letter); c.setFont('Helvetica', 11); c.drawString(72, H - 72, 'Rotated page: the panel delivers 640 mW at noon.'); c.showPage()
    c.save()
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(str(out / 'dev' / 'rotated.pdf')); w = PdfWriter(); p = r.pages[0]; p.rotate(90); w.add_page(p); w.write(str(out / 'dev' / 'rotated.pdf')); w.close()
    manifest['dev']['rotated.pdf'] = {'pages': 1, 'method': ['native'], 'rotation': 90, 'passages': [{'page': 1, 'text': '640 mW at noon'}], 'expected_import': 'ready'}
    # 6. unicode symbols
    report_pdf(out / 'dev' / 'unicode.pdf', 'Unicode symbols', ['Temperature −40 °C to +85 °C; efficiency η = 0.92; ΔE = 12 mJ; résumé of the ﬁrst run.'])
    manifest['dev']['unicode.pdf'] = {'pages': 1, 'method': ['native'], 'passages': [{'page': 1, 'text': '−40 °C'}, {'page': 1, 'text': 'ΔE = 12 mJ'}], 'expected_import': 'ready', 'note': 'the minus sign U+2212 must survive; ligature ﬁ may be normalized to fi in retrieval text only'}
    # 7. repeated headers: a table continued over two pages with identical header rows
    rows1 = [['slot', 'supply_mW', 'demand_mW']] + [[str(i), str(100 + i), str(80 + 2 * i)] for i in range(1, 9)]
    rows2 = [['slot', 'supply_mW', 'demand_mW']] + [[str(i), str(100 + i), str(80 + 2 * i)] for i in range(9, 15)]
    p1 = out / 'dev' / '_rh1.pdf'; report_pdf(p1, 'Slot table, part 1', ['Slots 1 to 8.'], rows1, 'Table 2. Slot supply and demand')
    p2 = out / 'dev' / '_rh2.pdf'; report_pdf(p2, 'Slot table, part 2', ['Slots 9 to 14 continue the table.'], rows2, 'Table 2 (continued)')
    concat([p1, p2], out / 'dev' / 'repeated-headers.pdf'); p1.unlink(); p2.unlink()
    manifest['dev']['repeated-headers.pdf'] = {'pages': 2, 'method': ['native', 'native'], 'tables': [{'page': 1, 'header': rows1[0], 'rows': 14, 'continued': True, 'cell': {'row': 14, 'col': 1, 'value': '114'}}], 'expected_import': 'ready'}
    # 8. ambiguous locale: 1,250 could be 1250 or 1.25
    table = [['item', 'energy', 'unit'], ['pack A', '1,250', 'mJ'], ['pack B', '2,5', 'J'], ['pack C', '3.75', 'J']]
    report_pdf(out / 'dev' / 'locale-ambiguous.pdf', 'Energy per pack (locale not declared)', ['Values were transcribed from a supplier sheet with an undeclared decimal convention.'], table, 'Table 3. Pack energy')
    manifest['dev']['locale-ambiguous.pdf'] = {'pages': 1, 'method': ['native'], 'tables': [{'page': 1, 'header': table[0], 'rows': 3}], 'expected_import': 'ready', 'conversion': 'must wait for a declared locale: 1,250 is ambiguous'}
    # 9. malformed
    (out / 'dev' / 'malformed.pdf').write_bytes(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\nthis is not a pdf body\n%%EOF\n')
    manifest['dev']['malformed.pdf'] = {'expected_import': 'failed', 'reason_contains': 'malformed'}
    (out / 'dev' / 'not-a-pdf.pdf').write_bytes(b'PK\x03\x04 zip disguised as pdf')
    manifest['dev']['not-a-pdf.pdf'] = {'expected_import': 'failed', 'reason_contains': 'not a pdf'}
    # held-out: three variations (different content, same layouts family)
    table = [['phase', 'duration_s', 'power_mW', 'temp_C'], ['warmup', '120', '300', '21.5'], ['cruise', '5400', '90', '18.0'], ['burst', '45', '1400', '32.5']]
    report_pdf(out / 'heldout' / 'h1-report.pdf', 'Held-out report one', ['Held-out reserve requirement is 3500 mJ at every boundary. Capacity 20000 mJ.'], table, 'Table H1. Phases')
    manifest['heldout']['h1-report.pdf'] = {'pages': 1, 'method': ['native'], 'passages': [{'page': 1, 'text': 'reserve requirement is 3500 mJ'}], 'tables': [{'page': 1, 'header': table[0], 'rows': 3, 'cell': {'row': 3, 'col': 2, 'value': '1400'}}], 'expected_import': 'ready'}
    n1 = out / 'heldout' / '_h2.pdf'; report_pdf(n1, 'Held-out scanned two', ['Reference capacitor 220 uF. Batch code BX-77. Ambient 19.0 C.'])
    image_pdf(out / 'heldout' / 'h2-scanned.pdf', n1, 0); n1.unlink()
    manifest['heldout']['h2-scanned.pdf'] = {'pages': 1, 'method': ['ocr'], 'fields': [{'page': 1, 'name': 'capacitor_uF', 'value': '220'}, {'page': 1, 'name': 'batch', 'value': 'BX-77'}], 'expected_import': 'ready'}
    rows1 = [['run', 'energy_mJ', 'time_s']] + [[str(i), str(1000 + 7 * i), str(30 + i)] for i in range(1, 7)]
    rows2 = [['run', 'energy_mJ', 'time_s']] + [[str(i), str(1000 + 7 * i), str(30 + i)] for i in range(7, 11)]
    p1 = out / 'heldout' / '_h3a.pdf'; report_pdf(p1, 'Held-out runs part 1', ['Runs one to six.'], rows1, 'Table H3')
    p2 = out / 'heldout' / '_h3b.pdf'; report_pdf(p2, 'Held-out runs part 2', ['Runs seven to ten.'], rows2, 'Table H3 (continued)')
    concat([p1, p2], out / 'heldout' / 'h3-continued.pdf'); p1.unlink(); p2.unlink()
    manifest['heldout']['h3-continued.pdf'] = {'pages': 2, 'method': ['native', 'native'], 'tables': [{'page': 1, 'header': rows1[0], 'rows': 10, 'continued': True, 'cell': {'row': 10, 'col': 1, 'value': '1070'}}], 'expected_import': 'ready'}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    print(json.dumps({'dev': sorted(manifest['dev']), 'heldout': sorted(manifest['heldout'])}))


if __name__ == '__main__':
    main(sys.argv[1])
