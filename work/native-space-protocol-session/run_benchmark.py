"""Spawn one disposable domain (API + worker, x402 disabled) and run metacoin_service.benchmark_native against it; write the result and the storage sizes."""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['METACOIN_X402_DISABLED'] = '1'
from metacoin_service.tests.journeys_native import Dom, PY, ROOT
out = sys.argv[1]
d = Dom('bench-dom')
try:
    cred = d.cred_file('owner')
    p = subprocess.run([PY, '-m', 'metacoin_service.benchmark_native', d.base, str(cred), '--transfers', '300', '--concurrency', '8', '--awards', '6', '--out', out], cwd=ROOT, env=dict(d.env, METACOIN_BENCH_HOME=str(d.inst.home)), capture_output=True, text=True, timeout=2400)
    print('benchmark rc', p.returncode); print(p.stderr[-800:])
    if p.returncode == 0:
        r = json.load(open(out)); print(json.dumps({k: r[k] for k in ('transfers', 'settlement', 'journal_replay', 'recovery_sweep', 'storage_bytes', 'economic_summary') if k in r}, indent=1)[:2500])
finally:
    d.close()
