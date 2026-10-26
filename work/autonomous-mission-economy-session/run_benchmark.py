"""Before/after benchmark runner (order 2026-10-23 §15): the SAME native workload (metacoin_service.benchmark_native) against a
domain spawned from the base revision (git worktree of 6a9b592) and from HEAD, then the mission-economy measurements against
HEAD with signer, witness and a peer domain. Writes <out_dir>/benchmark-before.json, benchmark-after.json, benchmark-mission.json."""
import json, os, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); os.environ['METACOIN_X402_DISABLED'] = '1'
from metacoin_service.tests.journeys_mission import MDom, SignerProcess, WitnessProcess, PY
from metacoin_service.tests.test_service import ENV, free_port
import httpx
out_dir = Path(sys.argv[1]).resolve(); out_dir.mkdir(parents=True, exist_ok=True); base_rev = (ROOT / 'work' / 'autonomous-mission-economy-session' / 'artifacts' / 'BASE_REVISION.txt').read_text().strip()


def native_bench(tree, label):
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix='bench-%s-' % label)); home = tmp / 'home'; port = free_port(); base = 'http://127.0.0.1:%d' % port
    env = dict(ENV, PYTHONPATH=str(tree), METACOIN_X402_DISABLED='1', METACOIN_FACILITATOR_URL='http://127.0.0.1:1/blocked')
    init = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'init'], cwd=tree, env=env, capture_output=True, text=True)   # the TREE's own schema, never HEAD's
    info = json.loads(init.stdout); tok = json.load(open(info['credential_file']))['principals']['owner']['token']
    api_log = open(tmp / 'api.log', 'ab'); worker_log = open(tmp / 'worker.log', 'ab')
    api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'serve', '--port', str(port)], cwd=tree, env=env, stdout=subprocess.DEVNULL, stderr=api_log)
    stop = tmp / 'stop'; worker = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'worker', '--name', 'bench', '--stop-file', str(stop)], cwd=tree, env=env, stdout=subprocess.DEVNULL, stderr=worker_log)
    class inst:
        pass
    inst.home = home; inst.temp = type('T', (), {'name': str(tmp)})(); inst.tok = {'owner': tok}; inst.close = lambda: None
    try:
        time.sleep(2)
        if worker.poll() is not None:
            print(label, 'worker exited early rc', worker.returncode, open(tmp / 'worker.log', 'rb').read()[-600:])
        for _ in range(300):
            try:
                if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        cred = Path(inst.temp.name) / 'cred.json'; cred.write_text(json.dumps({'token': inst.tok['owner']})); os.chmod(cred, 0o600); out = out_dir / ('benchmark-%s.json' % label)
        p = subprocess.run([PY, '-m', 'metacoin_service.benchmark_native', base, str(cred), '--transfers', '300', '--concurrency', '8', '--awards', '4', '--out', str(out)], cwd=tree, env=dict(env, METACOIN_BENCH_HOME=str(inst.home)), capture_output=True, text=True, timeout=2400)
        print(label, 'native benchmark rc', p.returncode, p.stderr[-400:] if p.returncode else '')
        rev = httpx.get(base + '/api/health').json().get('revision')
        if out.exists():
            d = json.load(open(out)); d['revision'] = rev; d['tree'] = str(tree); json.dump(d, open(out, 'w'), indent=1)
    finally:
        stop.write_text('stop'); api.terminate(); api.wait(timeout=20)
        try:
            worker.wait(timeout=60)
        except subprocess.TimeoutExpired:
            worker.terminate()
        api_log.close(); worker_log.close()


wt = Path(os.environ.get('METACOIN_BENCH_WORKTREE', str(Path(sys.argv[2]) if len(sys.argv) > 2 else out_dir / 'base-worktree')))
if not (wt / 'metacoin_service').exists():
    subprocess.run(['git', 'worktree', 'add', '--detach', str(wt), base_rev], cwd=ROOT, check=True, capture_output=True)
    for f in ('protocol/ledger_data.jsonl', 'mission_verdict.json'):
        if (ROOT / f).exists() and not (wt / f).exists():
            (wt / f).parent.mkdir(parents=True, exist_ok=True); (wt / f).write_bytes((ROOT / f).read_bytes())
only = os.environ.get('METACOIN_BENCH_ONLY')
if only in (None, 'before'):
    native_bench(wt, 'before')
if only == 'before':
    sys.exit(0)
native_bench(ROOT, 'after')
# mission measurements against HEAD with signer, witness and a peer
W = WitnessProcess(); sgA = SignerProcess(domains=('bench-a',)); sgB = SignerProcess(domains=('bench-b',))
wenv = {'METACOIN_WITNESS_URL': W.base, 'METACOIN_WITNESS_TOKEN_FILE': str(W.home / 'token'), 'METACOIN_WITNESS_KEY_ID': W.identity['key_id']}
A = MDom('bench-a', dict(wenv, METACOIN_SIGNER_URL=sgA.base, METACOIN_SIGNER_TOKEN_FILE=str(sgA.home / 'token'))); B = MDom('bench-b', dict(wenv, METACOIN_SIGNER_URL=sgB.base, METACOIN_SIGNER_TOKEN_FILE=str(sgB.home / 'token'))); A.pin(B); B.pin(A)
try:
    p = subprocess.run([PY, '-m', 'metacoin_service.benchmark_mission', A.base, str(A.cred_file('owner')), '--signer', sgA.base, '--signer-token', str(sgA.home / 'token'), '--peer-base', B.base, '--peer-cred', str(B.cred_file('owner')), '--peer-domain', 'bench-b', '--worker-pid', str(A.workers[0].pid), '--out', str(out_dir / 'benchmark-mission.json')], cwd=ROOT, env=A.env, capture_output=True, text=True, timeout=2400)
    print('mission benchmark rc', p.returncode, p.stderr[-600:] if p.returncode else '')
finally:
    A.close(); B.close(); sgA.close(); sgB.close(); W.close()
