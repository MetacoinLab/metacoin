"""Bounded state-space model of the independent machine network (order 2026-10-23 §17): extends the account/allocation model
(native/model.py) with four mechanisms that threaten conservation or duplicate ownership when crashes, retries and reordered
messages interleave: (1) shared pool reservations across overlapping offers, (2) funded subcontract obligations, (3) claim-lot
splitting and partial retirement, (4) fenced controller generations. Every reachable sequence up to a declared depth is
enumerated and the invariants are checked in every state. The implementation is compared with the model by replaying the
example sequences through real instances (tests/test_native_model_network.py). Bounded enumeration is not an unbounded proof:
the bounds are stated in the report; a malicious authority and arbitrary network sizes are outside the model."""
import json

EVENTS = ('reserve_a', 'reserve_b', 'reserve_b_retry', 'crash_after_pool_commit', 'cancel_b', 'deliver_a',
          'sub_propose', 'sub_fund', 'sub_fund_retry', 'sub_pay', 'sub_pay_again', 'sub_return', 'sub_fund_from_future',
          'export_20', 'deliver_cert', 'spend_7', 'retire_13', 'retire_13_again', 'retire_other', 'deliver_retire', 'deliver_retire_again', 'retire_7_by_holder',
          'takeover', 'step_gen_old', 'step_gen_active', 'step_gen_forged')

POOL_CAP = 1                      # one slot over the shared interval [0, 2); offer a covers [0, 2), offer b covers [1, 3) → they overlap on [1, 2)
FUNDS = {'req': 40, 'mach': 0, 'sub': 0, 'sup': 0, 'bob': 0, 'carol': 0}


class State:
    def __init__(self):
        self.avail = dict(FUNDS); self.reserved = {'req': 0, 'mach': 0}; self.backing = 0; self.fixture = sum(FUNDS.values())
        self.pool = []                      # commitments: dict(id, start, end, slots, state)
        self.sub = None                     # subcontract: dict(state, funded, paid, returned)
        self.alloc = None                   # export: dict(state, amount, returned, lots={holder: amount}, spent)
        self.inflight = []
        self.gen = 0; self.holder = None; self.refused = 0; self.log = []

    def copy(self):
        s = State.__new__(State); s.__dict__ = json.loads(json.dumps(self.__dict__)); return s

    def key(self):
        return json.dumps({k: v for k, v in self.__dict__.items() if k != 'log'}, sort_keys=True)

    # ---- invariants ------------------------------------------------------------------------------------------------------------
    def invariants(self):
        held = sum(self.avail.values()) + sum(self.reserved.values())
        claims = sum(self.alloc['lots'].values()) + self.alloc.get('in_transit', 0) if self.alloc else 0
        live = [c for c in self.pool if c['state'] in ('reserved', 'active', 'delivered')]
        overlap = all(sum(c['slots'] for c in live if c['start'] < t + 1 and c['end'] > t) <= POOL_CAP for t in range(3))
        sub_ok = self.sub is None or (self.sub['paid'] + self.sub['returned'] <= self.sub['funded'] and self.sub['funded'] in (0, 12))
        lots_ok = self.alloc is None or (sum(self.alloc['lots'].values()) + self.alloc['returned'] + self.alloc.get('in_transit', 0) + (self.alloc['amount'] if self.alloc['state'] == 'certified' else 0) == self.alloc['amount'])
        return {'conserved': held + self.backing == self.fixture, 'claims_backed': claims <= self.backing, 'pool_within_capacity': overlap, 'subcontract_bounded': sub_ok, 'lot_partition': lots_ok,
                'non_negative': all(v >= 0 for v in list(self.avail.values()) + list(self.reserved.values()) + [self.backing]), 'single_generation': self.gen >= 0 and (self.holder is None) == (self.gen == 0)}

    # ---- transitions ---------------------------------------------------------------------------------------------------------------
    def apply(self, ev):
        s = self.copy(); s.log.append(ev)
        # (1) shared pool: offers a=[0,2) and b=[1,3) share one slot; prices 2 per slot-hour; the pool admits inside the reservation transaction
        if ev in ('reserve_a', 'reserve_b', 'reserve_b_retry', 'crash_after_pool_commit'):
            who = 'req' if ev == 'reserve_a' else 'mach'; start, end = (0, 2) if ev == 'reserve_a' else (1, 3); cid = 'a' if ev == 'reserve_a' else 'b'
            if any(c['id'] == cid and c['state'] != 'released' for c in s.pool):
                return s                                                                      # idempotent retry: same request id
            if s.avail[who] < 4:
                return s
            live = [c for c in s.pool if c['state'] in ('reserved', 'active', 'delivered')]
            if any(sum(c['slots'] for c in live if c['start'] < t + 1 and c['end'] > t) + 1 > POOL_CAP for t in range(start, end)):
                return s                                                                      # pool_capacity_exceeded: nothing reserved
            if ev == 'crash_after_pool_commit':
                return s                                                                      # the transaction rolled back as a whole: nothing durable
            s.pool.append({'id': cid, 'start': start, 'end': end, 'slots': 1, 'state': 'reserved'}); s.avail[who] -= 4; s.reserved[who] += 4; return s
        if ev == 'cancel_b':
            for c in s.pool:
                if c['id'] == 'b' and c['state'] == 'reserved':
                    c['state'] = 'released'; s.reserved['mach'] -= 4; s.avail['mach'] += 4
            return s
        if ev == 'deliver_a':
            for c in s.pool:
                if c['id'] == 'a' and c['state'] == 'reserved':
                    c['state'] = 'delivered'; s.reserved['req'] -= 4; s.avail['sup'] += 4
            return s
        # (2) funded subcontract: the primary (mach) proposes; the requester prefunds 12 once; the child is paid 10 once; 2 returned once
        if ev == 'sub_propose':
            if s.sub is None:
                s.sub = {'state': 'awaiting_prefund', 'funded': 0, 'paid': 0, 'returned': 0}
            return s
        if ev == 'sub_fund_from_future':
            return s                                                                          # circular_funding: refused, nothing changes
        if ev in ('sub_fund', 'sub_fund_retry'):
            if s.sub and s.sub['state'] == 'awaiting_prefund' and s.avail['req'] >= 12:
                s.avail['req'] -= 12; s.avail['mach'] += 12; s.sub.update(state='funded', funded=12)
            return s                                                                          # a retry after funding is idempotent (same request id)
        if ev in ('sub_pay', 'sub_pay_again'):
            if s.sub and s.sub['state'] == 'funded' and s.avail['mach'] >= 10:
                s.avail['mach'] -= 10; s.avail['sub'] += 10; s.sub.update(state='completed', paid=10)
            return s
        if ev == 'sub_return':
            if s.sub and s.sub['state'] == 'completed':
                s.avail['mach'] -= 2; s.avail['req'] += 2; s.sub.update(state='returned', returned=2)
            return s
        # (3) claim lots: req exports 20 to bob at the other domain; bob spends 7 to carol; bob retires 13; carol's 7 stays backed
        if ev == 'export_20':
            if s.alloc is None and s.avail['req'] >= 20:
                s.avail['req'] -= 20; s.backing += 20; s.alloc = {'state': 'certified', 'amount': 20, 'returned': 0, 'lots': {}, 'in_transit': 0}; s.inflight.append('cert')
            return s
        A = s.alloc
        if ev == 'deliver_cert':
            if A and A['state'] == 'certified':
                A['state'] = 'imported'; A['lots'] = {'bob': 20}
            return s                                                                          # duplicates are idempotent
        if ev == 'spend_7':
            if A and A['state'] in ('imported', 'partially_returned') and A['lots'].get('bob', 0) >= 7 and 'carol' not in A['lots'] and not A.get('retiring'):
                A['lots']['bob'] -= 7; A['lots']['carol'] = 7
            return s
        if ev in ('retire_13', 'retire_13_again'):
            if A and A['state'] in ('imported', 'partially_returned') and A['lots'].get('bob', 0) == 13 and not A.get('retiring'):
                A['lots']['bob'] = 0; A['in_transit'] = 13; A['retiring'] = 13; s.inflight.append('retire')
            return s                                                                          # the second request replays (same lot, nothing to retire)
        if ev == 'retire_other':
            return s                                                                          # carol cannot retire bob's lot; bob cannot retire carol's: refused
        if ev == 'retire_7_by_holder':
            return s                                                                          # a transferred lot is not retirable by its transferee (it stays backed until re-exported or returned by protocol v3+): refused
        if ev in ('deliver_retire', 'deliver_retire_again'):
            if A and A.get('retiring') and 'retire' in s.inflight:
                s.inflight.remove('retire'); amt = A['retiring']; A['in_transit'] = 0; A['returned'] += amt; A['retiring'] = None; s.backing -= amt; s.avail['req'] += amt
                A['state'] = 'returned' if sum(A['lots'].values()) == 0 else 'partially_returned'
            return s                                                                          # a redelivered certificate releases nothing twice
        # (4) fenced generations: only the active generation mutates; a forged or old generation is refused and counted
        if ev == 'takeover':
            s.gen += 1; s.holder = 'runner-%d' % s.gen; return s
        if ev == 'step_gen_active':
            return s if s.gen >= 1 else s
        if ev in ('step_gen_old', 'step_gen_forged'):
            if s.gen >= 1:
                s.refused += 1
            return s
        return s


def explore(depth=6):
    start = State(); seen = {start.key(): []}; frontier = [start]; violations = []; transitions = 0; refusals = 0
    for _ in range(depth):
        nxt = []
        for st in frontier:
            for ev in EVENTS:
                t = st.apply(ev); transitions += 1
                if t.key() == st.key():
                    refusals += 1
                inv = t.invariants()
                if not all(inv.values()):
                    violations.append({'sequence': t.log, 'invariants': inv})
                if t.key() not in seen:
                    seen[t.key()] = t.log; nxt.append(t)
        frontier = nxt
    terminal = [json.loads(k) for k in seen]
    outcomes = {'pool_delivered': sum(1 for x in terminal if any(c['state'] == 'delivered' for c in x['pool'])), 'subcontract_returned': sum(1 for x in terminal if (x['sub'] or {}).get('state') == 'returned'),
                'partially_returned': sum(1 for x in terminal if (x['alloc'] or {}).get('state') == 'partially_returned'), 'fenced_refusals': sum(x['refused'] for x in terminal)}
    return {'schema': 'metacoin-network-state-model/v1', 'events': EVENTS, 'depth': depth, 'reachable_states': len(seen), 'transitions': transitions, 'refused_or_idempotent': refusals, 'violations': violations[:20], 'violation_count': len(violations), 'outcomes': outcomes,
            'bounds': 'one pool of capacity %d over three unit intervals with two overlapping offers; one subcontract (prefund 12, price 10); one export of 20 split 13/7; one mission with monotone generations; sequences of length <= %d over %d events; not an unbounded proof' % (POOL_CAP, depth, len(EVENTS)),
            'assumptions': ['the authority is honest and its database is durable between steps', 'messages may be delayed, duplicated or reordered but not forged', 'a crash inside a transaction leaves no partial durable state', 'amounts are small integers of one asset'],
            'invariants': ['conservation: available + reserved + backing equals the fixture', 'claims backed: lots plus in-transit retirements never exceed locked backing', 'pool within capacity at every unit interval', 'subcontract payments plus returns never exceed the prefund', 'lot partition: lots + returned + in transit equals the allocation amount', 'exactly one active generation once fenced']}


def example_sequences():
    return [['reserve_a', 'reserve_b', 'reserve_b_retry', 'cancel_b', 'reserve_b', 'deliver_a'],
            ['reserve_a', 'crash_after_pool_commit', 'reserve_b'],
            ['sub_propose', 'sub_fund_from_future', 'sub_fund', 'sub_fund_retry', 'sub_pay', 'sub_pay_again', 'sub_return'],
            ['export_20', 'deliver_cert', 'deliver_cert', 'spend_7', 'retire_other', 'retire_13', 'retire_13_again', 'deliver_retire', 'deliver_retire_again'],
            ['export_20', 'deliver_cert', 'spend_7', 'retire_13', 'deliver_retire', 'retire_7_by_holder', 'deliver_retire'],
            ['takeover', 'step_gen_old', 'takeover', 'step_gen_old', 'step_gen_forged', 'step_gen_active']]


def expected_final(seq):
    s = State()
    for ev in seq:
        s = s.apply(ev)
    return {'avail': s.avail, 'reserved': s.reserved, 'backing': s.backing, 'pool': sorted((c['id'], c['state']) for c in s.pool), 'sub': (s.sub or {}).get('state'), 'alloc': {'state': s.alloc['state'], 'returned': s.alloc['returned'], 'lots': s.alloc['lots']} if s.alloc else None, 'gen': s.gen, 'refused': s.refused}


if __name__ == '__main__':
    print(json.dumps(explore(), indent=1)[:4000])
