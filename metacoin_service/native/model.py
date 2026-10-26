"""Bounded state-space model of the account/allocation protocol (order §18): two actors (alice on dom-a, bob on dom-b), small
integer amounts, and an event alphabet that includes retries, reordered/duplicated deliveries, crashes between durable steps
and attempted double use. Every reachable sequence up to a declared depth is enumerated; the invariants are checked in every
state. The implementation is compared with the model by replaying sequences (tests/test_native_model.py). Explored bounds
are stated in the report: bounded enumeration is not an unbounded proof."""
import itertools
import json

EVENTS = ('export_1', 'export_2', 'deliver_cert', 'deliver_cert_again', 'ack', 'ack_again', 'drop_cert', 'spend_at_b', 'return_req', 'deliver_return', 'deliver_return_again', 'crash_after_reserve', 'double_use')


class State:
    """Abstract state: balances per category and the allocation lifecycle (one allocation at a time keeps the space small)."""
    def __init__(self, fixture=3):
        self.a_avail = fixture; self.a_in_transit = 0; self.a_backing = 0            # dom-a: alice available, in transit, backing locked for the export
        self.b_claim_avail = 0; self.b_claim_in_transit = 0; self.b_burned = 0; self.carol = 0   # dom-b: bob's claim, carol's spend
        self.alloc = None                                                              # None | dict(state, amount)
        self.inflight = []                                                             # messages in the channel (type, allocation)
        self.fixture = fixture; self.log = []

    def copy(self):
        s = State.__new__(State); s.__dict__ = json.loads(json.dumps(self.__dict__)); return s

    def key(self):
        return json.dumps({k: v for k, v in self.__dict__.items() if k != 'log'}, sort_keys=True)

    def invariants(self):
        held_a = self.a_avail + self.a_in_transit; held_b = self.b_claim_avail + self.b_claim_in_transit + self.carol
        return {'conserved': held_a + self.a_backing == self.fixture, 'single_ownership': held_a + held_b <= self.fixture, 'claims_backed': held_b <= self.a_backing,
                'non_negative': all(v >= 0 for v in (self.a_avail, self.a_in_transit, self.a_backing, self.b_claim_avail, self.b_claim_in_transit, self.carol))}

    def apply(self, ev):
        """Returns a new state or None when the event is not enabled / is refused (refusals are states too: nothing changes)."""
        s = self.copy(); s.log.append(ev); A = s.alloc
        if ev in ('export_1', 'export_2'):
            n = 1 if ev == 'export_1' else 2
            if A is not None or s.a_avail < n: return s       # one allocation at a time; insufficient -> refused
            s.a_avail -= n; s.a_backing += n; s.alloc = {'state': 'certified', 'amount': n}; s.inflight.append('cert'); return s
        if ev == 'crash_after_reserve':
            if A is not None or s.a_avail < 1: return s
            s.a_avail -= 1; s.a_in_transit += 1; s.alloc = {'state': 'reserved', 'amount': 1}; return s
        if A is None: return s
        if ev == 'deliver_cert' or ev == 'deliver_cert_again':
            if A['state'] == 'reserved':                      # recovery resumes the export first (reserved -> certified)
                s.a_in_transit -= A['amount']; s.a_backing += A['amount']; A['state'] = 'certified'; s.inflight.append('cert')
            if 'cert' in s.inflight or ev == 'deliver_cert_again':
                if 'cert' in s.inflight: s.inflight.remove('cert')
                if A['state'] == 'certified':                 # first delivery credits; duplicates are idempotent
                    A['state'] = 'imported'; s.b_claim_avail += A['amount']; s.inflight.append('ack')
                elif A['state'] in ('imported', 'acknowledged', 'return_requested', 'returned'):
                    if 'ack' not in s.inflight and A['state'] == 'imported': s.inflight.append('ack')   # replayed ack
            return s
        if ev == 'drop_cert':
            if 'cert' in s.inflight: s.inflight.remove('cert'); s.inflight.append('cert')  # the sender keeps it queued: a drop only delays
            return s
        if ev in ('ack', 'ack_again'):
            if 'ack' in s.inflight: s.inflight.remove('ack')
            if A['state'] == 'imported': A['state'] = 'acknowledged'
            return s
        if ev == 'spend_at_b':
            if A['state'] in ('imported', 'acknowledged') and s.b_claim_avail >= 1: s.b_claim_avail -= 1; s.carol += 1; A['spent'] = True
            return s
        if ev == 'return_req':
            if A['state'] in ('imported', 'acknowledged') and not A.get('spent') and s.b_claim_avail >= A['amount']:
                s.b_claim_avail -= A['amount']; s.b_claim_in_transit += A['amount']; A['state'] = 'return_requested'; s.inflight.append('return')
            return s
        if ev in ('deliver_return', 'deliver_return_again'):
            if 'return' in s.inflight: s.inflight.remove('return')
            if A['state'] == 'return_requested':
                s.a_backing -= A['amount']; s.a_avail += A['amount']; s.b_claim_in_transit -= A['amount']; s.b_burned += A['amount']; A['state'] = 'returned'
            return s
        if ev == 'double_use':                                # attempt: spend the claim at B AND return it (or re-import the same cert)
            if A['state'] in ('imported', 'acknowledged') and s.b_claim_avail >= 1:
                s.b_claim_avail -= 1; s.carol += 1; A['spent'] = True
            return s                                           # the return request afterwards is refused by the model (spent)
        return s


def explore(depth=5, fixture=3):
    """BFS over event sequences up to `depth`; returns the report with counts, violations and explored bounds."""
    start = State(fixture); seen = {start.key(): []}; frontier = [start]; violations = []; transitions = 0; refusals = 0
    for d in range(depth):
        nxt = []
        for s in frontier:
            for ev in EVENTS:
                t = s.apply(ev); transitions += 1
                if t.key() == s.key():
                    refusals += 1
                inv = t.invariants()
                if not all(inv.values()):
                    violations.append({'sequence': t.log, 'invariants': inv})
                if t.key() not in seen:
                    seen[t.key()] = t.log; nxt.append(t)
        frontier = nxt
    terminal = [json.loads(k) for k in seen]
    outcomes = {'acknowledged': sum(1 for st in terminal if (st['alloc'] or {}).get('state') == 'acknowledged'), 'returned': sum(1 for st in terminal if (st['alloc'] or {}).get('state') == 'returned'), 'imported_spent': sum(1 for st in terminal if (st['alloc'] or {}).get('spent'))}
    return {'schema': 'metacoin-native-state-model/v0', 'events': EVENTS, 'depth': depth, 'fixture': fixture, 'reachable_states': len(seen), 'transitions': transitions, 'refused_or_idempotent': refusals, 'violations': violations[:20], 'violation_count': len(violations), 'outcomes': outcomes,
            'bounds': 'two domains, one allocation at a time, amounts 1..2 over a fixture of %d, sequences of length <= %d over %d events; not an unbounded proof' % (fixture, depth, len(EVENTS)),
            'invariants': ['single ownership: units held at A plus units held at B never exceed the fixture', 'claims backed: every claim unit at B is backed by a locked unit at A', 'non-negative categories']}


def example_sequences():
    """A bounded set of concrete sequences replayed against the implementation by the test (retries, reorder, crash, double use)."""
    return [['export_1', 'deliver_cert', 'ack'], ['export_1', 'deliver_cert', 'deliver_cert_again', 'ack', 'ack_again'], ['export_2', 'drop_cert', 'deliver_cert', 'ack'], ['export_1', 'deliver_cert', 'spend_at_b', 'return_req'],
            ['export_1', 'deliver_cert', 'return_req', 'deliver_return', 'deliver_return_again'], ['crash_after_reserve', 'deliver_cert', 'ack'], ['export_2', 'deliver_cert', 'double_use', 'return_req', 'deliver_return'], ['export_1', 'deliver_cert', 'ack', 'deliver_cert_again']]


def expected_final(seq, fixture=3):
    s = State(fixture)
    for ev in seq:
        s = s.apply(ev)
    return {'a_avail': s.a_avail, 'a_backing': s.a_backing, 'b_claim_avail': s.b_claim_avail, 'b_claim_in_transit': s.b_claim_in_transit, 'carol': s.carol, 'alloc_state': (s.alloc or {}).get('state')}


if __name__ == '__main__':
    print(json.dumps(explore(), indent=1)[:3000])
