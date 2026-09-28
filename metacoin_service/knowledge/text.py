"""Bounded document normalization and deterministic chunking.

Formats: text, markdown, csv (rows rendered as "column: value" lines). No script, macro, formula or remote reference
is ever evaluated; bytes are decoded as UTF-8 and refused otherwise. PDF is not supported in this build (no
maintained parser is installed); the format is refused with a precise reason instead of producing an empty document.

Chunker metacoin-chunker/v1: paragraphs (blank-line separated) are packed greedily into chunks of at most
CHUNK_CHARS characters; a single paragraph longer than the limit is split at sentence ends, then hard at the limit.
Overlap policy: none. Every chunk records its byte offsets into the normalized UTF-8 text and the nearest Markdown
heading above it, so a quoted span can be checked byte-for-byte against the stored normalized text."""
import csv
import hashlib
import io
import re

PARSER_ID = 'metacoin-text-normalizer/v1'
CHUNKER_ID = 'metacoin-chunker/v1'
FORMATS = ('text', 'markdown', 'csv')
CHUNK_CHARS = 1200
MAX_CHUNKS_PER_DOCUMENT = 400
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')
_TOKEN = re.compile(r'[a-z0-9]+')


def normalize(fmt, raw, limits):
    """Return (text, warnings). Raises ValueError with a safe reason."""
    if fmt not in FORMATS:
        raise ValueError('unsupported format %r (supported: %s; pdf is refused: no maintained parser installed)' % (fmt, ', '.join(FORMATS)))
    if len(raw) > limits['knowledge_max_document_bytes']:
        raise ValueError('document exceeds %d bytes' % limits['knowledge_max_document_bytes'])
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('not valid UTF-8 at byte %d' % exc.start) from None
    if '\x00' in text:
        raise ValueError('NUL bytes are refused')
    warnings = []
    if fmt == 'csv':
        text, warnings = _csv_to_text(text, limits)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = [ln.rstrip() for ln in text.split('\n')]
    out, blank = [], 0
    for ln in lines:
        if ln.strip() == '':
            blank += 1
            if blank <= 2:
                out.append('')
        else:
            blank = 0
            out.append(ln)
    text = '\n'.join(out).strip('\n') + '\n'
    if not text.strip():
        raise ValueError('document has no text after normalization (empty documents are refused, not stored as successes)')
    removed = sum(1 for ch in text if ord(ch) < 32 and ch not in '\n\t')
    if removed:
        text = ''.join(ch for ch in text if ord(ch) >= 32 or ch in '\n\t')
        warnings.append('removed %d control characters' % removed)
    return text, warnings


def _csv_to_text(text, limits):
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError('csv has no rows')
    header = [h.strip() for h in rows[0]]
    if not header or any(not h for h in header):
        raise ValueError('csv header must name every column')
    body = rows[1:]
    warnings = []
    if len(body) > limits['knowledge_max_csv_rows']:
        raise ValueError('csv exceeds %d rows' % limits['knowledge_max_csv_rows'])
    out = ['# columns: ' + ', '.join(header), '']
    for i, r in enumerate(body):
        if len(r) != len(header):
            warnings.append('row %d has %d fields, expected %d' % (i + 1, len(r), len(header)))
        cells = ['%s: %s' % (header[j], (r[j].strip() if j < len(r) else '')) for j in range(len(header))]
        out.append('row %d. ' % (i + 1) + '; '.join(cells)); out.append('')
    return '\n'.join(out), warnings


def chunk(text):
    """Deterministic chunks: [{ordinal, start, end, heading, sha256}] with byte offsets into text.encode('utf-8')."""
    data = text.encode('utf-8')
    paragraphs = []      # (start_byte, end_byte)
    pos = 0
    for para in text.split('\n\n'):
        b = para.encode('utf-8')
        if para.strip():
            lead = len(b) - len(b.lstrip())
            trail = len(b) - len(b.rstrip())
            paragraphs.append((pos + lead, pos + len(b) - trail))
        pos += len(b) + 2
    chunks, cur_start, cur_end, heading, cur_heading = [], None, None, None, None
    def flush():
        nonlocal cur_start, cur_end
        if cur_start is not None:
            piece = data[cur_start:cur_end]
            chunks.append({'ordinal': len(chunks), 'start': cur_start, 'end': cur_end, 'heading': cur_heading, 'sha256': hashlib.sha256(piece).hexdigest(), 'chars': len(piece.decode('utf-8'))})
            cur_start = cur_end = None
    for (s, e) in paragraphs:
        para = data[s:e].decode('utf-8')
        first = para.split('\n', 1)[0]
        if first.startswith('#'):
            heading = first.lstrip('#').strip()[:120]
        if e - s > CHUNK_CHARS:
            flush()
            for (ss, ee) in _split_long(data, s, e):
                cur_start, cur_end, cur_heading = ss, ee, heading
                flush()
            continue
        if cur_start is None:
            cur_start, cur_end, cur_heading = s, e, heading
        elif e - cur_start <= CHUNK_CHARS:
            cur_end = e
        else:
            flush(); cur_start, cur_end, cur_heading = s, e, heading
        if len(chunks) >= MAX_CHUNKS_PER_DOCUMENT:
            break
    flush()
    return chunks[:MAX_CHUNKS_PER_DOCUMENT]


def _split_long(data, s, e):
    text = data[s:e].decode('utf-8')
    parts, out, acc, acc_start = _SENTENCE_END.split(text), [], '', 0
    pos = 0
    pieces = []
    for p in parts:
        idx = text.find(p, pos)
        pieces.append((idx, idx + len(p))); pos = idx + len(p)
    cur = None
    for (a, b) in pieces:
        if cur is None:
            cur = [a, b]
        elif len(text[cur[0]:b].encode('utf-8')) <= CHUNK_CHARS:
            cur[1] = b
        else:
            out.append(tuple(cur)); cur = [a, b]
    if cur:
        out.append(tuple(cur))
    result = []
    for (a, b) in out:
        seg = text[a:b]
        if len(seg.encode('utf-8')) <= CHUNK_CHARS:
            result.append((s + len(text[:a].encode('utf-8')), s + len(text[:b].encode('utf-8'))))
        else:
            start = 0
            while start < len(seg):
                sub = seg[start:start + CHUNK_CHARS]
                while len(sub.encode('utf-8')) > CHUNK_CHARS:
                    sub = sub[:-1]
                result.append((s + len(text[:a + start].encode('utf-8')), s + len(text[:a + start + len(sub)].encode('utf-8')))); start += len(sub)
    return result


def tokens(text):
    return _TOKEN.findall(text.lower())


def chunk_text(text_bytes, c):
    return text_bytes[c['start']:c['end']].decode('utf-8')
