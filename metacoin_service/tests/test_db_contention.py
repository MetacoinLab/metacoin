"""Write-lock contention (order 2026-10-23 §18, endurance defects found 2026-10-23): a lock-wait timeout must surface as the
retryable JOURNAL_BUSY refusal (503, Retry-After, nothing begun) — never as LOCAL_OPERATION_FAILED masked by a failing ROLLBACK;
the in-process write gate keeps many threads from starving one another; the worker survives a busy database instead of exiting.

Run:  METACOIN_X402_DISABLED=1 PYTHONPATH=. .venv-service/bin/python -m unittest metacoin_service.tests.test_db_contention -v
"""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from metacoin_service import db as database
from metacoin_service.errors import from_exception
from metacoin_service.tests.test_service import Instance


class TransactionManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / 'c.sqlite'
        c = database.open_db(self.path, create=True); c.execute('CREATE TABLE t(x INTEGER)'); c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _hold(self):
        holder = database.open_db(self.path); holder.execute('BEGIN IMMEDIATE'); holder.execute('INSERT INTO t VALUES (1)'); return holder

    def test_lock_wait_timeout_surfaces_as_journal_busy_not_masked_by_rollback(self):
        holder = self._hold()
        try:
            D = database.Database(self.path, busy_timeout_ms=300); t0 = time.time()
            with self.assertRaises(sqlite3.OperationalError) as cm:
                with D.tx() as x:
                    x.execute('INSERT INTO t VALUES (2)')
            self.assertIn('locked', str(cm.exception)); self.assertNotIn('rollback', str(cm.exception))
            self.assertTrue(database.is_busy(cm.exception)); self.assertGreaterEqual(time.time() - t0, 0.25); self.assertEqual(D.busy_count, 1)
            err = from_exception(cm.exception)
            self.assertEqual((err.code, err.status), ('JOURNAL_BUSY', 503)); self.assertTrue(err.detail['retryable']); self.assertFalse(err.detail['begun'])
        finally:
            holder.execute('ROLLBACK'); holder.close()
        with database.Database(self.path, busy_timeout_ms=300).tx() as x:                       # the same request succeeds once the lock is free
            x.execute('INSERT INTO t VALUES (2)')
        with database.Database(self.path).read() as x:
            self.assertEqual([r[0] for r in x.execute('SELECT x FROM t ORDER BY x')], [2])        # the holder's row was rolled back; ours committed once

    def test_failure_inside_the_transaction_still_rolls_back(self):
        D = database.Database(self.path)
        with self.assertRaises(ValueError):
            with D.tx() as x:
                x.execute('INSERT INTO t VALUES (3)'); raise ValueError('inside')
        with D.read() as x:
            self.assertEqual(x.execute('SELECT COUNT(*) FROM t').fetchone()[0], 0)

    def test_in_process_write_gate_prevents_starvation(self):
        """24 threads × 10 short transactions (≈ 9 ms each): under strict FIFO no acquisition waits longer than one round of the queue
        (≈ 0.25 s), so a 1 s busy timeout never fires; without the gate SQLite's polling busy handler lets fresh arrivals overtake
        long waiters, which is what the endurance runs exposed. The ungated half only asserts what KIND of failure can occur."""
        def storm(D, threads=24, per_thread=10, work_s=0.004):
            failures = []; done = []
            def run(i):
                for j in range(per_thread):
                    try:
                        with D.tx() as x:
                            x.execute('INSERT INTO t VALUES (?)', (i * 1000 + j,)); time.sleep(work_s)
                        done.append(1)
                    except sqlite3.OperationalError as exc:
                        failures.append(str(exc))
            ts = [threading.Thread(target=run, args=(i,)) for i in range(threads)]; [t.start() for t in ts]; [t.join() for t in ts]
            return len(done), failures
        gated = database.Database(self.path, busy_timeout_ms=1000)
        n, failures = storm(gated)
        self.assertEqual(failures, []); self.assertEqual(n, 24 * 10)
        with gated.read() as x:
            self.assertEqual(x.execute('SELECT COUNT(*) FROM t').fetchone()[0], 24 * 10)
        self.assertGreater(database._gate_for(self.path).waiters_max, 1)                         # the queue was really exercised
        ungated = database.Database(self.path, busy_timeout_ms=1000); ungated.gate_enabled = False
        n2, failures2 = storm(ungated)
        self.assertEqual(n2 + len(failures2), 24 * 10)
        self.assertTrue(all('locked' in f and 'rollback' not in f for f in failures2))            # whatever fails without the gate fails as a lock timeout, never as 'cannot rollback'
        print('\nungated lock timeouts under the same storm: %d of %d' % (len(failures2), 24 * 10))

    def test_fair_gate_is_fifo_and_tolerates_abandoned_waiters(self):
        g = database.FairGate(); order = []
        self.assertTrue(g.acquire(1.0))
        def waiter(i, timeout):
            if g.acquire(timeout):
                order.append(i); time.sleep(0.01); g.release()
            else:
                order.append(-i)
        ts = []
        for i in (1, 2, 3):
            t = threading.Thread(target=waiter, args=(i, 5.0)); t.start(); ts.append(t); time.sleep(0.02)
        quitter = threading.Thread(target=waiter, args=(4, 0.05)); quitter.start(); quitter.join()                 # gives up while queued behind 1–3
        g.release(); [t.join() for t in ts]
        self.assertEqual(order, [-4, 1, 2, 3])


class ApiBusyRefusalTests(unittest.TestCase):
    def test_api_returns_503_journal_busy_with_retry_after_and_logs_it_as_busy_not_defect(self):
        os.environ['METACOIN_DB_BUSY_TIMEOUT_MS'] = '300'; database.DEFAULT_BUSY_TIMEOUT_MS = 300
        try:
            inst = Instance()
            holder = database.open_db(inst.settings.db_path); holder.execute('BEGIN IMMEDIATE')
            try:
                r = inst.client.get('/api/v1/capabilities', headers=inst.h('owner'))
            finally:
                holder.execute('ROLLBACK'); holder.close()
            self.assertEqual(r.status_code, 503, r.text); body = r.json()
            self.assertEqual(body['code'], 'JOURNAL_BUSY'); self.assertTrue(body['detail']['retryable']); self.assertFalse(body['detail']['begun'])
            self.assertEqual(r.headers.get('Retry-After'), '1'); self.assertEqual(r.headers.get('Cache-Control'), 'no-store')
            self.assertFalse((inst.settings.logs_dir / 'defects.log').exists())
            busy = (inst.settings.logs_dir / 'busy.log'); self.assertTrue(busy.exists()); self.assertIn('JOURNAL_BUSY GET /api/v1/capabilities', busy.read_text())
            r2 = inst.client.get('/api/v1/capabilities', headers=inst.h('owner')); self.assertEqual(r2.status_code, 200)   # the identical request succeeds once the lock is free
        finally:
            os.environ.pop('METACOIN_DB_BUSY_TIMEOUT_MS', None); database.DEFAULT_BUSY_TIMEOUT_MS = 15000


class WorkerBusyTests(unittest.TestCase):
    def test_worker_step_survives_lock_wait_timeout_and_runs_the_job_afterwards(self):
        os.environ['METACOIN_DB_BUSY_TIMEOUT_MS'] = '300'; database.DEFAULT_BUSY_TIMEOUT_MS = 300
        try:
            inst = Instance(); jid = inst.job(); w = inst.worker(); w.busy_streak = 0
            holder = database.open_db(inst.settings.db_path); holder.execute('BEGIN IMMEDIATE')
            try:
                ran, advanced, _ = w.step(0)                                        # claim times out: logged, loop continues, no exception
            finally:
                holder.execute('ROLLBACK'); holder.close()
            self.assertIsNone(ran); self.assertEqual(w.busy_streak, 1)
            self.assertIn('JOURNAL_BUSY worker', (inst.settings.logs_dir / 'busy.log').read_text())
            ran, advanced, _ = w.step(0)
            self.assertEqual(ran[0], jid); self.assertEqual(w.busy_streak, 0)
            with inst.worker().db.read() as db:
                self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0], 'succeeded')
                self.assertEqual(db.execute('SELECT state FROM workers WHERE id=?', (w.worker_id,)).fetchone()[0], 'active')
        finally:
            os.environ.pop('METACOIN_DB_BUSY_TIMEOUT_MS', None); database.DEFAULT_BUSY_TIMEOUT_MS = 15000

    def test_worker_gives_up_only_after_a_long_busy_streak(self):
        inst = Instance(); w = inst.worker(); w.busy_streak = w.BUSY_GIVE_UP_AFTER - 1; w.db.busy_timeout_ms = 200
        holder = database.open_db(inst.settings.db_path); holder.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(sqlite3.OperationalError):
                w.step(0)
        finally:
            holder.execute('ROLLBACK'); holder.close()


if __name__ == '__main__':
    unittest.main()
