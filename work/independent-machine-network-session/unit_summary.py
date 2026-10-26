"""Run every unit suite and write verification-results.json + logs/unit-summary.txt (+ provider conformance v2 under the system interpreter)."""
import json, os, subprocess, sys, time
A = sys.argv[1]
suites = ['test_native_pools','test_native_custody','test_native_subcontracts','test_native_lots','test_native_products','test_native_relay','test_native_continuity','test_native_portfolio','test_native_onboarding','test_console_network','test_native_model_network','test_native_extensions','test_native_missions','test_native_market','test_native_signer','test_native_provider_client','test_native_lineage','test_native_domains','test_native_witness','test_native_conformance','test_native_planner','test_native_model','test_native_protocol','test_native_interfaces','test_native_evidence','test_native_lunar','test_work_money','test_service','test_mcp','test_console_pages','test_work_evidence','test_work_terms','test_work_board','test_work_faults','test_agent_paid','test_db_contention']
rows = []; lines = []
for s in suites:
    t0 = time.time(); p = subprocess.run([sys.executable, '-m', 'metacoin_service.tests.' + s], capture_output=True, text=True, timeout=1800, env={**os.environ, 'PYTHONPATH': '.', 'METACOIN_X402_DISABLED': '1'})
    tail = [l for l in p.stderr.splitlines() if l.startswith(('Ran ', 'OK', 'FAILED'))]; res = ' '.join(tail) if tail else ('rc=%d' % p.returncode); rows.append({'suite': s, 'result': res, 'rc': p.returncode, 'seconds': round(time.time() - t0, 1)}); lines.append('%-30s %s' % (s, res))
    open(os.path.join(A, 'logs', 'unit-%s.log' % s), 'w').write(p.stderr[-20000:])
rev = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
conf = json.loads(subprocess.run(['python3', '-m', 'metacoin_provider', 'conformance'], capture_output=True, text=True, cwd='provider_client', env={'PATH': os.environ['PATH'], 'PYTHONPATH': '.'}).stdout)
kit = json.loads(subprocess.run(['python3', 'provider_client/starter_kit/compat_test.py'], capture_output=True, text=True).stdout)
json.dump({'schema': 'metacoin-verification-results/v2', 'revision': rev, 'unit_suites': rows, 'all_ok': all(r['rc'] == 0 for r in rows), 'provider_conformance': {k: conf.get(k) for k in ('version', 'cases', 'passed', 'ok')}, 'starter_kit_compat': {k: kit.get(k) for k in ('cases', 'passed', 'skipped', 'ok')}}, open(os.path.join(A, 'verification-results.json'), 'w'), indent=1)
open(os.path.join(A, 'logs', 'unit-summary.txt'), 'w').write('\n'.join(lines) + '\n'); print('\n'.join(lines)); print('all ok', all(r['rc'] == 0 for r in rows), 'conformance', conf.get('passed'), '/', conf.get('cases'), 'kit', kit.get('passed'), '/', kit.get('cases'))
