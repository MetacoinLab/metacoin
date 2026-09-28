"""Bounded local PDF renderer for evidence-linked reports (runs under the compute interpreter, which carries reportlab).
Reads {title, markdown, metadata, limits} as JSON on stdin and writes the PDF bytes to stdout. Renders the Markdown
subset the report writer emits (headings, paragraphs, bullet lists, block quotes, pipe tables, inline code/bold). Metadata
carries only what the caller passes (identifiers and the disclosure scope): no user names, paths or private labels."""
import json
import re
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    spec = json.loads(sys.stdin.buffer.read())
    md = spec['markdown'][:spec.get('limits', {}).get('max_chars', 400_000)]
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
    from xml.sax.saxutils import escape
    import io
    buf = io.BytesIO()
    meta = spec.get('metadata') or {}
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm, title=meta.get('Title', spec['title']), subject=meta.get('Subject', ''), author='', creator='metacoin report renderer', keywords=meta.get('Keywords', ''))
    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['BodyText'], fontSize=9.5, leading=12.5)
    mono = ParagraphStyle('mono', parent=body, fontName='Courier', fontSize=8.5, leading=11)
    quote = ParagraphStyle('quote', parent=body, leftIndent=12, textColor=colors.HexColor('#444444'))
    cell = ParagraphStyle('cell', parent=body, fontSize=8, leading=10)
    def inline(s):
        s = escape(s)
        s = re.sub(r'`([^`]+)`', r'<font face="Courier">\1</font>', s)
        s = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)
        s = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'<i>\1</i>', s)
        return s
    story = []
    table_rows = []
    def flush_table():
        nonlocal table_rows
        if not table_rows:
            return
        data = [[Paragraph(inline(c), cell) for c in row] for row in table_rows]
        ncol = max(len(r) for r in data)
        data = [r + [Paragraph('', cell)] * (ncol - len(r)) for r in data]
        width = (A4[0] - 36 * mm) / max(ncol, 1)
        t = Table(data, colWidths=[width] * ncol, repeatRows=1)
        t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#999999')), ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(t); story.append(Spacer(1, 6)); table_rows = []
    for line in md.split('\n'):
        if line.startswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if all(set(c) <= {'-'} for c in cells):
                continue
            table_rows.append(cells); continue
        flush_table()
        if line.startswith('# '):
            story.append(Paragraph(inline(line[2:]), styles['Title']))
        elif line.startswith('## '):
            story.append(Spacer(1, 6)); story.append(Paragraph(inline(line[3:]), styles['Heading2']))
        elif line.startswith('### '):
            story.append(Paragraph(inline(line[4:]), styles['Heading3']))
        elif line.startswith('- '):
            story.append(Paragraph('&bull; ' + inline(line[2:]), body))
        elif line.startswith('  > '):
            story.append(Paragraph(inline(line[4:]), quote))
        elif line.startswith('  '):
            story.append(Paragraph(inline(line.strip()), body))
        elif line.strip():
            story.append(Paragraph(inline(line), body))
        else:
            story.append(Spacer(1, 4))
    flush_table()
    doc.build(story)
    data = buf.getvalue()
    sys.stdout.buffer.write(data)


if __name__ == '__main__':
    main()
