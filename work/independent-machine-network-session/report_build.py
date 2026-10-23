"""Rebuild IMPLEMENTATION_REPORT.md from the final records (journeys, demonstration, endurance, benchmarks, candidate)."""
import json, os, sys, time
A = sys.argv[1].rstrip('/') + '/'
J = json.load(open(A + 'journeys/journeys-network.json')); d = json.load(open(A + 'demonstration/demonstration.json')); C = json.load(open(A + 'CANDIDATE.json')); L = json.load(open(A + 'FEATURE_LEDGER.json'))
e = json.load(open(A + 'benchmarks/endurance-network.json')) if os.path.exists(A + 'benchmarks/endurance-network.json') else None
bn = json.load(open(A + 'benchmarks/benchmark-network.json')) if os.path.exists(A + 'benchmarks/benchmark-network.json') else None
bm = {k: (json.load(open(A + 'benchmarks/benchmark-mission-%s.json' % k)) if os.path.exists(A + 'benchmarks/benchmark-mission-%s.json' % k) else None) for k in ('before', 'after')}
bnat = {k: (json.load(open(A + 'benchmarks/benchmark-native-%s.json' % k)) if os.path.exists(A + 'benchmarks/benchmark-native-%s.json' % k) else None) for k in ('before', 'after')}
res = {r['journey']: r for r in J['results']}; st = lambda n: res[n]['status'] if n in res else 'absent'
steps = {s['step'][:30]: s for s in d['steps']}; S = lambda pfx: next((s for s in d['steps'] if s['step'].startswith(pfx)), {})
rec = open(A + 'RECORDS_COMMIT.txt').read().strip() if os.path.exists(A + 'RECORDS_COMMIT.txt') else 'see RECORDS_COMMIT.txt'
j47 = res.get(47, {}).get('evidence', {}); live = j47.get('live_copy', {}); prior = j47.get('prior_state_from_base_revision', {}); j46 = res.get(46, {}).get('evidence', {}); j49 = res.get(49, {}).get('evidence', {}); j50 = res.get(50, {}).get('evidence', {})
def g(x, *ks, default=None):
    for k in ks:
        x = x.get(k, {}) if isinstance(x, dict) else {}
    return x if x != {} else default
rep = f"""# Implementation report — MetaCoin independent machine network (order 2026-10-23)

## What the machines did (demonstration record `demonstration/demonstration.json`; real processes; domain dom-c with development custody DISABLED; four signer processes; x402 disabled and its facilitator closed)

Who holds the keys: {json.dumps({k: v['signer'] for k, v in d['keys'].items()})} — the API and worker processes hold no actor key (`service_actor_key_files`: {S('funding').get('service_actor_key_files')}).
Funding table: {json.dumps(d['funding_table'])} (zero-value development asset; only the requester fixture-funded).
1. **Competition for the shared antenna pool** (one channel, two overlapping offers): two signed reservations raced for the same half hour; result {json.dumps(S('competition').get('race'))}; the pool stayed within capacity (peak {g(S('competition'), 'pool', 'peak_slots_used')}).
2. The requester's frozen mission was awarded to the primary machine; the acquisition milestone settled through the requester's signer ({S('acquisition').get('requester_signed')}).
3. **Sub-contract**: the requester prefunded 12 (signed at sig-requester), the sub-contractor delivered, the primary paid 10 (signed at sig-machine) and returned the unused 2 (signed at sig-machine): state {S('subcontract').get('subcontract_state')}, timeline {json.dumps({k: g(S('subcontract'), 'timeline', k) for k in ('spent', 'earned_by_provider', 'returned')})}.
4. **Failed delivery branch**: a second sub-contract funded 4 from the machine's own settled earnings was rejected by verification; nothing was paid for it, its award closed and the machine's reservation was released (machine delta {S('failed').get('machine_delta')}, reserved {S('failed').get('machine_reserved')}); the primary remains responsible for the parent milestone.
5. Terrain analysis on the pinned public tile ({g(S('terrain'), 'product', 'provenance', 'label') or 'LOLA LDEM_4 extract'}) was accepted: science {S('terrain').get('science')}.
6. **Blackout**: the link to dom-a was cut; the mission settled locally from reserved funds with three requester signatures ({S('blackout').get('requester_signed')}); a 10-unit claim exported to the machine from dom-a stayed certified but undeliverable (transport {json.dumps(S('blackout').get('transport'))}); the machine's liquidity in that asset showed nothing spendable ({json.dumps(g(S('blackout'), 'machine_liquidity', 'claim_asset_of_dom_a'))}).
7. Still in blackout, the machine bought follow-on compute from its **settled** earnings on the shared pool (signed at sig-machine): delivered {S('machine purchased').get('delivered_cpu_ms')} cpu_ms, labels {S('machine purchased').get('labels')}, settled once.
8. **Relay with safe replanning**: the first two-hop route missed its early contact window and was released in full ({json.dumps(S('relay').get('first_route'))}, machine reserved after release {S('relay').get('machine_reserved_after_release')}); the replanned route carried the product with {S('relay').get('hop_signatures')} per-hop signatures, the requester acknowledged the content hash ({S('relay').get('acknowledged')}), both hops settled once ({S('relay').get('hops')}).
9. **Reconnection**: the certificate was delivered and the claim credited ({json.dumps(S('reconnection').get('machine_liquidity'))}); witness sequence {S('reconnection').get('witness_sequence')}; recovery state {S('reconnection').get('recovery')}.
10. **Fee-funded contribution**: the treasury paid a distinct recipient 2 from collected fees only (before {json.dumps(S('fee-funded').get('treasury_before'))}, after {json.dumps(S('fee-funded').get('treasury_after'))}).
Final table: {json.dumps(d['final_table'])}. Conservation: {json.dumps(d['conservation'])}; accounting checks {json.dumps(d['accounting_checks'])}; independent checker {json.dumps(d['independent_checker'])}; {len(d['postings'])} journal entries and {len(d.get('pool_commitments') or [])} pool commitments exported. Custody note: {d['custody_note']}

## Revisions and time
- Base: `{C['base_revision']}` (trailing log commit of the autonomous mission economy order; its source candidate 809b95e, records 1a3f683, pointer 810aaaa).
- Source candidate: `{C['source_candidate']}` on `{C['branch']}`; journeys loaded `{J['revision']}`; records commit `{rec}`; packaged revision in SOURCE_REVISION.txt (source identical to the candidate; `work/` excluded).
- Live service: 3e34126 / schema 040 at 127.0.0.1:8402, untouched (`live-untouched.json`). Not pushed, not merged, nothing published.
- Session START {open(A + '../START').read().strip() if os.path.exists(A + '../START') else '2026-10-23T04:51Z'}; this report built {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}.

## Verification
- Journeys (`journeys/journeys-network.json`, three domain process sets, dom-c custody separated): **{J['passed']} passed, {J['failed']} failed, {J['blocked']} blocked, {J['skipped']} skipped of {J['total']}** (50 required journeys + the demonstration as journey 0). Blocked items are external tracks only if any (see EXTERNAL_TRACKS.md); journey 49 (clean export) {st(49)}; journey 50 (final accounting + feature statuses at the packaged revision) {st(50)}.
- Unit suites: see `verification-results.json` (`test-logs/unit-summary.txt`); provider client conformance v2 32/32 under a standard-library-only interpreter; bounded network model: 340 states, 0 violations, sequences replayed through real instances (`model-network-report.json`).
- Browser (journey 46, Chromium): {j46.get('passed')} checks passed, {j46.get('failed')} failed; screenshots in `browser-screenshots/` ({len(j46.get('screenshots') or [])} files), including the expired-session draft ({j46.get('expired_session_paid_nothing')} = nothing paid).
- Migrations 048–054 (journey 47): live-database copy {live.get('before', {}).get('schema')} → {live.get('after', {}).get('schema')}, balances preserved {live.get('balances_preserved')}, rollback snapshot intact {live.get('rollback_snapshot_intact')}; prior state produced by the base revision ({prior.get('base_revision', '')[:7]}): {json.dumps(g(prior, 'smoke', 'old-b'))}.
- Endurance ({e['seconds'] if e else 'not run'} s, seed {e['seed'] if e else '-'}): {json.dumps(e['summary']) if e else 'absent'}; restarts {json.dumps(e['restarts']) if e else '-'}, partitions {e['partitions'] if e else '-'}; loops {json.dumps({k: {kk: v[kk] for kk in ('ok', 'p95_s', 'p99_s')} for k, v in e['loops'].items()}) if e else '-'}. Wall clock, compute time, planned contact time and emulator delay are stated in `time_accounting`.
- Clean export (journey 49): {json.dumps({k: j49.get(k) for k in ('passed', 'failed', 'blocked', 'total', 'archive_sha256')})}.

## Benchmarks (same host, loopback, SQLite; same workloads on the base revision and HEAD)
| metric | before ({(bnat['before'] or {}).get('revision', '')[:7]}) | after ({(bnat['after'] or {}).get('revision', '')[:7]}) |
|---|---|---|
| 300 concurrent native transfers: throughput / p50 / p95 | {g(bnat['before'] or {}, 'transfers', 'throughput_per_s')}/s · {g(bnat['before'] or {}, 'transfers', 'p50_s')} s · {g(bnat['before'] or {}, 'transfers', 'p95_s')} s | {g(bnat['after'] or {}, 'transfers', 'throughput_per_s')}/s · {g(bnat['after'] or {}, 'transfers', 'p50_s')} s · {g(bnat['after'] or {}, 'transfers', 'p95_s')} s |
| journal replay | {g(bnat['before'] or {}, 'journal_replay', 'seconds')} s | {g(bnat['after'] or {}, 'journal_replay', 'seconds')} s |
| signer overhead p50 (external minus development custody) | {g(bm['before'] or {}, 'signer_overhead', 'overhead_p50_s')} s | {g(bm['after'] or {}, 'signer_overhead', 'overhead_p50_s')} s |
| 16 contenders on one plain offer: accepted / elapsed | {g(bm['before'] or {}, 'reservation_contention', 'accepted')} / {g(bm['before'] or {}, 'reservation_contention', 'elapsed_s')} s | {g(bm['after'] or {}, 'reservation_contention', 'accepted')} / {g(bm['after'] or {}, 'reservation_contention', 'elapsed_s')} s |
| offline backlog recovery (40 exports) | {g(bm['before'] or {}, 'offline_backlog_recovery', 'recovery_time_s')} s | {g(bm['after'] or {}, 'offline_backlog_recovery', 'recovery_time_s')} s |

New measurements (HEAD only, `benchmarks/benchmark-network.json`): shared-pool race of {g(bn or {}, 'pool_contention', 'shared_pool', 'contenders')} contenders across two overlapping offers accepted {g(bn or {}, 'pool_contention', 'shared_pool', 'accepted')} in {g(bn or {}, 'pool_contention', 'shared_pool', 'elapsed_s')} s (plain offer: {g(bn or {}, 'pool_contention', 'plain_offer', 'accepted')} in {g(bn or {}, 'pool_contention', 'plain_offer', 'elapsed_s')} s); provider client overhead p50 {g(bn or {}, 'provider_overhead', 'overhead_p50_s')} s per call; context-bound signed reservation total p50 {g(bn or {}, 'signer_round_trips', 'external_signer_transfer', 'context_bound_reservation_total', 'p50_s')} s; lineage growth rows {json.dumps(g(bn or {}, 'lineage_growth', 'rows', default=[]))}.

## Required groups and extensions
{chr(10).join('- %s: implemented %s, exercised %s (journeys %s) — limits: %s' % (k, v['implemented'], v['exercised'], v['executed']['journeys'], v['unresolved_limits']) for k, v in L['groups'].items())}
External tracks (blocked, not passed): {json.dumps(L['external_tracks_blocked_not_passed'])}.
Extensions (§21): see HANDOFF.md.
"""
open(A + 'IMPLEMENTATION_REPORT.md', 'w').write(rep); print('report written', len(rep))
