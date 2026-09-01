"""spice_state_extractor — the documented extraction method behind the pinned
heliocentric state vectors in demo/tasks/pinned_spice_sources.py.

Research-only TOOLING, not a task and not imported by any task: given a local
copy of NAIF's de440s.bsp (sha256 recorded in the pinned module), this script
reads the DAF/SPK binary directly with the standard library and evaluates the
Type-2 Chebyshev segments to produce J2000-frame heliocentric states for
Earth (399) and Mars (499) at the pinned ET epochs — the exact method the
provenance blocks cite, committed so anyone holding the kernel can regenerate
the pinned values byte-for-byte:

    python3 demo/tasks/spice_state_extractor.py /path/to/de440s.bsp

METHOD, stated precisely: (1) parse the DAF file record (LOCIDW "DAF/SPK",
ND=2, NI=6, little-endian per LOCFMT "LTL-IEEE"); (2) walk the summary
records from FWARD, reading each segment's descriptor (ET start/stop; then
target, center, frame, data type, initial and final array addresses);
(3) for SPK data type 2 (Chebyshev position, velocity by differentiation of
the polynomial — the type used by the planetary segments of de440s), read
the segment directory (INIT, INTLEN, RSIZE, N) from the last four doubles of
the array, select the record covering the requested epoch, and evaluate the
Chebyshev sums for position and the term-derivative sums (times 2/INTLEN)
for velocity; (4) chain segments: heliocentric Earth = (399 rel 3) +
(3 rel 0) - (10 rel 0); heliocentric Mars = (4 rel 0) - (10 rel 0) — a
STATED approximation: de440s carries no (499 rel 4) segment, and the Mars
system barycenter sits within ~0.3 km of Mars's center (the Phobos+Deimos
GM fraction of the system is ~2e-8), negligible for heliocentric transfer
arcs and recorded here rather than papered over. Frame: J2000. Units: km
and km/s, exactly as stored.

SELF-CHECKS the script performs before printing anything: Earth heliocentric
distance within [1.45e8, 1.55e8] km and speed within [28.5, 30.5] km/s at
every pinned epoch; Mars distance within [2.0e8, 2.5e8] km and speed within
[21, 27] km/s; each evaluated epoch strictly inside its segment coverage.
A violated check aborts the extraction — stop, don't fudge.

SPICE kernels are U.S. government works distributed by NAIF; their rules
page states verbatim "No fees or licensing are required" and "Redistribution
of SPICE kernels distributed by NAIF is permitted as long as they have not
been modified" (this script redistributes nothing — it reads a locally
fetched copy). No NASA affiliation or endorsement. Not financial, legal, or
flight-engineering advice. Test-META is a zero-value testnet placeholder and
never mints base supply (MIP-0001 paragraph 3, MIP-0002 paragraph 8).
"""

import struct
import sys

sys.dont_write_bytecode = True

# The pinned ET epochs (TDB seconds past J2000) are supplied by the caller
# or taken from the pinned module when run without arguments beyond the
# kernel path; see __main__.


def _read_file_record(f):
    f.seek(0)
    rec = f.read(1024)
    locidw = rec[0:8].decode("ascii").strip()
    assert locidw.startswith("DAF/SPK"), f"not a DAF/SPK file: {locidw!r}"
    locfmt = rec[88:96].decode("ascii").strip()
    assert locfmt == "LTL-IEEE", f"unsupported binary format {locfmt!r}"
    nd = struct.unpack("<i", rec[8:12])[0]
    ni = struct.unpack("<i", rec[12:16])[0]
    fward = struct.unpack("<i", rec[76:80])[0]
    assert (nd, ni) == (2, 6), f"unexpected ND/NI {(nd, ni)}"
    return fward


def _segments(f, fward):
    """Yield (et0, et1, target, center, frame, dtype, start, stop)."""
    recno = fward
    while recno > 0:                        # bounded: the DAF summary chain
        f.seek((recno - 1) * 1024)
        rec = f.read(1024)
        nxt = int(struct.unpack("<d", rec[0:8])[0])
        nsum = int(struct.unpack("<d", rec[16:24])[0])
        off = 24
        for _ in range(nsum):               # bounded: summaries per record
            dc = struct.unpack("<2d", rec[off:off + 16])
            ic = struct.unpack("<6i", rec[off + 16:off + 40])
            yield dc[0], dc[1], ic[0], ic[1], ic[2], ic[3], ic[4], ic[5]
            off += 40
        recno = nxt


def _read_doubles(f, start_word, count):
    f.seek((start_word - 1) * 8)
    return struct.unpack(f"<{count}d", f.read(count * 8))


class Type2Segment:
    def __init__(self, f, et0, et1, start, stop):
        self.et0, self.et1 = et0, et1
        init, intlen, rsize, n = _read_doubles(f, stop - 3, 4)
        self.init, self.intlen = init, intlen
        self.rsize, self.n = int(rsize), int(n)
        self.f, self.start = f, start

    def state(self, et):
        assert self.et0 <= et <= self.et1, (
            f"epoch {et} outside segment coverage [{self.et0}, {self.et1}]")
        idx = min(int((et - self.init) // self.intlen), self.n - 1)
        rec = _read_doubles(self.f, self.start + idx * self.rsize, self.rsize)
        mid, radius = rec[0], rec[1]
        ncoef = (self.rsize - 2) // 3
        s = (et - mid) / radius             # normalized time in [-1, 1]
        # Chebyshev polynomials and derivatives, iteratively (no recursion)
        t = [1.0, s]
        dt = [0.0, 1.0]
        for j in range(2, ncoef):           # bounded: ncoef terms
            t.append(2.0 * s * t[j - 1] - t[j - 2])
            dt.append(2.0 * t[j - 1] + 2.0 * s * dt[j - 1] - dt[j - 2])
        pos, vel = [], []
        for axis in range(3):               # bounded: three axes
            c = rec[2 + axis * ncoef:2 + (axis + 1) * ncoef]
            pos.append(sum(c[j] * t[j] for j in range(ncoef)))
            vel.append(sum(c[j] * dt[j] for j in range(ncoef)) / radius)
        return pos + vel


def load_segments(path):
    f = open(path, "rb")
    fward = _read_file_record(f)
    segs = {}
    for et0, et1, tgt, ctr, frame, dtype, start, stop in _segments(f, fward):
        assert frame == 1, f"non-J2000 frame {frame} in segment {tgt}/{ctr}"
        assert dtype == 2, f"unsupported SPK type {dtype} for {tgt} rel {ctr}"
        segs[(tgt, ctr)] = Type2Segment(f, et0, et1, start, stop)
    return segs


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def heliocentric_state(segs, body, et):
    """J2000 heliocentric state (km, km/s) for 399 (Earth) or 499 (Mars)."""
    sun = segs[(10, 0)].state(et)
    if body == 399:
        emb = segs[(3, 0)].state(et)
        earth = segs[(399, 3)].state(et)
        return _sub(_add(earth, emb), sun)
    if body == 499:
        # de440s has no (499 rel 4) segment; the Mars system barycenter
        # (4 rel 0) stands in for Mars — stated approximation, ~0.3 km
        return _sub(segs[(4, 0)].state(et), sun)
    raise ValueError(f"unsupported body {body}")


def _norm3(v):
    return (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5


SANITY = {399: ((1.45e8, 1.55e8), (28.5, 30.5)),
          499: ((2.0e8, 2.5e8), (21.0, 27.0))}


def extract(path, epochs_et):
    """{(body, et): [x,y,z,vx,vy,vz]} with the sanity gates applied."""
    segs = load_segments(path)
    out = {}
    for body in (399, 499):                 # bounded: two bodies
        (rlo, rhi), (vlo, vhi) = SANITY[body]
        for et in epochs_et:                # bounded: the pinned epoch list
            st = heliocentric_state(segs, body, et)
            r, v = _norm3(st[:3]), _norm3(st[3:])
            assert rlo <= r <= rhi, (
                f"sanity violated: body {body} at ET {et}: r={r} km")
            assert vlo <= v <= vhi, (
                f"sanity violated: body {body} at ET {et}: v={v} km/s")
            out[(body, et)] = st
    return out


if __name__ == "__main__":
    kernel = sys.argv[1]
    try:
        from demo.tasks import pinned_spice_sources as src
    except ImportError:
        sys.path.insert(0, __file__.rsplit("/", 1)[0])
        import pinned_spice_sources as src
    epochs = [et for _label, et in src.GRID_EPOCHS_ET]
    states = extract(kernel, epochs)
    for (body, et), st in sorted(states.items()):
        print(body, repr(et), [f"{x!r}" for x in st])
