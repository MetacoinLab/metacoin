"""Minimal, pickle-free NumPy `.npy` (format 1.0) codec for the two dtypes the compute services use:
little-endian int64 and float64, C order, at most 3 dimensions. Written in plain Python so the API,
the worker and an ordinary client can read exported arrays without importing numpy or any server
internals. Object arrays, Fortran order, big-endian data and other dtypes are refused."""
import ast
import struct

MAGIC = b'\x93NUMPY'
DTYPES = {'<i8': ('q', 8), '<f8': ('d', 8)}
MAX_ELEMENTS = 64 * 1024 * 1024


def encode(values, dtype, shape):
    """values: flat list (row-major) of ints (<i8) or floats (<f8)."""
    if dtype not in DTYPES:
        raise ValueError('unsupported dtype')
    n = 1
    for s in shape:
        if type(s) is not int or s < 0:
            raise ValueError('bad shape')
        n *= s
    if n != len(values) or n > MAX_ELEMENTS or len(shape) > 3:
        raise ValueError('shape/values mismatch or too large')
    fmt, size = DTYPES[dtype]
    header = "{'descr': '%s', 'fortran_order': False, 'shape': %s, }" % (dtype, repr(tuple(shape)) if len(shape) != 1 else '(%d,)' % shape[0])
    pad = 64 - ((len(MAGIC) + 2 + 2 + len(header) + 1) % 64)
    header = header + ' ' * pad + '\n'
    body = struct.pack('<%d%s' % (n, fmt), *values)
    return MAGIC + bytes([1, 0]) + struct.pack('<H', len(header)) + header.encode('latin1') + body


def decode(data, max_elements=MAX_ELEMENTS):
    """Returns (flat list, dtype, shape). Refuses anything but the allowed dtypes; never unpickles."""
    if data[:6] != MAGIC or data[6] != 1 or data[7] != 0:
        raise ValueError('not an npy v1.0 file')
    hlen = struct.unpack('<H', data[8:10])[0]
    header = data[10:10 + hlen].decode('latin1')
    try:
        meta = ast.literal_eval(header.strip())
    except (ValueError, SyntaxError):
        raise ValueError('bad npy header')
    if type(meta) is not dict or set(meta) != {'descr', 'fortran_order', 'shape'} or meta['fortran_order'] is not False:
        raise ValueError('unsupported npy header')
    dtype = meta['descr']
    if dtype not in DTYPES:
        raise ValueError('unsupported dtype ' + str(dtype))
    shape = meta['shape']
    if type(shape) is not tuple or len(shape) > 3 or not all(type(s) is int and s >= 0 for s in shape):
        raise ValueError('bad shape')
    n = 1
    for s in shape:
        n *= s
    if n > max_elements:
        raise ValueError('array too large')
    fmt, size = DTYPES[dtype]
    body = data[10 + hlen:]
    if len(body) != n * size:
        raise ValueError('array/header size mismatch')
    return list(struct.unpack('<%d%s' % (n, fmt), body)), dtype, list(shape)


def write(path, values, dtype, shape):
    with open(path, 'wb') as f:
        f.write(encode(values, dtype, shape))


def read(path, max_elements=MAX_ELEMENTS):
    with open(path, 'rb') as f:
        return decode(f.read(), max_elements)
