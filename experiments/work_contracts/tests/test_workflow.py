"""Analytical, differential, adversarial and failure-injection verification."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from fractions import Fraction
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import acceptance, cli, contract, demo, energy_analysis as energy, fixtures
from experiments.work_contracts.execution_state import Journal
from integrations.x402.legacy_adapter import LegacyAdapter

NOW = 1_900_000_000


def reference_outcome(data):
    """Independent reference in watt-hours, with exact rational arithmetic."""
    demand = []
    for bound in ('power_low', 'power_high'):
        load_wh = sum((Fraction(row[bound], 1000) * Fraction(row['duration'], 3600)
                       for row in data['segments']), Fraction(0))
        demand.append(load_wh + Fraction(data['reserve'], 3_600_000))
    available = [Fraction(data[name], 3_600_000) for name in ('available_low', 'available_high')]
    if demand[1] <= available[0]:
        return 'FEASIBLE'
    if demand[0] > available[1]:
        return 'INFEASIBLE'
    return 'INDETERMINATE'


class EnergyTests(unittest.TestCase):
    def test_hand_calculated_examples_and_equalities(self):
        for outcome in energy.OUTCOMES:
            actual = energy.analyze(fixtures.inputs(outcome))
            self.assertEqual(actual['outcome'], outcome)
            self.assertEqual((actual['required_low'], actual['required_high']), (580000, 700000))
        for value, expected in [(700000, 'FEASIBLE'), (580000, 'INDETERMINATE'), (579999, 'INFEASIBLE')]:
            data = fixtures.inputs()
            data['available_low'] = data['available_high'] = value
            self.assertEqual(energy.analyze(data)['outcome'], expected)

    def test_independent_reference_and_properties_on_seeded_inputs(self):
        rng = random.Random(73019)
        rank = {'INFEASIBLE': 0, 'INDETERMINATE': 1, 'FEASIBLE': 2}
        for _ in range(200):
            data = fixtures.inputs()
            data['segments'] = []
            for __ in range(rng.randint(1, 10)):
                low = rng.randint(0, 5000)
                data['segments'].append({'power_low': low, 'power_high': low + rng.randint(0, 5000),
                                         'duration': rng.randint(2, 10000)})
            data['reserve'] = rng.randint(0, 100000)
            data['available_low'] = rng.randint(0, 100_000_000)
            data['available_high'] = data['available_low'] + rng.randint(0, 100_000_000)
            initial = energy.analyze(data)['outcome']
            self.assertEqual(initial, reference_outcome(data))
            more_load = deepcopy(data)
            more_load['segments'][0]['power_low'] += 100
            more_load['segments'][0]['power_high'] += 100
            more_load['reserve'] += 100
            self.assertLessEqual(rank[energy.analyze(more_load)['outcome']], rank[initial])
            more_energy = deepcopy(data)
            more_energy['available_low'] += 1_000_000
            more_energy['available_high'] += 1_000_000
            self.assertGreaterEqual(rank[energy.analyze(more_energy)['outcome']], rank[initial])
            wider = deepcopy(data)
            wider['available_low'] = 0
            wider['available_high'] += 1_000_000_000
            for row in wider['segments']:
                row['power_low'] = 0
                row['power_high'] += 10000
            if initial == 'INDETERMINATE':
                self.assertEqual(energy.analyze(wider)['outcome'], initial)
            split = deepcopy(data)
            first = split['segments'].pop(0)
            part = dict(first, duration=1)
            split['segments'][:0] = [part, dict(first, duration=first['duration'] - 1)]
            self.assertEqual(energy.analyze(split), energy.analyze(data))
            scaled = deepcopy(data)
            for name in ('available_low', 'available_high', 'reserve'):
                scaled[name] *= 10
            for row in scaled['segments']:
                row['power_low'] *= 10
                row['power_high'] *= 10
            self.assertEqual(energy.analyze(scaled)['outcome'], initial)

    def test_units_and_invalid_model_domain(self):
        data = fixtures.inputs()
        data.update(available_low=3_600_000, available_high=3_600_000, reserve=0,
                    segments=[{'duration': 3600, 'power_low': 1000, 'power_high': 1000}])
        self.assertEqual(energy.analyze(data)['required_high'], 3_600_000)
        data.update(available_low=1, available_high=1,
                    segments=[{'duration': 1, 'power_low': 1, 'power_high': 1}])
        self.assertEqual(energy.analyze(data)['required_high'], 1)
        mutations = [dict(reserve=True), dict(reserve=-1), dict(reserve=1.0),
                     dict(available_low=2, available_high=1), dict(segments=[]),
                     dict(segments=[{'duration': 0, 'power_low': 0, 'power_high': 1}]),
                     dict(segments=[{'duration': 2, 'power_low': 2, 'power_high': 1}]),
                     dict(segments=[{'duration': energy.LIMIT, 'power_low': 2, 'power_high': 2}]),
                     dict(segments=[{'duration': 1, 'power_low': 0, 'power_high': 1}] * 129),
                     dict(units={'energy': 'Wh', 'power': 'W', 'duration': 's'}),
                     dict(private_label='x' * 129), dict(accepted=True)]
        for changes in mutations:
            with self.subTest(changes=list(changes)), self.assertRaises(merkle.Invalid):
                energy.analyze(dict(data, **changes))

    def test_strict_json_ingress(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1.0}',
                    '{"x":9007199254740992}', '[' * 30 + '0' + ']' * 30,
                    ' ' * (merkle.MAX_FILE + 1)):
            with self.assertRaises(merkle.Invalid):
                merkle.parse(raw)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.terms, self.input_vault, self.evidence = fixtures.prepare('evidence-job', 'INFEASIBLE')
        self.pin = contract.digest(self.terms)

    def audit(self, terms=None, inputs=None, evidence=None):
        return acceptance.audit(terms or self.terms, self.pin, inputs or self.input_vault, evidence or self.evidence)

    def test_negative_work_and_public_proof_boundaries(self):
        result = self.audit()
        self.assertTrue(result['work_completed'])
        self.assertEqual(result['scientific_outcome'], 'INFEASIBLE')
        public = acceptance.verify_public(self.terms, self.pin, result['bundle'], result['evidence_root'])
        self.assertFalse(public['task_correctness_proven'])
        self.assertFalse(public['issuer_authenticated'])
        self.assertFalse(public['spend_permitted'])

    def test_feasible_only_policy_and_optional_outcome(self):
        terms, inputs, evidence = fixtures.prepare('restricted', 'INFEASIBLE',
                                                    accepted_outcomes=('FEASIBLE',), disclose_outcome=False)
        result = acceptance.audit(terms, contract.digest(terms), inputs, evidence)
        self.assertFalse(result['work_completed'])
        values = merkle.verify(result['bundle'], result['evidence_root'])
        self.assertNotIn('outcome', values)
        extra = merkle.disclose(evidence, list(contract.BINDINGS) + ['outcome'])
        with self.assertRaises(merkle.Invalid):
            acceptance.verify_public(terms, contract.digest(terms), extra, evidence['receipt']['root'])

    def test_substituted_contract_program_and_input(self):
        for key, value in [('input_root', '0' * 64), ('verifier_id', 'arbitrary.module'),
                           ('verifier_digest', 'f' * 64), ('evidence_kind', 'zk-proof'),
                           ('schema', 'future'), ('accepted', True), ('result_schema', 'future')]:
            terms = deepcopy(self.terms)
            terms[key] = value
            with self.subTest(key=key), self.assertRaises(merkle.Invalid):
                self.audit(terms=terms)
        _, another_input = merkle.commit({'inputs': fixtures.inputs('FEASIBLE')})
        with self.assertRaises(merkle.Invalid):
            self.audit(inputs=another_input)
        terms = deepcopy(self.terms)
        terms['required_disclosures'].append('audit_details')
        with self.assertRaises(merkle.Invalid):
            contract.validate(terms)

    def test_full_audit_rejects_committed_lies_extra_fields_and_duplicate_hidden_names(self):
        values = acceptance.full_values(self.evidence, self.evidence['receipt']['root'])
        for changes in ({'outcome': 'FEASIBLE'}, {'accepted': True}, {'input_root': '0' * 64},
                        {'contract_digest': '0' * 64}, {'verifier_digest': '0' * 64}):
            _, malicious = merkle.commit(dict(values, **changes))
            with self.assertRaises(merkle.Invalid):
                self.audit(evidence=malicious)
        malformed = deepcopy(self.evidence)
        malformed['fields'][1]['name'] = malformed['fields'][0]['name']
        with self.assertRaises(merkle.Invalid):
            self.audit(evidence=malformed)

    def test_public_tamper_missing_fields_root_substitution_and_kind(self):
        result = self.audit()
        for mode in ('outcome', 'root', 'kind', 'missing'):
            bundle = deepcopy(result['bundle'])
            if mode == 'outcome':
                next(x for x in bundle['disclosures'] if x['name'] == 'outcome')['value'] = 'FEASIBLE'
            elif mode == 'root':
                bundle['receipt']['root'] = '0' * 64
            elif mode == 'kind':
                bundle['receipt']['kind'] = 'zk-proof'
            else:
                bundle['disclosures'].pop()
            with self.subTest(mode=mode), self.assertRaises(merkle.Invalid):
                acceptance.verify_public(self.terms, self.pin, bundle, result['evidence_root'])

    def test_public_objects_do_not_contain_private_fields_or_canary(self):
        result = self.audit()
        encoded = json.dumps({'contract': self.terms, 'bundle': result['bundle']})
        for secret in ('SYNTHETIC_PRIVATE_CANARY_73', 'available_low', 'required_high', 'worst_margin'):
            self.assertNotIn(secret, encoded)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.state = Journal(self.path, 'campaign', 3)
        self.faucet = fixtures.funded_faucet(amount=10)
        self.adapter = LegacyAdapter(self.faucet)

    def add(self, job='job', outcome='FEASIBLE', **kwargs):
        terms, inputs, evidence = fixtures.prepare(job, outcome, expires_at=NOW + 100, **kwargs)
        self.state.register(terms, contract.digest(terms), 'local-owner', NOW)
        result = self.state.audit(job, inputs, evidence, 'local-auditor', NOW)
        return terms, inputs, evidence, result

    def test_registration_pinning_and_owner_authorization(self):
        terms, inputs, evidence = fixtures.prepare('job')
        with self.assertRaises(merkle.Invalid):
            self.state.register(terms, '0' * 64, 'local-owner', NOW)
        with self.assertRaises(merkle.Invalid):
            self.state.register(terms, contract.digest(terms), 'intruder', NOW)
        self.state.register(terms, contract.digest(terms), 'local-owner', NOW)
        self.state.register(terms, contract.digest(terms), 'local-owner', NOW)
        altered = deepcopy(terms)
        altered['action']['amount'] = altered['action']['limit'] = 2
        with self.assertRaises(merkle.Invalid):
            self.state.register(altered, contract.digest(altered), 'local-owner', NOW)
        with self.assertRaises(merkle.Invalid):
            self.state.audit('job', inputs, evidence, 'intruder', NOW)
        with self.assertRaises(merkle.Invalid):
            self.state.request('job', 'request')

    def test_identical_retry_single_spend_and_no_new_entitlement(self):
        self.add()
        request = self.state.request('job', 'request')
        first = self.state.dispatch(request, 'agent-fixture', self.adapter, NOW)
        self.assertEqual(first['state'], 'CONFIRMED')
        self.assertEqual(self.state.dispatch(request, 'agent-fixture', self.adapter, NOW), first)
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 9)
        with self.assertRaises(merkle.Invalid):
            self.state.dispatch(self.state.request('job', 'fresh-nonce'), 'agent-fixture', self.adapter, NOW)
        self.assertEqual(self.state.exposure(), 1)

    def test_rebinding_each_action_field_and_forged_accepted(self):
        self.add()
        request = self.state.request('job', 'request')
        changes = {'contract_digest': '0' * 64, 'evidence_root': '0' * 64, 'actor': 'other',
                   'recipient': 'other', 'resource': 'other', 'amount': 2, 'asset': 'BTC',
                   'network': 'mainnet', 'capability': 'http_transport_test', 'expires_at': NOW + 999,
                   'accepted': True}
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(merkle.Invalid):
                self.state.dispatch(dict(request, **{key: value}), 'agent-fixture', self.adapter, NOW)
        self.assertEqual(self.state.exposure(), 0)
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 10)

    def test_actor_expiry_and_rejected_science_cannot_spend(self):
        terms, inputs, evidence, result = self.add()
        request = self.state.request('job', 'request')
        for actor, now in [('other', NOW), ('agent-fixture', NOW + 100)]:
            with self.assertRaises(merkle.Invalid):
                self.state.dispatch(request, actor, self.adapter, now)
        with self.assertRaises(merkle.Invalid):
            self.state.audit('job', inputs, evidence, 'local-auditor', NOW + 100)
        self.add('rejected', 'INFEASIBLE', accepted_outcomes=('FEASIBLE',))
        with self.assertRaises(merkle.Invalid):
            self.state.request('rejected', 'denied-request')

    def test_expiry_between_reservation_and_dispatch_releases_without_spend(self):
        self.add()
        request = self.state.request('job', 'request')
        with patch('experiments.work_contracts.execution_state.clock', side_effect=[NOW, NOW + 100]):
            result = self.state.dispatch(request, 'agent-fixture', self.adapter)
        self.assertEqual(result['state'], 'FAILED_CONFIRMED')
        self.assertEqual(result['result']['reference'], 'local-expired-before-dispatch')
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 10)
        self.assertEqual(self.state.exposure(), 0)

    def test_payment_metadata_excludes_private_values_and_status_is_scoped(self):
        self.add()
        request = self.state.request('job', 'request')
        with patch.object(self.adapter, 'submit', wraps=self.adapter.submit) as submit:
            self.state.dispatch(request, 'agent-fixture', self.adapter, NOW)
        serialized = json.dumps(submit.call_args.args[0])
        for secret in ('SYNTHETIC_PRIVATE_CANARY_73', 'available_low', 'audit_details', 'segments'):
            self.assertNotIn(secret, serialized)
        with self.assertRaises(merkle.Invalid):
            self.state.reconcile('request', 'different-actor', self.adapter)
        for malformed in (None, [], True, 'request'):
            with self.assertRaises(merkle.Invalid):
                self.state.dispatch(malformed, 'agent-fixture', self.adapter, NOW)

    def test_new_result_root_cannot_replace_recorded_audit(self):
        terms, inputs, evidence, _ = self.add()
        _, another = acceptance.execute(terms, contract.digest(terms), inputs)
        with self.assertRaises(merkle.Invalid):
            self.state.audit('job', inputs, another, 'local-auditor', NOW)

    def test_concurrent_identical_requests_dispatch_once(self):
        self.add()
        request = self.state.request('job', 'request')
        def worker(_):
            journal = Journal(self.path, 'campaign', 3)
            return journal.dispatch(request, 'agent-fixture', self.adapter, NOW)
        with patch.object(self.adapter, 'submit', wraps=self.adapter.submit) as submit:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(worker, range(16)))
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.state.status('request', 'agent-fixture')['state'], 'CONFIRMED')
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 9)
        self.assertEqual(self.state.exposure(), 1)
        with self.assertRaises(merkle.Invalid):
            self.state.status('request', 'other')

    def test_concurrent_jobs_enforce_campaign_cap(self):
        self.add('a', amount=2)
        self.add('b', amount=2)
        requests = [self.state.request(x, 'request-' + x) for x in ('a', 'b')]
        def worker(request):
            try:
                return Journal(self.path, 'campaign', 3).dispatch(request, 'agent-fixture', self.adapter, NOW)['state']
            except merkle.Invalid:
                return 'REFUSED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, requests))
        self.assertEqual(sorted(results), ['CONFIRMED', 'REFUSED'])
        self.assertEqual(self.state.exposure(), 2)
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 8)

    def test_request_id_cannot_be_reused_for_another_job(self):
        self.add('a')
        self.add('b')
        self.state.dispatch(self.state.request('a', 'same'), 'agent-fixture', self.adapter, NOW)
        with self.assertRaises(merkle.Invalid):
            self.state.dispatch(self.state.request('b', 'same'), 'agent-fixture', self.adapter, NOW)

    def test_failed_confirmed_releases_budget_without_granting_compute(self):
        self.add()
        from demo.test_meta_faucet import _Faucet
        result = self.state.dispatch(self.state.request('job', 'request'), 'agent-fixture', LegacyAdapter(_Faucet()), NOW)
        self.assertEqual(result['state'], 'FAILED_CONFIRMED')
        self.assertEqual(result['result']['compute_units'], 0)
        self.assertEqual(self.state.exposure(), 0)

    def test_crash_before_dispatch_reservation_can_resume(self):
        self.add()
        request = self.state.request('job', 'request')
        with patch.object(self.state, '_claim', side_effect=SystemExit), self.assertRaises(SystemExit):
            self.state.dispatch(request, 'agent-fixture', self.adapter, NOW)
        self.assertEqual(self.state.status('request', 'agent-fixture')['state'], 'RESERVED')
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 10)
        recovered = Journal(self.path, 'campaign', 3)
        self.assertEqual(recovered.dispatch(request, 'agent-fixture', self.adapter, NOW)['state'], 'CONFIRMED')
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 9)

    def test_crash_after_dispatch_before_ack_fresh_adapter_stays_unknown(self):
        class Crash(LegacyAdapter):
            def submit(self, request):
                super().submit(request)
                raise SystemExit()
        self.add()
        request = self.state.request('job', 'request')
        with self.assertRaises(SystemExit):
            self.state.dispatch(request, 'agent-fixture', Crash(self.faucet), NOW)
        self.assertEqual(self.state.status('request', 'agent-fixture')['state'], 'SUBMISSION_PENDING')
        recovered = Journal(self.path, 'campaign', 3)
        fresh = LegacyAdapter(self.faucet)
        self.assertEqual(recovered.reconcile('request', 'agent-fixture', fresh)['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(recovered.dispatch(request, 'agent-fixture', fresh, NOW)['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 9)
        self.assertEqual(recovered.exposure(), 1)

    def test_expired_unsubmitted_reservation_can_be_reconciled_without_dispatch(self):
        self.add()
        request = self.state.request('job', 'request')
        with patch.object(self.state, '_claim', side_effect=SystemExit), self.assertRaises(SystemExit):
            self.state.dispatch(request, 'agent-fixture', self.adapter, NOW)
        recovered = Journal(self.path, 'campaign', 3)
        result = recovered.reconcile('request', 'agent-fixture', self.adapter, NOW + 100)
        self.assertEqual(result['state'], 'FAILED_CONFIRMED')
        self.assertEqual(recovered.exposure(), 0)
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 10)

    def test_crash_after_ack_before_persistence_reconciles_without_spend(self):
        self.add()
        request = self.state.request('job', 'request')
        with patch.object(self.state, '_finish', side_effect=SystemExit), self.assertRaises(SystemExit):
            self.state.dispatch(request, 'agent-fixture', self.adapter, NOW)
        recovered = Journal(self.path, 'campaign', 3)
        self.assertEqual(recovered.reconcile('request', 'agent-fixture', self.adapter)['state'], 'CONFIRMED')
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 9)

    def test_lost_response_holds_budget_and_reconciles_only_from_adapter(self):
        self.add(amount=3)
        request = self.state.request('job', 'request')
        adapter = demo.LostAcknowledgement(self.faucet)
        self.assertEqual(self.state.dispatch(request, 'agent-fixture', adapter, NOW)['state'], 'OUTCOME_UNKNOWN')
        self.add('second')
        with self.assertRaises(merkle.Invalid):
            self.state.dispatch(self.state.request('second', 'second-request'), 'agent-fixture', adapter, NOW)
        self.assertEqual(self.state.reconcile('request', 'agent-fixture', adapter)['state'], 'CONFIRMED')
        self.assertEqual(self.state.exposure(), 3)
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 7)

    def test_wrong_adapter_ack_is_not_confirmation(self):
        class Wrong(LegacyAdapter):
            def submit(self, request):
                result = super().submit(request)
                result['request_digest'] = '0' * 64
                return result
        self.add()
        result = self.state.dispatch(self.state.request('job', 'request'), 'agent-fixture', Wrong(self.faucet), NOW)
        self.assertEqual(result['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(self.state.exposure(), 1)

    def test_immutable_campaign_and_private_file(self):
        with self.assertRaises(merkle.Invalid):
            Journal(self.path, 'campaign', 999)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.path.parent / 'symlink.sqlite'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            Journal(link, 'campaign', 3)


class InterfaceTests(unittest.TestCase):
    def test_demo_all_outcomes_and_unknown_without_private_canary(self):
        result = demo.run()
        self.assertEqual([x['outcome'] for x in result['cases']], list(energy.OUTCOMES))
        self.assertTrue(result['tampered_disclosure_refused'])
        self.assertEqual(result['lost_response']['state'], 'OUTCOME_UNKNOWN')
        self.assertNotIn('SYNTHETIC_PRIVATE_CANARY_73', json.dumps(result))

    def test_cli_refusal_redacts_private_input_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / 'PRIVATE_CANARY_PATH.json'
            private.write_text('{"PRIVATE_CANARY_VALUE":NaN}')
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(['prepare', '--input', str(private), '--out-dir', str(Path(tmp) / 'new'),
                                 '--job', 'job', '--expires-at', '2000000000'])
            self.assertEqual(code, 2)
            self.assertNotIn('PRIVATE_CANARY', stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(tmp, stdout.getvalue() + stderr.getvalue())

    def test_cli_owner_worker_auditor_public_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def invoke(*args):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(list(args)), 0)
                return json.loads(output.getvalue())
            invoke('fixture', '--out', str(root / 'input.json'), '--outcome', 'INFEASIBLE')
            prepared = invoke('prepare', '--input', str(root / 'input.json'), '--out-dir', str(root / 'owner'),
                              '--job', 'roundtrip', '--expires-at', '2000000000')
            terms = root / 'owner' / 'contract.json'
            vault = root / 'owner' / 'private-input-vault.json'
            pin = prepared['contract_digest']
            common = ('--contract', str(terms), '--expected-contract-digest', pin)
            invoke('execute', *common, '--input-vault', str(vault), '--out', str(root / 'private-evidence.json'))
            audited = invoke('audit', *common, '--input-vault', str(vault),
                             '--evidence-vault', str(root / 'private-evidence.json'), '--out', str(root / 'public.json'))
            public = invoke('verify', *common, '--bundle', str(root / 'public.json'),
                            '--expected-root', audited['expected_evidence_root'])
            self.assertTrue(public['membership_verified'])
            self.assertFalse(public['task_correctness_proven'])
            self.assertEqual(public['disclosed']['outcome'], 'INFEASIBLE')
            self.assertNotIn('SYNTHETIC_PRIVATE_CANARY_73', (root / 'public.json').read_text())
            self.assertEqual(vault.stat().st_mode & 0o777, 0o600)
