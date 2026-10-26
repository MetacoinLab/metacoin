"""Write CANDIDATE.json from the final journeys record (source candidate = the revision the journeys loaded)."""
import json, subprocess, sys
from pathlib import Path
A = Path(sys.argv[1]); J = json.load(open(A / 'journeys' / 'journeys-network.json')); C = json.load(open(A / 'CANDIDATE.json'))
rs = J['results']; req = [r for r in rs if r['journey'] <= 50]; ext = [r for r in rs if r['journey'] > 50]
C.update({'source_candidate': J['revision'], 'journeys_revision': J['revision'],
          'journeys': {'required': {'passed': sum(r['status'] == 'passed' for r in req), 'failed': sum(r['status'] == 'failed' for r in req), 'blocked': sum(r['status'] == 'blocked' for r in req), 'total': len(req)},
                       'extensions': {'passed': sum(r['status'] == 'passed' for r in ext), 'failed': sum(r['status'] == 'failed' for r in ext), 'total': len(ext)}},
          'unit_suites': json.load(open(A / 'verification-results.json'))['all_ok'] if (A / 'verification-results.json').exists() else None,
          'endurance_ok': json.load(open(A / 'benchmarks' / 'endurance-network.json'))['ok'] if (A / 'benchmarks' / 'endurance-network.json').exists() else None,
          'records_commit': (A / 'RECORDS_COMMIT.txt').read_text().strip() if (A / 'RECORDS_COMMIT.txt').exists() else None,
          'git_head_at_build': subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()})
json.dump(C, open(A / 'CANDIDATE.json', 'w'), indent=1); print(json.dumps({k: C[k] for k in ('source_candidate', 'journeys', 'unit_suites', 'endurance_ok', 'records_commit')}))
