"""An agent under a grant pays for a priced service over the real local x402 transport (test-http), with a
checkpointed payment identifier so an interruption after payment re-presents the same authorization."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
import httpx
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV
from metacoin_service.tests.test_agents import TEMPORAL, policy
from metacoin_service import agent_runner

PY = sys.executable


class AgentPaidInvocationOverTcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = Instance(provider_mode='test-http'); cls.port = free_port(); cls.base = 'http://127.0.0.1:%d' % cls.port
        cls.proc = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(cls.inst.home), '--provider-mode', 'test-http', 'serve', '--port', str(cls.port)],
                                    cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                if httpx.get(cls.base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        cls.http = httpx.Client(base_url=cls.base, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=20); cls.inst.close()

    def files(self, name, token, pol):
        d = Path(self.inst.temp.name) / name; d.mkdir()
        cred = d / 'cred.json'; cred.write_text(json.dumps({'token': token})); os.chmod(cred, 0o600)
        pf = d / 'policy.json'; pf.write_text(json.dumps(pol)); os.chmod(pf, 0o600)
        inp = d / 'inputs.json'; inp.write_text(json.dumps(TEMPORAL))
        return cred, pf, inp, d / 'checkpoint.json'

    def runner_cli(self, cmd, cred, pf, cp, *extra):
        p = subprocess.run([PY, '-m', 'metacoin_service.agent_runner', '--base', self.base, '--credential-file', str(cred), '--policy-file', str(pf), '--checkpoint', str(cp), cmd, *extra],
                           cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=180)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith('{') else p.stdout + p.stderr)

    def test_agent_pays_over_x402_and_resumes_after_interruption(self):
        H = self.inst.h('owner')
        pol = policy(ceilings={'total_amount': 2, 'per_action_amount': 1, 'max_jobs': 2, 'max_workflows': 1, 'concurrency': 2})
        g = self.http.post('/api/v1/agents/grants', headers=H, json={'policy': pol}).json()
        cred, pf, inp, cp = self.files('paid', g['token'], pol)
        rc, out = self.runner_cli('execute', cred, pf, cp, '--service', 'temporal_energy', '--inputs-file', str(inp))
        self.assertEqual(rc, 0, out)
        self.assertEqual((out['executed'], out['job']['state'], out['job'].get('paid')), (True, 'queued', True), out)
        jid = out['job']['job_id']
        # the payment identifier was checkpointed before the paid request; a second execute re-presents it and gets the same job back
        rc, again = self.runner_cli('execute', cred, pf, cp, '--service', 'temporal_energy', '--inputs-file', str(inp))
        self.assertEqual(again['job']['job_id'], jid)
        # simulate an interruption between payment and job checkpoint: drop the job step, keep the payment identifier, re-run
        state = json.load(open(cp)); ident = state['steps']['payment']['identifier']; state['steps'].pop('job'); json.dump(state, open(cp, 'w'))
        rc, resumed = self.runner_cli('execute', cred, pf, cp, '--service', 'temporal_energy', '--inputs-file', str(inp))
        self.assertEqual((resumed['job']['job_id'], resumed['job'].get('replayed')), (jid, True), resumed)
        self.assertEqual(json.load(open(cp))['steps']['payment']['identifier'], ident)
        # one job, one settled sale, one usage record after the worker; the grant counted one job and reserved one unit
        subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--once'], cwd=ROOT, env=ENV, check=True, capture_output=True, timeout=120)
        jobs = self.http.get('/api/v1/jobs', headers=H).json()['items']
        self.assertEqual([j['id'] for j in jobs], [jid])
        usage = self.http.get('/api/v1/usage', headers=H).json()['items']
        self.assertEqual((len(usage), usage[0]['job_id'], usage[0]['assessed_charge'], usage[0]['states']['provider_settlement']['state']), (1, jid, 1, 'CONFIRMED'))
        view = self.http.get('/api/v1/agents/grants/' + g['grant_id'], headers=H).json()
        self.assertEqual((view['counters']['jobs_created'], view['counters']['amount_reserved']), (1, 1))
        # a second invocation exceeds per-action exposure once the total ceiling is reached: refused at acceptance before any payment
        cred2, pf2, inp2, cp2 = self.files('paid2', g['token'], pol)
        rc, second = self.runner_cli('execute', cred2, pf2, cp2, '--service', 'temporal_energy', '--inputs-file', str(inp2))
        self.assertEqual((second['executed'], second['job']['state']), (True, 'queued'))
        cred3, pf3, inp3, cp3 = self.files('paid3', g['token'], pol)
        rc, third = self.runner_cli('execute', cred3, pf3, cp3, '--service', 'temporal_energy', '--inputs-file', str(inp3))
        self.assertFalse(third['executed'])
        self.assertIn((third.get('refusal') or third.get('plan', {}).get('refusal') or {}).get('code'), ('BUDGET_EXHAUSTED', 'RATE_LIMITED'))
        self.assertEqual(len(self.http.get('/api/v1/jobs', headers=H).json()['items']), 2)


if __name__ == '__main__':
    unittest.main()
