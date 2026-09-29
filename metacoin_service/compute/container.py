"""A tiny multi-file container (`metacoin-compute-container/v1`) so a checkpoint or an output set is one
encrypted artifact: magic, u32 header length, JSON header {schema, files: [{name, offset, size, sha256}]},
then the concatenated blobs. Names are bounded plain basenames; no paths, no executables, no pickles."""
import hashlib
import json
import re
import struct

MAGIC = b'MCCT'
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
ALLOWED_SUFFIXES = ('.npy', '.json')
MAX_FILES = 64


def pack(files):
    """files: dict name -> bytes. Returns container bytes."""
    if len(files) > MAX_FILES:
        raise ValueError('too many files')
    entries, blobs, offset = [], [], 0
    for name in sorted(files):
        data = files[name]
        if not NAME_RE.match(name) or not name.endswith(ALLOWED_SUFFIXES) or type(data) is not bytes:
            raise ValueError('bad container member ' + str(name)[:40])
        entries.append({'name': name, 'offset': offset, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        blobs.append(data); offset += len(data)
    header = json.dumps({'schema': 'metacoin-compute-container/v1', 'files': entries}, separators=(',', ':')).encode()
    return MAGIC + struct.pack('<I', len(header)) + header + b''.join(blobs)


def unpack(data, max_bytes=256 * 1024 * 1024):
    if data[:4] != MAGIC or len(data) > max_bytes:
        raise ValueError('not a compute container')
    hlen = struct.unpack('<I', data[4:8])[0]
    header = json.loads(data[8:8 + hlen])
    if header.get('schema') != 'metacoin-compute-container/v1' or type(header.get('files')) is not list or len(header['files']) > MAX_FILES:
        raise ValueError('bad container header')
    base = 8 + hlen
    out = {}
    for e in header['files']:
        if not NAME_RE.match(e['name']) or not e['name'].endswith(ALLOWED_SUFFIXES):
            raise ValueError('bad container member')
        blob = data[base + e['offset']: base + e['offset'] + e['size']]
        if len(blob) != e['size'] or hashlib.sha256(blob).hexdigest() != e['sha256']:
            raise ValueError('container member integrity failure: ' + e['name'])
        out[e['name']] = blob
    return out


def listing(data):
    hlen = struct.unpack('<I', data[4:8])[0]
    return json.loads(data[8:8 + hlen])['files']
