"""Server-sent events with cursors over a real socket, plus cursor polling."""
import json
import subprocess
import sys
import threading
import time
import unittest
import httpx
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV


class EventStreamOverTcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = Instance(); cls.port = free_port(); cls.base = 'http://127.0.0.1:%d' % cls.port
        cls.proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service', '--home', str(cls.inst.home), 'serve', '--port', str(cls.port)],
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

    def read_events(self, headers, params, stop_after_types, timeout=20):
        """Consume the stream until every wanted event type was seen (or the server ends it)."""
        got = []
        with self.http.stream('GET', '/api/v1/events/stream', headers=headers, params=params, timeout=timeout) as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers['content-type'].startswith('text/event-stream'))
            current = {}
            for line in r.iter_lines():
                if line.startswith(':'):
                    continue
                if line == '':
                    if current:
                        got.append(current); current = {}
                    if stop_after_types <= {e.get('event') for e in got} or any(e.get('event') == 'end' for e in got):
                        break
                    continue
                k, _, v = line.partition(': ')
                current[k] = v
        return got

    def test_stream_cursor_resume_and_poll(self):
        H = self.inst.h('owner')
        self.assertEqual(self.http.get('/api/v1/events/stream', headers={'Authorization': 'Bearer nope'}).status_code, 401)
        # a job submitted while the stream is open arrives as a typed event with a sequence id
        cid = self.inst.contract()
        def submit_later():
            time.sleep(0.5)
            self.http.post('/api/v1/jobs', headers=H, json={'contract_id': cid})
        t = threading.Thread(target=submit_later); t.start()
        got = self.read_events(H, {'after': 0, 'max_seconds': 15}, {'job.queued'})
        t.join()
        types = [e['event'] for e in got]
        self.assertIn('contract.created', types); self.assertIn('job.queued', types)
        queued = [e for e in got if e['event'] == 'job.queued'][0]
        data = json.loads(queued['data'])
        self.assertEqual((data['object_type'], data['seq']), ('job', int(queued['id'])))
        self.assertNotIn('USER_PRIVATE_LABEL', json.dumps(got))                                # inputs never leak through events
        # resume from the last id: nothing already delivered is repeated, later events continue
        last = int(got[-1]['id'])
        jid = data['object_id']
        subprocess.run([sys.executable, '-m', 'metacoin_service', '--home', str(self.inst.home), 'worker', '--once'], cwd=ROOT, env=ENV, check=True, capture_output=True, timeout=120)
        resumed = self.read_events(dict(H, **{'Last-Event-ID': str(last)}), {'types': 'job.claimed,job.result_committed', 'max_seconds': 10}, {'job.result_committed'})
        self.assertTrue(all(int(e['id']) > last for e in resumed if 'id' in e), resumed)
        self.assertEqual([e['event'] for e in resumed], ['job.claimed', 'job.result_committed'])
        self.assertEqual(json.loads(resumed[-1]['data'])['object_id'], jid)
        # type filtering applies to the poll endpoint too, and the cursor advances
        poll = self.http.get('/api/v1/events', headers=H, params={'after': last, 'types': 'job.result_committed'}).json()
        self.assertEqual([i['event_type'] for i in poll['items']], ['job.result_committed'])
        self.assertGreater(poll['cursor'], last)
        self.assertEqual(self.http.get('/api/v1/events', headers=H, params={'after': 'x'}).status_code, 422)
        # a viewer sees the same administrative stream; a bounded stream ends with an end event carrying the cursor
        ended = self.read_events(self.inst.h('viewer'), {'after': poll['latest'], 'max_seconds': 1}, {'end'})
        self.assertEqual(ended[-1]['event'], 'end')
        self.assertEqual(json.loads(ended[-1]['data'])['cursor'], poll['latest'])


if __name__ == '__main__':
    unittest.main()
