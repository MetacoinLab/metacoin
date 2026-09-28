"""A calibrated prediction: numeric dataset -> verified least-squares fit (worker job) -> prediction inside the fitted
domain -> an extrapolation that is labelled and refused for automatic use. Synthetic exact data (y = 3 x1 - 2 x2 + 5)."""
import json
import sys
from _client import call, must, wait_job

rows = [{'x1': i, 'x2': (i * 3) % 11, 'y': 3 * i - 2 * ((i * 3) % 11) + 5} for i in range(40)]
ds = must(*call('POST', '/api/v1/calibration/datasets', {'name': 'example exact', 'columns': ['x1', 'x2', 'y'], 'target': 'y', 'units': {'y': 'ms'}, 'rows': rows, 'provenance': 'synthetic example'}))
fit = must(*call('POST', '/api/v1/calibration/fits', {'inputs': {'dataset_id': ds['id'], 'features': ['x1', 'x2'], 'target': 'y', 'intercept': True, 'split': {'method': 'random', 'train_fraction_percent': 75, 'seed': 1}}}))
job = wait_job(fit['job_id'])
if job['state'] != 'succeeded':
    sys.exit(json.dumps({'fit': job['state'], 'error_code': job.get('error_code')}))
models = must(*call('GET', '/api/v1/calibration/models'))['items']
m = next(x for x in models if x['job_id'] == fit['job_id'])
inside = must(*call('POST', '/api/v1/calibration/models/' + m['id'] + '/predict', {'features': {'x1': 10, 'x2': 4}}))
outside = must(*call('POST', '/api/v1/calibration/models/' + m['id'] + '/predict', {'features': {'x1': 5000, 'x2': 4}}))
print(json.dumps({'dataset_id': ds['id'], 'model_id': m['id'], 'verification_passed': m['verification_passed'], 'eval_rmse': m['metrics']['eval']['rmse'],
                  'inside': {'prediction': inside['prediction'], 'domain_status': inside['domain_status'], 'interval': inside['interval']},
                  'outside': {'domain_status': outside['domain_status'], 'usable_for_scheduling': outside['usable_for_scheduling'], 'outside_domain': outside['outside_domain']},
                  'meaning': inside['meaning']}, indent=1))
