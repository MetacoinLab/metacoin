"""Agent policy grants (server-enforced) and the policy-limited runner (client, public API only)."""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV
from metacoin_service import agent_runner, temporal, workflows

TEMPORAL = {'schema': temporal.INPUT_SCHEMA, 'capacity': 10_000, 'initial_low': 6_000, 'initial_high': 6_000, 'reserve': 2_000,
            'segments': [{'duration': 10, 'harvest_low': 600, 'harvest_high': 800, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0}],
            'units': dict(temporal.UNITS), 'assumptions': list(temporal.ASSUMPTIONS), 'provenance': 'synthetic', 'private_label': 'AGENT_PRIVATE_1'}


def policy(**over):
    base = {'schema': 'metacoin-agent-policy/v1', 'permitted_services': ['temporal_energy'], 'allowed_operations': ['services:read', 'quote', 'invoke', 'job:read'],
            'ceilings': {'total_amount': 3, 'per_action_amount': 2, 'max_jobs': 2, 'max_workflows': 1, 'concurrency': 2}, 'validity_seconds': 3600,
            'review_gate_mandatory': True, 'input_visibility': 'own'}
    base.update(over)
    return base


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client
        services = self.c.get('/api/v1/services', headers=self.inst.h('owner')).json()['items']
        self.sid = next(s['id'] for s in services if s['kind'] == 'temporal_energy')
        self.energy_sid = next(s['id'] for s in services if s['kind'] == 'energy_audit')

    def grant(self, **over):
        r = self.c.post('/api/v1/agents/grants', headers=self.inst.h('owner'), json={'policy': policy(**over)})
        self.assertEqual(r.status_code, 201, r.text)
        g = r.json()
        return g, {'Authorization': 'Bearer ' + g['token']}

    def invoke(self, H, inputs=TEMPORAL, sid=None):
        sid = sid or self.sid
        q = self.c.post('/api/v1/services/' + sid + '/quote', headers=H, json={'inputs': inputs})
        if q.status_code != 201:
            return q
        a = self.c.post('/api/v1/quotes/' + q.json()['quote_id'] + '/accept', headers=H)
        if a.status_code != 200:
            return a
        return self.c.post('/api/v1/services/' + sid + '/invoke', headers=H, json={'quote_id': q.json()['quote_id'], 'inputs': inputs})

    def test_grant_governs_every_mutation_and_counts_conservatively(self):
        g, H = self.grant()
        self.assertNotIn('token', json.dumps(self.c.get('/api/v1/agents/grants/' + g['grant_id'], headers=self.inst.h('owner')).json()))
        # discovery and reads are allowed; scope widening is refused at every door
        self.assertEqual(self.c.get('/api/v1/services', headers=H).status_code, 200)
        self.assertEqual(self.c.post('/api/v1/credentials', headers=H, json={'operations': ['job:read']}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/agents/grants', headers=H, json={'policy': policy()}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/agents/grants/' + g['grant_id'] + '/stop', headers=H).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/contracts', headers=H, json={'kind': 'temporal_energy', 'title': 'x', 'inputs': TEMPORAL, 'policy': {}}).status_code, 201)  # contract:create is implied by quote
        # a service outside the allowlist is refused even though the credential permission would allow it
        r = self.c.post('/api/v1/services/' + self.energy_sid + '/quote', headers=H, json={'inputs': self.inst.contract and __import__('metacoin_service.tests.test_service', fromlist=['own_inputs']).own_inputs()})
        self.assertEqual((r.status_code, r.json()['code']), (403, 'FORBIDDEN'))
        # per-action ceiling: quantity 3 -> amount 3 > per_action 2
        r = self.c.post('/api/v1/services/' + self.sid + '/quote', headers=H, json={'inputs': TEMPORAL, 'quantity_max': 3})
        self.assertEqual(self.c.post('/api/v1/quotes/' + r.json()['quote_id'] + '/accept', headers=H).json()['code'], 'BUDGET_EXHAUSTED')
        # two invocations fit (amount 1 each, jobs 2); the third is refused by the job ceiling before anything is created
        first = self.invoke(H); self.assertEqual(first.status_code, 202, first.text)
        second = self.invoke(H); self.assertEqual(second.status_code, 202, second.text)
        third = self.invoke(H); self.assertEqual((third.status_code, third.json()['code']), (429, 'RATE_LIMITED'))   # refused at acceptance, before any reservation
        view = self.c.get('/api/v1/agents/grants/' + g['grant_id'], headers=self.inst.h('owner')).json()
        self.assertEqual((view['counters']['jobs_created'], view['counters']['amount_reserved'], view['remaining']['jobs']), (2, 2, 0))
        self.assertEqual(len(view['obligations']['jobs_in_flight']), 2)
        self.assertEqual([j['id'] for j in view['obligations']['jobs_in_flight']], [first.json()['job_id'], second.json()['job_id']])
        # the agent reads its own job but is refused review decisions and credential admin
        self.assertEqual(self.c.get('/api/v1/jobs/' + first.json()['job_id'], headers=H).status_code, 200)
        self.assertEqual(self.c.post('/api/v1/agents/grants/' + g['grant_id'] + '/simulate', headers=self.inst.h('owner'),
                                     json={'operations': [{'operation': 'invoke', 'service': 'temporal_energy', 'jobs': 1}, {'operation': 'action:create', 'amount': 1}]}).json()['decisions'][0]['reason'], 'job ceiling')
        # the worker completes the jobs; counters are never decremented (conservative accounting)
        self.inst.worker().run_once(); self.inst.worker().run_once()
        view = self.c.get('/api/v1/agents/grants/' + g['grant_id'], headers=self.inst.h('owner')).json()
        self.assertEqual((view['counters']['jobs_created'], view['obligations']['jobs_in_flight']), (2, []))
        self.assertEqual(len(self.c.get('/api/v1/usage', headers=self.inst.h('owner')).json()['items']), 2)
        # stop: obligations remain visible, new mutations refused, reads still work; revoke kills the credential
        stopped = self.c.post('/api/v1/agents/grants/' + g['grant_id'] + '/stop', headers=self.inst.h('owner')).json()
        self.assertEqual(stopped['state'], 'stopped')
        self.assertEqual(self.c.post('/api/v1/services/' + self.sid + '/quote', headers=H, json={'inputs': TEMPORAL}).json()['code'], 'FORBIDDEN')
        self.assertEqual(self.c.get('/api/v1/jobs/' + first.json()['job_id'], headers=H).status_code, 200)
        self.c.post('/api/v1/agents/grants/' + g['grant_id'] + '/revoke', headers=self.inst.h('owner'))
        self.assertEqual(self.c.get('/api/v1/services', headers=H).status_code, 401)
        self.assertEqual(self.c.get('/api/v1/agents/grants', headers=self.inst.h('owner')).json()['items'][0]['state'], 'revoked')

    def test_policy_validation_and_issuer_bounds(self):
        bad = policy(); bad['allowed_operations'] = ['admin']
        self.assertEqual(self.c.post('/api/v1/agents/grants', headers=self.inst.h('owner'), json={'policy': bad}).status_code, 422)
        bad = policy(); bad['ceilings']['total_amount'] = -1
        self.assertEqual(self.c.post('/api/v1/agents/grants', headers=self.inst.h('owner'), json={'policy': bad}).status_code, 422)
        bad = policy(); bad['extra'] = 1
        self.assertEqual(self.c.post('/api/v1/agents/grants', headers=self.inst.h('owner'), json={'policy': bad}).status_code, 422)
        # a reviewer cannot issue grants; a viewer cannot either
        self.assertEqual(self.c.post('/api/v1/agents/grants', headers=self.inst.h('reviewer'), json={'policy': policy()}).status_code, 403)
        # a grant that needs job:submit cannot be issued by a principal that lacks it
        self.assertEqual(self.c.post('/api/v1/agents/grants', headers=self.inst.h('viewer'), json={'policy': policy()}).status_code, 403)
        # policy digest is deterministic
        a, _ = self.grant(); b, _ = self.grant()
        self.assertEqual(a['policy_digest'], b['policy_digest'])

    def test_shared_grant_concurrency_and_exhaustion_are_exact(self):
        g, H = self.grant(ceilings={'total_amount': 100, 'per_action_amount': 5, 'max_jobs': 3, 'max_workflows': 1, 'concurrency': 2})
        results = []
        def worker():
            results.append(self.invoke(H).status_code)
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(results), [202, 202, 429, 429, 429, 429], results)     # concurrency 2 admits exactly two
        view = self.c.get('/api/v1/agents/grants/' + g['grant_id'], headers=self.inst.h('owner')).json()
        self.assertEqual(view['counters']['jobs_created'], 2)
        self.assertEqual(len(self.c.get('/api/v1/jobs', headers=self.inst.h('owner')).json()['items']), 2)
        self.inst.worker().run_once(); self.inst.worker().run_once()
        # after completion one more fits the job ceiling (3), then the ceiling is exhausted for good
        self.assertEqual(self.invoke(H).status_code, 202)
        self.assertEqual(self.invoke(H).status_code, 429)

    def test_workflow_and_campaign_starts_are_charged_up_front(self):
        g, H = self.grant(allowed_operations=['services:read', 'quote', 'workflow:run', 'job:read'], ceilings={'total_amount': 0, 'per_action_amount': 0, 'max_jobs': 1, 'max_workflows': 1, 'concurrency': 2})
        definition = {'schema': workflows.SCHEMA, 'name': 'two temporal', 'nodes': [
            {'id': 't1', 'type': 'temporal_energy', 'inputs': TEMPORAL},
            {'id': 't2', 'type': 'temporal_energy', 'inputs': TEMPORAL, 'depends_on': ['t1']}], 'outputs': ['t2']}
        wid = self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': definition}).json()['id']
        r = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=H, json={})
        self.assertEqual((r.status_code, r.json()['code']), (429, 'RATE_LIMITED'))            # 2 service nodes > max_jobs 1
        one = dict(definition, nodes=definition['nodes'][:1], outputs=['t1'])
        wid1 = self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': one}).json()['id']
        self.assertEqual(self.c.post('/api/v1/workflows/' + wid1 + '/runs', headers=H, json={}).status_code, 202)
        self.assertEqual(self.c.post('/api/v1/workflows/' + wid1 + '/runs', headers=H, json={}).json()['code'], 'RATE_LIMITED')   # max_workflows 1
        view = self.c.get('/api/v1/agents/grants/' + g['grant_id'], headers=self.inst.h('owner')).json()
        self.assertEqual((view['counters']['jobs_created'], view['counters']['workflows_started']), (1, 1))


class RunnerOverTcp(unittest.TestCase):
    """The policy-limited runner as a separate process against a live socket (simulation provider mode)."""

    @classmethod
    def setUpClass(cls):
        cls.inst = Instance(); cls.port = free_port(); cls.base = 'http://127.0.0.1:%d' % cls.port
        cls.proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service', '--home', str(cls.inst.home), 'serve', '--port', str(cls.port)],
                                    cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        import httpx
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
        p = subprocess.run([sys.executable, '-m', 'metacoin_service.agent_runner', '--base', self.base, '--credential-file', str(cred), '--policy-file', str(pf), '--checkpoint', str(cp), cmd, *extra],
                           cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith('{') else p.stdout + p.stderr)

    def test_plan_execute_resume_and_revocation_between_plan_and_dispatch(self):
        H = self.inst.h('owner')
        pol = policy()
        g = self.http.post('/api/v1/agents/grants', headers=H, json={'policy': pol}).json()
        cred, pf, inp, cp = self.files('a', g['token'], pol)
        # plan: discovery + validation + quote, nothing accepted or signed
        rc, plan = self.runner_cli('plan', cred, pf, cp, '--service', 'temporal_energy', '--inputs-file', str(inp))
        self.assertEqual(rc, 0, plan)
        self.assertEqual((plan['plan'], plan['within_policy'], plan['signed_anything'], plan['max_exposure']['amount']), (True, True, False, 1))
        self.assertEqual(self.http.get('/api/v1/quotes/' + plan['quote_id'], headers=H).json()['state'], 'offered')
        self.assertTrue(os.stat(cp).st_mode & 0o077 == 0)
        # interruption after acceptance: simulate by accepting via the checkpointed quote, then killing before invoke
        runner = agent_runner.Runner(self.base, g['token'], pol, str(cp))
        class Interrupted(Exception):
            pass
        original = runner.remember
        def remember_then_die(step, value):
            original(step, value)
            if step == 'accepted':
                raise Interrupted()
        runner.remember = remember_then_die
        with self.assertRaises(Interrupted):
            runner.execute('temporal_energy', TEMPORAL)
        self.assertEqual(self.http.get('/api/v1/quotes/' + plan['quote_id'], headers=H).json()['state'], 'accepted')
        # resume from the checkpoint: the stored acceptance is reused, one invocation happens, the job completes
        rc, out = self.runner_cli('execute', cred, pf, cp, '--service', 'temporal_energy', '--inputs-file', str(inp))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out['executed'])
        jid = out['job']['job_id']
        rc, again = self.runner_cli('execute', cred, pf, cp, '--service', 'temporal_energy', '--inputs-file', str(inp))
        self.assertEqual(again['job']['job_id'], jid)                                       # idempotent resume: same job, no second invocation
        subprocess.run([sys.executable, '-m', 'metacoin_service', '--home', str(self.inst.home), 'worker', '--once'], cwd=ROOT, env=ENV, check=True, capture_output=True, timeout=120)
        rc, status = self.runner_cli('follow', cred, pf, cp)
        self.assertEqual((status['state'], status['outcome'], status['result_available']), ('succeeded', 'withheld-by-policy-or-not-yet-reviewed', False))   # review gate first
        self.assertEqual(self.http.post('/api/v1/jobs/' + jid + '/review-request', headers=H).status_code, 200)
        self.assertEqual(self.http.post('/api/v1/reviews/' + jid + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'accepted'}).status_code, 200)
        rc, status = self.runner_cli('follow', cred, pf, cp)
        self.assertEqual((status['state'], status['outcome'], status['result_available']), ('succeeded', 'FEASIBLE', True))
        view = self.http.get('/api/v1/agents/grants/' + g['grant_id'], headers=H).json()
        self.assertEqual((view['counters']['jobs_created'], view['counters']['amount_reserved'], view['counters']['invocations']), (1, 1, 3))   # quote + accept + invoke each pass the guard
        # revoked between plan and dispatch: a second agent plans, the operator revokes, execution is refused server-side
        g2 = self.http.post('/api/v1/agents/grants', headers=H, json={'policy': pol}).json()
        cred2, pf2, inp2, cp2 = self.files('b', g2['token'], pol)
        rc, plan2 = self.runner_cli('plan', cred2, pf2, cp2, '--service', 'temporal_energy', '--inputs-file', str(inp2))
        self.assertTrue(plan2['plan'])
        self.http.post('/api/v1/agents/grants/' + g2['grant_id'] + '/revoke', headers=H)
        rc, out2 = self.runner_cli('execute', cred2, pf2, cp2, '--service', 'temporal_energy', '--inputs-file', str(inp2))
        self.assertEqual(rc, 2)
        self.assertFalse(out2['executed'])
        self.assertEqual((out2.get('refusal') or out2['plan']['refusal'])['code'], 'UNAUTHENTICATED')   # the credential itself is dead
        self.assertEqual(self.http.get('/api/v1/quotes/' + plan2['quote_id'], headers=H).json()['state'], 'offered')
        # stopped (not revoked) between plan and dispatch: refused with a policy reason, obligations remain readable
        g3 = self.http.post('/api/v1/agents/grants', headers=H, json={'policy': pol}).json()
        cred3, pf3, inp3, cp3 = self.files('c', g3['token'], pol)
        self.runner_cli('plan', cred3, pf3, cp3, '--service', 'temporal_energy', '--inputs-file', str(inp3))
        self.http.post('/api/v1/agents/grants/' + g3['grant_id'] + '/stop', headers=H)
        rc, out3 = self.runner_cli('execute', cred3, pf3, cp3, '--service', 'temporal_energy', '--inputs-file', str(inp3))
        self.assertEqual((out3['executed'], out3['refusal']['code']), (False, 'FORBIDDEN'))
        # a checkpoint from another policy is refused
        other = dict(pol, ceilings=dict(pol['ceilings'], max_jobs=99))
        pf_other = Path(self.inst.temp.name) / 'a' / 'policy2.json'; pf_other.write_text(json.dumps(other)); os.chmod(pf_other, 0o600)
        rc, msg = self.runner_cli('status', cred, pf_other, cp)
        self.assertNotEqual(rc, 0); self.assertIn('different policy', msg)


if __name__ == '__main__':
    unittest.main()
