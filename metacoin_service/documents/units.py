"""Narrow unit layer (Order 07 §16): the quantities the energy, time, power and temperature services use, as an explicitly
enumerated conversion table with dimensional checks and exact Decimal arithmetic. Not a symbolic algebra system.

Every conversion keeps original value, original unit, converted value, target unit and the rule. Temperature is affine
(Celsius/Fahrenheit offsets) and is handled explicitly. Conversion to the integer base units of the exact services (mJ,
mW, s, and millikelvin for temperature) is directed: a point value must be exactly representable unless the caller
declares a rounding policy; an interval bound rounds outward (low floors, high ceils) so a constraint never becomes easier."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, InvalidOperation, getcontext
from fractions import Fraction

getcontext().prec = 40


def _fmt(fr):
    """Exact rational -> plain decimal string (no exponent); non-terminating fractions are shown to 12 places and flagged by the caller."""
    if fr.denominator == 1:
        return str(fr.numerator)
    d = Decimal(fr.numerator) / Decimal(fr.denominator)
    return format(d.normalize(), 'f')

# unit -> (dimension, factor to base, offset to base) ; base units: energy mJ, power mW, time s, temperature mK (millikelvin), plain dimensionless
TABLE = {
    'mJ': ('energy', Decimal(1), Decimal(0)), 'J': ('energy', Decimal(1000), Decimal(0)), 'kJ': ('energy', Decimal(1_000_000), Decimal(0)), 'MJ': ('energy', Decimal(10 ** 9), Decimal(0)),
    'mWh': ('energy', Decimal(3600), Decimal(0)), 'Wh': ('energy', Decimal(3_600_000), Decimal(0)), 'kWh': ('energy', Decimal(3_600_000_000), Decimal(0)),
    'mW': ('power', Decimal(1), Decimal(0)), 'W': ('power', Decimal(1000), Decimal(0)), 'kW': ('power', Decimal(1_000_000), Decimal(0)), 'uW': ('power', Decimal('0.001'), Decimal(0)), 'µW': ('power', Decimal('0.001'), Decimal(0)),
    's': ('time', Decimal(1), Decimal(0)), 'ms': ('time', Decimal('0.001'), Decimal(0)), 'min': ('time', Decimal(60), Decimal(0)), 'h': ('time', Decimal(3600), Decimal(0)), 'd': ('time', Decimal(86400), Decimal(0)),
    'K': ('temperature', Decimal(1000), Decimal(0)), 'mK': ('temperature', Decimal(1), Decimal(0)),
    '°C': ('temperature', Decimal(1000), Decimal('273150')), 'C': ('temperature', Decimal(1000), Decimal('273150')), 'degC': ('temperature', Decimal(1000), Decimal('273150')),
    '°F': ('temperature', Fraction(5000, 9), Fraction(273150) - Fraction(32 * 5000, 9)), 'F': ('temperature', Fraction(5000, 9), Fraction(273150) - Fraction(32 * 5000, 9)),
    '': ('dimensionless', Decimal(1), Decimal(0)), '1': ('dimensionless', Decimal(1), Decimal(0)), '%': ('dimensionless', Decimal('0.01'), Decimal(0)),
}
BASE = {'energy': 'mJ', 'power': 'mW', 'time': 's', 'temperature': 'mK', 'dimensionless': ''}
ALIASES = {'millijoule': 'mJ', 'joule': 'J', 'kilojoule': 'kJ', 'milliwatt': 'mW', 'watt': 'W', 'kilowatt': 'kW', 'second': 's', 'seconds': 's', 'sec': 's', 'minute': 'min', 'minutes': 'min', 'hour': 'h', 'hours': 'h', 'hr': 'h',
           'kelvin': 'K', 'celsius': '°C', 'fahrenheit': '°F', 'degrees C': '°C', 'deg C': '°C', 'ohm': 'ohm'}


class UnitError(ValueError):
    pass


def canonical_unit(u):
    if u is None:
        return ''
    u = str(u).strip()
    u = ALIASES.get(u, ALIASES.get(u.lower(), u))
    if u not in TABLE:
        raise UnitError('unknown unit %r (supported: %s)' % (u, ', '.join(sorted(k for k in TABLE if k))))
    return u


def dimension(u):
    return TABLE[canonical_unit(u)][0]


def parse_decimal(text, locale='point'):
    """Exact decimal from a cell string under a DECLARED locale: 'point' (1,250.5 -> 1250.5 with comma thousands) or
    'comma' (1.250,5 -> 1250.5). A string that is ambiguous under the declared locale is still refused: '1,250' is
    ambiguous under 'undeclared' and must not be parsed at all."""
    if text is None:
        raise UnitError('missing value')
    s = str(text).strip().replace('−', '-').replace(' ', '').replace(' ', '')
    if s in ('', '-', '—', '–', 'n/a', 'N/A', 'NA'):
        raise UnitError('not a number: %r' % text)
    if locale == 'undeclared':
        if ',' in s or ('.' in s and s.count('.') > 1):
            raise UnitError('ambiguous numeric locale for %r: declare point or comma decimal convention' % text)
    elif locale == 'point':
        if s.count(',') and s.count('.') <= 1:
            parts = s.replace('-', '').replace('+', '').split('.')
            if not all(len(g) == 3 for g in parts[0].split(',')[1:]):
                raise UnitError('%r is not a valid point-locale number (thousands groups must have 3 digits)' % text)
            s = s.replace(',', '')
    elif locale == 'comma':
        if s.count('.') and s.count(',') <= 1:
            parts = s.replace('-', '').replace('+', '').split(',')
            if not all(len(g) == 3 for g in parts[0].split('.')[1:]):
                raise UnitError('%r is not a valid comma-locale number' % text)
            s = s.replace('.', '')
        s = s.replace(',', '.')
    else:
        raise UnitError('unknown locale policy %r' % locale)
    pct = s.endswith('%')
    if pct:
        s = s[:-1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise UnitError('not a number: %r' % text) from None
    if not d.is_finite():
        raise UnitError('non-finite value refused: %r' % text)
    return d / 100 if pct else d


def convert(value, from_unit, to_unit):
    """Exact Decimal conversion with dimensional check and affine temperature handling."""
    fu, tu = canonical_unit(from_unit), canonical_unit(to_unit)
    (dim_f, f_factor, f_off), (dim_t, t_factor, t_off) = TABLE[fu], TABLE[tu]
    if dim_f != dim_t:
        raise UnitError('dimension mismatch: %s is %s, %s is %s' % (fu, dim_f, tu, dim_t))
    v = Fraction(Decimal(value))
    base = v * Fraction(f_factor) + Fraction(f_off)
    out = (base - Fraction(t_off)) / Fraction(t_factor)
    rule = ('affine: base = value*%s + %s; target = (base - %s)/%s' % (f_factor, f_off, t_off, t_factor)) if (f_off or t_off) else ('multiplicative: value * %s / %s' % (f_factor, t_factor))
    exact = _fmt(out); terminating = (Decimal(out.numerator) / Decimal(out.denominator)) == Fraction(Decimal(exact)) if out.denominator != 1 else True
    return {'original_value': format(Decimal(value).normalize(), 'f'), 'original_unit': fu, 'converted_value': exact, 'exact': bool(terminating), 'target_unit': tu, 'rule': rule, 'dimension': dim_f}


def to_base_integer(value, unit, role='point', rounding='reject'):
    """Integer base units for the exact services. role: point | low | high (interval bound). rounding: reject (default for
    points: a non-integer base value is an error) | outward (low floors, high ceils, points are refused unless exact)."""
    u = canonical_unit(unit)
    dim, factor, off = TABLE[u]
    base = Fraction(Decimal(value)) * Fraction(factor) + Fraction(off)
    exact = base.denominator == 1
    import math
    if exact:
        n = int(base)
    elif role == 'low' and rounding == 'outward':
        n = math.floor(base)
    elif role == 'high' and rounding == 'outward':
        n = math.ceil(base)
    else:
        raise UnitError('%s %s is %s %s in base units: not an integer; declare an outward-rounding interval bound or supply an exact value' % (value, u, _fmt(base), BASE[dim]))
    if abs(n) > 10 ** 15:
        raise UnitError('magnitude beyond the exact services\' range (|%d| > 1e15 %s)' % (n, BASE[dim]))
    return {'value': n, 'unit': BASE[dim], 'dimension': dim, 'exact': exact, 'directed': None if exact else ('floor' if role == 'low' else 'ceil'), 'original': {'value': format(Decimal(value).normalize(), 'f'), 'unit': u}}


UNCERTAINTY_KINDS = ('specification_bound', 'observed_min_max', 'confidence_interval', 'parser_confidence', 'operating_tolerance', 'model_prediction_error')


def interval_from_source(low, high, unit, interpretation):
    """A source range needs a declared interpretation before it can feed robust optimization."""
    if interpretation not in UNCERTAINTY_KINDS:
        raise UnitError('declare the interpretation of the range: one of %s' % ', '.join(UNCERTAINTY_KINDS))
    lo, hi = to_base_integer(low, unit, 'low', 'outward'), to_base_integer(high, unit, 'high', 'outward')
    if lo['value'] > hi['value']:
        raise UnitError('reversed interval')
    return {'low': lo, 'high': hi, 'interpretation': interpretation, 'usable_for_robust_optimization': interpretation in ('specification_bound', 'observed_min_max'),
            'note': 'a confidence interval or tolerance is not a hard bound: robust feasibility under it is a modelling assumption, not a guarantee'}
