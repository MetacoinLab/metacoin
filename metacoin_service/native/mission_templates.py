"""Executable mission templates (order 2026-10-23 group C). Each returns a dependency graph the mission controller can run:
  terrain_delivery          acquire (bundled LOLA tile) -> compute route feasibility -> settle; optional storage of the product
  compute_and_relay         reserve compute -> compute -> deliver over a contact reservation -> settle
  evidence_before_followon  compute -> evidence gate (buy a decision-changing acquisition only if useful) -> expensive follow-on terrain product
Prices are template parameters; nothing here funds anything."""
from ..lunar.__main__ import example


def build(name, policy):
    p = dict({'price': 40, 'bps': 500, 'verifier': 3, 'verifier_pay_to': None, 'source': 'apollo11_32', 'storage_kib_seconds': 2048, 'contact_kib': 32, 'compute_cpu_ms': 2000, 'max_unit_price': 1, 'followon_price': 12, 'gate_budget': 10, 'permitted_providers': None, 'followon_provider_account': None}, **{k: v for k, v in policy.items() if k in ('price', 'bps', 'verifier', 'verifier_pay_to', 'source', 'storage_kib_seconds', 'contact_kib', 'compute_cpu_ms', 'max_unit_price', 'followon_price', 'gate_budget', 'permitted_providers', 'followon_provider_account')})
    route = example('route', p['source']); route['private_label'] = 'MISSION_' + name.upper()
    work = {'template': 'lunar_route_feasibility', 'inputs': route, 'price': p['price'], 'bps': p['bps'], 'verifier': p['verifier'], 'verifier_pay_to': p['verifier_pay_to'], 'permitted_providers': p['permitted_providers'], 'verification_class': 'analytical'}
    if name == 'terrain_delivery':
        return {'milestones': [{'key': 'acquire', 'kind': 'work', 'spec': dict(work, template='lunar_terrain_tile', inputs=dict(example('terrain', p['source']), private_label='MISSION_ACQ'), price=max(1, p['price'] // 10), bps=0, verifier=0, verifier_pay_to=None), 'depends': []},
                               {'key': 'settle_acquire', 'kind': 'settle', 'spec': {'of': 'acquire'}, 'depends': ['acquire']},
                               {'key': 'compute', 'kind': 'work', 'spec': work, 'depends': ['acquire']},
                               {'key': 'settle', 'kind': 'settle', 'spec': {'of': 'compute'}, 'depends': ['compute']},
                               {'key': 'store', 'kind': 'resource', 'optional': True, 'spec': {'offer_kind': 'storage', 'quantity': p['storage_kib_seconds'], 'max_price_per_unit': p['max_unit_price']}, 'depends': ['compute']},
                               {'key': 'followon', 'kind': 'resource', 'actor': 'provider', 'spec': dict({'offer_kind': 'compute', 'quantity': p['compute_cpu_ms'], 'max_price_per_unit': p['max_unit_price'], 'buyer': 'provider_of:compute'}, **({'provider_account': p['followon_provider_account']} if p['followon_provider_account'] else {})), 'depends': ['compute']}],
                'notes': 'terrain-analysis delivery: the provider earns on acceptance and buys its follow-on compute from settled earnings'}
    if name == 'compute_and_relay':
        return {'milestones': [{'key': 'reserve_compute', 'kind': 'resource', 'spec': {'offer_kind': 'compute', 'quantity': p['compute_cpu_ms'], 'max_price_per_unit': p['max_unit_price']}, 'depends': []},
                               {'key': 'compute', 'kind': 'work', 'spec': work, 'depends': ['reserve_compute']},
                               {'key': 'reserve_contact', 'kind': 'resource', 'spec': {'offer_kind': 'contact', 'quantity': p['contact_kib'], 'max_price_per_unit': p['max_unit_price']}, 'depends': ['compute']},
                               {'key': 'relay', 'kind': 'deliver', 'spec': {'artifact_from': 'compute', 'via': 'contact', 'reservation_from': 'reserve_contact'}, 'depends': ['reserve_contact']},
                               {'key': 'settle', 'kind': 'settle', 'spec': {'of': 'compute'}, 'depends': ['relay']}],
                'notes': 'compute-and-relay delivery: the result is relayed over a reserved contact window before the work settles'}
    if name == 'evidence_before_followon':
        from ..lunar import evidence as ev
        return {'milestones': [{'key': 'compute', 'kind': 'work', 'spec': work, 'depends': []},
                               {'key': 'gate', 'kind': 'evidence_gate', 'spec': {'route_from': 'compute', 'states': ev.DEFAULT_STATES, 'budget': p['gate_budget']}, 'depends': ['compute']},
                               {'key': 'followon', 'kind': 'work', 'spec': dict(work, template='lunar_terrain_tile', inputs=dict(example('terrain', 'tycho_48'), private_label='MISSION_FOLLOWON'), price=p['followon_price'], bps=0, verifier=0, verifier_pay_to=None), 'depends': ['gate']},
                               {'key': 'settle', 'kind': 'settle', 'spec': {'of': 'followon'}, 'depends': ['followon']}],
                'notes': 'request additional evidence before the expensive follow-on: the gate buys an acquisition only when it can change the decision and proceeds only when the decision is resolved'}
    raise ValueError('template')


TEMPLATES = {'terrain_delivery': 'acquire -> compute (route feasibility on pinned LOLA data) -> settle; optional storage; provider follow-on compute purchase from settled earnings',
             'compute_and_relay': 'reserve compute -> compute -> reserve contact -> relay the product over the emulated link -> settle',
             'evidence_before_followon': 'compute -> evidence gate (decision-changing acquisition only) -> expensive follow-on -> settle'}
