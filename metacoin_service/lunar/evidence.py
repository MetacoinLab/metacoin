"""Evidence acquisition inside the lunar mission (order §13): a declared finite set of plausible terrain/energy states that
disagree on the route decision, expressed as a finite-decision-observability candidate model (the completed decision
package); a measurement menu of eligible extra computations/measurements with quoted costs; the plan selects the view that
separates decision-relevant pairs (useful) or nothing (redundant). The purchase itself goes through the native work path.

Finite-model guarantees are separate from real-world validity: the hypotheses are the declared states, nothing more."""
from . import route as route_mod

COORDS = [{'name': 'slope_bias_mdeg', 'unit': 'mdeg'}, {'name': 'battery_margin_mwh', 'unit': 'mWh'}]


def candidate_model(route_inputs, states, question='is the route feasible under the declared rover model?', label='LUNAR_EVIDENCE'):
    """states: list of {id, slope_bias_milli_deg, battery_delta_mwh, provenance}. Each state's decision label is computed by the
    route product on the perturbed inputs (so the labels are exact consequences of the declared model, not opinions)."""
    hyps = []
    for st in states:
        inp = {k: v for k, v in route_inputs.items() if k != 'private_label'}
        inp = dict(inp, terrain=dict(inp['terrain'], parameters=dict(inp['terrain'].get('parameters', {}), vertical_error_mm=max(0, inp['terrain'].get('parameters', {}).get('vertical_error_mm', 1000) + st.get('vertical_error_delta_mm', 0)))),
                   rover=dict(inp['rover'], max_slope_milli_deg=max(0, inp['rover']['max_slope_milli_deg'] - st.get('slope_bias_milli_deg', 0)), energy_budget_mwh=max(inp['rover']['reserve_mwh'], inp['rover']['energy_budget_mwh'] + st.get('battery_delta_mwh', 0))))
        res = route_mod.compute(inp)
        hyps.append({'id': st['id'], 'label': 'ROUTE_' + res['outcome'], 'state': [st.get('slope_bias_milli_deg', 0), st.get('battery_delta_mwh', 0)], 'provenance': st.get('provenance', 'declared plausible state')})
    return {'schema': 'decision-observability-input/v1', 'model': {'id': 'finite-decision-observability/v1', 'version': 1}, 'candidate_set_version': 'lunar-states-1', 'measurement_menu_version': 'lunar-menu-1',
            'coordinates': COORDS, 'hypotheses': hyps, 'views': [], 'question': question, 'private_label': label}


def menu(expires_at, provider_id=None, slope_cost=4, battery_cost=2, redundant_cost=1):
    """Eligible extra acquisitions: a slope calibration pass (reads the slope bias coordinate), a battery gauge read (battery
    margin), and a redundant view that reads neither coordinate with discriminating precision."""
    m = [{'id': 'slope_calibration_pass', 'rows': [[1, 0]], 'noise': [500], 'output_units': ['mdeg'], 'cost': slope_cost, 'quote_expires_at': expires_at, 'execution_modes': ['local_worker'], 'provenance': 'extra terrain computation at finer vertical error (simulated instrument)'},
         {'id': 'battery_gauge_read', 'rows': [[0, 1]], 'noise': [5_000_000], 'output_units': ['mWh'], 'cost': battery_cost, 'quote_expires_at': expires_at, 'execution_modes': ['local_worker', 'instrument'], 'provenance': 'battery telemetry (simulated)'},
         {'id': 'coarse_slope_redundant', 'rows': [[1, 0]], 'noise': [1_000_000], 'output_units': ['mdeg'], 'cost': redundant_cost, 'quote_expires_at': expires_at, 'execution_modes': ['local_worker'], 'provenance': 'a slope view too noisy (±1000 deg) to separate any declared pair: buying it changes nothing'}]
    if provider_id:
        for x in m:
            x['provider_id'] = provider_id
    return m


DEFAULT_STATES = [{'id': 'nominal', 'slope_bias_milli_deg': 0, 'battery_delta_mwh': 0, 'provenance': 'nominal declared state'},
                  {'id': 'slopes_steeper_than_mapped', 'slope_bias_milli_deg': 14500, 'battery_delta_mwh': 0, 'provenance': 'plausible: grid-scale slopes underestimate local slopes so that the effective limit is 0.5 deg'},
                  {'id': 'battery_degraded', 'slope_bias_milli_deg': 0, 'battery_delta_mwh': -40_000_000, 'provenance': 'plausible: 40 kWh less than the declared budget'}]
