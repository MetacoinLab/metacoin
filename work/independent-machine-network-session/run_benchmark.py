"""Before/after benchmark runner (order 2026-10-23 §18): the SAME workloads against domains spawned from the recorded base revision
(git worktree) and from HEAD: (1) the native workload (metacoin_service.benchmark_native), (2) the mission measurements
(benchmark_mission: signer overhead, reservation contention on a plain offer, backlog recovery, planner runtime, worker use) and
(3) the network measurements (benchmark_network: shared-pool contention, provider overhead, signer round trips, lineage growth)
which only exist on HEAD. Each tree runs its OWN modules and schema. Writes <out_dir>/benchmark-{native,mission}-{before,after}.json
and benchmark-network.json."""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); os.environ['METACOIN_X402_DISABLED'] = '1'
from metacoin_service.tests.test_service import ENV, free_port
import httpx
PY = sys.executable
out_dir = Path(sys.argv[1]).resolve(); out_dir.mkdir(parents=True, exist_ok=True); base_rev = (ROOT / 'work' / 'independent-machine-network-session' / 'artifacts' / 'BASE_REVISION.txt').read_text().strip()


class Dom:
    """API + worker + signer (+ optional witness pin) spawned from a source TREE with its own home."""
    def __init__(self, tree, did, label):
        self.tree = tree; self.id = did; self.tmp = Path(tempfile.mkdtemp(prefix='bench-%s-%s-' % (label, did))); self.home = self.tmp / 'home'; self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port
        self.env = dict(ENV, PYTHONPATH=str(tree), METACOIN_X402_DISABLED='1', METACOIN_FACILITATOR_URL='http://127.0.0.1:1/blocked', METACOIN_LIMITS_JSON=json.dumps({'test_hooks': 1}))
        init = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.home), '--provider-mode', 'simulation', 'init'], cwd=tree, env=self.env, capture_output=True, text=True); info = json.loads(init.stdout); self.tok = json.load(open(info['credential_file']))['principals']['owner']['token']
        self.cred = self.tmp / 'cred.json'; self.cred.write_text(json.dumps({'token': self.tok})); os.chmod(self.cred, 0o600)
        # signer from the same tree
        self.sg_home = self.tmp / 'signer'; subprocess.run([PY, '-m', 'metacoin_service.native.signer', 'init', '--home', str(self.sg_home), '--domains', did, '--signer-id', 'sig-bench'], cwd=tree, env=self.env, check=True, capture_output=True)
        self.sg_port = free_port(); self.sg_base = 'http://127.0.0.1:%d' % self.sg_port; self.sg = subprocess.Popen([PY, '-m', 'metacoin_service.native.signer', 'serve', '--home', str(self.sg_home), '--port', str(self.sg_port)], cwd=tree, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env = dict(self.env, METACOIN_SIGNER_URL=self.sg_base, METACOIN_SIGNER_TOKEN_FILE=str(self.sg_home / 'token'))
        self.api_log = open(self.tmp / 'api.log', 'ab'); self.api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.home), '--provider-mode', 'simulation', 'serve', '--port', str(self.port)], cwd=tree, env=env, stdout=subprocess.DEVNULL, stderr=self.api_log)
        self.stop = self.tmp / 'stop'; self.worker = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.home), '--provider-mode', 'simulation', 'worker', '--name', 'bench-' + did, '--stop-file', str(self.stop)], cwd=tree, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            try:
                if httpx.get(self.base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        h = {'Authorization': 'Bearer ' + self.tok}; r = httpx.post(self.base + '/api/v1/native/domain', headers=h, json={'domain_id': did, 'operator': 'benchmark'}, timeout=60); assert r.status_code == 201, r.text; self.h = h
        self.rev = httpx.get(self.base + '/api/health').json().get('revision')

    def pin(self, other):
        d = httpx.get(other.base + '/api/v1/native/domain', headers=other.h).json(); r = httpx.post(self.base + '/api/v1/native/peers', headers=self.h, json={'domain_id': other.id, 'authority_key_id': d['authority']['key_id'], 'authority_public_hex': d['authority']['public_hex'], 'epoch': d['authority']['epoch']}); assert r.status_code in (200, 201), r.text

    def close(self):
        self.stop.write_text('stop'); self.api.terminate(); self.api.wait(timeout=20); self.sg.terminate(); self.sg.wait(timeout=10)
        try:
            self.worker.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.worker.terminate()
        self.api_log.close()


def run_mod(tree, mod, args, out, env_extra=None, timeout=3600):
    p = subprocess.run([PY, '-m', mod] + args + ['--out', str(out)], cwd=tree, env=dict(ENV, PYTHONPATH=str(tree), METACOIN_X402_DISABLED='1', **(env_extra or {})), capture_output=True, text=True, timeout=timeout)
    print(mod, 'rc', p.returncode, p.stderr[-500:] if p.returncode else ''); return p.returncode


wt = Path(os.environ.get('METACOIN_BENCH_WORKTREE', str(out_dir / 'base-worktree')))
if not (wt / 'metacoin_service').exists():
    subprocess.run(['git', 'worktree', 'add', '--detach', str(wt), base_rev], cwd=ROOT, check=True, capture_output=True)
for f in ('protocol/ledger_data.jsonl', 'mission_verdict.json'):
    if (ROOT / f).exists() and not (wt / f).exists():
        (wt / f).parent.mkdir(parents=True, exist_ok=True); (wt / f).write_bytes((ROOT / f).read_bytes())
only = os.environ.get('METACOIN_BENCH_ONLY')
for label, tree in (('before', wt), ('after', ROOT)):
    if only and only != label:
        continue
    A = Dom(tree, 'bench-a', label); B = Dom(tree, 'bench-b', label); A.pin(B); B.pin(A)
    try:
        run_mod(tree, 'metacoin_service.benchmark_native', [A.base, str(A.cred), '--transfers', '300', '--concurrency', '8', '--awards', '4'], out_dir / ('benchmark-native-%s.json' % label), {'METACOIN_BENCH_HOME': str(A.home)})
        run_mod(tree, 'metacoin_service.benchmark_mission', [A.base, str(A.cred), '--signer', A.sg_base, '--signer-token', str(A.sg_home / 'token'), '--peer-base', B.base, '--peer-cred', str(B.cred), '--peer-domain', 'bench-b', '--worker-pid', str(A.worker.pid), '--transfers', '60', '--contenders', '16', '--backlog', '40', '--awards', '4'], out_dir / ('benchmark-mission-%s.json' % label))
        if label == 'after':
            run_mod(tree, 'metacoin_service.benchmark_network', [A.base, str(A.cred), '--signer', A.sg_base, '--signer-token', str(A.sg_home / 'token'), '--peer-base', B.base, '--peer-cred', str(B.cred), '--peer-domain', 'bench-b', '--transfers', '60', '--contenders', '16', '--lots', '40'], out_dir / 'benchmark-network.json')
        for f in out_dir.glob('benchmark-*-%s.json' % label):
            d = json.load(open(f)); d['revision'] = A.rev; d['tree'] = str(tree); json.dump(d, open(f, 'w'), indent=1)
        if label == 'after' and (out_dir / 'benchmark-network.json').exists():
            d = json.load(open(out_dir / 'benchmark-network.json')); d['revision'] = A.rev; json.dump(d, open(out_dir / 'benchmark-network.json', 'w'), indent=1)
    finally:
        A.close(); B.close()
print('benchmarks written to', out_dir)
