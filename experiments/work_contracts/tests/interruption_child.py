"""Child process for interruption/concurrency tests. Terminates itself at one
exact point with os._exit (no cleanup) when --crash-at names it.

Points (in dispatch order; commit numbering starts at the first dispatch tx):
  before_reserve_commit  | after_reserve_commit | after_intent_commit
  after_effect_before_response | before_confirm_commit | after_confirm_commit
"""
import argparse
import json
import os
import sys
import time
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import refusals
from experiments.work_contracts.execution_state import Journal
from experiments.work_contracts.tests.durable_provider import DurableProvider

CRASH_EXIT = 7


class CrashingJournal(Journal):
    def __init__(self, *args, crash_at=None, **kwargs):
        self.crash_at = crash_at
        self.commits = 0
        self.armed = False
        super().__init__(*args, **kwargs)

    def _before_commit(self, db):
        point = {1: 'before_reserve_commit', 3: 'before_confirm_commit'}.get(self.commits + 1)
        if self.armed and point is not None and point == self.crash_at:
            os._exit(CRASH_EXIT)

    def _after_commit(self, db):
        if not self.armed:
            return
        self.commits += 1
        point = {1: 'after_reserve_commit', 2: 'after_intent_commit', 3: 'after_confirm_commit'}.get(self.commits)
        if point is not None and point == self.crash_at:
            os._exit(CRASH_EXIT)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--campaign', default='campaign')
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--actor', default='agent-fixture')
    parser.add_argument('--now', type=int, default=1_900_000_000)
    parser.add_argument('--crash-at', default=None)
    parser.add_argument('--barrier', default=None)
    parser.add_argument('--amnesiac', action='store_true')
    parser.add_argument('--operation', default='dispatch', choices=('dispatch', 'reconcile', 'status'))
    args = parser.parse_args(argv)
    request = merkle.read(args.request)
    journal = CrashingJournal(args.journal, args.campaign, args.limit, crash_at=args.crash_at)
    provider = DurableProvider(args.provider, durable=not args.amnesiac,
                               hook=lambda point: os._exit(CRASH_EXIT) if point == args.crash_at else None)
    if args.barrier:
        deadline = time.monotonic() + 10
        while not os.path.exists(args.barrier):
            if time.monotonic() > deadline:
                print(json.dumps({'error': 'barrier timeout'}))
                return 3
            time.sleep(0.002)
    journal.armed = True
    try:
        if args.operation == 'dispatch':
            result = journal.dispatch(request, args.actor, provider, args.now)
        elif args.operation == 'reconcile':
            result = journal.reconcile(request['request_id'], args.actor, provider, args.now)
        else:
            result = journal.status(request['request_id'], args.actor)
        print(json.dumps({'ok': True, 'result': result, 'pid': os.getpid()}))
        return 0
    except Exception as exc:
        code, message = refusals.classify(exc)
        print(json.dumps({'ok': False, 'code': code, 'pid': os.getpid()}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
