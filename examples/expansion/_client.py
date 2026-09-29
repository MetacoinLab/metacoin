"""Minimal shared client for the examples: credential from a private file, JSON over HTTP, bounded polling."""
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get('METACOIN_BASE_URL', 'http://127.0.0.1:8402').rstrip('/')


def token():
    path = os.environ.get('METACOIN_CREDENTIAL_FILE')
    if not path:
        sys.exit(json.dumps({'error': 'METACOIN_CREDENTIAL_FILE not set (private JSON file {"token": ...}, mode 0600)'}))
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(json.dumps({'error': 'credential file is readable by others; chmod 600 ' + path}))
    return json.load(open(path))['token']


def call(method, path, body=None, tok=None, base=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request((base or BASE) + path, data=data, method=method, headers={'Authorization': 'Bearer ' + (tok or token()), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except ValueError:
            return e.code, {'error': True, 'code': 'HTTP_%d' % e.code}


def must(status, body, want=(200, 201, 202)):
    if status not in want:
        sys.exit(json.dumps({'refused': True, 'status': status, 'error': body}))
    return body


def wait_job(jid, timeout=600, tok=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, j = call('GET', '/api/v1/jobs/' + jid, tok=tok)
        if st == 200 and j['state'] in ('succeeded', 'failed', 'cancelled'):
            return j
        time.sleep(1)
    sys.exit(json.dumps({'timed_out': True, 'job_id': jid, 'note': 'the job keeps running; re-run with the same id to resume watching, nothing was resubmitted'}))


# A small synthetic temporal batch (labelled EXAMPLE_SYNTHETIC) used by the batch/audit/MCP/federation examples.
BATCH_SPEC = {'schema': 'temporal-batch-input/v1', 'base': {'schema': 'temporal-energy-input/v1', 'capacity': 10000, 'initial_low': 6000, 'initial_high': 6000, 'reserve': 2000, 'segments': [{'duration': 10, 'harvest_low': 600, 'harvest_high': 800, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0}, {'duration': 30, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 100, 'load_high': 200, 'leakage_low': 0, 'leakage_high': 5}, {'duration': 20, 'harvest_low': 900, 'harvest_high': 1200, 'load_low': 0, 'load_high': 10, 'leakage_low': 0, 'leakage_high': 0}], 'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'}, 'assumptions': ['piecewise_constant_power_bounds', 'independent_interval_bounds', 'powers_at_usable_energy_boundary', 'saturation_at_capacity', 'constant_reserve', 'virtual_energy_below_reserve_for_diagnostics', 'no_unmodeled_loads', 'no_recharge_physics'], 'provenance': 'synthetic', 'private_label': 'COMPUTE_SYNTHETIC'}, 'scenarios': None, 'grid': [{'path': 'reserve', 'start': 0, 'stop': 9000, 'step': 1000}, {'path': 'load_scale_percent', 'values': [50, 100]}], 'device_policy': 'cpu', 'verification': 'auto', 'private_label': 'EXAMPLE_SYNTHETIC'}
