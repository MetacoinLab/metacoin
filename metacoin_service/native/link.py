"""Labelled local link emulator for contact-window capacity (order 2026-10-23 group B). A loopback TCP transfer with a declared
bandwidth cap produces received-byte evidence (count + digest + timing). This is an emulator on one host: it is evidence for a
contract policy about delivered bytes, never a measurement of a radio link and never a basis for issuance."""
import hashlib
import socket
import threading
import time

LABEL = 'emulated loopback link (one host; declared bandwidth cap; not a radio or a DTN node)'


def transfer(payload, bandwidth_bytes_per_s=None, chunk=4096):
    """Send payload over a loopback socket; return received-byte evidence."""
    srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1); port = srv.getsockname()[1]
    received = {'bytes': 0, 'digest': None, 'chunks': 0}
    h = hashlib.sha256()
    def rx():
        conn, _ = srv.accept()
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                h.update(data); received['bytes'] += len(data); received['chunks'] += 1
    t = threading.Thread(target=rx); t.start(); t0 = time.perf_counter()
    cl = socket.create_connection(('127.0.0.1', port))
    sent = 0
    with cl:
        for i in range(0, len(payload), chunk):
            piece = payload[i:i + chunk]; cl.sendall(piece); sent += len(piece)
            if bandwidth_bytes_per_s:
                target = sent / bandwidth_bytes_per_s; el = time.perf_counter() - t0
                if target > el:
                    time.sleep(min(target - el, 0.05))
    t.join(timeout=30); srv.close(); dt = time.perf_counter() - t0
    received['digest'] = h.hexdigest()
    return {'label': LABEL, 'sent_bytes': sent, 'received_bytes': received['bytes'], 'received_digest': received['digest'], 'sent_digest': hashlib.sha256(payload).hexdigest(), 'match': received['digest'] == hashlib.sha256(payload).hexdigest(),
            'duration_ms': int(dt * 1000), 'declared_bandwidth_bytes_per_s': bandwidth_bytes_per_s, 'effective_bytes_per_s': int(received['bytes'] / dt) if dt > 0 else None, 'kind': 'metered_fact (emulator)'}
